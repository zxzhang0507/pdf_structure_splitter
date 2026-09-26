#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面结构分析：识别页面上有哪些元素，以及图形元素落在哪些区域。

## 为什么需要它

若把每一页都**整页渲染**成位图再统计全页的彩色像素占比，有两个代价：

1. **纯文字页也要渲染**。132 页的论文里若 80 页是纯文字，这些渲染
   完全是被浪费的（75 dpi 下一页约 35 ms）。
2. **色彩被稀释**。页边距、大片留白、黑色正文都不含彩色像素，分母却
   把它们算进去 —— 一个 10 pt 的彩色图标在整页渲染下只占万分之几
   的像素，很容易被 ``color_ratio`` 阈值滤掉。

因此这里先读**页面结构**（PDF 内容流里本来就有这些信息，不需要渲染）：

============  ==================================================
页面元素       处理方式
============  ==================================================
文字          字符数 > 0 → 该页"有文字"
位图图片      取 bbox → **只渲染这些区域**做色度检测
矢量图形      直接读 fill / stroke 的**颜色值**，完全不渲染
============  ==================================================

于是：

* **没有任何图形元素的页面**（纯文字 / 空白）→ 直接判黑白，零渲染；
* **矢量图形的颜色是 PDF 里显式声明的**，不是像素渲染出来的，没有
  抗锯齿噪声 —— ``chroma = max(R,G,B) - min(R,G,B)`` 超过色度阈值就是
  彩色。一条 0.5 pt 的彩色细线也逃不掉，而渲染法在低 dpi 下只能得到
  几个半透明的像素，很容易漏检；
* **位图图片**仍走渲染检测：照片、扫描件、截图的实际内容必须看像素，
  PDF 里只存着"这是一张图"而不知道它是不是彩色的。

本模块**不依赖 split_pdf**（只在函数内部惰性引用），因此可以被反向导入
而不产生循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

try:  # PyMuPDF >= 1.24 推荐的新命名
    import pymupdf as fitz
except ImportError:  # pymupdf < 1.24 只有 fitz 这个模块名
    import fitz  # type: ignore[no-redef]

import numpy as np

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认检测 DPI（仅用于颜色判定，不影响输出 PDF 质量）。
#:
#: 它只作用于**位图区域**的渲染 —— 纯文字页与矢量页完全不渲染，
#: 因此这个参数的重要性远低于"整页渲染"式的做法。
#:
#: 取 75 而非更高，是因为**高 DPI 会把位图内部的压缩伪影暴露出来**：
#: 扫描件与 JPEG 位图常带有几度的色偏（色度 16–20，紧贴阈值 15），
#: DPI 越高这些像素越不会被平均掉，于是纯黑白的图被误判成彩色。以一张
#: 纯黑白阶梯图为例（其内嵌位图有轻微 JPEG 色偏）：
#:
#: ==========  ============  ================
#: DPI         彩色像素       判定
#: ==========  ============  ================
#: 75          0  (被平均掉)   位图灰度 ✅
#: 100         195 (0.0018)    位图彩色 ❌ 误判
#: 150         944 (0.0038)    位图彩色 ❌ 误判更明显
#: ==========  ============  ================
#:
#: 反过来，DPI 过低会让小位图区域内的样本太少、可能漏掉真实的小面积
#: 彩色。75 是这两者之间的平衡点。
#:
#: 若遇到带压缩伪影的扫描图被误判成彩色，**应调高「色度阈值」**
#: （15 → 20–25）而不是调低 DPI —— 伪影色度大多在 16–20，
#: 真实彩色内容通常远高于此，调阈值能一次解决整份文档的同类问题。
DEFAULT_DPI = 75
#: 默认色度阈值：max(R,G,B) - min(R,G,B) 超过该值即视为彩色。
#: 同时用于矢量颜色判定与像素色度判定，两者量纲一致（都是 0..255）。
DEFAULT_COLOR_THRESHOLD = 15
#: 默认彩色像素占比阈值
DEFAULT_COLOR_RATIO = 0.001
#: 渲染高度上限，避免超大页面在低 DPI 下仍占用过多内存
MAX_PIXELS = 12_000_000

#: 图形区域渲染时的**最小边长**（像素）。
#:
#: 区域裁剪后可能非常小（一个 12 pt 的小图标在 75 dpi 下只有 12 像素），
#: 色度统计会因为样本太少而不稳定。这里给区域一个最低像素预算：
#: 小区域自动用更高的 dpi 渲染，直到边长达到该值（或撞上 dpi 上限）。
MIN_REGION_PX = 240
#: 图形区域渲染的 dpi 上限。小区域提精度也不能无限提，否则一个 2pt 的
#: 图标会被渲染成几千像素的图，白白拖慢检测。
MAX_REGION_DPI = 300
#: 区域外扩（pt）。图形 bbox 是紧贴内容的，抗锯齿与线宽会溢出边界
#: 约半个像素，外扩一点避免把边缘裁掉而让色度统计失真。
REGION_PAD_PT = 1.0

#: 区域渲染的**面积上限**（pt²）。超过它就不必再提精度。
#:
#: 与 ``MIN_REGION_PX`` 配合使用：一个 1×500 pt 的细条若按"短边凑够
#: 240 px"来提精度，会得到 17280 dpi × 240 px 的巨型图像。因此当区域
#: 本身就很大时（面积超过此值），放弃提精度、直接用基准 dpi。
BIG_REGION_PT2 = 4000.0

#: 逐路径做颜色判定的矢量路径数量上限。
#:
#: CAD 图、超密集表格可能含上万条路径，逐条解析颜色会很慢。超过上限就
#: 停止收集并把该页标记为 ``vector_overflow``，由上层退化为整页渲染检测
#: ——宁可慢一点，也不能给出不完整的结论。
MAX_VECTOR_PATHS = 4000

#: 参与矩形合并的区域数量上限。超过则直接并成一个总包围盒
#: （即退化为"渲染图形覆盖区"），避免 O(n²) 合并拖垮检测。
MAX_MERGE_RECTS = 240

#: 合并区域时的容差（pt）：距离小于它的两个矩形视为同一块图形
MERGE_GAP_PT = 2.0

Rect4 = Tuple[float, float, float, float]


class PageRenderError(Exception):
    """单页渲染失败。"""


# ---------------------------------------------------------------------------
# 基础渲染与色度统计
# ---------------------------------------------------------------------------


def render_page(page: "fitz.Page", dpi: int) -> "fitz.Pixmap":
    """将整页低分辨率渲染为 RGB pixmap。

    仅在需要时缩放到 ``MAX_PIXELS`` 以内，正常页面不会触发。

    GUI 版也用它渲染缩略图，因此对外公开（旧名 ``_render_page`` 保留为别名）。
    """
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)

    est = abs(page.rect.width * scale) * abs(page.rect.height * scale)
    if est > MAX_PIXELS:
        shrink = (MAX_PIXELS / est) ** 0.5
        matrix = fitz.Matrix(scale * shrink, scale * shrink)

    return page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)


#: 兼容旧名（CLI 版内部及历史调用方）
_render_page = render_page


def analyze_page_color(
    pixmap: "fitz.Pixmap",
    color_threshold: int = DEFAULT_COLOR_THRESHOLD,
) -> Tuple[float, int, int]:
    """分析 pixmap 中的彩色像素比例。

    对每个像素计算 ``chroma = max(R,G,B) - min(R,G,B)``：

    * ``chroma <= color_threshold`` -> 灰度像素
    * ``chroma >  color_threshold`` -> 彩色像素

    采用色度差而非 ``R != G != B``，因此能容忍 JPEG 压缩偏差、扫描噪声、
    抗锯齿边缘和灰度图片带来的微小通道差异。

    :returns: ``(color_ratio, color_pixels, total_pixels)``
    """
    if pixmap.n == 1:  # 纯灰度渲染结果
        return 0.0, 0, pixmap.width * pixmap.height
    if pixmap.n == 4:  # 理论上不会出现（alpha=False），防御性处理
        arr = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(-1, 4)
        arr = arr[:, :3]
    else:
        arr = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(-1, 3)

    total = int(arr.shape[0])
    if total == 0:
        raise PageRenderError("渲染结果为空（0 像素）")

    # 逐块处理，避免大页面生成多个同尺寸的 int16 临时数组
    chunk = max(1, 4_000_000)
    color_pixels = 0
    for start in range(0, total, chunk):
        block = arr[start:start + chunk].astype(np.int16)
        chroma = block.max(axis=1) - block.min(axis=1)
        color_pixels += int(np.count_nonzero(chroma > color_threshold))

    return color_pixels / total, color_pixels, total


# ---------------------------------------------------------------------------
# 颜色值工具（用于矢量图形）
# ---------------------------------------------------------------------------


def color_chroma(rgb) -> float:
    """把 PyMuPDF 的颜色值换算成 0..255 量纲的色度。

    ``get_drawings()`` 返回的 ``fill`` / ``color`` 是 **0..1 的浮点三元组**，
    而像素色度是 0..255 的整数差。为了让两者能用同一个 ``color_threshold``
    比较，这里统一折算到 0..255 量纲。

    无法解析时返回 0（当作灰度）—— 判错成黑白比判错成彩色更安全，
    因为灰度的东西送黑白打印机不会出问题，彩色的东西送黑白打印机才会。
    """
    if rgb is None:
        return 0.0
    try:
        r, g, b = (float(v) for v in tuple(rgb)[:3])
    except (TypeError, ValueError):
        return 0.0
    # 防御：个别构造会返回 0..255 的整数值而非 0..1 浮点。
    # 只在需要时才放大，否则 255 会被二次放大成 65025。
    if max(r, g, b) <= 1.0001:
        r, g, b = r * 255.0, g * 255.0, b * 255.0
    return abs(max(r, g, b) - min(r, g, b))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class PageStructure:
    """一页的元素构成。全部字段都来自 PDF 内容流，**不需要渲染**。

    ``image_rects`` / ``vector_rects`` 里的矩形都是 ``(x0, y0, x1, y1)``，
    单位 pt，已夹在页面范围内。
    """

    page_w: float = 0.0
    page_h: float = 0.0
    #: 去空白后的字符数（含全角标点，不含空格换行）
    text_chars: int = 0
    #: 位图图片在页面上的显示区域
    image_rects: List[Rect4] = field(default_factory=list)
    #: 全部矢量路径的包围盒
    vector_rects: List[Rect4] = field(default_factory=list)
    #: **颜色非灰**的矢量路径包围盒
    colored_vector_rects: List[Rect4] = field(default_factory=list)
    #: **颜色无法判定**的矢量路径包围盒（渐变 / 阴影填充）
    unknown_vector_rects: List[Rect4] = field(default_factory=list)
    #: 矢量路径总数（含灰色路径，不含纯裁剪路径）
    vector_count: int = 0
    #: 颜色非灰的矢量路径数
    colored_vector_count: int = 0
    #: 颜色无法判定的矢量路径数（渐变等）
    unknown_vector_count: int = 0
    #: 矢量路径过多、颜色判定未做完（上层应退化为整页渲染）
    vector_overflow: bool = False
    #: 分析过程中遇到的非致命问题（供报告与排查使用）
    notes: List[str] = field(default_factory=list)

    # ---- 便捷判断 ----

    @property
    def has_text(self) -> bool:
        return self.text_chars > 0

    @property
    def raster_count(self) -> int:
        return len(self.image_rects)

    @property
    def has_raster(self) -> bool:
        """是否含位图图片。"""
        return bool(self.image_rects)

    @property
    def has_vector(self) -> bool:
        """是否含矢量图形（无论颜色）。"""
        return self.vector_count > 0

    @property
    def has_colored_vector(self) -> bool:
        """是否含**颜色非灰**的矢量图形。"""
        return self.colored_vector_count > 0

    @property
    def has_unknown_vector(self) -> bool:
        """是否含颜色无法判定的矢量图形（渐变 / 阴影填充）。"""
        return self.unknown_vector_count > 0

    @property
    def has_graphic(self) -> bool:
        """是否含任何图形元素（位图或矢量）。

        为 False 表示"整页只有文字"（或干脆是空白页）—— 这是新策略里
        **唯一可以直接下结论、完全不需要渲染**的情况。
        """
        return self.has_raster or self.has_vector

    def needs_region_scan(self) -> bool:
        """是否需要渲染图形区域。

        纯文字页返回 ``False`` —— 这正是新策略省下大量渲染的地方。
        彩色矢量已经给出"这页是彩色"的结论、不必再渲染；只有**位图**和
        **颜色未知的矢量**（渐变）需要看像素。
        """
        return self.has_raster or self.has_unknown_vector

    @property
    def kind(self) -> str:
        """元素构成的中文短语，直接用于界面与报告。"""
        parts: List[str] = []
        if self.has_text:
            parts.append("文字")
        if self.has_raster:
            parts.append(f"{self.raster_count} 张位图")
        if self.has_vector:
            n = self.vector_count
            extra: List[str] = []
            if self.colored_vector_count:
                extra.append(f"{self.colored_vector_count} 彩色")
            if self.unknown_vector_count:
                extra.append(f"{self.unknown_vector_count} 渐变")
            if extra:
                parts.append(f"{n} 条矢量（{'／'.join(extra)}）")
            else:
                parts.append(f"{n} 条矢量")
        if not parts:
            return "空白页"
        return " + ".join(parts)

    # ---- 面积 ----

    @property
    def page_area(self) -> float:
        return max(1.0, self.page_w * self.page_h)

    @property
    def graphic_area_ratio(self) -> float:
        """图形元素（合并后）覆盖的页面面积占比。

        用于报告与排查：占比接近 1 说明整页都是图（例如扫描页），
        占比很小说明只是零星小图或线条。
        """
        rects = self.image_rects + self.colored_vector_rects + self.vector_rects
        if not rects:
            return 0.0
        merged = merge_rects(rects)
        area = sum(max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1]) for r in merged)
        return min(1.0, area / self.page_area)


@dataclass
class RegionScan:
    """位图区域扫描结果。"""

    color_ratio: float = 0.0
    color_pixels: int = 0
    total_pixels: int = 0
    #: 实际渲染的区域数
    rendered: int = 0
    #: 单个区域渲染失败的信息
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# 矩形工具
# ---------------------------------------------------------------------------


def _clamp_bbox(bbox, page_rect: "fitz.Rect",
                min_size: float = 0.05) -> Optional[Rect4]:
    """把任意来源的 bbox 规范化成夹在页面内的 ``(x0,y0,x1,y1)``。

    无效（空、反向、退化成一个点）时返回 ``None``。
    """
    if bbox is None:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in tuple(bbox)[:4])
    except (TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = max(x0, float(page_rect.x0))
    y0 = max(y0, float(page_rect.y0))
    x1 = min(x1, float(page_rect.x1))
    y1 = min(y1, float(page_rect.y1))
    if x1 - x0 < min_size or y1 - y0 < min_size:
        return None
    return (x0, y0, x1, y1)


def _path_bbox(path: dict, page_rect: "fitz.Rect") -> Optional[Rect4]:
    """取一条矢量路径的包围盒，并把**退化的规则线**还原成有面积的矩形。

    实测（真实论文 PDF）：页眉横线这类"规则线"的 bbox 高度是 **0**
    （例如 ``(89.86, 63.98, 505.42, 63.98)``，一条 415 pt 长、0.4 pt 粗的
    分隔线）。若原样使用，渲染时面积为 0 会直接失败，而且
    :func:`merge_rects` 会把它们全部丢掉。

    因此对退化矩形按**线宽**外扩成有面积的窄条 —— 既保留"这里有一条线"
    的事实，也让它在需要渲染时能正常光栅化。
    """
    box = _clamp_bbox(path.get("rect"), page_rect, min_size=0.0)
    if box is None:
        return None
    w = box[2] - box[0]
    h = box[3] - box[1]
    if w >= 0.05 and h >= 0.05:
        return box
    if w < 0.01 and h < 0.01:
        return None                      # 真·一个点，不构成图形
    try:
        line_w = float(path.get("width") or 1.0)
    except (TypeError, ValueError):
        line_w = 1.0
    pad = max(0.5, line_w / 2.0 + 0.25)
    return _clamp_bbox(
        (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
        page_rect, min_size=0.0,
    )


def _close(a: Sequence[float], b: Sequence[float], gap: float) -> bool:
    """两个矩形在各自外扩 ``gap`` 后是否相交。"""
    return not (a[2] + gap < b[0] or b[2] + gap < a[0]
                or a[3] + gap < b[1] or b[3] + gap < a[1])


def merge_rects(rects: Sequence[Rect4], *,
                gap: float = MERGE_GAP_PT,
                limit: int = MAX_MERGE_RECTS) -> List[Rect4]:
    """把相互重叠或距离小于 ``gap`` 的矩形合并，减少渲染次数。

    合并的意义：一张跨栏大图可能被拆成十几个 tile，一行表格线可能是
    几十条独立路径。逐个区域渲染会反复重建 MuPDF 的裁剪矩阵，开销远大于
    渲染本身；合并成少数几块之后再渲染就快得多。

    数量超过 ``limit`` 时直接并成一个总包围盒（退化为"渲染图形覆盖区"），
    避免 O(n²) 的合并循环拖垮检测。
    """
    boxes: List[List[float]] = []
    for r in rects:
        if r is None:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in tuple(r)[:4])
        except (TypeError, ValueError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        boxes.append([x0, y0, x1, y1])

    if not boxes:
        return []
    if len(boxes) > limit:
        return [(
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )]

    changed = True
    while changed and len(boxes) > 1:
        changed = False
        out: List[List[float]] = []
        for box in boxes:
            for keep in out:
                if _close(keep, box, gap):
                    keep[0] = min(keep[0], box[0])
                    keep[1] = min(keep[1], box[1])
                    keep[2] = max(keep[2], box[2])
                    keep[3] = max(keep[3], box[3])
                    changed = True
                    break
            else:
                out.append(box)
        boxes = out
    return [tuple(b) for b in boxes]


# ---------------------------------------------------------------------------
# 结构分析
# ---------------------------------------------------------------------------


def _iter_image_info(page: "fitz.Page") -> List[dict]:
    """取页面上**实际显示**的位图信息（含 bbox）。

    用 ``get_image_info()`` 而不是 ``get_images()``：后者返回的是文档级的
    XObject 引用，可能被多页复用、也可能根本没被绘制；前者返回的是页面
    显示列表中真正出现的位置，正是我们需要的。
    """
    for name in ("get_image_info",):
        fn = getattr(page, name, None)
        if fn is None:
            continue
        try:
            return list(fn())
        except Exception:  # noqa: BLE001 - 个别构造会抛，按"没有图"处理
            return []
    return []


def _get_drawings(page: "fitz.Page") -> Tuple[List[dict], str]:
    """取页面的矢量路径。

    优先用 C 实现的 ``get_cdrawings()``（PyMuPDF 1.19+），它比纯 Python 的
    ``get_drawings()`` 快数倍；不可用时回退。
    """
    last_error = ""
    for name in ("get_cdrawings", "get_drawings"):
        fn = getattr(page, name, None)
        if fn is None:
            continue
        try:
            return list(fn()), ""
        except Exception as exc:  # noqa: BLE001 - 换下一个接口再试
            last_error = f"{type(exc).__name__}: {exc}"
    return [], last_error or "无可用的矢量解析接口"


def analyze_page_structure(
    page: "fitz.Page",
    *,
    color_threshold: int = DEFAULT_COLOR_THRESHOLD,
    max_vector_paths: int = MAX_VECTOR_PATHS,
) -> PageStructure:
    """读出一个页面的元素构成。**不渲染任何像素**。

    :param color_threshold: 判定矢量颜色"是否算彩色"的色度阈值
    :param max_vector_paths: 逐路径判定颜色的数量上限，超过则置
        ``vector_overflow``（该页的矢量结论不完整，上层应退化为整页渲染）
    """
    rect = page.rect
    w = float(rect.width) if rect.width > 0 else 1.0
    h = float(rect.height) if rect.height > 0 else 1.0
    page_rect = fitz.Rect(0.0, 0.0, w, h)

    struct = PageStructure(page_w=w, page_h=h)

    # ---- 文字 ----
    try:
        text = page.get_text("text") or ""
    except Exception as exc:  # noqa: BLE001 - 文字读不到不影响图形判定
        text = ""
        struct.notes.append(f"文字提取失败：{type(exc).__name__}: {exc}")
    # 去掉所有空白：一段只有空格和换行的"文字"不是内容
    struct.text_chars = len("".join(text.split()))

    # ---- 位图 ----
    for info in _iter_image_info(page):
        box = _clamp_bbox(info.get("bbox") if isinstance(info, dict) else info,
                          page_rect)
        if box is not None:
            struct.image_rects.append(box)

    # ---- 矢量 ----
    drawings, vector_err = _get_drawings(page)
    if vector_err:
        struct.notes.append(f"矢量解析失败：{vector_err}")
    ignored_clips = 0
    for path in drawings:
        if struct.vector_count >= max_vector_paths:
            struct.vector_overflow = True
            struct.notes.append(
                f"矢量路径超过 {max_vector_paths} 条，已停止逐条颜色判定"
            )
            break
        if not isinstance(path, dict):
            continue
        box = _path_bbox(path, page_rect)

        # 纯裁剪路径只定义可见范围、不画任何东西，其"颜色"字段可能是
        # 残留值而非实际绘制色，计入会凭空制造假彩色。
        if str(path.get("type") or "").strip().lower() in ("clip", "clip_stroke"):
            ignored_clips += 1
            continue

        struct.vector_count += 1

        # 颜色直接读 PDF 里声明的值 —— 填充色与描边色都要看：
        # 一个纯描边的彩色圆环（fill=None, color=红）同样是彩色内容。
        chroma = max(
            color_chroma(path.get("fill")),
            color_chroma(path.get("color")),
        )
        if chroma > color_threshold:
            struct.colored_vector_count += 1
            if box is not None:
                struct.colored_vector_rects.append(box)
        elif _is_unresolved(path):
            # 渐变 / 阴影填充：实际颜色只在内容流里，声明的 fill 是个占位符，
            # 必须渲染才能知道它是不是彩色的。
            struct.unknown_vector_count += 1
            if box is not None:
                struct.unknown_vector_rects.append(box)

        if box is not None:
            struct.vector_rects.append(box)

    if ignored_clips:
        struct.notes.append(f"跳过 {ignored_clips} 条纯裁剪路径")
    return struct



def _is_unresolved(path: dict) -> bool:
    """这条路径的颜色是不是"读不出来、必须渲染"？

    只有**渐变**属于这一类：它的 ``fill`` 只是渐变起止色或占位色，
    中间可以有完全不同的颜色（例如白→红→蓝），按声明值判定会把彩色
    渐变误判成黑白。判据是 ``get_drawings()`` 里显式的 ``"shading"``
    键（渐变填充会带上它）。

    **不要**用 ``fill_opacity`` 或 ``type`` 当判据 —— 实测（真实论文
    PDF，6007 条路径）证明：

    * ``fill_opacity`` 出现在**每一条**填充路径上，值几乎恒为 1.0，
      把它当渐变信号会把所有填充都误判成"需渲染"；
    * ``type`` 只有 ``'f'``（填充）/ ``'s'``（描边）/ ``'fs'``（两者）
      三种取值，全部是正常绘制，没有一个代表渐变或阴影。
    """
    return "shading" in path


# ---------------------------------------------------------------------------
# 图形区域渲染检测
# ---------------------------------------------------------------------------


def region_scale(rect: Rect4, base_dpi: int, *,
                 min_short_side_px: int = MIN_REGION_PX,
                 max_dpi: int = MAX_REGION_DPI) -> float:
    """算出某个区域该用的渲染缩放系数。

    取「基准 dpi」与「凑够 ``min_short_side_px`` 所需缩放」中的较大者，
    再用 ``max_dpi`` 封顶 —— 于是大图按基准 dpi 走（不浪费），小图标
    自动提精度（避免样本太少导致色度统计不稳）。

    **按短边**（而不是长边或面积）决定提精度幅度：一条 400×1 pt 的细线
    长边完全够格，但短边只有 1 pt，在 75 dpi 下不足 1 像素宽，渲染出来
    会因抗锯齿而整体变淡、色度被稀释。按短边提精度才能让它真正着色。
    """
    w = max(1e-6, rect[2] - rect[0])
    h = max(1e-6, rect[3] - rect[1])
    base = max(1, int(base_dpi)) / 72.0
    if w * h >= BIG_REGION_PT2:
        return base
    need = min_short_side_px / min(w, h)
    return min(max(base, need), max_dpi / 72.0)


def render_region(page: "fitz.Page", rect: Rect4, scale: float, *,
                  max_pixels: int = MAX_PIXELS,
                  pad: float = REGION_PAD_PT) -> "fitz.Pixmap":
    """只渲染页面上的一个矩形区域。

    ``clip`` 让 MuPDF 在光栅化阶段就只处理该区域，比先渲染整页再裁剪
    省得多。超出 ``max_pixels`` 时同比例缩小（与整页渲染的策略一致）。
    """
    page_rect = page.rect
    x0 = max(float(page_rect.x0), min(rect[0], rect[2]) - pad)
    y0 = max(float(page_rect.y0), min(rect[1], rect[3]) - pad)
    x1 = min(float(page_rect.x1), max(rect[0], rect[2]) + pad)
    y1 = min(float(page_rect.y1), max(rect[1], rect[3]) + pad)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise PageRenderError("图形区域为空")

    scale = max(1e-6, float(scale))
    est = (x1 - x0) * scale * (y1 - y0) * scale
    if est > max_pixels:
        scale *= (max_pixels / est) ** 0.5

    clip = fitz.Rect(x0, y0, x1, y1)
    return page.get_pixmap(
        matrix=fitz.Matrix(scale, scale), clip=clip,
        colorspace=fitz.csRGB, alpha=False,
    )


def scan_regions(
    page: "fitz.Page",
    rects: Sequence[Rect4],
    *,
    dpi: int,
    color_threshold: int = DEFAULT_COLOR_THRESHOLD,
) -> RegionScan:
    """渲染若干区域并汇总色度统计。

    把各区域的彩色像素与总像素**分别累加后**再算比例（而不是先算各自
    比例再平均）——这样大图自然占更大权重，一张小图标不会因为"它自己
    区域内 100% 是彩色"就把整页拉成彩色。
    """
    scan = RegionScan()
    merged = merge_rects(rects)
    for rect in merged:
        try:
            scale = region_scale(rect, dpi)
            pixmap = render_region(page, rect, scale)
            _ratio, color_px, total_px = analyze_page_color(pixmap, color_threshold)
        except Exception as exc:  # noqa: BLE001 - 单块失败不影响其他块
            scan.failures.append(f"{type(exc).__name__}: {exc}")
            continue
        scan.color_pixels += color_px
        scan.total_pixels += total_px
        scan.rendered += 1

    if scan.total_pixels:
        scan.color_ratio = scan.color_pixels / scan.total_pixels
    return scan
