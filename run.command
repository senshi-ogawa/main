#!/bin/bash
# Mac用の起動ファイル。Finderでダブルクリックすると起動する。
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    PY="$c"; break
  fi
done

if [ -z "$PY" ]; then
  echo "Python 3.9 以上が見つかりません。"
  echo "https://www.python.org/downloads/ からインストールしてから、もう一度起動してください。"
else
  "$PY" bootstrap.py "$@"
fi

echo
read -r -p "Enterキーで閉じます" _
