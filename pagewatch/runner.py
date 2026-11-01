"""
抓取执行与变化判断模块

单次 poll 的完整流程：
  1. 读上一次成功的快照
  2. 抓取并解析成 Observation
  3. 和上一次 diff，算出 count_delta / n_new / n_gone / primary_changed
  4. 按配置生成告警
  5. 落库（先落库再发通知，保证数据不因通知失败而丢失）
  6. 发通知

关于"新增"的两种口径，两个都有用，不要混淆：
  n_new  —— 相对上一次抓取，本页新出现的 ID。会把"从第2页挤上第1页"的老货算进来。
  真新品 —— 历史上从没见过的 ID。更严格，但前几天库是空的会全算新品。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import notify
from .config import Config
from .models import Alert, Item, Observation
from .store import Store
from .watchers import build


@dataclass
class PollResult:
    """一次 poll 的结果摘要，供 CLI 打印"""

    watcher: str
    ok: bool
    error: str | None = None
    obs: Observation | None = None
    count_delta: int | None = None
    n_new: int | None = None
    n_gone: int | None = None
    n_brand_new: int | None = None
    n_new_in_top: int | None = None
    rule_hits: dict[str, int] | None = None
    primary_changed: bool = False
    alerts: list[Alert] | None = None
    notify_problems: list[str] | None = None
    first_run: bool = False
    anomalous: bool = False
    anomaly_reason: str | None = None


def _to_local(ts: datetime, tz_name: str | None) -> datetime:
    """
    把一个 UTC 时间换算成本地时间。

    必须由同一个瞬间换算出 ts_utc 和 ts_local，不能各取各的 now()——
    否则两者相差一两秒，跨整点时会被分到不同的小时桶，
    而按小时分桶正是本项目要回答的问题。
    """
    return ts.astimezone(ZoneInfo(tz_name)) if tz_name else ts.astimezone()


def _detect_anomaly(
    total: int | None, recent: list[int], max_dev: float
) -> str | None:
    """
    判断本次结果集是否不可信，不可信时返回原因字符串。

    实测 Nordstrom Rack 偶尔会返回一个残缺结果集：总数从 6950 骤降到 400，
    商品也完全换掉，下一次抓取又恢复正常。原因不明（疑似后端降级或限流）。
    如果不拦住，它会制造假的"榜首换人"告警，并往 item 表灌几十件假新品。

    判据：和最近若干次可信抓取的中位数相比偏离超过 max_dev（默认 50%）。
    日常波动是分钟级 ±10（约 0.15%），正常补货也远不到 50%，所以这个
    阈值既挡得住残缺结果，又不会误伤真实的大批量上架。
    """
    if total is None:
        return "结果中没有总数字段"
    if len(recent) < 3:
        return None  # 样本不足，先全部接受，等基线建立起来
    median = statistics.median(recent)
    if median <= 0:
        return None
    deviation = abs(total - median) / median
    if deviation > max_dev:
        return f"总数 {total:,} 偏离近期中位数 {median:,.0f} 达 {deviation:.0%}"
    return None


#: 记忆范围要比告警窗口宽多少名。
#:
#: 必须 > 0：被新货挤出前 N 名的条目要落在记忆范围内，记录才能持续刷新，
#: 它们回到前 N 名时才不会被误判成新面孔。留 10 名余量意味着可以容忍
#: 同时涌入 10 件新货。band 由 top_n 推导，所以改 top_n 不会悄悄把这个约束破坏掉。
TOP_SEEN_MARGIN = 10


def top_seen_band(top_n: int) -> int:
    """记忆范围：告警窗口 + 余量"""
    return top_n + TOP_SEEN_MARGIN


def _new_in_top(
    obs: Observation,
    top_seen: dict[str, datetime],
    now: datetime,
    top_n: int,
    reentry_hours: float = 24.0,
) -> list[Item]:
    """
    算出"真的刚挤进前 N 名"的条目。

    判据是"这个条目最近 reentry_hours 小时内没在前 TOP_SEEN_BAND 名出现过"，
    而不是"跟上一次快照比是新的"。

    为什么不能只跟上一次比：前 5 名挤进一件新货 X，原来的第 5 名 E 被推到第 6；
    X 卖掉后 E 回到第 5 名，相对上一次快照就成了"新面孔" —— 但 E 根本没变化，
    这是假警报。用记忆范围取前 10，E 在第 6 名期间记录一直在刷新，就不会误报。

    top_seen 为空（首次运行）时返回空列表：此时无从判断新旧，全报等于刷屏。
    """
    if not obs.ranked_ids or not top_seen:
        return []
    by_id = {i.item_id: i for i in obs.items}
    out: list[Item] = []
    for item_id in obs.ranked_ids[:top_n]:
        if item_id not in by_id:
            continue
        last = top_seen.get(item_id)
        if last is None or (now - last).total_seconds() > reentry_hours * 3600:
            out.append(by_id[item_id])
    return out


def _rule_matches(items: list[Item], rule: dict[str, Any]) -> list[Item]:
    """
    筛出满足该规则的条目。只判断"是否满足"，不管有没有报过。

    支持的条件（都可省略）：
      min_pct_off / max_pct_off   折扣区间
      min_ship_qty / max_ship_qty 可直邮件数区间。设了 max 就意味着"快没了"
      min_price / max_price       现价区间
      min_compare_at / max_compare_at  原价（对标价）区间
      brands / exclude_brands     品牌白名单 / 黑名单（不区分大小写）
      require_tags                必须带这些标签，如 ["NEW_MARKDOWN"]
    """
    out: list[Item] = []
    for it in items:
        pct = it.pct_off
        if (lo := rule.get("min_pct_off")) is not None:
            if pct is None or pct < int(lo):
                continue
        if (hi := rule.get("max_pct_off")) is not None:
            if pct is None or pct > int(hi):
                continue

        qty = it.ship_qty
        if (lo := rule.get("min_ship_qty")) is not None:
            if qty is None or qty < int(lo):
                continue
        if (hi := rule.get("max_ship_qty")) is not None:
            if qty is None or qty > int(hi):
                continue

        if (lo := rule.get("min_price")) is not None:
            if it.price is None or it.price < float(lo):
                continue
        if (hi := rule.get("max_price")) is not None:
            if it.price is None or it.price > float(hi):
                continue

        cmp_at = it.compare_at
        if (lo := rule.get("min_compare_at")) is not None:
            if cmp_at is None or cmp_at < float(lo):
                continue
        if (hi := rule.get("max_compare_at")) is not None:
            if cmp_at is None or cmp_at > float(hi):
                continue

        # 品牌匹配不区分大小写，也容忍站点写法差异（"Gucci" vs "GUCCI"）
        brand = (it.brand or "").strip().lower()
        if (allow := rule.get("brands")):
            if brand not in {str(b).strip().lower() for b in allow}:
                continue
        if (deny := rule.get("exclude_brands")):
            if brand in {str(b).strip().lower() for b in deny}:
                continue

        need = rule.get("require_tags")
        if need and not set(need).issubset(set(it.tags)):
            continue

        out.append(it)
    return out


def _new_for_rule(
    items: list[Item],
    rule: dict[str, Any],
    seen: dict[str, datetime],
    now: datetime,
    warmed_up: bool = True,
) -> list[Item]:
    """
    该规则下"这次才需要通知"的条目。

    一个条目只要持续满足规则就不该每轮报一次，所以靠 rule_seen 去重。
    超过 reentry_hours 没命中过的，重新算新消息（它可能是又降价了才回来的）。

    warmed_up=False（这个 watcher 第一次跑）时返回空：此时存量全是"新的"，
    全报等于刷屏。

    注意不能用"seen 是空的"当预热判据 —— 一条至今从未命中过的规则，
    它的记忆**永远**是空的，那样这条规则就再也不会响了。
    """
    if not warmed_up:
        return []
    matched = _rule_matches(items, rule)
    cooldown = float(rule.get("reentry_hours", 24)) * 3600
    return [
        it for it in matched
        if (last := seen.get(it.item_id)) is None
        or (now - last).total_seconds() > cooldown
    ]


def _fmt_money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "n/a"


def _build_alerts(
    obs: Observation,
    prev: Any,
    count_delta: int | None,
    n_new: int | None,
    alert_cfg: dict[str, Any],
    n_brand_new: int | None = None,
    warming_up: bool = False,
    new_in_top: list[Item] | None = None,
    rule_hits: dict[str, list[Item]] | None = None,
) -> list[Alert]:
    """
    根据 diff 结果和配置生成告警列表。

    warming_up 为 True 时抑制"上新"类告警：刚开始跑的几次，item 表几乎是空的，
    所有条目看起来都像新品，会刷屏。榜首类告警不受影响，因为它只看上一次快照。
    """
    alerts: list[Alert] = []
    p = obs.primary

    # --- 1. 第一个商品换人 ---
    #
    # only_new 时只在"新榜首是真的新面孔"才报。否则会出现这种假警报：
    # 一件好货抢到榜首 → 报一次（对）；它卖掉后榜首换回原来的常驻商品 → 又报一次
    # （错，什么新东西都没有）。实测 10-24 06:17 和 06:32 就是这么一对。
    new_ids = {i.item_id for i in (new_in_top or [])}
    if alert_cfg.get("primary_changed", True) and p and prev is not None:
        prev_id = prev["primary_item_id"]
        is_revert = (alert_cfg.get("primary_changed_only_new", True)
                     and p.item_id not in new_ids)
        if prev_id and prev_id != p.item_id and not is_revert:
            lines = [p.label(), p.price_line()]
            if p.flags():
                lines.append(p.flags())
            lines.append(
                f"上一个: {prev['primary_title'] or prev_id} "
                f"({_fmt_money(prev['primary_price'])})"
            )
            body = "\n".join(lines)
            alerts.append(
                Alert(
                    kind="PRIMARY_CHANGED",
                    title=f"榜首换人 · {p.pct_off}% off" if p.pct_off else "榜首换人",
                    body=body,
                    url=p.url,
                    urgent=True,
                    item_id=p.item_id,
                )
            )
        # --- 2. 第一个商品没换但降价了 ---
        elif (
            alert_cfg.get("primary_price_drop", True)
            and prev_id == p.item_id
            and p.price is not None
            and prev["primary_price"] is not None
            and p.price < float(prev["primary_price"])
        ):
            drop = float(prev["primary_price"]) - p.price
            alerts.append(
                Alert(
                    kind="PRIMARY_PRICE_DROP",
                    title=f"榜首降价 -{_fmt_money(drop)}",
                    body="\n".join(
                        [p.label(), f"{_fmt_money(prev['primary_price'])} → {p.price_line()}"]
                        + ([p.flags()] if p.flags() else [])
                    ),
                    url=p.url,
                    urgent=True,
                    item_id=p.item_id,
                )
            )

    # --- 3. 前 N 名出现新面孔 ---
    #
    # 这是实测驱动的规则：榜首常常被一件"没人要但折扣标得高"的商品长期霸占
    # （实测有一件 571 件库存躺一整天没卖出去，却霸榜数周）。真正的好货进来时
    # 往往夺不了冠，却会很快被买走。只盯第 0 名就会一直错过它们。
    # 开关统一在这里判，和其他告警类型一致；调用方只负责算出 new_in_top
    n = int(alert_cfg.get("top_n", 5))
    if alert_cfg.get("top_n_new", False) and new_in_top:
        lines = []
        for it in new_in_top:
            line = f"{it.label()}  {it.price_line()}"
            if it.flags():
                line += f"  [{it.flags()}]"
            lines.append(line)
        best = min(new_in_top, key=lambda i: i.price if i.price is not None else 1e9)
        alerts.append(
            Alert(
                kind="TOP_N_NEW",
                title=(f"前 {n} 名进了 {len(new_in_top)} 件新货"
                       if len(new_in_top) > 1 else f"前 {n} 名有新货"),
                body="\n".join(lines),
                url=best.url,
                urgent=True,
                item_id=new_in_top[0].item_id,
            )
        )

    # --- 3b. 自定义规则命中 ---
    for rule_name, hits in (rule_hits or {}).items():
        if not hits:
            continue
        lines = []
        for it in hits:
            line = f"{it.label()}  {it.price_line()}"
            if it.flags():
                line += f"  [{it.flags()}]"
            lines.append(line)
        best = min(hits, key=lambda i: i.price if i.price is not None else 1e9)
        alerts.append(
            Alert(
                kind="RULE_MATCH",
                title=(f"{rule_name} · {len(hits)} 件" if len(hits) > 1
                       else f"{rule_name}"),
                body="\n".join(lines),
                url=best.url,
                urgent=True,
                item_id=hits[0].item_id,
            )
        )

    # --- 4. 出现历史从未见过的商品 = 确定性的上新 ---
    #
    # 这里刻意不用"总数上涨"做判断。实测总数在分钟级双向波动 ±10（随时有人下单、
    # 有人退货），所以上架 1 件商品完全淹没在噪声里，靠阈值既漏报又误报。
    # 而"这个 product_id 我从来没见过"是无歧义的，上新 1 件也能精确捕捉。
    if (
        alert_cfg.get("new_arrivals", False)
        and not warming_up
        and n_brand_new is not None
        and n_brand_new >= int(alert_cfg.get("new_arrivals_min", 1))
    ):
        detail = f"{n_brand_new} 件新商品进入榜单"
        if obs.total_count is not None:
            delta_txt = f"{count_delta:+d}" if count_delta is not None else "n/a"
            detail += f"\n总数 {obs.total_count:,} ({delta_txt})"
        alerts.append(
            Alert(
                kind="NEW_ARRIVALS",
                title=f"上新 {n_brand_new} 件",
                body=detail,
                url=obs.primary.url if obs.primary else None,
                urgent=False,
            )
        )

    # --- 5. 总数异常暴涨。默认关闭：日常波动用它判断不可靠，
    #        但批量补货（几百件）时它比逐个 ID 更快反映出来。 ---
    if (
        alert_cfg.get("count_up", False)
        and count_delta is not None
        and count_delta >= int(alert_cfg.get("count_up_min", 100))
    ):
        alerts.append(
            Alert(
                kind="COUNT_UP",
                title="总数大幅上涨",
                body=f"总数 {obs.total_count:,} (+{count_delta})",
                url=None,
                urgent=False,
            )
        )

    return alerts


def poll_one(name: str, cfg: Config, store: Store, dry_run: bool = False) -> PollResult:
    """
    执行单个 watcher 的一次抓取。任何异常都被捕获并记入数据库。
    """
    wcfg = cfg.watchers[name]
    alert_cfg = wcfg.get("alerts", {})
    prev = store.last_snapshot(name)

    try:
        watcher = build(name, wcfg)
        obs = watcher.observe()
    except Exception as exc:  # 抓取/解析失败都在这里兜住
        err = f"{type(exc).__name__}: {exc}"
        if not dry_run:
            now = datetime.now(UTC)
            store.record_failure(name, now, _to_local(now, cfg.timezone), err)
        return PollResult(watcher=name, ok=False, error=err)

    # 和 obs.ts 换算自同一瞬间，保证 UTC 与本地时间指向同一个小时
    ts_local = _to_local(obs.ts, cfg.timezone)

    # 结果集可信度检查。不可信就只留痕、不告警、不污染 item 表，
    # 也不会成为下一次的 diff 基线。
    anomaly_reason = _detect_anomaly(
        obs.total_count,
        store.recent_counts(name),
        float(wcfg.get("anomaly_max_deviation", 0.5)),
    )
    anomalous = anomaly_reason is not None

    # ---- diff ----
    count_delta: int | None = None
    n_new: int | None = None
    n_gone: int | None = None
    primary_changed = False

    if prev is not None:
        if obs.total_count is not None and prev["total_count"] is not None:
            count_delta = obs.total_count - int(prev["total_count"])

        if obs.items and prev["item_ids"]:
            prev_ids = set(json.loads(prev["item_ids"]))
            cur_ids = set(obs.item_ids)
            n_new = len(cur_ids - prev_ids)
            n_gone = len(prev_ids - cur_ids)

        if obs.primary and prev["primary_item_id"]:
            primary_changed = obs.primary.item_id != prev["primary_item_id"]

    # 历史上从没见过的 ID 数量 —— 这是判断"真上新"的主信号
    n_brand_new: int | None = None
    if obs.items:
        known = store.known_item_ids(name)
        if known:  # 库为空时这个指标没有意义，跳过
            n_brand_new = len(set(obs.item_ids) - known)

    # 前 N 名的新面孔。即使告警关着也记录下来，供事后分析。
    top_seen = store.top_seen_map(name)
    new_in_top = _new_in_top(
        obs, top_seen, obs.ts,
        int(alert_cfg.get("top_n", 5)),
        float(wcfg.get("reentry_hours", 24)),
    )
    n_new_in_top = len(new_in_top) if top_seen else None

    # 自定义规则。每条规则独立去重，互不干扰。
    rule_hits: dict[str, list[Item]] = {}
    rules = [r for r in wcfg.get("rule", []) if r.get("enabled", True)]
    # 预热判据用"这个 watcher 跑过几次"，不是"记忆里有没有东西"
    rules_warmed = store.snapshot_count(name) > 0
    for rule in rules:
        rname = rule.get("name") or "rule"
        hits = _new_for_rule(obs.items, rule,
                             store.rule_seen_map(name, rname), obs.ts,
                             warmed_up=rules_warmed)
        if hits:
            rule_hits[rname] = hits

    # 预热期内 item 表还没见过足够多的商品，先不报上新
    warmup = int(wcfg.get("warmup_polls", 3))
    warming_up = store.snapshot_count(name) < warmup

    alerts = (
        []
        if anomalous
        else _build_alerts(obs, prev, count_delta, n_new, alert_cfg,
                           n_brand_new=n_brand_new, warming_up=warming_up,
                           new_in_top=new_in_top, rule_hits=rule_hits)
    )

    if dry_run:
        return PollResult(
            watcher=name, ok=True, obs=obs, count_delta=count_delta, n_new=n_new,
            n_gone=n_gone, n_brand_new=n_brand_new, n_new_in_top=n_new_in_top,
            rule_hits={k: len(v) for k, v in rule_hits.items()},
            primary_changed=primary_changed,
            alerts=alerts, notify_problems=[], first_run=prev is None,
            anomalous=anomalous, anomaly_reason=anomaly_reason,
        )

    # 先落库，再发通知 —— 通知挂了也不能丢数据
    store.record(
        obs,
        ts_local,
        count_delta=count_delta,
        n_new=n_new,
        n_gone=n_gone,
        n_brand_new=n_brand_new,
        n_new_in_top=n_new_in_top,
        primary_changed=primary_changed,
        # 异常结果绝不能进 item 表，否则几十件假商品会被永久记成"见过"，
        # 导致真正的新品在下次被误判成老货
        track_items=bool(wcfg.get("track_all_items", True)) and not anomalous,
        anomalous=anomalous,
        anomaly_reason=anomaly_reason,
    )

    # 先更新记忆再发通知：记忆必须反映"本次已看到这些在前列"，
    # 否则下一轮会把它们当成新面孔重报一遍。
    if not anomalous:
        band = top_seen_band(int(alert_cfg.get("top_n", 5)))
        store.touch_top_seen(name, obs.ranked_ids[:band], obs.ts)
        # 规则记忆要记下"本次所有命中的条目"（不只是报过的），
        # 否则持续满足规则的条目会每轮重报
        for rule in rules:
            rname = rule.get("name") or "rule"
            store.touch_rule_seen(
                name, rname,
                [i.item_id for i in _rule_matches(obs.items, rule)], obs.ts)

    problems = (notify.dispatch(alerts, cfg.notify, cfg.db_path.parent / '.state')
                if alerts else [])
    store.record_events(name, obs.ts, ts_local, alerts, notified=not problems)

    return PollResult(
        watcher=name, ok=True, obs=obs, count_delta=count_delta, n_new=n_new,
        n_gone=n_gone, n_brand_new=n_brand_new, n_new_in_top=n_new_in_top,
        rule_hits={k: len(v) for k, v in rule_hits.items()},
        primary_changed=primary_changed,
        alerts=alerts, notify_problems=problems, first_run=prev is None,
        anomalous=anomalous, anomaly_reason=anomaly_reason,
    )


def poll_all(cfg: Config, store: Store, only: str | None = None,
             dry_run: bool = False) -> list[PollResult]:
    """执行所有启用的 watcher；only 指定时只跑那一个"""
    targets = cfg.enabled_watchers()
    if only:
        if only not in cfg.watchers:
            raise KeyError(f"配置中没有名为 '{only}' 的 watcher")
        targets = {only: cfg.watchers[only]}
    return [poll_one(name, cfg, store, dry_run=dry_run) for name in targets]
