"""
告警通知模块

支持两个渠道，互不影响，任一渠道失败不影响另一个：
  - macOS 系统通知：走 osascript，零配置零依赖
  - 邮件：走 smtplib，需要在 config.toml 里填 SMTP 凭据

设计原则：通知失败绝不能让抓取任务崩溃，所有异常都捕获并返回错误串，
由上层记录日志。时间序列数据的完整性比告警送达更重要。
"""

from __future__ import annotations

import contextlib
import html
import os
import signal
import json
import smtplib
import subprocess
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .models import Alert, Item


def _osascript_escape(s: str) -> str:
    """
    转义 AppleScript 字符串字面量。

    顺序很重要：先转反斜杠，再转引号，最后才把真实换行变成 \n 转义序列 ——
    反过来的话刚插入的 \n 会被当成待转义的反斜杠而变成 \\n（字面两个字符）。
    """
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    return out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _pid_is_osascript(pid: int) -> bool:
    """
    确认 pid 真的是我们弹出的 osascript，再去杀它。

    PID 会被系统复用，直接照着 pidfile 里的号码杀，有可能误杀无关进程。
    """
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "osascript" in r.stdout


def dismiss_previous(state_dir: Path) -> None:
    """
    关掉上一个还挂在屏幕上的持久窗口。

    这就是"人不在时被下一条替换"的实现：杀掉 osascript 进程，它的窗口随之消失。
    """
    pidfile = state_dir / "alert.pid"
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        return
    if _pid_is_osascript(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    pidfile.unlink(missing_ok=True)


def send_macos_persistent(
    alerts: list[Alert], state_dir: Path, sound: bool = True
) -> str | None:
    """
    弹一个**不会自动消失**的窗口，停在屏幕上直到用户点掉。

    和 display notification 的区别：横幅几秒就自己没了，人不在就永远错过；
    这个会一直等。再来新告警时先把旧窗口关掉，所以离开一整天回来也只有
    最新的一个，不会堆成一屏。

    本次所有告警合并进同一个窗口 —— 否则同一轮里的多条会互相替换掉。
    进程不等待返回，立刻放回后台。
    """
    if not alerts:
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    dismiss_previous(state_dir)

    head = alerts[0].title if len(alerts) == 1 else f"{len(alerts)} 条变化"
    blocks = []
    for a in alerts:
        blocks.append(f"[{a.title}]\n{a.body}")
    body = "\n\n".join(blocks)

    url = next((a.url for a in alerts if a.url), None)
    if url:
        script = (
            f'set r to display alert "{_osascript_escape(head)}" '
            f'message "{_osascript_escape(body)}" '
            f'buttons {{"知道了", "打开链接"}} default button "打开链接"\n'
            f'if button returned of r is "打开链接" then '
            f'open location "{_osascript_escape(url)}"'
        )
    else:
        script = (
            f'display alert "{_osascript_escape(head)}" '
            f'message "{_osascript_escape(body)}" buttons {{"知道了"}}'
        )

    try:
        if sound:
            play_sound()
        # start_new_session 是必须的，不是可选优化：
        # launchd 任务结束时会回收整个进程组，普通子进程会被连带杀掉，
        # 窗口一闪就没。实测验证过——不加这个参数，launchd 发的窗口
        # 全部活不过任务退出，而邮件照常送达，表现就是"有邮件没弹窗"。
        proc = subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return f"弹出持久窗口失败: {exc}"

    # 确认它真的活下来了。只看 Popen 没报错是不够的 —— osascript 可能
    # 因为没有 GUI 会话、权限不足等原因立刻退出，那样 notified 就会谎报成功。
    time.sleep(0.4)
    rc = proc.poll()
    if rc is not None:
        detail = ""
        if proc.stderr:
            with contextlib.suppress(Exception):
                detail = proc.stderr.read().decode(errors="replace").strip()
        return f"持久窗口立刻退出（码 {rc}）{(': ' + detail[:200]) if detail else ''}"

    (state_dir / "alert.pid").write_text(str(proc.pid))
    return None


SOUNDS_DIR = Path("/System/Library/Sounds")


def system_volume() -> int | None:
    """读取当前系统输出音量（0-100）。读不到返回 None。"""
    try:
        r = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


#: afplay -v 的安全区间。太小听不见，太大会削波失真。
_V_MIN, _V_MAX = 0.5, 12.0


def amplification(target: int, current: int | None, fallback: float) -> float:
    """
    算出要把 afplay 放大多少倍，才能让提示音听起来相当于"系统音量 target"。

    固定倍数不行：用户把主音量从 25 调到 80 之后，同样的倍数会炸耳朵；
    调回去又听不见。按当前主音量反推倍数，不管滑块在哪都是一致的响度。
    """
    if not current:
        return fallback
    return max(_V_MIN, min(_V_MAX, target / current))


def play_sound(
    name: str = "Submarine",
    volume: float = 2.0,
    target_volume: int | None = None,
) -> str | None:
    """
    用 afplay 独立播放提示音，不走通知自带的声音。

    通知自带的 `sound name "..."` 走系统提示音通道，会被专注模式、全屏应用
    抑制，音量也只是主音量的一个比例 —— 放音乐或看视频时基本听不见。
    afplay 是普通音频播放，走默认输出设备和主音量，不受这些影响。

    target_volume 给定时按当前主音量自适应放大（推荐）；否则用固定的 volume。

    start_new_session 同样是必须的：launchd 任务退出会回收进程组，
    不加的话声音会被掐断（和持久窗口是同一个坑）。
    """
    path = SOUNDS_DIR / f"{name}.aiff"
    if not path.exists():
        return f"提示音文件不存在: {path}"
    v = (amplification(target_volume, system_volume(), volume)
         if target_volume else volume)
    try:
        subprocess.Popen(
            ["afplay", "-v", f"{v:.2f}", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return f"afplay 调用失败: {exc}"
    return None


def send_macos(alert: Alert, sound: bool = True) -> str | None:
    """
    弹一条 macOS 系统通知（屏幕右上角，显示在主屏上）。

    注意：display notification 正文不支持换行，这里压成一行。

    会不会自动消失由**系统设置**决定，代码控制不了：
    系统设置 → 通知 → 脚本编辑器 → 提醒样式，选「持续提醒」就会一直停着。
    另外 osascript 退出码 0 不代表用户真的看见了 —— 被系统静默丢弃时也是 0。
    """
    body = " · ".join(line for line in alert.body.splitlines() if line.strip())
    script = (
        f'display notification "{_osascript_escape(body)}" '
        f'with title "{_osascript_escape(alert.title)}"'
    )
    # 刻意不加 `sound name "..."` —— 那条通道会被专注模式和全屏应用静音。
    # 声音统一由 play_sound() 用 afplay 独立播放。
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"osascript 调用失败: {exc}"
    if proc.returncode != 0:
        return f"osascript 退出码 {proc.returncode}: {proc.stderr.strip()}"
    return None


def send_macos_merged(alerts: list[Alert], sound: bool = True) -> str | None:
    """
    把本次所有告警合并成**一条**系统通知。

    不合并的话同一轮里的两三条会在角落摞成一叠，看着比一条乱。
    详情走邮件，角落这条只负责"有事发生了，是什么事"。
    """
    if not alerts:
        return None
    if len(alerts) == 1:
        return send_macos(alerts[0], sound=sound)

    title = f"{len(alerts)} 条变化"
    parts = [a.title for a in alerts]
    merged = Alert(
        kind="MERGED",
        title=title,
        body=" / ".join(parts),
        url=next((a.url for a in alerts if a.url), None),
        urgent=any(a.urgent for a in alerts),
    )
    return send_macos(merged, sound=sound)


KEYCHAIN_SERVICE = "page-watch-smtp"


def keychain_get(account: str) -> str | None:
    """
    从 macOS 钥匙串读取 SMTP 密码。读不到返回 None。

    这样 config.toml 里就不用躺一个明文的 Gmail 应用专用密码。
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", account, "-w"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def keychain_set(account: str, password: str) -> str | None:
    """把密码写入钥匙串（-U 表示已存在则更新）。成功返回 None。"""
    try:
        proc = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
             "-a", account, "-w", password],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"写入钥匙串失败: {exc}"
    if proc.returncode != 0:
        return f"写入钥匙串失败: {proc.stderr.strip()}"
    return None


APP_PASSWORD_HINT = (
    "Gmail 应用专用密码应为 16 位纯小写字母。普通登录密码自 2022 年起已不被 "
    "SMTP 接受，且 Gmail 会直接挂断连接而不返回错误码。"
    "生成地址: https://myaccount.google.com/apppasswords"
    "（需先开启两步验证）"
)


def looks_like_app_password(pw: str) -> bool:
    """判断密码是否符合 Gmail 应用专用密码的形态（16 位纯小写字母）"""
    return len(pw) == 16 and pw.isalpha() and pw.islower()


def resolve_password(cfg: dict[str, Any]) -> str | None:
    """
    取 SMTP 密码。优先钥匙串，其次 config.toml 里的明文字段。

    钥匙串优先是为了让"把密码从配置文件里挪走"成为默认路径，
    同时又不破坏已经在用明文配置的情况。
    """
    if cfg.get("use_keychain", True):
        pw = keychain_get(str(cfg.get("username") or ""))
        if pw:
            return _clean(pw)
    raw = cfg.get("password")
    return _clean(raw) if raw else None


def _clean(pw: str) -> str:
    """
    去掉密码里的空白字符。

    Google 展示应用专用密码时是 "abcd efgh ijkl mnop" 这种带空格的四段式，
    直接复制粘贴会带着空格，不处理就会认证失败。
    """
    return "".join(str(pw).split())


#: Bark 推送的超时。比邮件短得多 —— 它的全部价值就是"人不在电脑旁时手机能响"，
#: 为一条推送阻塞住后面的邮件不值得。
BARK_TIMEOUT = 10.0


def _bark_items(a: Alert, min_compare_at: float | None) -> list[Item]:
    """
    这条告警里够格推手机的商品。

    min_compare_at 为空（默认）就是全推。设了值则只留原价达标的那些 ——
    实测按现有规则每天会产生 15~35 条告警，醒着的时候平均 45 分钟响一次，
    全推到锁屏上大概率会被静音，那这个渠道就白做了。
    把门槛放在这里而不是改规则，是因为邮件要继续收全量：
    手机负责"现在就得看"，邮件负责"回头翻"。
    """
    if not min_compare_at:
        return list(a.items)
    return [i for i in a.items
            if i.compare_at is not None and i.compare_at >= min_compare_at]


def _bark_body(items: list[Item], fallback: str) -> str:
    """
    推送正文。价格放最前面 —— 锁屏上只扫一眼，先看到的应该是"多少钱"。
    """
    if not items:
        return fallback
    lines = []
    for it in items:
        bits = [f"${it.price:,.2f}" if it.price is not None else "价格未知"]
        if it.pct_off is not None:
            bits.append(f"{it.pct_off}% off")
        bits.append(it.label())
        lines.append(" · ".join(bits))
    return "\n".join(lines)


def send_bark(alerts: list[Alert], cfg: dict[str, Any]) -> str | None:
    """
    推送到 Bark（iOS）。成功返回 None，失败返回错误描述。

    一条告警推一条通知，不合并 —— Bark 一条通知只能挂一个 URL，
    合并之后点开只能到其中一件，不如分开推、各自可点。

    cfg: key（设备码，必填）/ server / group / sound / min_compare_at
    """
    key = str(cfg.get("key") or "").strip()
    if not key:
        return "Bark 未配置 key（跳过发送）"
    server = str(cfg.get("server") or "https://api.day.app").rstrip("/")
    min_compare_at = cfg.get("min_compare_at")
    min_compare_at = float(min_compare_at) if min_compare_at else None

    problems: list[str] = []
    for a in alerts:
        items = _bark_items(a, min_compare_at)
        # 带商品的告警，一件都不达标就整条跳过；本来就没商品的（COUNT_UP）照推
        if a.items and not items:
            continue
        payload: dict[str, Any] = {
            "title": a.title,
            "body": _bark_body(items, a.body),
            # 同一个 watcher 的推送在通知中心归成一组，不会刷屏
            "group": str(cfg.get("group") or "page-watch"),
            # timeSensitive 能穿透专注模式 —— 这个渠道存在的理由就是别错过
            "level": "timeSensitive" if a.urgent else "passive",
        }
        url = (items[0].url if items else None) or a.url
        if url:
            payload["url"] = url
        if sound := cfg.get("sound"):
            payload["sound"] = str(sound)

        req = urllib.request.Request(
            f"{server}/{key}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=BARK_TIMEOUT) as resp:
                raw = resp.read(500).decode(errors="replace")
            body = json.loads(raw)
            if int(body.get("code", 0)) != 200:
                problems.append(f"Bark 返回 {body.get('code')}: {body.get('message')}")
        except urllib.error.HTTPError as exc:
            problems.append(f"Bark HTTP {exc.code}: {exc.reason}")
        except (urllib.error.URLError, OSError) as exc:
            problems.append(f"Bark 请求失败: {type(exc).__name__}: {exc}")
        except (ValueError, TypeError) as exc:
            problems.append(f"Bark 响应解析失败: {exc}")

    return "; ".join(problems) if problems else None


def render_text(alerts: list[Alert]) -> str:
    """
    邮件的纯文本正文：每件商品一行，**紧跟自己的链接**。

    Alert.body 本身只有商品名和价格，没有链接 —— 它是给 macOS 通知用的，
    塞几条 URL 进去会把气泡撑爆。所以带商品的告警在这里用 items 重新渲染，
    不复用 body；没有商品的（COUNT_UP 之类）才退回 body。
    """
    blocks: list[str] = []
    for a in alerts:
        lines = [f"[{a.kind}] {a.title}"]
        if a.items:
            for it in a.items:
                lines.append("")
                lines.append(f"  {it.label()}")
                flags = it.flags()
                lines.append(f"  {it.price_line()}" + (f"  ·  {flags}" if flags else ""))
                if it.url:
                    lines.append(f"  {it.url}")
            if a.context:
                lines.append("")
                lines.append(a.context)
        else:
            lines.append(a.body)
            if a.url:
                lines.append(a.url)
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)


def _item_html(it: Item) -> str:
    """单件商品的 HTML：商品名就是链接，下面一行价格和状态"""
    name = html.escape(it.label())
    link = (f'<a href="{html.escape(it.url, quote=True)}"'
            f' style="color:#0b63ce;text-decoration:none;font-weight:600">{name}</a>'
            if it.url else f'<span style="font-weight:600">{name}</span>')
    bits = [f'<span style="color:#111">{html.escape(it.price_line())}</span>']
    if flags := it.flags():
        # 库存紧张之类的提示用橙色，一眼能从价格里区分出来
        bits.append(f'<span style="color:#b35309">{html.escape(flags)}</span>')
    return (
        '<tr><td style="padding:8px 0;border-bottom:1px solid #eee">'
        f'{link}<br>'
        f'<span style="font-size:13px">{"&nbsp;·&nbsp;".join(bits)}</span>'
        '</td></tr>'
    )


def render_html(alerts: list[Alert]) -> str:
    """
    邮件的 HTML 正文：每件商品渲染成一个可点的超链接。

    样式全部内联 —— Gmail 会把 <style> 块整个丢掉。
    """
    blocks: list[str] = []
    for a in alerts:
        head = (f'<div style="font-weight:600;font-size:15px;margin:0 0 6px">'
                f'{html.escape(a.title)}</div>'
                f'<div style="font-size:12px;color:#888;margin:0 0 10px">'
                f'{html.escape(a.kind)}</div>')
        if a.items:
            rows = "".join(_item_html(it) for it in a.items)
            body = f'<table style="width:100%;border-collapse:collapse">{rows}</table>'
            if a.context:
                body += (f'<div style="margin-top:8px;font-size:13px;color:#666">'
                         f'{html.escape(a.context)}</div>')
        else:
            # 没有商品的告警（只报数量）原样呈现 body，保留换行
            body = (f'<div style="white-space:pre-wrap">{html.escape(a.body)}</div>')
            if a.url:
                body += (f'<div style="margin-top:6px"><a href='
                         f'"{html.escape(a.url, quote=True)}">打开页面</a></div>')
        blocks.append(f'<div style="margin:0 0 26px">{head}{body}</div>')

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        'Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#222">'
        + "".join(blocks) +
        '</div>'
    )


def send_email(alerts: list[Alert], cfg: dict[str, Any]) -> str | None:
    """
    把本次所有告警合成一封邮件发出。成功返回 None，失败返回错误描述。

    cfg 需要 host / port / username / password / from / to，任一缺失即跳过。
    """
    missing = [k for k in ("host", "username", "from", "to") if not cfg.get(k)]
    password = resolve_password(cfg)
    if not password:
        missing.append("password（钥匙串和配置文件里都没有）")
    if missing:
        return f"邮件配置不完整，缺少: {', '.join(missing)}（跳过发送）"

    subject = alerts[0].title if len(alerts) == 1 else f"{len(alerts)} 条页面变化告警"

    msg = EmailMessage()
    msg["Subject"] = f"[page-watch] {subject}"
    msg["From"] = cfg["from"]
    recipients = [t.strip() for t in str(cfg["to"]).split(",") if t.strip()]
    msg["To"] = ", ".join(recipients)
    # 两份正文：纯文本兜底，HTML 让每件商品各自可点。
    # set_content + add_alternative 会自动组成 multipart/alternative，
    # 客户端挑一份渲染 —— Gmail 取 HTML，纯文本客户端取前者。
    msg.set_content(render_text(alerts))
    msg.add_alternative(render_html(alerts), subtype="html")

    port = int(cfg.get("port", 587))
    is_gmail = "gmail" in str(cfg.get("host", "")).lower()

    # 密码明显不对时直接拦下，不用等服务器把连接掐掉再猜原因
    if is_gmail and not looks_like_app_password(password):
        return (f"密码格式不对：当前 {len(password)} 位。{APP_PASSWORD_HINT}")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(cfg["host"], port, timeout=30) as s:
                s.login(cfg["username"], password)
                s.send_message(msg, to_addrs=recipients)
        else:
            with smtplib.SMTP(cfg["host"], port, timeout=30) as s:
                s.starttls()
                s.login(cfg["username"], password)
                s.send_message(msg, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error.decode(errors="replace") if exc.smtp_error else ""
        msg = f"SMTP 认证失败 ({exc.smtp_code}): {detail[:200]}"
        if is_gmail and not looks_like_app_password(password):
            msg += f"\n  → 当前密码 {len(password)} 位，不像应用专用密码。{APP_PASSWORD_HINT}"
        return msg
    except smtplib.SMTPServerDisconnected as exc:
        # Gmail 遇到明显不合法的凭据时会直接挂断，而不是回 535，
        # 原始报错 "Connection unexpectedly closed" 完全看不出原因。
        msg = f"SMTP 连接被服务器断开: {exc}"
        if is_gmail and not looks_like_app_password(password):
            msg += f"\n  → 当前密码 {len(password)} 位，不像应用专用密码。{APP_PASSWORD_HINT}"
        return msg
    except (smtplib.SMTPException, OSError) as exc:
        return f"SMTP 发送失败: {type(exc).__name__}: {exc}"
    return None


def dispatch(
    alerts: list[Alert],
    notify_cfg: dict[str, Any],
    state_dir: Path | None = None,
) -> list[str]:
    """
    把告警发往所有启用的渠道，返回错误信息列表（空列表代表全部成功）。
    """
    problems: list[str] = []
    if not alerts:
        return problems

    macos_cfg = notify_cfg.get("macos", {})
    if macos_cfg.get("enabled", True):
        sound = macos_cfg.get("sound", True)
        # 声音只放一次：同一轮多条告警合并成一条通知，声音也该只响一次
        if sound and any(a.urgent for a in alerts):
            err = play_sound(
                str(macos_cfg.get("sound_name", "Submarine")),
                float(macos_cfg.get("sound_volume", 2.0)),
                macos_cfg.get("sound_target_volume"),
            )
            if err:
                problems.append(err)
        sound = False  # 下面各分支不再重复放
        # style="center" 用屏幕中央的模态窗口（可替换，但位置无法控制）；
        # style="corner" 用系统通知，显示在主屏右上角 —— 位置和"是否常驻"
        # 都由 macOS 决定，代码管不了，要靠系统设置里的「提醒样式」。
        style = macos_cfg.get("style", "corner")
        if style == "center" and state_dir is not None:
            urgent = [a for a in alerts if a.urgent]
            rest = [a for a in alerts if not a.urgent]
            if urgent:
                err = send_macos_persistent(urgent, state_dir, sound=sound)
                if err:
                    problems.append(err)
            for a in rest:   # 不紧急的仍然走横幅，不打断你
                err = send_macos(a, sound=False)
                if err:
                    problems.append(err)
        else:
            err = send_macos_merged(alerts, sound=sound)
            if err:
                problems.append(err)

    bark_cfg = notify_cfg.get("bark", {})
    if bark_cfg.get("enabled", False):
        err = send_bark(alerts, bark_cfg)
        if err:
            problems.append(err)

    email_cfg = notify_cfg.get("email", {})
    if email_cfg.get("enabled", False):
        err = send_email(alerts, email_cfg)
        if err:
            problems.append(err)

    return problems
