# NotchLyrics

Synchronized song lyrics pinned to the top-center of a Windows screen, in the
notch position. Two visual styles ship built in: **Spotify** (multiple lines,
active line bold and white, neighbours dimmed, no background box) and a classic
black **pill**. The overlay is always on top and click-through, so it never
steals input from the application underneath. A launcher window controls every
setting.

This is not a browser extension, so it is not confined to a browser window.
Track metadata is read from **SMTC** (the Windows media session API), which
means it works with Brave, Chrome, Edge, YouTube, the Spotify desktop client —
anything that publishes a media session.

## Usage

1. Once only: double-click `setup.cmd`. It creates `.venv`, installs `winsdk`,
   and runs the self-tests.
2. Double-click `NotchLyrics.pyw`. The launcher window opens and the lyrics
   appear at the top-center of the screen.

There is no autostart and nothing runs in the background behind your back.
Closing the launcher window exits the application. If you want it to start
automatically, set that up yourself (`shell:startup`).

Launching it twice does not create a second overlay — a named mutex makes the
second instance exit immediately.

## Configuration

Everything is in the launcher window, and changes apply live:

| Control | Effect |
|---|---|
| **Preset: Spotify / Pill** | one click sets every field below to that style |
| Visible lines | 1 gives a single-line pill, 5 gives the Spotify panel |
| Row height | pitch between rows; total height is row height × visible lines. If it is shorter than the font's ink box it is raised automatically so descenders (`g y p j`) are never clipped |
| Width (px) | width of the block |
| Top offset | vertical position (raise it to about 34 if the notch or taskbar covers the text) |
| Horizontal offset | offset from horizontal center |
| Font | dropdown of installed fonts, and the field is still editable |
| Font size | size of the active line; every other line is 78% |
| Bold active line | active line bold, the rest regular |
| Active line colour | white `#ffffff` by default |
| Inactive line colour | grey `#b3b3b3` by default |
| Background colour | used when the transparent background is disabled |
| Transparent background | Windows colour-key transparency — only the text is visible |
| Opacity | slider from 0.2 to 1.0 for the whole window |
| Click-through | when disabled, the overlay becomes clickable |
| Hide lyrics | toggles the overlay without closing the launcher |

Settings are saved automatically to `config.json` next to the script and read
back on the next launch. It is plain JSON and may be edited by hand; invalid
values are clamped or reset to defaults rather than crashing the application.

For a closer Spotify match: click the **Spotify** preset, raise *Top offset*
until the active line sits just below the notch, and reduce *Visible lines* to
3 if the panel feels too tall.

## Tests

```
.venv\Scripts\python.exe NotchLyrics.pyw --test    # LRC parser, config, presets
.venv\Scripts\python.exe test_ui.py                # launcher, rendering, animation
```

Verify the live SMTC session and real lyrics (a track must be playing):

```
.venv\Scripts\python.exe -c "import importlib.util as u; s=u.spec_from_file_location('n','NotchLyrics.pyw'); n=u.module_from_spec(s); s.loader.exec_module(n); print(n._probe()); print(n.fetch_lyrics('Patience','Take That')[:3])"
```

## How it works

| Component | Responsibility |
|---|---|
| `_probe()` | `winsdk` → `GlobalSystemMediaTransportControlsSessionManager`; reads title, artist, playback position and status |
| `fetch_lyrics()` | `lrclib.net/api/search` → `syncedLyrics` (free, no API key); picks the version whose duration matches the playing track |
| `parse_lrc()` | `[mm:ss.xx]text` → `[(seconds, text)]` |
| `poll()` | background thread: track + position → `state["lines"]` and `state["idx"]` |
| `Overlay.row_h()` | effective row pitch = `max(height, font linespace + 2)`; used by `apply()` and `render()` so no glyph is clipped |
| `Overlay.render(pos)` | `pos` is a fractional line index; every row is offset per pixel while colour and size are interpolated from its distance to the playhead |
| `Overlay.animate_to()` | `after(16ms)` for ~260 ms, ease-out cubic — the playhead glides instead of jumping |
| `Overlay.apply()` | geometry, alpha and font; `-transparentcolor` for the transparent background |
| `App` | the launcher window; every field change calls `apply()` and updates the overlay |

The transition works because `pos` is a float, not an integer. When the lyric
advances, `animate_to()` moves `pos` from 3.0 to 4.0 over 260 ms along the
`1-(1-t)³` curve. Each frame repositions every label to `(i - top) * row` and
interpolates its colour from `dim` towards `fg` by `1 - |i - pos|`, so midway
through the transition two adjacent lines are each half-bright — the same fade
Spotify uses. A new track snaps rather than sliding out of the previous song.

`bootstrap()` at the top of the file handles being opened by a Python that
lacks `winsdk` (3.14, for instance): it relaunches itself with
`.venv\Scripts\pythonw.exe`.

## Known limits

- Brave keeps its SMTC session while the window is minimized, but reports a
  frozen timeline (`position` stays at 0.01 s, `last_updated_time` at the moment
  it lost focus). Position is extrapolated from that anchor, so lyrics keep
  tracking normally; only a pause or seek performed while minimized can leave
  the anchor stale until playback controls next update the session. Tab focus is
  irrelevant — SMTC is per application, not per tab.
- If the player drops the session entirely (Brave closed, playback stopped),
  lyrics disappear until a new session appears.
- Tracks with no synced lyrics on lrclib fall back to `♪ title — artist`.
- Multi-monitor: the overlay is always on the primary display.
- Lyrics about 0.3 s ahead of the vocal? There is no offset control yet.
- The animation uses `after(16ms)`, so on a slow machine it may drop to roughly
  30 fps. Still smooth, just not 60. Tune `ANIM_FRAME_MS` / `ANIM_MS` at the top
  of the file.
- Transparency is colour-key based, so text that happens to be exactly
  `#010203` disappears with the background. The defaults are safe.
- Not packaged as an `.exe` yet — it needs Python 3.12 installed (`winsdk` has
  no 3.14 wheel).
