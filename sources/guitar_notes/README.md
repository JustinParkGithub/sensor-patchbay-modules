# guitar_notes

Guitar notes from an ESP32 guitar tuner (ESP-IDF, PCM1808 ADC → pitch
detection), read over USB serial at 115200 baud.

The firmware prints the nearest note name, one per line, when a new note has
held for a couple of analysis frames:

```
Guitar tuner ready (ESP32, I2S 48000 Hz, analysis 24000 Hz).
E2
A#3
```

A note is printed once when it starts, not while it rings, and again after
silence. Flat spellings (`Bb3`) are accepted too. Every other line is ignored.

## Ports

| port | type | what |
|---|---|---|
| `note.E2` … `note.E6` | event | one gate per note across the neck (open low E to the 24th fret on the high E). Sharps are spelled `s`: `note.Cs3` is C#3 |
| `note.active` | event | any note is up |
| `pitch.midi` | scalar | MIDI number of the current note (E2 = 40), 0 when none |
| `pitch.hz` | scalar | equal-tempered frequency of that note |
| `fret` | scalar | fret of that note on an E string (E2 = 0, E6 = 48) |

A printed note's gate stays up for `hold` seconds (default 0.12), or until a
different note is printed, so one pluck is one key tap. Plucking the same note
again while it is up drops the gate for one tick, so the key is pressed again.

## Options

| arg | default | |
|---|---|---|
| `port` | auto | serial device; empty picks the first CP210x / CH34x / FTDI / Espressif board |
| `baud` | 115200 | the firmware's console baud rate |
| `hold` | 0.12 | seconds a note stays up after it's printed |

Only one program can hold the serial port: close `pio device monitor` first.

## Preset: `guitar-fretboard`

Each keyboard row is a 12-fret octave of an E string, and each column is a
fret, so the same column is always the same note name:

| row | notes | keys, frets 0 → 11 |
|---|---|---|
| number | E5–D#6 | `1 2 3 4 5 6 7 8 9 0 - =` |
| top | E4–D#5 | `q w e r t y u i o p [ ]` |
| home | E3–D#4 | `a s d f g h j k l ; ' enter` |
| bottom | E2–D#3 | `z x c v b n m , . / shift_r up` |

E6 (24th fret) is `backspace`. The bottom row has only ten character keys, so
D3 and D#3 are right Shift and Up.
