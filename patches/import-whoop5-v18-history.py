#!/usr/bin/env python3
"""Import verified WHOOP 5 v18 historical HR samples into Goose's HR series."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def one(name: str) -> Path:
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one {name}, found {len(matches)}")
    return matches[0]

# WHOOP 5 v18 is the ordinary per-second historical record. Its inner record
# starts at frame payload byte 0; the notification parser exposes body bytes
# beginning at inner[13], so HR is body[4], RR count body[5], RR slots body[6..].
samples = one("WhoopEventSamples.swift")
s = samples.read_text()
anchor = """  var isPulseInformationPacket: Bool {
"""
computed = """  /// Verified WHOOP 5 v18 per-second historical HR (not a live estimate).
  /// Only packet type 47 + record revision 18 is accepted; other K/revision
  /// layouts are intentionally left untouched.
  var historicalHeartRateBPM: Int? {
    guard packetType == 47, packetK == 18,
          let body = Data(hexString: bodyHex), body.count >= 6 else {
      return nil
    }
    let bpm = Int(body[4])
    return (25...230).contains(bpm) ? bpm : nil
  }

  /// RR values carried by this one-second record. They are exposed for
  /// diagnostics only; do not compute RMSSD across non-adjacent records.
  var historicalRRIntervalsMS: [Int] {
    guard packetType == 47, packetK == 18,
          let body = Data(hexString: bodyHex), body.count >= 6 else {
      return []
    }
    let count = Int(body[5])
    guard (1...4).contains(count), body.count >= 6 + 2 * count else {
      return []
    }
    return (0..<count).compactMap { index in
      let offset = 6 + 2 * index
      let value = Int(body[offset]) | (Int(body[offset + 1]) << 8)
      return (200...2500).contains(value) ? value : nil
    }
  }

  /// v18 timestamps use 1/32768-second subsecond units.
  var historicalSampleDate: Date? {
    guard packetType == 47, packetK == 18,
          let seconds = deviceTimestampSeconds, seconds > 0 else {
      return nil
    }
    return Date(
      timeIntervalSince1970:
        Double(seconds) + Double(deviceTimestampSubseconds ?? 0) / 32768.0
    )
  }

"""
if computed not in s:
    if s.count(anchor) != 1:
        raise SystemExit("Could not locate WhoopDataSignalSample computed-property insertion point.")
    s = s.replace(anchor, computed + anchor, 1)
samples.write_text(s)

pipeline = one("WhoopDataSignalPipeline.swift")
p = pipeline.read_text()
p = p.replace(
    "  var onStatus: ((String) -> Void)?\n",
    "  var onStatus: ((String) -> Void)?\n  var onHistoricalHeartRateSample: ((Int, Date) -> Void)?\n",
    1,
)
p = p.replace(
    "  private var lastLoggedAt: [String: Date] = [:]\n",
    "  private var lastLoggedAt: [String: Date] = [:]\n"
    "  private var importedHistoricalRecordKeys: Set<String> = []\n"
    "  private var importedHistoricalRecordOrder: [String] = []\n",
    1,
)
anchor = """  private func process(_ sample: WhoopDataSignalSample) {
"""
insert = """  private func process(_ sample: WhoopDataSignalSample) {
    if let bpm = sample.historicalHeartRateBPM,
       let sampleDate = sample.historicalSampleDate,
       let counter = sample.counterOrPage {
      let key = "\\(sample.packetK):\\(counter):\\(Int((sampleDate.timeIntervalSince1970 * 32768).rounded()))"
      if importedHistoricalRecordKeys.insert(key).inserted {
        importedHistoricalRecordOrder.append(key)
        if importedHistoricalRecordOrder.count > 100_000 {
          let expired = importedHistoricalRecordOrder.prefix(5_000)
          for oldKey in expired {
            importedHistoricalRecordKeys.remove(oldKey)
          }
          importedHistoricalRecordOrder.removeFirst(5_000)
        }
        onHistoricalHeartRateSample?(bpm, sampleDate)
      }
    }
"""
if insert not in p:
    if p.count(anchor) != 1:
        raise SystemExit("Could not locate WhoopDataSignalPipeline.process.")
    p = p.replace(anchor, insert, 1)
pipeline.write_text(p)

app = one("GooseAppModel.swift")
a = app.read_text()
anchor = """    let heartRateSamplePipeline = self.heartRateSamplePipeline
"""
replacement = anchor + """    whoopDataSignalPipeline.onHistoricalHeartRateSample = { [weak heartRateSamplePipeline] bpm, sampleDate in
      heartRateSamplePipeline?.recordHeartRateSample(
        bpm: bpm,
        source: "whoop.gen5.history.v18",
        capturedAt: sampleDate
      )
    }
"""
if replacement not in a:
    if a.count(anchor) != 1:
        raise SystemExit("Could not locate heart-rate pipeline setup.")
    a = a.replace(anchor, replacement, 1)
app.write_text(a)

print("Imported validated WHOOP 5 v18 historical HR into the persisted HR timeline.")
