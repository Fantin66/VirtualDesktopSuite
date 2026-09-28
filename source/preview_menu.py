# -*- coding: utf-8 -*-
"""preview_menu.py — 把控制面板按真尺寸渲染成 PNG，离线看排版

为什么需要它：面板的尺寸/字号/落点全是写死的数字，改完必须能**立刻用眼睛
验收**，而不是装进任务栏里点开看。

⚠️ 行数据**不再手抄**（2026-09-23 改过一版）：原来这里有一份手写的 ROWS，
   跟 desktop_suite.Suite._panel_rows 是两套东西，改了真面板忘了改这里就会
   悄悄漂移。现在直接调真方法生成 —— 用一个假 self 冒充 Suite 实例，
   不起窗口、不碰系统。代价是这个脚本要 import desktop_suite，得用**带
   tkinter 的解释器**跑（本机 C:\\Program Files\\Python312\\python.exe）。

用法： C:\\Program Files\\Python312\\python.exe source\\preview_menu.py
产出： 根目录 预览-控制面板.png / 预览-控制面板2x.png
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
_LIBS = os.path.join(HERE, "libs")
if os.path.isdir(_LIBS):
    # 放 sys.path 最后当兜底，**不要**插到最前：libs/ 里是 3.12 编的离线依赖
    # （PIL 的 _imaging 是 cp312），一旦它盖住 site-packages，用 3.13 跑就报
    # "cannot import name '_imaging' from 'PIL'"。落在最后则两边都能跑。
    sys.path.append(_LIBS)

from PIL import Image  # noqa: E402

# 导入套件模块只为取得真实菜单行，不能触发模块顶层的单实例接管。
os.environ["VDB_NO_MUTEX"] = "1"
import desktop_suite as S  # noqa: E402
import suite_ui as ui      # noqa: E402


class _FakeSuite:
    """冒充 Suite 实例 —— 只需要 _panel_rows 用到的那几样"""

    def __init__(self):
        self.cfg = dict(S.DEFAULT_SETTINGS)
        self.state = [
            {"number": 1, "label": "1", "is_current": False, "is_mvd": False},
            {"number": 2, "label": "2", "is_current": True, "is_mvd": False},
            {"number": 3, "label": "微信", "is_current": False, "is_mvd": True},
        ]

    def _hotkey_combo(self, hid):
        return {S.HK_TASKBAR: "Ctrl+Alt+T",
                S.HK_LAUNCH: "Ctrl+Alt+Shift+X"}.get(hid, "—")

    def _autostart_on(self):
        return False

    def mod_label(self):
        return S.MVD_MODS[S.MVD_MOD_DEFAULT][0]


ROWS, SUB = S.Suite._panel_rows(_FakeSuite())
ACCENT = (46, 123, 155)          # 本机实测的强调色（DWM\AccentColor, ABGR 解出）


def main():
    # hover 挑一行高亮，看"鼠标移到某行"是长什么样的（找那条独占开关）
    hover = next((i for i, r in enumerate(ROWS)
                  if r.get("id") == "mvd_mod"), 0)
    img, layout = ui.render_menu(
        ROWS, ACCENT, False, title="虚拟桌面套件", subtitle=SUB, hover=hover)

    print("面板布局（由 desktop_suite.Suite._panel_rows 直接生成）")
    print("  副标题：%s" % SUB)
    print("  尺寸 %dx%d" % (layout["w"], layout["h"]))
    for i, (a, b) in enumerate(layout["rows"]):
        r = ROWS[i]
        tail = r.get("label") or ""
        if r.get("kind") == "toggle":
            tail += f"   [{'开' if r.get('on') else '关'}]"
        if r.get("note"):
            tail += f"   （{r['note']}）"
        print("  行%-2d y=%3d..%-3d 高%-3d %-9s %s"
              % (i, a, b, b - a, r.get("kind"), tail))

    out = os.path.join(ROOT, "预览-控制面板.png")
    img.save(out)
    img.resize((img.width * 2, img.height * 2), Image.NEAREST).save(
        os.path.join(ROOT, "预览-控制面板2x.png"))
    print("\n已存", out, img.size)


main()
