"""
Watcher 基类

新增一个被监控页面时，继承 Watcher 并实现 observe() 即可。
存储、diff、告警、调度都由框架统一处理，watcher 本身不碰这些。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Observation


class Watcher(ABC):
    """
    一个被监控的页面

    name  配置里的唯一标识，同时作为数据库的分区键
    cfg   该 watcher 在 config.toml 里的那一段配置
    """

    #: 配置中 type 字段的取值，用于注册表查找
    type_name: str = ""

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg

    @abstractmethod
    def observe(self) -> Observation:
        """抓取一次并解析成 Observation。失败时抛异常，由上层记录。"""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"
