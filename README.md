# sensor-patchbay-modules

The sources (inputs), sinks (outputs) and presets for **sensor-patchbay**, a
sensor → routing → command pipeline. The engine ships with none of them. It
installs them from this repo's `index.json`:

- **Patch panel:** *Browse…* in the top bar. Search, filter sources / sinks / presets, then **Install**, **Update** or **Remove**.
- **Console** (`run.py --cli`): `catalog [query]`, `install <name> [--force]`, `uninstall <name>`.
- **Command line:** `python run.py --catalog`, `python run.py --install controller-mouse-keyboard`.

A module's `depends` are installed with it, and installing a preset also
installs the modules it uses.

## Modules

| type | kind | what |
|---|---|---|
| `controller` | source | any game controller via SDL2: Xbox, PlayStation (DualShock 4, DualSense), Switch Pro and Joy-Cons, 8BitDo, Steam, generic DInput. Motion (gyro, accelerometer, tilt) and touchpad where the pad has them. [README](sources/controller/README.md) |
| `bend4` | source | Arduino Nano + four flex sensors over USB serial (sketch in `firmware/`) |
| `cv` | source | webcam hand tracking (OpenCV + MediaPipe). [README](sources/cv/README.md) |
| `hid` | sink | virtual mouse + keyboard (pynput) |

Each module's own README (where it has one) covers its wiring, pairing and ports.

## Presets

| preset | what | uses |
|---|---|---|
| `controller-mouse-keyboard` | any controller as keyboard + mouse. D-pad and left stick are the arrow keys; A/B/X/Y/Start are Space/Esc/E/Tab/Enter; right stick moves the mouse; LB/RB and RT/LT click; Back cycles presets | `controller`, `hid` |
| `controller-mouse` | right stick moves the mouse, and a PlayStation touchpad works as a trackpad. RT/LT (and a touchpad click) left/right-click, R3 middle-clicks, LB/RB are back/forward, the left stick scrolls | `controller`, `hid` |
| `controller-air-mouse` | point a motion controller (PlayStation, Switch) to move the cursor. RT clicks, RB right-clicks, the right stick scrolls, a wrist flick is *forward* | `controller`, `hid` |
| `controller-tilt-keys` | tilt a motion controller for W/A/S/D (20° on, 12° off), A for Space, shake for Esc | `controller`, `hid` |
| `bend4-mouse-keyboard` | bend fingers to look left/right (mouse), walk forward (W) and jump (Space) | `bend4`, `hid` |

The controller presets cycle to the next preset on the Guide (Xbox / PS / Home)
button, except `controller-mouse-keyboard`, which uses Back.

The motion presets' gyro axes and signs follow SDL's conventions (see the
`controller` README). Speeds are a starting point: tune the `scale` nodes in the
patch panel and **Save as** your own preset.

---

# Writing a source or sink

Everything a module must do to be accepted into this repo and to run in the
engine. Words in **bold** are requirements; the rest is advice.

## 1. Layout

A module is **one folder** under `sources/` or `sinks/`. The **folder name is
the module's type** (its name in the menus and on the command line), and
**must be a Python identifier**: letters, digits, `_`, not starting with a
digit. Names are **unique across `sources/` and `sinks/`**.

```
sources/bend4/
  module.toml        required: the manifest (section 2)
  __init__.py        required: the entry (section 3), unless `entry` names another file
  README.md          recommended: wiring, pairing, ports, permissions
  firmware/ ...      anything else the module needs
```

Don't commit generated files (`__pycache__`, `*.pyc`) or large downloads.
Declare those as `[[assets]]` instead, and the engine fetches them.

## 2. The manifest: `module.toml`

```toml
kind        = "source"                                    # "source" | "sink"
description = "Arduino Nano + four flex sensors"         # one line, shown in the catalog
version     = "1.0.0"                                     # dotted numbers
entry       = "__init__.py"                               # optional; this is the default
python      = ["pyserial>=3.5"]                           # pip requirements; markers allowed
depends     = []                                          # modules whose code this one imports
system      = ["Linux: your user in the `dialout` group"] # setup pip can't do; shown, not run

[[assets]]                                # optional, repeatable: fetched into the folder
file   = "model.task"
url    = "https://example.com/model.task"
sha256 = "…"
```

| key | required here | rules |
|---|---|---|
| `kind` | **yes** | `"source"` or `"sink"`, matching the parent folder |
| `description` | **yes** | one line, no trailing period needed |
| `version` | **yes** | dotted numbers (`1.0.0`). **Bump it with every change.** The engine compares versions to tell upgrades from downgrades |
| `entry` | no | the file that runs, relative to the folder. Default `__init__.py`. Any other `.py` file is imported the same way. A non-`.py` entry is a native program: it's listed but not run by the Python engine |
| `python` | no | [PEP 508](https://peps.python.org/pep-0508/) requirement strings. Use environment markers for platform-only packages (`"RPi.GPIO; platform_machine == 'aarch64'"`). Every enabled module shares one environment, so **don't pin exact versions** without a reason. Two modules that pin incompatible versions can't both be enabled |
| `depends` | no | other module types (folder names) whose code this one imports. **Each must exist in this repo**. They're installed alongside |
| `system` | no | one line per manual step (pairing, udev rules, groups, OS permissions). Prefix platform-specific ones: `"Linux: …"`, `"macOS: …"` |
| `[[assets]]` | no | `file`, `url`, `sha256`. Downloaded on sync and hash-checked. Leave them out of git (`*.task` is ignored) |

Unknown keys are ignored, with a warning.

## 3. The plugin

The entry file must register **at least one plugin class whose `type` equals
the folder name**. It may register more than one type.

The engine provides `engine.registry`, `engine.signals`, `sources.base` and
`sinks.base`. Import only those from the engine.

### Ports

Every value on the engine's bus is a plain `float`. A port declares what one
value means:

```python
from engine.signals import PortSpec, SCALAR, EVENT

PortSpec("gyro.x", SCALAR, "gyro.x (pitch rate)", "motion", "deg/s", "hint shown on hover")
#        id        type    label                  group     unit     hint
```

- **`type` is `SCALAR`** for continuous values, **or `EVENT`** for a 0/1 gate
  or one-tick pulse. A scalar patched into an event port chatters unless a
  threshold node sits between them, so pick the type honestly.
- **Flatten vectors into one port per axis** (`accel.x`, `accel.y`, `accel.z`).
- Use dotted ids grouped by meaning (`btn.a`, `stick.x`, `mouse.dx`). Presets
  refer to ports as `<id>/<port>`, so **port ids are an API: don't rename
  them** in a minor version.
- `group` sorts ports in the module card. `unit` and `hint` are shown to the user.

### A source

```python
from engine.registry import register_source
from engine.signals import PortSpec, SCALAR, EVENT
from sources.base import Source


@register_source
class MySensor(Source):
    type = "mysensor"            # == the folder name
    id = "mysensor"              # default bus namespace: ports appear as mysensor/<port>
    label = "My Sensor"          # shown in the menus
    PORTS = [PortSpec("tilt", SCALAR, "tilt", "motion", "deg"),
             PortSpec("button", EVENT, "button", "buttons")]
    ARGS = {"port": {"default": None, "type": str, "help": "serial device; auto if omitted"}}

    @classmethod
    def available(cls):
        try:
            import serial  # noqa: F401  (lazy: only to check it's there)
        except Exception as e:
            return False, f"needs pyserial ({e})"
        return True

    def __init__(self, port=None): ...
    def start(self): ...           # open the device, start your reader thread
    def stop(self): ...            # stop the thread, close the device
    def status(self): return "ok"  # or "searching", "disconnected", "error: …"
    def read(self):                # called every engine tick
        return {"tilt": self._tilt, "button": 1.0 if self._pressed else 0.0}
```

**Requirements:**

- **`read()` must not block, and must be cheap.** It runs inside the engine's
  fixed-rate loop (120 Hz by default). Do the device I/O on your own thread and
  have `read()` return the latest values.
- **`read()` returns `{port id: float}`** for the ports in `PORTS`. A port it
  leaves out reads as 0 that tick.
- **Import third-party packages lazily**, inside `__init__`, `start` or
  `available`, never at the top of the file. The class must still register
  when its packages are missing, so the menus can list it greyed out with a reason.
- **`available()` is a cheap check** (an import, not a device connection). It
  returns `True`, or `(False, "reason")`.
- **`stop()` releases everything**: threads end, devices close. Keep
  `start()` quick: open the device and hand slow work (pairing, retries) to
  your thread.
- A failure in the device (unplugged, out of range) shows up in **`status()`**.
  It must never raise out of `read()`.
- `ARGS` lists the constructor's keyword arguments: `default`, `type` (`str`,
  `int`, `float`, or a function that parses `"on"`/`"off"` for a toggle) and
  `help`. Values typed on the command line arrive as strings and are converted
  with `type`.
- A camera-style source may set `PREVIEW = True` and return JPEG bytes (or
  `None`) from `preview_jpeg()` to show a live preview.

### A sink

```python
from engine.registry import register_sink
from engine.signals import PortSpec, SCALAR, EVENT
from sinks.base import Sink


@register_sink
class MyOutput(Sink):
    type = "myoutput"
    id = "myoutput"
    label = "My Output"
    PORTS = [PortSpec("level", SCALAR, "level", "out", "0..1"),
             PortSpec("fire", EVENT, "fire", "out")]

    def apply(self, values, dt):    # called every engine tick
        level = values.get("level", 0.0)   # absent = no signal: treat as 0
        ...
    def release_all(self): ...      # let go of everything held, now
```

**Requirements:**

- **`apply(values, dt)` gets `{port id: float}` every tick.** A port missing
  from `values` means *no signal*: **treat it as 0 and release whatever it was
  holding** (a key, a button, a motor).
- **`release_all()` must silence every output immediately.** It runs on
  PANIC, on removal, and on shutdown, and may be called more than once.
- **`apply()` must not block.** Queue slow I/O to your own thread.
- Several cables into one sink port are summed. Clamp if your output has a range.
- `accepts_any = True` lets presets target port ids that aren't in `PORTS`
  (e.g. one port per key name).
- The same rules as sources apply to lazy imports, `available()`, `status()`,
  `ARGS` and `stop()`.

### Stand-ins and namespaces

`type` and `id` are separate on purpose. `id` is the namespace presets are
written against. A stand-in can keep its own `type` and **borrow the real
module's `id`**, so a preset runs unchanged on either: a simulator could have
`type = "controller_sim"` and `id = "controller"`. **Keep `id` stable across versions**, since saved presets
depend on it.

### Using another module's code

List the module in `depends`, then import it by package:
`from sources.<that_module> import SOMETHING`. Don't import a module you haven't
listed: it may not be installed.

## 4. Checklist before a pull request

- [ ] Folder name = `type`, a Python identifier, unique in the repo.
- [ ] `module.toml` has `kind`, `description` and `version`, and the version is bumped if the module already existed.
- [ ] Third-party imports are lazy; `available()` explains what's missing.
- [ ] `read()` / `apply()` never block; sinks release on absent ports and in `release_all()`.
- [ ] Port ids and `id` unchanged, unless this is a major version.
- [ ] Manual setup steps are in `system`, and longer ones in the module's README.
- [ ] `python tools/build_index.py` run and `index.json` committed with the change.

CI checks the last item on every pull request.

---

# Writing a preset

A preset is one patch: the cables and the transform chain on each. Build it in
the patch panel, **Save** it, then copy the file from your `presets/` folder into
this repo's `presets/`. Requirements (`tools/build_index.py` checks all of them):

- **The file is `presets/<name>.json`**, and `<name>` is letters, digits, space,
  `.`, `_` or `-`. If the file has a `"name"`, it matches.
- **A one-line `"description"`**, shown in the catalog.
- **A `"requires"` list naming every namespace the cables use** (except the
  engine's own `sys`): `{"id": "controller", "kind": "source", "types":
  ["controller"]}`. **Every type listed must be a module in this repo.** Saving
  from the patch panel writes `requires` for you; check it names only catalog
  modules.
- Connections refer to ports as `<id>/<port>`. Use the module's default `id`
  (`controller`, not `controller-2`), so the preset works on a fresh install.
- Prefer giving connections readable `id`s (`m_dx`, `k_space`). They're kept
  across edits.

Bump nothing: presets have no version. The engine tells an updated preset
from an edited one by its install record, and asks before overwriting edits.

---

## Repo layout

```
index.json                 generated; don't edit by hand
sources/<type>/            source modules
sinks/<type>/              sink modules
presets/<name>.json        presets
tools/build_index.py       regenerates index.json (standard library only, Python 3.11+)
```

`index.json` lists every module with its manifest fields and every file's path,
size and sha256, and every preset with its description, requires and sha256. The engine downloads files relative to the index's URL, checks
each hash, and refuses the whole module if one doesn't match. So **commit
`index.json` in the same commit as the files it describes.** CI fails a pull
request whose index is stale, and rebuilds it on `main`.

The engine keeps a record of what it installed. An installed module or preset
whose files were edited (or saved over) afterwards shows as **modified locally** and isn't overwritten
without asking. One copied in by hand shows as **local only**.

## Developing a module against a local engine

Point the engine at a checkout of this repo instead of GitHub:

```bash
PATCHBAY_CATALOG=path/to/sensor-patchbay-modules python run.py    # install from your checkout
python run.py --ext path/to/sensor-patchbay-modules/sources \
              --ext path/to/sensor-patchbay-modules/sinks       # or load the folders in place
```

`catalog = "..."` in `patchbay.toml` does the same as `PATCHBAY_CATALOG`. It
takes any URL that serves this layout (a fork, GitHub Pages, a LAN server) or a
local path. After editing a module, press **⟳ Sync** in the patch panel to
reload it; remove and re-add a running instance to pick up the change.
