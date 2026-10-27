#!/usr/bin/env python3
"""Persist complete WHOOP 5 historical bursts before echoing the HISTORY_END token."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def one(name: str) -> Path:
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one {name}, found {len(matches)}")
    return matches[0]

# Expose a batch-commit hook. The ACK path must not advance the strap cursor
# until the exact raw burst and its echoed token are on durable local storage.
client = one("GooseBLEClient.swift")
s = client.read_text()
anchor = "  var pendingHistoryEndAckPayload: [UInt8]?\n"
replacement = anchor + """  var pendingHistoricalBatchFrames: [Data] = []
  var historicalBatchCommitComplete = false
  var historicalBatchCommitInProgress = false
  var onHistoricalBatchCommit: (([Data], [UInt8], @escaping (Bool) -> Void) -> Void)?
"""
if "var pendingHistoricalBatchFrames: [Data]" not in s:
    if s.count(anchor) != 1:
        raise SystemExit("Could not locate historical batch state.")
    s = s.replace(anchor, replacement, 1)
client.write_text(s)

# Buffer every complete non-control frame belonging to the current history burst.
handlers = one("GooseBLEClient+HistoricalHandlers.swift")
h = handlers.read_text()
anchor = """    switch packetType {
    case V5PacketType.commandResponse, V5PacketType.puffinCommandResponse:
"""
replacement = """    if packetType != V5PacketType.commandResponse,
       packetType != V5PacketType.puffinCommandResponse,
       packetType != V5PacketType.metadata,
       packetType != V5PacketType.puffinMetadata {
      pendingHistoricalBatchFrames.append(frame)
    }

    switch packetType {
    case V5PacketType.commandResponse, V5PacketType.puffinCommandResponse:
"""
if replacement not in h:
    if h.count(anchor) != 1:
        raise SystemExit("Could not locate historical frame switch.")
    h = h.replace(anchor, replacement, 1)

start_anchor = """    case .historyStart:
      historyStartReceived = true
      historyEndAckQueued = false
      historyEndAckSentThisBurst = false
      pendingHistoryEndAckPayload = nil
"""
start_replacement = """    case .historyStart:
      historyStartReceived = true
      historyEndAckQueued = false
      historyEndAckSentThisBurst = false
      pendingHistoryEndAckPayload = nil
      pendingHistoricalBatchFrames.removeAll(keepingCapacity: true)
      historicalBatchCommitComplete = false
      historicalBatchCommitInProgress = false
"""
if start_replacement not in h:
    if h.count(start_anchor) != 1:
        raise SystemExit("Could not locate HISTORY_START reset.")
    h = h.replace(start_anchor, start_replacement, 1)

complete_anchor = """    historyEndAckQueued = false
    historyEndAckSentThisBurst = false
    pendingHistoryEndAckPayload = nil
    historyStartReceived = false
"""
complete_replacement = """    historyEndAckQueued = false
    historyEndAckSentThisBurst = false
    pendingHistoryEndAckPayload = nil
    pendingHistoricalBatchFrames.removeAll(keepingCapacity: true)
    historicalBatchCommitComplete = false
    historicalBatchCommitInProgress = false
    historyStartReceived = false
"""
if complete_replacement not in h:
    if h.count(complete_anchor) != 1:
        raise SystemExit("Could not locate historical completion reset.")
    h = h.replace(complete_anchor, complete_replacement, 1)
handlers.write_text(h)

# Every new transfer starts with an empty durable-batch state. ACK is enabled
# by default only because processQueuedHistoricalDataResultAck now has a commit gate.
commands = one("GooseBLEClient+HistoricalCommands.swift")
c = commands.read_text()
c = c.replace(
    "acknowledgeHistoricalDataResult: Bool = false",
    "acknowledgeHistoricalDataResult: Bool = true",
    1,
)
reset_anchor = """    pendingHistoryEndAckPayload = nil
    historyEndReceived = false
"""
reset_replacement = """    pendingHistoryEndAckPayload = nil
    pendingHistoricalBatchFrames.removeAll(keepingCapacity: true)
    historicalBatchCommitComplete = false
    historicalBatchCommitInProgress = false
    historyEndReceived = false
"""
if reset_replacement not in c:
    if c.count(reset_anchor) != 1:
        raise SystemExit("Could not locate new-sync batch reset.")
    c = c.replace(reset_anchor, reset_replacement, 1)
commands.write_text(c)

# Wait for durable batch storage before clearing the ACK queue or writing the
# 8-byte continuation token. If storage fails, fail closed: never ACK the band.
debug = one("GooseBLEClient+DebugAndSync.swift")
d = debug.read_text()
anchor = """    if !historicalDataResultAckEnabled && historicalPacketsReceivedThisSync > 0 {
"""
gate = """    if historicalDataResultAckEnabled && !historicalBatchCommitComplete {
      guard !historicalBatchCommitInProgress else {
        historyEndAckQueued = true
        return true
      }
      guard !pendingHistoricalBatchFrames.isEmpty else {
        historyEndAckQueued = false
        failHistoricalSync("HISTORY_END arrived without any complete burst frames; refusing to ACK an empty batch.")
        return true
      }
      guard let onHistoricalBatchCommit else {
        historyEndAckQueued = false
        failHistoricalSync("No durable historical batch store is installed; refusing to ACK the strap.")
        return true
      }

      historicalBatchCommitInProgress = true
      let frames = pendingHistoricalBatchFrames
      onHistoricalBatchCommit(frames, ackPayload) { [weak self] success in
        DispatchQueue.main.async {
          guard let self, self.isHistoricalSyncing else { return }
          self.historicalBatchCommitInProgress = false
          guard success else {
            self.failHistoricalSync("Could not durably save the WHOOP history batch; the band was not ACKed.")
            return
          }
          self.historicalBatchCommitComplete = true
          self.pendingHistoricalBatchFrames.removeAll(keepingCapacity: true)
          self.record(
            source: "ble.sync",
            title: "historical_sync.batch.durable",
            body: "frames=\\(frames.count) token=\\(Data(ackPayload).hexString)"
          )
          self.historyEndAckQueued = true
          _ = self.processQueuedHistoricalDataResultAck(reason: "durable_batch_commit")
        }
      }
      return true
    }

"""
if gate not in d:
    if d.count(anchor) != 1:
        raise SystemExit("Could not locate historical ACK suppression gate.")
    d = d.replace(anchor, gate + anchor, 1)
debug.write_text(d)

# Durable append-only batch archive. Each atomic JSON file contains the exact
# complete raw frames and ACK payload; a re-offer is harmless and remains auditable.
overnight = one("GooseAppModel+OvernightRun.swift")
o = overnight.read_text()
archive = """final class GooseHistoricalSyncBatchArchive {
  static let shared = GooseHistoricalSyncBatchArchive()
  private let queue = DispatchQueue(label: "com.goose.swift.historical-batch-archive", qos: .utility)

  private init() {}

  func append(
    frames: [Data],
    ackPayload: [UInt8],
    completion: @escaping (Bool) -> Void
  ) {
    queue.async {
      do {
        let base = try FileManager.default.url(
          for: .applicationSupportDirectory,
          in: .userDomainMask,
          appropriateFor: nil,
          create: true
        )
        let directory = base.appendingPathComponent("Goose", isDirectory: true).appendingPathComponent("HistoricalSync", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let token = ackPayload.dropFirst().map { String(format: "%02x", $0) }.joined()
        let name = "batch-\\(token)-\\(UUID().uuidString).json"
        let url = directory.appendingPathComponent(name)
        let payload: [String: Any] = [
          "version": 1,
          "saved_at": ISO8601DateFormatter().string(from: Date()),
          "ack_payload_hex": ackPayload.map { String(format: "%02x", $0) }.joined(),
          "token_hex": token,
          "frame_count": frames.count,
          "frames_base64": frames.map { $0.base64EncodedString() }
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        try data.write(to: url, options: .atomic)
        let check = try Data(contentsOf: url)
        guard check == data,
              let decoded = try JSONSerialization.jsonObject(with: check) as? [String: Any],
              (decoded["frame_count"] as? Int) == frames.count else {
          completion(false)
          return
        }
        completion(true)
      } catch {
        completion(false)
      }
    }
  }
}

"""
if "final class GooseHistoricalSyncBatchArchive" not in o:
    anchor = "extension GooseAppModel {\n"
    if o.count(anchor) != 1:
        raise SystemExit("Could not locate app model extension for archive type.")
    o = o.replace(anchor, archive + anchor, 1)
overnight.write_text(o)

app = one("GooseAppModel.swift")
a = app.read_text()
anchor = """    ble.onRawNotificationWithContext = { [weak self] event, context in
"""
hook = """    ble.onHistoricalBatchCommit = { frames, ackPayload, completion in
      GooseHistoricalSyncBatchArchive.shared.append(
        frames: frames,
        ackPayload: ackPayload,
        completion: completion
      )
    }
"""
if hook not in a:
    if a.count(anchor) != 1:
        raise SystemExit("Could not locate app model BLE callback setup.")
    a = a.replace(anchor, hook + anchor, 1)
app.write_text(a)

print("Enabled durable WHOOP history batch storage before HISTORY_END ACK.")
