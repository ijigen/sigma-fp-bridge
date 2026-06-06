#!/bin/bash
# Sigma fp Mac Bridge 啟動腳本
set -e
cd "$(dirname "$0")"

# 確認 venv
if [ ! -d ".venv" ]; then
  echo "==> 第一次啟動，建立 venv 並安裝依賴..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

# 讓 pyusb 找得到 libusb（Apple Silicon 用 /opt/homebrew，Intel 用 /usr/local）
if [ -d /opt/homebrew/lib ]; then
  export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:$DYLD_FALLBACK_LIBRARY_PATH"
fi
if [ -d /usr/local/lib ]; then
  export DYLD_FALLBACK_LIBRARY_PATH="/usr/local/lib:$DYLD_FALLBACK_LIBRARY_PATH"
fi

echo "==> 啟動 Sigma fp Bridge..."
echo "    Ctrl+C 結束"
echo ""
exec .venv/bin/python mac_bridge_server.py
