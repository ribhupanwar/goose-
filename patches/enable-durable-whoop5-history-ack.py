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
  var historicalExpectedPacketCount: Int?
  var historicalBurstPacketCount = 0
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
replacement = """    if isHistoricalSyncing {
      // Match Edge's burst count: known data/event/console/wrapper
      // packets plus unknown future packet types; exclude commands,
      // metadata, live streams and the two IMU stream packet types.
      switch packetType {
      case 35, 36, 37, 38, 40, 41, 49, 51, 52, 56:
        break
      default:
        historicalBurstPacketCount += 1
      }
      if packetType != V5PacketType.commandResponse,
         packetType != V5PacketType.puffinCommandResponse,
         packetType != V5PacketType.metadata,
         packetType != V5PacketType.puffinMetadata {
        pendingHistoricalBatchFrames.append(frame)
      }
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
      historicalExpectedPacketCount = nil
      historicalBurstPacketCount = 0
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
    historicalExpectedPacketCount = nil
    historicalBurstPacketCount = 0
    historicalBatchCommitComplete = false
    historicalBatchCommitInProgress = false
    historyStartReceived = false
"""
if complete_replacement not in h:
    if h.count(complete_anchor) < 1:
        raise SystemExit("Could not locate historical completion/failure reset.")
    h = h.replace(complete_anchor, complete_replacement)
end_anchor = """      pendingHistoryEndAckPayload = ackPayload
      historyEndAckQueued = true
"""
end_replacement = """      pendingHistoryEndAckPayload = ackPayload
      historicalExpectedPacketCount = payload.count >= 13
        ? Int(UInt32(payload[9]) | (UInt32(payload[10]) << 8) | (UInt32(payload[11]) << 16) | (UInt32(payload[12]) << 24))
        : nil
      historyEndAckQueued = true
"""
if end_replacement not in h:
    if h.count(end_anchor) != 1:
        raise SystemExit("Could not locate HISTORY_END expected-count fields.")
    h = h.replace(end_anchor, end_replacement, 1)
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
    historicalExpectedPacketCount = nil
    historicalBurstPacketCount = 0
    historicalBatchCommitComplete = false
    historicalBatchCommitInProgress = false
    historyEndReceived = false
"""
if reset_replacement not in c:
    if c.count(reset_anchor) != 1:
        raise SystemExit("Could not locate new-sync batch reset.")
    c = c.replace(reset_anchor, reset_replacement, 1)
commands.write_text(c)

# The manual/user-action entry point explicitly opted out of HISTORY_END ACKs.
# Turn it on now that the durable commit gate above is installed.
actions = one("GooseBLEClient+UserActions.swift")
actions_source = actions.read_text()
old_ack_opt_out = "acknowledgeHistoricalDataResult: false"
if "acknowledgeHistoricalDataResult: true" not in actions_source:
    if actions_source.count(old_ack_opt_out) != 1:
        raise SystemExit("Could not locate the explicit historical ACK opt-out.")
    actions_source = actions_source.replace(
        old_ack_opt_out,
        "acknowledgeHistoricalDataResult: true",
        1,
    )
actions.write_text(actions_source)

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
      guard let expected = historicalExpectedPacketCount else {
        historyEndAckQueued = false
        failHistoricalSync("HISTORY_END did not contain a valid expected-packet count; refusing to ACK.")
        return true
      }
      let actual = historicalBurstPacketCount
      guard actual >= expected, expected == 0 || !pendingHistoricalBatchFrames.isEmpty else {
        historyEndAckQueued = false
        failHistoricalSync("HISTORY_END burst count shortfall: expected=\\(expected) received=\\(actual); refusing to ACK.")
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
        let handle = try FileHandle(forWritingTo: url)
        try handle.synchronize()
        try handle.close()
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


# Ensure the app has finished parsing all earlier notifications and flushed its
# historical HR/HRV series before the band is allowed to trim its cursor.
stores = one("HeartRateSeriesStores.swift")
store_source = stores.read_text()
hr_flush = """  func flush() -> Bool {
    stateLock.lock()
    pendingWrite?.cancel()
    pendingWrite = nil
    var shouldPostUpdate = false
    if !pendingHistoricalSamples.isEmpty {
      samples.append(contentsOf: pendingHistoricalSamples)
      pendingHistoricalSamples.removeAll(keepingCapacity: true)
      samples.sort { $0.capturedAt < $1.capturedAt }
      let countBeforePrune = samples.count
      prune(relativeTo: Date())
      if samples.count != countBeforePrune {
        sampleIDs = Set(samples.map(\\.id))
      }
      shouldPostUpdate = markUpdateNotificationIfNeeded()
    }
    let destination = url
    let payload = HeartRateSeriesFile(version: 1, samples: samples)
    stateLock.unlock()
    let saved = writeQueue.sync {
      do {
        try FileManager.default.createDirectory(
          at: destination.deletingLastPathComponent(),
          withIntermediateDirectories: true
        )
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        try encoder.encode(payload).write(to: destination, options: .atomic)
        return true
      } catch {
        NSLog("GooseSwift heart-rate flush failed: \\(String(describing: error))")
        return false
      }
    }
    if saved && shouldPostUpdate {
      DispatchQueue.main.async {
        NotificationCenter.default.post(name: Self.didUpdateNotification, object: self)
      }
    }
    return saved
  }

"""
if "func flush() -> Bool" not in store_source:
    first = store_source.find("  private func schedulePersist() {")
    if first < 0:
        raise SystemExit("Could not locate heart-rate store persistence method.")
    store_source = store_source[:first] + hr_flush + store_source[first:]
    second = store_source.find("  private func schedulePersist() {", first + len(hr_flush) + 1)
    if second < 0:
        raise SystemExit("Could not locate HRV store persistence method.")
    hrv_flush = """  func flush() -> Bool {
    stateLock.lock()
    pendingWrite?.cancel()
    pendingWrite = nil
    let destination = url
    let payload = HRVSeriesFile(version: 1, samples: samples)
    stateLock.unlock()
    return writeQueue.sync {
      do {
        try FileManager.default.createDirectory(
          at: destination.deletingLastPathComponent(),
          withIntermediateDirectories: true
        )
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        try encoder.encode(payload).write(to: destination, options: .atomic)
        return true
      } catch {
        NSLog("GooseSwift HRV flush failed: \\(String(describing: error))")
        return false
      }
    }
  }

"""
    store_source = store_source[:second] + hrv_flush + store_source[second:]
stores.write_text(store_source)

pipeline = one("GooseAppModel+NotificationPipeline.swift")
p = pipeline.read_text()
flush_method = """  func flushHistoricalNotificationPipeline(completion: @escaping (Bool) -> Void) {
    notificationIngestQueue.async { [weak self] in
      guard let self else {
        DispatchQueue.main.async { completion(false) }
        return
      }
      self.notificationParseQueue.async { [weak self] in
        guard let self else {
          DispatchQueue.main.async { completion(false) }
          return
        }
        // Parsed HR frames that require main-thread handling are queued from
        // notificationParseQueue. Drain those main-queue updates before the
        // durable store flush, then return to the parse queue for disk I/O.
        DispatchQueue.main.async { [weak self] in
          guard let self else {
            completion(false)
            return
          }
          self.notificationParseQueue.async { [weak self] in
            guard let self else {
              DispatchQueue.main.async { completion(false) }
              return
            }
            let heartRateSaved = HeartRateSeriesStore.shared.flush()
            let hrvSaved = HRVSeriesStore.shared.flush()
            DispatchQueue.main.async {
              completion(heartRateSaved && hrvSaved)
            }
          }
        }
      }
    }
  }

"""
if "func flushHistoricalNotificationPipeline(" not in p:
    anchor = "  func handleNotification(_ event: GooseNotificationEvent) {"
    if p.count(anchor) != 1:
        raise SystemExit("Could not locate notification pipeline insertion point.")
    p = p.replace(anchor, flush_method + anchor, 1)
pipeline.write_text(p)

app = one("GooseAppModel.swift")
a = app.read_text()
old_hook = """    ble.onHistoricalBatchCommit = { frames, ackPayload, completion in
      GooseHistoricalSyncBatchArchive.shared.append(
        frames: frames,
        ackPayload: ackPayload,
        completion: completion
      )
    }
"""
new_hook = """    ble.onHistoricalBatchCommit = { [weak self] frames, ackPayload, completion in
      GooseHistoricalSyncBatchArchive.shared.append(
        frames: frames,
        ackPayload: ackPayload
      ) { saved in
        guard saved, let self else {
          completion(false)
          return
        }
        DispatchQueue.main.async {
          self.flushHistoricalNotificationPipeline(completion: completion)
        }
      }
    }
"""
if new_hook not in a:
    if a.count(old_hook) != 1:
        raise SystemExit("Could not locate historical batch commit callback.")
    a = a.replace(old_hook, new_hook, 1)
app.write_text(a)

print("Enabled durable WHOOP history batch storage before HISTORY_END ACK.")
