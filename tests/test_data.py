"""データ取得まわりのテスト。ネットワークには接続しない。

    python -m unittest discover -s tests
"""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from kabutrainer.data import fetch, universe
from kabutrainer.data.store import Bar, PriceStore, Symbol

D = dt.date


def frame(rows, splits=None):
    """rows: [(date, close)] から日足の DataFrame を作る。"""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows])
    closes = [c for _, c in rows]
    df = pd.DataFrame({
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000] * len(rows), "splits": [0.0] * len(rows),
    }, index=idx)
    for d, ratio in (splits or {}).items():
        df.loc[pd.Timestamp(d), "splits"] = ratio
    return df


class FakeDownloader:
    """銘柄ごとの「Yahoo側の全履歴」を持ち、start 以降を返す。"""

    def __init__(self, history):
        self.history = history
        self.calls = []

    def download(self, symbols, start):
        self.calls.append((tuple(symbols), start))
        out = {}
        for s in symbols:
            df = self.history.get(s, pd.DataFrame())
            if start is not None and not df.empty:
                df = df[df.index >= pd.Timestamp(start)]
            out[s] = df
        return out


class FailingDownloader:
    def __init__(self):
        self.calls = 0

    def download(self, symbols, start):
        self.calls += 1
        raise fetch.DownloadError("CONNECT tunnel failed, response 403")


def days(start, n):
    """土日を除いた n 営業日。"""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PriceStore(Path(self.tmp.name) / "p.sqlite3")
        self.store.sync_symbols([
            Symbol("7203.T", "7203", "トヨタ自動車", "プライム", "stock", 100),
            Symbol("9999.T", "9999", "テスト", "スタンダード", "stock", 100),
        ], "stock")
        self.store.sync_symbols(universe.fx_symbols(), "fx")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def update(self, dl, symbols, today):
        return fetch.update_prices(self.store, symbols, dl, pause=0, today=today, progress=lambda m: None)


class TestUniverse(unittest.TestCase):
    def test_parse_keeps_prime_and_standard_only(self):
        df = pd.DataFrame({
            "日付": [20261010] * 6,
            "コード": [7203.0, "130A", 1301, 2914, 1305, 9999],
            "銘柄名": ["トヨタ自動車", "英字コード社", "極洋", "ＪＴ", "ETF", "グロース社"],
            "市場・商品区分": ["プライム（内国株式）", "スタンダード（内国株式）", "プライム（内国株式）",
                          "プライム（内国株式）", "ETF・ETN", "グロース（内国株式）"],
            "33業種区分": ["輸送用機器", "情報・通信業", "水産・農林業", "食料品", "-", "サービス業"],
        })
        got = {s.symbol: s for s in universe.parse_jpx_list(df)}
        self.assertEqual(sorted(got), ["1301.T", "130A.T", "2914.T", "7203.T"])
        self.assertEqual(got["7203.T"].code, "7203")
        self.assertEqual(got["7203.T"].market, "プライム")
        self.assertEqual(got["130A.T"].market, "スタンダード")
        self.assertEqual(got["7203.T"].unit, 100)

    def test_parse_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            universe.parse_jpx_list(pd.DataFrame({"code": [1]}))

    def test_fx_unit_is_1000(self):
        fx = universe.fx_symbols()
        self.assertIn("USDJPY=X", [s.symbol for s in fx])
        self.assertTrue(all(s.unit == 1000 and s.market == "為替" for s in fx))


class TestStore(StoreCase):
    def test_delisted_symbol_is_hidden_but_data_kept(self):
        self.store.write_bars("9999.T", [Bar(D(2026, 1, 5), 1, 2, 0.5, 1.5, 10)])
        added, removed = self.store.sync_symbols(
            [Symbol("7203.T", "7203", "トヨタ自動車", "プライム", "stock", 100)], "stock")
        self.assertEqual((added, removed), (0, 1))
        self.assertNotIn("9999.T", [s.symbol for s in self.store.symbols()])
        self.assertEqual(len(self.store.bars("9999.T")), 1)

    def test_fx_sync_does_not_hide_stocks(self):
        self.store.sync_symbols(universe.fx_symbols(), "fx")
        self.assertIn("7203.T", [s.symbol for s in self.store.symbols()])


class TestNormalize(unittest.TestCase):
    def test_multiindex_and_timezone(self):
        idx = pd.DatetimeIndex(["2026-01-05 00:00", "2026-01-06 00:00"], tz="Asia/Tokyo")
        cols = pd.MultiIndex.from_product(
            [["7203.T", "USDJPY=X"], ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]])
        raw = pd.DataFrame(1.0, index=idx, columns=cols)
        raw[("USDJPY=X", "Close")] = [150.0, float("nan")]
        out = fetch.normalize_yf(raw, ["7203.T", "USDJPY=X", "0000.T"])
        self.assertEqual(list(out["7203.T"].columns), ["open", "high", "low", "close", "volume", "splits"])
        self.assertIsNone(out["7203.T"].index.tz)
        self.assertEqual(len(out["USDJPY=X"]), 1)  # 終値が欠けた行は捨てる
        self.assertTrue(out["0000.T"].empty)

    def test_empty_download(self):
        out = fetch.normalize_yf(pd.DataFrame(), ["7203.T"])
        self.assertTrue(out["7203.T"].empty)


class TestUpdate(StoreCase):
    def test_first_fetch_saves_history_but_not_today(self):
        ds = days(D(2026, 1, 5), 10)
        dl = FakeDownloader({"7203.T": frame([(d, 100 + i) for i, d in enumerate(ds)])})
        r = self.update(dl, ["7203.T"], today=ds[-1])
        self.assertEqual(r.new, 1)
        bars = self.store.bars("7203.T")
        self.assertEqual(len(bars), 9)
        self.assertEqual(bars[-1].date, ds[-2])
        self.assertEqual(dl.calls, [(("7203.T",), None)])

    def test_incremental_adds_only_new_days(self):
        ds = days(D(2026, 1, 5), 30)
        hist = frame([(d, 100 + i) for i, d in enumerate(ds)])
        dl = FakeDownloader({"7203.T": hist.iloc[:20]})
        self.update(dl, ["7203.T"], today=ds[20])
        dl.history["7203.T"] = hist
        r = self.update(dl, ["7203.T"], today=ds[29])
        self.assertEqual((r.updated, r.refetched), (1, 0))
        self.assertEqual(len(self.store.bars("7203.T")), 29)
        start = dl.calls[-1][1]
        self.assertEqual(start, ds[19] - dt.timedelta(days=fetch.OVERLAP_DAYS))

    def test_provisional_last_bar_is_overwritten_without_refetch(self):
        ds = days(D(2026, 1, 5), 12)
        hist = frame([(d, 100.0) for d in ds])
        dl = FakeDownloader({"7203.T": hist.iloc[:10]})
        self.update(dl, ["7203.T"], today=ds[10])
        self.store.write_bars("7203.T", [Bar(ds[9], 1, 1, 1, 90.0, 1)])  # 前回は値が未確定だった
        dl.history["7203.T"] = hist
        r = self.update(dl, ["7203.T"], today=ds[11])
        self.assertEqual(r.refetched, 0)
        self.assertEqual(self.store.bars("7203.T")[9].close, 100.0)

    def test_split_adjustment_triggers_full_refetch(self):
        ds = days(D(2026, 1, 5), 30)
        dl = FakeDownloader({"7203.T": frame([(d, 1000.0) for d in ds[:20]])})
        self.update(dl, ["7203.T"], today=ds[20])
        # 1:2 の分割。Yahoo側の過去の株価がすべて半分に調整し直される
        dl.history["7203.T"] = frame([(d, 500.0) for d in ds], splits={ds[22]: 2.0})
        r = self.update(dl, ["7203.T"], today=ds[29])
        self.assertEqual(r.refetched, 1)
        bars = self.store.bars("7203.T")
        self.assertEqual(len(bars), 29)
        self.assertTrue(all(b.close == 500.0 for b in bars))
        self.assertIsNone(dl.calls[-1][1])

    def test_symbol_without_data_is_marked_nodata(self):
        ds = days(D(2026, 1, 5), 5)
        dl = FakeDownloader({"7203.T": frame([(d, 100) for d in ds])})
        r = self.update(dl, ["7203.T", "9999.T"], today=ds[-1] + dt.timedelta(days=1))
        self.assertEqual((r.new, r.nodata, r.errors), (1, 1, 0))

    def test_network_failure_aborts_without_writing(self):
        syms = [s.symbol for s in universe.fx_symbols()]
        orig = fetch.time.sleep
        fetch.time.sleep = lambda s: None
        try:
            dl = FailingDownloader()
            r = fetch.update_prices(self.store, syms, dl, batch_size=2, pause=0,
                                    today=D(2026, 2, 1), progress=lambda m: None)
        finally:
            fetch.time.sleep = orig
        self.assertTrue(r.aborted)
        self.assertEqual(dl.calls, fetch.MAX_FAILED_BATCHES_IN_A_ROW * fetch.RETRIES)
        self.assertEqual(r.errors, 6)
        self.assertEqual(self.store.summary()["bars"], 0)

    def test_all_empty_batch_counts_as_failure(self):
        orig = fetch.time.sleep
        fetch.time.sleep = lambda s: None
        try:
            r = self.update(FakeDownloader({}), ["7203.T", "9999.T"], today=D(2026, 2, 1))
        finally:
            fetch.time.sleep = orig
        self.assertEqual((r.errors, r.nodata), (2, 0))


if __name__ == "__main__":
    unittest.main()
