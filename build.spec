# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台窗口。

用法::

    pyinstaller --noconfirm --clean build.spec

**关于 hiddenimports**：PyMuPDF 有动态导入路径，PyInstaller 的静态分析经常
漏掉，导致打包后启动即崩（``ModuleNotFoundError: fitz``）。因此显式声明
``pymupdf`` 与 ``fitz`` —— 这是最常见的打包坑。

> 注意：设计文档 §9.2 的示例里还写了 ``PIL.ImageQt``，但本项目**没有使用
> Pillow**（``ui/thumbnail.py`` 走 numpy -> QImage 的路径）。若照抄那行，
> 打包会因找不到 Pillow 而失败，因此这里**不含** ``PIL``。
"""

from pathlib import Path

_ROOT = Path(SPECPATH)

a = Analysis(
    ["main.pyw"],
    pathex=[str(_ROOT)],
    binaries=[],
    # 样式表要打进包里；``resources`` 整个目录一并带上（含 app.ico）
    datas=[
        ("ui/style.qss", "ui"),
        ("resources", "resources"),
    ],
    hiddenimports=[
        "pymupdf",
        "fitz",
        # PyMuPDF 内部的动态子模块，保险起见一并声明
        "pymupdf.mupdf",
        "pymupdf.utils",
        "pymupdf.table",
        # 打印功能用到；PySide6 的 hook 有时会漏掉 QPrinter 相关模块
        "PySide6.QtPrintSupport",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除用不到的重型依赖，显著减小体积
    excludes=[
        "tkinter",
        "matplotlib",
        "pytest",
        "scipy",
        "pandas",
        "IPython",
        # 我们不用 Pillow（缩略图走 numpy -> QImage），
        # 但 PyMuPDF 有可选集成会把 PIL 拖进来
        "PIL",
        "PIL.ImageQt",
        # PyMuPDF 不需要 lxml
        "lxml",
        "_elementpath",
        # 未使用的 Qt 模块（含 QML / Quick / WebEngine / Qt 自带 PDF）
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebChannel",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQml",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtBluetooth",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtNetworkAuth",
        "PySide6.QtNfc",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPositioning",
        "PySide6.QtQmlModels",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio",
        "PySide6.QtSql",
        "PySide6.QtStateMachine",
        "PySide6.QtSvgWidgets",
        "PySide6.QtTest",
        "PySide6.QtTextToSpeech",
        "PySide6.QtWebSockets",
        "PySide6.QtXml",
    ],
    noarchive=False,
    optimize=0,
)

# ----------------------------------------------------------------------
# 二进制过滤：剔除静态分析拉进来但运行时用不到的大块头
#
# 这些模块在 ``excludes`` 里排除后，PySide6 的 hook 有时仍会把对应 DLL
# 塞进 binaries，所以这里再按文件名滤一遍。
# ----------------------------------------------------------------------

#: 要剔除的 DLL / 扩展模块（按文件名匹配）
_DROP_BINARIES = {
    # Qt 的软件 OpenGL 回退实现，约 19.7 MB；本程序不碰 OpenGL
    "opengl32sw.dll",
    # QML / Quick 运行时，约 11 MB；界面只用 Widgets
    "qt6qml.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6quick.dll",
    "qt6quickwidgets.dll",
    "qt6quickcontrols2.dll",
    "qt6quickshapes.dll",
    "qt6quicktemplates2.dll",
    "qt6quickdialogs2.dll",
    "qt6quicklayouts.dll",
    # Qt 自带 PDF 引擎，约 4.4 MB；我们用 PyMuPDF 读写 PDF
    "qt6pdf.dll",
    "qt6pdfwidgets.dll",
    # Qt 网络/多媒体等未用到的运行时
    "qt6network.dll",
    "qt6multimedia.dll",
    "qt6websockets.dll",
    "qt6sql.dll",
    "qt6test.dll",
    "qt6svgwidgets.dll",
    "qt6designer.dll",
    "qt6help.dll",
    # Pillow / lxml 的 C 扩展（已在 excludes 里排除纯 Python 部分）
    "_imaging.pyd",
    "etree.cp311-win_amd64.pyd",
    "_elementpath",
}

#: 注意：**不要**剔除 numpy 的 OpenBLAS（libopenblas*.dll，约 36 MB）。
#: 实测过：numpy 1.26 的 ``_multiarray_umath`` 扩展模块硬依赖它，
#: 删掉后即使本程序只做逐元素 max/min，导入 numpy 时也会直接
#: 报 ``ImportError: DLL load failed while importing _multiarray_umath``。
#: 这 36 MB 省不掉，是 numpy 的固有开销。

_DROP_SUFFIXES = (".pyc", ".pyo")


def _keep_binary(entry) -> bool:
    """判断某个 binary 条目是否保留。"""
    name = Path(str(entry[0])).name.lower()
    return name not in _DROP_BINARIES


a.binaries = [entry for entry in a.binaries if _keep_binary(entry)]
a.datas = [
    entry
    for entry in a.datas
    if not Path(str(entry[0])).name.lower().endswith(_DROP_SUFFIXES)
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pdf_structure_splitter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # PySide6 压缩后偶发兼容问题，不压
    runtime_tmpdir=None,
    console=False,             # 等价 --windowed，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="resources/app.ico",
)
