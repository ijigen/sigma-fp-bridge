# iPhone × Sigma fp 對焦控制 — 架構規劃

## 💥 必須先講清楚的限制

**iPhone 上的 App Store app 不能對 fp 發任意 PTP 命令。**

iOS 的 USB sandbox 不開放 vendor-specific USB 通訊：
- `ImageCaptureCore` 框架可以**讀取**相機照片，**不能**發 vendor PTP opcode
- `IOUSBHost` 框架理論上能做 raw USB，但 App Store 審核會擋
- 即使是 iPhone 15/16 的 USB-C，App 也只能用 Apple 限定的協定（HID、UVC、storage）
- 私有 API 路線（sideload + IOUSB）能跑但維護成本高、被 ban 風險高

**所以——必須有「橋」**。iPhone 不能直接控制 fp，但可以遙控某個能控的東西。

---

## 🏗️ 三種可行架構

### A: Raspberry Pi Zero 2 W 橋接（最務實）⭐

```
fp (USB MSC/PTP mode)
  ↕ USB-C
RPi Zero 2 W
  - Python + sigma-ptpy + 本 PoC 的 patch
  - 透過 BLE GATT server 提供 focus_position read/write 屬性
  - Bluez 用 D-Bus 或 BlueZero 寫
  ↕ BLE
iPhone
  - SwiftUI app
  - LiDAR (ARKit) + Vision (face/eye tracking)
  - Core Bluetooth central
  - 算出目標距離 → 查校準表 → 寫 BLE characteristic
```

**硬體成本：**
- RPi Zero 2 W：US$15
- microSD 16GB：$5
- 18650 電池 + USB-C BMS：$10
- 3D 印外殼：$5
- **總計：~$35**（裝在 fp 熱靴上）

**優點：**
- Python 直接跑，跟 PoC 一模一樣
- BLE 延遲足夠（典型 30-50ms 連線間隔，對焦不敏感）
- iPhone app 完全 App Store 合法
- 可以擴充：HDMI 監看、SD 卡備份、Wi-Fi 圖傳

**缺點：**
- 啟動 ~15 秒（Linux boot）
- 耗電：~1.5W，2000mAh 電池跑 ~5 小時
- 體積：5×3×2 cm

---

### B: ESP32-S3 USB Host 橋接（最迷你）

```
fp (PTP)
  ↕ USB
ESP32-S3 (USB Host Mode + WiFi/BLE)
  - 自己寫 PTP client（C/Rust）
  - 發送 0x9031 GetCamDataGroupFocus / 0x9032 SetCamDataGroupFocus
  ↕ BLE
iPhone
```

**硬體成本：**
- ESP32-S3-DevKitC：~$10
- 18650 電池 + BMS：$8
- **總計：~$20**

**優點：**
- 啟動 <1 秒
- 耗電 ~0.3W，2000mAh → 18 小時
- 體積 3×2×1 cm
- 真正「迷你」

**缺點：**
- **PTP client 要用 C 自寫**（沒有現成 lib）
- USB Host mode 在 ESP32-S3 還是新功能，文件少
- Debug 困難
- **預估開發時間 1-2 個月**（核心瓶頸）

---

### C: MacBook 當橋（最快驗證）

```
fp (PTP)
  ↕ USB-C
MacBook (本 PoC 的 sigma_fp_focus.py 直接跑)
  - 額外加一個 mDNS/Bonjour server 或 WebSocket
  ↕ Wi-Fi local
iPhone
```

**硬體成本：$0**（用現有 Mac）

**優點：**
- 今天就能 demo
- 不寫 firmware
- 可以邊跑邊監看相機 live view

**缺點：**
- 不是「迷你」，要帶 Mac
- 只適合棚拍 / studio
- 行動拍片不適用

---

## 🎯 我推薦的開發順序

**Phase 1: PoC 驗證（用 Mac，1 週）**
- 跑 `sigma_fp_focus.py` 確認 Tag 0x81 真的能驅動鏡頭
- 建立 1-2 顆鏡頭的校準表
- 量延遲：set position → motor idle 大概多少 ms

**Phase 2: Mac 橋（架構 C，2 週）**
- 把 PoC 包成 WebSocket server
- 寫 SwiftUI app：ARKit 抓臉 → 算距離 → 送 WS → Mac 控焦
- **這就是可用的 demo 了**

**Phase 3: RPi 橋（架構 A，4 週）**
- 把 Mac 那套搬到 RPi Zero 2 W
- BLE 取代 Wi-Fi
- 3D 印熱靴座
- **此時已經是攜帶式產品**

**Phase 4 (optional): ESP32 橋（架構 B，2-3 個月）**
- 只有當你想做商品化、體積極限優化才需要
- 重寫 PTP client in C
- 風險高，但學到的東西最多

---

## 🧠 iPhone 端細節

### ARKit + Vision 取距離

iPhone 12 Pro 以上有 LiDAR scanner。要量到拍攝對象距離：

```swift
import ARKit
import Vision

// 1. 啟動 ARSession with LiDAR
let config = ARWorldTrackingConfiguration()
config.frameSemantics = .sceneDepth
arSession.run(config)

// 2. 在 ARFrame delegate 抓 depth map
func session(_ session: ARSession, didUpdate frame: ARFrame) {
    guard let depthData = frame.sceneDepth?.depthMap else { return }
    
    // 3. 用 Vision 偵測人臉/眼睛位置
    let request = VNDetectFaceLandmarksRequest()
    let handler = VNImageRequestHandler(cvPixelBuffer: frame.capturedImage, ...)
    try? handler.perform([request])
    
    // 4. 在 depth map 對應位置取值（單位：公尺）
    let faceCenterDepth = sampleDepthMap(depthData, at: faceLocation)
    
    // 5. 查校準表 → focus position → 送出
    let pos = calibrationLookup(distance: faceCenterDepth)
    bleSendFocusPosition(pos)
}
```

### BLE GATT 設計

定義一個簡單 service：

```
Service UUID: 0x180F (自訂)
  Characteristic 0xFF01 (Read/Write) — Focus Position (Int16)
  Characteristic 0xFF02 (Read/Notify) — Focus State (UInt8)
  Characteristic 0xFF03 (Read) — Focus Position Range (Int16 x 2)
  Characteristic 0xFF04 (Read/Write) — Active Lens ID (用來切校準表)
```

iPhone 訂閱 0xFF02，相機馬達狀態變化會主動推 — 可以做 close loop。

### iPhone 端控制律

不要每 frame 都送新 position（BLE 連線間隔限制）。建議：
- 60 fps 從 ARKit 取距離
- 用 **EMA filter** (alpha=0.3) 平滑，避免人臉偵測抖動
- **只在 distance 變化超過 dead band**（比如 ±5cm @ 1m, ±20cm @ 5m）才送
- 預期 BLE 寫入頻率：5-15 Hz

---

## ⚠️ 已知風險

1. **iPhone LiDAR 範圍 ~5m**，更遠的目標要靠 Vision 估深度（不準）
2. **ARKit 在低光環境 Vision 變差** —— 攝影師常在這種環境工作
3. **fp 的 focus 馬達速度** —— 可能跟不上人物快速移動，要實測
4. **延遲鏈** = ARKit (16ms) + Vision (10-30ms) + BLE (30ms) + PTP (50ms?) + 馬達 (?ms) = **~100-200ms**。對講話的特寫 OK，運動拍攝可能不夠
5. **iPhone 攝影機跟 fp 攝影機視角不同** —— iPhone LiDAR 在背面，跟 fp 鏡頭的軸線有偏移，遠處 OK，近距離特寫要做視差校正

---

## 📐 替代方案：如果 iPhone 距離量測不夠用

- 在 fp 熱靴上加一顆 **TMF8828**（9 區 ToF，~$15）
- TMF8828 跟 fp 鏡頭同軸 → 沒視差問題
- 透過 RPi/ESP32 讀取 → 直接算距離
- iPhone 退居「觸控選人臉」+ 「監看」角色

如果你的拍攝場景以人物特寫為主（< 3m）、或常拍動態，這條路反而是對的。

---

## TL;DR

- **不能用 iPhone 直接 USB 控 fp**（iOS sandbox 限制）
- **架構 C (Mac)** 今天能 demo
- **架構 A (RPi)** 是真正可攜帶的最小可行產品
- **架構 B (ESP32)** 是商品化方向，但成本是 2-3 個月開發
