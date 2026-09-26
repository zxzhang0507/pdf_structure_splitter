"""主窗口：工具栏 + 参数面板 + 左右双区 + 状态栏。

P1 阶段只搭骨架与布局，检测/拖拽/导出在后续阶段接入。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QDrag, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

try:
    import pymupdf as fitz
except ImportError:  # pymupdf < 1.24 只有 fitz 这个模块名
    import fitz  # type: ignore[no-redef]

from core import BW, COLOR
from core.split_pdf import _to_pymupdf_path
from ui.help_dialog import HelpDialog
from ui.page_delegate import MAX_ZOOM, MIN_ZOOM, ZOOM_STEP, PageDelegate
from ui.param_panel import ParamPanel
from ui.preview_dialog import PreviewDialog, render_preview_pages
from ui.print_dialog import PrintDialog
from ui.thumbnail import (
    render_thumbnail,
    thumbnail_dpi_for,
    thumbnail_width_for,
)
from ui.thumbnail_model import MIME_TYPE, PageListModel, SideFilterProxy
from workers.detect_worker import DetectWorker
from workers.export_worker import ExportWorker
from workers.print_worker import (
    DEFAULT_PRINT_DPI,
    PrintController,
    build_print_jobs,
    default_output_path,
    is_virtual_printer,
)


def _resource_path(*parts: str) -> Path:
    """定位资源文件，兼容开发态与 PyInstaller 打包态。

    打包后（``--onefile``）资源被解压到 ``sys._MEIPASS``，源码目录已不存在，
    因此必须按 ``_MEIPASS`` 优先查找。
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


def load_stylesheet() -> str:
    """读取 QSS；文件缺失时返回空串而不是崩溃。"""
    for candidate in (
        _resource_path("ui", "style.qss"),
        Path(__file__).with_name("style.qss"),
    ):
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


class ZoneView(QListView):
    """带分区语义的列表视图。

    ## 鼠标拖拽：完全自己实现，不用 Qt 的拖放框架

    前后试过三套基于 ``QDrag`` / ``QMimeData`` 的方案，全部失败：
    即便 ``canDropMimeData`` 返回 True、``dragEnterEvent`` 被接受、
    ``QDrag.exec()`` 也确实被调用，它最终仍返回 ``IgnoreAction`` ——
    放置没有发生。``QListView`` 在 ``IconMode`` + ``setMovement(Static)``
    + 自定义委托的组合下，内建拖放的行为不受控。

    因此改为**纯鼠标事件 + 几何判定**，彻底不依赖 Qt 的 DnD：

    * ``mousePressEvent`` 记下按下的行与位置
    * ``mouseMoveEvent`` 超过阈值后进入「拖拽中」，持续上报全局坐标
      （主窗口据此高亮光标所在的另一个分区）
    * ``mouseReleaseEvent`` 上报落点全局坐标，由主窗口判断落在哪个分区，
      是另一个分区就搬移

    好处是行为**完全确定、可单元测试**（不依赖窗口管理器），
    也不会再被 Qt 内部状态机影响。

    ## 另外两点

    * ``Ctrl+滚轮`` 缩放缩略图
    * 滚轮逐行滚动，而非按像素跳（否则一格掠过好几行，翻页过快）
    """

    #: Ctrl+滚轮缩放时发出，携带缩放倍数因子
    zoom_requested = Signal(float)
    #: 拖拽中光标移动，携带全局坐标（用于高亮目标分区）
    drag_moved = Signal(QPoint)
    #: 拖拽结束并落下，携带 ``(源行号列表, 落点全局坐标)``
    drag_dropped = Signal(list, QPoint)

    def __init__(self, side: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.side = side
        self._zone: ZoneWidget | None = None
        #: 按下位置（超出阈值后清空，避免重复进入拖拽态）
        self._press_pos: QPoint | None = None
        #: 本次拖拽携带的源行号（进入拖拽态后才有值）
        self._drag_rows: list[int] = []

    # ------------------------------------------------------------------
    # 滚轮：Ctrl 缩放 / 普通逐行滚动
    # ------------------------------------------------------------------

    def wheelEvent(self, event) -> None:
        """``Ctrl+滚轮`` → 缩放；普通滚轮 → **逐行**平滑滚动。"""
        if event.modifiers() & Qt.ControlModifier:
            factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP
            self.zoom_requested.emit(factor)
            event.accept()
            return

        # 默认 QListView 在 IconMode 下按像素滚动，一格会跳好几行。
        # 改成「一格滚一行」，翻页速度平缓可控。
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        # 一格通常 120；按方向滚一行（向上为正）
        steps = max(1, abs(delta) // 120)
        self._scroll_by_lines(-steps if delta > 0 else steps)
        event.accept()

    def _scroll_by_lines(self, lines: int) -> None:
        """按「行」滚动。``lines`` 为负表示向上。"""
        bar = self.verticalScrollBar()
        grid_h = max(1, self.gridSize().height() or 1)
        bar.setValue(bar.value() + lines * grid_h)

    # ------------------------------------------------------------------
    # 鼠标拖拽（纯鼠标事件实现）
    # ------------------------------------------------------------------

    #: 判定「开始拖拽」的像素阈值
    DRAG_THRESHOLD = 8

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._drag_rows = []
            # 三种情况**不要**调用 setCurrentIndex（它会把选中集合缩成一项）：
            #   1. 按住 Ctrl / Shift —— 那是多选操作
            #   2. 按在一个**已被选中**的卡片上 —— 用户多半是要拖动整批选中的卡片
            multi = bool(
                event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
            )
            index = self.indexAt(self._press_pos)
            already = bool(
                index.isValid() and self.selectionModel().isSelected(index)
            )
            if not multi and not already:
                if index.isValid():
                    self.setCurrentIndex(index)
        else:
            self._press_pos = None
            self._drag_rows = []
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """按下左键并移动超过阈值 → 进入拖拽态。"""
        if self._press_pos is not None and event.buttons() & Qt.LeftButton:
            moved = (event.position().toPoint() - self._press_pos).manhattanLength()
            if moved >= self.DRAG_THRESHOLD:
                rows = self._selected_source_rows() or self._rows_under(self._press_pos)
                self._press_pos = None          # 只进入一次
                if rows:
                    self._drag_rows = rows
                    self.viewport().setCursor(Qt.ClosedHandCursor)
                    self.drag_moved.emit(event.globalPosition().toPoint())
                    return
        if self._drag_rows:
            # 拖拽中：持续上报位置让目标分区高亮
            self.drag_moved.emit(event.globalPosition().toPoint())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """在拖拽态下松手 → 上报落点，由主窗口决定搬去哪。"""
        if self._drag_rows and event.button() == Qt.LeftButton:
            rows, self._drag_rows = self._drag_rows, []
            self._press_pos = None
            self.viewport().unsetCursor()
            self.drag_dropped.emit(rows, event.globalPosition().toPoint())
            event.accept()
            return
        self._press_pos = None
        self._drag_rows = []
        super().mouseReleaseEvent(event)

    def cancel_drag(self) -> None:
        """放弃当前拖拽（例如按下 Esc）。"""
        self._drag_rows = []
        self._press_pos = None
        self.viewport().unsetCursor()

    def _rows_under(self, pos: QPoint) -> list[int]:
        """返回 ``pos`` 位置对应的源模型行号（没有则空列表）。"""
        model = self.model()
        if model is None or not hasattr(model, "mapToSource"):
            return []
        index = self.indexAt(pos)
        if not index.isValid():
            return []
        row = model.mapToSource(index).row()
        return [row] if row >= 0 else []

    def _selected_source_rows(self) -> list[int]:
        """当前选中的**源模型行号**。"""
        model = self.model()
        if model is None or not hasattr(model, "mapToSource"):
            return []
        rows = {
            model.mapToSource(i).row()
            for i in self.selectionModel().selectedIndexes()
            if i.isValid()
        }
        return sorted(rows)

    # ------------------------------------------------------------------
    # 拖放目标（仍保留 Qt 的 enter/leave 用于高亮；实际搬移走上面的手工路径）
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if self._zone is not None:
            self._zone.set_drag_active(True)
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:
        if self._zone is not None:
            self._zone.set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        if self._zone is not None:
            self._zone.set_drag_active(False)
        model = self.model()
        if model is not None and hasattr(model, "sourceModel"):
            src = model.sourceModel()
            if hasattr(src, "set_drop_target_side"):
                src.set_drop_target_side(self.side)
        super().dropEvent(event)
        self.viewport().update()

    # ------------------------------------------------------------------
    # 拖放目标
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if self._zone is not None:
            self._zone.set_drag_active(True)
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:
        if self._zone is not None:
            self._zone.set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        if self._zone is not None:
            self._zone.set_drag_active(False)
        # 告知模型这次放置属于哪个分区（dropMimeData 拿不到视图信息）
        model = self.model()
        if model is not None and hasattr(model, "sourceModel"):
            src = model.sourceModel()
            if hasattr(src, "set_drop_target_side"):
                src.set_drop_target_side(self.side)
        super().dropEvent(event)
        # 拖完清掉高亮状态
        self.viewport().update()


class ZoneWidget(QWidget):
    """一个分区：标题栏（名称 + 页数 + 操作提示）+ 缩略图视图。"""

    #: 各分区的操作提示（标题栏第二行的小字）
    HINT = "拖动卡片到另一侧，或选中后按 ← → 搬移　·　Ctrl 点击可多选"

    def __init__(self, kind: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind

        self.title_label = QLabel(title)
        self.title_label.setObjectName("ZoneTitle")
        self.count_label = QLabel("0 页")
        self.count_label.setObjectName("ZoneCount")

        # 标题行：标题 + 弹性空隙 + 页数
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)
        top_row.addWidget(self.title_label)
        top_row.addStretch(1)
        top_row.addWidget(self.count_label)

        # 提示行：把「怎么搬移」直接写在标题栏，用户不用翻帮助
        self.hint_label = QLabel(self.HINT)
        self.hint_label.setObjectName("ZoneHint")
        self.hint_label.setWordWrap(True)

        header = QWidget()
        # 左侧色条由 ZoneHeader + zone 属性共同决定（QSS 的属性选择器）
        header.setObjectName("ZoneHeader")
        header.setProperty("zone", kind)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(12, 7, 12, 8)
        header_layout.setSpacing(2)
        header_layout.addLayout(top_row)
        header_layout.addWidget(self.hint_label)

        self.view = ZoneView(kind)
        self.view._zone = self
        self.view.setObjectName("ZoneView")
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(8)
        # 扩展选择：配合 Ctrl 可多选，一次搬移多张纸。
        # 跨区仍互斥（见 MainWindow._on_zone_selection_changed），
        # 因此不会出现「两个区各选一张、一次搬两处」的歧义。
        self.view.setSelectionMode(QListView.ExtendedSelection)
        self.view.setWordWrap(True)
        self.view.setMouseTracking(True)      # 悬停高亮需要
        self.delegate = PageDelegate()
        self.view.setItemDelegate(self.delegate)
        self.view.setGridSize(self.delegate.card_size)

        # 拖拽
        self.view.setDragEnabled(True)
        self.view.setAcceptDrops(True)
        self.view.setDropIndicatorShown(True)
        self.view.setDefaultDropAction(Qt.MoveAction)
        self.view.setDragDropMode(QListView.DragDrop)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.view, 1)

    def set_count(self, count: int, upgraded: int = 0) -> None:
        """``upgraded``: 因双面配对被升级到本区的页数（仅彩色区有意义）。"""
        self._base_count_text = f"{count} 页"
        if upgraded:
            self._base_count_text += f"（含 {upgraded} 页因配对升级）"
        self._render_count()

    def set_selected_count(self, selected_pages: int) -> None:
        """把「已选 N 页」显示在页数旁边，让多选结果一目了然。"""
        self._selected_pages = max(0, selected_pages)
        self._render_count()

    def _render_count(self) -> None:
        """按「页数」与「已选」拼出标题栏计数文本。"""
        base = getattr(self, "_base_count_text", "0 页")
        selected = getattr(self, "_selected_pages", 0)
        if selected:
            self.count_label.setText(f"{base}　·　已选 {selected} 页")
            self.count_label.setProperty("selected", "true")
        else:
            self.count_label.setText(base)
            self.count_label.setProperty("selected", "false")
        # 属性选择器需要手动触发重绘
        self.count_label.style().unpolish(self.count_label)
        self.count_label.style().polish(self.count_label)

    def set_drag_active(self, active: bool) -> None:
        """拖拽悬停时给分区加虚线高亮边框（配合 QSS 的属性选择器）。"""
        self.view.setProperty("dragActive", "true" if active else "false")
        # 属性选择器需要手动触发重绘
        self.view.style().unpolish(self.view)
        self.view.style().polish(self.view)


class MainWindow(QMainWindow):
    #: 导出完成后发出，携带输出目录（无头测试用，避免依赖模态对话框）
    export_completed = Signal(str)
    #: 打印完成后发出，携带摘要文本（无头测试用）
    print_completed = Signal(str)
    #: 打印失败时发出
    print_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("pdf_structure_splitter · PDF 结构优先彩色 / 黑白分类")
        self.resize(1180, 780)
        self.setMinimumSize(900, 600)

        icon_path = _resource_path("resources", "app.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # ---- 状态 ----
        self._doc: "fitz.Document | None" = None      # 由 worker 线程独占，主线程不碰
        self._pdf_path: Path | None = None
        self._sheet_count = 0
        self._worker: DetectWorker | None = None
        self._thumb_pending: list[int] = []
        #: 无头测试时置 True，跳过会阻塞的模态对话框
        self._suppress_dialogs = False
        #: 缩略图渲染失败累计次数（用于状态栏提示，不静默忽略）
        self._thumb_failures = 0
        #: 最近一次检测的完整结果（导出写报告用）
        self._infos: list = []
        self._sheets: list = []
        #: 正在程序化清除另一区的选中（防止 selectionChanged 递归）
        self._clearing_selection = False
        self._export_worker: "ExportWorker | None" = None
        #: 本次导出是否输出了 CSV（启动时记录，供完成提示使用）
        self._export_wrote_csv = False
        #: 打印控制器（主线程分帧执行，见 workers/print_worker.py）
        self._printer: "PrintController | None" = None

        # ---- 数据模型 ----
        self.model = PageListModel(self)
        self.bw_proxy = SideFilterProxy(BW, self)
        self.color_proxy = SideFilterProxy(COLOR, self)
        self.bw_proxy.setSourceModel(self.model)
        self.color_proxy.setSourceModel(self.model)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())
        self.param_panel = ParamPanel()
        root.addWidget(self.param_panel)
        root.addWidget(self._build_zones(), 1)
        root.addWidget(self._build_status_bar())

        self.setCentralWidget(central)

        # ---- 信号连接 ----
        self.open_button.clicked.connect(self.on_open)
        self.export_button.clicked.connect(self.on_export)
        self.print_button.clicked.connect(self.on_print)
        self.param_panel.params_changed.connect(self.on_params_changed)
        self.param_panel.strict_duplex_changed.connect(self.on_strict_changed)
        self.param_panel.single_sided_changed.connect(self.on_single_sided_changed)
        self.model.side_changed.connect(self.on_side_changed)

        # 缩略图懒加载：滚动时才渲染可视区的页
        self.bw_zone.view.verticalScrollBar().valueChanged.connect(
            self._schedule_thumbnails
        )
        self.color_zone.view.verticalScrollBar().valueChanged.connect(
            self._schedule_thumbnails
        )

        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(60)
        self._thumb_timer.timeout.connect(self._render_visible_thumbnails)

        # 两侧都用同一个「双击 → 预览」的监听
        for zone in (self.bw_zone, self.color_zone):
            zone.view.doubleClicked.connect(self.on_preview_requested)
            zone.view.setContextMenuPolicy(Qt.CustomContextMenu)
            zone.view.customContextMenuRequested.connect(self.on_zone_context_menu)
            zone.view.zoom_requested.connect(self.on_zoom_requested)
            # 手工拖拽：移动时高亮目标分区，松手时搬移
            zone.view.drag_moved.connect(self._on_drag_moved)
            zone.view.drag_dropped.connect(self._on_drag_dropped)
            # 跨区互斥选择：选中一区时清掉另一区的选中
            zone.view.selectionModel().selectionChanged.connect(
                self._on_zone_selection_changed
            )

        self._build_shortcuts()

    # ------------------------------------------------------------------
    # 鼠标拖拽搬移（纯几何判定，不依赖 Qt 拖放框架）
    # ------------------------------------------------------------------

    def _zone_at_global(self, global_pos: QPoint) -> "ZoneWidget | None":
        """全局坐标落在哪个分区内（不在任何分区则 None）。"""
        for zone in (self.bw_zone, self.color_zone):
            view = zone.view
            local = view.viewport().mapFromGlobal(global_pos)
            if view.viewport().rect().contains(local):
                return zone
        return None

    def _on_drag_moved(self, global_pos: QPoint) -> None:
        """拖拽过程中：高亮光标所在的分区。"""
        hovered = self._zone_at_global(global_pos)
        for zone in (self.bw_zone, self.color_zone):
            zone.set_drag_active(zone is hovered)

    def _on_drag_dropped(self, rows: list, global_pos: QPoint) -> None:
        """拖拽落下：若落在**另一个**分区上，就把这些行搬过去。"""
        for zone in (self.bw_zone, self.color_zone):
            zone.set_drag_active(False)

        if not rows or self._pdf_path is None:
            return

        target = self._zone_at_global(global_pos)
        if target is None:
            self.set_status("拖到了分区外，未做改动")
            return

        source = self.sender()
        # 落在同一区：什么都不做（不报错，符合直觉）
        if source is target.view:
            return

        side = target.kind        # ZoneWidget.kind 就是 BW / COLOR
        if self.model.move_sheets(rows, side):
            moved_pages = sum(
                len(self.model.slot_at(r).pages)
                for r in rows
                if self.model.slot_at(r) is not None
            )
            zone_name = "彩色区" if side == COLOR else "黑白区"
            self.set_status(f"已把 {moved_pages} 页拖到{zone_name}")
            self._after_move()

    def _after_move(self) -> None:
        """搬移后的界面收尾（计数、过滤、缩略图）。"""
        self._refresh_counts()
        self.proxy_invalidate()
        self._schedule_thumbnails()

    # ------------------------------------------------------------------
    # 选中状态（跨区互斥 + 多选计数）
    # ------------------------------------------------------------------

    def _on_zone_selection_changed(self, selected, _deselected) -> None:
        """一个区产生选中时，清空**另一个**区的选中，并刷新选中计数。

        跨区互斥的原因：用户可能在黑白区选几张、彩色区再选几张，
        此时按 ``←``/``→`` 该往哪边搬就有歧义。限制为「同一时刻只操作一个区」
        后，搬移目标唯一、符合直觉 —— 而**同一区内**仍可 Ctrl 多选。

        （``Ctrl`` 点在已选卡片上会取消它，于是可能产生「空选中」的变化；
        这里只处理有选中的情况，空选中交给下方计数逻辑即可。）
        """
        if not self._clearing_selection:
            sender = self.sender()
            others = [
                zone.view.selectionModel()
                for zone in (self.bw_zone, self.color_zone)
                if zone.view.selectionModel() is not sender
            ]
            if any(m.selectedIndexes() for m in others):
                self._clearing_selection = True
                try:
                    for model in others:
                        model.clearSelection()
                finally:
                    self._clearing_selection = False

        self._update_selection_status()

    def _update_selection_status(self) -> None:
        """把「已选中 N 页」显示到标题栏与状态栏。"""
        rows = self._selected_source_rows()
        pages = sum(
            len(self.model.slot_at(r).pages)
            for r in rows
            if self.model.slot_at(r) is not None
        )
        # 标题栏：让多选结果一眼可见
        for zone, proxy in (
            (self.bw_zone, self.bw_proxy),
            (self.color_zone, self.color_proxy),
        ):
            sel = {
                proxy.mapToSource(i).row()
                for i in zone.view.selectionModel().selectedIndexes()
                if i.isValid()
            }
            zone_pages = sum(
                len(self.model.slot_at(r).pages)
                for r in sel
                if self.model.slot_at(r) is not None
            )
            zone.set_selected_count(zone_pages)

        if pages:
            self.set_status(
                f"已选中 {pages} 页（{len(rows)} 张纸）"
                "　·　按 ← → 或拖动可整体搬移，Delete 复位"
            )

    # ------------------------------------------------------------------
    # 缩略图缩放（Ctrl+滚轮）
    # ------------------------------------------------------------------

    def on_zoom_requested(self, factor: float) -> None:
        """两侧一起缩放，保持左右视觉一致。

        放大后原来的缩略图分辨率就不够了（会发虚），因此这里把
        **清晰度已不足的缩略图丢掉**，让 :meth:`_render_visible_thumbnails`
        按新的缩放级别重渲 —— 缩小则保留（多余的像素不算浪费，
        用户可能马上又放大回去）。
        """
        zoom = self.bw_zone.delegate.zoom * factor
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if abs(zoom - self.bw_zone.delegate.zoom) < 1e-6:
            return

        dropped = self.model.drop_thumbnails_below(zoom)
        for zone in (self.bw_zone, self.color_zone):
            delegate = zone.delegate
            delegate.set_zoom(zoom)
            # 卡片尺寸变了，必须让视图重新计算布局
            zone.view.setGridSize(delegate.card_size)
            zone.view.reset()

        self._thumb_timer.start()
        extra = f"，重渲 {dropped} 页" if dropped else ""
        self.set_status(f"缩略图缩放 {zoom * 100:.0f}%（Ctrl+滚轮调整）{extra}")

    # ------------------------------------------------------------------
    # 右键菜单：拆分 / 合并单页
    # ------------------------------------------------------------------

    def on_zone_context_menu(self, pos) -> None:
        """分区卡片上的右键菜单。

        「拆分为单页」只在**非严格模式**下可用 —— 严格模式下拆纸会破坏
        本程序的核心规则，因此不给入口。
        """
        view = self.sender()
        if view is None:
            return
        proxy = self.bw_proxy if view is self.bw_zone.view else self.color_proxy
        index = view.indexAt(pos)
        if not index.isValid():
            return
        source_row = proxy.mapToSource(index).row()
        slot = self.model.slot_at(source_row)
        if slot is None:
            return

        # 右键的那一行若是选中集合的一部分，则对整批操作
        selected = self._selected_source_rows()
        rows = selected if source_row in selected and len(selected) > 1 else [source_row]

        menu = QMenu(self)

        if self.model.can_split(source_row):
            action = menu.addAction("拆分为单页")
            action.setToolTip("拆开后两页可分别搬到黑白区 / 彩色区")
        if self.model.can_merge(rows):
            menu.addAction("合并回一张纸")

        menu.addSeparator()
        preview = menu.addAction(f"预览 P{slot.pages[0].number}")
        preview.setEnabled(True)

        if menu.isEmpty():
            return
        chosen = menu.exec(view.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        text = chosen.text()
        if text == "拆分为单页":
            count = self.model.split_slots(rows)
            if count:
                self._refresh_counts()
                self.proxy_invalidate()
                self.set_status(f"已把 {count} 张纸拆分为单页，可分别搬移")
        elif text == "合并回一张纸":
            if self.model.merge_slots(rows):
                self._refresh_counts()
                self.proxy_invalidate()
                self.set_status("已把拆开的页面合并回一张纸")
        elif text.startswith("预览"):
            self._show_preview(source_row, 0)

    # ------------------------------------------------------------------
    # 键盘快捷键（可访问性）
    # ------------------------------------------------------------------

    def _build_shortcuts(self) -> None:
        """全局快捷键。

        ``Ctrl+O`` 打开 / ``Ctrl+E`` 导出 / ``Ctrl+P`` 打印 /
        ``←`` ``→`` 搬移 / ``Ctrl+A`` 全选 / ``Delete`` 复位 / ``空格`` 预览。

        用 ``ApplicationShortcut`` 上下文，保证焦点在缩略图列表里时也能触发。
        """
        def add(seq: str, slot) -> None:
            shortcut = QShortcut(QKeySequence(seq), self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(slot)

        add("Ctrl+O", self.on_open)
        add("Ctrl+E", self.on_export)
        add("Ctrl+P", self.on_print)
        add("F1", self.on_help)
        add("Ctrl+A", self.select_all_current_zone)
        add("Delete", self.reset_selected)
        add("Left", lambda: self.move_selected_to(BW))
        add("Right", lambda: self.move_selected_to(COLOR))
        add("Space", self.preview_selected)

    # ------------------------------------------------------------------
    # 快捷键动作
    # ------------------------------------------------------------------

    def _focused_zone(self) -> "ZoneWidget | None":
        """返回当前获得焦点的分区；都没有则返回 None。"""
        for zone in (self.bw_zone, self.color_zone):
            if zone.view.hasFocus():
                return zone
        return None

    def _selected_source_rows(self) -> list[int]:
        """当前分区里选中的**源模型行号**（纸张行号），跨两个区合并。"""
        rows: set[int] = set()
        for zone, proxy in ((self.bw_zone, self.bw_proxy), (self.color_zone, self.color_proxy)):
            for idx in zone.view.selectionModel().selectedIndexes():
                if idx.isValid():
                    rows.add(proxy.mapToSource(idx).row())
        return sorted(rows)

    def select_all_current_zone(self) -> None:
        zone = self._focused_zone()
        if zone is None:
            return
        zone.view.selectAll()

    def move_selected_to(self, side: str) -> None:
        """把选中的纸张搬到指定分区（等价于拖拽）。

        搬移后 Qt 的过滤视图会清空选中项（行已不在本区），
        这里主动把选中项转移到**目标区**，让「搬过去 → 按 Delete 撤销」
        这样的连续操作符合直觉。
        """
        rows = self._selected_source_rows()
        if not rows or self._pdf_path is None:
            return
        if not self.model.move_sheets(rows, side):
            return

        zone_name = "彩色区" if side == COLOR else "黑白区"
        moved_pages = sum(len(self.model.slot_at(r).pages) for r in rows)
        self.set_status(f"已把 {moved_pages} 页搬到{zone_name}")

        target_zone = self.color_zone if side == COLOR else self.bw_zone
        target_proxy = self.color_proxy if side == COLOR else self.bw_proxy
        selection = target_zone.view.selectionModel()
        selection.clearSelection()
        moved = set(rows)
        for proxy_row in range(target_proxy.rowCount()):
            if target_proxy.mapToSource(target_proxy.index(proxy_row, 0)).row() in moved:
                selection.select(
                    target_proxy.index(proxy_row, 0),
                    selection.SelectionFlag.Select | selection.SelectionFlag.Rows,
                )
        target_zone.view.setFocus()

    def reset_selected(self) -> None:
        """把选中的纸张复位到检测的原始结果。"""
        rows = self._selected_source_rows()
        if not rows:
            return
        if self.model.reset_to_detected(rows):
            self.set_status("已把选中页复位到检测结果")

    def preview_selected(self) -> None:
        rows = self._selected_source_rows()
        if rows:
            self._show_preview(rows[0], 0)

    def on_preview_requested(self, index) -> None:
        """双击某张卡片 → 打开该纸第一页的预览大图。"""
        zone = self.sender()
        proxy = self.bw_proxy if zone is self.bw_zone.view else self.color_proxy
        self._show_preview(proxy.mapToSource(index).row(), 0)

    def _show_preview(self, row: int, page_offset: int = 0) -> None:
        """弹预览大图，**一张纸的两页都显示**（左右并排）。

        只显示第一页会让用户无法核对配对页（例如 P25 看着是黑白，
        但 P26 才是真正带彩图的那面）。因此这里把该行所有页一起渲染。
        """
        slot = self.model.slot_at(row)
        if slot is None or self._pdf_path is None:
            return
        if not 0 <= page_offset < len(slot.pages):
            return

        indices = [p.index for p in slot.pages]
        try:
            pixmaps = render_preview_pages(self._pdf_path, indices)
        except Exception as exc:  # noqa: BLE001 - 预览失败不该崩窗口
            QMessageBox.warning(
                self, "预览失败",
                f"无法渲染第 {slot.label} 页：{exc}",
            )
            return

        titles = [
            f"第 {p.number} 页 · 检测 {p.page_color}"
            + (f"（{p.reason}）" if p.reason else "")
            + (f" 占比 {p.color_ratio:.4f}" if p.rendered else " 未渲染")
            for p in slot.pages
        ]
        captions = [
            f"第 {slot.sheet_number} 张纸 · 整行归属 {slot.side}"
            + ("（已拆分）" if slot.split else "")
            + (" · 已人工调整" if slot.manually_moved else ""),
            "Ctrl+滚轮 或 +/− 缩放 · 0 适应窗口",
        ]
        if not slot.ok:
            captions.insert(0, "⚠ 该行有页面渲染失败")

        dialog = PreviewDialog(
            pixmaps, titles, captions, parent=self,
            window_title=f"预览 · 第 {slot.sheet_number} 张纸（{slot.label}）",
        )
        dialog.fit_to_window()
        dialog.exec()
        dialog.deleteLater()

    # ------------------------------------------------------------------
    # 布局构建
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("ToolBar")
        bar.setFixedHeight(52)

        title = QLabel("📄 pdf_structure_splitter")
        title.setObjectName("AppTitle")
        subtitle = QLabel("结构优先：纯文字页直接判黑白，彩色矢量读声明颜色，位图只渲染图形区域")
        subtitle.setObjectName("AppSubtitle")

        self.open_button = QPushButton("打开 PDF")
        self.open_button.setObjectName("PrimaryButton")
        self.open_button.setMinimumWidth(96)

        self.export_button = QPushButton("导出")
        self.export_button.setMinimumWidth(80)
        self.export_button.setEnabled(False)      # 检测完成前不可用

        # 导出选项：是否额外输出 CSV 报告。
        # 默认**不勾选** —— 多数场景只要两个 PDF 去打印；CSV 是给需要
        # 逐页核对或留档的场景准备的。放在「导出」按钮旁边，作用域一目了然。
        self.csv_box = QCheckBox("输出CSV报告")
        self.csv_box.setChecked(False)
        self.csv_box.setObjectName("CsvOption")
        self.csv_box.setToolTip(
            "勾选后，导出时额外生成 <文件名>_report.csv（逐页检测报告）。\n"
            "默认不生成。"
        )

        self.print_button = QPushButton("打印")
        self.print_button.setMinimumWidth(80)
        self.print_button.setEnabled(False)       # 检测完成前不可用
        self.print_button.setToolTip(
            "把彩色件与黑白件分别送到两台打印机（人工复核结果为准）"
        )

        self.help_button = QPushButton("帮助")
        self.help_button.setMinimumWidth(72)
        self.help_button.setToolTip("查看使用说明与快捷键（F1）")
        self.help_button.clicked.connect(self.on_help)

        self.reset_button = QPushButton("恢复默认")
        self.reset_button.setMinimumWidth(88)
        self.reset_button.clicked.connect(self.param_panel_reset)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(self.help_button)
        layout.addWidget(self.reset_button)
        layout.addWidget(self.print_button)
        layout.addWidget(self.csv_box)
        layout.addWidget(self.export_button)
        layout.addWidget(self.open_button)
        return bar

    def on_help(self) -> None:
        """打开帮助页面。"""
        dialog = HelpDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def param_panel_reset(self) -> None:
        self.param_panel.reset_to_defaults()

    def _build_zones(self) -> QWidget:
        self.bw_zone = ZoneWidget(BW, "黑白区")
        self.color_zone = ZoneWidget(COLOR, "彩色区")

        self.bw_zone.view.setModel(self.bw_proxy)
        self.color_zone.view.setModel(self.color_proxy)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.bw_zone)
        splitter.addWidget(self.color_zone)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([570, 570])
        self.splitter = splitter
        return splitter

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(30)

        self.status_label = QLabel("就绪 · 请先打开一个 PDF 文件")
        self.status_label.setObjectName("StatusLabel")

        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.hide()          # 空闲时不占视觉空间

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.progress)
        return bar

    # ------------------------------------------------------------------
    # 状态辅助
    # ------------------------------------------------------------------

    def set_status(self, text: str, level: str = "normal") -> None:
        """level: normal | warning | error"""
        self.status_label.setText(text)
        self.status_label.setObjectName(
            {"warning": "StatusWarning", "error": "StatusError"}.get(
                level, "StatusLabel"
            )
        )
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.progress.hide()
            return
        self.progress.show()
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _refresh_counts(self) -> None:
        """更新两侧页数。

        分区里展示的是**页面自身的检测结果**（拖拽的直接对象）；
        状态栏额外说明**整张纸归属**的结果 —— 后者才是真正决定打印的。
        """
        n_bw = self.model.count_of(BW)
        n_color = self.model.count_of(COLOR)
        # 因双面配对而离开本区的页数。
        # 注意行的单位是**一张纸**（``SheetSlot``），判断该纸自身是否含彩色页
        # 要用 ``Slot.any_color``；该纸内部各页的归属必然一致。
        slots = (self.model.slot_at(r) for r in range(self.model.rowCount()))
        upgraded = sum(
            len(s.pages)
            for s in slots
            if s is not None and s.side == COLOR and not s.any_color
        )
        downgraded = 0
        if not self.model.strict_duplex:
            downgraded = sum(
                len(s.pages)
                for s in (self.model.slot_at(r) for r in range(self.model.rowCount()))
                if s is not None and s.side == BW and s.any_color
            )
        self.bw_zone.set_count(n_bw, downgraded)
        self.color_zone.set_count(n_color, upgraded)

    # ------------------------------------------------------------------
    # 打开文件与检测
    # ------------------------------------------------------------------

    def on_open(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择 PDF 文件", "", "PDF 文件 (*.pdf);;所有文件 (*)"
        )
        if not path_str:
            return
        self.load_pdf(Path(path_str))

    def load_pdf(self, path: Path) -> None:
        """装载 PDF 并启动检测。"""
        self._stop_worker()
        self._pdf_path = path
        self.model.clear()
        self._refresh_counts()
        self.export_button.setEnabled(False)
        self.print_button.setEnabled(False)
        self.set_progress(0, 0)
        self.set_status(f"正在检测 {path.name} …")
        self._start_detect()

    def _start_detect(self) -> None:
        if self._pdf_path is None:
            return
        worker = DetectWorker(
            self._pdf_path,
            self.param_panel.dpi,
            self.param_panel.color_threshold,
            self.param_panel.color_ratio,
            parent=self,
        )
        worker.page_done.connect(self.on_page_done)
        worker.progress.connect(self.set_progress)
        worker.finished_ok.connect(self.on_detect_finished)
        worker.failed.connect(self.on_detect_failed)
        # 只连一次 deleteLater，且**不要**在 _stop_worker 里再调一次，
        # 否则 C++ 对象会被删除两次，Python 侧持有的引用随即成为野指针
        # （实测会触发 access violation）。
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_finished(self) -> None:
        """线程结束后清引用并交给 Qt 回收。

        ``sender()`` 在被删除过程中不可靠，因此用捕获不到的 **裸引用比较**：
        只有在仍指向当前 worker 时才清空 self._worker。
        """
        worker = self.sender()
        if self._worker is worker:
            self._worker = None
        if isinstance(worker, QThread):
            worker.deleteLater()

    def _stop_worker(self) -> None:
        """取消正在跑的检测线程。滑块频繁调整时会反复调用。

        注意：这里**不调用** ``deleteLater()``（由 ``_on_worker_finished`` 负责），
        只负责请求中断并等待线程真正结束，避免「线程还在跑但对象已删」。
        """
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        try:
            if worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(5000):
                    worker.terminate()
                    worker.wait(2000)
        except RuntimeError:
            # C++ 对象已被销毁（正常回收时序），无需处理
            return

    def on_page_done(self, info) -> None:
        """单页检测完成的增量反馈（进度由 ``progress`` 信号负责）。

        模型的权威数据由 ``on_detect_finished`` 用完整结果一次性装载：
        行单位是**一张纸**，而检测是逐*页*完成的，中途无法可靠地拼出纸张分组
        （配对页可能尚未检测）。因此这里只做轻量提示，不碰模型。
        """
        if info.number % 16 == 0:
            self.set_status(
                f"检测中 … {info.number} 页"
                + ("" if info.ok else "（有页面渲染失败）")
            )

    def on_detect_finished(self, infos, sheets) -> None:
        self._sheet_count = len(sheets)
        # 导出需要「原始检测结果」写报告（page_color 列）与纸张信息
        self._infos = list(infos)
        self._sheets = list(sheets)

        # 模型按**当前模式**分组：双面=一张纸一行，单面=一页一行。
        # set_pages 内部会读 _single_sided，因此这里不用额外分支。
        self.model.set_pages(infos)
        self._refresh_counts()
        self.proxy_invalidate()

        self.export_button.setEnabled(bool(infos))
        self.print_button.setEnabled(bool(infos))
        self.set_progress(0, 0)

        n_color = self.model.count_of(COLOR)
        n_bw = self.model.count_of(BW)
        failed = sum(1 for i in infos if not i.ok)
        mode = self.model.mode_label
        if self.model.single_sided:
            counts = f"{len(infos)} 页 · 彩色 {n_color} 页 · 黑白 {n_bw} 页"
        else:
            counts = (
                f"{len(infos)} 页 / {self._sheet_count} 张纸 · "
                f"彩色 {n_color} 页 · 黑白 {n_bw} 页"
            )

        # 把"多少页真的渲染了"报出来 —— 这是新策略最直观的收益，
        # 也让用户理解为什么这份 PDF 检测得比预期快。
        rendered = sum(1 for i in infos if i.rendered)
        saved = (
            f" · 仅 {rendered} 页需渲染（省下 {len(infos) - rendered} 页）"
            if rendered < len(infos) else ""
        )

        self.set_status(
            f"{self._pdf_path.name} · {counts} · {mode}{saved}"
            + (f" · ⚠ {failed} 页渲染失败" if failed else "")
        )
        self._schedule_thumbnails()

    def on_detect_failed(self, message: str) -> None:
        self.set_progress(0, 0)
        self.export_button.setEnabled(False)
        self.print_button.setEnabled(False)
        self.set_status("检测失败", "error")
        QMessageBox.critical(self, "检测失败", message)

    # ------------------------------------------------------------------
    # 参数变化
    # ------------------------------------------------------------------

    def on_params_changed(self, dpi: int, threshold: int, ratio: float) -> None:
        """用户点了「重新检测」（滑块本身不会自动触发）。"""
        if self._pdf_path is None:
            return
        self.set_status(f"参数已更新（DPI {dpi} / 阈值 {threshold} / 占比 {ratio:.4f}），重新检测…")
        self._stop_worker()
        self.model.clear()
        self._refresh_counts()
        self.export_button.setEnabled(False)
        self.print_button.setEnabled(False)
        self._start_detect()

    def on_strict_changed(self, enabled: bool) -> None:
        """检测模式变化（严格双面 ↔ 单面），两者互斥。

        切换后**立即按新模式重新分组当前数据**，让用户马上看到效果：
        切到单面模式时，原本合并的 `P25-26` 会立刻变成两张独立的 `P25`、`P26`。
        这个重分组不需要重新检测 —— 每页自身的检测结果在检测阶段
        就已逐页算好，单面模式直接用 ``is_color`` 即可。
        点「重新检测」则是用当前滑块参数把检测整个重跑一遍。
        """
        self.model.set_strict_duplex(enabled)
        self._apply_mode_change(dirty=True)

    def on_single_sided_changed(self, enabled: bool) -> None:
        """单面模式开关（与严格双面互斥）。"""
        self.model.set_single_sided(enabled)
        self._apply_mode_change(dirty=True)

    def _apply_mode_change(self, dirty: bool = True) -> None:
        """模式变化后的界面刷新。"""
        self._refresh_counts()
        self.proxy_invalidate()
        # 分组变了，缩略图的 (row, offset) 映射也变了，缓存的图要重挂
        self._resync_thumbnails()
        self._schedule_thumbnails()

        if self._pdf_path is None:
            return
        if self.model.single_sided:
            self.set_status(
                "单面模式：每页独立判定（同一张纸的两页可分属不同输出）"
            )
        else:
            self.set_status(
                "严格双面模式：同一张纸的两页始终一起走"
                if self.model.strict_duplex
                else "⚠ 已关闭配对：同一张纸的两页可分属不同输出"
            )

    def _resync_thumbnails(self) -> None:
        """重新分组后，把已渲染的缩略图按**页 index** 重新挂到新行上。

        行数变了（双面 66 行 ↔ 单面 132 行），直接丢弃重渲会让用户
        每次切模式都等一遍全屏重绘。这里按 index 迁移，能省下大部分渲染。
        """
        bucket: dict[int, QPixmap] = {}
        for slot in range(self.model.rowCount()):
            s = self.model.slot_at(slot)
            if s is None:
                continue
            for offset, pixmap in enumerate(s.pixmaps):
                if pixmap is not None and offset < len(s.pages):
                    bucket[s.pages[offset].index] = pixmap

        self.model.release_pixmaps()
        for row in range(self.model.rowCount()):
            s = self.model.slot_at(row)
            if s is None:
                continue
            for offset, info in enumerate(s.pages):
                pixmap = bucket.get(info.index)
                if pixmap is not None:
                    self.model.set_page_pixmap(row, offset, pixmap)

    def on_side_changed(self, rows: list) -> None:
        self._refresh_counts()
        self.proxy_invalidate()
        moved = self.model.moved_count()
        if moved:
            if self.model.single_sided:
                note = "（单面模式：逐页独立）"
            elif self.model.strict_duplex:
                note = "（严格双面模式：配对页已联动）"
            else:
                note = "（⚠ 非严格模式）"
            self.set_status(f"已人工调整 {moved} 页{note}")

    def proxy_invalidate(self) -> None:
        self.bw_proxy.invalidateFilter()
        self.color_proxy.invalidateFilter()

    # ------------------------------------------------------------------
    # 缩略图懒加载
    # ------------------------------------------------------------------

    def _schedule_thumbnails(self, _value: int = 0) -> None:
        self._thumb_timer.start()

    def _render_visible_thumbnails(self) -> None:
        """只为当前可视区**尚缺缩略图**的页面渲染，滚动到位才渲染（懒加载）。

        行的单位是一张纸（含 1~2 页），因此按 ``(row, page_offset)`` 逐页渲染。
        单页失败不静默忽略：计入 ``self._thumb_failures`` 并在状态栏提示。
        """
        if self._pdf_path is None or self.model.rowCount() == 0:
            return

        # 0) 按当前缩放级别决定渲染精度。
        #    渲染 dpi/宽度必须跟着缩放走，否则放大后是「低分辨率图铺大面积」→ 发虚。
        page_px = self.bw_zone.delegate.page_w
        thumb_dpi = thumbnail_dpi_for(page_px)
        thumb_width = thumbnail_width_for(page_px)

        # 1) 收集可视区内还缺图的 (row, page_offset)
        wanted: set[tuple[int, int]] = set()
        for zone, proxy in (
            (self.bw_zone, self.bw_proxy),
            (self.color_zone, self.color_proxy),
        ):
            view = zone.view
            rect = view.viewport().rect()
            # 按网格采样，避免只为可见区域里稀疏的几个点做 indexAt
            for y in range(rect.top(), rect.bottom() + 1, 40):
                for x in range(rect.left(), rect.right() + 1, 60):
                    idx = view.indexAt(QPoint(x, y))
                    if not idx.isValid():
                        continue
                    row = proxy.mapToSource(idx).row()
                    slot = self.model.slot_at(row)
                    if slot is None:
                        continue
                    for offset in range(len(slot.pages)):
                        if slot.pixmaps[offset] is None:
                            wanted.add((row, offset))

        if not wanted:
            return

        # 2) 打开一次文档，逐页渲染回填
        failures = 0
        doc = None
        try:
            doc = fitz.open(_to_pymupdf_path(self._pdf_path))
            for row, offset in sorted(wanted):
                slot = self.model.slot_at(row)
                if slot is None or not 0 <= offset < len(slot.pages):
                    continue
                if slot.pixmaps[offset] is not None:
                    continue  # 同一次事件循环里可能已被渲染
                info = slot.pages[offset]
                try:
                    pixmap = render_thumbnail(
                        doc.load_page(info.index),
                        dpi=thumb_dpi,
                        target_width=thumb_width,
                    )
                except Exception as exc:  # noqa: BLE001 - 单页失败不影响其他页
                    failures += 1
                    if not info.render_error:
                        info.render_error = f"缩略图渲染失败: {exc}"
                    continue
                self.model.set_page_pixmap(row, offset, pixmap)
        except Exception as exc:  # noqa: BLE001 - 打不开文档属于真错误，要报出来
            self.set_status(f"缩略图渲染失败：{exc}", "error")
            return
        finally:
            if doc is not None:
                doc.close()

        if failures:
            self._thumb_failures += failures
            self.set_status(
                f"⚠ {self._thumb_failures} 页缩略图渲染失败（该页显示为故障占位卡）",
                "warning",
            )

    # ------------------------------------------------------------------
    # 导出（P5 接入）
    # ------------------------------------------------------------------

    def on_export(self) -> None:
        """选目录 → 确认覆盖 → 后台导出两个 PDF + 一份 CSV。"""
        if self._pdf_path is None or not self._infos:
            return
        if self._export_worker is not None and self._export_worker.isRunning():
            self.set_status("导出正在进行中…", "warning")
            return

        # 1) 选输出目录，默认 = PDF 所在目录
        start_dir = str(self._pdf_path.parent)
        out_dir_str = QFileDialog.getExistingDirectory(
            self, "选择导出目录", start_dir
        )
        if not out_dir_str:
            return
        out_dir = Path(out_dir_str)
        prefix = self._pdf_path.stem

        # 2) 覆盖确认（对应 CLI 的 --force）
        write_csv = self.csv_box.isChecked()
        existing = [
            p.name
            for p in self._export_targets(out_dir, prefix, write_csv)
            if p.exists()
        ]
        if existing:
            reply = QMessageBox.question(
                self,
                "输出文件已存在",
                "以下文件已存在，要覆盖吗？\n\n"
                + "\n".join(f"· {name}" for name in existing),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self.set_status("已取消导出")
                return

        self.start_export(out_dir, prefix)

    @staticmethod
    def _export_targets(out_dir: Path, prefix: str,
                        write_csv: bool = False) -> list[Path]:
        """本次导出将要写出的文件列表。

        ``write_csv=False`` 时**不含** CSV —— 否则覆盖确认会为一个
        根本不会生成的旧 CSV 文件弹窗，让用户困惑。
        """
        targets = [
            out_dir / f"{prefix}_color.pdf",
            out_dir / f"{prefix}_bw.pdf",
        ]
        if write_csv:
            targets.append(out_dir / f"{prefix}_report.csv")
        return targets

    def start_export(self, out_dir: Path, prefix: str,
                     write_csv: bool | None = None) -> None:
        """启动后台导出（不弹任何对话框，便于无头测试与脚本调用）。

        :param write_csv: 是否输出 CSV 报告。``None`` 时读界面上的勾选框。
        """
        if self._pdf_path is None or not self._infos:
            return
        if self._export_worker is not None and self._export_worker.isRunning():
            self.set_status("导出正在进行中…", "warning")
            return

        if write_csv is None:
            write_csv = self.csv_box.isChecked()
        # 记下来供完成提示使用（见 on_export_finished）
        self._export_wrote_csv = write_csv

        # 取最终归属。严格模式下用 final_side_map（内含 enforce_duplex 兜底），
        # 杜绝任何路径下把同一张纸拆到两个文件。
        strict = self.model.strict_duplex
        side_map = self.model.final_side_map() if strict else self.model.side_map()

        n_color = sum(1 for s in side_map.values() if s == COLOR)
        n_bw = len(side_map) - n_color

        meta = {
            "source": self._pdf_path.name,
            "pages": len(self._infos),
            "sheets": self._sheet_count,
            "dpi": self.param_panel.dpi,
            "color_threshold": self.param_panel.color_threshold,
            "color_ratio_threshold": self.param_panel.color_ratio,
            "strict_duplex": "是" if strict else "否",
        }

        worker = ExportWorker(
            self._pdf_path,
            Path(out_dir),
            prefix,
            side_map,
            self._infos,
            self._sheets,
            meta,
            write_csv=write_csv,
            parent=self,
        )
        worker.progress.connect(self.set_progress)
        worker.finished_ok.connect(self.on_export_finished)
        worker.failed.connect(self.on_export_failed)
        worker.finished.connect(self._on_export_worker_finished)
        self._export_worker = worker

        # 提示里列出真正会产出的文件，避免用户以为漏了什么
        produced = f"{prefix}_color.pdf / {prefix}_bw.pdf"
        if write_csv:
            produced += f" / {prefix}_report.csv"

        self.export_button.setEnabled(False)
        self.print_button.setEnabled(False)
        self.set_progress(0, 3 if write_csv else 2)
        self.set_status(
            f"正在导出 … 彩色 {n_color} 页 / 黑白 {n_bw} 页（{produced}）"
            + ("" if strict else "（⚠ 非严格双面模式）"),
            "normal" if strict else "warning",
        )
        worker.start()

    def on_export_finished(self, out_dir: str) -> None:
        self.set_progress(0, 0)
        self.export_button.setEnabled(True)
        self.print_button.setEnabled(True)
        prefix = self._pdf_path.stem if self._pdf_path else ""

        # 用**启动时记录**的开关状态，而不是当前勾选框 ——
        # 用户可能在导出过程中改了勾选，那不该影响这次的完成提示。
        files = [f"{prefix}_color.pdf", f"{prefix}_bw.pdf"]
        if self._export_wrote_csv:
            files.append(f"{prefix}_report.csv")
        listing = " / ".join(files)
        self.set_status(f"已导出到 {out_dir} · {listing}")

        if self._suppress_dialogs:
            self.export_completed.emit(out_dir)
            return

        box = QMessageBox(self)
        box.setWindowTitle("导出完成")
        box.setIcon(QMessageBox.Information)
        box.setText("导出完成。")
        box.setInformativeText(
            f"输出目录：\n{out_dir}\n\n"
            + "\n".join(f"· {name}" for name in files)
        )
        open_btn = box.addButton("打开所在文件夹", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_in_explorer(Path(out_dir))
        self.export_completed.emit(out_dir)

    def on_export_failed(self, message: str) -> None:
        self.set_progress(0, 0)
        self.export_button.setEnabled(bool(self._infos))
        self.print_button.setEnabled(bool(self._infos))
        self.set_status("导出失败", "error")
        QMessageBox.critical(self, "导出失败", message)

    # ------------------------------------------------------------------
    # 打印
    # ------------------------------------------------------------------

    def on_print(self) -> None:
        """选两台打印机与画质，把彩色件与黑白件分别送去打印。"""
        if self._pdf_path is None or not self._infos:
            return
        if self._print_running():
            self.set_status("打印任务正在进行中…", "warning")
            return

        # 模式决定归属：单面模式按页独立，双面模式整张纸一致
        side_map = self.model.final_side_map()
        n_color = sum(1 for s in side_map.values() if s == COLOR)
        n_bw = len(side_map) - n_color

        if n_color == 0 and n_bw == 0:
            QMessageBox.information(self, "打印", "没有可打印的页面。")
            return

        dialog = PrintDialog(n_color, n_bw, self)
        if dialog.exec() != QDialog.Accepted or not dialog.can_submit:
            return

        jobs = build_print_jobs(
            side_map,
            color_printer=dialog.color_choice.printer_name,
            bw_printer=dialog.bw_choice.printer_name,
            color_copies=dialog.color_choice.copy_count,
            bw_copies=dialog.bw_choice.copy_count,
            color_duplex=dialog.color_choice.use_duplex,
            bw_duplex=dialog.bw_choice.use_duplex,
            color_grayscale=dialog.color_choice.use_grayscale,
            bw_grayscale=dialog.bw_choice.use_grayscale,
            dpi=dialog.dpi,
        )
        if not jobs:
            QMessageBox.information(self, "打印", "没有需要打印的页面。")
            return

        # 虚拟打印机（Print to PDF / XPS / OneNote …）不出纸而是产出文件。
        # 若不预先指定输出路径，QPrinter.begin() 会阻塞数秒等「保存为」对话框
        # ——界面会完全无响应（实测 4.9~6.5 秒）。这里先问好保存位置。
        if not self._resolve_virtual_outputs(jobs):
            return
        self.start_print(jobs)

    def _resolve_virtual_outputs(self, jobs: list[dict]) -> bool:
        """为虚拟打印机任务确定输出文件。

        :returns: 是否可以继续（用户取消则为 False）。
        """
        virtual = [j for j in jobs if is_virtual_printer(j.get("printer", ""))]
        if not virtual:
            return True

        if self._suppress_dialogs:
            # 无头/脚本场景：用默认路径，不弹框
            for job in virtual:
                job["output_file"] = default_output_path(
                    self._pdf_path, job["label"]
                )
            return True

        for job in virtual:
            default = default_output_path(self._pdf_path, job["label"])
            path_str, _ = QFileDialog.getSaveFileName(
                self,
                f"{job['label']}：选择输出文件（{job.get('printer')} 不出纸，"
                f"而是保存为文件）",
                str(default),
                "PDF 文件 (*.pdf);;所有文件 (*)",
            )
            if not path_str:
                self.set_status("已取消打印")
                return False
            job["output_file"] = Path(path_str)
        return True

    def _print_running(self) -> bool:
        return self._printer is not None and self._printer.is_running

    def start_print(self, jobs: list[dict]) -> None:
        """启动打印。

        **在主线程分帧执行**（不是工作线程）—— 因为虚拟打印机在
        ``QPrinter.begin()`` 时要弹「保存输出为」对话框，而对话框只能在主线程创建；
        放进工作线程会把整个界面冻死（实测卡 6.3 秒且无响应）。
        分帧由 :class:`PrintController` 内部的 ``QTimer`` 完成，
        每帧只渲染一页，界面保持响应。

        不弹任何对话框，便于无头测试与脚本调用。
        """
        if self._pdf_path is None or not jobs:
            return
        if self._print_running():
            self.set_status("打印任务正在进行中…", "warning")
            return

        # 兜底：任何调用路径都要保证虚拟打印机有输出路径，
        # 否则 QPrinter.begin() 会阻塞数秒等「保存为」对话框（界面冻结）
        for job in jobs:
            if is_virtual_printer(job.get("printer", "")) and not job.get("output_file"):
                job["output_file"] = default_output_path(
                    self._pdf_path, job.get("label", "打印")
                )

        controller = PrintController(self._pdf_path, jobs, parent=self)
        controller.progress.connect(self.set_progress)
        controller.finished_ok.connect(self.on_print_finished)
        controller.failed.connect(self.on_print_failed)
        self._printer = controller

        self.print_button.setEnabled(False)
        self.set_progress(0, max(controller.total_pages, 1))
        names = "、".join(
            f"{j['label']}→{j.get('printer') or '默认打印机'}" for j in jobs
        )
        self.set_status(
            f"正在打印 {controller.total_pages} 页（{names}）…"
        )
        controller.start()

    def on_print_finished(self, summary: str) -> None:
        self.set_progress(0, 0)
        self.print_button.setEnabled(bool(self._infos))
        self._printer = None

        warning = "⚠" in summary
        if warning:
            self.set_status(f"打印完成（有告警）· {summary}", "warning")
        else:
            self.set_status(f"打印完成 · {summary}")

        if not self._suppress_dialogs:
            box = QMessageBox(self)
            box.setWindowTitle("打印完成（有告警）" if warning else "打印完成")
            box.setIcon(QMessageBox.Warning if warning else QMessageBox.Information)
            box.setText("打印任务已提交给打印机。")
            box.setInformativeText(summary)
            box.exec()
        self.print_completed.emit(summary)

    def on_print_failed(self, message: str) -> None:
        self.set_progress(0, 0)
        self.print_button.setEnabled(bool(self._infos))
        self._printer = None
        self.set_status("打印失败", "error")
        if not self._suppress_dialogs:
            QMessageBox.critical(self, "打印失败", message)
        self.print_failed.emit(message)

    def _stop_print_worker(self) -> None:
        """取消正在进行的打印（关闭窗口时调用）。"""
        controller = self._printer
        self._printer = None
        if controller is not None:
            try:
                controller.cancel()
            except RuntimeError:
                pass

    def _on_export_worker_finished(self) -> None:
        worker = self.sender()
        if self._export_worker is worker:
            self._export_worker = None
        if isinstance(worker, QThread):
            worker.deleteLater()

    @staticmethod
    def _open_in_explorer(path: Path) -> None:
        """在文件管理器里打开目录（跨平台尽力而为）。"""
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:  # noqa: BLE001 - 打不开文件夹不该影响主流程
            pass

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._stop_worker()
        self._stop_export_worker()
        self._stop_print_worker()
        # QPixmap 是 GUI 资源，必须在 Qt 拆掉之前释放，
        # 否则解释器退出时回收会触发 access violation。
        self._thumb_timer.stop()
        self.model.release_pixmaps()
        super().closeEvent(event)

    def _stop_export_worker(self) -> None:
        """取消正在跑的导出线程并等待退出，避免悬挂线程。"""
        worker = self._export_worker
        if worker is None:
            return
        self._export_worker = None
        try:
            if worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(10000):
                    worker.terminate()
                    worker.wait(2000)
        except RuntimeError:
            return
