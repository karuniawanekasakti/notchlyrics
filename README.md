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
| **Preset: Spotify / Pill / Card** | one click sets every field below to that style |
| Source | which player to follow. The list is built from the SMTC sessions Windows is publishing right now, so every open player appears (`Brave`, `Spotify`, `msedge`, …) and idle ones are listed too. A `tab: ` entry pins the overlay to one browser tab that is making sound, which is the only way to tell two tabs of the same browser apart (see below). `Auto` follows whichever session is actually playing. The field is editable, so a player that is not running yet can still be typed in |
| Visible lines | 1 gives a single-line pill, 5 gives the Spotify panel |
| Active line position | `top`, `center` or `bottom`: which of the visible rows carries the highlighted lyric. `bottom` means the playhead sits on the last row and the lyrics scroll up past it, `top` means it sits on the first row and they run down |
| Row height | pitch between rows; total height is row height × visible lines. If it is shorter than the font's ink box it is raised automatically so descenders (`g y p j`) are never clipped |
| Width (px) | width of the block |
| Top offset | fine vertical nudge on top of **Position** |
| Horizontal offset | fine horizontal nudge on top of **Position** |
| Position | where the bar sits on screen: `top-center` (the notch), `bottom-center`, or any of the four corners. Anchors are resolved against the screen's work area, so the top edge sits flush at `y=0` and the bottom edge stops just above the Windows taskbar instead of hiding under it. The side that touches a screen edge is drawn square, so the shape always reads as cut into that edge — `top-center` rounds only the bottom corners, `top-left` keeps a single rounded corner. `Top offset` / `Horizontal offset` then nudge from there |
| Font | dropdown of installed fonts, and the field is still editable |
| Font size | size of every line. Emphasis comes from colour and position, not size, so the glyphs never have to be re-rasterised mid-glide |
| Bold active line | bold for every line (the font must stay constant, see above) |
| Active line colour | white `#ffffff` by default |
| Inactive line colour | grey `#b3b3b3` by default |
| Background colour | the card fill when the card is on, and the window fill when transparency is off |
| Rounded card container | paints a rounded panel behind the lyrics |
| Card radius | corner radius of that panel |
| Card border colour | 1px outline around the card |
| Show album art | draws the track's cover art from the Windows media session to the left of the lyrics |
| Waveform bars | synthetic equalizer on the right, animated from a sine — not real audio (WASAPI loopback returns silence on this machine). Width, colour and bar count are all fields |
| Slide lyrics in on a new track | on a track change the outgoing lyrics slide left and fade out, then the new ones slide in from the right. Off makes it snap. Each track gets its own curve (see below) so consecutive songs do not move identically. `ENTER_MS` / `ENTER_OUT` at the top of the file set the length and the leave/arrive split |
| Transparent background | Windows colour-key transparency — only the card and the text are visible |
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
| `sessions()` | every SMTC session on the machine, songs and non-songs alike — feeds both the overlay and the Source dropdown |
| `app_names()` | `Auto`, then one `tab: ` entry per browser tab that is making sound, then one label per live session (`Spotify.exe` → `Spotify`, `PackageFamily!AppId` → `AppId`); returns just `Auto` if SMTC is unavailable |
| `browser_tabs()` | the tab strip of every Chromium-family window via UI Automation (`uiautomation`). SMTC publishes one session per browser, so this is the only source of per-tab identity. Cached for the poll loop, full price (~0.5 s) for the dropdown |
| `split_track()` | `Sydney Rose - We Hug Now (Official Video)` → `("We Hug Now", "Sydney Rose")`. A browser publishes the video title as the track name, and lrclib matches nothing on it, which is why YouTube used to show its own title as a lyric line |
| `pick_session()` | playing wins over paused, then an optional case-insensitive app-id filter from the Source setting; a `tab: ` source is resolved by `_probe()` instead |
| `_probe()` | `sessions()` filtered by `is_song()` and handed to `pick_session()`; for a `tab: ` source it keeps the browser's session (position, length, cover) and uses the tab label to confirm the tab is still playing |
| `fetch_lyrics()` | `lrclib.net/api/search` → `syncedLyrics` (free, no API key); picks the version whose duration matches the playing track |
| `parse_lrc()` | `[mm:ss.xx]text` → `[(seconds, text)]` |
| `poll()` | background thread: track + position → `state["lines"]` and `state["idx"]`. Re-reads the cover for a short window after a track change, because players publish the new title before the real artwork (Brave substitutes its own app icon first) Holds the previous track's lyrics through the ~1 s SMTC gap of a track change (`GAP_S`), so the outgoing text is still on screen for the swap; a pause or stop still clears at once |
| `screen_work()` | primary monitor's work area via `SystemParametersInfoW(SPI_GETWORKAREA)` — the screen minus the taskbar, on any side. Returns `None` if Windows reports nothing, and the caller falls back to the raw screen |
| `anchor_xy()` | anchor + work area → top-left pixel. The single place that knows top edge is `work.top` and bottom edge is `work.bottom - h` |
| `notch_corners()` | which corners stay rounded for a Position anchor: every corner not on a flush screen edge |
| `notch_shape()` | screen-edge polygon, computed point by point; `rounded` names the corners that keep the radius |
| `Overlay.set_art()` | decodes the PNG bytes from `art_png()` and (re)places the cover label; subsamples by powers of two to the requested size. The cache key is the bytes themselves, so a cover that arrives late replaces the placeholder |
| `round_rect()` | rounded rectangle as a smoothed polygon — Tk has no primitive for it |
| `Overlay.render(pos)` | `pos` is a fractional line index; rows move per pixel, emphasis comes from `lerp_color(dim, fg, act)` only |
| `Overlay.animate_to()` | `after(16ms)` for ~260 ms, ease-out cubic — the playhead glides instead of jumping |
| `Overlay._enter_from()` | new-track swap: picks this track's curve with `ease_for()` and drives `_enter` 0 → 1 over `ENTER_MS`; `render()` turns that into a left exit then a right entry, and blends the text from the card colour |
| `ease_for()` | maps a track's lyrics to one of `EASINGS` (`in-out-sine`, `in-out-cubic`, `in-out-quint`, `out-back`) via `crc32` — stable per track, and unlike `hash()` it does not change between launches. The departure half uses the same curve mirrored, so every curve starts slow and the motion is continuous through the midpoint |
| `Overlay.apply()` | geometry from `screen_work()` + `anchor_xy()` (work area minus taskbar, so bottom anchors clear the taskbar), alpha, card and font; `-transparentcolor` for the transparent background |
| `App` | the launcher window; every field change calls `apply()` and updates the overlay |

The transition works because `pos` is a float, not an integer. When the lyric
advances, `animate_to()` moves `pos` from 3.0 to 4.0 over 260 ms along the
`1-(1-t)³` curve. Each frame repositions every label to `(i - top) * row` and
interpolates its colour from `dim` towards `fg` by `1 - |i - pos|`, so midway
through the transition two adjacent lines are each half-bright — the same fade
Spotify uses. A new track snaps rather than sliding out of the previous song.

Emphasis is deliberately colour-only. An earlier version also interpolated the
font size and toggled bold at 50 % brightness; Tk fonts are integer-sized, so a
single glide produced exactly 5 distinct sizes, 33 weight flips and 160 font
reconfigurations, and the text width jumped up to 84 px. That is what made the
motion look stiff — not the framework. One constant font keeps the layout still
and lets only the colour animate.

On a card, the lyric labels carry the card colour instead of the colour key.
`-transparentcolor` applies to the composited window, so a label painted in the
key colour would punch a transparent hole through the card; the labels are also
inset by the radius so their rectangles never cover the rounded corners.

`bootstrap()` at the top of the file handles being opened by a Python that
lacks `winsdk` (3.14, for instance): it relaunches itself with
`.venv\Scripts\pythonw.exe`.

## Known limits

- Brave keeps its SMTC session while the window is minimized, but reports a
  frozen timeline (`position` stays at 0.01 s, `last_updated_time` at the moment
  it lost focus). Position is extrapolated from that anchor, so lyrics keep
  tracking normally; only a pause or seek performed while minimized can leave
  the anchor stale until playback controls next update the session.
- SMTC is per application, not per tab: a browser publishes exactly one session
  however many tabs are open. `tab: ` entries exist because the tab strip is
  read separately through UI Automation, which needs the optional
  `uiautomation` package. Without it the dropdown falls back to one entry per
  player and `Auto` follows whichever tab is making sound — correct for one
  playing tab, ambiguous when two are.
- A tab only appears in the Source list while it is making sound; Brave marks
  that with ` - Audio playing` in the tab's accessible name.
- If the player drops the session entirely (Brave closed, playback stopped) the
  lyrics stay up for `GAP_S` (2 s) before disappearing, because a track change
  tears the session down the same way. A pause or stop clears at once.
- Tracks with no synced lyrics on lrclib fall back to `♪ title — artist`.
- Multi-monitor: the overlay is always on the primary display.
- Lyrics about 0.3 s ahead of the vocal? There is no offset control yet.
- The animation uses `after(16ms)`, so on a slow machine it may drop to roughly
  30 fps. Still smooth, just not 60. Tune `ANIM_FRAME_MS` / `ANIM_MS` at the top
  of the file.
- Transparency is colour-key based, so text that happens to be exactly
  `#010203` disappears with the background. The defaults are safe.
- The colour key is 1-bit: the card can be rounded and filled, but it cannot
  have a soft shadow, a glow or per-pixel alpha, and Tk's canvas has no
  anti-aliasing, so the corners are stair-stepped when you zoom in. Real blur
  needs a web view (`backdrop-filter`); a Tauri rewrite was measured and
  rejected — no Rust/MSVC toolchain, ~4.5 GB of downloads, and its
  click-through is all-or-nothing.
- The cover art is downscaled with `PhotoImage.subsample`, which only divides by
  whole numbers, so it is snapped to the nearest power of two and centred. No
  resampling filter, no anti-aliasing.
- Not packaged as an `.exe` yet — it needs Python 3.12 installed (`winsdk` has
  no 3.14 wheel).
