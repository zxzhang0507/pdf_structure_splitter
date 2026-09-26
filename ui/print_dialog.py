"""打印对话框：为彩色件与黑白件分别指定打印机。

设计要点：

* 上下两块，各含「份数 / 打印机 / 双面」设置
* 打印机下拉框列出系统所有打印机，并**预选默认打印机**
* 「双面」复选框只在所选打印机确实支持双面时才可用
  （用 ``QPrinterInfo.supportedDuplexModes()`` 查询；虚拟打印机通常只支持单面）
* 顶部显示两个分区的页数，让用户确认分配无误
"""

from __future__ import annotations

from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class PrinterChoice(QGroupBox):
    """单个分区（彩色件 / 黑白件）的打印设置。"""

    def __init__(self, title: str, page_count: int, accent: str,
                 color_output: bool, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.setObjectName("PrinterBox")
        self.setProperty("accent", accent)
        #: 本组是否按彩色输出（彩色件 True / 黑白件 False）
        self.color_output = color_output

        #: 本组的页数（供能力提示复用）
        self._pages = page_count
        self.summary = QLabel(f"共 {page_count} 页")
        self.summary.setObjectName("PrintSummary")

        self.printer_box = QComboBox()
        self.printer_box.setMinimumWidth(300)
        self._fill_printers()
        self.printer_box.currentIndexChanged.connect(self._refresh_capabilities)

        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        self.copies.setValue(1)

        self.duplex = QCheckBox("双面打印（正反面）")

        # 「灰度输出」：默认按本组性质决定 ——
        # 黑白件默认勾上（强制灰度，避免驱动按彩色渲染而浪费彩色碳粉），
        # 彩色件默认不勾。
        self.grayscale = QCheckBox("灰度输出（黑白打印）")
        self.grayscale.setChecked(not color_output)
        self.grayscale.setToolTip(
            "勾选后强制打印机按灰度输出。\n"
            "黑白件默认勾选，可避免打印机把纯黑白内容当彩色页渲染而浪费彩粉。\n"
            "彩色件若误勾，彩色内容会变成灰阶。"
        )

        form = QFormLayout(self)
        form.setContentsMargins(12, 10, 12, 12)
        form.setSpacing(8)
        form.addRow(self.summary)
        form.addRow("打印机", self.printer_box)
        form.addRow("份数", self.copies)
        form.addRow("", self.grayscale)
        form.addRow("", self.duplex)

        self._refresh_capabilities()

    # ------------------------------------------------------------------

    def _fill_printers(self) -> None:
        printers = QPrinterInfo.availablePrinters()
        default = QPrinterInfo.defaultPrinter()
        default_name = default.printerName() if not default.isNull() else ""

        for info in printers:
            self.printer_box.addItem(info.printerName())
        if not printers:
            self.printer_box.addItem("（系统未检测到打印机）")
            self.printer_box.setEnabled(False)
            return

        # 预选默认打印机
        index = self.printer_box.findText(default_name)
        self.printer_box.setCurrentIndex(index if index >= 0 else 0)

    def _refresh_capabilities(self) -> None:
        """按所选打印机的能力，调整「双面」「灰度」复选框的可用性。

        虚拟打印机（Microsoft Print to PDF 等）通常只支持单面，
        此时禁用双面复选框并给出说明，避免用户以为设置了却没生效。
        """
        info = self.current_printer_info()
        if info is None or info.isNull():
            self.duplex.setEnabled(False)
            self.duplex.setChecked(False)
            self.duplex.setToolTip("未选择打印机")
            return

        # ---- 双面 ----
        try:
            modes = info.supportedDuplexModes()
        except Exception:  # noqa: BLE001 - 个别驱动会抛异常
            modes = []
        supports = any(m != QPrinter.DuplexNone for m in modes)
        self.duplex.setEnabled(supports)
        if supports:
            self.duplex.setToolTip("在该打印机上启用双面打印")
        else:
            self.duplex.setChecked(False)
            self.duplex.setToolTip("该打印机不支持双面打印")

        # ---- 彩色能力 ----
        # 若打印机自称只支持灰度，而本组是彩色件，提醒用户选错了打印机
        try:
            is_gray_only = info.colorMode() == QPrinter.GrayScale
        except Exception:  # noqa: BLE001
            is_gray_only = False

        if self.color_output and is_gray_only:
            self.summary.setText(
                f"共 {self._pages} 页　⚠ 该打印机不支持彩色，彩色页会变灰阶"
            )
        else:
            self.summary.setText(f"共 {self._pages} 页")

        self.grayscale.setEnabled(not is_gray_only)

    def current_printer_info(self) -> QPrinterInfo | None:
        name = self.printer_box.currentText()
        for info in QPrinterInfo.availablePrinters():
            if info.printerName() == name:
                return info
        return None

    @property
    def printer_name(self) -> str:
        return self.printer_box.currentText()

    @property
    def copy_count(self) -> int:
        return self.copies.value()

    @property
    def use_duplex(self) -> bool:
        return self.duplex.isChecked() and self.duplex.isEnabled()

    @property
    def use_grayscale(self) -> bool:
        """是否强制灰度输出。

        **彩色打印确实需要显式设置**：QPrinter 默认是 ``ColorMode.Color``，
        即把 RGB 数据原样交给驱动。对黑白件而言这有实际风险 ——
        某些驱动会把「看起来是彩色任务」的内容按彩色渲染，
        白白消耗彩色碳粉。因此黑白件默认勾选灰度、显式声明。
        """
        return self.grayscale.isChecked() and self.grayscale.isEnabled()

    def apply_to(self, printer: QPrinter) -> None:
        """把本组设置应用到 QPrinter。"""
        if self.printer_name and self.printer_box.isEnabled():
            printer.setPrinterName(self.printer_name)
        printer.setCopyCount(self.copy_count)
        printer.setDuplex(
            QPrinter.DuplexLongSide if self.use_duplex else QPrinter.DuplexNone
        )
        # 显式声明彩色/灰度，不依赖驱动默认行为
        printer.setColorMode(
            QPrinter.GrayScale if self.use_grayscale else QPrinter.Color
        )


#: 打印画质档位：(显示名, dpi, 说明)
#:
#: 档位依据打印行业的常见取值覆盖 150~1200 dpi：
#: A4 办公文档 300 dpi 为标准、600 dpi 更精细，
#: 1200 dpi 只在极细线条/小幅面精细输出时才有意义。
QUALITY_PRESETS = [
    ("草稿（150 dpi）", 150,
     "最快，排版校对够用；小字略软"),
    ("标准（300 dpi，推荐）", 300,
     "办公文档标准档：与 Word 打印 A4 的常用设置一致"),
    ("清晰（450 dpi）", 450,
     "介于标准与高之间；含较多小字或表格时比 300 更稳"),
    ("高（600 dpi）", 600,
     "细线条图更锐利；光栅数据是 300 dpi 的 4 倍"),
    ("极高（900 dpi）", 900,
     "图纸、密集表格等极限场景；数据量很大"),
    ("最高（1200 dpi）", 1200,
     "只在极精细小幅面输出时才需要；耗时与内存最高"),
]
#: 默认档位索引（标准 300 dpi）
DEFAULT_QUALITY_INDEX = 1
#: 默认打印 dpi（供其它模块引用，等于 QUALITY_PRESETS[DEFAULT_QUALITY_INDEX]）
DEFAULT_QUALITY_DPI = QUALITY_PRESETS[DEFAULT_QUALITY_INDEX][1]


class PrintDialog(QDialog):
    """让用户为两个输出件各选一台打印机，并指定打印画质。"""

    def __init__(self, color_pages: int, bw_pages: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("打印")
        self.setMinimumWidth(560)

        # ---- 画质 ----
        self.quality_box = QComboBox()
        self.quality_box.setMinimumWidth(240)
        for name, _dpi, _hint in QUALITY_PRESETS:
            self.quality_box.addItem(name)
        self.quality_box.setCurrentIndex(DEFAULT_QUALITY_INDEX)
        self.quality_box.setToolTip(
            "\n".join(f"{n}：{h}" for n, _d, h in QUALITY_PRESETS)
        )
        self.quality_hint = QLabel()
        self.quality_hint.setObjectName("ParamHint")
        self.quality_box.currentIndexChanged.connect(self._refresh_quality_hint)

        quality_row = QHBoxLayout()
        quality_row.addWidget(QLabel("打印画质"))
        quality_row.addWidget(self.quality_box, 1)

        # ---- 两个分区 ----
        self.color_choice = PrinterChoice(
            "彩色件", color_pages, "COLOR", color_output=True
        )
        self.bw_choice = PrinterChoice(
            "黑白件", bw_pages, "BW", color_output=False
        )

        hint = QLabel(
            "为两个输出件各选一台打印机（例如彩色件选彩色打印机、"
            "黑白件选黑白激光打印机）。\n"
            "彩色件会以**彩色模式**、黑白件默认以**灰度模式**输出 —— "
            "这些都会显式告诉打印机，不依赖驱动默认行为。"
        )
        hint.setObjectName("PrintHint")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox()
        self.ok_button = buttons.addButton("打印", QDialogButtonBox.AcceptRole)
        self.ok_button.setObjectName("PrimaryButton")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        root.addWidget(hint)
        root.addLayout(quality_row)
        root.addWidget(self.quality_hint)
        root.addWidget(self.color_choice)
        root.addWidget(self.bw_choice)
        root.addWidget(buttons)

        self._refresh_quality_hint()

        # 没有可用打印机时不允许提交
        if not QPrinterInfo.availablePrinters():
            self.ok_button.setEnabled(False)

    def _refresh_quality_hint(self) -> None:
        _name, _dpi, tip = QUALITY_PRESETS[self.quality_box.currentIndex()]
        self.quality_hint.setText(tip)

    @property
    def dpi(self) -> int:
        """选中的打印画质（dpi）。"""
        return QUALITY_PRESETS[self.quality_box.currentIndex()][1]

    @property
    def can_submit(self) -> bool:
        return bool(QPrinterInfo.availablePrinters())
