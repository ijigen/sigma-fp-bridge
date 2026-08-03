# spike/

一次性的探針，用來回答「值不值得動架構」這種問題。不是產品程式碼，不會進
執行檔，也不保證維護。

- `icprobe.swift` — ImageCaptureCore 能不能送 Sigma 的 vendor PTP opcode。
  能，而且不用 sudo。結果記在 `docs/GOTCHAS.md`。

  ```bash
  swiftc -O icprobe.swift -o icprobe && ./icprobe
  ```
