"""无头截图：加载 PDF 后把主窗口渲染成 PNG，用于人工确认布局与分区。

用法（PowerShell）::

    $env:QT_QPA_PLATFORM="offscreen"; python tests/shot.py [输出png] [--pdf xxx.pdf]

无头模式下窗口尺寸由 ``resize`` 显式指定（offscreen 平台不会真正显示窗口）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow, load_stylesheet  # noqa: E402


def _load_system_fonts() -> int:
    """offscreen 平台没有字体（全渲染成方框），手动注入系统字体。

    真实桌面环境下 Qt 自己会加载，这里仅为让无头截图可读。
    """
    fonts_dir = Path("C:/Windows/Fonts")
    wanted = ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "segoeui.ttf", "segoeuib.ttf"]
    loaded = 0
    for name in wanted:
        path = fonts_dir / name
        if path.exists() and QFontDatabase.addApplicationFont(str(path)) >= 0:
            loaded += 1
    return loaded


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else _ROOT / "shot.png"
    sys.path.insert(0, str(_ROOT))
    from tests.fixture import resolve_pdf
    for i, arg in enumerate(sys.argv):
        if arg == "--pdf" and i + 1 < len(sys.argv):
            from tests.fixture import set_pdf
            set_pdf(sys.argv[i + 1])
    pdf = resolve_pdf()
    if pdf is None:
        from tests.fixture import hint
        print(hint())
        return 2

    app = QApplication(sys.argv)
    n_fonts = _load_system_fonts()
    print(f"已注入 {n_fonts} 个系统字体，可用字体族 {len(QFontDatabase.families())} 个")
    app.setStyleSheet(load_stylesheet())
    window = MainWindow()
    window.resize(1280, 800)
    window.show()

    shots = {"n": 0}

    def grab(tag: str) -> None:
        shots["n"] += 1
        target = out if shots["n"] == 1 else out.with_name(f"{out.stem}_{tag}{out.suffix}")
        window.grab().save(str(target))
        print("已保存截图:", target)

    def start():
        window.load_pdf(pdf)
        # 检测约 7 s；完成后再等缩略图渲染
        QTimer.singleShot(11000, lambda: (window._render_visible_thumbnails(), None))
        QTimer.singleShot(12500, lambda: grab("loaded"))
        QTimer.singleShot(14000, lambda: (window.close(), app.quit()))

    QTimer.singleShot(200, start)
    QTimer.singleShot(60_000, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
