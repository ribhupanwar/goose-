#!/usr/bin/env python3
"""Add OpenStrap-inspired, independently implemented robust readiness to Goose."""
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
'''  /// Only samples from the last 36 hours can represent current readiness.
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
'''/// A transparent readiness estimate; it is not a WHOOP proprietary score.
struct HealthKitRecoveryAssessment {
  let score: Int
  let hrvContribution: Int
  let restingHRContribution: Int
  let status: String
  let confidence: String
  let calibrationStatus: String
  let calibrationNights: Int
  let compositeZ: Double
}

/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
"recovery assessment type")
replace_once(
'''  let source: String

  static let didUpdate''',
'''  let source: String

  /// Robust-z readiness. HRV and RHR form one autonomic group, so both are
  /// required here; a missing signal never gets zero-imputed or promoted alone.
  var recoveryAssessment: HealthKitRecoveryAssessment? {
    guard let hrv = hrvSDNNMilliseconds, hrv > 0,
          let rhr = restingHeartRateBPM, rhr > 0,
          let hrvBase = hrv30DayBaseline,
          let rhrBase = restingHR30DayBaseline,
          hrvBase.sampleCount >= 14, rhrBase.sampleCount >= 14 else { return nil }

    let hrvScale = hrvBase.robustScale
    let rhrScale = rhrBase.robustScale
    // A zero MAD means there is no defensible personal dispersion estimate.
    // Do not fall back to a tiny SD or manufacture a large readiness score.
    guard hrvScale.isFinite, rhrScale.isFinite, hrvScale > 0, rhrScale > 0 else { return nil }

    let hrvZ = (hrv - hrvBase.median) / hrvScale
    let rhrZ = (rhrBase.median - rhr) / rhrScale
    guard hrvZ.isFinite, rhrZ.isFinite else { return nil }

    // OpenStrap treats HRV+RHR as one autonomic input (mean of oriented z's).
    let compositeZ = (hrvZ + rhrZ) / 2
    let calibration = HealthKitReadinessCalibration.score(compositeZ, at: capturedAt)
    let score = Int(calibration.score.rounded()).clamped(to: 0...100)
    let status = score >= 67 ? "Ready" : (score >= 34 ? "Moderate" : "Recover")
    let confidence = hrvBase.sampleCount >= 20 && rhrBase.sampleCount >= 20 ? "Strong" : "Building"
    return HealthKitRecoveryAssessment(
      score: score,
      hrvContribution: Int((50 + 25 * hrvZ).clamped(to: 0...100).rounded()),
      restingHRContribution: Int((50 + 25 * rhrZ).clamped(to: 0...100).rounded()),
      status: status,
      confidence: confidence,
      calibrationStatus: calibration.status,
      calibrationNights: calibration.nights,
      compositeZ: compositeZ
    )
  }

  static let didUpdate''',
"robust readiness calculation")
p.write_text(s)

p = root / "HealthView.swift"
v = p.read_text()
start = '''      if let assessment = store.healthKitTrainingSnapshot?.recoveryAssessment {'''
end = '''      LazyVGrid(columns: columns, spacing: 10) {'''
if start not in v or end not in v:
    raise SystemExit("Recovery UI block anchors missing")
a = v.index(start)
b = v.index(end, a)
recovery_ui = '''      if let assessment = store.healthKitTrainingSnapshot?.recoveryAssessment {
        VStack(alignment: .leading, spacing: 8) {
          HStack(alignment: .firstTextBaseline) {
            Text("RECOVERY").font(.caption.weight(.bold)).tracking(1.1).foregroundStyle(.secondary)
            Spacer()
            Text(assessment.status.uppercased()).font(.caption.weight(.bold).tracking(0.8))
          }
          HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("\\(assessment.score)").font(.system(size: 42, weight: .bold, design: .rounded)).monospacedDigit()
            Text("/ 100").font(.subheadline).foregroundStyle(.secondary)
            Spacer()
            Text(assessment.confidence).font(.caption).foregroundStyle(.secondary)
          }
          HStack(spacing: 12) {
            Label("HRV z \\(assessment.hrvContribution)", systemImage: "waveform.path.ecg")
            Label("RHR z \\(assessment.restingHRContribution)", systemImage: "heart")
          }.font(.caption).foregroundStyle(.secondary)
          Text("Median + MAD personal baseline · HRV/RHR autonomic composite")
            .font(.caption2).foregroundStyle(.secondary)
          Text(assessment.calibrationStatus + " · " + String(assessment.calibrationNights) + "/14 prior days")
            .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      } else {
        VStack(alignment: .leading, spacing: 4) {
          Text("Recovery score is unavailable").font(.subheadline.weight(.semibold))
          Text("Needs fresh HRV and resting-HR readings from the last 36 hours, at least 14 personal baseline samples for each, and non-zero MAD dispersion. Missing or unreliable data is not estimated.")
            .font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      }
'''
v = v.slice(0, a) + recovery_ui + v.slice(b)
p.write_text(v)
print("Added fresh-sample robust-z readiness, missing-data abstention, and calibrated score history.")
