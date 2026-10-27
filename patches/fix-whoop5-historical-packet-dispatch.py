#!/usr/bin/env python3
"""Apply robust WHOOP 5.0 historical notification dispatch/reassembly fixes."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def one(name: str) -> Path:
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one {name}, found {len(matches)}")
    return matches[0]

# Keep historical packet handling on the main queue while a sync is active.
peripheral = one("GooseBLEClient+PeripheralDelegate.swift")
source = peripheral.read_text()
guard = """    guard notificationCharacteristicIDs.contains(characteristic.uuid) else {
      return false
    }

"""
if "if isHistoricalSyncing {\n      return true\n    }" not in source:
    if source.count(guard) != 1:
        raise SystemExit("Could not uniquely locate notification dispatch guard.")
    source = source.replace(
        guard,
        guard + """    // Historical frames can be split across BLE notifications. During an
    // active offload, route every notification through the stateful reassembler.
    if isHistoricalSyncing {
      return true
    }

""",
        1,
    )
anchor = """      case V5PacketType.commandResponse,
           V5PacketType.puffinCommandResponse,
           V5PacketType.event,
           V5PacketType.metadata,
           V5PacketType.puffinMetadata:
        return true
      default:
"""
replacement = """      case V5PacketType.commandResponse,
           V5PacketType.puffinCommandResponse,
           V5PacketType.event,
           V5PacketType.metadata,
           V5PacketType.puffinMetadata:
        return true
      case V5PacketType.historicalData,
           V5PacketType.historicalIMUDataStream:
        // Route historical body packets only during an offload; leave live
        // high-rate frames on the existing off-main path.
        if isHistoricalSyncing {
          return true
        }
        continue
      default:
"""
if replacement not in source:
    if source.count(anchor) != 1:
        raise SystemExit("Could not uniquely locate packet dispatch switch.")
    source = source.replace(anchor, replacement, 1)
peripheral.write_text(source)

# A V5 frame is allowed to span more than one CoreBluetooth notification.
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
    var bytes = Array(v5NotificationFrameBuffers[characteristic] ?? Data())
    bytes.append(contentsOf: data)
    var frames: [Data] = []

    while let startIndex = bytes.firstIndex(of: 0xaa) {
      if startIndex > 0 {
        bytes.removeFirst(startIndex)
      }
      guard bytes.count >= 8 else {
        break
      }
      let declaredLength = Int(UInt16(bytes[2]) | UInt16(bytes[3]) << 8)
      guard declaredLength >= 4, declaredLength <= 4096 else {
        bytes.removeFirst()
        continue
      }
      let expectedLength = declaredLength + 8
      guard bytes.count >= expectedLength else {
        break
      }
      frames.append(Data(bytes[0..<expectedLength]))
      bytes.removeFirst(expectedLength)
    }

    if bytes.first == 0xaa && bytes.count <= 4104 {
      v5NotificationFrameBuffers[characteristic] = Data(bytes)
    } else {
      v5NotificationFrameBuffers.removeValue(forKey: characteristic)
    }
    return frames
  }

"""
    parsing_source = parsing_source.replace(static_anchor, method + static_anchor, 1)
parsing.write_text(parsing_source)

# Use the incremental parser only for the sync state machine. Other consumers
# keep their independent parser/reassembly path.
historical = one("GooseBLEClient+HistoricalHandlers.swift")
historical_source = historical.read_text()
old = "    for frame in Self.v5Frames(in: value) {"
new = "    for frame in v5Frames(in: value, characteristic: characteristic.uuid) {"
if new not in historical_source:
    if historical_source.count(old) < 1:
        raise SystemExit("Could not locate historical sync frame loop.")
    historical_source = historical_source.replace(old, new, 1)
historical.write_text(historical_source)

print("Applied WHOOP 5.0 split-notification reassembly and historical dispatch fixes.")
