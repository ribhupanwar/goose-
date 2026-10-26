#!/usr/bin/env python3
"""Print focused WHOOP BLE source diagnostics without modifying app code."""
from pathlib import Path
import re, sys

root = Path(sys.argv[1])
terms = re.compile(
    r"GET_DATA_RANGE|SEND_HISTORICAL_DATA|HISTORICAL_DATA_RESULT|"
    r"historical_metadata_idle|historical.*(packet|sync)|"
    r"puffinCommandFrame|crc16Modbus|DATA_FROM_STRAP|"
    r"setNotifyValue|didUpdateValueFor|0x2F|0x22|0x16",
    re.IGNORECASE,
)
extensions = {".swift", ".rs", ".m", ".mm", ".h", ".kt", ".java"}
hits = 0
print("=== WHOOP 5.0 historical-sync source diagnostics ===")
for path in root.rglob("*"):
    if not path.is_file() or path.suffix.lower() not in extensions:
        continue
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        continue
    matching = [i for i, line in enumerate(lines) if terms.search(line)]
    if not matching:
        continue
    hits += len(matching)
    print(f"\n--- {path.relative_to(root)} ({len(matching)} matches) ---")
    shown = set()
    for i in matching:
        for j in range(max(0, i-3), min(len(lines), i+4)):
            if j not in shown:
                print(f"{j+1:5}: {lines[j][:260]}")
                shown.add(j)
        print("     ...")
print(f"\nTotal matching source lines: {hits}")
if hits == 0:
    print("WARNING: no matching identifiers found; BLE implementation may be in generated/binary dependencies.")
