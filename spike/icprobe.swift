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

let ISO_AUTO_OFF = "0008000000"
let ISO_AUTO_ON  = "0008000100"

func hex2(_ d: Data?, _ limit: Int = 32) -> String { hex(d, limit) }

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

    // 影格計時
    var times: [Double] = []
    var sizes: [Int] = []
    var pending = 0
    let total = 60

    func grabFrame() {
        guard let cam = camera, pending < total else { report(); return }
        pending += 1
        let t0 = Date()
        cam.requestSendPTPCommand(ptpCommand(0x902b), outData: nil) {
            data, _, error in
            self.times.append(Date().timeIntervalSince(t0) * 1000)
            self.sizes.append(data.count)
            if let error, self.pending == 1 {
                print("   ✗ \(error.localizedDescription)")
            }
            self.grabFrame()
        }
    }

    func report() {
        let t = times.sorted()
        let bytes = sizes.reduce(0, +)
        let wall = times.reduce(0, +) / 1000.0
        print("=== SigmaGetViewFrame ×\(times.count) ===")
        if !t.isEmpty {
            print(String(format: "  每張耗時  中位數 %.1f ms  90分位 %.1f  最大 %.1f",
                         t[t.count/2], t[Int(Double(t.count)*0.9)], t.last!))
            print(String(format: "  影格大小  平均 %.0f KB", Double(bytes)/Double(sizes.count)/1024))
            print(String(format: "  連續拉取  %.1f 張/秒  （%.1f MB/s）",
                         Double(times.count)/wall, Double(bytes)/wall/1_048_576))
        }
        camera?.requestCloseSession()
        done = true
    }

    /// 送一個帶資料階段的指令（host → camera）。這是這個 bridge 一半的工作：
    /// 每一個設定的寫入都走這條路，而先前只測過讀取。
    func sendWithData(_ label: String, _ opcode: UInt16, _ hex: String,
                      then next: @escaping () -> Void) {
        guard let cam = camera else { next(); return }
        var bytes = [UInt8]()
        var i = hex.startIndex
        while i < hex.endIndex {
            let j = hex.index(i, offsetBy: 2)
            bytes.append(UInt8(hex[i..<j], radix: 16)!)
            i = j
        }
        let payload = Data(bytes)
        print("→ \(label)  opcode 0x\(String(format: "%04x", opcode))  "
              + "送出 \(payload.count) bytes: \(hex)")
        cam.requestSendPTPCommand(ptpCommand(opcode), outData: payload) {
            data, response, error in
            if let error {
                print("   ✗ \(error.localizedDescription)")
            } else {
                print("   回應 \(hex2(response, 16))")
                let code = response.count >= 8
                    ? UInt16(response[6]) | (UInt16(response[7]) << 8) : 0
                print("   回應碼 0x\(String(format: "%04x", code))  "
                      + (code == 0x2001 ? "OK" : "← 不是 OK"))
            }
            print("")
            next()
        }
    }

    func writeTest() {
        // 測 ISOAuto 這個旗標，不測 ISOSpeed —— 相機在 ISO 自動時會自己蓋掉
        // ISOSpeed（實測它從 32 漂到 43，因為光線變了），寫進去看不出效果。
        // payload 由本專案自己的編碼器產生（sigma_ptpy.schema.CamDataGroup1）。
        send("讀 DataGroup1（改之前）", 0x9012) {
            self.sendWithData("關掉 ISO 自動", 0x9016, ISO_AUTO_OFF) {
                self.send("讀 DataGroup1（改之後）", 0x9012) {
                    self.sendWithData("還原 ISO 自動", 0x9016, ISO_AUTO_ON) {
                        self.send("讀 DataGroup1（還原後）", 0x9012) {
                            self.camera?.requestCloseSession()
                            self.done = true
                        }
                    }
                }
            }
        }
    }

    /// 同時丟出去的指令，是排隊還是並行？
    ///
    /// 這決定 worker 要不要重寫。現在的優先權佇列存在的理由是「USB 只有一條
    /// 線」，如果 ImageCaptureCore 自己就會排隊，那個理由不變；如果它能交錯，
    /// 那麼下載一張 27 MB 的 DNG（實測 2.5 秒）就不會再卡住 live view。
    func concurrencyTest() {
        let t0 = Date()
        func ms() -> Double { Date().timeIntervalSince(t0) * 1000 }
        guard let cam = camera else { done = true; return }

        var finished = 0
        func maybeDone() {
            finished += 1
            if finished == 2 { self.phase2(t0) }
        }

        print("同時送出：慢的（GetViewFrame，約 30 ms／623 KB）與快的（DataGroup1，21 bytes）")
        print(String(format: "  %7.1f ms  送出 慢的", ms()))
        cam.requestSendPTPCommand(ptpCommand(0x902b), outData: nil) { d, _, e in
            print(String(format: "  %7.1f ms  ← 慢的回來 (%d bytes)%@",
                         ms(), d.count, e == nil ? "" : " 錯誤"))
            maybeDone()
        }
        print(String(format: "  %7.1f ms  送出 快的", ms()))
        cam.requestSendPTPCommand(ptpCommand(0x9012), outData: nil) { d, _, e in
            print(String(format: "  %7.1f ms  ← 快的回來 (%d bytes)%@",
                         ms(), d.count, e == nil ? "" : " 錯誤"))
            maybeDone()
        }
    }

    /// 十個一起丟，總時間是「一個的十倍」還是更少？
    func phase2(_ _unused: Date) {
        guard let cam = camera else { done = true; return }
        print("\n十個 GetViewFrame 同時丟出去…")
        let t0 = Date()
        var left = 10
        for _ in 0..<10 {
            cam.requestSendPTPCommand(ptpCommand(0x902b), outData: nil) { _, _, _ in
                left -= 1
                if left == 0 {
                    let total = Date().timeIntervalSince(t0) * 1000
                    print(String(format: "  十個總共 %.0f ms → 每個 %.1f ms", total, total/10))
                    print("  （單獨一個是 29.7 ms。接近 297 ms＝排隊，接近 30 ms＝並行）")
                    self.camera?.requestCloseSession()
                    self.done = true
                }
            }
        }
    }

    /// 每個指令平均要多久。這決定 worker 怎麼排序才對。
    struct Op { let code: UInt16; let name: String; let write: String? }
    let ops: [Op] = [
        Op(code: 0x1001, name: "GetDeviceInfo（標準）",      write: nil),
        Op(code: 0x9012, name: "GetCamDataGroup1（曝光）",    write: nil),
        Op(code: 0x9013, name: "GetCamDataGroup2（解析度）",  write: nil),
        Op(code: 0x9014, name: "GetCamDataGroup3（色彩）",    write: nil),
        Op(code: 0x9023, name: "GetCamDataGroup4",           write: nil),
        Op(code: 0x9027, name: "GetCamDataGroup5",           write: nil),
        Op(code: 0x9031, name: "GetCamDataGroupFocus",       write: nil),
        Op(code: 0x9033, name: "GetCamDataGroupMovie",       write: nil),
        Op(code: 0x9030, name: "GetCamCanSetInfo5（能力）",   write: nil),
        Op(code: 0x9015, name: "GetCamCaptStatus",           write: nil),
        Op(code: 0x902b, name: "GetViewFrame（623 KB）",      write: nil),
        Op(code: 0x9016, name: "SetCamDataGroup1（寫入）",    write: "0008000100"),
        Op(code: 0x9032, name: "SetCamDataGroupFocus（寫入）", write: nil),
    ]
    var results: [(String, [Double], Int)] = []

    /// 依序套用設定，全部完成才開始計時
    func applySteps(_ steps: [(String, UInt16, String)], _ idx: Int) {
        if idx >= steps.count {
            // CanSetInfo5 是相機宣告「我接受哪些值、用什麼編碼」。bridge 成功
            // 的關鍵就是原封送回這裡面的編碼，而不是自己推導（見
            // movie_settings._encode_preferring_camera_form）。
            if let cam = self.camera {
                cam.requestSendPTPCommand(ptpCommand(0x9030), outData: nil) { d, _, _ in
                    print("CanSetInfo5 完整 (\(d.count) bytes):")
                    print(d.map { String(format: "%02x", $0) }.joined())
                    self.afterCaps()
                }
                return
            }
            print("設定完成，讀回確認：")
            send("DataGroup1（曝光）", 0x9012) {
                guard let cam = self.camera else { self.benchAll(); return }
                cam.requestSendPTPCommand(ptpCommand(0x9033), outData: nil) { d, _, _ in
                    print("DataGroupMovie 完整 (\(d.count) bytes):")
                    print(d.map { String(format: "%02x", $0) }.joined())
                    self.benchAll()
                }
            }
            return
        }
        let (name, code, hexs) = steps[idx]
        sendWithData("設定 " + name, code, hexs) { self.applySteps(steps, idx + 1) }
    }

    func afterCaps() {
        print("設定完成，讀回確認：")
        send("DataGroup1（曝光）", 0x9012) {
            guard let cam = self.camera else { self.benchAll(); return }
            cam.requestSendPTPCommand(ptpCommand(0x9033), outData: nil) { d, _, _ in
                print("DataGroupMovie 完整 (\(d.count) bytes):")
                print(d.map { String(format: "%02x", $0) }.joined())
                self.benchAll()
            }
        }
    }

    /// 對焦控制走不走得通 —— 這是整個 bridge 最重要的寫入路徑。
    ///
    /// 照 sigma_fp_focus.set_focus_position 的做法：一次寫入同時關掉三個會
    /// 搶回焦點的子系統（FocusMode=MF、AFLock=Off、PreConstAF=Off）再帶位置。
    /// 少關任何一個，寫進去的位置都會被相機自己蓋掉。
    func focusTest() {
        let near = "3800000004000000010001000100000001000000020001000100000000000000330001000100000000000000510003000100000064190000"  // 6500
        let far  = "3800000004000000010001000100000001000000020001000100000000000000330001000100000000000000510003000100000028230000"  // 9000
        // 先確實切到 AF-S，才問「從 AF 出發的第一筆寫入會不會被吞」
        sendWithData("切到 AF-S", 0x9032, "1400000001000000010001000100000003000000") {
        self.settle {
        self.readFocus("起點（AF-S）") {
            self.sendWithData("寫入位置 6500（第一筆）", 0x9032, near) {
                self.settle {
                    self.readFocus("寫 6500 之後") {
                        self.sendWithData("寫入位置 9000", 0x9032, far) {
                            self.settle {
                                self.readFocus("寫 9000 之後") {
                                    self.camera?.requestCloseSession(); self.done = true
                                }
                            }
                        }
                    }
                }
            }
        }
        }}
    }

    /// 設定對焦要多久。分兩種：寫同一個位置（純交易成本，馬達不動）和
    /// 交替兩個位置（真實使用，馬達要動）。
    func focusTiming() {
        let a = "3800000004000000010001000100000001000000020001000100000000000000330001000100000000000000510003000100000064190000"  // 6500
        let b = "3800000004000000010001000100000001000000020001000100000000000000330001000100000000000000510003000100000098190000"  // 6552
        func run(_ label: String, alternate: Bool, _ next: @escaping () -> Void) {
            var times: [Double] = []
            func one(_ left: Int) {
                if left == 0 {
                    let t = times.sorted()
                    print(String(format: "  %@  中位數 %.2f ms  90分位 %.2f  最大 %.2f",
                                 label as NSString, t[t.count/2],
                                 t[Int(Double(t.count)*0.9)], t.last ?? 0))
                    next(); return
                }
                let hexs = alternate ? (left % 2 == 0 ? a : b) : a
                var bytes = [UInt8](); var i = hexs.startIndex
                while i < hexs.endIndex {
                    let j = hexs.index(i, offsetBy: 2)
                    bytes.append(UInt8(hexs[i..<j], radix: 16)!); i = j
                }
                let t0 = Date()
                self.camera?.requestSendPTPCommand(ptpCommand(0x9032),
                                                   outData: Data(bytes)) { _, _, _ in
                    times.append(Date().timeIntervalSince(t0) * 1000)
                    one(left - 1)
                }
            }
            one(30)
        }
        print("SetCamDataGroupFocus（0x9032）：")
        run("寫同一個位置（馬達不動）", alternate: false) {
            run("交替兩個位置（馬達要動）", alternate: true) {
                // 對照：讀一次對焦狀態
                var rt: [Double] = []
                func rd(_ left: Int) {
                    if left == 0 {
                        let t = rt.sorted()
                        print(String(format: "  讀回對焦狀態（0x9031）  中位數 %.2f ms", t[t.count/2]))
                        print(String(format: "\n  → 一次完整的設定對焦（寫＋讀回）約 %.1f ms", 0.0))
                        self.camera?.requestCloseSession(); self.done = true
                        return
                    }
                    let t0 = Date()
                    self.camera?.requestSendPTPCommand(ptpCommand(0x9031), outData: nil) { _,_,_ in
                        rt.append(Date().timeIntervalSince(t0) * 1000); rd(left - 1)
                    }
                }
                rd(30)
            }
        }
    }

    func settle(_ next: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { next() }
    }

    func readFocus(_ label: String, then next: @escaping () -> Void) {
        guard let cam = camera else { next(); return }
        cam.requestSendPTPCommand(ptpCommand(0x9031), outData: nil) { d, _, _ in
            print("\(label): \(d.map { String(format: "%02x", $0) }.joined())")
            next()
        }
    }

    func benchAll() {
        print("每個指令跑 30 次…\n")
        runOp(0)
    }

    func runOp(_ idx: Int) {
        if idx >= ops.count { benchReport(); return }
        let op = ops[idx]
        // SetCamDataGroupFocus 沒有安全的無害 payload，跳過寫入只測指令本身
        if op.code == 0x9032 { runOp(idx + 1); return }
        var times: [Double] = []
        var size = 0
        func one(_ left: Int) {
            if left == 0 {
                self.results.append((op.name, times, size))
                self.runOp(idx + 1)
                return
            }
            let t0 = Date()
            let payload: Data? = op.write.map { h in
                var b = [UInt8](); var i = h.startIndex
                while i < h.endIndex {
                    let j = h.index(i, offsetBy: 2)
                    b.append(UInt8(h[i..<j], radix: 16)!); i = j
                }
                return Data(b)
            }
            self.camera?.requestSendPTPCommand(ptpCommand(op.code), outData: payload) {
                d, _, _ in
                times.append(Date().timeIntervalSince(t0) * 1000)
                size = max(size, d.count)
                one(left - 1)
            }
        }
        one(30)
    }

    func benchReport() {
        print(String(format: "%-30s %9s %9s %9s %10s", "指令", "中位數", "90分位", "最大", "回傳"))
        for (name, ts, size) in results {
            let t = ts.sorted()
            print(String(format: "%-30@ %7.2f ms %7.2f %7.2f %9@",
                         name as NSString, t[t.count/2],
                         t[Int(Double(t.count) * 0.9)], t.last ?? 0,
                         (size > 2048 ? String(format: "%.0f KB", Double(size)/1024)
                                      : "\(size) B") as NSString))
        }
        camera?.requestCloseSession(); done = true
    }

    /// 小指令要多久 —— 決定「每次寫入都讀回」值不值得
    func smallCommandTiming() {
        guard let cam = camera else { done = true; return }
        var times: [Double] = []
        let n = 40
        func one(_ left: Int) {
            if left == 0 {
                let t = times.sorted()
                print(String(format: "\nDataGroupFocus 讀取 ×%d  中位數 %.1f ms  最大 %.1f",
                             t.count, t[t.count/2], t.last!))
                print(String(format: "  對照：GetViewFrame（623 KB）29.7 ms"))
                print(String(format: "  AI 以 30/秒 送位置時，光是 readback 就佔掉 %.0f ms/秒",
                             t[t.count/2] * 30))
                self.camera?.requestCloseSession(); self.done = true
                return
            }
            let t0 = Date()
            cam.requestSendPTPCommand(ptpCommand(0x9031), outData: nil) { _, _, _ in
                times.append(Date().timeIntervalSince(t0) * 1000)
                one(left - 1)
            }
        }
        one(n)
    }

    func run() {
        if CommandLine.arguments.contains("focustime") {
            send("SigmaConfigApi", 0x9035, [0]) { self.focusTiming() }
            return
        }
        if CommandLine.arguments.contains("focus") {
            send("SigmaConfigApi", 0x9035, [0]) { self.focusTest() }
            return
        }
        if CommandLine.arguments.contains("bench") {
            // ConfigApi 會重設設定，所以要在**同一個 session 裡**自己設好，
            // 透過 bridge 設的東西對這個 session 不算數。
            //
            // 順序有講究，而且是反過來的：**寫幀率會把快門改掉**（實測寫在
            // 快門後面，1/60 變成 1/125），所以幀率要在快門之前。
            // payload 由專案自己的編碼器產生（movie_settings / CamDataGroup1）。
            send("SigmaConfigApi", 0x9035, [0]) {
                self.applySteps([
                    ("CINE",            0x9034, "1400000001000000010001000100000002000000"),
                    ("CinemaDNG + FHD", 0x9034, "20000000020000003200010001000000010000003c0001000100000001000000"),
                    ("快門用速度表示",   0x9034, "1400000001000000060001000100000001000000"),
                    // 曝光模式一定要先設成 M。ConfigApi 會把設定重設，而相機
                    // 在 P 模式下自己控制快門 —— 寫進去會被收下然後蓋掉，就跟
                    // ISO 自動時寫 ISOSpeed 一樣。這是同一個坑踩第二次。
                    ("曝光模式 M",       0x9017, "0004000400"),
                    ("ISO 手動",        0x9016, "0008000000"),
                    ("ISO 100",         0x9016, "0010002000"),
                    ("29.97p",          0x9034, "1c000000010000003d0005000100000014000000b50b000064000000"),
                    // 速度模式下快門走影片群組的 tag 7，分子是 APEX 碼（104 = 1/60）。
                    // DataGroup1 的 ShutterSpeed 在 CINE 下寫進去會被默默丟掉，
                    // 即使已經切到速度模式也一樣 —— 實測三次都回 1/125。
                    ("快門 1/60",       0x9034, "1c0000000100000007000500010000001400000068000000100e0000"),
                ], 0)
            }
            return
        }
        if CommandLine.arguments.contains("config") {
            guard let cam = camera else { done = true; return }
            cam.requestSendPTPCommand(ptpCommand(0x9035, [0]), outData: nil) {
                d, _, _ in
                print("ConfigApi 完整內容 (\(d.count) bytes):")
                print(d.map { String(format: "%02x", $0) }.joined())
                self.camera?.requestCloseSession(); self.done = true
            }
            return
        }
        if CommandLine.arguments.contains("small") {
            send("SigmaConfigApi", 0x9035, [0]) { self.smallCommandTiming() }
            return
        }
        if CommandLine.arguments.contains("concurrent") {
            send("SigmaConfigApi", 0x9035, [0]) { self.concurrencyTest() }
            return
        }
        if CommandLine.arguments.contains("write") {
            send("SigmaConfigApi", 0x9035, [0]) { self.writeTest() }
            return
        }
        // 1) 標準指令：先證明通道本身能用
        send("GetDeviceInfo（標準 PTP）", 0x1001) {
            // 2) Sigma 的 API 開關 —— 多數 vendor 指令要先開這個
            self.send("SigmaConfigApi（vendor）", 0x9035, [0]) {
                // 3) 真正要的：讀第一組相機設定
                self.send("SigmaGetCamDataGroup1（vendor）", 0x9012) {
                    // 4) 大量傳輸 —— 這才是決定值不值得換傳輸層的數字
                    print("拉 \(self.total) 張 live view 影格計時…\n")
                    self.grabFrame()
                }
            }
        }
    }
}

let probe = Probe()
probe.start()
let deadline = Date().addingTimeInterval(180)
while !probe.done && Date() < deadline {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
}
if !probe.done { print("逾時 —— 沒有找到相機，或權限沒過") }
