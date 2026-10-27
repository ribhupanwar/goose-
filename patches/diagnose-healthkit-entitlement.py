#!/usr/bin/env python3
"""Expose whether the installed sideload profile actually grants HealthKit."""
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "GooseSwift"
snapshot = root / "HealthKitSleepImporter.swift"
s = snapshot.read_text()
anchor = '  static let didUpdate = Notification.Name("GooseHealthKitTrainingSnapshotDidUpdate")\n'
addition = anchor + """  /// A free-Apple-ID re-sign may strip HealthKit even when the IPA requested it.
  /// Read the embedded provisioning profile instead of mistaking a silent no-prompt for user denial.
  static let hasHealthKitEntitlement: Bool = {
    guard let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
          let data = try? Data(contentsOf: url) else {
      // App Store builds do not carry an embedded profile; treat those as entitled.
      return true
    }
    guard let xmlStart = data.range(of: Data("<?xml".utf8)),
          let xmlEnd = data.range(of: Data("</plist>".utf8)) else { return true }
    let plistData = data.subdata(in: xmlStart.lowerBound..<xmlEnd.upperBound)
    guard let plist = try? PropertyListSerialization.propertyList(from: plistData, options: [], format: nil) as? [String: Any],
          let entitlements = plist["Entitlements"] as? [String: Any] else { return true }
    return entitlements["com.apple.developer.healthkit"] != nil
  }()
"""
if addition not in s:
    if s.count(anchor) != 1: raise SystemExit("HealthKit entitlement state anchor missing")
    s = s.replace(anchor, addition, 1)
snapshot.write_text(s)

store = root / "HealthDataStore.swift"
v = store.read_text()
old = """  func syncHealthKitTrainingSignals() {
    healthKitSyncStatus = "Syncing Apple Health…"
"""
new = """  func syncHealthKitTrainingSignals() {
    guard HealthKitTrainingSnapshot.hasHealthKitEntitlement else {
      healthKitSyncStatus = "This SideStore-signed build has no HealthKit entitlement. Re-sign an IPA that includes the HealthKit capability template."
      return
    }
    healthKitSyncStatus = "Syncing Apple Health…"
"""
if new not in v:
    if v.count(old) != 1: raise SystemExit("HealthKit sync entry point missing")
    v = v.replace(old, new, 1)
store.write_text(v)
print("Added runtime HealthKit entitlement detection and honest missing-capability status.")
