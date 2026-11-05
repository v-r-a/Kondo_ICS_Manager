import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from ics_manager.eeprom import Eeprom, load_image, save_image, FIELDS, FLAGS
from ics_manager.transport import SerialSettings, SerialTransport, CommunicationError, ReplyTimeout
from ics_manager.controller import Controller
from ics_manager.demo import DemoSerial

class ScriptedSerial:
    """Independent fixed-byte fixtures, deliberately not using demo codec."""
    def __init__(self, rx, fragment=999, short_write=False):
        self.answer, self.fragment = rx, fragment
        self.rx = bytearray()
        self.timeout = 0.05
        self.baudrate = 115200
        self.short_write = short_write
        self.writes = []
        self.events = []
    @property
    def in_waiting(self): return len(self.rx)
    def reset_input_buffer(self): self.events.append('reset'); self.rx.clear()
    def write(self, tx):
        self.events.append('write'); self.writes.append(bytes(tx)); self.rx.extend(self.answer)
        return len(tx) - 1 if self.short_write else len(tx)
    def flush(self): self.events.append('drain')
    def read(self, count):
        n = min(count, self.fragment)
        out = bytes(self.rx[:n]); del self.rx[:n]; return out
    def close(self): pass

def scripted(rx, echo=True, **kwargs):
    serial = ScriptedSerial(rx, **kwargs)
    t = SerialTransport(SerialSettings(echo=echo, pre_tx_delay=0), serial_instance=serial)
    return Controller(t, sleeper=lambda x: None), serial

class GoldenWireTests(unittest.TestCase):
    def test_neutral_position_id12(self):
        c, s = scripted(bytes.fromhex('8c 3a 4c 0c 3a 4c'), fragment=1)
        self.assertEqual(c.set_position(12, 7500), 7500)
        self.assertEqual(s.writes, [bytes.fromhex('8c 3a 4c')])
        self.assertEqual(s.events, ['reset', 'write', 'drain'])

    def test_free(self):
        c, s = scripted(bytes.fromhex('80 00 00 00 3a 4c'))
        self.assertEqual(c.free(0), 7500)
        self.assertEqual(s.writes[0], bytes.fromhex('80 00 00'))

    def test_read_position_36_has_four_reply_bytes(self):
        c, s = scripted(bytes.fromhex('aa 05 2a 05 3a 4c'), fragment=2)
        self.assertEqual(c.read_parameter(10, 5), 7500)
        self.assertEqual(s.writes[0], bytes.fromhex('aa 05'))

    def test_read_stretch(self):
        c, s = scripted(bytes.fromhex('a0 01 20 01 1e'))
        self.assertEqual(c.read_parameter(0, 1), 30)

    def test_ram_speed_write(self):
        c, s = scripted(bytes.fromhex('ca 02 64 4a 02 64'))
        self.assertEqual(c.write_parameter(10, 2, 100), 100)
        self.assertEqual(s.writes[0], bytes.fromhex('ca 02 64'))

    def test_id_has_unmasked_header(self):
        c, s = scripted(bytes.fromhex('ff 00 00 00 f9'))
        self.assertEqual(c.get_id(), 25)
        self.assertEqual(s.writes[0], bytes.fromhex('ff 00 00 00'))

    def test_get_id31_with_echo_does_not_confuse_ff_reply(self):
        c, _ = scripted(bytes.fromhex('ff 00 00 00 ff'), fragment=1)
        self.assertEqual(c.get_id(), 31)

    def test_no_echo_is_explicit(self):
        c, _ = scripted(bytes.fromhex('20 01 1e'), echo=False)
        self.assertEqual(c.read_parameter(0, 1), 30)

    def test_echo_only_is_not_success(self):
        c, _ = scripted(bytes.fromhex('a0 01'))
        with self.assertRaises(ReplyTimeout): c.read_parameter(0, 1)

    def test_echo_mismatch(self):
        c, _ = scripted(bytes.fromhex('a1 01 20 01 1e'))
        with self.assertRaisesRegex(CommunicationError, 'Echo mismatch'): c.read_parameter(0, 1)

    def test_wrong_id_or_subcommand_rejected(self):
        for answer in ('a0 01 21 01 1e', 'a0 01 20 02 1e'):
            c, _ = scripted(bytes.fromhex(answer))
            with self.assertRaises(CommunicationError): c.read_parameter(0, 1)

    def test_short_read_and_short_write(self):
        c, _ = scripted(bytes.fromhex('a0 01 20 01'))
        with self.assertRaises(ReplyTimeout): c.read_parameter(0, 1)
        c, _ = scripted(b'', short_write=True)
        with self.assertRaisesRegex(CommunicationError, 'Short write'): c.read_parameter(0, 1)

    def test_invalid_parameter_data(self):
        c, _ = scripted(bytes.fromhex('a0 01 20 01 9e'))
        with self.assertRaises(CommunicationError): c.read_parameter(0, 1)

    def test_id_invalid_header(self):
        c, _ = scripted(bytes.fromhex('ff 00 00 00 19'))
        with self.assertRaises(CommunicationError): c.get_id()

    def test_no_unrequested_retries(self):
        c, s = scripted(b'')
        with self.assertRaises(ReplyTimeout): c.set_position(0, 7500)
        self.assertEqual(len(s.writes), 1)
        self.assertEqual(c.transport.errors, 1)

    def test_stale_buffer_cleared_before_tx_only(self):
        c, s = scripted(bytes.fromhex('a0 01 20 01 1e'))
        s.rx.extend(b'junk')
        self.assertEqual(c.read_parameter(0, 1), 30)
        self.assertEqual(s.events.count('reset'), 1)

class EepromTests(unittest.TestCase):
    def test_stretch_scaling_and_sign(self):
        im = Eeprom.demo()
        im.set('stretch', 30)
        self.assertEqual(im.data[2:4], bytes([3, 12]))
        for val, raw in [(10, 246), (-10, 10), (127, 129), (-127, 127), (0, 0)]:
            im.set('user_offset', val)
            self.assertEqual(im.raw(52), raw)
            self.assertEqual(im.get('user_offset'), val)

    def test_word_nibbles(self):
        im = Eeprom.demo()
        self.assertEqual(im.data[16:20], bytes.fromhex('02 0c 0e 0c'))
        self.assertEqual(im.data[20:24], bytes.fromhex('00 0d 0a 0c'))
        self.assertEqual(im.get('limit_cw'), 11500)
        self.assertEqual(im.get('limit_ccw'), 3500)

    def test_baud_codes(self):
        im = Eeprom.demo()
        for baud, code in [(115200, 10), (625000, 1), (1250000, 0)]:
            out = im.updated({}, baud, im.flags)
            self.assertEqual(out.data[26:28], bytes([0, code]))
            self.assertEqual(out.baud, baud)

    def test_all_user_fields_ranges_roundtrip(self):
        for f in FIELDS:
            for value in (f.minimum, f.maximum, f.default):
                im = Eeprom.demo(); im.set(f.key, value)
                self.assertEqual(im.get(f.key), value)
            with self.assertRaises(ValueError): im.set(f.key, f.maximum + 1)

    def test_preserve_calibration_id_and_reserved(self):
        im = Eeprom.demo()
        im.put(14, 2, 0x66)
        im.put(56, 2, 17)
        original = bytes(im.data)
        out = im.updated({'speed': 80}, 625000, FLAGS['reverse'] | FLAGS['serial'])
        for i in range(64):
            if i not in (4, 5, 14, 15, 26, 27): self.assertEqual(out.data[i], im.data[i])
        self.assertEqual(out.flags, 0x6F)
        self.assertEqual(bytes(im.data), original)

    def test_preset_255_never_sent(self):
        im = Eeprom([255] * 64)
        with self.assertRaises(ValueError): im.updated({'speed': 10}, 115200, 8)

    def test_signature_required(self):
        im = Eeprom([0] * 64)
        with self.assertRaises(ValueError): im.validate_live()

    def test_file_roundtrip(self):
        im = Eeprom.demo()
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in ('.sdt', '.json'):
                p = Path(tmp) / ('settings' + suffix)
                save_image(p, im)
                self.assertEqual(load_image(p).data, im.data)

    def test_sdt_unknown_bytes_preserved_offline(self):
        im = Eeprom([255] * 64); im.set('speed', 90)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'preset.sdt'; save_image(p, im)
            restored = load_image(p)
            self.assertEqual(restored.data, im.data)
            self.assertEqual(restored.values(), {'speed': 90})

class WorkflowTests(unittest.TestCase):
    def make(self, fake=None):
        s = fake or DemoSerial()
        t = SerialTransport(SerialSettings(pre_tx_delay=0), serial_instance=s)
        return Controller(t, sleeper=lambda x: None), s

    def test_write_readback_backup_and_baud_change(self):
        c, s = self.make()
        before = bytes(s.image.data)
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(0, {'speed': 90, 'user_offset': -20}, 625000, 8, tmp)
            self.assertTrue(result.verified, result.message)
            self.assertEqual(load_image(result.backup).data, bytearray(before))
            self.assertEqual(result.image.get('speed'), 90)
            self.assertEqual(result.image.get('user_offset'), -20)
            self.assertEqual(s.baudrate, 625000)
            writes = [tx for tx in s.writes if len(tx) == 66]
            self.assertEqual(len(writes), 1)
            self.assertTrue(all(x < 16 for x in writes[0][2:]))
            self.assertEqual(s.writes[0], b'\xa0\0')

    def test_bad_read_prevents_eeprom_write(self):
        s = DemoSerial(); s.image.data[0] = 0
        c, s = self.make(s)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError): c.write_config(0, {'speed': 90}, 115200, 8, tmp)
        self.assertFalse(any(len(tx) == 66 for tx in s.writes))

    def test_missing_ack_still_readback_no_retry(self):
        class MissingAck(DemoSerial):
            def write(self, tx):
                n = super().write(tx)
                if len(tx) == 66: del self.rx[66:]
                return n
        c, s = self.make(MissingAck())
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(0, {'speed': 90}, 115200, 8, tmp)
        self.assertTrue(result.verified)
        self.assertIn('acknowledgement', result.message)
        self.assertEqual(sum(len(tx) == 66 for tx in s.writes), 1)

    def test_one_byte_ack_observed_manager_tolerated_via_readback(self):
        class OneByteAck(DemoSerial):
            def write(self, tx):
                n = super().write(tx)
                if len(tx) == 66: del self.rx[-1:]
                return n
        c, s = self.make(OneByteAck())
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(0, {'speed': 91}, 115200, 8, tmp)
        self.assertTrue(result.verified)

    def test_mismatched_readback_is_not_success(self):
        class Corrupt(DemoSerial):
            def write(self, tx):
                n = super().write(tx)
                if len(tx) == 66: self.image.put(34, 4, 39000)
                return n
        c, s = self.make(Corrupt())
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(0, {'speed': 90}, 115200, 8, tmp)
        self.assertFalse(result.verified)
        self.assertIn('mismatch', result.message)

    def test_no_readback_is_not_success(self):
        class Vanishes(DemoSerial):
            def write(self, tx):
                n = super().write(tx)
                if len(tx) == 66: self.servo_baud = 9999
                return n
        c, s = self.make(Vanishes())
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(0, {'speed': 90}, 115200, 8, tmp)
        self.assertFalse(result.verified)
        self.assertEqual(sum(len(tx) == 66 for tx in s.writes), 1)

    def test_backup_failure_prevents_write(self):
        c, s = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'file'; path.write_text('not a folder')
            with self.assertRaises(OSError): c.write_config(0, {'speed': 90}, 115200, 8, path)
        self.assertFalse(any(len(tx) == 66 for tx in s.writes))

    def test_no_changes_does_not_wear_eeprom(self):
        c, s = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(0, {}, 115200, s.image.flags, tmp)
        self.assertTrue(result.verified)
        self.assertFalse(any(len(tx) == 66 for tx in s.writes))

    def test_id_write_then_get_confirmation(self):
        c, s = self.make()
        self.assertEqual(c.set_id(25, current_id=0, detected_ids={0}, isolated=True), 25)
        self.assertEqual(s.writes[-2:], [bytes.fromhex('f9 01 01 01'), bytes.fromhex('ff 00 00 00')])
        self.assertTrue(all(tx[0] & 0xE0 == 0xA0 for tx in s.writes[:-2]))

    def test_autodetect_at_all_bauds(self):
        for baud in (115200, 625000, 1250000):
            c, s = self.make(DemoSerial(servo_id=31, baud=baud))
            actual_baud, actual_id, _ = c.discover()
            self.assertEqual((actual_baud, actual_id), (baud, 31))

    def test_error_does_not_lock_transport(self):
        c, s = self.make()
        with self.assertRaises(ReplyTimeout): c.read_parameter(1, 1)
        self.assertEqual(c.read_parameter(0, 1), 30)

class PtyTests(unittest.TestCase):
    def test_actual_pyserial_reads_fragmented_delayed_reply(self):
        """A PTY tests buffering/deadlines, but does NOT emulate UART parity/USB."""
        import pty, serial
        master, slave = pty.openpty()
        path = os.ttyname(slave)
        # PTYs cannot preserve all real UART flags; production opens 8E1.
        ser = serial.Serial(path, baudrate=115200, timeout=0.1)
        transport = SerialTransport(SerialSettings(port=path, timeout=0.15, pre_tx_delay=0), serial_instance=ser)
        got = []
        def remote():
            command = bytearray()
            while len(command) < 2: command.extend(os.read(master, 2 - len(command)))
            got.append(bytes(command))
            for part in (b'\xa0', b'\x01', b'\x20', b'\x01\x1e'):
                os.write(master, part)
                time.sleep(0.008)
        worker = threading.Thread(target=remote, daemon=True)
        worker.start()
        try:
            self.assertEqual(Controller(transport).read_parameter(0, 1), 30)
            worker.join(1)
            self.assertEqual(got, [b'\xa0\x01'])
        finally:
            transport.close(); os.close(slave); os.close(master)

if __name__ == '__main__': unittest.main()
