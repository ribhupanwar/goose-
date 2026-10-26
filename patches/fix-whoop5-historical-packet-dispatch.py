#!/usr/bin/env python3
"""Apply the WHOOP 5.0 historical packet dispatch fix to the extracted Goose source."""
from pathlib import Path
import sys

root = Path(sys.argv[1])
matches = list(root.rglob("GooseBLEClient+PeripheralDelegate.swift"))
if len(matches) != 1:
    raise SystemExit(f"Expected exactly one GooseBLEClient+PeripheralDelegate.swift, found {len(matches)}")
path = matches[0]
source = path.read_text()

if "Route historical body packets to the main handler only while a sync" in source:
    print("WHOOP 5.0 historical packet dispatch fix already present.")
    raise SystemExit(0)

anchor = """      case V5PacketType.commandResponse,
           V5PacketType.puffinCommandResponse,
           V5PacketType.event,
           V5PacketType.metadata,
           V5PacketType.puffinMetadata:
        return true
      default:
"""
replacement = """      case V5PacketType.commandResponse,
           V5PacketType.puffinCommandResponse,
           V5PacketType.event,
           V5PacketType.metadata,
           V5PacketType.puffinMetadata:
        return true
      case V5PacketType.historicalData,
           V5PacketType.historicalIMUDataStream:
        // During historical sync, route body packets to the main handler so
        // handleHistoricalSyncValue can count them. Keep live frames off-main.
        if isHistoricalSyncing {
          return true
        }
        continue
      default:
"""
count = source.count(anchor)
if count != 1:
    raise SystemExit(f"Expected one dispatch switch anchor, found {count}; source left unchanged.")
path.write_text(source.replace(anchor, replacement, 1))
print(f"Applied WHOOP 5.0 historical packet dispatch fix: {path}")
