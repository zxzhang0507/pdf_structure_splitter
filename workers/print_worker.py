"""打印控制：**主线程分帧渲染**。

## 为什么不用工作线程

虚拟打印机（Microsoft Print to PDF / OneNote / Adobe PDF 等）在
``QPrinter.begin()`` 时会请求弹出「保存输出为」对话框。该对话框只能在
**主线程**创建；若在工作线程里调用 ``begin()``，请求无法被处理，
``begin()`` 会阻塞数秒到永久 —— 实测在 worker 线程里卡 **6.3 秒**，
界面上表现为「完全无响应」。

因此打印改为在**主线程**执行，但用 ``QTimer`` 把渲染**分帧**：
每个定时器周期只渲染并绘制少量页面，随即返回事件循环。
于是：

* ``begin()`` 在主线程 → 虚拟打印机的对话框正常工作
* 每帧工作量很小 → 界面保持响应（可滚动、可看进度）
* 两个任务**交错推进** → 彩色件与黑白件几乎同时送向各自的打印机

## 与导出/检测的区别

检测与导出是纯计算，放工作线程最合适（见 ``detect_worker`` / ``export_worker``）。
打印是唯一需要「主线程 + 分帧」的场景，原因就是上面那个对话框约束。

打印内容**以人工复核后的归属为准**（``side_map``），与导出逻辑一致。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtPrintSupport import QPrinter

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - pymupdf < 1.24 只有 fitz 这个模块名
    import fitz  # type: ignore[no-redef]

from core import PdfSplitterError, open_input_pdf

#: 打印渲染 dpi 的默认值。
#:
#: 300 dpi 是办公文档在激光/喷墨打印机上的**标准档**（Word 打印 A4 的常用设置），
#: 600 dpi 用于更精细的图文。
#:
#: 注意：300 dpi 下单页 A4 光栅约 24.9 MB，132 页累计约 3.3 GB。
#: 由于现在改为「虚拟打印机预设输出路径」（见 print_worker 模块文档），
#: 数据直接落到文件而不经 spooler 排队，实测 132 页 @300dpi 也能顺利完成。
#: 若真实打印机较老、内存不足，可在对话框里降到 150 dpi。
DEFAULT_PRINT_DPI = 300
#: 渲染失败时逐级回退的 dpi
FALLBACK_PRINT_DPIS = (150, 100, 72)

#: 每帧渲染的页数。数值越大越快但界面越顿。
#:
#: 300 dpi 单页渲染约 36 ms（150 dpi 约 23 ms），两个任务各 1 页
#: 约 70 ms/帧 —— 相当于 14 fps，界面滚动略顿但可用。
#: 取 1 是为了保证任何 dpi 下都不会出现明显的界面冻结。
PAGES_PER_TICK = 1


class _JobState:
    """一个打印任务的运行状态。"""

    __slots__ = (
        "label", "indices", "printer", "printer_name", "painter", "page_rect",
        "cursor", "printed", "skipped", "done", "finished", "output_file",
        "device_dpi", "render_dpi", "target_px",
    )

    def __init__(self, label: str, indices: list[int], printer: QPrinter,
                 painter: QPainter, page_rect,
                 printer_name: str = "", output_file: Path | None = None,
                 device_dpi: int = DEFAULT_PRINT_DPI,
                 render_dpi: int | None = None,
                 target_px: tuple[int, int] | None = None) -> None:
        self.label = label
        self.indices = indices
        self.printer = printer
        #: 打印机名要在配置阶段记下来 —— 设了 outputFileName 之后
        #: ``QPrinter.printerName()`` 会返回空串（Qt 切到了「打印到文件」模式）
        self.printer_name = printer_name
        self.output_file = output_file
        self.painter = painter
        self.page_rect = page_rect
        #: 打印机实际设备分辨率（由 printer.resolution() 取回）
        self.device_dpi = device_dpi
        #: 本任务实际使用的渲染 dpi（按可打印区反推，见 :meth:`_open_job`）
        self.render_dpi = render_dpi or device_dpi
        #: 目标像素尺寸 = 可打印区像素数；渲染后按此尺寸绘制（1:1 不重采样）
        self.target_px = target_px or (
            max(1, int(page_rect.width())), max(1, int(page_rect.height()))
        )
        self.cursor = 0          # 下一个要处理的 indices 下标
        self.printed = 0
        self.skipped = 0
        self.done = False
        self.finished = False    # painter.end() 是否已调用


class PrintController(QObject):
    """在主线程分帧执行打印，保持界面响应。

    信号
    ----
    progress(int, int)
        ``(已完成页数, 总页数)``，两个任务合并计数。
    job_started(str, int)
        ``(任务名, 页数)``
    finished_ok(str)
        全部完成，参数是可直接显示的摘要文本。
    failed(str)
        出错信息（已经是中文可读文案）。
    """

    progress = Signal(int, int)
    job_started = Signal(str, int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        pdf_path: Path,
        jobs: Sequence[dict],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pdf_path = Path(pdf_path)
        self._jobs = [dict(j) for j in jobs]
        self._doc = None
        self._states: list[_JobState] = []
        self._errors: list[str] = []
        self._cancelled = False
        self._total = sum(len(j.get("indices", [])) for j in self._jobs)

        self._timer = QTimer(self)
        self._timer.setInterval(0)          # 尽快，但每帧都回事件循环
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------

    @property
    def total_pages(self) -> int:
        return self._total

    @property
    def is_running(self) -> bool:
        return self._timer.isActive()

    def cancel(self) -> None:
        """请求取消（关闭窗口时调用）。"""
        self._cancelled = True
        self._timer.stop()
        self._finalize_all()

    # ------------------------------------------------------------------

    def start(self) -> None:
        """打开文档、为每个任务建立打印机并 begin，然后开始分帧渲染。"""
        if self._cancelled:
            return
        if not self._jobs:
            self.failed.emit("没有需要打印的页面。")
            return

        try:
            self._doc = open_input_pdf(self._pdf_path)
        except PdfSplitterError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"无法打开 PDF：{type(exc).__name__}: {exc}")
            return

        # 逐个建立任务。begin() 必须在主线程（虚拟打印机会弹保存对话框）。
        for job in self._jobs:
            indices = list(job.get("indices", []))
            if not indices:
                continue
            label = job.get("label", "打印任务")
            try:
                state = self._open_job(job, indices)
            except Exception as exc:  # noqa: BLE001
                self._errors.append(f"{label}：{exc}")
                continue
            self._states.append(state)
            self.job_started.emit(label, len(indices))

        if not self._states:
            self._finalize_all()
            msg = "；".join(self._errors) if self._errors else "没有可打印的页面。"
            self.failed.emit(msg)
            return

        self.progress.emit(0, max(self._total, 1))
        self._timer.start()

    def _open_job(self, job: dict, indices: list[int]) -> _JobState:
        """配置 QPrinter 并 begin。**必须在主线程调用。**

        **关键**：虚拟打印机（Microsoft Print to PDF / XPS / Adobe PDF 等）
        在不预设 ``outputFileName`` 时，``QPrinter.begin()`` 会阻塞数秒去等
        系统弹出「保存输出为」对话框 —— 实测 4.9~6.5 秒，期间界面完全无响应。
        主线程与工作线程都一样会卡（对话框请求来自驱动，不区分线程）。

        因此：**能预设输出路径就预设**，把交互消掉。
        真实打印机不受影响（它走纸，不写文件）。
        """
        printer = QPrinter(QPrinter.HighResolution)
        name = job.get("printer", "")
        if name:
            printer.setPrinterName(name)
        printer.setCopyCount(max(1, int(job.get("copies", 1))))
        printer.setDuplex(
            QPrinter.DuplexLongSide if job.get("duplex") else QPrinter.DuplexNone
        )
        printer.setResolution(int(job.get("dpi", DEFAULT_PRINT_DPI)))
        # 显式声明彩色 / 灰度，不依赖打印机驱动的默认行为。
        # 黑白件走灰度可避免驱动按彩色渲染而浪费彩粉。
        printer.setColorMode(
            QPrinter.GrayScale if job.get("grayscale") else QPrinter.Color
        )

        # 虚拟打印机：由调用方预先指定的输出路径（见 MainWindow.on_print），
        # 避免 begin() 时弹「保存为」对话框而阻塞界面。
        out_file = job.get("output_file")
        if out_file:
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(str(out_file))

        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError(
                "无法开始打印（打印机可能离线、被占用或驱动异常）"
            )
        page_rect = printer.pageRect(QPrinter.DevicePixel)
        # 打印机**实际生效**的分辨率可能与请求值不同（驱动会取整到支持的档位），
        # 因此以 printer.resolution() 为准来换算渲染 dpi。
        device_dpi = printer.resolution() or int(job.get("dpi", DEFAULT_PRINT_DPI))

        # 按**可打印区**（pageRect，已扣除页边距）反推渲染 dpi。
        #
        # 为什么要这样：整页 A4 在 300dpi 下是 2481x3508 像素，
        # 但打印机的可打印区通常只有 2395x3424（小 3.5%）。
        # 若按整页尺寸渲染再 1:1 画上去，多出的部分会被**裁掉**；
        # 若等比缩到可打印区，又会重采样丢细节（实测锐度只剩 13%）。
        #
        # 正解是让图像尺寸**正好等于可打印区像素数**：这样既 1:1 不重采样，
        # 又不会裁切。最终纸张上仍是完整页面，只是按可打印区等比缩放了一点点
        # （3.5%，肉眼不可见）。
        page_w_px = max(1, int(round(page_rect.width())))
        page_h_px = max(1, int(round(page_rect.height())))
        page_w_pt = printer.pageRect(QPrinter.Point).width() or 1.0

        # 渲染 dpi = 可打印区像素宽 / 可打印区宽度(英寸)
        render_dpi = int(round(page_w_px / (page_w_pt / 72))) or device_dpi

        return _JobState(
            label=job.get("label", "打印任务"),
            indices=indices,
            printer=printer,
            painter=painter,
            page_rect=page_rect,
            printer_name=name,
            device_dpi=device_dpi,
            render_dpi=render_dpi,
            target_px=(page_w_px, page_h_px),
            output_file=Path(out_file) if out_file else None,
        )

    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """每帧：给每个未完成的任务各渲染 PAGES_PER_TICK 页。"""
        if self._cancelled:
            return

        all_done = True
        for state in self._states:
            if state.done:
                continue
            self._advance(state, PAGES_PER_TICK)
            if not state.done:
                all_done = False

        done_pages = sum(s.printed + s.skipped for s in self._states)
        self.progress.emit(min(done_pages, self._total), max(self._total, 1))

        if all_done:
            self._finish()

    def _advance(self, state: _JobState, budget: int) -> None:
        """推进一个任务若干页。"""
        for _ in range(budget):
            if state.cursor >= len(state.indices):
                state.done = True
                self._end_job(state)
                return
            if self._cancelled:
                state.done = True
                self._end_job(state)
                return

            index = state.indices[state.cursor]
            state.cursor += 1

            image = self._render_page_image(index, state)
            if image is None or image.isNull():
                # 单页渲染失败不静默忽略：跳过并计数，最后报给用户
                state.skipped += 1
                continue

            # 首页由 begin() 自动开始，之后每页都要 newPage()
            if state.printed > 0 and not state.printer.newPage():
                state.done = True
                self._errors.append(
                    f"{state.label}：打印机拒绝新建页面（任务可能被取消）"
                )
                self._end_job(state)
                return

            # **1 图像像素 = 1 设备像素**，不做任何缩放/重采样。
            #
            # 这是打印清晰度的关键。此前把图像「等比缩放铺满页面」，
            # 会触发一次重采样（源 300dpi → 目标 600 设备点位），
            # 实测锐度只剩基准的 13%（96.7 vs 728.3）；改成 1:1 后
            # 同分辨率下达到 112%，即连降采样都不做、完全不丢细节。
            #
            # 因为渲染 dpi 就是按设备分辨率定的，图像与页面在像素层面一一对应，
            # 所以这里可以直接按图像自身尺寸绘制、靠坐标原点居中。
            state.painter.drawImage(self._origin_rect(image, state), image)
            state.printed += 1

    def _end_job(self, state: _JobState) -> None:
        """结束单个任务：提交 painter。"""
        if state.finished:
            return
        state.finished = True
        try:
            state.painter.end()
        except Exception as exc:  # noqa: BLE001
            self._errors.append(f"{state.label}：提交打印任务失败（{exc}）")
        if state.skipped:
            self._errors.append(
                f"{state.label}：有 {state.skipped} 页渲染失败未能打印"
                f"（已打出 {state.printed} 页）"
            )

    def _finalize_all(self) -> None:
        """确保所有任务的 painter 都已 end（取消/异常路径）。"""
        for state in self._states:
            self._end_job(state)
            state.done = True
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:  # noqa: BLE001
                pass
            self._doc = None

    def _finish(self) -> None:
        self._timer.stop()
        self._finalize_all()

        results = []
        for s in self._states:
            if not s.printed:
                continue
            dest = s.printer_name or "默认打印机"
            if s.output_file is not None:
                dest += f"（保存为 {s.output_file.name}）"
            results.append(f"{s.label} 已打出 {s.printed} 页 → {dest}")
        errors = list(self._errors)

        if not results:
            self.failed.emit(
                "；".join(errors) if errors else "没有页面被成功打印。"
            )
            return

        summary = "；".join(results)
        if errors:
            # 部分成功：仍然算完成，但把告警带上
            summary += "　⚠ " + "；".join(errors)
        self.finished_ok.emit(summary)

    # ------------------------------------------------------------------

    def _render_page_image(self, index: int, state: _JobState) -> QImage | None:
        """把一页渲染成 QImage，尺寸**正好等于可打印区的像素数**。

        用 PyMuPDF 的 ``Matrix`` 直接给出目标像素宽高，让 MuPDF 自己
        做那 3.5% 的缩放 —— 它是在光栅化时一次性完成的，比先渲染再缩放
        质量更高（没有二次重采样）。

        绘制阶段就能 1:1 贴上去，不裁切也不重采样。

        失败时逐级降低 dpi 重试，仍失败返回 None。
        """
        if self._doc is None:
            return None

        target_w, target_h = state.target_px
        cs = self._rgb_colorspace()

        ladder = [state.render_dpi] + [
            d for d in FALLBACK_PRINT_DPIS if d < state.render_dpi
        ]
        for dpi in ladder:
            if self._cancelled:
                return None
            try:
                page = self._doc.load_page(index)
                rect = page.rect
                if rect.width <= 0 or rect.height <= 0:
                    continue
                # 按目标像素数与页面尺寸算缩放矩阵：
                # 横向缩放 = 目标宽 / 页面宽(pt)，纵向同理 → 光栅化即为该像素数
                sx = target_w / rect.width
                sy = target_h / rect.height
                mat = fitz.Matrix(sx, sy)
                pixmap = page.get_pixmap(
                    matrix=mat, alpha=False, colorspace=cs
                )
            except Exception:  # noqa: BLE001 - 该档失败，试下一个
                continue
            try:
                import numpy as np

                arr = np.frombuffer(pixmap.samples, dtype=np.uint8)
                arr = arr.reshape(pixmap.height, pixmap.width, pixmap.n)
                if pixmap.n == 4:
                    arr = arr[:, :, :3]
                arr = np.ascontiguousarray(arr)
                # copy() 必需：arr 是临时缓冲，QImage 只引用其内存
                return QImage(
                    arr.data, pixmap.width, pixmap.height,
                    3 * pixmap.width, QImage.Format_RGB888,
                ).copy()
            except Exception:  # noqa: BLE001
                continue
        return None

    @staticmethod
    def _origin_rect(image: QImage, state: _JobState) -> QRectF:
        """把图像 1:1 放在可打印区里（左上对齐，不缩放）。

        渲染时已让图像尺寸等于可打印区像素数，因此这里直接铺满即可；
        仍按实际尺寸（而非 page_rect）绘制，避免舍入误差导致边缘被裁。
        """
        return QRectF(
            state.page_rect.left(),
            state.page_rect.top(),
            float(image.width()),
            float(image.height()),
        )

    @staticmethod
    def _rgb_colorspace():
        return fitz.csRGB


#: 兼容旧名（早期版本用 QThread 实现）
PrintWorker = PrintController


#: 虚拟打印机的名字特征（这些不会出纸，而是产出文件/笔记）
_VIRTUAL_PRINTER_HINTS = (
    "print to pdf", "xps", "onenote", "adobe pdf", "pdf creator",
    "pdf24", "cutepdf", "foxit", "nitro", "fax", "虚拟", "pdf",
)


def is_virtual_printer(printer_name: str) -> bool:
    """判断是否是虚拟打印机（产出文件而非出纸）。

    虚拟打印机在 ``QPrinter.begin()`` 时会弹「保存输出为」对话框，
    必须先预设输出路径，否则会阻塞界面数秒（实测 4.9~6.5s）。
    """
    lowered = printer_name.lower()
    return any(hint in lowered for hint in _VIRTUAL_PRINTER_HINTS)


def default_output_path(input_pdf: Path, label: str, suffix: str = ".pdf") -> Path:
    """虚拟打印机的默认输出路径：``<PDF名>_<任务名>打印<suffix>``。"""
    stem = Path(input_pdf).stem
    safe = label.replace("/", "_").replace("\\", "_")
    return Path(input_pdf).parent / f"{stem}_{safe}打印{suffix}"


def build_print_jobs(
    side_map: dict[int, str],
    color_printer: str,
    bw_printer: str,
    color_copies: int = 1,
    bw_copies: int = 1,
    color_duplex: bool = False,
    bw_duplex: bool = False,
    color_grayscale: bool = False,
    bw_grayscale: bool = True,
    dpi: int = DEFAULT_PRINT_DPI,
) -> list[dict]:
    """按最终归属生成打印任务参数（彩色件 / 黑白件各一个）。

    **索引必须先排序**：打印顺序要与 PDF 原始页序一致。

    ``color_grayscale`` / ``bw_grayscale`` 决定是否强制灰度输出。
    黑白件默认 True —— 显式声明灰度可避免某些驱动把纯黑白内容
    按彩色渲染、白白消耗彩色碳粉。
    """
    from core import COLOR

    color_idx = sorted(i for i, s in side_map.items() if s == COLOR)
    bw_idx = sorted(i for i, s in side_map.items() if s != COLOR)

    jobs: list[dict] = []
    if color_idx and color_printer:
        jobs.append({
            "label": "彩色件",
            "indices": color_idx,
            "printer": color_printer,
            "copies": color_copies,
            "duplex": color_duplex,
            "grayscale": color_grayscale,
            "dpi": dpi,
        })
    if bw_idx and bw_printer:
        jobs.append({
            "label": "黑白件",
            "indices": bw_idx,
            "printer": bw_printer,
            "copies": bw_copies,
            "duplex": bw_duplex,
            "grayscale": bw_grayscale,
            "dpi": dpi,
        })
    return jobs
