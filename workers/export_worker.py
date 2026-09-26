"""后台导出线程。

**所有 fitz 调用都只发生在本线程内**，``Document`` 对象不跨线程共享。
主线程与它之间只通过 Qt 信号通信，worker 不直接触碰任何控件。

导出的**颜色归属以用户人工复核结果为准**（``side``），而不是
``classify_pages`` 的原始输出 —— 这正是 GUI 存在的意义。

输出内容：

* ``<前缀>_color.pdf`` / ``<前缀>_bw.pdf`` —— 始终输出
* ``<前缀>_report.csv`` —— **仅当 ``write_csv=True``**（界面勾选框控制，
  默认不输出）。多数用户只要两个 PDF 去打印，CSV 是给需要逐页核对
  或留档的场景准备的。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core import (
    BW,
    COLOR,
    PageInfo,
    PdfSplitterError,
    SheetInfo,
    create_output_pdf,
    open_input_pdf,
    write_report,
)


class ExportWorker(QThread):
    """把人工复核后的归属写盘：两个 PDF + 一份 CSV 报告。

    信号
    ----
    progress(int, int)
        ``(已完成步骤数, 总步骤数)``
    finished_ok(str)
        输出目录路径，界面据此提示「已导出到 …」并提供打开文件夹。
    failed(str)
        出错信息（已经是中文可读文案）。
    """

    progress = Signal(int, int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        pdf_path: Path,
        out_dir: Path,
        prefix: str,
        side_map: dict[int, str],
        page_infos: list[PageInfo],
        sheets: list[SheetInfo],
        meta: dict,
        write_csv: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._pdf_path = Path(pdf_path)
        self._out_dir = Path(out_dir)
        self._prefix = prefix
        self._side_map = dict(side_map)
        self._page_infos = list(page_infos)
        self._sheets = list(sheets)
        self._meta = dict(meta)
        #: 是否额外输出 CSV 报告。默认 **False** —— 多数用户只要两个 PDF，
        #: CSV 是给需要逐页核对/留档的场景准备的，由界面上的勾选框控制。
        self._write_csv = bool(write_csv)

    # ------------------------------------------------------------------

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
            color_idx = sorted(i for i, s in self._side_map.items() if s == COLOR)
            bw_idx = sorted(i for i, s in self._side_map.items() if s == BW)

            stem = self._prefix or self._pdf_path.stem
            out_dir = self._out_dir
            color_pdf = out_dir / f"{stem}_color.pdf"
            bw_pdf = out_dir / f"{stem}_bw.pdf"
            report_path = out_dir / f"{stem}_report.csv"

            # 步数随是否输出 CSV 变化：彩色件、黑白件（、报告）
            total_steps = 3 if self._write_csv else 2
            self.progress.emit(0, total_steps)

            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PdfSplitterError(
                    f"无法写入输出目录 {out_dir}（可能没有写权限）：{exc}"
                ) from exc

            if self.isInterruptionRequested():
                return
            # create_output_pdf 内部按「连续区段」批量复制，
            # **依赖传入列表已升序**（它自己不排序），故上面先 sorted()。
            create_output_pdf(doc, color_idx, color_pdf)
            self.progress.emit(1, total_steps)

            if self.isInterruptionRequested():
                return
            create_output_pdf(doc, bw_idx, bw_pdf)
            self.progress.emit(2, total_steps)

            if self._write_csv:
                if self.isInterruptionRequested():
                    return
                self._write_report(report_path, color_idx, bw_idx)
                self.progress.emit(3, total_steps)

            self.finished_ok.emit(str(out_dir))
        except PdfSplitterError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"导出过程出错：{type(exc).__name__}: {exc}")
        finally:
            if doc is not None:
                doc.close()

    # ------------------------------------------------------------------

    def _write_report(
        self, report_path: Path, color_idx: list[int], bw_idx: list[int]
    ) -> None:
        """写 CSV。

        ``page_color`` 列保留**原始检测结果**，``output`` 列写**最终归属**
        （人工复核后），这样报告能同时反映「算法认为」与「用户决定」。
        """
        # write_report 依赖 info.output / sheet.output 来决定输出列，
        # 因此这里把最终归属回写到 info 上（只在本次导出的副本里改）。
        for info in self._page_infos:
            side = self._side_map.get(info.index)
            if side is not None:
                info.output = side

        final_sheet_side: dict[int, str] = {}
        for sheet in self._sheets:
            sides = {
                self._side_map[i] for i in sheet.pages if i in self._side_map
            }
            if sides == {COLOR}:
                final_sheet_side[sheet.number] = COLOR
            elif sides == {BW}:
                final_sheet_side[sheet.number] = BW
            elif sides:
                # 理论上不会发生：同一张纸的页归属必然一致（模型保证 +
                # enforce_duplex 兜底）。真出现了就取并集，偏向彩色。
                final_sheet_side[sheet.number] = COLOR if COLOR in sides else BW

        for sheet in self._sheets:
            if sheet.number in final_sheet_side:
                sheet.output = final_sheet_side[sheet.number]

        meta = dict(self._meta)
        meta["color_pages"] = len(color_idx)
        meta["bw_pages"] = len(bw_idx)
        # 新策略的统计：让报告能反映"省下了多少渲染"
        meta["rendered_pages"] = sum(1 for p in self._page_infos if p.rendered)
        for reason in ("纯文字", "黑白矢量", "彩色矢量",
                       "位图彩色", "位图灰度", "空白页"):
            count = sum(1 for p in self._page_infos if p.reason == reason)
            if count:
                meta[f"判定依据·{reason}"] = count

        write_report(report_path, self._page_infos, self._sheets, meta)
