# macos

macOS system shortcuts as patchable jacks: media and volume, display and
keyboard brightness, Mission Control and Spaces, windows and apps, screenshots,
Focus modes, and any shortcut from the Shortcuts app.

Every port except `volume.rate` and `brightness.rate` fires **once** when its
gate rises past 0.5 and re-arms when it drops below. Holding doesn't repeat; for
a knob, patch a stick or tilt into a `*.rate` port (steps per second, + up / - down).

## Setup

1. **Accessibility.** Give the app running the engine (Terminal, your IDE, or
   sensor-patchbay.app) access in *System Settings > Privacy & Security >
   Accessibility*. Without it the key events are silently dropped.
2. **Numbered desktops** (`desktop.1` … `desktop.9`) send ctrl+1 … ctrl+9.
   Turn on *Switch to Desktop n* in *System Settings > Keyboard > Keyboard
   Shortcuts > Mission Control*, and create that many desktops. `desktop.left`
   and `desktop.right` (ctrl+← / ctrl+→) are on by default.
3. **Focus modes.** macOS has no public way to set a Focus from code, so each
   `focus.<mode>` runs a shortcut from the Shortcuts app named
   `Focus: <Mode>`. Make one per mode you patch:

   | port | shortcut name | action in it |
   |---|---|---|
   | `focus.dnd` | `Focus: Do Not Disturb` | *Set Focus* → Do Not Disturb, **Toggle** (or On) |
   | `focus.work` | `Focus: Work` | *Set Focus* → Work, Toggle |
   | `focus.personal` | `Focus: Personal` | *Set Focus* → Personal, Toggle |
   | `focus.sleep` | `Focus: Sleep` | *Set Focus* → Sleep, Toggle |
   | `focus.off` | `Focus: Off` | *Set Focus* → Do Not Disturb, **Turn Off** (turns off any Focus) |

   Any other `focus.<mode>` works the same way (`focus.gaming` → `Focus: Gaming`;
   underscores become spaces). Change the `Focus: ` prefix with the
   `focus_prefix` arg.

## Ports

| group | ports | sends |
|---|---|---|
| media | `play_pause`, `next`, `previous` | the media keys |
| volume | `mute`, `volume.up`, `volume.down`, `volume.rate` | the volume keys |
| display | `brightness.up`, `brightness.down`, `brightness.rate` | the brightness keys (built-in and Apple displays) |
| | `kbd_light.up`, `kbd_light.down` | keyboard backlight keys |
| | `display.sleep` | `pmset displaysleepnow` |
| spaces | `mission_control`, `app_windows`, `show_desktop` | opens Mission Control (all windows, App Exposé, desktop) |
| | `desktop.left`, `desktop.right` | ctrl+← / ctrl+→ |
| | `desktop.<n>` (1-9) | ctrl+n |
| apps | `spotlight` | cmd+space |
| | `apps` | opens Apps (Launchpad before macOS 26) |
| | `app.switch`, `app.hide`, `app.quit` | cmd+tab, cmd+H, cmd+Q |
| | `open.<App Name>` | `open -a "<App Name>"`: launch or bring forward |
| windows | `window.close`, `window.minimize`, `window.fullscreen`, `window.cycle` | cmd+W, cmd+M, ctrl+cmd+F, cmd+` |
| screen | `screenshot.screen`, `screenshot.area`, `screenshot.tools` | cmd+shift+3 / 4 / 5 |
| | `lock` | ctrl+cmd+Q |
| focus | `focus.<mode>` | `shortcuts run "Focus: <Mode>"` |
| shortcuts | `shortcut.<name>` | `shortcuts run "<name>"` |

`desktop.<n>`, `focus.<mode>`, `shortcut.<name>` and `open.<app>` take any
name from a preset. The args choose which of them show as jacks in the patch
panel:

| arg | default | |
|---|---|---|
| `desktops` | `4` | `desktop.1` … `desktop.4` |
| `focus_modes` | `dnd, work, personal, sleep, off` | comma-separated |
| `focus_prefix` | `Focus: ` | shortcut name = prefix + Mode |
| `shortcuts` | | comma-separated Shortcuts names, e.g. `Open App, Start Timer` |
| `apps` | | comma-separated app names, e.g. `Safari, Music` |

Actions run in order on a worker thread, so a slow `shortcuts run` never stalls
the engine. A failing one (a shortcut that doesn't exist, an app that isn't
installed) shows its error as the sink's status. PANIC drops anything still
queued.
