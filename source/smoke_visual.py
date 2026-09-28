# -*- coding: utf-8 -*-
"""smoke_visual.py — 真机冒烟：拉起 exe，抓药丸在任务栏上的**真实屏幕像素**，量化检查

    (a) 药丸四周有没有"白边"（窗口底色 ≠ 任务栏底色 就会露出来）
    (b) 药丸之间是不是各自独立（间隙列应该正好是任务栏底色，而不是任何底衬）

全程**不切虚拟桌面、不改任务栏设置**；跑完就自己退，退出走套件自己的 quit()。

⚠️ 坐标上踩过的坑（别再犯）：
  · 药丸窗口 rect 实测 (6,1984)-(325,2080)，**高 96 = 任务栏全高、左边几乎贴屏幕左缘**。
    所以"往窗口左边/上边多抓一圈"会直接抓到屏幕外，得到一片纯黑 —— 拿它当基准色，
    后面所有比较全是假的。基准色只能往**窗口右侧**取（任务栏在那一边管够）。
  · 窗口上边之外是**桌面**不是任务栏，同样不能当基准。

判据：
  · base = 抓图最右侧 10 列的平均色（一定在药丸窗口之外、又在屏幕之内）。
  · 窗口内药丸上方那条透明带（约 11px）、下方那条、以及药丸之间的间隙列，
    都应该 ≈ base。偏亮 → 铺了浅色底（用户看到的"白边/白空隙"）；偏暗 → 抠像留了黑边。
"""
import os
import sys
import time
import ctypes
import ctypes.wintypes as wt
import subprocess

HERE = r"D:\Documents\WorkBuddy\Interest\VirtualDesktopBar"
# 默认冒烟的是打包好的 exe；想快速迭代就跑源码模式：
#   set VDB_TARGET=C:\Program Files\Python312\pythonw.exe|D:\...\source\desktop_suite.py
#   （用 | 分隔命令行 —— 不能用空格，路径里有空格。源码模式日志落在 source\ 下）
_T = os.environ.get("VDB_TARGET")
CMD = _T.split("|") if _T else [os.path.join(HERE, "VirtualDesktopSuite.exe")]
LOG = os.path.join(HERE, "source", "suite_life.log") if _T \
    else os.path.join(HERE, "suite_life.log")
_LIBS = os.path.join(HERE, "source", "libs")
# libs/ 里是 3.12 编的离线依赖（PIL 的 _imaging 是 cp312）。插到 sys.path 最前会
# 盖住 site-packages，用 3.13 跑就报 "cannot import name '_imaging' from 'PIL'"。
# 放在最后当兜底，3.12 / 3.13 两边都能跑。
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.append(_LIBS)
from PIL import Image  # noqa: E402

u = ctypes.windll.user32
gdi = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    u.SetProcessDPIAware()

u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u.FindWindowW.restype = wt.HWND
u.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
u.GetWindowRect.restype = wt.BOOL
u.IsWindowVisible.argtypes = [wt.HWND]
u.IsWindowVisible.restype = wt.BOOL
u.GetClassNameW.argtypes = [wt.HWND, wt.LPCWSTR, ctypes.c_int]
u.GetClassNameW.restype = ctypes.c_int
u.EnumChildWindows.argtypes = [wt.HWND, ctypes.c_void_p, wt.LPARAM]
u.EnumChildWindows.restype = wt.BOOL
u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u.GetWindowThreadProcessId.restype = wt.DWORD
u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u.PostMessageW.restype = wt.BOOL
u.GetDC.argtypes = [wt.HWND]
u.GetDC.restype = ctypes.c_void_p
u.ReleaseDC.argtypes = [wt.HWND, ctypes.c_void_p]
u.ReleaseDC.restype = ctypes.c_int
gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi.CreateCompatibleDC.restype = ctypes.c_void_p
gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi.SelectObject.restype = ctypes.c_void_p
gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                       ctypes.c_uint32]
gdi.BitBlt.restype = wt.BOOL
gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
gdi.GetDIBits.restype = ctypes.c_int
gdi.DeleteObject.argtypes = [ctypes.c_void_p]
gdi.DeleteDC.argtypes = [ctypes.c_void_p]
u.GetForegroundWindow.argtypes = []
u.GetForegroundWindow.restype = wt.HWND

PCB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def screen_grab(x, y, w, h):
    hdc = u.GetDC(0)
    mdc = gdi.CreateCompatibleDC(hdc)
    bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
    old = gdi.SelectObject(mdc, bmp)
    gdi.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)
    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    gdi.SelectObject(mdc, old)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mdc)
    u.ReleaseDC(0, hdc)
    return img


def tray_kids():
    tray = u.FindWindowW("Shell_TrayWnd", None)
    out = []
    if not tray:
        return out

    def cb(h, lp):
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(h, cls, 128)
        rc = RECT()
        u.GetWindowRect(h, ctypes.byref(rc))
        out.append({"h": h, "pid": pid.value, "cls": cls.value,
                    "rect": (rc.left, rc.top, rc.right, rc.bottom),
                    "vis": bool(u.IsWindowVisible(h))})
        return True

    u.EnumChildWindows(tray, PCB(cb), 0)
    return out


def log_lines():
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def delta(a, b):
    return max(abs(a[i] - b[i]) for i in range(3))


def session_unusable():
    """这一屏现在能不能拿来做像素判据？不能就返回原因，能就返回 None。

    三种"看着有画面、其实测不了"的情况（2026-09-23 全踩过）：
      · 没有前台窗口 —— 会话不可交互；
      · 前台是**锁屏界面** `Windows.UI.Core.CoreWindow`（LockApp.exe）。
        ⚠️ 这一条是最阴的：锁屏有壁纸的时候屏幕**不是纯黑**（实测左上角
        300x60 就有 42 种颜色），只查"唯一色数 == 1"完全漏判，于是拿锁屏
        画面当任务栏基准色，得出"药丸连成一整段、没有间隙"这种荒唐结论；
      · 抓到的确实整片纯色 —— 显示器休眠 / 无信号。
    """
    fg = u.GetForegroundWindow()
    if not fg:
        return "没有前台窗口（会话不可交互）"
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(fg, cls, 256)
    if cls.value == "Windows.UI.Core.CoreWindow":
        return "前台是锁屏界面（Windows.UI.Core.CoreWindow / LockApp.exe）"
    if len(screen_grab(0, 0, 160, 100).getcolors(maxcolors=99999)) == 1:
        return "从屏幕 DC 抓到的是整片纯色（显示器休眠 / 无信号）"
    return None


def main():
    # ⚠️ 放在**拉起套件之前**：锁屏时跑完一圈纯属白费，还会在用户的锁屏界面
    #    背后凭空起一个进程。
    why = session_unusable()
    if why:
        print(f"[SKIP] {why} —— 屏幕像素判据这次做不了，不计入失败")
        return
    before = len(log_lines())
    print("启动", " ".join(CMD))
    t0 = time.time()
    subprocess.Popen(CMD, cwd=HERE)   # ⚠️ onefile exe：Popen 的 pid 是解压器，
    pid = None                        #    真正的 app pid 在日志里
    for _ in range(160):
        time.sleep(0.25)
        for ln in log_lines()[before:]:
            if "[suite] start pid=" in ln:
                pid = int(ln.rsplit("=", 1)[1])
                break
        if pid:
            break
    if not pid:
        print("[FAIL] 日志里没等到 start 行")
        return
    print(f"起来 pid={pid}  用时 {time.time() - t0:.1f}s"
          f"  （⚠️ 0.1s 左右说明连到了残留实例；exe onefile 冷启动本来就慢）")

    pill = None
    for _ in range(80):
        time.sleep(0.25)
        for k in tray_kids():
            if k["pid"] == pid and k["vis"] and "Tk" in k["cls"] \
                    and (k["rect"][2] - k["rect"][0]) > 50:
                pill = k
                break
        if pill:
            break
    if not pill:
        print("[FAIL] 任务栏里没找到属于本进程的可见药丸窗口")
        print("      任务栏子窗口:", [(k["pid"], k["cls"]) for k in tray_kids()][:12])
        _shutdown(before)
        return
    x0, y0, x1, y1 = pill["rect"]
    pw, ph = x1 - x0, y1 - y0
    print(f"[OK] 药丸已嵌进任务栏 hwnd={pill['h']} rect=({x0},{y0})-({x1},{y1}) {pw}x{ph}")
    if ph <= 60:
        print(f"     ⚠️ 窗口高只有 {ph}，和任务栏高度差太多，下面的取样可能不准")

    time.sleep(1.8)                       # 等渲染与补间都落定
    # ⚠️ 再确认一次（拉起这段时间里用户可能刚好锁屏）。锁屏判据见 session_unusable()：
    #    光看"前台句柄==0"或"整屏纯色"会漏掉**带壁纸的锁屏**，那是最容易骗过人的一种。
    why = session_unusable()
    if why:
        print(f"[SKIP] {why} —— 像素判据这次做不了，不计入失败")
        _shutdown(before)
        return
    for ln in log_lines()[before:]:
        if "embed:" in ln:
            print("     日志:", ln)

    PADX = 90                             # 只往右多抓：右边一定是屏幕内的任务栏
    raw = screen_grab(x0, y0, pw + PADX, ph)
    px = raw.load()
    rw, rh = raw.size

    # 基准色 = 最右侧 10 列的平均色（在药丸窗口之外）
    acc = [0, 0, 0]
    n = 0
    for lx in range(rw - 10, rw):
        for ly in range(rh):
            c = px[lx, ly]
            acc[0] += c[0]
            acc[1] += c[1]
            acc[2] += c[2]
            n += 1
    base = tuple(v // n for v in acc)
    print(f"     基准（窗口右侧的任务栏本色）= {base}")

    def band(y_lo, y_hi, tag):
        worst = (0, None)
        bad = 0
        for lx in range(4, pw - 4, 2):
            for ly in range(y_lo, y_hi):
                d = delta(px[lx, ly], base)
                if d > worst[0]:
                    worst = (d, (lx, ly, px[lx, ly]))
                if d > 12:
                    bad += 1
                    break
        cols = len(range(4, pw - 4, 2))
        print(f"     {tag}: 偏差>12 的列 {bad}/{cols}，最大偏差 {worst[0]}"
              f"（{worst[1]}）  → {'[FAIL] 有异色带' if bad > 3 else '[PASS] 全是任务栏本色'}")
        return bad == 0

    # ⚠️ 上带从 y=4 起算，不要从 1 起算。
    #    药丸窗口高 96 = 任务栏全高，窗口顶边正好压在任务栏顶边上，而 explorer 在
    #    任务栏最上面自己画了 2px 描边：实测 (171,184,189)，旁边本色是 (207,229,238)。
    #    那是系统自带的，跟药丸无关 —— 从 1 起算会 100% 误报"有白边"。
    top_ok = band(4, 11, "药丸上方透明带（已避开任务栏自带顶边 2px）")
    bot_ok = band(ph - 9, ph - 1, "药丸下方透明带")

    # 列扫描：哪些列"有内容"
    col_on = []
    for lx in range(pw):
        ink = 0
        for ly in range(ph):
            if delta(px[lx, ly], base) > 24:
                ink += 1
        col_on.append(ink > 4)
    runs, cur = [], None
    for i, on in enumerate(col_on):
        if on and cur is None:
            cur = i
        elif not on and cur is not None:
            runs.append((cur, i - cur))
            cur = None
    if cur is not None:
        runs.append((cur, len(col_on) - cur))
    runs = [r for r in runs if r[1] > 3]
    gaps = [runs[i + 1][0] - (runs[i][0] + runs[i][1]) for i in range(len(runs) - 1)]
    print(f"     药丸分段数={len(runs)} 各段宽={[r[1] for r in runs]} 间隙宽={gaps}")
    if len(runs) >= 2 and all(g > 0 for g in gaps):
        print("     [PASS] 药丸是一颗一颗独立的，之间留出了空隙")
    elif len(runs) <= 1:
        print("     [WARN] 只测到一整段，可能又加了整排底衬")
    else:
        print("     [WARN] 测到多段但间隙为 0，边缘可能糊在一起")

    for lx, tag in ((0, "药丸左边缘"), (pw - 1, "药丸右边缘")):
        col = [px[lx, ly] for ly in range(4, ph - 4, 3)]
        bad = sum(1 for c in col if delta(c, base) > 12)
        print(f"     窗口{tag}列：与任务栏本色不符的取样 {bad}/{len(col)}")

    clean = raw.crop((0, 0, pw, ph))
    clean.save(os.path.join(HERE, "预览-真机药丸.png"))
    clean.resize((clean.width * 3, clean.height * 3), Image.NEAREST).save(
        os.path.join(HERE, "source", "_smoke_zoom.png"))
    print(f"     已存 预览-真机药丸.png {clean.size}")

    _shutdown(before)


def _shutdown(before):
    """WM_CLOSE 给 Tk 根窗口 → 走套件自己的 quit()；以日志出现 exit 行为准"""
    root = u.FindWindowW(None, "VDSuite")
    if root:
        u.PostMessageW(root, 0x0010, 0, 0)
    else:
        print("     （没找到 VDSuite 根窗口，只能 taskkill）")
    done = False
    for _ in range(48):
        time.sleep(0.25)
        if any("[suite] exit pid=" in ln for ln in log_lines()[before:]):
            done = True
            break
    print("     收尾:", "已正常退出（清理路径跑完）" if done else "⚠️ 10s 内没见到 exit 行")
    print("收尾后日志尾部:")
    for ln in log_lines()[-5:]:
        print("    ", ln)


if __name__ == "__main__":
    # ⚠️ 必须有这层保护：之前是裸调用 main()，结果被 probe_tray_row.py 一 import
    #    就把整套冒烟（启动 exe、往任务栏塞窗口）全跑了一遍 —— 白白打扰一次用户。
    main()
