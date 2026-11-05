"""GUI integration tests. Run under Xvfb when a desktop is unavailable."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
from ics_manager.gui import App
from ics_manager.eeprom import Eeprom

@unittest.skipUnless(os.environ.get('DISPLAY'), 'Needs an X display (e.g. xvfb-run)')
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.app = App(self.root, demo=True)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.temp = tempfile.TemporaryDirectory()
        self.app.backup_dir = Path(self.temp.name)
        self.patches = [patch('ics_manager.gui.messagebox.showerror'), patch('ics_manager.gui.messagebox.showwarning'),
                        patch('ics_manager.gui.messagebox.showinfo'), patch('ics_manager.gui.messagebox.askokcancel', return_value=True)]
        self.mocks = [p.start() for p in self.patches]
        self.wait_for(lambda: self.app.controller is not None and self.app.future is None)
        self.app.controller.sleep = lambda _: None
        self.app.transport.settings.pre_tx_delay = 0

    def tearDown(self):
        self.app.close()
        end = time.monotonic() + 2
        while time.monotonic() < end:
            try:
                self.root.update()
                if not self.root.winfo_exists(): break
            except tk.TclError: break
            time.sleep(0.005)
        for p in self.patches: p.stop()
        self.temp.cleanup()
        self.assertFalse(self.errors)

    def wait_for(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.root.update(); time.sleep(0.005)
        self.root.update()
        self.assertTrue(predicate(), self.app.status.get())

    def idle(self):
        self.wait_for(lambda: self.app.future is None)

    def select(self, sid, baud=115200):
        self.app.bus_tree.selection_set(self.app.row_id((baud, sid)))
        self.root.update()
        self.idle()
        self.assertEqual(self.app.active_key, (baud, sid))

    def test_connect_does_not_move(self):
        self.assertFalse(self.app.armed.get())
        self.assertTrue(self.app.transport.serial.writes)
        self.assertTrue(all(tx[0] & 0xE0 == 0xA0 for tx in self.app.transport.serial.writes))
        self.app.move()
        self.assertIn('Enable motion', self.app.status.get())

    def test_motion_and_free(self):
        self.app.armed.set(True)
        self.app.position.set(8000)
        self.app.move(); self.idle()
        self.assertEqual(self.app.actual_pos.get(), '8000')
        self.app.free(); self.idle()
        self.assertTrue(self.app.transport.serial.motors[0].free_mode)
        self.assertFalse(self.app.armed.get())

    def test_programmatic_image_load_never_triggers_motion(self):
        self.app.armed.set(True); self.app.live.set(True)
        before = list(self.app.transport.serial.writes)
        self.app.apply_image(Eeprom.demo())
        self.root.update()
        self.assertEqual(before, self.app.transport.serial.writes)

    def test_id_disabled_during_operation(self):
        self.app.read_ram(4)
        self.assertEqual(str(self.app.id_box.cget('state')), 'disabled')
        self.idle()
        self.assertEqual(str(self.app.id_box.cget('state')), 'readonly')

    def test_parameters_and_eeprom_gui_workflow(self):
        self.app.ram_controls['speed'].set(100)
        self.app.set_ram(2); self.idle()
        self.app.read_ram(2); self.idle()
        self.assertEqual(self.app.ram_controls['speed'].get(), 100)
        self.app.fields['speed'].set(80)
        self.app.fields['user_offset'].set(-20)
        self.app.config_baud.set(625000)
        self.app.write_eeprom(); self.idle()
        self.assertEqual(self.app.baud.get(), '625000')
        self.assertEqual(self.app.image.get('speed'), 80)
        self.assertEqual(self.app.image.get('user_offset'), -20)
        self.assertEqual(len(list(Path(self.temp.name).glob('*.sdt'))), 1)
        self.assertFalse(self.mocks[0].called)
        self.assertFalse(self.mocks[1].called)

    def test_visible_controls_fit_default_window(self):
        self.root.update_idletasks()
        for ctrl in self.app.fields.values():
            self.assertGreater(ctrl.winfo_width(), 300)
            self.assertLess(ctrl.winfo_rooty() + ctrl.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
        self.assertGreater(self.app.log_text.winfo_reqwidth(), 100)

    def test_all_detected_ids_are_visible(self):
        self.assertEqual(set(self.app.motors), {(115200, i) for i in (0, 4, 12)})
        self.assertEqual(len(self.app.bus_tree.get_children()), 3)
        self.assertTrue(self.app.target_ready)

    def test_selection_loads_parameters_and_disarms_motion(self):
        self.app.armed.set(True)
        self.app.poll_temp.set(True)
        self.select(4)
        self.assertEqual(self.app.fields['speed'].get(), 90)
        self.assertEqual(self.app.ram_controls['speed'].get(), 90)
        self.assertFalse(self.app.armed.get())
        self.assertFalse(self.app.poll_temp.get())
        self.select(12)
        self.assertEqual(self.app.fields['speed'].get(), 60)

    def test_only_selected_motor_written_without_isolation_checkbox(self):
        self.select(4)
        self.assertFalse(self.app.single.get())
        self.app.fields['speed'].set(50)
        self.app.write_eeprom(); self.idle()
        self.assertEqual([m.image.get('speed') for m in self.app.transport.serial.motors], [127, 50, 60])
        self.assertFalse(self.mocks[0].called)

    def test_baud_change_moves_selected_row_and_selection_switches_host(self):
        self.select(4)
        self.app.config_baud.set(625000)
        self.app.write_eeprom(); self.idle()
        self.assertIn((625000, 4), self.app.motors)
        self.assertNotIn((115200, 4), self.app.motors)
        self.select(12)
        self.assertEqual(self.app.transport.baud, 115200)
        self.select(4, 625000)
        self.assertEqual(self.app.transport.baud, 625000)
        self.assertEqual(self.app.fields['speed'].get(), 90)

    def test_duplicate_id_error_without_any_write(self):
        self.app.new_id.set('4')
        before = list(self.app.transport.serial.writes)
        self.app.set_id()
        self.assertEqual(before, self.app.transport.serial.writes)
        self.assertEqual(self.mocks[0].call_args.args[0], 'ID already in use')

    def test_multimotor_id_change_blocked_even_if_checkbox_ticked(self):
        self.app.new_id.set('7'); self.app.single.set(True)
        before = list(self.app.transport.serial.writes)
        self.app.set_id()
        self.assertEqual(before, self.app.transport.serial.writes)
        self.assertEqual(self.mocks[0].call_args.args[0], 'ID change requires isolation')

    def test_isolation_rescan_keeps_used_ids_reserved(self):
        self.app.transport.serial.motors = self.app.transport.serial.motors[:1]
        self.app.scan_bus(); self.idle()
        self.assertEqual(self.app.motor_states[(115200, 4)], 'No reply')
        self.app.new_id.set('4'); self.app.single.set(True)
        before = list(self.app.transport.serial.writes)
        self.app.set_id()
        self.assertEqual(before, self.app.transport.serial.writes)
        self.assertEqual(self.mocks[0].call_args.args[0], 'ID already in use')

    def test_isolated_rename_refreshes_selected_motor(self):
        self.app.transport.serial.motors = self.app.transport.serial.motors[:1]
        self.app.scan_bus(); self.idle()
        self.app.new_id.set('7'); self.app.single.set(True)
        self.app.set_id(); self.idle()
        self.assertEqual(self.app.active_key, (115200, 7))
        self.assertTrue(self.app.target_ready)
        self.assertEqual(self.app.image.raw(56), 7)
        self.assertNotIn((115200, 0), self.app.motors)
        self.assertFalse(self.mocks[0].called)

    def test_selection_cannot_change_during_transaction(self):
        self.app.read_ram(4)
        self.app.bus_tree.selection_set(self.app.row_id((115200, 12)))
        self.app.target_changed()
        self.assertEqual(self.app.active_key, (115200, 0))
        self.assertEqual(self.app.bus_tree.selection(), ('115200:0',))
        self.idle()

    def test_failed_selection_prevents_writing_previous_motor_values(self):
        self.app.transport.serial.motors = self.app.transport.serial.motors[:1]
        self.select(4)
        self.assertFalse(self.app.target_ready)
        before = list(self.app.transport.serial.writes)
        self.app.write_eeprom()
        self.assertEqual(before, self.app.transport.serial.writes)

    def test_scan_all_bauds_finds_moved_motor(self):
        from ics_manager.eeprom import BAUD_CODES
        motor = self.app.transport.serial.motors[1]
        motor.servo_baud = 625000
        motor.image.put(26, 2, BAUD_CODES[625000])
        self.app.scan_all.set(True)
        self.app.scan_bus(); self.idle()
        self.assertEqual(self.app.motor_states[(625000, 4)], 'Ready')
        self.select(4, 625000)
        self.assertEqual(self.app.transport.baud, 625000)

    def test_baud_change_collision_is_blocked(self):
        from ics_manager.demo import DemoSerial
        self.app.transport.serial.motors.append(DemoSerial(servo_id=0, baud=625000))
        self.app.scan_all.set(True)
        self.app.scan_bus(); self.idle()
        self.app.config_baud.set(625000)
        before = list(self.app.transport.serial.writes)
        self.app.write_eeprom()
        self.assertEqual(before, self.app.transport.serial.writes)
        self.assertEqual(self.mocks[0].call_args.args[0], 'Address already detected')

if __name__ == '__main__': unittest.main()
