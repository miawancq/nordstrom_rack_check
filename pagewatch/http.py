"""
HTTP 抓取模块

只用标准库，不引入 requests/httpx，保证 launchd 定时任务不依赖虚拟环境。

关键点：
- 主动声明 Accept-Encoding: gzip。实测 Nordstrom Rack 页面
  2.1MB -> 284KB，是唯一有意义的提速手段。
- 不声明 br（brotli），因为标准库解不了。
- 记录压缩后的实际传输字节数，用于流量统计。
"""

from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import zlib
from typing import NamedTuple

# 伪装成普通 Chrome。不加 UA 的话部分 CDN 会直接返回简化页面。
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """
    抓取失败（网络错误、超时、非 2xx 状态码）。

    status 保留 HTTP 状态码（没有的话是 None）。区分 429/403（被限流或拦截）
    和 5xx/网络错误（对方故障或本地网络问题）必须靠它 —— 光看异常文本
    事后分析很难归类。
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class FetchResult(NamedTuple):
    """一次抓取的结果与元信息"""

    text: str
    wire_bytes: int          # 压缩后的实际传输字节数
    elapsed_ms: int
    status: int              # HTTP 状态码


def fetch_text(url: str, timeout: float = 30.0, ua: str = DEFAULT_UA) -> FetchResult:
    """
    抓取一个 URL 并返回解码后的文本及元信息。
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        },
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            charset = resp.headers.get_content_charset() or "utf-8"
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTPError {exc.code}: {exc.reason}", status=exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    wire_bytes = len(raw)

    # 按服务端声明的编码解压
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        # 有的服务端给裸 deflate 流，有的给 zlib 包装，两种都试
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return FetchResult(raw.decode(charset, errors="replace"),
                       wire_bytes, elapsed_ms, status)
