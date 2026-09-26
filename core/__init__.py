"""核心逻辑层：结构优先的颜色检测算法，**不含任何 Qt 依赖**。

拆成两个模块：

* :mod:`core.structure` —— 页面结构分析（文字/位图/矢量）与图形区域渲染。
  不依赖 ``split_pdf``，因此可被反向导入而不产生循环依赖。
* :mod:`core.split_pdf` —— 判定规则、双面配对、输入输出与命令行。

这样核心逻辑仍可脱离界面单独运行与单元测试。
"""

from .structure import (
    BIG_REGION_PT2,
    DEFAULT_COLOR_RATIO,
    DEFAULT_COLOR_THRESHOLD,
    DEFAULT_DPI,
    MAX_PIXELS,
    MAX_REGION_DPI,
    MAX_VECTOR_PATHS,
    MIN_REGION_PX,
    PageRenderError,
    PageStructure,
    RegionScan,
    analyze_page_color,
    analyze_page_structure,
    color_chroma,
    merge_rects,
    region_scale,
    render_page,
    render_region,
    scan_regions,
)

from .split_pdf import (
    BW,
    COLOR,
    REASON_EMPTY,
    REASON_GRAY_VECTOR,
    REASON_RASTER,
    REASON_RASTER_GRAY,
    REASON_RENDER_FAIL,
    REASON_TEXT_ONLY,
    REASON_VECTOR,
    PageInfo,
    PdfSplitterError,
    SheetInfo,
    build_page_infos,
    classify_pages,
    create_output_pdf,
    decide_color,
    detect_page,
    detect_pages,
    enforce_duplex,
    group_duplex_pages,
    open_input_pdf,
    resolve_output_paths,
    write_report,
)

__all__ = [
    # 常量
    "BW",
    "COLOR",
    "BIG_REGION_PT2",
    "DEFAULT_COLOR_RATIO",
    "DEFAULT_COLOR_THRESHOLD",
    "DEFAULT_DPI",
    "MAX_PIXELS",
    "MAX_REGION_DPI",
    "MAX_VECTOR_PATHS",
    "MIN_REGION_PX",
    "REASON_EMPTY",
    "REASON_GRAY_VECTOR",
    "REASON_RASTER",
    "REASON_RASTER_GRAY",
    "REASON_RENDER_FAIL",
    "REASON_TEXT_ONLY",
    "REASON_VECTOR",
    # 数据结构
    "PageInfo",
    "PageRenderError",
    "PageStructure",
    "PdfSplitterError",
    "RegionScan",
    "SheetInfo",
    # 结构分析
    "analyze_page_color",
    "analyze_page_structure",
    "color_chroma",
    "merge_rects",
    "region_scale",
    "render_page",
    "render_region",
    "scan_regions",
    # 判定与流程
    "build_page_infos",
    "classify_pages",
    "create_output_pdf",
    "decide_color",
    "detect_page",
    "detect_pages",
    "enforce_duplex",
    "group_duplex_pages",
    "open_input_pdf",
    "resolve_output_paths",
    "write_report",
]
