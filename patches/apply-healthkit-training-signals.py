#!/usr/bin/env python3
"""Apply the Goose Apple Health training-signal integration to the ZIP source tree."""
from pathlib import Path
import sys

root = Path(sys.argv[1])
swift = root / "GooseSwift"
def edit(path, old, new):
    p = swift / path
    s = p.read_text()
    if old not in s:
        raise SystemExit(f"Expected source anchor missing in {path}; refusing partial patch")
    p.write_text(s.replace(old, new, 1))

p = swift / "HealthKitSleepImporter.swift"
s = p.read_text()
old = """    var types = Set<HKObjectType>()
    if let bodyMassType = HKObjectType.quantityType(forIdentifier: .bodyMass) {
      types.insert(bodyMassType)
    }
    return types"""
new = """    var types = Set<HKObjectType>()
    let identifiers: [HKQuantityTypeIdentifier] = [
      .bodyMass, .heartRate, .heartRateVariabilitySDNN, .restingHeartRate,
      .respiratoryRate, .oxygenSaturation, .vo2Max, .stepCount, .activeEnergyBurned,
    ]
    for identifier in identifiers {
      if let type = HKObjectType.quantityType(forIdentifier: identifier) { types.insert(type) }
    }
    if let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) { types.insert(sleep) }
    types.insert(HKObjectType.workoutType())
    return types"""
if old not in s: raise SystemExit("HealthKit readTypes anchor missing")
s = s.replace(old, new, 1)
old = """      let autofill = await latestMeasurements(store: store)
      return HealthKitProfileImportResult(
        status: autofill.hasValues ? "Requested in Health; \\(autofill.sourceSummary)" : "Requested in Health",
        autofill: autofill
      )"""
new = """      let autofill = await latestMeasurements(store: store)
      let training = await latestTrainingSnapshot(store: store)
      return HealthKitProfileImportResult(
        status: "Apple Health sync: \\(training.availableSignalCount)/9 signals found. \\(autofill.sourceSummary)",
        autofill: autofill
      )"""
if old not in s: raise SystemExit("HealthKit authorization result anchor missing")
s = s.replace(old, new, 1)
s += r'''

/// Latest Apple Health values used as source signals, never as fabricated scores.
struct HealthKitTrainingSnapshot: Codable {
  let capturedAt: Date
  let heartRateBPM: Double?
  let hrvSDNNMilliseconds: Double?
  let restingHeartRateBPM: Double?
  let respiratoryRatePerMinute: Double?
  let oxygenSaturationPercent: Double?
  let vo2Max: Double?
  let stepsToday: Double?
  let activeEnergyKilocaloriesToday: Double?
  let sleepMinutes: Double?
  let source: String

  static let didUpdate = Notification.Name("GooseHealthKitTrainingSnapshotDidUpdate")
  var availableSignalCount: Int {
    [heartRateBPM, hrvSDNNMilliseconds, restingHeartRateBPM, respiratoryRatePerMinute,
     oxygenSaturationPercent, vo2Max, stepsToday, activeEnergyKilocaloriesToday,
     sleepMinutes].compactMap { $0 }.count
  }
  var summary: String {
    [
      heartRateBPM.map { "HR \\(Int($0.rounded())) bpm" },
      hrvSDNNMilliseconds.map { "HRV \\(Int($0.rounded())) ms" },
      restingHeartRateBPM.map { "RHR \\(Int($0.rounded())) bpm" },
      respiratoryRatePerMinute.map { "Resp \\(String(format: "%.1f", $0))/min" },
      oxygenSaturationPercent.map { "SpO₂ \\(String(format: "%.1f", $0))%" },
      vo2Max.map { "VO₂ max \\(String(format: "%.1f", $0))" },
      stepsToday.map { "Steps \\(Int($0.rounded()))" },
      activeEnergyKilocaloriesToday.map { "Active energy \\(Int($0.rounded())) kcal" },
      sleepMinutes.map { "Sleep \\(String(format: "%.1f", $0 / 60)) h" },
    ].compactMap { $0 }.joined(separator: " | ")
  }
  private static var fileURL: URL? {
    guard let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return nil }
    let folder = base.appendingPathComponent("GooseSwift", isDirectory: true)
    try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    return folder.appendingPathComponent("healthkit-training-snapshot.json")
  }
  static func load() -> HealthKitTrainingSnapshot? {
    guard let url = fileURL, let data = try? Data(contentsOf: url) else { return nil }
    return try? JSONDecoder().decode(Self.self, from: data)
  }
  func save() {
    guard let url = Self.fileURL, let data = try? JSONEncoder().encode(self) else { return }
    do {
      try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
      DispatchQueue.main.async { NotificationCenter.default.post(name: Self.didUpdate, object: nil) }
    } catch { /* Keep Health values out of logs. */ }
  }
}

extension HealthKitProfileImporter {
  static func latestTrainingSnapshot(store: HKHealthStore = HKHealthStore(), now: Date = Date()) async -> HealthKitTrainingSnapshot {
    async let hr = latest(store, .heartRate, HKUnit.count().unitDivided(by: .minute()))
    async let hrv = latest(store, .heartRateVariabilitySDNN, HKUnit.secondUnit(with: .milli))
    async let rhr = latest(store, .restingHeartRate, HKUnit.count().unitDivided(by: .minute()))
    async let resp = latest(store, .respiratoryRate, HKUnit.count().unitDivided(by: .minute()))
    async let spo2 = latest(store, .oxygenSaturation, .percent())
    async let vo2 = latest(store, .vo2Max, HKUnit(from: "ml/kg*min"))
    async let steps = totalToday(store, .stepCount, .count(), now)
    async let energy = totalToday(store, .activeEnergyBurned, .kilocalorie(), now)
    async let sleep = sleepMinutes(store, now)
    let snapshot = await HealthKitTrainingSnapshot(
      capturedAt: now, heartRateBPM: hr, hrvSDNNMilliseconds: hrv,
      restingHeartRateBPM: rhr, respiratoryRatePerMinute: resp,
      oxygenSaturationPercent: spo2.map { $0 * 100 }, vo2Max: vo2,
      stepsToday: steps, activeEnergyKilocaloriesToday: energy,
      sleepMinutes: sleep, source: "Apple Health"
    )
    snapshot.save()
    return snapshot
  }
  private static func latest(_ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit) async -> Double? {
    guard let type = HKObjectType.quantityType(forIdentifier: id) else { return nil }
    return await withCheckedContinuation { c in
      let q = HKSampleQuery(sampleType: type, predicate: nil, limit: 1,
        sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)]) { _, samples, _ in
        let value = (samples?.first as? HKQuantitySample)?.quantity.doubleValue(for: unit)
        c.resume(returning: value.flatMap { $0.isFinite && $0 > 0 ? $0 : nil })
      }
      store.execute(q)
    }
  }
  private static func totalToday(_ store: HKHealthStore, _ id: HKQuantityTypeIdentifier, _ unit: HKUnit, _ now: Date) async -> Double? {
    guard let type = HKObjectType.quantityType(forIdentifier: id) else { return nil }
    let start = Calendar.current.startOfDay(for: now)
    let predicate = HKQuery.predicateForSamples(withStart: start, end: now, options: .strictStartDate)
    return await withCheckedContinuation { c in
      let q = HKStatisticsQuery(quantityType: type, quantitySamplePredicate: predicate, options: .cumulativeSum) { _, stats, _ in
        let value = stats?.sumQuantity()?.doubleValue(for: unit)
        c.resume(returning: value.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil })
      }
      store.execute(q)
    }
  }
  private static func sleepMinutes(_ store: HKHealthStore, _ now: Date) async -> Double? {
    guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { return nil }
    let start = Calendar.current.date(byAdding: .hour, value: -36, to: now) ?? now.addingTimeInterval(-129600)
    let predicate = HKQuery.predicateForSamples(withStart: start, end: now, options: .strictEndDate)
    return await withCheckedContinuation { c in
      let q = HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, _ in
        let asleep = [HKCategoryValueSleepAnalysis.asleep.rawValue, HKCategoryValueSleepAnalysis.asleepCore.rawValue,
          HKCategoryValueSleepAnalysis.asleepDeep.rawValue, HKCategoryValueSleepAnalysis.asleepREM.rawValue,
          HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue]
        let intervals = (samples as? [HKCategorySample] ?? []).filter { asleep.contains($0.value) }
          .map { (max($0.startDate, start), min($0.endDate, now)) }.filter { $0.1 > $0.0 }.sorted { $0.0 < $1.0 }
        var merged: [(Date, Date)] = []
        for item in intervals {
          if let last = merged.last, item.0 <= last.1 { merged[merged.count - 1] = (last.0, max(last.1, item.1)) }
          else { merged.append(item) }
        }
        let minutes = merged.reduce(0.0) { $0 + $1.1.timeIntervalSince($1.0) } / 60
        c.resume(returning: minutes > 0 ? minutes : nil)
      }
      store.execute(q)
    }
  }
}
'''
p.write_text(s)

p = swift / "HealthDataStore.swift"
s = p.read_text()
s = s.replace('  @Published var heartRateTimelineStatus = "No HR samples stored"\\n',
'''  @Published var heartRateTimelineStatus = "No HR samples stored"
  @Published var healthKitTrainingSnapshot: HealthKitTrainingSnapshot?
  @Published var healthKitSyncStatus = "Not synced"
''', 1)
s = s.replace('  var heartRateSeriesUpdateObserver: NSObjectProtocol?\\n',
'''  var heartRateSeriesUpdateObserver: NSObjectProtocol?
  var healthKitSnapshotObserver: NSObjectProtocol?
''', 1)
s = s.replace('''    primarySleepDetail = nil
    refreshHeartRateTimeline()
''','''    primarySleepDetail = nil
    healthKitTrainingSnapshot = HealthKitTrainingSnapshot.load()
    refreshHeartRateTimeline()
    healthKitSnapshotObserver = NotificationCenter.default.addObserver(
      forName: HealthKitTrainingSnapshot.didUpdate, object: nil, queue: .main
    ) { [weak self] _ in
      Task { @MainActor in self?.healthKitTrainingSnapshot = HealthKitTrainingSnapshot.load() }
    }
''', 1)
s = s.replace('''    if let heartRateSeriesUpdateObserver {
      NotificationCenter.default.removeObserver(heartRateSeriesUpdateObserver)
    }
''','''    if let heartRateSeriesUpdateObserver {
      NotificationCenter.default.removeObserver(heartRateSeriesUpdateObserver)
    }
    if let healthKitSnapshotObserver { NotificationCenter.default.removeObserver(healthKitSnapshotObserver) }
''', 1)
s = s.replace('"HealthKit metric import: disabled; profile weight only",',
              '"Apple Health training signals: \\\\(healthKitTrainingSummary())",', 1)
anchor = '  func loadBridgeCatalogsIfNeeded() {\\n'
methods = '''  func syncHealthKitTrainingSignals() {
    healthKitSyncStatus = "Syncing Apple Health…"
    Task { @MainActor in
      let result = await HealthKitProfileImporter.requestProfileAccess()
      healthKitTrainingSnapshot = HealthKitTrainingSnapshot.load()
      healthKitSyncStatus = result.status
    }
  }

  func healthKitTrainingSummary() -> String {
    guard let snapshot = healthKitTrainingSnapshot else { return "Not imported yet; tap Sync to request Apple Health access" }
    return "\\\\(snapshot.summary) | updated \\\\(snapshot.capturedAt.formatted(date: .abbreviated, time: .shortened))"
  }

'''
if anchor not in s: raise SystemExit("HealthDataStore insertion anchor missing")
s = s.replace(anchor, methods + anchor, 1)
p.write_text(s)

p = swift / "HealthView.swift"
s = p.read_text()
anchor = '        HealthActivityOverviewSection(\\n'
if anchor not in s: raise SystemExit("HealthView section anchor missing")
s = s.replace(anchor, '        HealthKitTrainingSignalsSection(store: store)\\n\\n' + anchor, 1)
s += r'''

private struct HealthKitTrainingSignalsSection: View {
  @ObservedObject var store: HealthDataStore
  private let columns = [GridItem(.flexible()), GridItem(.flexible())]
  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      HStack {
        VStack(alignment: .leading, spacing: 3) {
          Text("APPLE HEALTH").font(.caption.weight(.bold)).tracking(1.1).foregroundStyle(.secondary)
          Text("Recovery & running signals").font(.headline)
        }
        Spacer()
        Button { store.syncHealthKitTrainingSignals() } label: {
          Label("Sync", systemImage: "arrow.clockwise").font(.subheadline.weight(.semibold))
        }.buttonStyle(.bordered)
      }
      LazyVGrid(columns: columns, spacing: 10) {
        metric("Heart rate", number(store.healthKitTrainingSnapshot?.heartRateBPM, " bpm", 0), "heart.fill")
        metric("HRV (SDNN)", number(store.healthKitTrainingSnapshot?.hrvSDNNMilliseconds, " ms", 0), "waveform.path.ecg")
        metric("Resting HR", number(store.healthKitTrainingSnapshot?.restingHeartRateBPM, " bpm", 0), "heart")
        metric("Respiratory rate", number(store.healthKitTrainingSnapshot?.respiratoryRatePerMinute, "/min", 1), "lungs")
        metric("Blood oxygen", number(store.healthKitTrainingSnapshot?.oxygenSaturationPercent, "%", 1), "drop.fill")
        metric("VO₂ max", number(store.healthKitTrainingSnapshot?.vo2Max, "", 1), "figure.run")
        metric("Steps today", count(store.healthKitTrainingSnapshot?.stepsToday), "shoeprints.fill")
        metric("Active energy", number(store.healthKitTrainingSnapshot?.activeEnergyKilocaloriesToday, " kcal", 0), "flame.fill")
        metric("Sleep (36h window)", number(store.healthKitTrainingSnapshot?.sleepMinutes.map { $0 / 60 }, " h", 1), "moon.zzz.fill")
      }
      Text(store.healthKitSyncStatus == "Not synced" ? store.healthKitTrainingSummary() : store.healthKitSyncStatus)
        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
      Text("Missing values can mean no samples or unavailable read access. These are source measurements, not a fabricated recovery score.")
        .font(.caption2).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
    }
    .padding(14)
    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
  }
  private func metric(_ title: String, _ value: String, _ icon: String) -> some View {
    HStack(spacing: 9) {
      Image(systemName: icon).font(.system(size: 15, weight: .semibold)).foregroundStyle(.secondary).frame(width: 24)
      VStack(alignment: .leading, spacing: 3) {
        Text(title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        Text(value).font(.subheadline.weight(.semibold)).monospacedDigit().lineLimit(1)
      }
      Spacer(minLength: 0)
    }
    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
    .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
  }
  private func number(_ value: Double?, _ suffix: String, _ digits: Int) -> String {
    guard let value, value.isFinite, value > 0 else { return "—" }
    return value.formatted(.number.precision(.fractionLength(digits))) + suffix
  }
  private func count(_ value: Double?) -> String {
    guard let value, value.isFinite, value > 0 else { return "—" }
    return Int(value.rounded()).formatted()
  }
}
'''
p.write_text(s)

edit("CoachLocalToolContext.swift",
     '        "external_sleep_import": healthStore.externalSleepImportStatus,\\n',
     '        "external_sleep_import": healthStore.externalSleepImportStatus,\\n        "apple_health_training_signals": healthStore.healthKitTrainingSummary(),\\n')
edit("CodexCoachSupport.swift",
     '      "packet_scores": [\\n',
     '      "apple_health_training_signals": healthStore.healthKitTrainingSummary(),\\n      "packet_scores": [\\n')
edit("MoreDataStore.swift",
     '"Apple Health profile autofill available"',
     '"Apple Health recovery and running signals available"')
edit("MoreDataStore.swift",
     '"Reads body mass only for profile autofill"',
     '"Reads HR, HRV, resting HR, respiratory rate, SpO₂, VO₂ max, steps, active energy, sleep and body mass; stored on device"')
info = swift / "Info.plist"
s = info.read_text().replace(
  "Goose reads Apple Health body mass only to prefill your local profile weight.",
  "Goose reads selected Apple Health heart, recovery, fitness, activity and sleep metrics to power your personal insights. Health data is processed and stored locally on your device.",
  1
)
info.write_text(s)
print("Applied Apple Health training signal integration.")
