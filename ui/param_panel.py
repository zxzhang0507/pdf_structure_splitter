"""参数面板：三个滑块 + 严格双面模式开关 + 「重新检测」按钮。

**滑块只改数值，不自动重跑检测**：检测很贵（132 页约 7 秒），
拖动过程中反复重跑会卡住界面。改完参数后由用户点「重新检测」才真正执行。

滑块变化时面板会把自己标记为「待应用」（``dirty``），
按钮上带一个小圆点提示，界面据此把按钮高亮。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from core import DEFAULT_COLOR_RATIO, DEFAULT_COLOR_THRESHOLD, DEFAULT_DPI

#: color_ratio 滑块的放大倍数。QSlider 只支持整数，浮点值乘以该系数后存储。
RATIO_SCALE = 10_000


class _SliderRow:
    """一个「标签 + 滑块 + 数值」的横向行。"""

    def __init__(self, label: str, minimum: int, maximum: int,
                 value: int, on_changed, step: int = 1) -> None:
        self.label = QLabel(label)
        self.label.setObjectName("ParamLabel")

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(minimum, maximum)
        self.slider.setValue(value)
        self.slider.setSingleStep(step)
        self.slider.setPageStep(max(step * 10, 1))
        self.slider.setMinimumWidth(220)
        self.slider.valueChanged.connect(on_changed)
        # 双击滑块复位到默认值
        self.slider.mouseDoubleClickEvent = self._on_double_click  # type: ignore[method-assign]
        self._default = value

        self.value_label = QLabel()
        self.value_label.setObjectName("ParamValue")
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def _on_double_click(self, event) -> None:
        self.slider.setValue(self._default)

    def set_text(self, text: str) -> None:
        self.value_label.setText(text)


class ParamPanel(QWidget):
    """参数调节区。

    信号
    ----
    params_changed(dpi, color_threshold, color_ratio)
        用户点了「重新检测」后发出（携带当前三个值）。
    strict_duplex_changed(bool)
        严格双面模式开关状态变化。
    dirty_changed(bool)
        是否存在「已改参数但尚未重新检测」的状态。
    """

    params_changed = Signal(int, int, float)
    strict_duplex_changed = Signal(bool)
    single_sided_changed = Signal(bool)
    dirty_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ParamPanel")

        grid = QGridLayout(self)
        grid.setContentsMargins(16, 10, 16, 10)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        # 彩色占比量程 0 ~ 0.0200（需求规定上限）。
        # 默认 0.0010 落在量程 1/20 处，滑轨前段会偏挤，但保证用户
        # 能调到「排除小面积彩色」所需的较大值（0.01 量级）。
        self._ratio_max = 0.0200

        # 步长 5：让默认值 75 落在网格上（否则方向键会从 75 跳到 85）
        self._dpi = _SliderRow(
            "检测 DPI", 50, 200, DEFAULT_DPI, self._on_any_changed, step=5
        )
        self._threshold = _SliderRow(
            "色度阈值", 0, 60, DEFAULT_COLOR_THRESHOLD, self._on_any_changed
        )
        self._ratio = _SliderRow(
            "彩色占比",
            0,
            int(self._ratio_max * RATIO_SCALE),
            int(DEFAULT_COLOR_RATIO * RATIO_SCALE),
            self._on_any_changed,
        )

        rows = [
            (self._dpi, "只渲染位图/渐变区域；调低更快，可能漏检细小彩色元素"),
            (self._threshold, "矢量颜色与像素共用；越大越宽松，容易把彩色判成黑白"),
            (self._ratio, "只对渲染出来的图形区域生效；纯文字页与彩色矢量页不受它影响"),
        ]
        for row_index, (row, hint) in enumerate(rows):
            grid.addWidget(row.label, row_index, 0)
            grid.addWidget(row.slider, row_index, 1)
            grid.addWidget(row.value_label, row_index, 2)
            hint_label = QLabel(hint)
            hint_label.setObjectName("ParamHint")
            grid.addWidget(hint_label, row_index, 3)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 0)

        # 策略说明：把判定路径摆给用户看 ——
        # 否则"为什么这页被判彩色"会变成一个黑盒。
        self.strategy_label = QLabel(
            "识别策略：整页无图形 → 直接判黑白（不渲染）　·　"
            "彩色矢量 → 读 PDF 声明的颜色　·　"
            "位图/渐变 → 只渲染图形区域检测"
        )
        self.strategy_label.setObjectName("StrategyHint")
        self.strategy_label.setWordWrap(True)
        grid.addWidget(self.strategy_label, len(rows), 0, 1, 4)

        # 检测模式：严格双面 / 单面，二者互斥。
        # 两个独立勾选框（而非单选框），但勾一个会自动取消另一个。
        self.strict_box = QCheckBox("严格双面模式（同一张纸联动搬移）")
        self.strict_box.setChecked(True)
        self.strict_box.setToolTip(
            "整张纸为一个单位：任意一面彩色则整张纸按彩色打印。\n"
            "适合双面打印（一张纸的两面必须去同一台打印机）。"
        )
        self.strict_box.toggled.connect(self._on_strict_toggled)

        self.single_box = QCheckBox("单面模式（每页独立判定）")
        self.single_box.setChecked(False)
        self.single_box.setToolTip(
            "每一页独立判定彩色/黑白，不再按纸张配对。\n"
            "适合只打印单面、或希望彩色页尽量少印的场景。\n"
            "切换后需点「重新检测」才会按新模式重新计算。"
        )
        self.single_box.toggled.connect(self._on_single_toggled)

        # 「重新检测」按钮：参数改动**不会**自动重跑，必须点它。
        # 检测 132 页约 7 秒，滑块拖动时反复重跑会卡住界面。
        self.apply_button = QPushButton("重新检测")
        self.apply_button.setObjectName("ApplyButton")
        self.apply_button.setMinimumWidth(104)
        self.apply_button.setEnabled(False)      # 没有待应用的改动时置灰
        self.apply_button.clicked.connect(self._emit_params)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 2, 0, 0)
        bottom.addWidget(self.strict_box)
        bottom.addWidget(self.single_box)
        bottom.addStretch(1)
        bottom.addWidget(self.apply_button)
        grid.addLayout(bottom, len(rows) + 1, 0, 1, 4)

        #: 互斥切换时阻止回调互相触发（见 _on_strict_toggled）
        self._toggling = False

        self._dirty = False
        self._refresh_labels()

    # ------------------------------------------------------------------
    # 取值
    # ------------------------------------------------------------------

    @property
    def dpi(self) -> int:
        return self._dpi.slider.value()

    @property
    def color_threshold(self) -> int:
        return self._threshold.slider.value()

    @property
    def color_ratio(self) -> float:
        return self._ratio.slider.value() / RATIO_SCALE

    @property
    def strict_duplex(self) -> bool:
        return self.strict_box.isChecked()

    @property
    def single_sided(self) -> bool:
        """是否为单面模式（每页独立判定）。"""
        return self.single_box.isChecked()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _on_any_changed(self, _value: int) -> None:
        """滑块动了：只更新显示 + 标记待应用，**不**触发检测。"""
        self._refresh_labels()
        self.set_dirty(True)

    def _on_strict_toggled(self, enabled: bool) -> None:
        """两个勾选框互斥：勾严格双面就取消单面，反之亦然。

        ``_toggling`` 防止「A 取消 B → B 的信号又去取消 A」的无限回环。
        """
        if self._toggling:
            return
        self._toggling = True
        try:
            if enabled and self.single_box.isChecked():
                self.single_box.setChecked(False)
            elif not enabled and not self.single_box.isChecked():
                # 用户取消勾选严格双面，但也没勾单面 —— 自动落到单面模式，
                # 保证任何时刻总有一种模式生效（避免「两种都不是」的歧义状态）
                self.single_box.setChecked(True)
        finally:
            self._toggling = False

        self.strict_duplex_changed.emit(self.strict_duplex)
        self.single_sided_changed.emit(self.single_sided)
        # 模式变化会改变分组与配对，属于「需重新检测」的改动
        self.set_dirty(True)

    def _on_single_toggled(self, enabled: bool) -> None:
        if self._toggling:
            return
        self._toggling = True
        try:
            if enabled and self.strict_box.isChecked():
                self.strict_box.setChecked(False)
            elif not enabled and not self.strict_box.isChecked():
                self.strict_box.setChecked(True)
        finally:
            self._toggling = False

        self.strict_duplex_changed.emit(self.strict_duplex)
        self.single_sided_changed.emit(self.single_sided)
        self.set_dirty(True)

    def _refresh_labels(self) -> None:
        self._dpi.set_text(str(self.dpi))
        self._threshold.set_text(str(self.color_threshold))
        self._ratio.set_text(f"{self.color_ratio:.4f}")

    def _emit_params(self) -> None:
        """点「重新检测」：发出当前参数并清除待应用标记。"""
        self.set_dirty(False)
        self.params_changed.emit(self.dpi, self.color_threshold, self.color_ratio)

    @property
    def dirty(self) -> bool:
        """是否存在已改但尚未应用（重新检测）的参数。"""
        return self._dirty

    def set_dirty(self, dirty: bool) -> None:
        if self._dirty == dirty:
            return
        self._dirty = dirty
        self._update_apply_hint()
        self.dirty_changed.emit(dirty)

    def _update_apply_hint(self) -> None:
        """按钮文案：有待应用改动时提示，否则置灰。"""
        self.apply_button.setEnabled(self._dirty)
        self.apply_button.setText("重新检测 •" if self._dirty else "重新检测")
        if self._dirty:
            self.apply_button.setToolTip(
                "参数已改动，点此按新参数重新检测"
                + ("（严格双面模式当前为关闭）" if not self.strict_duplex else "")
            )
        else:
            self.apply_button.setToolTip("参数未改动，无需重新检测")

    def reset_to_defaults(self) -> None:
        """把三个滑块复位到默认值（用于「恢复默认」按钮）。

        只改数值，不自动重新检测 —— 与滑块行为一致，需用户点「重新检测」。
        若三个值本来就已经是默认值，则不标记为待应用（因为确实没变化）。
        """
        self._dpi.slider.setValue(DEFAULT_DPI)
        self._threshold.slider.setValue(DEFAULT_COLOR_THRESHOLD)
        self._ratio.slider.setValue(int(DEFAULT_COLOR_RATIO * RATIO_SCALE))
        self._refresh_labels()
