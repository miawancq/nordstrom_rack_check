#!/usr/bin/env python3
"""
page-watch 命令行入口

常用命令：
  python3 run.py poll                  跑一次所有启用的 watcher（launchd 调用的就是这个）
  python3 run.py poll --dry-run        只抓取和判断，不写库不发通知
  python3 run.py status                看当前状态和最近的告警
  python3 run.py analyze               星期×小时 热力图，回答"什么时候更新"
  python3 run.py calibrate             根据实际数据推荐告警阈值
  python3 run.py events                列出历史告警
  python3 run.py test-notify           测试通知渠道是否通
  python3 run.py install-launchd       生成并加载 launchd 定时任务
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from pagewatch import analyze as an
from pagewatch import notify
from pagewatch.config import PROJECT_ROOT, load
from pagewatch.models import Alert
from pagewatch.runner import _rule_matches, poll_all, top_seen_band
from pagewatch.watchers import build
from pagewatch.store import Store

LAUNCHD_LABEL = "com.xwan.pagewatch"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _fmt_delta(v: int | None) -> str:
    if v is None:
        return "  n/a"
    return f"{v:+5d}"


def cmd_poll(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    store = Store(cfg.db_path)
    try:
        results = poll_all(cfg, store, only=args.watcher, dry_run=args.dry_run)
    finally:
        store.close()

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    exit_code = 0
    for r in results:
        if not r.ok:
            print(f"[{stamp}] {r.watcher}  失败: {r.error}", file=sys.stderr)
            exit_code = 1
            continue

        o = r.obs
        assert o is not None
        head = (
            f"[{stamp}] {r.watcher}  total={o.total_count:,} "
            f"Δ{_fmt_delta(r.count_delta)}  new={r.n_new} gone={r.n_gone}"
        )
        if r.n_brand_new is not None:
            head += f" 真新品={r.n_brand_new}"
        head += f"  {o.fetch_ms}ms {o.bytes_wire/1024:.0f}KB"
        print(head)
        if r.anomalous:
            print(f"           ⚠ 结果集异常，已忽略（{r.anomaly_reason}）")

        if o.primary:
            mark = " ← 换人了" if r.primary_changed else ""
            print(f"           榜首: {o.primary.label()} | {o.primary.price_line()}{mark}")
            if o.primary.flags():
                print(f"                 {o.primary.flags()}")
        n_blocked = sum(1 for i in o.items if i.shippable is False)
        if n_blocked:
            print(f"           本页有 {n_blocked}/{len(o.items)} 件仅限门店自提（网上买不了）")
        if r.first_run:
            print("           (首次运行，已建立基线，下次抓取开始产生对比)")
        if r.rule_hits:
            for rn, cnt in r.rule_hits.items():
                print(f"           规则「{rn}」命中 {cnt} 件")
        for a in r.alerts or []:
            print(f"           ⚠ [{a.kind}] {a.title}")
        for p in r.notify_problems or []:
            print(f"           通知问题: {p}", file=sys.stderr)
        if args.dry_run:
            print("           (dry-run：未写库、未发通知)")
    return exit_code


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    store = Store(cfg.db_path)
    try:
        names = args.watcher and [args.watcher] or (store.watcher_names() or list(cfg.watchers))
        for name in names:
            s = an.summarize(store, name)
            print(f"=== {name} ===")
            if not s["n_polls"]:
                print("  还没有任何抓取记录。先跑一次 `python3 run.py poll`\n")
                continue
            print(f"  抓取次数   {s['n_polls']}  (失败 {s['n_failed']})")
            print(f"  时间范围   {s['first_ts']}  →  {s['last_ts']}")
            print(f"  最新总数   {s['last_count']:,}" if s["last_count"] else "  最新总数   n/a")
            print(f"  变化分布   上涨 {s['n_up']} 次 / 下跌 {s['n_down']} 次 / 持平 {s['n_flat']} 次")
            if s["n_anomalous"]:
                print(f"  异常结果   {s['n_anomalous']} 次（站点返回残缺结果集，已自动忽略）")
            print(f"  榜首变化   {s['n_top_changes']} 次")
            if s["markdown_last"] is not None:
                print(f"  刚降价件数 当前 {s['markdown_last']}/72"
                      f"（区间 {s['markdown_min']}–{s['markdown_max']}）")
            print(f"  仅门店自提 {s['n_polls_with_blocked']} 次抓取见到（单次最多 "
                  f"{s['max_not_shippable']} 件）；榜首不可直邮 {s['n_primary_blocked']} 次")
            if s["n_polls_measured"]:
                print(f"  未见过的ID 累计 {s['total_brand_new']} 个，"
                      f"{s['n_polls_with_arrivals']}/{s['n_polls_measured']} 次抓取检测到")
            print(f"  累计流量   {s['total_wire_mb']:.1f} MB")
            last = s["rows"][-1] if s["rows"] else None
            if last and last["primary_title"]:
                print(f"  当前榜首   {last['primary_title']} "
                      f"(${last['primary_price']:.2f}, {last['primary_pct_off']}% off)")
            evs = store.events(name, limit=5)
            if evs:
                print("  最近告警:")
                for e in evs:
                    print(f"    {e['ts_local'][:19]}  [{e['kind']}]  {e['title']}")
            print()
    finally:
        store.close()
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    store = Store(cfg.db_path)
    try:
        names = args.watcher and [args.watcher] or (store.watcher_names() or list(cfg.watchers))
        for name in names:
            s = an.summarize(store, name)
            print(f"=== {name} · {args.metric} 热力图（本地时间） ===")
            if s["n_polls"] < 2:
                print("  数据不足，至少需要两次成功抓取。\n")
                continue
            print(f"  样本: {s['n_polls']} 次抓取，{s['first_ts'][:16]} → {s['last_ts'][:16]}\n")
            if args.metric == "asof":
                hm = an.asof_heatmap(store, name, cfg.timezone)
                print(f"  依据 {hm['n_observations']} 条 (商品, asOf) 观测"
                      f"，覆盖 {(hm['span_from'] or '?')[:16]} → "
                      f"{(hm['span_to'] or '?')[:16]} UTC")
                print("  （只增不改，跑得越久覆盖越完整；含电脑睡着的时段）\n")
                print(an.render_heatmap(hm))
            else:
                print(an.render_heatmap(an.heatmap(store, name, metric=args.metric)))
            print()
    finally:
        store.close()
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    store = Store(cfg.db_path)
    try:
        names = args.watcher and [args.watcher] or (store.watcher_names() or list(cfg.watchers))
        for name in names:
            c = an.calibrate(store, name)
            print(f"=== {name} 阈值校准 ===")
            print(f"  样本数 {c['n_samples']}  (建议 ≥30 再采信)")
            if "message" in c:
                print(f"  {c['message']}\n")
                continue
            print(f"  delta 范围   {c['delta_min']} .. {c['delta_max']}  中位数 {c['delta_median']}")
            print(f"  |delta| p90  {c['delta_abs_p90']}")
            if "up_p95" in c:
                print(f"  上涨 p50/p90/p95/max  "
                      f"{c['up_median']}/{c['up_p90']}/{c['up_p95']}/{c['up_max']}")
                print(f"\n  → 推荐 count_up_min = {c['recommended_count_up_min']}")
                print(f"    （按此阈值回测，约每天 {c['est_alerts_per_day']} 次告警）")
                print(f"    改 config.toml 里 [watcher.alerts] 的 count_up_min")
            if not c["ready"]:
                print("\n  ⚠ 样本还太少，建议再跑一两天")
            print()
    finally:
        store.close()
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    store = Store(cfg.db_path)
    try:
        evs = store.events(args.watcher, limit=args.limit)
        if not evs:
            print("还没有任何告警事件。")
            return 0
        for e in evs:
            print(f"{e['ts_local'][:19]}  {e['watcher']:<18} [{e['kind']}] {e['title']}")
            for line in (e["body"] or "").splitlines():
                print(f"    {line}")
            if e["url"]:
                print(f"    {e['url']}")
            if not e["notified"]:
                print("    (当时通知发送失败)")
            print()
    finally:
        store.close()
    return 0


def cmd_reseed_top(args: argparse.Namespace) -> int:
    """
    把当前前列条目写进"见过"记忆，但不发任何告警。

    改大 top_n 之后必须跑一次：记忆里原本只有旧窗口范围内的条目，
    新扩出来的那几名不在记忆里，下一轮会被当成新面孔误报一批。
    """
    cfg = load(args.config)
    store = Store(cfg.db_path)
    try:
        targets = {args.watcher: cfg.watchers[args.watcher]} if args.watcher \
            else cfg.enabled_watchers()
        for name, wcfg in targets.items():
            top_n = int(wcfg.get("alerts", {}).get("top_n", 5))
            band = top_seen_band(top_n)
            before = len(store.top_seen_map(name))
            try:
                obs = build(name, wcfg).observe()
            except Exception as exc:
                print(f"{name}: 抓取失败 {exc}", file=sys.stderr)
                continue
            store.touch_top_seen(name, obs.ranked_ids[:band], obs.ts)
            after = len(store.top_seen_map(name))
            print(f"  {name:<22} top_n={top_n} band={band}  "
                  f"排名记忆 {before} → {after} 个条目  (本次抓到 {len(obs.items)} 件)")
            for rule in wcfg.get("rule", []):
                if not rule.get("enabled", True):
                    continue
                rn = rule.get("name") or "rule"
                hits = _rule_matches(obs.items, rule)
                b4 = len(store.rule_seen_map(name, rn))
                store.touch_rule_seen(name, rn, [i.item_id for i in hits], obs.ts)
                print(f"    规则「{rn}」当前命中 {len(hits)} 件，"
                      f"规则记忆 {b4} → {len(store.rule_seen_map(name, rn))}")
                for it in hits:
                    print(f"      {it.pct_off}% off  ${it.price:<8.2f} "
                          f"直邮{it.ship_qty}件  {it.label()[:40]}")
    finally:
        store.close()
    print("已播种，下一轮抓取不会因为窗口变化而误报")
    return 0


def cmd_setup_email(args: argparse.Namespace) -> int:
    """
    交互式配置邮件告警。

    密码用 getpass 读取（不回显、不进 shell 历史），存进 macOS 钥匙串，
    config.toml 里只留一个 use_keychain 开关，不落明文。
    """
    import getpass
    import tomllib

    cfg_path = Path(args.config) if args.config else Path(PROJECT_ROOT / "config.toml")
    raw = tomllib.loads(cfg_path.read_text())
    email = raw.get("notify", {}).get("email", {})
    account = email.get("username") or ""
    if not account:
        print("config.toml 里 [notify.email] 的 username 还没填，先填好再跑这个命令。",
              file=sys.stderr)
        return 1

    print(f"账号: {account}")
    print("请粘贴 Gmail 应用专用密码（16 位，输入不会显示）：")
    pw = getpass.getpass("  密码: ").strip().replace(" ", "")
    if not pw:
        print("没有输入，已取消。", file=sys.stderr)
        return 1

    err = notify.keychain_set(account, pw)
    if err:
        print(err, file=sys.stderr)
        return 1
    print(f"✓ 已存入钥匙串（服务名 {notify.KEYCHAIN_SERVICE}）")

    # 打开开关
    text = cfg_path.read_text()
    block_start = text.index("[notify.email]")
    block_end = text.find("\n[", block_start + 1)
    block = text[block_start : block_end if block_end > 0 else len(text)]
    if "enabled = false" in block:
        text = text[:block_start] + block.replace("enabled = false", "enabled = true", 1) \
             + (text[block_end:] if block_end > 0 else "")
        cfg_path.write_text(text)
        print("✓ 已把 [notify.email] 的 enabled 改成 true")

    print("\n发送测试邮件...")
    return cmd_test_notify(args)


def cmd_blocks(args: argparse.Namespace) -> int:
    """
    被挡/被降级的情况汇总。

    用来回答：我们的抓取频率是不是触发了对方的限流？
    关键看**比例随时间的变化** —— 10-31 把轮询从 96 次/天提到 168 次/天，
    如果异常率跟着涨，就说明跟频率有关，需要退回去。
    """
    import collections

    cfg = load(args.config)
    store = Store(cfg.db_path)
    try:
        conn = store._conn
        tot, okc, anom, fail = conn.execute(
            "SELECT COUNT(*), SUM(ok=1 AND anomalous=0), SUM(ok=1 AND anomalous=1),"
            " SUM(ok=0) FROM snapshot"
        ).fetchone()
        print(f"=== 累计 {tot} 次抓取 ===")
        print(f"  正常      {okc or 0:>5}  ({(okc or 0)/tot:.2%})")
        print(f"  残缺结果  {anom or 0:>5}  ({(anom or 0)/tot:.2%})")
        print(f"  彻底失败  {fail or 0:>5}  ({(fail or 0)/tot:.2%})")

        print("\n=== 按天：异常率趋势（看频率提高后有没有变差）===")
        print(f"  {'日期':<9}{'抓取':>6}{'残缺':>6}{'失败':>6}{'异常率':>8}")
        rows = conn.execute(
            "SELECT substr(ts_local,1,10) d, COUNT(*) n,"
            " SUM(ok=1 AND anomalous=1) a, SUM(ok=0) f"
            " FROM snapshot GROUP BY d ORDER BY d"
        ).fetchall()
        for r in rows:
            bad = (r["a"] or 0) + (r["f"] or 0)
            bar = "█" * int(bad / max(r["n"], 1) * 200)
            print(f"  {r['d'][5:]:<9}{r['n']:>6}{r['a'] or 0:>6}{r['f'] or 0:>6}"
                  f"{bad/r['n']:>7.1%}  {bar}")

        print("\n=== 残缺结果的特征 ===")
        sig = conn.execute(
            "SELECT total_count, COUNT(*) n, MIN(http_status) st"
            " FROM snapshot WHERE anomalous=1 GROUP BY total_count ORDER BY n DESC"
        ).fetchall()
        for r in sig:
            print(f"  total={r['total_count']}  出现 {r['n']} 次  HTTP {r['st']}")
        print("  → total 恒为同一个值说明是服务端固定兜底集，不是随机截断")

        print("\n=== 失败原因 ===")
        for e, n in conn.execute(
            "SELECT error, COUNT(*) FROM snapshot WHERE ok=0 GROUP BY error"
            " ORDER BY 2 DESC"
        ):
            print(f"  {n:>3}  {(e or '')[:88]}")

        print("\n=== HTTP 状态码分布（有记录的）===")
        got = False
        for st, n in conn.execute(
            "SELECT http_status, COUNT(*) FROM snapshot WHERE http_status IS NOT NULL"
            " GROUP BY http_status ORDER BY 2 DESC"
        ):
            print(f"  {st}  {n} 次")
            got = True
        if not got:
            print("  （本次改动之前的记录没有状态码，之后会有）")

        print("\n=== 异常的时间分布（限流通常会聚集在高频时段）===")
        byh = collections.Counter(
            int(t[11:13]) for t, in conn.execute(
                "SELECT ts_local FROM snapshot WHERE ok=0 OR anomalous=1")
        )
        if byh:
            mx = max(byh.values())
            for h in range(24):
                n = byh.get(h, 0)
                if n:
                    print(f"  {h:>2}h  {'█' * int(n / mx * 30)}{n}")
        print("\n=== 连续失败（真被封会连着好几次）===")
        seq = conn.execute(
            "SELECT ts_local, (ok=0 OR anomalous=1) bad FROM snapshot ORDER BY id"
        ).fetchall()
        run = 0
        worst = 0
        for r in seq:
            run = run + 1 if r["bad"] else 0
            worst = max(worst, run)
        print(f"  最长连续异常 {worst} 次  "
              f"{'（孤立事件，不像被封）' if worst <= 2 else '（值得警惕）'}")
    finally:
        store.close()
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    alert = Alert(
        kind="TEST",
        title="page-watch 测试通知",
        body="如果你看到这条，说明通知渠道是通的。",
        url="https://www.nordstromrack.com/clearance",
        urgent=True,
    )
    problems = notify.dispatch([alert], cfg.notify, cfg.db_path.parent / '.state')
    if problems:
        for p in problems:
            print(f"✗ {p}", file=sys.stderr)
        return 1
    enabled = [k for k in ("macos", "email") if cfg.notify.get(k, {}).get("enabled")]
    print(f"✓ 已发送到: {', '.join(enabled) or '(没有启用任何渠道)'}")
    return 0


#: 站点的 `*/9` cron 在分钟 0,9,18,27,36,45,54 发布变更，批次要 1-3 分钟跑完。
#: 在每个批次刚结束时抓取，把发现延迟压到最低 —— 这比单纯缩短间隔更有效，
#: 因为它对齐的是站点真实的发布节拍，而不是随便挑个周期。
GRID_MINUTES = [3, 12, 21, 30, 39, 48, 57]

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        <string>poll</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{workdir}</string>
    {schedule}
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{workdir}/logs/poll.log</string>
    <key>StandardErrorPath</key>
    <string>{workdir}/logs/poll.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def cmd_install_launchd(args: argparse.Namespace) -> int:
    """
    生成 LaunchAgent 并加载。

    用 launchd 而不是 cron 的原因：Mac 合盖睡眠期间 cron 的任务会被直接丢弃，
    launchd 在唤醒后会补跑，时间序列不会出现大段空洞。

    --grid 对齐站点的 `*/9` cron，在每批发布后 3 分钟抓取，延迟最低。
    """
    if args.grid:
        sched = ("<key>StartCalendarInterval</key>\n    <array>\n"
                 + "".join(f"        <dict><key>Minute</key>"
                           f"<integer>{m}</integer></dict>\n"
                           for m in GRID_MINUTES)
                 + "    </array>")
        desc = f"对齐站点 */9 节拍，每小时 {len(GRID_MINUTES)} 次（分钟 {GRID_MINUTES}）"
    else:
        sched = (f"<key>StartInterval</key>\n    "
                 f"<integer>{args.interval * 60}</integer>")
        desc = f"每 {args.interval} 分钟一次"
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    plist = PLIST_TEMPLATE.format(
        label=LAUNCHD_LABEL,
        python=sys.executable,
        script=str(PROJECT_ROOT / "run.py"),
        workdir=str(PROJECT_ROOT),
        schedule=sched,
    )
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist)
    print(f"已写入 {PLIST_PATH}")

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
                   capture_output=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print(f"launchctl bootstrap 失败: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"已加载，{desc}。日志: {PROJECT_ROOT}/logs/poll.log")
    print(f"停止:  launchctl bootout gui/{uid}/{LAUNCHD_LABEL}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="run.py", description="page-watch 页面变化监控")
    ap.add_argument("--config", help="配置文件路径，默认 ./config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("poll", help="抓取一次")
    p.add_argument("--watcher", help="只跑指定的 watcher")
    p.add_argument("--dry-run", action="store_true", help="不写库不发通知")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("status", help="查看状态")
    p.add_argument("--watcher")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("analyze", help="星期×小时热力图")
    p.add_argument("--watcher")
    p.add_argument("--metric",
                   choices=["asof", "top_change", "new_markdown", "brand_new", "up",
                            "new", "polls"],
                   default="top_change",
                   help="asof=站点自报的重新定价时刻(唯一能看到夜间的), "
                        "top_change=榜首变化次数(配合 Newest watcher 即上新时刻), "
                        "new_markdown=本页刚降价件数(重新定价探针), "
                        "brand_new=未见过的ID数, up=总数上涨量, "
                        "new=榜单换血量, polls=采样次数")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("calibrate", help="推荐告警阈值")
    p.add_argument("--watcher")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("events", help="列出历史告警")
    p.add_argument("--watcher")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("blocks", help="被挡/被降级情况汇总，判断是否触发限流")
    p.set_defaults(func=cmd_blocks)

    p = sub.add_parser("test-notify", help="测试通知渠道")
    p.set_defaults(func=cmd_test_notify)

    p = sub.add_parser("reseed-top",
                       help="改过 top_n 后跑一次，避免误报一批新面孔")
    p.add_argument("--watcher")
    p.set_defaults(func=cmd_reseed_top)

    p = sub.add_parser("setup-email", help="交互式配置邮件（密码存进钥匙串）")
    p.set_defaults(func=cmd_setup_email)

    p = sub.add_parser("install-launchd", help="安装定时任务")
    p.add_argument("--interval", type=int, default=15, help="固定间隔分钟数，默认 15")
    p.add_argument("--grid", action="store_true",
                   help="对齐站点 */9 cron（分钟 3,12,21,30,39,48,57），延迟最低")
    p.set_defaults(func=cmd_install_launchd)

    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
