#!/usr/bin/env python3
"""Add a transparent 7-night sleep-need and sleep-debt summary from Apple Health."""
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "GooseSwift"
p = root / "HealthKitSleepImporter.swift"
s = p.read_text()

def replace_once(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f"Sleep need/debt patch anchor missing: {label}")
    s = s.replace(old, new, 1)

replace_once(
'''/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
'''/// One completed sleep period, assigned to its wake-up calendar day.
struct HealthKitSleepNight: Codable, Identifiable {
  let date: Date
  let durationMinutes: Double
  var id: Date { date }
}

/// A transparent 7-night summary. Missing nights are excluded, not treated as zero sleep.
struct HealthKitSleepSummary {
  let nightlyNeedHours: Double
  let debtHours: Double
  let observedNights: Int
  let averageSleepHours: Double
  let lastNightHours: Double?
}

/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
"sleep model declarations")

replace_once(
'''  let sleepMinutes: Double?
  let source: String''',
'''  let sleepMinutes: Double?
  let sleepHistory: [HealthKitSleepNight]?
  let source: String''',
"snapshot sleep history field")

replace_once(
'''  static let didUpdate = Notification.Name("GooseHealthKitTrainingSnapshotDidUpdate")
  var availableSignalCount''',
'''  static let didUpdate = Notification.Name("GooseHealthKitTrainingSnapshotDidUpdate")
  /// Initial target is a transparent 8-hour nightly baseline; later versions can personalize it.
  static let nightlySleepNeedHours = 8.0

  var sleepSummary: HealthKitSleepSummary? {
    guard let sleepHistory, !sleepHistory.isEmpty else { return nil }
    let calendar = Calendar.current
    let today = calendar.startOfDay(for: capturedAt)
    let firstDay = calendar.date(byAdding: .day, value: -6, to: today) ?? today
    let recent = sleepHistory.filter { $0.date >= firstDay && $0.date <= capturedAt }
      .sorted { $0.date < $1.date }
    guard !recent.isEmpty else { return nil }
    let durations = recent.map { max(0, $0.durationMinutes / 60) }
    let debt = durations.reduce(0.0) { $0 + max(0, Self.nightlySleepNeedHours - $1) }
    return HealthKitSleepSummary(
      nightlyNeedHours: Self.nightlySleepNeedHours,
      debtHours: debt,
      observedNights: recent.count,
      averageSleepHours: durations.reduce(0, +) / Double(durations.count),
      lastNightHours: durations.last
    )
  }

  var availableSignalCount''',
"sleep summary calculation")

replace_once(
'''    async let sleep = sleepMinutes(store, now)
    let snapshot = await HealthKitTrainingSnapshot(''',
'''    async let sleep = sleepMinutes(store, now)
    async let nights = sleepHistory(store, now)
    let snapshot = await HealthKitTrainingSnapshot(''',
"sleep history query")

if "sleepHistory: nights" not in s:
    marker = "sleepMinutes: sleep"
    if marker not in s:
        start = s.find("let snapshot = await HealthKitTrainingSnapshot(")
        raise SystemExit("Sleep history initializer anchor missing; nearby source: " + s[start:start + 700])
    s = s.replace(marker, marker + ", sleepHistory: nights", 1)

replace_once(
'''  private static func sleepMinutes(_ store: HKHealthStore, _ now: Date) async -> Double? {''',
'''  /// Reads up to 10 calendar days so the latest 7 wake-up days can be summarized.
  /// Sleep-stage intervals are unioned to avoid double-counting overlapping Apple Health sources.
  private static func sleepHistory(_ store: HKHealthStore, _ now: Date) async -> [HealthKitSleepNight]? {
    guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { return nil }
    let calendar = Calendar.current
    let today = calendar.startOfDay(for: now)
    let start = calendar.date(byAdding: .day, value: -9, to: today) ?? now.addingTimeInterval(-10 * 86400)
    let predicate = HKQuery.predicateForSamples(withStart: start, end: now, options: [])
    return await withCheckedContinuation { continuation in
      let query = HKSampleQuery(
        sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit,
        sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: true)]
      ) { _, samples, _ in
        let asleep = [
          HKCategoryValueSleepAnalysis.asleep.rawValue,
          HKCategoryValueSleepAnalysis.asleepCore.rawValue,
          HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
          HKCategoryValueSleepAnalysis.asleepREM.rawValue,
          HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue
        ]
        let intervals = (samples as? [HKCategorySample] ?? [])
          .filter { asleep.contains($0.value) }
          .map { (max($0.startDate, start), min($0.endDate, now)) }
          .filter { $0.1 > $0.0 }
          .sorted { $0.0 < $1.0 }
        var merged: [(Date, Date)] = []
        for interval in intervals {
          if let last = merged.last, interval.0 <= last.1 {
            merged[merged.count - 1] = (last.0, max(last.1, interval.1))
          } else {
            merged.append(interval)
          }
        }
        var minutesByWakeDay: [Date: Double] = [:]
        for interval in merged {
          let wakeDay = calendar.startOfDay(for: interval.1)
          minutesByWakeDay[wakeDay, default: 0] += interval.1.timeIntervalSince(interval.0) / 60
        }
        let nights = minutesByWakeDay
          .filter { $0.value > 0 }
          .map { HealthKitSleepNight(date: $0.key, durationMinutes: $0.value) }
          .sorted { $0.date < $1.date }
        continuation.resume(returning: nights.isEmpty ? nil : nights)
      }
      store.execute(query)
    }
  }

  private static func sleepMinutes(_ store: HKHealthStore, _ now: Date) async -> Double? {''',
"sleep history query helper")

p.write_text(s)

p = root / "HealthView.swift"
v = p.read_text()
anchor = '''      LazyVGrid(columns: columns, spacing: 10) {'''
sleep_ui = '''      if let sleep = store.healthKitTrainingSnapshot?.sleepSummary {
        VStack(alignment: .leading, spacing: 8) {
          HStack {
            Text("SLEEP NEED & DEBT").font(.caption.weight(.bold)).tracking(1.0).foregroundStyle(.secondary)
            Spacer()
            Text("\\(sleep.observedNights)/7 nights").font(.caption).foregroundStyle(.secondary)
          }
          HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(String(format: "%.1f h", sleep.nightlyNeedHours))
              .font(.title2.weight(.bold)).monospacedDigit()
            Text("nightly need").font(.caption).foregroundStyle(.secondary)
            Spacer()
            Text(String(format: "%.1f h debt", sleep.debtHours))
              .font(.title3.weight(.bold)).monospacedDigit()
          }
          HStack {
            Text(String(format: "7-night average: %.1f h", sleep.averageSleepHours))
            Spacer()
            if let last = sleep.lastNightHours {
              Text(String(format: "Last night: %.1f h", last))
            }
          }.font(.caption).foregroundStyle(.secondary)
          Text("Debt adds each tracked night's shortfall below the 8-hour target. Nights without Health samples are excluded.")
            .font(.caption2).foregroundStyle(.tertiary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      } else {
        VStack(alignment: .leading, spacing: 4) {
          Text("Sleep history is building").font(.subheadline.weight(.semibold))
          Text("Sync Apple Health sleep data. Sleep debt will use up to 7 tracked nights; missing nights won't be counted as zero.")
            .font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      }
      LazyVGrid(columns: columns, spacing: 10) {'''
if anchor not in v:
    raise SystemExit("Sleep UI insertion anchor missing")
v = v.replace(anchor, sleep_ui, 1)
p.write_text(v)
print("Added 7-night sleep need/debt summary with overlap-safe HealthKit sleep aggregation.")
