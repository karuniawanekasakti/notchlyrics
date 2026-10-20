"""NotchLyrics - synchronized song lyrics pinned to the top-center of the screen.

Track metadata comes from the Windows SMTC media-session API, so it works with
Brave, Chrome, Edge, YouTube and the Spotify desktop client. Lyrics come from
lrclib.net (free, no API key).

Run:   double-click NotchLyrics.pyw
Test:  .venv\\Scripts\\python.exe NotchLyrics.pyw --test
"""
import asyncio
import base64
import ctypes
import json
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import colorchooser, font as tkfont, ttk

CONFIG = Path(__file__).with_name("config.json")
MAGIC = "#010203"       # key colour Windows drops when -transparentcolor is on
RETRY_S = 30            # re-ask lrclib after a failed fetch, don't cache the miss


def bootstrap():
    """Double-clicking the .pyw picks whatever python owns the extension, which
    is often a version without winsdk. Hop into the local venv if it exists."""
    pyw = Path(__file__).with_name(".venv") / "Scripts" / "pythonw.exe"
    if not pyw.exists() or "--test" in sys.argv:
        return
    try:
        if Path(sys.executable).parent.resolve() == pyw.parent.resolve():
            return          # already the venv's interpreter (python.exe or pythonw.exe)
    except OSError:
        pass
    script = Path(__file__).resolve()
    args = sys.argv[1:]
    if args and Path(args[0]).resolve() == script:
        args = args[1:]     # relaunched with the script path already in argv
    subprocess.Popen([str(pyw), str(script), *args])
    sys.exit(0)


DEFAULTS = {
    "width": 720,
    "height": 34,
    "top": 0,
    "x_off": 0,
    "font_family": "Segoe UI",
    "font_size": 15,
    "bold": True,
    "fg": "#ffffff",
    "dim": "#b3b3b3",
    "bg": "#000000",
    "alpha": 0.86,
    "lines": 1,
    "transparent": False,
    "clickthrough": True,
    "source": "Auto",
    "card": False,
    "radius": 20,
    "card_edge": "#2c2c2c",
    "art": False,
    "art_size": 56,
}

# The Source dropdown lists whatever SMTC currently publishes, so it is built at
# runtime by app_names(). It stays editable: typing any fragment of an app id
# works too, even for a player that is not running yet.

# Spotify's lyrics panel: several lines, current one white + bold + bigger,
# neighbours muted grey, no background box at all.
PRESET_SPOTIFY = {
    "width": 900,
    "height": 40,          # >= Segoe UI 20pt bold linespace+2 (39), else descenders clip
    "font_family": "Segoe UI",
    "font_size": 20,
    "bold": True,
    "fg": "#ffffff",
    "dim": "#b3b3b3",
    "alpha": 1.0,
    "lines": 5,
    "transparent": True,
    "card": False,
    "art": False,
}

# the "notch card": rounded container, album art, two lyric rows, one font size
PRESET_CARD = {
    "width": 900,
    "height": 40,
    "font_family": "Segoe UI",
    "font_size": 19,
    "bold": True,
    "fg": "#ffffff",
    "dim": "#9a9a9a",
    "bg": "#101010",
    "card_edge": "#2c2c2c",
    "card": True,
    "radius": 22,
    "art": True,
    "art_size": 56,
    "alpha": 1.0,
    "lines": 2,
    "transparent": True,
}

# the original one-line black pill
PRESET_PILL = {
    "width": 720,
    "height": 34,
    "font_family": "Segoe UI",
    "font_size": 15,
    "bold": True,
    "fg": "#ffffff",
    "dim": "#b3b3b3",
    "bg": "#000000",
    "alpha": 0.86,
    "lines": 1,
    "transparent": False,
}


# ---------- lyrics ----------
LRC_STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(raw):
    """[mm:ss.xx]text -> [(seconds, text)] sorted."""
    out = []
    for row in raw.splitlines():
        row = row.strip()
        while row.startswith("[") and not LRC_STAMP.match(row):
            close = row.find("]")
            if close < 0:
                break
            row = row[close + 1:].strip()
        stamps, end = [], 0
        while (m := LRC_STAMP.match(row, end)):
            stamps.append(m.groups())
            end = m.end()
        text = row[end:].strip()
        if not text:
            continue
        for mm, ss in stamps:
            out.append((int(mm) * 60 + float(ss), text))
    out.sort()
    return out


def clean_name(s):
    """'The Night We Met (feat. Phoebe Bridgers) [Remastered]' -> 'The Night We Met'.
    Bracket noise is what makes lrclib's search miss."""
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s or "")
    s = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -–—")


def fetch_lyrics(title, artist, track_len=None):
    """Best synced lyrics for this track, picked by DURATION.

    lrclib returns every cover/live/edit of the same title (177s, 183s, 208s,
    209s ...). Taking the first hit with syncedLyrics is how lyrics end up 30s
    off. Duration is the only reliable discriminator, so ask for it.
    """
    title, artist = clean_name(title), clean_name(artist)
    q = urllib.parse.urlencode({"q": f"{artist} {title}".strip()})
    req = urllib.request.Request(
        "https://lrclib.net/api/search?" + q,
        headers={"User-Agent": "notchlyrics/1.0 (personal)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            hits = json.load(r)
    except Exception:
        return []
    best = None
    fallback = None
    for h in hits:
        if not h.get("syncedLyrics"):
            continue
        parsed = parse_lrc(h["syncedLyrics"])
        if not parsed:
            continue
        d = h.get("duration")
        # duration can be null; treating that as "perfect match" made a
        # duration-less hit beat the real one. Rank it last instead.
        off = abs(d - track_len) if (d and track_len) else float("inf")
        if fallback is None or off < fallback[0]:
            fallback = (off, parsed)
        if best is None or off < best[0]:
            best = (off, parsed)
    if not best:
        return []
    # Prefer a version whose length matches the track. If none does, still show
    # the closest one — an off-by-a-few-seconds lyric beats a blank overlay.
    # ponytail: 5s is a guess; widen if a legit remaster keeps losing to a cover.
    if best[0] <= 5 or not track_len:
        return best[1]
    return fallback[1]


# ---------- now playing (SMTC) ----------

def is_song(title, artist, length):
    """Does this SMTC session look like a song rather than a video post?

    A Brave tab playing an Instagram reel/live publishes the post's CAPTION as
    the session title — often with no artist — so `artist` and `length` prove
    nothing (songs arrive artist-less; reels run long). The title's SHAPE does:
    a caption is prose (400+ chars, newlines, #hashtags, emoji); a song/video
    title is short (<~240; all seven measured cases are <120) and plain.
    """
    t = (title or "").strip()
    if not t:
        return False
    # Emoji blocks only. A blanket `ord(c) > 0x2100` would also reject CJK and
    # Hangul titles, which are perfectly ordinary songs.
    emoji = any(
        0x1F300 <= ord(c) <= 0x1FAFF      # pictographs, emoticons, symbols
        or 0x2600 <= ord(c) <= 0x27BF     # misc symbols + dingbats
        for c in t
    )
    return len(t) <= 240 and "\n" not in t and "#" not in t and not emoji


def pick_session(items, source="Auto"):
    """Choose which SMTC session the overlay follows.

    `items` is one dict per session: app, title, artist, len, pos, playing.
    Preference order: a playing session beats a paused one, and the user's
    chosen source beats everything else. Without this a paused tab left open in
    the tab strip could win over the song actually playing.
    """
    want = "" if (source or "").strip().lower() in ("", "auto") else source.strip().lower()
    if want:
        items = [i for i in items if want in i["app"].lower()]
    if not items:
        return None
    # playing first, then keep the original (SMTC) order for stability
    return sorted(items, key=lambda i: not i["playing"])[0]


def sessions():
    """Every SMTC session on the machine, song or not, newest order preserved.

    Split out so the Source dropdown can list players that are open but idle
    (a paused Spotify still publishes a session, sometimes without a title).
    """
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
    )

    async def go():
        mgr = await Manager.request_async()
        items = []
        for s in mgr.get_sessions():
            p = await s.try_get_media_properties_async()
            info = s.get_playback_info()
            tl = s.get_timeline_properties()
            pos = tl.position.total_seconds()
            if tl.last_updated_time:
                pos += (time.time() - tl.last_updated_time.timestamp())
            items.append({
                "app": s.source_app_user_model_id or "",
                "title": p.title or "",
                "artist": p.artist or "",
                "pos": max(pos, 0.0),
                "len": tl.end_time.total_seconds() if tl.end_time else 0.0,
                "playing": int(info.playback_status) == 4,
                "thumb": p.thumbnail,          # stream ref, read lazily
            })
        return items

    return asyncio.run(go())


def art_png(ref):
    """Album art as PNG bytes. Read only when the track changes: the stream is
    real I/O, so doing it on every poll tick would be wasteful."""
    if ref is None:
        return b""
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

    async def go():
        st = await ref.open_read_async()
        buf = Buffer(st.size)
        await st.read_async(buf, st.size, InputStreamOptions.NONE)
        r = DataReader.from_buffer(buf)
        out = bytearray(r.unconsumed_buffer_length)
        r.read_bytes(out)
        return bytes(out)

    try:
        return asyncio.run(go())
    except Exception:
        return b""


def app_label(app):
    """'Spotify.exe' -> 'Spotify'. The label is a substring of the session's
    app id, so pick_session's filter matches what the dropdown shows."""
    b = (app or "").rsplit("\\", 1)[-1].strip()
    if "!" in b:                 # packaged app: PackageFamily!AppId -> AppId
        b = b.split("!", 1)[1]
    return b[:-4] if b.lower().endswith(".exe") else b


def app_names():
    """Dropdown values: Auto plus every player SMTC currently knows about."""
    try:
        apps = {app_label(i["app"]) for i in sessions() if i["app"]}
    except Exception:
        apps = set()          # SMTC unavailable -> dropdown still offers Auto
    return ["Auto"] + sorted(apps, key=str.lower)


def _probe(source="Auto"):
    items = [i for i in sessions()
             if is_song(i["title"], i["artist"], i["len"])]  # a video's caption is not a song
    return pick_session(items, source)


def poll(state, stop, cfg=None):
    """Background thread: keeps state["lines"]/state["idx"] fresh."""
    last_key, lines = "", []
    fetched_at = 0.0
    while not stop.is_set():
        try:
            np = _probe((cfg or {}).get("source", "Auto"))
        except Exception:
            np = None
        if np:
            key = np["title"] + "|" + np["artist"]
            # A 503/timeout must not be cached as "this song has no lyrics" —
            # retry every RETRY_S while we're still showing the title fallback.
            stale = not lines and time.monotonic() - fetched_at > RETRY_S
            if key != last_key:
                # New track: drop the old song's lines right now. Otherwise the
                # previous track's lyrics get shown against this one's position
                # until the fetch returns (or forever, if it returns nothing).
                lines = []
            if key != last_key or stale:
                last_key = key
                fetched_at = time.monotonic()
                lines = fetch_lyrics(np["title"], np["artist"], np["len"])
                # cover is per track, so read it here and not on every tick
                state["art"] = art_png(np.get("thumb"))
            i = None
            for idx, (t, _) in enumerate(lines):
                if t <= np["pos"]:
                    i = idx
            if not np["playing"]:
                state["lines"], state["idx"] = [], None
            elif not lines:
                state["lines"] = [f"\u266a {np['title']} \u2014 {np['artist']}"]
                state["idx"] = 0
            else:
                # Before the first timestamp there's no "current" line yet, but
                # the lyrics ARE loaded — show them from the top instead of
                # falling back to the title for the whole intro.
                state["lines"], state["idx"] = [t for _, t in lines], i or 0
        else:
            last_key, lines = "", []
            state["lines"], state["idx"] = [], None
            state["art"] = b""
        stop.wait(0.3)


# ---------- config ----------

def sanitize(raw):
    """Clamp/repair a config dict so garbage in the file can't break the UI."""
    d = dict(DEFAULTS)
    d.update({k: v for k, v in raw.items() if k in DEFAULTS})

    def num(key, lo, hi):
        try:
            d[key] = max(lo, min(hi, int(float(d[key]))))
        except (TypeError, ValueError):
            d[key] = DEFAULTS[key]

    num("width", 120, 4000)
    num("height", 16, 400)
    num("top", -200, 2000)
    num("x_off", -4000, 4000)
    num("font_size", 6, 96)
    num("lines", 1, 12)
    try:
        d["alpha"] = round(max(0.2, min(1.0, float(d["alpha"]))), 2)
    except (TypeError, ValueError):
        d["alpha"] = DEFAULTS["alpha"]
    d["clickthrough"] = bool(d["clickthrough"])
    d["bold"] = bool(d["bold"])
    d["transparent"] = bool(d["transparent"])
    d["source"] = d["source"].strip() if isinstance(d["source"], str) else DEFAULTS["source"]
    if not d["source"]:
        d["source"] = DEFAULTS["source"]
    d["card"] = bool(d["card"])
    d["art"] = bool(d["art"])
    num("radius", 0, 200)
    num("art_size", 16, 300)
    d["card_edge"] = d["card_edge"] if isinstance(d["card_edge"], str) and \
        re.fullmatch(r"#[0-9a-fA-F]{6}", d["card_edge"]) else DEFAULTS["card_edge"]
    for key in ("fg", "dim", "bg"):
        if not (isinstance(d[key], str) and d[key].startswith("#") and len(d[key]) == 7):
            d[key] = DEFAULTS[key]
    return d


def load_cfg():
    try:
        return sanitize(json.loads(CONFIG.read_text("utf-8")))
    except Exception:
        return dict(DEFAULTS)


def save_cfg(cfg):
    try:
        CONFIG.write_text(json.dumps(cfg, indent=2), "utf-8")
    except Exception:
        pass


# ---------- overlay ----------

ANIM_MS = 260          # transition length, close to Spotify's lyric glide
ANIM_FRAME_MS = 16     # ~60fps; coarse steps look like a jump, not a glide


def lerp_color(c1, c2, t):
    """'#rrggbb' -> '#rrggbb' blended t of the way from c1 to c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle as a spline-smoothed polygon. Tk ships no rounded
    rectangle primitive, and smooth=True is the only way to get corners that
    are not visibly stair-stepped."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    if r <= 0:
        return cv.create_rectangle(x1, y1, x2, y2, **kw)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r,
           x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, splinesteps=16, **kw)


class Overlay:
    def __init__(self, root, cfg):
        self.cfg = cfg
        self.lines = []
        self.texts = []
        self.idx = None
        self.pos = 0.0          # float playhead: animates towards idx
        self._row = 0           # cached row pitch, set in apply()
        self._anim_job = None
        self._from = self._to = 0.0
        self._t0 = 0.0
        self._art_png = b""
        self._art_size = 0
        self._x0, self._w = 0, cfg["width"]
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        # canvas first so a rounded card can be painted behind the text labels
        self.canvas = tk.Canvas(self.win, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.box = tk.Frame(self.win)
        self.box.place(x=0, y=0, relwidth=1, relheight=1)
        self.art = None          # album-art label, only when art is enabled
        self._art_key = None     # which track the current image belongs to
        self.win.bind("<Map>", lambda e: self._clickthrough())
        self.apply(cfg)

    def row_h(self):
        """Row pitch. Never shorter than the font's own ink box — Tk clips
        descenders ('g','y','p') when a label is placed shorter than ascent+descent.
        Cached: creating a tkfont.Font per frame leaks named fonts and stalls the glide."""
        return self._row

    def apply(self, cfg):
        self.cfg = cfg
        w = cfg["width"]
        f = tkfont.Font(family=cfg["font_family"], size=cfg["font_size"],
                        weight="bold" if cfg["bold"] else "normal")
        self._row = max(cfg["height"], f.metrics("linespace") + 2)
        h = self._row * max(1, cfg["lines"])
        x = (self.win.winfo_screenwidth() - w) // 2 + cfg["x_off"]
        self.win.geometry(f"{w}x{h}+{x}+{cfg['top']}")
        self.win.attributes("-alpha", cfg["alpha"])
        self.win.configure(bg=cfg["bg"])
        self.box.configure(bg=cfg["bg"])

        # transparent = paint the background in a colour Windows is told to drop
        try:
            self.win.attributes("-transparentcolor", MAGIC if cfg["transparent"] else "")
        except tk.TclError:
            pass
        bg = MAGIC if cfg["transparent"] else cfg["bg"]
        self.win.configure(bg=bg)

        # --- rounded card, painted on the canvas behind the text -----------
        # The canvas is placed before the text widgets, so it is already beneath
        # them without any explicit stacking call.
        self.canvas.delete("all")
        self.canvas.configure(bg=bg, width=w, height=h)
        if cfg["card"]:
            # The card must fill the window edge to edge. With any inset the
            # label rects (which start at y=0) stick out past the polygon's top
            # and bottom edge and paint a square strip over the rounded shape.
            round_rect(self.canvas, 0, 0, w, h, cfg["radius"],
                       fill=cfg["bg"], outline=cfg["card_edge"], width=1)

        # Colour key is applied to the composited window, so a label painted in
        # MAGIC would punch a hole through the card down to the desktop. On a
        # card the labels must carry the card colour instead, and they are inset
        # by the radius so their rectangles never cover the rounded corners.
        # The frame itself stays transparent, otherwise its full rect would paint
        # square corners back over the rounded polygon.
        lbl_bg = cfg["bg"] if cfg["card"] else bg
        pad_x = cfg["radius"] if cfg["card"] else 0
        self.box.configure(bg=bg)

        # --- album art -----------------------------------------------------
        self._render_art(cfg)

        # Lyric rows: geometry is computed once here and only re-used by
        # render(), so the row rect (and its radius inset) can never be
        # overridden by the animation frame.
        self._x0 = pad_x
        self._w = max(20, w - 2 * pad_x)
        if cfg["art"] and self.art is not None:
            aw = self.art.winfo_width() or 0
            if aw:
                self._x0 = aw + 16
                self._w = max(20, w - self._x0 - pad_x)

        for lbl in self.lines:
            lbl.destroy()
        self.lines = []
        # one extra row than the window shows, so a sliding block never leaves a
        # blank strip at the bottom edge
        for _ in range(max(1, cfg["lines"]) + 1):
            lbl = tk.Label(self.box, text="", bg=lbl_bg, anchor="center", justify="center")
            lbl.place(x=self._x0, y=0, width=self._w, height=self.row_h())
            self.lines.append(lbl)
        self.render(self.pos)
        self._clickthrough()
        self.win.after(300, self._clickthrough)   # Tk rewrites ex-style on map

    def _render_art(self, cfg):
        """Album art as a Tk image. PhotoImage only scales by integer factors, so
        the source is halved repeatedly until it is close to art_size."""
        if not cfg["art"]:
            if self.art is not None:
                self.art.destroy()
                self.art = None
            return
        key = self._art_key
        want = cfg["art_size"]
        if self.art is not None and getattr(self, "_art_size", None) == want:
            return
        if self.art is not None:
            self.art.destroy()
            self.art = None
        if not self._art_png:
            return
        try:
            img = tk.PhotoImage(data=base64.b64encode(self._art_png).decode())
        except tk.TclError:
            return                       # not a format Tk can read (webp, etc.)
        while img.width() > want * 2:
            img = img.subsample(2, 2)
        self._art_img = img              # keep a reference or Tk collects it
        h = cfg["height"] * max(1, cfg["lines"])
        # On a card the label must be card-coloured: a MAGIC background would
        # punch a transparent rectangle around the cover, straight through the
        # card, because the colour key applies to the composited window.
        lbg = cfg["bg"] if cfg["card"] else self.box.cget("bg")
        self.art = tk.Label(self.box, image=img, bg=lbg, bd=0)
        self.art.place(x=10, y=(h - img.height()) // 2, width=img.width(),
                       height=img.height())
        self._art_size = want

    def set_art(self, png):
        """New track: swap the cover if it actually changed."""
        if png == self._art_png:
            return
        self._art_png = png
        if self.art is not None:
            self.art.destroy()
            self.art = None
        self._art_size = 0
        self.apply(self.cfg)

    def render(self, pos=None):
        """Spotify-style, continuous: `pos` is a float line index.

        Emphasis comes from colour and vertical position only, never from font
        size or weight. Tk fonts are integer-sized, so interpolating the size
        gave exactly 5 hard steps per glide and re-measured the glyphs 160 times
        (text width jumped up to 84px) -- which is what made the motion look
        stiff. One constant font means zero re-rasterising: only fill changes.
        """
        cfg = self.cfg
        if pos is not None:
            self.pos = pos
        if not self.texts:
            for lbl in self.lines:
                lbl.place_forget()
            return

        h = self.row_h()
        vis = max(1, cfg["lines"])
        pos = max(0.0, min(float(self.pos), len(self.texts) - 1))
        self.pos = pos                     # keep it clamped for the next glide
        # scroll so the playhead sits mid-block, clamped at both ends
        top = max(0.0, min(pos - (vis - 1) / 2, max(0, len(self.texts) - vis)))
        fg, dim = cfg["fg"], cfg["dim"]
        font = (cfg["font_family"], cfg["font_size"],
                "bold" if cfg["bold"] else "normal")
        x0, w = getattr(self, "_x0", 0), getattr(self, "_w", cfg["width"])
        for lbl in self.lines:
            lbl.place_forget()
        for k, lbl in enumerate(self.lines):
            i = int(top) + k
            if i >= len(self.texts):
                break
            act = max(0.0, 1.0 - min(1.0, abs(i - pos)))   # 1 = playhead, 0 = 2+ away
            lbl.configure(text=self.texts[i], font=font,
                          fg=lerp_color(dim, fg, act))
            lbl.place(x=x0, y=round((i - top) * h), width=w, height=h)

    def animate_to(self, idx):
        """Glide the playhead from where it is to `idx` instead of snapping."""
        if idx is None:
            self.pos = 0.0
            self.render()
            return
        if float(idx) == self._to and self._anim_job:
            return                         # already gliding there
        if self._anim_job:
            try:
                self.win.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None
        self._from, self._to = self.pos, float(idx)
        self._t0 = time.monotonic()

        def step():
            t = min(1.0, (time.monotonic() - self._t0) * 1000 / ANIM_MS)
            eased = 1 - (1 - t) ** 3          # ease-out, same feel as Spotify
            self.render(self._from + (self._to - self._from) * eased)
            if t < 1:
                self._anim_job = self.win.after(ANIM_FRAME_MS, step)
            else:
                self._anim_job = None

        step()

    def set_content(self, texts, idx):
        if texts != self.texts:
            self.texts = texts
            self.idx = None                # new song: snap, don't glide
            self.pos = float(idx or 0)
            self.render()
            self.idx = idx
            return
        if idx == self.idx:
            return
        self.idx = idx
        self.animate_to(idx)

    def _clickthrough(self):
        hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        want = (style | 0x20) if self.cfg["clickthrough"] else (style & ~0x20)
        if want != style:      # want is derived from this same read, so this is exact
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, want)

    def show(self, on):
        if on:
            self.win.deiconify()
            self.apply(self.cfg)
        else:
            self.win.withdraw()


# ---------- launcher UI ----------

class App:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.cfg = load_cfg()
        self.state = {"lines": [], "idx": None, "art": b""}
        self.stop = threading.Event()
        self._save_job = None

        self.root = tk.Tk()
        self.root.title("NotchLyrics")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.build_ui()
        self.overlay = Overlay(self.root, self.cfg)
        self.tick()

        # cfg is shared by reference, so changing Source takes effect on the
        # next poll tick without restarting
        threading.Thread(target=poll, args=(self.state, self.stop, self.cfg),
                         daemon=True).start()
        self.root.mainloop()

    # --- ui ---

    def build_ui(self):
        c = self.cfg
        f = ttk.Frame(self.root, padding=14)
        f.grid(sticky="nsew")
        f.columnconfigure(1, weight=1)
        self.vars = {}
        self.swatches = {}
        r = 0

        def entry(label, key, width=8):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(c[key]))
            ttk.Entry(f, textvariable=var, width=width).grid(
                row=r, column=1, columnspan=2, sticky="w", padx=(10, 0))
            var.trace_add("write", lambda *_: self.apply())
            self.vars[key] = var
            r += 1

        def color(label, key):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            sw = tk.Label(f, width=4, relief="solid", bd=1, bg=c[key])
            sw.grid(row=r, column=1, sticky="w", padx=(10, 0))

            def pick():
                got = colorchooser.askcolor(color=c[key], parent=self.root)[1]
                if got:
                    c[key] = got
                    sw.configure(bg=got)
                    self.overlay.apply(c)
                    self.save()

            sw.bind("<Button-1>", lambda e: pick())
            ttk.Button(f, text="Choose", width=6, command=pick).grid(row=r, column=2, sticky="w")
            self.swatches[key] = sw
            r += 1

        # --- style presets ---
        ttk.Label(f, text="Preset", foreground="#777").grid(row=r, column=0, sticky="w")
        pr = ttk.Frame(f)
        pr.grid(row=r, column=1, columnspan=2, sticky="w", padx=(10, 0))
        ttk.Button(pr, text="Spotify", width=9,
                   command=lambda: self.preset(PRESET_SPOTIFY)).pack(side="left")
        ttk.Button(pr, text="Pill", width=9,
                   command=lambda: self.preset(PRESET_PILL)).pack(side="left", padx=4)
        ttk.Button(pr, text="Card", width=9,
                   command=lambda: self.preset(PRESET_CARD)).pack(side="left")
        r += 1

        # --- which player to follow: filled from the live SMTC session list ---
        ttk.Label(f, text="Source").grid(row=r, column=0, sticky="w", pady=3)
        self.vars["source"] = tk.StringVar(value=c["source"])
        src = ttk.Combobox(f, textvariable=self.vars["source"], values=app_names(),
                           width=24, postcommand=lambda: src.configure(values=app_names()))
        self.source_box = src
        src.grid(row=r, column=1, columnspan=2, sticky="w", padx=(10, 0))
        self.vars["source"].trace_add("write", lambda *_: self.apply())
        r += 1

        # --- font: dropdown of installed families, still typeable ---
        ttk.Label(f, text="Font").grid(row=r, column=0, sticky="w", pady=3)
        self.vars["font_family"] = tk.StringVar(value=c["font_family"])
        fonts = sorted(set(tkfont.families(self.root)))
        ttk.Combobox(f, textvariable=self.vars["font_family"], values=fonts,
                     width=24).grid(row=r, column=1, columnspan=2, sticky="w", padx=(10, 0))
        self.vars["font_family"].trace_add("write", lambda *_: self.apply())
        r += 1

        entry("Font size", "font_size")
        entry("Visible lines", "lines")
        entry("Width (px)", "width")
        entry("Row height", "height")
        entry("Top offset", "top")
        entry("Horizontal offset", "x_off")

        self.bold = tk.BooleanVar(value=c["bold"])
        ttk.Checkbutton(f, text="Bold active line", variable=self.bold,
                        command=self.apply).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1

        color("Active line colour", "fg")
        color("Inactive line colour", "dim")
        color("Background colour", "bg")
        color("Card border colour", "card_edge")

        self.card_var = tk.BooleanVar(value=c["card"])
        ttk.Checkbutton(f, text="Rounded card container", variable=self.card_var,
                        command=self.apply).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1
        entry("Card radius", "radius")

        self.art_var = tk.BooleanVar(value=c["art"])
        ttk.Checkbutton(f, text="Show album art", variable=self.art_var,
                        command=self.apply).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1

        self.transparent = tk.BooleanVar(value=c["transparent"])
        ttk.Checkbutton(f, text="Transparent background (no box)", variable=self.transparent,
                        command=self.apply).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1

        ttk.Label(f, text="Opacity").grid(row=r, column=0, sticky="w", pady=3)
        self.alpha = tk.DoubleVar(value=c["alpha"])
        ttk.Scale(f, from_=0.2, to=1.0, variable=self.alpha, length=140,
                  command=lambda _: self.apply()).grid(row=r, column=1, sticky="w", padx=(10, 0))
        r += 1

        self.ct = tk.BooleanVar(value=c["clickthrough"])
        ttk.Checkbutton(f, text="Click-through (never blocks mouse input)", variable=self.ct,
                        command=self.apply).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1

        self.toggle_btn = ttk.Button(f, text="Hide lyrics", command=self.toggle)
        self.toggle_btn.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 0))
        r += 1

        ttk.Label(f, text="Closing this window quits. Settings save automatically.",
                  foreground="#777").grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.shown = True

    # --- actions ---

    def apply(self):
        if getattr(self, "_busy", False):
            return
        c = self.cfg
        # per-field: a half-typed number must not freeze the other controls
        for k in ("width", "height", "top", "x_off", "font_size", "lines"):
            try:
                c[k] = int(float(self.vars[k].get()))
            except (ValueError, tk.TclError):
                pass
        c["font_family"] = self.vars["font_family"].get().strip() or DEFAULTS["font_family"]
        c["source"] = self.vars["source"].get().strip() or DEFAULTS["source"]
        c["art"] = self.art_var.get()
        c["card"] = self.card_var.get()
        try:
            c["radius"] = int(float(self.vars["radius"].get()))
        except (ValueError, tk.TclError):
            pass
        c["alpha"] = round(self.alpha.get(), 2)
        c["bold"] = self.bold.get()
        c["transparent"] = self.transparent.get()
        c["clickthrough"] = self.ct.get()
        c.update(sanitize(c))
        self.overlay.apply(c)
        self.schedule_save()

    def preset(self, values):
        """Apply a style preset and push it into every widget so the UI agrees."""
        # every var.set() fires apply() via its trace, and apply() reads *all*
        # widgets - which still hold the old values and would clobber the preset
        self._busy = True
        try:
            self.cfg.update(sanitize({**self.cfg, **values}))
            c = self.cfg
            for k in ("width", "height", "top", "x_off", "font_size", "lines",
                      "font_family", "radius"):
                self.vars[k].set(str(c[k]))
            self.art_var.set(c["art"])
            self.card_var.set(c["card"])
            self.bold.set(c["bold"])
            self.alpha.set(c["alpha"])
            self.transparent.set(c["transparent"])
            self.ct.set(c["clickthrough"])
            for k, sw in self.swatches.items():
                sw.configure(bg=c[k])
        finally:
            self._busy = False
        self.overlay.apply(self.cfg)
        self.save()

    def schedule_save(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(700, self.save)

    def save(self):
        self._save_job = None
        save_cfg(self.cfg)

    def toggle(self):
        self.shown = not self.shown
        self.overlay.show(self.shown)
        self.toggle_btn.config(text="Hide lyrics" if self.shown else "Show lyrics")

    def tick(self):
        self.tick_once()
        self.root.after(150, self.tick)

    def tick_once(self):
        self.overlay.set_content(self.state["lines"], self.state["idx"])
        self.overlay.set_art(self.state.get("art", b""))

    def quit(self):
        self.stop.set()
        save_cfg(self.cfg)
        self.root.destroy()


def main():
    # single instance: double-clicking twice shouldn't stack two bars
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, "NotchLyrics.SingleInstance")
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        return
    App()


if __name__ == "__main__":
    bootstrap()
    if "--test" in sys.argv:
        l = parse_lrc("[00:12.50]dua\n[00:01.00]satu\n[junk]\n[00:02.00]\n")
        assert len(l) == 2, l
        assert l[0] == (1.0, "satu") and l[1] == (12.5, "dua"), l
        l = parse_lrc("[by:5]\n[ar:123]\n[00:12.00][01:00.00]chorus\n")
        assert l == [(12.0, "chorus"), (60.0, "chorus")], l

        # a video post publishes its caption as the session title; that is not a song
        cap = ("(9+) Menceritakan dua penjaga minimarket yang shift malamnya berubah jadi "
               "mimpi buruk. Genre: Comedy, Horror, Short Ratings: \u2b50 IMDb - 7.4/10 "
               "\U0001f345 Rotten Tomatoes #RemainCalm #NontonFilm")
        assert not is_song(cap, "", 68.2), cap
        assert not is_song("lol cute \U0001f602 #reels", "", 15.0)
        assert not is_song("", "Someone", 200.0)
        # songs survive, including ones with no artist and non-Latin titles
        assert is_song("Oceans & Engines", "", 336.0)
        assert is_song("Patience", "Take That", 202.0)
        assert is_song("\u591c\u306b\u99c6\u3051\u308b", "", 261.0)
        assert is_song("\ubc24\ud3b8\uc9c0", "", 238.0)

        # source selection: playing beats paused, filter matches the app id
        a = {"app": "Spotify.exe", "title": "A", "playing": False}
        b = {"app": "Brave", "title": "B", "playing": True}
        assert pick_session([a, b])["app"] == "Brave"          # playing wins over order
        assert pick_session([a, b], "brave")["app"] == "Brave"    # case-insensitive
        assert pick_session([a, b], "spotify")["app"] == "Spotify.exe"
        assert pick_session([a, b], "msedge") is None          # no such source -> nothing
        assert pick_session([], "Auto") is None

        # dropdown labels come from the live session list, not a hardcoded tuple
        assert app_label("Spotify.exe") == "Spotify"
        assert app_label("msedge.exe") == "msedge" and app_label("") == ""
        assert app_label(r"C:\Apps\vlc.exe") == "vlc"
        assert app_label("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") == \
            "Microsoft.ZuneMusic"
        real = sessions
        try:
            globals()["sessions"] = lambda: [
                {"app": "Spotify.exe"}, {"app": "Brave"}, {"app": "Brave"}, {"app": ""}]
            assert app_names() == ["Auto", "Brave", "Spotify"], app_names()
            globals()["sessions"] = lambda: (_ for _ in ()).throw(RuntimeError)
            assert app_names() == ["Auto"]   # SMTC dead -> launcher still usable
        finally:
            globals()["sessions"] = real

        assert sanitize({"source": "  "})["source"] == "Auto"
        assert sanitize({"source": None})["source"] == "Auto"

        # garbage config must not produce a broken overlay
        s = sanitize({"width": "99999", "height": -5, "alpha": "x", "top": "abc",
                      "font_size": 500, "bold": 0, "nonsense": 1})
        assert s["width"] == 4000 and s["height"] == 16, s
        assert s["alpha"] == DEFAULTS["alpha"] and s["top"] == DEFAULTS["top"], s
        assert s["font_size"] == 96 and s["bold"] is False, s
        assert "nonsense" not in s, s

        # presets must survive sanitize intact (they're the "looks like Spotify" promise)
        for p in (PRESET_SPOTIFY, PRESET_PILL):
            assert sanitize(p) == {**DEFAULTS, **p}, sanitize(p)
        assert PRESET_SPOTIFY["transparent"] is True and PRESET_SPOTIFY["lines"] == 5
        assert PRESET_PILL["transparent"] is False and PRESET_PILL["lines"] == 1

        # bad colours fall back instead of blowing up Tk
        bad = sanitize({"fg": "not-a-colour", "dim": 42, "bg": "#ff"})
        assert bad["fg"] == DEFAULTS["fg"] and bad["dim"] == DEFAULTS["dim"], bad
        assert bad["bg"] == DEFAULTS["bg"], bad
        assert sanitize({}) == DEFAULTS

        # config roundtrip: what the UI writes is what the next launch reads
        import tempfile
        orig, CONFIG = CONFIG, Path(tempfile.gettempdir()) / "nl_test_cfg.json"
        try:
            save_cfg({**DEFAULTS, "width": 900, "fg": "#ff0000"})
            back = load_cfg()
            assert back["width"] == 900 and back["fg"] == "#ff0000", back
            CONFIG.write_text("{not json", "utf-8")
            assert load_cfg() == DEFAULTS      # corrupt file falls back, no crash
        finally:
            CONFIG.unlink(missing_ok=True)
            CONFIG = orig
        print("ok")
    else:
        main()
