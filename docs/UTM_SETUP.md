# UTM + Ubuntu Linux VM 設定（在 Mac 上跑 fp bridge）

因為 macOS 14/15 對 PTP 相機鎖死，pyusb 跟 libgphoto2 在 Apple Silicon 都失敗。Linux 沒這個問題——`libusb` 暢通、沒有 `ptpcamerad`、沒有 Apple Silicon arm64 build bug。

整個過程 ~30 分鐘。

---

## 步驟 1：下載 UTM（免費）

**選一個**：

- **App Store 版**（$9.99，方便自動更新）：搜 "UTM Virtual Machines"
- **免費版**：https://mac.getutm.app/ 下載 `.dmg`

兩個功能完全一樣，免費版下載即可。

---

## 步驟 2：下載 Ubuntu Server ARM64

Apple Silicon 用 **arm64 版**，不是 x86_64。

下載：https://ubuntu.com/download/server/arm

挑 **Ubuntu Server 24.04 LTS** (arm64) — 是 server 版（沒 GUI），fp bridge 不需要桌面，更省資源。

下載完是個 `ubuntu-24.04.x-live-server-arm64.iso`，約 3GB。

---

## 步驟 3：UTM 建立 VM

1. 開 UTM → 點 "Create a New Virtual Machine"
2. 選 **"Virtualize"**（不是 Emulate；arm64 host 跑 arm64 guest 才能用 virtualize）
3. 選 **Linux**
4. 在 "Boot ISO Image" 點 Browse 選你下載的 Ubuntu ISO
5. RAM：**4 GB**（你 Mac 應該夠）
6. CPU cores：**2**
7. Storage：**16 GB** 動態大小
8. Shared directory：跳過（之後用 ssh + scp 比較簡單）
9. Save 命名為 `sigma-bridge`

---

## 步驟 4：第一次啟動 + Ubuntu 安裝

1. Start VM → Ubuntu installer 開始
2. 全部選預設選項，注意以下幾個重點：
   - **Network**：用 default DHCP
   - **Storage**：使用整個 disk
   - **Profile**：
     - Server name: `sigma-bridge`
     - Username: 自己取一個（下面的例子用 `you`）
     - Password: 設個你記得的（待會要 SSH）
   - **SSH**：**勾選 "Install OpenSSH server"** ← 重要！
   - **Featured snaps**：全部跳過
3. 安裝大概 10-15 分鐘
4. 完成後選 reboot now
5. **重啟後 UTM 要手動退出 ISO**：上方 toolbar → 點 disc icon → CD/DVD → Clear

---

## 步驟 5：拿到 VM 的 IP

VM 重啟到登入畫面，登入後：

```bash
ip addr show
```

找 `enp0s1` 或類似網卡，記下 IP（UTM 的共享網路通常是 `192.168.64.x`）。

也可以從 Mac 端用 `arp -a` 看 `192.168.64.*` 段。

---

## 步驟 6：從 Mac SSH 進去（之後都用 SSH，不用 VM 視窗）

在 Mac 終端：

```bash
ssh you@VM_IP        # 換成上一步記下的帳號與 IP
```

進去後做基本更新：

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git libusb-1.0-0-dev
```

---

## 步驟 7：USB 直通 fp 到 VM 🔑

**這是關鍵步驟**。VM 預設看不到 USB 裝置，要手動 attach。

1. fp 接 Mac USB-C
2. UTM VM 視窗的上方 toolbar：
   - 找 **USB icon**（看起來像個 USB 插頭）
   - 下拉選單會列出 Mac 上所有 USB 裝置
   - 找 **"SIGMA fp"**，點它打勾
3. fp 從 macOS 「消失」，被 VM 接管
4. 進 VM 終端跑：
   ```bash
   lsusb
   ```
   應該看到：
   ```
   Bus 001 Device 002: ID 1003:c432 SIGMA Corporation Sigma fp
   ```
5. **VM 內測試 gphoto2**：
   ```bash
   sudo apt install -y gphoto2
   gphoto2 --auto-detect
   gphoto2 --summary
   ```
   Linux 上應該直接成功印出相機資訊。

---

## 步驟 8：把 bridge code 傳到 VM

從 Mac 端傳：

```bash
cd ~/Desktop/sigma-fp-focus-poc
scp -r . you@VM_IP:~/sigma-bridge/
```

進 VM：

```bash
ssh you@VM_IP
cd ~/sigma-bridge
chmod +x run_linux.sh   # 待會會新增這個檔案
```

---

## 步驟 9：跑 bridge

```bash
./run_linux.sh
```

第一次會建 venv + 裝依賴。之後直接 run。

成功的話會印：
```
2026-06-05 ... INFO sigma-bridge | 相機已連線
2026-06-05 ... INFO sigma-bridge |   瀏覽器測試:   http://VM_IP:1025/
```

---

## 步驟 10：從 Mac 瀏覽器測試

在 Mac 開瀏覽器：

```
http://VM_IP:1025/
```

應該看到控制面板、live view、可以設 focus position！

---

## 後續：iPhone 連線

VM 的網路通常會跟 Mac 共享同網段。如果你的 Wi-Fi 跟 Mac 同一個：
- iPhone 也能直接連 `http://VM_IP:1025/`
- Bonjour 也能找到（_sigmafp._tcp）

如果 UTM 用「Shared Network」模式，VM 在自己的 NAT 後面，iPhone 可能要：
- UTM 設定改 **"Bridged Mode"**：VM 拿到跟 Mac 同網段的 IP
- 或在 Mac 端做 port forwarding

---

## 常見問題

**Q: UTM 抓不到 SIGMA fp？**
- 確認 fp USB Mode 是 PTP
- 試重新插拔
- 確認 macOS 沒被 ptpcamerad 搶走（先 `sudo killall -STOP ptpcamerad` 再插）

**Q: VM 內 lsusb 看到但 gphoto2 仍說 Could not claim？**
- VM 內 user 要在 plugdev / dialout group：
  ```bash
  sudo usermod -aG plugdev,dialout $USER
  # 然後 logout/login
  ```

**Q: VM 開機很慢？**
- UTM 在 M 系列上應該很快（< 30s）
- 慢的話檢查 Settings → System → 是不是用了 Virtualize 不是 Emulate

**Q: VM 跑起來 Mac 變很卡？**
- VM RAM 降到 2GB
- CPU cores 降到 1

---

## 下一步

跑通後告訴我，我幫你規劃：
1. VM 開機自動啟動 bridge（systemd service）
2. fp 自動偵測 + 自動連線
3. 之後遷移到 Raspberry Pi 的流程
