"""
Nordstrom Rack 列表页 watcher

原理：Nordstrom Rack 是服务端渲染的 React 应用，整个页面状态以
``window.__INITIAL_CONFIG__ = {...}`` 的形式内嵌在 HTML 里（约 1.9MB JSON）。
不需要浏览器自动化，也不需要 HTML 解析库，直接把这坨 JSON 抠出来即可。

用到的字段：
  productResults.query.resultCount          结果总数（主信号）
  productResults.productUi.byId[].sourceIndex  页面排序
  productResults.productsById[id]           标题、品牌、价格
  propositions[0].asOf                      站点自报的数据快照时间
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from ..http import fetch_text
from ..models import Item, Observation
from .base import Watcher

BASE = "https://www.nordstromrack.com"
_ASSIGN_RE = re.compile(r"window\.__INITIAL_CONFIG__\s*=\s*")


class ParseError(RuntimeError):
    """页面结构变了，解析不出预期字段"""


def extract_initial_config(html: str) -> dict[str, Any]:
    """
    从 HTML 中抠出 __INITIAL_CONFIG__ 这个 JSON 对象。

    不能用正则匹配到行尾，因为 JSON 里有海量嵌套和转义引号。
    这里做一次括号配对扫描，并正确跳过字符串字面量内的大括号。
    """
    m = _ASSIGN_RE.search(html)
    if not m:
        raise ParseError("未找到 window.__INITIAL_CONFIG__，页面结构可能已变更")

    start = m.end()
    if start >= len(html) or html[start] != "{":
        raise ParseError("__INITIAL_CONFIG__ 后面不是 JSON 对象")

    depth = 0
    in_string = False
    escaped = False
    i = start
    while i < len(html):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1
    else:
        raise ParseError("__INITIAL_CONFIG__ 的大括号没有闭合")

    return json.loads(html[start : i + 1])


def _num(v: Any) -> float | None:
    """价格字段是字符串形式的小数，统一转 float；缺失或不合法返回 None"""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_item(pid: str, product: dict[str, Any]) -> Item:
    """把 productsById 里的一条记录转成通用 Item"""
    props = product.get("propositions") or [{}]
    prop = props[0] if props else {}

    price = _num((prop.get("sellingRetailPriceRange") or {}).get("min"))
    compare_at = _num((prop.get("compareAtRetailPriceRange") or {}).get("max"))

    # 优先用站点算好的折扣区间；没有就用现价/原价自己算
    pct = None
    rng = prop.get("sellingRetailPercentageOffRange") or {}
    raw_pct = rng.get("max") if rng.get("max") is not None else rng.get("min")
    if raw_pct is not None:
        pct_f = _num(raw_pct)
        pct = round(pct_f) if pct_f is not None else None
    elif price is not None and compare_at:
        pct = round((1 - price / compare_at) * 100)

    # 配送能力。isPickAndNotShip=True 明确表示"只能到店取、不能寄"；
    # 另外 shipQuantity=0 也意味着实际寄不了，两个条件都要看。
    av = prop.get("availability") or {}
    pick_only = av.get("isPickAndNotShip")
    ship_qty = av.get("shipQuantity")
    if pick_only is None and ship_qty is None:
        shippable = None  # 信息缺失，不臆断
    else:
        shippable = not bool(pick_only) and bool(ship_qty)

    alias = product.get("webPathAlias")
    url = f"{BASE}/s/{alias}/{pid}" if alias else f"{BASE}/s/-/{pid}"

    return Item(
        item_id=str(pid),
        title=product.get("copyProductTitle") or "(untitled)",
        brand=product.get("labelDisplayName"),
        price=price,
        compare_at=compare_at,
        pct_off=pct,
        url=url,
        shippable=shippable,
        ship_qty=ship_qty if isinstance(ship_qty, int) else None,
        inventory_warning=product.get("inventoryWarning"),
        tags=tuple(
            t.get("type", "") for t in (prop.get("enticementTags") or []) if t.get("type")
        ),
        as_of=prop.get("asOf"),
    )


class NordstromRackWatcher(Watcher):
    """
    监控一个 Nordstrom Rack 列表页（清仓页、品类页、搜索结果页都行）

    配置字段：
      url              完整页面 URL，务必保留 sort / preferredStore 等参数
      pages            抓前几页，默认 1
      track_all_items  是否把整页条目都落库，默认 True（不增加抓取成本）
      require_shippable 榜首是否必须是能直邮的商品，默认 True。
                       Rack 在门店模式下会混入"仅限到店自提"的商品，
                       那些东西网上根本买不了，排在第一位没有意义。
      timeout          单次请求超时秒数，默认 30
    """

    type_name = "nordstrom_rack"

    def observe(self) -> Observation:
        url: str = self.cfg["url"]
        pages: int = int(self.cfg.get("pages", 1))
        timeout: float = float(self.cfg.get("timeout", 30))
        # 自动翻页：一直翻到本页最低折扣跌破这个门槛为止。
        # 固定页数不可靠 —— 折扣分布每天都在变（实测 10-26 第 1 页末尾是 88%，
        # 10-28 变成 91%），写死页数会让规则悄悄漏掉命中项。
        until_pct: int | None = self.cfg.get("until_pct_off")
        max_pages: int = int(self.cfg.get("max_pages", 6))

        items: list[Item] = []
        total_count: int | None = None
        source_ts: str | None = None
        total_bytes = 0
        total_ms = 0
        last_status: int | None = None
        pages_done = 0

        page_limit = max_pages if until_pct is not None else pages
        for page in range(1, page_limit + 1):
            page_url = url if page == 1 else _with_page(url, page)
            html, wire, ms, status = fetch_text(page_url, timeout=timeout)
            total_bytes += wire
            total_ms += ms
            last_status = status
            pages_done += 1

            cfg = extract_initial_config(html)
            results = cfg.get("productResults")
            if not results:
                raise ParseError("JSON 中缺少 productResults 节点")

            # 总数只认第 1 页的，后续页码的值会因为库存变动而漂移
            if page == 1:
                total_count = (results.get("query") or {}).get("resultCount")

            products = results.get("productsById") or {}
            ui = (results.get("productUi") or {}).get("byId") or {}
            ordered = sorted(ui.values(), key=lambda x: x.get("sourceIndex", 1 << 30))

            for entry in ordered:
                pid = str(entry.get("productId") or "")
                product = products.get(pid)
                if not pid or not product:
                    continue
                items.append(_build_item(pid, product))
                if source_ts is None:
                    props = product.get("propositions") or []
                    if props:
                        source_ts = props[0].get("asOf")

            if until_pct is not None:
                page_pcts = [
                    i.pct_off for i in items[-len(ordered):] if i.pct_off is not None
                ]
                if page_pcts and min(page_pcts) < int(until_pct):
                    break

        if not items and total_count is None:
            raise ParseError("既没解析到条目也没解析到总数")

        require_ship = bool(self.cfg.get("require_shippable", True))
        ranked = _effective_ranking(items, require_ship)

        return Observation(
            watcher=self.name,
            ts=datetime.now(UTC),
            total_count=total_count,
            ranked_ids=[i.item_id for i in ranked],
            primary=ranked[0] if ranked else None,
            items=items if self.cfg.get("track_all_items", True) else items[:1],
            source_ts=source_ts,
            fetch_ms=total_ms,
            bytes_wire=total_bytes,
            http_status=last_status,
            pages_fetched=pages_done,
        )


def _effective_ranking(items: list[Item], require_shippable: bool) -> list[Item]:
    """
    过滤出"你真能买到的"条目，保持原始排序。

    require_shippable 时剔除明确不能直邮的商品 —— 它们网上买不了，
    排在前面毫无意义。shippable 为 None（信息缺失）时保留，
    宁可多报也不要因为字段缺失而漏掉真正的好货。

    全部被剔除时退回原始列表，避免榜首变成 None 导致监控失效。
    """
    if not items:
        return []
    if not require_shippable:
        return list(items)
    kept = [it for it in items if it.shippable is not False]
    return kept or list(items)


def _with_page(url: str, page: int) -> str:
    """给 URL 加上 page 参数；已有 page 参数时替换掉"""
    if re.search(r"[?&]page=", url):
        return re.sub(r"([?&]page=)\d+", rf"\g<1>{page}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"
