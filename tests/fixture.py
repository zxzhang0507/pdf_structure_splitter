"""测试用 PDF 的定位与校验。

**本项目不附带任何测试 PDF**，也不生成样本文件 —— 请自备一份用于测试的
PDF（内容可以公开的那种），放到项目根目录或指定路径。

定位顺序：

1. 命令行 ``--pdf`` 指定的路径
2. 环境变量 ``PSS_TEST_PDF``
3. 项目根目录下的 ``test.pdf``

找不到时，需要真实 PDF 的测试会**自动跳过并说明原因**，不会失败 ——
纯逻辑测试不受影响，随时可以跑。

用法::

    from fixture import resolve_pdf, require_pdf

    pdf = resolve_pdf()          # 找不到返回 None
    pdf = require_pdf()          # 找不到抛 SkipTest（由测试捕获）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: 命令行传入的路径（测试入口解析 --pdf 后写入）
_cli_path: Path | None = None


class SkipTest(Exception):
    """表示该测试因缺少输入文件而跳过（不算失败）。"""


def set_pdf(path: str | Path | None) -> None:
    """由测试入口把 ``--pdf`` 的结果登记进来。"""
    global _cli_path
    _cli_path = Path(path).resolve() if path else None


def candidates() -> list[Path]:
    """按优先级返回所有候选路径。"""
    out: list[Path] = []
    if _cli_path:
        out.append(_cli_path)
    env = os.environ.get("PSS_TEST_PDF")
    if env:
        out.append(Path(env).expanduser().resolve())
    out.append(_ROOT / "test.pdf")
    out.append(_ROOT.parent / "test.pdf")
    return out


def resolve_pdf() -> Path | None:
    """返回第一个存在的候选 PDF；都没有则返回 ``None``。"""
    for p in candidates():
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def require_pdf() -> Path:
    """同 :func:`resolve_pdf`，但找不到时抛 :class:`SkipTest`。"""
    pdf = resolve_pdf()
    if pdf is None:
        raise SkipTest(
            "未提供测试用 PDF。请把一份 PDF 放到项目根目录并命名为 "
            "test.pdf，或用 --pdf <路径> / 环境变量 PSS_TEST_PDF 指定。"
        )
    return pdf


def hint() -> str:
    """给用户看的提示文本（找不到 PDF 时打印）。"""
    return (
        "跳过：未提供测试用 PDF。\n"
        "     请自备一份 PDF（内容可公开的那种），任选一种方式指定：\n"
        "       · 放到项目根目录并命名为 test.pdf\n"
        "       · 运行时加 --pdf <路径>\n"
        "       · 设置环境变量 PSS_TEST_PDF=<路径>\n"
        "     纯逻辑测试（test_structure / test_classify）不受影响。"
    )


if __name__ == "__main__":
    p = resolve_pdf()
    print(f"测试用 PDF：{p}" if p else hint())
