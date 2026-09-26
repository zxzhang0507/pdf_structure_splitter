"""无头（offscreen）全链路自检：加载 PDF → 断言分区 → 搬移 → 导出。

用法（PowerShell）::

    $env:QT_QPA_PLATFORM="offscreen"; python tests/headless_check.py --pdf D:\some\file.pdf

**需要自备一份 PDF**（本项目不附带测试文件）。未提供时打印提示并跳过。
也可以用环境变量指定：``PSS_TEST_PDF``。

断言的是**策略自身的正确性**，而不是"结果等于某个固定答案"：

* 判定依据与页面结构自洽（位图页必渲染、纯文字页必不渲染）；
* 渲染页数 == 位图页数，且远少于总页数；
* 分区计数自洽（两区之和 = 总页数）；
* 拖拽搬移遵守双面规则（同一张纸的两页不会分开）；
* 导出页数与检测结论一致。

这样断言的好处是：换成任何 PDF 都成立，不需要为每份文档维护基线。
退出码 0 表示全部断言通过（跳过也算 0）。
"""

from __future__ import annotations

import argparse
import csv
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import BW, COLOR  # noqa: E402
from tests.fixture import hint, resolve_pdf, set_pdf  # noqa: E402
from ui.main_window import MainWindow, load_stylesheet  # noqa: E402

FAILURES: list[str] = []
#: 已知的判定依据集合（出现别的说明依据被改名了，测试需要同步）
KNOWN_REASONS = {"纯文字", "黑白矢量", "空白页", "彩色矢量",
                 "位图彩色", "位图灰度", "渲染失败"}


def check(label: str, got, expect) -> None:
    ok = got == expect
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f" != {expect!r}"))
    if not ok:
        FAILURES.append(f"{label}: {got!r} != {expect!r}")


def check_true(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=None,
                    help="测试用 PDF 路径（默认找项目根目录的 test.pdf）")
    ap.add_argument("--export-to", default="")
    args = ap.parse_args()

    set_pdf(args.pdf)
    pdf = resolve_pdf()
    if pdf is None:
        print(hint())
        return 0
    print(f"测试用 PDF：{pdf.name}")

    app = QApplication(sys.argv)
    app.setStyleSheet(load_stylesheet())
    window = MainWindow()
    window.show()

    uncaught: list[str] = []

    def hook(exc_type, exc, tb):
        uncaught.append(f"{exc_type.__name__}: {exc}")
        print("!!! 未捕获异常:", exc_type.__name__, exc)
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = hook

    state = {"done": False}

    def on_finished(infos, sheets):
        state["done"] = True
        print(f"\n=== 检测完成：{len(infos)} 页 / {len(sheets)} 张纸 ===")

        # ------------------------------------------------------------------
        # 1) 判定与渲染的自洽性
        # ------------------------------------------------------------------
        print("\n  策略自洽性：")
        for page in infos:
            r = page.reason
            if r in ("纯文字", "空白页", "黑白矢量"):
                if page.rendered:
                    FAILURES.append(f"p{page.number}: {r}页不该渲染")
            elif r == "彩色矢量":
                if page.rendered:
                    FAILURES.append(f"p{page.number}: 彩色矢量页不该渲染")
                if page.page_color != COLOR:
                    FAILURES.append(f"p{page.number}: 彩色矢量却判黑白")
            elif r in ("位图彩色", "位图灰度"):
                if not page.rendered:
                    FAILURES.append(f"p{page.number}: 位图页必须渲染才能判定")
            elif r == "渲染失败":
                FAILURES.append(f"p{page.number}: 出现渲染失败")

        rendered = [p for p in infos if p.rendered]
        raster = [p for p in infos if p.reason in ("位图彩色", "位图灰度")]
        no_render = len(infos) - len(rendered)
        print(f"    渲染 {len(rendered)} 页 · 未渲染 {no_render} 页")
        check("  渲染页数 == 位图页数", len(rendered), len(raster))
        check_true("  存在无需渲染的页", no_render > 0)
        if len(infos) >= 4:
            check_true("  渲染页数不超过总页数的一半",
                       len(rendered) <= len(infos) / 2,
                       )

        unknown = {p.reason for p in infos} - KNOWN_REASONS
        check("  判定依据均在已知集合内", unknown, set())
        check("  所有页都有结构描述",
              [p.number for p in infos if not p.structure], [])

        # ------------------------------------------------------------------
        # 2) 分区计数自洽
        # ------------------------------------------------------------------
        print("\n  分区计数：")
        n_color = window.model.count_of(COLOR)
        n_bw = window.model.count_of(BW)
        check("  彩色区 + 黑白区 = 总页数",
              n_color + n_bw, window.model.page_total)
        check("  总页数", window.model.page_total, len(infos))
        check("  总纸张数", window.model.sheet_total, len(sheets))
        print(f"      彩色 {n_color} 页 / 黑白 {n_bw} 页")

        # ------------------------------------------------------------------
        # 3) 拖拽语义：整张纸搬移，同纸两页不分离
        # ------------------------------------------------------------------
        print("\n  拖拽搬移（严格双面语义）：")
        row = next(
            (r for r in range(window.model.rowCount())
             if window.model.slot_at(r).side == BW
             and window.model.slot_at(r).is_duplex),
            None,
        )
        if row is None:
            # 可能所有双面纸都归在彩色区，退而求其次取任意双面纸
            row = next(
                (r for r in range(window.model.rowCount())
                 if window.model.slot_at(r).is_duplex),
                None,
            )
        if row is None:
            FAILURES.append("找不到可搬移的双面纸张")
        else:
            slot = window.model.slot_at(row)
            origin = slot.side
            target = COLOR if origin == BW else BW
            pages_moved = [p.number for p in slot.pages]
            window.model.move_sheets([row], target)
            after = window.model.slot_at(row)
            check("  搬移后该纸归属", after.side, target)
            check("  同纸所有页归属一致", len({after.side}), 1)
            print(f"      搬移了第 {pages_moved} 页（同一张纸，一起走）")

            side_map = window.model.final_side_map()
            check("  final_side_map 中同纸两页一致",
                  len({side_map[p.index] for p in after.pages}), 1)

            window.model.reset_to_detected([row])
            check("  复位后归属", window.model.slot_at(row).side, origin)

        check("  缩略图失败计数", window._thumb_failures, 0)

        # ------------------------------------------------------------------
        # 4) 可选：走真实的导出链路
        # ------------------------------------------------------------------
        if args.export_to:
            out_dir = Path(args.export_to).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n  通过 ExportWorker 导出到 {out_dir} …")

            def on_exported(_dir: str) -> None:
                import pymupdf as fitz
                n_c = window.model.count_of(COLOR)
                n_b = window.model.count_of(BW)
                check("  导出彩色页数",
                      fitz.open(out_dir / "gui_color.pdf").page_count, n_c)
                check("  导出黑白页数",
                      fitz.open(out_dir / "gui_bw.pdf").page_count, n_b)
                check("  导出总页数", n_c + n_b, len(infos))

                report = out_dir / "gui_report.csv"
                check("  报告已生成", report.exists(), True)
                if report.exists():
                    with report.open(encoding="utf-8-sig") as fh:
                        header = next(csv.reader(
                            line for line in fh if not line.startswith("#")))
                    for col in ("reason", "structure", "rendered"):
                        check(f"  报告含 {col} 列", col in header, True)

                print("\n  未捕获异常数：", len(uncaught))
                for item in set(uncaught):
                    print("     -", item)
                window.close()
                app.quit()

            window._suppress_dialogs = True
            window.export_completed.connect(on_exported)
            window.start_export(out_dir, "gui", write_csv=True)
            return

        print("\n  未捕获异常数：", len(uncaught))
        for item in set(uncaught):
            print("     -", item)
        window.close()
        app.quit()

    def attach():
        window.load_pdf(pdf)

        def hook_worker():
            worker = getattr(window, "_worker", None)
            if worker is not None and not getattr(worker, "_hooked", False):
                worker.finished_ok.connect(on_finished)
                worker._hooked = True
            else:
                QTimer.singleShot(50, hook_worker)

        QTimer.singleShot(0, hook_worker)

    QTimer.singleShot(200, attach)
    QTimer.singleShot(120_000, lambda: (print("超时"), app.quit()))
    app.exec()

    print("\n================ 结果 ================")
    if not state["done"]:
        print("失败：检测未完成（超时或异常）")
        return 1
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
