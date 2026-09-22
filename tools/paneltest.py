"""Prove the panel physically lights up.

    sudo .venv/bin/python tools/paneltest.py

Everything else in this project can pass while the panel stays black: the C
library reports success for a panel that never responds, so "no errors in the
log" says nothing about whether anything is lit. This is the only check that
answers the question directly, by drawing something a person can look at.

It draws, in order:
  1. each primary at full width -- proves R, G and B channels all reach it
  2. a one-pixel border -- proves the geometry is right, since a wrong
     chain length or rows setting shows up as a border that wraps, doubles
     or falls off the edge
  3. the word OK

Exits non-zero if the matrix can't be created at all.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import matrix  # noqa: E402


def _fill(canvas, r, g, b):
    for y in range(matrix.HEIGHT):
        for x in range(matrix.WIDTH):
            canvas.SetPixel(x, y, r, g, b)


def _border(canvas, r, g, b):
    w, h = matrix.WIDTH, matrix.HEIGHT
    for x in range(w):
        canvas.SetPixel(x, 0, r, g, b)
        canvas.SetPixel(x, h - 1, r, g, b)
    for y in range(h):
        canvas.SetPixel(0, y, r, g, b)
        canvas.SetPixel(w - 1, y, r, g, b)


def main() -> int:
    print(f"driving: {matrix.describe()}")
    if matrix.EMULATE:
        print("!! running against the EMULATOR -- this proves nothing about the")
        print("   real panel. On the Pi that means the rgbmatrix bindings did")
        print("   not install; re-run deploy/install-pi.sh.")

    try:
        m = matrix.create_matrix(brightness=60)
    except Exception as exc:  # noqa: BLE001 -- surfacing it is the whole job
        print(f"!! could not open the panel: {exc}")
        return 1

    canvas = m.CreateFrameCanvas()
    steps = [
        ("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255)),
        ("white", (255, 255, 255)),
    ]
    try:
        for name, (r, g, b) in steps:
            print(f"  {name} -- the whole panel should be {name}")
            canvas.Clear()
            _fill(canvas, r, g, b)
            canvas = m.SwapOnVSync(canvas)
            time.sleep(1.5)

        print(f"  border -- a single clean rectangle around all "
              f"{matrix.WIDTH}x{matrix.HEIGHT}")
        canvas.Clear()
        _border(canvas, 255, 255, 255)
        canvas = m.SwapOnVSync(canvas)
        time.sleep(3)

        print("  OK")
        canvas.Clear()
        from app.render import bigtext  # local: keeps the import cost off failures
        bigtext.fill(canvas, "OK", (0, 0, matrix.WIDTH, matrix.HEIGHT), (0, 255, 0))
        canvas = m.SwapOnVSync(canvas)
        time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        # Release the matrix explicitly. Its refresh thread runs at real-time
        # priority (SCHED_FIFO), so one left running outranks everything else
        # on the machine -- the kernel still answers pings while sshd never
        # gets enough CPU to send a banner, which looks exactly like a dead Pi
        # and can only be cleared by pulling the power.
        canvas.Clear()
        m.SwapOnVSync(canvas)
        try:
            m.Clear()
        except Exception:  # noqa: BLE001 -- teardown must not mask the real result
            pass
        del canvas, m

    print()
    print("If the panel stayed black through all of that, the usual causes in")
    print("order of likelihood:")
    print("  1. wrong MATRIX_PANEL_TYPE -- FM6126A panels need it and give no")
    print("     error whatsoever without it. Try: MATRIX_PANEL_TYPE=FM6126A")
    print("  2. the panel's own power harness isn't connected (the ribbon")
    print("     carries data only)")
    print("  3. ribbon in the panel's OUT socket instead of IN")
    print("  4. wrong MATRIX_MAPPING for the adapter board")
    print()
    print("If it reset the Pi instead, that's a brownout: give the Pi its own")
    print("5V supply rather than sharing the panel's. Check with:")
    print("  vcgencmd get_throttled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
