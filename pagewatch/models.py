"""
数据模型模块

定义所有 watcher 共用的通用数据结构。设计目标是"一个列表页"的抽象：
不管监控的是 Nordstrom Rack 清仓页、还是以后别的任何商品列表页，
都可以归约成 "总数 + 排第一的条目 + 本页条目 ID 集合" 这三样东西。

因此新增一个 watcher 时只需要实现 fetch/parse，不用碰存储和告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Item:
    """
    列表页中的单个条目（商品）

    除 item_id 和 title 外都是可选的，因为不同站点能拿到的字段不一样。
    """

    item_id: str
    title: str
    brand: str | None = None
    price: float | None = None  # 现价
    compare_at: float | None = None  # 原价 / 对比价
    pct_off: int | None = None  # 折扣百分比
    url: str | None = None
    shippable: bool | None = None  # 能否直邮。False = 仅限门店自提，网上买不了
    ship_qty: int | None = None  # 可直邮的件数。为 1 时意味着手慢就没了
    inventory_warning: str | None = None  # 站点给的库存提示，如 low-inventory
    tags: tuple[str, ...] = ()  # 站点打的标签，如 NEW_MARKDOWN / FINAL_SALE / NEW
    as_of: str | None = None  # 站点自报：这条商品数据上次被重新计算的时刻（UTC ISO）

    def price_line(self) -> str:
        """生成人类可读的一行价格描述，用于告警正文"""
        bits: list[str] = []
        if self.price is not None:
            bits.append(f"${self.price:,.2f}")
        if self.compare_at:
            bits.append(f"(was ${self.compare_at:,.2f})")
        if self.pct_off is not None:
            bits.append(f"{self.pct_off}% off")
        return " ".join(bits) if bits else "price n/a"

    def label(self) -> str:
        """品牌 + 标题，品牌缺失时只返回标题"""
        return f"{self.brand} / {self.title}" if self.brand else self.title

    def flags(self) -> str:
        """把配送和库存状态拼成一行提示，没有可提示的就返回空串"""
        bits: list[str] = []
        if self.shippable is False:
            bits.append("⚠ 仅门店自提，网上买不了")
        if self.ship_qty is not None and 0 < self.ship_qty <= 3:
            bits.append(f"可直邮仅剩 {self.ship_qty} 件")
        if "NEW_MARKDOWN" in self.tags:
            bits.append("刚降价")
        if self.inventory_warning == "low-inventory":
            bits.append("库存紧张")
        elif self.inventory_warning:
            bits.append(str(self.inventory_warning))
        if "FINAL_SALE" in self.tags:
            bits.append("不可退")
        return " · ".join(bits)


@dataclass
class Observation:
    """
    一次抓取的观测结果

    这是 watcher 的输出、也是存储层和告警层的输入。
    """

    watcher: str
    ts: datetime  # UTC 时间
    total_count: int | None = None  # 页面声明的结果总数
    primary: Item | None = None  # 排序后的第一个条目
    items: list[Item] = field(default_factory=list)  # 本页全部条目（可能为空）
    ranked_ids: list[str] = field(default_factory=list)
    """
    有效排名：经过可买性过滤后的条目 ID 顺序，primary 就是它的第 0 个。

    和 items 分开存是因为 items 要保留整页原始数据（给 asOf / 新品统计用），
    而告警只该看"你真能买到的那几个"。
    """

    source_ts: str | None = None  # 站点自己给出的数据快照时间（如有）
    fetch_ms: int = 0
    bytes_wire: int = 0  # 实际网络传输字节数（压缩后）
    http_status: int | None = None  # 最后一次请求的 HTTP 状态码
    pages_fetched: int = 0  # 实际抓了几页（自动翻页时每次可能不同）

    @property
    def item_ids(self) -> list[str]:
        """本次观测到的条目 ID 列表，顺序即页面排序"""
        return [i.item_id for i in self.items]


@dataclass
class Alert:
    """
    一条待发送的告警

    kind 用于去重和统计；urgent 决定 macOS 通知是否带声音。
    """

    kind: str  # PRIMARY_CHANGED / COUNT_UP / ...
    title: str  # 通知标题（短）
    body: str  # 通知正文
    url: str | None = None
    urgent: bool = False
    item_id: str | None = None
