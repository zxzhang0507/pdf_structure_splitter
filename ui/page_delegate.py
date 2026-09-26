"""纸张卡片绘制委托。

**每张卡片 = 一张纸**（而不是一页）：

* 双面纸并排显示两页缩略图，单面纸居中显示一页
* 每页缩略图底部有一条细色带，表示**该页自身**的检测结果
  （灰 = 黑白，红 = 彩色）—— 这是人工复核时最需要的信息：
  一眼就能看出「这张纸为什么被判成彩色」
* 卡片下方是纸张标签（``P31-32`` / ``P7``）

绘制全部走 QPainter，样式表管不到 item 内部，因此卡片配色在这里定义，
需与 ``style.qss`` 的调色板保持一致。
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

# 调色板（与 style.qss 一致）
CARD_BG = QColor("#FFFFFF")
CARD_BORDER = QColor("#E5E7EB")
CARD_BORDER_HOVER = QColor("#2563EB")
CARD_SELECTED = QColor("#2563EB")
LABEL_COLOR = QColor("#374151")
ERROR_BG = QColor("#F3F4F6")
ERROR_BORDER = QColor("#D1D5DB")
ERROR_TEXT = QColor("#9CA3AF")
MOVED_BADGE = QColor("#D97706")          # 人工调整过的角标
COLOR_STRIP = QColor("#EF4444")          # 该页自身检测为彩色
BW_STRIP = QColor("#9CA3AF")             # 该页自身检测为黑白
#: 该页被判彩色、但依据是**矢量颜色** —— 与位图检出的彩色区分开。
#:
#: 这个区分对复核很有用：矢量彩色是 PDF 里显式声明的确定结论，
#: 而位图彩色是像素统计的结果、可能受阈值影响。扫一眼色带的深浅
#: 就知道哪些页需要重点复核。
VECTOR_STRIP = QColor("#B91C1C")         # 深红 = 彩色矢量（权威结论）
RASTER_STRIP = QColor("#F59E0B")         # 橙 = 位图区域检出彩色像素
#: 该页未经渲染（纯文字页）—— 用低饱和灰把"确定不会有彩色"表达出来
TEXT_ONLY_STRIP = QColor("#D1D5DB")
#: 该页只有灰色矢量（表格线、线条图）—— 比纯文字略深一点：
#: 页面上确实有图形，虽然已由 PDF 声明颜色判定为灰，但复核时值得区分。
GRAY_VECTOR_STRIP = QColor("#A8AFB8")


def strip_color(info) -> QColor:
    """按判定依据给出该页色带的颜色。

    绿色系表示"确定是黑白"（纯文字页 / 位图灰度回落），
    暖色系表示"是彩色"，并用深浅区分依据的可靠程度。
    """
    if not info.is_color:
        reason = getattr(info, "reason", "")
        if reason == "纯文字":
            return TEXT_ONLY_STRIP
        if reason == "黑白矢量":
            return GRAY_VECTOR_STRIP
        return BW_STRIP
    if getattr(info, "reason", "") == "彩色矢量":
        return VECTOR_STRIP
    if getattr(info, "reason", "") == "位图彩色":
        return RASTER_STRIP
    return COLOR_STRIP

#: 基准单页缩略图尺寸（A4 竖版比例约 0.707）。实际尺寸 = 此值 × zoom。
BASE_PAGE_W = 80
BASE_PAGE_H = 113
#: 双面纸两页之间的间距（基准）
BASE_PAGE_GAP = 6
#: 卡片内边距（基准）
BASE_PADDING = 7
#: 检测结果色带高度（基准）
BASE_STRIP_H = 3
#: 纸张标签区高度（基准）
BASE_LABEL_H = 17

#: 缩放范围与步长（Ctrl+滚轮）
MIN_ZOOM = 0.6
MAX_ZOOM = 3.0
ZOOM_STEP = 1.15

#: 兼容旧代码：基准卡片尺寸（缩放为 1.0 时）
CARD_W = BASE_PAGE_W * 2 + BASE_PAGE_GAP + 2 * BASE_PADDING
CARD_H = BASE_PAGE_H + BASE_STRIP_H + 4 + BASE_LABEL_H + 2 * BASE_PADDING


class PageDelegate(QStyledItemDelegate):
    """把每个 item 画成一张**纸张**卡片。支持运行时缩放（Ctrl+滚轮）。"""

    def __init__(self, parent=None, zoom: float = 1.0) -> None:
        super().__init__(parent)
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))

    # ------------------------------------------------------------------
    # 缩放
    # ------------------------------------------------------------------

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))

    def zoom_by(self, factor: float) -> bool:
        """按倍数调整缩放。返回是否**确实发生了变化**。"""
        old = self._zoom
        self.set_zoom(self._zoom * factor)
        return abs(self._zoom - old) > 1e-6

    # 当前生效的尺寸（按 zoom 换算）
    @property
    def page_w(self) -> int:
        return max(12, round(BASE_PAGE_W * self._zoom))

    @property
    def page_h(self) -> int:
        return max(17, round(BASE_PAGE_H * self._zoom))

    @property
    def page_gap(self) -> int:
        return max(2, round(BASE_PAGE_GAP * self._zoom))

    @property
    def padding(self) -> int:
        return max(2, round(BASE_PADDING * self._zoom))

    @property
    def strip_h(self) -> int:
        return max(2, round(BASE_STRIP_H * self._zoom))

    @property
    def label_h(self) -> int:
        return max(10, round(BASE_LABEL_H * self._zoom))

    @property
    def card_size(self) -> QSize:
        w = self.page_w * 2 + self.page_gap + 2 * self.padding
        h = self.page_h + self.strip_h + 4 + self.label_h + 2 * self.padding
        return QSize(w, h)

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return self.card_size

    # ------------------------------------------------------------------

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        slot = index.data(Qt.UserRole)
        if slot is None:
            return

        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        sheet_ok = slot.ok

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        # ---- 卡片底 ----
        painter.setBrush(ERROR_BG if not sheet_ok else CARD_BG)
        if selected:
            painter.setPen(QPen(CARD_SELECTED, 2))
        elif hovered:
            painter.setPen(QPen(CARD_BORDER_HOVER, 1))
        else:
            painter.setPen(QPen(ERROR_BORDER if not sheet_ok else CARD_BORDER, 1))
        painter.drawRoundedRect(rect, 6, 6)

        # ---- 缩略图区 ----
        page_w, page_h = self.page_w, self.page_h
        top = rect.top() + self.padding
        count = len(slot.pages)
        # 拆分后每行只有 1 页，但卡片仍按「可容纳 2 页」的宽度绘制，
        # 这样网格不会因拆分而参差不齐。
        if count == 1:
            x = rect.center().x() - page_w // 2
            self._draw_page(painter, x, top, 0, slot)
        else:
            total = page_w * 2 + self.page_gap
            x0 = rect.left() + (rect.width() - total) // 2
            for offset in range(min(2, count)):
                self._draw_page(
                    painter, x0 + offset * (page_w + self.page_gap), top, offset, slot
                )

        # ---- 纸张标签 ----
        label_rect = QRect(
            rect.left() + self.padding,
            top + page_h + self.strip_h + 2,
            rect.width() - 2 * self.padding,
            self.label_h,
        )
        painter.setPen(LABEL_COLOR)
        font = QFont(painter.font().family(), max(6, round(9 * self._zoom)))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignVCenter, slot.label)

        # ---- 角标 ----
        self._draw_badges(painter, rect, slot)

        painter.restore()

    def _draw_page(self, painter: QPainter, x: int, y: int,
                   page_offset: int, slot) -> None:
        """画一页的缩略图，以及该页自身检测结果的色带。"""
        info = slot.pages[page_offset]
        page_rect = QRect(x, y, self.page_w, self.page_h)

        pixmaps = getattr(slot, "pixmaps", None) or []
        pixmap = pixmaps[page_offset] if page_offset < len(pixmaps) else None
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(
                page_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            px = page_rect.left() + (page_rect.width() - scaled.width()) // 2
            py = page_rect.top() + (page_rect.height() - scaled.height()) // 2
            painter.drawPixmap(px, py, scaled)
        else:
            # 尚未渲染 / 渲染失败
            painter.setPen(QPen(ERROR_BORDER, 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(page_rect, 3, 3)
            painter.setPen(ERROR_TEXT)
            painter.setFont(QFont(painter.font().family(), max(5, round(7 * self._zoom))))
            painter.drawText(
                page_rect,
                Qt.AlignCenter,
                "渲染失败" if not info.ok else "…",
            )

        # 该页自身的检测结果与判定依据。人工复核就看这条色带。
        strip = QRect(
            page_rect.left(), page_rect.bottom() + 1,
            page_rect.width(), self.strip_h,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(strip_color(info))
        painter.drawRect(strip)

    def _draw_badges(self, painter: QPainter, rect: QRect, slot) -> None:
        """右上角：人工调整过 = 橙点。"""
        if not slot.manually_moved:
            return
        r = max(3, round(4 * self._zoom))
        x = rect.right() - r - 5
        y = rect.top() + r + 6
        painter.setPen(Qt.NoPen)
        painter.setBrush(MOVED_BADGE)
        painter.drawEllipse(x - r, y - r, r * 2, r * 2)
