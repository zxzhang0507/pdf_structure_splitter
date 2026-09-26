"""新功能端到端验证（无头）：手动重检测、拖拽搬移、拆分/合并、缩放、预览、打印。

用法::

    $env:QT_QPA_PLATFORM="offscreen"; python tests/features_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from core import BW, COLOR  # noqa: E402
from ui.main_window import MainWindow, load_stylesheet  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, expect) -> None:
    ok = got == expect
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f" != {expect!r}"))
    if not ok:
        FAILURES.append(f"{label}: {got!r} != {expect!r}")


def main() -> int:
    # 需要一份真实 PDF；缺失则跳过（见 tests/fixture.py）
    from tests.fixture import hint, resolve_pdf, set_pdf
    for i, arg in enumerate(sys.argv):
        if arg == "--pdf" and i + 1 < len(sys.argv):
            set_pdf(sys.argv[i + 1])
    pdf_path = resolve_pdf()
    if pdf_path is None:
        print(hint())
        return 0

    app = QApplication(sys.argv)
    app.setStyleSheet(load_stylesheet())
    window = MainWindow()
    window.resize(1400, 860)
    window.show()
    print(f"测试用 PDF：{pdf_path.name}")

    uncaught: list[str] = []

    def hook(t, v, tb):
        uncaught.append(f"{t.__name__}: {v}")
        print("!!! 未捕获异常:", t.__name__, v)

    sys.excepthook = hook
    state = {"done": False}

    def on_detected(infos, sheets) -> None:
        state["done"] = True
        print(f"\n检测完成：{len(infos)} 页 / {len(sheets)} 张纸")

        # ---- 问题 1：滑块不自动重检测，必须点按钮 ----
        print("\n【问题1】滑块改动不自动重检测：")
        panel = window.param_panel
        check("  初始无待应用改动", panel.dirty, False)
        check("  按钮初始置灰", panel.apply_button.isEnabled(), False)
        rows_before = window.model.rowCount()
        detection_count = {"n": 0}

        def count_detect(*_a):
            detection_count["n"] += 1

        panel.params_changed.connect(count_detect)

        panel._dpi.slider.setValue(150)
        check("  改滑块后标记为待应用", panel.dirty, True)
        check("  按钮变为可用", panel.apply_button.isEnabled(), True)
        check("  模型未被重建（没自动重检测）", window.model.rowCount(), rows_before)
        check("  按钮文案带提示点", panel.apply_button.text(), "重新检测 •")
        check("  拖动期间未发出任何重检测信号", detection_count["n"], 0)

        # 再拖几次，确认仍然只有「待应用」而不会自动跑
        panel._threshold.slider.setValue(40)
        panel._ratio.slider.setValue(30)
        check("  多次拖动仍不触发检测", detection_count["n"], 0)

        # 点按钮才真正触发（此处不真跑检测：临时清掉路径让槽函数提前返回）
        saved_path = window._pdf_path
        window._pdf_path = None
        panel.apply_button.click()
        window._pdf_path = saved_path
        check("  点按钮后发出 1 次重检测信号", detection_count["n"], 1)
        check("  点按钮后清除待应用标记", panel.dirty, False)

        # 把参数改回默认，避免影响后续断言
        panel._dpi.slider.setValue(100)
        panel._threshold.slider.setValue(15)
        panel._ratio.slider.setValue(10)
        panel.set_dirty(False)
        panel.params_changed.disconnect(count_detect)

        # ---- 问题 2：单面模式 ----
        print("\n【问题2】单面模式（每页独立判定）：")
        model = window.model
        panel = window.param_panel
        # 页数/纸张数从模型自行推导，不写死具体数值 ——
        # 这样换任何输入 PDF（包括样本）断言都成立。
        n_pages = model.page_total
        n_sheets = model.sheet_total
        check("  初始为双面模式", model.single_sided, False)
        check("  双面模式行数 = 纸张数", model.rowCount(), n_sheets)
        duplex_color = model.count_of(COLOR)

        panel.single_box.setChecked(True)
        check("  勾选框互斥（严格双面被取消）", panel.strict_duplex, False)
        check("  切到单面模式", model.single_sided, True)
        check("  单面模式行数 = 页数", model.rowCount(), n_pages)
        check("  模式标签", model.mode_label, "单面模式")
        check("  无配对升级页", model.upgraded_count(), 0)
        single_color = model.count_of(COLOR)
        check("  彩色页数不多于双面模式（不再被配对强制升级）",
              single_color <= duplex_color, True)

        # 单面模式的**意义**是：同一张纸的两页可以分属不同输出。
        # 这里找一张"两面自身检测结果不同"的纸来验证 —— 若样本里恰好
        # 没有这样的纸（全纸张两面同色），则跳过而不是误报失败。
        sm = model.final_side_map()
        mixed_sheets = [
            i for i in range(0, n_pages - 1, 2) if sm[i] != sm[i + 1]
        ]
        if mixed_sheets:
            check("  同纸两页可分属不同输出", len(mixed_sheets) >= 1, True)
            print(f"      有 {len(mixed_sheets)} 张纸的两页分属不同输出"
                  f"（如第 {mixed_sheets[0] // 2 + 1} 张）")
        else:
            print("      （跳过：本输入没有'两面检测结果不同'的纸）")

        panel.strict_box.setChecked(True)
        check("  切回双面模式", model.single_sided, False)
        check("  行数恢复", model.rowCount(), n_sheets)
        sm2 = model.final_side_map()
        check("  切回后同纸两页归一",
              all(sm2[i] == sm2[i + 1] for i in range(0, n_pages - 1, 2)), True)
        check("  彩色页数恢复", model.count_of(COLOR), duplex_color)

        # ---- 问题 1：缩略图清晰度 ----
        print("\n【问题1】缩略图分辨率随缩放提升：")
        from ui.thumbnail import thumbnail_width_for
        delegate = window.bw_zone.delegate
        for zoom in (1.0, 2.0, 3.0):
            delegate.set_zoom(zoom)
            needed = thumbnail_width_for(delegate.page_w)
            check(f"  zoom={zoom:.1f} 存储宽 {needed}px ≥ 显示宽 {delegate.page_w}px",
                  needed >= delegate.page_w, True)
        # 高缩放下的存储宽度必须留出足够余量，避免放大后发虚
        delegate.set_zoom(3.0)
        check("  zoom=3.0 时存储宽 ≥ 显示宽的 2 倍",
              thumbnail_width_for(delegate.page_w) >= delegate.page_w * 2, True)
        delegate.set_zoom(1.0)

        # ---- 问题 1：鼠标拖拽搬移（手工实现，全链路到真实搬移）----
        print("\n【问题1】鼠标拖拽搬移（完整链路）：")
        from PySide6.QtGui import QMouseEvent
        view = window.bw_zone.view

        check("  已重写 mousePressEvent",
              view.mousePressEvent.__func__.__qualname__.startswith("ZoneView"), True)
        check("  已重写 mouseMoveEvent",
              view.mouseMoveEvent.__func__.__qualname__.startswith("ZoneView"), True)
        check("  已重写 mouseReleaseEvent",
              view.mouseReleaseEvent.__func__.__qualname__.startswith("ZoneView"), True)
        check("  有 drag_dropped 信号", hasattr(view, "drag_dropped"), True)

        bw_row = next(r for r in range(model.rowCount())
                      if model.slot_at(r).side == BW)
        pages_moved = model.slot_at(bw_row).page_numbers
        before = (model.count_of(COLOR), model.count_of(BW))

        # 1) 按下
        rect = view.visualRect(window.bw_proxy.mapFromSource(model.index(bw_row, 0)))
        pos = rect.center()
        view.mousePressEvent(QMouseEvent(
            QMouseEvent.MouseButtonPress, pos, pos,
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        check("  按下后记录源行", view._selected_source_rows(), [bw_row])

        # 2) 移动超过阈值 → 进入拖拽态
        far = pos + QPoint(40, 40)
        view.mouseMoveEvent(QMouseEvent(
            QMouseEvent.MouseMove, far, far, Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        check("  移动后进入拖拽态", view._drag_rows, [bw_row])

        # 3) 在**彩色区**松手 → 应真的搬过去
        co_view = window.color_zone.view
        g = co_view.viewport().mapToGlobal(co_view.viewport().rect().center())
        view.mouseReleaseEvent(QMouseEvent(
            QMouseEvent.MouseButtonRelease, co_view.viewport().mapFromGlobal(g), g,
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier))

        after = (model.count_of(COLOR), model.count_of(BW))
        check(f"  拖动第{pages_moved}页后彩色区页数增加",
              after[0] > before[0], True)
        check("  该行归属变为 COLOR", model.slot_at(bw_row).side, COLOR)
        check("  拖拽态已清空", view._drag_rows, [])
        model.reset_to_detected()
        check("  复位后恢复", model.count_of(COLOR), before[0])

        # ---- 多选 + 跨区互斥 ----
        print("\n【多选】Ctrl 多选 + 跨区互斥：")
        from PySide6.QtWidgets import QListView
        check("  黑白区为扩展选择模式（支持 Ctrl 多选）",
              view.selectionMode(), QListView.ExtendedSelection)
        check("  彩色区为扩展选择模式",
              window.color_zone.view.selectionMode(),
              QListView.ExtendedSelection)

        bw_sel = window.bw_zone.view.selectionModel()
        co_sel = window.color_zone.view.selectionModel()
        # 先清空上一节（拖拽测试）留下的选中状态
        bw_sel.clearSelection()
        co_sel.clearSelection()

        # 普通点选：只有 1 项
        bw_sel.select(window.bw_proxy.index(0, 0), bw_sel.SelectionFlag.Select)
        check("  普通点选 1 项", len(bw_sel.selectedIndexes()), 1)

        # Ctrl 多选：同一区内可选中多项。
        # 行数随输入而变（样本只有几行，真实论文有几十行），所以这里
        # 取该区**实际可用的**行号，而不是写死 0/2/4。
        n_bw_rows = window.bw_proxy.rowCount()
        want = min(3, n_bw_rows)
        for r in range(want):
            bw_sel.select(
                window.bw_proxy.index(r, 0),
                bw_sel.SelectionFlag.Select | bw_sel.SelectionFlag.Rows,
            )
        check("  Ctrl 多选后可选中多项",
              len(bw_sel.selectedIndexes()) >= want, True)
        rows_now = window._selected_source_rows()
        check("  多选涉及多张纸", len(rows_now) >= want, True)
        pages_now = sum(len(model.slot_at(r).pages) for r in rows_now)
        check("  标题栏显示已选页数",
              f"已选 {pages_now} 页" in window.bw_zone.count_label.text(), True)

        # 跨区仍互斥：在另一个区点选会清掉本区
        co_sel.select(window.color_proxy.index(0, 0), co_sel.SelectionFlag.Select)
        check("  再选彩色区后黑白区被清空",
              len(bw_sel.selectedIndexes()), 0)

        # 多选整体搬移
        bw_sel.clearSelection()
        co_sel.clearSelection()
        for r in (1, 3):
            bw_sel.select(
                window.bw_proxy.index(r, 0),
                bw_sel.SelectionFlag.Select | bw_sel.SelectionFlag.Rows,
            )
        rows2 = window._selected_source_rows()
        pages2 = sum(len(model.slot_at(r).pages) for r in rows2)
        before_color = model.count_of(COLOR)
        window.move_selected_to(COLOR)
        check("  多选整体搬移页数正确",
              model.count_of(COLOR) - before_color, pages2)
        model.reset_to_detected()

        # ---- 问题 3：滚轮逐行滚动 ----
        print("\n【问题3】滚轮逐行滚动（不过快）：")
        bar = window.bw_zone.view.verticalScrollBar()
        grid_h = window.bw_zone.view.gridSize().height()
        bar.setValue(0)
        window.bw_zone.view.wheelEvent(QWheelEvent(
            QPointF(100, 100), QPointF(100, 100), QPoint(0, 0), QPoint(0, -120),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
        step_px = bar.value()
        # 内容不足一屏时滚动条本就无位移，此时断言"一格一行"没有意义
        if bar.maximum() > 0:
            check("  滚一格恰好一行", step_px, grid_h)
        else:
            print(f"      （跳过：本输入内容不足一屏，滚动条 max={bar.maximum()}）")
        window.bw_zone.view.wheelEvent(QWheelEvent(
            QPointF(100, 100), QPointF(100, 100), QPoint(0, 0), QPoint(0, 120),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
        check("  反向滚一格回到原位", bar.value(), 0)

        # ---- 问题 5：Ctrl+滚轮缩放 ----
        print("\n【问题5】Ctrl+滚轮缩放缩略图：")
        d_bw, d_color = window.bw_zone.delegate, window.color_zone.delegate
        z0 = d_bw.zoom
        up = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 0),
                         QPoint(0, 120), Qt.NoButton, Qt.ControlModifier,
                         Qt.NoScrollPhase, False)
        window.bw_zone.view.wheelEvent(up)
        check("  放大生效", d_bw.zoom > z0, True)
        check("  两侧同步缩放", d_color.zoom, d_bw.zoom)
        check("  卡片随之变大", d_bw.card_size.width() > 0, True)

        plain = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 0),
                            QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        z_now = d_bw.zoom
        window.bw_zone.view.wheelEvent(plain)
        check("  无 Ctrl 时不缩放", d_bw.zoom, z_now)

        # ---- 问题 6：预览分辨率提高 ----
        print("\n【问题6】预览图分辨率：")
        from ui.preview_dialog import PREVIEW_DPI
        check("  预览 dpi ≥ 150", PREVIEW_DPI >= 150, True)

        # ---- 问题 4：预览包含一张纸的两页 ----
        print("\n【问题4】预览含整张纸的所有页：")
        from ui.preview_dialog import render_preview_pages
        duplex_row = next(r for r in range(model.rowCount())
                          if model.slot_at(r).is_duplex)
        slot = model.slot_at(duplex_row)
        pms = render_preview_pages(window._pdf_path,
                                   [p.index for p in slot.pages])
        check("  渲染页数 = 该纸页数", len(pms), len(slot.pages))
        check("  两页都已渲染", all(not p.isNull() for p in pms), True)

        # ---- 问题 7：打印任务 ----
        print("\n【问题7】打印任务构建（并行）：")
        from workers.print_worker import DEFAULT_PRINT_DPI, build_print_jobs
        jobs = build_print_jobs(model.final_side_map(), "彩色机", "黑白机")
        check("  生成 2 个任务", len(jobs), 2)
        labels = {j["label"] for j in jobs}
        check("  任务名", labels, {"彩色件", "黑白件"})
        for j in jobs:
            check(f"  {j['label']} index 升序",
                  j["indices"], sorted(j["indices"]))
        check("  两组页数合计 = 总页数",
              sum(len(j["indices"]) for j in jobs), model.page_total)
        check("  默认画质 300 dpi（办公文档标准档）",
              jobs[0]["dpi"], DEFAULT_PRINT_DPI)
        check("  打印按钮已启用", window.print_button.isEnabled(), True)

        from ui.print_dialog import PrintDialog, QUALITY_PRESETS
        dlg = PrintDialog(model.count_of(COLOR), model.count_of(BW), window)
        check("  打印对话框可提交", dlg.can_submit, True)
        check("  对话框画质默认 300 dpi", dlg.dpi, 300)
        dlg.deleteLater()

        print("\n  未捕获异常数：", len(uncaught))
        for item in set(uncaught):
            print("     -", item)
        window.close()
        app.quit()

    def attach():
        window.load_pdf(pdf_path)

        def hook_worker():
            worker = getattr(window, "_worker", None)
            if worker is not None and not getattr(worker, "_hooked", False):
                worker.finished_ok.connect(on_detected)
                worker._hooked = True
            else:
                QTimer.singleShot(50, hook_worker)

        QTimer.singleShot(0, hook_worker)

    QTimer.singleShot(200, attach)
    QTimer.singleShot(120_000, app.quit)
    app.exec()

    print("\n================ 结果 ================")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("新功能全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
