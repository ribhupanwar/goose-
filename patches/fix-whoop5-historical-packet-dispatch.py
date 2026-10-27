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
  var clientHelloBootstrapScheduledForCurrentConnection = false
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

    guard !clientHelloBootstrapScheduledForCurrentConnection else {
      return
    }
    clientHelloBootstrapScheduledForCurrentConnection = true
    updateConnectionState("settling")
    record(
      source: "ble",
      title: "gatt.required_notifications.ready",
      body: "cmd_from,events,data reason=\\(reason); waiting 500ms before GET_HELLO"
    )
    // Edge's Gen5 bootstrap waits 500ms after the final CCC registration
    // before its mandatory GET_HELLO exchange.
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self, weak peripheral] in
      guard let self, let peripheral,
            self.activePeripheral?.identifier == peripheral.identifier else {
        return
      }
      let current = peripheral.services?.flatMap { $0.characteristics ?? [] } ?? []
      let stillReady = requiredIDs.allSatisfy { id in
        current.first(where: { $0.uuid == id })?.isNotifying == true
      }
      guard stillReady else {
        self.clientHelloBootstrapScheduledForCurrentConnection = false
        self.updateConnectionState("subscribing")
        return
      }
      self.updateConnectionState("ready")
      self.sendClientHelloIfNeeded(reason: "required_notifications_ready")
    }
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


# The app's PRIMARY notification ingest path has its own reassembler. Harden
# that path too (not only the historical counter path): validate the header
# before trusting the length, and resync after a bad payload CRC while still
# returning the corrupt frame for Rust diagnostics.
pipeline = one("GooseAppModel+NotificationPipeline.swift")
pipeline_source = pipeline.read_text()
function_anchor = "  func gooseFrames(in data: Data, event: GooseNotificationEvent) -> FrameReassemblyResult {"
next_anchor = "  func frameReassemblyKey(for event: GooseNotificationEvent) -> String {"
start = pipeline_source.find(function_anchor)
end = pipeline_source.find(next_anchor, start + len(function_anchor))
if start < 0 or end < 0:
    raise SystemExit("Could not uniquely locate the app notification reassembler.")
replacement = """  static func notificationHeaderCRCValid(_ bytes: [UInt8], deviceType: String) -> Bool {
    if deviceType == "GEN4" {
      guard bytes.count >= 4 else { return false }
      var crc: UInt8 = 0
      for byte in bytes[1..<3] {
        crc ^= byte
        for _ in 0..<8 {
          let shifted = UInt16(crc) << 1
          crc = UInt8(((crc & 0x80) != 0 ? shifted ^ 0x07 : shifted) & 0xff)
        }
      }
      return crc == bytes[3]
    }
    guard bytes.count >= 8 else { return false }
    let stored = UInt16(bytes[6]) | (UInt16(bytes[7]) << 8)
    return GooseBLEClient.crc16Modbus(Array(bytes[0..<6])) == stored
  }

  static func notificationPayloadCRCValid(_ frame: [UInt8], headerLength: Int) -> Bool {
    guard frame.count >= headerLength + 4 else { return false }
    let payloadEnd = frame.count - 4
    let stored = UInt32(frame[payloadEnd])
      | (UInt32(frame[payloadEnd + 1]) << 8)
      | (UInt32(frame[payloadEnd + 2]) << 16)
      | (UInt32(frame[payloadEnd + 3]) << 24)
    return GooseBLEClient.crc32(Array(frame[headerLength..<payloadEnd])) == stored
  }

  func gooseFrames(in data: Data, event: GooseNotificationEvent) -> FrameReassemblyResult {
    let key = frameReassemblyKey(for: event)
    let hadBufferedData = frameReassemblyBuffers[key]?.isEmpty == false
    var bytes = Array(frameReassemblyBuffers[key] ?? Data())
    bytes.append(contentsOf: data)
    var frames: [Data] = []
    var droppedBytes = 0
    var expectedBytes: Int?
    let headerLength = event.rustDeviceType == "GEN4" ? 4 : 8

    func resync() -> Bool {
      if let next = bytes.dropFirst().firstIndex(of: 0xaa) {
        droppedBytes += next
        bytes.removeFirst(next)
        return true
      }
      droppedBytes += bytes.count
      bytes.removeAll(keepingCapacity: true)
      return false
    }

    while bytes.count >= headerLength + 4 {
      guard bytes.first == 0xaa else {
        if !resync() { break }
        continue
      }
      guard Self.notificationHeaderCRCValid(bytes, deviceType: event.rustDeviceType) else {
        if !resync() { break }
        continue
      }

      let declaredLength: Int
      if event.rustDeviceType == "GEN4" {
        declaredLength = Int(bytes[1]) | (Int(bytes[2]) << 8)
      } else {
        declaredLength = Int(bytes[2]) | (Int(bytes[3]) << 8)
      }
      let frameLength = headerLength + declaredLength
      guard declaredLength >= 4, frameLength <= 4096 else {
        if !resync() { break }
        continue
      }
      guard bytes.count >= frameLength else {
        expectedBytes = frameLength
        break
      }

      let frame = Array(bytes[0..<frameLength])
      // Keep bad-CRC frames visible to the Rust parser/diagnostics, but do not
      // trust their declared length to consume the next valid frame.
      frames.append(Data(frame))
      guard Self.notificationPayloadCRCValid(frame, headerLength: headerLength) else {
        if !resync() { break }
        continue
      }

      bytes.removeFirst(frameLength)
      while bytes.first == 0x00 {
        bytes.removeFirst()
      }
    }

    if bytes.isEmpty {
      frameReassemblyBuffers.removeValue(forKey: key)
    } else if bytes.first == 0xaa && bytes.count <= Self.maximumBufferedFrameBytes {
      frameReassemblyBuffers[key] = Data(bytes)
    } else {
      droppedBytes += bytes.count
      frameReassemblyBuffers.removeValue(forKey: key)
    }

    return FrameReassemblyResult(
      frames: frames,
      bufferedBytes: frameReassemblyBuffers[key]?.count ?? 0,
      expectedBytes: expectedBytes,
      droppedBytes: droppedBytes,
      usedBufferedData: hadBufferedData
    )
  }

"""
pipeline_source = pipeline_source[:start] + replacement + pipeline_source[end:]
pipeline.write_text(pipeline_source)

# Edge's Gen5 live path includes compact REALTIME_DATA (0x28): unix seconds
# at payload[2..6] and BPM at payload[8]. Goose previously treated packet 40
# like a generic data record, so this common live-HR form never reached the HR
# series pipeline.
protocol_file = one("protocol.rs")
protocol_source = protocol_file.read_text()
summary_anchor = """    NormalHistory {
        hr_present: Option<bool>,
        marker_offset: Option<usize>,
        marker_value: Option<u8>,
    },
"""
summary_replacement = summary_anchor + """    RealtimeCompactHeartRate {
        heart_rate: Option<u8>,
    },
"""
if "RealtimeCompactHeartRate" not in protocol_source:
    if protocol_source.count(summary_anchor) != 1:
        raise SystemExit("Could not locate data packet summary enum.")
    protocol_source = protocol_source.replace(summary_anchor, summary_replacement, 1)

payload_match_anchor = """        PACKET_TYPE_REALTIME_DATA
        | PACKET_TYPE_REALTIME_RAW_DATA
        | PACKET_TYPE_HISTORICAL_DATA
"""
payload_match_replacement = """        PACKET_TYPE_REALTIME_DATA
            if payload.len() < 64 && payload.get(1) == Some(&2) =>
        {
            Some(parse_realtime_compact_hr_payload(payload))
        }
        PACKET_TYPE_REALTIME_DATA
        | PACKET_TYPE_REALTIME_RAW_DATA
        | PACKET_TYPE_HISTORICAL_DATA
"""
if payload_match_replacement not in protocol_source:
    if protocol_source.count(payload_match_anchor) != 1:
        raise SystemExit("Could not locate realtime data payload dispatch.")
    protocol_source = protocol_source.replace(payload_match_anchor, payload_match_replacement, 1)

parse_anchor = """fn parse_data_packet_payload(payload: &[u8]) -> ParsedPayload {
"""
realtime_parser = """fn parse_realtime_compact_hr_payload(payload: &[u8]) -> ParsedPayload {
    let mut warnings = Vec::new();
    if payload.len() < 9 {
        warnings.push("realtime_compact_hr_payload_too_short".to_string());
    }
    if read_u32_le(payload, 2).is_none() {
        warnings.push("realtime_compact_hr_timestamp_missing".to_string());
    }
    let heart_rate = payload.get(8).copied();
    let body_offset = 8.min(payload.len());

    ParsedPayload::DataPacket {
        // The second byte is the live stream/record discriminator. Preserve
        // its actual wire value; the Edge decoder identifies this by packet 40.
        packet_k: payload.get(1).copied(),
        domain: Some("realtime_compact_heart_rate".to_string()),
        status_or_stream: payload.get(1).copied(),
        counter_or_page: None,
        timestamp_seconds: read_u32_le(payload, 2),
        timestamp_subseconds: None,
        hr_marker_offset: Some(8),
        hr_present_marker: heart_rate,
        body_offset,
        body_hex: hex::encode(&payload[body_offset..]),
        body_summary: if payload.len() >= 9 {
            Some(DataPacketBodySummary::RealtimeCompactHeartRate { heart_rate })
        } else {
            None
        },
        warnings,
    }
}

"""
if realtime_parser not in protocol_source:
    if protocol_source.count(parse_anchor) != 1:
        raise SystemExit("Could not locate data packet parser insertion point.")
    protocol_source = protocol_source.replace(parse_anchor, realtime_parser + parse_anchor, 1)
protocol_file.write_text(protocol_source)

bridge = one("bridge.rs")
bridge_source = bridge.read_text()
heart_rate_anchor = """                Some(DataPacketBodySummary::RawMotionK10 { heart_rate, .. }) => *heart_rate,
                _ => None,
"""
heart_rate_replacement = """                Some(DataPacketBodySummary::RawMotionK10 { heart_rate, .. }) => *heart_rate,
                Some(DataPacketBodySummary::RealtimeCompactHeartRate { heart_rate }) => *heart_rate,
                _ => None,
"""
if heart_rate_replacement not in bridge_source:
    if bridge_source.count(heart_rate_anchor) != 1:
        raise SystemExit("Could not locate compact bridge heart-rate mapping.")
    bridge_source = bridge_source.replace(heart_rate_anchor, heart_rate_replacement, 1)

body_kind_anchor = """        Some(DataPacketBodySummary::NormalHistory { .. }) => "normal_history",
"""
body_kind_replacement = body_kind_anchor + """        Some(DataPacketBodySummary::RealtimeCompactHeartRate { .. }) => "realtime_compact_heart_rate",
"""
if body_kind_replacement not in bridge_source:
    if bridge_source.count(body_kind_anchor) != 1:
        raise SystemExit("Could not locate bridge body summary kind mapping.")
    bridge_source = bridge_source.replace(body_kind_anchor, body_kind_replacement, 1)
bridge.write_text(bridge_source)

publisher = one("GooseAppModel+PacketPublishing.swift")
publisher_source = publisher.read_text()
extract_anchor = """      let body = payload["body_summary"] as? [String: Any],
      body["kind"] as? String == "raw_motion_k10"
"""
extract_replacement = """      let body = payload["body_summary"] as? [String: Any],
      let bodyKind = body["kind"] as? String,
      ["raw_motion_k10", "realtime_compact_heart_rate"].contains(bodyKind)
"""
if extract_replacement not in publisher_source:
    if publisher_source.count(extract_anchor) != 1:
        raise SystemExit("Could not locate live heart-rate extraction gate.")
    publisher_source = publisher_source.replace(extract_anchor, extract_replacement, 1)
publisher.write_text(publisher_source)

tests = one("protocol_tests.rs")
tests_source = tests.read_text()
test_anchor = """#[test]
fn parses_generic_command_response_payload_contract() {
"""
live_test = """#[test]
fn parses_gen5_compact_realtime_heart_rate_packet_0x28() {
    let timestamp = 1_797_000_000u32;
    let mut payload = vec![PACKET_TYPE_REALTIME_DATA, 2];
    payload.extend_from_slice(&timestamp.to_le_bytes());
    payload.extend_from_slice(&[0, 0, 72]);
    let frame = build_v5_payload_frame(&payload);

    let parsed = parse_frame(DeviceType::Goose, &frame).unwrap();
    assert_eq!(parsed.packet_type, Some(PACKET_TYPE_REALTIME_DATA));
    let Some(ParsedPayload::DataPacket {
        packet_k,
        domain,
        timestamp_seconds,
        body_summary,
        ..
    }) = parsed.parsed_payload else {
        panic!("expected compact realtime data packet");
    };
    assert_eq!(packet_k, Some(2));
    assert_eq!(domain.as_deref(), Some("realtime_compact_heart_rate"));
    assert_eq!(timestamp_seconds, Some(timestamp));
    assert_eq!(
        body_summary,
        Some(DataPacketBodySummary::RealtimeCompactHeartRate {
            heart_rate: Some(72),
        })
    );
}

"""
if live_test not in tests_source:
    if tests_source.count(test_anchor) != 1:
        raise SystemExit("Could not locate Rust protocol test insertion point for live HR.")
    tests_source = tests_source.replace(test_anchor, live_test + test_anchor, 1)
tests.write_text(tests_source)

# Feed the compact live HR into the same persisted signal/metric paths as
# R10, and keep exhaustive Rust summary/export matches aware of this variant.
correlation = one("capture_correlation.rs")
correlation_source = correlation.read_text()
correlation_anchor = """        DataPacketBodySummary::NormalHistory { .. } => "normal_history",
"""
correlation_replacement = correlation_anchor + """        DataPacketBodySummary::RealtimeCompactHeartRate { .. } => "realtime_compact_heart_rate",
"""
if correlation_replacement not in correlation_source:
    if correlation_source.count(correlation_anchor) != 1:
        raise SystemExit("Could not locate capture correlation summary mapping.")
    correlation_source = correlation_source.replace(correlation_anchor, correlation_replacement, 1)
correlation.write_text(correlation_source)

metrics = one("metric_features.rs")
metrics_source = metrics.read_text()
metric_anchor = """        DataPacketBodySummary::RawMotionK10 {
            heart_rate: Some(heart_rate),
            ..
        } => Some(HeartRatePlan {
"""
metric_variant = """        DataPacketBodySummary::RealtimeCompactHeartRate {
            heart_rate: Some(heart_rate),
        } => Some(HeartRatePlan {
            body_summary_kind: "realtime_compact_heart_rate",
            source_signal: "realtime_compact_heart_rate",
            quality_flag: "preliminary_realtime_compact_heart_rate",
            marker_offset: 8,
            marker_value: heart_rate,
            device_timestamp_seconds: timestamp_seconds,
            device_timestamp_subseconds: timestamp_subseconds,
        }),
"""
if metric_variant not in metrics_source:
    if metrics_source.count(metric_anchor) != 1:
        raise SystemExit("Could not locate metric heart-rate plan insertion point.")
    metrics_source = metrics_source.replace(metric_anchor, metric_variant + metric_anchor, 1)
metrics.write_text(metrics_source)

export_file = one("export.rs")
export_source = export_file.read_text()
export_anchor = """            DataPacketBodySummary::R17OpticalOrLabradorFiltered {
                samples, warnings, ..
            } => {
"""
export_variant = """            DataPacketBodySummary::RealtimeCompactHeartRate { heart_rate } => {
                if let Some(value) = heart_rate.filter(|value| *value > 0) {
                    rows.push(sensor_u8_sample(
                        &context,
                        "realtime_compact_heart_rate",
                        "live_compact_heart_rate",
                        0,
                        8,
                        value,
                        "bpm_candidate",
                        vec!["raw_live_hr".to_string()],
                    ));
                }
            }
"""
if export_variant not in export_source:
    if export_source.count(export_anchor) != 1:
        raise SystemExit("Could not locate sensor export match insertion point.")
    export_source = export_source.replace(export_anchor, export_variant + export_anchor, 1)
export_file.write_text(export_source)

# Decode the Gen5 v18 historical HR/RR slots from Edge's confirmed offsets:
# HR inner[14], RR count inner[15], up to four signed RR values at 16/18/20/22.
# HR is persisted now; RR stays raw until contiguous beat-time HRV is implemented.
bridge = one("bridge.rs")
bridge_source = bridge.read_text()
heart_rate_map_anchor = """                Some(DataPacketBodySummary::RealtimeCompactHeartRate { heart_rate }) => *heart_rate,
                _ => None,
"""
heart_rate_map_replacement = """                Some(DataPacketBodySummary::RealtimeCompactHeartRate { heart_rate }) => *heart_rate,
                Some(DataPacketBodySummary::NormalHistory { marker_offset: Some(14), marker_value: Some(value), .. })
                    if parsed.device_type == DeviceType::Goose && *packet_k == Some(18) && (25..=230).contains(value) => Some(*value),
                _ => None,
"""
if heart_rate_map_replacement not in bridge_source:
    if bridge_source.count(heart_rate_map_anchor) != 1:
        raise SystemExit("Could not locate compact Gen5 history heart-rate mapping.")
    bridge_source = bridge_source.replace(heart_rate_map_anchor, heart_rate_map_replacement, 1)

rr_helper = """fn compact_gen5_history_rr_intervals(
    parsed: &ParsedFrame,
    packet_k: Option<u8>,
    body_hex: &str,
) -> Vec<u16> {
    if parsed.device_type != DeviceType::Goose
        || parsed.packet_type != Some(PACKET_TYPE_HISTORICAL_DATA)
        || packet_k != Some(18)
    {
        return Vec::new();
    }
    let Ok(bytes) = hex::decode(body_hex) else {
        return Vec::new();
    };
    let Some(declared_count) = bytes.get(2).copied() else {
        return Vec::new();
    };
    if declared_count > 4 {
        return Vec::new();
    }

    let mut accepted = Vec::new();
    for index in 0..usize::from(declared_count) {
        let offset = 3 + index * 2;
        let Some(pair) = bytes.get(offset..offset + 2) else {
            break;
        };
        let value = i16::from_le_bytes([pair[0], pair[1]]);
        if (200..=2500).contains(&value) {
            accepted.push(value as u16);
        }
    }
    accepted
}

"""
if rr_helper not in bridge_source:
    bridge_source = bridge_source.replace("fn compact_parsed_frame_summary(parsed: &ParsedFrame) -> serde_json::Value {", rr_helper + "fn compact_parsed_frame_summary(parsed: &ParsedFrame) -> serde_json::Value {", 1)

rr_insert_anchor = """            let movement = compact_k10_movement_summary(body_summary.as_ref());
            json!({
"""
rr_insert = """            let movement = compact_k10_movement_summary(body_summary.as_ref());
            let rr_intervals_ms = compact_gen5_history_rr_intervals(parsed, *packet_k, body_hex);
            json!({
"""
if rr_insert not in bridge_source:
    if bridge_source.count(rr_insert_anchor) != 1:
        raise SystemExit("Could not locate compact RR summary insertion point.")
    bridge_source = bridge_source.replace(rr_insert_anchor, rr_insert, 1)
rr_json_anchor = """                "heart_rate": heart_rate,
                "movement": movement,
"""
rr_json_replacement = """                "heart_rate": heart_rate,
                "rr_intervals_ms": rr_intervals_ms,
                "movement": movement,
"""
if rr_json_replacement not in bridge_source:
    if bridge_source.count(rr_json_anchor) != 1:
        raise SystemExit("Could not locate compact RR JSON fields.")
    bridge_source = bridge_source.replace(rr_json_anchor, rr_json_replacement, 1)
bridge.write_text(bridge_source)

# Surface compact RR arrays and distinguish historical samples from live HR so
# historical backfill uses strap timestamps and never overwrites the live HR tile.
parsing = one("NotificationFrameParsing.swift")
parsing_source = parsing.read_text()
compact_field_anchor = """  let heartRateBPM: Int?
  let r17Flags: Int?
"""
compact_field_replacement = """  let heartRateBPM: Int?
  let rrIntervalsMs: [Int]
  let r17Flags: Int?
"""
if compact_field_replacement not in parsing_source:
    if parsing_source.count(compact_field_anchor) != 1:
        raise SystemExit("Could not locate compact HR summary fields.")
    parsing_source = parsing_source.replace(compact_field_anchor, compact_field_replacement, 1)
compact_init_anchor = """    heartRateBPM = NotificationFrameParser.intValue(raw["heart_rate"])
    r17Flags = NotificationFrameParser.intValue(raw["r17_flags"])
"""
compact_init_replacement = """    heartRateBPM = NotificationFrameParser.intValue(raw["heart_rate"])
    rrIntervalsMs = (raw["rr_intervals_ms"] as? [Any])?.compactMap { NotificationFrameParser.intValue($0) } ?? []
    r17Flags = NotificationFrameParser.intValue(raw["r17_flags"])
"""
if compact_init_replacement not in parsing_source:
    if parsing_source.count(compact_init_anchor) != 1:
        raise SystemExit("Could not locate compact RR initializer.")
    parsing_source = parsing_source.replace(compact_init_anchor, compact_init_replacement, 1)
interpretation_fields_anchor = """  let heartRateBPM: Int?
  let movementSample: MovementPacketSample?
"""
interpretation_fields_replacement = """  let heartRateBPM: Int?
  let heartRateSource: String?
  let heartRateTimestamp: Date?
  let rrIntervalsMs: [Int]
  let movementSample: MovementPacketSample?
"""
if interpretation_fields_replacement not in parsing_source:
    if parsing_source.count(interpretation_fields_anchor) != 1:
        raise SystemExit("Could not locate interpretation HR fields.")
    parsing_source = parsing_source.replace(interpretation_fields_anchor, interpretation_fields_replacement, 1)
parsing.write_text(parsing_source)

publisher = one("GooseAppModel+PacketPublishing.swift")
publisher_source = publisher.read_text()
extract_signature_anchor = """  static func extractHeartRate(from parsed: [String: Any]) -> Int? {
"""
extract_signature_replacement = """  static func extractHeartRate(from parsed: [String: Any], deviceType: String) -> Int? {
"""
if extract_signature_replacement not in publisher_source:
    if publisher_source.count(extract_signature_anchor) != 1:
        raise SystemExit("Could not locate heart-rate extractor signature.")
    publisher_source = publisher_source.replace(extract_signature_anchor, extract_signature_replacement, 1)
extract_body_anchor = """      let bodyKind = body["kind"] as? String,
      ["raw_motion_k10", "realtime_compact_heart_rate"].contains(bodyKind)
    else {
      return nil
    }
    return intValue(body["heart_rate"])
"""
extract_body_replacement = """      let bodyKind = body["kind"] as? String
    else {
      return nil
    }
    if ["raw_motion_k10", "realtime_compact_heart_rate"].contains(bodyKind) {
      return intValue(body["heart_rate"])
    }
    if deviceType == "GOOSE",
       bodyKind == "normal_history",
       intValue(payload["packet_k"]) == 18,
       intValue(body["marker_offset"]) == 14,
       let value = intValue(body["marker_value"]),
       (25...230).contains(value) {
      return value
    }
    return nil
"""
if extract_body_replacement not in publisher_source:
    if publisher_source.count(extract_body_anchor) != 1:
        raise SystemExit("Could not locate heart-rate extractor body.")
    publisher_source = publisher_source.replace(extract_body_anchor, extract_body_replacement, 1)
publisher.write_text(publisher_source)

pipeline = one("GooseAppModel+NotificationPipeline.swift")
pipeline_source = pipeline.read_text()
error_init_anchor = """        heartRateBPM: nil,
        movementSample: nil,
"""
error_init_replacement = """        heartRateBPM: nil,
        heartRateSource: nil,
        heartRateTimestamp: nil,
        rrIntervalsMs: [],
        movementSample: nil,
"""
if error_init_replacement not in pipeline_source:
    if pipeline_source.count(error_init_anchor) != 1:
        raise SystemExit("Could not locate failed interpretation initializer.")
    pipeline_source = pipeline_source.replace(error_init_anchor, error_init_replacement, 1)

success_init_anchor = """      heartRateBPM: compact?.heartRateBPM ?? parsed.flatMap(extractHeartRate),
      movementSample: extractMovementPacket(
"""
success_init_replacement = """      heartRateBPM: compact?.heartRateBPM ?? parsed.flatMap { extractHeartRate(from: $0, deviceType: event.rustDeviceType) },
      heartRateSource: {
        let packetType = compact?.packetType ?? parsed.flatMap { intValue($0["packet_type"]) }
        let packetK = compact?.packetK ?? (parsed?["parsed_payload"] as? [String: Any]).flatMap { intValue($0["packet_k"]) }
        if event.rustDeviceType == "GOOSE", packetType == 47, packetK == 18 {
          return "whoop.gen5.history.v18"
        }
        if event.rustDeviceType == "GOOSE", packetType == 40 {
          return "whoop.gen5.live.0x28"
        }
        return "rust.k10"
      }(),
      heartRateTimestamp: {
        let packetType = compact?.packetType ?? parsed.flatMap { intValue($0["packet_type"]) }
        let packetK = compact?.packetK ?? (parsed?["parsed_payload"] as? [String: Any]).flatMap { intValue($0["packet_k"]) }
        guard event.rustDeviceType == "GOOSE", packetType == 47, packetK == 18,
              let seconds = compact?.timestampSeconds
                ?? (parsed?["parsed_payload"] as? [String: Any]).flatMap({ intValue($0["timestamp_seconds"]) }) else {
          return nil
        }
        let subseconds = compact?.timestampSubseconds
          ?? (parsed?["parsed_payload"] as? [String: Any]).flatMap({ intValue($0["timestamp_subseconds"]) })
          ?? 0
        return Date(timeIntervalSince1970: Double(seconds) + Double(subseconds) / 32768.0)
      }(),
      rrIntervalsMs: {
        let packetType = compact?.packetType ?? parsed.flatMap { intValue($0["packet_type"]) }
        let packetK = compact?.packetK ?? (parsed?["parsed_payload"] as? [String: Any]).flatMap { intValue($0["packet_k"]) }
        guard event.rustDeviceType == "GOOSE", packetType == 47, packetK == 18 else { return [] }
        return compact?.rrIntervalsMs ?? []
      }(),
      movementSample: extractMovementPacket(
"""
if success_init_replacement not in pipeline_source:
    if pipeline_source.count(success_init_anchor) != 1:
        raise SystemExit("Could not locate successful interpretation initializer.")
    pipeline_source = pipeline_source.replace(success_init_anchor, success_init_replacement, 1)

heart_rate_handler_anchor = """    if let bpm = interpretation.heartRateBPM {
      ble.recordLiveHeartRate(bpm, source: "rust.k10", at: event.capturedAt)
      recordDeviceSignalPoint(
        family: "HR",
        value: "\\(bpm) bpm",
        detail: "raw_motion_k10 embedded heart-rate byte",
        capturedAt: event.capturedAt,
        minimumInterval: 1
      )
    }
"""
heart_rate_handler_replacement = """    if let bpm = interpretation.heartRateBPM {
      if interpretation.heartRateSource == "whoop.gen5.history.v18" {
        let capturedAt = interpretation.heartRateTimestamp ?? event.capturedAt
        HeartRateSeriesStore.shared.appendHistorical(
          bpm: bpm,
          source: "whoop.gen5.history.v18",
          capturedAt: capturedAt
        )
      } else {
        let source = interpretation.heartRateSource ?? "rust.k10"
        ble.recordLiveHeartRate(bpm, source: source, at: event.capturedAt)
        recordDeviceSignalPoint(
          family: "HR",
          value: "\\(bpm) bpm",
          detail: source == "whoop.gen5.live.0x28" ? "WHOOP 5 compact live HR" : "raw motion embedded heart-rate byte",
          capturedAt: event.capturedAt,
          minimumInterval: 1
        )
      }
    }
"""
if heart_rate_handler_replacement not in pipeline_source:
    if pipeline_source.count(heart_rate_handler_anchor) != 1:
        raise SystemExit("Could not locate parsed heart-rate persistence handler.")
    pipeline_source = pipeline_source.replace(heart_rate_handler_anchor, heart_rate_handler_replacement, 1)

# Do not turn one-second RR fragments into RMSSD values. Edge computes
# HRV from contiguous, timestamped beat series over longer windows; until
# Goose has that same beat-time substrate, keep these RR values raw only.
pipeline.write_text(pipeline_source)

# The compact bridge needs the protocol packet constant for the Gen5 v18
# historical branch.
bridge = one("bridge.rs")
bridge_source = bridge.read_text()
import_anchor = """        DataPacketBodySummary, DeviceType, I16SeriesSummary, ParsedFrame, ParsedPayload,
        parse_frame_hex,
"""
import_replacement = """        DataPacketBodySummary, DeviceType, I16SeriesSummary, ParsedFrame, ParsedPayload,
        PACKET_TYPE_HISTORICAL_DATA, parse_frame_hex,
"""
if import_replacement not in bridge_source:
    if bridge_source.count(import_anchor) != 1:
        raise SystemExit("Could not locate bridge protocol imports.")
    bridge_source = bridge_source.replace(import_anchor, import_replacement, 1)
bridge.write_text(bridge_source)

# Historical backfill arrives out of chronological order relative to live
# samples. Keep the persisted HR/HRV series sorted and deduplicated by stable
# sample ID so an unacknowledged re-offer cannot duplicate a whole history day.
stores = one("HeartRateSeriesStores.swift")
stores_source = stores.read_text()
stores_source = stores_source.replace(
    "  private static let retention: TimeInterval = 7 * 24 * 60 * 60",
    "  private static let retention: TimeInterval = 14 * 24 * 60 * 60",
    1,
)
hr_samples_anchor = """  private var samples: [HeartRateSamplePoint]
  private var pendingWrite: DispatchWorkItem?
"""
hr_samples_replacement = """  private var samples: [HeartRateSamplePoint]
  private var sampleIDs: Set<String> = []
  private var pendingWrite: DispatchWorkItem?
"""
if hr_samples_replacement not in stores_source:
    if stores_source.count(hr_samples_anchor) != 1:
        raise SystemExit("Could not locate heart-rate store sample state.")
    stores_source = stores_source.replace(hr_samples_anchor, hr_samples_replacement, 1)

hr_init_anchor = """    self.samples = Self.loadSamples(from: url)
    prune(relativeTo: Date())
"""
hr_init_replacement = """    self.samples = Self.loadSamples(from: url)
    prune(relativeTo: Date())
    sampleIDs = Set(samples.map(\\.id))
"""
if stores_source.count(hr_init_anchor) < 1:
    raise SystemExit("Could not locate heart-rate store initializer.")
stores_source = stores_source.replace(hr_init_anchor, hr_init_replacement, 1)

hr_append_anchor = """    samples.append(HeartRateSamplePoint(bpm: bpm, source: source, capturedAt: capturedAt))
    prune(relativeTo: capturedAt)
"""
hr_append_replacement = """    let sample = HeartRateSamplePoint(bpm: bpm, source: source, capturedAt: capturedAt)
    guard !sampleIDs.contains(sample.id) else {
      stateLock.unlock()
      return false
    }
    samples.append(sample)
    sampleIDs.insert(sample.id)
    let countBeforePrune = samples.count
    prune(relativeTo: capturedAt)
    if samples.count != countBeforePrune {
      sampleIDs = Set(samples.map(\\.id))
    }
"""
if stores_source.count(hr_append_anchor) != 1:
    raise SystemExit("Could not uniquely locate live HR store append.")
stores_source = stores_source.replace(hr_append_anchor, hr_append_replacement, 1)

hr_historical = """  func appendHistorical(bpm: Int, source: String, capturedAt: Date) -> Bool {
    guard (20...240).contains(bpm) else { return false }
    stateLock.lock()
    let sample = HeartRateSamplePoint(bpm: bpm, source: source, capturedAt: capturedAt)
    guard !sampleIDs.contains(sample.id) else {
      stateLock.unlock()
      return false
    }
    let insertionIndex = samples.firstIndex(where: { $0.capturedAt > capturedAt }) ?? samples.endIndex
    samples.insert(sample, at: insertionIndex)
    sampleIDs.insert(sample.id)
    let countBeforePrune = samples.count
    prune(relativeTo: Date())
    if samples.count != countBeforePrune {
      sampleIDs = Set(samples.map(\\.id))
    }
    schedulePersist()
    let shouldPostUpdate = markUpdateNotificationIfNeeded()
    stateLock.unlock()
    if shouldPostUpdate {
      NotificationCenter.default.post(name: Self.didUpdateNotification, object: self)
    }
    return true
  }

"""
if hr_historical not in stores_source:
    hr_insert_anchor = """  func hourlyRanges(forDayContaining date: Date = Date(), calendar: Calendar = .current) -> [HeartRateHourlyRange] {
"""
    if stores_source.count(hr_insert_anchor) != 1:
        raise SystemExit("Could not locate historical HR insertion point.")
    stores_source = stores_source.replace(hr_insert_anchor, hr_historical + hr_insert_anchor, 1)

# The second sample store is HRVSeriesStore.
hrv_samples_anchor = """  private var samples: [HRVSamplePoint]
  private var pendingWrite: DispatchWorkItem?
"""
hrv_samples_replacement = """  private var samples: [HRVSamplePoint]
  private var sampleIDs: Set<String> = []
  private var pendingWrite: DispatchWorkItem?
"""
if hrv_samples_replacement not in stores_source:
    if stores_source.count(hrv_samples_anchor) != 1:
        raise SystemExit("Could not locate HRV store sample state.")
    stores_source = stores_source.replace(hrv_samples_anchor, hrv_samples_replacement, 1)

hrv_init_anchor = """    prune(relativeTo: Date())
  }

  func append(rmssdMS: Double, rrIntervalCount: Int, source: String, capturedAt: Date) -> Bool {
"""
hrv_init_replacement = """    prune(relativeTo: Date())
    sampleIDs = Set(samples.map(\\.id))
  }

  func append(rmssdMS: Double, rrIntervalCount: Int, source: String, capturedAt: Date) -> Bool {
"""
if hrv_init_replacement not in stores_source:
    if stores_source.count(hrv_init_anchor) != 1:
        raise SystemExit("Could not locate HRV store initializer.")
    stores_source = stores_source.replace(hrv_init_anchor, hrv_init_replacement, 1)

hrv_append_anchor = """    samples.append(HRVSamplePoint(rmssdMS: rmssdMS, rrIntervalCount: rrIntervalCount, source: source, capturedAt: capturedAt))
    prune(relativeTo: capturedAt)
"""
hrv_append_replacement = """    let sample = HRVSamplePoint(rmssdMS: rmssdMS, rrIntervalCount: rrIntervalCount, source: source, capturedAt: capturedAt)
    guard !sampleIDs.contains(sample.id) else {
      stateLock.unlock()
      return false
    }
    samples.append(sample)
    sampleIDs.insert(sample.id)
    let countBeforePrune = samples.count
    prune(relativeTo: capturedAt)
    if samples.count != countBeforePrune {
      sampleIDs = Set(samples.map(\\.id))
    }
"""
if stores_source.count(hrv_append_anchor) != 1:
    raise SystemExit("Could not uniquely locate live HRV store append.")
stores_source = stores_source.replace(hrv_append_anchor, hrv_append_replacement, 1)

hrv_historical = """  func appendHistorical(rmssdMS: Double, rrIntervalCount: Int, source: String, capturedAt: Date) -> Bool {
    guard rmssdMS.isFinite, (0...300).contains(rmssdMS), rrIntervalCount >= 2 else { return false }
    stateLock.lock()
    let sample = HRVSamplePoint(rmssdMS: rmssdMS, rrIntervalCount: rrIntervalCount, source: source, capturedAt: capturedAt)
    guard !sampleIDs.contains(sample.id) else {
      stateLock.unlock()
      return false
    }
    let insertionIndex = samples.firstIndex(where: { $0.capturedAt > capturedAt }) ?? samples.endIndex
    samples.insert(sample, at: insertionIndex)
    sampleIDs.insert(sample.id)
    let countBeforePrune = samples.count
    prune(relativeTo: Date())
    if samples.count != countBeforePrune {
      sampleIDs = Set(samples.map(\\.id))
    }
    schedulePersist()
    let shouldPostUpdate = markUpdateNotificationIfNeeded()
    stateLock.unlock()
    if shouldPostUpdate {
      NotificationCenter.default.post(name: Self.didUpdateNotification, object: self)
    }
    return true
  }

"""
if hrv_historical not in stores_source:
    hrv_insert_anchor = """  func dailyEstimate(
"""
    if stores_source.count(hrv_insert_anchor) != 1:
        raise SystemExit("Could not locate historical HRV insertion point.")
    stores_source = stores_source.replace(hrv_insert_anchor, hrv_historical + hrv_insert_anchor, 1)

prune_anchor = """  private func prune(relativeTo date: Date) {
    let cutoff = date.addingTimeInterval(-Self.retention)
    if let firstKept = samples.firstIndex(where: { $0.capturedAt >= cutoff }), firstKept > 0 {
      samples.removeFirst(firstKept)
    } else if samples.allSatisfy({ $0.capturedAt < cutoff }) {
      samples.removeAll()
    }
    if samples.count > Self.maxSamples {
      samples.removeFirst(samples.count - Self.maxSamples)
    }
  }
"""
prune_replacement = """  private func prune(relativeTo date: Date) {
    let originalCount = samples.count
    let cutoff = date.addingTimeInterval(-Self.retention)
    if let firstKept = samples.firstIndex(where: { $0.capturedAt >= cutoff }), firstKept > 0 {
      samples.removeFirst(firstKept)
    } else if samples.allSatisfy({ $0.capturedAt < cutoff }) {
      samples.removeAll()
    }
    if samples.count > Self.maxSamples {
      samples.removeFirst(samples.count - Self.maxSamples)
    }
    if samples.count != originalCount {
      sampleIDs = Set(samples.map(\\.id))
    }
  }
"""
if prune_replacement not in stores_source:
    if stores_source.count(prune_anchor) != 2:
        raise SystemExit("Expected both HR and HRV prune methods.")
    stores_source = stores_source.replace(prune_anchor, prune_replacement)
stores.write_text(stores_source)

# Edge defines 0x28 exclusively as compact live HR. K21 grouped motion
# records belong to REALTIME_RAW_DATA (0x2B), not 0x28; correct the two old
# Goose fixtures that conflated those packet types.
tests = one("protocol_tests.rs")
tests_source = tests.read_text()
tests_source = tests_source.replace(
    "payload[0] = PACKET_TYPE_REALTIME_DATA;",
    "payload[0] = PACKET_TYPE_REALTIME_RAW_DATA;",
)
tests.write_text(tests_source)

# Exercise the exact Edge v18 offsets through the compact bridge contract,
# not only the lower-level packet parser.
bridge = one("bridge.rs")
bridge_source = bridge.read_text()
bridge_test_anchor = """    #[test]
    fn capture_arrival_next_focus_includes_recovery_sensor_capture_actions() {
"""
bridge_test = """    #[test]
    fn compact_summary_extracts_gen5_v18_hr_and_rr_intervals() {
        let timestamp = 1_797_000_000u32;
        let mut payload = vec![PACKET_TYPE_HISTORICAL_DATA, 18, 0];
        payload.extend_from_slice(&1u32.to_le_bytes());
        payload.extend_from_slice(&timestamp.to_le_bytes());
        payload.extend_from_slice(&0u16.to_le_bytes());
        payload.extend_from_slice(&[0, 150, 4]);
        for rr in [800i16, 810, 805, 795] {
            payload.extend_from_slice(&rr.to_le_bytes());
        }

        let frame = crate::protocol::build_v5_payload_frame(&payload);
        let parsed = crate::protocol::parse_frame(DeviceType::Goose, &frame).unwrap();
        let compact = compact_parsed_frame_summary(&parsed);

        assert_eq!(compact["heart_rate"].as_u64(), Some(150));
        let rr: Vec<u16> = serde_json::from_value(compact["rr_intervals_ms"].clone()).unwrap();
        assert_eq!(rr, vec![800, 810, 805, 795]);
    }

"""
if bridge_test not in bridge_source:
    if bridge_source.count(bridge_test_anchor) != 1:
        raise SystemExit("Could not locate bridge unit test insertion point.")
    bridge_source = bridge_source.replace(bridge_test_anchor, bridge_test + bridge_test_anchor, 1)
bridge.write_text(bridge_source)

print("Applied WHOOP 5.0 data-channel dispatch and CRC-validated incremental frame reassembly.")
