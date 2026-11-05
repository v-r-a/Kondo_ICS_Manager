"""Tk/ttk desktop interface. Only the main thread touches Tk widgets."""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import argparse
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import __version__
from .controller import Controller, Motor, BAUD_RATES
from .demo import DemoBusSerial
from .eeprom import Eeprom, FIELDS, FLAGS, BAUD_CODES, BY_KEY, load_image, save_image
from .transport import SerialSettings, SerialTransport

BACKGROUND = '#f2f5f8'
INK = '#1c3044'
BLUE = '#146caa'

class ValueControl(ttk.Frame):
    def __init__(self, parent, label, minimum, maximum, default, command=None, width=23):
        super().__init__(parent)
        self.minimum, self.maximum = minimum, maximum
        self.command = command
        self.var = tk.StringVar(value=str(default))
        self.scale_var = tk.DoubleVar(value=default)
        self.label = ttk.Label(self, text=label, width=width)
        self.label.grid(row=0, column=0, sticky='w')
        self.scale = ttk.Scale(self, from_=minimum, to=maximum, variable=self.scale_var, command=self._slide)
        self.scale.grid(row=0, column=1, sticky='ew', padx=(4, 12))
        self.spin = ttk.Spinbox(self, from_=minimum, to=maximum, textvariable=self.var,
                                width=7, command=self._typed)
        self.spin.grid(row=0, column=2, sticky='e')
        self.spin.bind('<Return>', self._typed)
        self.spin.bind('<FocusOut>', self._typed)
        self.columnconfigure(1, weight=1)

    def _slide(self, value):
        self.var.set(str(round(float(value))))
        if self.command:
            self.command()

    def _typed(self, event=None):
        try:
            value = self.get()
            self.scale_var.set(value)
            if self.command:
                self.command()
        except ValueError:
            self.bell()

    def get(self):
        try:
            value = int(self.var.get())
        except ValueError as exc:
            raise ValueError(f'{self.label.cget("text")}: enter an integer') from exc
        if not self.minimum <= value <= self.maximum:
            raise ValueError(f'{self.label.cget("text")}: expected {self.minimum}..{self.maximum}')
        return value

    def set(self, value):
        # Programmatic changes never call the transmit callback.
        self.var.set(str(value))
        self.scale_var.set(value)


class App:
    def __init__(self, root, demo=False):
        self.root, self.demo = root, demo
        self.root.title(f'Kondo ICS 3.5 / 3.6 | Linux Manager {__version__}' + (' | DEMO' if demo else ''))
        self.root.geometry('1360x930')
        self.root.minsize(1180, 850)
        self.settings = SerialSettings()
        self.transport = self.controller = None
        self.image = None
        self.image_target = None
        self.active_key = None
        self.target_ready = False
        self.motors = {}
        self.motor_states = {}
        self.inventory_port = None
        self.scan_issues = []
        self.scan_progress = queue.SimpleQueue()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ics-serial')
        self.future = None
        self.done_callback = None
        self.operation = ''
        self.silent = False
        self.closing = False
        self.pending_free = False
        self.jobs = {}
        self.log_queue = queue.Queue(maxsize=4000)
        self.log_history = deque(maxlen=12000)
        self.port = tk.StringVar(value='/dev/ttyUSB0')
        self.baud = tk.StringVar(value='115200')
        self.servo_id = tk.StringVar(value='0')
        self.new_id = tk.StringVar(value='1')
        self.single = tk.BooleanVar(value=False)
        self.scan_all = tk.BooleanVar(value=False)
        self.bus_summary = tk.StringVar(value='Connect to discover IDs 0–31.')
        self.target_label = tk.StringVar(value='Select a detected motor')
        self.armed = tk.BooleanVar(value=False)
        self.live = tk.BooleanVar(value=False)
        self.free_monitor = tk.BooleanVar(value=False)
        self.poll_temp = tk.BooleanVar(value=False)
        self.poll_current = tk.BooleanVar(value=False)
        self.temp = tk.StringVar(value='--')
        self.current = tk.StringVar(value='--')
        self.actual_pos = tk.StringVar(value='--')
        self.version = tk.StringVar(value='--')
        self.config_baud = tk.IntVar(value=115200)
        self.flags = {name: tk.BooleanVar(value=(name == 'serial')) for name in FLAGS}
        self.status = tk.StringVar(value='Demo mode: no hardware access' if demo else 'Disconnected')
        self.stats = tk.StringVar(value='No transactions')
        self.source_label = tk.StringVar(value='Read EEPROM or open a model preset before writing.')
        self.backup_dir = Path.home() / '.local/share/kondo-ics-manager/backups'
        self._style()
        self._build()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(40, self._drain)
        self.poll_job = self.root.after(200, self._poll)
        self.refresh_ports()
        self.demo_job = self.root.after(100, self.connect) if demo else None

    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')
        s.configure('.', font=('DejaVu Sans', 10), background=BACKGROUND, foreground=INK)
        s.configure('TFrame', background=BACKGROUND)
        s.configure('TLabelframe', background=BACKGROUND, bordercolor='#cbd5df', relief='solid')
        s.configure('TLabelframe.Label', font=('DejaVu Sans', 10, 'bold'), foreground=BLUE)
        s.configure('TButton', padding=(10, 5))
        s.configure('Primary.TButton', foreground='white', background=BLUE)
        s.map('Primary.TButton', background=[('active', '#0d5489')])
        s.configure('Title.TLabel', font=('DejaVu Sans', 18, 'bold'))
        s.configure('Muted.TLabel', foreground='#526678', font=('DejaVu Sans', 9))
        s.configure('Danger.TButton', foreground='#a83232')
        self.root.configure(background=BACKGROUND)

    def _build(self):
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label='Open settings / Kondo .sdt...', command=self.open_file)
        file_menu.add_command(label='Save displayed settings...', command=self.save_file)
        file_menu.add_command(label='Save communication log...', command=self.save_log)
        file_menu.add_separator()
        file_menu.add_command(label='Quit', command=self.close)
        menu.add_cascade(label='File', menu=file_menu)
        menu.add_command(label='Connection settings', command=self.connection_settings)
        menu.add_command(label='Help', command=self.help)
        self.root.config(menu=menu)
        header = ttk.Frame(self.root, padding=(18, 12, 18, 6))
        header.pack(fill='x')
        ttk.Label(header, text='ICS Servo Manager', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='LINUX  /  ICS 3.5 + 3.6' + ('  /  VIRTUAL BUS' if self.demo else ''),
                  style='Muted.TLabel').pack(side='right')
        connection = ttk.Frame(self.root, padding=(18, 4, 18, 10))
        connection.pack(fill='x')
        ttk.Label(connection, text='Port').pack(side='left')
        self.port_box = ttk.Combobox(connection, textvariable=self.port, width=22)
        self.port_box.pack(side='left', padx=6)
        ttk.Button(connection, text='Refresh', command=self.refresh_ports).pack(side='left')
        ttk.Label(connection, text='Host baud').pack(side='left', padx=(15, 4))
        self.baud_box = ttk.Combobox(connection, textvariable=self.baud, width=10,
                                    values=('Auto', '115200', '625000', '1250000'), state='readonly')
        self.baud_box.pack(side='left')
        self.baud_box.bind('<<ComboboxSelected>>', self.change_host_baud)
        self.connect_button = ttk.Button(connection, text='Connect', style='Primary.TButton', command=self.connect)
        self.connect_button.pack(side='left', padx=10)
        ttk.Label(connection, text='Select a motor in the bus list to edit its settings.', style='Muted.TLabel').pack(side='left', padx=5)
        ttk.Button(connection, text='Settings...', command=self.connection_settings).pack(side='right')
        workspace = ttk.Frame(self.root)
        workspace.pack(fill='both', expand=True, padx=18, pady=(0, 8))
        bus = ttk.LabelFrame(workspace, text='Motors on bus', padding=8)
        bus.pack(side='left', fill='y', padx=(0, 10))
        ttk.Button(bus, text='Scan bus', style='Primary.TButton', command=self.scan_bus).pack(fill='x')
        ttk.Checkbutton(bus, text='Scan all baud rates', variable=self.scan_all).pack(anchor='w', pady=7)
        tree_frame = ttk.Frame(bus)
        tree_frame.pack(fill='both', expand=True)
        self.bus_tree = ttk.Treeview(tree_frame, columns=('id', 'baud', 'state'), show='headings',
                                    selectmode='browse', height=17)
        for col, label, width in [('id', 'ID', 32), ('baud', 'Baud', 76), ('state', 'State', 85)]:
            self.bus_tree.heading(col, text=label)
            self.bus_tree.column(col, width=width, minwidth=width, stretch=False)
        self.bus_tree.pack(side='left', fill='both', expand=True)
        bus_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.bus_tree.yview)
        self.bus_tree.configure(yscrollcommand=bus_scroll.set)
        bus_scroll.pack(side='right', fill='y')
        self.bus_tree.tag_configure('stale', foreground='#7c6470')
        self.bus_tree.bind('<<TreeviewSelect>>', self.target_changed)
        self.bus_tree.bind('<Button-1>', self._block_bus_selection, add=True)
        self.bus_tree.bind('<Key>', self._block_bus_selection, add=True)
        ttk.Label(bus, textvariable=self.bus_summary, wraplength=195, style='Muted.TLabel').pack(fill='x', pady=8)
        ttk.Separator(bus).pack(fill='x', pady=6)
        ttk.Checkbutton(bus, text='Target physically isolated', variable=self.single).pack(anchor='w')
        ttk.Label(bus, text='For ID changes only. Disconnect every other motor first, then rescan. '
                  'A scan cannot prove physical isolation.', wraplength=195, style='Muted.TLabel').pack(fill='x', pady=6)
        self.tabs = ttk.Notebook(workspace)
        self.tabs.pack(side='left', fill='both', expand=True)
        manager = ttk.Frame(self.tabs, padding=12)
        diagnostics = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(manager, text='  Servo controls  ')
        self.tabs.add(diagnostics, text='  Communication log  ')
        manager.columnconfigure(0, weight=1)
        manager.columnconfigure(1, weight=1)
        identity = ttk.LabelFrame(manager, text='Selected motor', padding=10)
        identity.grid(row=0, column=0, sticky='ew', padx=(0, 7), pady=(0, 8))
        ttk.Label(identity, textvariable=self.target_label, font=('DejaVu Sans', 10, 'bold')).pack(anchor='w')
        idrow = ttk.Frame(identity)
        idrow.pack(fill='x', pady=(6, 0))
        ttk.Label(idrow, text='New ID').pack(side='left', padx=(0, 7))
        self.id_box = ttk.Combobox(idrow, textvariable=self.new_id, values=list(range(32)), width=4, state='readonly')
        self.id_box.pack(side='left')
        ttk.Button(idrow, text='Change ID...', command=self.set_id).pack(side='left', padx=8)
        position = ttk.LabelFrame(manager, text='Position', padding=10)
        position.grid(row=1, column=0, sticky='nsew', padx=(0, 7), pady=(0, 10))
        self.position = ValueControl(position, 'Target', 3500, 11500, 7500,
                                     command=lambda: self._live_changed('position'), width=7)
        self.position.pack(fill='x')
        row = ttk.Frame(position)
        row.pack(fill='x', pady=(8, 0))
        ttk.Button(row, text='Move', command=self.move).pack(side='left')
        ttk.Button(row, text='Neutral', command=self.neutral).pack(side='left', padx=5)
        ttk.Button(row, text='FREE', style='Danger.TButton', command=self.free).pack(side='left')
        ttk.Button(row, text='Read (3.6)', command=lambda: self.read_ram(5)).pack(side='right')
        row = ttk.Frame(position)
        row.pack(fill='x', pady=(7, 0))
        ttk.Checkbutton(row, text='Enable motion', variable=self.armed).pack(side='left')
        ttk.Checkbutton(row, text='Live sliders', variable=self.live).pack(side='left', padx=8)
        ttk.Checkbutton(row, text='Monitor FREE', variable=self.free_monitor,
                        command=self._free_monitor_changed).pack(side='right')
        row = ttk.Frame(position)
        row.pack(fill='x', pady=(6, 0))
        ttk.Label(row, text='Measured: ', style='Muted.TLabel').pack(side='left')
        ttk.Label(row, textvariable=self.actual_pos).pack(side='left')
        realtime = ttk.LabelFrame(manager, text='Real-time parameters', padding=10)
        realtime.grid(row=0, column=1, rowspan=2, sticky='nsew', padx=(7, 0), pady=(0, 10))
        realtime.columnconfigure(0, weight=1)
        self.ram_controls = {}
        for i, (key, title, default, sc) in enumerate([('stretch', 'Stretch', 30, 1), ('speed', 'Speed', 127, 2)]):
            ctrl = ValueControl(realtime, title, 1, 127, default,
                                 command=lambda k=key: self._live_changed(k), width=8)
            ctrl.grid(row=i, column=0, sticky='ew', pady=4)
            self.ram_controls[key] = ctrl
            ttk.Button(realtime, text='Set', width=6, command=lambda s=sc: self.set_ram(s)).grid(row=i, column=1, padx=(8, 3))
            ttk.Button(realtime, text='Get', width=6, command=lambda s=sc: self.read_ram(s)).grid(row=i, column=2)
        for i, (label, variable, poll, sc) in enumerate([('Temperature (raw)', self.temp, self.poll_temp, 4),
                                                       ('Current (raw)', self.current, self.poll_current, 3)], start=2):
            row = ttk.Frame(realtime)
            row.grid(row=i, column=0, sticky='ew', pady=8)
            ttk.Label(row, text=label).pack(side='left')
            ttk.Label(row, textvariable=variable, width=6, anchor='e').pack(side='right')
            ttk.Checkbutton(realtime, text='Poll', variable=poll).grid(row=i, column=1)
            ttk.Button(realtime, text='Get', width=6, command=lambda s=sc: self.read_ram(s)).grid(row=i, column=2)
        config = ttk.LabelFrame(manager, text='EEPROM configuration', padding=10)
        config.grid(row=2, column=0, columnspan=2, sticky='nsew')
        config.columnconfigure(0, weight=1)
        config.columnconfigure(1, weight=1)
        baudrow = ttk.Frame(config)
        baudrow.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        ttk.Label(baudrow, text='Servo baud:').pack(side='left')
        for baud, label in [(1250000, '1.25 M'), (625000, '625 k'), (115200, '115.2 k')]:
            ttk.Radiobutton(baudrow, text=label, value=baud, variable=self.config_baud).pack(side='left', padx=5)
        flagrow = ttk.Frame(config)
        flagrow.grid(row=0, column=1, sticky='ew', pady=(0, 8), padx=(20, 0))
        for name in FLAGS:
            ttk.Checkbutton(flagrow, text=name.title(), variable=self.flags[name]).pack(side='left', padx=(0, 9))
        self.fields = {}
        for i, f in enumerate(FIELDS):
            col, row = (0, i + 1) if i < 8 else (1, i - 7)
            ctrl = ValueControl(config, f.label, f.minimum, f.maximum, f.default)
            ctrl.grid(row=row, column=col, sticky='ew', padx=(0, 14) if col == 0 else (14, 0), pady=4)
            self.fields[f.key] = ctrl
        versionrow = ttk.Frame(config)
        versionrow.grid(row=8, column=1, sticky='ew', padx=(14, 0))
        ttk.Label(versionrow, text='Firmware version (raw)', width=23).pack(side='left')
        ttk.Label(versionrow, textvariable=self.version).pack(side='right')
        actions = ttk.Frame(config)
        actions.grid(row=9, column=0, columnspan=2, sticky='ew', pady=(12, 0))
        ttk.Button(actions, text='Read EEPROM', command=self.read_eeprom).pack(side='left')
        ttk.Button(actions, text='Write EEPROM...', style='Primary.TButton', command=self.write_eeprom).pack(side='left', padx=8)
        ttk.Button(actions, text='Open preset...', command=self.open_file).pack(side='left')
        ttk.Button(actions, text='Save settings...', command=self.save_file).pack(side='right')
        ttk.Label(manager, textvariable=self.source_label, style='Muted.TLabel', wraplength=950).grid(
            row=3, column=0, columnspan=2, sticky='w', pady=(9, 0))
        row = ttk.Frame(diagnostics)
        row.pack(fill='x', pady=(0, 8))
        ttk.Label(row, text='Timestamped TX / RX | stale bytes | errors | transaction time').pack(side='left')
        ttk.Button(row, text='Save log...', command=self.save_log).pack(side='right')
        ttk.Button(row, text='Clear', command=self.clear_log).pack(side='right', padx=6)
        self.log_text = tk.Text(diagnostics, wrap='word', font=('DejaVu Sans Mono', 10), background='#102434',
                                foreground='#dbeaf5', insertbackground='white', state='disabled')
        scroll = ttk.Scrollbar(diagnostics, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.log_text.pack(fill='both', expand=True)
        footer = ttk.Frame(self.root, padding=(18, 0, 18, 10))
        footer.pack(fill='x')
        ttk.Label(footer, textvariable=self.status, wraplength=820).pack(side='left')
        ttk.Label(footer, textvariable=self.stats, style='Muted.TLabel').pack(side='right')

    def emit(self, kind, message):
        line = f'{datetime.now().isoformat(timespec="milliseconds")}  {kind:6}  {message}'
        try:
            self.log_queue.put_nowait(line)
        except queue.Full:
            pass

    def submit(self, name, work, callback=None, silent=False):
        if self.future is not None or self.closing:
            if not silent:
                self.status.set('Busy: ' + self.operation)
            return False
        self.operation, self.done_callback, self.silent = name, callback, silent
        self.id_box.configure(state='disabled')
        self.baud_box.configure(state='disabled')
        self.port_box.configure(state='disabled')
        self.status.set(name + '...')
        self.future = self.executor.submit(work)
        return True

    def _drain(self):
        progress = None
        while not self.scan_progress.empty():
            progress = self.scan_progress.get_nowait()
        if progress and self.future is not None:
            baud, sid = progress
            self.status.set(f'{self.operation}: checking ID {sid:02d} / 31 at {baud} baud...')
        if self.future is not None and self.future.done():
            future, callback, silent = self.future, self.done_callback, self.silent
            self.future = None
            try:
                result = future.result()
                if callback:
                    callback(result)
                else:
                    self.status.set(self.operation + ': complete')
            except Exception as exc:
                self.emit('ERROR', str(exc))
                self.status.set(f'{self.operation}: {exc}')
                self.stop_polling()
                self.armed.set(False)
                if self.operation.startswith(('Load ID ', 'Write', 'Check bus and write')):
                    self.target_ready = False
                    self.image_target = None
                    if self.active_key in self.motors:
                        self.motor_states[self.active_key] = 'Unverified'
                        self._render_bus()
                if self.transport:
                    self.baud.set(str(self.transport.baud))
                if not silent and not self.closing:
                    messagebox.showerror(self.operation, str(exc), parent=self.root)
            if self.future is None:
                self.id_box.configure(state='readonly')
                self.baud_box.configure(state='readonly')
                self.port_box.configure(state='disabled' if self.controller else 'normal')
            if self.pending_free and self.controller and not self.closing:
                self.pending_free = False
                self.free()
        lines = []
        for _ in range(500):
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_history.extend(lines)
            self.log_text.configure(state='normal')
            self.log_text.insert('end', '\n'.join(lines) + '\n')
            if int(self.log_text.index('end-1c').split('.')[0]) > 12000:
                self.log_text.delete('1.0', '1001.0')
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        if self.transport:
            t = self.transport
            self.stats.set(f'OK {t.ok} / errors {t.errors} / scan empty {t.no_reply} / {t.last_ms:.1f} ms')
        if self.closing and self.future is None:
            if self.transport:
                self.transport.close()
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.root.destroy()
            return
        self.root.after(40, self._drain)

    def require(self, target=True):
        if not self.controller:
            self.status.set('Connect first.')
            return False
        if self.future is not None:
            self.status.set('Busy: ' + self.operation)
            return False
        if target and (self.active_key is None or not self.target_ready):
            self.status.set('Select a responding motor and read its EEPROM first.')
            return False
        return True

    def selected_id(self):
        return int(self.servo_id.get())

    def target_token(self):
        return (self.port.get(), self.active_key)

    @staticmethod
    def row_id(key):
        return f'{key[0]}:{key[1]}'

    def _block_bus_selection(self, event=None):
        if self.future is not None:
            return 'break'

    def _render_bus(self):
        for row in self.bus_tree.get_children():
            self.bus_tree.delete(row)
        for key in sorted(self.motors, key=lambda k: (k[1], k[0])):
            state = self.motor_states[key]
            self.bus_tree.insert('', 'end', iid=self.row_id(key),
                values=(f'{key[1]:02d}', str(key[0]), state), tags=() if state == 'Ready' else ('stale',))
        if self.active_key in self.motors:
            self.bus_tree.selection_set(self.row_id(self.active_key))
        ready = sum(s == 'Ready' for s in self.motor_states.values())
        stale = len(self.motors) - ready
        self.bus_summary.set(f'{ready} responding address(es)' + (f' · {stale} unverified / no reply' if stale else '') +
            '\nSelect a row to load its settings. Silent motors and duplicate physical IDs cannot be counted reliably.')
        if self.scan_issues:
            self.bus_summary.set(self.bus_summary.get() + f'\n{len(self.scan_issues)} scan issue(s); see log.')

    def _stop_target_activity(self):
        self.stop_polling()
        self.cancel_live()
        self.armed.set(False)
        self.pending_free = False
        self.image_target = None
        self.target_ready = False
        self.temp.set('--'); self.current.set('--'); self.actual_pos.set('--')

    def _scan_progress(self, baud, sid):
        self.scan_progress.put((baud, sid))

    def _accept_scan(self, result):
        self.scan_issues = result.issues
        for key in self.motors:
            if key[0] in result.scanned_bauds:
                self.motor_states[key] = 'No reply'
        for motor in result.motors:
            self.motors[motor.key] = motor
            self.motor_states[motor.key] = 'Ready'
        found = {m.key for m in result.motors}
        choice = self.active_key if self.active_key in found else (result.motors[0].key if result.motors else None)
        self.active_key = choice
        self._render_bus()
        for issue in result.issues:
            self.emit('SCAN', issue)
        if choice is not None and not self.closing:
            self._select_motor(choice)
        else:
            self.target_label.set('No responding motor selected')
            self.status.set('Scan complete: no valid replies. Check power, baud and echo; see log.')

    def scan_bus(self, bauds=None):
        if not self.require(target=False):
            return
        self._stop_target_activity()
        self.single.set(False)
        bauds = bauds or (BAUD_RATES if self.scan_all.get() or self.baud.get() == 'Auto' else (int(self.baud.get()),))
        self.submit('Scan bus', lambda: self.controller.scan_bus(bauds, self._scan_progress), self._accept_scan)

    def refresh_ports(self):
        if self.controller or self.future is not None:
            return
        try:
            from serial.tools.list_ports import comports
            ports = sorted(p.device for p in comports() if p.device.startswith('/dev/ttyUSB'))
        except ImportError:
            ports = []
        self.port_box['values'] = ports
        if ports and self.port.get() not in ports:
            self.port.set(ports[0])

    def connect(self):
        if self.future is not None:
            return
        if self.controller:
            self._stop_target_activity()
            self.single.set(False)
            t = self.transport
            def done(_):
                self.controller = self.transport = None
                self.image_target = None
                self.active_key = None
                self.motor_states = {key: 'No reply' for key in self.motors}
                self._render_bus()
                self.target_label.set('Disconnected')
                self.connect_button.configure(text='Connect')
                self.status.set('Disconnected')
                self.source_label.set('Disconnected; displayed values retained. Read EEPROM after reconnecting.')
            self.submit('Disconnect', t.close, done)
            return
        auto = self.baud.get() == 'Auto'
        if self.inventory_port != self.port.get():
            self.motors.clear(); self.motor_states.clear()
            self.scan_issues = []
            self.active_key = None
            self.inventory_port = self.port.get()
            self._render_bus()
        settings = replace(self.settings, port=self.port.get(), baud=115200 if auto else int(self.baud.get()))
        bauds = BAUD_RATES if auto or self.scan_all.get() else (settings.baud,)
        self.single.set(False)
        def work():
            t = SerialTransport(settings, self.emit,
                    serial_instance=DemoBusSerial(echo=settings.echo, baud=settings.baud) if self.demo else None)
            c = Controller(t)
            try:
                result = c.scan_bus(bauds, self._scan_progress)
            except Exception as exc:
                # Leave the port open for selecting a different ID / diagnosis.
                return t, c, None, str(exc)
            return t, c, result, ''
        def done(result):
            self.transport, self.controller, found, err = result
            self.connect_button.configure(text='Disconnect')
            if found:
                self._accept_scan(found)
            else:
                self.status.set('Port open; servo not verified. Check ID / baud / echo. ' + err)
        self.submit('Connect and scan bus', work, done)

    def target_changed(self, event=None):
        rows = self.bus_tree.selection()
        if not rows or not self.controller:
            return
        key = tuple(map(int, rows[0].split(':')))
        if key == self.active_key:
            return
        if self.future is not None:
            if self.active_key in self.motors:
                self.bus_tree.selection_set(self.row_id(self.active_key))
            return
        self.single.set(False)
        self._select_motor(key)

    def _select_motor(self, key):
        self._stop_target_activity()
        self.active_key = key
        baud, sid = key
        self.servo_id.set(str(sid))
        self.target_label.set(f'ID {sid:02d}  ·  {baud:,} baud')
        self.source_label.set(f'Loading settings for ID {sid}; wait for EEPROM read.')
        def work():
            image = self.controller.select_motor(baud, sid)
            ram = {sc: self.controller.read_parameter(sid, sc) for sc in (1, 2)}
            return image, ram
        def done(result):
            image, ram = result
            self.baud.set(str(baud))
            self.motors[key] = Motor(sid, baud, image)
            self.motor_states[key] = 'Ready'
            self.apply_image(image, hardware=True)
            self.ram_controls['stretch'].set(ram[1])
            self.ram_controls['speed'].set(ram[2])
            self._render_bus()
            self.status.set(f'ID {sid:02d} selected at {baud} baud. Controls apply to this motor.')
        self.submit(f'Load ID {sid}', work, done)

    def change_host_baud(self, event=None):
        if not self.controller:
            return
        if self.future is not None:
            self.baud.set(str(self.transport.baud))
            self.status.set('Wait for the current transaction before changing host baud.')
            return
        self.scan_bus(BAUD_RATES if self.baud.get() == 'Auto' else (int(self.baud.get()),))

    def set_id(self):
        if not self.require():
            return
        sid, new_id = self.selected_id(), int(self.new_id.get())
        known_ids = {key[1] for key in self.motors}
        if new_id == sid:
            messagebox.showerror('ID unchanged', f'The selected motor already has ID {sid}.', parent=self.root)
            return
        if new_id in known_ids:
            messagebox.showerror('ID already in use',
                f'ID {new_id} was already detected on this bus. Choose an unused ID.\n'
                'IDs with “No reply” remain reserved for this session.', parent=self.root)
            return
        if sum(state == 'Ready' for state in self.motor_states.values()) != 1 or not self.single.get():
            messagebox.showerror('ID change requires isolation',
                'The ICS ID-write command is broadcast; it cannot address just the selected motor.\n\n'
                'Disconnect every other motor, scan again, select the target, then tick '
                '“Target physically isolated”. Other parameter changes do not require isolation.', parent=self.root)
            return
        if not messagebox.askokcancel('Set persistent ID',
                f'Change the isolated motor from ID {sid} to {new_id}?\n'
                'Only this motor may be physically connected. An addressed scan will recheck the bus first.', parent=self.root):
            return
        self.stop_polling(); self.cancel_live(); self.armed.set(False)
        old_key = self.active_key
        def done(value):
            old = self.motors.pop(old_key)
            self.motor_states.pop(old_key)
            new_key = (old_key[0], value)
            self.motors[new_key] = Motor(value, old_key[0], old.image)
            self.motor_states[new_key] = 'Unverified'
            self.active_key = new_key
            self._render_bus()
            self._select_motor(new_key)
        self.submit('Check bus and write ID', lambda: self.controller.set_id(new_id,
            current_id=sid, detected_ids=known_ids, isolated=True), done)

    def cancel_live(self):
        for job in self.jobs.values():
            self.root.after_cancel(job)
        self.jobs.clear()

    def _live_changed(self, key):
        if not self.live.get() or (key == 'position' and not self.armed.get()):
            return
        if key in self.jobs:
            self.root.after_cancel(self.jobs.pop(key))
        def send():
            self.jobs.pop(key, None)
            if self.live.get():
                if self.future is not None:
                    self.jobs[key] = self.root.after(100, send)
                    return
                if key == 'position':
                    self.move(silent=True)
                else:
                    self.set_ram(1 if key == 'stretch' else 2, silent=True)
        self.jobs[key] = self.root.after(100, send)

    def move(self, silent=False):
        if not self.require():
            return
        if not self.armed.get():
            self.status.set('Tick Enable motion before sending a position command.')
            return
        try:
            pos = self.position.get()
            if self.image_target == self.target_token():
                lo, hi = self.image.get('limit_ccw'), self.image.get('limit_cw')
                if not lo <= pos <= hi:
                    raise ValueError(f'Position exceeds the read servo limits ({lo}..{hi})')
        except ValueError as exc:
            self.status.set(str(exc)); return
        self.free_monitor.set(False)
        sid = self.selected_id()
        self.submit('Set position', lambda: self.controller.set_position(sid, pos),
                    lambda n: self._measured(n, 'Position command accepted; reply is the position at command receipt'), silent=silent)

    def neutral(self):
        self.position.set(7500)
        self.move()

    def _measured(self, value, status):
        self.actual_pos.set(str(value))
        self.status.set(status)

    def free(self):
        self.cancel_live(); self.armed.set(False)
        if self.future is not None and self.controller:
            if not self.target_ready:
                self.status.set('Wait for motor selection / scan before sending FREE.')
                return
            self.pending_free = True
            self.status.set('FREE queued after the active serial transaction')
            return
        if self.require():
            sid = self.selected_id()
            self.submit('FREE', lambda: self.controller.free(sid),
                        lambda n: self._measured(n, 'Torque released; current position read'))

    def _free_monitor_changed(self):
        if self.free_monitor.get():
            self.free()

    def read_ram(self, sc):
        if not self.require():
            return
        sid = self.selected_id()
        def done(value):
            if sc == 5:
                self.actual_pos.set(str(value))
            elif sc == 4:
                self.temp.set(str(value))
            elif sc == 3:
                self.current.set(str(value))
            else:
                self.ram_controls['stretch' if sc == 1 else 'speed'].set(value)
            self.status.set('Parameter read successfully' + (' | ICS 3.6 position' if sc == 5 else ''))
        self.submit('Read parameter', lambda: self.controller.read_parameter(sid, sc), done)

    def set_ram(self, sc, silent=False):
        if not self.require():
            return
        key = 'stretch' if sc == 1 else 'speed'
        try:
            value = self.ram_controls[key].get()
        except ValueError as exc:
            self.status.set(str(exc)); return
        sid = self.selected_id()
        self.submit('Set ' + key, lambda: self.controller.write_parameter(sid, sc, value), silent=silent)

    def stop_polling(self):
        self.poll_temp.set(False); self.poll_current.set(False); self.free_monitor.set(False)

    def _poll(self):
        if self.closing:
            return
        if self.controller and self.target_ready and self.future is None and not self.pending_free:
            scs = ([4] if self.poll_temp.get() else []) + ([3] if self.poll_current.get() else [])
            free = self.free_monitor.get()
            sid = self.selected_id()
            if scs or free:
                def work():
                    result = {sc: self.controller.read_parameter(sid, sc) for sc in scs}
                    if free:
                        result[0] = self.controller.free(sid)
                    return result
                def done(result):
                    for sc, value in result.items():
                        {4: self.temp, 3: self.current, 0: self.actual_pos}[sc].set(str(value))
                    self.status.set('Monitoring | 200 ms minimum interval')
                self.submit('Monitor', work, done, silent=True)
        self.poll_job = self.root.after(200, self._poll)

    def apply_image(self, image, hardware=False, origin='file'):
        self.image = image
        for key, value in image.values().items():
            self.fields[key].set(value)
        try:
            self.config_baud.set(image.baud)
        except ValueError:
            # Factory presets commonly leave baud as 255/255 (unspecified).
            if self.transport:
                self.config_baud.set(self.transport.baud)
        try:
            for name, bit in FLAGS.items():
                self.flags[name].set(bool(image.flags & bit))
        except ValueError:
            pass
        try:
            self.version.set(str(image.raw(32)))
        except ValueError:
            self.version.set('unknown')
        if hardware:
            self.image_target = self.target_token()
            self.target_ready = True
            self.source_label.set(f'EEPROM read from ID {self.selected_id()} at {self.active_key[0]} baud. '
                                  'Calibration and reserved bytes are preserved on write.')
        else:
            self.image_target = None
            self.source_label.set(f'Loaded {origin}. Only displayed settings will be applied; hardware calibration is preserved.')

    def read_eeprom(self):
        if self.require(target=False) and self.active_key is not None:
            self.stop_polling(); self.cancel_live()
            self._select_motor(self.active_key)

    def field_values(self):
        return {key: control.get() for key, control in self.fields.items()}

    def flag_value(self):
        return sum(bit for name, bit in FLAGS.items() if self.flags[name].get())

    def write_eeprom(self):
        if not self.require():
            return
        if self.image is None:
            messagebox.showinfo('Read first', 'Read EEPROM or open a matching model preset before writing.', parent=self.root)
            return
        try:
            values, baud, flags = self.field_values(), self.config_baud.get(), self.flag_value()
        except ValueError as exc:
            messagebox.showerror('Invalid configuration', str(exc), parent=self.root); return
        sid = self.selected_id()
        old_key = self.active_key
        new_key = (baud, sid)
        if new_key != old_key and new_key in self.motors:
            messagebox.showerror('Address already detected',
                f'ID {sid} has already been detected at {baud} baud. '
                'Changing baud would create an address conflict.', parent=self.root)
            return
        warning = '\nSlave mode suppresses replies and may prevent readback.' if flags & FLAGS['slave'] else ''
        details = '\n'.join(f'{BY_KEY[k].label}: {v}' for k, v in values.items())
        if not messagebox.askokcancel('Write EEPROM',
                f'Write displayed settings to the selected motor, ID {sid} at {old_key[0]} baud?\n\n{details}\n'
                f'Baud: {baud}   Flags: 0x{flags:02X}\n\n'
                'A fresh read and local backup precede the write. Calibration and ID are preserved.' + warning,
                parent=self.root):
            return
        self.stop_polling(); self.cancel_live(); self.armed.set(False)
        # The legacy broadcast recovery is never used by this bus GUI.
        # Retained only as an explicitly isolated low-level compatibility API.
        def done(result):
            self.baud.set(str(self.transport.baud))
            if result.verified:
                self.motors.pop(old_key, None)
                self.motor_states.pop(old_key, None)
                self.active_key = new_key
                self.motors[new_key] = Motor(sid, baud, result.image)
                self.motor_states[new_key] = 'Ready'
                self.target_label.set(f'ID {sid:02d}  ·  {baud:,} baud')
                self.apply_image(result.image, hardware=True)
                messagebox.showinfo('EEPROM verified', result.message + ('\nBackup: ' + result.backup if result.backup else ''), parent=self.root)
            else:
                self.image_target = None
                self.target_ready = False
                self.motor_states[old_key] = 'Unverified'
                self.source_label.set(f'ID {sid}: write unverified. Scan all baud rates or power-cycle and rescan before another write.')
                messagebox.showwarning('Write not verified', result.message + '\nBackup: ' + result.backup, parent=self.root)
            self._render_bus()
            self.status.set(result.message)
            self.emit('RESULT', result.message)
        self.submit('Write and verify EEPROM',
            lambda: self.controller.write_config(sid, values, baud, flags, self.backup_dir), done)

    def open_file(self):
        if self.future is not None:
            self.status.set('Wait for the current transaction before loading settings.'); return
        path = filedialog.askopenfilename(parent=self.root, title='Open Kondo settings or preset',
            filetypes=[('Kondo settings', '*.sdt *.json'), ('All files', '*')])
        if path:
            try:
                image = load_image(path)
                # Validate known user fields before replacing the form.
                for key, value in image.values().items():
                    f = BY_KEY[key]
                    if not f.minimum <= value <= f.maximum:
                        raise ValueError(f'{f.label} out of range in file: {value}')
                self.stop_polling(); self.cancel_live(); self.armed.set(False)
                self.apply_image(image, origin=Path(path).name)
            except Exception as exc:
                messagebox.showerror('Load settings', str(exc), parent=self.root)

    def save_file(self):
        if self.image is None:
            messagebox.showinfo('No configuration', 'Read EEPROM or open a preset first.', parent=self.root); return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.sdt',
            filetypes=[('Kondo settings', '*.sdt'), ('JSON image', '*.json')])
        if path:
            try:
                image = self.image.copy()
                for key, value in self.field_values().items():
                    image.set(key, value)
                image.put(26, 2, BAUD_CODES[self.config_baud.get()])
                try:
                    old_flags = image.flags
                except ValueError:
                    old_flags = 4
                mask = sum(FLAGS.values())
                image.put(14, 2, (old_flags & ~mask) | self.flag_value() | 4)
                save_image(path, image, note='Displayed settings; hardware calibration is preserved on write')
                self.status.set('Saved ' + path)
            except Exception as exc:
                messagebox.showerror('Save settings', str(exc), parent=self.root)

    def save_log(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.log',
                                           filetypes=[('Communication log', '*.log')])
        if path:
            try:
                Path(path).write_text('\n'.join(self.log_history) + '\n')
                self.status.set('Log saved')
            except OSError as exc:
                messagebox.showerror('Save log', str(exc), parent=self.root)

    def clear_log(self):
        self.log_history.clear()
        self.log_text.configure(state='normal'); self.log_text.delete('1.0', 'end'); self.log_text.configure(state='disabled')

    def connection_settings(self):
        if self.controller or self.future is not None:
            messagebox.showinfo('Connection settings', 'Disconnect before changing transport settings.', parent=self.root); return
        win = tk.Toplevel(self.root)
        win.title('Connection settings')
        win.transient(self.root); win.grab_set()
        body = ttk.Frame(win, padding=18); body.pack(fill='both', expand=True)
        echo = tk.BooleanVar(value=self.settings.echo)
        variables = {
            'timeout': tk.StringVar(value=str(round(self.settings.timeout * 1000))),
            'pre_tx_delay': tk.StringVar(value=str(round(self.settings.pre_tx_delay * 1000))),
            'eeprom_timeout': tk.StringVar(value=str(round(self.settings.eeprom_timeout * 1000))),
        }
        ttk.Checkbutton(body, text='Adapter echoes TX (Kondo USB adapters: enabled)', variable=echo).grid(row=0, column=0, columnspan=2, sticky='w')
        labels = {'timeout': 'Reply timeout (ms)', 'pre_tx_delay': 'Pre-TX delay (ms)', 'eeprom_timeout': 'EEPROM timeout (ms)'}
        for row, (key, variable) in enumerate(variables.items(), start=1):
            ttk.Label(body, text=labels[key]).grid(row=row, column=0, sticky='w', pady=5)
            ttk.Entry(body, textvariable=variable).grid(row=row, column=1, padx=(15, 0))
        ttk.Label(body, text='Bus mode uses addressed commands; broadcast recovery is disabled.',
                  style='Muted.TLabel').grid(row=4, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Label(body, text='Kondo USB adapter | 8 data bits | even parity | 1 stop bit\n'
            'The adapter handles bus direction. Keep TX echo enabled for normal operation.',
            style='Muted.TLabel').grid(row=5, column=0, columnspan=2, sticky='w', pady=7)
        def apply():
            try:
                settings = replace(self.settings, echo=echo.get(), legacy_recovery=False,
                    timeout=float(variables['timeout'].get()) / 1000,
                    pre_tx_delay=float(variables['pre_tx_delay'].get()) / 1000,
                    eeprom_timeout=float(variables['eeprom_timeout'].get()) / 1000)
                settings.validate()
                self.settings = settings
                win.destroy()
            except Exception as exc:
                messagebox.showerror('Connection settings', str(exc), parent=win)
        ttk.Button(body, text='Apply', command=apply).grid(row=6, column=1, sticky='e', pady=(8, 0))

    def help(self):
        messagebox.showinfo('ICS Linux Manager',
            f'Independent Python / Tkinter manager {__version__}\n\n'
            'Connect the bus, select port and baud, then Connect. IDs 0–31 are scanned without motion.\n'
            'Auto / Scan all baud rates searches 115200, 625000 and 1250000.\n'
            'Select a motor in the list to load and edit its settings. Host baud follows the selection.\n\n'
            'ID changes are broadcast: disconnect other motors, rescan, confirm physical isolation.\n'
            'Previously detected IDs remain reserved for this session, even when no longer responding.\n'
            'Silent motors and multiple motors sharing an ID cannot be reliably discovered.\n\n'
            'Enable motion before Move / Neutral; Live sliders sends changes after 100 ms.\n'
            'FREE releases torque. Monitor FREE repeatedly releases torque and reads position.\n'
            'Read (3.6) obtains position without releasing torque; unsupported by ICS 3.5.\n\n'
            'EEPROM writes first read hardware, back up, preserve calibration, write once, then verify.\n'
            'Open original Kondo .sdt presets to restore model-specific settings.\n'
            'Temperature and current are raw protocol values, not degrees C or amperes.\n\n'
            'Hardware validation is still required. See README.md and docs/PROTOCOL_FINDINGS.md.', parent=self.root)

    def close(self):
        self.closing = True
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        if self.demo_job:
            self.root.after_cancel(self.demo_job)
            self.demo_job = None
        self.stop_polling(); self.cancel_live(); self.pending_free = False
        self.status.set('Closing after the active transaction finishes...')


def main():
    parser = argparse.ArgumentParser(description='Kondo ICS 3.5/3.6 Linux desktop manager')
    parser.add_argument('--demo', action='store_true', help='Use three virtual motors; no serial hardware access')
    args = parser.parse_args()
    root = tk.Tk()
    App(root, demo=args.demo)
    root.mainloop()

if __name__ == '__main__':
    main()
