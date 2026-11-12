# Source `cv` — camera hand tracking (OpenCV + MediaPipe)

Turns a webcam into a source: the index-finger tip as `hand.x`/`hand.y`, a
thumb–index `hand.pinch`, and all 21 raw MediaPipe hand landmarks grouped by
finger — `wrist.x`, `thumb_tip.y`, `index_mcp.z`, ... (-1..1, x mirrored, y up,
z + toward the camera). Its card shows a small live camera preview with the
landmarks drawn on.

Its [`module.toml`](module.toml) declares `mediapipe` + OpenCV and the MediaPipe
model files, so the sync installs and downloads them. If you don't want them, add
`disable = ["cv"]` to `patchbay.toml`.

```bash
python run.py --source cv                      # or add "Hand tracker (camera)" from the UI
python run.py --source cv:camera=1
```

On macOS, grant your terminal camera access in System Settings › Privacy the
first time.

`hand_tracker.py` and `gesture_tracker.py` are the standalone OpenCV prototype
scripts the module was built from. The module imports its landmarker setup from
`hand_tracker.py`, but the engine never runs the scripts themselves.
