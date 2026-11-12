"""Webcam hand tracking (MediaPipe), as a patchbay source.

The MediaPipe ``HandLandmarker`` from ``hand_tracker.py`` (next to this file; its
options, model path and landmark helpers are reused from there), wrapped in the
``Source`` contract: the camera and the model run on their own thread, and
``read()`` just hands back the latest flat dict of ports — all 21 raw landmarks
(``wrist.x``, ``thumb_tip.y``, ``index_mcp.z``, ...) plus a few derived conveniences (``hand.x``, ``hand.pinch``).

Self-contained: nothing in the engine, server or UI knows about it. It is just
another registered source, added like any other — from the GUI's *add source*
picker, ``--source cv:camera=1``, or the ``--cli`` console's ``add`` — and
removed the same way.

Needs ``mediapipe`` + OpenCV and the model file ``hand_landmarker.task`` next to
this file (override with ``model=``) — all declared in ``module.toml``, so the
start-up sync installs and fetches them. Both libraries are imported lazily, so the plugin registers
without them and :meth:`HandTrackerCV.available` greys it out in the picker with
the reason instead.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from engine.registry import register_source
from engine.signals import EVENT, SCALAR, PortSpec
from sources.base import Source

# Same path as hand_tracker.MODEL_PATH; duplicated so this module
# doesn't import cv2/mediapipe (via hand_tracker) before available() has checked them.
DEFAULT_MODEL = Path(__file__).resolve().parent / "hand_landmarker.task"

# Camera preview shown in the module card: width in pixels, max frame rate, and
# the landmark dots (BGR pink/purple, small enough for the scaled-down frame).
PREVIEW_WIDTH, PREVIEW_FPS = 240, 15
PREVIEW_DOT_COLOR, PREVIEW_DOT_RADIUS = (230, 70, 255), 2

# Landmark indices (see MediaPipe hand landmark docs).
WRIST, THUMB_TIP, INDEX_TIP, MIDDLE_MCP = 0, 4, 8, 9
# The 21 landmarks in MediaPipe index order as (port id prefix, label, UI group).
LANDMARKS = [("wrist", "wrist", "wrist"),
             ("thumb_cmc", "thumb base", "thumb"), ("thumb_mcp", "thumb knuckle", "thumb"),
             ("thumb_ip", "thumb joint", "thumb"), ("thumb_tip", "thumb tip", "thumb")]
for _finger in ("index", "middle", "ring", "pinky"):
    LANDMARKS += [(f"{_finger}_mcp", f"{_finger} knuckle", _finger),
                  (f"{_finger}_pip", f"{_finger} middle joint", _finger),
                  (f"{_finger}_dip", f"{_finger} top joint", _finger),
                  (f"{_finger}_tip", f"{_finger} tip", _finger)]

# Raw landmarks, same space as hand.x / hand.y: -1..1, x mirrored, y up.
# z is depth vs the wrist on the x scale, flipped so + is toward the camera.
AXIS_HINTS = {"x": "-1 left .. 1 right", "y": "-1 down .. 1 up", "z": "depth vs wrist, + toward camera"}

PORTS = [
    PortSpec("hand.present", EVENT, "hand detected", "hand", hint="1 while a hand is tracked"),
    PortSpec("hand.x", SCALAR, "pointer x", "hand", hint="index tip, -1 left .. 1 right (mirrored)"),
    PortSpec("hand.y", SCALAR, "pointer y", "hand", hint="index tip, -1 down .. 1 up"),
    PortSpec("hand.pinch", SCALAR, "pinch", "hand",
             hint="thumb-index gap / palm size; ~0 pinched, ~1+ open"),
    *[PortSpec(f"{pid}.{axis}", SCALAR, f"{label} {axis}", group, hint=AXIS_HINTS[axis])
      for pid, label, group in LANDMARKS for axis in "xyz"],
]


@register_source
class HandTrackerCV(Source):
    type = "cv"
    id = "cv"
    label = "Hand tracker (camera)"
    PORTS = PORTS
    PREVIEW = True

    @classmethod
    def available(cls):
        try:
            import cv2  # noqa: F401
            import mediapipe  # noqa: F401
        except Exception as e:
            return False, f"needs opencv + mediapipe — run `python run.py --sync` ({e})"
        return True
    ARGS = {
        "camera": {"default": 0, "type": int, "help": "OpenCV camera index"},
        "model": {"default": str(DEFAULT_MODEL), "type": str,
                  "help": "path to hand_landmarker.task"},
    }

    def __init__(self, camera=0, model=str(DEFAULT_MODEL)):
        import cv2  # lazy: optional deps (see available())
        import mediapipe as mp
        from . import hand_tracker

        self._cv2, self._mp, self._ht = cv2, mp, hand_tracker
        self._lock = threading.Lock()
        self._latest = self._idle()
        self._jpeg = None
        self._stop = threading.Event()
        self._thread = None
        self._status = "starting"

        if not Path(model).is_file():
            raise FileNotFoundError(f"MediaPipe model not found at {model}")
        opts = hand_tracker.make_options(
            running_mode=hand_tracker.VisionRunningMode.VIDEO, num_hands=1, model_path=model)
        self._landmarker = hand_tracker.HandLandmarker.create_from_options(opts)

        # Open eagerly so a missing camera / denied permission fails the
        # request that asked for it, instead of a silent "ok" module.
        self._cap = cv2.VideoCapture(int(camera))
        if not self._cap.isOpened():
            self._landmarker.close()
            raise RuntimeError(f"could not open camera {camera} (on macOS, grant the "
                               "terminal camera access in System Settings > Privacy)")

    def start(self):
        self._thread = threading.Thread(target=self._poll, name="cv", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._cap.release()
        self._landmarker.close()

    def status(self):
        return self._status

    def read(self):
        with self._lock:
            return dict(self._latest)

    def preview_jpeg(self):
        return self._jpeg

    # ------------------------------------------------------------------
    @staticmethod
    def _idle():
        return {p.id: 0.0 for p in PORTS}

    def _poll(self):
        cv2, mp = self._cv2, self._mp
        t0 = time.monotonic()
        last_preview = 0.0
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                self._status = "error: camera read failed"
                time.sleep(0.2)
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            ts_ms = int((time.monotonic() - t0) * 1000)  # VIDEO mode needs it monotonic
            try:
                result = self._landmarker.detect_for_video(image, ts_ms)
            except Exception as e:
                self._status = f"error: {e}"
                continue
            out = self._ingest(result)
            self._status = "ok" if out["hand.present"] else "ok · no hand"
            with self._lock:
                self._latest = out
            if time.monotonic() - last_preview >= 1 / PREVIEW_FPS:
                last_preview = time.monotonic()
                self._jpeg = self._preview(frame, result)

    def _preview(self, frame, result):
        """Shrink the frame, draw the landmark overlay, mirror it like a selfie view."""
        cv2 = self._cv2
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (PREVIEW_WIDTH, round(h * PREVIEW_WIDTH / w)),
                           interpolation=cv2.INTER_AREA)
        for hand_landmarks in result.hand_landmarks:
            self._ht.draw_hand_landmarks(small, hand_landmarks,  # normalized coords, any size
                                         PREVIEW_DOT_COLOR, PREVIEW_DOT_RADIUS)
        ok, buf = cv2.imencode(".jpg", cv2.flip(small, 1), [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None

    def _ingest(self, result) -> dict:
        out = self._idle()
        if not result.hand_landmarks:
            return out
        pts = self._ht.landmark_points(result.hand_landmarks[0])  # normalized 0..1, see hand_tracker
        out["hand.present"] = 1.0
        # Camera frames aren't mirrored: moving your hand right moves it left in
        # the image, so flip x to match what you see in a selfie view.
        for (pid, _, _), (x, y, z) in zip(LANDMARKS, pts):
            out[f"{pid}.x"] = (0.5 - x) * 2.0
            out[f"{pid}.y"] = (0.5 - y) * 2.0
            out[f"{pid}.z"] = -z * 2.0
        out["hand.x"], out["hand.y"] = out["index_tip.x"], out["index_tip.y"]
        palm = math.dist(pts[WRIST][:2], pts[MIDDLE_MCP][:2])
        gap = math.dist(pts[THUMB_TIP][:2], pts[INDEX_TIP][:2])
        out["hand.pinch"] = gap / palm if palm > 1e-6 else 0.0
        return out
