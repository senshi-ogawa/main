@echo off
rem Windows用の起動ファイル。エクスプローラーでダブルクリックすると起動する。
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
  echo Python 3.9 以上が見つかりません。
  echo https://www.python.org/downloads/ からインストールしてから、もう一度起動してください。
  echo インストール時は「Add python.exe to PATH」にチェックを入れてください。
) else (
  %PY% bootstrap.py %*
)

echo.
pause
