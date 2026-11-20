"""Guitar notes from the ESP-IDF guitar tuner (``guitar_tuner/src/main.cpp``),
read over USB serial with `pyserial`.

The ESP32 runs pitch detection on the guitar signal and prints the nearest note
name, one per line, when a new note has held for a couple of analysis frames::

    Guitar tuner ready (ESP32, I2S 48000 Hz, analysis 24000 Hz).
    E2
    A#3
    Bb3        (if the firmware is built with USE_SHARPS = false)

A note is printed once when it starts, not repeatedly while it rings. Silence
resets the firmware, so plucking the same note again prints it again. Other
lines (the banner, ESP-IDF boot logs) are ignored.

Each printed note raises that note's gate for ``hold`` seconds, or until a
different note is printed. With the default hold, one pluck becomes one key tap
on the keyboard sink. Plucking the same note again while its gate is still up
drops the gate for one tick first, so the key is pressed again.

Ports
-----
  note.E2 .. note.E6   event   one gate per note across the guitar's range
                               (sharps are spelled ``s``: ``note.Cs3`` is C#3,
                               and flats from the firmware map to the same ports)
  note.active          event   any note is up
  pitch.midi           scalar  MIDI number of the current note (E2 = 40), 0 when none
  pitch.hz             scalar  equal-tempered frequency of that note (A4 = 440 Hz)
  fret                 scalar  fret of that note on an E string, 0-24 (E2 = 0)

With ``port`` left empty, the first USB serial device that looks like an ESP32
dev board (CP210x, CH34x, FTDI, Espressif) is used. The port is reopened if the
board is unplugged.
"""

from __future__ import annotations

import re
import threading
import time

from engine.registry import register_source
from engine.signals import PortSpec, SCALAR, EVENT
from sources.base import Source

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_PC = {**{n: i for i, n in enumerate(NOTES)},
       "Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10, "Cb": 11, "Fb": 4, "E#": 5, "B#": 0}

LOW, HIGH = 40, 88           # E2 (open low E) .. E6 (24th fret, high E)

# USB-UART bridges found on ESP32 dev boards, by USB vendor id.
_ESP_VIDS = {0x10C4: "CP210x", 0x1A86: "CH34x", 0x0403: "FTDI", 0x303A: "Espressif"}

# A whole line that is just a note name: "E2", "A#3", "Bb3", "C-1".
_LINE = re.compile(r"^\s*([A-G][#b]?)(-?\d+)\s*$")


def note_name(midi: int) -> str:
    return f"{NOTES[midi % 12]}{midi // 12 - 1}"


def note_port(midi: int) -> str:
    return "note." + note_name(midi).replace("#", "s")


PORTS = [PortSpec(note_port(m), EVENT, f"note.{note_name(m)}", "notes", "",
                  f"MIDI {m}, fret {m - LOW} on the low E string")
         for m in range(LOW, HIGH + 1)]
PORTS += [
    PortSpec("note.active", EVENT, "note.active", "pitch", "", "any note is up"),
    PortSpec("pitch.midi", SCALAR, "pitch.midi", "pitch", "", "E2 = 40, A4 = 69; 0 when none"),
    PortSpec("pitch.hz", SCALAR, "pitch.hz", "pitch", "Hz", "equal-tempered; 0 when none"),
    PortSpec("fret", SCALAR, "fret", "pitch", "", "fret on an E string, E2 = 0"),
]
_SILENT = {p.id: 0.0 for p in PORTS}


def parse_line(line: str) -> int | None:
    """MIDI number of one line of the tuner's output, or None."""
    m = _LINE.match(line)
    if not m or m.group(1) not in _PC:
        return None
    return 12 * (int(m.group(2)) + 1) + _PC[m.group(1)]


def ports_for(midi: int) -> dict:
    """Every port's value while *midi* is up."""
    out = dict(_SILENT)
    if LOW <= midi <= HIGH:
        out[note_port(midi)] = 1.0
    out["note.active"] = 1.0
    out["pitch.midi"] = float(midi)
    out["pitch.hz"] = 440.0 * 2 ** ((midi - 69) / 12)
    out["fret"] = float(midi - LOW)
    return out


def find_port() -> str | None:
    """The first serial device that looks like an ESP32 board."""
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.vid in _ESP_VIDS:
            return p.device
    return None


@register_source
class GuitarNotes(Source):
    type = "guitar_notes"
    id = "guitar_notes"
    label = "Guitar notes (ESP32 tuner)"
    PORTS = PORTS
    ARGS = {
        "port": {"default": None, "type": str,
                 "help": "serial device, e.g. /dev/cu.usbserial-210 or COM5; empty = auto-detect"},
        "baud": {"default": 115200, "type": int, "help": "the firmware's console baud rate"},
        "hold": {"default": 0.12, "type": float,
                 "help": "seconds a note's gate stays up after it is printed (0 = one tick)"},
    }

    @classmethod
    def available(cls):
        try:
            import serial  # noqa: F401
        except Exception as e:
            return False, f"needs pyserial ({e})"
        return True

    def __init__(self, port=None, baud=115200, hold=0.12):
        import serial  # noqa: F401  (fail at construction, like the other sources)
        self._port = (port or "").strip() or None
        self._baud = int(baud)
        self._hold = max(0.0, float(hold))
        self._state = "searching"
        self._lock = threading.Lock()
        self._midi = None         # last note printed
        self._seen = 0.0          # perf_counter when it was printed
        self._count = 0           # notes printed so far
        self._shown = 0           # _count as of the last read() that showed a note
        self._shown_midi = None   # the note that read() showed, if any
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name=self.type, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def status(self):
        return self._state

    def read(self, now: float | None = None):
        now = time.perf_counter() if now is None else now
        with self._lock:
            fresh = self._count != self._shown
            if self._midi is None or not (fresh or now - self._seen <= self._hold):
                self._shown_midi = None
                return dict(_SILENT)
            if fresh and self._shown_midi == self._midi:
                # Same note plucked again while its gate is up: drop it for one
                # tick so the sink sees a new press.
                self._shown_midi = None
                return dict(_SILENT)
            self._shown = self._count
            self._shown_midi = self._midi
            return ports_for(self._midi)

    def feed(self, line: str, now: float | None = None) -> bool:
        """Take one line of the tuner's output. True if it held a note."""
        midi = parse_line(line)
        if midi is None:
            return False
        with self._lock:
            self._midi = midi
            self._seen = time.perf_counter() if now is None else now
            self._count += 1
        return True

    # ------------------------------------------------------------------
    def _run(self):
        import serial
        while not self._stop.is_set():
            device = self._port or find_port()
            if device is None:
                self._state = "searching"
                self._stop.wait(1.0)
                continue
            try:
                # The timeout keeps readline() from blocking stop(). Opening the
                # port resets most ESP32 boards; the firmware reprints its banner.
                with serial.Serial(device, self._baud, timeout=0.2) as ser:
                    self._state = f"ok: {device}"
                    while not self._stop.is_set():
                        raw = ser.readline()
                        if raw:
                            self.feed(raw.decode("ascii", errors="replace"))
            except (serial.SerialException, OSError) as e:
                self._state = f"disconnected: {e}" if self._port else "searching"
                self._stop.wait(1.0)
        self._state = "stopped"
