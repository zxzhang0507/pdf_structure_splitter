"""预览大图对话框。

要点：

* **一张纸的两页都显示**（左右并排），而不是只给第一页 ——
  否则用户没法核对配对页到底是不是彩色。
* 分辨率比缩略图高得多（默认 dpi 150），可看清正文与小字。
* 关闭即释放 pixmap：全项目任何时刻只保留一个预览对话框。
* 支持缩放（``Ctrl+滚轮`` / 按钮）与「适应窗口」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

#: 预览渲染 dpi（比缩略图的 20 高得多）
PREVIEW_DPI = 150
#: 缩放范围
MIN_SCALE = 0.25
MAX_SCALE = 4.0
SCALE_STEP = 1.15


class PreviewDialog(QDialog):
    """显示一张纸（1~2 页）的预览大图。"""

    def __init__(
        self,
        pixmaps: Sequence[QPixmap],
        titles: Sequence[str],
        captions: Sequence[str],
        parent: QWidget | None = None,
        window_title: str = "预览",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.resize(1200, 900)

        self._pixmaps = list(pixmaps)      # 持有引用；closeEvent 里释放
        self._labels: list[QLabel] = []
        self._scale = 1.0

        # ---- 页面区（每页一个可滚动区域，横向排列）----
        pages_row = QHBoxLayout()
        pages_row.setSpacing(12)
        for pixmap, title in zip(self._pixmaps, titles):
            block = QWidget()
            block_lay = QVBoxLayout(block)
            block_lay.setContentsMargins(0, 0, 0, 0)
            block_lay.setSpacing(6)

            header = QLabel(title)
            header.setObjectName("PreviewCaption")
            header.setAlignment(Qt.AlignCenter)

            label = QLabel()
            label.setAlignment(Qt.AlignCenter)
            label.setPixmap(pixmap)
            self._labels.append(label)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setAlignment(Qt.AlignCenter)
            scroll.setWidget(label)

            block_lay.addWidget(header)
            block_lay.addWidget(scroll, 1)
            pages_row.addWidget(block, 1)

        # ---- 底部说明 + 缩放控制 ----
        self._caption = QLabel("　".join(c for c in captions if c))
        self._caption.setObjectName("PreviewCaption")
        self._caption.setAlignment(Qt.AlignCenter)
        self._caption.setWordWrap(True)

        zoom_out = QPushButton("缩小")
        zoom_in = QPushButton("放大")
        zoom_fit = QPushButton("适应窗口")
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("PrimaryButton")
        for btn, delta in ((zoom_out, -1), (zoom_in, +1)):
            btn.setMinimumWidth(64)
            btn.clicked.connect(lambda _=False, d=delta: self._zoom(d))
        zoom_fit.clicked.connect(self.fit_to_window)
        close_btn.clicked.connect(self.accept)

        controls = QHBoxLayout()
        controls.addWidget(self._caption, 1)
        controls.addWidget(zoom_out)
        controls.addWidget(zoom_in)
        controls.addWidget(zoom_fit)
        controls.addWidget(close_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addLayout(pages_row, 1)
        root.addLayout(controls)

        # Ctrl+滚轮缩放
        for label in self._labels:
            label.installEventFilter(self)

    # ------------------------------------------------------------------

    def _zoom(self, direction: int) -> None:
        factor = SCALE_STEP if direction > 0 else 1 / SCALE_STEP
        self._apply_scale(self._scale * factor)

    def _apply_scale(self, scale: float) -> None:
        scale = max(MIN_SCALE, min(MAX_SCALE, scale))
        self._scale = scale
        for label, pixmap in zip(self._labels, self._pixmaps):
            label.setPixmap(pixmap.scaled(
                pixmap.size() * scale,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            ))

    def fit_to_window(self) -> None:
        """把图片缩放到适应窗口（不放大超过 100%）。"""
        if not self._pixmaps:
            return
        viewport = self.size()
        target_h = max(200, viewport.height() - 160)
        scale = min(
            target_h / max(1, self._pixmaps[0].height()),
            1.0,
        )
        self._apply_scale(scale)

    def wheelEvent(self, event) -> None:
        """Ctrl+滚轮缩放。"""
        if event.modifiers() & Qt.ControlModifier:
            self._zoom(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom(1)
        elif event.key() == Qt.Key_Minus:
            self._zoom(-1)
        elif event.key() == Qt.Key_0:
            self.fit_to_window()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        # 释放 pixmap：QPixmap 必须在 Qt 活着时释放（Windows 上否则可能 access violation）
        for label in self._labels:
            label.clear()
        self._pixmaps = []
        super().closeEvent(event)


def render_preview_pages(
    pdf_path: Path,
    page_indices: Sequence[int],
    dpi: int = PREVIEW_DPI,
) -> list[QPixmap]:
    """渲染若干页的预览图。

    **所有 fitz 调用只在调用方所在线程内**（本函数同步执行，
    在 GUI 线程内调用时要保证页数少 —— 一张纸最多 2 页，足够快）。
    """
    import pymupdf as fitz

    from core.split_pdf import _to_pymupdf_path
    from ui.thumbnail import pixmap_to_qimage

    pixmaps: list[QPixmap] = []
    doc = fitz.open(_to_pymupdf_path(pdf_path))
    try:
        for index in page_indices:
            pixmap = doc.load_page(index).get_pixmap(
                dpi=dpi, alpha=False, colorspace=fitz.csRGB
            )
            pixmaps.append(QPixmap.fromImage(pixmap_to_qimage(pixmap)))
    finally:
        doc.close()
    return pixmaps
