"""Arduino Nano + 4 bend/flex sensors over USB serial.

Pairs with ``sources/bend4/firmware/bend4_nano/bend4_nano.ino`` — four flex sensors wired to
analog pins A2-A5, read as 10-bit ADC values (0-1023) and streamed one
whitespace-separated line per sample::

    raw2    raw3    raw4    raw5\\n

one frame = four ints. The four channels are exposed in wire order (A2, A3, A4,
A5) as ``bend.0 .. bend.3``, plus a derived ``bend.max`` (the most-bent of the
four, handy as a single "anything bent?" signal). Any line that doesn't parse to
exactly four ints is silently skipped, so a startup banner or a torn frame never
corrupts a real sample; it just costs one frame.

Note the baud: the sketch uses ``Serial.begin(9600)``, so this source defaults
to 9600 (not 115200 like the BMI160 one). Change both sides together if you
reconfigure the sketch.

Normalisation
-------------
By default each channel is reported as ``raw / 1023`` (0.0 flat .. 1.0 at the ADC
ceiling). Flex sensors rarely swing the full ADC range, so pass ``autoscale=1``
to instead map each channel onto 0..1 from the min/max it has seen so far — bend
every sensor through its full travel once after start to establish the range.
"""

from __future__ import annotations

import threading
import time

from engine.registry import register_source
from engine.signals import PortSpec, SCALAR
from sources.base import Source

ADC_MAX = 1023.0          # 10-bit ADC full scale
N_CHANNELS = 4            # A2, A3, A4, A5


def _as_bool(v) -> bool:
    """Coerce a CLI arg to bool. ``bool("0")`` is True, so parse the string."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)

# USB-UART chips found on typical Nano clones, for port auto-detection.
_KNOWN_VID_PID = {
    (0x1A86, 0x7523),  # CH340
    (0x0403, 0x6001),  # FTDI FT232
    (0x10C4, 0xEA60),  # CP2102/CP2104
    (0x2341, 0x0043),  # genuine Arduino Uno/Nano (some clones)
}

PORTS = [
    PortSpec("bend.0", SCALAR, "bend.0 (A2)", "flex", "", "0 flat .. 1 fully bent"),
    PortSpec("bend.1", SCALAR, "bend.1 (A3)", "flex", "", "0 flat .. 1 fully bent"),
    PortSpec("bend.2", SCALAR, "bend.2 (A4)", "flex", "", "0 flat .. 1 fully bent"),
    PortSpec("bend.3", SCALAR, "bend.3 (A5)", "flex", "", "0 flat .. 1 fully bent"),
    PortSpec("bend.max", SCALAR, "bend.max", "derived", "", "most-bent of the four channels"),
]


def _autodetect_port():
    import serial.tools.list_ports as list_ports
    candidates = list(list_ports.comports())
    for p in candidates:
        if (p.vid, p.pid) in _KNOWN_VID_PID:
            return p.device
    for p in candidates:
        if "arduino" in (p.description or "").lower():
            return p.device
    raise RuntimeError(
        "no Arduino-like serial port found; pass one explicitly, e.g. "
        "--source bend4:port=/dev/ttyUSB0 (Linux), COM3 (Windows), "
        "/dev/cu.usbserial-XXXX (macOS)")


@register_source
class ArduinoBend4(Source):
    type = "bend4"
    id = "bend4"
    label = "4-Way Bend Sensor (Arduino Nano)"
    PORTS = PORTS

    @classmethod
    def available(cls):
        try:
            import serial  # noqa: F401
        except Exception as e:
            return False, f"needs pyserial ({e})"
        return True

    ARGS = {
        "port": {"default": None, "type": str,
                 "help": "serial device; auto-detected if omitted"},
        "baud": {"default": 9600, "type": int,
                 "help": "must match Serial.begin() in the sketch"},
        "autoscale": {"default": False, "type": _as_bool,
                      "help": "map each channel to 0..1 from the min/max seen so "
                              "far (else raw/1023)"},
    }

    def __init__(self, port=None, baud=9600, autoscale=False):
        self._port_arg = port
        self._baud = baud
        self._autoscale = bool(autoscale)
        self._ser = None
        self._ok = False
        self._lock = threading.Lock()
        self._latest = {p.id: 0.0 for p in self.PORTS}
        self._min = [None] * N_CHANNELS   # per-channel running range for autoscale
        self._max = [None] * N_CHANNELS
        self._stop = threading.Event()
        self._thread = None
        self._frames = 0        # valid 4-int frames parsed since start
        self._unparsed = 0      # consecutive non-empty lines that didn't parse
        self._warned_format = False

        import serial  # raises ImportError if pyserial isn't installed
        self._serial_mod = serial
        # Open eagerly so a bad --source arg fails fast instead of degrading
        # to a "disconnected" module the UI silently shows no data for.
        self._open()

    # ------------------------------------------------------------------
    def _open(self):
        port = self._port_arg or _autodetect_port()
        try:
            self._ser = self._serial_mod.Serial(port, self._baud, timeout=0.5)
        except self._serial_mod.SerialException as e:
            # errno 16 (EBUSY) almost always means another program already has
            # the port open — on macOS the Arduino IDE's Serial Monitor is the
            # usual culprit, and cu.* / tty.* are the same device so either one
            # locks out the other. Say so instead of surfacing "Resource busy".
            if getattr(e, "errno", None) == 16 or "Resource busy" in str(e):
                raise RuntimeError(
                    f"{port} is busy — another program has it open. Close the "
                    "Arduino Serial Monitor (or any other serial console), then "
                    "retry.") from e
            raise
        self._port_arg = port  # remember what we resolved to, for reconnects

    def start(self):
        self._thread = threading.Thread(target=self._poll, name="bend4", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass

    def status(self):
        if not self._ok:
            return "disconnected"
        # Bytes are arriving but nothing parses as a 4-int frame — almost always
        # the wrong sketch or a baud mismatch. Say so rather than reporting "ok"
        # while every port silently reads its initial 0.
        if self._frames == 0 and self._unparsed > 0:
            return "error: receiving data but no valid frames (wrong sketch or baud?)"
        return "ok"

    def read(self):
        with self._lock:
            return dict(self._latest)

    # ------------------------------------------------------------------
    def _poll(self):
        while not self._stop.is_set():
            try:
                line = self._ser.readline()
                self._ok = True  # the port is open and readable
                if not line:
                    continue  # read timeout with nothing waiting; keep last value
                self._ingest(line)
            except Exception:
                self._ok = False
                self._reconnect()

    def _reconnect(self):
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        time.sleep(1.0)
        try:
            self._open()
        except Exception:
            pass  # keep retrying on the next loop iteration

    def _normalise(self, i: int, raw: int) -> float:
        """Map one channel's raw ADC count to 0..1."""
        if not self._autoscale:
            return raw / ADC_MAX
        lo, hi = self._min[i], self._max[i]
        lo = raw if lo is None else min(lo, raw)
        hi = raw if hi is None else max(hi, raw)
        self._min[i], self._max[i] = lo, hi
        if hi <= lo:
            return 0.0  # not enough travel seen yet to establish a range
        return (raw - lo) / (hi - lo)

    def _ingest(self, line: bytes):
        text = line.decode("ascii", "ignore").strip()
        parts = text.split()  # sketch separates columns with runs of spaces
        ints = None
        if len(parts) == N_CHANNELS:
            try:
                ints = tuple(int(x) for x in parts)
            except ValueError:
                ints = None
        if ints is None:
            # Not a data frame. A '#'-prefixed banner is tolerated; anything else
            # that keeps coming means the wrong sketch or baud. Warn once (only
            # before we've ever seen a real frame) so the operator gets a pointer
            # instead of a silent stream of zeros.
            if text and not text.startswith("#"):
                self._unparsed += 1
                if (self._frames == 0 and self._unparsed >= 20
                        and not self._warned_format):
                    self._warned_format = True
                    print(f"[bend4] receiving data on {self._port_arg} but no "
                          f"line parses as four ints (got e.g. {text!r}). Wrong "
                          f"sketch, or baud != {self._baud}? Upload "
                          "sources/bend4/firmware/bend4_nano/bend4_nano.ino.")
            return  # a banner line or a torn frame; wait for the next one
        self._frames += 1
        self._unparsed = 0

        vals = [self._normalise(i, raw) for i, raw in enumerate(ints)]
        out = {f"bend.{i}": v for i, v in enumerate(vals)}
        out["bend.max"] = max(vals)
        with self._lock:
            self._latest = out
