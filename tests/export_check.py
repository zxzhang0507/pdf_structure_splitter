"""导出链路端到端验证（无头）。

直接驱动 ``ExportWorker``（绕过文件对话框），断言：

1. 导出的两个 PDF **页数与检测结论一致**、两区互补、覆盖全部页；
2. 导出 PDF 的**逐页内容与原文件完全等价**（尺寸 + 文本 + 顺序）——
   ``create_output_pdf`` 走 ``insert_pdf`` 原样复制页面对象，因此
   每一页的文字层与尺寸都必须与源文件逐字节等价；
3. CSV 报告的**列与判定语义**正确。

**不用 MD5 比对**：``create_output_pdf`` 里的 ``save(garbage=3)`` 是非
确定性的（同一份代码同一份输入连跑两次，MD5 都不同），MD5 不能作为
回归判据。

**需要自备一份 PDF**（本项目不附带测试文件）。
用法::

    python tests/export_check.py --pdf D:\some\file.pdf
"""



from __future__ import annotations

import csv
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pymupdf as fitz  # noqa: E402

from core import (  # noqa: E402
    BW,
    COLOR,
    DEFAULT_COLOR_RATIO,
    DEFAULT_COLOR_THRESHOLD,
    DEFAULT_DPI,
    classify_pages,
    detect_page,
    enforce_duplex,
    open_input_pdf,
)
from tests.fixture import hint, resolve_pdf, set_pdf  # noqa: E402
from workers.export_worker import ExportWorker  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, expect) -> None:
    ok = got == expect
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f" != {expect!r}"))
    if not ok:
        FAILURES.append(f"{label}: {got!r} != {expect!r}")


def detect(pdf: Path):
    """用默认参数跑一遍检测（与 GUI / CLI 默认一致）。"""
    doc = open_input_pdf(pdf)
    infos = []
    for i in range(doc.page_count):
        info, _s = detect_page(
            doc[i], dpi=DEFAULT_DPI,
            color_threshold=DEFAULT_COLOR_THRESHOLD,
            color_ratio_threshold=DEFAULT_COLOR_RATIO,
        )
        info.index = i
        info.number = i + 1
        info.sheet = i // 2 + 1
        infos.append(info)
    doc.close()
    return classify_pages(infos)


def page_signature(path: Path):
    """逐页指纹：尺寸 + 全文。用来比较两份 PDF 内容是否等价。"""
    doc = fitz.open(path)
    try:
        return [
            (tuple(round(v, 2) for v in doc[i].rect), doc[i].get_text())
            for i in range(doc.page_count)
        ]
    finally:
        doc.close()


def main() -> int:
    import sys as _sys
    for i, arg in enumerate(_sys.argv):
        if arg == "--pdf" and i + 1 < len(_sys.argv):
            set_pdf(_sys.argv[i + 1])
    pdf = resolve_pdf()
    if pdf is None:
        print(hint())
        return 0
    src_pdf = pdf
    out_dir = _ROOT / "tests" / "_export_out"

    print("=== 1) 检测（默认参数，结构优先策略）===")
    infos, sheets = detect(pdf)
    n_color = sum(1 for p in infos if p.output == COLOR)
    n_bw = sum(1 for p in infos if p.output == BW)
    print(f"      {len(infos)} 页 / {len(sheets)} 张纸 · 彩色 {n_color} · 黑白 {n_bw}")
    check("两区互补覆盖全部页", n_color + n_bw, len(infos))
    check("总纸张数", len(sheets), (len(infos) + 1) // 2)

    # 每张纸内部归属必须一致（双面规则）
    bad = [
        s.number for s in sheets
        if len({infos[i].output for i in s.pages}) != 1
    ]
    check("每张纸内页面归属一致", bad, [])

    print("=== 2) 导出（模拟用户点导出，严格双面模式）===")
    side = {p.index: (COLOR if p.output == COLOR else BW) for p in infos}
    side = enforce_duplex(side)
    meta = {
        "source": pdf.name, "pages": len(infos), "sheets": len(sheets),
        "dpi": DEFAULT_DPI, "color_threshold": DEFAULT_COLOR_THRESHOLD,
        "color_ratio_threshold": DEFAULT_COLOR_RATIO, "strict_duplex": "是",
    }

    results = {}
    # 显式要求输出 CSV（默认是不输出的，见 test_gui_logic 的 test_csv_option）
    worker = ExportWorker(pdf, out_dir, "gui", side, infos, sheets, meta,
                          write_csv=True)
    worker.finished_ok.connect(lambda d: results.update(ok=d))
    worker.failed.connect(lambda m: results.update(err=m))
    worker.run()          # 同步跑（本来在 QThread 里，这里直接调用 run 便于断言）
    if "err" in results:
        print("  导出失败:", results["err"])
        FAILURES.append(f"导出失败: {results['err']}")
        return 1
    print("  输出目录:", results.get("ok"))

    color_pdf = out_dir / "gui_color.pdf"
    bw_pdf = out_dir / "gui_bw.pdf"
    report = out_dir / "gui_report.csv"

    check("彩色件页数 = 检测结论", fitz.open(color_pdf).page_count, n_color)
    check("黑白件页数 = 检测结论", fitz.open(bw_pdf).page_count, n_bw)
    check("导出总页数 = 源页数",
          fitz.open(color_pdf).page_count + fitz.open(bw_pdf).page_count,
          len(infos))
    check("报告已生成（write_csv=True）", report.exists(), True)

    print("=== 3) 导出内容与源文件逐页等价（尺寸/文本/顺序）===")
    # 原样复制页面对象 => 每个导出页的尺寸与文本必须与源文件对应页完全一致。
    # 这是"insert_pdf 不做重新渲染/重新编码"的直接验证。
    src_sig = page_signature(src_pdf)
    for label, out_path, want_side in (
        ("彩色件", color_pdf, COLOR),
        ("黑白件", bw_pdf, BW),
    ):
        doc = fitz.open(out_path)
        try:
            got = [
                (tuple(round(v, 2) for v in doc[i].rect), doc[i].get_text())
                for i in range(doc.page_count)
            ]
        finally:
            doc.close()

        # 该输出件应包含的源页（按 index 升序）
        idx = sorted(i for i, s in side.items() if s == want_side)
        check(f"{label} 页数", len(got), len(idx))

        # 顺序与内容都要对上
        mism = []
        for pos, src_index in enumerate(idx):
            if pos >= len(got):
                mism.append((pos, src_index, "缺页"))
                continue
            if got[pos] != src_sig[src_index]:
                same_text = got[pos][1] == src_sig[src_index][1]
                same_rect = got[pos][0] == src_sig[src_index][0]
                mism.append((pos, src_index,
                             f"text_eq={same_text} rect_eq={same_rect}"))
        check(f"{label} 逐页尺寸+文本与原文件一致", mism, [])

    print("=== 4) 报告列与新策略语义 ===")
    with report.open(encoding="utf-8-sig") as fh:
        lines = [l for l in fh if not l.startswith("#")]
    rows = list(csv.DictReader(lines))
    check("报告行数", len(rows), len(infos))
    check("列名", list(rows[0].keys()),
          ["page", "sheet", "page_color", "output", "reason", "structure",
           "color_ratio", "color_pixels", "total_pixels", "rendered",
           "sheet_output"])

    # 找一页"自身 BW 但与彩色页同纸"的，验证 output 被升级为 COLOR
    upgraded = [
        r for r in rows
        if r["page_color"] == BW and r["sheet_output"] == COLOR
    ]
    print(f"      自身黑白但因配对升级的页：{len(upgraded)} 页")
    for r in upgraded:
        check(f"  p{r['page']} output 应为 COLOR", r["output"], COLOR)

    # reason 必须非空，且"纯文字"页必须 rendered=否
    check("所有行 reason 非空",
          [r["page"] for r in rows if not r["reason"]], [])
    wrong = [r["page"] for r in rows
             if r["reason"] in ("纯文字", "黑白矢量") and r["rendered"] != "否"]
    check("纯文字/黑白矢量页 rendered 均为否", wrong, [])
    wrong2 = [r["page"] for r in rows
              if r["reason"] in ("位图彩色", "位图灰度") and r["rendered"] != "是"]
    check("位图页 rendered 均为是", wrong2, [])

    # 报告头部的统计要与实际一致
    with report.open(encoding="utf-8-sig") as fh:
        head = {}
        for line in fh:
            if not line.startswith("#"):
                break
            if ":" in line:
                k, v = line[1:].split(":", 1)
                head[k.strip()] = v.strip()
    check("头部 color_pages 一致", head.get("color_pages"), str(n_color))
    check("头部 bw_pages 一致", head.get("bw_pages"), str(n_bw))
    check("头部 rendered_pages 一致", head.get("rendered_pages"),
          str(sum(1 for p in infos if p.rendered)))

    print("\n================ 结果 ================")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("导出链路全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
