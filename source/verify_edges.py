# -*- coding: utf-8 -*-
"""verify_edges.py — 验证"药丸圆角的二值化台阶有没有被消掉"

思路：同一个状态渲染两遍，然后**逐像素对比**这两张图 —— 差异只会出现在圆角
边缘（别处要么是药丸本体、要么是键色，两条路径画得一模一样）。
   ① bg=None        二值化（老路径）：alpha < 118 整片丢掉，圆角变台阶
   ② bg=(任务栏底色) 软合成（新路径）：alpha >= 10 的都留下，并按覆盖率混到底色上

判据：
    · 差异像素数落在合理区间（太少=软合成没生效，太多=动到了不该动的地方）；
    · 差异像素在**新路径里更接近底色** —— 方向必须对，这才能叫"混到底色上"
      而不是"随便变了个色"；
    · 圆角小方块里，新路径"接近底色的像素"明显更多（过渡带存在）；
    · 药丸间隙、窗口上沿/下沿：两条路径都必须仍是键色（透的必须还是透的）；
    · 预热之后再量耗时（第一次调用要建主题缓存、加载字体，量进去会得出
      "新路径比老路径还快"这种荒唐结论）。

全程离线，不碰系统。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
_LIBS = os.path.join(HERE, "libs")
if os.path.isdir(_LIBS):
    # 放 sys.path 最后当兜底，**不要**插到最前：libs/ 里是 3.12 编的离线依赖
    # （PIL 的 _imaging 是 cp312），一旦它盖住 site-packages，用 3.13 跑就报
    # "cannot import name '_imaging' from 'PIL'"。落在最后则两边都能跑。
    sys.path.append(_LIBS)

from PIL import Image, ImageDraw  # noqa: E402

import suite_ui as ui  # noqa: E402

KEY = ui.KEY
BG = (204, 228, 236)          # 本机实测的任务栏底色
ACCENT = (46, 123, 155)       # 本机实测的强调色

STATE = [
    {"number": 1, "label": "1", "is_current": True, "is_mvd": False},
    {"number": 2, "label": "2", "is_current": False, "is_mvd": False},
    {"number": 3, "label": "微信", "is_current": False, "is_mvd": True},
]

ok_all = True


def say(ok, title, detail=""):
    global ok_all
    if not ok:
        ok_all = False
    print(f"  [{'PASS' if ok else 'FAIL'}] {title}"
          + (f"  — {detail}" if detail else ""))


def dist_bg(c):
    return max(abs(c[i] - BG[i]) for i in range(3))


def main():
    m = ui.compute_metrics(96, [s["label"] for s in STATE])
    W, H = m["win_w"], 96
    print(f"窗口 {W}x{H}   药丸高 {m['ph']} 圆角 r={m['r']}  宽 {m['widths']}")
    print(f"底色 {BG}   强调色 {ACCENT}")

    # 预热：首次调用要建缓存、加载字体
    for _ in range(2):
        ui.render_pixmap(W, H, STATE, ACCENT, False, m, bg=BG)
        ui.render_pixmap(W, H, STATE, ACCENT, False, m)

    def bench_pair(fa, fb, n=21):
        """两条路径**交替**测量，再各取中位数。

        ⚠️ 为什么不能"先量完 A、再量完 B"：这台机器的 CPU 会变频，整段测量
        落在睿频窗口里的话 15 次全部快一倍 —— 同一份代码实测有 9.7ms 和
        18.6ms 两档。先 A 后 B 就可能出现"A 落在睿频窗口、B 没落"，凭空差出
        8.6 ms，报一个假 FAIL。交替测量让两条路径经历同样的 CPU 状态。

        ⚠️ 也要取中位数而不是单发：单发同样被文件系统过滤 / GC / 别的程序
        抢 CPU 拉动，量出过多一倍的值。
        """
        ta, tb = [], []
        for _ in range(n):
            t0 = time.perf_counter()
            fa()
            ta.append((time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
            fb()
            tb.append((time.perf_counter() - t0) * 1000.0)
        ta.sort()
        tb.sort()
        return ta[len(ta) // 2], tb[len(tb) // 2]

    t_hard, t_soft = bench_pair(
        lambda: ui.render_pixmap(W, H, STATE, ACCENT, False, m),
        lambda: ui.render_pixmap(W, H, STATE, ACCENT, False, m, bg=BG))

    hard = ui.render_pixmap(W, H, STATE, ACCENT, False, m)
    soft = ui.render_pixmap(W, H, STATE, ACCENT, False, m, bg=BG)

    p1, p2 = hard.load(), soft.load()
    diff = [(x, y) for y in range(H) for x in range(W) if p1[x, y] != p2[x, y]]
    print(f"\n两条路径的差异像素：{len(diff)} 个")
    say(150 <= len(diff) <= 3000, "差异像素数量合理（只动了圆角边缘那一圈）",
        f"{len(diff)} 个")

    if diff:
        d_hard = sum(dist_bg(p1[x, y]) for x, y in diff) / len(diff)
        d_soft = sum(dist_bg(p2[x, y]) for x, y in diff) / len(diff)
        print(f"这些像素到任务栏底色的平均距离：老路径 {d_hard:.0f}，"
              f"新路径 {d_soft:.0f}")
        say(d_soft < d_hard - 15,
            "差异像素在新路径里明显更接近底色（方向对：混到底色上了）",
            f"{d_hard:.0f} → {d_soft:.0f}")

    # 圆角小方块里"接近底色的像素"数量
    r = m["r"]
    y0 = (H - m["ph"]) // 2
    box = (m["pad_l"], y0, m["pad_l"] + r + 2, y0 + r + 2)

    def near_bg_count(img):
        p = img.load()
        n = 0
        for y in range(box[1], box[3]):
            for x in range(box[0], box[2]):
                c = p[x, y]
                if c != KEY and dist_bg(c) <= 40:
                    n += 1
        return n

    n1, n2 = near_bg_count(hard), near_bg_count(soft)
    print(f"左上圆角 {r + 2}x{r + 2} 方块内、接近底色的像素："
          f"老 {n1}，新 {n2}")
    say(n2 >= n1 + 8, "圆角处出现了老路径没有的过渡带",
        f"{n1} → {n2}")

    # 该透的地方必须还是键色
    x_gap = (m["pad_l"] + m["widths"][0] + m["gap"] + m["widths"][1]
             + m["gap"] // 2)
    for tag, img in (("老路径", hard), ("新路径", soft)):
        p = img.load()
        gap_ok = all(p[x_gap, y] == KEY for y in range(H))
        top_ok = all(p[x, 0] == KEY for x in range(W))
        bot_ok = all(p[x, H - 1] == KEY for x in range(W))
        say(gap_ok and top_ok and bot_ok,
            f"{tag}：药丸间隙与窗口上/下沿仍是键色（透的还是透的）",
            f"间隙列 x={x_gap}={gap_ok} 上沿={top_ok} 下沿={bot_ok}")

    # ★★ 底色策略的像素契约 —— 用户 2026-09-23 实拍"深色壁纸上一圈白边"的防线。
    #
    #    浮窗那一路**必须不混底色**（见 desktop_suite.Suite.bg_for）。但"浮窗传了
    #    哪个底色"是 render() 里的一个分支，纯函数测不到；这里锁住的是**更底层的
    #    事实**：不给底色的那条路径，圆角处**一个"接近底色的像素"都不该有**。
    #
    #    ⚠️ 别去判四角 —— 四角是**纯透明区**（alpha==0），_flatten 对 alpha==0 本来
    #       就原样保留键色（真机上交给 -transparentcolor 抠掉），给不给底色都一样。
    #       只有 0<alpha<255 的**半透明边缘**才会被混色，而那正好落在圆角那一圈。
    #       第一版就是判错了地方，报了个假 FAIL。
    say(n1 == 0,
        "不给底色时圆角处没有任何「接近底色」的像素 —— 这就是浮窗那一路必须走的状态",
        f"老路径 {n1} 个" + ("（一旦不为 0，说明这把底色混进去了）" if n1 else ""))
    print(f"（对照：给了底色时圆角处有 {n2} 个混色像素 —— 嵌入版才该长这样）")

    print(f"\n单帧耗时（21 次交替测量取中位数）：老路径 {t_hard:.1f} ms，"
          f"新路径 {t_soft:.1f} ms（动画帧间隔 28 ms）")
    say(t_soft - t_hard < 8.0 and t_soft < 28.0,
        "软合成的额外开销可以接受（单帧仍在 28 ms 帧预算内）",
        f"多 {t_soft - t_hard:.1f} ms，合计 {t_soft:.1f} ms")

    # 对比图：左上圆角放大并排。
    # ⚠️ 一定要**先按任务栏底色合成**再放大。直接拿渲染结果看（键色是一块怪色），
    #    左右两边都"和背景不一样"，看着差不多；合成之后才是用户在真机上看到的
    #    样子 —— 左边是一格一格的硬边（覆盖率被一刀切成 0/1），右边是连续过渡。
    def over_taskbar(img):
        out = img.copy()
        o = out.load()
        for yy in range(out.size[1]):
            for xx in range(out.size[0]):
                if o[xx, yy] == KEY:
                    o[xx, yy] = BG
        return out

    Z = 12
    cw = r + 6
    a = over_taskbar(hard).crop((m["pad_l"] - 2, y0 - 2,
                                 m["pad_l"] - 2 + cw, y0 - 2 + cw))
    b = over_taskbar(soft).crop((m["pad_l"] - 2, y0 - 2,
                                 m["pad_l"] - 2 + cw, y0 - 2 + cw))
    sheet = Image.new("RGB", (cw * Z * 2 + 34, cw * Z + 62), (250, 250, 252))
    sheet.paste(a.resize((cw * Z, cw * Z), Image.NEAREST), (8, 30))
    sheet.paste(b.resize((cw * Z, cw * Z), Image.NEAREST), (cw * Z + 26, 30))
    d = ImageDraw.Draw(sheet)
    f = ui.load_font(15)
    d.text((8, 8), "改前：二值化 → 台阶", font=f, fill=(190, 40, 34))
    d.text((cw * Z + 26, 8), "改后：软合成 → 圆角", font=f, fill=(20, 110, 70))
    d.text((8, cw * Z + 38),
           f"两者都按任务栏底色 {BG} 合成过 = 真机上看到的样子",
           font=ui.load_font(13), fill=(90, 90, 96))
    out = os.path.join(ROOT, "预览-圆角对比.png")
    sheet.save(out)
    print(f"已存 {out} {sheet.size}（{Z}x 放大，左上圆角，已按任务栏底色合成）")

    # ---- 第二张图：**整颗药丸贴在深色壁纸上**的样子，直接对应用户报的现象。
    # 用户 2026-09-23 的实拍：任务栏自动隐藏 → 浮窗露在深色地图壁纸上，
    # 一圈浅色的圆角边在深色背景上非常刺眼。根因是浮窗那一路也在做软合成，
    # 把"实测的任务栏底色"混到了自己边缘上，而它脚下根本不是任务栏。
    #   上图 = 改前（浮窗误用任务栏底色）→ 一圈白边
    #   下图 = 改后（浮窗不混底色）    → 干净的台阶
    WALL = (34, 48, 62)          # 深色地图壁纸的近似色（照用户截图取的）

    def over_wall(img):
        o_img = img.copy()
        o = o_img.load()
        for yy in range(o_img.size[1]):
            for xx in range(o_img.size[0]):
                if o[xx, yy] == KEY:
                    o[xx, yy] = WALL
        return o_img

    Z2, lab_h, pad = 2, 24, 16
    w_img = W * Z2
    h_img = H * Z2
    sheet2 = Image.new("RGB",
                       (w_img + pad * 2, 30 + (lab_h + h_img) * 2 + pad),
                       WALL)
    sheet2.paste(over_wall(soft).resize((w_img, h_img), Image.NEAREST),
                 (pad, 30 + lab_h))
    sheet2.paste(over_wall(hard).resize((w_img, h_img), Image.NEAREST),
                 (pad, 30 + (lab_h + h_img) + lab_h))
    d2 = ImageDraw.Draw(sheet2)
    d2.text((pad, 6), "浮窗药丸贴在深色壁纸上（就跑你截图那个场景）",
            font=ui.load_font(15), fill=(232, 234, 240))
    d2.text((pad, 30 + 3), "改前：浮窗混了任务栏底色 → 一圈白边",
            font=ui.load_font(13), fill=(255, 150, 140))
    d2.text((pad, 30 + lab_h + h_img + 3), "改后：浮窗不混底色 → 台阶干净",
            font=ui.load_font(13), fill=(150, 232, 175))
    out2 = os.path.join(ROOT, "预览-浮窗白边.png")
    sheet2.save(out2)
    print(f"已存 {out2} {sheet2.size}（2x，深色壁纸背景）")

    print("\n合计：" + ("全部通过" if ok_all else "**有失败项**"))
    return 0 if ok_all else 1


sys.exit(main())
