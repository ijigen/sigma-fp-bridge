#!/bin/bash
# Sigma fp Mac Bridge 啟動腳本
set -e
cd "$(dirname "$0")"

# 確認 venv
#
# 這支腳本跑的是原始碼，不是打包好的執行檔 —— 打包版是 dist/sigma-fp-bridge，
# 那個不需要 python 也不需要 venv。兩邊都要 sudo，理由跟打包無關：要從
# ptpcamerad 手上搶到相機就是需要 root。
if [ ! -d ".venv" ]; then
  echo "==> 第一次啟動，建立 venv 並安裝依賴..."
  # venv 要用真正的使用者建，不能用 root。整包 sudo 跑的話 .venv 會變成
  # root 所有，之後不用 sudo 就裝不了東西，而錯誤訊息只會說 permission
  # denied，看不出來是這裡造成的。
  if [ -n "${SUDO_USER:-}" ]; then
    sudo -u "$SUDO_USER" python3 -m venv .venv
    sudo -u "$SUDO_USER" .venv/bin/python -m pip install --upgrade pip
    sudo -u "$SUDO_USER" .venv/bin/python -m pip install -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
  fi
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
