# -*- coding: utf-8 -*-
"""verify_panel.py — 控制面板的纯函数断言（不启动 GUI、不碰系统）

起因：面板上原来**八个开关全是同一个样子**，跟实际配置完全不符 —— 因为
desktop_suite.Suite._panel_rows() 造 toggle 行时压根没给 `on` 字段，而
suite_ui._switch 收到 None 就一律当 False 画。这条断言就是防它再回来。

做法：Suite._panel_rows 是普通方法，用一个假的 self 调它就行 ——
不需要真的起进程、起窗口。

另外验证：
  · 开关值必须**跟着配置走**（改 cfg，on 就跟着变）；
  · on=True / on=False 渲染出来的开关区域必须**明显不同**，而且 True 那张
    要含强调色（否则"开着"和"关着"看起来一样）；
  · 面板行结构稳定：首行是 head、末行是退出项、sep 不夹在 head 前后出错；
  · menu_hit 对每一行的中位点返回正确索引（点的落点和画的落点必须是一套）；
  · 字号比"字太小"那版确实放大了。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_LIBS = os.path.join(HERE, "libs")
if os.path.isdir(_LIBS):
    # 放 sys.path 最后当兜底，**不要**插到最前：libs/ 里是 3.12 编的离线依赖
    # （PIL 的 _imaging 是 cp312），一旦它盖住 site-packages，用 3.13 跑就报
    # "cannot import name '_imaging' from 'PIL'"。落在最后则两边都能跑。
    sys.path.append(_LIBS)

# 纯函数验证不可关闭用户正在运行的套件实例。
os.environ["VDB_NO_MUTEX"] = "1"
import desktop_suite as S     # noqa: E402
import suite_ui as ui         # noqa: E402

ACCENT = (46, 123, 155)
ok_all = True


def say(ok, title, detail=""):
    global ok_all
    if not ok:
        ok_all = False
    print(f"  [{'PASS' if ok else 'FAIL'}] {title}"
          + (f"  — {detail}" if detail else ""))


class Fake:
    """冒充 Suite 实例 —— 只需要 _panel_rows 用到的那几样"""
    def __init__(self, **cfg):
        # 贴真实默认值：mvd_shift_only=True（只有 Shift+最大化 才独占）。
        # 漏掉某一项不会让断言炸（sw() 里 bool(None)=False 仍是布尔），
        # 但那样测的就是"假配置下的面板"，不是用户真正看到的那一版。
        self.cfg = {"hotkey_taskbar": True, "hotkey_launch": True,
                    "auto_app_desktop": True, "mvd_shift_only": True,
                    "mvd_modifier": "shift",
                    "mvd_link": True,
                    "immersive_hide": False, "fullscreen_fill": False}
        self.cfg.update(cfg)
        self.state = [
            {"number": 1, "label": "1", "is_current": False, "is_mvd": False},
            {"number": 2, "label": "2", "is_current": True, "is_mvd": False},
            {"number": 3, "label": "微信", "is_current": False, "is_mvd": True},
        ]

    def _hotkey_combo(self, hid):
        return "Ctrl+Alt+H"

    def _autostart_on(self):
        return False

    # _panel_rows 会把当前修饰键写进标签（"只认 Shift+最大化"），所以假 self
    # 也得有这个口子 —— 用真的那套表，别自己编一个字符串。
    def mod_label(self):
        return S.MVD_MODS.get(
            str(self.cfg.get("mvd_modifier", S.MVD_MOD_DEFAULT)).lower(),
            S.MVD_MODS[S.MVD_MOD_DEFAULT])[0]


def rows_of(**cfg):
    return S.Suite._panel_rows(Fake(**cfg))


def main():
    rows, sub = rows_of()
    print(f"面板 {len(rows)} 行，副标题「{sub}」")

    toggles = [r for r in rows if r.get("kind") == "toggle"]
    missing = [r.get("key") for r in toggles if not isinstance(r.get("on"), bool)]
    say(len(toggles) >= 6 and not missing,
        "每个开关行都带了布尔 on 字段",
        f"{len(toggles)} 个开关，缺 on 的：{missing or '无'}")

    # "允许独占"和"只认 Shift"是**两件事**，必须能单独开关。
    # 以前揉成一件事（关掉自动开关会把 Shift 那条路也关掉），所以把两条都锁在契约里。
    keys = [r.get("key") for r in toggles]
    pair = {"auto_app_desktop", "mvd_shift_only"} <= set(keys)
    on_pair = all(next(r["on"] for r in toggles if r.get("key") == k) is True
                  for k in ("auto_app_desktop", "mvd_shift_only"))
    say(pair and on_pair, "独占桌面拆成两个独立开关，默认都是开着",
        f"keys={keys}" if not pair else f"默认 on={on_pair}")

    # 修饰键必须**显示**出来，而且跟着配置走 —— 用户换过键之后，面板上要是还写
    # "只认 Shift+最大化"，他就会照着错的键去按。
    ctrl_rows, _ = rows_of(mvd_modifier="ctrl")
    lab = next((r.get("label") for r in ctrl_rows
                if r.get("key") == "mvd_shift_only"), "")
    mod_row = next((r for r in ctrl_rows if r.get("id") == "mvd_mod"), None)
    mod_lab = mod_row.get("label") if mod_row else None
    say("Ctrl+最大化" in lab and mod_lab is not None and "Ctrl" in mod_lab,
        "面板写的是当前配的修饰键，不是硬编码 Shift",
        f"开关行={lab!r}；换键行={mod_lab!r}")

    # on 要跟着配置走
    a, _ = rows_of(hotkey_taskbar=True)
    b, _ = rows_of(hotkey_taskbar=False)
    va = next(r["on"] for r in a if r.get("key") == "hotkey_taskbar")
    vb = next(r["on"] for r in b if r.get("key") == "hotkey_taskbar")
    say(va is True and vb is False, "开关值跟着配置走", f"True→{va}  False→{vb}")

    autostart = next((r for r in rows if r.get("key") == "autostart"), None)
    say(autostart is not None and autostart.get("on") is False,
        "开机自启按 Startup 文件夹里的 .vbs 判断（这里是 False）",
        str(autostart.get("on")))

    # 退出项不能挂按不出来的快捷键提示
    quit_row = next((r for r in rows if r.get("id") == "quit"), None)
    say(quit_row is not None and not quit_row.get("hint"),
        "退出项没有假的快捷键提示", f"hint={quit_row.get('hint')!r}")

    say(rows[0].get("kind") == "head", "首行是标题行", str(rows[0].get("kind")))
    say(rows[-1].get("id") == "quit", "末行是退出", str(rows[-1].get("id")))

    # 渲染：on 与 off 必须能看出差别，且"开"要带强调色
    def render(on):
        r = [{"kind": "toggle", "key": "k", "label": "测试开关", "on": on}]
        img, lay = ui.render_menu(r, ACCENT, False, title="t", subtitle="s")
        return img, lay

    i_on, lay = render(True)
    i_off, _ = render(False)
    region = (lay["w"] - ui.PAD_X - ui.SW_W - 2, 0, lay["w"], lay["h"])
    c_on = set(i_on.crop(region).getdata())
    c_off = set(i_off.crop(region).getdata())
    print(f"开关区域颜色数：开={len(c_on)} 关={len(c_off)}  "
          f"共同={len(c_on & c_off)}")

    def has_accent(colors):
        return any(max(abs(c[i] - ACCENT[i]) for i in range(3)) <= 10
                   for c in colors)

    say(has_accent(c_on) and not has_accent(c_off),
        "开着的那张开关区域含强调色，关着的不含",
        f"开={has_accent(c_on)} 关={has_accent(c_off)}")
    say(len(c_on ^ c_off) > 20, "开/关两种状态的像素确实不同",
        f"差异色 {len(c_on ^ c_off)} 种")

    # menu_hit：每行中点必须落在自己那一行
    bad = []
    for i, (y0, y1) in enumerate(lay["rows"]):
        if ui.menu_hit(lay, (y0 + y1) // 2) != i:
            bad.append(i)
    say(not bad, "menu_hit 对每行中点都返回正确索引", f"错行：{bad or '无'}")

    # 字号确实放大了（原来 MENU_W=322 / ROW_H=37）
    say(ui.MENU_W > 322 and ui.ROW_H > 37,
        "面板尺寸比「字太小」那版大", f"MENU_W={ui.MENU_W} ROW_H={ui.ROW_H}")

    # ---- 右侧 note 不能跟左侧 label 叠字 ----
    # 起因：label 左对齐、note 右对齐，两者各画各的不避让，"只认 Shift+最大化"
    # 直接压住了后面那行灰字（预览图上一个字摞在另一个字上）。
    # 修法是画之前量一遍、放不下就把 note 截断（label 优先）。
    f = ui._font_light(12 * ui.RS)
    cases = [
        ("Ctrl+Alt+T", 400, "Ctrl+Alt+T"),          # 放得下 → 原样
        ("关掉＝任何最大化都独占（旧行为）", 120, None),   # 放不下 → 截断且更短
        ("随便什么", 0, ""),                          # 一点空间都没有 → 空串
    ]
    bad = []
    for s, room, want in cases:
        got = ui.ellipsize(f, s, room)
        if want is None:
            if not (0 < ui.text_width(f, got) <= room and got.endswith("…")
                    and len(got) < len(s)):
                bad.append(f"{s!r}/{room}→{got!r}")
        elif got != want:
            bad.append(f"{s!r}/{room}→{got!r}≠{want!r}")
    say(not bad, "note 放不下时会被截断，不会跟 label 叠字",
        "；".join(f"「{s}」{room}px→{ui.ellipsize(f, s, room)!r}"
                  for s, room, _ in cases) if not bad else "；".join(bad))

    # 每个带 note 的行都得留得下 —— 至少 NOTE_GAP 的空隙，否则就轮到 label 出界
    tight = []
    for r in rows:
        tail = r.get("note") or r.get("hint")
        if not tail:
            continue
        sw_w = ui.SW_W + 14 if r.get("kind") == "toggle" else 0
        room = ((ui.MENU_W - ui.PAD_X - 8 - sw_w) * ui.RS
                - ui.PAD_X * ui.RS - ui.text_width(f, r.get("label", ""))
                - ui.NOTE_GAP * ui.RS)
        if room <= 0:
            tight.append(r.get("label"))
    say(not tight, "每一行的 label 之后都还留得下 note 的位置",
        f"挤到没位置的={tight or '无'}")

    print("\n合计：" + ("全部通过" if ok_all else "**有失败项**"))
    return 0 if ok_all else 1


sys.exit(main())
