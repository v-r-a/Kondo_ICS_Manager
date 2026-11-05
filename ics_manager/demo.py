"""Virtual single servo for interactive demo and protocol integration tests.

This models documented frames, not real electronics or timing characteristics.
"""
from .eeprom import Eeprom, BAUD_CODES

class DemoSerial:
    def __init__(self, echo=True, servo_id=0, baud=115200):
        self.echo, self.servo_id = echo, servo_id
        self.baudrate = self.servo_baud = baud
        self.timeout = 0.05
        self.is_open = True
        self.image = Eeprom.demo()
        self.image.put(56, 2, servo_id)
        self.image.put(26, 2, BAUD_CODES[baud])
        self.position = 7500
        self.ram = {1: 30, 2: 127, 3: 4, 4: 95}
        self.rx = bytearray()
        self.writes = []
        self.free_mode = True

    @property
    def in_waiting(self):
        return len(self.rx)

    def reset_input_buffer(self):
        self.rx.clear()

    def write(self, tx):
        tx = bytes(tx)
        self.writes.append(tx)
        if self.echo:
            self.rx.extend(tx)
        if self.baudrate != self.servo_baud:
            return len(tx)
        cmd, sid = tx[0] & 0xE0, tx[0] & 31
        reply = b''
        if cmd == 0xE0:
            if tx[1:] == b'\x01\x01\x01':
                self.servo_id = sid
                self.image.put(56, 2, sid)
            reply = bytes([0xE0 | self.servo_id])
        elif sid == self.servo_id:
            if cmd == 0x80:
                value = tx[1] * 128 + tx[2]
                self.free_mode = value == 0
                if value:
                    self.position = value
                reply = bytes([sid, self.position >> 7, self.position & 127])
            elif cmd == 0xA0:
                sc = tx[1]
                payload = bytes(self.image.data) if sc == 0 else (
                    bytes([self.position >> 7, self.position & 127]) if sc == 5 else bytes([self.ram[sc]]))
                reply = bytes([0x20 | sid, sc]) + payload
            elif cmd == 0xC0:
                sc = tx[1]
                if sc == 0:
                    self.image = Eeprom(tx[2:]).validate_live()
                    self.servo_baud = self.image.baud
                    self.ram[1], self.ram[2] = self.image.get('stretch'), self.image.get('speed')
                    reply = bytes([0x40 | sid, 0])
                else:
                    if sc in (1, 2):
                        self.ram[sc] = tx[2]
                    reply = bytes([0x40 | sid, sc, tx[2]])
        self.rx.extend(reply)
        return len(tx)

    def flush(self):
        pass

    def read(self, count):
        data = bytes(self.rx[:count])
        del self.rx[:count]
        return data

    def close(self):
        self.is_open = False


class DemoBusSerial:
    """Several independently addressed motors behind one echoing adapter.

    Baud changes are immediate in this simulation. Real hardware may need a
    power cycle. This simulator does not reproduce electrical collisions.
    """
    def __init__(self, echo=True, motors=None, baud=115200):
        self.echo = echo
        self.motors = motors if motors is not None else [
            DemoSerial(echo=False, servo_id=sid) for sid in (0, 4, 12)]
        if motors is None:
            for motor, speed in zip(self.motors, (127, 90, 60)):
                motor.image.set('speed', speed)
                motor.ram[2] = speed
        self.baudrate = baud
        self.timeout = 0.05
        self.is_open = True
        self.rx = bytearray()
        self.writes = []

    @property
    def in_waiting(self):
        return len(self.rx)

    def reset_input_buffer(self):
        self.rx.clear()

    def write(self, tx):
        tx = bytes(tx)
        self.writes.append(tx)
        if self.echo:
            self.rx.extend(tx)
        for motor in self.motors:
            motor.echo = False
            motor.baudrate = self.baudrate
            motor.reset_input_buffer()
            motor.write(tx)
            self.rx.extend(motor.read(motor.in_waiting))
        return len(tx)

    def flush(self):
        pass

    def read(self, count):
        data = bytes(self.rx[:count])
        del self.rx[:count]
        return data

    def close(self):
        self.is_open = False
