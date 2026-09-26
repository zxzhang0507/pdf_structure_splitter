"""结构分析模块单元测试：矩形合并、区域缩放、颜色折算。

**不依赖真实 PDF** —— 用合成矩形数据验证纯逻辑部分。
需要真实 PDF 的端到端验证见 ``headless_check.py``。

运行：
    python tests/test_structure.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf  # noqa: E402

from core.structure import (  # noqa: E402
    BIG_REGION_PT2,
    MAX_REGION_DPI,
    color_chroma,
    merge_rects,
    region_scale,
)

FAILURES: list[str] = []


def check(label: str, got, expect) -> None:
    ok = got == expect
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f" != {expect!r}"))
    if not ok:
        FAILURES.append(f"{label}: {got!r} != {expect!r}")


# ----------------------------------------------------------------------
# 颜色折算
# ----------------------------------------------------------------------

def test_color_chroma() -> None:
    """矢量颜色（0..1 浮点）必须折算到与像素色度（0..255）同一量纲。

    否则 ``color_threshold`` 无法同时用于两者 —— 把 (0,0,1) 蓝色
    按原值算 chroma=1，会远小于阈值 15 而被当成灰色。
    """
    print("颜色折算（矢量 0..1 -> 0..255 量纲）：")
    check("纯黑", color_chroma((0.0, 0.0, 0.0)), 0.0)
    check("纯白", color_chroma((1.0, 1.0, 1.0)), 0.0)
    check("中灰", color_chroma((0.5, 0.5, 0.5)), 0.0)
    check("纯红", color_chroma((1.0, 0.0, 0.0)), 255.0)
    check("纯蓝", color_chroma((0.0, 0.0, 1.0)), 255.0)
    # 关键回归：蓝色必须超过默认阈值 15，否则彩色矢量会被判成灰色
    check("纯蓝 > 默认阈值 15", color_chroma((0.0, 0.0, 1.0)) > 15, True)
    check("淡蓝 (0.8,0.85,1.0)", round(color_chroma((0.8, 0.85, 1.0)), 2), 51.0)
    check("近灰 (0.5,0.51,0.5)", round(color_chroma((0.5, 0.51, 0.5)), 2), 2.55)

    # None（无填充/无描边）与非法值都不能崩
    check("None -> 0", color_chroma(None), 0.0)
    check("非法值 -> 0", color_chroma("abc"), 0.0)
    check("空串 -> 0", color_chroma(""), 0.0)
    # 防御：个别构造返回 0..255 整数
    check("已是 0..255 的整数", color_chroma((255, 0, 0)), 255.0)


# ----------------------------------------------------------------------
# 矩形合并
# ----------------------------------------------------------------------

def test_merge_rects() -> None:
    """矩形合并：重叠/相邻的块合成一块，减少渲染次数。"""
    print("\n矩形合并：")

    # 两个不相交的矩形各自保留
    got = merge_rects([(0, 0, 10, 10), (100, 100, 110, 110)])
    check("不相交不合", len(got), 2)

    # 重叠的合并成一个
    got = merge_rects([(0, 0, 10, 10), (5, 5, 15, 15)])
    check("重叠合并成 1 块", len(got), 1)
    check("  合并后范围", tuple(round(v) for v in got[0]), (0, 0, 15, 15))

    # 间距小于 gap 的也合并（表格线、跨栏图的碎片）
    got = merge_rects([(0, 0, 10, 10), (11, 0, 20, 10)], gap=2.0)
    check("间隙 1pt < gap 2pt -> 合并", len(got), 1)

    # 间距大于 gap 的不合并
    got = merge_rects([(0, 0, 10, 10), (20, 0, 30, 10)], gap=2.0)
    check("间隙 10pt > gap 2pt -> 不合", len(got), 2)

    # 空/非法输入
    check("空列表", merge_rects([]), [])
    check("退化矩形被丢弃", merge_rects([(5, 5, 5, 5)]), [])
    check("反向矩形被丢弃", merge_rects([(10, 10, 0, 0)]), [])
    check("None 被跳过", len(merge_rects([None, (0, 0, 10, 10)])), 1)

    # 超过 limit 时退化为一个总包围盒（避免 O(n²) 拖垮检测）
    many = [(i * 100, 0, i * 100 + 5, 5) for i in range(50)]
    got = merge_rects(many, limit=10)
    check("超 limit -> 并成 1 块", len(got), 1)
    check("  该块覆盖全部",
          tuple(round(v) for v in got[0]), (0, 0, 4905, 5))

    # 大量矩形仍应在合理时间内完成（性能护栏）
    import time
    big = [(i * 3, i * 3, i * 3 + 2, i * 3 + 2) for i in range(2000)]
    t0 = time.time()
    out = merge_rects(big)
    elapsed = time.time() - t0
    check(f"2000 个矩形合并耗时 < 2s（实测 {elapsed:.3f}s）", elapsed < 2.0, True)
    check("  结果非空", len(out) > 0, True)


# ----------------------------------------------------------------------
# 区域缩放
# ----------------------------------------------------------------------

def test_region_scale() -> None:
    """区域渲染精度：大图按基准 dpi，小图标自动提精度。

    关键是**按短边**决定 —— 一条 400×1 pt 的细线若按长边算就完全
    不需要提精度，但它在 75 dpi 下不足 1 像素宽（实测高度比例为 0），
    渲染不出颜色。
    """
    print("\n区域缩放（短边决定精度）：")

    base = region_scale((0, 0, 400, 400), 75)
    check("大区域 = 基准 dpi", round(base * 72), 75)

    # 小区域提精度
    small = region_scale((0, 0, 8, 8), 75)
    check("8pt 小图提精度", small > base, True)
    check("  提精度后短边足够", 8 * small >= 240 or small >= MAX_REGION_DPI / 72 - 1e-6, True)

    # 细长条按短边提精度（关键：不能按长边）
    strip = region_scale((0, 0, 400, 0.4), 75)
    check("400x0.4 细线提精度", strip > base, True)
    check("  受 MAX_REGION_DPI 封顶", round(strip * 72) <= MAX_REGION_DPI + 1, True)

    # 超大区域不提精度（否则会渲染出巨型图像）
    huge = region_scale((0, 0, 2000, 2000), 75)
    check("超大区域 = 基准 dpi（不暴涨）", round(huge * 72), 75)

    # 面积超过 BIG_REGION_PT2 时直接返回基准
    area_over = (BIG_REGION_PT2 ** 0.5 + 1) ** 2
    side = area_over ** 0.5
    got = region_scale((0, 0, side, side), 75)
    check("面积超限 -> 基准 dpi", round(got * 72), 75)

    # 渲染像素数不应爆炸：任何区域的渲染像素都应在合理范围
    for rect in [(0, 0, 400, 0.4), (0, 0, 1, 1), (0, 0, 8, 8), (0, 0, 500, 700)]:
        scale = region_scale(rect, 75)
        px = (rect[2] - rect[0]) * scale * (rect[3] - rect[1]) * scale
        check(f"  {rect[2]-rect[0]:.0f}x{rect[3]-rect[1]:.1f} -> {int(px)}px 合理",
              px <= 12_000_000, True)


# ----------------------------------------------------------------------
# 退化规则线（真实 PDF 里实测到的陷阱）
# ----------------------------------------------------------------------

def test_degenerate_line_bbox() -> None:
    """退化矩形（高度为 0 的规则线）必须被还原成有面积的窄条。

    **实测数据**：真实论文 PDF 的页眉横线 bbox 是
    ``(89.86, 63.98, 505.42, 63.98)`` —— 415 pt 长、高度 0。
    若原样使用：渲染时面积为 0 会直接失败，``merge_rects`` 也会把它丢掉，
    于是"页眉有横线"这个事实就消失了。
    """
    print("\n退化规则线还原：")
    from core.structure import _path_bbox

    page = pymupdf.Rect(0, 0, 595, 842)

    # 高度为 0 的横线
    box = _path_bbox(
        {"rect": (89.86, 63.98, 505.42, 63.98), "width": 0.4}, page
    )
    check("横线被还原（非 None）", box is not None, True)
    check("  高度 > 0", box[3] - box[1] > 0, True)
    check("  长度保留（含外扩）", round(box[2] - box[0], 1), 416.6)
    check("  线宽被计入（高度 >= 线宽）", box[3] - box[1] >= 0.4, True)

    # 宽度为 0 的竖线
    box_v = _path_bbox(
        {"rect": (100.0, 10.0, 100.0, 300.0), "width": 0.8}, page
    )
    check("竖线被还原", box_v is not None and box_v[2] - box_v[0] > 0, True)

    # 真·一个点：不成图形
    check("一个点 -> None",
          _path_bbox({"rect": (50, 50, 50, 50), "width": 1.0}, page), None)

    # 正常矩形原样返回
    box_n = _path_bbox({"rect": (10, 10, 100, 100), "width": 1.0}, page)
    check("正常矩形不变", tuple(round(v) for v in box_n), (10, 10, 100, 100))

    # 超页面的矩形被夹到页面内
    box_c = _path_bbox({"rect": (-50, -50, 700, 900), "width": 1.0}, page)
    check("超界被夹取", tuple(round(v) for v in box_c), (0, 0, 595, 842))


def test_unresolved_only_shading() -> None:
    """「需渲染」的判据只能是 ``shading``，不能用 fill_opacity 或 type。

    **实测数据**（真实论文 PDF，6007 条路径）：
    * ``fill_opacity`` 出现在**每一条**填充路径上，值几乎恒为 1.0；
      用它当渐变信号会把所有填充都误判成"需渲染"。
    * ``type`` 只有 'f' / 's' / 'fs' 三种，全是正常绘制，
      没有一个代表渐变或阴影。
    """
    print("\n需渲染判据（只能用 shading）：")
    from core.structure import _is_unresolved

    # 普通填充：不该被判为需渲染
    check("普通填充(f) -> 不需渲染",
          _is_unresolved({"type": "f", "fill": (0, 0, 0),
                          "fill_opacity": 1.0}), False)
    check("普通描边(s) -> 不需渲染",
          _is_unresolved({"type": "s", "color": (0, 0, 0),
                          "stroke_opacity": 1.0}), False)
    check("填充+描边(fs) -> 不需渲染",
          _is_unresolved({"type": "fs", "fill": (1, 0, 0),
                          "color": (0, 0, 0)}), False)
    # 关键回归：带 fill_opacity=1.0 的不能误判
    check("fill_opacity=1.0 不触发",
          _is_unresolved({"type": "f", "fill": (0.5, 0.5, 0.5),
                          "fill_opacity": 1.0}), False)

    # 渐变：该判为需渲染
    check("渐变(shading) -> 需渲染",
          _is_unresolved({"type": "f", "fill": (1.0, 1.0, 1.0),
                          "shading": {"a": 1}}), True)

    # 既无填充也无描边：不画东西
    check("无填充无描边 -> 不需渲染",
          _is_unresolved({"type": "s", "fill": None, "color": None}), False)


if __name__ == "__main__":
    test_color_chroma()
    test_merge_rects()
    test_region_scale()
    test_degenerate_line_bbox()
    test_unresolved_only_shading()

    print("\n================ 结果 ================")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        raise SystemExit(1)
    print("全部通过。")
