#!/usr/bin/env python3
"""Add transparent 7/30-day personal HRV and RHR baselines to the HealthKit snapshot."""
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "GooseSwift"
p = root / "HealthKitSleepImporter.swift"
s = p.read_text()

def replace_once(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f"Baseline patch anchor missing: {label}")
    s = s.replace(old, new, 1)

replace_once(
'''/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
'''/// Mean of real Apple Health samples. Sample count is retained for transparency.
struct HealthKitRollingBaseline: Codable {
  let mean: Double
  let sampleCount: Int
  let windowDays: Int
}

/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
"snapshot declaration")
replace_once(
'''  let hrvSDNNMilliseconds: Double?
  let restingHeartRateBPM: Double?''',
'''  let hrvSDNNMilliseconds: Double?
  let hrv7DayBaseline: HealthKitRollingBaseline?
  let hrv30DayBaseline: HealthKitRollingBaseline?
  let restingHR7DayBaseline: HealthKitRollingBaseline?
  let restingHR30DayBaseline: HealthKitRollingBaseline?
  let restingHeartRateBPM: Double?''',
"snapshot baseline fields")
replace_once(
'''    async let hrv = latest(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli))
    async let rhr = latest(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()))''',
'''    async let hrv = latest(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli))
    async let hrv7 = rollingBaseline(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli), now, 7, 3)
    async let hrv30 = rollingBaseline(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli), now, 30, 5)
    async let rhr7 = rollingBaseline(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), now, 7, 3)
    async let rhr30 = rollingBaseline(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), now, 30, 5)
    async let rhr = latest(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()))''',
"baseline queries")
replace_once(
'''      capturedAt: now, heartRateBPM: hr, hrvSDNNMilliseconds: hrv,
      restingHeartRateBPM: rhr,''',
'''      capturedAt: now, heartRateBPM: hr, hrvSDNNMilliseconds: hrv,
      hrv7DayBaseline: hrv7, hrv30DayBaseline: hrv30,
      restingHR7DayBaseline: rhr7, restingHR30DayBaseline: rhr30,
      restingHeartRateBPM: rhr,''',
"snapshot initialization")
replace_once(
'''  private static func latest(_ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit) async -> Double? {''',
'''  private static func rollingBaseline(
    _ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit,
    _ now: Date, _ days: Int, _ minimumSamples: Int
  ) async -> HealthKitRollingBaseline? {
    guard let type = HKObjectType.quantityType(forIdentifier: id) else { return nil }
    let start = Calendar.current.date(byAdding: .day, value: -days, to: now) ?? now.addingTimeInterval(-Double(days) * 86400)
    let predicate = HKQuery.predicateForSamples(withStart: start, end: now, options: [])
    return await withCheckedContinuation { continuation in
      let query = HKSampleQuery(
        sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit,
        sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)]
      ) { _, samples, _ in
        let values = (samples as? [HKQuantitySample] ?? []).map {
          $0.quantity.doubleValue(for: unit)
        }.filter { $0.isFinite && $0 > 0 }
        guard values.count >= minimumSamples else {
          continuation.resume(returning: nil)
          return
        }
        let mean = values.reduce(0, +) / Double(values.count)
        continuation.resume(returning: HealthKitRollingBaseline(mean: mean, sampleCount: values.count, windowDays: days))
      }
      store.execute(query)
    }
  }

  private static func latest(_ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit) async -> Double? {''',
"baseline query helper")
p.write_text(s)

p = root / "HealthView.swift"
v = p.read_text()
old = '''        metric("HRV (SDNN)", number(store.healthKitTrainingSnapshot?.hrvSDNNMilliseconds, " ms", 0), "waveform.path.ecg")
        metric("Resting HR", number(store.healthKitTrainingSnapshot?.restingHeartRateBPM, " bpm", 0), "heart")'''
new = '''        metric("HRV (SDNN)", number(store.healthKitTrainingSnapshot?.hrvSDNNMilliseconds, " ms", 0), "waveform.path.ecg")
        metric("HRV 7-day avg", baseline(store.healthKitTrainingSnapshot?.hrv7DayBaseline, " ms"), "chart.line.uptrend.xyaxis")
        metric("HRV 30-day avg", baseline(store.healthKitTrainingSnapshot?.hrv30DayBaseline, " ms"), "chart.xyaxis.line")
        metric("Resting HR", number(store.healthKitTrainingSnapshot?.restingHeartRateBPM, " bpm", 0), "heart")
        metric("RHR 7-day avg", baseline(store.healthKitTrainingSnapshot?.restingHR7DayBaseline, " bpm"), "chart.line.downtrend.xyaxis")
        metric("RHR 30-day avg", baseline(store.healthKitTrainingSnapshot?.restingHR30DayBaseline, " bpm"), "chart.xyaxis.line")'''
if old not in v: raise SystemExit("HealthView baseline insertion anchor missing")
v = v.replace(old, new, 1)
anchor = '''  private func count(_ value: Double?) -> String {'''
helper = '''  private func baseline(_ value: HealthKitRollingBaseline?, _ suffix: String) -> String {
    guard let value else { return "Building baseline" }
    return value.mean.formatted(.number.precision(.fractionLength(0))) + suffix + " · n=" + String(value.sampleCount)
  }
  private func count(_ value: Double?) -> String {'''
if anchor not in v: raise SystemExit("HealthView baseline helper anchor missing")
v = v.replace(anchor, helper, 1)
p.write_text(v)
print("Added 7/30-day HRV and resting-HR baselines with sample-count transparency.")
