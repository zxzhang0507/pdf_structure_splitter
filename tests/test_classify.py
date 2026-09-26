"""分类逻辑与判定规则单元测试（不依赖真实 PDF）。

运行：
    python tests/test_classify.py
或：
    python -m pytest tests/test_classify.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import (  # noqa: E402
    BW,
    COLOR,
    REASON_EMPTY,
    REASON_GRAY_VECTOR,
    REASON_RASTER,
    REASON_RASTER_GRAY,
    REASON_RENDER_FAIL,
    REASON_TEXT_ONLY,
    REASON_VECTOR,
    PageStructure,
)
from core.split_pdf import (  # noqa: E402
    build_page_infos,
    classify_pages,
    decide_color,
    enforce_duplex,
)

CASES = [
    ("Test 1", [BW, BW], [[1, 2]], [BW]),
    ("Test 2", [BW, COLOR], [[1, 2]], [COLOR]),
    ("Test 3", [COLOR, BW], [[1, 2]], [COLOR]),
    ("Test 4", [BW, BW, COLOR, BW], [[1, 2], [3, 4]], [BW, COLOR]),
    ("Test 5", [BW, BW, COLOR], [[1, 2], [3]], [BW, COLOR]),
]


def run(case_name, page_colors, expect_sheets, expect_outputs):
    flags = [c == COLOR for c in page_colors]
    ratios = [0.02 if f else 0.00001 for f in flags]
    infos = build_page_infos(flags, ratios, [0] * len(flags), [1] * len(flags))
    infos, sheets = classify_pages(infos)

    got_sheets = [s.pages for s in sheets]
    got_outputs = [s.output for s in sheets]
    assert got_sheets == [[i - 1 for i in g] for g in expect_sheets], (
        f"{case_name}: 配对错误 {got_sheets}"
    )
    assert got_outputs == expect_outputs, f"{case_name}: 分类错误 {got_outputs}"

    # 同一张纸的所有页面必须归属同一个输出
    for sheet in sheets:
        outs = {infos[i].output for i in sheet.pages}
        assert outs == {sheet.output}, f"{case_name}: 纸张 {sheet.number} 内页面归属不一致"

    print(f"{case_name} OK  纸张={got_sheets} 归属={got_outputs}")


def run_enforce_duplex():
    """``enforce_duplex`` 是 GUI 拖拽的兜底，必须保证同纸两页归属一致。

    配对规则：index (0,1) (2,3) (4,5) ... 即 PDF 第 1-2 页、第 3-4 页。
    """
    cases = [
        # page31(idx30)=BW, page32(idx31)=COLOR -> 同一张纸 -> 都 COLOR
        ({30: BW, 31: COLOR}, {30: COLOR, 31: COLOR}, "一黑白一彩色 -> 整张升级"),
        # page33(idx32)=BW, page34(idx33)=BW -> 保持
        ({32: BW, 33: BW}, {32: BW, 33: BW}, "都黑白 -> 保持"),
        # 奇数总页数末尾的单面纸
        ({6: BW}, {6: BW}, "单面纸 黑白"),
        ({6: COLOR}, {6: COLOR}, "单面纸 彩色"),
        # 用户把同纸两页拖成不同归属 -> 被纠正
        ({30: COLOR, 31: BW}, {30: COLOR, 31: COLOR}, "拖反 -> 纠正"),
        # 稀疏 keys（调用方只传了部分页）
        ({30: BW, 31: COLOR, 40: BW}, {30: COLOR, 31: COLOR, 40: BW}, "稀疏 keys"),
        ({}, {}, "空字典"),
    ]
    for src, expect, label in cases:
        got = enforce_duplex(src)
        assert got == expect, f"enforce_duplex {label}: {got} != {expect}"
        print(f"enforce_duplex OK  {label}")

    original = {0: BW, 1: COLOR}
    enforce_duplex(original)
    assert original == {0: BW, 1: COLOR}, "enforce_duplex 不应修改入参"
    print("enforce_duplex OK  不修改入参")


# ----------------------------------------------------------------------
# 判定规则（新策略的核心）
# ----------------------------------------------------------------------

def _struct(*, text=0, images=(), vectors=0, colored=0, unknown=0, w=595.0, h=842.0):
    """构造一个 PageStructure，便于逐条验证 :func:`decide_color`。"""
    s = PageStructure(page_w=w, page_h=h)
    s.text_chars = text
    s.image_rects = list(images)
    s.vector_count = vectors
    s.colored_vector_count = colored
    s.unknown_vector_count = unknown
    return s


def run_decide_rules():
    """四类判断的优先级与结果。

    这是新策略的规则表，任何一条被改动都应在这里立刻反映出来。
    """
    print("\n判定规则（结构优先）：")
    cases = [
        # (说明, struct, scan_ratio, 期望 is_color, 期望 reason)
        ("纯文字页（无矢量）-> 黑白，且不扫描",
         _struct(text=1200), None, False, REASON_TEXT_ONLY),
        ("空白页 -> 黑白",
         _struct(text=0), None, False, REASON_EMPTY),
        ("彩色矢量 -> 彩色（无需渲染）",
         _struct(text=800, vectors=40, colored=3), None, True, REASON_VECTOR),
        ("灰色矢量 -> 黑白（无位图，不渲染）",
         _struct(text=800, vectors=40), None, False, REASON_GRAY_VECTOR),
        ("位图检出彩色 -> 彩色",
         _struct(text=200, images=[(10, 10, 300, 300)]), 0.02, True, REASON_RASTER),
        ("位图检出灰度 -> 回落黑白",
         _struct(text=200, images=[(10, 10, 300, 300)]), 0.0, False, REASON_RASTER_GRAY),
        ("位图接近阈值但未达 -> 黑白",
         _struct(text=200, images=[(10, 10, 300, 300)]), 0.0009,
         False, REASON_RASTER_GRAY),
        ("位图刚好达阈值 -> 彩色",
         _struct(text=200, images=[(10, 10, 300, 300)]), 0.0010, True, REASON_RASTER),
        ("彩色矢量优先于位图（不扫描）",
         _struct(text=200, images=[(10, 10, 300, 300)], vectors=9, colored=1),
         None, True, REASON_VECTOR),
        ("渐变（颜色未知）-> 需要扫描",
         _struct(text=100, vectors=5, unknown=1), 0.02, True, REASON_RASTER),
        ("渐变扫描为灰度 -> 黑白",
         _struct(text=100, vectors=5, unknown=1), 0.0, False, REASON_RASTER_GRAY),
        ("扫描失败 -> 按黑白",
         _struct(text=200, images=[(10, 10, 300, 300)]), None,
         False, REASON_RENDER_FAIL),
    ]
    for label, struct, ratio, exp_color, exp_reason in cases:
        is_color, reason = decide_color(
            struct, ratio,
            color_ratio_threshold=0.001,
            scan_failed=(exp_reason == REASON_RENDER_FAIL),
        )
        assert is_color == exp_color, f"{label}: is_color {is_color} != {exp_color}"
        assert reason == exp_reason, f"{label}: reason {reason} != {exp_reason}"
        print(f"  判定 OK  {label} -> {'COLOR' if is_color else 'BW'} / {reason}")

    # needs_region_scan：决定"要不要渲染"的关键谓词
    print("\n是否需要渲染（needs_region_scan）：")
    scan_cases = [
        ("纯文字页不渲染", _struct(text=1200), False),
        ("彩色矢量页不渲染", _struct(text=800, vectors=40, colored=3), False),
        ("灰色矢量页不渲染", _struct(text=800, vectors=40), False),
        ("位图页需要渲染", _struct(text=200, images=[(1, 1, 9, 9)]), True),
        ("渐变页需要渲染", _struct(text=100, vectors=5, unknown=1), True),
        ("空白页不渲染", _struct(text=0), False),
    ]
    for label, struct, expect in scan_cases:
        got = struct.needs_region_scan()
        assert got == expect, f"{label}: {got} != {expect}"
        print(f"  渲染判定 OK  {label}: {got}")


def run_gray_vector_label():
    """带灰色矢量的页必须与真正的纯文字页区分开。

    **实测动机**：一份论文里几乎每页都带 1 条页眉横线。若把这类页
    一律标成"纯文字"，报告的 structure/reason 会与实际内容不符 ——
    用户复盘时会以为那页真的只有字，而实际上它还有线条图或表格框。
    两者都不渲染、都判黑白，但**页面内容不同**，报告必须如实反映。
    """
    print("\n纯文字 vs 黑白矢量（依据不能混淆）：")
    text_only = _struct(text=1200)
    gray_vec = _struct(text=1200, vectors=1)

    for label, struct, expect, expect_kind in (
        ("无矢量 -> 纯文字", text_only, REASON_TEXT_ONLY, "文字"),
        ("1 条矢量 -> 黑白矢量", gray_vec, REASON_GRAY_VECTOR, None),
    ):
        is_color, reason = decide_color(struct, None, color_ratio_threshold=0.001)
        assert is_color is False, f"{label}: 不该是彩色"
        assert reason == expect, f"{label}: {reason} != {expect}"
        print(f"  OK  {label} -> {reason!r}")

    assert text_only.kind == "文字", text_only.kind
    assert "矢量" in gray_vec.kind, gray_vec.kind
    print(f"  区分正确：纯文字 kind={text_only.kind!r} / "
          f"带矢量 kind={gray_vec.kind!r}")

    # 两者都不渲染（判定依据不同，但"零渲染"这一性质相同）
    assert not text_only.needs_region_scan()
    assert not gray_vec.needs_region_scan()
    print("  两者都不需要渲染（灰色矢量颜色已从 PDF 读出）")


def run_structure_helpers():
    """PageStructure 的派生属性（kind / has_* / has_graphic）。"""
    print("\n结构属性：")
    s = _struct(text=500, images=[(1, 1, 5, 5)], vectors=10, colored=2)
    assert s.has_text and s.has_raster and s.has_vector
    assert s.has_colored_vector and s.has_graphic
    assert s.raster_count == 1
    print(f"  混合页 kind = {s.kind!r}")
    assert "位图" in s.kind and "矢量" in s.kind and "彩色" in s.kind

    # 纯文字页：has_graphic 为 False 是"零渲染"的前提
    t = _struct(text=900)
    assert t.has_text and not t.has_graphic and not t.needs_region_scan()
    assert t.kind == "文字", t.kind
    print(f"  纯文字页 kind = {t.kind!r}  has_graphic={t.has_graphic}")

    e = _struct(text=0)
    assert e.kind == "空白页", e.kind
    print(f"  空白页 kind = {e.kind!r}")

    # 渐变页要在 kind 里体现出来
    g = _struct(text=100, vectors=3, unknown=2)
    assert "渐变" in g.kind, g.kind
    print(f"  渐变页 kind = {g.kind!r}")


def run_image_rect_guard():
    """位图区域必须被记录 —— 没有它就无法只渲染图形区域。

    这是新策略"位图页只渲染图片区域"的前提：若 ``image_rects`` 为空，
    位图页会被误判为纯文字页而直接判黑白。
    """
    print("\n位图区域记录（不能为空）：")
    s = _struct(text=100, images=[(10, 10, 300, 300), (400, 10, 600, 200)])
    assert s.has_raster, "有位图时 has_raster 必须为 True"
    assert s.raster_count == 2
    assert s.needs_region_scan(), "有位图就必须触发区域扫描"
    assert s.has_graphic, "有位图就不能被当成纯文字页"
    print(f"  {s.raster_count} 张位图 -> has_graphic=True needs_region_scan=True")

    # 面积占比（报告用）
    s2 = _struct(text=0, images=[(0, 0, 100, 100)], w=200, h=200)
    assert abs(s2.graphic_area_ratio - 0.25) < 1e-9, s2.graphic_area_ratio
    print(f"  面积占比 = {s2.graphic_area_ratio:.4f}")


if __name__ == "__main__":
    for name, colors, exp_sheets, exp_out in CASES:
        run(name, colors, exp_sheets, exp_out)
    run_enforce_duplex()
    run_decide_rules()
    run_gray_vector_label()
    run_structure_helpers()
    run_image_rect_guard()
    print("\n全部通过。")
