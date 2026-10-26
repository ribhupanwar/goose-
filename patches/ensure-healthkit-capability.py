#!/usr/bin/env python3
"""Ensure the Goose iOS target declares HealthKit capability and read usage text."""
from pathlib import Path
import plistlib
import re
import sys

root = Path(sys.argv[1]).resolve()
project = root / "GooseSwift.xcodeproj" / "project.pbxproj"
if not project.is_file():
    raise SystemExit(f"Missing Xcode project: {project}")

entitlements = root / "GooseSwift" / "GooseSwift.entitlements"
entitlements.parent.mkdir(parents=True, exist_ok=True)
with entitlements.open("wb") as f:
    plistlib.dump({
        "com.apple.developer.healthkit": True,
        "com.apple.developer.healthkit.access": [],
    }, f, fmt=plistlib.FMT_XML, sort_keys=True)

text = project.read_text()
setting = "CODE_SIGN_ENTITLEMENTS = GooseSwift/GooseSwift.entitlements;"
if setting not in text:
    # Apply to every target configuration; the GooseSwift app is the only app target
    # in this project and all configurations must use the same entitlement file.
    pattern = r"(buildSettings = \\{)"
    matches = list(re.finditer(pattern, text))
    if not matches:
        raise SystemExit("No Xcode build settings blocks found")
    text = re.sub(pattern, r"\\1\n\\t\\t\\t\\t" + setting, text)
project.write_text(text)

info = root / "GooseSwift" / "Info.plist"
if not info.is_file():
    candidates = list(root.rglob("Info.plist"))
    if len(candidates) != 1:
        raise SystemExit(f"Could not identify app Info.plist; found {len(candidates)}")
    info = candidates[0]
with info.open("rb") as f:
    plist = plistlib.load(f)
plist["NSHealthShareUsageDescription"] = (
    "Goose reads your Apple Health heart rate, HRV, resting heart rate, "
    "sleep, fitness and activity data to calculate your personal recovery "
    "and running insights. This data stays on your device."
)
with info.open("wb") as f:
    plistlib.dump(plist, f, fmt=plistlib.FMT_XML, sort_keys=False)

print(f"HealthKit entitlement configured: {entitlements}")
print(f"HealthKit read usage description configured: {info}")
