"""
page-watch 单元测试

用标准库 unittest，不引入 pytest，保持零第三方依赖。
运行:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pagewatch.analyze import asof_heatmap, calibrate, heatmap, render_heatmap
from pagewatch import notify
from pagewatch.models import Alert, Item, Observation
from pagewatch.runner import (
    top_seen_band,
    _build_alerts,
    _detect_anomaly,
    _new_for_rule,
    _new_in_top,
    _rule_matches,
    _to_local,
)
from pagewatch.store import Store
from pagewatch.watchers.nordstrom_rack import (
    ParseError,
    _build_item,
    _effective_ranking,
    _with_page,
    extract_initial_config,
)


class TestExtractInitialConfig(unittest.TestCase):
    """解析器是最脆弱的一环：页面一改版这里先炸，必须测透"""

    def test_simple(self):
        html = '<script>window.__INITIAL_CONFIG__ = {"a": 1}</script>'
        self.assertEqual(extract_initial_config(html), {"a": 1})

    def test_nested_braces(self):
        html = 'window.__INITIAL_CONFIG__ = {"a": {"b": {"c": [1, 2]}}};more'
        self.assertEqual(extract_initial_config(html)["a"]["b"]["c"], [1, 2])

    def test_brace_inside_string(self):
        """字符串字面量里的大括号绝不能参与配对计数"""
        html = 'window.__INITIAL_CONFIG__ = {"t": "a{b}c}}}"}'
        self.assertEqual(extract_initial_config(html)["t"], "a{b}c}}}")

    def test_escaped_quote_inside_string(self):
        """转义引号不能被当成字符串结束"""
        html = r'window.__INITIAL_CONFIG__ = {"t": "say \"hi\" {", "u": 2}'
        cfg = extract_initial_config(html)
        self.assertEqual(cfg["t"], 'say "hi" {')
        self.assertEqual(cfg["u"], 2)

    def test_trailing_backslash_in_string(self):
        """以反斜杠转义的反斜杠结尾，后面的引号应被视为字符串结束"""
        html = r'window.__INITIAL_CONFIG__ = {"t": "back\\", "u": 3}'
        cfg = extract_initial_config(html)
        self.assertEqual(cfg["t"], "back\\")
        self.assertEqual(cfg["u"], 3)

    def test_missing_marker(self):
        with self.assertRaises(ParseError):
            extract_initial_config("<html>nothing here</html>")

    def test_unclosed(self):
        with self.assertRaises(ParseError):
            extract_initial_config('window.__INITIAL_CONFIG__ = {"a": {"b": 1}')

    def test_not_an_object(self):
        with self.assertRaises(ParseError):
            extract_initial_config('window.__INITIAL_CONFIG__ = [1,2,3]')


class TestWithPage(unittest.TestCase):
    def test_adds_param(self):
        self.assertEqual(_with_page("https://x.com/a?sort=P", 3), "https://x.com/a?sort=P&page=3")

    def test_adds_first_param(self):
        self.assertEqual(_with_page("https://x.com/a", 2), "https://x.com/a?page=2")

    def test_replaces_existing(self):
        self.assertEqual(_with_page("https://x.com/a?page=1&z=2", 5), "https://x.com/a?page=5&z=2")


class TestBuildItem(unittest.TestCase):
    def test_full_record(self):
        it = _build_item("123", {
            "copyProductTitle": "Thing",
            "labelDisplayName": "Brand",
            "webPathAlias": "the-thing",
            "propositions": [{
                "sellingRetailPriceRange": {"min": "19.98", "max": "19.98"},
                "compareAtRetailPriceRange": {"min": "295.00", "max": "295.00"},
            }],
        })
        self.assertEqual(it.price, 19.98)
        self.assertEqual(it.compare_at, 295.0)
        self.assertEqual(it.pct_off, 93)  # 自行计算的折扣
        self.assertEqual(it.url, "https://www.nordstromrack.com/s/the-thing/123")

    def test_prefers_site_pct(self):
        """站点给了折扣区间就用它，不自己算"""
        it = _build_item("1", {
            "copyProductTitle": "T",
            "propositions": [{
                "sellingRetailPriceRange": {"min": "50"},
                "compareAtRetailPriceRange": {"max": "100"},
                "sellingRetailPercentageOffRange": {"min": "40", "max": "40"},
            }],
        })
        self.assertEqual(it.pct_off, 40)

    def test_missing_fields_dont_crash(self):
        it = _build_item("9", {"propositions": []})
        self.assertEqual(it.item_id, "9")
        self.assertEqual(it.title, "(untitled)")
        self.assertIsNone(it.price)
        self.assertIsNone(it.pct_off)

    def test_zero_compare_at_no_divide_by_zero(self):
        it = _build_item("9", {
            "copyProductTitle": "T",
            "propositions": [{
                "sellingRetailPriceRange": {"min": "10"},
                "compareAtRetailPriceRange": {"max": "0"},
            }],
        })
        self.assertIsNone(it.pct_off)


class TestShippability(unittest.TestCase):
    """Rack 在门店模式下会混入"仅限到店自提"的商品，这些网上买不了"""

    @staticmethod
    def _item(pick_only, ship_qty):
        return _build_item("1", {
            "copyProductTitle": "T",
            "propositions": [{
                "sellingRetailPriceRange": {"min": "10"},
                "availability": {"isPickAndNotShip": pick_only, "shipQuantity": ship_qty},
            }],
        })

    def test_normal_item_is_shippable(self):
        self.assertTrue(self._item(False, 30).shippable)

    def test_pick_and_not_ship(self):
        self.assertFalse(self._item(True, 0).shippable)

    def test_zero_ship_quantity_is_not_shippable(self):
        """就算没打 pick-only 标记，寄不出去的也算买不了"""
        self.assertFalse(self._item(False, 0).shippable)

    def test_missing_info_is_unknown_not_false(self):
        """字段缺失时必须是 None，不能臆断成买不了而把好货跳过"""
        it = _build_item("1", {"copyProductTitle": "T", "propositions": [{}]})
        self.assertIsNone(it.shippable)

    def test_tags_and_ship_qty_parsed(self):
        it = _build_item("1", {
            "copyProductTitle": "T",
            "propositions": [{
                "sellingRetailPriceRange": {"min": "7.49"},
                "availability": {"isPickAndNotShip": False, "shipQuantity": 1},
                "enticementTags": [{"type": "NEW_MARKDOWN"}, {"type": "FINAL_SALE"}],
            }],
        })
        self.assertEqual(it.ship_qty, 1)
        self.assertEqual(it.tags, ("NEW_MARKDOWN", "FINAL_SALE"))
        f = it.flags()
        self.assertIn("可直邮仅剩 1 件", f)
        self.assertIn("刚降价", f)
        self.assertIn("不可退", f)

    def test_ample_ship_qty_not_flagged(self):
        """库存充足时不要刷无用提示"""
        it = _build_item("1", {
            "copyProductTitle": "T",
            "propositions": [{
                "sellingRetailPriceRange": {"min": "10"},
                "availability": {"isPickAndNotShip": False, "shipQuantity": 500},
            }],
        })
        self.assertNotIn("可直邮", it.flags())

    def test_inventory_warning_surfaced(self):
        it = _build_item("1", {"copyProductTitle": "T", "inventoryWarning": "low-inventory",
                               "propositions": [{}]})
        self.assertIn("库存紧张", it.flags())

    def test_flags_empty_when_nothing_to_say(self):
        self.assertEqual(self._item(False, 30).flags(), "")

    def test_flags_warn_on_pickup_only(self):
        self.assertIn("仅门店自提", self._item(True, 0).flags())


class TestEffectiveRanking(unittest.TestCase):
    SHIP = Item("A", "Shippable", None, 10.0, 100.0, 90, "u", shippable=True)
    NOSHIP = Item("B", "Pickup only", None, 5.0, 100.0, 95, "u", shippable=False)
    UNKNOWN = Item("C", "Unknown", None, 7.0, 100.0, 93, "u", shippable=None)

    def test_skips_pickup_only(self):
        r = _effective_ranking([self.NOSHIP, self.SHIP], True)
        self.assertEqual([i.item_id for i in r], ["A"])

    def test_keeps_all_when_not_required(self):
        r = _effective_ranking([self.NOSHIP, self.SHIP], False)
        self.assertEqual([i.item_id for i in r], ["B", "A"])

    def test_unknown_is_not_skipped(self):
        """信息缺失不等于买不了，不能因此跳过"""
        r = _effective_ranking([self.UNKNOWN, self.SHIP], True)
        self.assertEqual([i.item_id for i in r], ["C", "A"])

    def test_all_blocked_falls_back(self):
        """全都买不了时也要给出排名，否则榜首变 None 会丢掉监控"""
        self.assertEqual([i.item_id for i in _effective_ranking([self.NOSHIP], True)], ["B"])

    def test_order_preserved(self):
        r = _effective_ranking([self.SHIP, self.NOSHIP, self.UNKNOWN], True)
        self.assertEqual([i.item_id for i in r], ["A", "C"])

    def test_empty(self):
        self.assertEqual(_effective_ranking([], True), [])


class _Row(dict):
    """模拟 sqlite3.Row：取不存在的键返回 None 而不是抛 KeyError"""

    def __getitem__(self, k):
        return dict.get(self, k)


def _obs(total=9000, item=None):
    it = item or Item("A", "Thing", "Brand", 19.98, 295.0, 93, "https://u")
    return Observation(watcher="t", ts=datetime.now(UTC), total_count=total,
                       primary=it, items=[it])


ALERT_CFG = {"primary_changed": True, "primary_price_drop": True,
             "new_arrivals": True, "new_arrivals_min": 1}


class TestAlerts(unittest.TestCase):
    def test_first_run_is_silent(self):
        """首次运行没有基线，绝不能告警"""
        self.assertEqual(_build_alerts(_obs(), None, None, None, ALERT_CFG), [])

    def test_primary_changed(self):
        prev = _Row(primary_item_id="OLD", primary_title="Old", primary_price=30.0,
                    total_count=9000)
        obs = _obs()
        kinds = [a.kind for a in _build_alerts(obs, prev, 0, 0, ALERT_CFG,
                                               new_in_top=[obs.primary])]
        self.assertEqual(kinds, ["PRIMARY_CHANGED"])

    def test_primary_revert_is_silent(self):
        """
        回归：好货抢到榜首后被买走，榜首换回原来的常驻商品 —— 那是回到原状，
        不是新消息。实测 10-24 06:17（Halogen $4.29 96%off）和 06:32（换回
        Adornia）就是这么一对假警报。
        """
        prev = _Row(primary_item_id="GOODDEAL", primary_title="刚卖掉的好货",
                    primary_price=4.29, total_count=9000)
        # new_in_top 为空 = 当前榜首不是新面孔，只是回来了
        kinds = [a.kind for a in _build_alerts(_obs(), prev, 0, 0, ALERT_CFG,
                                               new_in_top=[])]
        self.assertEqual(kinds, [], "榜首回退不该告警")

    def test_primary_revert_can_be_re_enabled(self):
        prev = _Row(primary_item_id="GOODDEAL", primary_title="x",
                    primary_price=4.29, total_count=9000)
        cfg = {**ALERT_CFG, "primary_changed_only_new": False}
        kinds = [a.kind for a in _build_alerts(_obs(), prev, 0, 0, cfg,
                                               new_in_top=[])]
        self.assertEqual(kinds, ["PRIMARY_CHANGED"])

    def test_primary_unchanged_is_silent(self):
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=19.98,
                    total_count=9000)
        self.assertEqual(_build_alerts(_obs(), prev, 0, 0, ALERT_CFG), [])

    def test_price_drop(self):
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=24.98,
                    total_count=9000)
        alerts = _build_alerts(_obs(), prev, 0, 0, ALERT_CFG)
        self.assertEqual([a.kind for a in alerts], ["PRIMARY_PRICE_DROP"])
        self.assertIn("$5.00", alerts[0].title)

    def test_price_increase_is_silent(self):
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=9.98,
                    total_count=9000)
        self.assertEqual(_build_alerts(_obs(), prev, 0, 0, ALERT_CFG), [])

    def test_count_up_is_off_by_default(self):
        """总数上涨噪声太大，默认不能告警，必须显式开启"""
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=19.98,
                    total_count=9000)
        self.assertEqual(_build_alerts(_obs(), prev, 500, 0, ALERT_CFG), [])

    def test_count_up_threshold_when_enabled(self):
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=19.98,
                    total_count=9000)
        cfg = {**ALERT_CFG, "count_up": True, "count_up_min": 100}
        for delta, expected in [(99, []), (100, ["COUNT_UP"]), (-500, [])]:
            kinds = [a.kind for a in _build_alerts(_obs(), prev, delta, 0, cfg)]
            self.assertEqual(kinds, expected, f"delta={delta}")

    def test_new_arrivals_fires_on_single_item(self):
        """用户的核心诉求：只上新 1 件也要能抓到，不能被阈值吃掉"""
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=19.98,
                    total_count=9000)
        alerts = _build_alerts(_obs(), prev, 0, 1, ALERT_CFG, n_brand_new=1)
        self.assertEqual([a.kind for a in alerts], ["NEW_ARRIVALS"])
        self.assertIn("1", alerts[0].title)

    def test_new_arrivals_silent_when_none(self):
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=19.98,
                    total_count=9000)
        self.assertEqual(_build_alerts(_obs(), prev, 0, 0, ALERT_CFG, n_brand_new=0), [])

    def test_new_arrivals_suppressed_during_warmup(self):
        """预热期 item 表还没基线，所有商品都像新品，必须压住"""
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=19.98,
                    total_count=9000)
        alerts = _build_alerts(_obs(), prev, 0, 72, ALERT_CFG,
                               n_brand_new=72, warming_up=True)
        self.assertEqual(alerts, [])

    def test_new_arrivals_respects_min(self):
        prev = _Row(primary_item_id="A", primary_title="Thing", primary_price=19.98,
                    total_count=9000)
        cfg = {**ALERT_CFG, "new_arrivals_min": 5}
        self.assertEqual(_build_alerts(_obs(), prev, 0, 4, cfg, n_brand_new=4), [])
        self.assertEqual(
            [a.kind for a in _build_alerts(_obs(), prev, 0, 5, cfg, n_brand_new=5)],
            ["NEW_ARRIVALS"])

    def test_alerts_can_be_disabled(self):
        prev = _Row(primary_item_id="OLD", primary_title="Old", primary_price=30.0,
                    total_count=9000)
        off = {"primary_changed": False, "count_up": False, "primary_price_drop": False,
               "new_arrivals": False}
        self.assertEqual(_build_alerts(_obs(), prev, 999, 50, off, n_brand_new=50), [])


class TestRuleEngine(unittest.TestCase):
    """自定义规则：在整个抓取范围内筛选，不受排名窗口限制"""

    NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    @staticmethod
    def _it(iid, pct=None, qty=None, price=10.0, tags=()):
        return Item(iid, f"T{iid}", "B", price, 100.0, pct, f"http://u/{iid}",
                    shippable=(qty or 0) > 0, ship_qty=qty, tags=tags)

    def test_the_users_rule(self):
        """≥90% off 且 可直邮 1-3 件"""
        rule = {"min_pct_off": 90, "min_ship_qty": 1, "max_ship_qty": 3}
        items = [
            self._it("hit1", pct=95, qty=1),
            self._it("hit2", pct=90, qty=3),
            self._it("low_pct", pct=89, qty=1),       # 折扣不够
            self._it("too_many", pct=95, qty=4),      # 库存太多，不着急
            self._it("pickup_only", pct=95, qty=0),   # 寄不了
        ]
        got = [i.item_id for i in _rule_matches(items, rule)]
        self.assertEqual(got, ["hit1", "hit2"])

    def test_missing_fields_excluded_not_crash(self):
        rule = {"min_pct_off": 90, "max_ship_qty": 3}
        items = [self._it("no_pct", pct=None, qty=1),
                 self._it("no_qty", pct=95, qty=None)]
        self.assertEqual(_rule_matches(items, rule), [])

    def test_price_bounds(self):
        rule = {"min_price": 10, "max_price": 50}
        items = [self._it("cheap", price=3.99), self._it("ok", price=39.99),
                 self._it("pricey", price=200.0)]
        self.assertEqual([i.item_id for i in _rule_matches(items, rule)], ["ok"])

    def test_require_tags(self):
        rule = {"require_tags": ["NEW_MARKDOWN"]}
        items = [self._it("a", tags=("NEW_MARKDOWN", "FINAL_SALE")),
                 self._it("b", tags=("FINAL_SALE",))]
        self.assertEqual([i.item_id for i in _rule_matches(items, rule)], ["a"])

    def test_sunglasses_rule(self):
        """原价 ≥$250 且现价 ≤$40"""
        rule = {"min_compare_at": 250, "max_price": 40, "min_ship_qty": 1}
        items = [
            Item("hit", "Sunnies", "Moschino", 39.99, 279.0, 86, "u",
                 shippable=True, ship_qty=5),
            Item("too_pricey", "S", "BOSS", 41.98, 480.0, 91, "u",
                 shippable=True, ship_qty=5),       # 现价超了
            Item("cheap_origin", "S", "DIFF", 17.98, 82.0, 78, "u",
                 shippable=True, ship_qty=5),       # 原价不够贵
            Item("pickup", "S", "Chloé", 30.0, 445.0, 93, "u",
                 shippable=False, ship_qty=0),      # 寄不了
        ]
        self.assertEqual([i.item_id for i in _rule_matches(items, rule)], ["hit"])

    def test_brand_rule_case_insensitive(self):
        """站点品牌写法可能是 Gucci / GUCCI，不能因为大小写漏掉"""
        rule = {"brands": ["gucci"], "max_price": 50}
        items = [Item("a", "S", "Gucci", 47.0, 500.0, 90, "u"),
                 Item("b", "S", "GUCCI", 48.0, 500.0, 90, "u"),
                 Item("c", "S", "Chloé", 30.0, 500.0, 94, "u")]
        self.assertEqual([i.item_id for i in _rule_matches(items, rule)], ["a", "b"])

    def test_brand_rule_price_cap(self):
        rule = {"brands": ["Gucci"], "max_price": 50}
        items = [Item("a", "S", "Gucci", 107.48, 465.0, 76, "u")]
        self.assertEqual(_rule_matches(items, rule), [])

    def test_exclude_brands(self):
        rule = {"exclude_brands": ["Gucci"]}
        items = [Item("a", "S", "Gucci", 10.0, 100.0, 90, "u"),
                 Item("b", "S", "Chloé", 10.0, 100.0, 90, "u")]
        self.assertEqual([i.item_id for i in _rule_matches(items, rule)], ["b"])

    def test_missing_brand_not_matched_by_whitelist(self):
        rule = {"brands": ["Gucci"]}
        self.assertEqual(_rule_matches([Item("a", "S", None, 10.0)], rule), [])

    def test_compare_at_missing_excluded(self):
        rule = {"min_compare_at": 250}
        self.assertEqual(_rule_matches([Item("a", "S", "B", 10.0, None)], rule), [])

    def test_empty_rule_matches_everything(self):
        self.assertEqual(len(_rule_matches([self._it("a"), self._it("b")], {})), 2)

    def test_dedup_持续命中只报一次(self):
        """一个条目持续满足规则，不该每 15 分钟报一次"""
        rule = {"min_pct_off": 90}
        items = [self._it("a", pct=95)]
        seen = {"a": self.NOW - timedelta(minutes=15)}
        self.assertEqual(_new_for_rule(items, rule, seen, self.NOW), [])

    def test_dedup_first_run_silent(self):
        """watcher 第一次跑，存量全是"新的"，全报等于刷屏"""
        rule = {"min_pct_off": 90}
        self.assertEqual(
            _new_for_rule([self._it("a", pct=95)], rule, {}, self.NOW,
                          warmed_up=False), [])

    def test_never_matched_rule_still_fires(self):
        """
        回归：一条至今从未命中过的规则，它的记忆永远是空的。
        如果拿"记忆为空"当预热判据，这条规则就再也不会响 —— 等于白写。
        墨镜、Save The Duck 那几条规则建好时就是 0 命中。
        """
        rule = {"min_compare_at": 250, "max_price": 40}
        item = Item("deal", "Sunnies", "Moschino", 39.99, 279.0, 86, "u",
                    shippable=True, ship_qty=5)
        got = _new_for_rule([item], rule, {}, self.NOW, warmed_up=True)
        self.assertEqual([i.item_id for i in got], ["deal"])

    def test_dedup_new_item_alerts(self):
        rule = {"min_pct_off": 90}
        seen = {"old": self.NOW - timedelta(minutes=15)}
        got = _new_for_rule([self._it("old", pct=95), self._it("fresh", pct=95)],
                            rule, seen, self.NOW)
        self.assertEqual([i.item_id for i in got], ["fresh"])

    def test_dedup_respects_cooldown(self):
        rule = {"min_pct_off": 90, "reentry_hours": 24}
        items = [self._it("a", pct=95)]
        self.assertEqual(
            _new_for_rule(items, rule, {"a": self.NOW - timedelta(hours=48)},
                          self.NOW)[0].item_id, "a")
        self.assertEqual(
            _new_for_rule(items, rule, {"a": self.NOW - timedelta(hours=12)},
                          self.NOW), [])

    def test_alert_body_lists_hits(self):
        cfg = {"primary_changed": False, "primary_price_drop": False}
        hits = [self._it("x", pct=92, qty=1, price=19.99),
                self._it("y", pct=90, qty=2, price=47.49)]
        alerts = _build_alerts(_obs(), _Row(primary_item_id="A", total_count=1),
                               0, 0, cfg, rule_hits={"深折扣薄库存": hits})
        self.assertEqual([a.kind for a in alerts], ["RULE_MATCH"])
        self.assertIn("深折扣薄库存", alerts[0].title)
        self.assertIn("2 件", alerts[0].title)
        self.assertEqual(alerts[0].url, "http://u/x", "链接指向最便宜的")

    def test_rules_are_independent(self):
        """多条规则各报各的，互不合并"""
        cfg = {"primary_changed": False, "primary_price_drop": False}
        alerts = _build_alerts(
            _obs(), _Row(primary_item_id="A", total_count=1), 0, 0, cfg,
            rule_hits={"A规则": [self._it("x", pct=95)],
                       "B规则": [self._it("y", pct=95)]})
        self.assertEqual(len(alerts), 2)
        self.assertEqual({a.title for a in alerts}, {"A规则", "B规则"})


class TestTopNReentry(unittest.TestCase):
    """
    回归测试：用户实测发现的假警报。

    前 5 名挤进一件新货 X，原来的第 5 名 E 被推到第 6；X 卖掉后 E 回到第 5 名。
    只跟上一次快照比的话 E 会被当成"新面孔"再报一次 —— 但 E 根本没变化。
    """

    NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    @staticmethod
    def _obs_with(ids):
        items = [Item(i, f"T{i}", "B", 10.0, 100.0, 90, f"http://u/{i}") for i in ids]
        o = Observation(watcher="t", ts=TestTopNReentry.NOW, total_count=1,
                        primary=items[0], items=items)
        o.ranked_ids = list(ids)
        return o

    def _seen(self, ids, minutes_ago=5):
        return {i: self.NOW - timedelta(minutes=minutes_ago) for i in ids}

    def test_the_reported_false_alarm(self):
        """E 被挤到第 6 又回到第 5，不该告警"""
        # 记忆里 A-E 都在前 10 名（E 在第 6 名期间也一直被刷新）
        seen = self._seen(["A", "B", "C", "D", "E", "X"])
        obs = self._obs_with(["A", "B", "C", "D", "E"])
        self.assertEqual(_new_in_top(obs, seen, self.NOW, 5), [],
                         "X 卖掉后 E 回到前 5，不是新面孔")

    def test_genuinely_new_item_still_alerts(self):
        seen = self._seen(["A", "B", "C", "D", "E"])
        obs = self._obs_with(["Z", "A", "B", "C", "D"])
        got = [i.item_id for i in _new_in_top(obs, seen, self.NOW, 5)]
        self.assertEqual(got, ["Z"])

    def test_multiple_entrants_then_all_sold(self):
        """一次进来 3 件新货，把原前 5 挤到 6-8 名；它们卖光后原班人马回来不该告警"""
        seen = self._seen(["A", "B", "C", "D", "E", "X", "Y", "Z"])
        obs = self._obs_with(["A", "B", "C", "D", "E"])
        self.assertEqual(_new_in_top(obs, seen, self.NOW, 5), [])

    def test_long_absent_item_alerts_again(self):
        """隔了很久才重新进前 5，算新消息（比如它大幅降价了）"""
        seen = {"A": self.NOW - timedelta(hours=48),
                "B": self.NOW - timedelta(minutes=5)}
        obs = self._obs_with(["A", "B"])
        got = [i.item_id for i in _new_in_top(obs, seen, self.NOW, 5, reentry_hours=24)]
        self.assertEqual(got, ["A"])

    def test_camper_never_alerts(self):
        """长期霸榜的常驻选手不该反复打扰"""
        seen = self._seen(["A"])
        obs = self._obs_with(["A"])
        self.assertEqual(_new_in_top(obs, seen, self.NOW, 5), [])

    def test_first_run_is_silent(self):
        """记忆为空时无从判断新旧，全报等于刷屏"""
        obs = self._obs_with(["A", "B", "C"])
        self.assertEqual(_new_in_top(obs, {}, self.NOW, 5), [])

    def test_band_is_always_wider_than_alert_window(self):
        """
        记忆范围必须宽于告警窗口，否则被挤出前 N 名的条目记录会断，
        回来时被误判成新面孔。改 top_n 不能破坏这个约束。
        """
        for n in (1, 5, 15, 30, 72):
            self.assertGreater(top_seen_band(n), n, f"top_n={n}")

    def test_entrant_beyond_n_ignored(self):
        seen = self._seen(["A", "B", "C", "D", "E"])
        obs = self._obs_with(["A", "B", "C", "D", "E", "Z"])
        self.assertEqual(_new_in_top(obs, seen, self.NOW, 5), [])


class TestTopNNew(unittest.TestCase):
    """
    前 N 名新面孔的**告警呈现**（"谁是新面孔"的判定在 TestTopNReentry）。

    动机：榜首会被"折扣标得高但没人要"的商品长期霸占，真好货进来时夺不了冠
    却很快被买走。只盯第 0 名会一直错过它们。
    """

    CFG = {"top_n_new": True, "top_n": 5, "primary_changed": False,
           "primary_price_drop": False}

    @staticmethod
    def _it(i, price=10.0):
        return Item(i, f"T{i}", "B", price, 100.0, 90, f"http://u/{i}")

    def _alerts(self, new_in_top):
        prev = _Row(primary_item_id="A", total_count=1)
        return _build_alerts(_obs(), prev, 0, 0, self.CFG, new_in_top=new_in_top)

    def test_single_entrant(self):
        alerts = self._alerts([self._it("Z")])
        self.assertEqual([a.kind for a in alerts], ["TOP_N_NEW"])
        self.assertIn("TZ", alerts[0].body.replace(" ", ""))
        self.assertTrue(alerts[0].urgent, "好货要带声音")

    def test_nothing_new_is_silent(self):
        self.assertEqual(self._alerts([]), [])

    def test_multiple_merged_into_one(self):
        alerts = self._alerts([self._it("X", 3.0), self._it("Y", 7.0)])
        self.assertEqual(len(alerts), 1, "多件合并成一条，不要刷屏")
        self.assertIn("2 件", alerts[0].title)
        self.assertEqual(alerts[0].url, "http://u/X", "链接指向最便宜的那件")

    def test_flags_included(self):
        it = Item("Z", "T", "B", 5.0, 100.0, 95, "http://u/Z",
                  shippable=True, ship_qty=1, tags=("NEW_MARKDOWN",))
        body = self._alerts([it])[0].body
        self.assertIn("可直邮仅剩 1 件", body)
        self.assertIn("刚降价", body)

    def test_disabled_by_default(self):
        cfg = {k: v for k, v in self.CFG.items() if k != "top_n_new"}
        prev = _Row(primary_item_id="A", total_count=1)
        self.assertEqual(
            _build_alerts(_obs(), prev, 0, 0, cfg, new_in_top=[self._it("Z")]), [])


class TestSchemaMigration(unittest.TestCase):
    """
    迁移列清单从 SCHEMA 自动推导。

    回归：以前手写第二份清单，往 SCHEMA 加了列却忘了同步清单，
    新库正常、旧库缺列，直到运行时 INSERT 才炸。实际踩过两次。
    """

    def test_every_schema_column_is_migratable(self):
        from pagewatch.store import SCHEMA, _schema_columns
        parsed = _schema_columns(SCHEMA)
        self.assertIn("snapshot", parsed)
        names = {c for c, _ in parsed["snapshot"]}
        for must in ("http_status", "pages_fetched", "anomalous", "top_ids",
                     "n_new_in_top", "n_brand_new"):
            self.assertIn(must, names, f"SCHEMA 里的 {must} 没被解析出来")

    def test_table_constraints_are_skipped(self):
        from pagewatch.store import _schema_columns
        sql = (
            "CREATE TABLE IF NOT EXISTS t (\n"
            "    a TEXT NOT NULL,   -- 注释\n"
            "    b INTEGER,\n"
            "    PRIMARY KEY (a, b)\n"
            ");"
        )
        self.assertEqual([c for c, _ in _schema_columns(sql)["t"]], ["a", "b"])

    def test_old_db_gets_new_columns(self):
        """模拟旧库：建一个缺列的 snapshot，打开 Store 后应被补齐"""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY, watcher TEXT,"
                        " ts_utc TEXT, ts_local TEXT, dow INTEGER, hour INTEGER)")
            con.commit()
            con.close()
            store = Store(path)
            try:
                cols = {r[1] for r in
                        store._conn.execute("PRAGMA table_info(snapshot)")}
                self.assertIn("http_status", cols)
                self.assertIn("anomalous", cols)
            finally:
                store.close()


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_roundtrip(self):
        obs = _obs(total=9000)
        local = _to_local(obs.ts, None)
        self.store.record(obs, local, count_delta=None, n_new=None, n_gone=None, n_brand_new=None, primary_changed=False, track_items=True)
        last = self.store.last_snapshot("t")
        self.assertEqual(last["total_count"], 9000)
        self.assertEqual(last["primary_item_id"], "A")
        self.assertEqual(json.loads(last["item_ids"]), ["A"])
        self.assertEqual(self.store.known_item_ids("t"), {"A"})

    def test_item_upsert_keeps_first_seen_and_best_rank(self):
        it = Item("A", "Thing", "Brand", 19.98, 295.0, 93, "u")
        other = Item("B", "Other", "Brand", 5.0, 50.0, 90, "u2")
        o1 = Observation(watcher="t", ts=datetime.now(UTC), total_count=1,
                         primary=other, items=[other, it])  # A 排第 2
        self.store.record(o1, _to_local(o1.ts, None), count_delta=None, n_new=None, n_gone=None, n_brand_new=None, primary_changed=False, track_items=True)
        first_seen = self.store._conn.execute(
            "SELECT first_seen, best_rank FROM item WHERE item_id='A'").fetchone()

        o2 = Observation(watcher="t", ts=datetime.now(UTC), total_count=1,
                         primary=it, items=[it, other])  # A 升到第 1
        self.store.record(o2, _to_local(o2.ts, None), count_delta=None, n_new=None, n_gone=None, n_brand_new=None, primary_changed=True, track_items=True)
        after = self.store._conn.execute(
            "SELECT first_seen, best_rank FROM item WHERE item_id='A'").fetchone()

        self.assertEqual(after["first_seen"], first_seen["first_seen"], "first_seen 不应被覆盖")
        self.assertEqual(after["best_rank"], 0, "best_rank 应取历史最好名次")

    def test_failure_is_recorded_but_not_used_as_baseline(self):
        obs = _obs(total=9000)
        self.store.record(obs, _to_local(obs.ts, None), count_delta=None, n_new=None, n_gone=None, n_brand_new=None, primary_changed=False, track_items=True)
        now = datetime.now(UTC)
        self.store.record_failure("t", now, _to_local(now, None), "boom")
        last = self.store.last_snapshot("t")
        self.assertEqual(last["total_count"], 9000, "失败记录不能成为 diff 基线")
        self.assertEqual(len(self.store.snapshots("t")), 2, "但失败本身要留痕")

    def test_track_items_false_skips_item_table(self):
        obs = _obs()
        self.store.record(obs, _to_local(obs.ts, None), count_delta=None, n_new=None, n_gone=None, n_brand_new=None, primary_changed=False, track_items=False)
        self.assertEqual(self.store.known_item_ids("t"), set())

    def test_watchers_are_isolated(self):
        o1 = _obs()
        o1.watcher = "w1"
        o2 = _obs(total=5)
        o2.watcher = "w2"
        for o in (o1, o2):
            self.store.record(o, _to_local(o.ts, None), count_delta=None, n_new=None, n_gone=None, n_brand_new=None, primary_changed=False, track_items=True)
        self.assertEqual(self.store.last_snapshot("w1")["total_count"], 9000)
        self.assertEqual(self.store.last_snapshot("w2")["total_count"], 5)

    def test_snapshot_count_ignores_failures(self):
        obs = _obs()
        self.store.record(obs, _to_local(obs.ts, None), count_delta=None, n_new=None, n_gone=None, n_brand_new=None, primary_changed=False, track_items=True)
        now = datetime.now(UTC)
        self.store.record_failure("t", now, _to_local(now, None), "boom")
        self.assertEqual(self.store.snapshot_count("t"), 1)

    def test_brand_new_persisted(self):
        obs = _obs()
        self.store.record(obs, _to_local(obs.ts, None), count_delta=3, n_new=1, n_gone=1, n_brand_new=7, primary_changed=False, track_items=True)
        self.assertEqual(self.store.last_snapshot("t")["n_brand_new"], 7)

    def test_anomaly_reason_persisted(self):
        """被挡的原因必须留痕，否则事后无从分析是不是被限流"""
        obs = _obs(total=400)
        self.store.record(obs, _to_local(obs.ts, None), anomalous=True,
                          anomaly_reason="总数 400 偏离近期中位数 6,950 达 94%",
                          track_items=False)
        row = self.store._conn.execute(
            "SELECT error, anomalous FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["anomalous"], 1)
        self.assertIn("94%", row["error"])

    def test_http_status_persisted(self):
        obs = _obs()
        obs.http_status = 200
        obs.pages_fetched = 2
        self.store.record(obs, _to_local(obs.ts, None), track_items=False)
        row = self.store.last_snapshot("t")
        self.assertEqual(row["http_status"], 200)
        self.assertEqual(row["pages_fetched"], 2)

    def test_events(self):
        now = datetime.now(UTC)
        self.store.record_events("t", now, _to_local(now, None),
                                 [Alert("K", "title", "body", "u", True, "A")], True)
        evs = self.store.events("t")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["kind"], "K")


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed(self, deltas):
        for d in deltas:
            obs = _obs()
            self.store.record(obs, _to_local(obs.ts, None), count_delta=d, n_new=0, n_gone=0, n_brand_new=0, primary_changed=False, track_items=False)

    def test_calibrate_empty(self):
        c = calibrate(self.store, "t")
        self.assertFalse(c["ready"])
        self.assertIn("message", c)

    def test_calibrate_recommends_above_noise(self):
        """噪声在 ±10 内、偶有 +80 的真上新时，阈值应落在两者之间"""
        self._seed([1, -2, 3, -5, 2, 8, -9, 4, -1, 0] * 5 + [80, 95])
        c = calibrate(self.store, "t")
        rec = c["recommended_count_up_min"]
        self.assertGreater(rec, 10, "阈值必须高过日常噪声")
        self.assertLessEqual(rec, 95, "阈值不能高到把真上新也滤掉")

    def test_heatmap_brand_new_is_default(self):
        obs = _obs()
        self.store.record(obs, _to_local(obs.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=4, primary_changed=False, track_items=False)
        hm = heatmap(self.store, "t")
        self.assertEqual(hm["metric"], "brand_new")
        self.assertEqual(sum(hm["grid"].values()), 4)

    def test_heatmap_new_markdown(self):
        it = Item("A", "T", None, 10.0, 100.0, 90, "u", tags=("NEW_MARKDOWN",))
        plain = Item("B", "T2", None, 10.0, 100.0, 90, "u")
        obs = Observation(watcher="t", ts=datetime.now(UTC), total_count=1,
                          primary=it, items=[it, plain])
        self.store.record(obs, _to_local(obs.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=0, primary_changed=False, track_items=True)
        hm = heatmap(self.store, "t", metric="new_markdown")
        self.assertEqual(sum(hm["grid"].values()), 1)

    def test_heatmap_top_change(self):
        """榜首变化次数是 Newest watcher 的上新标记，必须按次数而非数值累加"""
        for changed in (True, False, True):
            obs = _obs()
            self.store.record(obs, _to_local(obs.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=0, primary_changed=changed, track_items=False)
        hm = heatmap(self.store, "t", metric="top_change")
        self.assertEqual(sum(hm["grid"].values()), 2)

    def test_heatmap_renders(self):
        self._seed([5, -3, 20])
        out = render_heatmap(heatmap(self.store, "t", metric="up"))
        self.assertIn("Mon", out)
        self.assertIn("?", out)  # 未采样的时段

    def test_heatmap_empty(self):
        self.assertEqual(render_heatmap(heatmap(self.store, "t")), "(无数据)")


class TestDetectAnomaly(unittest.TestCase):
    """站点实测会偶发返回残缺结果集（6950 件 → 400 件），必须挡住"""

    BASE = [6950, 6948, 6952, 6949, 6951]

    def test_real_world_degraded_response(self):
        self.assertIsNotNone(_detect_anomaly(400, self.BASE, 0.5))

    def test_normal_jitter_passes(self):
        for total in (6940, 6960, 6900, 7000):
            self.assertIsNone(_detect_anomaly(total, self.BASE, 0.5), f"total={total}")

    def test_large_but_plausible_restock_passes(self):
        """真实大批量上架 +30% 不能被误杀"""
        self.assertIsNone(_detect_anomaly(9035, self.BASE, 0.5))

    def test_doubling_is_rejected(self):
        self.assertIsNotNone(_detect_anomaly(20000, self.BASE, 0.5))

    def test_insufficient_history_accepts_everything(self):
        """样本不足时不做判断，否则冷启动会把一切都当异常"""
        self.assertIsNone(_detect_anomaly(400, [6950, 6948], 0.5))

    def test_missing_total_is_anomalous(self):
        self.assertIsNotNone(_detect_anomaly(None, self.BASE, 0.5))


class TestAnomalyIsolation(unittest.TestCase):
    """异常快照不能成为 diff 基线，也不能进 item 表"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_anomalous_excluded_from_baseline(self):
        good = _obs(total=6950)
        self.store.record(good, _to_local(good.ts, None), primary_changed=False,
                          track_items=True)
        bad = _obs(total=400, item=Item("ZZZ", "Junk", None, 1.0, 2.0, 50, "u"))
        self.store.record(bad, _to_local(bad.ts, None), primary_changed=False,
                          track_items=False, anomalous=True)

        last = self.store.last_snapshot("t")
        self.assertEqual(last["total_count"], 6950, "异常快照不能当基线")
        self.assertEqual(self.store.recent_counts("t"), [6950])
        self.assertNotIn("ZZZ", self.store.known_item_ids("t"), "异常数据不能进 item 表")
        self.assertEqual(self.store.snapshot_count("t"), 1)
        self.assertEqual(len(self.store.snapshots("t")), 2, "但异常本身要留痕")


class TestEmailPassword(unittest.TestCase):
    """密码优先从钥匙串取，避免 Gmail 应用专用密码明文落盘"""

    def test_keychain_preferred_over_plaintext(self):
        cfg = {"username": "a@b.com", "password": "PLAINTEXT", "use_keychain": True}
        with patch("pagewatch.notify.keychain_get", return_value="FROMKEYCHAIN"):
            self.assertEqual(notify.resolve_password(cfg), "FROMKEYCHAIN")

    def test_falls_back_to_plaintext(self):
        cfg = {"username": "a@b.com", "password": "PLAINTEXT", "use_keychain": True}
        with patch("pagewatch.notify.keychain_get", return_value=None):
            self.assertEqual(notify.resolve_password(cfg), "PLAINTEXT")

    def test_keychain_can_be_disabled(self):
        cfg = {"username": "a@b.com", "password": "PLAINTEXT", "use_keychain": False}
        with patch("pagewatch.notify.keychain_get", return_value="FROMKEYCHAIN") as m:
            self.assertEqual(notify.resolve_password(cfg), "PLAINTEXT")
            m.assert_not_called()

    def test_no_password_anywhere(self):
        with patch("pagewatch.notify.keychain_get", return_value=None):
            self.assertIsNone(notify.resolve_password({"username": "a@b.com"}))

    def test_send_email_reports_missing_password(self):
        cfg = {"host": "smtp.gmail.com", "username": "a@b.com",
               "from": "a@b.com", "to": "a@b.com"}
        with patch("pagewatch.notify.keychain_get", return_value=None):
            err = notify.send_email([Alert("K", "t", "b")], cfg)
        self.assertIsNotNone(err)
        self.assertIn("password", err)

    def test_app_password_shape(self):
        self.assertTrue(notify.looks_like_app_password("abcdefghijklmnop"))
        self.assertFalse(notify.looks_like_app_password("short"))
        self.assertFalse(notify.looks_like_app_password("ABCDEFGHIJKLMNOP"))
        self.assertFalse(notify.looks_like_app_password("abcd1234efgh5678"))

    def test_spaces_stripped_from_password(self):
        """Google 展示的是 'abcd efgh ijkl mnop'，直接粘贴会带空格"""
        cfg = {"username": "a@b.com", "password": "abcd efgh ijkl mnop",
               "use_keychain": False}
        self.assertEqual(notify.resolve_password(cfg), "abcdefghijklmnop")

    def test_gmail_bad_password_caught_before_connecting(self):
        """密码形态不对时直接拦下，不要让用户面对 'Connection unexpectedly closed'"""
        cfg = {"host": "smtp.gmail.com", "username": "a@b.com", "from": "a@b.com",
               "to": "a@b.com", "password": "LoginPass1", "use_keychain": False}
        with patch("pagewatch.notify.smtplib.SMTP") as m:
            err = notify.send_email([Alert("K", "t", "b")], cfg)
            m.assert_not_called()
        self.assertIn("16 位", err)
        self.assertIn("apppasswords", err)

    def test_non_gmail_host_not_format_checked(self):
        """只有 Gmail 才有这个形态要求，别的 SMTP 不能误拦"""
        cfg = {"host": "smtp.company.com", "username": "a@b.com", "from": "a@b.com",
               "to": "a@b.com", "password": "AnyPass123", "use_keychain": False}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, u, p): pass
            def send_message(self, msg, to_addrs=None): pass

        with patch("pagewatch.notify.smtplib.SMTP", FakeSMTP):
            self.assertIsNone(notify.send_email([Alert("K", "t", "b")], cfg))

    def test_send_email_self_addressed_builds_fine(self):
        """自己发给自己：from 和 to 相同不应有任何特殊处理"""
        cfg = {"host": "smtp.x", "username": "a@b.com", "from": "a@b.com",
               "to": "a@b.com", "password": "abcdefghijklmnop", "use_keychain": False}
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, u, p): sent["login"] = (u, p)
            def send_message(self, msg, to_addrs=None):
                sent["to_addrs"] = to_addrs
                sent["from"] = msg["From"]

        with patch("pagewatch.notify.smtplib.SMTP", FakeSMTP):
            err = notify.send_email([Alert("K", "标题", "正文", "http://u")], cfg)
        self.assertIsNone(err)
        self.assertEqual(sent["to_addrs"], ["a@b.com"])
        self.assertEqual(sent["from"], "a@b.com")
        self.assertEqual(sent["login"], ("a@b.com", "abcdefghijklmnop"))


class TestAsOfHeatmap(unittest.TestCase):
    """asOf 是唯一能看到夜间活动的途径：商品自带上次重新定价的时刻"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _record(self, stamps):
        items = [Item(f"I{i}", f"T{i}", None, 1.0, 2.0, 50, "u", as_of=a)
                 for i, a in enumerate(stamps)]
        obs = Observation(watcher="t", ts=datetime.now(UTC), total_count=1,
                          primary=items[0], items=items)
        self.store.record(obs, _to_local(obs.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=0, primary_changed=False, track_items=True)

    def test_night_hours_are_visible(self):
        """03:00 UTC = 前一天 23:00 纽约时间 —— 电脑睡着的时段也要统计到"""
        self._record(["2026-10-22T03:30:00.000Z"] * 3)
        hm = asof_heatmap(self.store, "t", "America/New_York")
        self.assertEqual(hm["grid"].get((2, 23)), 3)  # 周三 23 点

    def test_each_item_counted_once_across_polls(self):
        """连续抓取看到同一批未变化的商品，不能重复累加"""
        for _ in range(5):
            self._record(["2026-10-22T03:30:00.000Z", "2026-10-22T04:30:00.000Z"])
        hm = asof_heatmap(self.store, "t", "America/New_York")
        self.assertEqual(sum(hm["grid"].values()), 2)

    def test_history_accumulates_not_overwritten(self):
        """同一件商品被重新定价后，新旧两个时刻都要保留 —— 这是"跑几天"的前提"""
        items = [Item("SAME", "T", None, 1.0, 2.0, 50, "u", as_of="2026-10-22T03:30:00.000Z")]
        obs = Observation(watcher="t", ts=datetime.now(UTC), total_count=1,
                          primary=items[0], items=items)
        self.store.record(obs, _to_local(obs.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=0, primary_changed=False, track_items=True)
        items2 = [Item("SAME", "T", None, 1.0, 2.0, 50, "u", as_of="2026-10-23T08:30:00.000Z")]
        obs2 = Observation(watcher="t", ts=datetime.now(UTC), total_count=1,
                           primary=items2[0], items=items2)
        self.store.record(obs2, _to_local(obs2.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=0, primary_changed=False, track_items=True)

        hm = asof_heatmap(self.store, "t", "America/New_York")
        self.assertEqual(sum(hm["grid"].values()), 2, "旧的 asOf 不能被覆盖掉")
        self.assertEqual(hm["grid"].get((2, 23)), 1)  # 周三 23 点
        self.assertEqual(hm["grid"].get((4, 4)), 1)   # 周五 04 点

    def test_anomalous_snapshot_does_not_pollute_asof(self):
        items = [Item("JUNK", "J", None, 1.0, 2.0, 50, "u", as_of="2026-10-22T03:30:00.000Z")]
        obs = Observation(watcher="t", ts=datetime.now(UTC), total_count=400,
                          primary=items[0], items=items)
        self.store.record(obs, _to_local(obs.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=0, primary_changed=False, track_items=False, anomalous=True)
        self.assertEqual(sum(asof_heatmap(self.store, "t").get("grid", {}).values()), 0)

    def test_asof_recorded_even_when_track_items_off(self):
        """asOf 体积极小又是夜间唯一信息源，不该被 track_all_items 关掉"""
        items = [Item("X", "T", None, 1.0, 2.0, 50, "u", as_of="2026-10-22T03:30:00.000Z")]
        obs = Observation(watcher="t", ts=datetime.now(UTC), total_count=1,
                          primary=items[0], items=items)
        self.store.record(obs, _to_local(obs.ts, None), count_delta=0, n_new=0, n_gone=0, n_brand_new=0, primary_changed=False, track_items=False)
        self.assertEqual(self.store.known_item_ids("t"), set())
        self.assertEqual(len(self.store.as_of_stamps("t")), 1)

    def test_no_unsampled_markers(self):
        """asOf 模式下不存在'未采样'，渲染不该出现 ?"""
        self._record(["2026-10-22T03:30:00.000Z"])
        out = render_heatmap(asof_heatmap(self.store, "t", "America/New_York"))
        self.assertNotIn("?", out)
        self.assertIn("夜间同样可见", out)

    def test_unparsable_counted_not_crashed(self):
        self._record(["garbage", "2026-10-22T03:30:00.000Z"])
        hm = asof_heatmap(self.store, "t", "America/New_York")
        self.assertEqual(hm["n_unparsable"], 1)
        self.assertEqual(sum(hm["grid"].values()), 1)

    def test_empty(self):
        hm = asof_heatmap(self.store, "t")
        self.assertEqual(sum(hm["grid"].values()), 0)


class TestAlertSound(unittest.TestCase):
    """
    提示音走 afplay，不走通知自带的 sound name。

    起因：用户反馈听歌/看视频时听不到提示音。通知自带的声音走系统「提示音」
    通道，会被专注模式和全屏应用抑制，音量也只是主音量的一个比例。
    """

    def test_notification_script_has_no_builtin_sound(self):
        """通知脚本里不能再出现 sound name，否则两条声音路径会打架"""
        sent = {}
        class R:
            returncode = 0
            stderr = ""
        with patch("pagewatch.notify.subprocess.run",
                   side_effect=lambda a, **k: (sent.update(s=a[2]), R())[1]):
            notify.send_macos(Alert("K", "t", "b", urgent=True), sound=True)
        self.assertNotIn("sound name", sent["s"])

    def test_play_sound_uses_afplay_with_volume(self):
        seen = {}
        class P:
            pid = 1
        with patch("pagewatch.notify.subprocess.Popen",
                   side_effect=lambda a, **k: (seen.update(a=a, k=k), P())[1]):
            self.assertIsNone(notify.play_sound("Submarine", 2.0))
        self.assertEqual(seen["a"][0], "afplay")
        self.assertIn("-v", seen["a"])
        self.assertEqual(seen["a"][seen["a"].index("-v") + 1], "2.00")
        self.assertTrue(seen["k"].get("start_new_session"),
                        "不脱离进程组的话 launchd 退出时声音会被掐断")

    def test_amplification_adapts_to_system_volume(self):
        """
        固定倍数不行：主音量 25 时 2 倍还是很小声，调到 80 时 2 倍就炸耳朵。
        按当前主音量反推，响度才稳定。
        """
        from pagewatch.notify import amplification
        self.assertAlmostEqual(amplification(200, 25, 2.0), 8.0)
        self.assertAlmostEqual(amplification(200, 100, 2.0), 2.0)
        self.assertAlmostEqual(amplification(80, 80, 2.0), 1.0)

    def test_amplification_clamped(self):
        """超过 12 倍会削波失真，低于 0.5 等于听不见"""
        from pagewatch.notify import amplification
        self.assertEqual(amplification(1000, 1, 2.0), 12.0)
        self.assertEqual(amplification(1, 1000, 2.0), 0.5)

    def test_amplification_falls_back_when_volume_unreadable(self):
        from pagewatch.notify import amplification
        self.assertEqual(amplification(200, None, 3.3), 3.3)
        self.assertEqual(amplification(200, 0, 3.3), 3.3)

    def test_target_volume_passed_through_dispatch(self):
        calls = []
        with patch("pagewatch.notify.play_sound",
                   side_effect=lambda *a, **k: calls.append(a) or None), \
             patch("pagewatch.notify.send_macos", return_value=None):
            notify.dispatch([Alert("A", "t", "b", urgent=True)],
                            {"macos": {"enabled": True, "sound": True,
                                       "sound_target_volume": 200}}, None)
        self.assertEqual(calls[0][2], 200)

    def test_missing_sound_file_reported(self):
        self.assertIn("不存在", notify.play_sound("NoSuchSoundXYZ") or "")

    def test_sound_plays_once_per_round(self):
        """同一轮多条告警合并成一条通知，声音也只该响一次"""
        calls = []
        with patch("pagewatch.notify.play_sound",
                   side_effect=lambda *a, **k: calls.append(a) or None), \
             patch("pagewatch.notify.send_macos", return_value=None):
            notify.dispatch(
                [Alert("A", "t1", "b", urgent=True),
                 Alert("B", "t2", "b", urgent=True),
                 Alert("C", "t3", "b", urgent=True)],
                {"macos": {"enabled": True, "sound": True}}, None)
        self.assertEqual(len(calls), 1)

    def test_no_sound_for_non_urgent(self):
        calls = []
        with patch("pagewatch.notify.play_sound",
                   side_effect=lambda *a, **k: calls.append(a) or None), \
             patch("pagewatch.notify.send_macos", return_value=None):
            notify.dispatch([Alert("A", "t", "b", urgent=False)],
                            {"macos": {"enabled": True, "sound": True}}, None)
        self.assertEqual(calls, [])

    def test_sound_config_respected(self):
        calls = []
        with patch("pagewatch.notify.play_sound",
                   side_effect=lambda *a, **k: calls.append(a) or None), \
             patch("pagewatch.notify.send_macos", return_value=None):
            notify.dispatch([Alert("A", "t", "b", urgent=True)],
                            {"macos": {"enabled": True, "sound": True,
                                       "sound_name": "Hero",
                                       "sound_volume": 3.5}}, None)
        self.assertEqual(calls[0], ("Hero", 3.5, None))


class TestPersistentAlert(unittest.TestCase):
    """常驻窗口：不自动消失，且新的替换旧的"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_escape_handles_quotes_backslash_newline(self):
        """顺序错了会把换行转成字面的反斜杠加 n"""
        out = notify._osascript_escape('a"b\\c\nd')
        self.assertEqual(out, 'a\\"b\\\\c\\nd')

    def test_merges_alerts_into_one_window(self):
        """同一轮的多条必须合并，否则它们会互相替换掉"""
        seen = {}

        class FakeProc:
            pid = 4242
            stderr = None
            def poll(self): return None

        def fake_popen(args, **kw):
            if args[0] == "osascript":
                seen["script"] = args[2]
            return FakeProc()

        with patch("pagewatch.notify.subprocess.Popen", fake_popen), \
             patch("pagewatch.notify.dismiss_previous"), \
             patch("pagewatch.notify.time.sleep"):
            err = notify.send_macos_persistent(
                [Alert("K1", "榜首换人", "商品 A", "http://a", True),
                 Alert("K2", "前5有新货", "商品 B", "http://b", True)],
                self.dir)
        self.assertIsNone(err)
        self.assertIn("榜首换人", seen["script"])
        self.assertIn("前5有新货", seen["script"])
        self.assertIn("2 条变化", seen["script"])
        self.assertIn("http://a", seen["script"], "链接用第一条的")

    def test_writes_pidfile(self):
        class FakeProc:
            pid = 777
            stderr = None
            def poll(self): return None      # 仍在运行
        with patch("pagewatch.notify.subprocess.Popen", lambda *a, **k: FakeProc()), \
             patch("pagewatch.notify.dismiss_previous"), \
             patch("pagewatch.notify.time.sleep"):
            notify.send_macos_persistent([Alert("K", "t", "b")], self.dir)
        self.assertEqual((self.dir / "alert.pid").read_text(), "777")

    def test_must_detach_from_process_group(self):
        """
        launchd 任务退出时会回收整个进程组。不加 start_new_session 的话
        窗口会被连带杀掉 —— 实测表现就是"邮件收到了但弹窗没出现"。
        """
        seen = {}

        class FakeProc:
            pid = 1
            stderr = None
            def poll(self): return None

        def fake_popen(args, **kw):
            if args[0] == "osascript":
                seen["kw"] = kw
            return FakeProc()

        with patch("pagewatch.notify.subprocess.Popen", fake_popen), \
             patch("pagewatch.notify.dismiss_previous"), \
             patch("pagewatch.notify.time.sleep"):
            notify.send_macos_persistent([Alert("K", "t", "b")], self.dir, sound=False)
        self.assertTrue(seen["kw"].get("start_new_session"),
                        "必须 start_new_session，否则 launchd 一退出窗口就没")

    def test_reports_when_window_dies_immediately(self):
        """osascript 立刻退出时不能谎报成功，否则 notified 标记是假的"""
        import io

        class DeadProc:
            pid = 9
            stderr = io.BytesIO(b"execution error: not authorized")
            def poll(self): return 1

        with patch("pagewatch.notify.subprocess.Popen", lambda *a, **k: DeadProc()), \
             patch("pagewatch.notify.dismiss_previous"), \
             patch("pagewatch.notify.time.sleep"):
            err = notify.send_macos_persistent([Alert("K", "t", "b")], self.dir,
                                               sound=False)
        self.assertIsNotNone(err)
        self.assertIn("not authorized", err)
        self.assertFalse((self.dir / "alert.pid").exists(),
                         "没成功就不该留下 pidfile")

    def test_dismiss_skips_foreign_pid(self):
        """PID 会被系统复用，不是 osascript 就绝不能杀"""
        (self.dir / "alert.pid").write_text("12345")
        with patch("pagewatch.notify._pid_is_osascript", return_value=False), \
             patch("pagewatch.notify.os.kill") as k:
            notify.dismiss_previous(self.dir)
            k.assert_not_called()
        self.assertFalse((self.dir / "alert.pid").exists(), "过期 pidfile 应清掉")

    def test_dismiss_kills_our_pid(self):
        (self.dir / "alert.pid").write_text("12345")
        with patch("pagewatch.notify._pid_is_osascript", return_value=True), \
             patch("pagewatch.notify.os.kill") as k:
            notify.dismiss_previous(self.dir)
            k.assert_called_once()

    def test_dismiss_tolerates_garbage_pidfile(self):
        (self.dir / "alert.pid").write_text("not-a-number")
        notify.dismiss_previous(self.dir)   # 不该抛异常

    def test_no_url_means_single_button(self):
        seen = {}

        class FakeProc:
            pid = 1
            stderr = None
            def poll(self): return None
        def fake_popen(args, **kw):
            if args[0] == "osascript": seen["s"] = args[2]
            return FakeProc()
        with patch("pagewatch.notify.subprocess.Popen", fake_popen), \
             patch("pagewatch.notify.dismiss_previous"), \
             patch("pagewatch.notify.time.sleep"):
            notify.send_macos_persistent([Alert("K", "t", "b")], self.dir)
        self.assertNotIn("打开链接", seen["s"])

    def test_dispatch_routes_urgent_to_persistent(self):
        cfg = {"macos": {"enabled": True, "style": "center", "sound": False}}
        with patch("pagewatch.notify.send_macos_persistent", return_value=None) as pers, \
             patch("pagewatch.notify.send_macos", return_value=None) as banner:
            notify.dispatch([Alert("A", "t", "b", urgent=True),
                             Alert("B", "t2", "b2", urgent=False)], cfg, self.dir)
        self.assertEqual(len(pers.call_args[0][0]), 1, "只有紧急的走常驻窗口")
        banner.assert_called_once()  # 不紧急的仍走横幅

    def test_dispatch_falls_back_without_state_dir(self):
        cfg = {"macos": {"enabled": True, "style": "center"}}
        with patch("pagewatch.notify.send_macos_persistent") as pers, \
             patch("pagewatch.notify.send_macos", return_value=None) as banner:
            notify.dispatch([Alert("A", "t", "b", urgent=True)], cfg, None)
        pers.assert_not_called()
        banner.assert_called_once()

    def test_corner_is_the_default_style(self):
        """默认走角落通知，不要在屏幕中间弹窗挡住人"""
        with patch("pagewatch.notify.send_macos_persistent") as pers, \
             patch("pagewatch.notify.send_macos", return_value=None) as banner:
            notify.dispatch([Alert("A", "t", "b", urgent=True)],
                            {"macos": {"enabled": True}}, self.dir)
        pers.assert_not_called()
        banner.assert_called_once()

    def test_corner_merges_multiple_into_one(self):
        """同一轮多条合并成一条，否则角落会摞成一叠"""
        sent = []
        with patch("pagewatch.notify.send_macos",
                   side_effect=lambda a, sound=True: sent.append(a)):
            notify.dispatch(
                [Alert("A", "榜首换人", "x", urgent=True),
                 Alert("B", "前5有新货", "y", "http://u", urgent=True)],
                {"macos": {"enabled": True, "style": "corner"}}, self.dir)
        self.assertEqual(len(sent), 1)
        self.assertIn("2 条变化", sent[0].title)
        self.assertIn("榜首换人", sent[0].body)
        self.assertIn("前5有新货", sent[0].body)
        self.assertEqual(sent[0].url, "http://u")

    def test_corner_single_alert_kept_verbatim(self):
        sent = []
        with patch("pagewatch.notify.send_macos",
                   side_effect=lambda a, sound=True: sent.append(a)):
            notify.dispatch([Alert("A", "榜首换人 · 95% off", "商品详情", urgent=True)],
                            {"macos": {"enabled": True, "style": "corner"}}, self.dir)
        self.assertEqual(sent[0].title, "榜首换人 · 95% off")
        self.assertEqual(sent[0].body, "商品详情")


class TestExampleConfigHasNoSecrets(unittest.TestCase):
    """
    回归：示例配置曾经把真实的 Gmail 应用专用密码带了进去。
    根因是用字符串替换生成示例文件，只换了邮箱没换密码。
    """

    def test_builder_blanks_all_secret_keys(self):
        import importlib.util
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "mkexample", root / "tools" / "make_example_config.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        src = (
            '[notify.email]\n'
            'enabled = true\n'
            'username = "real@gmail.com"\n'
            'password = "abcd efgh ijkl mnop"   # 注释\n'
            'from = "real@gmail.com"\n'
            'to = "real@gmail.com"\n'
        )
        out = mod.build(src)
        self.assertNotIn("abcd efgh ijkl mnop", out)
        self.assertNotIn("real@gmail.com", out)
        self.assertIn('password = ""', out)
        self.assertIn("# 注释", out, "注释要保留，否则示例文件失去说明作用")
        self.assertEqual(mod.leaked(out, src), [])

    def test_leak_detector_catches_residue(self):
        import importlib.util
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "mkexample2", root / "tools" / "make_example_config.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = '[notify.email]\npassword = "SECRET123"\n'
        self.assertIn("password", mod.leaked('password = "SECRET123"', src))

    def test_shipped_example_file_is_clean(self):
        """仓库里现存的那份示例文件本身必须干净"""
        root = Path(__file__).resolve().parent.parent
        example = (root / "config.example.toml").read_text()
        cfg = root / "config.toml"
        if not cfg.exists():
            self.skipTest("本机没有 config.toml")
        import tomllib
        email = tomllib.loads(cfg.read_text()).get("notify", {}).get("email", {})
        for k in ("password", "username"):
            v = str(email.get(k) or "")
            if v:
                self.assertNotIn(v, example, f"示例配置泄漏了 {k}")


class TestPollAllSelection(unittest.TestCase):
    """
    回归：--watcher 改成可重复之后，poll_all 还按单个字符串处理，
    每次启动直接崩溃，而日志为空看不出原因。
    """

    def setUp(self):
        from pagewatch.config import Config
        self.cfg = Config(watchers={
            "a": {"name": "a", "type": "x"},
            "b": {"name": "b", "type": "x"},
            "c": {"name": "c", "type": "x", "enabled": False},
        })

    def _names(self, only):
        from pagewatch import runner
        seen = []
        with patch.object(runner, "poll_one",
                          side_effect=lambda n, *a, **k: seen.append(n)):
            runner.poll_all(self.cfg, None, only=only)
        return seen

    def test_list_of_watchers(self):
        self.assertEqual(self._names(["a", "b"]), ["a", "b"])

    def test_single_string_still_works(self):
        self.assertEqual(self._names("a"), ["a"])

    def test_none_runs_all_enabled(self):
        self.assertEqual(self._names(None), ["a", "b"])

    def test_unknown_name_raises_with_all_missing_listed(self):
        from pagewatch import runner
        with self.assertRaises(KeyError) as e:
            runner.poll_all(self.cfg, None, only=["a", "nope", "alsonope"])
        self.assertIn("nope", str(e.exception))
        self.assertIn("alsonope", str(e.exception))


class TestToLocal(unittest.TestCase):
    def test_same_instant(self):
        ts = datetime(2026, 10, 22, 3, 0, 0, tzinfo=UTC)
        self.assertEqual(_to_local(ts, "America/New_York").hour, 23)  # 前一天 23 点
        self.assertEqual(_to_local(ts, "America/New_York").day, 21)


if __name__ == "__main__":
    unittest.main()
