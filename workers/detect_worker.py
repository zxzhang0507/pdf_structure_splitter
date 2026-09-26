"""后台检测线程。

**所有 fitz 调用都只发生在本线程内**，``Document`` 对象不跨线程共享。
主线程与它之间只通过 Qt 信号通信，worker 不直接触碰任何控件。

检测不做"整页渲染"，而是**先看页面结构**（见 ``core.structure``）。
纯文字页零渲染，位图页只渲染图片区域 —— 循环因此很轻，
结构就是逐页调用 ``detect_page``。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core import (
    BW,
    COLOR,
    PageInfo,
    PageStructure,
    PdfSplitterError,
    classify_pages,
    detect_page,
    open_input_pdf,
)


class DetectWorker(QThread):
    """逐页「结构分析 → 按需渲染」→ 纸张级分类。

    信号
    ----
    page_done(PageInfo)
        每页检测完成即发出，界面据此增量淡入缩略图。
    progress(int, int)
        ``(已完成页数, 总页数)``
    finished_ok(list, list)
        ``(page_infos, sheets)``，分类完成。
    failed(str)
        出错信息（已经是中文可读文案）。
    """

    page_done = Signal(object)
    progress = Signal(int, int)
    finished_ok = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        pdf_path: Path,
        dpi: int,
        color_threshold: int,
        color_ratio: float,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._pdf_path = Path(pdf_path)
        self._dpi = dpi
        self._color_threshold = color_threshold
        self._color_ratio = color_ratio

    def run(self) -> None:  # noqa: D102 - QThread 约定
        doc = None
        try:
            doc = open_input_pdf(self._pdf_path)
        except PdfSplitterError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"无法打开 PDF：{type(exc).__name__}: {exc}")
            return

        try:
            total = doc.page_count
            infos = []
            structures: list[PageStructure] = []
            for index in range(total):
                # 用户拖动滑块 / 关闭窗口时会被置位，必须尽快退出
                if self.isInterruptionRequested():
                    return

                number = index + 1
                try:
                    info, struct = detect_page(
                        doc.load_page(index),
                        dpi=self._dpi,
                        color_threshold=self._color_threshold,
                        color_ratio_threshold=self._color_ratio,
                    )
                except Exception as exc:  # noqa: BLE001 - 单页失败不中断整体
                    info = PageInfo(
                        index=index, number=number, sheet=index // 2 + 1,
                        is_color=False, color_ratio=0.0,
                        color_pixels=0, total_pixels=0,
                        page_color=BW,
                        render_error=f"{type(exc).__name__}: {exc}",
                        reason="渲染失败",
                    )
                    struct = PageStructure()

                info.index = index
                info.number = number
                info.sheet = index // 2 + 1
                infos.append(info)
                structures.append(struct)
                self.page_done.emit(info)
                self.progress.emit(number, total)

            if self.isInterruptionRequested():
                return

            infos, sheets = classify_pages(infos)
            self.finished_ok.emit(infos, sheets)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"检测过程出错：{type(exc).__name__}: {exc}")
        finally:
            if doc is not None:
                doc.close()
