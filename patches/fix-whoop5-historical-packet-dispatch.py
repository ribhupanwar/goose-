#!/usr/bin/env python3
"""Apply WHOOP 5.0 historical dispatch and validated incremental framing."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def one(name: str) -> Path:
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one {name}, found {len(matches)}")
    return matches[0]

# Historical bodies arrive on the Gen5 data characteristic. Route that
# characteristic to the sync handler while a transfer is active, without
# forcing unrelated high-rate characteristics onto the main queue.
peripheral = one("GooseBLEClient+PeripheralDelegate.swift")
source = peripheral.read_text()
guard = """    guard notificationCharacteristicIDs.contains(characteristic.uuid) else {
      return false
    }

"""
dispatch = """    // Historical data is delivered on fd4b0005. Keep this channel on the
    // sync path while an offload is active; do not move every sensor stream
    // onto the main queue.
    if isHistoricalSyncing,
       characteristic.uuid == CBUUID(string: "fd4b0005-cce1-4033-93ce-002d5875f58a") {
      return true
    }

"""
if dispatch not in source:
    if source.count(guard) != 1:
        raise SystemExit("Could not uniquely locate notification dispatch guard.")
    source = source.replace(guard, guard + dispatch, 1)
peripheral.write_text(source)

# A notification can end mid-frame. Keep a bounded, per-characteristic
# reassembly buffer, and clear it at the start of each history transfer.
client = one("GooseBLEClient.swift")
client_source = client.read_text()
property_anchor = "  var notificationSideEffectSkipCount = 0\n"
if "var v5NotificationFrameBuffers: [CBUUID: Data]" not in client_source:
    if client_source.count(property_anchor) != 1:
        raise SystemExit("Could not uniquely locate BLE client state insertion point.")
    client_source = client_source.replace(
        property_anchor,
        property_anchor + "  var v5NotificationFrameBuffers: [CBUUID: Data] = [:]\n",
        1,
    )
# Edge also scans the 16-bit WHOOP SIG member UUID. Some iOS adverts
# expose fd4b rather than the full 128-bit service UUID, so include both forms
# in the scan filter while keeping GATT discovery on the full service UUID.
service_anchor = """  let whoopServices = [
    CBUUID(string: "fd4b0001-cce1-4033-93ce-002d5875f58a"),
    CBUUID(string: "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"),
  ]
"""
service_replacement = """  let whoopServices = [
    CBUUID(string: "fd4b0001-cce1-4033-93ce-002d5875f58a"),
    CBUUID(string: "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"),
    CBUUID(string: "fd4b"),
  ]
"""
if service_replacement not in client_source:
    if client_source.count(service_anchor) != 1:
        raise SystemExit("Could not uniquely locate WHOOP scan service filters.")
    client_source = client_source.replace(service_anchor, service_replacement, 1)
client.write_text(client_source)

parsing = one("GooseBLEClient+Parsing.swift")
parsing_source = parsing.read_text()
method_marker = "  func v5Frames(in data: Data, characteristic: CBUUID) -> [Data] {"
if method_marker not in parsing_source:
    static_anchor = "  static func v5Frames(in data: Data) -> [Data] {"
    if parsing_source.count(static_anchor) != 1:
        raise SystemExit("Could not uniquely locate V5 frame parser.")
    method = """  func v5Frames(in data: Data, characteristic: CBUUID) -> [Data] {
    // WHOOP 5 frame: AA | revision(01) | declared length LE | direction |
    // CRC16-Modbus(header[0..<6]) | padded inner payload | CRC32(payload).
    // declared length includes padded payload plus the trailing CRC32.
    var bytes = Array(v5NotificationFrameBuffers[characteristic] ?? Data())
    bytes.append(contentsOf: data)
    var frames: [Data] = []

    func resync() {
      if let next = bytes.dropFirst().firstIndex(of: 0xaa) {
        bytes.removeFirst(next)
      } else {
        bytes.removeAll(keepingCapacity: true)
      }
    }

    while true {
      guard let startIndex = bytes.firstIndex(of: 0xaa) else {
        bytes.removeAll(keepingCapacity: true)
        break
      }
      if startIndex > 0 {
        bytes.removeFirst(startIndex)
      }
      guard bytes.count >= 8 else { break }

      // Header CRC protects the declared length; never trust length before it.
      let headerCRC = UInt16(bytes[6]) | (UInt16(bytes[7]) << 8)
      guard bytes[1] == 0x01,
            Self.crc16Modbus(Array(bytes[0..<6])) == headerCRC else {
        resync()
        continue
      }

      let declaredLength = Int(UInt16(bytes[2]) | (UInt16(bytes[3]) << 8))
      let expectedLength = declaredLength + 8
      guard declaredLength >= 4, expectedLength <= 4096 else {
        resync()
        continue
      }
      guard bytes.count >= expectedLength else { break }

      let payloadEnd = expectedLength - 4
      let storedCRC = UInt32(bytes[payloadEnd])
        | (UInt32(bytes[payloadEnd + 1]) << 8)
        | (UInt32(bytes[payloadEnd + 2]) << 16)
        | (UInt32(bytes[payloadEnd + 3]) << 24)
      let payload = Array(bytes[8..<payloadEnd])
      guard Self.crc32(payload) == storedCRC else {
        // A bad payload CRC means the declared length may itself be corrupt.
        // Resync from the next SOF rather than swallowing later good frames.
        resync()
        continue
      }

      frames.append(Data(bytes[0..<expectedLength]))
      bytes.removeFirst(expectedLength)
      // Edge's length-based reassembler skips null inter-record padding.
      while bytes.first == 0x00 {
        bytes.removeFirst()
      }
    }

    if bytes.count <= 4096 {
      v5NotificationFrameBuffers[characteristic] = Data(bytes)
    } else {
      v5NotificationFrameBuffers.removeValue(forKey: characteristic)
    }
    return frames
  }

"""
    parsing_source = parsing_source.replace(static_anchor, method + static_anchor, 1)
parsing.write_text(parsing_source)

historical = one("GooseBLEClient+HistoricalHandlers.swift")
historical_source = historical.read_text()
old = "    for frame in Self.v5Frames(in: value) {"
new = "    for frame in v5Frames(in: value, characteristic: characteristic.uuid) {"
if new not in historical_source:
    if historical_source.count(old) < 1:
        raise SystemExit("Could not locate historical sync frame loop.")
    historical_source = historical_source.replace(old, new, 1)
historical.write_text(historical_source)

commands = one("GooseBLEClient+HistoricalCommands.swift")
commands_source = commands.read_text()
start_anchor = """    historicalPacketsReceivedThisSync = 0
"""
if "v5NotificationFrameBuffers.removeAll()" not in commands_source:
    if commands_source.count(start_anchor) != 1:
        raise SystemExit("Could not uniquely locate historical sync reset point.")
    commands_source = commands_source.replace(
        start_anchor,
        start_anchor + "    v5NotificationFrameBuffers.removeAll()\n",
        1,
    )

# Never let the band trim an uncommitted history burst. Goose currently counts
# historical packets and emits signal diagnostics, but has no atomic durable
# record+token commit path comparable to Edge's DrainController. Keep ACK off
# until that storage path exists and commits before HISTORY_END is answered.
ack_anchor = "    acknowledgeHistoricalDataResult: Bool = true\n"
ack_replacement = """    // Safety: no HISTORY_END ACK until the historical records have been
    // durably committed. An ACK can advance the strap's trim cursor.
    acknowledgeHistoricalDataResult: Bool = false
"""
if ack_replacement not in commands_source:
    if commands_source.count(ack_anchor) != 1:
        raise SystemExit("Could not uniquely locate historical ACK default.")
    commands_source = commands_source.replace(ack_anchor, ack_replacement, 1)

commands.write_text(commands_source)

# The shared Rust notification parser also needs Edge-style deframing:
# validate the header before trusting its length, and resync after a bad payload
# CRC so one damaged frame cannot swallow the next valid BLE notification.
protocol_file = one("protocol.rs")
protocol_source = protocol_file.read_text()
feed_anchor = """    pub fn feed(&mut self, chunk: &[u8]) -> DeframeResult {
        self.buffer.extend_from_slice(chunk);
        let mut frames = Vec::new();
        let mut dropped = self.drop_until_frame_start();

        loop {
            let Some(expected_len) = self.device_type.expected_frame_len(&self.buffer) else {
                break;
            };
            if self.buffer.len() < expected_len {
                break;
            }
            frames.push(self.buffer[..expected_len].to_vec());
            self.buffer.drain(..expected_len);
            dropped += self.drop_until_frame_start();
        }

        DeframeResult {
            frames,
            buffered_len: self.buffer.len(),
            dropped_prefix_len: dropped,
        }
    }
"""
feed_replacement = """    pub fn feed(&mut self, chunk: &[u8]) -> DeframeResult {
        self.buffer.extend_from_slice(chunk);
        let mut frames = Vec::new();
        let mut dropped = self.drop_until_frame_start();
        let header_len = self.device_type.header_len();

        loop {
            if self.buffer.len() < header_len {
                break;
            }

            // Header CRC protects the declared length. Do not let a damaged
            // length consume a large portion of the next valid frame.
            if !self.header_crc_valid() {
                self.buffer.drain(..1);
                dropped += 1 + self.drop_until_frame_start();
                continue;
            }

            let Some(expected_len) = self.device_type.expected_frame_len(&self.buffer) else {
                break;
            };
            if expected_len < header_len + 4 || expected_len > 4096 {
                self.buffer.drain(..1);
                dropped += 1 + self.drop_until_frame_start();
                continue;
            }
            if self.buffer.len() < expected_len {
                break;
            }

            let frame = self.buffer[..expected_len].to_vec();
            frames.push(frame.clone());
            if !Self::payload_crc_valid(&frame, header_len) {
                // Keep the corrupt frame visible to diagnostics, but search
                // from the next byte for a fresh SOF instead of trusting its
                // length and swallowing subsequent good frames.
                self.buffer.drain(..1);
                dropped += 1 + self.drop_until_frame_start();
                continue;
            }

            self.buffer.drain(..expected_len);
            dropped += self.drop_until_frame_start();
        }

        DeframeResult {
            frames,
            buffered_len: self.buffer.len(),
            dropped_prefix_len: dropped,
        }
    }

    fn header_crc_valid(&self) -> bool {
        match self.device_type {
            DeviceType::Gen4 => {
                self.buffer.len() >= 4
                    && crc8(&self.buffer[1..3]) == self.buffer[3]
            }
            DeviceType::Maverick | DeviceType::Puffin | DeviceType::Goose => {
                self.buffer.len() >= 8
                    && crc16_modbus(&self.buffer[..6])
                        == u16::from_le_bytes([self.buffer[6], self.buffer[7]])
            }
        }
    }

    fn payload_crc_valid(frame: &[u8], header_len: usize) -> bool {
        if frame.len() < header_len + 4 {
            return false;
        }
        let payload_end = frame.len() - 4;
        let expected = u32::from_le_bytes([
            frame[payload_end],
            frame[payload_end + 1],
            frame[payload_end + 2],
            frame[payload_end + 3],
        ]);
        crc32fast::hash(&frame[header_len..payload_end]) == expected
    }
"""
if feed_replacement not in protocol_source:
    if protocol_source.count(feed_anchor) != 1:
        raise SystemExit("Could not uniquely locate Rust FrameAccumulator.feed.")
    protocol_source = protocol_source.replace(feed_anchor, feed_replacement, 1)
protocol_file.write_text(protocol_source)

tests = one("protocol_tests.rs")
tests_source = tests.read_text()
test_anchor = """#[test]
fn payload_crc_mismatch_preserves_parseable_header_with_warning() {
"""
test = """#[test]
fn deframer_resynchronizes_after_bad_payload_crc_and_keeps_next_frame() {
    let mut corrupt = hex::decode(GET_HELLO_FRAME).unwrap();
    let last = corrupt.len() - 1;
    corrupt[last] ^= 0xff;
    let good = hex::decode(GET_HELLO_FRAME).unwrap();
    let mut stream = corrupt;
    stream.extend_from_slice(&good);

    let mut accumulator = FrameAccumulator::new(DeviceType::Goose);
    let result = accumulator.feed(&stream);

    assert_eq!(result.frames.len(), 2);
    assert!(!parse_frame(DeviceType::Goose, &result.frames[0]).unwrap().payload_crc_valid);
    assert_eq!(result.frames[1], good);
    assert_eq!(result.buffered_len, 0);
}

#[test]
fn deframer_rejects_bad_header_crc_without_losing_next_frame() {
    let mut corrupt = hex::decode(GET_HELLO_FRAME).unwrap();
    corrupt[6] ^= 0xff;
    let good = hex::decode(GET_HELLO_FRAME).unwrap();
    let mut stream = corrupt;
    stream.extend_from_slice(&good);

    let mut accumulator = FrameAccumulator::new(DeviceType::Goose);
    let result = accumulator.feed(&stream);

    assert_eq!(result.frames, vec![good]);
    assert_eq!(result.buffered_len, 0);
}

"""
if test not in tests_source:
    if tests_source.count(test_anchor) != 1:
        raise SystemExit("Could not uniquely locate Rust protocol test insertion point.")
    tests_source = tests_source.replace(test_anchor, test + test_anchor, 1)
tests.write_text(tests_source)

# Match Edge's Gen5 bootstrap order: register the three required notify
# channels first, then send GET_HELLO, validate its correlated response, and
# only then permit history/sensor commands.
client = one("GooseBLEClient.swift")
client_source = client.read_text()
hello_state_anchor = "  var clientHelloSentForCurrentConnection = false\n"
hello_state_replacement = hello_state_anchor + """  var clientHelloResponseConfirmedForCurrentConnection = false
  var clientHelloResponseTimeoutWorkItem: DispatchWorkItem?
"""
if "var clientHelloResponseConfirmedForCurrentConnection = false" not in client_source:
    if client_source.count(hello_state_anchor) != 1:
        raise SystemExit("Could not uniquely locate client hello state.")
    client_source = client_source.replace(hello_state_anchor, hello_state_replacement, 1)
client.write_text(client_source)

commands_file = one("GooseBLEClient+Commands.swift")
commands_source = commands_file.read_text()
discovery_anchor = """    if commandCharacteristic != nil {
      updateConnectionState("ready")
      sendClientHelloIfNeeded(reason: cached ? "cached_gatt" : "gatt_discovery")
      scheduleDebugSkinTemperatureCommandIfNeeded(reason: cached ? "cached_ready" : "ready")
      scheduleAutomaticHistoricalSyncIfNeeded()
      scheduleAutomaticPhysiologyCaptureIfNeeded()
    } else if connectionState == "discovering" {
      updateConnectionState("connected")
    }
"""
discovery_replacement = """    if supportsV5HistoricalSync {
      updateConnectionState("subscribing")
      finishV5GattSetupIfNotificationsReady(
        peripheral,
        reason: cached ? "cached_notify_state" : "notify_state"
      )
    } else if commandCharacteristic != nil {
      updateConnectionState("ready")
      sendClientHelloIfNeeded(reason: cached ? "cached_gatt" : "gatt_discovery")
      scheduleDebugSkinTemperatureCommandIfNeeded(reason: cached ? "cached_ready" : "ready")
      scheduleAutomaticHistoricalSyncIfNeeded()
      scheduleAutomaticPhysiologyCaptureIfNeeded()
    } else if connectionState == "discovering" {
      updateConnectionState("connected")
    }
"""
if discovery_replacement not in commands_source:
    if commands_source.count(discovery_anchor) != 1:
        raise SystemExit("Could not uniquely locate GATT ready transition.")
    commands_source = commands_source.replace(discovery_anchor, discovery_replacement, 1)

helper_anchor = """  func scheduleAutomaticPhysiologyCaptureIfNeeded() {
"""
helper = """  func finishV5GattSetupIfNotificationsReady(_ peripheral: CBPeripheral, reason: String) {
    guard supportsV5HistoricalSync,
          activePeripheral?.identifier == peripheral.identifier else {
      return
    }
    let requiredIDs = [
      CBUUID(string: "fd4b0003-cce1-4033-93ce-002d5875f58a"),
      CBUUID(string: "fd4b0004-cce1-4033-93ce-002d5875f58a"),
      CBUUID(string: "fd4b0005-cce1-4033-93ce-002d5875f58a"),
    ]
    let discovered = peripheral.services?.flatMap { $0.characteristics ?? [] } ?? []
    let missing = requiredIDs.filter { id in
      !discovered.contains(where: { $0.uuid == id })
    }
    guard missing.isEmpty else {
      updateConnectionState("incomplete WHOOP GATT")
      record(
        level: .error,
        source: "ble",
        title: "gatt.required_notification_missing",
        body: missing.map(\\.uuidString).joined(separator: ",")
      )
      return
    }
    let pending = requiredIDs.filter { id in
      discovered.first(where: { $0.uuid == id })?.isNotifying != true
    }
    guard pending.isEmpty else {
      updateConnectionState("subscribing")
      return
    }

    updateConnectionState("ready")
    record(
      source: "ble",
      title: "gatt.required_notifications.ready",
      body: "cmd_from,events,data reason=\\(reason)"
    )
    sendClientHelloIfNeeded(reason: "required_notifications_ready")
  }

"""
if helper not in commands_source:
    if commands_source.count(helper_anchor) != 1:
        raise SystemExit("Could not locate GATT helper insertion point.")
    commands_source = commands_source.replace(helper_anchor, helper + helper_anchor, 1)

commands_source = commands_source.replace(
    "          supportsV5SensorCommands else {",
    "          supportsV5SensorCommands,\n          clientHelloResponseConfirmedForCurrentConnection else {",
    1,
)
commands_source = commands_source.replace(
    "          supportsV5HistoricalSync,\n          !isHistoricalSyncing else {",
    "          supportsV5HistoricalSync,\n          clientHelloResponseConfirmedForCurrentConnection,\n          !isHistoricalSyncing else {",
    1,
)
commands_file.write_text(commands_source)

# Only mark the Gen5 GATT link ready after all three required subscriptions
# have actually completed; a failed CCC registration blocks the handshake.
peripheral = one("GooseBLEClient+PeripheralDelegate.swift")
peripheral_source = peripheral.read_text()
notify_state_anchor = """    if let error {
      record(level: .error, source: "ble", title: "notify.failed", body: "\\(characteristic.uuid.uuidString) \\(error.localizedDescription)")
    } else {
      let state = characteristic.isNotifying ? "subscribed" : "unsubscribed"
      record(source: "ble", title: "notify.state", body: "\\(characteristic.uuid.uuidString) \\(state)")
    }
"""
notify_state_replacement = notify_state_anchor + """    if supportsV5HistoricalSync {
      let requiredIDs: Set<CBUUID> = [
        CBUUID(string: "fd4b0003-cce1-4033-93ce-002d5875f58a"),
        CBUUID(string: "fd4b0004-cce1-4033-93ce-002d5875f58a"),
        CBUUID(string: "fd4b0005-cce1-4033-93ce-002d5875f58a"),
      ]
      if let error, requiredIDs.contains(characteristic.uuid) {
        updateConnectionState("notify failed")
        return
      }
      finishV5GattSetupIfNotificationsReady(peripheral, reason: "notify_confirmed")
    }
"""
if notify_state_replacement not in peripheral_source:
    if peripheral_source.count(notify_state_anchor) != 1:
        raise SystemExit("Could not uniquely locate notification-state callback.")
    peripheral_source = peripheral_source.replace(notify_state_anchor, notify_state_replacement, 1)

# During the hello exchange, command-response notifications are low volume and
# must reach the main-owned handshake state machine even if split across chunks.
dispatch_anchor = """    guard notificationCharacteristicIDs.contains(characteristic.uuid) else {
      return false
    }

"""
dispatch_add = """    if clientHelloSentForCurrentConnection,
       !clientHelloResponseConfirmedForCurrentConnection,
       characteristic.uuid == CBUUID(string: "fd4b0003-cce1-4033-93ce-002d5875f58a") {
      return true
    }

"""
if dispatch_add not in peripheral_source:
    if peripheral_source.count(dispatch_anchor) != 1:
        raise SystemExit("Could not locate notification dispatch guard for hello.")
    peripheral_source = peripheral_source.replace(dispatch_anchor, dispatch_anchor + dispatch_add, 1)

value_anchor = """    handleDebugCommandValue(value, characteristic: characteristic)
    handleHistoricalSyncValue(value, characteristic: characteristic)
"""
value_replacement = """    handleDebugCommandValue(value, characteristic: characteristic)
    handleClientHelloResponseValue(value, characteristic: characteristic)
    handleHistoricalSyncValue(value, characteristic: characteristic)
"""
if value_replacement not in peripheral_source:
    if peripheral_source.count(value_anchor) != 1:
        raise SystemExit("Could not locate notification handler sequence.")
    peripheral_source = peripheral_source.replace(value_anchor, value_replacement, 1)
peripheral.write_text(peripheral_source)

user_actions = one("GooseBLEClient+UserActions.swift")
user_source = user_actions.read_text()
hello_write_anchor = """    clientHelloSentForCurrentConnection = true
    record(
      source: "ble",
      title: "hello.sent",
"""
hello_write_replacement = """    clientHelloResponseConfirmedForCurrentConnection = false
    clientHelloResponseTimeoutWorkItem?.cancel()
    clientHelloSentForCurrentConnection = true
    record(
      source: "ble",
      title: "hello.sent",
"""
if hello_write_replacement not in user_source:
    if user_source.count(hello_write_anchor) != 1:
        raise SystemExit("Could not locate client hello write completion.")
    user_source = user_source.replace(hello_write_anchor, hello_write_replacement, 1)
timeout_anchor = """    record(
      source: "ble",
      title: "hello.sent",
      body: "reason=\\(reason) \\(commandCharacteristic.uuid.uuidString) \\(writeTypeName(writeType)) \\(GooseHello.clientHelloFrameHex)"
    )
"""
timeout_replacement = timeout_anchor + """    let timeout = DispatchWorkItem { [weak self] in
      guard let self,
            self.clientHelloSentForCurrentConnection,
            !self.clientHelloResponseConfirmedForCurrentConnection else {
        return
      }
      self.record(
        level: .warn,
        source: "ble",
        title: "hello.response.timeout",
        body: "GET_HELLO 0x91 did not return a confirmed WHOOP 5 response"
      )
    }
    clientHelloResponseTimeoutWorkItem = timeout
    DispatchQueue.main.asyncAfter(deadline: .now() + 10, execute: timeout)
"""
if timeout_replacement not in user_source:
    if user_source.count(timeout_anchor) != 1:
        raise SystemExit("Could not locate hello timeout insertion point.")
    user_source = user_source.replace(timeout_anchor, timeout_replacement, 1)
user_actions.write_text(user_source)

# The Gen5 GET_HELLO response is command-response 0x24, opcode 0x91, echoed
# request sequence 1, status 1. Its 104-byte body contains the model discriminator.
historical = one("GooseBLEClient+HistoricalHandlers.swift")
historical_source = historical.read_text()
handler_anchor = """  func handleHistoricalSyncValue(_ value: Data, characteristic: CBCharacteristic) {
"""
hello_handler = """  func handleClientHelloResponseValue(_ value: Data, characteristic: CBCharacteristic) {
    guard supportsV5HistoricalSync,
          clientHelloSentForCurrentConnection,
          !clientHelloResponseConfirmedForCurrentConnection,
          characteristic.uuid == CBUUID(string: "fd4b0003-cce1-4033-93ce-002d5875f58a") else {
      return
    }

    for frame in v5Frames(in: value, characteristic: characteristic.uuid) {
      guard let payload = Self.v5Payload(in: frame),
            payload.count >= 5,
            payload[0] == V5PacketType.commandResponse,
            payload[2] == 145,
            payload[3] == 1 else {
        continue
      }

      let status = payload[4]
      if status == 2 {
        record(level: .debug, source: "ble", title: "hello.response.pending", body: "GET_HELLO seq=1")
        continue
      }
      clientHelloResponseTimeoutWorkItem?.cancel()
      clientHelloResponseTimeoutWorkItem = nil
      guard status == 1 else {
        record(level: .error, source: "ble", title: "hello.response.rejected", body: "GET_HELLO status=\\(status)")
        updateConnectionState("hello failed")
        return
      }

      let body = Array(payload.dropFirst(5))
      guard body.count >= 104, body[0] == 1 else {
        record(level: .error, source: "ble", title: "hello.response.invalid", body: "revision/body length invalid bytes=\\(body.count)")
        updateConnectionState("hello invalid")
        return
      }
      let optical = UInt32(body[87])
        | (UInt32(body[88]) << 8)
        | (UInt32(body[89]) << 16)
        | (UInt32(body[90]) << 24)
      guard optical >= 48, optical < 86 else {
        record(level: .error, source: "ble", title: "hello.response.wrong_device", body: "optical_discriminator=\\(optical), expected WHOOP 5.0")
        updateConnectionState("unsupported WHOOP model")
        return
      }

      clientHelloResponseConfirmedForCurrentConnection = true
      let firmware = "\\(body[91]).\\(body[92]).\\(body[93])"
      record(
        source: "ble",
        title: "hello.response.confirmed",
        body: "GET_HELLO seq=1 optical=\\(optical) firmware=\\(firmware)"
      )
      scheduleAutomaticHistoricalSyncIfNeeded()
      scheduleAutomaticPhysiologyCaptureIfNeeded()
      scheduleDebugSkinTemperatureCommandIfNeeded(reason: "hello_confirmed")
      return
    }
  }

"""
if hello_handler not in historical_source:
    if historical_source.count(handler_anchor) != 1:
        raise SystemExit("Could not locate historical handler insertion point.")
    historical_source = historical_source.replace(handler_anchor, hello_handler + handler_anchor, 1)
historical.write_text(historical_source)

# Manual sync is also blocked until the same verified hello response is received.
commands = one("GooseBLEClient+HistoricalCommands.swift")
commands_source = commands.read_text()
sync_guard_anchor = """    guard supportsV5HistoricalSync else {
      let characteristic = commandCharacteristic?.uuid.uuidString ?? "missing"
      failHistoricalSync("Historical sync currently supports the Goose V5 fd4b command characteristic. Active command characteristic: \\(characteristic).")
      return
    }

"""
sync_guard_replacement = sync_guard_anchor + """    guard clientHelloResponseConfirmedForCurrentConnection else {
      failHistoricalSync("Historical sync is waiting for a confirmed WHOOP 5 GET_HELLO response.")
      return
    }

"""
if sync_guard_replacement not in commands_source:
    if commands_source.count(sync_guard_anchor) != 1:
        raise SystemExit("Could not locate historical sync hello gate.")
    commands_source = commands_source.replace(sync_guard_anchor, sync_guard_replacement, 1)
commands.write_text(commands_source)

# Other commands and debug writes must not race the Gen5 bootstrap.
parsing = one("GooseBLEClient.swift")
parsing_source = parsing.read_text()
parsing_source = parsing_source.replace(
    "    canSendHello && !isHistoricalSyncing && supportsV5HistoricalSync",
    "    canSendHello && clientHelloResponseConfirmedForCurrentConnection && !isHistoricalSyncing && supportsV5HistoricalSync",
    1,
)
parsing_source = parsing_source.replace(
    "    canSendHello && !isHistoricalSyncing && supportsV5SensorCommands",
    "    canSendHello && clientHelloResponseConfirmedForCurrentConnection && !isHistoricalSyncing && supportsV5SensorCommands",
    1,
)
parsing_source = parsing_source.replace(
    "    canSendHello && !isHistoricalSyncing && supportsV5AlarmCommands",
    "    canSendHello && clientHelloResponseConfirmedForCurrentConnection && !isHistoricalSyncing && supportsV5AlarmCommands",
    1,
)
parsing_source = parsing_source.replace(
    "    canSendHello\n      && !isHistoricalSyncing",
    "    canSendHello\n      && clientHelloResponseConfirmedForCurrentConnection\n      && !isHistoricalSyncing",
    1,
)
parsing.write_text(parsing_source)

debug_file = one("GooseBLEClient+DebugAndSync.swift")
debug_source = debug_file.read_text()
debug_guard = "    guard autoSendDebugSkinTemperatureCommand, !debugSkinTemperatureCommandSent else {\n"
debug_new = "    guard clientHelloResponseConfirmedForCurrentConnection, autoSendDebugSkinTemperatureCommand, !debugSkinTemperatureCommandSent else {\n"
if debug_new not in debug_source:
    if debug_source.count(debug_guard) < 1:
        raise SystemExit("Could not locate debug command scheduling guard.")
    debug_source = debug_source.replace(debug_guard, debug_new)
debug_file.write_text(debug_source)

print("Applied WHOOP 5.0 data-channel dispatch and CRC-validated incremental frame reassembly.")
