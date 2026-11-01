"""
离线分析模块

回答的核心问题是："这个页面到底什么时候更新？"

手段是把每次抓取的 total_count 差分，再按 本地星期 × 本地小时 聚合，
画成终端里的 ASCII 热力图。上新如果有固定节奏，几天数据就能看出来。

另外提供 calibrate()，根据实际观测到的 delta 分布推荐告警阈值 ——
这个阈值不能拍脑袋，必须用数据定。
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .store import Store

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# 热力图的灰阶，从无到强
BLOCKS = " ·:+*#@"


def _heat_char(value: float, vmax: float) -> str:
    """把一个数值映射成一个灰阶字符"""
    if vmax <= 0 or value <= 0:
        return BLOCKS[0]
    idx = int(round(value / vmax * (len(BLOCKS) - 1)))
    return BLOCKS[max(1, min(idx, len(BLOCKS) - 1))]


def asof_heatmap(store: Store, watcher: str, tz_name: str | None = None) -> dict[str, Any]:
    """
    用商品自带的 asOf 时间戳建 星期×小时 矩阵。

    和 heatmap() 的根本区别：heatmap 只能覆盖"我们真的抓取过"的时段，
    电脑睡着的夜里是一片 '?'。而每件商品都带着自己上次被重新定价的时刻，
    所以白天抓一次就能回溯十几个小时 —— **这是唯一能看到夜间活动的途径**。

    每件商品只计一次（按 item 表去重），不会因为连续抓取而重复累加。
    """
    tz = ZoneInfo(tz_name) if tz_name else None
    grid: dict[tuple[int, int], float] = defaultdict(float)
    n_bad = 0
    stamps = store.as_of_stamps(watcher)
    for raw in stamps:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            n_bad += 1
            continue
        local = dt.astimezone(tz) if tz else dt.astimezone()
        grid[(local.weekday(), local.hour)] += 1

    # asOf 覆盖的是"商品被重新定价的时刻"，不是"我们采样的时刻"，
    # 所以这里不存在"未采样"的概念，counts 全格给满，渲染时就不会出现 '?'
    counts = {(d, h): 1 for d in range(7) for h in range(24)}
    lo, hi, total = store.as_of_span(watcher)
    return {"grid": dict(grid), "counts": counts, "metric": "asof",
            "n_rows": len(stamps), "n_unparsable": n_bad,
            "span_from": lo, "span_to": hi, "n_observations": total}


def summarize(store: Store, watcher: str) -> dict[str, Any]:
    """汇总一个 watcher 的全部抓取历史"""
    rows = store.snapshots(watcher)
    # 异常快照不参与任何统计，但要计数，便于发现站点不稳定
    ok_rows = [r for r in rows if r["ok"] and not r["anomalous"]]
    n_anomalous = sum(1 for r in rows if r["ok"] and r["anomalous"])
    deltas = [int(r["count_delta"]) for r in ok_rows if r["count_delta"] is not None]
    ups = [d for d in deltas if d > 0]
    downs = [d for d in deltas if d < 0]

    brand_new = [int(r["n_brand_new"]) for r in ok_rows if r["n_brand_new"] is not None]
    n_top_changes = sum(1 for r in ok_rows if r["primary_changed"])
    blocked = [int(r["n_not_shippable"]) for r in ok_rows if r["n_not_shippable"] is not None]
    n_primary_blocked = sum(1 for r in ok_rows if r["primary_shippable"] == 0)
    markdowns = [int(r["n_new_markdown"]) for r in ok_rows if r["n_new_markdown"] is not None]

    return {
        "watcher": watcher,
        "n_polls": len(rows),
        "total_brand_new": sum(brand_new),
        "n_polls_with_arrivals": sum(1 for b in brand_new if b > 0),
        "n_polls_measured": len(brand_new),
        "n_top_changes": n_top_changes,
        "max_not_shippable": max(blocked) if blocked else 0,
        "n_polls_with_blocked": sum(1 for b in blocked if b > 0),
        "n_primary_blocked": n_primary_blocked,
        "markdown_last": markdowns[-1] if markdowns else None,
        "markdown_min": min(markdowns) if markdowns else None,
        "markdown_max": max(markdowns) if markdowns else None,
        "n_failed": sum(1 for r in rows if not r["ok"]),
        "n_anomalous": n_anomalous,
        "first_ts": ok_rows[0]["ts_local"] if ok_rows else None,
        "last_ts": ok_rows[-1]["ts_local"] if ok_rows else None,
        "last_count": ok_rows[-1]["total_count"] if ok_rows else None,
        "deltas": deltas,
        "n_up": len(ups),
        "n_down": len(downs),
        "n_flat": len(deltas) - len(ups) - len(downs),
        "total_wire_mb": sum(int(r["bytes_wire"] or 0) for r in ok_rows) / 1_048_576,
        "rows": ok_rows,
    }


def calibrate(store: Store, watcher: str) -> dict[str, Any]:
    """
    根据实际 delta 分布推荐 count_up_min 阈值。

    思路：绝大多数时间点的 delta 只是库存抖动。取正向 delta 的高分位数
    作为阈值，可以让告警只在真正异常的上涨时触发。
    """
    s = summarize(store, watcher)
    deltas = s["deltas"]
    ups = sorted(d for d in deltas if d > 0)

    out: dict[str, Any] = {
        "n_samples": len(deltas),
        "ready": len(deltas) >= 30,
        "n_up": len(ups),
    }
    if not deltas:
        out["message"] = "还没有足够数据，至少需要两次成功抓取"
        return out

    out["delta_min"] = min(deltas)
    out["delta_max"] = max(deltas)
    out["delta_median"] = statistics.median(deltas)
    out["delta_abs_p90"] = sorted(abs(d) for d in deltas)[int(len(deltas) * 0.90)]

    if ups:
        out["up_median"] = statistics.median(ups)
        out["up_p90"] = ups[min(int(len(ups) * 0.90), len(ups) - 1)]
        out["up_p95"] = ups[min(int(len(ups) * 0.95), len(ups) - 1)]
        out["up_max"] = ups[-1]
        # 推荐值：取 p95 和「绝对值 p90 的两倍」里较大的那个，至少为 5
        rec = max(out["up_p95"], out["delta_abs_p90"] * 2, 5)
        out["recommended_count_up_min"] = int(rec)
        # 用推荐值回测，估算每天会被打扰几次
        hits = sum(1 for d in ups if d >= rec)
        span_days = max(len(deltas) / 96.0, 1 / 96.0)  # 15分钟一次 => 每天96次
        out["est_alerts_per_day"] = round(hits / span_days, 2)
    else:
        out["message"] = "样本期内总数从未上涨过，再多跑一段时间"

    return out


def heatmap(store: Store, watcher: str, metric: str = "brand_new") -> dict[str, Any]:
    """
    生成 星期 × 小时 的聚合矩阵。

    metric:
      "new_markdown" 每格内带 NEW_MARKDOWN 标的件数之和。Rack 每做一轮降价，
                   这个数就会跳 —— 是"重新定价"这件事最直接的探针。
      "top_change" 每格内"榜首发生变化"的次数。**配合 Newest 排序的 watcher
                   使用时，这是最干净的上新标记**：按上架时间倒序排列时，
                   任何一件真正的新商品都会落到第 0 位，所以榜首一变就是上新。
      "brand_new"  每格内"历史从未见过的 ID"数量之和。比 top_change 灵敏，
                   但包含第 71-72 名边界上的排序抖动，会偏高。
      "up"         每格内 total_count 正向增量之和。受买卖噪声干扰，仅供参考。
      "new"        每格内 n_new 之和（相对上一次的榜单换血量）。
      "polls"      每格内的抓取次数，用于检查采样是否均匀、有没有睡眠漏点。
    """
    rows = [r for r in store.snapshots(watcher) if r["ok"] and not r["anomalous"]]
    grid: dict[tuple[int, int], float] = defaultdict(float)
    counts: dict[tuple[int, int], int] = defaultdict(int)

    column = {"brand_new": "n_brand_new", "new": "n_new",
              "top_change": "primary_changed",
              "new_markdown": "n_new_markdown"}.get(metric)

    for r in rows:
        key = (int(r["dow"]), int(r["hour"]))
        counts[key] += 1
        if metric == "up":
            d = r["count_delta"]
            if d is not None and int(d) > 0:
                grid[key] += int(d)
        elif column:
            n = r[column]
            if n is not None:
                grid[key] += int(n)

    if metric == "polls":
        grid = {k: float(v) for k, v in counts.items()}

    return {"grid": dict(grid), "counts": dict(counts), "metric": metric, "n_rows": len(rows)}


def render_heatmap(hm: dict[str, Any]) -> str:
    """把 heatmap() 的结果渲染成终端 ASCII 图"""
    grid = hm["grid"]
    counts = hm["counts"]
    if not counts:
        return "(无数据)"

    vmax = max(grid.values()) if grid else 0.0
    lines = ["     " + "".join(f"{h%10}" for h in range(24)) + "   总计"]
    lines.append("     " + "-" * 24)
    for dow in range(7):
        row_total = 0.0
        cells = []
        for hour in range(24):
            key = (dow, hour)
            if key not in counts:
                cells.append("?")  # 该时段从没采样过
                continue
            v = grid.get(key, 0.0)
            row_total += v
            cells.append(_heat_char(v, vmax))
        lines.append(f"{DOW_NAMES[dow]}  " + "".join(cells) + f"   {row_total:>6.0f}")
    lines.append("")
    legend = f"图例: '{BLOCKS[1:]}' 由弱到强，峰值={vmax:.0f}"
    if hm.get("metric") == "asof":
        legend += "。数据来自商品自带的时间戳，不受采样时段限制，夜间同样可见"
    else:
        legend += "；'?' = 该时段从未采样（可能是电脑睡眠）"
    lines.append(legend)
    return "\n".join(lines)
