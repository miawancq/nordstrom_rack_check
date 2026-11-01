#!/usr/bin/env python3
"""
从 config.toml 生成 config.example.toml。

必须用这个脚本，不要手工复制或字符串替换 —— 实际踩过：
用 `s.replace('"真邮箱"','"占位"')` 生成示例文件时只换了邮箱没换密码，
真实的 Gmail 应用专用密码就留在了会被提交的示例文件里。

做法是**按键名**清空敏感字段，再回头校验真实值没有残留。
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 这些键在示例文件里一律替换成占位值
PLACEHOLDER = {
    "password": '""',
    "username": '"你的@gmail.com"',
    "from": '"你的@gmail.com"',
    "to": '"你的@gmail.com"',
}

#: URL 里的邮编和门店号属于个人信息，示例文件里也要占位
URL_SCRUB = [
    (r"postalCodeAvailability=\d+", "postalCodeAvailability=YOUR_ZIP"),
    (r"preferredPostalCode=\d+", "preferredPostalCode=YOUR_ZIP"),
    (r"preferredStore=\d+", "preferredStore=YOUR_STORE_ID"),
]

HEADER = (
    "# 复制为 config.toml 后按注释修改。config.toml 不入 git。\n"
    "# 由 tools/make_example_config.py 生成，不要手工编辑。\n"
)


def build(src_text: str) -> str:
    out = []
    for line in src_text.splitlines():
        m = re.match(r"^(\s*)(\w+)(\s*=\s*)(.*?)(\s*#.*)?$", line)
        if m and m.group(2) in PLACEHOLDER:
            indent, key, eq, _val, comment = m.groups()
            out.append(f"{indent}{key}{eq}{PLACEHOLDER[key]}{comment or ''}")
        else:
            out.append(line)
    text = HEADER + "\n".join(out) + "\n"
    for pat, repl in URL_SCRUB:
        text = re.sub(pat, repl, text)
    return text


def leaked(example: str, src_text: str) -> list[str]:
    """返回仍然残留在示例文件里的真实敏感值对应的键名"""
    email = tomllib.loads(src_text).get("notify", {}).get("email", {})
    return [k for k in PLACEHOLDER
            if (v := str(email.get(k) or "")) and v in example]


def main() -> int:
    src = ROOT / "config.toml"
    if not src.exists():
        print("config.toml 不存在", file=sys.stderr)
        return 1
    text = src.read_text()
    example = build(text)
    bad = leaked(example, text)
    if bad:
        print(f"✗ 示例配置残留真实值: {', '.join(bad)}", file=sys.stderr)
        return 1
    (ROOT / "config.example.toml").write_text(example)
    print("✓ config.example.toml 已生成并通过校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
