"""SQLite に銘柄一覧と日足を保存する。

日付は YYYYMMDD の整数、銘柄は整数IDで持ち、約3,200銘柄×20年分でも
容量が膨らみすぎないようにしている。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    id      INTEGER PRIMARY KEY,
    symbol  TEXT NOT NULL UNIQUE,   -- yfinance のティッカー (7203.T, USDJPY=X)
    code    TEXT NOT NULL,          -- 表示用コード (7203, USD/JPY)
    name    TEXT NOT NULL,
    market  TEXT NOT NULL,          -- プライム / スタンダード / 為替
    kind    TEXT NOT NULL,          -- stock / fx
    unit    INTEGER NOT NULL,       -- 売買単位 (株は単元株数、為替は通貨数)
    sector  TEXT NOT NULL DEFAULT '',
    active  INTEGER NOT NULL DEFAULT 1  -- 最新の一覧に載っているか
);
CREATE TABLE IF NOT EXISTS bars (
    sid    INTEGER NOT NULL,
    d      INTEGER NOT NULL,        -- YYYYMMDD
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (sid, d)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fetch_state (
    sid        INTEGER PRIMARY KEY,
    fetched_at TEXT,                -- 最後に取得を試した日時 (UTC ISO)
    status     TEXT,                -- ok / nodata / error
    message    TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class Symbol:
    symbol: str
    code: str
    name: str
    market: str
    kind: str
    unit: int
    sector: str = ""


@dataclass(frozen=True)
class Bar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: int


def date_to_int(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def int_to_date(n: int) -> dt.date:
    return dt.date(n // 10000, n // 100 % 100, n % 100)


class PriceStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PriceStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- 銘柄 ---

    def sync_symbols(self, symbols: Sequence[Symbol], kind: str) -> tuple[int, int]:
        """kind の銘柄一覧を最新に合わせる。一覧から消えた銘柄はデータを残して非表示にする。

        戻り値は (新規追加数, 非表示にした数)。
        """
        cur = self.conn.cursor()
        before = {row[0] for row in cur.execute("SELECT symbol FROM symbols WHERE kind=?", (kind,))}
        cur.execute("UPDATE symbols SET active=0 WHERE kind=?", (kind,))
        cur.executemany(
            """
            INSERT INTO symbols(symbol, code, name, market, kind, unit, sector, active)
            VALUES(?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(symbol) DO UPDATE SET
                code=excluded.code, name=excluded.name, market=excluded.market,
                kind=excluded.kind, unit=excluded.unit, sector=excluded.sector, active=1
            """,
            [(s.symbol, s.code, s.name, s.market, s.kind, s.unit, s.sector) for s in symbols],
        )
        self.conn.commit()
        now = {s.symbol for s in symbols}
        return len(now - before), len(before - now)

    def symbols(self, active_only: bool = True) -> list[Symbol]:
        sql = "SELECT symbol, code, name, market, kind, unit, sector FROM symbols"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY kind DESC, code"
        return [Symbol(*row) for row in self.conn.execute(sql)]

    def _sid(self, symbol: str) -> int:
        row = self.conn.execute("SELECT id FROM symbols WHERE symbol=?", (symbol,)).fetchone()
        if row is None:
            raise KeyError(symbol)
        return row[0]

    # --- 日足 ---

    def last_date(self, symbol: str) -> Optional[dt.date]:
        row = self.conn.execute("SELECT MAX(d) FROM bars WHERE sid=?", (self._sid(symbol),)).fetchone()
        return int_to_date(row[0]) if row and row[0] else None

    def bars(self, symbol: str, start: Optional[dt.date] = None,
             end: Optional[dt.date] = None) -> list[Bar]:
        sql = "SELECT d, open, high, low, close, volume FROM bars WHERE sid=?"
        args: list = [self._sid(symbol)]
        if start:
            sql += " AND d>=?"
            args.append(date_to_int(start))
        if end:
            sql += " AND d<=?"
            args.append(date_to_int(end))
        sql += " ORDER BY d"
        return [Bar(int_to_date(r[0]), *r[1:]) for r in self.conn.execute(sql, args)]

    def closes_since(self, symbol: str, start: dt.date) -> dict[dt.date, float]:
        return {b.date: b.close for b in self.bars(symbol, start=start)}

    def write_bars(self, symbol: str, bars: Iterable[Bar], replace_all: bool = False) -> int:
        sid = self._sid(symbol)
        rows = [(sid, date_to_int(b.date), b.open, b.high, b.low, b.close, int(b.volume)) for b in bars]
        with self.conn:
            if replace_all:
                self.conn.execute("DELETE FROM bars WHERE sid=?", (sid,))
            self.conn.executemany("INSERT OR REPLACE INTO bars VALUES(?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def set_fetch_state(self, symbol: str, status: str, message: str = "") -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO fetch_state(sid, fetched_at, status, message) VALUES(?, ?, ?, ?)",
                (self._sid(symbol), now, status, message),
            )

    def summary(self) -> dict:
        c = self.conn
        return {
            "symbols": c.execute("SELECT COUNT(*) FROM symbols WHERE active=1").fetchone()[0],
            "with_data": c.execute("SELECT COUNT(DISTINCT sid) FROM bars").fetchone()[0],
            "bars": c.execute("SELECT COUNT(*) FROM bars").fetchone()[0],
            "errors": c.execute("SELECT COUNT(*) FROM fetch_state WHERE status='error'").fetchone()[0],
            "nodata": c.execute("SELECT COUNT(*) FROM fetch_state WHERE status='nodata'").fetchone()[0],
            "latest": c.execute("SELECT MAX(d) FROM bars").fetchone()[0],
        }
