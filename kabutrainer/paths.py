"""データの保存場所。

株価データは数百MBになるので、アプリのフォルダではなく
ユーザーごとのデータフォルダに置く。アプリを入れ直しても消えない。
環境変数 KABU_DATA_DIR で場所を変えられる。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "KabuTrainer"


def data_dir() -> Path:
    override = os.environ.get("KABU_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def prices_db_path() -> Path:
    return data_dir() / "prices.sqlite3"
