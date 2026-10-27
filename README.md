# Lacewing Digital Library MCP

A research-oriented MCP server + Codex skill for the **Lacewing Digital Library (LDL)** and **Neuropterida Species of the World (NSW)**.

**[中文使用教程](#中文使用教程)**：从登录、按属下载文献、本地全文检索与 OCR，到证据表、分布图和导出研究包的完整流程。

It turns LDL's publicly reachable JSON endpoints into tools that match real taxonomic workflows: accepted-name resolution, historical combinations, nomenclature, type specimens and type-locality coordinates, distribution citations, bibliography, figures, phylogeny, identification keys, and batch reconciliation.

> This project is an independent research client. LDL's `/api/...` endpoints are used by the public web frontend and are not documented as a third-party public API. The server uses conservative request rates and does not bypass contributor-only access controls.

## 中文使用教程

这一节按实际科研顺序介绍：安装 → 登录 LDL → 按属下载文献 → 检查和检索本地文献 → 记录证据 → 生成分布数据和地图 → 导出研究包。每一步都给出可以直接对 Codex（或其他 MCP 客户端）说的话，以及背后调用的工具，方便你核对结果。

### 0. 安装

**macOS / Linux，Codex：**

```bash
# 新功能目前在 feat/mcp-downloader 分支：
curl -fsSL https://raw.githubusercontent.com/Gyoungwe/lacewing-digital-library-mcp/feat/mcp-downloader/install.sh | LDL_MCP_BRANCH=feat/mcp-downloader bash
# 合并到 main 之后改用：
# curl -fsSL https://raw.githubusercontent.com/Gyoungwe/lacewing-digital-library-mcp/main/install.sh | bash
codex mcp get lacewing-digital-library   # 确认已注册
```

安装脚本会建立独立的 Python 环境、注册 MCP、复制 Codex skill。登录窗口优先使用你电脑上已有的 Chrome 或 Edge；两者都没有时，才会自动下载 Playwright 自带的 Chromium。

**可选组件：**

```bash
INSTALL=~/.local/share/lacewing-digital-library-mcp
# 扫描版老文献 OCR：需要 tesseract 程序和语言包
brew install tesseract tesseract-lang
$INSTALL/.venv/bin/pip install -e "$INSTALL[ocr]"
# Robinson / Mercator 投影或 cartopy 底图（默认底图不需要它）
$INSTALL/.venv/bin/pip install -e "$INSTALL[maps]"
```

**其他 MCP 客户端（Claude Desktop、Cursor 等）**：把 stdio 服务器命令设为 `~/.local/share/lacewing-digital-library-mcp/.venv/bin/python`，参数设为 `-m ldl_mcp`。

**数据放在哪里**：MCP 产生的所有文件都在 `~/.local/share/ldl-mcp/` 下，包括登录 Cookie、下载任务、文献库、文本缓存、证据、数据集、图件和导出包。设置环境变量 `LDL_MCP_HOME` 可以改到别处，例如外接硬盘。

```text
~/.local/share/ldl-mcp/
├─ auth/          登录 Cookie（权限 600）和独立浏览器配置；不要分享
├─ jobs/          每个下载任务的状态、控制文件和日志
├─ libraries/     默认下载位置：LDL_<属名>/
├─ text_cache/    PDF 文本和 OCR 缓存（按 SHA-256）
├─ scopes/ plans/ 研究范围快照、文献计划
├─ evidence/      证据表（每个数据集一个 .jsonl）
├─ datasets/      分布数据集与 QC
├─ figures/       地图、manifest、图注
└─ exports/       研究包 zip
```

### 1. 登录 LDL（一次即可，过期后再登）

> 对 Codex 说：**“打开 LDL 登录窗口。”**（调用 `auth_start`）

1. 电脑上会弹出一个**单独的** Chrome 窗口。它使用独立的配置目录，读不到你日常浏览器的书签、历史记录或已保存的密码。
2. 在这个窗口里正常登录 LDL。
3. 点击右下角紫色按钮 **“我已登录，保存会话 / Save LDL session”**。窗口会自动关闭。
4. 对 Codex 说：**“检查 LDL 登录状态。”**（`auth_status`）。看到 `status: saved` 和 cookie 数量即表示成功。

说明：

- MCP 从不接收、也不保存密码。只有 `lacewing.tamu.edu` 的 Cookie 会保存到本机，`auth_status` 只显示 Cookie 名称和过期时间，不显示值。
- 想在保存前确认账号确实能打开全文，可以说“打开登录窗口，并用 EDoc 9603 验证”（`auth_start(verify_edoc_id=9603)`）。没登录成功时按钮会提示，不会保存。
- 退出登录：“删除 LDL 登录 Cookie。”（`auth_logout`）

### 2. 按属下载全部文献

> 对 Codex 说：**“下载 Micromus 属全部物种的 LDL 文献，用快速模式。”**（`download_start(genus="Micromus")`）

工具会立即返回一个 `job_id`。下载在**后台独立进程**里进行：关掉 Codex，甚至重启 MCP，都不会中断下载。下载范围、去重和结果目录与原来的“LDL 属文献下载器”完全一致：

- 读取每个物种的 Taxon、Distribution 和 Image 三类 Citations，只下载整篇 EDoc（Open file (work)），不下载 Image Gallery。
- 去重顺序：物种内断点 → LDL `r#` → EDoc 编号 → 文件 SHA-256。同一篇文献无论出现在多少个物种、多少个类别中，都只下载一次。
- 结果默认在 `~/.local/share/ldl-mcp/libraries/LDL_Micromus/`。也可以指定位置：“下载到 ~/Research/LDL”（`output_parent="~/Research/LDL"`）。

**三种下载模式**

| 模式 | 适用情况 | 设置 |
|---|---|---|
| `fast` 快速首轮 | 先把整个属快速扫一遍 | 每个约 1 MiB 的文件块最长等 90 秒，只试 1 次，失败就记录并继续 |
| `stable` 稳定下载 | 网络慢时的首次下载 | 每块最长 5 分钟，最多试 3 次 |
| `retry-failed` 仅补失败 | 首轮结束后补漏 | 只处理 `下载失败待补.csv` 里的项目，不重新遍历物种 |

**查看和控制任务**

- “Micromus 下载到哪了？”→ `download_status(job_id)`：返回状态、已完成物种数、失败数和最近的日志。不给 `job_id` 时列出最近的所有任务。
- “先暂停这个下载。”→ `download_control(job_id, "pause")`：在下一个请求之间安全停下，保留断点。
- “继续下载。”→ `download_control(job_id, "resume")`：已完成的物种会被跳过。
- “把失败的补一下。”→ `download_control(job_id, "retry_failed")`
- “取消任务。”→ `download_control(job_id, "cancel")`：已经核验过的文献不会被删除。

**任务状态的含义**

| 状态 | 含义 | 你需要做什么 |
|---|---|---|
| `running` | 正在下载 | 等待 |
| `completed` | 全部完成，没有失败项 | 无 |
| `attention_required` | 跑完了，但仍有失败项 | 执行 `retry_failed`，或查看 `下载失败待补.csv` |
| `auth_required` | 登录过期，或还没登录 | 重新执行第 1 步，然后 `resume` |
| `paused` / `cancelled` | 你主动停止的 | 需要时 `resume` |
| `interrupted` | 电脑休眠或重启导致进程消失 | `resume`，从断点继续 |
| `failed` | 意外错误 | 用 `download_status` 查看日志 |

**已有的旧下载结果**：用原来的 Windows 下载器下好的 `LDL_<属名>` 文件夹，可以直接接着用。下载时把 `output_parent` 指向它的**上一级**文件夹，就会按原来的 `progress.json` 和 `library_index.json` 续传，不会重复下载。

**按目、科一次下载（v0.4）**：先冻结研究范围，再用 `scope_id` 下载。整个范围共用一个文献库，跨属引用的文献只下载一次：

> “冻结脉翅目全部现生有效种，然后先只抓元数据，不下载 PDF。”
> → `freeze_taxon_scope(order="Neuroptera", extant_only=True)` → `download_start(scope_id=..., metadata_only=True)`

`metadata_only=True` 不需要登录，只读取每个物种的记录和三类 Citations，生成 `species_catalog.csv`（学名、作者、状态、模式、LDL 分布摘要）、`citations.csv`（物种 × 引用）和按覆盖物种数排序的 `literature_plan.json`，保存在 `~/.local/share/ldl-mcp/metadata/<scope_id>/`。可以断点续跑，返回值里有请求量和耗时估算。确认计划后，再用 `download_start(scope_id=...)` 下载全文。

**联网核验是否下全**：“核验 Micromus 文献是否全部下载。”（`download_verify`）。它会重新读取 LDL 当前的引用数据，与本地文件逐项比对，生成 `完整性检查报告.txt` 和 `完整性检查明细.csv`，不会下载任何文件。

### 3. 检查、合并和检索本地文献库

- “列出我的文献库。”→ `library_status()`；“Micromus 文献库现在什么情况？”→ `library_status(root)`
- “离线检查文件有没有损坏。”→ `library_verify(root, full_hash=True)`：逐个重新计算 SHA-256。
- “列出 *Micromus angulatus* 关联的文献。”→ `library_documents(root, species="Micromus angulatus")`

**全文检索**

> “在 Micromus 文献库里检索 ‘material examined’ 和 ‘Yunnan’，要求同时出现。”
> → `library_search(root, ["material examined", "Yunnan"], match="all")`

每条命中会给出 PDF 页码、上下文片段、文件 SHA-256、BibObjID 和 EDocID。检索不区分大小写，也忽略重音符号（如 é、ü）。需要读整页时说“读这篇文献第 12 页”（`library_page_text(root, sha256, 12)`）。

**扫描版老文献（OCR）**

没有文本层的 PDF 会列在检索结果的 `documents_without_text_layer` 里，而**不会**被当作“没有提到”。要检索它们，先做 OCR：

> “对 Micromus 文献库里的扫描版文献做 OCR，语言用英文加德文。”
> → `library_ocr(root, lang="eng+deu", max_documents=10)`

- OCR 只填补没有文本层的页面，不会覆盖原有文本。
- 检索命中和证据核验都会标注 `text_source: "ocr"`，提醒你对照原图复核。
- 常用语言代码：`eng`（英）、`deu`（德）、`fra`（法）、`chi_sim`（简体中文）、`jpn`（日）。不带参数调用 `library_ocr()` 可以查看本机已安装的语言。
- 一次处理的篇数有上限（`max_documents`），大批量时分几次调用。

**跨属合并**

> “把 ~/Research/LDL 下所有属的文献库合并成一个统一库，先给我看计划。”
> → `library_merge("~/Research/LDL")`（默认 `dry_run=True`，只报告不动文件）

确认后说“执行合并，用硬链接”（`dry_run=False, mode="hardlink"`）。会生成 `LDL_统一文献库/`，其中有 `00_唯一文献/`、`global_library_index.json` 和 `02_报告/`。与原 Windows 工具不同，**这里永远不会删除或修改各属文件夹里的源文件**。硬链接在同一个磁盘上不额外占空间；跨盘时会自动改为复制。统一库也可以直接用于 `library_search` 等工具。

### 4. 从文献到证据表

推荐的分工是：**Codex 读文献、提出结构化记录；MCP 负责核验，并保存证据链。**

1. **冻结研究范围**：“把 Micromus 属所有现生有效种冻结成研究范围。”→ `freeze_taxon_scope(genus="Micromus", extant_only=True)`。返回一个 `scope_id`，记录了物种、CombObjID/TaxObjID、状态、LDL 数据版本和时间。同样的输入总是得到同一个 `scope_id`。
2. **文献计划**（可选，只读不下载）：“列出这个范围需要的文献，按覆盖物种数排序。”→ `literature_plan(scope_id)`
3. **抽取并记录证据**：

   > “检索 Micromus 文献库中的 ‘Holotype’ 和 ‘Material examined’，逐条阅读相关页面，把每个标本地点按证据表格式记录到数据集 micromus。没有原文坐标的，不要自己补坐标。”

   Codex 会调用 `library_search` 和 `library_page_text`，再用 `record_evidence(dataset="micromus", rows=[...])` 写入证据。每一行都要带 `sha256`、`pdf_page` 和 `evidence_quote`（原文摘录）。MCP 会到该页原文里查找这段摘录：
   - 找到 → `quote_verified`
   - 找不到 → `unverified`。这一行会保留，但默认**不上地图**。
   - `accepted` / `rejected` 只能由人通过 `review_evidence` 设置，Codex 无法自行把证据标为“已接受”。

4. **人工复核**：“把这三条证据标为 accepted。”→ `review_evidence("micromus", [ids], "accepted")`；查看汇总：`evidence_table("micromus")`

**坐标基础（`coordinate_basis`）的规则**

| 值 | 含义 | 是否上地图 |
|---|---|---|
| `type_specimen` | LDL 提供的模式标本地点坐标 | 是 |
| `literature_locality` | 文献原文印出的坐标 | 是 |
| `georeferenced` | 原文没有坐标，由地名解析得到 | 是，但**必须**填写 `georeference_source`（地名库或工具、版本或网址）和 `coordinate_uncertainty_m`，否则会被标记并排除 |
| `occurrence` | 其他出现记录 | 是 |
| `country_summary` | 国家或地区级别的分布摘要 | 否，只进表格 |
| `inferred` | 推断的坐标 | 否 |

### 4b. 从文献自动抽取分布（v0.4）

手动记录证据适合少量物种。物种多时，可以让 MCP 先用规则批量抽取，人只复核被标记的行：

> “从 Micromus 文献库里自动抽取每个种的标本地点，写进证据表 micromus。”
> → `literature_distribution(root=".../LDL_Micromus", dataset="micromus", scope_id=...)`

每个物种依次做这些事：

1. **名称变体**：现行学名、原始组合、同一 TaxObjID 的所有历史组合，以及 LDL 在每条分布引用里记录的“引用时所用名称”。老文献多用旧名。
2. **定位页面**：LDL 引用的页码会通过页眉换算成 PDF 页码，再加上文中出现任一名称变体的页面。每篇最多 `max_pages_per_document` 页。
3. **规则抽取**：识别 Material examined、Type material、Holotype、Distribution 等段落（含德、法、西和中文标题），拆成单条记录，解析国家、地点、坐标（度分秒和十进制）、海拔、日期、采集人和模式类型。`evidence_quote` 一定是原文里的原样片段。
4. **地名解析**（`georeference=True`）：没有印出坐标的记录，用 GeoNames 解析，范围限定在原文所写的国家内，能确定省份时也限定省份。每个国家的 GeoNames 数据第一次使用时下载一次，之后离线使用（CC BY 4.0）。结果分为 `resolved`、`probable`、`coarse`、`ambiguous`、`not_found` 五类。只有前两类会写入坐标，并自动带上 `georeference_source`（GeoNames ID 和匹配到的地名）和 `coordinate_uncertainty_m`。`ambiguous` 只列出候选地点，**不会替你选一个**。
5. **交叉核对**：坐标是否落在原文所写的国家内（使用内置的 Natural Earth 国界）；该国是否在 LDL 接受的分布国家里。不符合的行标为 `needs_review`。
6. **入库**：通过 `record_evidence` 写入，摘录会再次在原页面上核验。

返回值统计抽取、地名解析和入库的结果。之后用 `evidence_table("micromus", review_status="needs_review")` 只看需要复核的行，再用 `review_evidence` 标为 accepted 或 rejected。

单独解析一个地名：“Yunnan, Kunming, Xishan 在中国的坐标是什么？”→ `georeference_locality("Kunming, Xishan", "China", admin1="Yunnan")`

### 5. 分布数据集与地图

> “用 Micromus 范围和 micromus 证据表生成分布数据集，研究区域是东亚。”
> → `distribution_dataset(name="micromus", scope_id=..., evidence_dataset="micromus", extent=[70, 150, 0, 55])`

`extent` 的顺序是 `[经度最小, 经度最大, 纬度最小, 纬度最大]`。数据集会合并三类来源：LDL 模式标本坐标、LDL 分布引用（按 `country_summary` 处理）、你的证据表。然后自动做坐标 QC，检查经纬度越界、0/0 坐标、重复点、整度精度、超出研究区和地理参照信息不全等问题，结果写在 `qc.json` 里。

> “画 Micromus 的分布图，Nature 风格，双栏宽，按物种分面。”
> → `distribution_map(dataset_id, theme="nature_like", width="double", facet=True)`

- **输出**：`map.svg`、`map.pdf`、`map.png`（默认 600 dpi）、`map_data.csv`（实际画出的点）、`map_qc.json`（被排除的点及原因）、`figure_manifest.json`（数据哈希、参数、底图版本）和 `caption.md`（图注草稿）。
- **`nature_like` 预设**：7 pt 无衬线字体、0.5 pt 线宽、Okabe–Ito 色盲友好配色、单栏 89 mm 或双栏 183 mm、SVG/PDF 中的文字可编辑。不同标记形状区分坐标基础，比例尺和指北针会自动放在没有数据点的角落。`plain_report` 是字号更大的报告风格。
- **底图**：默认使用内置的 Natural Earth 1:50m 陆地和国界数据（公有领域），不用联网。也可以用 `basemap="/path/to/land.geojson"` 换成你自己的底图；投稿前请确认国界画法符合期刊要求。
- **草稿与终稿**：数据集 QC 没有通过时，图会标记为 `draft`，原因写在 manifest 里。修正数据或复核证据后重新生成即可。
- **校验**：“检查这张图是否合格。”→ `figure_validate(figure_id)`：检查文件是否齐全、SVG 文字是否可编辑、PNG dpi、QC 和底图。

**GBIF 对比图层和覆盖度（v0.4）**

- `distribution_dataset(..., include_gbif=True)` 会从 GBIF 公共 API 取出现记录，作为单独的 `occurrence` 图层加进数据集。这一层**不会**混进文献证据。地图上用半透明小三角表示。`gbif_basis_of_record=["PRESERVED_SPECIMEN"]` 可以只保留馆藏标本，排除照片观察记录。没有坐标不确定度或不确定度超过 10 km 的记录会被标记。论文里请引用你在 gbif.org 用相同条件生成的下载 DOI，API 结果本身不能直接引用。
- 每次生成数据集都会写出 `coverage.csv`。“Micromus 哪些分布国家还没有点位证据？”→ `distribution_coverage(dataset_id, species=...)`：列出每个种在 LDL 中接受和存疑的分布国家、已有模式/文献/GBIF 点位支持的国家、还没有点位证据的 LDL 国家，以及有点位但不在 LDL 国家列表里的国家（可能是新记录，也可能是错误）。

地图**从不**绘制分布范围、凸包或生态位模型，也不会把国家级摘要画成点。

### 6. 导出研究包

> “把 micromus 数据集和刚才的地图打包导出。”
> → `export_research_package(dataset_id, figure_ids=[figure_id])`

导出一个 zip 文件，在 `~/.local/share/ldl-mcp/exports/` 下。里面包括：分布表（CSV/TSV/Markdown）、QC 报告、证据表、所引文献的 BibTeX、图件，以及记录每个文件 SHA-256 的 `run_manifest.json`。每个数据点都可以沿这条链追溯：

```text
研究范围快照 → 现行学名 → BibObjID/EDocID → 文件 SHA-256 → PDF 页码 → 原文摘录 → 表格行 → 图件
```

### 7. 一次完整对话示例

```text
1. 打开 LDL 登录窗口。                      （登录后点紫色按钮）
2. 检查登录状态。
3. 下载 Micromus 全属文献，稳定模式，保存到 ~/Research/LDL。
4. 下载进度怎么样？                         （可以随时问；关掉 Codex 也不影响）
5. 补一下失败的文献，然后联网核验是否下全。
6. 对扫描版文献做 OCR（英文+德文）。
7. 冻结 Micromus 现生有效种为研究范围。
8. 从 Micromus 文献自动抽取标本地点到证据表 micromus（没有坐标的用 GeoNames 在原文国家内解析）。
9. 列出 needs_review 和 unverified 的证据，我来复核。
10. 生成东亚分布数据集（加 GBIF 馆藏标本作对比层），画 nature_like 双栏分面图，并校验。
11. 哪些 LDL 分布国家还没有点位证据？
12. 导出研究包。
```

### 8. 常见问题

- **登录窗口没弹出来 / `status: failed`**：本机没有 Chrome、Edge，也没有 Playwright Chromium。执行 `~/.local/share/lacewing-digital-library-mcp/.venv/bin/python -m playwright install chromium` 后重试。
- **任务变成 `auth_required`**：LDL 会话过期了。重新 `auth_start` 登录，再 `download_control(job_id, "resume")`。
- **电脑休眠后任务显示 `interrupted`**：直接 `resume`，会从断点继续。
- **检索不到明明存在的内容**：检查结果里的 `documents_without_text_layer`。如果目标文献在其中，先运行 `library_ocr`。
- **地图一直是 `draft`**：查看数据集 `qc.json` 里的 `blocking_flags`，常见原因是 0/0 坐标或被拒绝的证据。修正后重新生成数据集。
- **想把数据放到外接硬盘**：在 MCP 的环境变量里设置 `LDL_MCP_HOME=/Volumes/Data/ldl-mcp`。
- **同一个属能同时开两个下载吗**：不能。同一属、同一保存位置同时只允许一个任务，避免两个进程写同一个索引。

### 使用规范

- 只下载你的账号有权查看的文献。工具不会绕过登录、权限或付费限制。
- 下载速度刻意放慢（顺序请求，间隔约 0.35 秒），请不要修改为并发下载。
- 下载的全文只用于获准的研究用途，不要再分发；`auth/` 文件夹包含登录信息，不要发给别人。
- LDL 的 `/api/...` 是网站前端使用的接口，并非公开的第三方 API；接口变化时请提交 issue。

## One-line install for Codex

```bash
curl -fsSL https://raw.githubusercontent.com/Gyoungwe/lacewing-digital-library-mcp/main/install.sh | bash
```

Set `LDL_MCP_BRANCH=feat/mcp-downloader` before `bash` to install a branch. The installer clones/updates the project under `~/.local/share/lacewing-digital-library-mcp` (override with `LDL_MCP_INSTALL_DIR`), creates an isolated Python virtual environment, installs the MCP server, copies the Codex skill into `~/.codex/skills/lacewing-digital-library`, and registers the stdio MCP server with `codex mcp add`.

Verify with:

```bash
codex mcp get lacewing-digital-library
```

Then ask Codex:

```text
Use $lacewing-digital-library to resolve Mantispa styriaca and summarize its accepted name, original combination, type locality, distribution and references.
```

## Literature download and research workflow (v0.4)

The genus literature downloader is built into the MCP: `auth_start` opens a separate browser for you to log in (only LDL cookies are stored locally, mode 0600; no passwords), `download_start` runs a detached background job that survives the MCP session, and the `library_*`, evidence, distribution, map and export tools turn the downloaded PDFs into auditable research outputs. See the [Chinese tutorial](#中文使用教程) below for the full walkthrough.

| Group | Tools |
|---|---|
| Login | `auth_start`, `auth_status`, `auth_logout` |
| Download jobs | `download_start`, `download_status`, `download_control`, `download_verify` |
| Local library | `library_status`, `library_verify`, `library_documents`, `library_search`, `library_page_text`, `library_ocr`, `library_merge` |
| Research | `freeze_taxon_scope`, `list_taxon_scopes`, `literature_plan`, `record_evidence`, `evidence_table`, `review_evidence`, `distribution_dataset`, `distribution_map`, `figure_validate`, `export_research_package` |
| Literature → distribution (v0.4) | `literature_distribution` (rule extraction + GeoNames georeferencing + country cross-checks), `georeference_locality`, `distribution_coverage`; `distribution_dataset(include_gbif=True)` adds GBIF occurrences as a separate comparison layer; `freeze_taxon_scope(order=...)` and `download_start(scope_id=..., metadata_only=True)` for order/family-wide catalogues |

Data root: `~/.local/share/ldl-mcp/` (override with `LDL_MCP_HOME`). Optional extras: `[maps]` (cartopy projections), `[ocr]` (pypdfium2; also needs the `tesseract` binary).

## Taxonomy tools

| Tool | Typical research question |
|---|---|
| `resolve_taxon` | What is the accepted/current name and LDL ID for this name? |
| `search_taxa` | Which taxa match a genus, family, author, year, or epithet? |
| `name_history` | What original/historical combinations are associated with this taxon? |
| `taxon_record` | Give me the combined LDL species/epithet/monograph record. |
| `type_info` | What is the type, type locality, and georeferenced type-specimen coordinate? |
| `distribution` | Where is this species recorded and which references support that distribution? |
| `taxonomic_references` | Which nomenclatural/taxonomic sources support this taxon? |
| `figures` | Which published figures are associated with this taxon? |
| `phylogeny` | Which LDL phylogenetic records/citations include this taxon? |
| `identification_keys` | Which identification keys include this taxon? |
| `bibliography` | What is LDL reference `BibObjID`, and which taxa/figures are linked to it? |
| `batch_resolve` | Reconcile a tree tip list / CSV taxon list to current LDL taxonomy. |
| `list_valid_extant_species` | List unique valid extant species in a family, suitable for reproducible sampling. |
| `distribution_sources` | Expand a taxon into LDL distribution records with BibObjID, EDocID, source page, and bibliography metadata. |
| `literature_distribution_seed` | Build a literature-extraction evidence package for one taxon. |
| `batch_literature_distribution_seed` | Build literature-extraction evidence packages for many taxa. |
| `credential_status` | Check whether optional contributor credentials are configured without exposing secrets. |
| `edoc_search` | With locally configured contributor access, search an LDL EDoc PDF for short keyword contexts and PDF pages. |
| `classification` | Retrieve the NSW classification data. |
| `raw_public_endpoint` | Advanced read-only access to an allowlisted LDL endpoint. |
| `health_check` | Can the server reach LDL, and what dataset/version info is visible? |


## Examples

Resolve a taxon:

```text
Resolve Chrysoperla carnea in LDL. Keep the input name, accepted name, author/year, CombObjID, TaxObjID and original combination.
```

Reconcile phylogeny tips:

```text
Use LDL to reconcile these 80 Neuropterida tree-tip names. Do not silently replace names: return input_name, accepted_name, status, CombObjID, TaxObjID and notes for ambiguous/unmatched rows.
```

Type-locality work:

```text
Find the type information and type-locality coordinates for Mantispa styriaca. State clearly whether the coordinates are type-specimen localities or occurrence data.
```

Bibliography-first workflow:

```text
Look up LDL BibObjID 18355, summarize the reference, and list taxa associated with it.
```

## Manual installation

```bash
git clone https://github.com/Gyoungwe/lacewing-digital-library-mcp.git
cd lacewing-digital-library-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
codex mcp add lacewing-digital-library -- "$PWD/.venv/bin/python" -m ldl_mcp
cp -R skill ~/.codex/skills/lacewing-digital-library
```

For another MCP client, use a stdio configuration whose command is the environment's Python and whose arguments are `-m ldl_mcp`.

## Optional LDL Contributor access

Public endpoints remain the default. Restricted EDoc search is opt-in and credentials are never accepted as MCP tool arguments or committed to the repository.

Set `LDL_USERNAME` and `LDL_PASSWORD` in the MCP process environment, or on macOS store only the username in `~/.config/lacewing-digital-library-mcp/config.json` and keep the password in Keychain under service `lacewing-digital-library-mcp`. Use `credential_status` to verify configuration without revealing the password.

`edoc_search` returns only short matching contexts and PDF page numbers. It does not return or redistribute complete restricted documents.

## Data interpretation rules

LDL search results can include current combinations, original combinations, historical placements, junior synonyms, invalid names and unavailable names in the same result set. Do not interpret every returned row as a distinct current species or as a synonym. Preserve `CombObjID`, `TaxObjID` and status fields so mappings remain auditable.

`general/getTypeSpmnCoordinates` provides **georeferenced type-specimen localities**. These coordinates are not a complete species occurrence dataset. The MCP joins them to species using LDL taxonomic identifiers rather than guessing coordinates from locality prose.

## Development and tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # offline, includes a simulated genus download
.venv/bin/python tests/live_smoke.py
.venv/bin/python tests/mcp_smoke.py
```

`live_smoke.py` verifies known LDL data for *Mantispa styriaca*. `mcp_smoke.py` starts the server over stdio, performs MCP initialization, checks the tool inventory, and calls `resolve_taxon` through the MCP protocol.

## License

MIT. Data returned by LDL remain subject to LDL's own terms, access controls, attribution expectations and underlying source rights.
