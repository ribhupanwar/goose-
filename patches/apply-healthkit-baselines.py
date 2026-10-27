#!/usr/bin/env python3
"""Add robust personal HRV/RHR baselines (median + scaled MAD) to Goose HealthKit."""
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "GooseSwift"
p = root / "HealthKitSleepImporter.swift"
s = p.read_text()

def replace_once(old, new, label):
    global s
    if old not in s:
        if label == "median/MAD baseline calculation":
            lines = s.splitlines()
            hits = [i for i, line in enumerate(lines) if "rollingBaseline" in line or "mean" in line.lower() or "HealthKitRollingBaseline" in line]
            print("Baseline source diagnostics (relevant lines):")
            shown = set()
            for i in hits:
                for j in range(max(0, i - 4), min(len(lines), i + 7)):
                    if j not in shown:
                        print(str(j + 1) + ": " + lines[j])
                        shown.add(j)
            raise SystemExit("Baseline patch anchor missing: " + label + "; inspect diagnostics above")
        raise SystemExit(f"Baseline patch anchor missing: {label}")
    s = s.replace(old, new, 1)

replace_once(
'''/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
'''/// Robust personal baseline. MAD is stored raw; robustScale applies 1.4826.
struct HealthKitRollingBaseline: Codable {
  let median: Double
  let mad: Double
  let sampleCount: Int
  let windowDays: Int
  var robustScale: Double { mad * 1.4826 }
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
    async let hrv30 = rollingBaseline(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli), now, 30, 14)
    async let rhr7 = rollingBaseline(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), now, 7, 3)
    async let rhr30 = rollingBaseline(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), now, 30, 14)
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
'''  private static func median(_ input: [Double]) -> Double? {
    guard !input.isEmpty else { return nil }
    let values = input.sorted()
    let middle = values.count / 2
    if values.count.isMultiple(of: 2) {
      return (values[middle - 1] + values[middle]) / 2
    }
    return values[middle]
  }

  private static func rollingBaseline(
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
        guard let center = median(values),
              let rawMAD = median(values.map { abs($0 - center) }) else {
          continuation.resume(returning: nil)
          return
        }
        continuation.resume(returning: HealthKitRollingBaseline(
          median: center, mad: rawMAD, sampleCount: values.count, windowDays: days
        ))
      }
      store.execute(query)
    }
  }

  private static func latest(_ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit) async -> Double? {''',
"baseline query helper")
# rollingBaseline() above already computes median + MAD.
# The old mean-based replacement below was redundant and failed after the
# helper was introduced earlier in this same script.
p.write_text(s)

p = root / "HealthView.swift"
v = p.read_text()
old = '''        metric("HRV (SDNN)", number(store.healthKitTrainingSnapshot?.hrvSDNNMilliseconds, " ms", 0), "waveform.path.ecg")
        metric("HRV 7-day avg", baseline(store.healthKitTrainingSnapshot?.hrv7DayBaseline, " ms"), "chart.line.uptrend.xyaxis")
        metric("HRV 30-day avg", baseline(store.healthKitTrainingSnapshot?.hrv30DayBaseline, " ms"), "chart.xyaxis.line")
        metric("Resting HR", number(store.healthKitTrainingSnapshot?.restingHeartRateBPM, " bpm", 0), "heart")
        metric("RHR 7-day avg", baseline(store.healthKitTrainingSnapshot?.restingHR7DayBaseline, " bpm"), "chart.line.downtrend.xyaxis")
        metric("RHR 30-day avg", baseline(store.healthKitTrainingSnapshot?.restingHR30DayBaseline, " bpm"), "chart.xyaxis.line")'''
new = '''        metric("HRV (SDNN)", number(store.healthKitTrainingSnapshot?.hrvSDNNMilliseconds, " ms", 0), "waveform.path.ecg")
        metric("HRV 7-day median", baseline(store.healthKitTrainingSnapshot?.hrv7DayBaseline, " ms"), "chart.line.uptrend.xyaxis")
        metric("HRV 30-day median", baseline(store.healthKitTrainingSnapshot?.hrv30DayBaseline, " ms"), "chart.xyaxis.line")
        metric("Resting HR", number(store.healthKitTrainingSnapshot?.restingHeartRateBPM, " bpm", 0), "heart")
        metric("RHR 7-day median", baseline(store.healthKitTrainingSnapshot?.restingHR7DayBaseline, " bpm"), "chart.line.downtrend.xyaxis")
        metric("RHR 30-day median", baseline(store.healthKitTrainingSnapshot?.restingHR30DayBaseline, " bpm"), "chart.xyaxis.line")'''
if old not in v: raise SystemExit("HealthView baseline display anchor missing")
v = v.replace(old, new, 1)
old_helper = '''  private func baseline(_ value: HealthKitRollingBaseline?, _ suffix: String) -> String {
    guard let value else { return "Building baseline" }
    return value.mean.formatted(.number.precision(.fractionLength(0))) + suffix + " · n=" + String(value.sampleCount)
  }'''
new_helper = '''  private func baseline(_ value: HealthKitRollingBaseline?, _ suffix: String) -> String {
    guard let value else { return "Building baseline" }
    let center = value.median.formatted(.number.precision(.fractionLength(0)))
    let spread = value.robustScale.formatted(.number.precision(.fractionLength(1)))
    return center + suffix + " · MAD σ≈" + spread + " · n=" + String(value.sampleCount)
  }'''
if old_helper not in v: raise SystemExit("HealthView baseline formatter anchor missing")
v = v.replace(old_helper, new_helper, 1)
p.write_text(v)
print("Added median + MAD personal baselines with minimum sample gates and transparent display.")
