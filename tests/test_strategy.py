"""识别策略的端到端验证。

验证的是**策略本身的性质**，而不是某个具体文档的结果 ——
所以换成任何 PDF 都成立，不需要维护基线：

1. **省渲染**：无图形的页与彩色矢量页零渲染，只有位图页才渲染；
2. **结论自洽**：判定依据与页面结构一致（不会有"标为纯文字却含矢量"）；
3. **回落生效**：位图内容为灰度时判黑白、为彩色时判彩色；
4. **区域统计**：色度统计的分母是**图形区域**而非整页 ——
   这正是小面积彩色不再被大片留白稀释的原因；
5. **色带颜色**：卡片色带能区分不同的判定依据。

**需要自备一份 PDF**（本项目不附带测试文件）。用法::

    python tests/test_strategy.py --pdf D:\\some\\file.pdf

未提供时打印提示并跳过（退出码 0）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pymupdf  # noqa: E402

from core import (  # noqa: E402
    DEFAULT_COLOR_RATIO,
    DEFAULT_COLOR_THRESHOLD,
    DEFAULT_DPI,
    analyze_page_color,
    detect_page,
    open_input_pdf,
    scan_regions,
)
from tests.fixture import hint, resolve_pdf, set_pdf  # noqa: E402

FAILURES: list[str] = []

#: 已知的判定依据集合
KNOWN_REASONS = {"纯文字", "黑白矢量", "空白页", "彩色矢量",
                 "位图彩色", "位图灰度", "渲染失败"}


def check(label: str, got, expect) -> None:
    ok = got == expect
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f" != {expect!r}"))
    if not ok:
        FAILURES.append(f"{label}: {got!r} != {expect!r}")


def check_true(label: str, cond: bool, extra: str = "") -> None:
    check(label + (f" ({extra})" if extra else ""), bool(cond), True)


def detect_all(pdf: Path):
    doc = open_input_pdf(pdf)
    out = []
    try:
        for i in range(doc.page_count):
            info, struct = detect_page(
                doc[i], dpi=DEFAULT_DPI,
                color_threshold=DEFAULT_COLOR_THRESHOLD,
                color_ratio_threshold=DEFAULT_COLOR_RATIO,
            )
            info.index = i
            info.number = i + 1
            info.sheet = i // 2 + 1
            out.append((info, struct))
    finally:
        doc.close()
    return out


def test_reason_known(rows) -> None:
    """0) 判定依据必须落在已知集合内（防止改名后测试失同步）。"""
    print("\n0) 判定依据合法性：")
    unknown = {info.reason for info, _s in rows} - KNOWN_REASONS
    check("依据均在已知集合内", unknown, set())
    check("所有页都有结构描述",
          [info.number for info, _s in rows if not info.structure], [])
    kinds = {info.reason for info, _s in rows}
    print(f"     出现 {len(kinds)} 种依据：{sorted(kinds)}")


def test_render_economy(rows) -> None:
    """1) 省渲染：无图形页与彩色矢量页必须零渲染。"""
    print("\n1) 渲染经济性：")
    n = len(rows)
    rendered = [info for info, _s in rows if info.rendered]
    no_render_reasons = ("纯文字", "空白页", "黑白矢量", "彩色矢量")
    no_render = [info for info, _s in rows if info.reason in no_render_reasons]
    raster = [info for info, _s in rows
              if info.reason in ("位图彩色", "位图灰度")]

    print(f"     总页数 {n} · 渲染 {len(rendered)} 页 · 不渲染 {len(no_render)} 页")
    check_true("渲染页数少于总页数", len(rendered) < n, f"{len(rendered)}/{n}")

    bad = [info.number for info in no_render if info.rendered]
    check("无图形页与彩色矢量页零渲染", bad, [])

    bad2 = [info.number for info in raster if not info.rendered]
    check("位图页都渲染了", bad2, [])

    check("渲染集合 == 位图页集合",
          sorted(info.number for info in rendered),
          sorted(info.number for info in raster))


def test_reason_consistency(rows) -> None:
    """2) 判定依据与页面结构自洽。"""
    print("\n2) 判定依据自洽性：")
    local: list[str] = []
    for info, struct in rows:
        r = info.reason
        if r == "彩色矢量":
            if not info.is_color:
                local.append(f"p{info.number}: 彩色矢量却非彩色")
            if not struct.has_colored_vector:
                local.append(f"p{info.number}: 标为彩色矢量但结构无彩色矢量")
        elif r == "黑白矢量":
            if info.is_color:
                local.append(f"p{info.number}: 黑白矢量却判彩色")
            if not struct.has_vector:
                local.append(f"p{info.number}: 标为黑白矢量但结构无矢量")
            if struct.has_raster:
                local.append(f"p{info.number}: 标为黑白矢量但含位图")
        elif r == "纯文字":
            if struct.has_vector or struct.has_raster:
                local.append(f"p{info.number}: 标为纯文字但含图形")
        elif r == "空白页":
            if struct.has_text or struct.has_graphic:
                local.append(f"p{info.number}: 标为空白页但结构非空")
        elif r in ("位图彩色", "位图灰度"):
            if not struct.has_raster and not struct.has_unknown_vector:
                local.append(f"p{info.number}: 标为位图但结构无位图")
            if (r == "位图彩色") != info.is_color:
                local.append(f"p{info.number}: {r} 与结论不一致")
    check("全部页的依据与结论自洽", local, [])
    FAILURES.extend(local)


def test_speed(rows, elapsed: float) -> None:
    """3) 速度：不渲染的页越多越快，这里只做一个宽松的上限护栏。"""
    print("\n3) 检测速度：")
    per_page = elapsed / max(1, len(rows))
    print(f"     {len(rows)} 页耗时 {elapsed:.3f}s（{per_page * 1000:.1f} ms/页）")
    check_true("平均每页 < 500ms", per_page < 0.5, f"{per_page * 1000:.1f} ms")


def test_region_vs_wholepage(pdf: Path, rows) -> None:
    """4) 准确度：色度统计的分母是图形区域，不是整页。

    这是"小面积彩色不再被留白稀释"的机制本身。对同一张图，
    区域统计的分母必然 **<=** 整页统计的分母。
    """
    print("\n4) 区域统计 vs 整页统计（分母差异）：")
    doc = pymupdf.open(str(pdf))
    checked = 0
    try:
        for info, struct in rows:
            if info.reason not in ("位图彩色", "位图灰度"):
                continue
            page = doc[info.index]
            pm = page.get_pixmap(dpi=DEFAULT_DPI, colorspace=pymupdf.csRGB,
                                 alpha=False)
            _r, cpx, tpx = analyze_page_color(pm, DEFAULT_COLOR_THRESHOLD)
            whole = cpx / tpx if tpx else 0.0

            scan = scan_regions(page, struct.image_rects, dpi=DEFAULT_DPI,
                                color_threshold=DEFAULT_COLOR_THRESHOLD)
            print(f"     p{info.number}: 整页 {whole:.6f} / 区域 "
                  f"{scan.color_ratio:.6f}  像素 {scan.total_pixels} vs {tpx}")
            check_true(f"  p{info.number} 区域像素 <= 整页",
                       scan.total_pixels <= tpx)
            check_true(f"  p{info.number} 区域占比 >= 整页（分母更小）",
                       scan.color_ratio >= whole - 1e-9)
            checked += 1
            if checked >= 6:
                break
    finally:
        doc.close()
    if checked == 0:
        print("     （跳过：本 PDF 没有需要渲染的位图页）")


def test_downgrade(rows) -> None:
    """5) 回落路径：位图内容为灰度时判黑白（而不是一律判彩色）。"""
    print("\n5) 灰度回落路径：")
    gray = [info for info, _s in rows if info.reason == "位图灰度"]
    if not gray:
        print("     （跳过：本 PDF 没有判定为灰度的位图页）")
        return
    for info in gray[:5]:
        check_true(f"  p{info.number} 确已渲染", info.rendered)
        check_true(f"  p{info.number} 结论为黑白", not info.is_color)
        check_true(f"  p{info.number} 占比低于阈值",
                   info.color_ratio < DEFAULT_COLOR_RATIO)


def test_strip_colors(rows) -> None:
    """6) 色带颜色能区分判定依据（复核时的视觉线索）。"""
    print("\n6) 卡片色带颜色：")
    from ui.page_delegate import strip_color

    seen: dict[str, set[str]] = {}
    for info, _s in rows:
        seen.setdefault(info.reason, set()).add(strip_color(info).name())
    for reason, colors in sorted(seen.items()):
        print(f"     {reason}: {sorted(colors)}")

    # 彩色矢量与位图彩色必须不同色，否则用户无法区分"确定"与"存疑"
    v, r = seen.get("彩色矢量", set()), seen.get("位图彩色", set())
    if v and r:
        check_true("彩色矢量与位图彩色色带不同", v != r,
                   f"{sorted(v)} vs {sorted(r)}")
    # 纯文字与黑白矢量应不同（前者真的只有字，后者有线条图）
    t, g = seen.get("纯文字", set()), seen.get("黑白矢量", set())
    if t and g:
        check_true("纯文字与黑白矢量色带不同", t != g,
                   f"{sorted(t)} vs {sorted(g)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=None,
                    help="测试用 PDF 路径（默认找项目根目录的 test.pdf）")
    args = ap.parse_args()

    set_pdf(args.pdf)
    pdf = resolve_pdf()
    if pdf is None:
        print(hint())
        return 0

    print(f"=== 识别策略验证（{pdf.name}）===")
    t0 = time.time()
    rows = detect_all(pdf)
    elapsed = time.time() - t0

    test_reason_known(rows)
    test_render_economy(rows)
    test_reason_consistency(rows)
    test_speed(rows, elapsed)
    test_region_vs_wholepage(pdf, rows)
    test_downgrade(rows)
    test_strip_colors(rows)

    print("\n================ 结果 ================")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("策略验证全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
