"""Virtual mouse + keyboard via `pynput` (Linux/uinput or X11, macOS Quartz,
Windows SendInput). Cross-platform, no per-OS code here.

Port semantics
--------------
  mouse.dx / mouse.dy   scalar, **pixels per second** (fractional motion is
                        accumulated so low speeds still move)
  mouse.wheel           scalar, scroll clicks per second
  mouse.left/right/middle,
  mouse.btn_back/btn_forward   event gate: >=0.5 held = button down
  key.<name>            event gate: >=0.5 held = key down. <name> is a single
                        character ("a") or a named key ("space", "esc", "enter",
                        "tab", "shift", "ctrl", "alt", "up", "left", ...). Any
                        key.<name> works, not just the ones listed as ports.

A freshly pressed key/button is held for at least ``MIN_HOLD_S`` so a one-tick
debounce pulse still registers in the target application.
"""

from __future__ import annotations

import sys
import threading
import time

from engine.registry import register_sink
from engine.signals import PortSpec, SCALAR, EVENT
from sinks.base import Sink

MIN_HOLD_S = 0.045

_KEYNAMES = [
    "space", "enter", "esc", "tab", "backspace", "delete", "shift", "ctrl", "alt",
    "cmd", "up", "down", "left", "right", "home", "end", "page_up", "page_down",
    "f1", "f2", "f3", "f4", "f5",
]


def _on_main_thread(fn, timeout=5.0):
    """Call *fn* on the main thread when a Cocoa app owns it, else right here.

    On macOS, pynput's keyboard Controller reads the keyboard layout through
    HIToolbox, which aborts the whole process (SIGTRAP) when called off the
    main thread while an NSApplication exists, i.e. under app.py's window or
    tray. Headless (run.py) there is no app and any thread is fine.
    """
    if sys.platform != "darwin" or threading.current_thread() is threading.main_thread():
        return fn()
    from AppKit import NSApp
    if NSApp() is None:
        return fn()
    from PyObjCTools import AppHelper

    done = threading.Event()
    result = {}

    def call():
        try:
            result["value"] = fn()
        except BaseException as e:  # re-raised on the calling thread
            result["error"] = e
        finally:
            done.set()

    AppHelper.callAfter(call)
    if not done.wait(timeout):
        raise TimeoutError("main thread busy; couldn't create the keyboard controller")
    if "error" in result:
        raise result["error"]
    return result["value"]


@register_sink
class MouseKeyboardSink(Sink):
    type = "hid"
    id = "hid"
    label = "mouse + keyboard"
    accepts_any = True

    @classmethod
    def available(cls):
        try:
            import pynput  # noqa: F401
        except Exception as e:
            return False, f"needs pynput + a display ({e})"
        return True

    PORTS = [
        PortSpec("mouse.dx", SCALAR, "mouse.dx", "mouse", "px/s"),
        PortSpec("mouse.dy", SCALAR, "mouse.dy", "mouse", "px/s"),
        PortSpec("mouse.wheel", SCALAR, "mouse.wheel", "mouse", "clicks/s"),
        PortSpec("mouse.left", EVENT, "mouse.left", "mouse"),
        PortSpec("mouse.right", EVENT, "mouse.right", "mouse"),
        PortSpec("mouse.middle", EVENT, "mouse.middle", "mouse"),
        PortSpec("mouse.btn_back", EVENT, "mouse.btn_back", "mouse"),
        PortSpec("mouse.btn_forward", EVENT, "mouse.btn_forward", "mouse"),
        PortSpec("key.space", EVENT, "key.space", "keyboard"),
        PortSpec("key.esc", EVENT, "key.esc", "keyboard"),
        PortSpec("key.enter", EVENT, "key.enter", "keyboard"),
        PortSpec("key.a", EVENT, "key.a", "keyboard"),
        PortSpec("key.d", EVENT, "key.d", "keyboard"),
        PortSpec("key.w", EVENT, "key.w", "keyboard"),
        PortSpec("key.s", EVENT, "key.s", "keyboard"),
        PortSpec("key.<name>", EVENT, "key.<any>", "keyboard", hint="any key works"),
    ]

    def __init__(self):
        self._mouse = None
        self._kbd = None
        self._Button = None
        self._Key = None
        self._fx = self._fy = self._fw = 0.0
        self._btn_down: dict[str, float] = {}     # name -> press time
        self._key_down: dict[str, float] = {}
        self._err = None

    def start(self):
        try:
            from pynput.mouse import Controller as MouseC, Button
            from pynput.keyboard import Controller as KbdC, Key
        except Exception as e:  # pragma: no cover - env dependent
            self._err = f"pynput unavailable: {e}"
            raise
        self._mouse = MouseC()
        self._kbd = _on_main_thread(KbdC)
        self._Button = Button
        self._Key = Key

    def stop(self):
        self.release_all()

    def status(self):
        return self._err or "ok"

    # ------------------------------------------------------------------
    def apply(self, values, dt):
        if self._mouse is None:
            return
        dx = values.get("mouse.dx", 0.0) * dt + self._fx
        dy = values.get("mouse.dy", 0.0) * dt + self._fy
        mx, my = int(dx), int(dy)
        self._fx, self._fy = dx - mx, dy - my
        if mx or my:
            self._mouse.move(mx, my)

        dw = values.get("mouse.wheel", 0.0) * dt + self._fw
        steps = int(dw)
        self._fw = dw - steps
        if steps:
            self._mouse.scroll(0, steps)

        now = time.perf_counter()
        self._hold(values, now, "mouse.left", self._btn_map("left"), self._btn_down, self._press_btn)
        self._hold(values, now, "mouse.right", self._btn_map("right"), self._btn_down, self._press_btn)
        self._hold(values, now, "mouse.middle", self._btn_map("middle"), self._btn_down, self._press_btn)
        self._hold(values, now, "mouse.btn_back", self._btn_map("back"), self._btn_down, self._press_btn)
        self._hold(values, now, "mouse.btn_forward", self._btn_map("forward"), self._btn_down, self._press_btn)

        wanted = {p.split("key.", 1)[1] for p, v in values.items()
                  if p.startswith("key.") and v >= 0.5 and not p.endswith("<name>")}
        for name in wanted:
            if name not in self._key_down:
                self._key_press(name, True)
                self._key_down[name] = now
        for name in list(self._key_down):
            still = name in wanted
            if not still and (now - self._key_down[name]) >= MIN_HOLD_S:
                self._key_press(name, False)
                self._key_down.pop(name, None)

    # ------------------------------------------------------------------
    def _hold(self, values, now, port, target, book, press):
        want = values.get(port, 0.0) >= 0.5
        if want and target not in book:
            press(target, True)
            book[target] = now
        elif not want and target in book and (now - book[target]) >= MIN_HOLD_S:
            press(target, False)
            book.pop(target, None)

    def _btn_map(self, name):
        b = self._Button
        return {"left": b.left, "right": b.right, "middle": b.middle,
                "back": getattr(b, "button8", getattr(b, "back", b.left)),
                "forward": getattr(b, "button9", getattr(b, "forward", b.right))}.get(name, b.left)

    def _press_btn(self, target, down):
        try:
            (self._mouse.press if down else self._mouse.release)(target)
        except Exception:
            pass

    def _key_press(self, name, down):
        key = self._resolve_key(name)
        if key is None:
            return
        try:
            (self._kbd.press if down else self._kbd.release)(key)
        except Exception:
            pass

    def _resolve_key(self, name):
        if len(name) == 1:
            return name
        alias = {"ctrl": "ctrl", "control": "ctrl", "cmd": "cmd", "win": "cmd",
                 "return": "enter", "escape": "esc", "pgup": "page_up", "pgdn": "page_down"}
        name = alias.get(name, name)
        return getattr(self._Key, name, None)

    def release_all(self):
        for target in list(self._btn_down):
            self._press_btn(target, False)
        self._btn_down.clear()
        for name in list(self._key_down):
            self._key_press(name, False)
        self._key_down.clear()
        self._fx = self._fy = self._fw = 0.0
