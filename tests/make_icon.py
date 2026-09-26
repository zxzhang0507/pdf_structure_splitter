"""生成程序图标 ``resources/app.ico``（多尺寸）。

不依赖 Pillow / 外部素材：用 Qt 直接画一个「左侧黑白页 + 右侧彩色页」的扁平图标，
与程序主题（同一张纸按彩色/黑白分类）呼应。

用法::

    python tests/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

#: 与 style.qss 同一套配色
PRIMARY = "#2563EB"
COLOR_ACCENT = "#EF4444"
BW_ACCENT = "#6B7280"
PAGE_BG = "#FFFFFF"
BG = "#F9FAFB"

#: 需要的尺寸（Windows 图标常用集合）
SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw_icon(size: int) -> QImage:
    """画一张 ``size × size`` 的图标。"""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)

    s = size / 256.0  # 统一按 256 设计再缩放

    # 底板
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(PRIMARY))
    painter.drawRoundedRect(QRectF(0, 0, size, size), 48 * s, 48 * s)

    # 两张页面：左（黑白）、右（彩色）
    page_w, page_h = 84 * s, 118 * s
    gap = 16 * s
    total_w = page_w * 2 + gap
    x0 = (size - total_w) / 2
    y0 = (size - page_h) / 2

    for i, accent in enumerate((BW_ACCENT, COLOR_ACCENT)):
        x = x0 + i * (page_w + gap)
        # 页面白底
        painter.setBrush(QColor(PAGE_BG))
        painter.drawRoundedRect(QRectF(x, y0, page_w, page_h), 8 * s, 8 * s)
        # 顶部色条标识归属
        painter.setBrush(QColor(accent))
        painter.drawRoundedRect(
            QRectF(x + 8 * s, y0 + 10 * s, page_w - 16 * s, 12 * s), 6 * s, 6 * s
        )
        # 文本行（示意内容）
        painter.setBrush(QColor("#D1D5DB"))
        line_y = y0 + 34 * s
        for width_ratio in (1.0, 0.85, 0.95, 0.6):
            painter.drawRoundedRect(
                QRectF(x + 8 * s, line_y, (page_w - 16 * s) * width_ratio, 7 * s),
                3 * s, 3 * s,
            )
            line_y += 14 * s

    painter.end()
    return image


def main() -> int:
    app = QApplication(sys.argv)  # noqa: F841 - QPainter 需要 QApplication
    out_path = _ROOT / "resources" / "app.ico"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 用最大尺寸构造 QPixmap，再由 Qt 生成多尺寸 ICO
    pixmaps = [QPixmap.fromImage(draw_icon(size)) for size in SIZES]

    # Qt 不直接写多尺寸 ICO，这里手写 ICO 容器（内嵌 PNG，Vista+ 支持）
    from PySide6.QtCore import QBuffer, QByteArray
    import struct

    pngs: list[bytes] = []
    for pixmap in pixmaps:
        buffer = QBuffer(QByteArray())
        buffer.open(QBuffer.WriteOnly)
        pixmap.save(buffer, "PNG")
        pngs.append(bytes(buffer.data()))
        buffer.close()

    with out_path.open("wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(pngs)))      # ICONDIR
        offset = 6 + 16 * len(pngs)
        for size, png in zip(SIZES, pngs):
            dim = 0 if size >= 256 else size                  # 256 用 0 表示
            fh.write(struct.pack(
                "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset
            ))
            offset += len(png)
        for png in pngs:
            fh.write(png)

    print(f"已生成 {out_path}（{out_path.stat().st_size} 字节，{len(SIZES)} 个尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
