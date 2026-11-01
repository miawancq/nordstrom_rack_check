"""
SQLite 存储模块

三张表，均带 watcher 列，所以一个库可以同时装多个被监控页面：

  snapshot      每次抓取一行。这是回答"什么时候更新"的原始时间序列。
  item          见过的条目主表，记录 first_seen / last_seen，用于判断新上架。
  event         产生过的告警事件，便于事后统计和避免重复打扰。

设计取舍：snapshot 直接存 item_ids 的 JSON 数组而不是开一张关联表。
72 个 ID 约 700 字节，每天 96 次抓取也才 67KB，远比多一张表简单。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import Alert, Item, Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    watcher         TEXT    NOT NULL,
    ts_utc          TEXT    NOT NULL,   -- ISO8601 UTC
    ts_local        TEXT    NOT NULL,   -- ISO8601 本地时间，分析"几点更新"时直接用
    dow             INTEGER NOT NULL,   -- 本地星期，0=周一
    hour            INTEGER NOT NULL,   -- 本地小时 0-23
    ok              INTEGER NOT NULL DEFAULT 1,
    error           TEXT,
    anomalous       INTEGER NOT NULL DEFAULT 0,  -- 结果集异常，不可信，不参与 diff
    total_count     INTEGER,
    count_delta     INTEGER,            -- 相对上一次成功抓取的差值
    primary_item_id TEXT,
    primary_title   TEXT,
    primary_price   REAL,
    primary_pct_off INTEGER,
    primary_shippable INTEGER,          -- 榜首能否直邮（1/0/NULL=未知）
    primary_changed INTEGER NOT NULL DEFAULT 0,
    n_not_shippable INTEGER,            -- 本页明确买不到（仅门店自提）的件数
    n_new_markdown  INTEGER,            -- 本页带 NEW_MARKDOWN 标的件数（降价刷新探针）
    n_tag_new       INTEGER,            -- 本页带 NEW 标的件数
    n_items         INTEGER NOT NULL DEFAULT 0,
    n_new           INTEGER,            -- 相对上一次的新增 ID 数
    n_gone          INTEGER,            -- 相对上一次的消失 ID 数
    n_brand_new     INTEGER,            -- 历史从未见过的 ID 数（上新主信号）
    item_ids        TEXT,               -- JSON 数组：整页全部 ID（原始顺序）
    top_ids         TEXT,               -- JSON 数组：有效排名前 30（过滤掉买不到的），仅供事后分析
    n_new_in_top    INTEGER,            -- 本次进入前 N 名的新面孔数
    source_ts       TEXT,               -- 站点自报的数据快照时间
    fetch_ms        INTEGER,
    bytes_wire      INTEGER,
    http_status     INTEGER,            -- 最后一次请求的 HTTP 状态码
    pages_fetched   INTEGER             -- 实际抓了几页
);
CREATE INDEX IF NOT EXISTS ix_snapshot_watcher_ts ON snapshot(watcher, ts_utc);

CREATE TABLE IF NOT EXISTS item (
    watcher     TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    title       TEXT,
    brand       TEXT,
    price       REAL,
    compare_at  REAL,
    pct_off     INTEGER,
    url         TEXT,
    shippable   INTEGER,
    as_of       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    best_rank   INTEGER,
    PRIMARY KEY (watcher, item_id)
);

-- 只增不改的 asOf 观测表。
-- item.as_of 是 upsert 覆盖的，只留最新值；而要回答"过去几天分别在什么时候
-- 重新定价"，必须把每一个出现过的 (商品, asOf) 组合都留下来。
-- 主键去重，所以连续抓取看到同一批未变化的商品不会产生新行。
CREATE TABLE IF NOT EXISTS asof_seen (
    watcher  TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    as_of    TEXT NOT NULL,
    seen_at  TEXT NOT NULL,
    PRIMARY KEY (watcher, item_id, as_of)
);
CREATE INDEX IF NOT EXISTS ix_asof_watcher ON asof_seen(watcher);

-- 每个条目最近一次出现在"前 M 名"的时间。
-- 用途：判断一个条目是不是"真的刚进前 N 名"。只跟上一次快照比是不够的 ——
-- 前 5 名里挤进一件新货，原来的第 5 名会被推到第 6 名；新货卖掉后它回到第 5 名，
-- 相对上一次快照就成了"新面孔"，产生假警报。
-- 记录范围取前 10（比告警用的前 5 宽），这样被挤出去的那几件在 6-10 名期间
-- 记录仍在刷新，回来时自然不算新。
CREATE TABLE IF NOT EXISTS top_seen (
    watcher   TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (watcher, item_id)
);

-- 每条规则各自记住"哪些条目已经报过"。
-- 和 top_seen 是一个道理：一个条目只要持续满足规则，就不该每 15 分钟报一次。
-- 按规则分开记，所以调整某条规则不会影响别的规则的去重状态。
CREATE TABLE IF NOT EXISTS rule_seen (
    watcher   TEXT NOT NULL,
    rule      TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (watcher, rule, item_id)
);

CREATE TABLE IF NOT EXISTS event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    watcher   TEXT NOT NULL,
    ts_utc    TEXT NOT NULL,
    ts_local  TEXT NOT NULL,
    dow       INTEGER NOT NULL,
    hour      INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    item_id   TEXT,
    title     TEXT,
    body      TEXT,
    url       TEXT,
    notified  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_event_watcher_ts ON event(watcher, ts_utc);
"""


def _schema_columns(schema: str) -> dict[str, list[tuple[str, str]]]:
    """
    从 CREATE TABLE 语句里解析出 {表名: [(列名, 类型声明), ...]}。

    只用于迁移，所以跳过表级约束行（PRIMARY KEY (...) 这种）和注释。
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", schema, re.S
    ):
        table, body = m.group(1), m.group(2)
        cols: list[tuple[str, str]] = []
        for raw in body.split("\n"):
            line = raw.split("--")[0].strip().rstrip(",").strip()
            if not line:
                continue
            head = line.split()[0].upper()
            if head in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                cols.append((parts[0], parts[1]))
        out[table] = cols
    return out


class Store:
    """SQLite 存储封装。所有写操作都在单个事务里完成。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        # WAL 让读写互不阻塞，便于一边 cron 写一边手动 analyze 读
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """
        给已存在的旧库补上后加的列。

        列清单**从 SCHEMA 自动推导**，不再手写第二份。
        手写清单和 SCHEMA 会各走各的：往 SCHEMA 加了列但忘了同步清单，
        新库正常、旧库却缺列，而且直到运行时 INSERT 才炸。实际踩过这个坑。
        """
        for table, cols in _schema_columns(SCHEMA).items():
            existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # 表还没建（executescript 会建），跳过
            for col, decl in cols:
                if col in existing:
                    continue
                # SQLite 的 ADD COLUMN 不接受非常量默认值，也不接受 PRIMARY KEY；
                # NOT NULL 必须带 DEFAULT，SCHEMA 里都是这么写的，直接照搬即可
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """事务上下文：正常提交，异常回滚"""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- 读 ----------

    def last_snapshot(self, watcher: str) -> sqlite3.Row | None:
        """
        取最近一次"可信"的抓取记录，作为本次 diff 的基线。

        刻意排除 anomalous=1 的记录：站点偶尔会返回残缺结果集（实测见过
        6950 件变成 400 件），拿它当基线会让下一次抓取产生满屏假告警。
        """
        cur = self._conn.execute(
            "SELECT * FROM snapshot WHERE watcher=? AND ok=1 AND anomalous=0 "
            "ORDER BY id DESC LIMIT 1",
            (watcher,),
        )
        return cur.fetchone()

    def recent_counts(self, watcher: str, limit: int = 10) -> list[int]:
        """最近若干次可信抓取的总数，用于算中位数做异常判定"""
        cur = self._conn.execute(
            "SELECT total_count FROM snapshot WHERE watcher=? AND ok=1 AND anomalous=0"
            " AND total_count IS NOT NULL ORDER BY id DESC LIMIT ?",
            (watcher, limit),
        )
        return [int(r[0]) for r in cur]

    def snapshot_count(self, watcher: str) -> int:
        """该 watcher 已有的成功抓取次数，用于判断是否还在预热期"""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM snapshot WHERE watcher=? AND ok=1 AND anomalous=0",
            (watcher,),
        )
        return int(cur.fetchone()[0])

    def known_item_ids(self, watcher: str) -> set[str]:
        """该 watcher 历史上见过的全部条目 ID，用于区分'真新品'和'翻回榜单'"""
        cur = self._conn.execute("SELECT item_id FROM item WHERE watcher=?", (watcher,))
        return {r[0] for r in cur}

    def snapshots(self, watcher: str, limit: int = 10000) -> list[sqlite3.Row]:
        """按时间正序取抓取历史，供分析使用"""
        cur = self._conn.execute(
            "SELECT * FROM snapshot WHERE watcher=? ORDER BY id DESC LIMIT ?",
            (watcher, limit),
        )
        return list(reversed(cur.fetchall()))

    def top_seen_map(self, watcher: str) -> dict[str, datetime]:
        """每个条目最近一次出现在前 M 名的时间"""
        out: dict[str, datetime] = {}
        for item_id, last_seen in self._conn.execute(
            "SELECT item_id, last_seen FROM top_seen WHERE watcher=?", (watcher,)
        ):
            try:
                out[item_id] = datetime.fromisoformat(last_seen)
            except ValueError:
                continue
        return out

    def touch_top_seen(self, watcher: str, item_ids: list[str], ts: datetime) -> None:
        """刷新这批条目"最近出现在前 M 名"的时间戳"""
        if not item_ids:
            return
        with self._tx() as c:
            c.executemany(
                "INSERT INTO top_seen (watcher, item_id, last_seen) VALUES (?,?,?)"
                " ON CONFLICT(watcher, item_id) DO UPDATE SET last_seen=excluded.last_seen",
                [(watcher, i, ts.isoformat()) for i in item_ids],
            )

    def rule_seen_map(self, watcher: str, rule: str) -> dict[str, datetime]:
        """某条规则下，每个条目最近一次被判定命中的时间"""
        out: dict[str, datetime] = {}
        for item_id, last_seen in self._conn.execute(
            "SELECT item_id, last_seen FROM rule_seen WHERE watcher=? AND rule=?",
            (watcher, rule),
        ):
            try:
                out[item_id] = datetime.fromisoformat(last_seen)
            except ValueError:
                continue
        return out

    def touch_rule_seen(
        self, watcher: str, rule: str, item_ids: list[str], ts: datetime
    ) -> None:
        """刷新这批条目在该规则下的"已见过"时间戳"""
        if not item_ids:
            return
        with self._tx() as c:
            c.executemany(
                "INSERT INTO rule_seen (watcher, rule, item_id, last_seen)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(watcher, rule, item_id)"
                " DO UPDATE SET last_seen=excluded.last_seen",
                [(watcher, rule, i, ts.isoformat()) for i in item_ids],
            )

    def as_of_stamps(self, watcher: str) -> list[str]:
        """
        该 watcher 历史上观测到的所有 (商品, asOf) 组合，按组合去重。

        这是"夜里发生了什么"的唯一信息来源：商品自带上次被重新定价的时刻，
        所以白天抓一次就能回溯过去十几个小时，电脑睡着的时段也覆盖得到。

        读的是 asof_seen 而不是 item —— 后者会被 upsert 覆盖成最新值，
        跑几天也只能看到最近十几小时的滚动窗口，累积不起来。
        """
        cur = self._conn.execute(
            "SELECT as_of FROM asof_seen WHERE watcher=?", (watcher,)
        )
        return [r[0] for r in cur]

    def as_of_span(self, watcher: str) -> tuple[str | None, str | None, int]:
        """asOf 观测的时间跨度和总数，用于在图上标注覆盖范围"""
        cur = self._conn.execute(
            "SELECT MIN(as_of), MAX(as_of), COUNT(*) FROM asof_seen WHERE watcher=?",
            (watcher,),
        )
        row = cur.fetchone()
        return row[0], row[1], int(row[2] or 0)

    def events(self, watcher: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        """按时间倒序取告警事件"""
        if watcher:
            cur = self._conn.execute(
                "SELECT * FROM event WHERE watcher=? ORDER BY id DESC LIMIT ?", (watcher, limit)
            )
        else:
            cur = self._conn.execute("SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,))
        return cur.fetchall()

    def watcher_names(self) -> list[str]:
        """库里出现过的所有 watcher 名字"""
        cur = self._conn.execute("SELECT DISTINCT watcher FROM snapshot ORDER BY watcher")
        return [r[0] for r in cur]

    # ---------- 写 ----------

    def record_failure(self, watcher: str, ts: datetime, ts_local: datetime, error: str) -> None:
        """记录一次失败的抓取。失败也要留痕，否则时间序列会出现无法解释的空洞。"""
        with self._tx() as c:
            c.execute(
                "INSERT INTO snapshot (watcher, ts_utc, ts_local, dow, hour, ok, error) "
                "VALUES (?,?,?,?,?,0,?)",
                (watcher, ts.isoformat(), ts_local.isoformat(),
                 ts_local.weekday(), ts_local.hour, error[:500]),
            )

    def record(
        self,
        obs: Observation,
        ts_local: datetime,
        *,
        count_delta: int | None = None,
        n_new: int | None = None,
        n_gone: int | None = None,
        n_brand_new: int | None = None,
        n_new_in_top: int | None = None,
        primary_changed: bool = False,
        track_items: bool = True,
        anomalous: bool = False,
        anomaly_reason: str | None = None,
    ) -> int:
        """
        写入一次成功的抓取结果，并 upsert 条目主表。

        diff 结果一律用关键字传入：这些参数都是同类型的可空整数，
        按位置传极易错位，而且每加一个字段就要改掉全部调用点。

        track_items 为 False 时只存总数和第一个条目，不落 item 表。
        """
        p = obs.primary
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO snapshot ("
                " watcher, ts_utc, ts_local, dow, hour, ok, anomalous,"
                " total_count, count_delta,"
                " primary_item_id, primary_title, primary_price, primary_pct_off,"
                " primary_shippable, primary_changed,"
                " n_items, n_new, n_gone, n_brand_new, n_not_shippable,"
                " n_new_markdown, n_tag_new, item_ids, top_ids, n_new_in_top,"
                " source_ts, fetch_ms, bytes_wire, http_status, pages_fetched, error"
                ") VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    obs.watcher, obs.ts.isoformat(), ts_local.isoformat(),
                    ts_local.weekday(), ts_local.hour, int(anomalous),
                    obs.total_count, count_delta,
                    p.item_id if p else None, p.label() if p else None,
                    p.price if p else None, p.pct_off if p else None,
                    None if (p is None or p.shippable is None) else int(p.shippable),
                    int(primary_changed),
                    len(obs.items), n_new, n_gone, n_brand_new,
                    sum(1 for i in obs.items if i.shippable is False) if obs.items else None,
                    sum(1 for i in obs.items if "NEW_MARKDOWN" in i.tags) if obs.items else None,
                    sum(1 for i in obs.items if "NEW" in i.tags) if obs.items else None,
                    json.dumps(obs.item_ids) if obs.items else None,
                    json.dumps(obs.ranked_ids[:30]) if obs.ranked_ids else None,
                    n_new_in_top,
                    obs.source_ts, obs.fetch_ms, obs.bytes_wire,
                    obs.http_status, obs.pages_fetched,
                    # 异常快照把判定原因写进 error —— 这一列对 ok=1 的行本来是空的，
                    # 复用它就不用再加一列，语义也一致："这条为什么不可信"
                    anomaly_reason,
                ),
            )
            snapshot_id = int(cur.lastrowid or 0)

            if not anomalous and obs.items:
                # asOf 不受 track_items 控制：它体积极小（主键去重后增长很慢），
                # 而且是唯一能看到夜间活动的数据，不该被"少存点"的开关关掉
                c.executemany(
                    "INSERT OR IGNORE INTO asof_seen (watcher, item_id, as_of, seen_at)"
                    " VALUES (?,?,?,?)",
                    [(obs.watcher, i.item_id, i.as_of, obs.ts.isoformat())
                     for i in obs.items if i.as_of],
                )

            if track_items and obs.items:
                now = obs.ts.isoformat()
                for rank, it in enumerate(obs.items):
                    c.execute(
                        "INSERT INTO item (watcher, item_id, title, brand, price, compare_at,"
                        " pct_off, url, shippable, as_of, first_seen, last_seen, best_rank)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(watcher, item_id) DO UPDATE SET"
                        "   title=excluded.title, brand=excluded.brand, price=excluded.price,"
                        "   compare_at=excluded.compare_at, pct_off=excluded.pct_off,"
                        "   url=excluded.url, shippable=excluded.shippable,"
                        "   as_of=excluded.as_of,"
                        "   last_seen=excluded.last_seen,"
                        "   best_rank=MIN(IFNULL(item.best_rank, 999999), excluded.best_rank)",
                        (obs.watcher, it.item_id, it.title, it.brand, it.price, it.compare_at,
                         it.pct_off, it.url,
                         None if it.shippable is None else int(it.shippable),
                         it.as_of, now, now, rank),
                    )
        return snapshot_id

    def record_events(self, watcher: str, ts: datetime, ts_local: datetime,
                      alerts: list[Alert], notified: bool) -> None:
        """记录本次产生的告警事件"""
        if not alerts:
            return
        with self._tx() as c:
            c.executemany(
                "INSERT INTO event (watcher, ts_utc, ts_local, dow, hour, kind, item_id,"
                " title, body, url, notified) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (watcher, ts.isoformat(), ts_local.isoformat(),
                     ts_local.weekday(), ts_local.hour,
                     a.kind, a.item_id, a.title, a.body, a.url, int(notified))
                    for a in alerts
                ],
            )
