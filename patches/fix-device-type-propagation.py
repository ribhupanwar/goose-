#!/usr/bin/env python3
"""Repair WHOOP parser device-type propagation before Xcode compilation."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def one(name):
    hits = list(root.rglob(name))
    if len(hits) != 1:
        raise SystemExit(f"Expected one {name}, found {len(hits)}")
    return hits[0]

p = one("WhoopEventSamples.swift")
s = p.read_text()
for old, new in [
    ("static func fromParsedFrame(_ parsed: [String: Any], capturedAt: Date) -> WhoopEventSample?",
     "static func fromParsedFrame(_ parsed: [String: Any], capturedAt: Date, deviceType: String) -> WhoopEventSample?"),
    ("static func fromCompactSummary(_ compact: NotificationFrameCompactSummary, capturedAt: Date) -> WhoopEventSample?",
     "static func fromCompactSummary(_ compact: NotificationFrameCompactSummary, capturedAt: Date, deviceType: String) -> WhoopEventSample?"),
    ("static func fromParsedFrame(_ parsed: [String: Any], capturedAt: Date) -> WhoopDataSignalSample?",
     "static func fromParsedFrame(_ parsed: [String: Any], capturedAt: Date, deviceType: String) -> WhoopDataSignalSample?"),
    ("static func fromCompactSummary(_ compact: NotificationFrameCompactSummary, capturedAt: Date) -> WhoopDataSignalSample?",
     "static func fromCompactSummary(_ compact: NotificationFrameCompactSummary, capturedAt: Date, deviceType: String) -> WhoopDataSignalSample?"),
]:
    s = s.replace(old, new, 1)
for old, new in [
    ("HistoryTemperatureCandidate.from(packetK: packetK, body: bodyData)", "HistoryTemperatureCandidate.from(packetK: packetK, body: bodyData, deviceType: deviceType)"),
    ("HistoryRespiratoryRateCandidate.from(packetK: packetK, body: bodyData)", "HistoryRespiratoryRateCandidate.from(packetK: packetK, body: bodyData, deviceType: deviceType)"),
    ("R21MotionCandidate.from(packetK: packetK, body: bodyData)", "R21MotionCandidate.from(packetK: packetK, body: bodyData, deviceType: deviceType)"),
    ("RawBodyDiagnostic.from(packetK: packetK, body: bodyData)", "RawBodyDiagnostic.from(packetK: packetK, body: bodyData, deviceType: deviceType)"),
]:
    s = s.replace(old, new)
p.write_text(s)

p = one("GooseAppModel+PacketPublishing.swift")
s = p.read_text()
for old, new in [
    ("WhoopEventSample.fromParsedFrame(parsed, capturedAt: capturedAt)", "WhoopEventSample.fromParsedFrame(parsed, capturedAt: capturedAt, deviceType: deviceType)"),
    ("WhoopEventSample.fromCompactSummary($0, capturedAt: capturedAt)", "WhoopEventSample.fromCompactSummary($0, capturedAt: capturedAt, deviceType: deviceType)"),
    ("WhoopDataSignalSample.fromParsedFrame(parsed, capturedAt: capturedAt)", "WhoopDataSignalSample.fromParsedFrame(parsed, capturedAt: capturedAt, deviceType: deviceType)"),
    ("WhoopDataSignalSample.fromCompactSummary($0, capturedAt: capturedAt)", "WhoopDataSignalSample.fromCompactSummary($0, capturedAt: capturedAt, deviceType: deviceType)"),
]:
    s = s.replace(old, new)
p.write_text(s)

p = one("GooseAppModel+NotificationPipeline.swift")
s = p.read_text()
for old, new in [
    ("extractWhoopEvent(from: compact, capturedAt: event.capturedAt)", "extractWhoopEvent(from: compact, capturedAt: event.capturedAt, deviceType: event.rustDeviceType)"),
    ("extractWhoopEvent(from: $0, capturedAt: event.capturedAt)", "extractWhoopEvent(from: $0, capturedAt: event.capturedAt, deviceType: event.rustDeviceType)"),
    ("extractWhoopDataSignal(from: compact, capturedAt: event.capturedAt)", "extractWhoopDataSignal(from: compact, capturedAt: event.capturedAt, deviceType: event.rustDeviceType)"),
    ("extractWhoopDataSignal(from: $0, capturedAt: event.capturedAt)", "extractWhoopDataSignal(from: $0, capturedAt: event.capturedAt, deviceType: event.rustDeviceType)"),
]:
    s = s.replace(old, new)
p.write_text(s)
print("WHOOP sample parser deviceType propagation normalized.")
