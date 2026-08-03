// ImageCaptureCore 能不能送 Sigma 的 vendor PTP opcode？
//
// 現在的 bridge 要 sudo，唯一的理由是跟 ptpcamerad 搶 USB 介面（見
// docs/GOTCHAS.md）。ImageCaptureCore 是「透過」ptpcamerad 說話而不是跟它
// 對打 —— 如果它承載得了 0x901x–0x904x，就有一條不用 sudo、可以簽名成
// 一般 .app 的路。
//
//   swiftc -O icprobe.swift -o icprobe && ./icprobe
import Foundation
import ImageCaptureCore

/// PTP 的指令封包：長度(4) 型別=1(2) opcode(2) transactionID(4) 參數(各 4)
func ptpCommand(_ opcode: UInt16, _ params: [UInt32] = []) -> Data {
    var d = Data()
    let len = UInt32(12 + params.count * 4)
    d.append(contentsOf: withUnsafeBytes(of: len.littleEndian, Array.init))
    d.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian, Array.init))
    d.append(contentsOf: withUnsafeBytes(of: opcode.littleEndian, Array.init))
    d.append(contentsOf: withUnsafeBytes(of: UInt32(1).littleEndian, Array.init))
    for p in params {
        d.append(contentsOf: withUnsafeBytes(of: p.littleEndian, Array.init))
    }
    return d
}

func hex(_ d: Data?, _ limit: Int = 32) -> String {
    guard let d, !d.isEmpty else { return "（空）" }
    let s = d.prefix(limit).map { String(format: "%02x", $0) }.joined(separator: " ")
    return d.count > limit ? "\(s) …（共 \(d.count) bytes）" : "\(s)（\(d.count) bytes）"
}

final class Probe: NSObject, ICDeviceBrowserDelegate, ICCameraDeviceDelegate {
    let browser = ICDeviceBrowser()
    var camera: ICCameraDevice?
    var done = false

    func start() {
        browser.delegate = self
        // 不設 mask，先看預設能看到什麼 —— 縮小範圍是等確認看得到裝置之後的事
        browser.browsedDeviceTypeMask = ICDeviceTypeMask(
            rawValue: ICDeviceTypeMask.camera.rawValue
                | ICDeviceLocationTypeMask.local.rawValue
                | ICDeviceLocationTypeMask.shared.rawValue)!
        browser.start()
        print("尋找相機…（macOS 可能會跳權限對話框，請按允許）")
        print("browser.devices 目前：\(browser.devices?.count ?? -1) 個")
    }

    func deviceBrowser(_ b: ICDeviceBrowser, didAdd device: ICDevice, moreComing: Bool) {
        print("didAdd：\(device.name ?? "?") 型別=\(device.type.rawValue) "
              + "\(device is ICCameraDevice ? "（相機）" : "（不是相機）")")
        guard let cam = device as? ICCameraDevice else { return }
        let caps = cam.capabilities.map { "\($0)" }
        print("找到：\(cam.name ?? "（無名）")")
        print("能接 PTP 指令：",
              caps.contains { $0.contains("PTPCommand") } ? "是" : "否   能力清單：\(caps)")
        camera = cam
        cam.delegate = self
        cam.requestOpenSession()
    }

    func deviceBrowser(_ b: ICDeviceBrowser, didRemove d: ICDevice, moreGoing: Bool) {}

    func device(_ device: ICDevice, didOpenSessionWithError error: Error?) {
        if let error { print("開 session 失敗：\(error)"); done = true; return }
        print("session 已開，開始送指令\n")
        run()
    }
    func device(_ device: ICDevice, didCloseSessionWithError error: Error?) { done = true }
    func didRemove(_ device: ICDevice) {}
    func cameraDeviceDidRemoveAccessRestriction(_ device: ICDevice) {}
    func cameraDeviceDidEnableAccessRestriction(_ device: ICDevice) {}
    func deviceDidBecomeReady(withCompleteContentCatalog device: ICCameraDevice) {}
    func cameraDevice(_ c: ICCameraDevice, didAdd items: [ICCameraItem]) {}
    func cameraDevice(_ c: ICCameraDevice, didRemove items: [ICCameraItem]) {}
    func cameraDevice(_ c: ICCameraDevice, didReceiveThumbnail t: CGImage?,
                      for item: ICCameraItem, error: Error?) {}
    func cameraDevice(_ c: ICCameraDevice, didReceiveMetadata m: [AnyHashable: Any]?,
                      for item: ICCameraItem, error: Error?) {}
    func cameraDevice(_ c: ICCameraDevice, didRenameItems items: [ICCameraItem]) {}
    func cameraDeviceDidChangeCapability(_ c: ICCameraDevice) {}
    func cameraDevice(_ c: ICCameraDevice, didReceivePTPEvent e: Data) {}
    func cameraDeviceDidEnumerateContents(_ c: ICCameraDevice) {}
    func cameraDevice(_ c: ICCameraDevice, didCompleteDeleteFilesWithError e: Error?) {}

    func send(_ label: String, _ opcode: UInt16, _ params: [UInt32] = [],
              then next: @escaping () -> Void) {
        guard let cam = camera else { next(); return }
        print("→ \(label)  opcode 0x\(String(format: "%04x", opcode))")
        cam.requestSendPTPCommand(ptpCommand(opcode, params), outData: nil) {
            data, response, error in
            if let error {
                print("   ✗ \(error.localizedDescription)")
            } else {
                print("   資料 \(hex(data))")
                print("   回應 \(hex(response, 16))")
            }
            print("")
            next()
        }
    }

    func run() {
        // 1) 標準指令：先證明通道本身能用
        send("GetDeviceInfo（標準 PTP）", 0x1001) {
            // 2) Sigma 的 API 開關 —— 多數 vendor 指令要先開這個
            self.send("SigmaConfigApi（vendor）", 0x9035, [0]) {
                // 3) 真正要的：讀第一組相機設定
                self.send("SigmaGetCamDataGroup1（vendor）", 0x9012) {
                    self.camera?.requestCloseSession()
                    self.done = true
                }
            }
        }
    }
}

let probe = Probe()
probe.start()
let deadline = Date().addingTimeInterval(20)
while !probe.done && Date() < deadline {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
}
if !probe.done { print("逾時 —— 沒有找到相機，或權限沒過") }
