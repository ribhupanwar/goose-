#!/usr/bin/env python3
"""Add a transparent HRV/RHR-based recovery assessment to Goose."""
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "GooseSwift"
p = root / "HealthKitSleepImporter.swift"
s = p.read_text()

def replace_once(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f"Recovery patch anchor missing: {label}")
    s = s.replace(old, new, 1)

replace_once(
'''    async let hrv = latest(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli))''',
'''    async let hrv = latestRecent(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli), now, 36)''',
"recent HRV measurement")
replace_once(
'''    async let rhr = latest(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()))''',
'''    async let rhr = latestRecent(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), now, 36)''',
"recent resting heart-rate measurement")
replace_once(
'''  private static func rollingBaseline(
''',
'''  /// Recovery uses fresh readings only; old Health samples must not masquerade as today's state.
  private static func latestRecent(
    _ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit,
    _ now: Date, _ maxAgeHours: Double
  ) async -> Double? {
    guard let type = HKObjectType.quantityType(forIdentifier: id) else { return nil }
    let start = now.addingTimeInterval(-maxAgeHours * 3600)
    let predicate = HKQuery.predicateForSamples(withStart: start, end: now, options: [])
    return await withCheckedContinuation { continuation in
      let query = HKSampleQuery(
        sampleType: type, predicate: predicate, limit: 1,
        sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)]
      ) { _, samples, _ in
        let value = (samples?.first as? HKQuantitySample)?.quantity.doubleValue(for: unit)
        continuation.resume(returning: value.flatMap { $0.isFinite && $0 > 0 ? $0 : nil })
      }
      store.execute(query)
    }
  }

  private static func rollingBaseline(
''',
"recent-sample query helper")
replace_once(
'''/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
'''/// Transparent recovery estimate derived from fresh HRV/RHR and personal 30-day means.
struct HealthKitRecoveryAssessment {
  let score: Int
  let hrvContribution: Int
  let restingHRContribution: Int
  let status: String
  let confidence: String
}

/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
"recovery assessment type")
replace_once(
'''  let source: String

  static let didUpdate''',
'''  let source: String

  /// Requires both 30-day baselines and readings from the last 36 hours.
  /// HRV contributes 60%; resting HR contributes 40%. Components are bounded 0–100.
  var recoveryAssessment: HealthKitRecoveryAssessment? {
    guard let hrv = hrvSDNNMilliseconds, hrv > 0,
          let rhr = restingHeartRateBPM, rhr > 0,
          let hrvBase = hrv30DayBaseline, hrvBase.mean > 0,
          let rhrBase = restingHR30DayBaseline, rhrBase.mean > 0 else { return nil }
    let hrvScore = min(100, max(0, 50 + ((hrv - hrvBase.mean) / hrvBase.mean) * 200))
    let rhrScore = min(100, max(0, 50 + ((rhrBase.mean - rhr) / rhrBase.mean) * 300))
    let total = Int((hrvScore * 0.6 + rhrScore * 0.4).rounded())
    let status = total >= 67 ? "Ready" : (total >= 34 ? "Moderate" : "Recover")
    let samples = min(hrvBase.sampleCount, rhrBase.sampleCount)
    let confidence = samples >= 20 ? "Strong" : (samples >= 10 ? "Good" : "Building")
    return HealthKitRecoveryAssessment(
      score: total, hrvContribution: Int(hrvScore.rounded()),
      restingHRContribution: Int(rhrScore.rounded()), status: status,
      confidence: confidence
    )
  }

  static let didUpdate''',
"recovery calculation")
p.write_text(s)

p = root / "HealthView.swift"
v = p.read_text()
anchor = '''      LazyVGrid(columns: columns, spacing: 10) {'''
recovery = '''      if let assessment = store.healthKitTrainingSnapshot?.recoveryAssessment {
        VStack(alignment: .leading, spacing: 8) {
          HStack(alignment: .firstTextBaseline) {
            Text("RECOVERY").font(.caption.weight(.bold)).tracking(1.1).foregroundStyle(.secondary)
            Spacer()
            Text(assessment.status.uppercased()).font(.caption.weight(.bold)).tracking(0.8)
          }
          HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("\(assessment.score)").font(.system(size: 42, weight: .bold, design: .rounded)).monospacedDigit()
            Text("/ 100").font(.subheadline).foregroundStyle(.secondary)
            Spacer()
            Text(assessment.confidence + " confidence").font(.caption).foregroundStyle(.secondary)
          }
          HStack(spacing: 12) {
            Label("HRV \(assessment.hrvContribution)", systemImage: "waveform.path.ecg")
            Label("RHR \(assessment.restingHRContribution)", systemImage: "heart")
          }.font(.caption).foregroundStyle(.secondary)
          Text("HRV weight 60% · RHR weight 40% · compared with your 30-day averages")
            .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      } else {
        VStack(alignment: .leading, spacing: 4) {
          Text("Recovery score is building").font(.subheadline.weight(.semibold))
          Text("Needs fresh HRV and resting-HR readings from the last 36 hours plus personal 30-day baselines. Sync Apple Health after wearing your watch.")
            .font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      }
      LazyVGrid(columns: columns, spacing: 10) {'''
if anchor not in v: raise SystemExit("Recovery UI insertion anchor missing")
v = v.replace(anchor, recovery, 1)
p.write_text(v)
print("Added fresh-sample HRV/RHR recovery assessment and Health UI.")
