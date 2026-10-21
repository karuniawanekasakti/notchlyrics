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
import ctypes.wintypes
import json
import math
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from tkinter import colorchooser, font as tkfont, ttk

CONFIG = Path(__file__).with_name("config.json")
MAGIC = "#010203"       # key colour Windows drops when -transparentcolor is on
RETRY_S = 30            # re-ask lrclib after a failed fetch, don't cache the miss
ART_RETRY_S = 1.0       # how often the cover stream is re-read after a track change
ART_TRIES = 10          # ... up to this many reads, then keep whatever came
TAB = "tab: "           # Source value prefix for a single browser tab
_TABS = (0.0, [])       # (monotonic stamp, tab list) cache for browser_tabs()

# Where the bar sits on screen: the two centre edges and the four corners.
# A side that touches a screen edge stays square so the bar reads as a notch
# cut into that edge, not a card floating next to it.
ANCHORS = ("top-center", "bottom-center",
           "top-left", "top-right", "bottom-left", "bottom-right")


def screen_work():
    """(left, top, right, bottom) of the primary monitor minus the taskbar.

    winfo_screenheight() counts the strip the taskbar covers, so a bottom
    anchor that used it put the bar underneath the taskbar. Falls back to the
    bare screen rect when the taskbar reports nothing (hidden/auto-hide)."""
    try:
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)
        if r.right > r.left and r.bottom > r.top:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def anchor_xy(anchor, sw, sh, w, h, work=None):
    """Top-left corner for `anchor` on an sw x sh screen.

    `work` is the usable rect (left, top, right, bottom) — the screen minus the
    taskbar. All four edges are honoured, so a taskbar on any side is avoided:
    the top edge is work.top, the bottom edge is work.bottom - h.
    """
    if not work:
        work = (0, 0, sw, sh)
    left, top, right, bottom = work
    ay, _, ax = anchor.partition("-")          # 'top-center' -> top, center
    x = {"left": left, "center": left + (right - left - w) // 2,
         "right": right - w}[ax]
    y = {"top": top, "bottom": bottom - h}[ay]
    return x, y


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
    "align": "center",     # where the playhead sits: center or bottom
    "anchor": "top-center",  # where the whole bar sits on screen
    "notch": True,         # flush with the top edge: only bottom corners round
    "viz": False,          # waveform bars at the right edge
    "viz_color": "#0095f5",
    "viz_bars": 4,
    "enter_anim": True,    # slide + fade the lyrics in on a new track
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

# the notch: flush with the top screen edge (flat top, rounded bottom corners),
# album art on the left, two lyric rows, waveform bars on the right
PRESET_CARD = {
    "width": 380,
    "height": 26,
    "font_family": "Segoe UI",
    "font_size": 11,
    "bold": True,
    "fg": "#f5f5f5",
    "dim": "#a5a5a5",
    "bg": "#070707",
    "card_edge": "#1c1c1c",
    "card": True,
    "notch": True,
    "radius": 14,
    "art": True,
    "art_size": 34,
    "alpha": 1.0,
    "lines": 2,
    "align": "bottom",
    "viz": True,
    "viz_color": "#0095f5",
    "viz_bars": 4,
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


YT_NOISE = re.compile(
    r"\s*\b(official|lyrics?|lyric\s+video|audio|video|music\s+video|mv|m/v|hd|hq|"
    r"4k|8k|remaster(ed)?|visuali[sz]er|performance|live|explicit|clean|"
    r"color\s+coded|sub(?:title[ds])?)\b[\s\-–—|]*$", re.I)


def split_track(title, artist):
    """Recover (title, artist) from a YouTube-style caption.

    A browser tab publishes the VIDEO title as the track name, in whichever
    order the uploader typed it: 'Sydney Rose - We Hug Now (Official Video)'
    or 'We Hug Now – Sydney Rose'. lrclib searches on 'artist title', so feeding
    it the caption unchanged matches nothing and the overlay falls back to
    showing the caption as if it were a lyric line.

    When the known artist sits on one side of the separator, the other side is
    the title. When there is no artist to compare against, assume the YouTube
    convention 'Artist - Title'.

    Noise words are only stripped from the END of the string: 'Live Forever' is
    a real song, so a bare 'live' anywhere else must survive.
    """
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title or "")   # (Official Video)
    while True:                                           # '... Official Video'
        trimmed = YT_NOISE.sub("", t)
        if trimmed == t:
            break
        t = trimmed
    t = re.sub(r"\s+", " ", t).strip(" -–—|")
    a = (artist or "").strip()
    parts = re.split(r"\s+([-–—•|])\s+", t, maxsplit=1)
    if len(parts) == 3:
        left, sep, right = parts[0].strip(), parts[1], parts[2].strip()
        if a:
            al = a.lower()
            if right.lower() == al or al in right.lower():
                return left, a            # 'Title – Artist'
            if left.lower() == al or al in left.lower():
                return right, a           # 'Artist - Title'
        # No artist to compare against, so the separator decides the order:
        # a bullet or an en/em dash is a "title – artist" label (YouTube Music
        # and Brave tabs use it); a plain hyphen is YouTube's "artist - title".
        # ponytail: a guess, and a harmless one — lrclib ignores word order
        # (measured: both orders return the same hit), so only the SPLIT matters.
        return (left, right) if sep in "•|–—" else (right, left)
    return t, a


def fetch_lyrics(title, artist, track_len=None):
    """Best synced lyrics for this track, picked by DURATION.

    lrclib returns every cover/live/edit of the same title (177s, 183s, 208s,
    209s ...). Taking the first hit with syncedLyrics is how lyrics end up 30s
    off. Duration is the only reliable discriminator, so ask for it.
    """
    title, artist = split_track(title, artist)
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
    if want.startswith(TAB):
        return None          # tab sources are resolved by _probe, not by app id
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
    """Album art as PNG bytes. One read per track in the normal case: the
    stream is real I/O, so re-reading it on every poll tick would be wasteful.

    Read in chunks, and take the length from the buffer WinRT hands back: the
    same reference can open onto an empty stream while the player is still
    publishing the track (measured: readable ~1 s after the title changes on an
    automatic advance), and a stream may hand back fewer bytes than asked for.
    One read sized by `st.size` returned b"" and that got cached as "no cover".
    """
    if ref is None:
        return b""
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

    async def go():
        st = await ref.open_read_async()
        out = bytearray()
        while True:
            buf = Buffer(1 << 16)
            got = await st.read_async(buf, 1 << 16, InputStreamOptions.NONE)
            if not got.length:
                return bytes(out)
            r = DataReader.from_buffer(got)
            chunk = bytearray(got.length)
            r.read_bytes(chunk)
            out += chunk

    try:
        return asyncio.run(go())
    except Exception:
        return b""


def browser_tabs(max_age=0.0):
    """Every tab in every open Chromium-family window: [(browser, title)].

    SMTC publishes ONE session per browser, so a Brave window playing music and
    a Brave window showing YouTube look identical to it — there is no per-tab
    identity in the media-session API at all. The tab strip is the only place
    that information exists, so read it through UI Automation.

    Costs ~0.5s for a dozen tabs. The poll loop runs every 0.3s, so it asks for
    a cached answer (max_age>0) and only the launcher dropdown pays full price.
    """
    global _TABS
    now = time.monotonic()
    if max_age and _TABS[1] and now - _TABS[0] < max_age:
        return _TABS[1]
    try:
        import uiautomation as auto
    except ImportError:
        return []                    # optional dependency; dropdown still works
    out = []
    try:
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ClassName != "Chrome_WidgetWin_1":
                    continue
                name = w.Name or ""
                browser = next((b for b in ("Brave", "Google Chrome", "Chromium",
                                            "Microsoft Edge", "Vivaldi")
                                if name.endswith("- " + b)), "")
                if not browser:
                    continue
                first = w.TabItemControl(searchDepth=8)
                if not first.Exists(0.3):
                    continue
                for c in first.GetParentControl().GetChildren():
                    if c.ControlTypeName == "TabItemControl" and c.Name:
                        out.append((browser, c.Name))
            except Exception:
                continue                 # one dead window must not kill the list
    except Exception:
        return []
    _TABS = (now, out)
    return out


def tab_label(title):
    """Trim Brave's tab-decoration tail: '... - Audio playing - Memory usage - 336 MB'."""
    t = re.sub(r"\s+-\s+(?:Audio playing|Memory usage|Muted).*$", "", title or "")
    return re.sub(r"\s+", " ", t).strip()


def tab_song(tab_title):
    """(title, artist) for the song a tab is playing, or ('', '') if none.

    Brave marks a tab that is making sound with ' - Audio playing' in its
    accessible name, so that suffix is the per-tab equivalent of SMTC's
    playback status. Only those tabs can be a lyric source.
    """
    if "Audio playing" not in (tab_title or ""):
        return "", ""
    return split_track(tab_label(tab_title), "")


def app_label(app):
    """'Spotify.exe' -> 'Spotify'. The label is a substring of the session's
    app id, so pick_session's filter matches what the dropdown shows."""
    b = (app or "").rsplit("\\", 1)[-1].strip()
    if "!" in b:                 # packaged app: PackageFamily!AppId -> AppId
        b = b.split("!", 1)[1]
    return b[:-4] if b.lower().endswith(".exe") else b


def app_names():
    """Dropdown values: Auto, then tabs that are making sound, then players."""
    try:
        apps = {app_label(i["app"]) for i in sessions() if i["app"]}
    except Exception:
        apps = set()          # SMTC unavailable -> dropdown still offers Auto
    tabs = [TAB + "%s :: %s" % (b, tab_label(t))
            for b, t in browser_tabs() if "Audio playing" in (t or "")]
    return ["Auto"] + tabs + sorted(apps, key=str.lower)


def _probe(source="Auto"):
    """Pick the session to follow. Returns a session dict or None.

    A `tab:` source is not an SMTC session — it is one tab inside a browser
    that has exactly one. The browser's session still carries the right
    position, length, cover and title for whichever tab is making sound, so the
    tab's own label is only needed to identify it; the parsed title is a
    fallback for when SMTC reports none.

    ponytail: assumes one playing tab per browser, which is what SMTC models.
    Two Brave windows both playing would still collapse into one session.
    """
    if (source or "").startswith(TAB):
        browser, _, wanted = source[len(TAB):].partition(" :: ")
        items = [i for i in sessions() if app_label(i["app"]) == browser]
        if not items:
            return None
        base = dict(pick_session(items))
        for b, t in browser_tabs(max_age=1.5):
            if b == browser and tab_label(t) == wanted:
                title, artist = tab_song(t)
                if not title:
                    return None      # tab stopped playing since the dropdown opened
                base["title"] = base["title"] or title
                base["artist"] = base["artist"] or artist
                return base
        return None                  # tab closed / renamed
    items = [i for i in sessions()
             if is_song(i["title"], i["artist"], i["len"])]  # a video's caption is not a song
    return pick_session(items, source)


def poll(state, stop, cfg=None):
    """Background thread: keeps state["lines"]/state["idx"] fresh."""
    last_key, lines = "", []
    fetched_at = 0.0
    art_key, art_tries, art_at = "", 0, 0.0
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
            # A player publishes the new title before its real artwork. Brave
            # puts its own 256x256 app icon in the thumbnail slot first (same
            # bytes for every track), and the actual cover only turns up a few
            # seconds later — measured at 0.3s for an icon, then 5-15s for a
            # 150x150 cover on an automatic advance. A single read therefore
            # latches the browser icon for the whole track, which is the
            # "cover is missing, Brave icon instead" report. Pressing Next looks
            # fine only because that path lands closer to when the real art is
            # ready.
            #
            # So: drop the previous cover on a track change and keep re-reading
            # for a short window, replacing what we have whenever the bytes
            # change. Reading is cheap (a stream open per second, ten per track)
            # and the last read wins, so no preference for "icon vs cover" has
            # to be guessed.
            if key != art_key:
                art_key, art_tries, art_at = key, 0, 0.0
                state["art"] = b""       # never keep the previous track's cover
            if art_tries < ART_TRIES and time.monotonic() >= art_at:
                art_tries += 1
                art_at = time.monotonic() + ART_RETRY_S
                png = art_png(np.get("thumb"))
                if png and png != state["art"]:
                    state["art"] = png
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
            last_key, lines, art_key = "", [], ""
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
    d["viz"] = bool(d["viz"])
    d["enter_anim"] = bool(d["enter_anim"])
    d["notch"] = bool(d["notch"])
    d["align"] = d["align"] if d["align"] in ("top", "center", "bottom") else DEFAULTS["align"]
    d["anchor"] = (d["anchor"] if d["anchor"] in ANCHORS
                   else DEFAULTS["anchor"])
    num("radius", 0, 200)
    num("art_size", 16, 300)
    num("viz_bars", 1, 12)
    d["card_edge"] = d["card_edge"] if isinstance(d["card_edge"], str) and \
        re.fullmatch(r"#[0-9a-fA-F]{6}", d["card_edge"]) else DEFAULTS["card_edge"]
    for key in ("fg", "dim", "bg", "viz_color"):
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
ENTER_MS = 320         # new-track swap: outgoing leaves left, incoming arrives right
ENTER_OUT = 0.42       # share of the swap spent leaving; the rest is arriving

# One easing per track, chosen from the track's own text, so consecutive songs
# do not all arrive with the identical motion. Deterministic: the same track
# always gets the same curve, which matters because a re-fetch of the same song
# must not change the feel mid-play.
EASINGS = {
    "in-out-sine": lambda t: 0.5 - 0.5 * math.cos(math.pi * t),
    "in-out-cubic": lambda t: 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2,
    "in-out-quint": lambda t: 16 * t ** 5 if t < 0.5 else 1 - (-2 * t + 2) ** 5 / 2,
    "out-back": lambda t: 1 + 2.70158 * (t - 1) ** 3 + 1.70158 * (t - 1) ** 2,
}


def ease_for(texts):
    """Pick this track's easing curve from its lyrics, stably.

    crc32 rather than hash(): CPython randomises string hashing per process, so
    hash() would give the same song a different curve on every launch.
    """
    key = "\x00".join(texts or ()).encode("utf-8", "ignore")
    names = sorted(EASINGS)
    return EASINGS[names[zlib.crc32(key) % len(names)]]


def lerp_color(c1, c2, t):
    """'#rrggbb' -> '#rrggbb' blended t of the way from c1 to c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def notch_corners(anchor):
    """Which corners stay rounded for a bar pinned to `anchor`.

    A side that touches a screen edge must be square, or the bar stops looking
    attached to it. So the rounded set is every corner NOT on a flush edge:
    top-center keeps br+bl, top-left keeps only br.
    """
    v, _, h = (anchor or "top-center").partition("-")   # 'top-center' -> top, center
    out = ""
    for corner, ch, cv in (("tl", "left", "top"), ("tr", "right", "top"),
                           ("br", "right", "bottom"), ("bl", "left", "bottom")):
        if h != ch and v != cv:
            out += corner
    return out or "brbl"


def notch_shape(cv, x1, y1, x2, y2, r, rounded="brbl", **kw):
    """Screen-edge shape: square on the sides that touch an edge, round elsewhere.

    `rounded` lists the corners that keep the radius, as a string of
    tl/tr/br/bl. A notch glued to the top edge rounds only br and bl; a card
    floating in the middle rounds all four.

    The outline is computed here rather than with create_arc, because Tk's arc
    angles are easy to get a quadrant wrong on, and a smooth=True polygon bows
    the top edge (which makes it stop looking attached to the screen edge).
    """
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    # (name, arc centre, start degrees, the actual corner when not rounded).
    # Walked clockwise; y grows downward, so 0deg is 3 o'clock, 90deg is 6.
    quads = (
        ("tl", x1 + r, y1 + r, 180, (x1, y1)),
        ("tr", x2 - r, y1 + r, 270, (x2, y1)),
        ("br", x2 - r, y2 - r, 0, (x2, y2)),
        ("bl", x1 + r, y2 - r, 90, (x1, y2)),
    )
    pts = []
    steps = 8
    for name, cx, cy, start, corner in quads:
        if name in rounded and r:
            for i in range(steps + 1):
                a = math.radians(start + 90 * i / steps)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        else:
            pts.append(corner)
    flat = [c for p in pts for c in p]
    return cv.create_polygon(flat, fill=kw.get("fill"),
                             outline=kw.get("outline", ""),
                             width=kw.get("width", 1))


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
        self._enter_job = None
        self._enter = 1.0        # 1 = settled; <1 while the new-track swap runs
        self._enter_prev = None  # outgoing lyrics, shown during the first half
        self._enter_ease = EASINGS["in-out-cubic"]   # replaced per track
        self._from = self._to = 0.0
        self._t0 = 0.0
        self._art_png = b""
        self._art_size = None    # cache key: everything the art label is built from
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
        self.viz = None          # waveform canvas, only when viz is enabled
        self._art_key = b""      # the cover bytes the current image was made from
        self._art_shown = False  # is self.art placed on screen right now?
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
        # Anchors resolve against the work area, not the raw screen: the top
        # edge stays at 0 while the bottom edge stops above the taskbar
        # instead of sliding under it.
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x, y = anchor_xy(cfg["anchor"], sw, sh, w, h, screen_work())
        x, y = x + cfg["x_off"], y + cfg["top"]
        self.win.geometry(f"{w}x{h}+{x}+{y}")
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
            # notch = flush with the screen edge on that side, so only the
            # corners away from it are rounded; that is what makes it read as a
            # notch and not a floating card.
            if cfg.get("notch", True):
                notch_shape(self.canvas, 0, 0, w, h, cfg["radius"],
                            rounded=notch_corners(cfg["anchor"]),
                            fill=cfg["bg"], outline=cfg["card_edge"], width=1)
            else:
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

        # Lyric rows: geometry is computed once here and only re-used by
        # render(), so the row rect (and its radius inset) can never be
        # overridden by the animation frame.
        self._x0 = pad_x
        self._w = max(20, w - 2 * pad_x)

        # --- album art -----------------------------------------------------
        self._render_art(cfg)
        # The cover's geometry is set here, after _render_art and before the
        # lyric rows, never inside _render_art: see the note at the label.
        # The image's own width/height are authoritative even before Tk has laid
        # the new label out (its winfo_width() is still 1 at this point).
        self._art_shown = False
        if cfg["art"] and self.art is not None:
            aw, ah = self._art_img.width(), self._art_img.height()
            self.art.place(x=10, y=(h - ah) // 2, width=aw, height=ah)
            self._art_shown = True
            self._x0 = 10 + aw + 12

        # --- waveform bars at the right edge --------------------------------
        # Own canvas, child of the text frame: the big canvas sits *behind* the
        # frame, so anything drawn there is covered by the lyric labels.
        # A vertical create_line with capstyle=round is a rounded bar; Tk has no
        # rounded-rectangle primitive.
        if getattr(self, "viz", None) is not None:
            self.viz.destroy()
            self.viz = None
        self._viz_ids = []
        self._viz_gutter = 0             # px of the row the bars reserve
        if cfg["viz"]:
            bw, gap, pad = 3, 3, 10
            gw = pad * 2 + cfg["viz_bars"] * bw + (cfg["viz_bars"] - 1) * gap
            gw = min(gw, max(20, w // 3))
            self._viz_n = max(1, (gw - pad * 2 + gap) // (bw + gap))
            self.viz = tk.Canvas(self.box, width=gw, height=h, bd=0,
                                 highlightthickness=0, bg=lbl_bg)
            self.viz.place(x=w - gw - max(0, pad_x - 6), y=0, width=gw, height=h)
            x = gw - 10
            for _ in range(self._viz_n):
                self._viz_ids.append(self.viz.create_line(
                    x, h // 2, x, h // 2, width=bw, capstyle="round",
                    fill=cfg["viz_color"]))
                x -= bw + gap
            self._viz_gutter = gw - max(0, pad_x - 6) + 6

        # Lyric rows: geometry is computed once here and only re-used by
        # render(), so the row rect (and its radius inset) can never be
        # overridden by the animation frame.
        self._w = max(20, w - self._x0 - pad_x - self._viz_gutter)

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
        self._viz_stop()
        self._viz_tick()

    def _render_art(self, cfg):
        """Album art as a Tk image, sized to what the card can actually hold.

        Two separate bugs lived here. (1) The size cache was keyed on art_size
        alone, so a new track's cover was never decoded: the next track showed
        the previous cover, or nothing when the previous decode had failed (as
        it does for webp). (2) PhotoImage only scales by integer factors, so
        halving stopped at the first size <= 2*art_size -- up to 2x too big.
        On the notch (34px cover, 52px card) the label then swallowed the
        whole card and the cover was drawn clipped in half.

        The result is the size closest to, but never larger than, the box, so
        the cover never fills or overflows the card. art_size is the ceiling;
        the card's height is the hard limit.
        """
        # The key must include everything the label is built from: the cover
        # bytes, the box the image is fitted into, and the label's background.
        # Leaving `art` out meant switching art OFF kept the old cover on
        # screen, because the cached key still matched.
        key = (self._art_png, cfg["art_size"], self.row_h(), cfg["lines"],
               cfg["art"], cfg["bg"] if cfg["card"] else self.box.cget("bg"))
        if self._art_shown and self._art_alive() and self._art_size == key:
            return                       # same cover, same box: nothing to redraw
        if self.art is not None and self._art_alive():
            self.art.destroy()
        self.art = None
        self._art_key = self._art_png
        self._art_size = key
        if not cfg["art"] or not self._art_png:
            return
        slot_h = self.row_h() * max(1, cfg["lines"])
        box = max(1, min(cfg["art_size"], slot_h - 2))
        try:
            img = tk.PhotoImage(data=base64.b64encode(self._art_png).decode())
        except tk.TclError:
            return                       # not a format Tk can read (webp, etc.)
        # Tk's b"..." data reader keeps the source size, so subsample()/zoom()
        # from the master hit the file's real resolution each time.
        # subsample(k) then zoom(z) gives source*z/k. k in [k0, k0+4] with
        # k0 = ceil(w/box) pairs a coarse downsample with 1x/2x/4x/8x, so a
        # cover can also be scaled UP into the box (a small source used to stay
        # tiny). Candidates over the box are skipped, so the cover always fits.
        src = img
        k0 = max(1, -(-src.width() // box))
        best = None                      # (width, k, z), widest that still fits
        for k in range(k0, k0 + 5):
            s = src if k == 1 else src.subsample(k, k)
            for z in (1, 2, 4, 8):
                w = s.width() * z
                if w <= box and (best is None or w > best[0]):
                    best = (w, k, z)
        if best is None:
            return
        _, k, z = best
        s = img if k == 1 else img.subsample(k, k)
        img = s.zoom(z, z) if z > 1 else s
        self._art_img = img              # keep a reference or Tk collects it
        # On a card the label must be card-coloured: a MAGIC background would
        # punch a transparent rectangle around the cover, straight through the
        # card, because the colour key applies to the composited window.
        lbg = cfg["bg"] if cfg["card"] else self.box.cget("bg")
        self.art = tk.Label(self.box, image=img, bg=lbg, bd=0)
        # Only created here. Where it sits is apply()'s call (see below): place()
        # re-enters the geometry manager, which runs the label's own callback
        # on the *current* box size, and that callback quits with return
        # "break" -- aborting the rest of apply() and leaving self.art pointing
        # at a widget that is already gone. The next apply() then found that
        # dead handle ("is not None") and took the "cover already drawn"
        # early return, so the art vanished for every later track until some
        # other change re-rendered it. Hence no place() here, and
        # _art_shown remembers whether the cover is really on screen.
        self._art_shown = False

    def _art_alive(self):
        """True when self.art is a widget that still exists."""
        if self.art is None:
            return False
        try:
            return bool(self.art.winfo_exists())
        except tk.TclError:
            return False

    def set_art(self, png):
        """New track: swap the cover if it actually changed."""
        if png == self._art_png:
            return
        self._art_png = png
        self._art_size = None
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
        x0, w = getattr(self, "_x0", 0), getattr(self, "_w", cfg["width"])
        blend = cfg["bg"] if cfg["card"] else self.box.cget("bg")

        # New-track transition, left to right: the outgoing lyrics travel left
        # and fade to the card colour, then the incoming ones arrive from the
        # right. One label set serves both halves — the text is swapped at the
        # midpoint, where the outgoing lines are already fully transparent, so
        # nothing visible ever changes text in place.
        ent = getattr(self, "_enter", 1.0)
        texts, dx, fade = self.texts, 0, 1.0
        if ent < 1:
            prev = getattr(self, "_enter_prev", None)
            # p is linear inside each half and the per-track curve is applied to
            # p, not to the clock. Easing the clock was the bug: out-quint reached
            # 0.8 of the whole transition in 30% of the time, so the incoming half
            # got about three frames and read as a cut. Each half now gets its
            # full share of ENTER_MS, curved.
            if prev and ent < ENTER_OUT:
                texts = prev
                # Departure is the ARRIVAL curve mirrored: q = 1 - ease(1 - q).
                # Using the raw curve here was the stiffness — out-back is
                # fastest at t=0, so the outgoing lyrics left the screen in two
                # frames and the swap read as a cut. Mirrored, every curve starts
                # slow, and the motion is continuous through the midpoint
                # because the two halves are the same curve back to back.
                q = ent / ENTER_OUT
                p = 1.0 - self._enter_ease(1.0 - q)
                dx, fade = round(-p * w), 1.0 - min(1.0, p)
            else:
                p = self._enter_ease((ent - ENTER_OUT) / (1.0 - ENTER_OUT))
                dx, fade = round((1.0 - p) * w), min(1.0, p)

        pos = max(0.0, min(float(self.pos), len(texts) - 1))
        if texts is self.texts:
            self.pos = pos                     # keep it clamped for the next glide
        # scroll so the playhead sits where the preset wants it, clamped at both
        # ends: top = first visible row, center = middle, bottom = last.
        rows = max(0, len(texts) - vis)
        off = {"top": 0.0, "center": 0.5, "bottom": 1.0}[cfg["align"]] * (vis - 1)
        top = max(0.0, min(pos - off, rows))
        fg, dim = cfg["fg"], cfg["dim"]
        font = (cfg["font_family"], cfg["font_size"],
                "bold" if cfg["bold"] else "normal")
        for lbl in self.lines:
            lbl.place_forget()
        for k, lbl in enumerate(self.lines):
            i = int(top) + k
            if i >= len(texts):
                break
            act = max(0.0, 1.0 - min(1.0, abs(i - pos)))   # 1 = playhead, 0 = 2+ away
            fcolor = lerp_color(dim, fg, act)
            if fade < 1:
                fcolor = lerp_color(blend, fcolor, fade)
            lbl.configure(text=texts[i], font=font, fg=fcolor)
            lbl.place(x=x0 + dx, y=round((i - top) * h), width=w, height=h)

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

    def _viz_tick(self):
        """Animate the bars. Not real audio: WASAPI loopback returns silence on
        this machine, so the shape is a phase-shifted sine per bar.
        ponytail: synthetic. Swap in real levels when process-loopback capture
        (ActivateAudioInterfaceAsync) is available."""
        self._viz_job = None
        if not self.cfg.get("viz") or not self._viz_ids:
            return
        h = self.row_h() * max(1, self.cfg["lines"])
        if not self.texts:                 # nothing playing: bars rest short
            for i in self._viz_ids:
                x = self.viz.coords(i)[0]
                self.viz.coords(i, x, h / 2 - 2, x, h / 2 + 2)
            self._viz_job = self.win.after(250, self._viz_tick)
            return
        t = time.monotonic() * 3.4
        for k, i in enumerate(self._viz_ids):
            ph = t + k * 0.9
            amp = 0.30 + 0.62 * abs(math.sin(ph) * math.cos(ph * 0.61 + k))
            half = max(2.0, amp * (h - 20) / 2)
            x = self.viz.coords(i)[0]
            cy = h / 2
            self.viz.coords(i, x, cy - half, x, cy + half)
        self._viz_job = self.win.after(45, self._viz_tick)

    def _viz_stop(self):
        if getattr(self, "_viz_job", None):
            try:
                self.win.after_cancel(self._viz_job)
            except tk.TclError:
                pass
            self._viz_job = None

    def set_content(self, texts, idx):
        if texts != self.texts:
            new_song = bool(self.texts) and bool(texts)
            self._enter_prev = self.texts if new_song else None
            self.texts = texts
            self.idx = None                # new song: snap, don't glide
            self.pos = float(idx or 0)
            if new_song and self.cfg.get("enter_anim", True):
                self._enter_from()         # left→right swap
            self.render()
            self.idx = idx
            return
        if idx == self.idx:
            return
        self.idx = idx
        self.animate_to(idx)

    def _enter_from(self):
        """Start the left→right track swap.

        Two tracks back to back share nothing to glide between, so a per-line
        glide reads as a hard cut. Here the outgoing lyrics slide left and fade
        out, then the incoming ones slide in from the right — a single `_enter`
        value covers both halves.

        ponytail: horizontal slide + colour blend only. Tk's colour key is 1-bit
        and labels have no alpha, so the "fade" is the text colour blended
        towards the card.
        """
        if self._enter_job:
            try:
                self.win.after_cancel(self._enter_job)
            except tk.TclError:
                pass
            self._enter_job = None
        self._enter = 0.0
        self._enter_ease = ease_for(self.texts)
        t0 = time.monotonic()

        def step():
            t = min(1.0, (time.monotonic() - t0) * 1000 / ENTER_MS)
            # Linear clock. render() applies _enter_ease inside each half, so the
            # curve shapes the motion instead of eating the incoming frames.
            self._enter = t
            self.render()
            if t < 1:
                self._enter_job = self.win.after(ANIM_FRAME_MS, step)
            else:
                self._enter_job = None
                self._enter = 1.0
                self.render()

        step()

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

        self.viz_var = tk.BooleanVar(value=c["viz"])
        ttk.Checkbutton(f, text="Waveform bars (synthetic, not real audio)", variable=self.viz_var,
                        command=self.apply).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1
        entry("Waveform bars", "viz_bars")
        color("Waveform colour", "viz_color")

        self.enter_var = tk.BooleanVar(value=c["enter_anim"])
        ttk.Checkbutton(f, text="Slide lyrics in on a new track", variable=self.enter_var,
                        command=self.apply).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1

        ttk.Label(f, text="Position").grid(row=r, column=0, sticky="w", pady=3)
        self.vars["anchor"] = tk.StringVar(value=c["anchor"])
        ttk.Combobox(f, textvariable=self.vars["anchor"], values=ANCHORS,
                     state="readonly", width=14).grid(row=r, column=1,
                                                      sticky="w", padx=(10, 0))
        self.vars["anchor"].trace_add("write", lambda *_: self.apply())
        r += 1

        ttk.Label(f, text="Active line position").grid(row=r, column=0, sticky="w", pady=3)
        self.vars["align"] = tk.StringVar(value=c["align"])
        al = ttk.Combobox(f, textvariable=self.vars["align"], values=("top", "center", "bottom"),
                          state="readonly", width=10)
        self.align_box = al
        al.grid(row=r, column=1, sticky="w", padx=(10, 0))
        self.vars["align"].trace_add("write", lambda *_: self.apply())
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
        c["viz"] = self.viz_var.get()
        c["enter_anim"] = self.enter_var.get()
        c["align"] = self.vars["align"].get()
        c["anchor"] = self.vars["anchor"].get()
        try:
            c["viz_bars"] = int(float(self.vars["viz_bars"].get()))
        except (ValueError, tk.TclError):
            pass
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
                      "font_family", "radius", "viz_bars"):
                self.vars[k].set(str(c[k]))
            self.art_var.set(c["art"])
            self.card_var.set(c["card"])
            self.viz_var.set(c["viz"])
            self.enter_var.set(c["enter_anim"])
            self.vars["align"].set(c["align"])
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
        real_tabs = browser_tabs
        try:
            globals()["browser_tabs"] = lambda *a, **k: []
            globals()["sessions"] = lambda: [
                {"app": "Spotify.exe"}, {"app": "Brave"}, {"app": "Brave"}, {"app": ""}]
            assert app_names() == ["Auto", "Brave", "Spotify"], app_names()
            globals()["sessions"] = lambda: (_ for _ in ()).throw(RuntimeError)
            assert app_names() == ["Auto"]   # SMTC dead -> launcher still usable
        finally:
            globals()["sessions"] = real
            globals()["browser_tabs"] = real_tabs

        # a YouTube caption is 'Artist - Title (Official Video)'; searching
        # lrclib with it verbatim matches nothing and the overlay shows the
        # caption as if it were a lyric line
        assert split_track("Sydney Rose - We Hug Now (Official Video)",
                           "Sydney Rose") == ("We Hug Now", "Sydney Rose")
        assert split_track("Sydney Rose - We Hug Now (Official Video)",
                           "") == ("We Hug Now", "Sydney Rose")
        assert split_track("Taylor Swift - Guilty as Sin? (Official Lyric Video)",
                           "Taylor Swift") == ("Guilty as Sin?", "Taylor Swift")
        # title-first order (a Brave tab uses 'Title • Artist')
        assert split_track("What if I miss you for the rest of my life? \u2022 "
                           "Janine Berdin", "") == (
            "What if I miss you for the rest of my life?", "Janine Berdin")
        assert split_track("We Hug Now", "Sydney Rose") == ("We Hug Now", "Sydney Rose")
        # a song whose name contains a noise word must keep it
        assert split_track("Live Forever (Official Video)", "Oasis") == (
            "Live Forever", "Oasis")
        assert split_track("", "") == ("", "")

        # a tab label carries Brave's decorations; only a playing tab is a source
        playing = "We Hug Now \u2013 Sydney Rose - Audio playing - Memory usage - 336 MB"
        assert tab_label(playing) == "We Hug Now \u2013 Sydney Rose", tab_label(playing)
        assert tab_label("Discord - Memory usage - 246 MB") == "Discord"
        assert tab_song("Discord - Memory usage - 246 MB") == ("", "")
        assert tab_song(playing) == ("We Hug Now", "Sydney Rose")
        # tab: sources must never be mistaken for an app id
        assert pick_session([a, b], TAB + "Brave :: We Hug Now") is None
        # an unknown/closed tab resolves to nothing instead of a wrong song
        try:
            globals()["sessions"] = lambda: [dict(b, artist="Sydney Rose", len=248.9)]
            globals()["browser_tabs"] = lambda *x, **k: [("Brave", playing)]
            assert _probe(TAB + "Brave :: Gone") is None
            got = _probe(TAB + "Brave :: We Hug Now \u2013 Sydney Rose")
            assert got and got["title"] == "B", got
        finally:
            globals()["sessions"] = real
            globals()["browser_tabs"] = real_tabs

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

        # anchors sit on the work area: bottom anchors stop above the taskbar,
        # and a taskbar on any other side is avoided too. (Tk is not started
        # here; live geometry is test_ui.py's job.)
        try:
            globals()["screen_work"] = lambda: (0, 0, 1920, 1040)   # 40px bottom taskbar
            assert screen_work() == (0, 0, 1920, 1040)
            work = (0, 0, 1920, 1040)
            assert anchor_xy("bottom-left", 1920, 1080, 34, 34, work) == (0, 1006)
            assert anchor_xy("bottom-right", 1920, 1080, 34, 34, work) == (1886, 1006)
            assert anchor_xy("bottom-center", 1920, 1080, 34, 34, work) == (943, 1006)
            assert anchor_xy("top-center", 1920, 1080, 34, 34, work) == (943, 0)
            assert anchor_xy("top-left", 1920, 1080, 34, 34, work) == (0, 0)
            # taskbar on top, and on the left: the work area, not the screen,
            # decides every edge
            top_bar = (0, 40, 1920, 1080)
            assert anchor_xy("top-left", 1920, 1080, 34, 34, top_bar) == (0, 40)
            assert anchor_xy("bottom-right", 1920, 1080, 34, 34, top_bar) == (1886, 1046)
            left_bar = (60, 0, 1920, 1080)
            assert anchor_xy("top-left", 1920, 1080, 34, 34, left_bar) == (60, 0)
            assert anchor_xy("bottom-center", 1920, 1080, 34, 34, left_bar) == (973, 1046)
            # no work area (hidden taskbar / non-Windows) falls back to the screen
            assert anchor_xy("bottom-left", 1920, 1080, 34, 34, None) == (0, 1046)
        finally:
            globals()["screen_work"] = real

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
