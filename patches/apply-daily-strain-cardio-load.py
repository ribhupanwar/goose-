#!/usr/bin/env python3
"""Add a transparent workout-based daily strain and cardio-load estimate."""
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "GooseSwift"
p = root / "HealthKitSleepImporter.swift"
s = p.read_text()

def replace_once(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f"Cardio-load patch anchor missing: {label}")
    s = s.replace(old, new, 1)

replace_once(
'''/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
'''/// One Apple Health workout with its duration and available heart-rate statistics.
struct HealthKitWorkoutLoad: Codable, Identifiable {
  let date: Date
  let activity: String
  let durationMinutes: Double
  let averageHeartRateBPM: Double?
  let peakHeartRateBPM: Double?
  let cardioLoadPoints: Double?
  var id: Date { date }
}

/// Rolling cardio-load values. Points are relative estimates, not WHOOP strain.
struct HealthKitCardioLoadSummary {
  let sevenDayPoints: Double?
  let todayPoints: Double?
  let sevenDayMinutes: Double
  let workoutCount: Int
  let recentWorkouts: [HealthKitWorkoutLoad]
}

/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {''',
"workout model declarations")

replace_once(
'''  let sleepHistory: [HealthKitSleepNight]?
  let source: String''',
'''  let sleepHistory: [HealthKitSleepNight]?
  let workoutLoadHistory: [HealthKitWorkoutLoad]?
  let source: String''',
"snapshot workout history field")

replace_once(
'''  var availableSignalCount: Int {''',
'''  var cardioLoadSummary: HealthKitCardioLoadSummary? {
    guard let workoutLoadHistory else { return nil }
    let weekStart = capturedAt.addingTimeInterval(-7 * 86400)
    let recent = workoutLoadHistory.filter { $0.date >= weekStart && $0.date <= capturedAt }
      .sorted { $0.date > $1.date }
    let measuredLoads = recent.compactMap(\\.cardioLoadPoints)
    let todayStart = Calendar.current.startOfDay(for: capturedAt)
    let todayLoads = recent.filter { $0.date >= todayStart }.compactMap(\\.cardioLoadPoints)
    return HealthKitCardioLoadSummary(
      sevenDayPoints: measuredLoads.isEmpty ? nil : measuredLoads.reduce(0, +),
      todayPoints: todayLoads.isEmpty ? nil : todayLoads.reduce(0, +),
      sevenDayMinutes: recent.reduce(0) { $0 + $1.durationMinutes },
      workoutCount: recent.count,
      recentWorkouts: Array(recent.prefix(5))
    )
  }

  var availableSignalCount: Int {''',
"cardio load summary")

replace_once(
'''    async let nights = sleepHistory(store, now)
    let snapshot = await HealthKitTrainingSnapshot(''',
'''    async let nights = sleepHistory(store, now)
    async let workouts = workoutLoadHistory(store, now)
    let snapshot = await HealthKitTrainingSnapshot(''',
"workout history query")

replace_once(
'''sleepMinutes: sleep, sleepHistory: nights, source: "Apple Health"''',
'''sleepMinutes: sleep, sleepHistory: nights, workoutLoadHistory: workouts, source: "Apple Health"''',
"workout history initializer")

replace_once(
'''  private static func sleepHistory(_ store: HKHealthStore, _ now: Date) async -> [HealthKitSleepNight]? {''',
'''  /// Loads 14 days of workouts. Relative cardio-load points are duration ×
  /// (average HR − resting HR) / resting HR; no age-based max-HR guess is used.
  private static func workoutLoadHistory(_ store: HKHealthStore, _ now: Date) async -> [HealthKitWorkoutLoad]? {
    let start = now.addingTimeInterval(-14 * 86400)
    let predicate = HKQuery.predicateForSamples(withStart: start, end: now, options: [])
    guard let heartRateType = HKObjectType.quantityType(forIdentifier: .heartRate) else { return nil }
    return await withCheckedContinuation { continuation in
      let query = HKSampleQuery(
        sampleType: HKObjectType.workoutType(), predicate: predicate, limit: HKObjectQueryNoLimit,
        sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)]
      ) { _, samples, _ in
        let restingHR = HealthKitTrainingSnapshot.load()?.restingHeartRateBPM
        let workouts = (samples as? [HKWorkout] ?? []).map { workout -> HealthKitWorkoutLoad in
          let stats = workout.statistics(for: heartRateType)
          let average = stats?.averageQuantity()?.doubleValue(for: HKUnit.count().unitDivided(by: .minute()))
          let peak = stats?.maximumQuantity()?.doubleValue(for: HKUnit.count().unitDivided(by: .minute()))
          let safeAverage = average.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
          let safePeak = peak.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
          let minutes = max(0, workout.duration / 60)
          let points: Double?
          if let safeAverage, let restingHR, restingHR > 0 {
            points = minutes * max(0, (safeAverage - restingHR) / restingHR)
          } else {
            points = nil
          }
          let activity: String
          switch workout.workoutActivityType {
          case .running: activity = "Run"
          case .walking: activity = "Walk"
          case .cycling: activity = "Cycle"
          case .hiking: activity = "Hike"
          case .traditionalStrengthTraining: activity = "Strength"
          default: activity = "Workout"
          }
          return HealthKitWorkoutLoad(
            date: workout.endDate, activity: activity, durationMinutes: minutes,
            averageHeartRateBPM: safeAverage, peakHeartRateBPM: safePeak,
            cardioLoadPoints: points
          )
        }
        continuation.resume(returning: workouts.isEmpty ? nil : workouts)
      }
      store.execute(query)
    }
  }

  private static func sleepHistory(_ store: HKHealthStore, _ now: Date) async -> [HealthKitSleepNight]? {''',
"workout history query helper")

p.write_text(s)

p = root / "HealthView.swift"
v = p.read_text()
anchor = '''      LazyVGrid(columns: columns, spacing: 10) {'''
cardio_ui = '''      if let load = store.healthKitTrainingSnapshot?.cardioLoadSummary {
        VStack(alignment: .leading, spacing: 9) {
          HStack {
            Text("DAILY STRAIN & CARDIO LOAD").font(.caption.weight(.bold)).tracking(1.0).foregroundStyle(.secondary)
            Spacer()
            Text("\\(load.workoutCount) workouts / 7d").font(.caption).foregroundStyle(.secondary)
          }
          HStack(alignment: .firstTextBaseline, spacing: 5) {
            Text(load.sevenDayPoints.map { String(format: "%.0f", $0) } ?? "—")
              .font(.system(size: 34, weight: .bold, design: .rounded)).monospacedDigit()
            Text("7-day HR load pts").font(.caption).foregroundStyle(.secondary)
            Spacer()
            Text(load.todayPoints.map { String(format: "Today %.0f", $0) } ?? "Today —")
              .font(.subheadline.weight(.semibold)).monospacedDigit()
          }
          Text(String(format: "%.0f workout minutes in the last 7 days", load.sevenDayMinutes))
            .font(.caption).foregroundStyle(.secondary)
          ForEach(load.recentWorkouts) { workout in
            HStack(spacing: 8) {
              Image(systemName: workout.activity == "Run" ? "figure.run" : "figure.mixed.cardio")
                .foregroundStyle(.secondary).frame(width: 20)
              VStack(alignment: .leading, spacing: 2) {
                Text(workout.activity).font(.subheadline.weight(.semibold))
                Text(workout.date.formatted(date: .abbreviated, time: .shortened))
                  .font(.caption2).foregroundStyle(.secondary)
              }
              Spacer()
              VStack(alignment: .trailing, spacing: 2) {
                Text(String(format: "%.0f min", workout.durationMinutes)).font(.subheadline.monospacedDigit())
                Text(workout.cardioLoadPoints.map { String(format: "%.0f load pts", $0) } ?? "HR data unavailable")
                  .font(.caption2).foregroundStyle(.secondary)
              }
            }
          }
          Text("Relative estimate: workout minutes × (average HR − resting HR) ÷ resting HR. Requires workout HR and a resting-HR reading; it is not WHOOP's proprietary Strain score.")
            .font(.caption2).foregroundStyle(.tertiary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      } else {
        VStack(alignment: .leading, spacing: 4) {
          Text("Cardio load is building").font(.subheadline.weight(.semibold))
          Text("Sync Apple Health workouts. Load points need workout heart-rate data and a resting-HR reading.")
            .font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
      }
      LazyVGrid(columns: columns, spacing: 10) {'''
if anchor not in v:
    raise SystemExit("Cardio-load UI insertion anchor missing")
v = v.replace(anchor, cardio_ui, 1)
p.write_text(v)
print("Added 14-day Apple Health workout history and transparent relative cardio-load summary.")
