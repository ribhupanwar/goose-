#!/usr/bin/env python3
"""Apply WHOOP 5.0 historical dispatch and validated incremental framing."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def one(name: str) -> Path:
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one {name}, found {len(matches)}")
    return matches[0]

# Historical bodies arrive on the Gen5 data characteristic. Route that
# characteristic to the sync handler while a transfer is active, without
# forcing unrelated high-rate characteristics onto the main queue.
peripheral = one("GooseBLEClient+PeripheralDelegate.swift")
source = peripheral.read_text()
guard = """    guard notificationCharacteristicIDs.contains(characteristic.uuid) else {
      return false
    }

"""
dispatch = """    // Historical data is delivered on fd4b0005. Keep this channel on the
    // sync path while an offload is active; do not move every sensor stream
    // onto the main queue.
    if isHistoricalSyncing,
       characteristic.uuid == CBUUID(string: "fd4b0005-cce1-4033-93ce-002d5875f58a") {
      return true
    }

"""
if dispatch not in source:
    if source.count(guard) != 1:
        raise SystemExit("Could not uniquely locate notification dispatch guard.")
    source = source.replace(guard, guard + dispatch, 1)
peripheral.write_text(source)

# A notification can end mid-frame. Keep a bounded, per-characteristic
# reassembly buffer, and clear it at the start of each history transfer.
client = one("GooseBLEClient.swift")
client_source = client.read_text()
property_anchor = "  var notificationSideEffectSkipCount = 0\n"
if "var v5NotificationFrameBuffers: [CBUUID: Data]" not in client_source:
    if client_source.count(property_anchor) != 1:
        raise SystemExit("Could not uniquely locate BLE client state insertion point.")
    client_source = client_source.replace(
        property_anchor,
        property_anchor + "  var v5NotificationFrameBuffers: [CBUUID: Data] = [:]\n",
        1,
    )
client.write_text(client_source)

parsing = one("GooseBLEClient+Parsing.swift")
parsing_source = parsing.read_text()
method_marker = "  func v5Frames(in data: Data, characteristic: CBUUID) -> [Data] {"
if method_marker not in parsing_source:
    static_anchor = "  static func v5Frames(in data: Data) -> [Data] {"
    if parsing_source.count(static_anchor) != 1:
        raise SystemExit("Could not uniquely locate V5 frame parser.")
    method = """  func v5Frames(in data: Data, characteristic: CBUUID) -> [Data] {
    // WHOOP 5 frame: AA | revision(01) | declared length LE | direction |
    // CRC16-Modbus(header[0..<6]) | padded inner payload | CRC32(payload).
    // declared length includes padded payload plus the trailing CRC32.
    var bytes = Array(v5NotificationFrameBuffers[characteristic] ?? Data())
    bytes.append(contentsOf: data)
    var frames: [Data] = []

    func resync() {
      if let next = bytes.dropFirst().firstIndex(of: 0xaa) {
        bytes.removeFirst(next)
      } else {
        bytes.removeAll(keepingCapacity: true)
      }
    }

    while true {
      guard let startIndex = bytes.firstIndex(of: 0xaa) else {
        bytes.removeAll(keepingCapacity: true)
        break
      }
      if startIndex > 0 {
        bytes.removeFirst(startIndex)
      }
      guard bytes.count >= 8 else { break }

      // Header CRC protects the declared length; never trust length before it.
      let headerCRC = UInt16(bytes[6]) | (UInt16(bytes[7]) << 8)
      guard bytes[1] == 0x01,
            Self.crc16Modbus(Array(bytes[0..<6])) == headerCRC else {
        resync()
        continue
      }

      let declaredLength = Int(UInt16(bytes[2]) | (UInt16(bytes[3]) << 8))
      let expectedLength = declaredLength + 8
      guard declaredLength >= 4, expectedLength <= 4096 else {
        resync()
        continue
      }
      guard bytes.count >= expectedLength else { break }

      let payloadEnd = expectedLength - 4
      let storedCRC = UInt32(bytes[payloadEnd])
        | (UInt32(bytes[payloadEnd + 1]) << 8)
        | (UInt32(bytes[payloadEnd + 2]) << 16)
        | (UInt32(bytes[payloadEnd + 3]) << 24)
      let payload = Array(bytes[8..<payloadEnd])
      guard Self.crc32(payload) == storedCRC else {
        // A bad payload CRC means the declared length may itself be corrupt.
        // Resync from the next SOF rather than swallowing later good frames.
        resync()
        continue
      }

      frames.append(Data(bytes[0..<expectedLength]))
      bytes.removeFirst(expectedLength)
      // Edge's length-based reassembler skips null inter-record padding.
      while bytes.first == 0x00 {
        bytes.removeFirst()
      }
    }

    if bytes.count <= 4096 {
      v5NotificationFrameBuffers[characteristic] = Data(bytes)
    } else {
      v5NotificationFrameBuffers.removeValue(forKey: characteristic)
    }
    return frames
  }

"""
    parsing_source = parsing_source.replace(static_anchor, method + static_anchor, 1)
parsing.write_text(parsing_source)

historical = one("GooseBLEClient+HistoricalHandlers.swift")
historical_source = historical.read_text()
old = "    for frame in Self.v5Frames(in: value) {"
new = "    for frame in v5Frames(in: value, characteristic: characteristic.uuid) {"
if new not in historical_source:
    if historical_source.count(old) < 1:
        raise SystemExit("Could not locate historical sync frame loop.")
    historical_source = historical_source.replace(old, new, 1)
historical.write_text(historical_source)

commands = one("GooseBLEClient+HistoricalCommands.swift")
commands_source = commands.read_text()
start_anchor = """    historicalPacketsReceivedThisSync = 0
"""
if "v5NotificationFrameBuffers.removeAll()" not in commands_source:
    if commands_source.count(start_anchor) != 1:
        raise SystemExit("Could not uniquely locate historical sync reset point.")
    commands_source = commands_source.replace(
        start_anchor,
        start_anchor + "    v5NotificationFrameBuffers.removeAll()\n",
        1,
    )

# Never let the band trim an uncommitted history burst. Goose currently counts
# historical packets and emits signal diagnostics, but has no atomic durable
# record+token commit path comparable to Edge's DrainController. Keep ACK off
# until that storage path exists and commits before HISTORY_END is answered.
ack_file = commands("GooseBLEClient+HistoricalCommands.swift")
ack_source = ack_file.read_text()
ack_anchor = "    acknowledgeHistoricalDataResult: Bool = true\n"
ack_replacement = """    // Safety: no HISTORY_END ACK until the historical records have been
    // durably committed. An ACK can advance the strap's trim cursor.
    acknowledgeHistoricalDataResult: Bool = false
"""
if ack_replacement not in ack_source:
    if ack_source.count(ack_anchor) != 1:
        raise SystemExit("Could not uniquely locate historical ACK default.")
    ack_source = ack_source.replace(ack_anchor, ack_replacement, 1)
ack_file.write_text(ack_source)

commands.write_text(commands_source)

print("Applied WHOOP 5.0 data-channel dispatch and CRC-validated incremental frame reassembly.")
