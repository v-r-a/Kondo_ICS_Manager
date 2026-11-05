"""ICS commands plus conservative EEPROM read/modify/write/readback workflow."""
from dataclasses import dataclass
from pathlib import Path
import time
from datetime import datetime, timezone
from .eeprom import Eeprom, FLAGS, integer, save_image
from .transport import CommunicationError, ReplyTimeout

BAUD_RATES = (115200, 625000, 1250000)

@dataclass
class Motor:
    servo_id: int
    baud: int
    image: Eeprom

    @property
    def key(self):
        return self.baud, self.servo_id

@dataclass
class ScanResult:
    motors: list
    scanned_bauds: tuple
    issues: list

@dataclass
class WriteResult:
    image: Eeprom
    verified: bool
    backup: str
    message: str

class Controller:
    def __init__(self, transport, *, sleeper=time.sleep):
        self.transport = transport
        self.sleep = sleeper

    def _id(self, servo_id):
        return integer(servo_id, 0, 31, 'Servo ID')

    def get_id(self):
        reply = self.transport.exchange(b'\xff\0\0\0', 1)
        if reply[0] & 0xE0 != 0xE0:
            raise CommunicationError(f'Invalid ID reply: {reply.hex()}')
        return reply[0] & 31

    def set_id(self, servo_id, *, current_id, detected_ids, isolated=False):
        """ID programming is broadcast: never use on a connected multi-servo bus.

        The caller must explicitly confirm physical isolation. A scan cannot
        prove isolation (slave-mode devices and duplicate IDs can be invisible).
        """
        servo_id = self._id(servo_id)
        self._id(current_id)
        if servo_id == current_id:
            raise ValueError(f'The selected motor already has ID {servo_id}; nothing written.')
        if servo_id in set(detected_ids):
            raise ValueError(f'ID {servo_id} was already detected on this bus. Choose an unused ID.')
        if not isolated:
            raise ValueError('ID changes are broadcast. Physically isolate the selected motor first.')
        # Recheck immediately before writing, using addressed reads only.
        # No ID-write is sent if another address now responds at any ICS baud.
        old_baud = self.transport.baud
        scan = self.scan_bus(BAUD_RATES)
        if scan.issues:
            raise CommunicationError('Cannot safely check the bus: ' + '; '.join(scan.issues))
        if any(m.servo_id == servo_id for m in scan.motors):
            raise ValueError(f'ID {servo_id} is already present on this bus. Choose an unused ID.')
        if {m.key for m in scan.motors} != {(old_baud, current_id)}:
            raise ValueError('ID change blocked: the scan must find only the selected, physically isolated motor.')
        command = bytes([0xE0 | servo_id, 1, 1, 1])
        self.transport.exchange(command, 1, prefix=command[:1],
                                timeout=self.transport.settings.eeprom_timeout)
        self.sleep(0.5)
        observed = self.get_id()
        if observed != servo_id:
            raise CommunicationError(f'ID write not verified: received {observed}, requested {servo_id}')
        return observed

    def set_position(self, servo_id, value):
        self._id(servo_id)
        if value != 0:
            integer(value, 3500, 11500, 'Position')
        command = bytes([0x80 | servo_id, value >> 7, value & 127])
        reply = self.transport.exchange(command, 3, prefix=bytes([servo_id]))
        if any(x & 0x80 for x in reply[1:]):
            raise CommunicationError('Invalid 7-bit position response')
        return (reply[1] << 7) | reply[2]

    def free(self, servo_id):
        return self.set_position(servo_id, 0)

    def read_parameter(self, servo_id, sc):
        self._id(servo_id)
        integer(sc, 1, 5, 'Read subcommand')
        prefix = bytes([0x20 | servo_id, sc])
        reply = self.transport.exchange(bytes([0xA0 | servo_id, sc]), 4 if sc == 5 else 3, prefix=prefix)
        if any(x & 128 for x in reply[2:]):
            raise CommunicationError('Invalid 7-bit parameter response')
        return ((reply[2] << 7) | reply[3]) if sc == 5 else reply[2]

    def write_parameter(self, servo_id, sc, value):
        self._id(servo_id)
        integer(sc, 1, 4, 'Write subcommand')
        integer(value, 0 if sc == 3 else 1, 63 if sc == 3 else 127, 'Parameter')
        command = bytes([0xC0 | servo_id, sc, value])
        reply = self.transport.exchange(command, 3, prefix=bytes([0x40 | servo_id, sc]))
        if reply[2] != value:
            raise CommunicationError(f'Parameter write reply differs: {reply[2]} != {value}')
        return value

    def read_eeprom(self, servo_id, *, probe=False):
        self._id(servo_id)
        reply = self.transport.exchange(bytes([0xA0 | servo_id, 0]), 66,
                                        prefix=bytes([0x20 | servo_id, 0]), probe=probe)
        image = Eeprom(reply[2:]).validate_live()
        if image.raw(56) != servo_id:
            raise CommunicationError(f'EEPROM ID {image.raw(56)} differs from reply address {servo_id}')
        return image

    def scan_bus(self, bauds=BAUD_RATES, progress=None):
        """Read every address, without position commands or broadcast ID reads.

        Empty addresses time out normally. Malformed replies and unsupported
        host baud rates are reported separately; no motor count is inferred
        from an echo. Always restore the previous host baud, even on failure.
        """
        bauds = tuple(dict.fromkeys(bauds))
        if not bauds or any(b not in BAUD_RATES for b in bauds):
            raise ValueError('Select one or more supported ICS baud rates')
        motors, scanned, issues = [], [], []
        original = self.transport.baud
        try:
            for baud in bauds:
                try:
                    self.transport.set_baud(baud)
                except (OSError, ValueError) as exc:
                    issues.append(f'{baud} baud unavailable: {exc}')
                    continue
                scanned.append(baud)
                for sid in range(32):
                    if progress:
                        progress(baud, sid)
                    try:
                        image = self.read_eeprom(sid, probe=True)
                        motors.append(Motor(sid, baud, image))
                    except ReplyTimeout as exc:
                        # Missing replies are expected at unused addresses.
                        # A partial reply/echo is not evidence of an empty ID.
                        if exc.received or exc.phase == 'echo':
                            issues.append(f'ID {sid} @ {baud}: {exc}')
                    except (CommunicationError, ValueError) as exc:
                        issues.append(f'ID {sid} @ {baud}: {exc}')
        finally:
            self.transport.set_baud(original)
        return ScanResult(motors, tuple(scanned), issues)

    def discover(self):
        """Compatibility helper; discovery now uses addressed reads."""
        scan = self.scan_bus()
        if not scan.motors:
            raise CommunicationError('No responding motors found. ' + '; '.join(scan.issues))
        motor = scan.motors[0]
        self.transport.set_baud(motor.baud)
        return motor.baud, motor.servo_id, motor.image

    def select_motor(self, baud, servo_id):
        self.transport.set_baud(baud)
        image = self.read_eeprom(servo_id)
        return image

    def write_config(self, servo_id, values, baud, flags, backup_dir, *, isolated=False):
        """Read fresh, preserve hidden bytes, persist backup, write ONCE, verify.

        EEPROM writes are addressed and can be used on a multi-servo bus.
        Optional legacy broadcast recovery is restricted to isolated use.
        A missing ACK is ambiguous, never grounds for repeating a write.
        """
        self._id(servo_id)
        before = self.read_eeprom(servo_id)
        target = before.updated(values, baud, flags)
        if not before.differences(target):
            return WriteResult(before, True, '', 'No EEPROM changes requested; nothing written.')
        directory = Path(backup_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        backup = directory / f'ID{servo_id:02d}_{stamp}_before.sdt'
        save_image(backup, before)
        self.transport.log('INFO', f'Pre-write backup: {backup}')
        command = bytes([0xC0 | servo_id, 0]) + bytes(target.data)
        ack_issue = ''
        try:
            # Public protocol specifies R_CMD + SC (2 bytes). Windows consumes
            # just one byte after its 66-byte echo. Readback is authoritative.
            self.transport.exchange(command, 2, prefix=bytes([0x40 | servo_id, 0]),
                                    timeout=self.transport.settings.eeprom_timeout)
        except (CommunicationError, OSError) as exc:
            ack_issue = f'Write acknowledgement unavailable/invalid ({exc}). '
        self.sleep(0.5)
        if self.transport.settings.legacy_recovery and isolated:
            # Observed in MainForm.btnWrite_Click; reason undocumented.
            # This is a single ID READ at 57600, never another EEPROM write.
            try:
                self.transport.set_baud(57600)
                self.transport.exchange(b'\xff\0\0\0', 1, timeout=0.05)
            except (CommunicationError, OSError, ValueError) as exc:
                self.transport.log('INFO', f'Legacy recovery probe: {exc}')
            self.sleep(0.5)
        try:
            self.transport.set_baud(baud)
            actual = self.read_eeprom(servo_id)
        except (CommunicationError, OSError, ValueError) as exc:
            reason = 'Slave mode can suppress replies. ' if flags & FLAGS['slave'] else ''
            return WriteResult(target, False, str(backup), ack_issue + reason +
                f'Write sent once, but readback failed at {baud}: {exc}. '
                'State is unverified. Check baud/power before any further write.')
        # FREE is dynamic; every other nibble, including calibration and ID,
        # must match. A bit becoming different is not silently accepted.
        expected, observed = target.copy(), actual.copy()
        expected.put(14, 2, expected.flags & ~2)
        observed.put(14, 2, observed.flags & ~2)
        changes = expected.differences(observed)
        if changes:
            summary = ', '.join(f'{i}: {a:X}->{b:X}' for i, a, b in changes)
            return WriteResult(actual, False, str(backup), ack_issue + 'Readback mismatch at wire offsets ' + summary)
        return WriteResult(actual, True, str(backup), 'EEPROM write verified by full readback. ' + ack_issue)
