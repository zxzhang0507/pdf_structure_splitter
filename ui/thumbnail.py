"""缩略图渲染与缓存。

分两级，避免一次性把整份 PDF 渲染成高清图把内存打爆：

* **微缩略图**（宽约 120 px，dpi≈20）—— 列表里展示，按需生成并缓存
* **预览图**（宽约 800 px，dpi≈120）—— 详情面板用，只保留最近一张

渲染路径::

    fitz.Page --get_pixmap--> Pixmap(RGB, alpha=False)
              --np.frombuffer--> ndarray (H, W, 3) uint8
              --QImage--> QPixmap

> 注意 ``QImage`` 直接引用 ndarray 的内存，必须保证数组在 QPixmap 构造完成前存活，
> 且内存 **C 连续**（否则图像会出现斜条纹）。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

from core import render_page

#: 微缩略图的目标宽度（像素）
THUMB_WIDTH = 120
#: 生成微缩略图时使用的渲染 dpi
THUMB_DPI = 20

#: 缩略图渲染的清晰度余量。
#:
#: 缩略图**不能**用固定 dpi：卡片放大到 300% 时页面区有 240 px 宽，
#: 而原来的 dpi=20 只渲染出 166 px —— 拿低分辨率图去铺更大的区域，
#: 必然发虚。因此存储的缩略图像素宽固定为「显示宽 × 该余量」，
#: 渲染 dpi 再据此反推。
#:
#: 取 2.0 是为了照顾高 DPI 屏（devicePixelRatio=2），普通屏会略缩小一点点，
#: 视觉上比升采样锐利得多。
THUMB_QUALITY_MARGIN = 2.0

#: 渲染 dpi 的下限 / 上限。上限防止极端缩放时白白吃内存。
THUMB_MIN_DPI = 20
THUMB_MAX_DPI = 150

#: A4 纸的宽（英寸），用于在「像素宽」与「dpi」之间换算
A4_WIDTH_INCH = 8.27

#: 预览图的目标宽度
PREVIEW_WIDTH = 800
#: 生成预览图时使用的渲染 dpi
PREVIEW_DPI = 150


def thumbnail_dpi_for(page_width_px: int) -> int:
    """给定卡片上页面的显示宽度（CSS 像素），算出该用多少 dpi 渲染。

    ``dpi ≈ 显示像素宽 / 8.27 × 清晰度余量``，再夹到上下限之间。

    **向上取整**：``round`` 会在边界处少给一点像素（例如 2.5x 时
    A4 在 48 dpi 下宽 397 px，而目标要 400 px），导致缩略图比目标略小、
    仍要轻微放大。向上取整只会多几个像素，代价可忽略。
    """
    import math

    raw = page_width_px / A4_WIDTH_INCH * THUMB_QUALITY_MARGIN
    return max(THUMB_MIN_DPI, min(THUMB_MAX_DPI, math.ceil(raw)))


def thumbnail_width_for(page_width_px: int) -> int:
    """缩略图存储时应有的像素宽（= 显示宽 × 余量）。"""
    return max(1, round(page_width_px * THUMB_QUALITY_MARGIN))


def pixmap_to_qimage(pixmap) -> QImage:
    """把 PyMuPDF 的 Pixmap 转成 QImage。

    :param pixmap: ``fitz.Pixmap``，须为 RGB、无 alpha
    """
    arr = np.frombuffer(pixmap.samples, dtype=np.uint8)
    arr = arr.reshape(pixmap.height, pixmap.width, pixmap.n)
    if pixmap.n == 3:
        arr = np.ascontiguousarray(arr)          # QImage 要求 C 连续
        fmt = QImage.Format_RGB888
        bytes_per_line = 3 * pixmap.width
    elif pixmap.n == 4:
        arr = np.ascontiguousarray(arr)
        fmt = QImage.Format_RGBA8888
        bytes_per_line = 4 * pixmap.width
    elif pixmap.n == 1:
        # 单通道灰度：先铺成 RGB 再交给 QImage
        gray = np.repeat(arr, 3, axis=2)
        arr = np.ascontiguousarray(gray)
        fmt = QImage.Format_RGB888
        bytes_per_line = 3 * pixmap.width
    else:
        raise ValueError(f"暂不支持的通道数: {pixmap.n}")

    image = QImage(arr.data, pixmap.width, pixmap.height,
                   bytes_per_line, fmt)
    # copy() 必需：arr 是临时数组，QImage 只是引用它的内存
    return image.copy()


def render_thumbnail(page, dpi: int = THUMB_DPI,
                     target_width: int = THUMB_WIDTH) -> QPixmap:
    """渲染单页的微缩略图。

    :param dpi: 渲染 dpi。应随卡片缩放级别调整（见 :func:`thumbnail_dpi_for`），
        否则放大后会因源图分辨率不足而发虚。
    :param target_width: 渲染后缩到的目标宽度；列表视图里不需要全尺寸像素。
    """
    return _render_scaled(page, dpi, target_width)


def render_preview(page) -> QPixmap:
    """渲染单页的预览大图。"""
    return _render_scaled(page, PREVIEW_DPI, PREVIEW_WIDTH)


def _render_scaled(page, dpi: int, target_width: int) -> QPixmap:
    pixmap = render_page(page, dpi)
    image = pixmap_to_qimage(pixmap)
    if image.width() > target_width:
        image = image.scaledToWidth(target_width, Qt.SmoothTransformation)
    return QPixmap.fromImage(image)
