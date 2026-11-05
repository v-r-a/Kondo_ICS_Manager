"""Multi-motor isolation and protocol regression tests; no hardware access."""
import tempfile
import unittest
from ics_manager.controller import Controller, BAUD_RATES
from ics_manager.demo import DemoSerial, DemoBusSerial
from ics_manager.transport import SerialSettings, SerialTransport, CommunicationError


class BusTests(unittest.TestCase):
    def make(self, motors=None, **settings):
        bus = DemoBusSerial(motors=motors)
        transport = SerialTransport(SerialSettings(pre_tx_delay=0, **settings), serial_instance=bus)
        return Controller(transport, sleeper=lambda _: None), bus

    def test_scan_all_32_ids_is_read_only_and_includes_id31(self):
        c, bus = self.make([DemoSerial(servo_id=i) for i in (0, 4, 12, 31)])
        scan = c.scan_bus((115200,))
        self.assertEqual([m.servo_id for m in scan.motors], [0, 4, 12, 31])
        self.assertEqual(bus.writes, [bytes([0xA0 | i, 0]) for i in range(32)])
        self.assertFalse(scan.issues)
        self.assertEqual(c.transport.no_reply, 28)
        self.assertEqual(c.transport.errors, 0)
        self.assertTrue(all(m.free_mode and m.position == 7500 for m in bus.motors))

    def test_mixed_baud_scan_and_host_restoration(self):
        c, bus = self.make([DemoSerial(servo_id=i, baud=b) for i, b in zip((1, 4, 31), BAUD_RATES)])
        c.transport.set_baud(625000)
        scan = c.scan_bus()
        self.assertEqual([m.key for m in scan.motors], [(115200, 1), (625000, 4), (1250000, 31)])
        self.assertEqual(c.transport.baud, 625000)

    def test_empty_bus_is_not_an_echo_detected_motor(self):
        c, _ = self.make([])
        self.assertEqual(c.scan_bus().motors, [])

    def test_scan_without_adapter_echo(self):
        c, bus = self.make(echo=False)
        bus.echo = False
        self.assertEqual([m.servo_id for m in c.scan_bus((115200,)).motors], [0, 4, 12])

    def test_partial_reply_reported_as_issue(self):
        class Partial(DemoSerial):
            def write(self, tx):
                n = super().write(tx)
                if tx == b'\xa0\0': del self.rx[3:]
                return n
        c, _ = self.make([Partial()])
        scan = c.scan_bus((115200,))
        self.assertFalse(scan.motors)
        self.assertEqual(len(scan.issues), 1)

    def test_address_mismatch_not_accepted(self):
        device = DemoSerial(servo_id=4)
        device.image.put(56, 2, 5)
        c, _ = self.make([device])
        scan = c.scan_bus((115200,))
        self.assertFalse(scan.motors)
        self.assertIn('differs', scan.issues[0])

    def test_restore_host_baud_when_scan_aborts(self):
        c, _ = self.make()
        def abort(baud, sid): raise OSError('USB removed')
        with self.assertRaises(OSError): c.scan_bus((625000,), abort)
        self.assertEqual(c.transport.baud, 115200)

    def test_eeprom_write_changes_only_selected_motor(self):
        c, bus = self.make()
        before = [bytes(m.image.data) for m in bus.motors]
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(4, {'speed': 50, 'user_offset': -15}, 115200, 8, tmp)
        self.assertTrue(result.verified)
        self.assertEqual(bytes(bus.motors[0].image.data), before[0])
        self.assertEqual(bytes(bus.motors[2].image.data), before[2])
        self.assertEqual(bus.motors[1].image.get('speed'), 50)
        self.assertEqual(bus.motors[1].image.get('user_offset'), -15)
        self.assertEqual([tx[:2] for tx in bus.writes if len(tx) == 66], [b'\xc4\0'])
        self.assertFalse(any(tx[0] & 0xE0 == 0xE0 for tx in bus.writes))

    def test_bus_write_never_uses_broadcast_recovery_even_if_option_set(self):
        c, bus = self.make(legacy_recovery=True)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(c.write_config(4, {'speed': 51}, 115200, 8, tmp).verified)
        self.assertFalse(any(tx[0] & 0xE0 == 0xE0 for tx in bus.writes))

    def test_selected_baud_change_keeps_other_motors_accessible(self):
        c, bus = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            result = c.write_config(4, {}, 625000, 8, tmp)
        self.assertTrue(result.verified)
        self.assertEqual([m.servo_baud for m in bus.motors], [115200, 625000, 115200])
        self.assertEqual(c.select_motor(115200, 12).raw(56), 12)
        self.assertEqual(c.select_motor(625000, 4).raw(56), 4)

    def test_ram_and_motion_only_selected_motor(self):
        c, bus = self.make()
        c.write_parameter(12, 2, 34)
        c.set_position(12, 8000)
        self.assertEqual([m.ram[2] for m in bus.motors], [127, 90, 34])
        self.assertEqual([m.position for m in bus.motors], [7500, 7500, 8000])

    def test_duplicate_requested_id_rejected_without_any_transmission(self):
        c, bus = self.make()
        with self.assertRaisesRegex(ValueError, 'already detected'):
            c.set_id(4, current_id=0, detected_ids={0, 4, 12}, isolated=True)
        self.assertEqual(bus.writes, [])

    def test_historical_id_remains_reserved_after_isolation(self):
        c, bus = self.make([DemoSerial(servo_id=0)])
        with self.assertRaisesRegex(ValueError, 'already detected'):
            c.set_id(12, current_id=0, detected_ids={0, 4, 12}, isolated=True)
        self.assertEqual(bus.writes, [])

    def test_id_change_requires_explicit_isolation(self):
        c, bus = self.make([DemoSerial()])
        with self.assertRaisesRegex(ValueError, 'Physically isolate'):
            c.set_id(7, current_id=0, detected_ids={0})
        self.assertEqual(bus.writes, [])

    def test_recheck_blocks_second_motor_even_with_isolation_claim(self):
        c, bus = self.make()
        with self.assertRaisesRegex(ValueError, 'only the selected'):
            c.set_id(7, current_id=0, detected_ids={0}, isolated=True)
        self.assertTrue(all(tx[0] & 0xE0 == 0xA0 for tx in bus.writes))
        self.assertEqual([m.servo_id for m in bus.motors], [0, 4, 12])

    def test_recheck_detects_new_collision_after_initial_scan(self):
        c, bus = self.make([DemoSerial(), DemoSerial(servo_id=7, baud=625000)])
        with self.assertRaisesRegex(ValueError, 'already present'):
            c.set_id(7, current_id=0, detected_ids={0}, isolated=True)
        self.assertTrue(all(tx[0] & 0xE0 == 0xA0 for tx in bus.writes))

    def test_isolated_id_change_and_readback(self):
        c, bus = self.make([DemoSerial(servo_id=4)])
        self.assertEqual(c.set_id(7, current_id=4, detected_ids={4}, isolated=True), 7)
        writes = [tx for tx in bus.writes if tx[0] & 0xE0 == 0xE0]
        self.assertEqual(writes, [b'\xe7\x01\x01\x01', b'\xff\0\0\0'])
        self.assertEqual(c.read_eeprom(7).raw(56), 7)

    def test_no_op_id_change_never_writes(self):
        c, bus = self.make()
        with self.assertRaisesRegex(ValueError, 'already has'):
            c.set_id(0, current_id=0, detected_ids={0}, isolated=True)
        self.assertEqual(bus.writes, [])


if __name__ == '__main__': unittest.main()
