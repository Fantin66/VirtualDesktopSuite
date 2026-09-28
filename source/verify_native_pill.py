# -*- coding: utf-8 -*-
"""Check that the native pill never runs its WndProc on Tk's main thread."""
import ctypes
import threading
import time

from PIL import Image

from layered_pill import NativePill


def main():
    main_tid = threading.get_ident()
    received = []
    pill = NativePill(lambda kind, x, y:
                      received.append((threading.get_ident(), kind, x, y)))
    try:
        assert pill._thread.ident != main_tid
        pill.render(Image.new("RGBA", (32, 32), (40, 110, 150, 255)),
                    -1000, -1000)
        pill.show()
        for msg in (0x0201, 0x0202, 0x0205):
            assert ctypes.windll.user32.PostMessageW(pill.hwnd, msg, 0,
                                                      (12 << 16) | 12)
        deadline = time.monotonic() + 2
        while len(received) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [r[1] for r in received[:3]] == ["press", "release", "menu"]
        assert all(r[0] == pill._thread.ident for r in received[:3])
    finally:
        pill.close()
        pill._thread.join(timeout=2)
    assert not pill._thread.is_alive()
    print("PASS: native mouse callbacks stayed on the dedicated thread")


if __name__ == "__main__":
    main()
