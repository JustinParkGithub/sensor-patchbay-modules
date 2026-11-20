"""macOS system shortcuts: media and volume, brightness, Mission Control and
Spaces, windows and apps, screenshots, Focus modes, and any Shortcuts-app
shortcut.

Everything is sent the way the hardware or the OS would send it: media,
volume and brightness keys as the system-defined (NX) key events the Apple
keyboard's top row produces, key combos as Quartz keyboard events, and the
rest through ``open``, ``pmset`` and ``shortcuts``. No pynput, so the
keyboard-layout lookup that must run on the main thread is never touched.

Port semantics
--------------
Every port except the ``*.rate`` ones is an **event**: it fires once when the
gate rises past 0.5 and re-arms when it falls below. Holding it doesn't repeat.

  play_pause, next, previous          media transport keys
  mute, volume.up, volume.down        one volume step
  volume.rate                         scalar, volume steps per second (+ up, - down);
                                      a stick or a tilt works as a volume knob
  brightness.up/down, brightness.rate display brightness, same as volume
  kbd_light.up/down                   keyboard backlight
  display.sleep                       turn the displays off now

  mission_control, app_windows (App Exposé), show_desktop
  desktop.left, desktop.right         previous / next Space (ctrl + arrow)
  desktop.<n>                         Space n, 1-9 (ctrl + n; switch these on in
                                      System Settings > Keyboard > Keyboard
                                      Shortcuts > Mission Control)
  spotlight, apps (Apps / Launchpad)
  app.switch (cmd-tab), app.hide, app.quit
  window.close, window.minimize, window.fullscreen, window.cycle (cmd-`)
  screenshot.screen, screenshot.area, screenshot.tools, lock

  focus.<mode>                        runs the Shortcuts-app shortcut named
                                      "<focus_prefix><Mode>": focus.dnd runs
                                      "Focus: Do Not Disturb", focus.off
                                      "Focus: Off", focus.work "Focus: Work".
                                      macOS has no public Focus API; Shortcuts'
                                      "Set Focus" action is the supported route
  shortcut.<name>                     runs the Shortcuts-app shortcut <name>
  open.<App Name>                     launches or brings forward that app

Any ``desktop.<n>``, ``focus.<mode>``, ``shortcut.<name>`` or ``open.<app>``
works from a preset, listed as a jack or not.

All the work happens on one worker thread, so ``apply()`` never blocks on a
slow ``shortcuts run``. Triggers that pile up behind a slow one past
``_QUEUE_MAX`` are dropped rather than replayed late.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time

from engine.registry import register_sink
from engine.signals import PortSpec, SCALAR, EVENT
from sinks.base import Sink

# ------------------------------------------------------------------ key tables
# NX_KEYTYPE_* (IOKit/hidsystem/ev_keymap.h): the top-row special keys.
_NX = {
    "play_pause": 16, "next": 17, "previous": 18,
    "volume.up": 0, "volume.down": 1, "mute": 7,
    "brightness.up": 2, "brightness.down": 3,
    "kbd_light.up": 21, "kbd_light.down": 22,
}

# Carbon kVK_* virtual key codes (HIToolbox/Events.h), ANSI positions.
_VK = {
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "space": 0x31, "tab": 0x30, "grave": 0x32,
    "q": 0x0C, "w": 0x0D, "m": 0x2E, "h": 0x04, "f": 0x03,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
    "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
}
_MODS = {  # name -> (key code, CGEventFlags bit)
    "cmd": (0x37, 0x100000), "shift": (0x38, 0x20000),
    "alt": (0x3A, 0x80000), "ctrl": (0x3B, 0x40000),
}
_ARROW_FLAGS = 0x800000 | 0x200000   # fn + numeric pad: what a real arrow key carries

_COMBOS = {  # port -> (modifiers, key)
    "desktop.left": (("ctrl",), "left"),
    "desktop.right": (("ctrl",), "right"),
    "spotlight": (("cmd",), "space"),
    "app.switch": (("cmd",), "tab"),
    "app.hide": (("cmd",), "h"),
    "app.quit": (("cmd",), "q"),
    "window.close": (("cmd",), "w"),
    "window.minimize": (("cmd",), "m"),
    "window.fullscreen": (("ctrl", "cmd"), "f"),
    "window.cycle": (("cmd",), "grave"),
    "screenshot.screen": (("cmd", "shift"), "3"),
    "screenshot.area": (("cmd", "shift"), "4"),
    "screenshot.tools": (("cmd", "shift"), "5"),
    "lock": (("ctrl", "cmd"), "q"),
}

_MISSION_CONTROL = "/System/Applications/Mission Control.app"
_COMMANDS = {  # port -> argv
    "mission_control": ["open", "-a", _MISSION_CONTROL],
    "show_desktop": ["open", "-a", _MISSION_CONTROL, "--args", "1"],
    "app_windows": ["open", "-a", _MISSION_CONTROL, "--args", "2"],
    "display.sleep": ["pmset", "displaysleepnow"],
}

_RATES = {"volume.rate": ("volume.up", "volume.down"),
          "brightness.rate": ("brightness.up", "brightness.down")}
_MAX_STEPS_PER_TICK = 4   # a huge scale factor shouldn't flood the OS with key events
_QUEUE_MAX = 32
_FOCUS_NAMES = {"dnd": "Do Not Disturb"}
_TAP_GAP_S = 0.004        # between the events of one combo
_ERROR_SHOWN_S = 15.0     # how long a failed action stays in status()


def _focus_title(mode: str) -> str:
    return _FOCUS_NAMES.get(mode, mode.replace("_", " ").title())


def _apps_app() -> str:
    """Apps (macOS 26+) replaced Launchpad."""
    for p in ("/System/Applications/Apps.app", "/System/Applications/Launchpad.app"):
        if os.path.isdir(p):
            return p
    return "Launchpad"


def _split(text) -> list[str]:
    return [s.strip() for s in str(text or "").split(",") if s.strip()]


_STATIC_PORTS = [
    PortSpec("play_pause", EVENT, "play_pause", "media"),
    PortSpec("next", EVENT, "next", "media"),
    PortSpec("previous", EVENT, "previous", "media"),
    PortSpec("mute", EVENT, "mute", "volume"),
    PortSpec("volume.up", EVENT, "volume.up", "volume"),
    PortSpec("volume.down", EVENT, "volume.down", "volume"),
    PortSpec("volume.rate", SCALAR, "volume.rate", "volume", "steps/s",
             "+ up, - down: patch a stick or tilt for a volume knob"),
    PortSpec("brightness.up", EVENT, "brightness.up", "display"),
    PortSpec("brightness.down", EVENT, "brightness.down", "display"),
    PortSpec("brightness.rate", SCALAR, "brightness.rate", "display", "steps/s",
             "+ brighter, - dimmer"),
    PortSpec("kbd_light.up", EVENT, "kbd_light.up", "display", hint="keyboard backlight"),
    PortSpec("kbd_light.down", EVENT, "kbd_light.down", "display", hint="keyboard backlight"),
    PortSpec("display.sleep", EVENT, "display.sleep", "display"),
    PortSpec("mission_control", EVENT, "mission_control", "spaces"),
    PortSpec("app_windows", EVENT, "app_windows", "spaces", hint="App Exposé"),
    PortSpec("show_desktop", EVENT, "show_desktop", "spaces"),
    PortSpec("desktop.left", EVENT, "desktop.left", "spaces", hint="previous Space (ctrl ←)"),
    PortSpec("desktop.right", EVENT, "desktop.right", "spaces", hint="next Space (ctrl →)"),
    PortSpec("spotlight", EVENT, "spotlight", "apps"),
    PortSpec("apps", EVENT, "apps", "apps", hint="Apps / Launchpad"),
    PortSpec("app.switch", EVENT, "app.switch", "apps", hint="cmd-tab: back to the last app"),
    PortSpec("app.hide", EVENT, "app.hide", "apps"),
    PortSpec("app.quit", EVENT, "app.quit", "apps"),
    PortSpec("window.close", EVENT, "window.close", "windows"),
    PortSpec("window.minimize", EVENT, "window.minimize", "windows"),
    PortSpec("window.fullscreen", EVENT, "window.fullscreen", "windows"),
    PortSpec("window.cycle", EVENT, "window.cycle", "windows", hint="cmd-`: next window of this app"),
    PortSpec("screenshot.screen", EVENT, "screenshot.screen", "screen"),
    PortSpec("screenshot.area", EVENT, "screenshot.area", "screen"),
    PortSpec("screenshot.tools", EVENT, "screenshot.tools", "screen"),
    PortSpec("lock", EVENT, "lock", "screen"),
]


@register_sink
class MacOSSink(Sink):
    type = "macos"
    id = "macos"
    label = "macOS shortcuts"
    accepts_any = True
    PORTS = _STATIC_PORTS
    ARGS = {
        "desktops": {"default": 4, "type": int,
                     "help": "how many desktop.<n> jacks to show (1-9); any n works from a preset"},
        "focus_modes": {"default": "dnd, work, personal, sleep, off", "type": str,
                        "help": "comma-separated focus.<mode> jacks to show"},
        "focus_prefix": {"default": "Focus: ", "type": str,
                         "help": "focus.<mode> runs the Shortcuts shortcut named prefix + Mode"},
        "shortcuts": {"default": "", "type": str,
                      "help": "comma-separated Shortcuts-app shortcut names to show as jacks"},
        "apps": {"default": "", "type": str,
                 "help": "comma-separated app names to show as open.<app> jacks"},
    }

    @classmethod
    def available(cls):
        if sys.platform != "darwin":
            return False, "macOS only"
        try:
            import Quartz  # noqa: F401
            import AppKit  # noqa: F401
        except Exception as e:
            return False, f"needs pyobjc Quartz + Cocoa ({e})"
        return True

    def __init__(self, desktops=4, focus_modes="dnd, work, personal, sleep, off",
                 focus_prefix="Focus: ", shortcuts="", apps=""):
        n = max(0, min(9, int(desktops or 0)))
        self._focus_prefix = str(focus_prefix if focus_prefix is not None else "Focus: ")
        self.PORTS = [
            *_STATIC_PORTS,
            *(PortSpec(f"desktop.{i}", EVENT, f"desktop.{i}", "spaces", hint=f"ctrl {i}")
              for i in range(1, n + 1)),
            PortSpec("desktop.<n>", EVENT, "desktop.<1-9>", "spaces",
                     hint="ctrl+n: turn on in Keyboard Shortcuts > Mission Control"),
            *(PortSpec(f"focus.{m}", EVENT, f"focus.{m}", "focus",
                       hint=f'runs shortcut "{self._focus_prefix}{_focus_title(m)}"')
              for m in _split(focus_modes)),
            PortSpec("focus.<mode>", EVENT, "focus.<any>", "focus",
                     hint=f'runs shortcut "{self._focus_prefix}<Mode>"'),
            *(PortSpec(f"shortcut.{s}", EVENT, s, "shortcuts") for s in _split(shortcuts)),
            PortSpec("shortcut.<name>", EVENT, "shortcut.<any>", "shortcuts",
                     hint="runs any Shortcuts-app shortcut"),
            *(PortSpec(f"open.{a}", EVENT, f"open {a}", "apps") for a in _split(apps)),
            PortSpec("open.<app>", EVENT, "open.<any app>", "apps"),
        ]
        self._high: set[str] = set()
        self._frac = {p: 0.0 for p in _RATES}
        self._q: queue.Queue = queue.Queue(_QUEUE_MAX)
        self._thread = None
        self._err = None
        self._dropped = 0

    # ------------------------------------------------------------- lifecycle
    def start(self):
        import Quartz  # noqa: F401  (fail at start, not on the first trigger)
        import AppKit  # noqa: F401
        self._q = queue.Queue(_QUEUE_MAX)
        self._thread = threading.Thread(target=self._work, name=self.type, daemon=True)
        self._thread.start()

    def stop(self):
        self.release_all()
        if self._thread:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass
            self._thread.join(timeout=1.0)
            self._thread = None

    def status(self):
        if self._err and time.monotonic() - self._err[1] < _ERROR_SHOWN_S:
            return f"error: {self._err[0]}"
        if self._dropped:
            return f"ok ({self._dropped} triggers dropped while busy)"
        return "ok"

    def release_all(self):
        # Every action is a tap, so nothing is held; drop what's still queued so
        # PANIC really stops the shortcuts that haven't run yet.
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._frac = {p: 0.0 for p in _RATES}
        # Leave _high as is: a gate still held after PANIC shouldn't re-fire.

    # ------------------------------------------------------------------ tick
    def apply(self, values, dt):
        if self._thread is None:
            return
        for port, v in values.items():
            if port in _RATES or "<" in port:
                continue
            if v >= 0.5:
                if port not in self._high:
                    self._high.add(port)
                    self._trigger(port)
        self._high.intersection_update(p for p, v in values.items() if v >= 0.5)

        for port, (up, down) in _RATES.items():
            f = self._frac[port] + values.get(port, 0.0) * dt
            steps = int(f)
            self._frac[port] = f - steps
            for _ in range(min(abs(steps), _MAX_STEPS_PER_TICK)):
                self._enqueue(("nx", _NX[up if steps > 0 else down]))

    def _trigger(self, port):
        if port in _NX:
            self._enqueue(("nx", _NX[port]))
        elif port in _COMBOS:
            self._enqueue(("combo", *_COMBOS[port]))
        elif port in _COMMANDS:
            self._enqueue(("run", _COMMANDS[port]))
        elif port == "apps":
            self._enqueue(("run", ["open", "-a", _apps_app()]))
        else:
            kind, _, arg = port.partition(".")
            if kind == "desktop" and len(arg) == 1 and arg in "123456789":
                self._enqueue(("combo", ("ctrl",), arg))
            elif kind == "focus" and arg:
                self._enqueue(("shortcut", self._focus_prefix + _focus_title(arg)))
            elif kind == "shortcut" and arg:
                self._enqueue(("shortcut", arg))
            elif kind == "open" and arg:
                self._enqueue(("run", ["open", "-a", arg]))

    def _enqueue(self, job):
        try:
            self._q.put_nowait(job)
        except queue.Full:
            self._dropped += 1

    # ---------------------------------------------------------------- worker
    def _work(self):
        import Quartz
        from AppKit import NSEvent

        def post(ev):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

        def nx(key):
            for down in (True, False):
                state = 0xA if down else 0xB
                ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    14, (0, 0), state << 8, 0, 0, None, 8, (key << 16) | (state << 8), -1)
                post(ev.CGEvent())

        def key(code, down, flags):
            ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
            Quartz.CGEventSetFlags(ev, flags)
            post(ev)
            time.sleep(_TAP_GAP_S)

        def combo(mods, name):
            code = _VK[name]
            extra = _ARROW_FLAGS if name in ("left", "right", "up", "down") else 0
            flags = 0
            for m in mods:
                flags |= _MODS[m][1]
                key(_MODS[m][0], True, flags)
            key(code, True, flags | extra)
            key(code, False, flags | extra)
            for m in reversed(mods):
                flags &= ~_MODS[m][1]
                key(_MODS[m][0], False, flags)

        def run(argv, timeout=30):
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                msg = (r.stderr or r.stdout).strip().splitlines()
                raise RuntimeError(f"{' '.join(argv[:3])}: {msg[-1] if msg else r.returncode}")

        while True:
            job = self._q.get()
            if job is None:
                return
            try:
                kind = job[0]
                if kind == "nx":
                    nx(job[1])
                elif kind == "combo":
                    combo(job[1], job[2])
                elif kind == "run":
                    run(job[1])
                elif kind == "shortcut":
                    run(["shortcuts", "run", job[1]])
            except Exception as e:  # never kill the worker over one action
                self._err = (str(e) or type(e).__name__, time.monotonic())
