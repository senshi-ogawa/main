"""yfinance から日足を取得して保存する。

- 初回は銘柄ごとに取れるだけ過去までさかのぼって取得する。
- 2回目以降は、保存済みの最終日の少し前から取り直して差分だけ追加する。
- 取り直した重なり部分の終値が保存済みと食い違ったら（株式分割などで
  過去の株価が調整し直された）、その銘柄だけ全期間を取り直す。
- 当日分は値が確定していない可能性があるので保存しない。

Yahoo は短時間に大量に問い合わせると一時的に受け付けなくなる（回数制限）。
そのため1銘柄ずつ間を空けて取得し、制限がかかったら待ってから続ける。
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Sequence

import pandas as pd

from .store import Bar, PriceStore

JST = dt.timezone(dt.timedelta(hours=9))

OVERLAP_DAYS = 14
TOLERANCE = 0.005  # 終値の食い違いをこれ以上で「調整し直された」とみなす (0.5%)
PAUSE = 0.5        # 銘柄ごとの間隔（秒）
RETRIES = 3        # 通信エラー時に同じ銘柄を試す回数
RATE_LIMIT_WAITS = (60, 120, 300, 600, 900)  # 回数制限にかかったときに待つ秒数
MAX_ERRORS_IN_A_ROW = 10
PROGRESS_EVERY = 20


class DownloadError(Exception):
    """通信の失敗など。少し待てば取れる可能性がある。"""


class NoData(Exception):
    """その銘柄のデータが Yahoo にない（上場直後、コード違いなど）。"""


class RateLimited(Exception):
    """Yahoo の回数制限にかかった。しばらく待つ必要がある。"""


class Downloader(Protocol):
    def download(self, symbol: str, start: Optional[dt.date]) -> pd.DataFrame:
        """1銘柄の日足を返す。列は open/high/low/close/volume/splits、index は日付。

        start が None なら取れるだけ過去から。
        データがなければ NoData、回数制限なら RateLimited、それ以外の失敗は DownloadError。
        """
        ...


def normalize_frame(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """yfinance の DataFrame を open/high/low/close/volume/splits の形にそろえる。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    if "stock_splits" in df.columns:
        df = df.rename(columns={"stock_splits": "splits"})
    if "splits" not in df.columns:
        df["splits"] = 0.0
    if "volume" not in df.columns:
        df["volume"] = 0
    need = ["open", "high", "low", "close"]
    if any(c not in df.columns for c in need):
        return pd.DataFrame()
    df = df.dropna(subset=need)
    df["volume"] = df["volume"].fillna(0)
    df["splits"] = df["splits"].fillna(0)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["open", "high", "low", "close", "volume", "splits"]]


class YahooDownloader:
    """yfinance を使う本番用の取得係。"""

    def __init__(self) -> None:
        import yfinance as yf

        self.yf = yf
        # 失敗を英語の警告で流さず、例外として受け取って種類ごとに扱う
        try:
            yf.config.debug.hide_exceptions = False
        except AttributeError:  # 古い yfinance
            pass
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    def download(self, symbol: str, start: Optional[dt.date]) -> pd.DataFrame:
        from yfinance import exceptions as yfe

        kwargs = dict(interval="1d", auto_adjust=False, actions=True, timeout=30)
        if start is None:
            kwargs["period"] = "max"
        else:
            kwargs["start"] = start.isoformat()
        try:
            raw = self.yf.Ticker(symbol).history(**kwargs)
        except yfe.YFRateLimitError as e:
            raise RateLimited(str(e)) from e
        except (yfe.YFPricesMissingError, yfe.YFTzMissingError, yfe.YFTickerMissingError) as e:
            raise NoData(str(e)) from e
        except Exception as e:
            if "Too Many Requests" in str(e) or "429" in str(e):
                raise RateLimited(str(e)) from e
            raise DownloadError(str(e)) from e
        df = normalize_frame(raw)
        if df.empty and start is None:
            raise NoData("データがありません")
        return df


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


class _Abort(Exception):
    pass


def update_prices(
    store: PriceStore,
    symbols: Sequence[str],
    downloader: Downloader,
    *,
    pause: float = PAUSE,
    today: Optional[dt.date] = None,
    progress: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> UpdateResult:
    today = today or dt.datetime.now(JST).date()
    result = UpdateResult()
    last: Dict[str, Optional[dt.date]] = {s: store.last_date(s) for s in symbols}
    fresh_syms = [s for s in symbols if last[s] is None]
    known_syms = [s for s in symbols if last[s] is not None]
    refetch: List[str] = []
    errors_in_a_row = 0
    total = len(symbols)
    done = 0

    def fetch(sym: str, start: Optional[dt.date]) -> Optional[pd.DataFrame]:
        """取れたら DataFrame、データがなければ空の DataFrame、エラーなら None（記録済み）。

        回数制限や通信エラーが続いたら _Abort で全体を止める。
        """
        nonlocal errors_in_a_row
        waits = list(RATE_LIMIT_WAITS)
        err = ""
        attempt = 0
        while True:
            try:
                df = downloader.download(sym, start)
                errors_in_a_row = 0
                return df
            except NoData:
                errors_in_a_row = 0
                return pd.DataFrame()
            except RateLimited:
                if not waits:
                    result.aborted = True
                    result.messages.append(
                        "Yahooの回数制限が続いたので中断しました。しばらく（1時間ほど）おいてから、"
                        "もう一度起動してください。続きから取得します。")
                    raise _Abort()
                w = waits.pop(0)
                progress(f"Yahooから少し待つよう求められました。{w}秒待ってから続けます…")
                sleep(w)
            except DownloadError as e:
                err = str(e)
                attempt += 1
                if attempt >= RETRIES:
                    break
                sleep(pause * (2 ** attempt) + 1)
        errors_in_a_row += 1
        store.set_fetch_state(sym, "error", err)
        result.errors += 1
        result.messages.append(f"取得失敗 ({sym}): {err}")
        if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
            result.aborted = True
            result.messages.append("連続して取得に失敗したので中断しました。ネットワーク接続を確認してください。")
            raise _Abort()
        return None

    def tick(label: str) -> None:
        nonlocal done
        done += 1
        if label and (done % PROGRESS_EVERY == 0 or done == total):
            progress(f"{label} {done}/{total}")

    def save_full(sym: str, refetched: bool = False) -> None:
        df = fetch(sym, None)
        if df is None:
            return
        bars = frame_to_bars(df, before=today)
        if not bars:
            store.set_fetch_state(sym, "nodata")
            result.nodata += 1
            return
        result.bars_written += store.write_bars(sym, bars, replace_all=True)
        store.set_fetch_state(sym, "ok")
        if refetched:
            result.refetched += 1
        else:
            result.new += 1

    def save_incremental(sym: str) -> None:
        stored_last = last[sym]
        assert stored_last is not None
        df = fetch(sym, stored_last - dt.timedelta(days=OVERLAP_DAYS))
        if df is None:
            return
        if df.empty:
            store.set_fetch_state(sym, "nodata", "新しいデータなし")
            return
        stored = store.closes_since(sym, df.index.min().date())
        if needs_full_refetch(stored, df, stored_last):
            refetch.append(sym)
            return
        bars = [b for b in frame_to_bars(df, before=today) if b.date >= stored_last]
        if bars:
            result.bars_written += store.write_bars(sym, bars)
            result.updated += 1
        store.set_fetch_state(sym, "ok")

    try:
        # 既存の銘柄を先に更新し、続けて新しい銘柄を全期間取得する
        for i, sym in enumerate(known_syms):
            if i and pause:
                sleep(pause)
            save_incremental(sym)
            tick("差分更新")
        for i, sym in enumerate(fresh_syms):
            if (i or known_syms) and pause:
                sleep(pause)
            save_full(sym)
            tick("新規取得")
        if refetch:
            progress(f"株式分割などで過去の株価が変わった {len(refetch)} 銘柄を取り直します")
            for sym in refetch:
                if pause:
                    sleep(pause)
                save_full(sym, refetched=True)
    except _Abort:
        pass
    return result
