"""コマンドラインの入口。

    python -m kabutrainer update            銘柄一覧と株価を最新にする
    python -m kabutrainer update --limit 20 最初の20銘柄だけ試す
    python -m kabutrainer update --symbols 7203.T,USDJPY=X
    python -m kabutrainer status            保存済みデータの状況を表示する
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import paths
from .data import universe
from .data.fetch import YahooDownloader, update_prices
from .data.store import PriceStore, int_to_date


def _print(msg: str) -> None:
    print(msg, flush=True)


def cmd_update(args: argparse.Namespace) -> int:
    db = Path(args.db) if args.db else paths.prices_db_path()
    _print(f"保存先: {db}")
    with PriceStore(db) as store:
        if not args.skip_list:
            xls = paths.data_dir() / "data_j.xls"
            try:
                _print("JPXの銘柄一覧を取得しています…")
                universe.download_jpx_list(xls)
            except Exception as e:  # 取れなくても前回の一覧で続ける
                _print(f"銘柄一覧を取得できませんでした: {e}")
                if not xls.exists():
                    _print("前回の一覧もないため、為替だけで続けます。")
            if xls.exists():
                stocks = universe.load_jpx_list(xls)
                added, removed = store.sync_symbols(stocks, "stock")
                _print(f"株: {len(stocks)}銘柄（新規 {added}、一覧から外れた {removed}）")
            store.sync_symbols(universe.fx_symbols(), "fx")

        targets = [s.symbol for s in store.symbols()]
        if args.symbols:
            wanted = [s.strip() for s in args.symbols.split(",") if s.strip()]
            unknown = [s for s in wanted if s not in targets]
            if unknown:
                _print(f"一覧にない銘柄です: {', '.join(unknown)}")
                return 2
            targets = wanted
        if args.limit:
            targets = targets[: args.limit]

        _print(f"{len(targets)}銘柄の株価を更新します。初回は時間がかかります（全銘柄で1時間ほど）。")
        r = update_prices(store, targets, YahooDownloader(), progress=_print)
        _print("")
        _print(f"新規 {r.new}、差分更新 {r.updated}、取り直し {r.refetched}、"
               f"データなし {r.nodata}、エラー {r.errors}（{r.bars_written:,}本の日足を書き込み）")
        for m in r.messages[-10:]:
            _print(m)
        return 1 if r.aborted else 0


def cmd_status(args: argparse.Namespace) -> int:
    db = Path(args.db) if args.db else paths.prices_db_path()
    if not db.exists():
        _print(f"まだデータがありません（{db}）")
        return 0
    with PriceStore(db) as store:
        s = store.summary()
    latest = int_to_date(s["latest"]).isoformat() if s["latest"] else "-"
    size_mb = db.stat().st_size / 1024 / 1024
    _print(f"保存先: {db}（{size_mb:,.0f}MB）")
    _print(f"銘柄 {s['symbols']}（データあり {s['with_data']}）、日足 {s['bars']:,}本、最新 {latest}")
    _print(f"データなし {s['nodata']}、エラー {s['errors']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kabutrainer", description="株取引練習アプリ")
    p.add_argument("--db", help="株価データベースの場所（省略時はユーザーのデータフォルダ）")
    sub = p.add_subparsers(dest="command")

    u = sub.add_parser("update", help="銘柄一覧と株価を最新にする")
    u.add_argument("--limit", type=int, help="先頭からこの数の銘柄だけ更新する（お試し用）")
    u.add_argument("--symbols", help="更新する銘柄をカンマ区切りで指定（例: 7203.T,USDJPY=X）")
    u.add_argument("--skip-list", action="store_true", help="銘柄一覧の取得を省く")
    u.set_defaults(func=cmd_update)

    st = sub.add_parser("status", help="保存済みデータの状況を表示する")
    st.set_defaults(func=cmd_status)

    argv = sys.argv[1:] if argv is None else argv
    args = p.parse_args(argv)
    if not args.command:
        # 今はまだ画面がないので、起動したらデータ更新を行う
        args = p.parse_args([*argv, "update"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
