# -*- coding: utf-8 -*-
"""Experimental taskbar edge reveal for VirtualDesktopSuite.

Uses the existing 400 ms status loop. Explorer gets the first chance to run
normal auto-hide. Only a taskbar that remains expanded away from the pointer
is hidden as a fallback; that fallback can be revealed at the screen edge.
"""
import atexit
import ctypes
import os
import sys
import time
import traceback

import desktop_suite as core
from layered_pill import NativePill
from PIL import Image, ImageDraw, ImageFont


EDGE_PX = 5
STUCK_GRACE_S = 1.4
LEAVE_GRACE_S = 0.35

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetTimeFormatEx.argtypes = [ctypes.c_wchar_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_wchar_p,
                                    ctypes.c_wchar_p, ctypes.c_int]
kernel32.GetDateFormatEx.argtypes = [ctypes.c_wchar_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_wchar_p,
                                    ctypes.c_wchar_p, ctypes.c_int,
                                    ctypes.c_wchar_p]


def system_clock_labels():
    """Use the same user-selected short time/date formats as the taskbar."""
    clock = ctypes.create_unicode_buffer(80)
    date = ctypes.create_unicode_buffer(80)
    if not kernel32.GetTimeFormatEx(None, 0x2, None, None,
                                   clock, len(clock)):
        clock.value = time.strftime("%H:%M")
    if not kernel32.GetDateFormatEx(None, 0x1, None, None,
                                   date, len(date), None):
        date.value = time.strftime("%Y-%m-%d")
    return clock.value, date.value


class ClockOverlay:
    """One click-through layered HWND, repainted only when a label changes."""
    def __init__(self, on_error):
        self.window = NativePill(lambda *_: None, on_error,
                                 click_through=True)
        self.shown = False
        self.last_key = None
        self.labels = None
        self._sample_at = 0.0
        self._on_dark_background = None
        self._resample_until = 0.0

    def _background_is_dark(self, dock_rect):
        # The taskbar has a fixed theme, but the window beneath a hidden
        # taskbar may be white. Sample only transparent margins of our clock.
        now = time.monotonic()
        if now < self._sample_at:
            return self._on_dark_background
        # A newly switched desktop can paint its app after the Shell has
        # already slid away. Follow the existing 400 ms loop briefly, then
        # return to a six-second background sample interval.
        self._sample_at = now + (0.4 if now < self._resample_until else 6.0)
        _, top, right, bottom = dock_rect
        height = bottom - top
        dc = core.user32.GetDC(0)
        if not dc:
            return self._on_dark_background
        try:
            values = [core.gdi32.GetPixel(dc, right - dx,
                                          top + round(height * fy))
                      for dx, fy in ((12, .30), (12, .70), (195, .50))]
        finally:
            core.user32.ReleaseDC(0, dc)
        values = [v for v in values if v != 0xFFFFFFFF]
        if values:
            luminance = sum((v & 255) * .2126
                            + ((v >> 8) & 255) * .7152
                            + ((v >> 16) & 255) * .0722 for v in values)
            self._on_dark_background = luminance / len(values) < 145
        return self._on_dark_background

    def hide(self):
        if self.shown:
            self.window.hide()
            self.shown = False
            self._sample_at = 0.0

    def update(self, dock_rect, dark):
        left, top, right, bottom = dock_rect
        bar_h = bottom - top
        scale = max(0.5, min(2.5, bar_h / 96.0))
        width = round(220 * scale)
        x = right - width
        if not self.shown:
            self._resample_until = time.monotonic() + 3.0
            self._sample_at = 0.0
        minute = time.localtime()[:5]
        if minute != getattr(self, "_minute", None):
            self.labels = system_clock_labels()
            self._minute = minute
        background_dark = self._background_is_dark(dock_rect)
        if background_dark is None:
            background_dark = dark
        key = (self.labels, dock_rect, background_dark)
        if key != self.last_key:
            font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                     "Fonts", "segoeui.ttf")
            font = ImageFont.truetype(font_path, round(24 * scale))
            image = Image.new("RGBA", (width, bar_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            color = ((247, 247, 247, 255) if background_dark else
                     (31, 31, 31, 255))
            text_right = width - round(38 * scale)
            draw.text((text_right, round(14 * scale)), self.labels[0],
                      font=font, fill=color, anchor="ra")
            draw.text((text_right, round(47 * scale)), self.labels[1],
                      font=font, fill=color, anchor="ra")
            self.window.render(image, x, top)
            self.last_key = key
        if not self.shown:
            self.window.set_z(core.HWND_TOPMOST)
            self.window.show()
            self.shown = True

    def close(self):
        self.window.close()


class EdgeSuite(core.Suite):
    def __init__(self):
        # super().__init__ can enter an existing exclusive desktop immediately.
        self._stuck_since = None
        self._edge_revealed = False
        self._edge_left_at = None
        self._clock_shell_hidden = False
        self.clock = None
        super().__init__()
        self.clock = ClockOverlay(lambda e: self._log(
            f"时钟绘制异常: {e!r}"))
        self.update_clock()
        self._log("触边试验版启动：原生自动隐藏优先，卡住时启用触边兜底")

    def update_clock(self):
        clock = self.clock
        if clock is None:
            return
        if not (self.in_mvd and self.cfg["mvd_link"]
                and self.tray_goal is True and self.dock_rect):
            clock.hide()
            return
        tray = self.tray or core.tray_hwnd()
        rc = core.tray_rect()
        top = self.dock_rect[1]
        height = self.dock_rect[3] - top
        shell_visible = bool(tray and core.user32.IsWindowVisible(tray))
        fully_hidden = (not shell_visible or
                        (rc is not None and rc[1] >= self.dock_rect[3] - 2))
        if fully_hidden and not self._clock_shell_hidden:
            # The first sample can be taken during the slide animation and
            # still see the old taskbar color. Resample the actual app now.
            clock._sample_at = 0.0
        self._clock_shell_hidden = fully_hidden
        # Hide the clone as the real clock slides in, before both overlap.
        clock_exposed = (tray and rc and shell_visible
                         and rc[1] <= top + height // 2
                         and rc[3] >= top + height)
        if clock_exposed:
            clock.hide()
        else:
            clock.update(self.dock_rect, self.dark)

    def poll(self):
        super().poll()
        self.update_clock()

    def enter_mvd(self):
        super().enter_mvd()
        self.update_clock()

    def leave_mvd(self, restore_immersive=True):
        if self.clock is not None:
            self.clock.hide()
        super().leave_mvd(restore_immersive=restore_immersive)

    def _pointer_at_taskbar_edge(self):
        tray = self.forced_tray_hwnd or self.tray or core.tray_hwnd()
        monitor = core.monitor_rect(tray) if tray else None
        if monitor is None:
            return False
        point = core.POINT()
        if not core.user32.GetCursorPos(ctypes.byref(point)):
            return False
        left, top, right, bottom = monitor
        # The suite currently supports the Windows 11 bottom taskbar.
        return (left <= point.x < right and
                bottom - EDGE_PX <= point.y < bottom)

    def _pointer_on_expanded_taskbar(self):
        # During the Shell slide animation only part of the taskbar is on
        # screen. tray_out is deliberately false until it is fully visible;
        # using that flag here would hide the bar under the user's cursor.
        return self.pointer_in_tray()

    def force_hide_tray(self):
        """Keep native hover; use ShowWindow only after Explorer gets stuck."""
        if not self.cfg["mvd_link"] or self.tray_goal is not True:
            return
        now = time.monotonic()
        at_edge = self._pointer_at_taskbar_edge()
        if self.forced_tray_hwnd:
            tray = self.forced_tray_hwnd
            if not core.is_window(tray):
                self.release_forced_tray()
                self._stuck_since = None
                return
            if at_edge or (self._edge_revealed and
                           self._pointer_on_expanded_taskbar()):
                self._edge_left_at = None
                if not self._edge_revealed or not core.user32.IsWindowVisible(tray):
                    core.user32.ShowWindow(tray, core.SW_SHOW)
                    self._edge_revealed = True
                    # A child pill may need to be shown again after its Shell
                    # parent was hidden; this also refreshes the floating z order.
                    self.reposition()
                    self.place_floating()
                    self.update_floating_visibility()
                    self.update_clock()
                    self._log("触边显示任务栏")
                return
            if self._edge_revealed:
                if self._edge_left_at is None:
                    self._edge_left_at = now
                    return
                if now - self._edge_left_at < LEAVE_GRACE_S:
                    return
                self._edge_revealed = False
                self._edge_left_at = None
            if core.user32.IsWindowVisible(tray):
                core.user32.ShowWindow(tray, core.SW_HIDE)
                self.reposition()
                self.place_floating()
                self.update_floating_visibility()
                self.update_clock()
                self._log("鼠标离开任务栏，兜底收起")
            return

        self.sample_tray()
        if self.tray_retracted() or self._pointer_on_expanded_taskbar() \
                or at_edge:
            self._stuck_since = None
            return
        if self._stuck_since is None:
            self._stuck_since = now
            return
        if now - self._stuck_since < STUCK_GRACE_S:
            return
        # Start the existing crash-recovery watchdog before hiding the Shell.
        super().force_hide_tray()
        if self.forced_tray_hwnd:
            self._edge_revealed = False
            self._edge_left_at = None
            self.sample_tray()
            self.place_floating()
            self.update_floating_visibility()
            self.update_clock()
            self._log("Explorer 未自行收起任务栏，已切入触边兜底")

    def release_forced_tray(self):
        super().release_forced_tray()
        self._stuck_since = None
        self._edge_revealed = False
        self._edge_left_at = None

    def apply_tray_goal(self):
        # The base implementation verifies the system setting with bounded
        # retries. This existing call site is also our only edge sample.
        super().apply_tray_goal()
        if self.in_mvd and self.cfg["mvd_link"] and self.tray_goal is True:
            self.force_hide_tray()

    def floating_above_tray(self):
        # In the expanded state the floating pill is the sole rendered pill
        # after an embedded window has exceeded its original Tk canvas width.
        if self.embed_fallback and self.in_mvd and self.tray_out:
            return True
        return super().floating_above_tray()

    def quit(self):
        clock = self.clock
        self.clock = None
        if clock is not None:
            clock.close()
        super().quit()


if __name__ == "__main__":
    life_path = os.path.join(core._HERE, "suite_life.log")

    def life(message):
        with open(life_path, "a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    atexit.register(life, f"[edge] exit pid={os.getpid()}")
    if "--probe" in sys.argv:
        life(f"[edge] probe-ready pid={os.getpid()}")
        sys.exit(0)
    life(f"[edge] start pid={os.getpid()}")
    try:
        EdgeSuite().run()
        life("[edge] mainloop returned normally")
    except BaseException:
        with open(os.path.join(core._HERE, "suite_errors.log"), "a",
                  encoding="utf-8") as stream:
            stream.write("\n===== FATAL edge preview =====\n")
            stream.write(traceback.format_exc())
        sys.exit(1)
