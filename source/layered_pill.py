# -*- coding: utf-8 -*-
"""Small Win32 per-pixel-alpha window used for the floating desktop pills."""
import ctypes
import os
import queue
import threading
from ctypes import wintypes as w

from PIL import Image, ImageChops


# 独立的 DLL 包装器：不要改 ctypes.windll.user32 上共享的 argtypes。
# 主程序的热键线程也注册窗口类，但它使用另一种 WNDCLASSW 定义。
u = ctypes.WinDLL("user32", use_last_error=True)
g = ctypes.WinDLL("gdi32", use_last_error=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
BI_RGB = 0
DIB_RGB_COLORS = 0
WM_MOUSEMOVE = 0x0200
WM_MOUSELEAVE = 0x02A3
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
TME_LEAVE = 0x00000002
WM_APP_COMMAND = 0x8001


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte),
                ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", w.WORD),
                ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


class RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", ctypes.c_byte), ("rgbGreen", ctypes.c_byte),
                ("rgbRed", ctypes.c_byte), ("rgbReserved", ctypes.c_byte)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", RGBQUAD * 1)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HANDLE), ("hIcon", w.HANDLE),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HANDLE),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("dwFlags", w.DWORD),
                ("hwndTrack", w.HWND), ("dwHoverTime", w.DWORD)]


u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u.RegisterClassW.restype = w.WORD
u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, w.HWND, w.HANDLE, w.HANDLE,
                              ctypes.c_void_p]
u.CreateWindowExW.restype = w.HWND
u.DefWindowProcW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
u.DefWindowProcW.restype = ctypes.c_ssize_t
u.UpdateLayeredWindow.argtypes = [w.HWND, w.HDC, ctypes.POINTER(POINT),
                                  ctypes.POINTER(SIZE), w.HDC,
                                  ctypes.POINTER(POINT), w.DWORD,
                                  ctypes.POINTER(BLENDFUNCTION), w.DWORD]
u.UpdateLayeredWindow.restype = w.BOOL
u.GetDC.argtypes = [w.HWND]
u.GetDC.restype = w.HDC
u.ReleaseDC.argtypes = [w.HWND, w.HDC]
u.ReleaseDC.restype = ctypes.c_int
u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, w.UINT]
u.SetWindowPos.restype = w.BOOL
u.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
u.TrackMouseEvent.restype = w.BOOL
u.SetCapture.argtypes = [w.HWND]
u.SetCapture.restype = w.HWND
u.ReleaseCapture.restype = w.BOOL
u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
u.DestroyWindow.argtypes = [w.HWND]
u.PostMessageW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
u.PostMessageW.restype = w.BOOL
u.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
u.GetMessageW.restype = ctypes.c_int
u.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
u.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
u.DispatchMessageW.restype = ctypes.c_ssize_t
u.PostQuitMessage.argtypes = [ctypes.c_int]

g.CreateCompatibleDC.argtypes = [w.HDC]
g.CreateCompatibleDC.restype = w.HDC
g.DeleteDC.argtypes = [w.HDC]
g.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT,
                               ctypes.POINTER(ctypes.c_void_p), w.HANDLE,
                               w.DWORD]
g.CreateDIBSection.restype = w.HBITMAP
g.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
g.SelectObject.restype = w.HGDIOBJ
g.DeleteObject.argtypes = [w.HGDIOBJ]
k.GetModuleHandleW.argtypes = [w.LPCWSTR]
k.GetModuleHandleW.restype = w.HMODULE


class NativePill:
    """One layered HWND on its own message thread, away from Tk's C mainloop."""

    def __init__(self, on_mouse, on_error=None, click_through=False):
        self.on_mouse = on_mouse
        self.on_error = on_error
        self.click_through = click_through
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._startup_error = None
        self._closing = False
        self.hwnd = None
        self.visible = False
        self._thread = threading.Thread(target=self._run, name="VDSuitePill",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("原生药丸窗口启动超时")
        if self._startup_error is not None:
            raise self._startup_error

    def _run(self):
        try:
            self._create()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        msg = w.MSG()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            if self.on_error is not None:
                self.on_error(exc)
        finally:
            self._close_now()

    def _create(self):
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._class_name = f"VDSuiteAlphaPill_{os.getpid()}_{id(self):x}"
        hinst = k.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        if not u.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError()
        ex = (WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
              | WS_EX_TOPMOST)
        if self.click_through:
            ex |= WS_EX_TRANSPARENT
        self.hwnd = u.CreateWindowExW(ex, self._class_name, "VDSuiteAlphaPill",
                                       WS_POPUP, 0, 0, 1, 1, None, None,
                                       hinst, None)
        if not self.hwnd:
            raise ctypes.WinError()
        self.dc = g.CreateCompatibleDC(None)
        if not self.dc:
            raise ctypes.WinError()
        self.bitmap = None
        self.old_bitmap = None
        self.bits = ctypes.c_void_p()
        self.size = (0, 0)

    def _enqueue(self, action, *args):
        if self._closing or not self.hwnd:
            return
        self._commands.put((action, args))
        if not u.PostMessageW(self.hwnd, WM_APP_COMMAND, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def _do_commands(self):
        while True:
            try:
                action, args = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if action == "render":
                    self._render_now(*args)
                elif action == "show":
                    if not self.visible:
                        u.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
                        self.visible = True
                elif action == "hide":
                    if self.visible:
                        u.ShowWindow(self.hwnd, SW_HIDE)
                        self.visible = False
                elif action == "z":
                    u.SetWindowPos(self.hwnd, args[0], 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                elif action == "close":
                    self._close_now()
                    u.PostQuitMessage(0)
                    return
            except Exception as exc:
                if self.on_error is not None:
                    self.on_error(exc)

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if self.click_through and msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_APP_COMMAND:
                self._do_commands()
                return 0
            if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP,
                       WM_RBUTTONUP):
                x = ctypes.c_short(lp & 0xFFFF).value
                y = ctypes.c_short((lp >> 16) & 0xFFFF).value
                if msg == WM_MOUSEMOVE:
                    event = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT),
                                            TME_LEAVE, hwnd, 0)
                    u.TrackMouseEvent(ctypes.byref(event))
                    self.on_mouse("move", x, y)
                elif msg == WM_LBUTTONDOWN:
                    u.SetCapture(hwnd)
                    self.on_mouse("press", x, y)
                elif msg == WM_LBUTTONUP:
                    u.ReleaseCapture()
                    self.on_mouse("release", x, y)
                else:
                    self.on_mouse("menu", x, y)
                return 0
            if msg == WM_MOUSELEAVE:
                self.on_mouse("leave", -1, -1)
                return 0
        except Exception as exc:
            if self.on_error is not None:
                self.on_error(exc)
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    def _resize_bitmap(self, width, height):
        if (width, height) == self.size:
            return
        if self.bitmap:
            g.SelectObject(self.dc, self.old_bitmap)
            g.DeleteObject(self.bitmap)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down, same order as Pillow
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        self.bits = ctypes.c_void_p()
        self.bitmap = g.CreateDIBSection(self.dc, ctypes.byref(info),
                                         DIB_RGB_COLORS, ctypes.byref(self.bits),
                                         None, 0)
        if not self.bitmap or not self.bits.value:
            raise ctypes.WinError()
        self.old_bitmap = g.SelectObject(self.dc, self.bitmap)
        self.size = (width, height)

    def _render_now(self, rgba: Image.Image, x: int, y: int):
        if rgba.mode != "RGBA":
            rgba = rgba.convert("RGBA")
        width, height = rgba.size
        self._resize_bitmap(width, height)
        r, green, b, a = rgba.split()
        premult = Image.merge("RGBA", (ImageChops.multiply(r, a),
                                        ImageChops.multiply(green, a),
                                        ImageChops.multiply(b, a), a))
        data = premult.tobytes("raw", "BGRA")
        ctypes.memmove(self.bits, data, len(data))
        dst = POINT(x, y)
        size = SIZE(width, height)
        src = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        screen_dc = u.GetDC(None)
        try:
            if not u.UpdateLayeredWindow(self.hwnd, screen_dc,
                                         ctypes.byref(dst), ctypes.byref(size),
                                         self.dc, ctypes.byref(src), 0,
                                         ctypes.byref(blend), ULW_ALPHA):
                raise ctypes.WinError()
        finally:
            u.ReleaseDC(None, screen_dc)

    def show(self):
        self._enqueue("show")

    def hide(self):
        self._enqueue("hide")

    def set_z(self, after):
        self._enqueue("z", after)

    def render(self, rgba: Image.Image, x: int, y: int):
        self._enqueue("render", rgba, x, y)

    def close(self):
        if self._closing:
            return
        self._commands.put(("close", ()))
        self._closing = True
        if self.hwnd:
            u.PostMessageW(self.hwnd, WM_APP_COMMAND, 0, 0)

    def _close_now(self):
        if self.visible:
            u.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False
        if self.hwnd:
            u.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.bitmap:
            g.SelectObject(self.dc, self.old_bitmap)
            g.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.dc:
            g.DeleteDC(self.dc)
            self.dc = None
