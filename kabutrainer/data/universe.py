"""対象銘柄の一覧。

株: JPX が公開している「東証上場銘柄一覧」(data_j.xlsx) から
プライムとスタンダードの銘柄を取り出す。
売買単位は、東証が2018年10月に全銘柄を100株に統一したので100株とする。

為替: 主な通貨ペアを固定で持つ。売買単位は1,000通貨。
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import pandas as pd

from .store import Symbol

# 一覧ファイルの置き場所や形式 (.xls → .xlsx) は JPX 側で変わることがあるので、
# 案内ページからリンクを探し、見つからなければ既知の場所を順に試す。
JPX_LIST_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_LIST_FALLBACK_URLS = [
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
]
JPX_LIST_NAMES = ("data_j.xlsx", "data_j.xls")

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


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def find_list_links(html: str, base: str = JPX_LIST_PAGE) -> list[str]:
    """案内ページの HTML から一覧ファイル (data_j.xlsx / data_j.xls) へのリンクを探す。"""
    hrefs = re.findall(r"""href=["']([^"']*data_j\.xlsx?)["']""", html, flags=re.IGNORECASE)
    return [urllib.parse.urljoin(base, h) for h in dict.fromkeys(hrefs)]


def download_jpx_list(dest_dir: Path, timeout: float = 60) -> Path:
    """JPX の銘柄一覧を dest_dir に保存し、保存したファイルを返す。"""
    candidates: list[str] = []
    try:
        candidates += find_list_links(_get(JPX_LIST_PAGE, timeout).decode("utf-8", "replace"))
    except Exception:
        pass  # 案内ページが読めなくても既知の場所を試す
    candidates += [u for u in JPX_LIST_FALLBACK_URLS if u not in candidates]

    last_error: Optional[Exception] = None
    for url in candidates:
        try:
            data = _get(url, timeout)
        except Exception as e:
            last_error = e
            continue
        name = "data_j.xlsx" if url.lower().endswith(".xlsx") else "data_j.xls"
        dest = dest_dir / name
        tmp = dest.with_name(name + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        for other in JPX_LIST_NAMES:  # 古い形式のファイルが残っていると紛らわしいので消す
            if other != name and (dest_dir / other).exists():
                (dest_dir / other).unlink()
        return dest
    raise last_error or RuntimeError("一覧ファイルが見つかりませんでした")


def cached_jpx_list(dest_dir: Path) -> Optional[Path]:
    """前回保存した一覧ファイルがあれば返す。"""
    for name in JPX_LIST_NAMES:
        if (dest_dir / name).exists():
            return dest_dir / name
    return None


def _clean_code(value) -> str:
    # 数字だけのコードは Excel 上で数値 (7203.0) として読まれることがある。
    # 2024年以降は 130A のような英字入りコードもある。
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def parse_jpx_list(df: pd.DataFrame) -> list[Symbol]:
    """data_j.xlsx の表から対象銘柄を取り出す。"""
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
    df = pd.read_excel(path, dtype=object, header=None)
    # 見出しが1行目とは限らないので、「コード」を含む行を見出しとして扱う
    for i in range(min(len(df), 10)):
        row = [str(v).strip() for v in df.iloc[i].tolist()]
        if "コード" in row:
            body = df.iloc[i + 1:].reset_index(drop=True)
            body.columns = row
            return parse_jpx_list(body)
    return parse_jpx_list(df)
