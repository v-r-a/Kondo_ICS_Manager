"""Serialized half-duplex transactions, explicit echo handling, bounded reads.

No guessed microsecond delays and no reuse of IcsHardSerialClass.synchronize.
"""
from dataclasses import dataclass
import threading
import time

class CommunicationError(RuntimeError):
    pass

class ReplyTimeout(CommunicationError):
    def __init__(self, message, *, received=0, phase='reply'):
        super().__init__(message)
        self.received, self.phase = received, phase

@dataclass
class SerialSettings:
    port: str = '/dev/ttyUSB0'
    baud: int = 115200
    echo: bool = True
    timeout: float = 0.050
    pre_tx_delay: float = 0.015
    eeprom_timeout: float = 1.0
    legacy_recovery: bool = False

    def validate(self):
        if self.baud not in (115200, 625000, 1250000):
            raise ValueError('Unsupported ICS baud rate')
        if not 0.005 <= self.timeout <= 5 or not 0 <= self.pre_tx_delay <= 1:
            raise ValueError('Timeout must be 5..5000 ms; pre-TX delay 0..1000 ms')
        if not 0.1 <= self.eeprom_timeout <= 10:
            raise ValueError('EEPROM timeout must be 100..10000 ms')
class SerialTransport:
    def __init__(self, settings, log=None, serial_instance=None):
        settings.validate()
        self.settings = settings
        self.log = log or (lambda *args: None)
        self.lock = threading.RLock()
        self.ok = self.errors = self.transactions = self.no_reply = 0
        self.last_ms = 0.0
        self.serial = serial_instance
        try:
            if self.serial is None:
                import serial
                self.serial = serial.Serial(port=settings.port, baudrate=settings.baud,
                    bytesize=serial.EIGHTBITS, parity=serial.PARITY_EVEN,
                    stopbits=serial.STOPBITS_ONE, timeout=settings.timeout,
                    write_timeout=1, xonxoff=False, rtscts=False, dsrdtr=False, exclusive=True)
            self.set_baud(settings.baud)
        except Exception:
            self.close()
            raise

    @property
    def baud(self):
        return self.serial.baudrate

    def set_baud(self, baud):
        with self.lock:
            self.serial.baudrate = baud
            self.log('INFO', f'Host baud = {baud}, 8E1')

    def close(self):
        with self.lock:
            if self.serial:
                self.serial.close()

    def _read_exact(self, count, deadline, *, phase='reply'):
        output = bytearray()
        while len(output) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.serial.timeout = remaining
            chunk = self.serial.read(count - len(output))
            if not chunk:
                break
            output.extend(chunk)
        if output:
            self.log('RX', bytes(output).hex(' ').upper())
        if len(output) != count:
            raise ReplyTimeout(f'Expected {count} bytes, received {len(output)}: {output.hex(" ")}',
                               received=len(output), phase=phase)
        return bytes(output)

    def exchange(self, tx, response_length, *, prefix=None, timeout=None, probe=False):
        """Consume an exact echo (if configured), then a validated servo reply.

        All operations are one at a time. Deadline includes echo and response,
        with wire time added. No automatic retries of commands that change state.
        """
        tx = bytes(tx)
        with self.lock:
            started = time.monotonic()
            self.transactions += 1
            try:
                time.sleep(self.settings.pre_tx_delay)
                # Only before TX, never purge RX after sending (would discard replies).
                pending = self.serial.in_waiting
                if pending:
                    stale = self.serial.read(pending)
                    self.log('STALE', bytes(stale).hex(' ').upper())
                self.serial.reset_input_buffer()
                self.log('TX', tx.hex(' ').upper())
                written = self.serial.write(tx)
                if written != len(tx):
                    raise CommunicationError(f'Short write: {written}/{len(tx)} bytes')
                # The Kondo USB adapter handles half-duplex direction in hardware.
                self.serial.flush()
                wait = self.settings.timeout if timeout is None else timeout
                deadline = time.monotonic() + wait + 11 * (len(tx) + response_length) / self.baud
                if self.settings.echo:
                    echoed = self._read_exact(len(tx), deadline, phase='echo')
                    if echoed != tx:
                        raise CommunicationError(f'Echo mismatch: TX {tx.hex(" ")}; echo {echoed.hex(" ")}')
                reply = self._read_exact(response_length, deadline)
                if prefix is not None and not reply.startswith(prefix):
                    raise CommunicationError(f'Wrong reply header/subcommand: {reply.hex(" ")}; expected {prefix.hex(" ")}')
                self.ok += 1
                return reply
            except Exception as exc:
                if probe and isinstance(exc, ReplyTimeout) and not exc.received and exc.phase == 'reply':
                    self.no_reply += 1
                    self.log('EMPTY', 'Scan address: no response')
                else:
                    self.errors += 1
                    self.log('ERROR', str(exc))
                raise
            finally:
                self.last_ms = (time.monotonic() - started) * 1000
                self.log('TIME', f'{self.last_ms:.2f} ms')
