"""Any game controller, through SDL2's game controller API (`pysdl2`, with the
SDL2 library itself from `pysdl2-dll`).

One source for every scheme SDL knows: Xbox (XInput), PlayStation (DualShock 4,
DualSense), Nintendo (Switch Pro, Joy-Cons), 8BitDo, Steam, Stadia, Luna, and
generic DInput pads. SDL maps them all onto one Xbox-shaped layout, so the ports
are *positional*: ``btn.a`` is the bottom face button whatever is printed on it
(Cross on PlayStation, B on Nintendo), ``btn.lb``/``btn.rb`` are the bumpers.

What a controller doesn't have reads 0: motion (``accel.*``, ``gyro.*``,
``tilt.*``) needs a pad with sensors (PlayStation, Switch), the touchpad ports a
PlayStation pad, the paddles an Elite/Steam-style pad.

Controllers SDL has no mapping for (some DInput pads) are read as a raw joystick
in the common axis/button order; add a line to the ``SDL_GAMECONTROLLERCONFIG``
environment variable if that order is wrong for yours (see README.md).

The device is opened from the poll thread and re-found if it's unplugged, so the
source can be added before the controller is connected.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time

from engine.registry import register_source
from engine.signals import PortSpec, SCALAR, EVENT
from sources.base import Source

_MOTION = "PlayStation / Switch pads; 0 on pads without sensors"
_TOUCH = "PlayStation touchpad, first finger"

CONTROLLER_PORTS = [
    PortSpec("stick.lx", SCALAR, "stick.lx", "sticks", "-1..1"),
    PortSpec("stick.ly", SCALAR, "stick.ly", "sticks", "-1..1", "up is +"),
    PortSpec("stick.rx", SCALAR, "stick.rx", "sticks", "-1..1"),
    PortSpec("stick.ry", SCALAR, "stick.ry", "sticks", "-1..1", "up is +"),
    PortSpec("trig.l", SCALAR, "trig.l", "triggers", "0..1"),
    PortSpec("trig.r", SCALAR, "trig.r", "triggers", "0..1"),
]
BUTTONS = ["a", "b", "x", "y", "lb", "rb", "lt", "rt", "back", "start", "guide", "ls", "rs",
           "misc", "touchpad", "paddle1", "paddle2", "paddle3", "paddle4"]
_BUTTON_HINTS = {
    "a": "bottom face button (Cross / Nintendo B)", "b": "right face button (Circle / Nintendo A)",
    "x": "left face button (Square / Nintendo Y)", "y": "top face button (Triangle / Nintendo X)",
    "lt": "trig.l past halfway", "rt": "trig.r past halfway",
    "back": "View / Share / Minus", "start": "Menu / Options / Plus", "guide": "Xbox / PS / Home",
    "misc": "Xbox Share / PS5 mic / Switch Capture", "touchpad": "PlayStation touchpad click",
}
CONTROLLER_PORTS += [PortSpec(f"btn.{b}", EVENT, f"btn.{b}", "buttons", "", _BUTTON_HINTS.get(b, ""))
                     for b in BUTTONS]
CONTROLLER_PORTS += [PortSpec(f"dpad.{d}", EVENT, f"dpad.{d}", "dpad")
                     for d in ("up", "down", "left", "right")]
CONTROLLER_PORTS += [PortSpec(f"accel.{a}", SCALAR, f"accel.{a}", "motion", "g", _MOTION) for a in "xyz"]
CONTROLLER_PORTS += [
    PortSpec("accel.mag", SCALAR, "accel.mag", "motion", "g", "~1.0 at rest; spikes on shake"),
    PortSpec("gyro.x", SCALAR, "gyro.x (pitch rate)", "motion", "deg/s", "+ = front edge rising"),
    PortSpec("gyro.y", SCALAR, "gyro.y (yaw rate)", "motion", "deg/s", "+ = turning left"),
    PortSpec("gyro.z", SCALAR, "gyro.z (roll rate)", "motion", "deg/s", "+ = right side rising"),
    PortSpec("tilt.pitch", SCALAR, "tilt.pitch", "motion", "deg", "gravity-referenced; + = front up"),
    PortSpec("tilt.roll", SCALAR, "tilt.roll", "motion", "deg", "gravity-referenced; + = right side down"),
    PortSpec("touch.active", EVENT, "touch.active", "touchpad", "", _TOUCH),
    PortSpec("touch.x", SCALAR, "touch.x", "touchpad", "0..1", _TOUCH),
    PortSpec("touch.y", SCALAR, "touch.y", "touchpad", "0..1", _TOUCH + "; 0 = top"),
    PortSpec("touch.dx", SCALAR, "touch.dx", "touchpad", "pad/s", "drag speed, touchpad widths per second"),
    PortSpec("touch.dy", SCALAR, "touch.dy", "touchpad", "pad/s", "drag speed, touchpad heights per second"),
    PortSpec("battery", SCALAR, "battery", "status", "0..1", "coarse; 1 when wired, -1 unknown"),
]

TRIGGER_PRESSED = 0.5        # btn.lt / btn.rt: the analog trigger past this
STANDARD_GRAVITY = 9.80665   # SDL reports accel in m/s^2
EIGHTBITDO = 0x2DC8          # 8BitDo's USB vendor id

# SDL calls from more than one instance's poll thread go one at a time.
_SDL_LOCK = threading.Lock()


def _flag(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _sdl_available():
    try:
        import sdl2  # noqa: F401  (raises if the SDL2 library itself can't be found)
    except Exception as e:
        return False, f"needs pysdl2 + pysdl2-dll ({e})"
    return True


@register_source
class Controller(Source):
    type = "controller"
    id = "controller"
    label = "Game controller"
    PORTS = CONTROLLER_PORTS
    ARGS = {
        "name": {"default": None, "type": str,
                 "help": "use the first controller whose name contains this (case-insensitive)"},
        "index": {"default": 0, "type": int, "help": "which matching controller, 0 = first"},
        "dpad_from_stick": {"default": "auto", "type": str,
                            "help": "read the left stick as the d-pad: auto (8BitDo Zero 2), on, off"},
        "vertical_joycon": {"default": False, "type": _flag,
                            "help": "a single Joy-Con held upright (stick on top) instead of sideways"},
    }

    @classmethod
    def available(cls):
        return _sdl_available()

    def __init__(self, name=None, index=0, dpad_from_stick="auto", vertical_joycon=False):
        import sdl2  # noqa: F401  (fail at construction, like the other sources)
        self._name = (name or "").lower() or None
        self._index = int(index or 0)
        self._dpad_mode = str(dpad_from_stick or "auto").strip().lower()
        if self._dpad_mode not in ("auto", "on", "off"):
            raise ValueError("dpad_from_stick must be auto, on or off")
        self._vertical = _flag(vertical_joycon)
        self._state = "searching"
        self._lock = threading.Lock()
        self._latest = {p.id: 0.0 for p in self.PORTS}
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

    def read(self):
        with self._lock:
            return dict(self._latest)

    # ------------------------------------------------------------------
    def _run(self):
        import sdl2
        # No window of ours ever has focus, so input must count in the background;
        # keep A/B/X/Y positional on Nintendo-layout pads (SDL swaps them by default).
        # Hints are process-wide: the first instance to start sets them.
        sdl2.SDL_SetHint(sdl2.SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS, b"1")
        sdl2.SDL_SetHint(sdl2.SDL_HINT_GAMECONTROLLER_USE_BUTTON_LABELS, b"0")
        if self._vertical:
            sdl2.SDL_SetHint(sdl2.SDL_HINT_JOYSTICK_HIDAPI_VERTICAL_JOY_CONS, b"1")
        flags = sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK | sdl2.SDL_INIT_SENSOR
        with _SDL_LOCK:
            if sdl2.SDL_InitSubSystem(flags) != 0:
                self._state = f"error: {sdl2.SDL_GetError().decode(errors='replace')}"
                return
            # Polled, not evented: nothing here runs an SDL event loop.
            sdl2.SDL_JoystickEventState(sdl2.SDL_IGNORE)
            sdl2.SDL_GameControllerEventState(sdl2.SDL_IGNORE)
        dev = None
        try:
            while not self._stop.is_set():
                if dev is None:
                    with _SDL_LOCK:
                        sdl2.SDL_JoystickUpdate()   # also detects newly attached devices
                        dev = self._open()
                    if dev is None:
                        self._state = "searching"
                        self._publish({p.id: 0.0 for p in self.PORTS})
                        self._stop.wait(0.5)
                        continue
                    self._state = f"ok: {dev.name}" if dev.name else "ok"
                with _SDL_LOCK:
                    sdl2.SDL_GameControllerUpdate()
                    if not dev.attached():
                        dev.close()
                        dev = None
                        self._state = "disconnected"
                        continue
                    try:
                        out = dev.read()
                    except Exception:
                        out = None
                if out is not None:
                    self._publish(self._derive(out, dev))
                time.sleep(1 / 250)
        finally:
            with _SDL_LOCK:
                if dev is not None:
                    dev.close()
                sdl2.SDL_QuitSubSystem(flags)

    def _derive(self, out, dev):
        """Ports computed from others: d-pad from the stick, tilt and shake."""
        if self._dpad_mode == "on" or (self._dpad_mode == "auto" and dev.zero2):
            # No sticks on a Zero 2: in DInput/macOS modes its d-pad arrives as the left stick.
            lx, ly = out["stick.lx"], out["stick.ly"]
            for port, pressed in (("dpad.right", lx > 0.5), ("dpad.left", lx < -0.5),
                                  ("dpad.up", ly > 0.5), ("dpad.down", ly < -0.5)):
                if pressed:
                    out[port] = 1.0
            out["stick.lx"] = out["stick.ly"] = 0.0
        ax, ay, az = out["accel.x"], out["accel.y"], out["accel.z"]
        mag = math.sqrt(ax * ax + ay * ay + az * az)
        out["accel.mag"] = mag
        if mag > 0.2:   # only with a sensor reporting gravity
            # SDL's frame: +x right, +y up, +z toward the player. At rest the
            # accelerometer reads +1 g along "up".
            out["tilt.pitch"] = math.degrees(math.atan2(-az, math.hypot(ax, ay)))
            out["tilt.roll"] = math.degrees(math.atan2(-ax, math.hypot(ay, az)))
        return out

    def _publish(self, out):
        with self._lock:
            self._latest = out

    def _open(self):
        """Open the controller this instance should use, or None if there isn't one."""
        import sdl2
        found = []
        for i in range(sdl2.SDL_NumJoysticks()):
            name = (sdl2.SDL_JoystickNameForIndex(i) or b"").decode(errors="replace")
            found.append((i, name, sdl2.SDL_JoystickGetDeviceVendor(i)))
        if self._name:
            found = [f for f in found if self._name in f[1].lower()]
        if self._index >= len(found):
            return None
        i, name, vendor = found[self._index]
        zero2 = vendor == EIGHTBITDO and "zero" in name.lower()
        if sdl2.SDL_IsGameController(i):
            gc = sdl2.SDL_GameControllerOpen(i)
            return _Mapped(gc, name, zero2) if gc else None
        js = sdl2.SDL_JoystickOpen(i)
        return _Raw(js, name, zero2) if js else None


# ----------------------------------------------------------------- device readers
def _axis(v):
    """SDL axis (-32768..32767) -> -1..1."""
    return max(-1.0, min(1.0, v / 32767.0))


def _blank():
    out = {p.id: 0.0 for p in CONTROLLER_PORTS}
    out["battery"] = -1.0
    return out


def _battery(js):
    import sdl2
    level = sdl2.SDL_JoystickCurrentPowerLevel(js)
    return {sdl2.SDL_JOYSTICK_POWER_EMPTY: 0.05, sdl2.SDL_JOYSTICK_POWER_LOW: 0.2,
            sdl2.SDL_JOYSTICK_POWER_MEDIUM: 0.6, sdl2.SDL_JOYSTICK_POWER_FULL: 1.0,
            sdl2.SDL_JOYSTICK_POWER_WIRED: 1.0}.get(level, -1.0)


class _Mapped:
    """A device SDL has a game-controller mapping for: named, positional inputs."""

    def __init__(self, gc, name, zero2):
        import sdl2
        self.gc = gc
        self.name = name
        self.zero2 = zero2
        self.js = sdl2.SDL_GameControllerGetJoystick(gc)
        self.motion = []
        for kind in (sdl2.SDL_SENSOR_ACCEL, sdl2.SDL_SENSOR_GYRO):
            if (sdl2.SDL_GameControllerHasSensor(gc, kind)
                    and sdl2.SDL_GameControllerSetSensorEnabled(gc, kind, sdl2.SDL_TRUE) == 0):
                self.motion.append(kind)
        self.touchpad = sdl2.SDL_GameControllerGetNumTouchpads(gc) > 0
        self._buf = (ctypes.c_float * 3)()
        self._f = [ctypes.c_uint8(), ctypes.c_float(), ctypes.c_float(), ctypes.c_float()]
        self._touch_prev = None       # (x, y, time) of the last finger sample
        self._buttons = {
            "a": sdl2.SDL_CONTROLLER_BUTTON_A, "b": sdl2.SDL_CONTROLLER_BUTTON_B,
            "x": sdl2.SDL_CONTROLLER_BUTTON_X, "y": sdl2.SDL_CONTROLLER_BUTTON_Y,
            "lb": sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER,
            "rb": sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER,
            "back": sdl2.SDL_CONTROLLER_BUTTON_BACK, "start": sdl2.SDL_CONTROLLER_BUTTON_START,
            "guide": sdl2.SDL_CONTROLLER_BUTTON_GUIDE,
            "ls": sdl2.SDL_CONTROLLER_BUTTON_LEFTSTICK, "rs": sdl2.SDL_CONTROLLER_BUTTON_RIGHTSTICK,
            "misc": sdl2.SDL_CONTROLLER_BUTTON_MISC1, "touchpad": sdl2.SDL_CONTROLLER_BUTTON_TOUCHPAD,
            "paddle1": sdl2.SDL_CONTROLLER_BUTTON_PADDLE1, "paddle2": sdl2.SDL_CONTROLLER_BUTTON_PADDLE2,
            "paddle3": sdl2.SDL_CONTROLLER_BUTTON_PADDLE3, "paddle4": sdl2.SDL_CONTROLLER_BUTTON_PADDLE4,
        }
        self._dpad = {"up": sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP,
                      "down": sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN,
                      "left": sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT,
                      "right": sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT}

    def attached(self):
        import sdl2
        return bool(sdl2.SDL_GameControllerGetAttached(self.gc))

    def close(self):
        import sdl2
        sdl2.SDL_GameControllerClose(self.gc)

    def read(self):
        import sdl2
        gc = self.gc
        ax = lambda a: _axis(sdl2.SDL_GameControllerGetAxis(gc, a))   # noqa: E731
        bt = lambda b: float(sdl2.SDL_GameControllerGetButton(gc, b))  # noqa: E731
        out = _blank()
        tl = max(0.0, ax(sdl2.SDL_CONTROLLER_AXIS_TRIGGERLEFT))
        tr = max(0.0, ax(sdl2.SDL_CONTROLLER_AXIS_TRIGGERRIGHT))
        out.update({
            "stick.lx": ax(sdl2.SDL_CONTROLLER_AXIS_LEFTX),
            "stick.ly": -ax(sdl2.SDL_CONTROLLER_AXIS_LEFTY),
            "stick.rx": ax(sdl2.SDL_CONTROLLER_AXIS_RIGHTX),
            "stick.ry": -ax(sdl2.SDL_CONTROLLER_AXIS_RIGHTY),
            "trig.l": tl, "trig.r": tr,
            "btn.lt": float(tl > TRIGGER_PRESSED), "btn.rt": float(tr > TRIGGER_PRESSED),
            "battery": _battery(self.js),
        })
        for name, b in self._buttons.items():
            out[f"btn.{name}"] = bt(b)
        for name, b in self._dpad.items():
            out[f"dpad.{name}"] = bt(b)
        # SDL's controller frame: +x right, +y up, +z toward the player.
        for kind in self.motion:
            if sdl2.SDL_GameControllerGetSensorData(gc, kind, self._buf, 3) != 0:
                continue
            x, y, z = self._buf
            if kind == sdl2.SDL_SENSOR_ACCEL:
                out["accel.x"], out["accel.y"], out["accel.z"] = (
                    x / STANDARD_GRAVITY, y / STANDARD_GRAVITY, z / STANDARD_GRAVITY)
            else:
                out["gyro.x"], out["gyro.y"], out["gyro.z"] = (
                    math.degrees(x), math.degrees(y), math.degrees(z))
        if self.touchpad:
            self._read_touch(out)
        return out

    def _read_touch(self, out):
        import sdl2
        state, x, y, _ = self._f
        down = (sdl2.SDL_GameControllerGetTouchpadFinger(
            self.gc, 0, 0, ctypes.byref(state), ctypes.byref(x), ctypes.byref(y), None) == 0
            and state.value)
        now = time.monotonic()
        if not down:
            self._touch_prev = None
            return
        out["touch.active"] = 1.0
        out["touch.x"], out["touch.y"] = x.value, y.value
        if self._touch_prev is not None:
            px, py, pt = self._touch_prev
            dt = now - pt
            if dt > 0:
                out["touch.dx"], out["touch.dy"] = (x.value - px) / dt, (y.value - py) / dt
        self._touch_prev = (x.value, y.value, now)


class _Raw:
    """A device with no SDL mapping, read in the common DInput order:
    axes lx, ly, rx, ry[, lt, rt]; buttons a, b, x, y, lb, rb, back, start, ls, rs,
    guide; d-pad on hat 0 (or buttons 11-14 when there's no hat)."""

    BUTTONS = ["a", "b", "x", "y", "lb", "rb", "back", "start", "ls", "rs", "guide"]

    def __init__(self, js, name, zero2):
        import sdl2
        self.js = js
        self.name = name
        self.zero2 = zero2
        self.n_axes = sdl2.SDL_JoystickNumAxes(js)
        self.n_buttons = sdl2.SDL_JoystickNumButtons(js)
        self.n_hats = sdl2.SDL_JoystickNumHats(js)

    def attached(self):
        import sdl2
        return bool(sdl2.SDL_JoystickGetAttached(self.js))

    def close(self):
        import sdl2
        sdl2.SDL_JoystickClose(self.js)

    def read(self):
        import sdl2
        js = self.js
        ax = lambda i: _axis(sdl2.SDL_JoystickGetAxis(js, i)) if i < self.n_axes else 0.0  # noqa: E731
        bt = lambda i: float(sdl2.SDL_JoystickGetButton(js, i)) if i < self.n_buttons else 0.0  # noqa: E731
        # Raw triggers rest at -1; with no trigger axes they read 0.
        tl = (ax(4) + 1.0) / 2.0 if self.n_axes > 4 else 0.0
        tr = (ax(5) + 1.0) / 2.0 if self.n_axes > 5 else 0.0
        out = _blank()
        out.update({
            "stick.lx": ax(0), "stick.ly": -ax(1), "stick.rx": ax(2), "stick.ry": -ax(3),
            "trig.l": tl, "trig.r": tr,
            "btn.lt": float(tl > TRIGGER_PRESSED), "btn.rt": float(tr > TRIGGER_PRESSED),
            "battery": _battery(js),
        })
        for i, b in enumerate(self.BUTTONS):
            out[f"btn.{b}"] = bt(i)
        if self.n_hats:
            hat = sdl2.SDL_JoystickGetHat(js, 0)
            out["dpad.up"] = float(bool(hat & sdl2.SDL_HAT_UP))
            out["dpad.down"] = float(bool(hat & sdl2.SDL_HAT_DOWN))
            out["dpad.left"] = float(bool(hat & sdl2.SDL_HAT_LEFT))
            out["dpad.right"] = float(bool(hat & sdl2.SDL_HAT_RIGHT))
        else:
            for i, d in enumerate(("up", "down", "left", "right"), start=len(self.BUTTONS)):
                out[f"dpad.{d}"] = bt(i)
        return out
