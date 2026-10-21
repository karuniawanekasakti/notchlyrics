"""Does the track swap actually move left→right, and can it be turned off?

Samples the live Overlay during a real track change: the outgoing lyrics must
travel left, the incoming ones must arrive from the right, the text may only
change while nothing is visible, and the block must settle flush at dx=0.
"""
import importlib.machinery as m
import importlib.util as u
import sys
import time
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
l = m.SourceFileLoader("nl", str(HERE / "NotchLyrics.pyw"))
nl = u.module_from_spec(u.spec_from_loader("nl", l))
l.exec_module(nl)

if sys.platform != "win32":
    nl.Overlay._clickthrough = lambda self: None    # ctypes.windll only exists on Windows

root = tk.Tk()
root.withdraw()
ov = nl.Overlay(root, nl.sanitize(dict(nl.PRESET_CARD, lines=2, align="top")))
root.update()
W, X0 = ov._w, ov._x0


def snap():
    rows = [(lbl.cget("text"), lbl.winfo_x() - X0, lbl.cget("fg"))
            for lbl in ov.lines if lbl.winfo_ismapped()]
    return sorted(rows, key=lambda r: r[1])


A = ["old line one", "old line two", "old line three"]
B = ["new line one", "new line two", "new line three"]

ov.set_content(A, 0)
root.update()
ov._enter = 1.0
ov.render()
root.update()
print("settled A:", snap())

ov.set_content(B, 0)
t0 = time.monotonic()
frames = []
while time.monotonic() - t0 < (nl.ENTER_MS / 1000 + 0.05):
    root.update()
    frames.append((round((time.monotonic() - t0) * 1000), ov._enter, snap()))
    time.sleep(0.012)
root.update()

print("frames:", len(frames), " content width:", W)
for t, e, rows in frames[::3]:
    texts = [r[0] for r in rows if r[0]]
    dxs = [r[1] for r in rows if r[0]]
    print("  t=%3dms enter=%.2f dx=%-16s %s" % (
        t, e, str(sorted(set(dxs))[:3]), texts[:1]))

# outgoing half: old text, negative x (moving left)
out = [(t, e, r) for t, e, rows in frames for r in rows
       if r[0].startswith("old") and e < 0.5]
assert out, "no outgoing frames: the old lyrics never showed"
assert min(r[1] for _, _, r in out) < 0, ("old lyrics did not move left",
                                          min(r[1] for _, _, r in out))
# incoming half: new text, positive x (arriving from the right)
inc = [(t, e, r) for t, e, rows in frames for r in rows
       if r[0].startswith("new") and e > 0.5]
assert inc, "no incoming frames: the new lyrics never showed"
assert max(r[1] for _, _, r in inc) > 0, ("new lyrics did not arrive from the right",
                                          max(r[1] for _, _, r in inc))
# text may only swap at the midpoint, where the outgoing lines are invisible
early = [r[0] for t, e, rows in frames if e < 0.4 for r in rows if r[0]]
assert not any(t.startswith("new") for t in early), ("new text appeared early", early)

# and it settles flush at 0 on the new track
last = snap()
assert all(r[1] == 0 for r in last if r[0]), last
assert last[0][0] == "new line one", last

print("\nPASS: outgoing left (min dx=%d), incoming from right (max dx=%d), settled at 0"
      % (min(r[1] for _, _, r in out), max(r[1] for _, _, r in inc)))

# with the animation off it must snap, not slide
ov.cfg["enter_anim"] = False
ov.set_content(A, 0)
root.update()
ov._enter = 1.0
ov.render()
ov.set_content(B, 0)
root.update()
assert ov._enter == 1.0 and ov.texts is B, (ov._enter, ov.texts)
assert all(r[1] == 0 for r in snap() if r[0]), snap()
print("enter_anim off -> snapped at dx=0")

# easing per track: deterministic for the same track, spread across tracks
names = sorted(nl.EASINGS)
seen = {nl.ease_for([f"track {i} line one", f"track {i} line two"]) for i in range(60)}
assert len(seen) == len(names), ("not every curve is reachable", len(seen), names)
# same track -> same curve, across separate calls (and across processes: crc32,
# not hash(), which CPython salts per process)
assert nl.ease_for(["a", "b"]) is nl.ease_for(["a", "b"])
assert nl.ease_for([]) is nl.ease_for([])

# every curve starts at 0 and lands exactly on 1, and none goes below 0.
# out-back deliberately overshoots above 1 mid-flight (that is the "back" part);
# render() clamps, so the overshoot cannot push a row past its slot.
for name, fn in nl.EASINGS.items():
    assert abs(fn(0.0)) < 1e-9, (name, fn(0.0))
    assert abs(fn(1.0) - 1.0) < 1e-9, (name, fn(1.0))
    vals = [fn(i / 40) for i in range(41)]
    assert min(vals) >= -1e-9, (name, min(vals))

# and the config round-trips
assert nl.sanitize({"enter_anim": 0})["enter_anim"] is False
assert nl.sanitize({})["enter_anim"] is True          # on by default
assert nl.PRESET_CARD.get("enter_anim", True) is True

root.destroy()
print("\nPASS: left-to-right swap animates, and the toggle controls it")
