#!/usr/bin/env python3
"""Use the Gen5 HELLO RTC timestamp and perform Edge-compatible SET_CLOCK before sync."""
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
replacement = anchor + "  var clientHelloClockBootstrapPending = false\n"
if "var clientHelloClockBootstrapPending = false" not in s:
    if s.count(anchor) != 1:
        raise SystemExit("Could not locate Gen5 HELLO state insertion point.")
    s = s.replace(anchor, replacement, 1)
client.write_text(s)

central = one("GooseBLEClient+CentralDelegate.swift")
d = central.read_text()
reset_anchor = """    clientHelloBootstrapScheduledForCurrentConnection = false
    clientHelloResponseTimeoutWorkItem?.cancel()
"""
reset_replacement = reset_anchor + """    clientHelloClockBootstrapPending = false
"""
if reset_replacement not in d:
    if d.count(reset_anchor) != 2:
        raise SystemExit("Could not locate both Gen5 clock-bootstrap connection resets.")
    d = d.replace(reset_anchor, reset_replacement)
central.write_text(d)

handlers = one("GooseBLEClient+HistoricalHandlers.swift")
h = handlers.read_text()
# Permit the one stateful Gen5 parser to handle HELLO/clock replies before the
# historical transfer starts. This avoids parsing the same notification twice.
guard_old = """    guard isHistoricalSyncing else {
      return
    }
"""
guard_new = """    guard isHistoricalSyncing
      || pendingClockCommand != nil
      || (clientHelloSentForCurrentConnection && !clientHelloResponseConfirmedForCurrentConnection) else {
      return
    }
"""
if guard_new not in h:
    if h.count(guard_old) != 1:
        raise SystemExit("Could not locate historical notification handler guard.")
    h = h.replace(guard_old, guard_new, 1)

# The notification set also contains standard 2A37 heart-rate characteristics.
# Only feed the three Gen5 fd4b protocol channels into the Puffin reassembler.
channel_anchor = "  func handleHistoricalSyncValue(_ value: Data, characteristic: CBCharacteristic) {\\n"
channel_guard = """    guard characteristic.uuid == CBUUID(string: "fd4b0003-cce1-4033-93ce-002d5875f58a")
       || characteristic.uuid == CBUUID(string: "fd4b0004-cce1-4033-93ce-002d5875f58a")
       || characteristic.uuid == CBUUID(string: "fd4b0005-cce1-4033-93ce-002d5875f58a") else {
      return
    }
"""
if channel_guard not in h:
    if h.count(channel_anchor) != 1:
        raise SystemExit("Could not locate historical channel guard insertion point.")
    h = h.replace(channel_anchor, channel_anchor + channel_guard, 1)

command_old = """    switch packetType {
    case V5PacketType.commandResponse, V5PacketType.puffinCommandResponse:
      handleHistoricalCommandResponse(payload)
"""
command_new = """    switch packetType {
    case V5PacketType.commandResponse:
      if payload.count >= 5, payload[2] == 145, payload[3] == 1 {
        handleClientHelloResponsePayload(payload)
      } else if payload.count >= 5, payload[2] == 10 || payload[2] == 11 {
        handleClockCommandResponse(payload)
      } else {
        handleHistoricalCommandResponse(payload)
      }
    case V5PacketType.puffinCommandResponse:
      handleHistoricalCommandResponse(payload)
"""
if command_new not in h:
    if h.count(command_old) != 1:
        raise SystemExit("Could not locate historical command-response switch.")
    h = h.replace(command_old, command_new, 1)

hello_methods = """  func handleClientHelloResponsePayload(_ payload: [UInt8]) {
    guard payload.count >= 5,
          payload[0] == V5PacketType.commandResponse,
          payload[2] == 145,
          payload[3] == 1 else {
      return
    }

    let status = payload[4]
    if status == 2 {
      record(level: .debug, source: "ble", title: "hello.response.pending", body: "GET_HELLO seq=1")
      return
    }
    clientHelloResponseTimeoutWorkItem?.cancel()
    clientHelloResponseTimeoutWorkItem = nil
    guard status == 1 else {
      record(level: .error, source: "ble", title: "hello.response.rejected", body: "GET_HELLO status=\\(status)")
      updateConnectionState("hello failed")
      return
    }

    let body = Array(payload.dropFirst(5))
    guard body.count >= 104 else {
      record(level: .error, source: "ble", title: "hello.response.invalid", body: "body length=\\(body.count)")
      updateConnectionState("hello invalid")
      return
    }
    let revision = body[0]
    guard let optical = Self.readUInt32LE(body, at: 87),
          optical >= 48, optical < 86 else {
      record(level: .error, source: "ble", title: "hello.response.wrong_device", body: "optical discriminator invalid")
      updateConnectionState("unsupported WHOOP model")
      return
    }

    let firmware = "\\(body[91]).\\(body[92]).\\(body[93])"
    var needsClockCorrection = true
    var clockDeltaSeconds: Double?
    if revision == 1,
       let seconds = Self.readUInt32LE(body, at: 6),
       let subseconds = Self.readUInt32LE(body, at: 10),
       seconds > 0, subseconds < 32768 {
      let strapDate = Date(
        timeIntervalSince1970: Double(seconds) + Double(subseconds) / 32768.0
      )
      let now = Date()
      let delta = abs(now.timeIntervalSince(strapDate))
      clockDeltaSeconds = delta
      needsClockCorrection = delta >= 2.0
      strapClockDate = strapDate
      strapClockOffsetSeconds = strapDate.timeIntervalSince(now)
      strapClockUpdatedAt = now
      strapClockStatus = needsClockCorrection
        ? "Clock differs by \\(Self.clockOffsetText(strapClockOffsetSeconds ?? 0)); syncing"
        : "Clock in sync"
    }

    record(
      source: "ble",
      title: "hello.response.confirmed",
      body: "GET_HELLO seq=1 revision=\\(revision) optical=\\(optical) firmware=\\(firmware) clock_delta=\\(clockDeltaSeconds.map { String(format: "%.3f", $0) } ?? "unknown")"
    )

    if needsClockCorrection {
      clientHelloClockBootstrapPending = true
      record(
        source: "ble.clock",
        title: "clock.bootstrap.set_required",
        body: "Gen5 HELLO timestamp missing, untrusted, or >=2s from phone; sending SET_CLOCK(10) 8-byte body"
      )
      writeClockCommand(.set(Date()), syncIfNeeded: false)
    } else {
      completeClientHelloBootstrap(reason: "hello_clock_in_sync")
    }
  }

  func completeClientHelloBootstrap(reason: String) {
    guard clientHelloSentForCurrentConnection else { return }
    clientHelloClockBootstrapPending = false
    clientHelloResponseConfirmedForCurrentConnection = true
    clientHelloResponseTimeoutWorkItem?.cancel()
    clientHelloResponseTimeoutWorkItem = nil
    updateConnectionState("ready")
    record(
      source: "ble",
      title: "hello.bootstrap.ready",
      body: "reason=\\(reason); Gen5 HELLO and RTC contract confirmed"
    )
    scheduleAutomaticHistoricalSyncIfNeeded()
    scheduleAutomaticPhysiologyCaptureIfNeeded()
    scheduleDebugSkinTemperatureCommandIfNeeded(reason: "hello_confirmed")
  }

"""
if "func handleClientHelloResponsePayload(" not in h:
    anchor = "  func handleHistoricalSyncValue(_ value: Data, characteristic: CBCharacteristic) {"
    if h.count(anchor) != 1:
        raise SystemExit("Could not locate HELLO payload handler insertion point.")
    h = h.replace(anchor, hello_methods + anchor, 1)
handlers.write_text(h)

# The stateful handler above is now the single parser for Gen5 command replies.
# Remove the second stateful parse that the first patch inserted in the peripheral delegate.
peripheral = one("GooseBLEClient+PeripheralDelegate.swift")
p = peripheral.read_text()
p = p.replace(
    """    handleClientHelloResponseValue(value, characteristic: characteristic)
""",
    "",
    1,
)
peripheral.write_text(p)

# Do not let the legacy stateless clock parser consume a Gen5 command response
# before the stateful reassembler has handled it.
clock_guard = """  func handleClockValue(_ value: Data, characteristic: CBCharacteristic) {
    guard notificationCharacteristicIDs.contains(characteristic.uuid) else {
"""
clock_replacement = """  func handleClockValue(_ value: Data, characteristic: CBCharacteristic) {
    if supportsV5HistoricalSync,
       characteristic.uuid == CBUUID(string: "fd4b0003-cce1-4033-93ce-002d5875f58a"),
       (isHistoricalSyncing
         || pendingClockCommand != nil
         || (clientHelloSentForCurrentConnection && !clientHelloResponseConfirmedForCurrentConnection)) {
      return
    }
    guard notificationCharacteristicIDs.contains(characteristic.uuid) else {
"""
if clock_replacement not in h:
    # handleClockValue is in the same historical handler file; re-read after prior edits.
    h = handlers.read_text()
    if h.count(clock_guard) != 1:
        raise SystemExit("Could not locate legacy clock value handler.")
    h = h.replace(clock_guard, clock_replacement, 1)
    handlers.write_text(h)

# On a confirmed SET_CLOCK response, release the HELLO gate. On failure/timeout,
# fail closed so automatic historical sync cannot run with an untrusted RTC.
commands = one("GooseBLEClient+Commands.swift")
cmd = commands.read_text()
fail_anchor = """  func failClockCommand(_ message: String) {
    clockCommandTimeoutWorkItem?.cancel()
    pendingClockCommand = nil
    strapClockStatus = message
"""
fail_replacement = """  func failClockCommand(_ message: String) {
    clockCommandTimeoutWorkItem?.cancel()
    pendingClockCommand = nil
    strapClockStatus = message
    if clientHelloClockBootstrapPending {
      clientHelloClockBootstrapPending = false
      clientHelloResponseTimeoutWorkItem?.cancel()
      clientHelloResponseTimeoutWorkItem = nil
      updateConnectionState("clock sync failed")
      record(level: .error, source: "ble", title: "hello.clock.bootstrap.failed", body: message)
    }
"""
if fail_replacement not in cmd:
    if cmd.count(fail_anchor) != 1:
        raise SystemExit("Could not locate clock failure handler.")
    cmd = cmd.replace(fail_anchor, fail_replacement, 1)
commands.write_text(cmd)

# A status=2 command response is pending, not terminal. Keep the same
# sequence and timeout alive until the final SET_CLOCK response arrives.
clock_handlers = handlers.read_text()
pending_anchor = """    clockCommandTimeoutWorkItem?.cancel()
    pendingClockCommand = nil
    let resultCode = payload[4]
"""
pending_replacement = """    let resultCode = payload[4]
    if resultCode == 2 {
      record(level: .debug, source: "ble.clock", title: "clock.response.pending", body: "\\(pending.kind.name) seq=\\(pending.sequence)")
      return
    }
    clockCommandTimeoutWorkItem?.cancel()
    pendingClockCommand = nil
"""
if pending_replacement not in clock_handlers:
    if clock_handlers.count(pending_anchor) != 1:
        raise SystemExit("Could not locate clock response terminal-status handling.")
    clock_handlers = clock_handlers.replace(pending_anchor, pending_replacement, 1)
handlers.write_text(clock_handlers)

# A SET_CLOCK response is correlated by opcode+sequence in handleClockCommandResponse.
# Add readiness completion only after a successful response.
h = handlers.read_text()
set_anchor = """      strapClockStatus = "Clock synced"
      record(source: "ble.clock", title: "clock.synced", body: "seq=\\(pending.sequence) \\(result)")
"""
set_replacement = set_anchor + """      if clientHelloClockBootstrapPending {
        completeClientHelloBootstrap(reason: "set_clock_confirmed")
      }
"""
if set_replacement not in h:
    if h.count(set_anchor) != 1:
        raise SystemExit("Could not locate successful SET_CLOCK response.")
    h = h.replace(set_anchor, set_replacement, 1)
handlers.write_text(h)

print("Applied Edge-compatible Gen5 HELLO clock contract before history offload.")
