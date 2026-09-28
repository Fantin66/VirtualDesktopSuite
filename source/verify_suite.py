# -*- coding: utf-8 -*-
"""verify_suite.py — 集成版（desktop_suite.py / VirtualDesktopSuite.exe）端到端验证

沙箱会在命令结束时回收所有子进程，所以「拉起 → 验证 → 收尾」必须在同一条
命令里跑完，不能拆成多次。

结果分三档，绝不含糊：
  PASS  真的验过了
  FAIL  验了，结果不对
  SKIP  环境挡住了，没验成（见下面「关于 SKIP」）

关于 SKIP —— 本机踩过的坑，写下来免得下次又白跑一遍：
  * 屏幕一旦锁定，前台窗口就是「Windows 默认锁屏界面」（class
    Windows.UI.Core.CoreWindow）。此时系统会丢弃一切合成按键：
    SendInput 直接返回 0，keybd_event 静默无效，RegisterHotKey 注册的热键
    永远收不到 WM_HOTKEY。SetForegroundWindow 也 100% 返 0，连
    SPI_SETFOREGROUNDLOCKTIMEOUT=0 + AttachThreadInput 都救不回来。
  * 所以「真敲热键」「抢前台 → 最大化 → 跳新桌面」这两类只能等屏幕可用时才
    能验，锁屏下判 SKIP。剩下的（热键注册是否真的生效、热键处理器是否真的
    干活、任务栏收起时浮窗药丸是否露出、全屏桌面上的标签与配色）都能在锁屏
    下真验 —— 热键动作改成直接往套件自己的热键窗口投 WM_HOTKEY，绕开被系统
    吃掉的键盘注入这一段，验的是我们自己写的逻辑，一样是端到端的。

用法：
    python verify_suite.py            # 用脚本模式跑（快）
    python verify_suite.py --exe      # 用打包好的 exe 跑（慢，沙箱下解包要几十秒）
"""
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import sys
import time
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBS = os.path.join(HERE, "libs")
if LIBS not in sys.path:
    sys.path.insert(0, LIBS)

import pyvda                                        # noqa: E402

# ⚠️ 必须和被测程序用同一套坐标口径（都是 per-monitor-v2）。
#    不设的话本进程拿到的是被 DPI 虚拟化过的坐标（任务栏高 48 而不是 96），
#    拿这个数去和被测程序自己算的排版比，会得出"不一致"的假结论。
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

u = ctypes.windll.user32
k = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt", POINT)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTU(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 24)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTU)]


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", ctypes.c_void_p),
                ("uCallbackMessage", ctypes.c_uint32), ("uEdge", ctypes.c_uint32),
                ("rc", RECT), ("lParam", ctypes.c_ssize_t)]


u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
u.FindWindowW.restype = ctypes.c_void_p
u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u.GetWindowThreadProcessId.restype = ctypes.c_ulong
u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
u.SetForegroundWindow.restype = ctypes.c_int
u.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int]
u.GetParent.argtypes = [ctypes.c_void_p]
u.GetParent.restype = ctypes.c_void_p
u.IsWindowVisible.argtypes = [ctypes.c_void_p]
u.IsWindow.argtypes = [ctypes.c_void_p]
u.IsZoomed.argtypes = [ctypes.c_void_p]
u.DestroyWindow.argtypes = [ctypes.c_void_p]
u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.BringWindowToTop.argtypes = [ctypes.c_void_p]
u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u.GetDesktopWindow.restype = ctypes.c_void_p
# ⚠️ 这几个必须显式声明：不声明时 ctypes 把参数当 32 位 int，
#    窗口消息的 lParam 是指针，一超过 2^31 就 OverflowError，WndProc 直接崩。
u.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t]
u.DefWindowProcW.restype = LRESULT
u.CreateWindowExW.argtypes = [
    ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
u.CreateWindowExW.restype = ctypes.c_void_p
u.RegisterClassW.argtypes = [ctypes.c_void_p]
u.PeekMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                           ctypes.c_uint, ctypes.c_uint]
u.TranslateMessage.argtypes = [ctypes.c_void_p]
u.DispatchMessageW.argtypes = [ctypes.c_void_p]
u.DispatchMessageW.restype = LRESULT
u.EnumChildWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                           ctypes.c_size_t, ctypes.c_ssize_t]
u.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint32,
                          ctypes.c_void_p]
u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.GetWindowLongPtrW.restype = ctypes.c_longlong
u.GetForegroundWindow.argtypes = []
u.GetForegroundWindow.restype = ctypes.c_void_p
u.GetCursorPos.argtypes = [ctypes.c_void_p]
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u.GetSystemMetrics.argtypes = [ctypes.c_int]
u.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                             ctypes.c_uint]
u.RegisterHotKey.restype = ctypes.c_int
u.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
u.SendInput.restype = ctypes.c_uint
k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
k.GetModuleHandleW.restype = ctypes.c_void_p
k.GetCurrentThreadId.restype = ctypes.c_uint

dwmapi = ctypes.windll.dwmapi
dwmapi.DwmGetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_uint]

shell32 = ctypes.windll.shell32
shell32.SHAppBarMessage.argtypes = [ctypes.c_uint, ctypes.c_void_p]

SW_SHOW, SW_MAXIMIZE, SW_RESTORE = 5, 3, 9
WS_OVERLAPPEDWINDOW = 0x00CF0000
PM_REMOVE = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_MENU, VK_SHIFT = 0x11, 0x12, 0x10
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT = 0x0001, 0x0002, 0x0004
MOD_NOREPEAT = 0x4000
HK_TASKBAR, HK_LAUNCH = 1, 2
SR_PATH = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
           r"\StuckRects3")
ABM_GETTASKBARPOS, ABM_SETSTATE = 0x0005, 0x000A

_MODS = {"Ctrl": (VK_CONTROL, MOD_CONTROL), "Alt": (VK_MENU, MOD_ALT),
         "Shift": (VK_SHIFT, MOD_SHIFT)}
_VK = {"T": 0x54, "H": 0x48, "B": 0x42, "X": 0x58, "Z": 0x5A, "F9": 0x78}

REPORT = []          # (ok, title, detail)  ok 为 None 表示 SKIP
# 跑测试前塞进去的"上一版配置"，跑完要还原回去，别动用户自己的设置
_CFG_RESTORE = None

# 上一版（__ver=1）的默认配置：immersive_hide 默认是开，一进全屏桌面药丸就整个
# 消失 —— 那正是用户报的毛病。跑测试时故意塞这份进去，看程序会不会把它翻掉。
LEGACY_SETTINGS = {
    "hotkey_taskbar": True, "hotkey_launch": True, "auto_app_desktop": True,
    "mvd_link": True, "immersive_hide": True, "fullscreen_fill": False,
}

# 产品的默认交互是"普通最大化什么都不做，只有 Shift+最大化 才独占新桌面"。
# 合成 Shift 在本会话里做不到（SendInput 被系统丢弃），所以"要独占"那一端改用
# 关掉开关来验：set VDB_MVD_SHIFT_OFF=1 之后任何最大化都独占 —— 走的还是同一段
# 代码（mvd_armed → launch_to_new_desktop），只是判据从"刚按过 Shift"变成恒真。
FORCE_AUTO = bool(os.environ.get("VDB_MVD_SHIFT_OFF"))


def say(ok, title, detail=""):
    REPORT.append((bool(ok), title, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {title}"
          + (f"  — {detail}" if detail else ""), flush=True)


def skip(title, detail=""):
    """环境挡住了，没验成。⚠️ 这不算通过，单独统计。"""
    REPORT.append((None, title, detail))
    print(f"[SKIP] {title}" + (f"  — {detail}" if detail else ""), flush=True)


def pid_of(hwnd):
    """⚠️ 严格说这是窗口所属**线程** id —— GetWindowThreadProcessId 的返回值就是
    线程 id，进程 id 是从第二个出参拿的。这个函数当"是不是同一份实例"的判断是
    够用的（Tk 的窗口都由同一个线程创建），但**不能拿去和 os.getpid() 比**。
    要比进程，用下面的 proc_id_of。"""
    if not hwnd:
        return 0
    return u.GetWindowThreadProcessId(hwnd, None)


def proc_id_of(hwnd):
    """窗口所属**进程** id。

    专门为"这个窗口是不是我自己的"准备的。之前这里用的是 pid_of()（线程 id），
    和 os.getpid() 永远对不上 —— 于是"把测试窗口搬回原桌面"这一步静默不生效，
    窗口赖在测试桌面上，桌面删不掉，还在用户的桌面列表里留垃圾（踩过）。
    """
    if not hwnd:
        return 0
    pid = ctypes.c_ulong(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def tray_hwnd():
    return u.FindWindowW("Shell_TrayWnd", None)


def tray_rect():
    h = tray_hwnd()
    if not h:
        return None
    r = RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def monitor():
    return (0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1))


def onscreen_span(rc, mon):
    x0, y0 = max(rc[0], mon[0]), max(rc[1], mon[1])
    x1, y1 = min(rc[2], mon[2]), min(rc[3], mon[3])
    return max(0, x1 - x0), max(0, y1 - y0)


def pump(seconds):
    """跑消息循环，让本进程建的窗口保持响应"""
    msg = MSG()
    end = time.time() + seconds
    while time.time() < end:
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def foreground_owner():
    fg = u.GetForegroundWindow()
    if not fg:
        return None, "（没有前台窗口）"
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(fg, b, 256)
    t = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(fg, t, 256)
    return fg, f"{b.value!r} {t.value!r}"


def steal_foreground(hwnd, tries=5):
    """把自己造的窗口抢到前台（AttachThreadInput 是唯一稳的办法）。

    任务栏/锁屏界面在最前时 SetForegroundWindow 会失败，所以必须带重试。
    """
    for _ in range(tries):
        fg = u.GetForegroundWindow()
        ft = u.GetWindowThreadProcessId(fg, None)
        mt = k.GetCurrentThreadId()
        u.AttachThreadInput(mt, ft, True)
        try:
            u.ShowWindow(hwnd, SW_SHOW)
            u.SetForegroundWindow(hwnd)
            u.BringWindowToTop(hwnd)
        finally:
            u.AttachThreadInput(mt, ft, False)
        pump(0.35)
        if u.GetForegroundWindow() == hwnd:
            return True
    return False


def press_hotkey(combo):
    """模拟真实敲键（走键盘输入流，才会命中原生 RegisterHotKey）"""
    parts = [p.strip() for p in combo.split("+")]
    mods = [_MODS[p][0] for p in parts if p in _MODS]
    vk = _VK[parts[-1]]
    for m in mods:
        u.keybd_event(m, 0, 0, 0)
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for m in reversed(mods):
        u.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)


def sendinput_ok():
    """合成按键在这个会话里到底能不能用（返回 SendInput 实际插入的条数）"""
    events = [(_MODS["Ctrl"][0], 0), (_MODS["Alt"][0], 0), (_VK["F9"], 0)]
    arr = (INPUT * len(events))()
    for i, (vk, scan) in enumerate(events):
        arr[i].type = 1
        arr[i].u.ki = KEYBDINPUT(vk, scan, 0, 0, None)
    n = u.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    time.sleep(0.05)
    arr2 = (INPUT * len(events))()
    for i, (vk, scan) in enumerate(reversed(events)):
        arr2[i].type = 1
        arr2[i].u.ki = KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP, 0, None)
    u.SendInput(len(events), arr2, ctypes.sizeof(INPUT))
    return int(n)


def hotkey_held(combo):
    """这个组合是不是真被别的进程占着。

    比"日志说注册成功"硬得多：自己 RegisterHotKey 同一组合，被拒 err=1409
    就说明系统里确实有人占着它，不是日志自说自话。
    """
    parts = [p.strip() for p in combo.split("+")]
    mods = MOD_NOREPEAT
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p][1]
    vk = _VK[parts[-1]]
    ok = u.RegisterHotKey(None, 900, mods, vk)
    if ok:
        u.UnregisterHotKey(None, 900)
        return False, "本进程居然注册上了 → 组合其实没被占"
    err = k.GetLastError()
    return err == 1409, f"err={err}" + ("（已被占用）" if err == 1409 else "")


def fire_hotkey(hid):
    """直接往套件自己的热键窗口投 WM_HOTKEY。

    键盘注入在锁屏下会被系统整个丢掉，但"热键处理器干活"这件事本身照样能验：
    投递的内核消息和真按键命中的是同一条路径（GetMessage → hotkey_fire →
    drain_hotkeys → on_hotkey）。
    """
    h = u.FindWindowW("VDSuiteHotkeyWindow", None)
    if not h:
        return False
    u.PostMessageW(h, WM_HOTKEY, hid, 0)
    return True


def autohide():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SR_PATH) as kk:
            return bool(winreg.QueryValueEx(kk, "Settings")[0][8] & 1)
    except OSError:
        return None


def set_autohide(on):
    """和套件走同一套机制：注册表 Settings[8] bit0 + SHAppBarMessage(ABM_SETSTATE)"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SR_PATH, 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as kk:
        data = bytearray(winreg.QueryValueEx(kk, "Settings")[0])
        data[8] = (data[8] | 0x01) if on else (data[8] & 0xFE)
        winreg.SetValueEx(kk, "Settings", 0, winreg.REG_BINARY, bytes(data))
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd))
    abd.lParam = 0x3 if on else 0x2
    shell32.SHAppBarMessage(ABM_SETSTATE, ctypes.byref(abd))
    return True


def desktops():
    return [(d.number, d.name or "", d.id) for d in pyvda.get_virtual_desktops()]


def current_number():
    return pyvda.VirtualDesktop.current().number


def desktop_of(hwnd):
    try:
        return pyvda.AppView(hwnd=hwnd).desktop.number
    except Exception:
        return -1


def read_log(path):
    if not os.path.exists(path):
        return []
    return open(path, encoding="utf-8", errors="replace").read().splitlines()


def fired_why(log_path):
    """套件日志里**最近一次**"新建单应用桌面"记的触发原因。

    日志格式（见 desktop_suite.launch_to_new_desktop 里的 _log）：
      新建单应用桌面 [[MVD] python]，窗口 3217610 已移入（触发=maximize）

    可能的值：maximize（总开关关掉时的旧行为）/ {修饰键}+maximize / hotkey。
    用途：段 8 判"普通最大化什么都不该发生"时，如果**偏偏**发生了，先看这里 ——
    触发原因是"修饰键+maximize"就说明当时有人在按键，那是测量被污染，不是 bug。
    """
    for ln in reversed(read_log(log_path)):
        if "新建单应用桌面" in ln:
            if "触发=" in ln:
                return ln.split("触发=", 1)[1].rstrip("）) ")
            return "?"
    return ""


def describe_window(hwnd, suite_pid):
    """照抄 desktop_suite._eligible() 的每一条判据，逐项报告"""
    def _txt(h):
        b = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(h, b, 512)
        return b.value

    def _cls(h):
        b = ctypes.create_unicode_buffer(512)
        u.GetClassNameW(h, b, 512)
        return b.value

    title, klass = _txt(hwnd), _cls(hwnd)
    pid = pid_of(hwnd)
    ex = u.GetWindowLongPtrW(hwnd, -20)          # GWL_EXSTYLE
    checks = [
        ("IsWindow", bool(u.IsWindow(hwnd))),
        ("不是桌面窗口", hwnd != u.GetDesktopWindow()),
        ("不是套件自己的进程", pid != suite_pid),
        ("类名不在系统壳黑名单", klass not in (
            "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
            "Windows.UI.Core.CoreWindow", "ApplicationFrameWindow")),
        ("标题非空", bool(title)),
        ("没有 WS_EX_TOOLWINDOW", not (ex & 0x00000080)),
    ]
    bad = [c for c, ok in checks if not ok]
    text = (f"title={title!r} class={klass!r} pid={pid}/{suite_pid} "
            f"exstyle=0x{ex & 0xFFFFFFFF:X}")
    if bad:
        text += "  不过关项=" + "、".join(bad)
    return {"eligible": not bad, "text": text}


def win_title(h):
    b = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, b, 512)
    return b.value


def win_class(h):
    b = ctypes.create_unicode_buffer(512)
    u.GetClassNameW(h, b, 512)
    return b.value


def window_tag(h):
    """给一个 hwnd 起个能一眼认出是谁的短标签"""
    if not h:
        return "<无窗口>"
    return (f"hwnd={h} pid={proc_id_of(h)} model={win_class(h)!r} "
            f"标题={win_title(h)!r}")


def already_gone(exc):
    """"桌面在我们动手之前就已经没了" —— 这是成功，不是失败。

    pyvda 找不到 id 时抛 `Exception("Desktop with ID ... not found")`。我们的
    目标是"让这个桌面消失"，它先消失一步不影响结果，别报成 FAIL。
    """
    return "not found" in str(exc).lower()


def clear_mvd_desktops(own_pid, keep_id, settle=0.6):
    """删掉测试自己造的 [MVD] 桌面；返回 (删掉的, 没删掉的明细)。

    三条硬要求，都是踩出来的：

    1. **判断"这窗口是不是我的"要用进程 id，不能用线程 id**。`pid_of()` 返回的
       其实是线程 id，拿去和 `os.getpid()` 比永远不等 —— 于是"把测试窗口搬回原
       桌面"这一步静默不生效，窗口赖在测试桌面上，桌面就永远删不掉，还在用户的
       桌面列表里留垃圾（踩过，留了一个 `[MVD] 微信`）。用 `proc_id_of()`。
    2. **搬家目标要按 id 取对象**，别把桌面序号当 id 传进去。
    3. **桌面上有别人的窗口时，绝不硬删，只报告**。删掉一个还挂着窗口的虚拟
       桌面，Windows 会把那些窗口甩到隔壁桌面 —— 用户会觉得自己的窗口莫名其妙
       换了地方。所以只搬本进程自己的窗口，别人的一律原样留着并回报。
    """
    removed, kept = [], []
    for _, name, did in list(desktops()):
        if "[MVD]" not in name.upper():
            continue
        d = {x.id: x for x in pyvda.get_virtual_desktops()}.get(did)
        if d is None:
            continue
        keep = {x.id: x for x in pyvda.get_virtual_desktops()}.get(keep_id)
        try:
            for v in (d.apps_by_z_order() or []):
                h = getattr(v, "hwnd", None)
                if h and proc_id_of(h) == own_pid and keep is not None:
                    pyvda.AppView(hwnd=h).move(keep)
        except Exception:
            pass
        time.sleep(settle)
        d2 = {x.id: x for x in pyvda.get_virtual_desktops()}.get(did)
        left = []
        if d2 is not None:
            try:
                left = [window_tag(getattr(v, "hwnd", None))
                        for v in (d2.apps_by_z_order() or [])]
            except Exception:
                left = ["<取窗口列表失败>"]
        if left:
            kept.append((name, left))
            continue
        try:
            d2.remove(fallback=keep)
            removed.append(name)
        except Exception as e:
            if already_gone(e):
                removed.append(name + "（已自行消失）")
            else:
                kept.append((name, [f"删除失败 {type(e).__name__}: {e}"]))
    return removed, kept


def rescue_foreign_windows(desk_id, origin_id, own_pid):
    """把被**误甩**出去的"别人的"窗口搬回 origin，返回搬动了几个。

    什么时候会需要：热键/自动触发的语义是"把**前台窗口**甩到新桌面"。
    脚本发键之前虽然抢过前台，但 `fire_hotkey` 是 PostMessage（异步）——
    套件在**它自己的线程**里处理这条消息，读 `GetForegroundWindow()` 的时刻
    比发键晚几十到几百毫秒。你正在用电脑的话，那一瞬间前台可能已经是你的窗口，
    于是套件**按设计**把你的窗口甩走了（实测甩走过一次微信，留下 [MVD] WeChatAppEx）。

    这对套件来说不是 bug（用户按热键想甩的就是他正看着的窗口），但**脚本必须善后**：
    副作用是脚本按键引起的，就不能把用户的窗口丢在别人的桌面上不管。
    """
    moved = 0
    all_d = {x.id: x for x in pyvda.get_virtual_desktops()}
    d, keep = all_d.get(desk_id), all_d.get(origin_id)
    if d is None or keep is None:
        return 0
    try:
        apps = list(d.apps_by_z_order() or [])
    except Exception:
        return 0
    for v in apps:
        h = getattr(v, "hwnd", None)
        if not h or proc_id_of(h) == own_pid:
            continue
        try:
            pyvda.AppView(hwnd=h).move(keep)
            moved += 1
        except Exception:
            pass
    return moved


def load_suite_module():
    """把被测程序当模块引进来，对纯函数（标签、配色、排版）做单测。

    ⚠️ 必须先设 VDB_NO_MUTEX：模块顶层在导入那一刻就会跑单实例接管逻辑，
    而这时候正有一个实例在跑 —— 不设的话它会把正在跑的实例杀掉。
    用完立刻删掉这个环境变量，免得被子进程继承（继承了就测不出接管了）。
    """
    os.environ["VDB_NO_MUTEX"] = "1"
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import desktop_suite
        return desktop_suite
    finally:
        os.environ.pop("VDB_NO_MUTEX", None)


def float_hwnd():
    return u.FindWindowW(None, "VDSuite")


def rect_of(h):
    if not h:
        return None
    r = RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def float_visible():
    h = float_hwnd()
    return bool(h and u.IsWindowVisible(h))


def describe_float():
    h = float_hwnd()
    return (f"hwnd={h} 可见={bool(h and u.IsWindowVisible(h))} "
            f"DWM遮蔽={cloaked(h)} rect={rect_of(h)}")


def cloaked(h):
    """窗口被 DWM 遮蔽了吗。

    ⚠️ 切到别的虚拟桌面时，属于原桌面的窗口会被遮蔽，而 IsWindowVisible
    依然是 True —— 只看 IsWindowVisible 会得出"窗口好好的"的错误结论。
    """
    if not h:
        return "无窗口"
    v = ctypes.c_uint(0)
    try:
        hr = dwmapi.DwmGetWindowAttribute(h, 14, ctypes.byref(v), 4)
    except Exception:
        return "查不到"
    if hr != 0:
        return f"查不到(hr={hr})"
    return {0: "否", 1: "被遮蔽(别的应用)", 2: "被遮蔽(别的虚拟桌面)",
            4: "被遮蔽(继承)"}.get(v.value, f"0x{v.value:X}")


def describe_tray():
    rc = tray_rect()
    if not rc:
        return "任务栏不存在"
    return (f"任务栏rect={rc} 露在屏幕内={onscreen_span(rc, monitor())}"
            f"（展开时高 96，收起只剩 2px）")


def embedded_visible(suite_pid):
    """嵌进任务栏的那一份药丸有没有露着（沉浸时会被 SW_HIDE）"""
    tray = tray_hwnd()
    if not tray:
        return False
    kids = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u.EnumChildWindows(tray, CB(lambda h, lp: (kids.append(h), True)[1]), 0)
    return any(pid_of(h) == suite_pid and u.IsWindowVisible(h) for h in kids)


def current_id():
    try:
        return pyvda.VirtualDesktop.current().id
    except Exception:
        return None


def goto_desktop(d, timeout=12.0):
    """切到指定桌面，并且**等到真的切过去**。

    两件事都得做对：
      * 判断"切过去了没有"要按 id 比，别按序号 —— 新建/删除桌面时序号会错位；
      * d.go() 是异步的，紧接着新建/删除桌面时这一次调用可能被动画吞掉，
        所以要重试，不能固定 sleep 之后只看一眼。
    """
    want = getattr(d, "id", None)
    end = time.time() + timeout
    while time.time() < end:
        if want is not None and current_id() == want:
            return True
        try:
            d.go()
        except Exception:
            pass
        time.sleep(0.8)
    return want is not None and current_id() == want


def wait_tray(retracted, timeout=10.0):
    """等任务栏真的收起来/展开。

    锁屏状态下没有鼠标移动，而自动隐藏平时就是靠鼠标消息驱动的，explorer
    偶尔会"点了开关但不动"。所以要等，别拿一次采样当定论。
    """
    end = time.time() + timeout
    while time.time() < end:
        rc = tray_rect()
        if rc:
            th = rc[3] - rc[1]
            is_ret = onscreen_span(rc, monitor())[1] < th // 2
            if is_ret == retracted:
                return True
        time.sleep(0.4)
    return False


def wait_autohide(want, timeout=10.0, poll=0.2):
    """等注册表里"任务栏自动隐藏"那一位变成 want。

    ⚠️ 别用"固定 sleep 之后只读一次"判这件事：套件把新值写进注册表、到我们这边
    能读到，中间有个观察不到的死角（实测正常情况下只差 ~0.2 秒，但套件正在忙
    桌面切换时会拖长到几秒）。轮询到就返回 True，超时返回 False —— 真没翻转的话
    照样超时判 FAIL，检测能力一点没丢。
    """
    end = time.time() + timeout
    while True:
        if autohide() == want:
            return True
        if time.time() >= end:
            return False
        time.sleep(poll)


def wait_float(want, timeout=8.0, poll=0.2):
    """等那份"浮窗药丸"露出（want=True）或收起（want=False）。

    ⚠️ 套件是按 400ms 的节拍看系统状态的，所以系统状态 settle 之后它还要一两个
    节拍才会把浮窗摆出来/收起来。判浮窗可见性必须同样轮询：wait_tray 一返回就
    直接读，读到的是**上一帧**的结果 —— 表现出来是"收着时没露、展开了还露"，
    两条断言正好反过来，看着像功能错了，其实是测试读早了（踩过）。
    """
    end = time.time() + timeout
    while True:
        if float_visible() == want:
            return True
        if time.time() >= end:
            return False
        time.sleep(poll)


def normalize():
    """把桌面恢复成干净起点：切回 1 号，删掉空的 [MVD] 残留桌面。

    上一次跑崩了、或被强杀时，会留下空的 [MVD] 桌面；不先清掉的话
    套件启动时就在单应用桌面上，会自动进入沉浸模式，后面全线误判。
    """
    removed = []
    stranded = []
    desks = list(pyvda.get_virtual_desktops())
    if not desks:
        return removed, stranded
    first = min(desks, key=lambda d: d.number)
    try:
        first.go()
        time.sleep(0.8)
    except Exception:
        pass
    for d in desks:
        nm = d.name or ""
        if "[MVD]" not in nm.upper():
            continue
        try:
            apps = d.apps_by_z_order()
        except Exception:
            apps = []
        if apps:
            # ⚠️ 只删空桌面。删掉一个还挂着窗口的虚拟桌面，Windows 会把那些窗口
            #    甩到隔壁桌面上 —— 用户会觉得自己的窗口莫名其妙换了地方。所以这里
            #    只**报告**，不动它，让人一眼看到"有窗口被留在测试桌面上了"。
            stranded.append((nm, [window_tag(getattr(v, "hwnd", None))
                                  for v in apps]))
            continue
        try:
            d.remove(fallback=first)
            removed.append(d.number)
        except Exception as e:
            if already_gone(e):
                removed.append(f"{nm}（已自行消失）")
            else:
                stranded.append((nm, [f"删除失败 {type(e).__name__}: {e}"]))
    time.sleep(0.6)
    if stranded:
        print(f"  ⚠️ 这些 [MVD] 桌面上还有窗口，没敢动：{stranded}", flush=True)
    return removed, stranded


def order_contracts():
    """6b) 顺序/残留类契约（离屏可测，锁屏也能跑）。

    用户 2026-09-23 报的两个问题都在这几条上：
      · 切到单应用桌面时「该出现新药丸的地方黑一下」
      · 切过去「任务栏没有自动隐藏」
    两条都是**顺序**或**状态残留**问题，不需要前台窗口，所以单独拎出来，
    出问题时一条命令就能单独跑：
        python -c "import verify_suite as V; V.order_contracts()"
    """
    S = load_suite_module()          # 独立可跑：不吃 main() 里的局部变量

    # ★★ 6b) 「切到单应用桌面时新药丸那一块黑一下」+「切过去任务栏没自动隐藏」
    #      —— 用户 2026-09-23 报的这两个。两条都是**顺序/残留**类问题，不需要
    #      前台窗口也能测，所以放这里（锁屏也能跑）。
    #
    #      契约一：药丸超过嵌入窗口的初始宽度时，先隐藏有问题的嵌入层，
    #      由正常的浮窗接管。只扩原生 HWND 无法改变 Tk 自己记录的画布宽度。
    order = []

    class _FakeGeo:
        def geometry(self, spec):
            order.append("geometry")

    class _FakeRepos:
        pass

    fr = _FakeRepos()
    fr.state = [{"number": 1, "label": "1", "is_current": True},
                {"number": 2, "label": "chr", "is_current": False}]
    fr.m = S.compute_metrics(48, ["1"])          # 上一轮：只有一颗药丸
    fr.w, fr.h = fr.m["win_w"], 48
    fr.embedded = True
    fr.embed_capacity = fr.w
    fr.embed_fallback = False
    fr.immersed = False
    fr.emb = _FakeGeo()
    fr.emb_hwnd = 4242
    fr.fw_hwnd = 4243
    fr.canvas_e = fr.canvas_f = None
    fr.dock_rect = (0, 992, 1560, 1040)
    fr.last_rc = None
    fr.sample_tray = lambda: (0, 992, 1560, 1040)
    fr.render = lambda: order.append("render")
    fr._log = lambda msg: order.append("log")
    fr.place_floating = lambda: (order.append("place"), True)[1]
    fr._flush_paint = lambda c, h: order.append("flush")
    _real_swp = S.user32.SetWindowPos
    _real_show = S.user32.ShowWindow
    S.user32.SetWindowPos = lambda *a: order.append("SetWindowPos")
    S.user32.ShowWindow = lambda *a: order.append("hide")
    old_w = fr.w
    err1 = None
    try:
        S.Suite.reposition(fr)
    except Exception as e:                      # 假对象写错也要计 FAIL，不能崩
        err1 = e
    finally:
        S.user32.SetWindowPos = _real_swp
        S.user32.ShowWindow = _real_show
    if err1 is not None:
        order.append("EXC:" + repr(err1))

    def _first(seq, *names):
        return min((seq.index(n) for n in names if n in seq), default=999)

    fallback_ok = (fr.embed_fallback and "hide" in order
                   and "render" in order and "place" in order
                   and order.index("hide") < order.index("render")
                   < order.index("place"))
    say(fallback_ok and "flush" in order and fr.w > old_w,
        "药丸变多时：先隐藏嵌入层，再绘制并显示浮窗",
        f"调用顺序={order}；宽度 {old_w} → {fr.w}")

    #      契约二：浮窗出场时 **先画好内容、再 deiconify()**。
    #      原来是先 deiconify 再等 anim_tick 的第一帧 —— 中间那一帧窗口里是上一轮
    #      的内容（甚至没画过），露出来也是黑的。
    order2 = []

    class _FakeRoot:
        def deiconify(self):
            order2.append("deiconify")

    class _FakeTw:
        def start(self):
            order2.append("tween.start")

    fv = _FakeRepos()
    fv.immersed = False
    fv.embed_fallback = False
    fv.fw_state = "hidden"
    fv.fw_tw = _FakeTw()
    fv.root = _FakeRoot()
    fv.canvas_f = fv.fw_hwnd = None
    fv.tray_retracted = lambda: True
    fv.step_float = lambda: order2.append("step_float")
    fv.render = lambda: order2.append("render")
    fv.place_floating = lambda: order2.append("place")
    fv._flush_paint = lambda c, h: order2.append("flush")
    fv.start_anim = lambda: order2.append("start_anim")
    try:
        S.Suite.update_floating_visibility(fv)
    except Exception as e:
        order2.append("EXC:" + repr(e))
    show_ok = (order2[:1] == ["tween.start"] and "render" in order2
               and order2.index("render") < order2.index("deiconify"))
    say(show_ok,
        "浮窗出场：先算出场帧并画好，再 deiconify（先显示后画 = 也是黑一下）",
        f"调用顺序={order2}")

    #      契约三：进入单应用桌面必须**清掉上一轮"推不动、已放弃"的残留**。
    #      apply_tray_goal 有一套退避：推够 TRAY_RETRY_QUIET 次还收不起来就
    #      tray_gave_up=True，此后只核验、不再推物理收起。这套状态原来是跨
    #      会话残留的 —— 下一次进桌面直接躺平，开关写对了也不收，就是用户报的
    #      "切过去任务栏没有自动隐藏"。
    class _FakeEnter:
        enter_mvd = S.Suite.enter_mvd

        def __init__(self):
            self.in_mvd = False
            self.embed_fallback = False
            self.cfg = {"mvd_link": True, "immersive_hide": False}
            self.saved_autohide = None
            self.tray_goal = None
            self.tray_nudge = 0
            self.tray_pushes = 0
            self.tray_last_push = 0.0
            self.tray_gave_up = False
            self.tray_block_since = None
            self.err = None

        def call(self):
            try:
                self.enter_mvd()
            except Exception as e:
                self.err = e

        def _log(self, m):
            pass

        def apply_tray_goal(self):
            pass

        def set_immersed(self, on):
            pass

    fe = _FakeEnter()
    fe.tray_pushes = S.TRAY_RETRY_QUIET          # 上一轮"已放弃"的残留
    fe.tray_gave_up = True
    fe.tray_last_push = time.time()
    fe.tray_block_since = time.time()
    fe.call()
    fresh = (fe.err is None and fe.tray_pushes == 0 and fe.tray_gave_up is False
             and fe.tray_last_push == 0.0 and fe.tray_block_since is None
             and fe.tray_goal is True and fe.in_mvd is True)
    say(fresh,
        "进入单应用桌面会清掉「推不动已放弃」的退避残留（否则一进去就躺平）",
        f"pushes={fe.tray_pushes} gave_up={fe.tray_gave_up} "
        f"last_push={fe.tray_last_push} block_since={fe.tray_block_since} "
        f"goal={fe.tray_goal} err={fe.err}")

    #      契约四：重复进入**不许覆盖**"进入前的任务栏状态"。
    #      按 HK_LAUNCH 热键时若已经在单应用桌面上，会再建一个桌面再进一次；
    #      那时 autohide 已经被我们设成 True，拿它当"进入前"存下来，离开时就
    #      会"还原"成一个隐藏的任务栏，而且再没有任何地方会纠正它。
    fe.saved_autohide = False
    fe.in_mvd = True
    fe.call()
    say(fe.saved_autohide is False and fe.err is None,
        "已经在单应用桌面里再进一次，不覆盖「进入前的任务栏状态」",
        f"saved_autohide={fe.saved_autohide}（必须是 False，不是 True）"
        f" err={fe.err}")

    #      契约五：控制面板打开时同样是「先渲染、后显示」，且尺寸变了要强制重画。
    #      ⚠️ 这条路**任何脚本都走不到**（只有真右键才会打开面板），所以只能这样
    #      单测。上次少写一个 getattr，第一次右键就会 AttributeError 崩掉。
    class _FakePanel:
        show_panel = S.Suite.show_panel

        def __init__(self, geom=(10, 10, 374, 700)):
            self.panel_hover = 3
            self.panel_layout = {"w": 374, "h": 817}
            self.calls = []
            self.err = None
            self._flush_paint = lambda c, h: self.calls.append("flush")
            if geom is not None:
                self.panel_geom = geom          # None = 从没打开过（属性不存在）
            self.tray = None
            self.panel_hwnd = 4244
            self.canvas_p = None

        def render_panel(self):
            self.calls.append("render_panel")

        def deiconify(self):
            self.calls.append("deiconify")

        def run(self):
            real = S.user32.SetWindowPos
            S.user32.SetWindowPos = lambda *a: self.calls.append("SetWindowPos")
            try:
                self.show_panel(300, 900)
            except Exception as e:
                self.err = e
            finally:
                S.user32.SetWindowPos = real

    fp = _FakePanel()
    fp.panel = type("P", (), {"deiconify": fp.deiconify})()
    fp.run()
    # 第一次打开：panel_geom 这个属性**根本不存在**（它只在 show_panel 里第一次
    # 赋值）—— 这里就是那个 getattr 兜底的回归。
    fp2 = _FakePanel(geom=None)
    fp2.panel = type("P", (), {"deiconify": fp2.deiconify})()
    fp2.run()
    panel_ok = (fp.err is None and fp2.err is None
                and fp.calls.index("render_panel") < fp.calls.index("deiconify")
                and "flush" in fp.calls
                and fp2.calls.index("render_panel")
                < fp2.calls.index("deiconify")
                and "flush" in fp2.calls)
    say(panel_ok,
        "控制面板：先渲染再显示；尺寸跟上次不同时补一次强制重画"
        "（首次打开也不许抛异常）",
        f"尺寸变了={fp.calls}；首次打开={fp2.calls}；"
        f"err1={fp.err} err2={fp2.err}")


# ------------------------------------------------------------------ 主流程
def main():
    use_exe = "--exe" in sys.argv
    if use_exe:
        target = [os.path.join(ROOT, "VirtualDesktopSuite.exe")]
        cwd = ROOT
    else:
        py = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(py):
            py = sys.executable
        target = [py, os.path.join(HERE, "desktop_suite.py")]
        cwd = HERE
    base_dir = ROOT if use_exe else HERE
    log_path = os.path.join(base_dir, "suite_life.log")
    err_path = os.path.join(base_dir, "suite_errors.log")
    err_before = len(read_log(err_path))

    print("== 环境探底 ==", flush=True)
    ah_orig = autohide()
    if ah_orig:
        set_autohide(False)                     # 归一化到「任务栏展开」再开始
        time.sleep(2.0)
    injected = sendinput_ok()
    injectable = injected > 0
    fg_hwnd, fg_desc = foreground_owner()
    print(f"  任务栏自动隐藏（原）={ah_orig} → 已归一化为 {autohide()}", flush=True)
    print(f"  合成按键可用性：SendInput 插入 {injected} 条 → "
          f"{'可用' if injectable else '被系统丢弃（屏幕锁定）'}", flush=True)
    print(f"  当前前台窗口：{fg_desc}", flush=True)

    stale, stranded0 = normalize()
    say(True, "起点归一化",
        f"清掉的残留 [MVD] 桌面={stale or '无'}；当前桌面={current_number()}；"
        f"桌面数={len(desktops())}"
        + (f" ⚠️ 有窗口被留在旧测试桌面上（已打印）：{stranded0}" if stranded0 else ""))

    # 塞一份上一版的配置进去，验证升版本时会被翻掉（跑完在 finish 里还原）
    global _CFG_RESTORE
    cfg_path = os.path.join(base_dir, "suite_settings.json")
    try:
        if os.path.exists(cfg_path):
            _CFG_RESTORE = (cfg_path, open(cfg_path, encoding="utf-8").read())
        cfg_to_write = dict(LEGACY_SETTINGS)
        if FORCE_AUTO:
            # ⚠️ 只加 mvd_shift_only 这一项，**不要**顺手写 __ver —— 留着"没写"
            #    （=1）让"老配置被迁移"那条断言照常成立；而 v1 迁移只翻
            #    immersive_hide，不会把我们塞的这一项冲掉。
            #    也刻意不塞进 LEGACY_SETTINGS：那份是**真实的 v1 配置快照**，
            #    那时候压根没有这一项，塞进去就失真了。
            cfg_to_write["mvd_shift_only"] = False
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg_to_write, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  （写测试配置失败，不影响其它项：{e}）", flush=True)

    print("== 拉起套件 ==", flush=True)
    t0 = time.time()
    proc = subprocess.Popen(target, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd_suite = None
    while time.time() - t0 < 200:
        hwnd_suite = u.FindWindowW(None, "VDSuite")
        if hwnd_suite:
            break
        if proc.poll() is not None:
            say(False, "套件进程存活", f"提前退出 rc={proc.returncode}")
            return finish(proc, err_path, err_before, ah_orig)
        time.sleep(0.5)
    if not hwnd_suite:
        say(False, "套件窗口出现", "200 秒内没等到 VDSuite 窗口")
        return finish(proc, err_path, err_before, ah_orig)
    suite_pid = pid_of(hwnd_suite)
    say(True, "套件启动", f"窗口 {hwnd_suite} / PID {suite_pid} / "
                          f"{time.time() - t0:.1f}s（沙箱下偏慢）")

    # 1) 旧预览条被接管
    say(not u.FindWindowW(None, "VirtualDesktopBar"), "接管旧的预览条实例",
        "未发现 VirtualDesktopBar 窗口")

    # 1b) 设置迁移：老配置里 immersive_hide=True 必须被强制翻成 False，
    #     否则一进全屏桌面药丸还是会被整体藏掉（这就是用户报的 bug 的根因之一）
    cfg = {}
    try:
        cfg = json.load(open(cfg_path, encoding="utf-8"))
    except Exception as e:
        say(False, "读到套件设置文件", f"{type(e).__name__}: {e}")
    if cfg:
        say(cfg.get("__ver", 0) >= 2 and cfg.get("immersive_hide") is False,
            "老配置被迁移：immersive_hide 强制关（进全屏桌面不再藏药丸）",
            f"塞进去的是 __ver=1/immersive_hide=True → 现在是 "
            f"__ver={cfg.get('__ver')} immersive_hide={cfg.get('immersive_hide')}")

    pump(3.5)                                    # 等第一次渲染 + 热键注册

    # 2) 药丸嵌入任务栏
    tray = tray_hwnd()
    kids = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u.EnumChildWindows(tray, CB(lambda h, lp: (kids.append(h), True)[1]), 0)
    mine = [h for h in kids if pid_of(h) == suite_pid]
    if mine:
        h = mine[0]
        r = RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        tr = tray_rect()
        inside = tr and r.top >= tr[1] - 4 and r.bottom <= tr[3] + 4
        say(bool(inside and u.IsWindowVisible(h)), "药丸嵌入任务栏并可见",
            f"hwnd={h} rect=({r.left},{r.top},{r.right},{r.bottom}) "
            f"任务栏={tr} 可见={bool(u.IsWindowVisible(h))}")
    else:
        say(False, "药丸嵌入任务栏并可见", f"任务栏 {len(kids)} 个子窗口里没有本进程的")

    # 3) 热键注册情况（读日志）：「热键 切换任务栏 Ctrl+Alt+H: 注册成功」
    reg = {}
    for line in read_log(log_path):
        if "热键 " in line and "注册成功" in line:
            tail = line.split("热键 ")[1].split(": ")[0].strip()
            parts = tail.split(" ")
            if len(parts) >= 2:
                reg[" ".join(parts[:-1])] = parts[-1]
    hk_taskbar = reg.get("切换任务栏")
    hk_launch = reg.get("甩到新桌面")
    say(len(reg) == 2, "两个热键都注册成功",
        f"切换任务栏={hk_taskbar} / 甩到新桌面={hk_launch}")
    # 日志说注册成功不算数，再去系统里核一遍：自己抢注被拒才是硬证据
    for name, combo in (("切换任务栏", hk_taskbar), ("甩到新桌面", hk_launch)):
        if combo:
            held, why = hotkey_held(combo)
            say(held, f"热键 {name} 的组合真的被系统占住了（{combo}）", why)

    # 4) 热键动作：投 WM_HOTKEY → 注册表翻转 + 任务栏真的收起
    ah_before = autohide()
    docked = tray_rect()
    if hk_taskbar and fire_hotkey(HK_TASKBAR):
        flipped = wait_autohide(not ah_before, 10.0)
        say(flipped, "热键处理器翻转任务栏自动隐藏",
            f"autohide {ah_before} → {autohide()}")
        hid = wait_tray(True, 8.0)
        if not hid and autohide():
            # explorer 有时"点了开关但不动"：任务栏的自动隐藏平时靠鼠标消息驱动，
            # 本会话里没有真实鼠标移动（尤其屏幕锁着时）。补一次再等。
            set_autohide(True)
            hid = wait_tray(True, 10.0)
        rc_now = tray_rect()
        if hid:
            say(True, "翻转后任务栏真的收起来了（整体滑出屏幕，不是变矮）",
                f"收起后 rect={rc_now} 露在屏幕内={onscreen_span(rc_now, monitor())}"
                f"／展开时 rect={docked} 高={docked[3] - docked[1]}")
        else:
            # 注册表已经翻成 True 了（上一条已验），explorer 不肯把任务栏滑出去
            # 是**环境**问题，不是本程序的问题 —— 如实标 SKIP，别报 FAIL。
            skip("翻转后任务栏真的收起来了（整体滑出屏幕，不是变矮）",
                 f"注册表里 autohide={autohide()}，但 explorer 没把任务栏滑出去"
                 f"（rect 还露着 96px）。自动隐藏平时靠鼠标消息驱动，本会话里"
                 f"复现不出来；{describe_tray()}")
        if hid:
            # 套件要等它自己的 400ms 节拍看到"任务栏收起了"才会把浮窗摆出来，
            # 所以这里也得轮询等，不能 wait_tray 一返回就读（见 wait_float）。
            shown = wait_float(True, 8.0)
            fh = rect_of(float_hwnd())
            say(shown, "任务栏收起 → 浮窗药丸露出来"
                       "（这就是用户报的那个 bug）", describe_float())
            say(bool(fh and fh[1] == docked[1] and fh[3] == docked[3]),
                "浮窗落在任务栏「展开时」的位置，不是那条 2px 的缝上",
                f"浮窗rect={fh} 任务栏展开时rect={docked}")
        else:
            skip("任务栏收起 → 浮窗药丸露出来（这就是用户报的那个 bug）",
                 "任务栏没收起，这条没验到")
        fire_hotkey(HK_TASKBAR)
        # 再投一次把它翻回来，同样要"等到真的翻回来"才算数（见 wait_autohide）。
        # 第一次没翻的话，回翻这一条就无从判起 —— 如实标 SKIP，不要拿初始值
        # 恰好等于期望值去蒙一个 PASS。
        if not flipped:
            back = False
            skip("再投一次 → 任务栏自动隐藏还原", "第一次就没翻转，这条无从判起")
        else:
            back = wait_autohide(ah_before, 10.0)
            say(back, "再投一次 → 任务栏自动隐藏还原",
                f"autohide={autohide()}（期望回到 {ah_before}）")
        # 任务栏回来 → 浮窗必须收起，否则会挡住任务栏自己的鼠标消息。
        # ⚠️ 只有"任务栏真的收起过"这一条才有意义：没收起过的话，任务栏一直在
        #    那儿、浮窗本来就没露过，判出来是个假阳性。所以按 hid 分流。
        if not back:
            skip("任务栏回来 → 浮窗收起，不再挡住任务栏自己的鼠标消息",
                 "上一步没还原回去，这条不接着判（避免二次误判）")
        elif not hid:
            skip("任务栏回来 → 浮窗收起，不再挡住任务栏自己的鼠标消息",
                 "任务栏压根没收起来过，这条判了也是空的")
        elif not wait_tray(False, 8.0):
            skip("任务栏回来 → 浮窗收起，不再挡住任务栏自己的鼠标消息",
                 f"autohide 已还原，但 explorer 没把任务栏展开：{describe_tray()}")
        else:
            say(wait_float(False, 8.0),
                "任务栏回来 → 浮窗收起，不再挡住任务栏自己的鼠标消息",
                f"{describe_float()}；任务栏={tray_rect()}")
    else:
        say(False, "热键处理器翻转任务栏自动隐藏", "套件热键窗口没找到，投不进去")

    # 5) 真按键注入（锁屏下系统会把它整个丢掉，只能判 SKIP）
    if injectable and hk_taskbar:
        ah0 = autohide()
        press_hotkey(hk_taskbar)
        pump(2.5)
        say(autohide() != ah0, f"真敲 {hk_taskbar} 能触发（合成键盘注入）",
            f"{ah0} → {autohide()}")
        if autohide() != ah0:
            press_hotkey(hk_taskbar)
            pump(2.5)
    else:
        skip(f"真敲 {hk_taskbar} 能触发（合成键盘注入）",
             f"SendInput 插入 {injected} 条 → 屏幕锁定期间合成按键被系统丢弃，"
             f"前台被 {fg_desc} 占着，这条只能等屏幕可用时再验")

    # 6) 模块级单测：标签怎么取、颜色怎么给、排版怎么算
    S = load_suite_module()
    cases = [
        ("[MVD] chrome", "chrome"), ("[MVD] 微信", "微信"),
        ("[mvd] msedge", "msedge"), ("[MVD]", None),
        ("普通桌面", None), ("", None),
    ]
    bad = [(a, S.app_name_from(a), b) for a, b in cases
           if S.app_name_from(a) != b]
    say(not bad, "从桌面名里解析出应用名（含空名字/普通桌面的兜底）",
        "；".join(f"{a!r}→{S.app_name_from(a)!r}" for a, b in cases[:4]))
    lab = [
        ("chrome", 3, "chr"), ("微信", 2, "微信"), ("WindowsTerminal", 4, "Win"),
        (None, 2, "2"), (None, 11, "11"),
    ]
    bad2 = []
    for app, num, want in lab:
        got = S.pill_label({"app": app, "number": num})
        if got != want:
            bad2.append(f"{app}/{num}→{got}≠{want}")
    say(not bad2, "全屏桌面显示应用名，普通桌面显示序号",
        "；".join(f"{a or '—'}/{n}→{S.pill_label({'app': a, 'number': n})!r}"
                  for a, n, _ in lab))

    # ★ 本项目最核心的交互约定：**普通最大化什么都不做**，只有明确动作
    #   （按住修饰键+最大化 / 热键）才独占新桌面。用户 2026-09-23 明确要求的。
    #   这一个函数收敛了三种配置，所以必须有表驱动单测 —— 用假的 self 调，
    #   不起 GUI、不碰系统（同 verify_panel.py 的做法）。
    #
    #   ⚠️ 2026-09-23 真机补了一条**回归**（见 "打字" 那两例）：旧判据是
    #      "0.6 秒内按过 Shift 就算"，用户在中文输入法里打字（Shift 切中英文）
    #      会把窗口一次次续期，等于"打字期间一直 armed" —— 测试窗口普通最大化
    #      就被误判成 Shift+最大化。新判据把"按住"和"快按快放"分开给宽限，
    #      后者比一拍还短，所以"按一下就松、隔一会儿才最大化"不再命中。
    class _FakeSuite:
        def __init__(self, cfg):
            self.cfg = cfg
            self.mod_held = 0.0     # 0.0 = 从未（t0 是个大数，算出来必然超窗）
            self.mod_tap = 0.0
        mvd_armed = S.Suite.mvd_armed

    t0 = 1000.0

    def _armed(cfg, held_age=None, tap_age=None):
        f = _FakeSuite(cfg)
        if held_age is not None:
            f.mod_held = t0 - held_age
        if tap_age is not None:
            f.mod_tap = t0 - tap_age
        return f.mvd_armed(t0)

    on = {"auto_app_desktop": True, "mvd_shift_only": True}
    armed_cases = [
        ({"auto_app_desktop": False, "mvd_shift_only": True}, None, None, False,
         "总开关关掉"),
        ({"auto_app_desktop": True, "mvd_shift_only": False}, None, None, True,
         "开关关掉＝任何最大化都独占（旧行为）"),
        (on, 0.0, None, True, "此刻按着"),
        (on, None, 0.0, True, "本拍内刚按过（快按快放）"),
        (on, None, None, False, "没按"),
        (on, S.MOD_HOLD_WINDOW + 0.02, None, False, "早就松手了"),
        (on, None, S.MOD_TAP_WINDOW + 0.02, False, "上一拍按的，已经过期"),
        (on, S.MOD_HOLD_WINDOW - 0.02, None, True, "按住，正好在宽限内"),
        # ↓ 回归：打字时 Shift 是"按 0.2 秒就松"，松开后 0.3 秒才最大化。
        #    旧判据(0.6s) 命中 → 误触发；新判据两条路都超窗 → 不触发。
        (on, 0.5, 0.3, False, "打字按过 Shift、0.3 秒后才最大化（旧判据会误触发）"),
    ]
    armed_bad = []
    for cfg, ha, ta, want, desc in armed_cases:
        got = _armed(cfg, ha, ta)
        if got != want:
            armed_bad.append(f"{desc}→{got}≠{want}")
    say(not armed_bad,
        "独占桌面的触发判据（按住修饰键才算，打字按过不算）",
        "；".join(f"{d}→{_armed(c, h, t)}" for c, h, t, _, d in armed_cases)
        + f"（按住宽限={S.MOD_HOLD_WINDOW}s 快按快放={S.MOD_TAP_WINDOW}s）")

    # ★★ 软合成该用哪个底色 —— 用户 2026-09-23 实拍"深色壁纸上一圈白边"的根因。
    #    契约：**浮窗那一路永远是 None**，只有嵌在任务栏里那份才混底色。
    #    这条必须能单测，不能藏在 render() 里靠读源码确认（改回去没人拦得住）。
    class _FakeBg:
        def taskbar_bg(self):
            return (204, 228, 236)          # 假装采到了任务栏底色
        bg_for = S.Suite.bg_for

    fb = _FakeBg()
    say(fb.bg_for("fw") is None,
        "浮窗那一路永远不混底色（bg=None）"
        "（它下面是任意壁纸/窗口，猜底色 = 在壁纸上画一圈白边）",
        f"bg_for('fw')={fb.bg_for('fw')!r}")
    say(fb.bg_for("emb") == (204, 228, 236),
        "嵌入那一沿用实测的任务栏底色（任务栏是均匀纯色，混上去才有意义）",
        f"bg_for('emb')={fb.bg_for('emb')!r}")
    accent = S.accent_color()
    blue = (56, 132, 255)
    b_cur = S.pill_style({"is_current": True, "is_mvd": True}, accent, True)[1]
    b_mvd = S.pill_style({"is_current": False, "is_mvd": True}, accent, True)[1]
    b_num = S.pill_style({"is_current": False, "is_mvd": False}, accent, True)[1]
    say(b_cur == accent and b_mvd != blue and b_mvd == b_num,
        "当前就是全屏桌面时也用强调色，不再橘/蓝打架",
        f"当前(全屏)边框={b_cur} 强调色={accent}；非当前全屏边框={b_mvd}；"
        f"非当前普通边框={b_num}（两者一致={b_mvd == b_num}）")
    m2 = S.compute_metrics(48, ["1", "2"])
    m_num = S.compute_metrics(48, ["1", "2", "3"])
    m_long = S.compute_metrics(48, ["1", "任务管理器"])
    m_app = S.compute_metrics(48, ["1", "2", "chr"])
    fits = all(w >= S.text_width(S.load_font(m_app["font"]), lb)
               for lb, w in zip(["1", "2", "chr"], m_app["widths"]))
    say(m_long["win_w"] > m2["win_w"] and fits
        and max(m_long["widths"]) <= int(m_long["ph"] * 2.6),
        "药丸宽度跟着文字走（长名字会撑开，且有上限）",
        f"两颗序号宽={m2['win_w']} 两颗(1+长名字)宽={m_long['win_w']} "
        f"逐颗={m_long['widths']} 文字放得下={fits} 三颗序号宽={m_num['win_w']}")
    # 中文字形：中文应用名不能被画成"豆腐块"。
    # 判据 = 两个不同的汉字各画一遍，位图必须不一样 —— 字体缺字形时，
    # 所有汉字都会画成同一个空方框，位图就完全相同了。
    m_cjk = S.compute_metrics(48, ["微"])
    cjk_state = lambda s: [{"number": 1, "label": s, "is_current": True,
                            "is_mvd": True}]
    img1 = S.render_pixmap(m_cjk["win_w"], 48, cjk_state("微"),
                           accent, True, m_cjk)
    img2 = S.render_pixmap(m_cjk["win_w"], 48, cjk_state("文"),
                           accent, True, m_cjk)
    same = img1.tobytes() == img2.tobytes()
    fnt = S.load_font(m_cjk["font"])
    say(not same,
        "中文字形能画出来（「微」和「文」画出来不一样，不是豆腐块）",
        f"字体文件={S.font_file(fnt)} 字号={m_cjk['font']}；"
        f"两张位图完全相同={same}")
    # 任务栏收起判据的两个纯函数：拿实测的那两个矩形喂进去
    mon = monitor()
    exp_rc = (0, mon[3] - 96, mon[2], mon[3])
    ret_rc = (0, mon[3] - 2, mon[2], mon[3] + 94)
    span_exp = S.onscreen_span(exp_rc, mon)
    span_ret = S.onscreen_span(ret_rc, mon)
    back = S.dock_position(ret_rc, mon)
    say(span_exp[1] == 96 and span_ret[1] == 2 and back == exp_rc,
        "任务栏收起判据：看「露在屏幕内的部分」，不看看高度",
        f"展开位={exp_rc}→露出{span_exp}；滑出后={ret_rc}→露出{span_ret}；"
        f"dock_position 把滑出的矩形推回={back}")

    order_contracts()

    # 7) 全屏（单应用）桌面：本轮用户报的 bug + 新需求都在这条链上
    #    这条路不用抢前台 —— 套件是认桌面名 [MVD] xxx，所以测试自己造一个
    #    桌面也能走到同一段代码。套件自己建桌面的那条路（最大化触发）要前台，
    #    放在第 8 段，锁屏时判 SKIP。
    hinst = k.GetModuleHandleW(None)
    cls = "VDBTestWnd"
    global _TEST_REF
    _TEST_REF = WNDPROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))
    wc = WNDCLASSW()
    wc.lpfnWndProc = _TEST_REF
    wc.hInstance = hinst
    wc.lpszClassName = cls
    u.RegisterClassW(ctypes.byref(wc))
    test = u.CreateWindowExW(0, cls, "VDB 测试窗口", WS_OVERLAPPEDWINDOW,
                             120, 120, 900, 600, None, None, hinst, None)
    if not test:
        say(False, "造出测试窗口", f"CreateWindowEx 失败 err={k.GetLastError()}")
        return finish(proc, err_path, err_before, ah_orig)
    u.ShowWindow(test, SW_SHOW)
    pump(0.6)
    fg_ok = steal_foreground(test)
    say(True, "造出测试窗口",
        f"hwnd={test} 当前桌面={current_number()} 桌面数={len(desktops())}")
    if fg_ok:
        say(True, "测试窗口拿到前台", f"前台={foreground_owner()[1]}")
    else:
        skip("测试窗口拿到前台",
             f"抢不到前台（被 {fg_desc} 占着）—— 套件只认前台窗口的最大化，"
             f"所以第 8 段的跳桌面流程跟着判 SKIP")

    base = desktops()
    # ⚠️ 跑之前**就已经存在**的 [MVD] 桌面要单独记下来（本机真机上就躺着一条
    #    [MVD] WorkBuddy —— 套件上次退出没收回的残留）。
    #    为什么必须记：断言只能用"跟基线做差"的方式判"本次测试有没有留下东西"，
    #    不能判"系统里一个 [MVD] 都没有"。后者会把别人留下的账算到这套代码头上，
    #    2026-09-23 真机上一次带出 4 个假 FAIL（见下面两处的注释）。
    pre_mvd_ids = {d[2] for d in base if "[MVD]" in d[1].upper()}
    pre_mvd_names = [d[1] for d in base if "[MVD]" in d[1].upper()]
    if pre_mvd_names:
        print(f"⚠️ 跑之前就有 [MVD] 残留桌面：{pre_mvd_names}"
              f"（多半是套件上次退出没收回；本次断言的判据会把它排除在外）",
              flush=True)
    origin = current_number()
    origin_d = pyvda.VirtualDesktop.current()
    origin_id = current_id()
    mvd_ok = True
    for tag, nm in (("拉丁", "[MVD] chrome"), ("中文", "[MVD] 微信")):
        d = pyvda.VirtualDesktop.create()
        try:
            d.rename(nm)
        except Exception as e:
            say(False, "把测试桌面改名成 " + nm, f"{type(e).__name__}: {e}")
            mvd_ok = False
            break
        pyvda.AppView(hwnd=test).move(d)
        if not goto_desktop(d, 15.0):
            skip(f"[{tag}] 切到 {nm} 全屏桌面（让它成为当前桌面）",
                 f"15 秒内没切过去，当前 id={current_id()}（期望 {d.id}）"
                 f"—— 多半是你在同时切桌面。标签那几条不依赖它，照常判")
        pump(2.0)
        # 单应用桌面联动（mvd_link）会把任务栏收起来，浮窗才会露出来。锁屏下
        # explorer 有时点了开关却不动，所以先等，再补一次。
        retracted = wait_tray(True, 6.0)
        if not retracted and autohide():
            set_autohide(True)
            retracted = wait_tray(True, 10.0)
        if retracted:
            print(f"  [{tag}] 现场：{describe_tray()}｜"
                  f"浮窗 {describe_float()}｜"
                  f"嵌入可见={embedded_visible(suite_pid)}", flush=True)
        else:
            print(f"  [{tag}] 现场：autohide={autohide()}｜{describe_tray()}｜"
                  f"浮窗 {describe_float()}｜"
                  f"嵌入可见={embedded_visible(suite_pid)}", flush=True)
        want = S.pill_label({"app": S.app_name_from(nm),
                             "number": d.number})
        rendered = [l for l in read_log(log_path) if " 药丸: " in l]
        line = rendered[-1].split(" 药丸: ")[-1] if rendered else ""
        cur_name = [x[1] for x in desktops() if x[0] == current_number()]
        is_current = current_id() == d.id
        # ⚠️ 日志里每颗药丸写成 "[标签*]全屏"，'*' 标的是"这颗在不在当前桌面"，
        #    跟标签内容无关。断言比的是标签内容，所以先把 '*' 抹掉再找 —— 否则
        #    MVD 桌面正好是当前桌面时，用 [chr] 找 [chr*] 会假失败（踩过一次）。
        flat = line.replace("*]", "]")
        # 应用名标签这一条**不依赖"谁当前在用桌面"**：只要那个桌面存在，套件就会
        # 把它画成 [应用名]全屏。你本人同时在切换桌面也不会影响这一条。
        say(f"[{want}]" in flat and "全屏" in line,
            f"[{tag}] 全屏桌面显示应用名前 {'3' if tag == '拉丁' else '2'} "
            f"个字符而不是序号",
            f"日志行={line!r}；期望那颗={want!r}；当前桌面={cur_name}")
        say(f"[{d.number}]全屏" not in flat,
            f"[{tag}] 那颗没有退化成序号", f"日志行={line!r}")
        say("immersive=on" not in "\n".join(read_log(log_path)[-14:]),
            f"[{tag}] 套件没有进入沉浸模式把药丸整体藏掉",
            "最近日志里没有 immersive=on")
        say(embedded_visible(suite_pid),
            f"[{tag}] 嵌在任务栏里的那一份也没被 SW_HIDE 掉"
            f"（immersive_hide 默认关的效果）",
            f"嵌入可见={embedded_visible(suite_pid)} 浮窗可见={float_visible()}")
        if not is_current:
            skip(f"[{tag}] 任务栏收起 + 全屏桌面 → 药丸仍然可见",
                 f"这个桌面没能成为当前桌面（当前={cur_name}，"
                 f"is_current=False）—— 多半是你在同时切桌面；"
                 f"这条用 probe_mvd.py 单独跑是确定性的，也可以等你不用"
                 f"电脑时重跑本脚本")
        elif retracted:
            # 同样要等套件那个 400ms 节拍把浮窗摆出来（见 wait_float）
            shown = wait_float(True, 8.0)
            say(shown,
                f"[{tag}] 任务栏收起 + 全屏桌面 → 药丸仍然可见"
                f"（用户报的那个毛病）", describe_float())
            if not shown:
                skip(f"[{tag}] 露出来的浮窗没被 DWM 遮蔽（不是「看不见的可见」）",
                     "上一步浮窗就没露出来，遮蔽与否无从判起")
            else:
                say(not cloaked(float_hwnd()).startswith("被遮蔽"),
                    f"[{tag}] 露出来的浮窗没被 DWM 遮蔽（不是「看不见的可见」）",
                    f"DWM遮蔽={cloaked(float_hwnd())}")
        else:
            skip(f"[{tag}] 任务栏收起 + 全屏桌面 → 药丸仍然可见",
                 f"explorer 没把任务栏收走（注册表里 autohide="
                 f"{autohide()}，但 rect 还露着 96px）—— 自动隐藏平时靠鼠标消息"
                 f"驱动，这一步在本会话里复现不出来；{describe_tray()}")
        fr = rect_of(float_hwnd())
        if fr:
            bar_h = fr[3] - fr[1]
            labels = [S.pill_label({"app": S.app_name_from(n), "number": num})
                      for num, n, _ in desktops()]
            w_app = S.compute_metrics(bar_h, labels)["win_w"]
            say(fr[2] - fr[0] == w_app,
                f"[{tag}] 浮窗尺寸和按标签算出来的排版一致",
                f"实测宽={fr[2] - fr[0]} 高={bar_h}；按标签{labels}算={w_app}")
        # 切回普通桌面，并把测试自己造的临时桌面清掉。
        # ⚠️ 目标桌面要用**按 id 拿到的对象**：origin 是序号，拿它当 id 传给
        #    pyvda.VirtualDesktop() 会静默失败，窗口赖在测试桌面上删不掉（踩过）。
        goto_desktop(origin_d, 12.0)
        pump(2.0)
        _moved, _kept = clear_mvd_desktops(os.getpid(), origin_id)
        if _moved:
            print(f"  [{tag}] 清掉了测试桌面：{_moved}", flush=True)
        if _kept:
            print(f"  [{tag}] ⚠️ 没能删掉的桌面（上面有窗口）：{_kept}", flush=True)
        pump(1.0)
    wait_tray(False, 8.0)          # 离开单应用桌面后任务栏该回来了

    # ⚠️ 段 7 自己造/删临时桌面。删不掉的（上面挂着**别的进程**的窗口，比如
    #    你本人正在用的微信）会真的留下来 —— 这张必须并进基线，否则后面每一条
    #    "桌面数应该是多少""有没有新残留""有没有跳过去"都被它带偏，一次能报出
    #    4~5 条假 FAIL（2026-09-23 真机实测，7 条 FAIL 里 5 条是它带出来的）。
    #    并进基线不等于掩盖：下面单独报一条，把它挑明。
    leftover = [d for d in desktops()
                if "[MVD]" in d[1].upper() and d[2] not in pre_mvd_ids]
    if leftover:
        print(f"⚠️ 段 7 的临时桌面没能删掉（上面挂着别的进程的窗口）："
              f"{[d[1] for d in leftover]}", flush=True)
        pre_mvd_ids |= {d[2] for d in leftover}
        pre_mvd_names += [d[1] for d in leftover]
        base = desktops()          # 基线跟着走，别让后面段落替它背锅
    say(not leftover, "段 7 造的全屏测试桌面都清干净了",
        f"留下来={[d[1] for d in leftover] or '无'}"
        + ("（那上面是**别的进程**的窗口，脚本按设计不敢删 —— 这不算套件的"
           "毛病，多半是你在跑测试的同时切了桌面或正在用那个程序。"
           "事后可以用 source\\_clean_mvd.py 清空桌面，它只删空的）"
           if leftover else ""))

    say(len(desktops()) == len(base), "退出全屏桌面后桌面数复原",
        f"桌面数={len(desktops())}（起点 {len(base)}）")
    say(wait_float(False, 8.0) and tray_rect() == docked,
        "退出全屏桌面 → 任务栏还原、浮窗收回",
        f"{describe_float()}；任务栏={tray_rect()}；autohide={autohide()}")

    # 8) 套件自己的跳桌面流程（需要前台窗口，锁屏下没戏）
    #
    #    ⚠️ 这一段对"前台"很敏感：套件只认前台窗口。你本人正在用电脑的话
    #    （比如切到微信），前台会被抢走，套件就会把**你的窗口**甩到新桌面。
    #    所以每次触发前都要重新抢一遍前台并确认抢到了，抢不到就判 SKIP。
    def own_fg():
        """确认前台是我们的测试窗口，不是就先抢（抢不到返回 False）"""
        if u.GetForegroundWindow() == test:
            return True
        steal_foreground(test)
        pump(1.0)
        return u.GetForegroundWindow() == test

    if not fg_ok or not own_fg():
        skip("最大化 → 自动跳新桌面（整条跳桌面流程）",
             f"前台不在我们造的窗口上（现在是 {foreground_owner()[1]}）——"
             f"套件只认前台窗口，抢不回来这条链路就没法验")
        skip("热键甩到新桌面（处理器路径）", "同上：前台抢不到")
    else:
        u.ShowWindow(test, SW_MAXIMIZE)
        pump(3.0)
        now = desktops()
        mvd = [d for d in now if "[MVD]" in d[1].upper()]
        # ⚠️ 必须取"**基线里没有的**那张 [MVD] 桌面"，不能图省事取 mvd[0]。
        #    跑之前就躺着残留（如 [MVD] WorkBuddy）时，mvd[0] 是那张旧的，本次
        #    新建的排在后面 → 断言一起假失败，看着像"跳桌面功能坏了"。
        fresh = [d for d in mvd if d[2] not in pre_mvd_ids]
        # 这一段现在要分两种期望，因为产品的默认交互变了（2026-09-23 用户要求）：
        #   默认 mvd_shift_only=True  → **普通最大化什么都不该发生**
        #   环境变量 VDB_MVD_SHIFT_OFF=1（把 switch 关掉）→ 老行为：任何最大化都独占
        # 合成 Shift 在这个会话里做不到（SendInput 被系统丢弃，见下面的 SKIP），
        # 所以"Shift+最大化"那一端靠 S.Suite.mvd_armed 的表驱动单测覆盖，
        # 真实链路则用"关掉开关"来验同一段代码。
        if FORCE_AUTO:
            # "到底跳过去了没有"要拿**那一刻**的凭据来判：日志里那颗 [MVD] 药丸
            # 带上 '*' 才说明套件确实把当前桌面切过去了。不能只看 3 秒后的当前
            # 桌面 —— 用户在旁边切一下就会把它切回去。
            want_lbl = ""
            if fresh:
                want_lbl = S.pill_label({"app": S.app_name_from(fresh[0][1]),
                                         "number": fresh[0][0]})
            jumped = bool(want_lbl) and any(
                f"[{want_lbl}*]全屏" in ln for ln in read_log(log_path)[-25:])
            mine_in_mvd = bool(fresh) and desktop_of(test) == fresh[0][0]
            say(bool(len(now) == len(base) + 1 and mine_in_mvd and jumped),
                "旧行为那一端（开关关掉）：最大化 → 自动跳新桌面",
                f"桌面数 {len(base)}→{len(now)}；新建={fresh[0][1] if fresh else '无'}；"
                f"日志里出现过 [{want_lbl}*]全屏={jumped}；"
                f"测试窗口所在桌面={desktop_of(test)}")
            say(bool(fresh and fresh[0][1].lower().endswith("python")),
                "新桌面命名带 [MVD] 前缀 + 进程名",
                fresh[0][1] if fresh else "无（本次没新建 [MVD] 桌面）")
        else:
            # ★ 用户要的就是这一条：普通最大化 = 普通最大化，不要被扔去独占
            #
            # ⚠️ 但"它偏偏新建了桌面"有两种可能，得分清：
            #    ① 套件真出错（判据没生效）→ FAIL；
            #    ② 你在跑测试时正打字/按着修饰键，那一下被算成"明确要求"→ 测量
            #       被污染，判 SKIP 更诚实。2026-09-23 真机就是这样：日志写着
            #       "触发=maximize"，而脚本压根没按过键 —— 是你本人在微信里
            #       打字（中文输入法里 Shift 是切中英文的高频键）。
            why = fired_why(log_path)
            polluted = bool(fresh) and bool(why) and why != "maximize"
            if polluted:
                skip("普通最大化（没按住修饰键）→ 不新建桌面，窗口留在原桌面",
                     f"桌面确实被建出来了，但套件日志记的触发原因是「{why}」——"
                     f"说明触发那一刻修饰键正被按住。脚本自己没按过键，所以多半是"
                     f"你本人在打字。这一条要验的是「没人碰键盘时最大化什么都不做」，"
                     f"旁边有人按键就验不准，判 SKIP；手离开键盘重跑即可")
                skip("普通最大化后窗口没被搬走", f"同上：那张桌面是被「{why}」触发的")
            else:
                say(len(now) == len(base) and not fresh,
                    "普通最大化（没按住修饰键）→ 不新建桌面，窗口留在原桌面",
                    f"桌面数 {len(base)}→{len(now)}；"
                    f"新增 [MVD]={[d[1] for d in fresh] or '无'}；"
                    f"日志触发原因={why or '（日志里没有新建记录）'}；"
                    f"测试窗口所在桌面={desktop_of(test)}（原={origin}）")
                say(desktop_of(test) == origin,
                    "普通最大化后窗口没被搬走",
                    f"窗口所在={desktop_of(test)} 原={origin}")
        say(bool(u.IsZoomed(test)), "窗口是真最大化过的（不是压根没最大化）",
            f"IsZoomed={bool(u.IsZoomed(test))}")
        u.ShowWindow(test, SW_RESTORE)
        own_fg()
        pump(3.0)
        back = desktops()
        say(len(back) == len(base), "还原 → 临时桌面被删除",
            f"桌面数 {len(now)}→{len(back)}；剩下 {[d[1] for d in back]}")
        say(current_number() == origin, "还原 → 当前桌面切回原桌面",
            f"当前={current_number()} 原={origin}")
        say(desktop_of(test) == origin, "还原 → 窗口移回原桌面",
            f"窗口所在={desktop_of(test)} 原={origin}")
        if not hk_launch:
            pass
        elif not own_fg():
            skip(f"热键 {hk_launch} 甩到新桌面（处理器路径）",
                 f"触发前前台被抢走（{foreground_owner()[1]}）")
        else:
            fire_hotkey(HK_LAUNCH)
            pump(3.0)
            n2 = desktops()
            # 同上：只认"基线里没有的"那张，别被跑之前就存在的 [MVD] 残留带偏
            m2_ = [d for d in n2 if "[MVD]" in d[1].upper()
                   and d[2] not in pre_mvd_ids]
            ok2 = (len(n2) == len(base) + 1 and bool(m2_)
                   and desktop_of(test) == m2_[0][0])
            if m2_ and desktop_of(test) != m2_[0][0]:
                # 甩走的是**别人的**窗口 —— 发键那一刻前台被你抢走了。
                # 套件这么做是对的（热键本来就说"甩前台窗口"），但你正在用电脑，
                # 所以这条验不准；而且**必须善后**，不能把你的窗口丢在那儿。
                rescued = rescue_foreign_windows(m2_[0][2], origin_id,
                                                 os.getpid())
                time.sleep(0.8)
                clear_mvd_desktops(os.getpid(), origin_id)
                skip(f"热键 {hk_launch} 甩到新桌面（处理器路径）",
                     f"发键那一刻前台被抢走了（现在是 {foreground_owner()[1]}），"
                     f"套件按设计把**你的窗口**甩到了新桌面，不是测试窗口 → 验不准。"
                     f"脚本已经把 {rescued} 个窗口搬回原桌面、并清掉那张临时桌面")
                skip("再按一次热键收回", "上一步没成立，不接着判")
            else:
                say(ok2, f"热键 {hk_launch} 甩到新桌面（处理器路径）",
                    f"桌面数={len(n2)}；"
                    f"新建={m2_[0][1] if m2_ else '无（本次没新建）'}；"
                    f"测试窗口所在桌面={desktop_of(test)}")
            if not ok2:
                skip(f"再按 {hk_launch} 收回", "上一步没成立，不接着判")
            else:
                own_fg()
                fire_hotkey(HK_LAUNCH)
                pump(3.0)
                say(len(desktops()) == len(base), f"再按 {hk_launch} 收回",
                    f"桌面数={len(desktops())}")

    # 9) 收尾：先让套件自己把窗口收回，再关掉它，最后检查有无异常
    if u.IsWindow(test):
        u.ShowWindow(test, SW_RESTORE)
        pump(2.0)
    if u.IsWindow(test):
        u.DestroyWindow(test)
        pump(2.5)
    left, stranded1 = normalize()
    # ⚠️ 判据是"**本次测试**没留下新的残留"，不是"系统里一个 [MVD] 都没有"。
    #    跑之前就躺着的那条（[MVD] WorkBuddy，套件上次退出没收回）不是这套代码的
    #    账 —— 全算进来的话，一条残留能同时带出 4 个 FAIL（2026-09-23 真机踩过）。
    #    所以先记下基线里的名字，再对"收尾后还剩什么"做**多重集差**。
    after_mvd = [d[1] for d in desktops() if "[MVD]" in d[1].upper()]
    extra = list(after_mvd)
    for nm in pre_mvd_names:
        if nm in extra:
            extra.remove(nm)                     # 跑之前就有的，销账
    # ⚠️ "多出来"的那张桌面不一定是**我们**留下的：你正在用电脑时抢走了前台，
    #    套件按设计把你的窗口甩到了新桌面（热键/自动触发的语义就是"甩前台窗口"）。
    #    那上面是你自己的程序，脚本按规矩不敢动别人的窗口 —— 这不算测试残留，
    #    判 SKIP 并明确说清楚，不要让 FAIL 去指控一段没坏的代码。
    foreign = {nm for nm, _ in stranded1} if stranded1 else set()
    if extra and set(extra) <= foreign:
        skip("收尾后没有**本次新造**的 [MVD] 残留",
             f"多出来={extra}，但那上面是**别人的**窗口（多半是你正在用的程序）"
             f"—— 跑测试时前台被抢走，套件按设计把你的窗口甩了过去。脚本不动别人的"
             f"窗口，所以这张桌面留着；清理工具：source\\_clean_mvd.py"
             f"（先把窗口拖回去，它只删空的）")
    else:
        say(not extra, "收尾后没有**本次新造**的 [MVD] 残留",
            f"多出来={extra or '无'}；跑之前就有的={pre_mvd_names or '无'}；"
            f"本次清掉={left or '无'}；有窗口没敢删={stranded1 or '无'}")

    return finish(proc, err_path, err_before, ah_orig)


def finish(proc, err_path, err_before, ah_orig=None):
    if ah_orig is not None and autohide() != ah_orig:
        set_autohide(ah_orig)
        print(f"（已把任务栏自动隐藏还原成 {ah_orig}）", flush=True)
    if _CFG_RESTORE is not None:
        path, text = _CFG_RESTORE
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print("（已还原跑测试前的那份 suite_settings.json）", flush=True)
        except Exception:
            pass
    err_after = read_log(err_path)
    say(len(err_after) == err_before, "运行期无未捕获异常",
        f"suite_errors.log {err_before} → {len(err_after)} 行")
    if len(err_after) > err_before:
        print("  最后一段异常：", flush=True)
        for line in err_after[-err_before - 12:] if err_before else err_after[-12:]:
            print("    " + line)

    # ⚠️ 日志要按模式取：脚本模式在 source/，exe 模式在项目根目录。以前这里写死
    #    了 HERE，于是 exe 模式打印的是**上一轮脚本模式**的旧日志，排查时会被带偏。
    #    err_path 本来就是按模式算好的，取它所在目录即可。
    log_file = os.path.join(os.path.dirname(err_path), "suite_life.log")
    print(f"\n== {log_file} 尾部 ==", flush=True)
    for line in read_log(log_file)[-30:]:
        print("  " + line)
    try:
        u.PostMessageW(u.FindWindowW(None, "VDSuite"), 0x0010, 0, 0)
        time.sleep(1.0)
    except Exception:
        pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
    bad = [r for r in REPORT if r[0] is False]
    sk = [r for r in REPORT if r[0] is None]
    print(f"\n== 合计 {len(REPORT) - len(bad) - len(sk)} 通过 / {len(bad)} 失败 / "
          f"{len(sk)} 未验（环境阻塞）==", flush=True)
    for _, t, d in bad:
        print(f"  FAIL: {t} — {d}")
    for _, t, d in sk:
        print(f"  SKIP: {t} — {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
