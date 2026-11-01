"""
Watcher 注册表

按 config.toml 里的 type 字段把配置映射到具体的 Watcher 类。
新增站点时在 REGISTRY 里加一行即可。
"""

from __future__ import annotations

from typing import Any

from .base import Watcher
from .nordstrom_rack import NordstromRackWatcher

REGISTRY: dict[str, type[Watcher]] = {
    NordstromRackWatcher.type_name: NordstromRackWatcher,
}


def build(name: str, cfg: dict[str, Any]) -> Watcher:
    """根据配置构造一个 Watcher 实例"""
    type_name = cfg.get("type")
    if not type_name:
        raise ValueError(f"watcher '{name}' 缺少 type 字段")
    cls = REGISTRY.get(type_name)
    if cls is None:
        known = ", ".join(sorted(REGISTRY)) or "(无)"
        raise ValueError(f"watcher '{name}' 的 type='{type_name}' 未注册，已知类型: {known}")
    return cls(name, cfg)


__all__ = ["REGISTRY", "Watcher", "build"]
