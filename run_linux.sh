#!/bin/bash
# Sigma fp Linux Bridge 啟動腳本
# 用於 UTM Ubuntu VM 或 Raspberry Pi 等 Linux 環境
set -e
cd "$(dirname "$0")"

# 檢查 libusb 系統依賴
if ! ldconfig -p | grep -q libusb-1.0; then
  echo "⚠️  系統缺 libusb，先裝："
  echo "    sudo apt install -y libusb-1.0-0-dev"
  exit 1
fi

# Linux 需要 plugdev 群組才能 access USB
if ! id -nG | grep -qw plugdev; then
  echo "⚠️  使用者不在 plugdev 群組，無法 access USB"
  echo "    執行：sudo usermod -aG plugdev,dialout $USER"
  echo "    然後 logout / login 重新生效"
  echo ""
  echo "或者每次用 sudo 跑（不推薦）："
  echo "    sudo ./run_linux.sh"
  # 不要 exit，可能是 root 跑
fi

# 建 venv（第一次）
if [ ! -d ".venv" ]; then
  echo "==> 第一次啟動，建立 venv 並安裝依賴..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

# 確認 fp 在 lsusb 看得到
if ! lsusb | grep -qi "1003:c432\|SIGMA"; then
  echo "⚠️  lsusb 看不到 Sigma fp"
  echo "    UTM users：上方 toolbar → USB icon → 勾選 SIGMA fp"
  echo "    Real Linux：確認 USB 線、相機 USB Mode 是 PTP"
  echo ""
  echo "    繼續啟動 server（會自動重連）..."
fi

echo "==> 啟動 Sigma fp Bridge..."
echo "    Ctrl+C 結束"
echo ""
exec .venv/bin/python mac_bridge_server.py
