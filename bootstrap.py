"""起動の下準備（Mac・Windows共通）。run.command / run.bat から呼ばれる。

1. git で入れた場合は最新版を取り込む
2. アプリ専用の Python 環境 (.venv) を用意する
3. requirements.txt が変わったときだけ部品を入れ直す
4. アプリを起動する

標準ライブラリだけで動くようにしてある。
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
STAMP = ROOT / ".venv-stamp"
REQUIREMENTS = ROOT / "requirements.txt"
MIN_PYTHON = (3, 9)


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def pull_latest() -> None:
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        return
    print("最新版を確認しています…", flush=True)
    r = subprocess.run(["git", "pull", "--ff-only"], cwd=ROOT)
    if r.returncode != 0:
        print("最新版を取り込めませんでした。今の版のまま起動します。", flush=True)


def ensure_venv() -> None:
    py = venv_python()
    if not py.exists():
        print("初回の準備をしています（数分かかります）…", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    want = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    have = STAMP.read_text().strip() if STAMP.exists() else ""
    if want != have:
        print("必要な部品を入れています…", flush=True)
        subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)], check=True)
        STAMP.write_text(want)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です（今は {sys.version.split()[0]}）。"
              "https://www.python.org/downloads/ から新しい版を入れてください。")
        return 1
    pull_latest()
    try:
        ensure_venv()
    except subprocess.CalledProcessError:
        print("準備に失敗しました。インターネット接続を確認して、もう一度起動してください。")
        return 1
    return subprocess.run([str(venv_python()), "-m", "kabutrainer", *sys.argv[1:]], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
