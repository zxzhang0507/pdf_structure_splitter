"""pdf_structure_splitter GUI 入口。

用 ``.pyw`` 后缀是为了在 Windows 上以 pythonw.exe 启动，不弹控制台窗口。

开发态运行::

    python main.pyw

也可以直接传入 PDF 路径（或在资源管理器里把 PDF 拖到 exe 上）::

    python main.pyw D:\\path\\to\\file.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从源码目录直接运行（打包后 PyInstaller 会把根目录加进 sys.path）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow, _resource_path, load_stylesheet  # noqa: E402


def _parse_pdf_arg(argv: list[str]) -> Path | None:
    """从命令行里取第一个看起来存在的 PDF 路径。

    支持「把 PDF 拖到 exe 图标上」这种用法 —— Windows 会把路径作为
    第一个参数传进来（含空格的路径会被加引号，Qt 已剥掉）。
    """
    for raw in argv[1:]:
        if raw.startswith("-"):
            continue
        candidate = Path(raw)
        if candidate.suffix.lower() == ".pdf" and candidate.is_file():
            return candidate
    return None


def _run_selftest(argv: list[str]) -> int:
    """``--selftest <pdf> [输出目录]``：不显示界面，跑完整流程并写结果文件。

    用途：在一台**未装 Python** 的机器上确认打包产物真的能干活 ——
    「能启动窗口」不等于「fitz / numpy / PySide6 都完整可用」。

    成功返回 0，失败返回 1，结果同时打印到 stdout 与 ``selftest_result.txt``。
    """
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print("用法: pdf_structure_splitter.exe --selftest <input.pdf> [输出目录]")
        return 2

    pdf = Path(args[0])
    out_dir = Path(args[1]) if len(args) > 1 else pdf.parent / "_selftest_out"
    log_path = out_dir.parent / "selftest_result.txt"

    lines: list[str] = []
    ok = True
    try:
        # 延迟导入，确保 --selftest 之外的启动路径不受影响
        sys.path.insert(0, str(_ROOT))
        from core import (
            BW,  # noqa: F401 - 供下方统计使用
            COLOR,
            classify_pages,
            create_output_pdf,
            detect_page,
            open_input_pdf,
            write_report,
        )

        lines.append(f"pdf_structure_splitter selftest")
        lines.append(f"输入: {pdf}")
        lines.append(f"版本/运行环境: Python {sys.version.split()[0]}")
        import pymupdf
        lines.append(f"PyMuPDF: {pymupdf.__doc__.strip().splitlines()[0]}")
        import numpy
        lines.append(f"numpy: {numpy.__version__}")
        from PySide6 import __version__ as pyside_ver
        lines.append(f"PySide6: {pyside_ver}")
        lines.append(f"frozen(是否打包运行): {getattr(sys, 'frozen', False)}")
        lines.append("")

        # 打印模块是打包时最容易漏掉的（QtPrintSupport 需要显式声明），
        # 单独探测一次，避免「能导出但一按打印就崩」。
        #
        # ⚠ 注意：QPrinter 必须在有 QApplication 的前提下实例化，
        # 否则会**直接崩溃**（Windows 上 0xC0000409，测不到异常）。
        # --selftest 是非 GUI 路径，所以要在这里临时建一个 QApplication。
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

            # 复用已有的 QApplication（若 GUI 已启动），否则新建一个
            app = QApplication.instance() or QApplication([sys.argv[0]])
            _ = app                                     # 仅为持有生命周期
            printer_count = len(QPrinterInfo.availablePrinters())
            QPrinter(QPrinter.HighResolution)           # 真正实例化一次
            lines.append(f"打印模块: 可用（系统检测到 {printer_count} 台打印机）")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"打印模块: 不可用 —— {type(exc).__name__}: {exc}")
            ok = False
        lines.append("")

        # 用程序真实的默认参数（而不是写死），这样自检结果能反映实际行为
        from core import (
            DEFAULT_COLOR_RATIO,
            DEFAULT_COLOR_THRESHOLD,
            DEFAULT_DPI,
        )

        dpi = DEFAULT_DPI
        threshold = DEFAULT_COLOR_THRESHOLD
        ratio_thr = DEFAULT_COLOR_RATIO
        lines.append(
            f"检测参数: dpi={dpi}, 色度阈值={threshold}, 彩色占比={ratio_thr}"
        )

        doc = open_input_pdf(pdf)
        lines.append(f"页数: {doc.page_count}")

        infos = []
        for i in range(doc.page_count):
            info, _struct = detect_page(
                doc[i], dpi=dpi, color_threshold=threshold,
                color_ratio_threshold=ratio_thr,
            )
            info.index = i
            info.number = i + 1
            info.sheet = i // 2 + 1
            infos.append(info)

        infos, sheets = classify_pages(infos)
        color_idx = sorted(i for i, p in enumerate(infos) if p.output == COLOR)
        bw_idx = sorted(i for i, p in enumerate(infos) if p.output == BW)
        rendered = [p for p in infos if p.rendered]
        lines.append(f"纸张数: {len(sheets)}")
        lines.append(f"彩色页: {len(color_idx)}")
        lines.append(f"黑白页: {len(bw_idx)}")
        lines.append(
            f"渲染页: {len(rendered)}/{len(infos)}"
            f"（{len(infos) - len(rendered)} 页仅靠页面结构判定，未做像素渲染）"
        )
        for reason in ("纯文字", "黑白矢量", "彩色矢量",
                       "位图彩色", "位图灰度", "空白页"):
            count = sum(1 for p in infos if p.reason == reason)
            if count:
                lines.append(f"  {reason}: {count} 页")

        out_dir.mkdir(parents=True, exist_ok=True)
        color_pdf = out_dir / "selftest_color.pdf"
        bw_pdf = out_dir / "selftest_bw.pdf"
        report = out_dir / "selftest_report.csv"
        create_output_pdf(doc, color_idx, color_pdf)
        create_output_pdf(doc, bw_idx, bw_pdf)
        write_report(report, infos, sheets, {
            "source": pdf.name, "pages": len(infos), "sheets": len(sheets),
            "dpi": dpi, "color_threshold": threshold,
            "color_ratio_threshold": ratio_thr,
            "color_pages": len(color_idx), "bw_pages": len(bw_idx),
        })
        doc.close()

        lines.append("")
        lines.append(f"输出彩色件: {color_pdf.name} ({len(color_idx)} 页)")
        lines.append(f"输出黑白件: {bw_pdf.name} ({len(bw_idx)} 页)")
        lines.append(f"输出报告: {report.name}")
        lines.append("")
        lines.append("结果: 通过 —— 打包产物的 PDF 读取、颜色检测、页面复制全部正常")

    except Exception as exc:  # noqa: BLE001 - 自检要把任何异常都报告出来
        import traceback
        ok = False
        lines.append("")
        lines.append(f"结果: 失败")
        lines.append(traceback.format_exc())

    text = "\n".join(lines)
    print(text)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 1


def main() -> int:
    # 自检模式：不建窗口、不用 Qt 事件循环，纯命令行跑完整流程。
    # 放在最前面，避免在无显示环境（或打包后）触碰 GUI。
    if "--selftest" in sys.argv:
        # 让 print 能输出中文（Windows 控制台默认 GBK）
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        return _run_selftest(sys.argv)

    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("pdf_structure_splitter")
    app.setApplicationDisplayName("pdf_structure_splitter")

    icon_path = _resource_path("resources", "app.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    stylesheet = load_stylesheet()
    if stylesheet:
        app.setStyleSheet(stylesheet)

    window = MainWindow()
    window.show()

    # 命令行带了 PDF 就直接开始检测；否则等用户点「打开」
    pdf_arg = _parse_pdf_arg(sys.argv)
    if pdf_arg is not None:
        window.load_pdf(pdf_arg)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
