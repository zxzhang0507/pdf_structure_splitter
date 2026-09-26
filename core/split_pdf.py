#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按"双面打印纸"为单位，将 PDF 拆分为彩色件与黑白件。

分类单位不是单个页面，而是**实际双面打印的一张纸**：

    Sheet 1 = pages 1, 2
    Sheet 2 = pages 3, 4
    ...

一张纸只要正面或背面任意一面是彩色页，整张纸的所有页面都进入彩色 PDF；
只有两面都是黑白页，整张纸才进入黑白 PDF。

输出 PDF 通过 ``insert_pdf()`` 直接从原文件复制页面对象，
不经过渲染→位图→重新编码，因此保留原始文字层、矢量内容与页面尺寸。

--------------------------------------------------------------------------
识别策略
--------------------------------------------------------------------------

**先看页面结构，再决定要不要渲染**：

1. **页面没有任何图形元素**（纯文字页 / 空白页）
   → 直接判**黑白**，一次渲染都不做。
   实测：一份 132 页的论文里这类页占大多数，整份 PDF 的检测时间
   因此大幅下降。

2. **页面有矢量图形**（图表、曲线、色块、表格线）
   → 矢量颜色在 PDF 内容流里是**显式声明**的，
   ``get_drawings()`` 直接给出 ``fill`` / ``color``，**完全不需要渲染**。
   判据是 ``chroma = max(R,G,B) - min(R,G,B)`` 是否超过色度阈值。
   比渲染法更可靠：一条 0.5 pt 的彩色细线在低 dpi 下只有几个半透明
   像素、很容易漏检，而它的声明颜色一眼就能看出来。

3. **页面有位图图片**（照片、截图、扫描页）
   → 先**暂标为彩色**，再只渲染**图片所在的区域**（用 ``clip`` 裁剪，
   不必渲染整页），统计区域内的彩色像素占比；
   区域里几乎没有彩色（灰度照片、黑白扫描件）→ **回落为黑白**。

四类判断的优先级（见 :func:`decide_color`）::

    彩色矢量存在            -> 彩色（矢量颜色是权威结论，无需渲染）
    有图形元素（位图/渐变） -> 渲染图形区域，按彩色像素占比判定
    无图形元素              -> 黑白（纯文字页，零渲染）

这一策略有两个直接的收益：

* **快**：纯文字页不渲染；位图页只渲染图片区域而非整页
  （一张占页面 30% 的图，渲染量约为整页的 30%）。
* **准**：色度统计的分母从"整页像素"变成"图形区域像素"，
  小面积彩色图形不再被大片留白稀释。整页渲染下一个 10 pt 的彩色图标
  只占万分之几，很容易被 ``color_ratio`` 阈值滤掉。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Tuple

try:  # PyMuPDF >= 1.24 推荐的新命名
    import pymupdf as fitz
except ImportError:  # pymupdf < 1.24 只有 fitz 这个模块名
    import fitz  # type: ignore[no-redef]

try:  # 作为包成员导入（`from core import ...`、GUI 与测试走这条）
    from core.structure import (
        DEFAULT_COLOR_RATIO,
        DEFAULT_COLOR_THRESHOLD,
        DEFAULT_DPI,
        MAX_PIXELS,
        PageRenderError,
        PageStructure,
        analyze_page_color,
        analyze_page_structure,
        merge_rects,
        render_page,
        render_region,
        region_scale,
        scan_regions,
    )
except ImportError:  # 直接 `python core/split_pdf.py` 时的同目录导入
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.structure import (  # type: ignore[no-redef]
        DEFAULT_COLOR_RATIO,
        DEFAULT_COLOR_THRESHOLD,
        DEFAULT_DPI,
        MAX_PIXELS,
        PageRenderError,
        PageStructure,
        analyze_page_color,
        analyze_page_structure,
        merge_rects,
        render_page,
        render_region,
        region_scale,
        scan_regions,
    )

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BW = "BW"
COLOR = "COLOR"

#: 判定依据（写入报告，便于回溯"这页为什么被判成彩色"）
#:
#: 刻意把"纯文字"与"黑白矢量"分开：两者都不渲染、都判黑白，但**页面
#: 内容不同** —— 前者真的只有字，后者还有黑色线条图/表格框。实测
#: 一份论文里几乎每页都带 1 条页眉横线，若都标成"纯文字"，报告会
#: 与实际内容不符，用户复盘时会被误导。
REASON_TEXT_ONLY = "纯文字"          # 既无位图也无矢量
REASON_GRAY_VECTOR = "黑白矢量"      # 只有灰色矢量（含表格线/线条图）
REASON_VECTOR = "彩色矢量"           # 矢量颜色即为彩色
REASON_RASTER = "位图彩色"           # 图片区域检出彩色像素
REASON_RASTER_GRAY = "位图灰度"      # 图片区域几乎无彩色像素 -> 黑白
REASON_EMPTY = "空白页"
REASON_RENDER_FAIL = "渲染失败"      # 失败按黑白处理

#: 图形区域渲染失败时，退化为整页渲染重试
_WHOLE_PAGE_RETRY = True


class PdfSplitterError(Exception):
    """程序可预期的错误（输入无效、输出不可写等）。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class PageInfo:
    """单页检测结果。``index`` 为 0-based，``number``/``sheet`` 为 1-based。"""

    index: int
    number: int
    sheet: int
    is_color: bool
    color_ratio: float
    color_pixels: int
    total_pixels: int
    page_color: str
    output: str = ""
    #: 该页渲染失败时的错误文本；成功时为空串（GUI 用它显示故障占位卡）
    render_error: str = ""

    # ---- 新策略专属：结构信息与判定依据 ----
    #: 页面元素构成的中文描述（如 "文字 + 2 张位图 + 8 条矢量"）
    structure: str = ""
    #: 判定依据（REASON_* 之一）
    reason: str = ""
    #: 该页是否发生了渲染（纯文字页为 False —— 这是新策略快的原因）
    rendered: bool = False
    #: 参与色度统计的图形元素类型："vector" / "raster" / "none"
    scan_kind: str = "none"

    @property
    def sheet_output(self) -> str:
        return self.output

    @property
    def ok(self) -> bool:
        """该页是否成功完成检测。"""
        return not self.render_error


@dataclass
class SheetInfo:
    """一张双面打印纸（1 或 2 页）。"""

    number: int
    pages: List[int]  # 0-based index
    output: str

    @property
    def first_number(self) -> int:
        return self.pages[0] + 1

    @property
    def last_number(self) -> int:
        return self.pages[-1] + 1

    @property
    def label(self) -> str:
        if len(self.pages) == 1:
            return f"page {self.first_number}"
        return f"pages {self.first_number}-{self.last_number}"


# ---------------------------------------------------------------------------
# 判定核心
# ---------------------------------------------------------------------------


def decide_color(
    struct: PageStructure,
    scan_ratio: float | None,
    *,
    color_ratio_threshold: float = DEFAULT_COLOR_RATIO,
    scan_failed: bool = False,
) -> Tuple[bool, str]:
    """按结构 + 区域扫描结果给出该页是否为彩色。

    :param struct: 页面结构分析结果
    :param scan_ratio: 图形区域的彩色像素占比；没扫描则为 ``None``
    :param scan_failed: 区域扫描整体失败（此时 ``scan_ratio`` 不可信）
    :returns: ``(is_color, reason)``

    判定顺序与理由：

    1. **彩色矢量** —— 矢量颜色是 PDF 里显式声明的，属于权威结论，
       直接判彩色，连渲染都省了。放在最前面是因为它比像素统计更可靠：
       一条 0.5 pt 的彩色细线渲染出来只有几个半透明像素，
       但它的声明颜色是确定无疑的。
    2. **有图形元素** —— 位图内容必须在像素上才能判断（PDF 只记录
       "这是一张图"），所以看区域扫描结果。
    3. **无图形元素** —— 纯文字页直接黑白，零渲染。
    """
    if struct.has_colored_vector:
        return True, REASON_VECTOR

    if struct.needs_region_scan():
        if scan_failed or scan_ratio is None:
            # 扫描失败：按黑白处理并记录（失败信息在 PageInfo.render_error 里），
            # 即"单页失败按黑白"的约定。
            return False, REASON_RENDER_FAIL
        if scan_ratio >= color_ratio_threshold:
            return True, REASON_RASTER
        return False, REASON_RASTER_GRAY

    # 走到这里说明：没有彩色矢量，且没有需要渲染的图形（位图/渐变）。
    # 剩下的可能是"纯文字"，也可能是"文字 + 灰色矢量"（表格线、线条图）。
    if struct.has_vector:
        return False, REASON_GRAY_VECTOR
    if not struct.has_text:
        return False, REASON_EMPTY
    return False, REASON_TEXT_ONLY


def detect_page(
    page: "fitz.Page",
    *,
    dpi: int = DEFAULT_DPI,
    color_threshold: int = DEFAULT_COLOR_THRESHOLD,
    color_ratio_threshold: float = DEFAULT_COLOR_RATIO,
    allow_text_only_color: bool = False,
) -> Tuple[PageInfo, PageStructure]:
    """检测**一页**：结构分析 → 按需渲染 → 判定。

    抽出成独立函数，便于 GUI 的检测线程逐页调用、也便于单元测试。

    :param allow_text_only_color: 保留的兼容开关。默认 ``False`` ——
        纯文字页一律判黑白，**不看文字颜色**（这是明确的策略选择：
        红色标题若走了黑白打印机是可接受的，而为此把大量纯文字页
        渲染一遍不值得）。
    """
    struct = analyze_page_structure(page, color_threshold=color_threshold)

    scan: float | None = None
    scan_failed = False
    error = ""
    rendered = False
    scan_kind = "none"

    if not struct.has_colored_vector and struct.needs_region_scan():
        scan_kind = "raster" if struct.has_raster else "vector"
        rects: List[Tuple[float, float, float, float]] = list(struct.image_rects)
        rects += struct.unknown_vector_rects
        try:
            result = scan_regions(
                page, rects, dpi=dpi, color_threshold=color_threshold
            )
            rendered = True
            if result.total_pixels == 0 or result.failures:
                # 所有区域都没渲染成功：退化为整页渲染再试一次。
                # 这一步只在异常路径发生，正常页面不会走到。
                if result.total_pixels == 0 and _WHOLE_PAGE_RETRY:
                    pixmap = render_page(page, dpi)
                    _r, cpx, tpx = analyze_page_color(pixmap, color_threshold)
                    scan = cpx / tpx if tpx else 0.0
                    scan_kind = "whole_page"
                else:
                    scan_failed = True
                    error = "；".join(result.failures[:3])
            else:
                scan = result.color_ratio
        except Exception as exc:  # noqa: BLE001 - 单页失败不中断整体
            try:
                pixmap = render_page(page, dpi)
                _r, cpx, tpx = analyze_page_color(pixmap, color_threshold)
                scan = cpx / tpx if tpx else 0.0
                scan_kind = "whole_page"
                rendered = True
            except Exception as exc2:  # noqa: BLE001
                scan_failed = True
                error = f"{type(exc2).__name__}: {exc2}"
                _ = exc
    elif struct.has_colored_vector:
        scan_kind = "vector"

    if allow_text_only_color and not struct.has_graphic and struct.has_text:
        # 兼容开关（默认关闭）：把"纯文字页"当作已扫描，比值 0
        scan = scan or 0.0

    is_color, reason = decide_color(
        struct, scan,
        color_ratio_threshold=color_ratio_threshold,
        scan_failed=scan_failed,
    )

    # 报告里的像素统计：只统计真正参与判定的图形区域
    color_pixels = int(round((scan or 0.0) * _scan_area_px(struct, dpi)))

    info = PageInfo(
        index=0,                      # 由调用方填
        number=0,
        sheet=0,
        is_color=is_color,
        color_ratio=float(scan or 0.0),
        color_pixels=color_pixels,
        total_pixels=_scan_area_px(struct, dpi),
        page_color=COLOR if is_color else BW,
        render_error=error,
        structure=struct.kind,
        reason=reason,
        rendered=rendered,
        scan_kind=scan_kind,
    )
    return info, struct


def _scan_area_px(struct: PageStructure, dpi: int) -> int:
    """估算参与判定的图形区域像素数（仅用于报告的 ``total_pixels`` 列）。

    纯文字页不渲染，因此为 0 —— 报告的语义是"实际统计了多少像素"，
    而不是"整页有多少像素"。
    """
    rects = list(struct.image_rects) + list(struct.unknown_vector_rects)
    if not rects:
        return 0
    total = 0.0
    for rect in merge_rects(rects):
        # region_scale 返回的是缩放系数（dpi/72），边长各自乘以它
        scale = region_scale(rect, dpi)
        w_px = (rect[2] - rect[0]) * scale
        h_px = (rect[3] - rect[1]) * scale
        total += max(1.0, w_px * h_px)
    return int(total)


# ---------------------------------------------------------------------------
# 分类与配对
# ---------------------------------------------------------------------------


def group_duplex_pages(page_colors: Sequence[bool]) -> List[List[int]]:
    """按原始顺序把页面配对成双面纸张（0-based index）。

    奇数总页数时最后一页单独成一张单面纸。
    """
    return [
        list(range(i, min(i + 2, len(page_colors))))
        for i in range(0, len(page_colors), 2)
    ]


def classify_pages(
    page_infos: Sequence[PageInfo],
) -> Tuple[List[PageInfo], List[SheetInfo]]:
    """执行双面纸张级分类，原地写入每页的 ``output`` 字段。

    核心规则::

        if front_is_color or back_is_color:
            entire_sheet -> COLOR
        else:
            entire_sheet -> BW
    """
    sheets: List[SheetInfo] = []
    for group in group_duplex_pages([p.is_color for p in page_infos]):
        sheet_out = COLOR if any(page_infos[i].is_color for i in group) else BW
        number = group[0] // 2 + 1
        sheets.append(SheetInfo(number=number, pages=group, output=sheet_out))
        for i in group:
            page_infos[i].output = sheet_out
    return list(page_infos), sheets


def enforce_duplex(side: dict) -> dict:
    """把「单页归属」强制修正为「整张纸归属」，原样返回新字典。

    规则与 :func:`classify_pages` 一致：一张纸（index ``i`` 与 ``i+1``）
    只要任意一侧是 COLOR，两页都改为 COLOR。

    GUI 版用它做**导出前兜底**：用户在界面上手工拖拽可能把同一张纸的两页
    分到不同区（该纸被拆到两台打印机），这个函数负责在写文件前纠正回来。

    :param side: ``{page_index: 'BW' | 'COLOR'}``，page_index 为 0-based
    :returns: 修正后的新字典（不修改入参）
    """
    fixed = dict(side)
    if not fixed:
        return fixed

    # 注意：keys 可能是不连续的（调用方只传了部分页），
    # 所以纸张分组必须由**实际存在的 key** 推导，不能用 len() 当上界。
    sheet_of = {i: i // 2 for i in fixed}          # page index -> sheet index
    grouped: dict = {}
    for page_index, sheet_index in sheet_of.items():
        grouped.setdefault(sheet_index, []).append(page_index)

    for pages in grouped.values():
        if any(fixed[i] == COLOR for i in pages):
            for i in pages:
                fixed[i] = COLOR
    return fixed


def build_page_infos(
    color_flags: Iterable[bool],
    ratios: Sequence[float],
    color_pixels: Sequence[int],
    total_pixels: Sequence[int],
    *,
    structures: Sequence[str] | None = None,
    reasons: Sequence[str] | None = None,
) -> List[PageInfo]:
    """把逐页检测结果装配成 :class:`PageInfo` 列表。"""
    infos: List[PageInfo] = []
    for idx, is_color in enumerate(color_flags):
        infos.append(
            PageInfo(
                index=idx,
                number=idx + 1,
                sheet=idx // 2 + 1,
                is_color=is_color,
                color_ratio=ratios[idx],
                color_pixels=color_pixels[idx],
                total_pixels=total_pixels[idx],
                page_color=COLOR if is_color else BW,
                structure=(structures[idx] if structures else ""),
                reason=(reasons[idx] if reasons else ""),
            )
        )
    return infos


# ---------------------------------------------------------------------------
# 输入 / 输出
# ---------------------------------------------------------------------------


def _to_pymupdf_path(path: Path):
    """把 :class:`Path` 转成 PyMuPDF 能接受的路径。

    **常见错误**：把非 ASCII 路径用 ``os.fsencode`` 转成 ``bytes``。
    PyMuPDF 1.24+ 已原生支持 UTF-8 路径，并且**不接受 bytes**，
    传入 bytes 会让中文文件名打不开 ——
    ``fitz.open(b'...中文...')`` 会报
    ``bad filename: type(filename)=<class 'bytes'>``。

    正确做法：**一律返回 ``str``**，让 PyMuPDF 自己处理编码。
    """
    return str(path)


def open_input_pdf(path: Path) -> "fitz.Document":
    """打开输入 PDF，并做完整性与加密检查。"""
    if not path.exists():
        raise PdfSplitterError(f"输入文件不存在: {path}")
    if not path.is_file():
        raise PdfSplitterError(f"输入路径不是文件: {path}")

    try:
        doc = fitz.open(_to_pymupdf_path(path))
    except Exception as exc:  # noqa: BLE001 - 需要包装成可读错误
        raise PdfSplitterError(f"无法打开 PDF（文件可能损坏或不是有效 PDF）: {exc}") from exc

    if doc.needs_pass:
        doc.close()
        raise PdfSplitterError("PDF 已加密，需要密码才能打开")
    if doc.page_count == 0:
        doc.close()
        raise PdfSplitterError("PDF 不包含任何页面")
    return doc


def resolve_output_paths(
    input_path: Path,
    out_dir: Path,
    prefix: str | None,
    force: bool,
) -> Tuple[Path, Path, Path]:
    """计算三个输出文件路径，并按需检查覆盖。

    输出目录解析规则（``--output-dir``）：

    * 绝对路径 -> 直接用
    * ``.``    -> **输入文件所在目录**（沿用「输出放在 PDF 旁边」的习惯）
    * 其它相对路径 -> **相对当前工作目录**解析

    第 3 条特意不采用「相对输入文件目录」，因为那会让 ``--output-dir out``
    在输入 PDF 位于别处时把结果写到用户意料之外的位置。
    """
    stem = prefix if prefix else input_path.stem
    out_dir = Path(out_dir)
    if not out_dir.is_absolute():
        out_dir = input_path.parent if str(out_dir) == "." else Path.cwd() / out_dir

    if not out_dir.exists():
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PdfSplitterError(f"无法创建输出目录 {out_dir}: {exc}") from exc
    if not out_dir.is_dir():
        raise PdfSplitterError(f"输出路径不是目录: {out_dir}")

    color_pdf = out_dir / f"{stem}_color.pdf"
    bw_pdf = out_dir / f"{stem}_bw.pdf"
    report = out_dir / f"{stem}_report.csv"

    if not force:
        clashes = [p for p in (color_pdf, bw_pdf, report) if p.exists()]
        if clashes:
            names = "、".join(p.name for p in clashes)
            raise PdfSplitterError(
                f"输出文件已存在: {names}（使用 --force 覆盖，或 --prefix/--output-dir 换个名字）"
            )
    return color_pdf, bw_pdf, report


def create_output_pdf(
    src: "fitz.Document",
    page_indices: Sequence[int],
    dest_path: Path,
) -> None:
    """把指定页面**原样复制**到新 PDF。

    使用 ``insert_pdf()`` 复制页面对象，不做任何重新渲染或重新编码，
    因此保留原始分辨率、文字层、矢量内容、尺寸与元数据。

    当 ``page_indices`` 为空时，生成一个 **0 页的合法占位 PDF**。

    注意：PyMuPDF 拒绝保存 0 页文档（``cannot save with zero pages``，
    见 ``Document.save`` 中的 ``if self.page_count < 1``），因此空文件不走
    ``save()``/``tobytes()``，而是直接写出一个最小的 0 页 PDF 文件。
    这是合法的 PDF，任何阅读器都能打开（显示 0 页）。
    """
    if not page_indices:
        _write_empty_pdf(src[0].rect, dest_path)
        return

    out = fitz.open()
    try:
        # 源文档页面是连续的，但一次 insert_pdf 需要连续区间；
        # 这里按区间批量复制，减少调用次数。
        start = prev = page_indices[0]
        for idx in list(page_indices[1:]) + [None]:
            if idx is not None and idx == prev + 1:
                prev = idx
                continue
            out.insert_pdf(src, from_page=start, to_page=prev)
            if idx is None:
                break
            start = prev = idx
        out.save(_to_pymupdf_path(dest_path), garbage=3, deflate=True)
    except OSError as exc:
        raise PdfSplitterError(f"无法写入 {dest_path}: {exc}") from exc
    finally:
        out.close()


def _write_empty_pdf(rect: "fitz.Rect", dest_path: Path) -> None:
    """写出一个 0 页的最小合法 PDF（占位用）。

    :param rect: 参考页面尺寸，用作空文档的 MediaBox
    """
    w, h = int(rect.width), int(rect.height)
    content = (
        "%PDF-1.4\n"
        "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        "2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
        f"trailer<</Root 1 0 R/Size 3/MediaBox[0 0 {w} {h}]>>\n"
        "%%EOF\n"
    ).encode("ascii")
    try:
        dest_path.write_bytes(content)
    except OSError as exc:
        raise PdfSplitterError(f"无法写入 {dest_path}: {exc}") from exc


def write_report(
    path: Path,
    page_infos: Sequence[PageInfo],
    sheets: Sequence[SheetInfo],
    meta: dict,
) -> None:
    """写出 CSV 检测报告（页码均为 1-based）。

    除像素统计外还有 ``structure`` 与 ``reason`` 两列 —— "为什么这页
    被判成彩色"不是单靠一个像素比例能说清的（可能是彩色矢量、也可能是
    位图区域检出彩色），把依据写进报告才好复盘。
    """
    sheet_out = {s.number: s.output for s in sheets}
    try:
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            fh.write("# PDF 彩色/黑白页面检测报告（结构优先策略）\n")
            for key, value in meta.items():
                fh.write(f"# {key}: {value}\n")

            fh.write("# 说明: page/page_color 为单页检测结果; "
                     "output 为整张双面纸的归属; sheet 为 1-based 纸张序号\n")
            fh.write("# structure 为该页元素构成; reason 为判定依据; "
                     "rendered 表示该页是否做过像素渲染（纯文字页为否）\n")

            writer = csv.writer(fh)
            writer.writerow(
                ["page", "sheet", "page_color", "output", "reason", "structure",
                 "color_ratio", "color_pixels", "total_pixels", "rendered",
                 "sheet_output"]
            )
            for info in page_infos:
                writer.writerow([
                    info.number,
                    info.sheet,
                    info.page_color,
                    info.output,
                    info.reason,
                    info.structure,
                    f"{info.color_ratio:.6f}",
                    info.color_pixels,
                    info.total_pixels,
                    "是" if info.rendered else "否",
                    sheet_out.get(info.sheet, info.output),
                ])
    except OSError as exc:
        raise PdfSplitterError(f"无法写入报告 {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# 检测流程
# ---------------------------------------------------------------------------


def detect_pages(
    doc: "fitz.Document",
    dpi: int,
    color_threshold: int,
    color_ratio_threshold: float,
) -> Tuple[List[PageInfo], List[Tuple[int, str]], List[PageStructure]]:
    """逐页「结构分析 → 按需渲染」→ 判定。

    返回 ``(page_infos, failures, structures)``。

    内存占用与页数无关：任何时刻只持有一页的图形区域 pixmap。
    """
    infos: List[PageInfo] = []
    structures: List[PageStructure] = []
    failures: List[Tuple[int, str]] = []
    total_pages = doc.page_count
    width = len(str(total_pages))
    rendered_count = 0

    for index in range(total_pages):
        number = index + 1
        try:
            page = doc.load_page(index)
            info, struct = detect_page(
                page,
                dpi=dpi,
                color_threshold=color_threshold,
                color_ratio_threshold=color_ratio_threshold,
            )
        except Exception as exc:  # noqa: BLE001 - 单页失败不应中断整个任务
            message = f"{type(exc).__name__}: {exc}"
            failures.append((number, message))
            info = PageInfo(
                index=index, number=number, sheet=index // 2 + 1,
                is_color=False, color_ratio=0.0, color_pixels=0, total_pixels=0,
                page_color=BW, render_error=message,
                reason=REASON_RENDER_FAIL,
            )
            struct = PageStructure()

        info.index = index
        info.number = number
        info.sheet = index // 2 + 1
        infos.append(info)
        structures.append(struct)
        if info.rendered:
            rendered_count += 1

        print(
            f"[{number:0{width}d}/{total_pages}] "
            f"{info.page_color:<5} {info.reason:<8} {struct.kind}"
            + (f"  ratio={info.color_ratio:.6f}" if info.rendered else "")
        )

    print(
        f"\n共 {total_pages} 页，其中 {rendered_count} 页需要像素渲染、"
        f"{total_pages - rendered_count} 页仅靠结构即可判定。"
    )
    return infos, failures, structures


def print_sheet_summary(sheets: Sequence[SheetInfo]) -> None:
    """打印纸张级分类结果，彩色纸张额外标注，便于人工核对。"""
    print("\nApplying duplex-sheet classification...\n")
    for sheet in sheets:
        mark = "  <= 整张纸升级为 COLOR" if sheet.output == COLOR else ""
        print(f"Sheet {sheet.number}: {sheet.label} -> {sheet.output}{mark}")


def run(args: argparse.Namespace) -> int:
    """完整流程。"""
    input_path = Path(args.input).expanduser()
    out_dir = Path(args.output_dir).expanduser()

    print(f"Input PDF: {input_path}")

    doc = open_input_pdf(input_path)
    try:
        total_pages = doc.page_count
        color_pdf, bw_pdf, report_path = resolve_output_paths(
            input_path, out_dir, args.prefix, args.force
        )

        print(f"Pages: {total_pages}")
        print(f"Detection DPI: {args.dpi}（仅用于位图/渐变区域渲染）")
        print(f"Color threshold: {args.color_threshold}")
        print(f"Color ratio threshold: {args.color_ratio}（仅对渲染的图形区域生效）")

        print("\nAnalyzing pages...")
        page_infos, failures, structures = detect_pages(
            doc, args.dpi, args.color_threshold, args.color_ratio
        )

        page_infos, sheets = classify_pages(page_infos)

        print_sheet_summary(sheets)

        color_indices = [p.index for p in page_infos if p.output == COLOR]
        bw_indices = [p.index for p in page_infos if p.output == BW]

        print("\nWriting output PDFs...")
        create_output_pdf(doc, color_indices, color_pdf)
        create_output_pdf(doc, bw_indices, bw_pdf)

        reasons: dict[str, int] = {}
        for info in page_infos:
            reasons[info.reason] = reasons.get(info.reason, 0) + 1

        meta = {
            "source": input_path.name,
            "pages": total_pages,
            "sheets": len(sheets),
            "strategy": "结构优先：无图形元素直接判黑白；彩色矢量读声明颜色；位图只渲染图形区域",
            "dpi": args.dpi,
            "color_threshold": args.color_threshold,
            "color_ratio_threshold": args.color_ratio,
            "color_pages": len(color_indices),
            "bw_pages": len(bw_indices),
            "rendered_pages": sum(1 for p in page_infos if p.rendered),
            "text_only_pages": reasons.get(REASON_TEXT_ONLY, 0),
            "gray_vector_pages": reasons.get(REASON_GRAY_VECTOR, 0),
            "vector_color_pages": reasons.get(REASON_VECTOR, 0),
            "raster_color_pages": reasons.get(REASON_RASTER, 0),
            "raster_gray_pages": reasons.get(REASON_RASTER_GRAY, 0),
        }
        write_report(report_path, page_infos, sheets, meta)

        def _describe(path: Path, count: int) -> str:
            if count == 0:
                return f"{path}  (0 页，占位空文件)"
            return f"{path}  ({count} 页)"

        print()
        print(f"Color PDF: {_describe(color_pdf, len(color_indices))}")
        print(f"BW PDF: {_describe(bw_pdf, len(bw_indices))}")
        print(f"Report: {report_path}")

        print("\n判定依据分布:")
        for reason in (REASON_TEXT_ONLY, REASON_GRAY_VECTOR, REASON_VECTOR,
                       REASON_RASTER, REASON_RASTER_GRAY, REASON_EMPTY,
                       REASON_RENDER_FAIL):
            if reasons.get(reason):
                print(f"  {reason}: {reasons[reason]} 页")

        if failures:
            print(f"\n警告: {len(failures)} 页渲染失败，已按黑白处理并记录在报告中:")
            for number, message in failures:
                print(f"  - page {number}: {message}")
        elif not bw_indices:
            print("\n提示: 未检测到黑白页面，bw.pdf 为空占位文件（0 页）。")
        elif not color_indices:
            print("\n提示: 未检测到彩色页面，color.pdf 为空占位文件（0 页）。")

        return 0
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="split_pdf.py",
        description=(
            "按双面打印纸为单位把 PDF 拆分为彩色件和黑白件。"
            "同一张纸（如第 31、32 页）只要任一面是彩色，整张纸都归入彩色 PDF。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "识别策略:\n"
            "  1. 页面无图形元素（纯文字）-> 直接判黑白，不渲染\n"
            "  2. 页面有矢量图形 -> 读 PDF 里声明的 fill/color，不渲染\n"
            "  3. 页面有位图图片 -> 先暂标彩色，再只渲染图片区域做色度检测，\n"
            "     检出为灰度则回落黑白\n"
            "\n示例:\n"
            "  python split_pdf.py input.pdf\n"
            "  python split_pdf.py input.pdf --dpi 100 --color-threshold 15 --color-ratio 0.001\n"
            "  python split_pdf.py input.pdf --output-dir output --prefix paper --force\n"
        ),
    )
    parser.add_argument("input", help="输入的 PDF 文件路径")
    parser.add_argument(
        "--dpi", type=int, default=DEFAULT_DPI,
        help=f"位图/渐变区域的颜色检测 DPI（仅检测用，输出 PDF 不受影响），"
             f"默认 {DEFAULT_DPI}",
    )
    parser.add_argument(
        "--color-threshold", type=int, default=DEFAULT_COLOR_THRESHOLD,
        help=(
            "色度阈值：max(R,G,B)-min(R,G,B) 大于该值才算彩色。"
            "矢量颜色与像素色度共用同一阈值。"
            f"默认 {DEFAULT_COLOR_THRESHOLD}"
        ),
    )
    parser.add_argument(
        "--color-ratio", type=float, default=DEFAULT_COLOR_RATIO,
        help="彩色像素占比阈值（仅对渲染出来的图形区域生效），"
             f"默认 {DEFAULT_COLOR_RATIO}",
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="输出目录；`.` 表示与输入文件同目录，其它相对路径基于当前工作目录",
    )
    parser.add_argument(
        "--prefix", default=None,
        help="输出文件名前缀，默认为输入文件的文件名（不含扩展名）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="允许覆盖已存在的输出文件（默认遇到同名文件直接报错退出）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.dpi <= 0:
        parser.error("--dpi 必须为正整数")
    if not 0 <= args.color_threshold <= 255:
        parser.error("--color-threshold 必须在 0..255 之间")
    if not 0.0 <= args.color_ratio <= 1.0:
        parser.error("--color-ratio 必须在 0.0..1.0 之间")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    try:
        return run(args)
    except PdfSplitterError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
