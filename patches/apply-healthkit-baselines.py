from pathlib import Path
import sys

swift = Path(sys.argv[1]) / "GooseSwift"
p = swift / "HealthKitSleepImporter.swift"
s = p.read_text()

def replace_once(old, new, label):
    global s
    if old not in s:
        raise SystemExit("Missing anchor: " + label)
    s = s.replace(old, new, 1)

replace_once(
"  let sleepMinutes: Double?\n  let source: String",
"  let sleepMinutes: Double?\n  let hrv7DayAverageMilliseconds: Double?\n  let hrv30DayAverageMilliseconds: Double?\n  let restingHR7DayAverageBPM: Double?\n  let restingHR30DayAverageBPM: Double?\n  let source: String",
"snapshot baseline properties")
replace_once(
"    async let sleep = sleepMinutes(store, now)\n    let snapshot = await HealthKitTrainingSnapshot(",
"    async let sleep = sleepMinutes(store, now)\n    async let hrv7 = average(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli), days: 7, now: now)\n    async let hrv30 = average(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli), days: 30, now: now)\n    async let rhr7 = average(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), days: 7, now: now)\n    async let rhr30 = average(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()), days: 30, now: now)\n    let snapshot = await HealthKitTrainingSnapshot(",
"baseline queries")
replace_once(
"      sleepMinutes: sleep, source: \"Apple Health\"",
"      sleepMinutes: sleep, hrv7DayAverageMilliseconds: hrv7, hrv30DayAverageMilliseconds: hrv30,\n      restingHR7DayAverageBPM: rhr7, restingHR30DayAverageBPM: rhr30, source: \"Apple Health\"",
"baseline snapshot initializer")
anchor = "  private static func sleepMinutes(_ store: HKHealthStore, _ now: Date) async -> Double? {"
helper = """  private static func average(_ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit, days: Int, now: Date) async -> Double? {
    guard let type = HKObjectType.quantityType(forIdentifier: id),
          let start = Calendar.current.date(byAdding: .day, value: -days, to: now) else { return nil }
    let predicate = HKQuery.predicateForSamples(withStart: start, end: now, options: .strictStartDate)
    return await withCheckedContinuation { c in
      let q = HKStatisticsQuery(quantityType: type, quantitySamplePredicate: predicate, options: .discreteAverage) { _, stats, _ in
        let value = stats?.averageQuantity()?.doubleValue(for: unit)
        c.resume(returning: value.flatMap { $0.isFinite && $0 > 0 ? $0 : nil })
      }
      store.execute(q)
    }
  }
"""
replace_once(anchor, helper + anchor, "baseline average query")
p.write_text(s)

p = swift / "HealthView.swift"
s = p.read_text()
anchor = '        metric("Sleep (36h window)", number(store.healthKitTrainingSnapshot?.sleepMinutes.map { $0 / 60 }, " h", 1), "moon.zzz.fill")'
addition = anchor + '''
        metric("HRV 7-day avg", number(store.healthKitTrainingSnapshot?.hrv7DayAverageMilliseconds, " ms", 0), "chart.line.uptrend.xyaxis")
        metric("HRV 30-day avg", number(store.healthKitTrainingSnapshot?.hrv30DayAverageMilliseconds, " ms", 0), "chart.xyaxis.line")
        metric("RHR 7-day avg", number(store.healthKitTrainingSnapshot?.restingHR7DayAverageBPM, " bpm", 0), "heart.text.square")
        metric("RHR 30-day avg", number(store.healthKitTrainingSnapshot?.restingHR30DayAverageBPM, " bpm", 0), "heart.circle")'''
replace_once(anchor, addition, "baseline dashboard tiles")
p.write_text(s)
print("Added 7/30-day HRV and resting-heart-rate sample averages.")
