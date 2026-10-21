"""UI check: build the real launcher (mainloop stubbed), verify every control
reaches the overlay, that presets restyle it, that the transition actually
animates, and that a config written by the UI is read back.

    .venv\\Scripts\\python.exe test_ui.py
"""
import base64
import importlib.machinery
import importlib.util
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

HERE = Path(__file__).resolve().parent

# spec_from_file_location() returns None for a .pyw on POSIX, so name the loader
spec = importlib.util.spec_from_loader(
    "nl", importlib.machinery.SourceFileLoader("nl", str(HERE / "NotchLyrics.pyw")))
nl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nl)

# isolate config so a real config.json is never touched
nl.CONFIG = Path(tempfile.gettempdir()) / "nl_ui_test_cfg.json"
nl.CONFIG.unlink(missing_ok=True)

tk.Tk.mainloop = lambda self, n=0: None      # no blocking loop
tk.Toplevel.mainloop = lambda self, n=0: None

# kill the real poll thread: it hits SMTC + lrclib and would race our fake state
real_poll = nl.poll
nl.poll = lambda state, stop, cfg=None: None
if sys.platform != "win32":
    nl.sessions = lambda: []         # no SMTC: the Source dropdown just offers Auto
    nl.Overlay._clickthrough = lambda self: None    # ctypes.windll only exists on Windows
    nl.screen_work = lambda: None                   # no SystemParametersInfoW outside Windows

app = nl.App()
root = app.root
root.update_idletasks()
ov = app.overlay


def settle(limit=2.0):
    """Pump the event loop until the transition animation finishes."""
    end = time.monotonic() + limit
    while time.monotonic() < end:
        root.update()
        if ov._anim_job is None:
            return
        time.sleep(0.01)
    raise AssertionError("animation never finished")


def shown():
    """Lines actually inside the window, top to bottom, with their y offset."""
    wh = ov.row_h() * ov.cfg["lines"]
    out = []
    for lbl in ov.lines:
        if not lbl.winfo_ismapped():
            continue
        y = lbl.winfo_y()
        if 0 <= y < wh:
            out.append((lbl.cget("text"), str(lbl.cget("font")), lbl.cget("fg"), y))
    return sorted(out, key=lambda r: r[3])


def clipped():
    """Rows shorter than the font's own ink box -> Tk chops the descenders."""
    bad = []
    for lbl in ov.lines:
        if not lbl.winfo_ismapped():
            continue
        f = tkfont.Font(font=lbl.cget("font"))
        need = f.metrics("ascent") + f.metrics("descent")
        if lbl.winfo_height() < need:
            bad.append((lbl.cget("text"), need, lbl.winfo_height()))
    return bad


assert ov.win.geometry().startswith("720x34"), ov.win.geometry()

# --- layout: N lines * height ---
app.vars["width"].set("1000")
app.vars["height"].set("30")
app.vars["lines"].set("3")
root.update()
row = ov.row_h()
assert ov.win.geometry().startswith(f"1000x{row * 3}+460+0"), ov.win.geometry()
assert len(ov.lines) == 4, len(ov.lines)      # 3 visible + 1 spare for the slide

# half-typed garbage is ignored, not a crash, and doesn't block other fields
app.vars["width"].set("abc")
app.vars["font_size"].set("28")
root.update()
assert ov.win.geometry().startswith(f"1000x{ov.row_h() * 3}"), ov.win.geometry()

# the row grows with the font: 28pt bold needs ~52px, so 30 is not honoured
row = ov.row_h()
assert row >= 50, row

# --- a new song snaps; the active line is big+white, neighbours small+dim ---
app.state["lines"], app.state["idx"] = ["one", "two", "three", "four", "five", "six"], 3
app.tick_once()
settle()
rows = shown()
print("render @3:", rows)
assert [r[0] for r in rows] == ["three", "four", "five"], rows
active = rows[1]
assert active[0] == "four" and active[2] == nl.DEFAULTS["fg"], active
assert "28" in active[1] and "bold" in active[1], active
for t, font, fg, y in (rows[0], rows[2]):
    assert fg == nl.DEFAULTS["dim"], fg
    # emphasis is colour only: one font for every row, so nothing re-rasterises
    # mid-glide (that was the stiffness: 5 sizes + a fixed width jump of 84px)
    assert font == active[1], (font, active[1])
# each line sits one row height below the previous
assert [r[3] for r in rows] == [0, row, 2 * row], [r[3] for r in rows]

# nothing is cut off, whatever the font/height combo the user picked
assert clipped() == [], clipped()

# --- transition: advancing one line animates instead of snapping ---
calls = []
real_render = ov.render


def spy(pos=None):
    calls.append(pos)
    return real_render(pos)


ov.render = spy
t0 = time.monotonic()
app.state["idx"] = 4
app.tick_once()
while ov._anim_job is not None:
    root.update()
    time.sleep(0.002)
elapsed = time.monotonic() - t0
ov.render = real_render
mid = [c for c in calls if c is not None and 3.0 < c < 4.0]
print(f"glide: {elapsed * 1000:.0f}ms, {len(calls)} frames, {len(set(mid))} mid positions")
assert 0.1 < elapsed < 0.8, f"transition took {elapsed * 1000:.0f}ms"
assert len(set(mid)) >= 5, f"only {len(set(mid))} intermediate positions - that's a jump"
assert ov.pos == 4.0, ov.pos
rows = shown()
print("after glide:", rows)
assert [r[0] for r in rows] == ["four", "five", "six"], rows
assert rows[1][2] == nl.DEFAULTS["fg"] and "28" in rows[1][1], rows[1]

# the glide is a real interpolation: at pos 3.5 both lines are half-lit
ov.render(3.5)
root.update()
blend = shown()
assert len(blend) == 3, blend


for t, font, fg, y in blend:
    if t in ("four", "five"):
        assert fg not in (nl.DEFAULTS["fg"], nl.DEFAULTS["dim"]), (t, fg)
        assert font == blend[0][1], (t, font)   # colour carries the blend, not size

# --- clamping at the ends ---
app.state["lines"], app.state["idx"] = ["one", "two", "three", "four"], 0
app.tick_once()
settle()
assert [r[0] for r in shown()] == ["one", "two", "three"]
app.state["idx"] = 3
app.tick_once()
settle()
assert [r[0] for r in shown()] == ["two", "three", "four"]

# no lyrics -> nothing drawn, no crash
app.state["lines"], app.state["idx"] = [], None
app.tick_once()
settle()
assert shown() == [], shown()

# --- presets ---
app.preset(nl.PRESET_SPOTIFY)
root.update()
print("spotify preset:", ov.win.geometry(), app.vars["lines"].get(), app.transparent.get())
assert ov.win.geometry().startswith("900x200+510+0"), ov.win.geometry()   # 40 * 5
assert len(ov.lines) == 6      # 5 visible + 1 spare
assert app.transparent.get() is True
assert app.vars["font_size"].get() == "20"
assert float(ov.win.attributes("-alpha")) == 1.0
assert app.swatches["fg"].cget("bg") == "#ffffff", app.swatches["fg"].cget("bg")
assert clipped() == [], clipped()          # 20pt bold needs 37px, preset row is 40

app.preset(nl.PRESET_PILL)
root.update()
print("pill preset:", ov.win.geometry(), app.vars["lines"].get(), app.transparent.get())
assert ov.win.geometry().startswith("720x34+600+0"), ov.win.geometry()
assert len(ov.lines) == 2      # 1 visible + 1 spare
assert app.transparent.get() is False
assert app.vars["font_size"].get() == "15"
assert clipped() == [], clipped()

# a deliberately too-short height is raised to the font's ink box, never clipped
app.vars["height"].set("18")
app.vars["font_size"].set("20")
root.update()
assert ov.row_h() == 39, ov.row_h()
assert clipped() == [], clipped()

# --- lyric fetch: pick the version whose duration matches the track ---
# lrclib returns every cover/edit of a title; grabbing the first one is what
# made lyrics drift ~30s. Feed it a fake response and check the pick.
import io
import json as _json
import urllib.request as _urlreq


def fake_hits(payload):
    """Make nl.fetch_lyrics see `payload` instead of hitting the network."""
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = _urlreq.urlopen
    _urlreq.urlopen = lambda *a, **k: R(_json.dumps(payload).encode())
    return lambda: setattr(_urlreq, "urlopen", orig)


payload = [
    {"duration": 177.0, "syncedLyrics": "[00:01.00]WRONG short\n"},
    {"duration": 209.0, "syncedLyrics": "[00:02.00]WRONG long\n"},
    {"duration": 208.0, "syncedLyrics": "[00:30.12]right line\n[00:35.00]next\n"},
    {"duration": 208.5, "syncedLyrics": "[00:30.12]right line\n[00:35.00]next\n"},
    {"duration": 208.0, "syncedLyrics": None},      # unsynced: must be skipped
]
restore = fake_hits(payload)
try:
    got = nl.fetch_lyrics("The Night We Met (feat. Phoebe Bridgers)",
                          "Lord Huron, Phoebe Bridgers", 208.2)
finally:
    restore()
print("duration pick:", got)
assert got and got[0] == (30.12, "right line"), got
assert len(got) == 2, got

# a title with no matching duration must STILL show something (a few seconds off
# beats a blank overlay) - this was a regression: the filter rejected everything
restore = fake_hits([{"duration": 120.0, "syncedLyrics": "[00:01.00]off by a lot\n"}])
try:
    got = nl.fetch_lyrics("Song", "Artist", 300.0)
    assert got == [(1.0, "off by a lot")], got
finally:
    restore()

# a hit with duration=None must not outrank one with a real matching duration
restore = fake_hits([
    {"duration": None, "syncedLyrics": "[00:09.00]no duration\n"},
    {"duration": 208.0, "syncedLyrics": "[00:30.12]correct\n"},
])
try:
    got = nl.fetch_lyrics("Song", "Artist", 208.2)
    assert got == [(30.12, "correct")], got
finally:
    restore()

# no duration known -> best effort, still returns lyrics (first synced hit wins)
restore = fake_hits(payload)
try:
    assert nl.fetch_lyrics("Song", "Artist"), "no lyrics at all without a duration"
finally:
    restore()

# bracket/feat noise is stripped, because that's what made the search miss
assert nl.clean_name("The Night We Met (feat. Phoebe Bridgers)") == "The Night We Met"
assert nl.clean_name("Song [Remastered]") == "Song"
assert nl.clean_name("Song (Live) {Deluxe}") == "Song"
assert nl.clean_name("Song feat. Someone") == "Song"
assert nl.clean_name("Plain") == "Plain"
assert nl.clean_name(None) == ""

# --- a failed fetch is retried, not cached as "this song has no lyrics" ---
calls = []
real_fetch = nl.fetch_lyrics
nl.fetch_lyrics = lambda t, a, l=None: calls.append((t, a, l)) or []

state = {"lines": [], "idx": None}
stop = threading.Event()


def probe_ok(source="Auto"):
    return {"title": "T", "artist": "A", "pos": 5.0, "len": 200.0, "playing": True}


real_probe, nl._probe = nl._probe, probe_ok
real_retry, nl.RETRY_S = nl.RETRY_S, 0.0     # retry immediately instead of 30s
th = threading.Thread(target=real_poll, args=(state, stop), daemon=True)
th.start()
time.sleep(0.9)
stop.set()
th.join(timeout=2)
nl._probe, nl.fetch_lyrics, nl.RETRY_S = real_probe, real_fetch, real_retry
print(f"retry while title-only: {len(calls)} fetch attempts")
assert len(calls) >= 2, f"only {len(calls)} attempt(s) - a 503 would stick forever"

# --- song change: the old track's lyrics must not leak into the new one ---
nl.fetch_lyrics = real_fetch          # restore for the next block
songs = {
    "A": {"title": "A", "artist": "x", "len": 200.0, "pos": 150.0,
          "lyrics": [(t, f"a{t}") for t in range(0, 190, 10)]},
    "B": {"title": "B", "artist": "y", "len": 180.0, "pos": 5.0,
          "lyrics": [(60.0, "b first"), (70.0, "b second")]},
}
cur = {"s": "A"}


def probe_switch(source="Auto"):
    s = songs[cur["s"]]
    return {"title": s["title"], "artist": s["artist"], "pos": s["pos"],
            "len": s["len"], "playing": True}


def fetch_switch(title, artist, track_len=None):
    for s in songs.values():
        if s["title"] == title:
            return list(s["lyrics"])
    return []


nl._probe, nl.fetch_lyrics = probe_switch, fetch_switch
st2 = {"lines": [], "idx": None}
stop2 = threading.Event()
th2 = threading.Thread(target=real_poll, args=(st2, stop2), daemon=True)
th2.start()

time.sleep(0.7)
assert len(st2["lines"]) == 19 and st2["idx"] == 15, st2

cur["s"] = "B"
time.sleep(0.7)
# song B's first lyric is at 60s and pos is 5s: lyrics are loaded, so show them
# from the top - NOT the old song's lines, and NOT a bare title
assert st2["lines"] == ["b first", "b second"], st2["lines"]
assert st2["idx"] == 0, st2["idx"]

songs["B"]["pos"] = 65.0
time.sleep(0.7)
assert st2["idx"] == 0, st2          # first lyric still current at 65s

# --- new song with no lyrics at all: title fallback, old lines gone ---
songs["C"] = {"title": "C", "artist": "z", "len": 200.0, "pos": 10.0, "lyrics": []}
cur["s"] = "C"
time.sleep(0.7)
assert len(st2["lines"]) == 1 and "\u266a" in st2["lines"][0], st2["lines"]

stop2.set()
th2.join(timeout=2)
nl._probe, nl.fetch_lyrics = real_probe, real_fetch
print("song change: ok")

# --- toggle ---
app.toggle()
root.update()
assert ov.win.state() == "withdrawn"
assert app.toggle_btn.cget("text") == "Show lyrics"
app.toggle()
root.update()
assert ov.win.state() == "normal"

# --- what the UI saved must come back on the next launch ---
app.vars["width"].set("1111")
app.vars["lines"].set("4")
app.vars["source"].set("Spotify")
app.apply()
app.save()
assert nl.CONFIG.exists(), "save wrote nothing"
back = nl.load_cfg()
assert back["width"] == 1111 and back["lines"] == 4, back
assert back["source"] == "Spotify", back

# --- Source dropdown is filled from live SMTC sessions ---
fake = [{"app": "Spotify.exe"}, {"app": "Brave"}]
real_sessions = nl.sessions
nl.sessions = lambda: fake
before = list(app.source_box.cget("values"))
app.source_box.tk.call(app.source_box.cget("postcommand"))   # what opening the list does
after = list(app.source_box.cget("values"))
nl.sessions = real_sessions
assert "Brave" in after and "Spotify" in after, after
print(f"source dropdown: {before} -> {after}")

# --- card preset: rounded container + album art --------------------------------
app.preset(nl.PRESET_CARD)
root.update()
print("card preset:", ov.win.geometry(), "items:", len(ov.canvas.find_all()),
      "art:", ov.art is not None)

# the card really is painted, and the geometry matches the preset
items = ov.canvas.find_all()
assert items, "nothing painted on the card canvas"
assert ov.win.geometry().startswith("380x52+770+0"), ov.win.geometry()   # 26 * 2
assert app.vars["radius"].get() == "14", app.vars["radius"].get()

# labels must NOT be painted in the colour key, or the card gets a hole punched
# through it; they must be inset by the radius so corners stay rounded
key = nl.MAGIC
for lbl in ov.lines:
    assert lbl.cget("bg") != key, lbl.cget("bg")
assert ov.box.cget("bg") == key, ov.box.cget("bg")
app.state["lines"], app.state["idx"] = ["one", "two", "three"], 1
app.tick_once()
settle()
x_positions = {lbl.winfo_x() for lbl in ov.lines if lbl.winfo_ismapped()}
assert x_positions and min(x_positions) >= 14, x_positions

# a real image: 1x1 GIF (GIF87a with the full 11-byte raster block; Tk rejects
# the short "1x1 hack" variant with "image format error"), the smallest thing
# Tk is guaranteed to decode
tiny = base64.b64decode("R0lGODdhAQABAIEAAP///wAAAAAAAAAAACwAAAAAAQABAAAIBAABBAQAOw==")
ov.set_art(tiny)
root.update()
assert ov.art is not None, "art was not drawn"
assert ov.art.winfo_x() >= 0 and ov.art.winfo_width() > 0
assert ov._art_png == tiny

# garbage art must not break the overlay
ov.set_art(b"not a png")
root.update()
assert ov.art is None


# a new track's cover must be re-fitted to the card on EVERY change. The size
# cache used to be keyed on art_size alone, so after one set_art() the next
# track reused the old decode; when that decode had failed the cover vanished,
# and when it had not the cover kept an old (too big) size -> drawn clipped.
def fakepng(w, h, rgb):
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + \
            struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + \
        chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")



app.preset(nl.PRESET_CARD)                  # the notch: 34px art in a 52px card
app.state["art"] = b""
app.tick_once()                             # nothing showing yet
root.update()
card = ov.row_h() * ov.cfg["lines"]
for n, px in enumerate((600, 640, 1000, 300, 600)):
    app.state["art"] = fakepng(px, px, (40 * n, 90, 160))
    app.state["lines"], app.state["idx"] = [f"track{n} a", f"track{n} b"], 0
    app.tick_once()
    root.update()
    assert ov.art is not None, f"track {n}: cover not drawn"
    assert ov._art_img.width() <= ov.cfg["art_size"], (px, ov._art_img.width())
    assert ov.art.winfo_height() <= card - 2, (px, ov.art.winfo_height(), card)
    assert ov.art.winfo_y() >= 0 and ov.art.winfo_y() + ov.art.winfo_height() <= card
    assert ov._x0 > 10 + ov._art_img.width(), (ov._x0, ov._art_img.width())  # lyrics clear the cover
app.state["art"] = b""
app.tick_once()
root.update()
assert ov.art is None, "cover stayed after the track lost its art"
app.state["art"] = fakepng(600, 600, (1, 2, 3))
app.tick_once()
root.update()
assert ov.art is not None, "cover did not come back after being empty"

# turning art off removes it and gives the text the full width back
app.art_var.set(False)
app.apply()
root.update()
assert ov.art is None
app.art_var.set(True)
app.apply()
root.update()

# card off -> no polygon, labels back on the colour key
app.card_var.set(False)
app.apply()
root.update()
assert "polygon" not in [ov.canvas.type(i) for i in ov.canvas.find_all()]
assert ov.lines[0].cget("bg") == key, ov.lines[0].cget("bg")

# and the settings round-trip through the file
app.preset(nl.PRESET_CARD)
app.save()
back = nl.load_cfg()
assert back["card"] is True and back["art"] is True and back["radius"] == 14, back
assert back["viz"] is True and back["viz_bars"] == 4 and back["align"] == "bottom", back
assert back["notch"] is True, back

# the notch shape: the top edge is flush with the screen, so it must be dead
# straight and the bottom corners rounded. A smooth polygon would bow the top,
# so the polygon must be built from explicit points.
polys = [i for i in ov.canvas.find_all() if ov.canvas.type(i) == "polygon"]
assert len(polys) == 1, polys
c = ov.canvas.coords(polys[0])
assert ov.canvas.itemcget(polys[0], "smooth") in ("0", "false", ""), \
    ov.canvas.itemcget(polys[0], "smooth")             # no spline bow
# top-center anchor: both top corners are on the screen edge, so both are
# square and must sit exactly at y=0 at x=0 and x=width.
top = [(c[i], c[i + 1]) for i in range(0, len(c), 2) if abs(c[i + 1]) < 1e-6]
assert (0.0, 0.0) in [(round(x, 6), round(y, 6)) for x, y in top], top
assert (380.0, 0.0) in [(round(x, 6), round(y, 6)) for x, y in top], top
# every point stays inside the box, and the bottom corners are pulled in
assert min(c[0::2]) >= 0 and max(c[0::2]) <= 380
assert min(c[1::2]) >= 0 and max(c[1::2]) <= 52
bottom = [y for y in c[1::2] if y > 52 - 14 - 1]
assert bottom, "no bottom-corner points: the bottom is not rounded"

# --- waveform bars -------------------------------------------------------------
# own canvas inside the text frame, one rounded line per bar
assert ov.viz is not None, "viz canvas missing"
bars = ov.viz.find_all()
assert len(bars) == 4, len(bars)
for i in bars:
    assert ov.viz.itemcget(i, "capstyle") == "round", ov.viz.itemcget(i, "capstyle")
    assert ov.viz.itemcget(i, "fill") == "#0095f5", ov.viz.itemcget(i, "fill")
xs = [ov.viz.coords(i)[0] for i in bars]
assert xs == sorted(xs, reverse=True), xs            # laid out right to left
# the lyric rows must stop before the bars start (viz x is local to its canvas)
root.update()                       # place geometry is applied on idle
viz_left = ov.viz.winfo_x()
assert viz_left > 0, viz_left
assert ov._x0 + ov._w <= viz_left + min(xs) - 6, (ov._x0, ov._w, viz_left, min(xs))

# bars move: sample the drawn height twice, it must differ
h0 = ov.viz.coords(bars[0])[3] - ov.viz.coords(bars[0])[1]
time.sleep(0.12)
ov._viz_tick()
h1 = ov.viz.coords(bars[0])[3] - ov.viz.coords(bars[0])[1]
assert h0 != h1, (h0, h1)
assert max(ov.viz.coords(i)[3] - ov.viz.coords(i)[1] for i in bars) > 8

# turning the bars off frees the gutter and removes the canvas
app.viz_var.set(False)
app.apply()
root.update()
assert ov.viz is None, "viz canvas was not destroyed"
assert ov._viz_gutter == 0
app.viz_var.set(True)
app.apply()
root.update()

# The active row is driven through app.state, not ov.texts: root.update() runs
# the app's own tick loop, which would overwrite ov.texts mid-assert with
# whatever the previous block left in state and make these checks pass by luck.
def draw(lines, idx):
    app.state["lines"], app.state["idx"] = lines, idx
    app.tick_once()
    root.update()
    return {lbl.cget("text"): lbl for lbl in ov.lines if lbl.winfo_ismapped()}


TEXT = ["a", "b", "c", "d", "e", "f"]

# align=bottom puts the playhead on the last visible row
app.preset(dict(nl.PRESET_CARD, lines=3, align="bottom"))
by_text = draw(TEXT, 2)
assert set(by_text) >= {"a", "b", "c"}, by_text.keys()
assert by_text["c"].winfo_y() == 2 * ov.row_h(), by_text["c"].winfo_y()
assert by_text["a"].winfo_y() == 0, by_text["a"].winfo_y()

# align=top puts it on the first visible row: row 2 sits at y=0 and the
# following lines fill downward from there. Six lines so the scroll clamp at
# the end of the list does not mask the difference.
app.preset(dict(nl.PRESET_CARD, lines=3, align="top"))
by_text = draw(TEXT, 2)
assert set(by_text) >= {"c", "d", "e"}, by_text.keys()
assert by_text["c"].winfo_y() == 0, by_text["c"].winfo_y()
assert by_text["d"].winfo_y() == ov.row_h(), by_text["d"].winfo_y()
# and the playhead is the brightest row
assert by_text["c"].cget("fg") == nl.PRESET_CARD["fg"], by_text["c"].cget("fg")

# align=center keeps it in the middle row of three
app.preset(dict(nl.PRESET_CARD, lines=3, align="center"))
by_text = draw(TEXT, 2)
assert by_text["c"].winfo_y() == ov.row_h(), by_text["c"].winfo_y()

# the option the user asked for must survive sanitize and appear in the UI
assert nl.sanitize({"align": "top"})["align"] == "top"
assert "top" in app.align_box.cget("values"), app.align_box.cget("values")

# --- position anchors ----------------------------------------------------------
# Every anchor must place the window on the matching screen edge/corner, and
# the shape must stay square on the edges that touch the screen: a bar pinned
# to a corner is flush on two sides, so it keeps one rounded corner at most.
sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
app.preset(nl.PRESET_CARD)                   # known 2-line card: 380x52
root.update()
W = nl.PRESET_CARD["width"]
H = ov.row_h() * nl.PRESET_CARD["lines"]
# Anchors resolve against the work area, so the expectation must come from the
# same function; otherwise this test would happily accept a bar under the
# taskbar. A fake 48px taskbar makes the difference visible even on a machine
# whose taskbar is hidden.
real_work = nl.screen_work
nl.screen_work = lambda: (0, 0, sw, sh - 48)
for anchor in nl.ANCHORS:
    app.vars["anchor"].set(anchor)
    app.apply()
    root.update()
    g = ov.win.geometry()                       # WxH+X+Y
    size, x, y = g.split("+")[0], int(g.split("+")[1]), int(g.split("+")[2])
    assert size == "%dx%d" % (W, H), (anchor, g)
    want_x, want_y = nl.anchor_xy(anchor, sw, sh, W, H, nl.screen_work())
    assert (x, y) == (want_x, want_y), (anchor, g, want_x, want_y)
    # the bottom edge must never reach under the taskbar
    vertical, _, horiz = anchor.partition("-")
    if vertical == "bottom":
        assert y + H == sh - 48, (anchor, y + H, sh - 48)
    else:
        assert y == 0, (anchor, y)

    # shape: corners on a flush edge must be square
    p = [i for i in ov.canvas.find_all() if ov.canvas.type(i) == "polygon"][0]
    c = ov.canvas.coords(p)
    pts = {(round(c[i], 3), round(c[i + 1], 3)) for i in range(0, len(c), 2)}
    vertical, _, horiz = anchor.partition("-")
    if horiz in ("center", "left"):
        assert (0.0, 0.0 if vertical == "top" else float(H)) in pts or \
               (0.0, 0.0) in pts, (anchor, sorted(pts)[:4])
    if vertical == "top":
        assert (0.0, 0.0) in pts and (float(W), 0.0) in pts, (anchor, pts)
    if vertical == "bottom":
        assert (0.0, float(H)) in pts and (float(W), float(H)) in pts, (anchor, pts)
    if horiz == "left":
        assert (0.0, 0.0) in pts or (0.0, float(H)) in pts, (anchor, pts)
    if horiz == "right":
        assert (float(W), 0.0) in pts or (float(W), float(H)) in pts, (anchor, pts)
    # and it never leaves its own box
    assert min(c[0::2]) >= 0 and max(c[0::2]) <= W, (anchor, min(c[0::2]), max(c[0::2]))
    assert min(c[1::2]) >= 0 and max(c[1::2]) <= H, (anchor, min(c[1::2]), max(c[1::2]))

# corner anchors must actually round fewer corners than a centre anchor
def rounded_count(anchor):
    app.vars["anchor"].set(anchor)
    app.apply()
    root.update()
    p = [i for i in ov.canvas.find_all() if ov.canvas.type(i) == "polygon"][0]
    c = ov.canvas.coords(p)
    return len({(round(c[i], 3), round(c[i + 1], 3)) for i in range(0, len(c), 2)})
assert rounded_count("top-center") > rounded_count("top-left"), "corner not squarer"
assert nl.notch_corners("top-center") == "brbl", nl.notch_corners("top-center")
assert nl.notch_corners("top-left") == "br", nl.notch_corners("top-left")
assert nl.notch_corners("bottom-right") == "tl", nl.notch_corners("bottom-right")
# unknown anchor must not crash; it falls back
assert nl.sanitize({"anchor": "nonsense"})["anchor"] == nl.DEFAULTS["anchor"]
nl.screen_work = real_work

app.stop.set()
root.destroy()
nl.CONFIG.unlink(missing_ok=True)
print("ui ok")