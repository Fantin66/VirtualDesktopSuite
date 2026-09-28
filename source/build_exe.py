# -*- coding: utf-8 -*-
"""build_exe.py — 把入口脚本打包成单个 exe

用法：
    "C:\\Program Files\\Python312\\python.exe" source\\build_exe.py
    "C:\\Program Files\\Python312\\python.exe" source\\build_exe.py <入口脚本> <成品名>

不带参数时打的是 vd_bar_v6.py → VirtualDesktopBar.exe（预览条单品）。
集成版：build_exe.py desktop_suite.py VirtualDesktopSuite

流程：
    1) 调用 make_icon.py 生成 vd_bar.ico
    2) 用 pyi_env 里的 PyInstaller 做 onefile + noconsole 打包
    3) 把产物复制到项目根目录（source/ 只是开发物料）

打包要点（踩过的坑）：
    * --paths libs          让分析器能 import 到 pyvda（它不在 site-packages 里）
    * --hidden-import PIL._tkinter_finder   ImageTk 在 PyInstaller 下需要它
    * 不用 --collect-submodules comtypes    pyvda 用的是静态 COMMETHOD 定义，
      没有运行时 typelib 代码生成，收集全部 submodules 只会让包白白变大
    * 数据目录语义靠 sys.frozen 区分：exe 模式把 json / 日志写到 exe 同级目录
    * 集成版内置轻量 tray_watchdog.exe，仅独占期间运行并在异常退出后恢复任务栏
    * 瘦身排除清单见下方 exclusions：numpy / PIL 的 avif+webp / OpenSSL，
      解包后合计省 ~39MB，成品从 30.8MB 降到 12.5MB，启动 4.0s → 2.5s
    * 不能加 --clean：会被本机沙箱的批量删除保护拦下；改用 %TEMP% 下的全新目录
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))     # = <项目>\source
ROOT = os.path.dirname(HERE)                          # = <项目>
PYI_ENV = os.path.join(HERE, "pyi_env")
LIBS = os.path.join(HERE, "libs")
ICON = os.path.join(HERE, "vd_bar.ico")
TRAY_WATCHDOG_SOURCE = os.path.join(HERE, "tray_watchdog.cs")
TRAY_WATCHDOG_EXE = os.path.join(HERE, "tray_watchdog.exe")

# 入口脚本 与 成品名：可用命令行参数覆盖，默认打预览条单品
_ENTRY_FILE = sys.argv[1] if len(sys.argv) > 1 else "vd_bar_v6.py"
_NAME = sys.argv[2] if len(sys.argv) > 2 else "VirtualDesktopBar"
ENTRY = os.path.join(HERE, _ENTRY_FILE)
NAME = _NAME

# 每次构建都用 %TEMP% 下的全新目录：PyInstaller 单次运行内部的删除次数
# 会累加到本机沙箱的批量删除阈值（50）然后被拦，全新目录就不会有覆盖删除。
_STAMP = time.strftime("%Y%m%d_%H%M%S")
_TMPROOT = os.path.join(tempfile.gettempdir(), f"vdb_build_{NAME}_{_STAMP}")
DIST = os.path.join(_TMPROOT, "dist")
BUILD = os.path.join(_TMPROOT, "work")


def main():
    # 1) 图标
    subprocess.run([sys.executable, os.path.join(HERE, "make_icon.py")], check=True)

    if _ENTRY_FILE in ("desktop_suite.py", "desktop_suite_edge.py"):
        csc = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
        if os.path.isfile(csc):
            subprocess.run([csc, "/nologo", "/optimize+", "/platform:x64",
                            "/target:winexe", "/out:" + TRAY_WATCHDOG_EXE,
                            TRAY_WATCHDOG_SOURCE], check=True)
        elif not os.path.isfile(TRAY_WATCHDOG_EXE):
            sys.exit("缺少 tray_watchdog.exe 和 .NET Framework 编译器")

    if not os.path.isdir(PYI_ENV):
        sys.exit("缺少 pyi_env（PyInstaller 未安装）。先跑 get_pyinstaller.py 再 pip 装。")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [PYI_ENV, LIBS] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    args = [
        sys.executable, "-m", "PyInstaller",
        # 不用 --clean：它会批量清空 build_tmp 与缓存目录，在本机沙箱里会被
        # 批量删除保护拦下（SAFE_DELETE_BULK_CONFIRM_REQUIRED）。
        # 增量重建结果完全一致，只是复用已有的工作目录。
        "--noconfirm",
        "--onefile", "--noconsole", "--noupx",
        "--name", NAME,
        "--icon", ICON,
        "--paths", LIBS,
        "--hidden-import", "PIL._tkinter_finder",
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
    ]
    if _ENTRY_FILE in ("desktop_suite.py", "desktop_suite_edge.py"):
        args += ["--add-binary", TRAY_WATCHDOG_EXE + os.pathsep + "."]
    # 瘦身：这些模块解析后根本用不到，但会被 hook 连带拖进来（解包后合计 ~39MB）
    #   numpy 25.8MB  —— PIL 的可选依赖，本程序不用 ndarray
    #   PIL._avif 7.5MB —— AVIF 编解码器，只用到 PNG 内存图
    #   libcrypto/libssl 5.7MB —— 由 ssl/_ssl/_hashlib 带入；本程序不联网
    #     注意 hashlib 保留（comtypes 用它算缓存键），只排 _hashlib 这个 OpenSSL 绑定
    args += [f"--exclude-module={m}" for m in (
        "numpy",
        "ssl", "_ssl", "_hashlib",
        "PIL._avif", "PIL.AvifImagePlugin",
        "PIL._webp", "PIL.WebPImagePlugin",
        "PIL._imagingcms", "PIL.ImageCms",
        "PIL.ImageQt", "PIL.ImageGrab", "PIL.ImageShow",
    )]
    args.append(ENTRY)
    print(">>", " ".join(args[2:]), flush=True)
    r = subprocess.run(args, env=env)
    if r.returncode != 0:
        sys.exit(f"PyInstaller 失败，返回码 {r.returncode}")

    src = os.path.join(DIST, NAME + ".exe")
    dst = os.path.join(ROOT, NAME + ".exe")
    shutil.copy2(src, dst)
    print(f"\nDONE -> {dst}  ({os.path.getsize(dst)/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
