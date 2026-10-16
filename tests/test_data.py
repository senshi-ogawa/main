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

    def __init__(self, history, rate_limited=0):
        self.history = history
        self.calls = []
        self.rate_limited = rate_limited  # 最初のこの回数は回数制限で断る

    def download(self, symbol, start):
        self.calls.append((symbol, start))
        if self.rate_limited:
            self.rate_limited -= 1
            raise fetch.RateLimited("Too Many Requests. Rate limited. Try after a while.")
        if symbol not in self.history:
            raise fetch.NoData("possibly delisted; no price data found")
        df = self.history[symbol]
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        return df


class FailingDownloader:
    def __init__(self):
        self.calls = 0

    def download(self, symbol, start):
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
        self.slept = []
        return fetch.update_prices(self.store, symbols, dl, pause=0, today=today,
                                   progress=lambda m: None, sleep=self.slept.append)


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

    def test_find_list_links_on_guide_page(self):
        html = """<a href="/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx">
            <img src="xlsx.png"></a><a href="/other.pdf">x</a>"""
        self.assertEqual(universe.find_list_links(html), [
            "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"])

    def test_load_xlsx_and_find_cached(self):
        df = pd.DataFrame({"日付": [20260930], "コード": [7203], "銘柄名": ["トヨタ自動車"],
                           "市場・商品区分": ["プライム（内国株式）"], "33業種区分": ["輸送用機器"]})
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(universe.cached_jpx_list(Path(d)))
            path = Path(d) / "data_j.xlsx"
            with pd.ExcelWriter(path) as w:  # 見出しの上に表題行がある形も読めること
                pd.DataFrame([["東証上場銘柄一覧"]]).to_excel(w, index=False, header=False)
                df.to_excel(w, index=False, startrow=2)
            self.assertEqual(universe.cached_jpx_list(Path(d)), path)
            got = universe.load_jpx_list(path)
        self.assertEqual([(s.symbol, s.market) for s in got], [("7203.T", "プライム")])

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
    def test_history_columns_and_timezone(self):
        idx = pd.DatetimeIndex(["2026-01-05 00:00", "2026-01-06 00:00"], tz="Asia/Tokyo")
        cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
        raw = pd.DataFrame(1.0, index=idx, columns=cols)
        raw["Close"] = [150.0, float("nan")]
        out = fetch.normalize_frame(raw)
        self.assertEqual(list(out.columns), ["open", "high", "low", "close", "volume", "splits"])
        self.assertIsNone(out.index.tz)
        self.assertEqual(len(out), 1)  # 終値が欠けた行は捨てる

    def test_empty_download(self):
        self.assertTrue(fetch.normalize_frame(pd.DataFrame()).empty)
        self.assertTrue(fetch.normalize_frame(None).empty)


class TestUpdate(StoreCase):
    def test_first_fetch_saves_history_but_not_today(self):
        ds = days(D(2026, 1, 5), 10)
        dl = FakeDownloader({"7203.T": frame([(d, 100 + i) for i, d in enumerate(ds)])})
        r = self.update(dl, ["7203.T"], today=ds[-1])
        self.assertEqual(r.new, 1)
        bars = self.store.bars("7203.T")
        self.assertEqual(len(bars), 9)
        self.assertEqual(bars[-1].date, ds[-2])
        self.assertEqual(dl.calls, [("7203.T", None)])

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
        syms = [x.symbol for x in universe.fx_symbols()] + ["7203.T", "9999.T"]
        dl = FailingDownloader()
        r = self.update(dl, syms, today=D(2026, 2, 1))
        self.assertTrue(r.aborted)
        self.assertEqual(r.errors, fetch.MAX_ERRORS_IN_A_ROW)
        self.assertEqual(dl.calls, fetch.MAX_ERRORS_IN_A_ROW * fetch.RETRIES)
        self.assertEqual(self.store.summary()["bars"], 0)

    def test_rate_limit_waits_then_continues(self):
        ds = days(D(2026, 1, 5), 5)
        dl = FakeDownloader({"7203.T": frame([(d, 100) for d in ds]),
                             "9999.T": frame([(d, 200) for d in ds])}, rate_limited=2)
        r = self.update(dl, ["7203.T", "9999.T"], today=ds[-1] + dt.timedelta(days=1))
        self.assertEqual((r.new, r.errors, r.nodata, r.aborted), (2, 0, 0, False))
        self.assertEqual(self.slept, list(fetch.RATE_LIMIT_WAITS[:2]))

    def test_endless_rate_limit_stops_and_keeps_what_was_saved(self):
        ds = days(D(2026, 1, 5), 5)
        dl = FakeDownloader({"7203.T": frame([(d, 100) for d in ds])})
        orig = dl.download

        def limited_after_first(symbol, start):
            if dl.calls:
                dl.calls.append((symbol, start))
                raise fetch.RateLimited("Too Many Requests")
            return orig(symbol, start)

        dl.download = limited_after_first
        r = self.update(dl, ["7203.T", "9999.T"], today=ds[-1] + dt.timedelta(days=1))
        self.assertTrue(r.aborted)
        self.assertEqual(r.new, 1)
        self.assertEqual(len(self.store.bars("7203.T")), 5)
        self.assertIn("回数制限", r.messages[-1])

if __name__ == "__main__":
    unittest.main()
