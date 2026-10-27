#!/usr/bin/env python3
"""Apply the Gen5 notification-registration settle delay used by Edge."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def one(name: str) -> Path:
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one {name}, found {len(matches)}")
    return matches[0]

client = one("GooseBLEClient.swift")
s = client.read_text()
anchor = "  var clientHelloBootstrapScheduledForCurrentConnection = false\n"
replacement = anchor + "  var v5NotificationRegistrationScheduled: Set<CBUUID> = []\n"
if "var v5NotificationRegistrationScheduled: Set<CBUUID>" not in s:
    if s.count(anchor) != 1:
        raise SystemExit("Could not locate Gen5 connection state insertion point.")
    s = s.replace(anchor, replacement, 1)
client.write_text(s)

commands = one("GooseBLEClient+Commands.swift")
c = commands.read_text()
anchor = """    peripheral.setNotifyValue(true, for: characteristic)
    record(source: "ble", title: "notify.requested", body: "\\(characteristic.uuid.uuidString)")
"""
replacement = """    let gen5NotificationIDs: Set<CBUUID> = [
      CBUUID(string: "fd4b0003-cce1-4033-93ce-002d5875f58a"),
      CBUUID(string: "fd4b0004-cce1-4033-93ce-002d5875f58a"),
      CBUUID(string: "fd4b0005-cce1-4033-93ce-002d5875f58a")
    ]
    if gen5NotificationIDs.contains(characteristic.uuid) {
      guard v5NotificationRegistrationScheduled.insert(characteristic.uuid).inserted else {
        return
      }
      let peripheralID = peripheral.identifier
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self, weak peripheral, weak characteristic] in
        guard let self, let peripheral, let characteristic,
              self.activePeripheral?.identifier == peripheralID else {
          return
        }
        peripheral.setNotifyValue(true, for: characteristic)
        self.record(
          source: "ble",
          title: "notify.requested",
          body: "\\(characteristic.uuid.uuidString) gen5_pre_registration_settle_ms=600"
        )
      }
      return
    }
    peripheral.setNotifyValue(true, for: characteristic)
    record(source: "ble", title: "notify.requested", body: "\\(characteristic.uuid.uuidString)")
"""
if replacement not in c:
    if c.count(anchor) != 1:
        raise SystemExit("Could not locate notify registration call.")
    c = c.replace(anchor, replacement, 1)
commands.write_text(c)

central = one("GooseBLEClient+CentralDelegate.swift")
d = central.read_text()
reset_anchor = """    clientHelloSentForCurrentConnection = false
"""
reset_replacement = reset_anchor + """    clientHelloResponseConfirmedForCurrentConnection = false
    clientHelloBootstrapScheduledForCurrentConnection = false
    clientHelloResponseTimeoutWorkItem?.cancel()
    clientHelloResponseTimeoutWorkItem = nil
    v5NotificationRegistrationScheduled.removeAll()
"""
if reset_replacement not in d:
    if d.count(reset_anchor) != 2:
        raise SystemExit("Could not locate both per-connection hello reset points.")
    d = d.replace(reset_anchor, reset_replacement)
central.write_text(d)

print("Applied 600ms Gen5 notification registration settle delay.")
