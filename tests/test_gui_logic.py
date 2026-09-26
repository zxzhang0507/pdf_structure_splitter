"""GUI 逻辑单元测试：滑块换算、拖拽语义、导出路径、资源定位。

不依赖真实 PDF，也不显示窗口 —— 可在无显示器环境跑。

运行::

    python tests/test_gui_logic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import (  # noqa: E402
    BW,
    COLOR,
    PageInfo,
    PdfSplitterError,
    build_page_infos,
    resolve_output_paths,
)
from ui.param_panel import RATIO_SCALE  # noqa: E402

#: 本文件会在部分测试里构造 QPrinter（如彩色/灰度模式）。
#: **QPrinter 必须在有 QApplication 时实例化**，否则原生崩溃
#: （退出码 0xC0000409，try/except 抓不到）。因此这里先建一个：
#: 无显示器环境下用 offscreen 平台即可（调用方通常已设 QT_QPA_PLATFORM）。
_APP = QApplication.instance() or QApplication(sys.argv[:1])

FAILURES: list[str] = []


def check(label: str, got, expect) -> None:
    ok = got == expect
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f" != {expect!r}"))
    if not ok:
        FAILURES.append(f"{label}: {got!r} != {expect!r}")


# ----------------------------------------------------------------------
# 1) 滑块浮点换算（color_ratio 是浮点，QSlider 只支持整数）
# ----------------------------------------------------------------------

def test_ratio_roundtrip() -> None:
    print("滑块浮点换算：")
    check("RATIO_SCALE", RATIO_SCALE, 10_000)

    # 设计文档明确要求：int(0.001 * 10000) == 10
    check("int(0.001*10000)", int(0.001 * RATIO_SCALE), 10)

    # 往返不丢精度：float -> int -> float 必须回到原值
    for value in (0.0000, 0.0001, 0.0010, 0.0055, 0.0200):
        stored = int(round(value * RATIO_SCALE))
        restored = stored / RATIO_SCALE
        check(f"  {value:.4f} 往返", round(restored, 4), round(value, 4))

    # 滑块范围：0 .. 0.0200
    check("范围上限 0.0200 -> 整数", int(0.0200 * RATIO_SCALE), 200)
    check("范围下限 0.0000 -> 整数", int(0.0000 * RATIO_SCALE), 0)

    # 显示格式化：4 位小数
    check("显示格式", f"{10 / RATIO_SCALE:.4f}", "0.0010")


# ----------------------------------------------------------------------
# 2) 拖拽语义：一行 = 一张纸，同纸两页永远同归属
# ----------------------------------------------------------------------

def test_sheet_grouping() -> None:
    """模型把页按纸张分组，组内各页归属必须一致（双面规则的结构性保证）。"""
    print("\n纸张分组与双面规则：")
    flags = [False, True, False, False, True, True]     # P2 彩色 -> 第1张纸升级
    ratios = [0.00001, 0.02, 0.00001, 0.00001, 0.02, 0.02]
    infos = build_page_infos(flags, ratios, [0] * 6, [1] * 6)

    # 模拟模型的分组逻辑（与 PageListModel._group_by_sheet 一致）
    groups: dict[int, list[PageInfo]] = {}
    for info in infos:
        groups.setdefault(info.sheet, []).append(info)
    sheets = [groups[k] for k in sorted(groups)]

    check("纸张数", len(sheets), 3)
    check("第1张纸含页数", len(sheets[0]), 2)
    check("第1张纸归属（任一面彩色则整张彩色）",
          COLOR if any(p.is_color for p in sheets[0]) else BW, COLOR)
    check("第2张纸归属", COLOR if any(p.is_color for p in sheets[1]) else BW, BW)

    # 每张纸内部，所有页的 index 必须连续（这就是配对规则）
    for i, group in enumerate(sheets):
        idxs = [p.index for p in group]
        check(f"  第{i + 1}张纸 index 连续", idxs, list(range(idxs[0], idxs[0] + len(idxs))))


def test_odd_last_page() -> None:
    """奇数总页数：最后一页独占一张单面纸。"""
    print("\n奇数页处理：")
    flags = [False, False, False]
    infos = build_page_infos(flags, [0.0] * 3, [0] * 3, [1] * 3)
    groups: dict[int, list[PageInfo]] = {}
    for info in infos:
        groups.setdefault(info.sheet, []).append(info)
    sheets = [groups[k] for k in sorted(groups)]
    check("纸张数", len(sheets), 2)
    check("末张纸页数", len(sheets[-1]), 1)
    last_side = COLOR if any(p.is_color for p in sheets[-1]) else BW
    check("末张纸归属", last_side, BW)


def test_export_semantic() -> None:
    """导出映射：同纸两页必进同一个输出文件（不会有页落在两个文件里）。"""
    print("\n导出归属映射：")
    flags = [False, True, False, False]
    infos = build_page_infos(flags, [0.00001, 0.02, 0.0, 0.0], [0] * 4, [1] * 4)
    from core import classify_pages, enforce_duplex

    infos, sheets = classify_pages(infos)
    side = enforce_duplex({p.index: p.output for p in infos})

    color_idx = sorted(i for i, s in side.items() if s == COLOR)
    bw_idx = sorted(i for i, s in side.items() if s == BW)

    check("彩色 index 已升序", color_idx, sorted(color_idx))
    check("黑白 index 已升序", bw_idx, sorted(bw_idx))
    check("两集合无交集", set(color_idx) & set(bw_idx), set())
    check("并集为全部页", sorted(color_idx + bw_idx), [0, 1, 2, 3])

    # 第 1 张纸（index 0,1）任一面彩色 -> 两页都在彩色件里
    check("  index 0 在彩色件", 0 in color_idx, True)
    check("  index 1 在彩色件", 1 in color_idx, True)

    # 关键：同一张纸的两页不会一个在彩色件、一个在黑白件
    for i in range(0, len(side), 2):
        pair = {side.get(i), side.get(i + 1)} - {None}
        check(f"  第{i // 2 + 1}张纸两页同归属", len(pair), 1)


# ----------------------------------------------------------------------
# 3) 导出路径解析与覆盖判定
# ----------------------------------------------------------------------

def test_output_paths(tmp: Path) -> None:
    print("\n导出路径解析：")
    pdf = tmp / "sample.pdf"

    color, bw, report = resolve_output_paths(pdf, tmp, "pre", force=True)
    check("彩色件名", color.name, "pre_color.pdf")
    check("黑白件名", bw.name, "pre_bw.pdf")
    check("报告名", report.name, "pre_report.csv")

    # 前缀为 None 时沿用输入文件名
    color2, _, _ = resolve_output_paths(pdf, tmp, None, force=True)
    check("默认前缀=输入 stem", color2.name, "sample_color.pdf")

    # 不带 --force 且文件已存在 -> 报错（GUI 里对应弹覆盖确认）
    color.write_bytes(b"%PDF-1.4\n")
    try:
        resolve_output_paths(pdf, tmp, "pre", force=False)
    except PdfSplitterError as exc:
        check("已存在时报错", "已存在" in str(exc), True)
    else:
        FAILURES.append("已存在时未报错")
        print("  [FAIL] 已存在时未报错")

    # force=True 时放行
    try:
        resolve_output_paths(pdf, tmp, "pre", force=True)
        check("force=True 放行", True, True)
    except PdfSplitterError as exc:
        check("force=True 放行", False, True)
        print("       ", exc)
    color.unlink()


# ----------------------------------------------------------------------
# 4) 资源定位（打包态 / 开发态）
# ----------------------------------------------------------------------

def test_resource_path() -> None:
    print("\n资源定位：")
    from ui.main_window import _resource_path, load_stylesheet

    qss = load_stylesheet()
    check("样式表非空", bool(qss), True)
    check("样式表含主色 #2563EB", "#2563EB" in qss, True)
    check("资源路径为绝对路径", Path(_resource_path("ui", "style.qss")).is_absolute(), True)


# ----------------------------------------------------------------------
# 5) 缩放换算与拆分/合并语义
# ----------------------------------------------------------------------

def test_zoom_and_split_logic() -> None:
    print("\n缩略图缩放换算：")
    from ui.page_delegate import (MAX_ZOOM, MIN_ZOOM, PageDelegate)

    d = PageDelegate()
    base_w, base_h = d.page_w, d.page_h
    check("初始 zoom", d.zoom, 1.0)

    d.set_zoom(2.0)
    check("放大后宽高加倍", (d.page_w, d.page_h), (base_w * 2, base_h * 2))

    d.set_zoom(999)
    check("上限被夹住", d.zoom, MAX_ZOOM)
    d.set_zoom(0.001)
    check("下限被夹住", d.zoom, MIN_ZOOM)

    # zoom_by 的返回值表示「是否真的变了」，用于界面判断要不要重排
    d.set_zoom(MAX_ZOOM)
    check("到顶后 zoom_by 返回 False", d.zoom_by(1.15), False)
    d.set_zoom(1.0)
    check("正常时 zoom_by 返回 True", d.zoom_by(1.15), True)

    d.set_zoom(1.0)
    card = d.card_size
    check("卡片宽 > 两页宽之和", card.width() > base_w * 2, True)

    print("\n拆分 / 合并语义（非严格双面模式）：")
    from ui.thumbnail_model import PageListModel

    flags = [False, True, False, False]
    infos = build_page_infos(flags, [0.0001, 0.02, 0.0, 0.0], [0] * 4, [1] * 4)
    model = PageListModel()

    model.set_pages(infos)
    check("初始行数 = 纸张数", model.rowCount(), 2)

    # 严格模式下不允许拆分
    model.set_strict_duplex(True)
    check("严格模式 can_split=False", model.can_split(0), False)
    check("严格模式 split_slots 无效", model.split_slots([0]), 0)

    # 非严格模式允许拆分
    model.set_strict_duplex(False)
    check("非严格 can_split(0)=True", model.can_split(0), True)
    check("单面纸不可拆（第2行有2页，仍可拆）", model.can_split(1), True)

    model.split_slots([0])
    check("拆分后行数 +1", model.rowCount(), 3)
    check("拆出的行各含1页", len(model.slot_at(0).pages), 1)
    check("拆出的行标记 split", model.slot_at(0).split, True)

    # 拆开后两页可以分别搬走 —— 这正是「不严格」的意义
    model.move_sheets([0], COLOR)
    model.move_sheets([1], BW)
    sm = model.final_side_map()
    idx_a = model.slot_at(0).pages[0].index
    idx_b = model.slot_at(1).pages[0].index
    check("非严格模式允许同纸两页分属不同区",
          {sm[idx_a], sm[idx_b]}, {COLOR, BW})

    # 严格模式的 final_side_map 必须把同纸两页归一
    model.set_strict_duplex(True)
    strict_sm = model.final_side_map()
    check("严格模式同纸两页归属一致",
          len({strict_sm[idx_a], strict_sm[idx_b]}), 1)

    # 合并回一张纸
    model.set_strict_duplex(False)
    check("can_merge 同纸两行", model.can_merge([0, 1]), True)
    model.merge_slots([0, 1])
    check("合并后行数 -1", model.rowCount(), 2)
    check("合并后该行含2页", len(model.slot_at(0).pages), 2)
    check("合并后页序正确",
          [p.index for p in model.slot_at(0).pages], [0, 1])


# ----------------------------------------------------------------------
# 6) 打印任务构建
# ----------------------------------------------------------------------

def test_print_jobs() -> None:
    print("\n打印任务构建：")
    from workers.print_worker import build_print_jobs

    side = {0: COLOR, 1: COLOR, 2: BW, 3: BW, 4: COLOR}
    jobs = build_print_jobs(
        side, "彩色机", "黑白机",
        color_copies=2, bw_copies=1, color_duplex=True, bw_duplex=False,
    )
    check("任务数", len(jobs), 2)

    by_label = {j["label"]: j for j in jobs}
    check("彩色件页数", len(by_label["彩色件"]["indices"]), 3)
    check("黑白件页数", len(by_label["黑白件"]["indices"]), 2)
    check("彩色件 index 升序",
          by_label["彩色件"]["indices"], sorted(by_label["彩色件"]["indices"]))
    check("黑白件 index 升序",
          by_label["黑白件"]["indices"], sorted(by_label["黑白件"]["indices"]))
    check("彩色件打印机", by_label["彩色件"]["printer"], "彩色机")
    check("彩色件双面", by_label["彩色件"]["duplex"], True)
    check("黑白件双面", by_label["黑白件"]["duplex"], False)
    check("份数透传", by_label["彩色件"]["copies"], 2)

    # 空分区不产生任务
    jobs2 = build_print_jobs({0: COLOR, 1: COLOR}, "彩色机", "黑白机")
    check("全彩色时只 1 个任务", len(jobs2), 1)
    check("  该任务为彩色件", jobs2[0]["label"], "彩色件")

    # 没有指定打印机则跳过该任务
    jobs3 = build_print_jobs({0: COLOR, 1: BW}, "彩色机", "")
    check("黑白机为空 -> 只打彩色件", len(jobs3), 1)

    check("空映射 -> 无任务", build_print_jobs({}, "A", "B"), [])

    # 两集合必须不重叠、并集为全部页
    color_set = set(by_label["彩色件"]["indices"])
    bw_set = set(by_label["黑白件"]["indices"])
    check("两集合无交集", color_set & bw_set, set())
    check("并集覆盖全部页", sorted(color_set | bw_set), sorted(side))

    # 画质 dpi 透传到任务里（并行打印时每个 worker 各读自己的 dpi）
    jobs_dpi = build_print_jobs(side, "A", "B", dpi=300)
    check("dpi 透传到彩色件", jobs_dpi[0]["dpi"], 300)
    check("dpi 透传到黑白件", jobs_dpi[1]["dpi"], 300)

    from workers.print_worker import DEFAULT_PRINT_DPI
    check("默认打印 dpi 为 300（办公文档标准档）", DEFAULT_PRINT_DPI, 300)


# ----------------------------------------------------------------------
# 7.5) 虚拟打印机识别与输出路径（问题 4 的关键）
# ----------------------------------------------------------------------

def test_csv_option(tmp: Path) -> None:
    """CSV 报告是**可选**的，默认不输出。

    多数场景只要两个 PDF 去打印；CSV 是给需要逐页核对/留档的场景准备的，
    由界面上的「输出CSV报告」勾选框控制。
    """
    print("\nCSV 报告开关：")
    import shutil

    from core import (
        DEFAULT_DPI,
        DEFAULT_COLOR_RATIO,
        DEFAULT_COLOR_THRESHOLD,
        classify_pages,
        build_page_infos,
    )
    from workers.export_worker import ExportWorker

    # 需要一份真实 PDF；没有就跳过这一节（见 tests/fixture.py）
    from tests.fixture import SkipTest, resolve_pdf
    src = resolve_pdf()
    if src is None:
        from tests.fixture import hint
        print(hint())
        return

    # 构造最小可用的导出输入
    infos = build_page_infos([False, True, False, False],
                             [0.0, 0.02, 0.0, 0.0], [0] * 4, [1] * 4)
    infos, sheets = classify_pages(infos)
    side = {p.index: p.output for p in infos}
    meta = {"source": "x.pdf", "pages": 4, "sheets": 2,
            "dpi": DEFAULT_DPI, "color_threshold": DEFAULT_COLOR_THRESHOLD,
            "color_ratio_threshold": DEFAULT_COLOR_RATIO}

    def run(write_csv: bool, sub: str) -> list[str]:
        d = tmp / sub
        d.mkdir(exist_ok=True)
        w = ExportWorker(src, d, "t", side, infos, sheets, meta,
                         write_csv=write_csv)
        err: list[str] = []
        w.failed.connect(lambda m: err.append(m))
        w.run()
        if err:
            print("      导出失败:", err[0])
        return sorted(p.name for p in d.glob("*"))

    # 默认（不勾选）：只有两个 PDF
    files_off = run(False, "off")
    check("默认不输出 CSV", any(f.endswith(".csv") for f in files_off), False)
    check("  仍输出两个 PDF", len([f for f in files_off if f.endswith(".pdf")]), 2)
    check("  彩色件存在", "t_color.pdf" in files_off, True)
    check("  黑白件存在", "t_bw.pdf" in files_off, True)

    # 勾选后：多一个 CSV
    files_on = run(True, "on")
    check("勾选后输出 CSV", any(f.endswith(".csv") for f in files_on), True)
    check("  共 3 个文件", len(files_on), 3)

    # ExportWorker 的默认参数也应是「不输出」
    d = tmp / "default"
    d.mkdir(exist_ok=True)
    w = ExportWorker(src, d, "t", side, infos, sheets, meta)
    check("ExportWorker 默认 write_csv=False", w._write_csv, False)


def test_export_targets_csv() -> None:
    """覆盖检查的文件清单必须与「本次真正会写出的文件」一致。

    否则不勾选 CSV 时，会为一个根本不会生成的旧 CSV 弹「是否覆盖」，
    让用户困惑。
    """
    print("\n导出覆盖检查清单：")
    from ui.main_window import MainWindow

    tmp = Path(__file__).parent / "_tgt"
    tmp.mkdir(exist_ok=True)
    try:
        off = [p.name for p in MainWindow._export_targets(tmp, "x", False)]
        on = [p.name for p in MainWindow._export_targets(tmp, "x", True)]
        check("不勾选时 2 个文件", len(off), 2)
        check("  不含 CSV", any("csv" in n for n in off), False)
        check("  含两个 PDF", sum(n.endswith(".pdf") for n in off), 2)
        check("勾选时 3 个文件", len(on), 3)
        check("  含 CSV", any(n.endswith(".csv") for n in on), True)
        # 默认参数应为不勾选
        default = [p.name for p in MainWindow._export_targets(tmp, "x")]
        check("默认参数等价于不勾选", default, off)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_print_geometry() -> None:
    """打印几何：渲染像素数必须**正好等于可打印区**像素数。

    这是打印清晰度的关键。踩过的坑：

    打印机的可打印区（``pageRect``）比整页小约 3.5%（有不可打印页边距）。
    早期实现按整页尺寸渲染，再「等比缩放铺进可打印区」——
    这一步重采样让锐度只剩基准的 **13%**（实测 96.7 vs 728.3），
    而且换多高 dpi 都没用，因为瓶颈是重采样而非分辨率。

    改成「按可打印区像素数渲染 + 1:1 绘制」后锐度恢复到 **111%**。
    """
    print("\n打印几何（清晰度关键）：")
    from PySide6.QtPrintSupport import QPrinter

    # 可打印区确实小于整页 —— 这是整个问题的前提
    p = QPrinter(QPrinter.HighResolution)
    p.setOutputFormat(QPrinter.PdfFormat)
    p.setOutputFileName(str(Path(__file__).parent / "_geo_probe.pdf"))
    p.setResolution(300)
    from PySide6.QtGui import QPainter

    painter = QPainter()
    if not painter.begin(p):
        print("  （跳过：无法初始化 QPrinter）")
        return
    dev = p.pageRect(QPrinter.DevicePixel)
    pts = p.pageRect(QPrinter.Point)
    painter.end()

    check("可打印区宽 > 0", dev.width() > 0, True)
    check("可打印区宽 < A4@300dpi(2480)",
          dev.width() < 2480, True)

    # 按实现里的公式复算渲染 dpi，应接近请求值
    render_dpi = round(dev.width() / (pts.width() / 72))
    check(f"由可打印区反推的渲染 dpi ({render_dpi}) 在 300 附近",
          abs(render_dpi - 300) <= 15, True)

    # 关键断言：用该 dpi 渲染出的像素数应**正好**等于可打印区像素数
    import pymupdf as fitz

    from tests.fixture import resolve_pdf
    src = resolve_pdf()
    if src is not None:
        doc = fitz.open(str(src))
        page = doc[0]
        msx = dev.width() / page.rect.width
        msy = dev.height() / page.rect.height
        pm = page.get_pixmap(matrix=fitz.Matrix(msx, msy),
                             alpha=False, colorspace=fitz.csRGB)
        doc.close()
        check("渲染宽 == 可打印区宽", pm.width, dev.width())
        check("渲染高 == 可打印区高", pm.height, dev.height())
    else:
        print("  （跳过像素验证：无测试用 PDF）")

    probe = Path(__file__).parent / "_geo_probe.pdf"
    probe.unlink(missing_ok=True)


def test_virtual_printer() -> None:
    """虚拟打印机必须被识别出来，否则 begin() 会阻塞数秒冻住界面。

    实测数据：不预设输出路径时 Microsoft XPS Writer 阻塞 6506ms、
    Print to PDF 4901ms、Adobe PDF 4957ms；预设后均为 1ms。
    """
    print("\n虚拟打印机识别：")
    from workers.print_worker import default_output_path, is_virtual_printer

    virtual = [
        "Microsoft Print to PDF", "Microsoft XPS Document Writer",
        "Adobe PDF", "OneNote (Desktop)", "Fax", "CutePDF Writer",
    ]
    real = [
        "HP LaserJet 1020", "Canon MF642C Series", "Epson L3150 Series",
        "Brother HL-L2350DW",
    ]
    for name in virtual:
        check(f"  {name} 判为虚拟", is_virtual_printer(name), True)
    for name in real:
        check(f"  {name} 判为真实", is_virtual_printer(name), False)

    print("\n虚拟打印机默认输出路径：")
    p = default_output_path(Path("/docs/论文终稿.pdf"), "彩色件")
    check("  含原名", "论文终稿" in p.name, True)
    check("  含任务名", "彩色件" in p.name, True)
    check("  是 .pdf", p.suffix, ".pdf")
    check("  与输入同目录", p.parent, Path("/docs"))

    # 任务名里出现非法字符时不应破坏路径
    p2 = default_output_path(Path("/docs/a.pdf"), "彩色/件\\x")
    check("  非法字符被替换", "/" not in p2.name and "\\" not in p2.name, True)


# ----------------------------------------------------------------------
# 7.55) 默认检测 DPI
# ----------------------------------------------------------------------

def test_default_dpi() -> None:
    """默认检测 DPI 为 75。

    它只作用于**位图区域**的渲染（纯文字页与矢量页完全不渲染），
    因此重要性低于"整页渲染"式的做法。

    取 75 而非更高，是因为**高 DPI 会把位图内部的压缩伪影暴露出来**：
    扫描件与 JPEG 位图常带几度色偏（色度 16–20，紧贴阈值 15），
    DPI 越高这些像素越不会被平均掉，于是纯黑白的图被判成彩色。
    反过来 DPI 过低会让小位图区域的样本太少、可能漏掉真实的小面积彩色。
    75 是两者的平衡点。
    """
    print("\n默认检测 DPI：")
    from core import DEFAULT_DPI

    check("默认 DPI", DEFAULT_DPI, 75)
    check("在滑块范围内（50~200）", 50 <= DEFAULT_DPI <= 200, True)
    # 步长必须能整除默认值，否则方向键会从 75 跳到 80/85
    from ui.param_panel import _SliderRow  # noqa: F401

    step = 5
    check("默认值落在步长网格上", DEFAULT_DPI % step, 0)


# ----------------------------------------------------------------------
# 7.6) 中文路径（问题 5 的回归防护）
# ----------------------------------------------------------------------

def test_chinese_path(tmp: Path) -> None:
    """中文文件名/目录必须能打开。

    常见 bug：把非 ASCII 路径转成 ``bytes``。PyMuPDF 1.24+ 原生支持
    UTF-8 路径且**不接受 bytes**，转成 bytes 反而会让中文文件名打不开。
    """
    print("\n中文路径处理：")
    import shutil

    from core import open_input_pdf, render_page, analyze_page_color
    from core import DEFAULT_DPI, DEFAULT_COLOR_THRESHOLD
    from core.split_pdf import _to_pymupdf_path

    from tests.fixture import resolve_pdf
    src = resolve_pdf()
    if src is None:
        from tests.fixture import hint
        print(hint())
        return

    # 纯函数层面：必须返回 str，绝不能是 bytes
    p = _to_pymupdf_path(tmp / "中文.pdf")
    check("  返回 str 而非 bytes", isinstance(p, str), True)

    # 实际打开中文名文件
    cn = tmp / "中文文档.pdf"
    shutil.copy2(src, cn)
    doc = open_input_pdf(cn)
    check("  中文名可打开", doc.page_count > 0, True)
    # 用真实的检测入口跑一遍，确认中文路径下的结构分析与判定都正常。
    # 不能断言"整页彩色像素 > 0" —— 首页可能是纯文字页，它本来就不该有
    # 彩色像素（而且根本不会被渲染），那样断言等于把输入的页序写死。
    from core import detect_page
    info, struct = detect_page(doc[0])
    check("  中文名可完成检测", info.reason != "", True)
    check("  中文名可读出结构", struct.kind != "", True)
    doc.close()

    # 中文目录 + 中文名
    sub = tmp / "中文目录"
    sub.mkdir(exist_ok=True)
    cn2 = sub / "另一个文档.pdf"
    shutil.copy2(src, cn2)
    doc2 = open_input_pdf(cn2)
    check("  中文目录+中文名可打开", doc2.page_count > 0, True)
    doc2.close()


# ----------------------------------------------------------------------
# 7) 打印画质档位
# ----------------------------------------------------------------------

def test_print_quality_presets() -> None:
    """打印画质档位（覆盖 150~1200 dpi，默认 300）。"""
    print("\n打印画质档位：")
    from ui.print_dialog import (
        DEFAULT_QUALITY_DPI,
        DEFAULT_QUALITY_INDEX,
        QUALITY_PRESETS,
    )

    dpis = [dpi for _n, dpi, _h in QUALITY_PRESETS]
    check("档位数 >= 5", len(QUALITY_PRESETS) >= 5, True)
    check("默认档位是 300 dpi（办公标准）",
          QUALITY_PRESETS[DEFAULT_QUALITY_INDEX][1], 300)
    check("DEFAULT_QUALITY_DPI 与默认档一致", DEFAULT_QUALITY_DPI, 300)
    for want in (150, 300, 450, 600, 900, 1200):
        check(f"含 {want} dpi 档", want in dpis, True)
    check("各档 dpi 互不相同", len(set(dpis)), len(dpis))
    check("dpi 递增排列", dpis, sorted(dpis))
    check("dpi 均为合理值", all(50 <= d <= 2400 for d in dpis), True)


# ----------------------------------------------------------------------
# 7.4) 彩色 / 灰度输出模式（QPrinter.setColorMode）
# ----------------------------------------------------------------------

def test_color_mode() -> None:
    """彩色件走彩色、黑白件默认走灰度。

    QPrinter 默认是 ``ColorMode.Color``，即把 RGB 原样交给驱动。
    对黑白件显式声明灰度，可避免某些驱动按彩色渲染而浪费彩色碳粉。
    """
    print("\n彩色 / 灰度输出模式：")
    from PySide6.QtPrintSupport import QPrinter

    from ui.print_dialog import PrintDialog
    from workers.print_worker import build_print_jobs

    side = {0: COLOR, 1: COLOR, 2: BW, 3: BW}
    jobs = build_print_jobs(side, "彩色机", "黑白机")
    by = {j["label"]: j for j in jobs}
    check("彩色件 grayscale=False", by["彩色件"]["grayscale"], False)
    check("黑白件 grayscale=True（默认显式灰度）",
          by["黑白件"]["grayscale"], True)

    # 可显式覆盖
    jobs2 = build_print_jobs(
        side, "彩色机", "黑白机",
        color_grayscale=True, bw_grayscale=False,
    )
    by2 = {j["label"]: j for j in jobs2}
    check("可把彩色件改为灰度", by2["彩色件"]["grayscale"], True)
    check("可把黑白件改为彩色", by2["黑白件"]["grayscale"], False)

    # Qt 枚举本身可用
    p = QPrinter(QPrinter.HighResolution)
    p.setColorMode(QPrinter.GrayScale)
    check("QPrinter 灰度模式可设", p.colorMode(), QPrinter.GrayScale)
    p.setColorMode(QPrinter.Color)
    check("QPrinter 彩色模式可设", p.colorMode(), QPrinter.Color)


# ----------------------------------------------------------------------
# 8) 单面模式 / 双面模式切换（问题 2）
# ----------------------------------------------------------------------

def test_mode_switching() -> None:
    """单面模式：每页独立判定；双面模式：整张纸一致。"""
    print("\n检测模式切换（单面 / 双面）：")
    from ui.thumbnail_model import PageListModel

    # 第1张纸：正面黑白、反面彩色 -> 双面模式下两页都归 COLOR
    flags = [False, True, False, False, True, False]
    ratios = [0.0001, 0.02, 0.0, 0.0, 0.02, 0.0001]
    infos = build_page_infos(flags, ratios, [0] * 6, [1] * 6)
    model = PageListModel()

    model.set_pages(infos)
    check("默认双面模式", model.single_sided, False)
    check("双面模式行数 = 纸张数", model.rowCount(), 3)
    check("双面模式模式名", model.mode_label, "严格双面模式")

    sm = model.final_side_map()
    check("双面：第1张纸两页同归属", len({sm[0], sm[1]}), 1)
    check("双面：第1张纸均 COLOR", sm[0], COLOR)
    check("双面：有配对升级页", model.upgraded_count(), 2)

    # 切到单面模式
    changed = model.set_single_sided(True)
    check("切换返回 True", changed, True)
    check("单面模式标志", model.single_sided, True)
    check("单面模式模式名", model.mode_label, "单面模式")
    check("单面模式行数 = 页数", model.rowCount(), 6)

    sm2 = model.final_side_map()
    check("单面：P1 按自身判定 BW", sm2[0], BW)
    check("单面：P2 按自身判定 COLOR", sm2[1], COLOR)
    check("单面：无配对升级", model.upgraded_count(), 0)

    # 同纸两页可分属不同输出 —— 这正是单面模式的意义
    check("单面：同纸两页可分属不同区", {sm2[0], sm2[1]}, {BW, COLOR})

    # 切回双面
    check("切回返回 True", model.set_single_sided(False), True)
    check("切回后行数", model.rowCount(), 3)
    sm3 = model.final_side_map()
    check("切回后同纸归一", len({sm3[0], sm3[1]}), 1)

    # 重复设置同一模式应返回 False（避免无谓刷新）
    check("重复设置返回 False", model.set_single_sided(False), False)

    # 人工调整在切换模式时被保留。
    # 注意：双面模式下搬的是**整张纸**，所以 row0 会同时带走 P1、P2。
    model2 = PageListModel()
    model2.set_pages(infos)
    model2.move_sheets([0], BW)          # 人工把第1张纸整张改判 BW
    model2.set_single_sided(True)
    ms = model2.final_side_map()
    check("切换模式后人工调整仍生效（整张纸，P1/P2 都 BW）", ms[0], BW)
    check("  同纸另一页也保持人工值", ms[1], BW)
    check("  未经人工调整的第2张纸按自身判定（P3 BW / P4 BW）", ms[2], BW)

    # 若只调整其中一页（单面模式下才可能），切换后仍按页保留
    model3 = PageListModel()
    model3.set_pages(infos)
    model3.set_single_sided(True)
    model3.move_sheets([0], COLOR)       # 单面模式下只动 P1
    model3.set_single_sided(False)       # 切回双面：P1 的人工值会带动整张纸
    ms3b = model3.final_side_map()
    check("单面下的人工值切回双面后整张纸一致",
          len({ms3b[0], ms3b[1]}), 1)


# ----------------------------------------------------------------------
# 8.5) 拖放可接受性（问题 2 的回归防护）
# ----------------------------------------------------------------------

def test_can_drop_mime_data() -> None:
    """``canDropMimeData`` 仍应正确实现（虽然搬移已改走手工路径）。

    历史：``QAbstractItemModel`` 默认实现返回 ``False``，导致
    ``dragEnterEvent`` 拒绝拖入、鼠标拖不动。这个 bug 曾连续躲过两轮
    「修复」，因为直接调 ``dropEvent`` 会绕过 ``canDropMimeData`` 而看起来正常。

    现在拖拽已改为不依赖 Qt 拖放框架的手工实现（见
    ``ZoneView.mouseMoveEvent`` / ``mouseReleaseEvent``），
    但保留本测试以防将来又误删该方法。
    """
    print("\n拖放可接受性（canDropMimeData）：")
    from PySide6.QtCore import QMimeData, QModelIndex, Qt

    from ui.thumbnail_model import MIME_TYPE, PageListModel

    model = PageListModel()
    model.set_pages(build_page_infos(
        [False, True, False, False], [0.0, 0.02, 0, 0], [0] * 4, [1] * 4))

    good = QMimeData()
    good.setData(MIME_TYPE, b"0")
    foreign = QMimeData()
    foreign.setText("hello")

    check("本程序 MIME + MoveAction 可放下",
          model.canDropMimeData(good, Qt.MoveAction, -1, 0, QModelIndex()), True)
    check("本程序 MIME + CopyAction 不可放下（只支持移动）",
          model.canDropMimeData(good, Qt.CopyAction, -1, 0, QModelIndex()), False)
    check("外来 MIME 不可放下",
          model.canDropMimeData(foreign, Qt.MoveAction, -1, 0, QModelIndex()), False)
    check("supportedDropActions 含 MoveAction",
          bool(model.supportedDropActions() & Qt.MoveAction), True)


# ----------------------------------------------------------------------
# 8.6) 手工拖拽（不依赖 Qt 拖放框架）
# ----------------------------------------------------------------------

def test_manual_drag_geometry() -> None:
    """手工拖拽的几何判定：源区、目标区、区外。

    ``ZoneView`` 用纯鼠标事件实现拖拽（Qt 的 ``QDrag`` 在本项目的
    ``IconMode`` + ``Static`` movement + 自定义委托组合下返回
    ``IgnoreAction``，放置不生效，试了三套方案均失败）。
    因此这里测的是几何逻辑本身，不需要真实窗口管理器。
    """
    print("\n手工拖拽几何判定：")
    from PySide6.QtCore import QPoint, QRect

    check("拖拽阈值 > 0", _drag_threshold() > 0, True)

    # 判定「点是否落在某个矩形内」的辅助逻辑（与 MainWindow 同构）
    left = QRect(0, 0, 100, 100)
    right = QRect(200, 0, 100, 100)
    check("左区内点归左区", left.contains(QPoint(50, 50)), True)
    check("右区内点归右区", right.contains(QPoint(250, 50)), True)
    check("两区之间不算任何区",
          left.contains(QPoint(150, 50)) or right.contains(QPoint(150, 50)), False)
    check("区外不算任何区", left.contains(QPoint(-5, 50)), False)


def _drag_threshold() -> int:
    from ui.main_window import ZoneView

    return ZoneView.DRAG_THRESHOLD


# ----------------------------------------------------------------------
# 8.7) 帮助页面与 UI 提示
# ----------------------------------------------------------------------

def test_help_dialog() -> None:
    """帮助页面：章节完整、可构造、每节都有内容。"""
    print("\n帮助页面：")
    from ui.help_dialog import SECTIONS, HelpDialog

    check("章节数 >= 8", len(SECTIONS) >= 8, True)
    titles = [t for t, _s, _h in SECTIONS]
    for want in ("软件做什么", "使用流程", "操作速查", "检测模式",
                 "参数怎么调", "打印说明", "输出文件", "常见问题"):
        check(f"  含「{want}」章", want in titles, True)

    # 每节都要有实际内容，不能是占位
    for title, _short, html in SECTIONS:
        check(f"  「{title}」有正文", len(html.strip()) > 100, True)

    # 快捷键章节必须把 Ctrl 多选写进去（本次新增功能）
    quick = next(h for t, _s, h in SECTIONS if t == "操作速查")
    check("  速查含 Ctrl+点击多选", "Ctrl+点击" in quick, True)
    check("  速查含方向键搬移", "←" in quick and "→" in quick, True)

    # 「输出文件」章节要说明 CSV 是默认关闭的
    outsec = next(h for t, _s, h in SECTIONS if t == "输出文件")
    check("  输出章节提到 CSV 可选", "输出CSV报告" in outsec, True)
    check("  输出章节说明默认不输出", "默认不输出" in outsec, True)

    # FAQ 里要有「没有 CSV」的条目
    faq = next(h for t, _s, h in SECTIONS if t == "常见问题")
    check("  FAQ 含 CSV 缺失说明", "没有 CSV" in faq, True)

    dlg = HelpDialog()
    check("  可构造", dlg is not None, True)
    check("  目录项数 = 章节数", dlg.toc.count(), len(SECTIONS))
    check("  内容页数 = 章节数", dlg.stack.count(), len(SECTIONS))
    dlg.deleteLater()


def test_zone_hints() -> None:
    """分区标题栏必须带操作提示（用户不用翻帮助就知道怎么搬移）。"""
    print("\n分区标题栏提示：")
    from ui.main_window import ZoneWidget

    hint = ZoneWidget.HINT
    check("含「拖动」", "拖动" in hint, True)
    check("含方向键", "←" in hint and "→" in hint, True)
    check("含 Ctrl 多选", "Ctrl" in hint, True)

    dlg_hint = ZoneWidget.HINT
    check("提示不是空串", bool(dlg_hint.strip()), True)


def main() -> int:
    import tempfile

    test_ratio_roundtrip()
    test_sheet_grouping()
    test_odd_last_page()
    test_export_semantic()
    with tempfile.TemporaryDirectory() as tmp:
        test_output_paths(Path(tmp))
    test_resource_path()
    test_zoom_and_split_logic()
    test_print_jobs()
    test_print_quality_presets()
    test_export_targets_csv()
    with tempfile.TemporaryDirectory() as tmp:
        test_csv_option(Path(tmp))
    test_print_geometry()
    test_virtual_printer()
    test_default_dpi()
    test_can_drop_mime_data()
    test_manual_drag_geometry()
    test_color_mode()
    test_help_dialog()
    test_zone_hints()
    test_mode_switching()
    with tempfile.TemporaryDirectory() as tmp:
        test_chinese_path(Path(tmp))

    print("\n================ 结果 ================")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
