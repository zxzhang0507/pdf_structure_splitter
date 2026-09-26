"""缩略图数据模型与拖拽语义。

**核心设计**：界面上的两个分区（黑白 / 彩色）共用**同一份**纸张列表，
各自用一个 ``QSortFilterProxyModel`` 按归属过滤。这样：

* 数据源唯一，不存在两个列表同步不一致的问题
* 拖动只改「某张纸归属哪个区」，不重排纸张 —— 导出顺序仍由页 index 决定

**模型的行 = 一张纸，不是一页**。一行携带 1~2 个 :class:`PageInfo`
（奇数总页数时最后一行为 1 页）。于是：

* 拖拽的单位天然就是纸张，用户不可能把同一张纸的两页拆开
* 双面规则由数据结构保证，而不是靠事后纠正
* ``strict_duplex`` 开关只影响提示语义（是否在状态栏提醒），
  不再影响搬移的正确性
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from PySide6.QtCore import (
    QAbstractListModel,
    QMimeData,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QPixmap

from core import BW, COLOR, PageInfo, enforce_duplex
from ui.page_delegate import BASE_PAGE_W

#: 拖拽时使用的 MIME 类型
MIME_TYPE = "application/x-pdf-splitter-pages"


class PageListModel(QAbstractListModel):
    """所有**纸张**（按 PDF 原始顺序）的权威数据源。

    **模型的行 = 一张纸，不是一页。** 一行携带 1~2 个 ``PageInfo``
    （奇数总页数时最后一行为 1 页）。这样：

    * 拖拽的单位天然就是纸张，用户不可能把同一张纸的两页拆开
    * 双面规则由数据结构保证，而不是靠事后纠正
    * ``strict_duplex`` 开关只影响「是否劝阻用户操作」的提示语义，
      不再影响搬移的正确性

    每个 item 的 ``SheetSlot.side`` 表示它归属哪个分区，由用户拖拽修改。

    信号
    ----
    side_changed(list)
        归属发生变化的**纸张行号列表**。
    """

    side_changed = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._slots: List[SheetSlot] = []
        #: 严格双面模式（整张纸为单位）—— 与 ``_single_sided`` 互斥
        self._strict_duplex = True
        #: 单面模式（每页独立判定）
        self._single_sided = False
        #: 最近一次检测的完整页列表，供切换模式时重新分组
        self._all_infos: List[PageInfo] = []

    # ------------------------------------------------------------------
    # 数据装载
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_sheet(infos: Sequence[PageInfo]) -> List[List[PageInfo]]:
        """按 ``info.sheet`` 把页面分组，保持原始顺序。"""
        groups: dict[int, List[PageInfo]] = {}
        for info in infos:
            groups.setdefault(info.sheet, []).append(info)
        return [groups[k] for k in sorted(groups)]

    @staticmethod
    def _pair_runs(infos: Sequence[PageInfo]) -> List[List[PageInfo]]:
        """按**配对规则**（index 0-1 / 2-3 / …）切分，而不是按 ``info.sheet``。

        重新检测时要用它把行还原成「一张纸一行」：
        用户可能已经把某张纸拆分过，若仍按 ``info.sheet`` 分组，
        拆开的两页会被误合并成一行（因为两页的 ``sheet`` 号相同）。
        """
        items = list(infos)
        return [items[i:i + 2] for i in range(0, len(items), 2)]

    def set_pages(self, infos: Sequence[PageInfo]) -> None:
        """装载（或替换）全部页面，按**当前模式**分组。

        会清空用户此前的手工调整与拆分 —— 重新检测意味着判定变了，
        旧的人工调整已失去参照。

        * 双面模式：一张纸一行（1~2 页），整行共享归属
        * 单面模式：一页一行，各页按自身检测结果独立归属
        """
        self._all_infos = list(infos)
        self.beginResetModel()
        self._slots = []
        for pages in self._group_for_mode(self._all_infos):
            side = (
                COLOR if any(p.is_color for p in pages) else BW
            ) if not self._single_sided else (
                COLOR if pages[0].is_color else BW
            )
            self._slots.append(
                SheetSlot(pages=pages, side=side, original_side=side)
            )
        self.endResetModel()

    def _group_for_mode(self, infos: Sequence[PageInfo]) -> List[List[PageInfo]]:
        """按当前模式把页面切成行。"""
        if self._single_sided:
            return [[info] for info in infos]
        return self._pair_runs(infos)

    def set_single_sided(self, enabled: bool) -> bool:
        """切换单面 / 双面模式，**立即按其重新分组**当前数据。

        返回是否真的发生了变化。

        分组时保留人工调整过的归属（按页 index 匹配），这样用户切模式
        不会白丢之前的手工复核成果。

        注意：单面模式用的是每页**自身**的检测结果（``is_color``），
        而这个值在检测阶段就已经逐页算好了 —— 所以切换模式**不需要**
        重新检测就能看到正确的单面分组。点「重新检测」只是用当前
        滑块参数重算一遍检测而已。
        """
        enabled = bool(enabled)
        if enabled == self._single_sided:
            return False

        # 记下人工调整过的页及其归属
        manual: dict[int, str] = {}
        for slot in self._slots:
            if slot.manually_moved:
                for info in slot.pages:
                    manual[info.index] = slot.side

        self._single_sided = enabled

        if not self._all_infos:
            return True

        self.beginResetModel()
        self._slots = []
        for pages in self._group_for_mode(self._all_infos):
            if self._single_sided:
                side = COLOR if pages[0].is_color else BW
            else:
                side = COLOR if any(p.is_color for p in pages) else BW
            slot = SheetSlot(pages=pages, side=side, original_side=side)
            # 恢复人工调整
            moved_sides = {manual[p.index] for p in pages if p.index in manual}
            if moved_sides:
                if len(moved_sides) == 1:
                    the_side = moved_sides.pop()
                    slot.side = the_side
                    slot.manually_moved = True
                else:
                    # 多页归属不一致（拆开的纸又被合回去）：按双面规则取并集
                    slot.side = COLOR if COLOR in moved_sides else BW
                    slot.manually_moved = True
            self._slots.append(slot)
        self.endResetModel()
        self.side_changed.emit([])
        return True

    def clear(self) -> None:
        self.beginResetModel()
        self._slots = []
        self.endResetModel()

    def release_pixmaps(self) -> None:
        """释放所有缓存的 QPixmap。

        **必须在 QApplication 销毁之前调用**（见 MainWindow.closeEvent）。
        QPixmap 是 GUI 资源，若在 Qt 拆掉之后才被 Python 回收，
        Windows 上会触发 access violation（实测踩过）。
        """
        for slot in self._slots:
            slot.pixmaps = [None] * len(slot.pages)

    def drop_thumbnails_below(self, zoom: float) -> int:
        """丢弃清晰度已不够的缩略图，返回丢弃的页数。

        缩略图的渲染像素宽是「卡片页面宽 × 余量」固定的；当用户放大到
        某个级别后，原有缩略图的实际像素宽已不足显示宽，会发虚。
        这里按当前 zoom 算出**最低需要的像素宽**，不够的就置空，
        交给懒加载按新的 dpi 重渲。

        只丢不渲 —— 渲染由调用方（主窗口）在可视区懒加载里做，
        避免为屏幕上根本看不到的页白白渲染。
        """
        from ui.thumbnail import thumbnail_width_for

        needed = thumbnail_width_for(round(BASE_PAGE_W * zoom))
        dropped = 0
        for slot in self._slots:
            for offset, pixmap in enumerate(slot.pixmaps):
                if pixmap is not None and pixmap.width() < needed:
                    slot.pixmaps[offset] = None
                    dropped += 1
        return dropped

    # ------------------------------------------------------------------
    # QAbstractListModel 接口
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._slots)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._slots):
            return None
        slot = self._slots[index.row()]

        if role == Qt.DisplayRole:
            return slot.label
        if role == Qt.ToolTipRole:
            return self._tooltip(slot)
        if role == Qt.UserRole:
            return slot                      # 供委托读取
        if role == Qt.UserRole + 1:
            # 一行是一张纸，可能有 0~2 张已渲染的页缩略图；
            # 取首个非空者作为该纸在列表里的代表图。
            return self.pixmap_at(index.row())
        if role == Qt.UserRole + 2:
            return slot.side
        return None

    def supportedDropActions(self):
        """内部搬移是**移动**语义，不是复制。

        必须显式声明：``QAbstractItemModel`` 默认只返回 ``CopyAction``，
        而视图设了 ``setDefaultDropAction(Qt.MoveAction)`` 且模型未接受该动作时，
        Qt 会判定拖放不可行（表现为拖不动）。
        """
        return Qt.MoveAction

    def supportedDragActions(self):
        return Qt.MoveAction

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        """能否接受这次放置。

        **必须重写**：``QAbstractItemModel`` 的默认实现一律返回 ``False``，
        于是 ``dragEnterEvent`` 会拒绝拖入、Qt 显示「禁止」光标，
        真实鼠标拖拽根本走不到 drop —— 表现为「拖不动」。
        （早期版本漏了这个方法，是我直接调 ``dropEvent`` 才误判为已修好。）
        """
        if data is None or not data.hasFormat(MIME_TYPE):
            return False
        if action == Qt.IgnoreAction:
            return True
        return bool(self.supportedDropActions() & action)

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if not index.isValid():
            # 允许拖到空白处（交给 dropMimeData 处理）
            return base | Qt.ItemIsDropEnabled
        return base | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled | Qt.ItemIsEnabled

    def _tooltip(self, slot) -> str:
        """鼠标悬停提示。

        新策略下"为什么这页是彩色"不再是单一的比例数字，所以这里把
        **判定依据**与**页面结构**一并列出 —— 用户看到一页被判彩色时，
        能立刻分辨是"有彩色矢量"还是"位图区域检出彩色像素"。
        """
        lines = [
            f"第 {slot.sheet_number} 张纸 · {slot.label}",
            f"整张纸归属：{slot.side}",
            "",
        ]
        for info in slot.pages:
            detail = f"  P{info.number}：{info.page_color}"
            if info.reason:
                detail += f"（{info.reason}）"
            if info.rendered:
                detail += f" 占比 {info.color_ratio:.6f}"
            lines.append(detail)
            if info.structure:
                lines.append(f"      结构：{info.structure}")
            if not info.rendered and not info.render_error:
                lines.append("      未渲染（仅靠页面结构判定）")
            if info.render_error:
                lines.append(f"      ⚠ 渲染失败：{info.render_error}")
        if slot.manually_moved:
            lines.append("")
            lines.append("（已人工调整，与检测结果不同）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 拆分 / 合并（配合「严格双面模式」开关）
    # ------------------------------------------------------------------

    def can_split(self, row: int) -> bool:
        """该行是否可拆分（非严格模式 + 双面纸 + 尚未拆分）。"""
        slot = self.slot_at(row)
        return (
            not self._strict_duplex
            and slot is not None
            and slot.is_duplex
            and not slot.split
        )

    def can_merge(self, rows: Sequence[int]) -> bool:
        """这些行是否可合并回一张纸（正是同一张纸的、已被拆开的两行）。"""
        targets = [self.slot_at(r) for r in rows]
        if len(targets) != 2 or any(s is None for s in targets):
            return False
        a, b = targets  # type: ignore[misc]
        if not (a.split or b.split):
            return False
        return a.sheet_number == b.sheet_number

    def split_slots(self, rows: Sequence[int]) -> int:
        """把双面纸拆成两个单页行，返回实际拆分的行数。

        拆分后两行各自持有 1 页，可分别搬到不同分区 ——
        这就是「不严格双面模式」的含义。
        """
        targets = sorted({r for r in rows if self.can_split(r)}, reverse=True)
        if not targets:
            return 0

        self.beginResetModel()
        for row in targets:
            slot = self._slots[row]
            replacements = [
                SheetSlot(pages=[page], side=slot.side,
                          original_side=slot.side, split=True)
                for page in slot.pages
            ]
            for new_slot in replacements:
                new_slot.manually_moved = slot.manually_moved
            self._slots[row:row + 1] = replacements
        self.endResetModel()
        self.side_changed.emit([])
        return len(targets)

    def merge_slots(self, rows: Sequence[int]) -> int:
        """把同一张纸被拆开的两行合并回一行。"""
        if not self.can_merge(rows):
            return 0
        targets = sorted(set(rows))
        if len(targets) != 2:
            return 0

        # 保持 PDF 原始页序（按 index 排），不按行号
        low, high = targets
        pages = sorted(
            self._slots[low].pages + self._slots[high].pages,
            key=lambda p: p.index,
        )
        # 合并后的归属：任一页为 COLOR 则整行 COLOR（回到双面规则）
        side = COLOR if any(p.is_color for p in pages) else BW
        merged = SheetSlot(pages=pages, side=side, original_side=side)
        merged.manually_moved = True

        self.beginResetModel()
        self._slots[low] = merged
        del self._slots[high]
        self.endResetModel()
        self.side_changed.emit([])
        return 1

    # ------------------------------------------------------------------
    # 拖拽
    # ------------------------------------------------------------------

    def mimeData(self, indexes) -> QMimeData:
        """把被拖**纸张行**的 index 打包。

        因为一行就是一张纸，两页天然同步移动，这里不再需要补齐配对页。
        """
        rows = sorted({i.row() for i in indexes if i.isValid()})
        if not rows:
            return None
        mime = QMimeData()
        payload = ",".join(str(r) for r in rows)
        mime.setData(MIME_TYPE, payload.encode("ascii"))
        mime.setText(payload)
        return mime

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if not data.hasFormat(MIME_TYPE):
            return False
        raw = bytes(data.data(MIME_TYPE)).decode("ascii")
        try:
            rows = [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            return False

        # 目标分区由落到哪个视图决定：row/parent 拿不到分区信息，
        # 因此由视图在调用前通过 set_drop_target_side() 告知。
        target_side = self._pending_drop_side
        if target_side not in (BW, COLOR):
            return False

        return self.move_sheets(rows, target_side)

    # 目标分区由视图设置（见 ZoneView）
    _pending_drop_side: str = ""

    def set_drop_target_side(self, side: str) -> None:
        self._pending_drop_side = side

    # ------------------------------------------------------------------
    # 搬移逻辑
    # ------------------------------------------------------------------

    def move_sheets(self, rows: Sequence[int], side: str) -> bool:
        """把指定**纸张行**搬到 ``side`` 分区（该行的所有页一起走）。

        :returns: 是否确实发生了改动
        """
        if side not in (BW, COLOR):
            return False

        moving = sorted({r for r in rows if 0 <= r < len(self._slots)})
        if not moving:
            return False

        changed = []
        for row in moving:
            slot = self._slots[row]
            if slot.side != side:
                slot.side = side
                changed.append(row)
            # 即便归属没变，只要用户动过就标记为「人工调整」
            slot.manually_moved = True

        if not changed:
            return False

        # 通知视图整段刷新（IconMode 下按区间发 dataChanged 更省事）
        self.dataChanged.emit(
            self.index(min(changed), 0),
            self.index(max(changed), 0),
            [Qt.DisplayRole, Qt.UserRole + 2],
        )
        self.side_changed.emit(sorted(changed))
        return True

    def reset_to_detected(self, rows: Sequence[int] | None = None) -> bool:
        """把纸张复位到检测的原始结果。``rows=None`` 表示全部复位。"""
        targets = range(len(self._slots)) if rows is None else rows
        changed = []
        for row in targets:
            if not 0 <= row < len(self._slots):
                continue
            slot = self._slots[row]
            if slot.side != slot.original_side or slot.manually_moved:
                slot.side = slot.original_side
                slot.manually_moved = False
                changed.append(row)
        if not changed:
            return False
        self.dataChanged.emit(
            self.index(min(changed), 0),
            self.index(max(changed), 0),
            [Qt.DisplayRole, Qt.UserRole + 2],
        )
        self.side_changed.emit(sorted(changed))
        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def strict_duplex(self) -> bool:
        return self._strict_duplex

    def set_strict_duplex(self, enabled: bool) -> None:
        self._strict_duplex = bool(enabled)

    def side_map(self) -> Dict[int, str]:
        """``{page_index: 'BW'|'COLOR'}`` —— 供导出使用。

        展开到**页**一级：同一张纸的所有页共享该纸的归属。
        """
        result: Dict[int, str] = {}
        for slot in self._slots:
            for info in slot.pages:
                result[info.index] = slot.side
        return result

    def final_side_map(self) -> Dict[int, str]:
        """导出/打印用的最终归属。

        * **严格双面模式**：调用 :func:`enforce_duplex` 兜底，确保同一张纸
          （index 2k / 2k+1）的两页一定同归属 —— 哪怕外部代码直接改过
          ``slot.side``，也不可能产出一张纸跨两个文件的违规结果。
        * **单面模式 / 非严格模式**：直接返回 ``side_map()``，**不做**兜底。
          这正是「按页独立」的意义 —— 同一张纸的两页可以分别归到彩色件
          与黑白件（例如只补印其中一面）。
        """
        if self._strict_duplex and not self._single_sided:
            return enforce_duplex(self.side_map())
        return self.side_map()

    @property
    def single_sided(self) -> bool:
        return self._single_sided

    @property
    def mode_label(self) -> str:
        """当前模式的简短中文名，用于状态栏显示。"""
        if self._single_sided:
            return "单面模式"
        return "严格双面模式" if self._strict_duplex else "非严格模式"

    def split_count(self) -> int:
        """被拆分成单页的行数（非严格模式下的人工操作）。"""
        return sum(1 for s in self._slots if s.split)

    def count_of(self, side: str) -> int:
        """统计归属到 ``side`` 的**页数**（不是纸张数）。"""
        return sum(len(s.pages) for s in self._slots if s.side == side)

    def sheet_count_of(self, side: str) -> int:
        """统计归属到 ``side`` 的**纸张数**。"""
        return sum(1 for s in self._slots if s.side == side)

    def upgraded_count(self) -> int:
        """因双面配对而「跟着」升级到彩色打印的页数（自身是黑白却被判 COLOR）。

        单面模式下没有配对，因此恒为 0。
        """
        if self._single_sided:
            return 0
        return sum(
            1
            for s in self._slots
            if s.side == COLOR
            for info in s.pages
            if not info.is_color
        )

    @property
    def sheet_total(self) -> int:
        """纸张总数。"""
        return len(self._slots)

    @property
    def page_total(self) -> int:
        """页面总数。"""
        return sum(len(s.pages) for s in self._slots)

    def moved_count(self) -> int:
        """被人工调整过的**页数**。"""
        return sum(len(s.pages) for s in self._slots if s.manually_moved)

    def slot_at(self, row: int):
        return self._slots[row] if 0 <= row < len(self._slots) else None

    def pixmap_at(self, row: int) -> QPixmap | None:
        """取某张纸的首个非空页面缩略图（列表视图的占位显示用）。"""
        slot = self.slot_at(row)
        if slot is None:
            return None
        for pixmap in slot.pixmaps:
            if pixmap is not None:
                return pixmap
        return None

    def set_page_pixmap(self, row: int, page_offset: int,
                        pixmap: QPixmap) -> None:
        """缓存某张纸**某一页**的缩略图（懒加载完成后回填）。

        :param row: 纸张行号
        :param page_offset: 该纸内的页序号（0 或 1）
        """
        slot = self.slot_at(row)
        if slot is None or not 0 <= page_offset < len(slot.pixmaps):
            return
        slot.pixmaps[page_offset] = pixmap
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.UserRole + 1])

    def set_pixmap(self, row: int, pixmap: QPixmap) -> None:
        """兼容旧调用：写入该纸的第一页。"""
        self.set_page_pixmap(row, 0, pixmap)


class SheetSlot:
    """一行在界面上的可变状态。

    正常情况下**一行 = 一张纸**（1~2 个 :class:`PageInfo`），
    归属（``side``）是整行共享的，因此双面规则由数据结构保证。

    例外：关闭「严格双面模式」后，用户可以右键把一张纸**拆分**成两行
    （``split`` 标志置位），此后两页可各自搬移 —— 这正是「不严格」的含义，
    导出前不再用 ``enforce_duplex`` 兜底。
    """

    __slots__ = ("pages", "side", "original_side", "pixmaps",
                 "manually_moved", "split")

    def __init__(self, pages: Sequence[PageInfo], side: str,
                 original_side: str, split: bool = False) -> None:
        assert pages, "一行至少要有 1 页"
        self.pages: List[PageInfo] = list(pages)
        self.side = side
        self.original_side = original_side
        #: 与 ``pages`` 等长，每个元素是该页的缩略图（懒加载，未渲染时为 None）
        self.pixmaps: List[QPixmap | None] = [None] * len(self.pages)
        self.manually_moved = False
        #: 是否已被用户「拆分为单页」（仅非严格模式下允许）
        self.split = split

    def has_any_pixmap(self) -> bool:
        return any(p is not None for p in self.pixmaps)

    def needs_pixmaps(self) -> bool:
        """是否还有页面没渲染缩略图。"""
        return any(p is None for p in self.pixmaps)

    @property
    def sheet_number(self) -> int:
        """1-based 纸张序号（拆分后两行共享同一张纸号）。"""
        return self.pages[0].sheet

    @property
    def label(self) -> str:
        """展示用标签：两页为 ``P31-32``，单页为 ``P7``。"""
        if len(self.pages) == 1:
            return f"P{self.pages[0].number}"
        return f"P{self.pages[0].number}-{self.pages[-1].number}"

    @property
    def is_duplex(self) -> bool:
        """是否为双面纸（有配对页）。"""
        return len(self.pages) == 2

    @property
    def page_numbers(self) -> List[int]:
        return [p.number for p in self.pages]

    @property
    def any_color(self) -> bool:
        """该纸任意一面自身检测为彩色。"""
        return any(p.is_color for p in self.pages)

    @property
    def ok(self) -> bool:
        """该纸所有页面都渲染成功。"""
        return all(p.ok for p in self.pages)



class SideFilterProxy(QSortFilterProxyModel):
    """按归属分区过滤。"""

    def __init__(self, side: str, parent=None) -> None:
        super().__init__(parent)
        self._side = side

    def set_side(self, side: str) -> None:
        self._side = side
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        slot = model.slot_at(source_row)
        return slot is not None and slot.side == self._side
