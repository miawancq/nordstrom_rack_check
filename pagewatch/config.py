"""
配置加载模块

用标准库 tomllib 读 config.toml（Python 3.11+ 内置）。
配置分两层：[notify] 全局告警渠道，[[watcher]] 每个被监控页面一段。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
DEFAULT_DB = PROJECT_ROOT / "data.db"


@dataclass
class Config:
    """解析后的完整配置"""

    db_path: Path = DEFAULT_DB
    timezone: str | None = None  # None = 跟随系统本地时区
    notify: dict[str, Any] = field(default_factory=dict)
    watchers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def enabled_watchers(self) -> dict[str, dict[str, Any]]:
        """只返回 enabled=true 的 watcher"""
        return {n: c for n, c in self.watchers.items() if c.get("enabled", True)}


def load(path: Path | str | None = None) -> Config:
    """
    读取并校验配置文件。

    校验点：watcher 必须有唯一的 name，必须有 type。
    这些错误在启动时暴露，总好过在凌晨三点的 cron 里静默失败。
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")

    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    db_raw = raw.get("db_path")
    db_path = Path(db_raw).expanduser() if db_raw else DEFAULT_DB
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    watchers: dict[str, dict[str, Any]] = {}
    for entry in raw.get("watcher", []):
        name = entry.get("name")
        if not name:
            raise ValueError("存在没有 name 字段的 [[watcher]] 配置块")
        if name in watchers:
            raise ValueError(f"watcher 名字重复: {name}")
        if not entry.get("type"):
            raise ValueError(f"watcher '{name}' 缺少 type 字段")
        watchers[name] = entry

    return Config(
        db_path=db_path,
        timezone=raw.get("timezone"),
        notify=raw.get("notify", {}),
        watchers=watchers,
    )
