"""64 wire-nibble EEPROM image. Offsets here are zero-based wire offsets.

Mapping checked against IcsBaseClass.dll supplied with Manager 1.2.1.0.
A disk preset may contain 255 for unknown fields; it must never go on the wire.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import xml.etree.ElementTree as ET

BAUD_CODES = {115200: 10, 625000: 1, 1250000: 0}
FLAGS = {'reverse': 0x01, 'serial': 0x08, 'slave': 0x80, 'rotation': 0x10}

@dataclass(frozen=True)
class Field:
    key: str
    label: str
    offset: int
    width: int
    minimum: int
    maximum: int
    default: int
    scale: int = 1

FIELDS = (
    Field('stretch', 'Stretch', 2, 2, 1, 127, 30, 2),
    Field('stretch1', 'Stretch 1 (SET1)', 58, 2, 1, 127, 60, 2),
    Field('stretch2', 'Stretch 2 (SET2)', 60, 2, 1, 127, 30, 2),
    Field('stretch3', 'Stretch 3 (SET3)', 62, 2, 1, 127, 127, 2),
    Field('speed', 'Speed', 4, 2, 1, 127, 127),
    Field('punch', 'Punch', 6, 2, 0, 10, 1),
    Field('dead_band', 'Dead band', 8, 2, 0, 10, 2),
    Field('response', 'Response', 50, 2, 1, 5, 3),
    Field('damping', 'Damping', 10, 2, 1, 255, 40),
    Field('protection', 'Protection', 12, 2, 10, 255, 250),
    Field('limit_cw', 'Limit (CW)', 16, 4, 8000, 11500, 11500),
    Field('limit_ccw', 'Limit (CCW)', 20, 4, 3500, 7500, 3500),
    Field('temperature_limit', 'Temperature limit (raw)', 28, 2, 1, 127, 80),
    Field('current_limit', 'Current limit (raw)', 30, 2, 0, 63, 63),
    Field('user_offset', 'User offset', 52, 2, -127, 127, 0),
)
BY_KEY = {f.key: f for f in FIELDS}


def integer(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}, got {value!r}')
    return value


class Eeprom:
    def __init__(self, data):
        if len(data) != 64:
            raise ValueError('EEPROM image must contain exactly 64 wire bytes')
        self.data = bytearray(integer(x, 0, 255, 'EEPROM byte') for x in data)

    def copy(self):
        return Eeprom(self.data)

    def raw(self, offset, width=2):
        values = self.data[offset:offset + width]
        if len(values) != width or any(x > 15 for x in values):
            raise ValueError(f'Unknown/non-nibble EEPROM field at offset {offset}')
        n = 0
        for x in values:
            n = (n << 4) | x
        return n

    def put(self, offset, width, value):
        integer(value, 0, (1 << (4 * width)) - 1, 'encoded field')
        self.data[offset:offset + width] = bytes((value >> (4 * i)) & 15 for i in reversed(range(width)))

    def validate_live(self):
        if any(x > 15 for x in self.data):
            raise ValueError('Live EEPROM contains non-nibble data; check echo mode, baud, wiring or collisions')
        if self.raw(0) != 0x5A:
            raise ValueError('EEPROM signature is not 0x5A; refusing configuration write')
        return self

    def get(self, key):
        f = BY_KEY[key]
        value = self.raw(f.offset, f.width)
        if key == 'user_offset':
            return -(value if value < 128 else value - 256)
        return value // f.scale

    def values(self):
        result = {}
        for f in FIELDS:
            try:
                result[f.key] = self.get(f.key)
            except ValueError:
                pass
        return result

    def set(self, key, value):
        f = BY_KEY[key]
        integer(value, f.minimum, f.maximum, f.label)
        encoded = (-value) & 255 if key == 'user_offset' else value * f.scale
        self.put(f.offset, f.width, encoded)

    @property
    def baud(self):
        code = self.raw(26)
        for baud, n in BAUD_CODES.items():
            if n == code:
                return baud
        raise ValueError(f'Unknown EEPROM baud code 0x{code:02X}')

    @property
    def flags(self):
        return self.raw(14)

    def updated(self, values, baud, flags):
        """Overlay user fields on freshly read hardware image; preserve calibration/ID.

        Preserve all unexposed bits, including the live FREE bit. Set bit 2 like
        the original manager does. Never apply unknown preset bytes verbatim.
        """
        self.validate_live()
        if baud not in BAUD_CODES:
            raise ValueError('Unsupported baud rate')
        if set(values) - BY_KEY.keys():
            raise ValueError('Unknown configuration field')
        integer(flags, 0, 255, 'flags')
        out = self.copy()
        for key, value in values.items():
            out.set(key, value)
        mask = sum(FLAGS.values())
        out.put(14, 2, (self.flags & ~mask) | (flags & mask) | 0x04)
        out.put(26, 2, BAUD_CODES[baud])
        return out

    def differences(self, other):
        return [(i, a, b) for i, (a, b) in enumerate(zip(self.data, other.data)) if a != b]

    @classmethod
    def demo(cls):
        image = cls([0] * 64)
        image.put(0, 2, 0x5A)
        image.put(14, 2, 0x0C)
        image.put(26, 2, 10)
        image.put(32, 2, 36)
        for i in (34, 38, 42, 46):
            image.put(i, 4, 40000)
        image.put(54, 2, 128)
        for f in FIELDS:
            image.set(f.key, f.default)
        return image


def load_image(path):
    path = Path(path)
    if path.stat().st_size > 100_000:
        raise ValueError('Configuration file is unexpectedly large')
    if path.suffix.lower() == '.json':
        record = json.loads(path.read_text())
        return Eeprom(record['eeprom_data'])
    source = path.read_bytes()
    if b'<!DOCTYPE' in source.upper() or b'<!ENTITY' in source.upper():
        raise ValueError('DTD/entities are not allowed in SDT files')
    root = ET.fromstring(source)
    nodes = root.findall('./eepromData/unsignedByte')
    if not nodes:
        raise ValueError('Expected Kondo IcsBaseCommand/eepromData XML')
    return Eeprom([int(x.text) for x in nodes])


def save_image(path, image, *, note=''):
    path = Path(path)
    if path.suffix.lower() == '.json':
        path.write_text(json.dumps({'format': 'kondo-ics-linux-v1', 'note': note,
                                   'eeprom_data': list(image.data)}, indent=2) + '\n')
    else:
        root = ET.Element('IcsBaseCommand', {'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance',
                                           'xmlns:xsd': 'http://www.w3.org/2001/XMLSchema'})
        data = ET.SubElement(root, 'eepromData')
        for x in image.data:
            ET.SubElement(data, 'unsignedByte').text = str(x)
        ET.SubElement(root, 'IcsVersion').text = '0'
        ET.indent(root)
        ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
