"""UI check: build the real launcher (mainloop stubbed), verify every control
reaches the overlay, that presets restyle it, that the transition actually
animates, and that a config written by the UI is read back.

    .venv\\Scripts\\python.exe test_ui.py
"""
import importlib.util
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("nl", HERE / "NotchLyrics.pyw")
nl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nl)

# isolate config so a real config.json is never touched
nl.CONFIG = Path(tempfile.gettempdir()) / "nl_ui_test_cfg.json"
nl.CONFIG.unlink(missing_ok=True)

tk.Tk.mainloop = lambda self, n=0: None      # no blocking loop
tk.Toplevel.mainloop = lambda self, n=0: None

# kill the real poll thread: it hits SMTC + lrclib and would race our fake state
real_poll = nl.poll
nl.poll = lambda state, stop: None

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
    assert "normal" in font, font
    assert "22" in font, font          # 28 * 0.78, rounded
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


def px(font):
    """'{Segoe UI} 24 normal' -> 24"""
    return int(font.split("} ")[1].split()[0])


for t, font, fg, y in blend:
    if t in ("four", "five"):
        assert fg not in (nl.DEFAULTS["fg"], nl.DEFAULTS["dim"]), (t, fg)
        assert 22 < px(font) < 28, (t, font)

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


def probe_ok():
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


def probe_switch():
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
app.apply()
app.save()
assert nl.CONFIG.exists(), "save wrote nothing"
back = nl.load_cfg()
assert back["width"] == 1111 and back["lines"] == 4, back

app.stop.set()
root.destroy()
nl.CONFIG.unlink(missing_ok=True)
print("ui ok")