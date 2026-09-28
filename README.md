# Virtual Desktop Suite

A single-process Windows 11 virtual desktop companion that combines three
tools into one:

1. **Taskbar desktop strip** — numbered pills for every virtual desktop,
   drawn directly on the taskbar. Click to switch.
2. **Taskbar auto-hide hotkey** — flip the taskbar auto-hide state with a
   global hotkey, staying in sync with the Settings UI.
3. **Maximize to a new virtual desktop** — hold a modifier key while
   maximizing a window (or press a hotkey) to send it to its own temporary
   desktop, then get it back automatically.

The three are designed to interlock: the temporary desktops created by (3) are
named `[MVD] <ProcessName>`, and (1) reads that name to display the app name
instead of a number, while optionally hiding the taskbar for a full-screen
feel. Leaving the desktop restores everything.

> Status: **experimental / personal project.** It works on the author's
> machine and has a fairly large automated verification suite, but it has not
> been tested broadly. Expect rough edges.

---

## Features

### 1. Taskbar desktop strip

- One pill per virtual desktop, embedded into `Shell_TrayWnd` so it follows
  the taskbar's position and visibility.
- **Colour expresses exactly one thing: whether the desktop is current.**
  No secondary colour coding.
- Desktop `[MVD] <ProcessName>` shows a short form of the app name
  (3 Latin characters, or 2 for CJK) instead of a number.
- Pill widths adapt to their labels, with an upper bound so the strip never
  drifts.
- A second **floating** copy of the strip appears when the taskbar auto-hides,
  because the taskbar slides entirely off-screen and would take the embedded
  copy with it.

### 2. Taskbar auto-hide hotkey

- Toggles the same registry value the Settings UI writes
  (`HKCU\...\Explorer\StuckRects3`), applied live via
  `SHAppBarMessage(ABM_SETSTATE)` — no Explorer restart.
- Default hotkey `Ctrl+Alt+T`, with automatic fallback to `Ctrl+Alt+H` and
  then `Ctrl+Alt+B` if it is already taken. The settings panel shows which one
  actually registered.

### 3. Maximize to a new virtual desktop

- **Plain maximize does nothing.** Exclusive full-screen desktop must be
  explicitly requested, via either:
  - holding a modifier key (default `Shift`, configurable) while maximizing —
    clicking the maximize button, double-clicking the title bar, or `Win+↑`;
  - or the global hotkey `Ctrl+Alt+Shift+X`.
- Creates a new desktop, names it `[MVD] <ProcessName>`, moves the window
  there, switches to it, and maximizes it.
- Restores the window and removes the temporary desktop on un-maximize,
  close, or pressing the hotkey again.

---

## Usage

The app runs as a background process with an optional floating strip. There
is no system tray icon — right-click the strip itself for the settings panel,
or press the hotkey for the taskbar toggle.

The settings panel exposes the modifier key used to arm exclusive
full-screen desktops, the taskbar-linking toggle, and the immersive-hide
toggle.

---

## Architecture

```
source/
├── desktop_suite.py          Core: virtual desktops, hotkeys, taskbar toggle,
│                             the maximize-to-desktop logic, LAF shell
├── desktop_suite_edge.py     Experimental entry point: edge-reveal taskbar
│                             and a clock face on the strip
├── suite_ui.py               Pure rendering (Pillow): pills, menu, geometry.
│                             No system calls — renderable offline to PNG
├── layered_pill.py           Small Win32 per-pixel-alpha window used for the
│                             floating strip
├── tray_watchdog.cs          Tiny C# helper: if the suite dies while the
│                             taskbar is force-hidden, restore the taskbar
│                             (`tray_watchdog.exe`)
├── build_exe.py              PyInstaller packaging script
├── preview_pill.py           Render the strip in any state to a PNG, offline
├── preview_menu.py           Same, for the settings menu
└── verify_*.py, smoke_visual.py
                              Automated verification suite (see below)
```

Design notes worth knowing:

- **Rendering and system interaction are strictly separated.**
  `suite_ui.py` is pure functions plus Pillow, so any state can be rendered
  to a PNG offline for visual review — far faster than iterating on a live
  taskbar.
- **Colour is never used to encode two things at once.** Whether the desktop
  is current is the only thing colour says; app-name desktops are identified
  by their label, not by a different colour.
- The strip is drawn with key-colour transparency, with rounded-corner edges
  composited against a **measured** taskbar background colour rather than
  being binarised, to avoid a visible halo.

---

## Requirements

- **Windows 11**
- **Python 3.12** with tkinter (to run from source; the released `.exe` is
  self-contained)

Runtime dependencies (`requirements.txt`): `pyvda`, `Pillow`, `comtypes` —
all bundled into the released executable. See `THIRD-PARTY-NOTICES.md` for
their licenses.

---

## Building

Running from source:

```bat
pip install -r requirements.txt
python source\desktop_suite.py
```

Note: you need a Python build **with tkinter**. Some minimal or managed
distributions ship without it.

Packaging into a single `.exe`:

```bat
python source\build_exe.py                REM -> VirtualDesktopSuite.exe
python source\build_exe.py <entry> <name> REM custom entry script and output name
```

The build script pins the icon, the vendored `libs/` path handling, and a
slimming exclusion list. It expects PyInstaller to be available; adjust the
`pyi_env` path or install it into your environment.

---

## Verification

The project ships a reasonably large automated verification suite (roughly
50 assertions across three outcomes: `PASS` / `FAIL` / `SKIP`, where `SKIP`
means the environment blocked the check rather than the check failing).

```bat
python source\verify_suite.py            REM run against source
python source\verify_suite.py --exe      REM run against the packaged exe
```

It restores the desktop state it touched on exit. There are also smaller,
offline suites:

| Script | What it checks |
|---|---|
| `verify_edges.py` | Rounded-corner compositing, against a **measured** taskbar colour |
| `verify_panel.py` | Settings panel layout and text ellipsis |
| `order_contracts()` | Five ordering / state-residue contracts, runnable off-screen |

---

## Credits & Prior Art

This project stands on other people's work. Concretely:

**Feature 3 (maximize to an exclusive full-screen desktop) is borrowed from
[MaximizeToVirtualDesktop](https://github.com/shanselman/MaximizeToVirtualDesktop)
by Scott Hanselman (MIT).** The idea, the `Ctrl+Alt+Shift+X` hotkey, and the
`[MVD] <ProcessName>` desktop naming convention all come from there. The
implementation here is written from scratch in Python on top of `pyvda` rather
than reusing that project's C# code, with two deliberate changes:

- it only triggers when a modifier key is held (or the hotkey is pressed),
  instead of on every maximize;
- the `[MVD]` prefix is also read back by feature 1, so an exclusive desktop
  shows up in the strip as the app's name rather than a number.

**Features 1 and 2 are original to this project** — there is no upstream tool
to borrow them from:

- The taskbar desktop strip (click-to-switch pills, current-desktop highlight,
  app-name labels) is built here from scratch, including the Win32 embedding
  and the per-pixel-alpha floating copy.
- The taskbar auto-hide hotkey first appeared in the author's own
  [TaskbarAutoHideToggle](https://github.com/Fantin66/TaskbarAutoHideToggle),
  and was folded into this project.

### Dependencies

- **[pyvda](https://github.com/mrob95/py-VirtualDesktopAccessor)** (MIT) —
  virtual desktop API wrapper
- **[Pillow](https://python-pillow.org)** (MIT-CMU) — pill and menu rendering
- **[comtypes](https://github.com/enthought/comtypes)** (MIT) — COM plumbing

Full license texts are in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md),
with verbatim copies under [`licenses/`](licenses/). If you download a binary
release, those files must ship alongside it.

---

## Known Limitations

- **Orphaned `[MVD]` desktops.** Temporary desktops are only cleaned up if
  this run created them. Desktops left behind by a previous run are not
  reclaimed on startup. MaximizeToVirtualDesktop handles this with a persisted
  tracking file; that is not implemented here yet.
- **Elevated windows** cannot be moved by a non-elevated instance — same
  constraint as any tool in this space.
- The strip assumes a single primary taskbar. Multi-monitor secondary
  taskbars are not handled.

---

## License

MIT — see [`LICENSE`](LICENSE).

This project builds on reverse-engineered, undocumented Windows COM
interfaces for virtual desktop management. Those interfaces are not
guaranteed to be stable across Windows updates.
