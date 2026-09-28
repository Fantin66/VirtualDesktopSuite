# -*- coding: utf-8 -*-
"""preview_pill.py — 离线把套件的界面渲染成一张 PNG，用眼睛验收。

**不碰系统**：不读注册表、不动任务栏、不开窗口。只把 suite_ui 里那几个
纯函数按几种真实状态跑一遍，拼成一张验收图。

改配色、排版、字体、动画曲线之后先跑它看一眼 —— 比把 exe 拉起来、
把鼠标挪到任务栏上、按快捷键试快得多，也不会把桌面搞乱。

    "C:\\Program Files\\Python312\\python.exe" source\\preview_pill.py
    → source\\预览-套件界面.png（项目根目录也会有一份）

两个刻意的设计：

1. **尺寸一律按真实值**（药丸窗口高 96，即 200% 缩放下的任务栏物理高度）。
   早先这里图省事按 48 画，结果预览好看、真机上不对 —— 预览必须能当判据用。
2. **底衬是深灰棋盘格，不是"近似任务栏色"**。这是刻意的：药丸那一层只要
   有一丁点没被抠掉的不透明像素，在棋盘格上立刻现形（白带、黑边、方角
   全都藏不住）。要是拿"接近任务栏的颜色"打底，同一个毛病在图上就是隐形的。

输出里刻意带上"最容易露馅"的那几档：浅色任务栏、超长应用名、过渡中间帧、
浮窗出场中间帧。只在深色下好看、只在短名字下好看，都是要返工的。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# libs/ 里是 3.12 编的离线依赖（PIL 的 _imaging 是 cp312）。插到最前会盖住
# site-packages，用 3.13 跑就报 "cannot import name '_imaging' from 'PIL'"。
# 放在最后当兜底，3.12 / 3.13 两边都能跑。
_LIBS = os.path.join(_HERE, "libs")
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.append(_LIBS)

from PIL import Image, ImageChops, ImageDraw          # noqa: E402

import suite_ui as ui                                  # noqa: E402

M = 26          # 页边距
GAP = 26        # 区块间距
BAR_H = 96      # 任务栏物理高度（200% 缩放下就是 96）—— 必须按真值来
CHK = 12        # 棋盘格边长


def key_mask(img):
    """把"键色像素"还原成"透明像素"，这样在预览图里能看见真实的圆角。

    suite_ui 输出的图里，透明的地方被填成了 KEY（tkinter 的 -transparentcolor
    只认精确颜色，留一点抗锯齿灰就会变成显眼的边）。预览时要反过来把它抠掉，
    才看得出实际观感；抠不干净的地方（也就等于真机上会显形的地方）会原样留下。
    """
    rgb = img.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, ui.KEY))
    return diff.convert("L").point(lambda v: 0 if v == 0 else 255)


def checker(w, h, a=(74, 78, 92), b=(54, 57, 68)):
    """深灰棋盘格。装上去当"任务栏"用，专门用来暴露没抠干净的不透明像素。"""
    img = Image.new("RGB", (w, h), a)
    dr = ImageDraw.Draw(img)
    for y in range(0, h, CHK):
        for x in range(0, w, CHK):
            if (x // CHK + y // CHK) % 2:
                dr.rectangle([x, y, x + CHK - 1, y + CHK - 1], fill=b)
    return img


def items(labs, cur, mvd=()):
    """按套件 refresh() 的口径造状态：真实的名字放在 app 上，
    label 是 pill_label() 截过的结果 —— 排版用的是截过的那个，
    预览要是拿全名去算宽度，就会和真机对不上。"""
    out = []
    for i, lb in enumerate(labs, 1):
        it = {"number": i, "label": lb, "is_current": i == cur,
              "is_mvd": i in mvd}
        if i in mvd:
            it["app"] = lb
            it["label"] = ui.pill_label(it)
        out.append(it)
    return out


def main():
    accent = (46, 123, 155)          # 用户当前的强调色（本机实测值）
    f_cap = ui.load_font(15)
    f_note = ui._font_light(12)
    f_h1 = ui.load_font(19)

    m_num = ui.compute_metrics(BAR_H, ["1", "2", "3"])
    m_app = ui.compute_metrics(BAR_H, ["1", "微信", "3"])
    m_long = ui.compute_metrics(BAR_H, ["1", "WindowsTerminal", "3"])

    # (说明, 度量, 状态, 悬停下标, 按下下标, 深色?, 高亮权重, dy, scale)
    plan = [
        ("① 普通桌面：当前 = 第 2 个。颜色只表达这一件事",
         m_num, items(["1", "2", "3"], 2), -1, -1, True, None, 0, 1.0),
        ("② 单应用桌面写应用名。当前正好是它 —— 依然只用强调色，"
         "不另上蓝色，否则两种含义会打架",
         m_app, items(["1", "微信", "3"], 2, mvd=(2,)), -1, -1, True, None,
         0, 1.0),
        ("③ 悬停在非当前药丸上：浅色抬起一档（原来完全没有反馈）",
         m_app, items(["1", "微信", "3"], 1, mvd=(2,)), 1, -1, True, None,
         0, 1.0),
        ("④ 按住不放：再抬一档，松手才真正切桌面",
         m_app, items(["1", "微信", "3"], 1, mvd=(2,)), -1, 1, True, None,
         0, 1.0),
        ("⑤ 超长名字截到 3 个字、宽度有上限，不会把整条拉歪",
         m_long, items(["1", "WindowsTerminal", "3"], 1, mvd=(2,)), -1, -1,
         True, None, 0, 1.0),
        ("⑥ 浅色任务栏：整套配色跟着主题翻，不是硬编码深色",
         m_app, items(["1", "微信", "3"], 2, mvd=(2,)), -1, -1, False, None,
         0, 1.0),
        ("⑦ 切桌面过渡中间帧：高亮从第 1 颗走到第 2 颗（这是 p≈0.5）"
         "—— 原来是一帧跳过去",
         m_app, items(["1", "微信", "3"], 2, mvd=(2,)), -1, -1, True,
         [0.55, 0.45, 0.0], 0, 1.0),
        ("⑧ 浮窗出场中间帧：整排在窗口内向上滑 + 轻微放大。"
         "窗口本身不动，只动像素",
         m_app, items(["1", "微信", "3"], 2, mvd=(2,)), -1, -1, True, None,
         -9, 1.14),
    ]

    panel_rows = [
        {"kind": "head"},
        {"kind": "sep"},
        {"kind": "action", "id": "refresh", "label": "刷新桌面列表",
         "hint": "重新读一遍"},
        {"kind": "sep"},
        {"kind": "section", "label": "快捷键"},
        {"kind": "toggle", "key": "hotkey_taskbar", "label": "切换任务栏自动隐藏",
         "note": "Ctrl+Alt+H"},
        {"kind": "toggle", "key": "hotkey_launch", "label": "把窗口甩到新桌面",
         "note": "Ctrl+Alt+Shift+X"},
        {"kind": "sep"},
        {"kind": "section", "label": "行为"},
        {"kind": "toggle", "key": "auto_app_desktop",
         "label": "窗口最大化时自动跳新桌面"},
        {"kind": "toggle", "key": "mvd_link",
         "label": "单应用桌面上自动隐藏任务栏"},
        {"kind": "toggle", "key": "immersive_hide",
         "label": "全屏桌面上一并藏起药丸"},
        {"kind": "toggle", "key": "fullscreen_fill",
         "label": "全屏补位（个别程序留空档时开）"},
        {"kind": "sep"},
        {"kind": "toggle", "key": "autostart", "label": "开机自启"},
        {"kind": "sep"},
        {"kind": "action", "id": "quit", "label": "退出套件", "danger": True,
         "hint": "Ctrl+Q"},
    ]
    sub = "3 个桌面 · 当前第 2 个 · 1 个单应用"
    p_dark, p_layout = ui.render_menu(panel_rows, accent, True,
                                      title="虚拟桌面套件", subtitle=sub,
                                      hover=5)
    p_light, _ = ui.render_menu(panel_rows, accent, False,
                                title="虚拟桌面套件", subtitle=sub, hover=9)

    # ---------------------------------------------------------- 预留画布
    per = [ui.render_pixmap(m["win_w"], BAR_H, st, accent, dk, m, emph=em,
                            hover=hv, press=pr, dy=dy, scale=sc)
           for _, m, st, hv, pr, dk, em, dy, sc in plan]
    pad_top = M + 52
    col_h = sum(22 + im.height + 16 for im in per)
    panel_y = pad_top + col_h + GAP
    W = max(max(im.width for im in per) + 400,
            M * 2 + p_layout["w"] * 2 + 26)
    H = panel_y + p_layout["h"] + 36 + M

    sheet = Image.new("RGB", (W, H), (24, 24, 27))
    dr = ImageDraw.Draw(sheet)
    dim, bright = (150, 152, 158), (232, 234, 240)

    dr.text((M, M - 8), "虚拟桌面套件 · 界面预览", font=f_h1, fill=bright)
    dr.text((M, M + 20),
            "离线渲染，未连接系统状态。底衬是深灰棋盘格 —— 故意用「不像任务栏」"
            "的颜色：药丸层只要漏出一点不透明像素就会立刻现形，",
            font=f_note, fill=dim)
    dr.text((M, M + 38),
            "棋盘格该透出来的地方就是真的透明（包括圆角）。"
            f"尺寸按真值：药丸窗口高 {BAR_H}px。",
            font=f_note, fill=dim)

    y = pad_top
    for (desc, m, st, hv, pr, dk, em, dy, sc), img in zip(plan, per):
        dr.text((M, y), desc, font=f_cap, fill=bright)
        y += 23
        tag = ("深色主题" if dk else "浅色主题") + \
              ("（浮窗动画帧）" if sc != 1.0 else "")
        strip = checker(img.width + 24, img.height)
        strip.paste(img, (12, 0), key_mask(img))
        sheet.paste(strip, (M, y))
        dr.text((M + strip.width + 16, y + 16),
                f"{tag} · 窗宽 {m['win_w']} · 逐颗 {m['widths']}"
                f" · 圆角 {m['r']} · 药丸高 {m['ph']}",
                font=f_note, fill=dim)
        y += img.height + 16

    dr.text((M, panel_y - 25), "右键控制面板", font=f_h1, fill=bright)
    dr.text((M + 158, panel_y - 21),
            "自绘，替代原来的系统菜单：圆角 / 分组 / 开关 / 悬停高亮，"
            "四角是真的透明，卡片外沿不再有一圈暗边",
            font=f_note, fill=dim)
    sheet.paste(p_dark, (M, panel_y), key_mask(p_dark))
    sheet.paste(p_light, (M + p_layout["w"] + 26, panel_y), key_mask(p_light))
    dr.text((M, panel_y + p_layout["h"] + 8),
            "深色主题（左）· 浅色主题（右）· 悬停那一行铺了一层强调色淡染；"
            "快捷键文字退到了开关左边，不再被压掉",
            font=f_note, fill=dim)

    out = os.path.join(_HERE, "预览-套件界面.png")
    sheet.save(out)
    try:
        sheet.save(os.path.join(os.path.dirname(_HERE), "预览-套件界面.png"))
    except Exception:
        pass
    print(f"已生成 {sheet.size[0]}×{sheet.size[1]} → {out}")


if __name__ == "__main__":
    main()
