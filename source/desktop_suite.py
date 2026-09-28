# -*- coding: utf-8 -*-
"""desktop_suite.py — 虚拟桌面套件（集成版）

把原来三个独立程序的能力合到一个进程里：

  1. 任务栏桌面预览条  ← 原 VirtualDesktopBar
     桌面序号药丸、当前桌面高亮、单应用桌面显示应用名、点击跳桌面

  2. 任务栏自动显隐    ← 原 ToggleTaskbar
     全局热键切换（与系统设置界面是同一个值，不重启 explorer）

  3. 最大化即跳新桌面  ← 原 MaximizeToVirtualDesktop
     前台窗口被最大化时：新建虚拟桌面 → 命名为 "[MVD] 进程名" → 把窗口移过去
     → 切换过去；窗口还原/关闭/再按热键时移回原桌面并删除临时桌面

三者是咬合的：第 3 步建的桌面带 "[MVD]" 前缀，第 1 步据此把它显示成应用名，
并（按配置）自动藏任务栏，形成一整块全屏观感；离开时还原。

画的部分全在 suite_ui.py，这里只管系统交互。

依赖: pyvda, Pillow（脚本模式从 ./libs 加载；exe 模式已打进包）
"""
import atexit
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace

# ---- 路径语义：脚本模式 与 打包成 exe 的 frozen 模式 分开处理
_FROZEN = bool(getattr(sys, "frozen", False))
if _FROZEN:
    _HERE = os.path.dirname(os.path.abspath(sys.executable))
    _LIBS = None
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _LIBS = os.path.join(_HERE, "libs")
_RESTART_TARGET = sys.executable if _FROZEN else os.path.join(_HERE, "desktop_suite.py")
_SETTINGS = os.path.join(_HERE, "suite_settings.json")
_TRAY_HIDE_MARKER = os.path.join(_HERE, "suite_taskbar_hidden.marker")
if _LIBS and os.path.isdir(_LIBS) and _LIBS not in sys.path:
    # 放 sys.path 最后当兜底：libs/ 里是 3.12 编的离线依赖（PIL 的 _imaging 是
    # cp312），插到最前会盖住 site-packages，换个解释器就 import 失败。
    sys.path.append(_LIBS)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import tkinter as tk
    import winreg
    from ctypes import wintypes

    from PIL import Image, ImageTk

    import pyvda
    import suite_ui as ui
    from layered_pill import NativePill
except Exception:
    tb = traceback.format_exc()
    with open(os.path.join(_HERE, "suite_crash.log"), "w",
              encoding="utf-8") as f:
        f.write(tb)
    # 脚本模式下也把 traceback 打出来。以前只写日志、屏幕上什么都不显示，
    # 进程直接退 1，看着像"程序压根没启动"——踩过一次，白查一整轮。
    # pythonw 下 stderr 是 None，包一层。
    try:
        sys.stderr.write(tb)
    except Exception:
        pass
    sys.exit(1)

# ---- 视觉层的名字原样再导出。
# verify_suite.py 是按 `S.xxx` 调的，拆分模块时这些名字必须继续存在，
# 否则验证脚本会在 import 阶段就炸掉，看起来像"程序坏了"。
KEY = ui.KEY
KEY_HEX = ui.KEY_HEX
load_font = ui.load_font
font_file = ui.font_file
text_width = ui.text_width
pill_label = ui.pill_label
pill_style = ui.pill_style
compute_metrics = ui.compute_metrics
render_pixmap = ui.render_pixmap
blend = ui.blend

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# 进程与模块句柄同 HWND 一样是指针宽度；ctypes 的默认 c_int 在 64 位
# Windows 上会截断它们。热键窗口、进程名查询和单实例锁都依赖这些句柄。
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                   wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]

user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

# ⚠️ 凡是**收发窗口句柄**的函数都得显式声明，不能让 ctypes 用默认的 c_int。
#    默认返回 c_int 会把 64 位的 HWND 截成 32 位带符号整数：句柄一旦落在
#    0x80000000 以上就变成负数，拿去比对"父窗口是不是任务栏"会得到随机结果，
#    拿去 SetParent 会挂到别的窗口上。这类故障不会报错，只会表现为
#    "嵌入偶尔不生效"，是最难查的一类。这里一次钉死。
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.SetParent.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_ssize_t]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]

# ⚠️ 热键线程那个窗口的 WndProc 用到了这两个。之前没声明 DefWindowProcW 的
#    argtypes，ctypes 会把第四个参数（LPARAM）按 c_int 转换；只要 LPARAM 超过
#    32 位（窗口消息里很常见，比如打包了坐标或指针），转换就抛 OverflowError，
#    整个 WndProc 静默失败、默认处理根本没跑。日志里刷了一屏
#    "argument 4: OverflowError: int too long to convert" 就是这个。
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                  ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = wintypes.BOOL
# 返回值是 SHORT（高字节=当前是否按住，低字节=自上次调用以来被按过），
# 不声明的话默认按 c_int 收，低字节照样能取到，但声明出来语义更明确。
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = wintypes.BOOL
# ---- 采任务栏底色（见 Suite.taskbar_bg）
# ⚠️ 用 GetPixel 逐点读，不用 BitBlt + GetDIBits 那一整套：这里只要几十个
#    像素，而且 6 秒才采一次（约 25 次系统调用，几十微秒），代码量却少一半。
gdi32 = ctypes.windll.gdi32
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.ReleaseDC.restype = ctypes.c_int
gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = ctypes.c_uint      # 0xFFFFFFFF（CLR_INVALID）= 读失败

# ---- 强制重画（见 Suite._flush_paint）
# 窗口刚被撑大之后，新露出来的那块在分层窗口里是**未定义像素**（黑）。要盖掉
# 它必须让窗口真的收一次 WM_PAINT —— 而 WM_PAINT 不在 Tk 的 idle 队列里，
# update_idletasks() 处理不到。RedrawWindow 带 RDW_UPDATENOW 是同步的那条路。
RDW_INVALIDATE = 0x0001
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080
user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_uint]
user32.RedrawWindow.restype = wintypes.BOOL

APP_TITLE = "VDSuite"
EMB_TITLE = "VDSuiteEmbed"
PANEL_TITLE = "VDSuitePanel"
LEGACY_TITLES = (APP_TITLE, EMB_TITLE, "VirtualDesktopBar",
                 "VirtualDesktopBarEmbed")

POLL_MS = 400            # 状态/渲染轮询
MVD_MS = 220             # 最大化监视轮询（比渲染快，尽量贴近"点下去就跳"）
ANIM_MS = 28             # 动画帧间隔（约 35fps）
HL_DUR = 0.20            # 高亮在药丸之间走的时长
FW_DUR = 0.22            # 浮窗药丸出场/退场的时长
MAX_DESKTOPS = 12
MAX_DESKTOPS_HARD = 16   # 自动建桌面的上限，防失控

# 从"当前桌面不是单应用桌面"到真正撤掉任务栏隐藏，要连续确认几轮。
# 单次观测可能是切桌面动画中的中间态，立刻响应会来回抖 —— 抖一次就会
# 写两遍注册表，explorer 那边看起来就是"任务栏闪了一下"。
MVD_LEAVE_CONFIRM = 2    # 连续几轮确认"确实离开单应用桌面"才真的离开
TRAY_RETRY_MAX = 4       # 连推多少次还没收起来 → 放慢（配合下面的观察窗）
TRAY_RETRY_GAP = 2.0     # 放慢之后的重推间隔（秒）
TRAY_RETRY_QUIET = 10    # 到这个次数就彻底停手，只留核验（见 apply_tray_goal）
# ⚠️ 推一次之后至少要等这么久**再核验**。
#    原来是无间隔连推（每轮 400ms 就推一次），实测日志是"重推第 1..8 次"
#    全部失败 —— 每次 ABM_SETSTATE 都会让 explorer 重新开始一遍收起流程，
#    推得越勤越收不起来，等于自己打断自己。explorer 收起要 0.4~1.2 秒，
#    观察窗必须比它长。
TRAY_OBSERVE = 0.85

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_MINIMIZEBOX = 0x00020000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOP = 0
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
SWP_NOSENDCHANGING = 0x0400
SW_HIDE = 0
SW_SHOW = 5
SW_MAXIMIZE = 3                      # = SW_SHOWMAXIMIZED
SW_RESTORE = 9                       # = SW_SHOWNORMAL

VK_LBUTTON = 0x01
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12                          # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
# 面板"点到外面就关"的看门狗（见 Suite.panel_watch）
PANEL_WATCH_MS = 140                 # 轮询间隔。140ms 够快，开销可忽略
PANEL_GRACE = 0.35                   # 打开后这段时间内不理任何点击，防误关

# "独占新桌面"要**按住**的修饰键 —— 这个键可以换（见 cfg["mvd_modifier"]）。
#
# 选键的硬要求有三条，缺一条都会难受：
#   1. 打字时不会高频按到 —— 否则"最近按过"会被反复续期（2026-09-23 撞过）；
#   2. 单按（不配别的键）没有系统副作用 —— 所以 Alt 要小心（单按会激活菜单栏）、
#      Win 也只在小指按下又抬起时才弹开始菜单，按住点鼠标没问题；
#   3. 按住它的同时点最大化按钮，物理上要顺手。
# 默认给 Shift：它虽然在第 1 条上最差（中文输入法用它切中英文），但配合
# mvd_armed 里"必须**按住**"的判据后问题基本消失 —— 打字时手在键盘上，
# 不可能同时在点最大化按钮，两个动作在物理上互斥。
MVD_MODS = {
    "shift": ("Shift", (VK_SHIFT,)),
    "ctrl": ("Ctrl", (VK_CONTROL,)),
    # ⚠️ Alt 的坑：按住 Alt 点最大化，老式 Win32 程序会先进入菜单模式，
    #    那次点击被菜单吃掉，最大化根本不发生 —— 想用 Alt 得接受这个代价。
    "alt": ("Alt", (VK_MENU,)),
    "win": ("Win", (VK_LWIN, VK_RWIN)),
    # 双键组合，用来彻底避开误触：Ctrl+Shift 打字时几乎不会同时按住。
    "ctrl+shift": ("Ctrl+Shift", (VK_CONTROL, VK_SHIFT)),
}
MVD_MOD_DEFAULT = "shift"

# ⚠️ 判据必须严格区分"按住"和"按过"，两者给的宽限**不一样** ——
#    2026-09-23 真机教训：原来只有一个 0.6 秒的窗口，本意是接住"快按快放"，
#    结果用户在中文输入法里打字（Shift 切中英文，每秒按两三次），窗口被一次次
#    续期，等于"打字期间一直处于待触发状态"。日志里明明白白写着"触发=maximize"，
#    而当时前台就是微信 —— 测试窗口一最大化就被误判成"Shift+最大化"。
#
#    MOD_HOLD_WINDOW：按住的那条路。按住期间每一拍都会刷新它，所以实际语义
#        就是"按着就算"，窗口只用来兜住"松手与跳变检测差了几百毫秒"。
#    MOD_TAP_WINDOW：快按快放的那条路。**必须比一拍(MVD_MS=220ms)还短** ——
#        低位(0x0001)的语义是"自上次调用以来按过"，而我们每拍都调一次，
#        所以它天然只覆盖"紧邻那一拍"。给个比一拍短的窗口，就是明确表示
#        "只认刚刚这一下，不认上一拍甚至更早"。
MOD_HOLD_WINDOW = 0.35
MOD_TAP_WINDOW = 0.12

MVD_PREFIX = "[MVD]"
FILL_GAP_TOL = 8
# 任务栏本身的高度下限。低于这个值说明拿到的不是真正的任务栏矩形
# （别把滑出去的那一条、或半截窗口当成参考位置）。
MIN_DOCK_H = 20

# 全局热键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
HK_TASKBAR = 1
HK_LAUNCH = 2
# 每个动作给一串候选组合，按顺序试，第一个注册成功的生效。
# 原因：Ctrl+Alt+T 可能被别的程序占着（历史上是 ToggleTaskbar.exe），
# 两台程序并存时必然 err=1409；有回退链就不会出现"菜单里写着但按了没反应"。
HOTKEY_DEFS = {
    HK_TASKBAR: ("切换任务栏", [
        ("Ctrl+Alt+T", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x54),
        ("Ctrl+Alt+H", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x48),
        ("Ctrl+Alt+B", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x42),
    ]),
    HK_LAUNCH: ("甩到新桌面", [
        ("Ctrl+Alt+Shift+X",
         MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, 0x58),
        ("Ctrl+Alt+Shift+Z",
         MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, 0x5A),
    ]),
}

SETTINGS_VER = 2

DEFAULT_SETTINGS = {
    "__ver": SETTINGS_VER,
    "hotkey_taskbar": True,     # 切换任务栏自动隐藏
    "hotkey_launch": True,      # 把前台窗口甩到新桌面
    # "允许独占新桌面"的**总开关**。开着也只是"允许"，真正触发还要看下一条 ——
    # ⚠️ 默认的交互是：**普通最大化什么都不做**（用户 2026-09-23 明确要求：
    #    "只要我点最大化，它就一定会新建一个虚拟桌面…这并不是我想要的效果"）。
    "auto_app_desktop": True,
    # ⚠️ 本项目的核心交互约定，默认 **True**：
    #    True  = 只有**按住修饰键去最大化**才独占新桌面（含 +双击标题栏、
    #            +Win+↑；"三指上滑"那种系统手势截不到，只能走热键）
    #    False = 老行为：任何最大化都独占（用户嫌吵，已不再默认）
    #    两条路并行不冲突：总开关管"允不允许自动"，这一条管"要不要明确动作"。
    "mvd_shift_only": True,
    # 上面那条"修饰键"具体是哪个键。Shift / Ctrl / Alt / Win / ctrl+shift，
    # 见 MVD_MODS。改它只影响判据，热键 Ctrl+Alt+Shift+X 不受影响。
    "mvd_modifier": MVD_MOD_DEFAULT,
    "mvd_link": True,           # 在单应用桌面上自动隐藏任务栏
    # 全屏桌面上把药丸也一起收起来。默认关 —— 任务栏都藏了，这时只剩浮窗
    # 能显示药丸；再把它也收掉，用户在全屏桌面上就完全看不到桌面状态了。
    "immersive_hide": False,
    "fullscreen_fill": False,   # 把不响应工作区变化的窗口拉到整屏
}


# ---------------------------------------------------------------- 设置持久化
def load_settings():
    s = dict(DEFAULT_SETTINGS)
    ok = False
    ver = 1                  # 文件里压根没写 __ver 的，就是 v1 的老配置
    try:
        with open(_SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            ver = int(data.get("__ver", 1))
            s.update(data)
            ok = True
    except Exception:
        ok = False
    # v1 把 immersive_hide 默认成了开，表现是"进了全屏桌面药丸彻底消失"。
    # 老配置文件里存的还是 True，直接沿用会复现这个毛病，所以升版本时翻掉。
    #
    # ⚠️ 判据必须是"文件里写的版本"，不能拿合并后的 s 去问 —— DEFAULT_SETTINGS
    #    里已经带着新版本号，合并之后连老配置文件也显得"已经是新版"，迁移就
    #    永远不会跑，老用户升上来药丸照样一进全屏桌面就消失。
    if ver < SETTINGS_VER:
        s["immersive_hide"] = False
        s["__ver"] = SETTINGS_VER
        ok = False
    if not ok:
        save_settings(s)
    return s


def save_settings(s):
    try:
        with open(_SETTINGS, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 任务栏开关
SR_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StuckRects3"
ABM_GETTASKBARPOS = 0x0005
ABM_SETSTATE = 0x000A


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", wintypes.HWND),
                ("uCallbackMessage", ctypes.c_uint32), ("uEdge", ctypes.c_uint32),
                ("rc", RECT), ("lParam", ctypes.c_ssize_t)]


def taskbar_autohide():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SR_PATH) as k:
            return bool(winreg.QueryValueEx(k, "Settings")[0][8] & 0x01)
    except OSError:
        return None


def set_taskbar_autohide(on):
    """把"自动隐藏任务栏"这个开关设成 on。

    ⚠️ 这里只保证**设置**写进去了，不保证 explorer 真的把任务栏滑走。
    实测（2026-09-23）：ABM_SETSTATE 之后，如果光标正停在任务栏矩形内，
    explorer 会拒绝执行收起的动作，而且 WM_MOUSELEAVE / 伪造 WM_MOUSEMOVE /
    直接 SetWindowPos 挪它的窗口，全都没用（它会把窗口立刻拽回原位）。
    真正让任务栏动起来的是**光标离开任务栏**那一刻。
    所以调用方不能"设一次就当成了" —— 见 Suite.apply_tray_goal。

    另外 ABM_GETTASKBARPOS **不会**填充 abd.hWnd（实测拿到的是 NULL），
    所以这里显式把任务栏窗口句柄带上，别让 SHAppBarMessage 去猜。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SR_PATH, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            data = bytearray(winreg.QueryValueEx(k, "Settings")[0])
            if bool(data[8] & 0x01) != bool(on):
                data[8] = (data[8] | 0x01) if on else (data[8] & 0xFE)
                winreg.SetValueEx(k, "Settings", 0, winreg.REG_BINARY,
                                  bytes(data))
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd))
        abd.hWnd = tray_hwnd()
        abd.lParam = 0x3 if on else 0x2
        shell32.SHAppBarMessage(ABM_SETSTATE, ctypes.byref(abd))
        return True
    except Exception:
        return False


def recover_forced_taskbar():
    """Recover the Shell window after an earlier process died while hiding it."""
    if not os.path.exists(_TRAY_HIDE_MARKER):
        return False
    tray = tray_hwnd()
    if not tray:
        return False
    if not user32.IsWindowVisible(tray):
        user32.ShowWindow(tray, SW_SHOW)
    if not user32.IsWindowVisible(tray):
        return False
    try:
        with open(_TRAY_HIDE_MARKER, encoding="ascii") as f:
            parts = f.read().split()
        if len(parts) >= 3:
            set_taskbar_autohide(parts[2] == "1")
    except OSError:
        pass
    try:
        os.remove(_TRAY_HIDE_MARKER)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- 窗口小工具
def is_window(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def window_title(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, b, 256)
    return b.value


def window_class(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def is_maximized(hwnd):
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return False
    return wp.showCmd == SW_MAXIMIZE


def window_show_cmd(hwnd):
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    return wp.showCmd if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)) else None


def pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def proc_name_of(hwnd):
    """窗口所属进程的可执行文件名（不含扩展名），用作桌面名。"""
    p = pid_of(hwnd)
    if not p:
        return None
    h = kernel32.OpenProcess(0x1000, False, p)      # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_uint32(512)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                   ctypes.byref(size)):
            return None
        return os.path.splitext(os.path.basename(buf.value))[0]
    finally:
        kernel32.CloseHandle(h)


def foreground_fill_screen():
    """把前台「已最大化」窗口的底部空档补掉（个别程序不响应工作区变化）。"""
    h = user32.GetForegroundWindow()
    if not h or not is_maximized(h):
        return False

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_uint32)]

    mon = user32.MonitorFromWindow(h, 2)
    if not mon:
        return False
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    rc = RECT()
    user32.GetWindowRect(h, ctypes.byref(rc))
    if abs(rc.bottom - m.bottom) <= FILL_GAP_TOL and \
            abs(rc.right - m.right) <= FILL_GAP_TOL:
        return False
    user32.SetWindowPos(h, 0, m.left, m.top, m.right - m.left,
                        m.bottom - m.top,
                        SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOSENDCHANGING)
    return True


def reg_read(root, path, name, default=None):
    try:
        with winreg.OpenKey(root, path) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return v
    except OSError:
        return default


def accent_color():
    v = reg_read(winreg.HKEY_CURRENT_USER,
                 r"Software\Microsoft\Windows\DWM", "AccentColor")
    if v is None:
        return (0, 120, 212)
    return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def taskbar_light():
    v = reg_read(winreg.HKEY_CURRENT_USER,
                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                 "SystemUsesLightTheme", 1)
    return bool(v)


def tray_hwnd():
    return user32.FindWindowW("Shell_TrayWnd", None)


def tray_rect():
    h = tray_hwnd()
    if not h:
        return None
    rc = wintypes.RECT()
    if not user32.GetWindowRect(h, ctypes.byref(rc)):
        return None
    return (rc.left, rc.top, rc.right, rc.bottom)


def monitor_rect(hwnd):
    """窗口所在显示器的整块矩形（物理像素，跟 DPI awareness 同一口径）"""

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_uint32)]

    try:
        mon = user32.MonitorFromWindow(hwnd or 0, 2)      # NEAREST
        if not mon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        return (mi.rcMonitor.left, mi.rcMonitor.top,
                mi.rcMonitor.right, mi.rcMonitor.bottom)
    except Exception:
        return None


def onscreen_span(rect, mon):
    """rect 露在显示器内的尺寸（宽, 高）—— 被推到屏幕外的部分不算"""
    w = max(0, min(rect[2], mon[2]) - max(rect[0], mon[0]))
    h = max(0, min(rect[3], mon[3]) - max(rect[1], mon[1]))
    return w, h


def dock_position(rc, mon):
    """把任务栏按它停靠的那条边"推回"到展开时该在的位置。

    关键：任务栏自动隐藏时**高度不变、整体滑出屏幕**。实测（200% 缩放）
    底栏从 y=1984 整体滑到 y=2078（屏幕底 2080），只留 2px 在屏幕里，
    高度还是 96。所以要判断"收起了没有"，只能看露在屏幕内的部分，
    看高度是看不出来的。
    """
    w, h = rc[2] - rc[0], rc[3] - rc[1]
    if w >= h:                                    # 上/下停靠
        if abs(rc[3] - mon[3]) <= abs(rc[1] - mon[1]):
            return (rc[0], mon[3] - h, rc[2], mon[3])
        return (rc[0], mon[1], rc[2], mon[1] + h)
    if abs(rc[0] - mon[0]) <= abs(rc[2] - mon[2]):   # 左/右停靠
        return (mon[0], rc[1], mon[0] + w, rc[3])
    return (mon[2] - w, rc[1], mon[2], rc[3])


def is_mvd_desktop(name):
    return bool(name) and name.upper().startswith(MVD_PREFIX)


def app_name_from(name):
    """从 '[MVD] chrome' 里取出 chrome。

    桌面名是「特殊前缀 + 应用名」，所以不需要枚举窗口就能知道这个全屏桌面
    装的是谁。前缀后面的东西原样返回（那是我们自己写进去的进程名）。
    """
    if not name:
        return None
    if not name.upper().startswith(MVD_PREFIX):
        return None
    rest = name[len(MVD_PREFIX):].strip()
    return rest.strip(" -–—:：|/\\") or None


# ---------------------------------------------------------------- 虚拟桌面
def desktop_snapshot():
    """桌面列表（含名字）+ 当前桌面。不做任何窗口枚举。"""
    try:
        cur = pyvda.VirtualDesktop.current()
        cur_id = cur.id
        desks = pyvda.get_virtual_desktops()
    except Exception:
        return None
    out = []
    # get_virtual_desktops() 已按任务视图顺序返回。d.number 会为每个桌面
    # 再次调用 get_all_desktops() 并遍历列表，轮询时变成重复的 COM 工作。
    for number, d in enumerate(desks, 1):
        did = d.id
        try:
            nm = d.name or ""
        except Exception:
            nm = ""
        app = app_name_from(nm)
        out.append({"id": did, "number": number, "name": nm, "app": app,
                    "is_current": did == cur_id, "is_mvd": bool(app)})
    out = out[:MAX_DESKTOPS]
    for it in out:
        it["label"] = pill_label(it)
    return out


def find_desktop(desk_id):
    try:
        for d in pyvda.get_virtual_desktops():
            if d.id == desk_id:
                return d
    except Exception:
        pass
    return None


def desktop_count():
    try:
        return len(pyvda.get_virtual_desktops())
    except Exception:
        return 0


# ---------------------------------------------------------------- 补间
class Tween:
    """一小段进度：0 → 1，按缓动求值。到点了自己停，避免空转重绘。"""

    def __init__(self, dur):
        self.dur = dur
        self.t0 = 0.0
        self.active = False

    def start(self):
        self.t0 = time.time()
        self.active = True

    def stop(self):
        self.active = False

    def value(self, ease=None):
        if not self.active:
            return 1.0
        raw = (time.time() - self.t0) / self.dur
        if raw >= 1.0:
            self.active = False
            return 1.0
        return (ease or ui.ease_out)(raw)


# ---------------------------------------------------------------- 全局热键线程
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt", POINT)]


class HotkeyThread(threading.Thread):
    """独立线程 + 隐藏窗口 + 自己的消息循环。

    收到 WM_HOTKEY 只往队列里塞一个 id，实际动作全部由 Tk 主线程执行 ——
    这样所有 pyvda / COM 调用都留在同一个线程里，不用操心 COM 单元线程模型。
    """

    def __init__(self, out_queue):
        super().__init__(daemon=True)
        self.q = out_queue
        self.tid = None
        self.hwnd = None
        self.results = {}          # id -> (label, ok, errno)
        self._wndproc_ref = None   # 必须持引用，否则回调被 GC

    def run(self):
        try:
            self.tid = kernel32.GetCurrentThreadId()
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = "VDSuiteHotkeyWindow"
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(
                0, cls_name, "", 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                self.q.put(("hotkey_fail", 0, "CreateWindowEx 失败"))
                return
            for hid, (desc, cands) in HOTKEY_DEFS.items():
                label, ok, err = cands[0][0], False, 0
                for lab, mods, vk in cands:
                    if user32.RegisterHotKey(self.hwnd, hid, mods, vk):
                        label, ok = lab, True
                        break
                    err = kernel32.GetLastError()
                info = {"desc": desc, "label": label, "ok": ok, "err": err}
                self.results[hid] = info
                self.q.put(("hotkey", hid, info))
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.q.put(("hotkey_fire", int(msg.wParam), None))
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self.q.put(("hotkey_fail", 0, traceback.format_exc()[-300:]))

    def _wndproc(self, hwnd, msg, wp, lp):
        # 热键线程的窗口不做别的，全部交给系统默认处理
        return user32.DefWindowProcW(hwnd, msg, wp, lp)


# ---------------------------------------------------------------- 单实例
def _close_legacy_instances():
    """请旧实例正常退出，让它先还原窗口、桌面和任务栏。"""
    seen = set()
    closed = []
    my_pid = os.getpid()
    for title in LEGACY_TITLES:
        h = user32.FindWindowW(None, title)
        if not h:
            continue
        pid = pid_of(h)
        if not pid or pid == my_pid or pid in seen:
            continue
        seen.add(pid)
        ph = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not ph:
            return False
        try:
            if not user32.PostMessageW(h, 0x0010, 0, 0):  # WM_CLOSE
                return False
            if kernel32.WaitForSingleObject(ph, 3000) != 0:  # WAIT_OBJECT_0
                return False
            closed.append(pid)
        finally:
            kernel32.CloseHandle(ph)
    return bool(closed)


_mutex = kernel32.CreateMutexW(None, False, "VirtualDesktopBarMutex")
if (kernel32.GetLastError() == 183
        and not os.environ.get("VDB_NO_MUTEX")
        and "--probe" not in sys.argv):
    _old_closed = _close_legacy_instances()
    if _old_closed:
        time.sleep(0.2)
        kernel32.CloseHandle(_mutex)
        _mutex = kernel32.CreateMutexW(None, False, "VirtualDesktopBarMutex")
    else:
        sys.exit(0)


class Suite:
    def __init__(self):
        recovered_tray = recover_forced_taskbar()
        self.cfg = load_settings()
        if not os.path.exists(_SETTINGS):
            save_settings(self.cfg)

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.overrideredirect(True)

        self.emb = tk.Toplevel(self.root)
        self.emb.title(EMB_TITLE)
        self.emb.overrideredirect(True)
        self.emb.attributes("-topmost", True)
        self.emb.attributes("-transparentcolor", KEY_HEX)

        self.panel = tk.Toplevel(self.root)
        self.panel.title(PANEL_TITLE)
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        self.panel.attributes("-transparentcolor", KEY_HEX)
        self.panel.withdraw()

        self.accent = accent_color()
        self.dark = not taskbar_light()
        self.state = None
        self.photo = None
        self.m = None
        self.w = None
        self.h = None
        self.last_rc = None
        self.tray_out = True     # 任务栏当前是否露在屏幕里（收起时为 False）
        self.tray = None
        self.embedded = False
        self.embed_capacity = None   # Tk 嵌入窗口启动时的真实宽度
        self.embed_fallback = False  # 扩宽时改用已存在的浮窗，避开分层窗口黑块
        self.dock_rect = None    # 任务栏展开时的矩形（浮窗药丸的落点）
        self.tick = 0
        self.mvd_tick = 0

        # —— 单应用桌面联动
        self.in_mvd = False
        self.mvd_leave_seen = 0      # 连续几轮看到"不是单应用桌面"
        self.saved_autohide = None
        self.tray_goal = None        # None=不管 / True=必须收起 / False=必须展开
        self.tray_block_since = None
        self.tray_nudge = 0
        self.tray_pushes = 0         # 连续重推次数（成功一次就清零）
        self.tray_last_push = 0.0
        self.tray_gave_up = False
        self.forced_tray_hwnd = None
        self._tray_watch_stop = None
        self._tray_watch_ready = None
        self._tray_watch_proc = None
        self.mvd_enter_ts = 0
        self.immersed = False
        self.last_mvd_descs = None

        # —— 交互与动画
        self.hover_i = -1
        self.press_i = -1
        self.press_ts = 0.0
        self.hl_tw = Tween(HL_DUR)       # 高亮从一颗药丸走到另一颗
        self.hl_from = None              # 桌面 id（按 id 记，列表变了也不会错位）
        self.hl_to = None
        self.fw_state = "hidden"         # hidden / showing / shown / hiding
        self.fw_tw = Tween(FW_DUR)
        self.fw_dy = 0.0
        self.fw_scale = 1.0
        self._anim_on = False
        self._img_ids = {}
        self._panel_img_id = None
        self._fw_geom = None
        self._last_render_ms = 0.0
        self._panel_open_ts = 0.0     # 面板打开的时刻（宽限期用，见 panel_watch）
        self._panel_watch_job = None   # 面板关闭时不运行点击看门狗
        self._bg = None               # 实测的任务栏底色（圆角软合成用）
        self._bg_ts = 0.0
        self._flushing = False        # 防重入闸门，见 _flush_paint
        self._last_error_at = {}      # 高频回调的异常日志限速

        # —— 跳新桌面
        self.tracked = {}        # hwnd -> {"origin": id, "temp": id, "name": str}
        self.seen_max = {}       # hwnd -> 上一次是否最大化
        self.mvd_suppress = 0.0  # 这段时间内不自动建桌面（防「刚收回又被弹出去」）
        # 修饰键状态（见 note_mod / mvd_armed）。分两个变量，因为宽限不一样：
        # held = 此刻按住（按住期间每拍刷新）；tap = 本拍内快按快放。
        self.mod_vks = MVD_MODS.get(
            str(self.cfg.get("mvd_modifier", MVD_MOD_DEFAULT)).lower(),
            MVD_MODS[MVD_MOD_DEFAULT])[1]
        self.mod_held = 0.0
        self.mod_tap = 0.0
        self.hk_status = {}      # id -> {"desc","label","ok","err"}
        self.my_pid = os.getpid()
        if recovered_tray:
            self._log("启动时已恢复上次异常退出后隐藏的任务栏")
        atexit.register(self.release_forced_tray)

        self.canvas_e = tk.Canvas(self.emb, highlightthickness=0, bd=0,
                                  bg=KEY_HEX)
        self.canvas_e.pack(fill="both", expand=True)
        self.canvas_p = tk.Canvas(self.panel, highlightthickness=0, bd=0,
                                  bg=KEY_HEX)
        self.canvas_p.pack(fill="both", expand=True)
        self.canvases = [self.canvas_e]

        for c in self.canvases:
            c.bind("<ButtonPress-1>", self.on_press)
            c.bind("<ButtonRelease-1>", self.on_release)
            c.bind("<Motion>", self.on_motion)
            c.bind("<Leave>", lambda e: self.set_hover(-1))
        self.canvas_p.bind("<Motion>", self.on_panel_motion)
        self.canvas_p.bind("<Leave>", lambda e: self.set_panel_hover(-1))
        self.canvas_p.bind("<Button-1>", self.on_panel_click)
        self.canvas_p.bind("<Button-3>", lambda e: self.hide_panel())

        self.startup_dir = os.path.join(os.environ.get("APPDATA", ""),
                                        "Microsoft", "Windows",
                                        "Start Menu", "Programs", "Startup")
        self.panel_rows = []
        self.panel_layout = None
        self.panel_hover = -1
        self.canvas_e.bind("<Button-3>", lambda e: self.on_menu(e, self.emb_hwnd))
        # 外部发 WM_CLOSE（任务管理器结束任务、其它程序关窗）也走优雅退出，
        # 否则被关掉时会留下没收回的窗口和空的 [MVD] 桌面。
        for w in (self.root, self.emb, self.panel):
            try:
                name = w.title()
                w.protocol("WM_DELETE_WINDOW",
                           lambda name=name: self.close_requested(name))
            except Exception:
                pass
        for w in (self.root, self.panel):
            try:
                w.bind("<Escape>", lambda e: self.hide_panel())
            except Exception:
                pass

        self.root.update_idletasks()
        self.root.report_callback_exception = self._on_tk_error
        self.tk_root_hwnd = (user32.GetParent(self.root.winfo_id())
                             or self.root.winfo_id())
        self.emb_hwnd = user32.GetParent(self.emb.winfo_id()) or self.emb.winfo_id()
        self.panel_hwnd = (user32.GetParent(self.panel.winfo_id())
                           or self.panel.winfo_id())
        self.make_noactivate(self.tk_root_hwnd)
        self.make_noactivate(self.emb_hwnd)
        self.make_noactivate(self.panel_hwnd)
        self._native_events = []
        self._native_lock = threading.Lock()
        self._native_event_queued = False
        self._native_closing = False
        self.root.bind("<<NativePillMouse>>", self._drain_native_mouse)
        self.alpha_pill = NativePill(self._native_mouse,
                                     lambda e: self._log(f"原生药丸鼠标事件异常: {e!r}"))
        self.fw_hwnd = self.alpha_pill.hwnd
        self.alpha_image = None
        self.try_embed()
        # Tk 根窗口只保留事件循环；真正的悬浮药丸由原生分层窗口显示。
        try:
            self.root.withdraw()
        except Exception:
            pass

        # 热键线程
        self.hk_q = queue.Queue()
        self.hk_thread = HotkeyThread(self.hk_q)
        self.hk_thread.start()

        self.refresh(force=True)
        self.root.after(POLL_MS, self.poll)
        self.root.after(MVD_MS, self.mvd_poll)
        self.root.after(200, self.drain_hotkeys)

    # ---- 日志
    def _log(self, msg):
        try:
            with open(os.path.join(_HERE, "suite_life.log"), "a",
                      encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def _log_callback_error(self, where):
        now = time.monotonic()
        if now - self._last_error_at.get(where, -1e9) >= 60:
            self._last_error_at[where] = now
            self._log(f"{where} 出错:\n{traceback.format_exc()[-700:]}")

    def _on_tk_error(self, exc, val, tb):
        try:
            with open(os.path.join(_HERE, "suite_errors.log"), "a",
                      encoding="utf-8") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} callback ---\n")
                f.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass

    def make_noactivate(self, hwnd):
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    # ---- 配置项
    def toggle_cfg(self, key):
        was_on = bool(self.cfg[key])
        # 关闭联动必须先按旧配置执行离开清理；先把配置关掉会让
        # leave_mvd() 跳过任务栏还原。随后仍留在当前单应用桌面。
        if key == "mvd_link" and was_on and self.in_mvd:
            self.leave_mvd(restore_immersive=False)
        self.cfg[key] = not self.cfg[key]
        save_settings(self.cfg)
        if key == "mvd_link":
            if self.cfg["mvd_link"] and self.in_mvd:
                # 关闭联动期间用户可能改过任务栏设置；重新启用时以当下为基线。
                self.saved_autohide = taskbar_autohide()
                self.enter_mvd()
            elif not self.cfg["mvd_link"] and was_on:
                self.enter_mvd()
            # 扩宽后的药丸由浮窗接管。关闭联动后任务栏重新出现，必须立刻
            # 把浮窗提到它前面，否则在单应用桌面里会整颗消失。
            self.sample_tray()
            self.place_floating()
            self.update_floating_visibility()
        if key == "immersive_hide":
            self.set_immersed(self.in_mvd and self.cfg["immersive_hide"])
        self._log(f"cfg {key} = {self.cfg[key]}")
        if self.panel_visible():
            self.render_panel()

    # ---- 开机自启
    def _autostart_path(self):
        return os.path.join(self.startup_dir, "VirtualDesktopSuite.vbs")

    def _autostart_on(self):
        return os.path.exists(self._autostart_path())

    def toggle_autostart(self):
        p = self._autostart_path()
        try:
            if os.path.exists(p):
                os.remove(p)
            else:
                if _FROZEN:
                    exe, arg = sys.executable, ""
                else:
                    exe = sys.executable.replace("python.exe", "pythonw.exe")
                    if not os.path.exists(exe):
                        exe = sys.executable
                    arg = f' ""{_RESTART_TARGET}""'
                with open(p, "w", encoding="utf-8") as f:
                    f.write('CreateObject("WScript.Shell").Run '
                            f'"""{exe}""{arg}, 0, False\r\n')
        except Exception:
            pass
        if self.panel_visible():
            self.render_panel()

    # ---- 嵌入 / 定位
    def try_embed(self):
        """把药丸那一份挂成任务栏的子窗口。

        ⚠️ 这里是**唯一**能证明"嵌进去了没有"的地方，所以每一步都记日志。
        曾经吃过一次闷亏：`embedded` 被置成 True、日志一片正常，但任务栏里
        根本没有我们的窗口 —— 因为 SetParent 的返回值压根没看。现在四个
        值（目标、句柄、返回值、事后复读的父窗口）全部落盘，出问题时一眼
        能看出是"没执行"还是"执行了没成"。
        """
        tray = tray_hwnd()
        if tray and self.emb_hwnd:
            ctypes.set_last_error(0)
            prev = user32.SetParent(self.emb_hwnd, tray)
            err = ctypes.get_last_error()
            style = user32.GetWindowLongPtrW(self.emb_hwnd, GWL_STYLE)
            new_style = (style & 0x7FFFFFFF) | WS_CHILD | WS_VISIBLE
            user32.SetWindowLongPtrW(self.emb_hwnd, GWL_STYLE,
                                     ctypes.c_longlong(new_style))
            now_par = user32.GetParent(self.emb_hwnd)
            self.tray = tray
            self.embedded = (now_par == tray)
            if self.embed_fallback:
                user32.ShowWindow(self.emb_hwnd, SW_HIDE)
            self._log(f"embed: 任务栏={tray} 药丸={self.emb_hwnd} "
                      f"SetParent 返回={prev} err={err} "
                      f"事后父窗口={now_par} "
                      f"{'失败' if now_par != tray else '成功'}")
        else:
            self.embedded = False
            self._log(f"embed: 跳过（任务栏={tray} 药丸={self.emb_hwnd}）")

    def keep_on_top(self):
        if (self.embed_fallback and self.fw_hwnd and not self.immersed
                and self.floating_above_tray()):
            # FluentFlyout 等任务栏增强程序会把自己的透明窗口重新压到任务栏
            # 上层：药丸看得见，但鼠标会先命中它。浮窗接管嵌入药丸后，维持
            # 自己的最上层顺序，才能保证左右键都由 Canvas 收到。
            self.alpha_pill.set_z(HWND_TOPMOST)
            return
        if self.embedded and self.emb_hwnd and not self.embed_fallback:
            flags = SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
            if not self.immersed:
                flags |= SWP_SHOWWINDOW
            user32.SetWindowPos(self.emb_hwnd, HWND_TOP, 0, 0, 0, 0, flags)

    def _sync_embed_children(self, width, height):
        """修复 Tk 外框显示后，内部窗口仍隐藏或仍是旧尺寸的情况。"""
        repaired = False
        for hwnd in (self.emb.winfo_id(), self.canvas_e.winfo_id()):
            rc = RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
                continue
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            if (rc.right - rc.left != width or rc.bottom - rc.top != height
                    or not (style & WS_VISIBLE)):
                user32.SetWindowPos(hwnd, 0, 0, 0, width, height,
                                    SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                repaired = True
        return repaired

    def tray_retracted(self):
        """任务栏是不是被自动隐藏收走了（滑出屏幕，只留几个像素）。

        判据是"露在屏幕内的部分"，不是高度、也不是 IsWindowVisible：
        自动隐藏的任务栏窗口高度不变，被整体推到屏幕外，而且依然算"可见"。
        """
        return not self.tray_out

    def pointer_in_tray(self):
        """光标是不是正压在任务栏矩形里。

        这个判断是"任务栏能不能收起"的关键：光标在栏内时 explorer 会拒绝
        收起（实测 4/4 复现），此时再怎么写注册表都是白写。
        """
        rc = tray_rect()
        if rc is None:
            return False
        pt = POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return False
        return rc[0] <= pt.x <= rc[2] and rc[1] <= pt.y <= rc[3]

    def sample_tray(self):
        """采样任务栏：它在不在、展开着没有、展开时占哪块。

        dock_rect 只在任务栏**完整露在屏幕里**的时候才更新 —— 收起时它的
        rect 是"滑出去的那一份"，拿它当参考会把药丸摆到屏幕外。
        """
        rc = tray_rect()
        if rc is None:
            self.tray_out = False
            return None
        mon = monitor_rect(self.tray or tray_hwnd())
        if mon is None:
            mon = (0, 0, user32.GetSystemMetrics(0),
                   user32.GetSystemMetrics(1))
        tw, th = rc[2] - rc[0], rc[3] - rc[1]
        vw, vh = onscreen_span(rc, mon)
        self.tray_out = bool(user32.IsWindowVisible(self.tray or tray_hwnd())) \
            and (vw >= tw and vh >= th)
        if self.tray_out and th >= MIN_DOCK_H:
            self.dock_rect = rc
        if self.dock_rect is None:
            self.dock_rect = dock_position(rc, mon)
        return rc

    def reposition(self):
        if self.state is None:
            return
        rc = self.sample_tray()
        if rc is None:
            return
        dl, dt, dr_, db = self.dock_rect
        bar_h = db - dt
        if rc != self.last_rc:
            self._bg = None
        self.last_rc = rc

        labels = [s.get("label") or str(s["number"]) for s in self.state]
        m = compute_metrics(bar_h, labels)
        need_render = (m != self.m) or (self.w is None)
        # "变大了"单独记一笔：只有变大才会露出**没画过的**区域，才需要强制重画
        # （变小是裁掉，不会露馅）。见下面 _flush_paint。
        grew = (self.w is None) or (m["win_w"] > self.w)
        self.m = m
        w = m["win_w"]
        # 这两个是渲染尺寸，跟嵌没嵌进去无关 —— 放在 if 里的话，
        # 任务栏一时不存在（explorer 重启）就会变成 None，药丸直接空白。
        self.w, self.h = w, bar_h

        # 任务栏内嵌的是 Tk 的分层窗口。它从一颗药丸扩到多颗时，原生外框
        # 即使已变宽，Tk 自己仍可能把画布留在旧宽度；新增区域会持续是黑色，
        # 而且无法点击。不能依赖 SetWindowPos 强行修内部 HWND：那不会更新
        # Tk 的逻辑尺寸。改用本来就在运行的浮窗覆盖在任务栏原位置，避免
        # 再扩大有问题的嵌入窗口。只在第一次扩宽时切换，之后不来回抖动。
        if self.embed_capacity is None:
            self.embed_capacity = w
        elif w > self.embed_capacity and not self.embed_fallback:
            self.embed_fallback = True
            if self.emb_hwnd:
                user32.ShowWindow(self.emb_hwnd, SW_HIDE)
            self._log(f"嵌入窗口需从 {self.embed_capacity} 扩到 {w}，"
                      "改由浮窗显示任务栏药丸")

        # ⚠️⚠️ 顺序不能反：**先把内容画出来，再改窗口尺寸。**
        #     原来是 geometry()/SetWindowPos 在前、render() 在后。于是"窗口已
        #     经变大、内容还没画上"的那一帧露出来的是**未定义像素** —— 分层
        #     窗口（我们用了 -transparentcolor）在重新分配表面之后，新区域的
        #     像素是纯黑，而纯黑**不等于**键色 #010203，不会被抠透明，就实打实
        #     显示成黑块。用户 2026-09-23 报的"该出现新药丸的地方会黑个一秒钟
        #     左右"就是它：切桌面时合成器正在放切换动画，这一帧会被合成出来
        #     并且停留好几百毫秒。
        #     改成先 render 是**安全**的，因为药丸是左对齐画的（见 _draw_pills
        #     里 x 从 m["pad_l"] 起步）：多一颗药丸只会让窗口往右长，已有药丸
        #     一个都不动。内容先就位，后面那一帧才有东西可画。
        if need_render:
            self.render()

        if self.embedded and not self.embed_fallback:
            x, y = m["pad_l"] // 2, 0
            # Tk 的 Canvas 是 emb 内部的另一个 HWND。只把外层 emb_hwnd 用
            # SetWindowPos 扩宽时，内部 Canvas 可能仍停在旧宽度：新增的整条区域
            # 就是黑的，也收不到绑定在 Canvas 上的点击。先要求 Tk 同步内部尺寸。
            if self.canvas_e is not None:
                self.canvas_e.configure(width=w, height=bar_h)
            self.emb.geometry(f"{w}x{bar_h}+{x}+{y}")
            if hasattr(self.emb, "update_idletasks"):
                self.emb.update_idletasks()
            # 外框扩大后，Tk 的两个内部 HWND 可能仍保持旧宽度；经过沉浸模式
            # 的 ShowWindow(SW_HIDE) 后甚至会保留隐藏标志。先修好内部窗口，
            # 再让外框出现，避免把旧画面和黑色空区短暂显示给用户。
            user32.SetWindowPos(self.emb_hwnd, HWND_TOP, x, y, w, bar_h,
                                SWP_NOACTIVATE)
            repaired = self._sync_embed_children(w, bar_h)
            if (not self.immersed and not
                    (user32.GetWindowLongPtrW(self.emb_hwnd, GWL_STYLE)
                     & WS_VISIBLE)):
                user32.ShowWindow(self.emb_hwnd, SW_SHOW)
            if (grew or repaired) and not self.immersed:
                self._flush_paint(self.canvas_e, self.emb_hwnd)
            if repaired:
                self._log(f"嵌入内部窗口已同步: {w}x{bar_h}")

        self.place_floating()

    def fw_target(self):
        """浮窗该待的那块矩形（x, y, w, h）。算不出来就 None。"""
        if not self.dock_rect or not self.m:
            return None
        dl, dt, dr_, db = self.dock_rect
        return (dl + self.m["pad_l"] // 2, dt, self.m["win_w"], db - dt)

    def floating_above_tray(self):
        # 自动隐藏目标为 True 时留在任务栏后面，避免阻止 Explorer 收起；
        # 任务栏要显示时，浮窗是唯一可用的药丸，必须压在它前面。
        return (not self.embedded or
                (self.embed_fallback and
                 (not self.in_mvd or not self.cfg["mvd_link"]
                  or self.tray_goal is not True)))

    def place_floating(self):
        """把浮窗挪到目标位置。返回 True 表示真的动了窗口（位置或尺寸）。

        浮窗永远摆在任务栏"展开时"的位置 —— 任务栏收走后那个位置就空出来了。
        """
        t = self.fw_target()
        if t is None:
            return False
        x, y, w, h = t
        z_after = HWND_TOPMOST if self.floating_above_tray() else self.tray
        if t == self._fw_geom:
            # 位置没变，但仍然要钉一次 z 序（任务栏重建会让它飘）。
            self.alpha_pill.set_z(z_after)
            return False
        # 位置和图像一起提交给 UpdateLayeredWindow，不会露出尚未绘制的区域。
        self._fw_geom = t
        if self.alpha_image is not None:
            self.alpha_pill.render(self.alpha_image, x, y)
        self.alpha_pill.set_z(z_after)
        return True

    def _flush_paint(self, canvas, hwnd):
        """窗口刚变大之后，**立刻**把新露出来的那块画掉。

        ⚠️ 为什么 `update_idletasks()` 不够、必须 `update()`：Tk 自己的重绘走
        idle 队列（`update_idletasks` 能跑到），但**窗口自己收到的 WM_PAINT
        只有 `update()` 会处理**。少这一步的后果不只是"晚一帧"——下一次整块
        重画要等到下一轮 render()（最多 400ms 的轮询，甚至等到下一次动画），
        再叠上切桌面时合成器的延迟，就成了一秒左右的黑块。

        `RedrawWindow(.., RDW_UPDATENOW)` 是备一手：让窗口同步收一次 WM_PAINT，
        完全绕开 Tk 的事件队列。两条路都试，哪条生效都行。

        `_flushing` 闸门是必须的：`update()` 会把**待处理的所有事件**都跑掉，
        其中可能就有下一轮 refresh 的定时器 —— 那会再进来一次 reposition，
        没有闸门就会一层层套下去。
        """
        if self._flushing:
            return
        self._flushing = True
        try:
            try:
                user32.RedrawWindow(hwnd, None, None,
                                    RDW_INVALIDATE | RDW_UPDATENOW
                                    | RDW_ALLCHILDREN)
            except Exception:
                pass
            canvas.update_idletasks()
            canvas.update()
        except Exception:
            self._log_callback_error("强制重画")
        finally:
            self._flushing = False

    def update_floating_visibility(self):
        """任务栏收起来了 → 露出浮窗药丸；任务栏展开 → 交给嵌入的那一份。

        出场/退场不是"啪"一下出现，而是从任务栏里滑出来 —— 这一段是我们
        自己的像素在动，跟系统开关无关（见文档里关于动画的说明）。

        注意别对浮窗调 lift()：z 序是靠 SetWindowPos(.., self.tray, ..) 钉成
        "紧跟在任务栏下面"的，抬到任务栏上面虽然像素一样，但会挡住任务栏
        自己和开始按钮的鼠标消息。
        """
        want = (not self.immersed) and (self.tray_retracted() or not self.embedded
                                       or self.embed_fallback)
        if want and self.embed_fallback:
            # 切换显示层时直接呈现完整药丸；过渡动画会让旧黑块多留一帧。
            if self.fw_state != "shown":
                self.fw_tw.stop()
                self.fw_state = "shown"
                self.fw_dy, self.fw_scale = 0.0, 1.0
                self.render()
                self.place_floating()
                self.alpha_pill.show()
            return
        if want and self.fw_state == "hidden":
            self.fw_state = "showing"
            self.fw_tw.start()
            # ⚠️⚠️ 出场这一支同样要"先画内容、后显示窗口"。
            #     原来是先 deiconify()：那一刻窗口里的像素还是上一轮的（甚至
            #     从没画过）内容，得等 anim_tick 的第一帧（28ms 后）才补上；
            #     而且它跟"刚多了一颗药丸"叠在一起 —— 窗口还是旧尺寸（少一颗），
            #     紧接着 refresh→place_floating 把它撑大，新露出来的那条又是
            #     未定义像素。两步叠起来，正好是用户看到的"该出现新药丸的地方
            #     黑一下"。现在 step_float + render 先把出场帧画好，再显示。
            self.step_float()          # 让 fw_dy/fw_scale 落到"刚出场"那一帧
            self.render()              # 内容就位（此时窗口还没显示）
            self.place_floating()      # 尺寸也定好（仍未显示）
            self.alpha_pill.show()
            self.start_anim()
        elif want and self.fw_state == "hiding":
            self.fw_state = "showing"          # 刚要走又回来了，接着演
            self.fw_tw.start()
            self.start_anim()
        elif not want and self.fw_state in ("showing", "shown"):
            self.fw_state = "hiding"
            self.fw_tw.start()
            self.start_anim()

    def step_float(self):
        """推进浮窗那一小段补间。返回 True 表示还在动。"""
        if self.fw_state in ("hidden", "shown"):
            self.fw_dy, self.fw_scale = 0.0, 1.0
            return False
        p = self.fw_tw.value()
        if self.fw_state == "showing":
            self.fw_dy = (1.0 - p) * 14.0
            self.fw_scale = 0.94 + 0.06 * p
            if not self.fw_tw.active:
                self.fw_state = "shown"
                self.fw_dy, self.fw_scale = 0.0, 1.0
        else:                                   # hiding
            self.fw_dy = p * 12.0
            self.fw_scale = 1.0 - 0.06 * p
            if not self.fw_tw.active:
                self.fw_state = "hidden"
                self.fw_dy, self.fw_scale = 0.0, 1.0
                self.alpha_pill.hide()
                return False
        return True

    # ---- 沉浸
    def set_immersed(self, on):
        if on == self.immersed:
            return
        self.immersed = on
        if on:
            self.alpha_pill.hide()
            self.fw_state = "hidden"
            if self.emb_hwnd:
                user32.ShowWindow(self.emb_hwnd, SW_HIDE)
        else:
            self.reposition()
            self.update_floating_visibility()
            self.render()
        self._log(f"immersive={'on' if on else 'off'}")

    # ---- 单应用桌面联动
    def _start_tray_watchdog(self, tray):
        nonce = f"{os.getpid()}_{time.monotonic_ns()}"
        ready_name = f"Local\\VDSuiteTrayReady_{nonce}"
        stop_name = f"Local\\VDSuiteTrayStop_{nonce}"
        ready = kernel32.CreateEventW(None, True, False, ready_name)
        stop = kernel32.CreateEventW(None, True, False, stop_name)
        if not (ready and stop):
            for handle in (ready, stop):
                if handle:
                    kernel32.CloseHandle(handle)
            return False
        proc = None
        try:
            with open(_TRAY_HIDE_MARKER, "w", encoding="ascii") as f:
                f.write(f"{os.getpid()} {tray} "
                        f"{int(bool(self.saved_autohide))}\n")
                f.flush()
                os.fsync(f.fileno())
            helper_dir = getattr(sys, "_MEIPASS", _HERE) if _FROZEN else _HERE
            helper = os.path.join(helper_dir, "tray_watchdog.exe")
            command = [helper, str(os.getpid()), ready_name, stop_name,
                       _TRAY_HIDE_MARKER]
            proc = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
            if kernel32.WaitForSingleObject(ready, 7000) != 0 \
                    or proc.poll() is not None:
                raise RuntimeError("任务栏恢复看护未就绪")
            self._tray_watch_ready = ready
            self._tray_watch_stop = stop
            self._tray_watch_proc = proc
            return True
        except Exception as exc:
            self._log(f"任务栏恢复看护启动失败，保留 Shell 自动隐藏: {exc!r}")
            kernel32.SetEvent(stop)
            for handle in (ready, stop):
                kernel32.CloseHandle(handle)
            try:
                os.remove(_TRAY_HIDE_MARKER)
            except OSError:
                pass
            return False

    def _stop_tray_watchdog(self):
        stop = self._tray_watch_stop
        if stop:
            kernel32.SetEvent(stop)
        for handle in (self._tray_watch_ready, stop):
            if handle:
                kernel32.CloseHandle(handle)
        self._tray_watch_ready = None
        self._tray_watch_stop = None
        self._tray_watch_proc = None

    def force_hide_tray(self):
        """Hide the actual Shell window for this exclusive desktop."""
        if not self.cfg["mvd_link"] or self.tray_goal is not True:
            return
        if self.forced_tray_hwnd:
            # Explorer can show its window again after a focus or desktop
            # transition. Reassert only if that actually happened.
            if (is_window(self.forced_tray_hwnd)
                    and user32.IsWindowVisible(self.forced_tray_hwnd)):
                user32.ShowWindow(self.forced_tray_hwnd, SW_HIDE)
            return
        tray = tray_hwnd()
        if not tray or not user32.IsWindowVisible(tray):
            return
        if not self._start_tray_watchdog(tray):
            return
        user32.ShowWindow(tray, SW_HIDE)
        if user32.IsWindowVisible(tray):
            self._stop_tray_watchdog()
            try:
                os.remove(_TRAY_HIDE_MARKER)
            except OSError:
                pass
            self._log("任务栏物理隐藏未生效")
            return
        self.forced_tray_hwnd = tray
        self._log(f"独占桌面任务栏已物理隐藏 {tray}")

    def release_forced_tray(self):
        tray = getattr(self, "forced_tray_hwnd", None)
        if not tray:
            return
        if is_window(tray) and not user32.IsWindowVisible(tray):
            user32.ShowWindow(tray, SW_SHOW)
        if not is_window(tray) or user32.IsWindowVisible(tray):
            self.forced_tray_hwnd = None
            try:
                os.remove(_TRAY_HIDE_MARKER)
            except OSError:
                pass
            self._stop_tray_watchdog()
            self._log("离开独占桌面，任务栏窗口已恢复")

    def enter_mvd(self):
        # ⚠️ 允许重复进入，而且**第二次进来不能重记 saved_autohide**。
        #    这条路真的会被走两次：按 HK_LAUNCH 热键时如果已经在单应用桌面上，
        #    会再建一个桌面再切过去、再进一次。那时 autohide 已经被我们设成
        #    True 了，拿它当"进入前的状态"存下来，离开时就会"还原"成一个隐藏的
        #    任务栏 —— 用户从此看不到任务栏，且没有任何地方会纠正它。
        was_in = self.in_mvd
        self.in_mvd = True
        if self.embed_fallback:
            self.place_floating()
        self.mvd_leave_seen = 0
        self.mvd_enter_ts = time.time()
        if not was_in:
            self.saved_autohide = taskbar_autohide()
        if self.cfg["mvd_link"]:
            # 只把目标记下来，真正的收留在 apply_tray_goal 里做 ——
            # 那一步会核验物理结果，收不掉还会重推（见那个函数的注释）。
            self.tray_goal = True
            # ⚠️⚠️ 重推计数必须**一起清零**，这是"切过去任务栏没自动隐藏"的修法。
            #     apply_tray_goal 里有一套退避：推够 TRAY_RETRY_QUIET(10) 次还
            #     收不起来就 tray_gave_up=True，此后**只核验、不再推物理收起**。
            #     这套状态原来是跨会话残留的 —— 上一轮因为光标压在任务栏上没收
            #     起来，攒到 10 次放弃；下一轮进来计数还挂在那儿，于是一进去就
            #     躺平（开关写对了也不推）。表现出来就是"有时候切过去任务栏死活
            #     不隐藏"，而且越用越容易复现。
            self.tray_nudge = 0
            self.tray_pushes = 0
            self.tray_last_push = 0.0            # 进桌面这一次要**立刻**推
            self.tray_gave_up = False
            self.tray_block_since = None
            self._log(f"进入单应用桌面 → 任务栏目标=收起"
                      f"（进入前 autohide={self.saved_autohide}"
                      f"{'；已在其中，沿用原记录' if was_in else ''}）")
            self.apply_tray_goal()
            self.force_hide_tray()
            self.sample_tray()
            self.update_floating_visibility()
        if not was_in:
            # 从热键、手势或药丸进入时，Shell 可能仍把原桌面窗口当作前台。
            # 只在进入的这一次补焦点，避免常驻轮询抢走用户操作。
            try:
                desktop_id = pyvda.VirtualDesktop.current().id
                self.root.after(80, lambda tid=desktop_id:
                                self.focus_mvd_window(tid))
            except Exception:
                pass
        if self.cfg["immersive_hide"]:
            self.set_immersed(True)

    def leave_mvd(self, restore_immersive=True):
        self.release_forced_tray()
        self.in_mvd = False
        if self.embed_fallback:
            self.place_floating()
        self.mvd_leave_seen = 0
        if self.cfg["mvd_link"] and self.saved_autohide is not None:
            cur = taskbar_autohide()
            if cur is None or cur == (True if self.tray_goal else False):
                self.tray_goal = self.saved_autohide
                # 还原这一次也要**立刻**推一次，别被上一轮的退避状态拖住
                # （同 enter_mvd 里的注释）。
                self.tray_pushes = 0
                self.tray_last_push = 0.0
                self.tray_gave_up = False
                self.tray_block_since = None
                self._log(f"离开单应用桌面 → 任务栏还原为 {self.saved_autohide}")
                self.apply_tray_goal()
            else:
                # 期间有人手动动过（热键、或另一个程序在抢这个开关），
                # 就别拿旧值去覆盖人家的选择。
                self.tray_goal = None
                self._log("离开单应用桌面 → 任务栏状态已被手动改动，不还原")
        else:
            self.tray_goal = None
        if restore_immersive:
            self.set_immersed(False)

    def apply_tray_goal(self):
        """把"任务栏该收着还是该展着"这件事**核验到物理结果**。

        ⚠️ 这里是问题一（"第一次进单应用桌面会藏任务栏，手动切走再切回来
        就不藏了"）的修法。三个原因叠加：

          1. ABM_SETSTATE 只写**设置**；explorer 真把任务栏滑走是另一步，
             而且**光标停在任务栏矩形内时会拒绝执行**。而"点药丸切桌面"
             这个动作天然让光标停在任务栏上，所以手动切回去那次必然失败；
          2. 光靠"进入/离开"这两个事件驱动，中间只要漏一次或状态抖一下，
             后面就再没人管了；
          3. 别的程序（历史上是 ToggleTaskbar.exe）也占着同一个开关，
             它一按就把我们设好的值翻回去。

        所以改成：**每轮轮询都核验目标状态**，不一致就纠正，收不掉就重推；
        光标压在栏上时不硬来（那时按 Windows 自己的规矩就该显示），
        等它离开立刻补上。

        ⚠️ 重推必须**有退避、有上限**。第一版是无条件每轮推一次，实测在一
        个 explorer 死活不肯收的会话里（锁屏、没有前台窗口，自动隐藏那条
        鼠标消息驱动的路根本没跑）20 秒推了 45 次，日志刷屏、注册表也跟着
        写。推不动不是"推得不够狠"，停手才对 —— 目标状态留着，下一轮照旧
        核验，条件一变（光标走开、设置被翻、离开再进入）立刻继续。
        """
        want = self.tray_goal
        if want is None or not self.cfg["mvd_link"]:
            return
        if (self.forced_tray_hwnd and self._tray_watch_proc is not None
                and self._tray_watch_proc.poll() is not None):
            self._log("任务栏恢复看护意外退出，立即显示任务栏")
            self.release_forced_tray()
            if self.saved_autohide is not None:
                set_taskbar_autohide(self.saved_autohide)
            self.tray_goal = None
            return
        if want and self.forced_tray_hwnd:
            self.force_hide_tray()
        # 现场重新采样一次：tray_out 平时只在 refresh 的状态签名变化时才更新，
        # 拿一个可能过期的值来判断"收起来了没有"会误判。
        self.sample_tray()
        cur = taskbar_autohide()
        if cur is None:
            return
        if cur != want:
            # 设置本身被改了（别的程序抢开关、或用户手动动了）。这一类必须
            # 纠正：写一下很便宜，不纠正就永远不对。顺便把重推计数清零 ——
            # 局面变了，重新给足机会。
            set_taskbar_autohide(want)
            self.tray_nudge += 1
            self.tray_pushes = 0
            self.tray_gave_up = False
            self._log(f"任务栏开关={cur} 与目标 {want} 不符 → 已纠正"
                      f"（第 {self.tray_nudge} 次）")
            return
        if want == self.tray_retracted():
            self.tray_block_since = None
            self.tray_pushes = 0
            self.tray_gave_up = False
            return
        if self.pointer_in_tray():
            # 光标压在任务栏矩形里时 explorer 会拒绝收起（实测 4/4 复现），
            # 三种催法都无效。这不是能"推"动的事：不计数、不硬来，等它离开。
            # ⚠️ 而"点药丸切桌面"这个动作**天然**让光标落在任务栏上 —— 用户
            #    会以为"没有自动隐藏"，其实只是 Windows 的规矩：光标在栏上时
            #    任务栏本来就该显示。所以这里必须等，而且绝不能因为它一直不动
            #    就转去重推（那会把 explorer 正在进行的收起打断）。
            if self.tray_block_since is None:
                self.tray_block_since = time.time()
            return
        if self.tray_block_since is not None:
            held = (time.time() - self.tray_block_since) * 1000
            self.tray_block_since = None
            # ⚠️ 光标刚离开的这一刻是**最关键的一次推送**：explorer 的自动隐藏
            #    是鼠标事件驱动的，设置写对了它也不一定马上重算。主动推一下，
            #    把收起从"等它想起来"变成"立刻开始"；顺便把重推计数清零 ——
            #    拦路的光标走了，局面变了，重新给足耐心。
            self.tray_pushes = 0
            self.tray_gave_up = False
            self.tray_last_push = 0.0
            set_taskbar_autohide(want)
            self.tray_nudge += 1
            self._log(f"光标离开任务栏（在栏上停了 {held:.0f} ms）"
                      f"→ 立刻推一次，让 explorer 开始收起")
            return

        now = time.time()
        # ⚠️ 观察窗：explorer 收起要 0.4~1.2 秒，推完立刻核验必然还是"没收起"，
        #    于是再推一次、把它的收起过程从头来过。见 TRAY_OBSERVE 的注释。
        if now - self.tray_last_push < TRAY_OBSERVE:
            return
        if self.tray_pushes >= TRAY_RETRY_MAX:
            # 推够了还是不灵 → 每隔 TRAY_RETRY_GAP 再温一次，避免彻底放弃；
            # 到 TRAY_RETRY_QUIET 次之后连推都不推了，只留核验（写注册表
            # 在 explorer 不肯动的时候没有任何意义）。
            if self.tray_pushes >= TRAY_RETRY_QUIET:
                if not self.tray_gave_up:
                    self.tray_gave_up = True
                    self._log(f"目标={want} 推了 {self.tray_pushes} 次仍未生效，"
                              f"停手只做核验（状态一变会继续）")
                return
            if now - self.tray_last_push < TRAY_RETRY_GAP:
                return
        self.tray_pushes += 1
        self.tray_last_push = now
        set_taskbar_autohide(want)
        self.tray_nudge += 1
        self._log(f"开关对、物理结果不对（目标={want} "
                  f"实际收起={self.tray_retracted()}）→ 推第 "
                  f"{self.tray_pushes} 次")

    def mvd_step(self, state):
        cur_item = next((s for s in state if s["is_current"]), None)
        cur_is_mvd = bool(cur_item and cur_item["is_mvd"])
        # ⚠️ "名字还没读到"的兜底：当前桌面的 id 如果正是**我们自己刚建出来的**
        #    那个临时桌面，那它一定算单应用桌面 —— 不能因为这一轮没读到 [MVD]
        #    前缀就判成"离开了"。少了这一条，刚切过去就可能被误判离开：任务栏
        #    先按目标收起来、又被还原回去，来回写两遍注册表，用户看到的就是
        #    "任务栏没自动隐藏"（或者闪一下）。用 id 判比用名字判稳。
        if not cur_is_mvd and cur_item is not None:
            cur_is_mvd = cur_item["id"] in {r["temp"]
                                            for r in self.tracked.values()}
        if cur_is_mvd and not self.in_mvd:
            # 进入：立刻响应。要藏任务栏就得赶在用户手离开之前尝试，
            # 晚一轮的成功率更低（那时光标已经停在任务栏上了）。
            self.enter_mvd()
        elif not cur_is_mvd and self.in_mvd:
            # 离开：要连续确认几轮。切桌面动画期间会有一两帧读到中间态，
            # 立刻响应会 enter/leave 来回抖，抖一次写两遍注册表，
            # 看起来就是"任务栏闪了一下"。
            self.mvd_leave_seen += 1
            if self.mvd_leave_seen >= MVD_LEAVE_CONFIRM:
                self.leave_mvd()
        elif cur_is_mvd and self.cfg["fullscreen_fill"] and \
                time.time() - self.mvd_enter_ts > 0.8:
            if foreground_fill_screen():
                self.mvd_enter_ts = time.time() + 10
                self._log("全屏补位：已把前台窗口拉到整屏")
        else:
            self.mvd_leave_seen = 0
        return cur_is_mvd

    # ---- 跳新桌面（原 MVD 的活）
    def guard_minimize_button(self, hwnd, rec):
        """Disable the standard minimize command only while the window is exclusive."""
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        if not (style & WS_MINIMIZEBOX):
            return
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style & ~WS_MINIMIZEBOX)
        if user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_MINIMIZEBOX:
            self._log(f"独占窗口 {hwnd} 的最小化按钮无法禁用")
            return
        rec["minimize_box_removed"] = True
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        self._log(f"独占窗口 {hwnd} 的标准最小化按钮已禁用")

    def restore_minimize_button(self, hwnd, rec):
        if not rec.get("minimize_box_removed") or not is_window(hwnd):
            return
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style | WS_MINIMIZEBOX)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)

    def _eligible(self, hwnd):
        """这个窗口该不该被甩到新桌面"""
        if not is_window(hwnd) or hwnd == user32.GetDesktopWindow():
            return False
        if pid_of(hwnd) == self.my_pid:                 # 自己
            return False
        if hwnd in (self.fw_hwnd, self.emb_hwnd, self.panel_hwnd):
            return False
        cls = window_class(hwnd)
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                   "Windows.UI.Core.CoreWindow", "ApplicationFrameWindow"):
            return False
        if not window_title(hwnd):                       # 无标题的多半是壳
            return False
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW:
            return False
        return True

    def launch_to_new_desktop(self, hwnd, why="hotkey"):
        """把 hwnd 甩到一个新建的桌面上并切过去、最大化。"""
        if hwnd in self.tracked:
            self.restore_from_new_desktop(hwnd, why="hotkey-again")
            return True
        if not self._eligible(hwnd):
            self._log(f"跳过（窗口不符合条件）hwnd={hwnd}")
            return False
        n = desktop_count()
        if n >= MAX_DESKTOPS_HARD:
            self._log(f"桌面数已达上限 {n}，不再新建")
            return False
        name = proc_name_of(hwnd) or "app"
        was_max = is_maximized(hwnd)          # 记下来，收回时好恢复原样
        try:
            origin = pyvda.AppView(hwnd=hwnd).desktop
            origin_id = origin.id
        except Exception:
            # 没有可靠的原桌面就不能保证失败或退出时把窗口送回去。
            self._log(f"跳过（无法确认窗口原桌面）hwnd={hwnd}")
            return False
        d = None
        moved = False
        try:
            d = pyvda.VirtualDesktop.create()
            try:
                d.rename(f"{MVD_PREFIX} {name}")
            except Exception as e:
                self._log(f"改名失败（不影响跳桌面）: {type(e).__name__}")
            pyvda.AppView(hwnd=hwnd).move(d)
            moved = True
            d.go()
            if not was_max:
                user32.ShowWindow(hwnd, SW_MAXIMIZE)
            self.tracked[hwnd] = {"origin": origin_id, "temp": d.id,
                                  "name": name, "was_max": was_max}
            self.guard_minimize_button(hwnd, self.tracked[hwnd])
            self.seen_max[hwnd] = True
            self._log(f"新建单应用桌面 [{MVD_PREFIX} {name}]，窗口 {hwnd} 已移入"
                      f"（触发={why}）")
            # ⚠️⚠️ 切过去之后**立刻**进单应用桌面，别等下一轮轮询。
            #     d.go() 内部就是 SwitchDesktop，桌面是同步切过去的（切换动画
            #     只是外观）；但要靠 desktop_snapshot() 认出"当前桌面就是那个
            #     [MVD] 桌面"得等下一轮 refresh —— 最多 400ms（POLL_MS），再叠上
            #     apply_tray_goal 的观察窗 TRAY_OBSERVE(0.85s)，用户在切过去之后
            #     盯着看一秒多，看到的都是"任务栏还杵在那儿没隐藏"。
            #     这里我们**知道**刚切到哪个桌面，直接把目标推给 explorer。
            #     万一 d.go() 没真的生效（少见），下一轮 mvd_step 会用 id 认出来
            #     并纠偏（见 mvd_step 里那段"名字还是旧的"兜底）。
            self.enter_mvd()
            # 让药丸尽快跟上（新桌面要占一颗药丸）。40ms 足够 SwitchDesktop 生效，
            # 又不至于让用户看出"第二颗药丸慢半拍才冒出来"。
            self.root.after(40, self.force_refresh)
            return True
        except Exception:
            self._log("跳新桌面失败:\n" + traceback.format_exc()[-500:])
            if hwnd in self.tracked:
                self.restore_from_new_desktop(hwnd, why="launch-error")
                if self.in_mvd:
                    self.leave_mvd()
            else:
                back = not moved
                if moved:
                    try:
                        pyvda.AppView(hwnd=hwnd).move(origin)
                        back = True
                    except Exception:
                        self._log(f"回滚窗口 {hwnd} 失败，保留临时桌面以免窗口丢失")
                if d is not None and back:
                    try:
                        if not d.apps_by_z_order():
                            d.remove(fallback=origin)
                    except Exception:
                        self._log("回滚临时桌面失败:\n"
                                  + traceback.format_exc()[-300:])
            return False

    def restore_from_new_desktop(self, hwnd, why="restore"):
        rec = self.tracked.pop(hwnd, None)
        if not rec:
            return False
        self.restore_minimize_button(hwnd, rec)
        try:
            was_on_temp = (pyvda.VirtualDesktop.current().id == rec["temp"])
        except Exception:
            was_on_temp = False
        origin = find_desktop(rec["origin"]) if rec["origin"] else None
        moved = False
        if is_window(hwnd) and origin is not None:
            try:
                pyvda.AppView(hwnd=hwnd).move(origin)
                moved = True
            except Exception:
                pass
        # 搬回原桌面后把窗口恢复成原来的样子：我们替它最大化的就还原，
        # 否则它会顶着一个最大化状态待在普通桌面上。
        if is_window(hwnd) and not rec.get("was_max") and is_maximized(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # RemoveDesktop 会把桌面上的其他窗口转移到 fallback。不能用
        # apps_by_z_order() 是否为空作前提：关闭目标窗口时，别的应用或尚未
        # 消失的应用视图仍可能留在临时桌面，旧逻辑会把它永久遗留在那里。
        removed = self.remove_owned_desktop(rec["temp"], rec["origin"])
        # 后台窗口关闭时，用户可能已在第三个桌面；清理不应突然拉走用户。
        if origin is not None and was_on_temp and not removed:
            try:
                origin.go()
            except Exception:
                pass
        # 跨桌面搬窗口会让 showCmd 有一瞬间不算最大化，不压住的话
        # 下一帧 220ms 的轮询会把它误判成「刚被最大化」，立刻又建一个桌面。
        if is_window(hwnd):
            self.seen_max[hwnd] = is_maximized(hwnd)
        self.mvd_suppress = time.time() + 1.5
        self._log(f"还原窗口 {hwnd} 回原桌面（触发={why}，moved={moved}，"
                  f"桌面已删除={removed}）")
        return True

    def remove_owned_desktop(self, temp_id, origin_id, attempt=0):
        """删除本程序建的桌面；Shell 会把剩余窗口送到原桌面。"""
        temp = find_desktop(temp_id)
        if temp is None:
            return True
        fallback = find_desktop(origin_id) if origin_id else None
        if fallback is None or fallback.id == temp_id:
            try:
                fallback = next((d for d in pyvda.get_virtual_desktops()
                                 if d.id != temp_id), None)
            except Exception as exc:
                self._log(f"查找回退桌面失败 {temp_id}: {exc!r}")
                return False
        if fallback is None:
            self._log(f"临时桌面删除失败：没有可用的回退桌面 {temp_id}")
            return False
        try:
            temp.remove(fallback=fallback)
            self._log(f"临时桌面已删除 {temp_id} → {fallback.id}")
            return True
        except Exception as exc:
            # 关窗时 Shell 的应用视图有短暂的销毁过渡；只在失败时低频重试。
            if attempt < 3 and not getattr(self, "_quitting", False):
                self.root.after(350 * (attempt + 1),
                                lambda: self.remove_owned_desktop(
                                    temp_id, origin_id, attempt + 1))
            else:
                self._log(f"临时桌面删除失败 {temp_id}: {exc!r}")
            return False

    def mod_label(self):
        """当前配的修饰键叫什么（面板上要写出来，不然用户不知道按哪个键）"""
        return MVD_MODS.get(
            str(self.cfg.get("mvd_modifier", MVD_MOD_DEFAULT)).lower(),
            MVD_MODS[MVD_MOD_DEFAULT])[0]

    def note_mod(self):
        """每拍记一次修饰键状态。**必须每拍调**，别想着"要用的时候再读"。

        为什么必须每拍调：低位(0x0001) 的语义是"自**上一次调用**以来按过"。
        不调它就一直在攒，等真要用的时候读出来的是"很久以前按过" ——
        这正是 2026-09-23 误触发的来源之一（见 MVD_MODS 上方的注释）。

        两个位分别落到两个变量上，因为后面给的宽限不一样：
          · 0x8000 → mod_held  此刻按着（用户按住不动、慢慢点最大化）
          · 0x0001 → mod_tap   本拍内按过（快按快放，两次轮询之间就松手了）
        组合键（比如 Ctrl+Shift）要求**每一个键都按着**才算 held。
        """
        now = time.time()
        held, tapped = True, False
        for vk in self.mod_vks:
            try:
                st = user32.GetAsyncKeyState(vk)
            except Exception:
                return
            if not (st & 0x8000):
                held = False
            if st & 0x0001:
                tapped = True
        if held:
            self.mod_held = now
        if tapped:
            self.mod_tap = now

    def mvd_armed(self, now=None):
        """这次"最大化"该不该独占一个新桌面？

        ⚠️ 默认（`mvd_shift_only=True`）**普通最大化什么都不做**。这是用户
        2026-09-23 明确提出的设计要求：原来的逻辑是"只要点最大化就一定新建
        虚拟桌面把窗口扔过去独占"，等于每个窗口都被强行独占，太吵。独占必须
        是**他主动要求的**，两条路：

          · 按住修饰键（默认 Shift）再去最大化（点最大化按钮 / 双击标题栏 /
            Win+↑ 都算）
          · 按全局热键 Ctrl+Alt+Shift+X（走 launch_to_new_desktop，不经这里）

        ⚠️ 判据是"**按住**"，不是"最近按过" —— 这个区别是 2026-09-23 真机
        撞出来的：原来只有一个 0.6 秒宽窗口，用户在中文输入法里打字时 Shift
        被按得很频繁，窗口被一次次续期，于是"打字期间一直 armed"，测试窗口
        普通最大化就被误判成 Shift+最大化（日志写着"触发=maximize"，而当时
        前台就是微信）。现在按住走 MOD_HOLD_WINDOW、快按快放走 MOD_TAP_WINDOW
        （比一拍还短），打字那种"按一下就松、而且松的时候手在键盘上不可能
        同时在点最大化按钮"就再也不会命中了。

        这一个函数把三种配置都收敛到一处，别再在 mvd_poll 里散着判：
          auto_app_desktop=False                  → 永不自建（只有热键）
          auto_app_desktop=True, shift_only=False → 老行为：任何最大化都独占
          auto_app_desktop=True, shift_only=True  → 只有 修饰键+最大化 才独占
        """
        if not self.cfg.get("auto_app_desktop"):
            return False
        if not self.cfg.get("mvd_shift_only", True):
            return True
        t = now or time.time()
        if (t - self.mod_held) <= MOD_HOLD_WINDOW:
            return True
        return (t - self.mod_tap) <= MOD_TAP_WINDOW

    def recover_minimized_window(self, hwnd, rec):
        now = time.time()
        if (rec.get("min_restore_attempts", 0) >= 3
                or now - rec.get("last_min_restore", 0) < 0.55):
            return
        rec["last_min_restore"] = now
        rec["min_restore_attempts"] = rec.get("min_restore_attempts", 0) + 1
        rec["minimize_guard_until"] = now + 1.5
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        try:
            if pyvda.VirtualDesktop.current().id == rec["temp"]:
                # ShowWindow changes placement, but Show Desktop can leave the
                # Shell in front. Activate the real application view as well.
                pyvda.AppView(hwnd=hwnd).switch_to()
                user32.SetForegroundWindow(hwnd)
        except Exception as exc:
            self._log(f"独占窗口 {hwnd} 恢复前台失败: {exc!r}")
        self._log(f"独占窗口 {hwnd} 最小化恢复尝试 "
                  f"{rec['min_restore_attempts']}/3，"
                  f"可见={bool(user32.IsWindowVisible(hwnd))} "
                  f"最小化={bool(user32.IsIconic(hwnd))} "
                  f"前台={user32.GetForegroundWindow() == hwnd}")

    def mvd_poll(self):
        """监视前台窗口：Shift+最大化 → 跳新桌面；已跟踪的窗口还原/关闭 → 收回来。"""
        try:
            self.mvd_tick += 1
            self.note_mod()                    # 每拍记一次修饰键状态（必须每拍）
            hwnd = user32.GetForegroundWindow()
            # 1) 已跟踪的窗口：取消最大化、关闭、或按热键 → 收回
            for h in list(self.tracked.keys()):
                if not is_window(h):
                    self.restore_from_new_desktop(h, why="closed")
                    continue
                rec = self.tracked[h]
                now = time.time()
                minimized = bool(user32.IsIconic(h)) or \
                    window_show_cmd(h) in (2, 6, 7)
                if minimized:
                    rec.pop("hidden_since", None)
                    self.recover_minimized_window(h, rec)
                    continue
                if not user32.IsWindowVisible(h):
                    if now < rec.get("minimize_guard_until", 0):
                        self.recover_minimized_window(h, rec)
                        continue
                    # 有些应用点 X 只是隐藏主窗口到托盘，HWND 仍存在。
                    # 连续隐藏一小段时间才回收，避开桌面切换动画的短暂状态。
                    hidden_since = rec.setdefault("hidden_since", now)
                    if now - hidden_since >= 0.7:
                        self.restore_from_new_desktop(h, why="hidden")
                    continue
                rec.pop("hidden_since", None)
                if is_maximized(h):
                    rec["min_restore_attempts"] = 0
                if h == hwnd and not is_maximized(h):
                    if time.time() < rec.get("minimize_guard_until", 0):
                        continue
                    self.restore_from_new_desktop(h, why="unmaximize")
            # 2) 新窗口：检测 未最大化 → 最大化 的跳变
            #    ⚠️ 条件**不是** `if self.cfg["auto_app_desktop"]` —— 那等于把
            #       "允不允许自动"和"要不要明确动作"两条语义揉在一起，表现是
            #       "关掉自动开关会连 Shift 那条路一起关掉"。统一交给 mvd_armed()。
            if hwnd and hwnd not in self.tracked:
                now_max = is_maximized(hwnd)
                prev = self.seen_max.get(hwnd)
                if prev is None:
                    self.seen_max[hwnd] = now_max      # 首次见到，只登记不触发
                else:
                    self.seen_max[hwnd] = now_max
                    if now_max and not prev and self._eligible(hwnd) \
                            and time.time() >= self.mvd_suppress:
                        cur = next((s for s in (self.state or [])
                                    if s["is_current"]), None)
                        if cur and cur["is_mvd"]:
                            pass           # 已经在单应用桌面上，别再套一层
                        elif self.mvd_armed():
                            self.launch_to_new_desktop(
                                hwnd, why=(f"{self.mod_label().lower()}+maximize"
                                           if self.cfg.get("mvd_shift_only", True)
                                           else "maximize"))
                        else:
                            # 记一行很有用：用户看到的是"最大化之后什么都没发生"，
                            # 日志里能一眼分清"按设计不做"和"该做但没做"
                            self._log(f"最大化但没按住 {self.mod_label()}"
                                      f"（窗口 {hwnd}）→ 按设计留在当前桌面")
            if self.mvd_tick % 40 == 0:
                self.seen_max = {h: v for h, v in self.seen_max.items()
                                 if is_window(h)}
        except Exception:
            self._log_callback_error("最大化监视")
        self.root.after(MVD_MS, self.mvd_poll)

    # ---- 热键队列
    def drain_hotkeys(self):
        try:
            while True:
                kind, a, b = self.hk_q.get_nowait()
                if kind == "hotkey":
                    self.hk_status[a] = b
                    st = "注册成功" if b["ok"] else f"失败 err={b['err']}"
                    self._log(f"热键 {b['desc']} {b['label']}: {st}")
                elif kind == "hotkey_fail":
                    self._log(f"热键线程异常: {b}")
                elif kind == "hotkey_fire":
                    self.on_hotkey(a)
        except queue.Empty:
            pass
        self.root.after(120, self.drain_hotkeys)

    def on_hotkey(self, hid):
        if hid == HK_TASKBAR:
            if not self.cfg["hotkey_taskbar"]:
                return
            cur = taskbar_autohide()
            if cur is None:
                return
            new = not cur
            set_taskbar_autohide(new)
            # ⚠️ 这里**不能**把 tray_goal 清成 None —— 原来就是，代价很实在。
            #    用户按这个热键，十次有九次是因为"点了药丸之后任务栏没动，
            #    我自己按一下试试"。把它理解成"以后别管了"，后果是：
            #      · 他在单应用桌面上再也等不到自动隐藏；
            #      · 离开单应用桌面时也等不到还原 —— 日志里 10:23:10、10:29:30
            #        各留了一次"任务栏状态已被手动改动，不还原"，任务栏就被
            #        晾在错误状态上没人管了。
            #    正确语义：把**目标对齐到他刚按出来的状态**。在单应用桌面里
            #    就以此为新的目标（他可能就是想临时看一眼任务栏），联动照旧。
            self.tray_goal = new if self.in_mvd else None
            if self.in_mvd:
                if new:
                    self.force_hide_tray()
                else:
                    self.release_forced_tray()
                self.sample_tray()
                self.update_floating_visibility()
            self.tray_pushes = 0
            self.tray_gave_up = False
            self.tray_last_push = 0.0
            # 立刻读回一次：注册表是同步写的，读回来跟写入不一致，就说明这次
            # 写入被别的进程覆盖了。日志里带上这个值，以后再出"热键按了没反应"
            # 这类哑谜就不用靠猜了。
            self._log(f"热键：任务栏自动隐藏 {cur} → {new}"
                      f"（写回后读到 {taskbar_autohide()}；"
                      f"{'单应用桌面内目标已跟随' if self.in_mvd else '当前无联动目标'}）")
        elif hid == HK_LAUNCH:
            if not self.cfg["hotkey_launch"]:
                return
            self.launch_to_new_desktop(user32.GetForegroundWindow(), why="hotkey")

    # ---- 高亮过渡
    def emph_list(self):
        """每颗药丸当前"有多像当前桌面"，用于把高亮在切换时补间过去。

        按**桌面 id** 记两端，不按序号 —— 切换过程中难免有桌面增删，
        用序号会在列表一变之后指错药丸。
        """
        if not self.state:
            return None
        p = self.hl_tw.value(ui.ease_in_out)
        moving = self.hl_tw.active or self.hl_from != self.hl_to
        if not moving or self.hl_from is None:
            return [1.0 if s["is_current"] else 0.0 for s in self.state]
        out = []
        for s in self.state:
            if s["id"] == self.hl_from and s["id"] != self.hl_to:
                out.append(1.0 - p)
            elif s["id"] == self.hl_to:
                out.append(p)
            else:
                out.append(0.0)
        return out

    def sync_highlight(self):
        """当前桌面变了就把高亮补间开起来（不是"啪"地跳过去）。"""
        cur = next((s["id"] for s in (self.state or []) if s["is_current"]), None)
        if cur is None:
            return
        if self.hl_to is None:
            self.hl_to = cur
            self.hl_from = cur
            return
        if cur != self.hl_to:
            self.hl_from = self.hl_to
            self.hl_to = cur
            self.hl_tw.start()
            self.start_anim()

    def anim_active(self):
        return self.hl_tw.active or self.fw_state in ("showing", "hiding")

    def start_anim(self):
        if not self._anim_on:
            self._anim_on = True
            self.root.after(ANIM_MS, self.anim_tick)

    def anim_tick(self):
        """动画帧。只在真的在动的时候跑，动完自己停 —— 不能常驻空转。"""
        try:
            self.step_float()
            if self.press_i >= 0 and time.time() - self.press_ts > 0.5:
                self.press_i = -1
            if self.anim_active():
                if self._last_render_ms > 45:
                    # 画一帧要 45ms 以上的话，硬撑帧率只会让点击发涩。
                    # 直接把两段补间推到终点，让画面立刻落到最终状态。
                    self.hl_tw.stop()
                    self.fw_tw.stop()
                self.render()
            else:
                self.render()          # 收在最终帧上
                self._anim_on = False
                return
        except Exception:
            self._anim_on = False
            self._log_callback_error("动画")
            return
        self.root.after(ANIM_MS, self.anim_tick)

    # ---- 刷新与渲染
    def refresh(self, force=False):
        new = desktop_snapshot()
        if new is not None:
            mvd_now = [s["number"] for s in new if s["is_mvd"]]
            if mvd_now != self.last_mvd_descs:
                self.last_mvd_descs = mvd_now
                self._log(f"单应用桌面: {mvd_now or '无'}")
            sig = [(s["number"], s["is_current"], s["is_mvd"], s["label"])
                   for s in new]
            old = [(s["number"], s["is_current"], s["is_mvd"], s["label"])
                   for s in (self.state or [])]
            self.state = new
            self.sync_highlight()
            # ⚠️⚠️ 先把画面更新掉，**再**去动任务栏。顺序反过来的代价很实在：
            #     切到单应用桌面时 mvd_step 会立刻把"任务栏目标=收起"推给
            #     explorer，explorer 随即开始滑走任务栏；如果这时候我们才去改
            #     药丸窗口的尺寸（嵌入那份是任务栏的**子窗口**，正跟着一起滑、
            #     一起重排），新露出来的区域就更容易被合成成黑块。先画完再推，
            #     画面永远有完整内容。
            if force or sig != old:
                # 把"实际画上去的那一行"记下来：排查颜色/文字问题时，
                # 这是唯一能证明程序真的读到了桌面名的凭据。
                self._log("药丸: " + " ".join(
                    "[%s%s]%s" % (s["label"], "*" if s["is_current"] else "",
                                  "全屏" if s["is_mvd"] else "")
                    for s in new))
                self.reposition()
                self.render()
            self.mvd_step(new)
            # ⚠️ 任务栏目标状态要**每轮核验**，不能只在进入/离开那一刻管一次。
            #    详见 apply_tray_goal 的注释（这就是问题一的修法）。
            self.apply_tray_goal()

        tray = tray_hwnd()
        if tray and (not self.embedded or user32.GetParent(self.emb_hwnd) != tray):
            self.try_embed()
            self.reposition()
        elif not tray and self.embedded:
            self.embedded = False
            self.reposition()

    def force_refresh(self):
        self.refresh(force=True)

    def _set_canvas_image(self, canvas, photo):
        """把图片换到既有画布项上，**不要** delete("all") 再重画。

        ⚠️ 这是"点药丸切桌面时药丸区域会变黑/闪动"的修法。原来的写法是每帧
        c.delete("all") + c.create_image(...)，中间存在一个**画布全空**的时刻；
        那一刻露出来的是画布底色 KEY_HEX (#010203)——一个近乎黑的颜色。平时
        这一帧短到看不见，但切桌面时整个合成器都在忙，它就被合成出来了，
        用户看到的就是"药丸区域黑了一下"。改成复用同一个画布项之后，
        任何一帧都有完整内容，不存在空帧。
        """
        iid = self._img_ids.get(id(canvas))
        if iid is None:
            self._img_ids[id(canvas)] = canvas.create_image(
                0, 0, image=photo, anchor="nw")
        else:
            try:
                canvas.itemconfig(iid, image=photo)
            except Exception:
                self._img_ids[id(canvas)] = canvas.create_image(
                    0, 0, image=photo, anchor="nw")

    def bg_for(self, which):
        """这一路软合成该用哪个底色：`"emb"` = 嵌在任务栏里那份，`"fw"` = 浮窗那份。

        ⚠️⚠️ **浮窗那一路永远返回 None**，这不是可以"以后优化"的地方。

        原因（用户 2026-09-23 实拍）：软合成的机制是"把圆角的半透明边缘按覆盖率
        混到一个底色上"。任务栏是一片**均匀纯色**，混上去确实能让圆角平滑；
        可浮窗下面是你当前的**壁纸或应用窗口**，内容完全是任意的 —— 采样点
        落在哪里就是哪里：落在地图的浅色区域就混成浅色，落在深色就混成深色。
        用户那次是深色地图壁纸 + 采样点恰好压在浅色区 → **整圈圆角混成浅色，
        也就是深色壁纸上一圈白边**，比"圆角有 1px 台阶"难看多了。

        结论：**宁可台阶，不要色晕。** 二值化的台阶只在放大看时才发现，
        混错底色的色晕是隔着半米都看得见的一圈边。

        写成方法而不是在 render() 里直接传 None，是为了让它**能被单测直接调** ——
        藏在渲染函数里的契约只能靠读源码确认，改回去也没人拦得住。
        """
        if which == "fw":
            return None
        return self.taskbar_bg()

    def taskbar_bg(self):
        """采一个"任务栏底色"回来，给圆角的软合成用（见 suite_ui._flatten）。

        为什么需要它：圆角本来是抗锯齿的，但 -transparentcolor 只能二值透明 ——
        半透明的边缘像素要么被丢掉（圆角变台阶，就是用户说的"边角漏了一块、
        不规整"），要么得**混到底色上**再显示。后者要求先知道底色是什么。

        ⚠️⚠️ 前提是"药丸**此刻确实压在任务栏矩形里**"（2026-09-23 补的判据）。
        任务栏是一片均匀的纯色，混上去能让圆角平滑，收益明确；**其余任何情况
        都不许猜** —— 药丸不压在任务栏上时，它下面是你当前的壁纸或应用窗口，
        内容完全是任意的，猜出来的底色混上去就是**一圈不属于这里的色晕**。
        用户 2026-09-23 实拍到的正是这个：任务栏自动隐藏后浮窗露在深色地图壁纸上，
        采样点恰好落在地图的浅色区域 → 整圈圆角混成浅色 → **深色壁纸上一圈白边**。
        比"圆角有 1px 台阶"难看得多。**宁可台阶，不要色晕。**

        怎么采：药丸窗口**右侧**外面那一小条一定是任务栏自己的颜色（窗口左边
        离屏幕左缘只有 6px，没地方采；右边管够）。规则：
          · 网格取 5x4 个点；任一通道最大偏差 > 12 就认为这块不干净（压在别的
            图标/药丸上），放弃；
          · 放弃就返回 None —— 渲染自动退回二值化。代价是圆角回到"台阶"，
            **不会画错**（这条很重要：底色估错了会变成一圈色晕，比台阶更丑）。

        缓存 6 秒：采一次约 20 次 GetPixel（几十微秒），但没必要每帧都采。
        ⚠️ 一旦"不该采"（药丸不在任务栏里），要把缓存也清掉 —— 否则任务栏刚
        收起的那几秒会拿旧值继续混，白边又回来了。
        """
        now = time.time()
        hwnd = self.emb_hwnd if (self.embedded and not self.immersed) \
            else self.fw_hwnd
        if not hwnd:
            return None
        rc = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            return None

        # ★ 判据：药丸必须**整个压在任务栏矩形里**（上边不低于任务栏顶、
        #   下边不超出任务栏底）。任务栏收起时嵌入版会跟着滑出屏幕、浮窗则
        #   挂在屏幕左下角的壁纸上 —— 两种都过不了这一关，直接退回二值化。
        tb = tray_rect()
        if not tb or rc.top < tb[1] or rc.bottom > tb[3] or rc.left < tb[0]:
            self._bg = None                 # 顺手把缓存作废，别让白边拖几秒
            return None

        if self._bg is not None and now - self._bg_ts < 6.0:
            return self._bg
        self._bg_ts = now
        self._bg = None
        x0 = rc.right + 3
        mon = monitor_rect(hwnd)
        if mon and x0 + 24 > mon[2]:
            return None                       # 右边没地方采，宁可不采
        hdc = user32.GetDC(0)
        if not hdc:
            return None
        pts = []
        try:
            span = max(1, rc.bottom - rc.top - 32)
            for iy in range(4):
                yy = rc.top + 16 + iy * (span // 4)
                for ix in range(5):
                    v = gdi32.GetPixel(hdc, x0 + ix * 5, yy)
                    if v == 0xFFFFFFFF:       # CLR_INVALID
                        return None
                    pts.append((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
        finally:
            user32.ReleaseDC(0, hdc)
        avg = tuple(sum(p[i] for p in pts) // len(pts) for i in range(3))
        for p in pts:
            if max(abs(p[i] - avg[i]) for i in range(3)) > 12:
                self._log(f"任务栏底色采样不干净（{p} 对均值 {avg}）"
                          f"→ 这一轮圆角退回二值化")
                return None
        self._bg = avg
        return avg

    def render(self):
        if not self.state or not self.m or not self.w:
            return
        t0 = time.time()
        emph = self.emph_list()
        if not self.embed_fallback:
            # 初始嵌入窗口仍由 Tk 显示；扩容后它被隐藏，不再为其创建位图。
            base = render_pixmap(self.w, self.h, self.state,
                                 self.accent, self.dark, self.m,
                                 emph=emph, hover=self.hover_i,
                                 press=self.press_i, bg=self.bg_for("emb"))
            self.photo = ImageTk.PhotoImage(base)
            self._set_canvas_image(self.canvas_e, self.photo)

        self.alpha_image = ui.render_rgba(
            self.w, self.h, self.state, self.accent, self.dark, self.m,
            emph=emph, hover=self.hover_i, press=self.press_i,
            dy=self.fw_dy, scale=self.fw_scale)
        target = self.fw_target()
        if target is not None:
            self.alpha_pill.render(self.alpha_image, target[0], target[1])
        self._last_render_ms = (time.time() - t0) * 1000.0

    # ---- 控制面板（自绘，替代原来的 tk.Menu）
    def _hotkey_combo(self, hid):
        st = self.hk_status.get(hid)
        if st is None:
            return "启动中"
        if st["ok"]:
            return st["label"]
        if st["err"] == 1409:
            return "全被占用"
        return f"注册失败 {st['err']}"

    def _panel_rows(self):
        n = len(self.state or [])
        cur = next((s["number"] for s in (self.state or []) if s["is_current"]), 0)
        mvd = sum(1 for s in (self.state or []) if s["is_mvd"])
        sub = f"{n} 个桌面 · 当前第 {cur} 个"
        if mvd:
            sub += f" · {mvd} 个单应用"

        # ⚠️ 每个 toggle 行都必须带 "on"。suite_ui._switch 收到 None 就当 False，
        #    所以漏了 "on" 的开关**永远画成"关"**，跟实际配置不符 ——
        #    面板上八个开关全是一个样子，这是"界面粗糙"里最实的一处。
        def sw(key, label, note=None):
            r = {"kind": "toggle", "key": key, "label": label,
                 "on": bool(self.cfg.get(key))}
            if note:
                r["note"] = note
            return r

        return [
            {"kind": "head"},
            {"kind": "sep"},
            {"kind": "action", "id": "refresh", "label": "刷新桌面列表",
             "hint": "重新读一遍"},
            {"kind": "sep"},
            {"kind": "section", "label": "快捷键"},
            sw("hotkey_taskbar", "切换任务栏自动隐藏",
               self._hotkey_combo(HK_TASKBAR)),
            sw("hotkey_launch", "把窗口甩到新桌面",
               self._hotkey_combo(HK_LAUNCH)),
            {"kind": "sep"},
            {"kind": "section", "label": "行为"},
            # ⚠️ 这两条是**两件事**，别合并：
            #    auto_app_desktop = 允不允许"最大化时自动建独占桌面"这件事发生
            #    mvd_shift_only   = 发生要不要**明确动作**（Shift）
            #    默认组合 = 只有 Shift+最大化 才独占；普通最大化什么都不做。
            sw("auto_app_desktop", "允许最大化时独占新桌面",
               f"{self.mod_label()}+最大化/热键"),
            sw("mvd_shift_only", f"只认 {self.mod_label()}+最大化",
               "关掉＝旧行为"),
            # 修饰键是哪个可以换（Shift 在中文输入法里太忙，用户 2026-09-23
            # 主动提过"也不一定非得是 shift 键"）。做成可点的一行，省得改配置。
            {"kind": "action", "id": "mvd_mod",
             "label": f"独占时按住的键：{self.mod_label()}",
             "hint": "点一下换一个"},
            sw("mvd_link", "单应用桌面上自动隐藏任务栏"),
            sw("immersive_hide", "全屏桌面上一并藏起药丸"),
            sw("fullscreen_fill", "全屏补位（个别程序留空档时开）"),
            {"kind": "sep"},
            # 自启不是 cfg 里的开关，它看的是 Startup 文件夹里有没有那个 .vbs
            {"kind": "toggle", "key": "autostart", "label": "开机自启",
             "on": self._autostart_on()},
            {"kind": "sep"},
            # ⚠️ 这里原来挂着 "Ctrl+Q" 的提示，但热键表里根本没有这个组合 ——
            #    等于写了一行按不出来快捷键。没有就别写。
            {"kind": "action", "id": "quit", "label": "退出套件",
             "danger": True},
        ], sub

    def panel_visible(self):
        try:
            return self.panel.state() == "normal"
        except Exception:
            return False

    def render_panel(self):
        rows, sub = self._panel_rows()
        self.panel_rows = rows
        img, layout = ui.render_menu(rows, self.accent, self.dark,
                                     title="虚拟桌面套件", subtitle=sub,
                                     hover=self.panel_hover)
        self.panel_layout = layout
        self.panel_photo = ImageTk.PhotoImage(img)
        iid = self._panel_img_id
        if iid is None:
            self._panel_img_id = self.canvas_p.create_image(
                0, 0, image=self.panel_photo, anchor="nw")
        else:
            self.canvas_p.itemconfig(iid, image=self.panel_photo)
        self.canvas_p.configure(width=layout["w"], height=layout["h"])

    def show_panel(self, x, pill_top):
        self.panel_hover = -1
        # 先渲染再显示（顺序跟浮窗药丸一样）—— 窗口映射出来的时候内容已经就位。
        self.render_panel()
        w, h = self.panel_layout["w"], self.panel_layout["h"]
        mon = monitor_rect(self.tray or 0) or (0, 0, w, h)
        # 面板位于被点击药丸的右上方，底边与药丸顶边留 16px。
        # 仅在屏幕确实放不下时才向内收。
        y = pill_top - h - 16
        x = max(mon[0] + 4, min(x, mon[2] - w - 4))
        y = max(mon[1] + 4, min(y, mon[3] - h - 4))
        # ⚠️ 用 getattr：panel_geom 原来只在 show_panel 里第一次赋值，
        #    这里直接 self.panel_geom 会在"第一次打开面板"时 AttributeError。
        prev = getattr(self, "panel_geom", None)
        try:
            self.panel.deiconify()
        except Exception:
            pass
        user32.SetWindowPos(self.panel_hwnd, HWND_TOPMOST, x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.panel_geom = (x, y, w, h)
        # ⚠️ 尺寸跟上次不一样时补一次强制重画：分层窗口变大后新露出来的那条是
        #    未定义像素（黑），机制跟药丸那边一模一样（见 _flush_paint）。
        #    面板的行数会变（快捷键注册状态、单应用桌面描述都会改文案），
        #    所以"每次打开都一样大"这个假设不成立。
        if prev is None or (prev[2], prev[3]) != (w, h):
            self._flush_paint(self.canvas_p, self.panel_hwnd)
        # 记下打开时刻，panel_watch 靠它划宽限期（防"刚开出来就被自己的看门狗关掉"）
        self._panel_open_ts = time.time()
        # 先清掉面板打开之前积攒的鼠标低位，再启动仅在面板可见时运行的看门狗。
        user32.GetAsyncKeyState(VK_LBUTTON)
        if getattr(self, "root", None) is not None:
            self._schedule_panel_watch()

    def hide_panel(self):
        self.panel_hover = -1
        self._panel_open_ts = 0.0
        job = getattr(self, "_panel_watch_job", None)
        if job is not None:
            self.root.after_cancel(job)
            self._panel_watch_job = None
        try:
            self.panel.withdraw()
        except Exception:
            pass

    def _schedule_panel_watch(self):
        # 单元测试中的轻量替身没有 Tk root；真实运行时只排一个定时任务。
        root = getattr(self, "root", None)
        if root is not None and self._panel_watch_job is None:
            self._panel_watch_job = root.after(PANEL_WATCH_MS, self.panel_watch)

    def panel_watch(self):
        """面板开着时盯着"是不是点到外面了"，点到外面就关掉。

        为什么不用那些看起来更省事的办法：
          · 面板是 WS_EX_NOACTIVATE —— 它**永远不会成为前台窗口**，
            所以 GetForegroundWindow() 判断不了"焦点跑掉了"；
          · Tk 的 bind_all 只收本程序窗口的消息，点到别的程序收不到；
          · grab_set() 会连"点在别的窗口上"也不透传，不符合 Windows 习惯
            （用户预期是"点哪就响应哪，面板顺便关掉"）。

        所以只能问系统："自上次问过之后，左键被按过吗？"
        GetAsyncKeyState 的**低位**（0x0001）正是这个语义 —— 这一点很关键：
        本轮 140ms 一次，只看"当前是否按住"会漏掉"快按快放"这种最常见的点法。

        ⚠️ 两个必须踩住的地方：
          1. 低位是"自**上次调用**以来"；show_panel 开始监听前先清一次，
             宽限期内也持续清掉，否则旧点击会在宽限期后被误判。
          2. 再加一道宽限期（PANEL_GRACE）：打开后 0.35 秒内一律不理。
        """
        self._panel_watch_job = None
        if not self.panel_visible():
            return
        try:
            clicked = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x0001)
            if clicked and time.time() - self._panel_open_ts > PANEL_GRACE:
                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                gx, gy, gw, gh = getattr(self, "panel_geom", None) \
                    or (0, 0, 0, 0)
                if not (gx <= pt.x <= gx + gw and gy <= pt.y <= gy + gh):
                    self._log(f"面板外点击 ({pt.x},{pt.y}) → 关闭面板")
                    self.hide_panel()
        except Exception:
            pass
        if self.panel_visible():
            self._schedule_panel_watch()

    def set_panel_hover(self, i):
        if i == self.panel_hover:
            return
        self.panel_hover = i
        if self.panel_visible():
            self.render_panel()

    def on_panel_motion(self, e):
        if not self.panel_layout:
            return
        i = ui.menu_hit(self.panel_layout, e.y)
        if i >= 0 and self.panel_rows[i].get("kind") in ("head", "section",
                                                         "sep"):
            i = -1
        self.set_panel_hover(i)

    def on_panel_click(self, e):
        if not self.panel_layout:
            return
        i = ui.menu_hit(self.panel_layout, e.y)
        if i < 0:
            return
        r = self.panel_rows[i]
        k = r.get("kind")
        if k == "head":
            gx, gy, gw, gh = self.panel_geom
            # 右上角那个叉
            if e.x > gw - 40:
                self.hide_panel()
            return
        if k == "toggle":
            key = r["key"]
            if key == "autostart":
                self.toggle_autostart()
            else:
                self.toggle_cfg(key)
            if self.panel_visible():
                self.render_panel()      # 开关留在原地继续开合，不关面板
            return
        if k == "action":
            act = r.get("id")
            if act == "mvd_mod":
                # 换"独占时按住哪个键"。刻意**不关面板**，方便连点挑一个顺手的。
                # 顺序里不放 Alt：按住 Alt 点最大化，老式程序会先进菜单模式，
                # 那次点击被菜单吃掉、最大化根本不发生（想用 Alt 就手改配置文件）。
                order = ["shift", "ctrl", "ctrl+shift", "win"]
                cur = str(self.cfg.get("mvd_modifier",
                                       MVD_MOD_DEFAULT)).lower()
                nxt = (order[(order.index(cur) + 1) % len(order)]
                       if cur in order else order[0])
                self.cfg["mvd_modifier"] = nxt
                self.mod_vks = MVD_MODS[nxt][1]
                self.mod_held = self.mod_tap = 0.0
                save_settings(self.cfg)
                self._log(f"cfg mvd_modifier = {nxt}"
                          f"（独占时按住 {MVD_MODS[nxt][0]} 再最大化）")
                if self.panel_visible():
                    self.render_panel()
                return
            self.hide_panel()
            if act == "refresh":
                self.force_refresh()
            elif act == "quit":
                self.quit()

    def on_menu(self, e, hwnd):
        x_root, y_root = self._to_screen(hwnd, e.x, e.y)
        rc = wintypes.RECT()
        if self.m and self.h and user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            i = max(0, self.pill_at(e.x))
            right = (rc.left + self.m["pad_l"]
                     + sum(self.m["widths"][:i + 1]) + self.m["gap"] * i)
            top = rc.top + (self.h - self.m["ph"]) // 2
            self.show_panel(right + 18, top)
        else:
            self.show_panel(x_root + 36, y_root - 20)

    # ---- 事件
    def _native_mouse(self, kind, x, y):
        """跨线程排入 Tk 事件队列；原生 WndProc 不接触绘制与 COM。"""
        if self._native_closing:
            return
        event = (kind, x, y)
        with self._native_lock:
            if (kind in ("move", "leave") and self._native_events
                    and self._native_events[-1][0] in ("move", "leave")):
                self._native_events[-1] = event
            else:
                self._native_events.append(event)
            notify = not self._native_event_queued
            self._native_event_queued = True
        if notify:
            try:
                self.root.event_generate("<<NativePillMouse>>", when="tail")
            except Exception as exc:
                with self._native_lock:
                    self._native_event_queued = False
                if not self._native_closing:
                    self._log(f"原生药丸投递 Tk 事件失败: {exc!r}")

    def _drain_native_mouse(self, _event=None):
        with self._native_lock:
            pending, self._native_events = self._native_events, []
            self._native_event_queued = False
        for kind, x, y in pending:
            if self._native_closing:
                break
            try:
                self._dispatch_native_mouse(kind, x, y)
            except Exception:
                self._log_callback_error("原生药丸鼠标事件")

    def _dispatch_native_mouse(self, kind, x, y):
        if kind == "leave":
            self.set_hover(-1)
            return
        event = SimpleNamespace(x=x, y=y)
        if kind == "move":
            self.on_motion(event)
        elif kind == "press":
            self.on_press(event)
        elif kind == "release":
            self.on_release(event)
        elif kind == "menu":
            self.on_menu(event, self.fw_hwnd)

    def pill_at(self, x):
        if not self.state or not self.m:
            return -1
        px = self.m["pad_l"]
        for i in range(len(self.state)):
            if px <= x <= px + self.m["widths"][i]:
                return i
            px += self.m["widths"][i] + self.m["gap"]
        return -1

    def _to_screen(self, hwnd, x, y):
        pt = POINT(x, y)
        user32.MapWindowPoints(hwnd, None, ctypes.byref(pt), 1)
        return pt.x, pt.y

    def set_hover(self, i):
        if i == self.hover_i:
            return
        self.hover_i = i
        if not self.anim_active():
            self.render()

    def on_motion(self, e):
        self.set_hover(self.pill_at(e.x))

    def on_press(self, e):
        i = self.pill_at(e.x)
        self.press_i = i
        self.press_ts = time.time()
        if i >= 0 and not self.anim_active():
            self.render()

    def on_release(self, e):
        i = self.pill_at(e.x)
        pressed = self.press_i
        self.press_i = -1
        if i < 0 or i != pressed or i >= len(self.state):
            self.render()
            return
        target = self.state[i]["id"]
        target_is_mvd = self.state[i]["is_mvd"]
        try:
            for d in pyvda.get_virtual_desktops():
                if d.id == target:
                    d.go()
                    if not target_is_mvd:
                        self.release_forced_tray()
                    if target_is_mvd:
                        # 桌面切换后，Windows 有时仍把原桌面的窗口当作前台。
                        # 这会让已开启自动隐藏的任务栏一直展开；用户按两次
                        # Win 键才会触发 Shell 重新处理焦点。点击药丸本身是
                        # 用户输入，因此这里把目标桌面的窗口激活。
                        self.root.after(60, lambda tid=target:
                                        self.focus_mvd_window(tid))
                    break
        except Exception:
            pass
        # 桌面 id 变化大约 10ms 就生效，这里不睡等：高亮过渡由 sync_highlight
        # 在下一轮轮询里触发，中间那段由补间填。
        self.render()
        self.root.after(120, self.force_refresh)

    def focus_mvd_window(self, desktop_id, retry=True):
        """点击独占桌面后激活它的窗口，不在后台轮询中抢用户焦点。"""
        try:
            if pyvda.VirtualDesktop.current().id != desktop_id:
                return
            desk = find_desktop(desktop_id)
            if desk is None or not is_mvd_desktop(desk.name):
                return
            candidates = [h for h, rec in self.tracked.items()
                          if rec["temp"] == desktop_id]
            # 兼容以前留下的 [MVD] 桌面：跟踪记录不在本次进程里。
            candidates.extend(view.hwnd for view in
                              desk.apps_by_z_order(include_pinned=False))
            for hwnd in dict.fromkeys(candidates):
                if not is_window(hwnd) or not user32.IsWindowVisible(hwnd) \
                        or user32.IsIconic(hwnd):
                    continue
                try:
                    view = pyvda.AppView(hwnd=hwnd)
                    if view.desktop.id != desktop_id:
                        continue
                except Exception:
                    continue
                if user32.GetForegroundWindow() == hwnd:
                    return
                user32.SetForegroundWindow(hwnd)
                if user32.GetForegroundWindow() != hwnd:
                    view.switch_to()
                if user32.GetForegroundWindow() == hwnd:
                    self._log(f"点击药丸后已激活独占窗口 {hwnd}")
                    return
                break
            if retry:
                self.root.after(180, lambda:
                                self.focus_mvd_window(desktop_id, False))
            else:
                self._log(f"独占桌面 {desktop_id} 的窗口未能取得前台焦点")
        except Exception:
            self._log_callback_error("独占窗口激活")

    # ---- 轮询
    def poll(self):
        try:
            self.tick += 1
            tray_was_out = self.tray_out
            key = tray_rect()
            if key != self.last_rc:
                self.reposition()
            # ⚠️ refresh() 必须跑在 update_floating_visibility() **前面**：
            #    refresh 会把药丸数量/宽度算准、把内容画好，之后浮窗才按新尺寸
            #    出场（见 update_floating_visibility 里那段注释）。反过来的话，
            #    浮窗先按旧尺寸（少一颗药丸）显示出来、再被 place_floating 撑大，
            #    多出来那一条正是"该出现新药丸的地方"—— 未定义像素，黑的。
            self.refresh()
            # 任务栏从屏幕外回到屏幕内时，嵌入窗口可能有上一轮扩宽后尚未
            # 合成的区域。即使本轮宽度未变化，也同步重画一次。
            if self.embedded and self.tray_out and not tray_was_out:
                self._flush_paint(self.canvas_e, self.emb_hwnd)
            self.update_floating_visibility()

            if self.tick % 20 == 0:
                new_accent = accent_color()
                dark = not taskbar_light()
                if dark != self.dark or new_accent != self.accent:
                    self.accent, self.dark = new_accent, dark
                    self._bg = None
                    self.render()

            self.keep_on_top()
        except Exception:
            self._log_callback_error("状态轮询")
        self.root.after(POLL_MS, self.poll)

    def close_requested(self, window_name):
        self._log(f"收到窗口关闭请求：{window_name}")
        self.quit()

    def quit(self):
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        callers = traceback.extract_stack(limit=5)[:-1]
        self._log("正常退出请求: " + " <- ".join(
            f"{frame.name}:{frame.lineno}" for frame in callers))
        self._native_closing = True
        with self._native_lock:
            self._native_events.clear()
        try:
            for h in list(self.tracked.keys()):
                self.restore_from_new_desktop(h, why="quit")
            if self.in_mvd:
                self.leave_mvd()
            # 退出前把任务栏状态落实到位，别让用户捡到一个藏起来的任务栏
            if self.tray_goal is not None:
                set_taskbar_autohide(self.tray_goal)
            if self.embedded and self.emb_hwnd:
                user32.SetParent(self.emb_hwnd, None)
        except Exception:
            self._log("退出清理出错:\n" + traceback.format_exc()[-500:])
        try:
            self.hide_panel()
        except Exception:
            pass
        try:
            if self.hk_thread.hwnd:
                user32.PostMessageW(self.hk_thread.hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:
            pass
        try:
            self.alpha_pill.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    import atexit

    _LIFE = os.path.join(_HERE, "suite_life.log")

    def _life(msg):
        try:
            with open(_LIFE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass

    atexit.register(_life, f"[suite] exit pid={os.getpid()}")
    if "--probe" in sys.argv:
        _life(f"[suite] probe-ready pid={os.getpid()}")
        sys.exit(0)
    _life(f"[suite] start pid={os.getpid()}")
    try:
        Suite().run()
        _life("[suite] mainloop returned normally")
    except BaseException:
        with open(os.path.join(_HERE, "suite_errors.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} FATAL suite =====\n")
            f.write(traceback.format_exc())
        sys.exit(1)
