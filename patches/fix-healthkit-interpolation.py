from pathlib import Path
import sys
root = Path(sys.argv[1]) / "GooseSwift"
for name in ["HealthKitSleepImporter.swift", "HealthDataStore.swift"]:
    path = root / name
    source = path.read_text()
    source = source.replace("\\\\(", "\\(")
    path.write_text(source)
print("Fixed Swift interpolation escaping.")
