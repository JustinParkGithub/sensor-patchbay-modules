# Source `controller`: any game controller

Reads any controller through SDL2's game controller API (`pysdl2`, with the
SDL2 library from `pysdl2-dll`; both come from [`module.toml`](module.toml)).
You only need to connect the controller.

| scheme | sticks, triggers, buttons | motion (`accel.*`, `gyro.*`, `tilt.*`) | touchpad |
|---|---|---|---|
| Xbox (XInput), Xbox Series | ✓ (Elite paddles as `btn.paddle1-4`) | – | – |
| PlayStation: DualShock 4, DualSense | ✓ | ✓ | ✓ |
| Nintendo: Switch Pro, Joy-Cons (single or pair) | ✓ | ✓ | – |
| 8BitDo (any mode), Steam, Stadia, Luna, Shield | ✓ | when the pad has sensors and SDL supports them | – |
| generic DInput pads | ✓ (raw order if SDL has no mapping; see below) | – | – |

Anything a controller doesn't have reads 0.

The source can be added before the controller is connected. It shows
`searching` until one appears, then `ok: <controller name>`, and goes back to
searching if the controller is unplugged.

## Args

| arg | default | |
|---|---|---|
| `name` | any | use the first controller whose name contains this, case-insensitive (`dualsense`, `joy-con`, `8bitdo`) |
| `index` | `0` | which matching controller: `0` the first, `1` the second, … |
| `dpad_from_stick` | `auto` | read the left stick as the d-pad. `auto` turns it on for an 8BitDo Zero 2 that's recognisable (DInput / macOS modes); use `on` for one in XInput mode, where it looks like a generic Xbox pad |
| `vertical_joycon` | off | treat a single Joy-Con as held upright (stick on top) instead of sideways |

Two controllers at once: add the source twice with `index=0` and `index=1` (or
two `name=` filters). The second instance's ports go under `controller-2/`.

## Ports

Ports are **positional, Xbox-shaped**. `btn.a` is the *bottom* face button
whatever is printed on it.

| port | Xbox | PlayStation | Nintendo |
|---|---|---|---|
| `btn.a` | A | Cross | B |
| `btn.b` | B | Circle | A |
| `btn.x` | X | Square | Y |
| `btn.y` | Y | Triangle | X |
| `btn.lb` / `btn.rb` | LB / RB | L1 / R1 | L / R (SL / SR on a sideways Joy-Con) |
| `trig.l` / `trig.r` (0..1), `btn.lt` / `btn.rt` (past halfway) | LT / RT | L2 / R2 | ZL / ZR (digital: 0 or 1) |
| `btn.back` / `btn.start` | View / Menu | Share / Options (Create / Options) | − / + |
| `btn.guide` | Xbox | PS | Home |
| `btn.misc` | Share (Series) | Mic (DualSense) | Capture |
| `btn.ls` / `btn.rs` | stick clicks | L3 / R3 | stick clicks |
| `btn.touchpad` | – | touchpad click | – |
| `btn.paddle1-4` | Elite paddles | Edge back buttons | – |
| `dpad.up/down/left/right` | d-pad | d-pad | d-pad |
| `stick.lx`, `stick.ly`, `stick.rx`, `stick.ry` | −1..1, **up is +** | | |

Motion, touchpad and status:

| port | | |
|---|---|---|
| `accel.x/y/z` | g | SDL's frame: +x right, +y up, +z toward you. Flat at rest reads `accel.y ≈ 1` |
| `accel.mag` | g | ≈ 1 at rest; spikes on a shake (threshold it, about 2–2.5) |
| `gyro.x` | deg/s | pitch rate, + = front edge rising |
| `gyro.y` | deg/s | yaw rate, + = turning left |
| `gyro.z` | deg/s | roll rate, + = right side rising |
| `tilt.pitch` | deg | from gravity, drift-free; + = front edge up |
| `tilt.roll` | deg | from gravity, drift-free; + = right side down |
| `touch.active` | 0/1 | a finger is on the touchpad |
| `touch.x`, `touch.y` | 0..1 | first finger's position, 0,0 = top left |
| `touch.dx`, `touch.dy` | pads/s | drag speed in touchpad widths/heights per second (for a trackpad mouse) |
| `battery` | 0..1 | coarse (SDL reports empty / low / medium / full); 1 when wired, −1 unknown |

## Connecting

USB or Bluetooth, in any of the controller's gamepad modes.

- **Linux:** your user needs read access to `/dev/input/event*`, and to
  `/dev/hidraw*` for motion and touchpad on PlayStation and Switch pads.
  Join the `input` group, or add a uaccess udev rule for the controller.
- **macOS / Windows:** nothing extra.
- **Joy-Cons:** SDL joins a left + right pair into one controller. A single
  Joy-Con is read sideways unless `vertical_joycon` is on.
- **8BitDo Zero 2:** pair by holding a mode button while pressing Start, then
  Select for 3 s:

  | hold | mode |
  |---|---|
  | Start + X | XInput: looks like a generic Xbox pad; set `dpad_from_stick=on` |
  | Start + B | DInput |
  | Start + A | macOS |
  | Start + Y | Switch |

  Its keyboard mode makes it a keyboard, not a controller.

Input is read in the background, so it reaches the patchbay while another app
has focus.

## A controller with no SDL mapping

Some DInput pads have no entry in SDL's controller database. Those are read as
a raw joystick in the common order:
- axes: lx, ly, rx, ry, then lt, rt;
- buttons: a, b, x, y, lb, rb, back, start, ls, rs, guide;
- d-pad: on hat 0, or on buttons 11–14 when there's no hat.

If yours is wired differently, give SDL a mapping. Make one with a tool such as
[SDL2 Gamepad Tool](https://generalarcade.com/gamepadtool/), then put the line
in the `SDL_GAMECONTROLLERCONFIG` environment variable before starting the
patchbay.
