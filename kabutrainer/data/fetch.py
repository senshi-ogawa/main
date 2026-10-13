"""yfinance から日足を取得して保存する。

- 初回は銘柄ごとに取れるだけ過去までさかのぼって取得する。
- 2回目以降は、保存済みの最終日の少し前から取り直して差分だけ追加する。
- 取り直した重なり部分の終値が保存済みと食い違ったら（株式分割などで
  過去の株価が調整し直された）、その銘柄だけ全期間を取り直す。
- 当日分は値が確定していない可能性があるので保存しない。
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence

import pandas as pd

from .store import Bar, PriceStore

JST = dt.timezone(dt.timedelta(hours=9))

BATCH_SIZE = 40
OVERLAP_DAYS = 14
TOLERANCE = 0.005  # 終値の食い違いをこれ以上で「調整し直された」とみなす (0.5%)
RETRIES = 3
MAX_FAILED_BATCHES_IN_A_ROW = 3


class DownloadError(Exception):
    pass


class Downloader(Protocol):
    def download(self, symbols: Sequence[str], start: Optional[dt.date]) -> Dict[str, pd.DataFrame]:
        """銘柄ごとの日足を返す。列は open/high/low/close/volume/splits、index は日付。

        start が None なら取れるだけ過去から。取れなかった銘柄は空の DataFrame か欠落。
        """
        ...


def normalize_yf(raw: Optional[pd.DataFrame], symbols: Sequence[str]) -> Dict[str, pd.DataFrame]:
    """yf.download の結果を銘柄ごとの DataFrame に分ける。"""
    out: Dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return {s: pd.DataFrame() for s in symbols}
    multi = isinstance(raw.columns, pd.MultiIndex)
    for sym in symbols:
        if multi:
            if sym not in raw.columns.get_level_values(0):
                out[sym] = pd.DataFrame()
                continue
            df = raw[sym].copy()
        elif len(symbols) == 1:
            df = raw.copy()
        else:
            out[sym] = pd.DataFrame()
            continue
        df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
        if "stock_splits" in df.columns:
            df = df.rename(columns={"stock_splits": "splits"})
        if "splits" not in df.columns:
            df["splits"] = 0.0
        if "volume" not in df.columns:
            df["volume"] = 0
        need = ["open", "high", "low", "close"]
        if any(c not in df.columns for c in need):
            out[sym] = pd.DataFrame()
            continue
        df = df.dropna(subset=need)
        df["volume"] = df["volume"].fillna(0)
        df["splits"] = df["splits"].fillna(0)
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        df.index = idx.normalize()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        out[sym] = df[["open", "high", "low", "close", "volume", "splits"]]
    return out


class YahooDownloader:
    """yfinance を使う本番用の取得係。"""

    def download(self, symbols: Sequence[str], start: Optional[dt.date]) -> Dict[str, pd.DataFrame]:
        import yfinance as yf

        # 取れなかった銘柄ごとの英語の警告は結果の集計で伝えるので、ここでは表示しない
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        kwargs = dict(
            interval="1d", auto_adjust=False, actions=True, group_by="ticker",
            threads=True, progress=False, multi_level_index=True, timeout=30,
        )
        if start is None:
            kwargs["period"] = "max"
        else:
            kwargs["start"] = start.isoformat()
        try:
            raw = yf.download(list(symbols), **kwargs)
        except Exception as e:  # yfinance が例外を投げるのは通信まわりの失敗
            raise DownloadError(str(e)) from e
        return normalize_yf(raw, symbols)


def frame_to_bars(df: pd.DataFrame, before: dt.date) -> List[Bar]:
    bars = []
    for ts, row in df.iterrows():
        d = ts.date()
        if d >= before:
            continue
        vol = row["volume"]
        bars.append(Bar(d, float(row["open"]), float(row["high"]), float(row["low"]),
                        float(row["close"]), int(vol) if not math.isnan(vol) else 0))
    return bars


def needs_full_refetch(stored: Dict[dt.date, float], fresh: pd.DataFrame,
                       stored_last: dt.date, tolerance: float = TOLERANCE) -> bool:
    """重なり部分の終値が食い違うか、新しく分割があれば True。

    保存済みの最終日は前回の時点で未確定だった可能性があるので比べない。
    """
    if fresh.empty:
        return False
    if (fresh.loc[fresh.index.date > stored_last, "splits"] > 0).any():
        return True
    for ts, row in fresh.iterrows():
        d = ts.date()
        if d >= stored_last or d not in stored:
            continue
        old = stored[d]
        if abs(row["close"] - old) > tolerance * max(abs(old), 1e-12):
            return True
    return False


@dataclass
class UpdateResult:
    new: int = 0          # 初めて取得できた銘柄
    updated: int = 0      # 差分を追加した銘柄
    refetched: int = 0    # 調整し直しで全期間を取り直した銘柄
    nodata: int = 0       # データがなかった銘柄
    errors: int = 0       # 通信エラーなどで取れなかった銘柄
    bars_written: int = 0
    aborted: bool = False
    messages: List[str] = field(default_factory=list)


def _batches(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def update_prices(
    store: PriceStore,
    symbols: Sequence[str],
    downloader: Downloader,
    *,
    batch_size: int = BATCH_SIZE,
    pause: float = 1.0,
    today: Optional[dt.date] = None,
    progress: Callable[[str], None] = print,
) -> UpdateResult:
    today = today or dt.datetime.now(JST).date()
    result = UpdateResult()
    last: Dict[str, Optional[dt.date]] = {s: store.last_date(s) for s in symbols}
    fresh_syms = [s for s in symbols if last[s] is None]
    known_syms = [s for s in symbols if last[s] is not None]
    refetch: List[str] = []
    failed_in_a_row = 0
    total = len(symbols)
    done = 0

    def fetch(batch: List[str], start: Optional[dt.date]) -> Optional[Dict[str, pd.DataFrame]]:
        nonlocal failed_in_a_row
        err = ""
        for attempt in range(RETRIES):
            try:
                frames = downloader.download(batch, start)
                # 複数銘柄なのに全部空なら、銘柄ではなく通信の問題とみなす
                if len(batch) > 1 and all(frames.get(s) is None or frames[s].empty for s in batch):
                    raise DownloadError("どの銘柄のデータも返ってきませんでした")
                failed_in_a_row = 0
                return frames
            except DownloadError as e:
                err = str(e)
                if attempt < RETRIES - 1:
                    time.sleep(pause * (2 ** (attempt + 1)))
        failed_in_a_row += 1
        for s in batch:
            store.set_fetch_state(s, "error", err)
        result.errors += len(batch)
        result.messages.append(f"取得失敗 ({batch[0]} ほか{len(batch) - 1}銘柄): {err}")
        return None

    def run(batches: Iterable[List[str]], start_of: Callable[[List[str]], Optional[dt.date]],
            handle: Callable[[str, pd.DataFrame], None], label: str) -> bool:
        nonlocal done
        for batch in batches:
            frames = fetch(batch, start_of(batch))
            if frames is not None:
                for s in batch:
                    handle(s, frames.get(s, pd.DataFrame()))
            if label:
                done += len(batch)
                progress(f"{label} {done}/{total}")
            if failed_in_a_row >= MAX_FAILED_BATCHES_IN_A_ROW:
                result.aborted = True
                result.messages.append("連続して取得に失敗したので中断しました。ネットワーク接続を確認してください。")
                return False
            if pause:
                time.sleep(pause)
        return True

    def handle_full(s: str, df: pd.DataFrame, refetched: bool = False) -> None:
        bars = frame_to_bars(df, before=today) if not df.empty else []
        if not bars:
            store.set_fetch_state(s, "nodata")
            result.nodata += 1
            return
        result.bars_written += store.write_bars(s, bars, replace_all=True)
        store.set_fetch_state(s, "ok")
        if refetched:
            result.refetched += 1
        else:
            result.new += 1

    def handle_incremental(s: str, df: pd.DataFrame) -> None:
        stored_last = last[s]
        assert stored_last is not None
        if df.empty:
            store.set_fetch_state(s, "nodata", "新しいデータなし")
            return
        stored = store.closes_since(s, df.index.min().date())
        if needs_full_refetch(stored, df, stored_last):
            refetch.append(s)
            return
        bars = [b for b in frame_to_bars(df, before=today) if b.date >= stored_last]
        if bars:
            result.bars_written += store.write_bars(s, bars)
            result.updated += 1
        store.set_fetch_state(s, "ok")

    def incremental_start(batch: List[str]) -> dt.date:
        return min(last[s] for s in batch) - dt.timedelta(days=OVERLAP_DAYS)  # type: ignore[type-var]

    # 既存の銘柄を先に更新し、続けて新しい銘柄を全期間取得する
    if not run(_batches(known_syms, batch_size), incremental_start, handle_incremental, "差分更新"):
        return result
    if not run(_batches(fresh_syms, batch_size), lambda b: None, handle_full, "新規取得"):
        return result
    if refetch:
        progress(f"株式分割などで過去の株価が変わった {len(refetch)} 銘柄を取り直します")
        run(_batches(refetch, batch_size), lambda b: None,
            lambda s, df: handle_full(s, df, refetched=True), "")
    return result
