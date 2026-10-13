"""対象銘柄の一覧。

株: JPX が公開している「東証上場銘柄一覧」(data_j.xls) から
プライムとスタンダードの銘柄を取り出す。
売買単位は、東証が2018年10月に全銘柄を100株に統一したので100株とする。

為替: 主な通貨ペアを固定で持つ。売買単位は1,000通貨。
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import pandas as pd

from .store import Symbol

JPX_LIST_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)

STOCK_UNIT = 100
FX_UNIT = 1000

# 市場・商品区分の先頭がこれで始まる銘柄だけを対象にする
TARGET_MARKETS = ("プライム", "スタンダード")

FX_PAIRS = [
    ("USDJPY=X", "USD/JPY", "米ドル/円"),
    ("EURJPY=X", "EUR/JPY", "ユーロ/円"),
    ("GBPJPY=X", "GBP/JPY", "英ポンド/円"),
    ("AUDJPY=X", "AUD/JPY", "豪ドル/円"),
    ("NZDJPY=X", "NZD/JPY", "NZドル/円"),
    ("CADJPY=X", "CAD/JPY", "カナダドル/円"),
    ("CHFJPY=X", "CHF/JPY", "スイスフラン/円"),
    ("EURUSD=X", "EUR/USD", "ユーロ/米ドル"),
    ("GBPUSD=X", "GBP/USD", "英ポンド/米ドル"),
    ("AUDUSD=X", "AUD/USD", "豪ドル/米ドル"),
]


def fx_symbols() -> list[Symbol]:
    return [Symbol(sym, code, name, "為替", "fx", FX_UNIT) for sym, code, name in FX_PAIRS]


def download_jpx_list(dest: Path, timeout: float = 60) -> Path:
    """JPX の銘柄一覧を dest に保存する。"""
    req = urllib.request.Request(JPX_LIST_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


def _clean_code(value) -> str:
    # 数字だけのコードは Excel 上で数値 (7203.0) として読まれることがある。
    # 2024年以降は 130A のような英字入りコードもある。
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def parse_jpx_list(df: pd.DataFrame) -> list[Symbol]:
    """data_j.xls の表から対象銘柄を取り出す。"""
    required = ["コード", "銘柄名", "市場・商品区分"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"JPXの銘柄一覧の形式が想定と違います（列がありません: {', '.join(missing)}）")

    symbols: list[Symbol] = []
    for rec in df.to_dict("records"):
        segment = str(rec.get("市場・商品区分") or "")
        market = next((m for m in TARGET_MARKETS if segment.startswith(m)), None)
        if market is None:
            continue
        code = _clean_code(rec.get("コード"))
        if not code:
            continue
        sector = rec.get("33業種区分")
        sector = "" if sector is None or (isinstance(sector, float) and pd.isna(sector)) or sector == "-" else str(sector)
        symbols.append(Symbol(f"{code}.T", code, str(rec.get("銘柄名")).strip(), market, "stock", STOCK_UNIT, sector))
    return symbols


def load_jpx_list(path: Path) -> list[Symbol]:
    df = pd.read_excel(path, dtype=object)
    return parse_jpx_list(df)
