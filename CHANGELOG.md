# Changelog

## 0.2.0 — Multi-motor bus manager

- Scan all 32 ICS IDs at the selected baud or all three supported rates using EEPROM reads only.
- Show responding and previously detected motors in a persistent session list with ID, baud and state.
- Select a motor to load fresh EEPROM and runtime stretch/speed; automatically set the host baud.
- Enable addressed EEPROM read/modify/write/readback with multiple motors connected. Keep backups, calibration preservation and single-attempt writes.
- Keep other motors accessible when the selected motor changes baud. Reject a baud change into a detected occupied address.
- Separate the selected motor from the requested new ID. Reject previously detected IDs, including no-reply entries and entries at different baud rates.
- Require physical isolation for broadcast ID programming; perform a fresh addressed scan immediately before writing and reject any extra responding motor or scan issue.
- Remove broadcast ID discovery and disable broadcast post-write recovery in the GUI.
- Stop polling/live work and disarm motion when the target changes. Block further writes after unverified writes or failed selection until a successful read.
- Count empty scan probes separately from communication errors.
- Demonstrate IDs 0, 4 and 12 with independent configuration and runtime state.

This version cannot make the ICS broadcast ID command address a single motor. It does not infer that only one motor is physically connected from one responding ID. Hardware validation remains outstanding.

## 0.1.0

Initial Linux Python/Tkinter version based on interoperability analysis of the supplied Windows manager, with Kondo USB echo handling, motion, runtime parameters, EEPROM backup/readback and settings files.
