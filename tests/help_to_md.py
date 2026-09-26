"""把 ``ui/help_dialog.py`` 里的帮助内容导出成 Markdown。

帮助页面里的 HTML 是**唯一数据源**（程序内 F1 打开的就是它），因此这里
只做单向导出，不维护第二份内容 —— 改了帮助页后重跑本脚本即可同步。

为什么用脚本而不是手写一份 md：手写的副本一定会和帮助页正文漂移，
而过时的文档比没有文档更糟。

用法::

    python tests/help_to_md.py                    # 输出到 使用说明.md
    python tests/help_to_md.py -o 帮助.md          # 指定输出
    python tests/help_to_md.py --check            # 只校验是否与现有 md 一致
"""

from __future__ import annotations

import argparse
import html as html_mod
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def _inline(text: str) -> str:
    """把行内 HTML 标签转成 Markdown 行内语法。

    Qt 的 QTextBrowser 只用到一个很小的 HTML 子集，所以这里的规则可以
    写得很紧：只处理实际出现过的标签（strong/b、code、em/i、br）。
    """
    # <br> 先换成占位符，避免被后续的空白归一化吃掉
    text = re.sub(r"<br\s*/?>", "\x00", text, flags=re.I)
    text = re.sub(r"<(?:b|strong)>(.*?)</(?:b|strong)>", r"**\1**",
                  text, flags=re.I | re.S)
    text = re.sub(r"<(?:i|em)>(.*?)</(?:i|em)>", r"*\1*",
                  text, flags=re.I | re.S)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.I | re.S)
    # 剩余标签一律剥掉（span 等）
    text = re.sub(r"<[^>]+>", "", text)
    # HTML 实体
    text = html_mod.unescape(text)
    # 去掉源码里的缩进换行（帮助页正文是缩进的多行字面量）
    text = re.sub(r"\s*\n\s*", " ", text)
    # 空格归一化。
    #
    # 帮助页正文是缩进的多行字面量，源码里的换行缩进会被折成一个空格，
    # 于是中文句子中间出现「彩色， 这张纸」这类多余空格。
    #
    # 这里的规则很简单：**全角标点与破折号的两侧一律不留空格**，
    # 其余位置的空格保留（中英文之间的空格如「使用 PyMuPDF 读写」是对的）。
    # 不用 look-behind —— 变长 look-behind 在 Python re 里不被支持。
    text = re.sub(r" +([，。、；：！？）】」』…—])", r"\1", text)
    text = re.sub(r"([，。、；：！？）】」』…（【「『—]) +", r"\1", text)
    # 「渲染成 位图」这类：两个汉字之间被换行插入的空格
    text = re.sub(r"([一-鿿]) +([一-鿿])", r"\1\2", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # 修掉重复的告警符号（<p class=warn> 里正文自己带了一个 ⚠）
    text = text.replace("⚠️ ⚠ ", "⚠️ ").replace("⚠️ ⚠", "⚠️")
    text = text.replace("⚠ ⚠️", "⚠️")
    return text.replace("\x00", "  \n").strip()


def _cell_lines(cell_html: str) -> list[str]:
    """把一个 <td> 的内容拆成若干行（单元格里可能有 <br>）。"""
    parts = re.split(r"<br\s*/?>", cell_html, flags=re.I)
    out = [_inline(p) for p in parts]
    return [x for x in out if x] or [""]


def _table_md(rows_raw: list[str]) -> list[str]:
    """把若干 ``<tr>`` 的原始 HTML 转成 Markdown 表格行。

    两个要点：

    * Markdown 表格的**行之间不能有空行**，否则解析器会认为表格结束、
      把后面的行当成普通段落。因此这里返回的行由调用方**紧凑拼接**。
    * 表格**必须有表头行**。原始 HTML 里未必有 ``<th>``，所以这里先看
      首行是否整行加粗（``<b>``）—— 是则当表头，否则**补一个空表头**，
      让数据行保持原样（补空表头比把第一行数据提成表头更忠实）。
    """
    parsed: list[list[str]] = []
    for row in rows_raw:
        cells_raw = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
        if not cells_raw:
            continue
        parsed.append([_inline(c) for c in cells_raw])
    if not parsed:
        return []

    ncol = max(len(r) for r in parsed)
    parsed = [r + [""] * (ncol - len(r)) for r in parsed]

    # 一律**补通用表头**，不猜哪一行是真正的表头。
    #
    # 理由：帮助页里的表格都是「标签 | 说明」式的两列，没有真正的表头行；
    # 若按「首行整行加粗」来猜，`识别原理` 那节（① ② ③ 三行都整行加粗）
    # 会把第一行数据误当成表头提走，反而破坏内容对应关系。
    #
    # Markdown 表格又必须有个表头行，所以补一个中性的「项目 / 说明」——
    # 比空表头更容易看出这是两列表格，且永远不会张冠李戴。
    if ncol == 2:
        header = ["项目", "说明"]
    else:
        header = [f"列{i + 1}" if i else "项目" for i in range(ncol)]
    body_rows = parsed

    def clean(cell: str) -> str:
        # 单元格里的换行在 md 表格中无法保留，用空格接起来；
        # 竖线要转义，否则会多切出一列
        return " ".join(cell.replace("|", "\\|").split())

    lines = [
        "| " + " | ".join(clean(c) for c in header) + " |",
        "|" + "---|" * ncol,
    ]
    lines += ["| " + " | ".join(clean(c) for c in row) + " |"
              for row in body_rows]
    return lines


def _convert_section(body: str) -> str:
    """把一节 HTML 转成 Markdown 片段。"""
    body = body.strip()
    out: list[str] = []

    # 逐个处理块级元素，保持文档顺序
    pattern = re.compile(
        r"<h2>(.*?)</h2>|<table[^>]*>(.*?)</table>|<ul>(.*?)</ul>"
        r"|<p\s+class=\"note\">(.*?)</p>|<p\s+class=\"warn\">(.*?)</p>"
        r"|<p>(.*?)</p>",
        re.I | re.S,
    )

    for m in pattern.finditer(body):
        h2, table, ul, note, warn, para = m.groups()

        if h2 is not None:
            out.append(f"### {_inline(h2)}")

        elif para is not None:
            text = _inline(para)
            if text:
                out.append(text)

        elif note is not None:
            text = _inline(note)
            lines = text.split("  \n")
            block = "\n".join(("> " + ln) if ln else ">" for ln in lines)
            out.append(block)

        elif warn is not None:
            text = _inline(warn)
            # 正文自己往往已经带了 ⚠，这里只在**没有**时才补一个，
            # 否则会出现「⚠️ ⚠ 单面模式下…」这种重复符号。
            if not text.lstrip().startswith(("⚠", "!")):
                text = "⚠️ " + text
            lines = text.split("  \n")
            block = "\n".join(("> " + ln) for ln in lines)
            out.append(block)

        elif ul is not None:
            items = re.findall(r"<li>(.*?)</li>", ul, re.I | re.S)
            lines = []
            for it in items:
                text = _inline(it).replace("  \n", " ")
                if text:
                    lines.append(f"- {text}")
            if lines:
                # 列表项之间用单换行（空行会让 Markdown 把每项拆成独立列表）
                out.append("\x01".join(lines))

        elif table is not None:
            rows = re.findall(r"<tr>(.*?)</tr>", table, re.I | re.S)
            lines = _table_md(rows)
            if lines:
                # 表格内部用单换行连接（空行会让 Markdown 认为表格已结束），
                # 用哨兵标记，最后统一把段落分隔与表格内换行区分开
                out.append("\x01".join(lines))

    text = "\n\n".join(out)
    # 表格行恢复成单换行
    return text.replace("\x01", "\n")


#: 目录里出现的标题（用于生成顶部目录）
def build_markdown() -> str:
    """生成完整 Markdown 文档。"""
    from ui.help_dialog import GUI_VERSION, SECTIONS

    parts: list[str] = [
        "# pdf_structure_splitter 使用说明",
        "",
        f"> 版本 {GUI_VERSION}　·　本文件由 `tests/help_to_md.py` 从程序内的"
        "帮助页面（F1 打开）**自动生成**，请勿手工编辑。",
        ">",
        "> 要改内容请改 `ui/help_dialog.py` 的 `SECTIONS`，然后重跑：",
        "> `python tests/help_to_md.py`",
        "",
        "---",
        "",
        "## 目录",
        "",
    ]
    for i, (title, _short, _html) in enumerate(SECTIONS, 1):
        anchor = re.sub(r"[^\w一-鿿]+", "-", title).strip("-").lower()
        parts.append(f"{i}. [{title}](#{i}-{anchor})")

    for i, (title, _short, body) in enumerate(SECTIONS, 1):
        parts += ["", "---", "", f"## {i}. {title}", ""]
        section = _convert_section(body)
        # 有些章节的标题与首个 h2 同名（如「输出文件」「常见问题」），
        # 会渲染出两个连着的同名标题。去掉重复的那个。
        first_h2 = re.search(r"^### (.+)$", section.split("\n\n")[0] or "")
        if first_h2 and first_h2.group(1).strip() == title:
            section = section.split("\n\n", 1)[1] if "\n\n" in section else ""
        parts.append(section)

    parts += [
        "",
        "---",
        "",
        "*本文件为自动生成的副本，与程序内帮助页面同源。*",
        "",
    ]
    # 折叠连续空行
    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default=str(_ROOT / "使用说明.md"))
    ap.add_argument("--check", action="store_true",
                    help="只校验现有文件是否与帮助页面一致（不写文件）")
    args = ap.parse_args()

    md = build_markdown()
    out = Path(args.output)

    if args.check:
        if not out.exists():
            print(f"✗ 文件不存在：{out}")
            return 1
        cur = out.read_text(encoding="utf-8")
        if cur == md:
            print(f"✓ 一致：{out}")
            return 0
        print(f"✗ 不一致：{out} 需要重新生成")
        return 1

    out.write_text(md, encoding="utf-8")
    n_lines = md.count("\n")
    print(f"已生成 {out}")
    print(f"  {len(md)} 字符 / {n_lines} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
