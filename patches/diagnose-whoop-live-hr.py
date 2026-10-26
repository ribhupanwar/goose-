#!/usr/bin/env python3
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
patterns = re.compile(r"didUpdateValueFor|setNotifyValue|heart.?rate|HeartRateMeasurement|shouldDispatchNotificationSideEffects|V5PacketType|live.?hr|notification|characteristic", re.I)
files = sorted(root.rglob("*.swift"))
out = []
out.append(f"Swift files scanned: {len(files)}")
for path in files:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        continue
    hits = [i for i, line in enumerate(lines) if patterns.search(line)]
    if not hits:
        continue
    out.append("\n### " + str(path.relative_to(root)))
    selected = set()
    for i in hits:
        selected.update(range(max(0, i-3), min(len(lines), i+4)))
    prev = -2
    for i in sorted(selected):
        if i > prev + 1:
            out.append("...")
        out.append(f"{i+1}: {lines[i][:400]}")
        prev = i
report = "\n".join(out) + "\n"
pathlib.Path("whoop-live-hr-diagnostics.txt").write_text(report)
print(report[:30000])
