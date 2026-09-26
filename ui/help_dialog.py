"""帮助页面：软件用途、使用流程、操作速查、常见问题。

做成对话框而不是外链文档，好处是**离线可用**、与程序版本同步，
打包成 exe 后也能正常打开。

结构上用「左侧目录 + 右侧内容」的两栏布局，比一长条文本好读。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

#: 版本号
GUI_VERSION = "1.0.0"


# ----------------------------------------------------------------------
# 各章节内容（HTML 片段；Qt 的 QTextBrowser 支持有限子集）
# ----------------------------------------------------------------------

SECTIONS: list[tuple[str, str, str]] = [
    (
        "软件做什么",
        "用途",
        """
        <h2>这个软件解决什么问题</h2>
        <p>双面打印时，一张纸有正反两面。如果正面是黑白、反面是彩色，
        这张纸<b>只能整张送去彩色打印机</b> —— 否则同一张纸会被拆到两台打印机上，
        等于白印。</p>
        <p>本软件把 PDF 按<b>双面打印纸</b>为单位分成两个文件：</p>
        <table cellpadding="6">
          <tr><td><b>彩色件</b></td>
              <td>整张纸任意一面有彩色 → 整张归入彩色件</td></tr>
          <tr><td><b>黑白件</b></td>
              <td>两面都是黑白 → 整张归入黑白件</td></tr>
        </table>
        <p>这样彩色页送去彩色打印机、黑白页送去黑白打印机，
        <b>避免为了几页彩色而整本用彩色打印</b>。</p>
        <p class="note">输出 PDF <b>原样复制页面对象</b>，不重新渲染、不重新编码，
        因此保留原始分辨率、文字层、矢量内容与页面尺寸。</p>
        """,
    ),
    (
        "识别原理",
        "识别策略",
        """
        <h2>先看结构，再决定要不要渲染</h2>
        <p><b>识别策略：先读页面结构、只在必要时渲染</b> ——
        不是把每一页都整页渲染成位图再统计全页的彩色像素占比。</p>

        <table cellpadding="6">
          <tr><td><b>① 整页无图形</b></td>
              <td>纯文字页或空白页 → <b>直接判黑白</b>。
                  完全不渲染，所以很快。</td></tr>
          <tr><td><b>② 有矢量图形</b></td>
              <td>图表、曲线、色块的颜色在 PDF 里是
                  <b>显式声明</b>的，直接读出来判断，
                  <b>同样不需要渲染</b>。</td></tr>
          <tr><td><b>③ 有位图图片</b></td>
              <td>先<b>暂标为彩色</b>，再只渲染<b>图片所在的区域</b>
                  （而不是整页）统计色彩；<br>
                  若图片其实是灰度的 → <b>回落为黑白</b>。</td></tr>
        </table>

        <h2>这样做的两个好处</h2>
        <p><b>更快</b>：纯文字页不渲染，位图页只渲染图片区域。
        实测一份 132 页的论文中<b>只有 8 页需要渲染</b>，
        124 页仅靠结构就判定了，整份检测约 1.1 秒。</p>
        <p><b>更准</b>：色度统计的分母从"整页像素"变成"图形区域像素"，
        小面积彩色不再被大片留白稀释。整页渲染下一个 10 pt 的彩色图标
        只占万分之几、很容易被阈值滤掉；
        按图形区域统计，同一张图占比是 <b>2% 量级</b>，不会再漏。</p>
        <p class="note">「彩色矢量」判据还更可靠：一条 0.5 pt 的彩色细线
        在低 dpi 下只渲染出几个半透明像素、很容易漏检，
        但它的<b>声明颜色</b>在 PDF 里是确定无疑的。
        实测有两页的彩色正是靠矢量颜色才判出来的
        （一页是红色虚线曲线图，一页是彩色插图）。</p>

        <h2>为什么「检测 DPI」不是越高越好</h2>
        <p>默认 75 是一个平衡点：位图的色度统计需要足够的像素样本，
        DPI 过低时小位图区域内的样本太少，容易漏掉真实的小面积彩色。</p>
        <p>但把它调高<b>会把位图内部的压缩伪影暴露出来</b>：扫描件与 JPEG
        位图常带有几度的色偏（色度 16–20，紧贴阈值 15），DPI 越高这些像素
        越不会被平均掉。实测一张纯黑白阶梯图（其内嵌位图有轻微 JPEG 色偏）：</p>
        <table cellpadding="6">
          <tr><td>75 dpi（默认）</td><td>彩色像素 0（被平均掉）→ 判<b>灰度</b> ✅</td></tr>
          <tr><td>100 dpi</td><td>彩色像素 195（0.0018）→ 判<b>彩色</b> ❌ 误判</td></tr>
          <tr><td>150 dpi</td><td>彩色像素 944（0.0038）→ 判<b>彩色</b> ❌ 误判更明显</td></tr>
        </table>
        <p class="warn">⚠ 遇到这类误判（扫描件/位图被错判成彩色），
        <b>应该调高「色度阈值」</b>（从 15 提到 20–25）而不是调低 DPI ——
        那些伪影的色度大多在 16–20，而真实彩色内容通常远高于此。
        调高阈值能一次解决整份文档里的同类伪影。</p>

        <h2>色带颜色说明</h2>
        <p>每张卡片底部的小色带表示<b>该页自身</b>的检测结果与依据：</p>
        <table cellpadding="6">
          <tr><td>最浅灰</td><td><b>纯文字</b>：整页只有文字，未渲染直接判黑白</td></tr>
          <tr><td>浅灰</td><td><b>黑白矢量</b>：有表格线 / 线条图，
              但矢量颜色已从 PDF 读出且为灰</td></tr>
          <tr><td>灰</td><td><b>位图灰度</b>：渲染图片区域后确认无彩色，回落为黑白</td></tr>
          <tr><td><b>深红</b></td><td><b>彩色矢量</b>（PDF 声明颜色，结论最确定）</td></tr>
          <tr><td><b>橙</b></td><td><b>位图彩色</b>：位图区域检出彩色像素</td></tr>
        </table>
        <p class="note">深红与橙的区分对复核很有用：矢量彩色是确定结论，
        位图彩色是像素统计的结果、可能受阈值影响。扫一眼色带就知道
        哪些页值得重点看一眼。</p>
        """,
    ),
    (
        "使用流程",
        "步骤",
        """
        <h2>四步完成</h2>
        <p><b>1. 打开 PDF</b><br>
        点工具栏「打开 PDF」或按 <code>Ctrl+O</code>。
        程序在后台逐页检测，完成后缩略图分左右两区显示。</p>
        <p><b>2. 核对分区</b><br>
        左边是<b>黑白区</b>（灰色竖条），右边是<b>彩色区</b>（红色竖条）。
        每张卡片代表<b>一张纸</b>，显示如 <code>P25-26</code>，
        卡片底部的小色条表示每一面自身是否彩色 ——
        一眼就能看出「这张纸为什么被判成彩色」。</p>
        <p><b>3. 人工复核</b><br>
        分错了就把卡片搬到另一侧：
        直接<b>拖动</b>，或选中后按 <code>←</code> / <code>→</code>。
        按住 <code>Ctrl</code> 点击可多选，一次搬移多张。</p>
        <p><b>4. 导出或打印</b><br>
        点「导出」（<code>Ctrl+E</code>）得到两个 PDF
        （需要逐页报告就先勾上「输出CSV报告」）；
        或点「打印」（<code>Ctrl+P</code>）分别送到两台打印机。</p>
        <p class="note">检测参数（DPI / 色度阈值 / 彩色占比）改完后，
        需要点「<b>重新检测</b>」才会生效 —— 滑块本身不会自动重跑检测，
        因为一份上百页的 PDF 检测要好几秒。</p>
        """,
    ),
    (
        "操作速查",
        "快捷键",
        """
        <h2>键盘与鼠标</h2>
        <table cellpadding="6">
          <tr><td><code>Ctrl+O</code></td><td>打开 PDF</td></tr>
          <tr><td><code>Ctrl+E</code></td><td>导出</td></tr>
          <tr><td><code>Ctrl+P</code></td><td>打印</td></tr>
          <tr><td><code>←</code> / <code>→</code></td>
              <td>把选中的纸搬到黑白区 / 彩色区</td></tr>
          <tr><td><code>Ctrl+A</code></td><td>全选当前区的纸</td></tr>
          <tr><td><code>Delete</code></td>
              <td>把选中的纸复位到检测的原始结果</td></tr>
          <tr><td><code>空格</code> / 双击</td><td>打开预览大图</td></tr>
          <tr><td><code>Ctrl+滚轮</code></td>
              <td>缩放缩略图（60%–300%）</td></tr>
          <tr><td><code>Ctrl+点击</code></td>
              <td>多选卡片（可跨多张纸）</td></tr>
          <tr><td>滚轮</td><td>逐行滚动</td></tr>
          <tr><td>右键卡片</td>
              <td>拆分为单页 / 合并回一张纸 / 预览</td></tr>
        </table>
        """,
    ),
    (
        "检测模式",
        "双面/单面",
        """
        <h2>严格双面模式（默认）</h2>
        <p>拖动的单位是<b>一张纸</b>，同一张纸的两页<b>永远一起走</b>。
        这是由数据结构保证的，不可能出现「P31 搬到彩色区、P32 留在黑白区」
        这种把一张纸拆到两台打印机的情况。</p>

        <h2>单面模式</h2>
        <p>每一页<b>独立判定</b>彩色/黑白，不再按纸张配对。切换后：</p>
        <ul>
          <li>卡片立刻从 <code>P25-26</code> 变成两张独立的 <code>P25</code>、<code>P26</code>
              （无需重新检测，因为每页的检测值早就有了）</li>
          <li>同一张纸的两页可以分属彩色件与黑白件</li>
          <li>不再有「因配对被强制升级为彩色」的页，彩色页数会变少</li>
        </ul>
        <p class="note">适合<b>只打单面</b>、或希望<b>彩色页尽量少印</b>的场景。
        例如一张纸正面有彩色图、反面是纯文字：双面模式下两面都得用彩色纸，
        单面模式下反面就能走黑白打印机。</p>
        <p class="warn">⚠ 单面模式下同一张纸的两页可能被送到两台不同的打印机。
        如果你要的是双面打印，请用严格双面模式。</p>
        """,
    ),
    (
        "参数怎么调",
        "参数",
        """
        <h2>三个参数</h2>
        <p class="note">注意：三个参数的作用范围都<b>只限于需要渲染的部分</b> ——
        因为大部分页面已经不需要渲染了。</p>
        <table cellpadding="6">
          <tr><td><b>检测 DPI</b></td><td>默认 75。
              只用于<b>位图与渐变区域</b>的渲染，<b>不影响输出质量</b>。
              纯文字页与已有彩色矢量的页完全不受它影响。<br>
              调低更快，但小位图区域内可用于统计的像素太少，
              可能漏掉真实的小面积彩色。<br>
              <b>注意：并非越高越好</b> —— 高 DPI 会把位图内部的压缩伪影
              暴露出来（详见下方「识别原理」一节）。</td></tr>
          <tr><td><b>色度阈值</b></td><td>默认 15。<code>max(R,G,B) - min(R,G,B)</code>
              超过该值即算彩色。<b>矢量颜色与像素色度共用这一个阈值</b>
              （两者都已折算到 0–255 量纲）。越大越宽松，
              容易把彩色判成黑白。</td></tr>
          <tr><td><b>彩色占比</b></td><td>默认 0.0010。
              <b>只对渲染出来的图形区域生效</b> —— 分母是图形区域的像素数，
              不是整页像素数。纯文字页与彩色矢量页不受它影响。
              只需抑制零星彩色噪点时可调大（如 0.0030）。</td></tr>
        </table>

        <h2>典型调整</h2>
        <ul>
          <li><b>彩色页被判成黑白</b> → 调低「色度阈值」；
              若那页只有位图，也可调低「彩色占比」</li>
          <li><b>黑白页被判成彩色</b>（零星彩点）→ 调高「色度阈值」
              或调高「彩色占比」</li>
          <li><b>检测太慢</b> → 调低「检测 DPI」</li>
        </ul>
        <p class="note">改完参数要点「重新检测」。滑块右边会显示当前值，双击滑块可复位到默认值。</p>
        <p class="note">「彩色占比」对彩色矢量页无效：矢量颜色在 PDF 里是
        显式声明的，只要色度超过阈值就判彩色，不存在"占比太小"的情况。
        这类页要放宽只能调「色度阈值」。</p>
        """,
    ),
    (
        "打印说明",
        "打印",
        """
        <h2>两台打印机同时工作</h2>
        <p>点「打印」后会弹对话框，为<b>彩色件</b>和<b>黑白件</b>各选一台打印机。
        两个任务会<b>同时</b>提交给各自的打印机，而不是「先打完彩色再打黑白」。</p>

        <h2>彩色 / 灰度是显式设置的</h2>
        <p><code>QPrinter</code> 默认会把 RGB 数据原样交给驱动、由驱动决定怎么输出。
        这有实际风险：某些驱动会把「看起来是彩色任务」的内容按彩色渲染，
        <b>白白消耗彩色碳粉</b>。</p>
        <p>所以程序里两个任务的输出模式是显式声明的：</p>
        <ul>
          <li><b>彩色件</b> → 彩色（可手动改灰度）</li>
          <li><b>黑白件</b> → <b>灰度</b>（默认勾选，强制灰度输出）</li>
        </ul>

        <h2>虚拟打印机要先选保存位置</h2>
        <p>像 Microsoft Print to PDF、XPS Document Writer、Adobe PDF、OneNote
        这类打印机<b>不出纸，而是产出文件</b>。选中它们后，程序会先弹「保存输出文件」
        对话框让你指定位置，然后才开始打印。</p>
        <p class="note">这一步不是多余的：不预先指定路径的话，Windows 会在打印开始时
        才弹「保存为」对话框，而那一刻程序正忙于提交任务，
        <b>界面会完全无响应数秒</b>（实测 4.9~6.5 秒 × 两个任务）。</p>

        <h2>打印画质</h2>
        <table cellpadding="6">
          <tr><td><b>草稿 150</b></td><td>排版校对，小字略软</td></tr>
          <tr><td><b>标准 300</b></td><td>默认。办公文档标准档</td></tr>
          <tr><td><b>清晰 450</b></td><td>小字或表格较多时更稳</td></tr>
          <tr><td><b>高 600</b></td><td>细线条图更锐利</td></tr>
          <tr><td><b>极高 900 / 最高 1200</b></td><td>极精细小幅面输出</td></tr>
        </table>
        <p class="note">页数多时建议不超过 600 dpi —— 1200 dpi 下 132 页的光栅数据约 50 GB。</p>
        """,
    ),
    (
        "输出文件",
        "输出",
        """
        <h2>输出文件</h2>
        <p>以输入 <code>test.pdf</code> 为例：</p>
        <table cellpadding="6">
          <tr><td><code>test_color.pdf</code></td>
              <td>归属彩色的整张纸（每张纸两面都在，保持原始顺序）</td></tr>
          <tr><td><code>test_bw.pdf</code></td><td>归属黑白的整张纸</td></tr>
          <tr><td><code>test_report.csv</code></td>
              <td>逐页检测报告 —— <b>默认不输出</b></td></tr>
        </table>

        <h2>CSV 报告是可选的</h2>
        <p>工具栏「导出」按钮左侧有 <b>☐ 输出CSV报告</b> 勾选框：</p>
        <ul>
          <li><b>默认不勾选</b>：只输出两个 PDF —— 多数场景拿这两个文件去打印就够了</li>
          <li><b>勾选后</b>：导出时额外生成 <code>&lt;文件名&gt;_report.csv</code></li>
        </ul>
        <p class="note">不勾选时，覆盖确认也只检查那两个 PDF ——
        不会因为存在一个旧的 CSV 而弹出多余的确认框。</p>

        <h2>CSV 报告列（勾选后才生成）</h2>
        <table cellpadding="6">
          <tr><td><code>page</code></td><td>页码（1 起）</td></tr>
          <tr><td><code>sheet</code></td><td>第几张纸（1 起）</td></tr>
          <tr><td><code>page_color</code></td>
              <td><b>该页自身</b>的检测结果（BW / COLOR）</td></tr>
          <tr><td><code>output</code></td>
              <td><b>整张纸</b>的最终归属（人工复核后的结果）</td></tr>
          <tr><td><code>reason</code></td>
              <td><b>判定依据</b>：纯文字 / 黑白矢量 /
                  彩色矢量 / 位图彩色 / 位图灰度 / 空白页 / 渲染失败。<br>
                  看到一页被判彩色时，靠它分辨是"有彩色矢量"
                  还是"位图区域检出彩色像素"。</td></tr>
          <tr><td><code>structure</code></td>
              <td><b>页面元素构成</b>：
                  如 <code>文字 + 1 张位图 + 3 条矢量（2 彩色）</code></td></tr>
          <tr><td><code>color_ratio</code></td>
              <td>彩色像素占比。<b>分母是图形区域的像素数</b>（不是整页）</td></tr>
          <tr><td><code>color_pixels</code></td><td>彩色像素数</td></tr>
          <tr><td><code>total_pixels</code></td>
              <td>参与统计的像素数。纯文字页为 <code>0</code>（没有渲染）</td></tr>
          <tr><td><code>rendered</code></td>
              <td><b>该页是否做过像素渲染</b>：是 / 否。
                  "否"表示仅靠页面结构就判定了</td></tr>
          <tr><td><code>sheet_output</code></td><td>同 output，纸张维度</td></tr>
        </table>
        <p class="note"><code>page_color=BW</code> 而 <code>output=COLOR</code> 表示：
        这页自身是黑白，但与它同张纸的另一面是彩色，所以整张纸按彩色打印。</p>
        <p class="note">报告头部的注释里还有「渲染页」与各「判定依据」的页数统计，
        可以直观看到这次检测省下了多少渲染。</p>
        """,
    ),
    (
        "常见问题",
        "FAQ",
        """
        <h2>常见问题</h2>
        <table cellpadding="6">
          <tr><td>为什么有些页没有渲染</td>
              <td>正常：纯文字页直接判黑白，彩色矢量页直接读声明颜色，
                  两者都不需要渲染。卡片的悬停提示与 CSV 报告的
                  <code>rendered</code> 列会标明。</td></tr>
          <tr><td>调了「彩色占比」但某页判定没变</td>
              <td>若那页是<b>彩色矢量页</b>或<b>纯文字页</b>，该参数对它无效 ——
                  它只作用于渲染出来的图形区域。彩色矢量页要放宽只能调
                  「色度阈值」。</td></tr>
          <tr><td>纯文字页上的红色标题被判成黑白</td>
              <td><b>有意如此</b>：先看结构，整页无图形即判黑白，
                  不看文字颜色。红色标题走黑白打印机是可接受的代价，
                  而为它把大量纯文字页渲染一遍并不划算。</td></tr>
          <tr><td>灰度照片页显示什么依据</td>
              <td>「位图灰度」：先暂标彩色，渲染图片区域后发现几乎无彩色像素，
                  于是回落为黑白。</td></tr>
          <tr><td>拖了滑块但结果没变</td>
              <td>设计如此：改完参数要点「<b>重新检测</b>」。</td></tr>
          <tr><td>导出后没有 CSV 文件</td>
              <td><b>默认不输出</b>。需要就在导出前勾上工具栏的
                  「输出CSV报告」。</td></tr>
          <tr><td>拖不动卡片</td>
              <td>按住后需移动约 8px 才进入拖拽；也可用 <code>←</code> <code>→</code>
                  或右键菜单。</td></tr>
          <tr><td>黑白件打印出来是彩色</td>
              <td>打印对话框里勾上「灰度输出」（默认已勾选）。</td></tr>
          <tr><td>彩色件打印出来是灰的</td>
              <td>检查「灰度输出」是否被误勾。</td></tr>
          <tr><td>打印时弹出「保存输出文件」</td>
              <td>正常：虚拟打印机不出纸而是产出文件。</td></tr>
          <tr><td>打印取消不了 / 想中断</td>
              <td>打印在主线程分帧执行，取消需关闭窗口或等当前任务结束。</td></tr>
          <tr><td>某些页显示灰色虚线故障卡</td>
              <td>该页渲染失败，状态栏与报告中会标注，不会被静默忽略。</td></tr>
          <tr><td>提示「PDF 已加密」</td>
              <td>加密 PDF 不支持，请先解密。</td></tr>
          <tr><td>检测太慢</td>
              <td>调低「检测 DPI」（默认 75）。多数文档里占大头的纯文字页与
                  矢量页本来就不渲染，所以调它的收益有限。</td></tr>
          <tr><td>exe 双击没反应</td>
              <td>首次启动需解压 1–3 秒；仍无反应请见 README 的排查章节。</td></tr>
        </table>
        """,
    ),
    (
        "关于",
        "关于",
        f"""
        <h2>pdf_structure_splitter 图形界面版</h2>
        <p>把 PDF 按双面打印纸为单位拆成彩色件与黑白件，
        供彩色 / 黑白打印机分别打印。</p>

        <h2>项目主页</h2>
        <p>源码、问题反馈与更新都在 GitHub：</p>
        <p><a href="https://github.com/zxzhang0507/pdf_structure_splitter">
        https://github.com/zxzhang0507/pdf_structure_splitter</a></p>
        <p class="note">本项目采用 Apache License 2.0 许可。</p>

        <h2>技术信息</h2>
        <table cellpadding="6">
          <tr><td>版本</td><td>{GUI_VERSION}</td></tr>
          <tr><td>界面框架</td><td>PySide6（Qt 6）</td></tr>
          <tr><td>PDF 引擎</td><td>PyMuPDF</td></tr>
          <tr><td>颜色统计</td><td>NumPy</td></tr>
          <tr><td>运行环境</td><td>Python {sys.version.split()[0]} · Windows x64</td></tr>
        </table>
        <p class="note">核心检测算法与命令行版
        <code>pdf_structure_splitter</code> 共用同一份代码（<code>core/split_pdf.py</code>），
        保证两者结果一致。</p>

        <h2>不做什么</h2>
        <ul>
          <li>不做 PDF 内容编辑（增删页、旋转、批注）</li>
          <li>不做多文件批量处理</li>
          <li>不做云同步</li>
          <li>仅中文界面</li>
        </ul>
        """,
    ),
]


class HelpDialog(QDialog):
    """帮助页面：左侧目录 + 右侧内容。"""

    def __init__(self, parent: QWidget | None = None,
                 initial_section: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用帮助 · pdf_structure_splitter")
        self.resize(880, 640)
        self.setMinimumSize(640, 480)

        # ---- 左侧目录 ----
        self.toc = QListWidget()
        self.toc.setObjectName("HelpToc")
        self.toc.setFixedWidth(160)
        for title, _short, _html in SECTIONS:
            self.toc.addItem(title)
        self.toc.currentRowChanged.connect(self._on_section_changed)

        # ---- 右侧内容 ----
        self.stack = QStackedWidget()
        for _title, _short, html in SECTIONS:
            self.stack.addWidget(self._make_page(html))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.toc)
        body.addWidget(self.stack, 1)

        # ---- 底部按钮 ----
        buttons = QDialogButtonBox()
        close_btn = buttons.addButton("关闭", QDialogButtonBox.AcceptRole)
        close_btn.setObjectName("PrimaryButton")
        buttons.accepted.connect(self.accept)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addLayout(body, 1)
        root.addWidget(buttons)

        self.toc.setCurrentRow(max(0, min(initial_section, len(SECTIONS) - 1)))
        self._nav_buttons = buttons

    @staticmethod
    def _make_page(html: str) -> QWidget:
        """把一段 HTML 包成可滚动的页面。"""
        browser = QTextBrowser()
        browser.setObjectName("HelpBody")
        # 允许点击链接用系统默认浏览器打开（「关于」里有项目主页）。
        # 帮助页正文里除了项目地址没有别的链接，因此不需要额外过滤域名。
        browser.setOpenExternalLinks(True)
        browser.setFrameShape(QTextBrowser.NoFrame)
        browser.setHtml(_wrap_html(html))
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(browser)
        return page

    def _on_section_changed(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)


def _wrap_html(body: str) -> str:
    """套上统一的基础样式（QTextBrowser 支持有限的 CSS 子集）。"""
    return f"""
    <html><head><style>
      body {{ color: #111827; font-size: 13px; line-height: 150%; }}
      h2 {{ color: #111827; font-size: 15px; margin-top: 14px; margin-bottom: 6px; }}
      p  {{ margin: 6px 0; }}
      ul {{ margin: 6px 0 6px 18px; }}
      li {{ margin: 3px 0; }}
      code {{ background: #F3F4F6; color: #1D4ED8; padding: 1px 4px; }}
      a {{ color: #2563EB; text-decoration: underline; }}
      table {{ border-collapse: collapse; margin: 6px 0; }}
      td {{ border-bottom: 1px solid #E5E7EB; vertical-align: top; }}
      .note {{ color: #6B7280; background: #F9FAFB;
               border-left: 3px solid #9CA3AF; padding: 6px 10px; }}
      .warn {{ color: #92400E; background: #FFFBEB;
               border-left: 3px solid #D97706; padding: 6px 10px; }}
    </style></head><body>{body}</body></html>
    """
