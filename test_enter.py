"""Does a new track actually animate, and does the toggle turn it off?

Measures the entrance frames on the real Overlay: mid-transition the rows must
be displaced (slide) and darker than settled (fade).
"""
import importlib.machinery as m
import importlib.util as u
import tempfile
import time
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
l = m.SourceFileLoader("nl", str(HERE / "NotchLyrics.pyw"))
nl = u.module_from_spec(u.spec_from_loader("nl", l))
l.exec_module(nl)

root = tk.Tk()
root.withdraw()
cfg = nl.sanitize(dict(nl.PRESET_CARD, lines=2, align="top"))
ov = nl.Overlay(root, cfg)
root.update()


def settle():
    root.update()


def frame_state():
    rows = []
    for lbl in ov.lines:
        if lbl.winfo_ismapped():
            rows.append((lbl.cget("text"), lbl.winfo_y(), lbl.cget("fg")))
    return sorted(rows, key=lambda r: r[1])


# first track: no entrance (nothing to transition from)
ov.set_content(["one a", "one b", "one c"], 0)
settle()
print("first track settled:", frame_state())

# second track: entrance must run
ov.set_content(["two a", "two b", "two c"], 0)
t0 = time.monotonic()
samples = []
while time.monotonic() - t0 < nl.ENTER_MS / 1000:
    settle()
    samples.append((time.monotonic() - t0, ov._enter, frame_state()[0]))
    time.sleep(0.012)
print("frames during entrance: %d" % len(samples))
for t, e, top in samples[:6]:
    print("   t=%5.0fms enter=%.2f top=(%r, y=%d, %s)" % (t * 1000, e, *top))

settle()
print("after entrance:", frame_state())
assert len(samples) > 3, samples
mid = [s for s in samples if 0.1 < s[1] < 0.9]
assert mid, "entrance never hit a mid frame: it is not animating"
# mid-transition rows sit lower than settled (slide from below) and their text
# is darker than the final colour (fade from the card background)
mid_state = mid[0][2]
final_state = frame_state()[0]
assert mid_state[1] > final_state[1], (mid_state, final_state)
assert mid_state[2] != final_state[2], (mid_state, final_state)

# turning it off: a new track must snap straight to the settled frame
ov.cfg["enter_anim"] = False
ov.set_content(["three a", "three b", "three c"], 0)
settle()
assert ov._enter == 1.0, ov._enter
assert frame_state()[0][1] == 0, frame_state()
print("enter_anim off -> snapped, no slide")

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
print("\nPASS: new-track entrance animates, and the toggle controls it")
