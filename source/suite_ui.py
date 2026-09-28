# -*- coding: utf-8 -*-
"""suite_ui.py — 套件里"画"的那一半。

单独拆出来的两个理由：

  1. 这里全是**纯函数 + Pillow**，不碰任何系统状态。所以可以离线把任意状态
     渲染成 PNG 预览（见 preview_pill.py），配色改一版看一眼，比进任务栏里
     反复试快得多；
  2. desktop_suite.py 那边只剩系统交互（虚拟桌面、全局热键、任务栏开关）。
     视觉和交互分开，改一边不会顺手把另一边带崩。

对外约定 —— desktop_suite 会把这些**原样再导出**，verify_suite.py 依赖这些
名字和语义，改名要同步改验证脚本：

    load_font / font_file / text_width / pill_label
    pill_style(item, accent, dark, ...) -> (fill, border, text, border_w)
    compute_metrics(bar_h, labels)      -> dict（ph/gap/pad_l/font/r/widths/win_w）
    render_pixmap(w, h, state, accent, dark, m, ...) -> PIL.Image
    render_menu(rows, accent, dark, title, subtitle, hover) -> (Image, layout)

设计上刻意守住的三条（都是用户明确要过的，别改回去）：
  * **颜色只表达一件事：是不是当前桌面。** 单应用桌面不给单独配色，
    它靠"药丸里写应用名"来区分 —— 否则"当前桌面正好是全屏桌面"时
    两种含义会打架。（verify_suite 里有一条断言专门钉这个）
  * 药丸宽度跟着文字走，但有上限（不超过 ph 的 2.6 倍），不能把整条拉歪。
  * 字体必须能画中日韩字形，否则中文应用名会变成方框（豆腐块）。
"""
import os

from PIL import Image, ImageDraw, ImageFont

# 透明色键。⚠️ 必须同时改 KEY_HEX（tkinter 的 -transparentcolor 参数）
KEY = (1, 2, 3)
KEY_HEX = "#010203"

# 绘制时的超采样倍率。直接按最终尺寸画圆角会全是锯齿，先放大画再缩回来。
# 代价是每次重绘要多花几毫秒 —— 所以动画帧率别开太高，见 desktop_suite 的
# ANIM_MS 注释。
RS = 4

# 全屏桌面上的药丸显示应用名的前几个字：中日韩按 2 个，拉丁按 3 个。
# （2 个 ASCII 字符区分度太差：ch/co/ca 分不清 chrome/code/calc）
LABEL_CJK = 2
LABEL_LATIN = 3

# 药丸那条"底衬"（把整排药丸框在一个浅色圆角块里，像 Win11 自己的分组）
RAIL_INSET = 3          # 底衬相对窗口边缘的内缩
RAIL_VPAD = 5           # 底衬上下比药丸多出来的高度
EDGE_PAD = 6            # 窗口左右给底衬和圆角留的空白


# ---------------------------------------------------------------- 颜色工具
def blend(a, b, t):
    """在 a 和 b 之间线性插值。t=0 得 a，t=1 得 b。"""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else (hi if v > hi else v)


def _lum(c):
    """感知亮度（0=黑 1=白）。用来决定压在强调色上的文字该用黑还是白。"""
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255.0


def contrast_text(bg):
    """压在 bg 上的文字色。强调色可能是深蓝也可能是亮黄，不能写死白色。"""
    return (252, 252, 254) if _lum(bg) < 0.62 else (18, 20, 24)


class Theme:
    """一套配色。由「强调色 + 深/浅色任务栏」推导出全部用色。

    深浅不读壁纸，只用系统那条 SystemUsesLightTheme —— 任务栏基本跟着它走。
    """

    def __init__(self, accent, dark):
        self.accent = tuple(accent)
        self.dark = bool(dark)
        base = (32, 32, 34) if dark else (243, 243, 245)     # 任务栏底色近似
        ink = (237, 239, 243) if dark else (24, 26, 30)      # 前景墨色
        self.base, self.ink = base, ink

        # —— 药丸底部那条浅色轨道
        self.rail = blend(base, ink, 0.050)
        self.rail_edge = blend(base, ink, 0.095)

        # —— 当前所在的桌面：实心强调色。
        #     边框就等于强调色本身（verify_suite 有一条断言钉这个），
        #     立体感靠内层渐变，不靠第二圈描边。
        self.cur_fill = blend(self.accent, (255, 255, 255) if dark else (0, 0, 0),
                              0.06)
        self.cur_fill_lo = blend(self.cur_fill, (0, 0, 0), 0.14)
        self.cur_hi = blend(self.cur_fill, (255, 255, 255), 0.30)
        self.cur_text = contrast_text(self.cur_fill)

        # —— 其他桌面：低对比的"玻璃片"，不抢视线
        self.idle_fill = blend(base, ink, 0.070)
        self.idle_fill_lo = blend(base, ink, 0.030)
        self.idle_edge = blend(base, ink, 0.150)
        self.idle_text = blend(base, ink, 0.700)
        self.idle_hi = blend(base, ink, 0.130)

        # —— 控制面板
        self.panel = blend(base, ink, 0.060)
        self.panel_lo = blend(base, ink, 0.030)
        self.panel_edge = blend(base, ink, 0.170)
        self.panel_title = blend(base, ink, 0.95)
        self.panel_sub = blend(base, ink, 0.52)
        self.panel_hint = blend(base, ink, 0.40)
        self.row_hover = blend(self.panel, self.accent, 0.22)
        self.row_text = blend(base, ink, 0.88)
        self.sep = blend(base, ink, 0.115)
        self.danger = (232, 88, 80) if dark else (198, 42, 34)

        # —— 开关
        self.sw_on = self.accent
        self.sw_off = blend(base, ink, 0.26)
        self.sw_knob = (250, 250, 253) if dark else (255, 255, 255)


_THEME_CACHE = {}


def theme_for(accent, dark):
    """按 (强调色, 深浅) 缓存。每帧每颗药丸都要用，不能重复构造。"""
    k = (tuple(accent), bool(dark))
    t = _THEME_CACHE.get(k)
    if t is None:
        t = Theme(accent, dark)
        _THEME_CACHE[k] = t
    return t


# ---------------------------------------------------------------- 字体
# **必须能画中日韩字形**，否则中文应用名会被画成一个空方框（豆腐块）——
# 单应用桌面的名字取的是进程名前几个字，中文进程名很常见。
# 微软雅黑的拉丁字形本来就源自 Segoe，中英混排不会花，所以优先用它；
# 后面几个纯拉丁的只作最后兜底。
_FONT_CANDIDATES = (
    "msyhbd.ttc", "msyh.ttc", "msyhl.ttc", "simhei.ttf", "simsun.ttc",
    "segoeuib.ttf", "arialbd.ttf",
)
_FONT_CACHE = {}


def load_font(size):
    """按字号取字体，带缓存。

    缓存是必要的：算排版（每颗药丸一次）和渲染（每次重绘）得用**同一个**
    字体对象，反复打开字体文件既慢、也可能拿到不同的度量，宽度就算不准。
    """
    f = _FONT_CACHE.get(size)
    if f is not None:
        return f
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    f = None
    for name in _FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(os.path.join(fonts, name), size)
            break
        except OSError:
            continue
    if f is None:
        f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def font_file(font):
    """这个字体到底来自哪个文件 —— 排查"中文画成方框"时看的就是它"""
    return getattr(font, "path", "") or "?"


def _font_light(size):
    """同族但更细的一档，用来画副标题、提示。取不到就退回粗体。"""
    f = _FONT_CACHE.get(("l", size))
    if f is not None:
        return f
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    f = None
    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc") + _FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(os.path.join(fonts, name), size)
            break
        except OSError:
            continue
    if f is None:
        f = load_font(size)
    _FONT_CACHE[("l", size)] = f
    return f


def text_width(font, s):
    try:
        b = font.getbbox(s)
        return max(1, b[2] - b[0])
    except Exception:
        return max(1, len(s) * 8)


def ellipsize(font, s, max_w):
    """把 s 截到 max_w 以内，尾巴补省略号。一个字都放不下就返回空串。

    为什么需要它：面板右侧的 note/hint 是**右对齐**画的，左边 label 是左对齐，
    两者各画各的、**不互相避让** —— label 一长就直接叠字（实测"只认 Shift+
    最大化"压住了后面那行灰字，画面上是一个字摞在另一个字上）。
    规矩定成：label 是主信息、永远优先；note 只是注释，放不下就砍它。
    """
    if max_w <= 0 or not s:
        return ""
    if text_width(font, s) <= max_w:
        return s
    ell = "…"
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_width(font, s[:mid] + ell) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (s[:lo] + ell) if lo > 0 else ""


# ---------------------------------------------------------------- 标签
def pill_label(item):
    """全屏（单应用）桌面显示应用名的头几个字，其它桌面显示桌面序号。"""
    app = item.get("app")
    if app:
        wide = sum(1 for ch in app if ord(ch) > 0x2E7F)      # 中日韩字符
        return app[:LABEL_CJK if wide else LABEL_LATIN]
    return str(item["number"])


# ---------------------------------------------------------------- 药丸配色
def pill_style(item, accent, dark, emph=None, hover=False, press=False):
    """一颗药丸的 (填充, 边框, 文字, 边框宽度)。

    emph 是"它有多像当前桌面"：0 = 完全不是，1 = 就是当前。
    传 None（默认）就是硬判定，直接用 is_current；
    传 0~1 的中间值就是**过渡中的一帧** —— 切桌面时高亮在两颗之间走，
    靠的就是这个。这条是"药丸切换很生硬"那个问题的着力点。

    hover / press 只作用在非当前的那一档：当前那颗本来就最亮，
    再提亮就看不出反馈了。
    """
    th = theme_for(accent, dark)
    if emph is None:
        e = 1.0 if item.get("is_current") else 0.0
    else:
        e = clamp(emph)

    lift = (0.10 if hover else 0.0) + (0.16 if press else 0.0)
    if lift:
        idle_fill = blend(th.idle_fill, th.ink, lift * 1.5)
        idle_edge = blend(th.idle_edge, th.ink, lift * 1.1)
        idle_text = blend(th.idle_text, th.ink, lift * 0.9)
    else:
        idle_fill, idle_edge, idle_text = (th.idle_fill, th.idle_edge,
                                           th.idle_text)

    if emph is None and e >= 1.0:
        # 硬判定走这一支，保证"当前那颗的边框 == 强调色"这件事**精确**成立
        return th.cur_fill, accent, th.cur_text, 3
    if emph is None:
        return idle_fill, idle_edge, idle_text, 1

    return (blend(idle_fill, th.cur_fill, e),
            blend(idle_edge, accent, e),
            blend(idle_text, th.cur_text, e),
            1 + round(2 * e))


# ---------------------------------------------------------------- 排版
def compute_metrics(bar_h, labels):
    """药丸尺寸 + 整窗宽度。

    宽度跟着文字走：序号是方的、应用名是宽的，硬套同一个宽度会挤。
    但要有上限，否则一个长进程名会把整条药丸拉歪。
    """
    ph = max(30, min(int(bar_h * 0.72), 64))
    gap = max(5, ph // 6)
    font_size = max(13, int(ph * 0.46))
    font = load_font(font_size)
    pw_min = int(ph * 1.4)
    pw_max = int(ph * 2.6)
    widths = []
    for lb in labels:
        widths.append(max(pw_min, min(int(text_width(font, lb) + ph * 0.62),
                                      pw_max)))
    pad_l = EDGE_PAD + RAIL_INSET + 3
    win_w = pad_l + sum(widths) + gap * max(0, len(widths) - 1) + pad_l
    return {
        "ph": ph, "gap": gap, "pad_l": pad_l, "font": font_size,
        "r": max(8, int(ph * 0.30)), "widths": widths, "win_w": win_w,
        "rail_inset": RAIL_INSET, "rail_vpad": RAIL_VPAD,
        "art_h": ph + RAIL_VPAD * 2,
    }


# ---------------------------------------------------------------- 底色渐变
def _vgrad(w, h, top, bottom):
    """一根竖直线性渐变，横着拉宽 —— 给药丸做出"上亮下暗"的体积感。"""
    if h <= 0 or w <= 0:
        return Image.new("RGB", (max(1, w), max(1, h)), top)
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        px[0, y] = blend(top, bottom, y / max(1, h - 1))
    return strip.resize((w, h))


def _draw_pills(win_w, state, accent, dark, m, emph=None, hover=-1, press=-1):
    """把整排药丸画在一张透明 RGBA 上（超采样尺寸）。不掺底色、不打键色。"""
    S = RS
    ph, gap, r = m["ph"], m["gap"], m["r"]
    art_h = m["art_h"]
    img = Image.new("RGBA", (win_w * S, art_h * S), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    th = theme_for(accent, dark)
    font = load_font(m["font"] * S)

    y0 = (art_h * S - ph * S) // 2
    x = m["pad_l"]

    # ⚠️ 这里**不要**画"整排底衬"。曾经加过一条浅色圆角轨道，想把一排框成一个
    #    整体，结果药丸之间和四周露出来的全是那条轨道的颜色 —— 浅色任务栏下就是
    #    一道道**白色空隙和边框**，看着像抠图没抠干净。药丸本来就该是一颗一颗
    #    独立的，间距露出真实任务栏才对。Theme 里的 rail / rail_edge 保留着，
    #    但不要再用。

    for i, item in enumerate(state):
        pw = m["widths"][i]
        x0 = x * S
        e_i = None if emph is None else emph[i]
        fill, border, text, bw = pill_style(
            item, accent, dark, emph=e_i,
            hover=(i == hover), press=(i == press))
        box = [x0, y0, x0 + pw * S, y0 + ph * S]

        # 填充：用渐变代替纯色，上亮下暗，视觉上"有厚度"
        hi, lo = (th.cur_hi, th.cur_fill_lo) if e_i is None and \
            item.get("is_current") else (th.idle_hi, th.idle_fill_lo)
        if emph is not None and e_i is not None:
            # 过渡帧：把"当前/非当前"的两套明暗也按 e 插值
            k = clamp(e_i)
            hi = blend(th.idle_hi, th.cur_hi, k)
            lo = blend(th.idle_fill_lo, th.cur_fill_lo, k)
        mask = Image.new("L", (pw * S, ph * S), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, pw * S - 1, ph * S - 1], radius=r * S, fill=255)
        grad = _vgrad(pw * S, ph * S, blend(fill, hi, 0.55),
                      blend(fill, lo, 0.55))
        img.paste(grad, (x0, y0), mask)

        dr.rounded_rectangle(box, radius=r * S, outline=border + (255,),
                             width=max(1, int(round(bw * S))))
        # 顶部一道很淡的内高光，等价于 1px 的"上边受光"。
        # ⚠️ 必须是不透明色，不能像原来那样用 alpha=110 画 —— 透明色抠图是
        #    二值的，半透明像素会被整条丢掉（见 render_pixmap 的 _flatten）。
        #    直接把白色按比例混进填充色，视觉等价且活得过二值化。
        if bw >= 2:
            hl = blend(fill, (255, 255, 255), 0.16)
            dr.line([x0 + r * S, y0 + max(1, bw * S // 2),
                     x0 + pw * S - r * S, y0 + max(1, bw * S // 2)],
                    fill=hl + (255,), width=max(1, S // 2))

        label = item.get("label") or str(item["number"])
        bbox = dr.textbbox((0, 0), label, font=font)
        tw, thh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        dr.text((x0 + (pw * S - tw) / 2 - bbox[0],
                 y0 + (ph * S - thh) / 2 - bbox[1]),
                label, font=font, fill=text + (255,))
        x += pw + gap
    return img


# 二值化阈值：**没有**底色可用时，alpha 低于它的像素直接当透明丢掉。
# ⚠️ 这正是"圆角有台阶感"的来源 —— 见下面 _flatten 的说明。
EDGE_A = 118

# 有底色可用时的阈值：低到这个程度本来就跟全透没区别，留着反而会在底色
# 估不准的时候露出一圈极淡的色晕。10 ≈ 4% 覆盖率。
SOFT_A = 10


def _flatten(art, width, height, ox, oy, bg=None):
    """把超采样下来的 RGBA 压成「不透明 + 键色透明」的 RGB。

    tkinter 的 -transparentcolor 是**二值**透明的：一个像素要么全透明、要么
    全显示。所以最后这一步必须把 RGBA 变成"要么是内容、要么是键色"。
    两种做法，由 bg 决定：

    ── bg=None：二值化（老路径，兜底）
       把 alpha >= EDGE_A 的当内容、其余当透明。圆角的抗锯齿像素（覆盖率
       30%、60% 那些）只能**一刀切**，圆角于是变成台阶状 —— 用户看到的
       "边角稍微漏了一块、不规整"就是这个。

    ── bg=(r,g,b)：软合成到实测的任务栏底色（新路径）
       把 0 < a < 255 的边缘像素**按 alpha 混到底色上**再设为不透明：
           merged = art * a + bg * (1 - a)
       视觉上等价于真正的抗锯齿，圆的还是圆的。代价是 bg 必须接近任务栏
       真实颜色 —— 由 desktop_suite 抓屏采样；采不到就自动退回老路径，
       所以这条路坏掉的后果只是"回到台阶"，不会变成画错。

    两条路都要先做**反预乘**：`_draw_pills` 里没画到的地方是 (0,0,0,0)，
    PIL 缩图不对 alpha 做预乘，RGB 通道会把那些"黑"平均进边缘，边缘颜色
    被拉向黑色。混合结果是 blended = true*a + KEY*(1-a)，反解即
    true = (blended - KEY*(1-a)) / a。

    只有 a 落在 (0, 255) 之间的像素需要处理（不到全图的 5%），所以逐个处理
    而不是整图遍历 —— 动画帧间隔只有 28ms，这里不能慢。
    """
    a = art.getchannel("A")
    rgb = art.convert("RGB")
    aw = art.size[0]
    px = rgb.load()
    flat = list(a.getdata())       # 一次 C 调用取全部 alpha，别去逐点索引
    for i, v in enumerate(flat):
        if 0 < v < 253:
            x, y = i % aw, i // aw
            k = v / 255.0
            r0, g0, b0 = px[x, y]
            px[x, y] = tuple(
                min(255, max(0, int(round((c - KEY[j] * (1.0 - k)) / k))))
                for j, c in enumerate((r0, g0, b0)))

    if bg is None:
        hard = a.point(lambda v: 255 if v >= EDGE_A else 0)
        out = Image.new("RGB", (width, height), KEY)
        out.paste(rgb, (ox, oy), hard)      # 出界会自动裁掉（动画位移时用得上）
        return out

    # Image.composite(i1, i2, m) = i1*m/255 + i2*(1-m/255)，正好是按覆盖率合成
    merged = Image.composite(rgb, Image.new("RGB", art.size, tuple(bg)), a)
    hard = a.point(lambda v: 255 if v >= SOFT_A else 0)
    out = Image.new("RGB", (width, height), KEY)
    out.paste(merged, (ox, oy), hard)
    return out


def _prepared_pills(width, height, state, accent, dark, m, emph,
                    hover, press, dy, scale):
    """超采样后缩到窗口尺寸，供二值透明和原生 alpha 两条绘制路共用。"""
    art = _draw_pills(width, state, accent, dark, m, emph, hover, press)
    art = art.reduce(RS) if art.width % RS == 0 and art.height % RS == 0 \
        else art.resize((max(1, art.width // RS), max(1, art.height // RS)),
                        Image.BOX)
    if scale != 1.0 and scale > 0:
        nw = max(1, int(round(art.width * scale)))
        nh = max(1, int(round(art.height * scale)))
        art = art.resize((nw, nh), Image.LANCZOS)
    ax = (width - art.width) // 2
    ay = (height - art.height) // 2 + int(dy)
    return art, ax, ay


def render_rgba(width, height, state, accent, dark, m, emph=None,
                hover=-1, press=-1, dy=0, scale=1.0):
    """供 UpdateLayeredWindow 使用的逐像素 alpha 画面。"""
    art, ax, ay = _prepared_pills(width, height, state, accent, dark, m,
                                  emph, hover, press, dy, scale)
    # 缩图会把透明区的黑色平均进 RGB，得到近似预乘色；GDI 随后还会按
    # alpha 合成一次。先恢复边缘的原色，避免这两次相乘形成深色毛边。
    alpha = art.getchannel("A")
    pixels = art.load()
    for i, a in enumerate(alpha.getdata()):
        if 0 < a < 255:
            x, y = i % art.width, i // art.width
            r, g, b, _ = pixels[x, y]
            pixels[x, y] = (min(255, (r * 255 + a // 2) // a),
                            min(255, (g * 255 + a // 2) // a),
                            min(255, (b * 255 + a // 2) // a), a)
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    out.paste(art, (ax, ay))
    return out


def render_pixmap(width, height, state, accent, dark, m, emph=None,
                  hover=-1, press=-1, dy=0, scale=1.0, bg=None):
    """一帧完整画面：药丸，其余地方一律是键色（交给 tkinter 抠成透明）。

    ⚠️ 以前这里顺手把整窗铺成 th.base（任务栏近似底色），想着"跟任务栏融为
    一体"。那是错的：-transparentcolor 是**二值抠像**，铺了底色就等于整块
    都不透明了。药丸图只有 74 高、窗口有 96 高，上下各 11px 的底色就成了一
    条实实在在的色带 —— 浅色主题下 base≈(243,243,245)，看着就是"白边"。
    底色只应该是键色，一块都不要多铺。

    bg 是**实测的任务栏底色**，只用来把圆角的半透明边缘合成得平滑
    （见 _flatten）。它不影响"哪里透明"这件事 —— 药丸之间和上下的空隙
    依旧是键色、依旧是真透明。

    dy / scale 是给动画用的：整排药丸在窗口里上下位移、缩放。
    窗口本身**不动** —— 移动窗口（SetWindowPos）会让分层窗口有一帧露出
    未绘制区域，就是我们看到的"药丸变黑/闪动"。
    """
    art, ax, ay = _prepared_pills(width, height, state, accent, dark, m,
                                  emph, hover, press, dy, scale)
    return _flatten(art, width, height, ax, ay, bg=bg)


# ---------------------------------------------------------------- 控制面板
# 尺寸整体放大约 16%，字号跟着一起放 —— 用户反馈"设置界面的字太小"。
# ⚠️ 字号和行高必须同时改：只放大字号会让文字顶到行框上、跟下一行挤在一起。
MENU_W = 374
TITLE_H = 60
SEC_H = 31
SEP_H = 10
ROW_H = 43
PAD_X = 15
SW_W = 40               # 开关轨道的宽
SW_H = 22               # 开关轨道的高
# label（左对齐）与 note（右对齐）之间最少要留的空隙（实际像素）。
# ⚠️ 这两者是分别对齐画的、**不互相避让**，所以必须有这一道保护，见 render_menu。
NOTE_GAP = 6


def menu_layout(rows):
    """算出每行的高度和纵向位置。面板高度完全由行决定，不写死。"""
    ys, y = [], 0
    for r in rows:
        k = r.get("kind")
        h = {"head": TITLE_H, "section": SEC_H, "sep": SEP_H}.get(k, ROW_H)
        ys.append((y, y + h))
        y += h
    return {"rows": ys, "h": y, "w": MENU_W}


def menu_hit(layout, y):
    """点在第几行上。-1 = 空白。"""
    for i, (a, b) in enumerate(layout["rows"]):
        if a <= y < b:
            return i
    return -1


def _switch(dr, x, y, on, th, S=RS):
    """一个 iOS 风格的开关。比 tk.Menu 的勾选符号清楚得多。"""
    w, h, r = SW_W * S, SW_H * S, SW_H * S // 2
    track = th.sw_on if on else th.sw_off
    dr.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=track + (255,))
    kn = r - 2 * S
    kx = x + w - r - S if on else x + r + S
    dr.ellipse([kx - kn, y + r - kn, kx + kn, y + r + kn],
               fill=th.sw_knob + (255,))


def _fmt(c):
    return "#%02x%02x%02x" % c


def render_menu(rows, accent, dark, title="", subtitle="", hover=-1, bg=None):
    """把控制面板整块画成一张图。返回 (Image, layout)。

    为什么要自绘而不是用 tk.Menu：系统菜单只能出灰底黑字那种老样式，
    跟药丸完全是两套视觉语言。自绘之后圆角、分组、开关、悬停高亮都统一了。

    bg 同 render_pixmap：给了就把圆角的半透明边缘合成到它上，圆角更圆。
    """
    layout = menu_layout(rows)
    W, H = layout["w"], layout["h"]
    S = RS
    th = theme_for(accent, dark)
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)

    # 面板本体：微渐变 + 1px 描边
    mask = Image.new("L", (W * S, H * S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, W * S - 1, H * S - 1], radius=15 * S, fill=255)
    img.paste(_vgrad(W * S, H * S, blend(th.panel, th.ink, 0.035),
                     blend(th.panel, th.ink, 0.010)), (0, 0), mask)
    dr.rounded_rectangle([0, 0, W * S - 1, H * S - 1], radius=15 * S,
                         outline=th.panel_edge + (255,), width=max(1, S // 2))

    f_title = load_font(17 * S)
    f_sub = _font_light(13 * S)
    f_row = load_font(15 * S)
    f_sec = _font_light(13 * S)
    f_note = _font_light(12 * S)

    for i, r in enumerate(rows):
        y0, y1 = layout["rows"][i]
        ys0, ys1 = y0 * S, y1 * S
        k = r.get("kind")
        if k == "sep":
            dr.line([(PAD_X + 2) * S, (ys0 + ys1) // 2,
                     (W - PAD_X - 2) * S, (ys0 + ys1) // 2],
                    fill=th.sep + (255,), width=max(1, S // 2))
            continue
        if k == "head":
            dr.text((PAD_X * S, (y0 + 14) * S), title, font=f_title,
                    fill=th.panel_title + (255,))
            dr.text((PAD_X * S, (y0 + 38) * S), subtitle, font=f_sub,
                    fill=th.panel_sub + (255,))
            # 右上角的关闭叉
            cx, cy = (W - PAD_X - 9) * S, (y0 + 24) * S
            dr.line([cx - 7 * S, cy - 7 * S, cx + 7 * S, cy + 7 * S],
                    fill=th.panel_sub + (255,), width=max(1, int(1.5 * S)))
            dr.line([cx - 7 * S, cy + 7 * S, cx + 7 * S, cy - 7 * S],
                    fill=th.panel_sub + (255,), width=max(1, int(1.5 * S)))
            continue
        if k == "section":
            dr.text((PAD_X * S, (y0 + 11) * S), r.get("label", ""),
                    font=f_sec, fill=th.panel_hint + (255,))
            continue

        # toggle / action 行：悬停时铺一层强调色淡染
        if i == hover:
            dr.rounded_rectangle([4 * S, (ys0 + 3 * S), (W - 4) * S,
                                  (ys1 - 3 * S)],
                                 radius=9 * S,
                                 fill=th.row_hover + (255,))
        cy = (ys0 + ys1) // 2
        label = r.get("label", "")
        color = th.danger if r.get("danger") else th.row_text
        if i == hover and r.get("danger"):
            color = blend(th.danger, (255, 255, 255), 0.25)
        dr.text((PAD_X * S, cy - 15 * S), label, font=f_row,
                fill=color + (255,))
        # ⚠️ 右侧文字的落点要看这一行有没有开关。原来 note 和开关都按
        #    "贴右边"算，于是 "Ctrl+Alt+H" 被开关压掉一半 —— 图上就是
        #    一条被截断的灰字。有开关就退到开关左边，没有才贴右边。
        sw_w = SW_W + 14 if k == "toggle" else 0
        if k == "toggle":
            _switch(dr, (W - PAD_X - SW_W) * S, cy - SW_H * S // 2, r.get("on"),
                    th, S)
        tail = r.get("note") or r.get("hint")
        if tail:
            # ⚠️ 先量一遍再画：label 左对齐、tail 右对齐，不避让就会叠字。
            #    放不下就砍 tail（label 优先），最少留 NOTE_GAP 的空隙。
            x_note_r = (W - PAD_X - 8 - sw_w) * S
            x_label_r = PAD_X * S + text_width(f_row, label)
            tail = ellipsize(f_note, tail, x_note_r - x_label_r - NOTE_GAP * S)
            if tail:
                nw = text_width(f_note, tail)
                dr.text((x_note_r - nw, cy - 7 * S), tail,
                        font=f_note, fill=th.panel_hint + (255,))

    # 与药丸同一套收尾：4x 缩到 1x，再反预乘（给了 bg 就软合成到它上）。
    # ⚠️ 不能沿用"铺 th.base 底色、只抠 alpha==0"的老写法：那样圆角四角会
    #    留四块底色方片，卡片外沿还会有一圈发暗的边（同 render_pixmap 的坑）。
    small = img.reduce(RS)
    return _flatten(small, W, H, 0, 0, bg=bg), layout


# ---------------------------------------------------------------- 缓动
def ease_out(t):
    """快起慢收。界面动效基本都用这一条，比线性自然得多。"""
    t = clamp(t)
    return 1.0 - (1.0 - t) ** 3


def ease_in_out(t):
    t = clamp(t)
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
