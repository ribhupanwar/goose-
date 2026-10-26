#!/usr/bin/env python3
import pathlib, sys

root = pathlib.Path(sys.argv[1])
candidates = list(root.rglob('GooseBLEClient+PeripheralDelegate.swift'))
if len(candidates) != 1:
    raise SystemExit(f'Expected exactly one PeripheralDelegate source, found {len(candidates)}')
p = candidates[0]
s = p.read_text()
old = '''      case V5PacketType.commandResponse,
           V5PacketType.puffinCommandResponse,
           V5PacketType.event,
           V5PacketType.metadata,
           V5PacketType.puffinMetadata:
        return true'''
new = '''      case V5PacketType.commandResponse,
           V5PacketType.puffinCommandResponse,
           V5PacketType.event,
           V5PacketType.metadata,
           V5PacketType.puffinMetadata:
        return true
      case V5PacketType.historicalData, V5PacketType.historicalIMUDataStream:
        // Historical payload parsing mutates sync state and must run on main.
        return isHistoricalSyncing'''
if old not in s:
    if 'V5PacketType.historicalData, V5PacketType.historicalIMUDataStream:' in s:
        print('WHOOP V5 historical routing already present; no change needed')
        raise SystemExit(0)
    raise SystemExit('Could not find expected V5 packet dispatch switch; refusing a blind patch')
p.write_text(s.replace(old, new, 1))
print(f'Patched {p}')
