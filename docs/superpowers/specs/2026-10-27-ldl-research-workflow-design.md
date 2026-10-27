# LDL 科研工作流与分布图设计

状态：v0.3 已实现第 1–5 阶段的最小闭环；v0.4 增加文献自动抽取分布、GeoNames 地名解析和 GBIF 对比层（见第 11 节）。

日期：2026-10-27

## 1. 背景与目标

当前仓库已经能够访问 Lacewing Digital Library（LDL）的分类、类型、分布、图版、系统发育、检索表和参考文献数据。随附的 LDL 属文献下载器还提供了独立浏览器登录、三类 Citations 读取、EDoc 下载、断点续传、失败补传、SHA-256 去重和完整性核验。

目标是把这些能力整合成一个由 MCP 驱动的本地科研工作流：用户从科研问题开始，经过分类范围锁定、文献计划、授权下载、本地全文检索、证据抽取和质量核验，最后得到可复核的表格、总结、分布图和出版级 figure。

MCP 负责研究编排和本地资料处理；LDL 的登录权限仍由用户在 MCP 启动的独立浏览器中完成。密码不会作为 MCP 参数传递，也不会写入日志或研究结果。

## 2. 范围与非目标

### 范围

- 统一 LDL 分类与文献元数据，并保存一次研究范围快照。
- 迁移现有下载器的文献发现、下载、续传、补失败、去重、核验和合并能力。
- 读取下载器产生的 `library_index.json`、`progress.json`、CSV 报告和本地文献。
- 在 PDF 中检索关键词，返回页码、上下文和来源标识。
- 从文献和 LDL 记录生成带证据链的分布、类型和分类表。
- 生成可复现的分布地图和 Nature 风格的出版级 figure 预设。
- 导出 CSV、TSV、Markdown、BibTeX、SVG、PDF 和 PNG，并附带运行清单。

### 非目标

- 绕过 LDL 的贡献者权限、验证码或付费限制。
- 默认下载 `Image Gallery` 图片或重新分发受限制的完整文献。
- 将分布摘要自动转换为标本地点。
- 在没有原文证据时自动补写缺失的地点、坐标或分类意见。
- 声称生成的图件符合 Nature 的投稿要求；预设只提供可审查的出版级排版基础。

## 3. 总体架构

```text
科研问题 -> taxonomy/scope -> literature plan -> browser authentication
          -> background download job -> local library and evidence index
          -> tables, summaries and comparison -> validated maps and figures
```

内部模块边界如下：

```text
src/ldl_mcp/
├── client.py              LDL API、节流、重试和授权请求
├── taxonomy.py            分类解析、名称历史和范围快照
├── literature.py          Citations、BibObjID/EDocID 和下载计划
├── auth_browser.py        独立浏览器、登录状态和会话检查
├── downloader.py          分段下载、断点续传和失败补传
├── library.py             本地文献库、SHA-256、索引和完整性核验
├── evidence.py            PDF 文本、页面定位和证据行
├── distribution.py        坐标标准化、质量检查和地图数据集
├── figures.py              figure 规范、渲染和验证
├── research.py             总结、冲突比较和导出
├── jobs.py                 后台任务状态、暂停、恢复和取消
└── server.py               MCP 工具注册与输入校验
```

现有 JSON/CSV 文件格式继续兼容。新的任务状态可以增加一个轻量的本地任务清单，但不应要求用户重新下载已有属文献库。

## 4. 端到端科研流程

### 4.1 研究范围锁定

用户可以从属、科、物种列表或自然语言科研问题开始。系统先解析现行名称，再保留输入名称、现行名称、`CombObjID`、`TaxObjID`、名称历史和状态字段。

`freeze_taxon_scope` 生成不可变范围快照，包含查询条件、物种及当前组合、LDL 数据版本、查询时间、有效/无效/化石/无法解析项目数量，以及后续文献任务使用的快照 ID。

### 4.2 文献计划

对每个物种读取 Taxon、Distribution 和 Image Citations。每条记录保留 `BibObjID`、`EDocID`、引用类别、引用文本、页码和可用性。

文献去重顺序为：`BibObjID/r#`、`EDocID`、最终文件 SHA-256。没有整篇 EDoc 的引用保留在“只有引用、没有文件”的清单中，不计作下载失败。

文献计划支持按年份、国家/地区、引用类别、整篇 EDoc 可用性、类型/分布/材料关键词、物种覆盖数排序或筛选。

### 4.3 授权与后台下载

`auth_start` 启动使用独立用户目录的 Chrome/Edge。用户在其中登录 LDL 后，`auth_status` 验证会话。MCP 不读取日常浏览器 profile，也不接受密码参数。

`download_start` 返回 `job_id` 后在后台执行。任务状态包括 `planned`、`auth_required`、`running`、`paused`、`completed`、`attention_required`、`cancelled` 和 `failed`。

下载器保持现有策略：低速顺序请求、分段 Range 下载、有限重试、原子写入、按引用和内容去重、断点保存、失败清单和完整性报告。

### 4.4 本地文献库与证据索引

下载完成后，系统读取 `library_index.json`、`progress.json`、物种关系 CSV 和本地文献。每个文件记录路径、大小、SHA-256、`BibObjID`、`EDocID`、物种关联和引用类别。

PDF 解析分为已有文本层、版面文本层和可选 OCR 层。OCR 结果标记为 `ocr`，不能与原始文本证据混淆。全文检索返回 PDF 页码、关键词、上下文、文件 SHA-256 和来源 EDoc。

### 4.5 证据表与总结

所有抽取结果同时保存结构化值和证据字段：原始值、规范化值、原文摘录、PDF 页码、`BibObjID`、`EDocID`、证据等级、置信度和人工复核状态。

总结工具只能引用证据表和 LDL 结构化记录。缺失文献、无法读取的页面和冲突观点必须单独列出，不能转换成“没有该信息”。

## 5. 分布研究与地图设计

### 5.1 分布数据集

`distribution_dataset` 从三类来源构建规范化数据：LDL 分布摘要和分布引用、PDF 中的材料/标本/类型/地点证据、LDL 类型标本坐标接口。

每行至少包含：

```text
species, accepted_name, input_name
locality_original, locality_normalized, country
latitude, longitude, coordinate_basis, type_status
collector, collection_date, source_citation
BibObjID, EDocID, pdf_page, evidence_quote
evidence_level, confidence, review_status
```

`coordinate_basis` 明确区分 `type_specimen`、`literature_locality`、`occurrence`、`country_summary` 和 `inferred`。默认地图只绘制前两类有明确地理依据的记录；`country_summary` 和 `inferred` 只能作为单独图层或表格显示。

### 5.2 坐标质量控制

- 纬度必须在 `[-90, 90]`，经度必须在 `[-180, 180]`。
- 保留原始精度和坐标来源，不从地名猜坐标后伪装成原始数据。
- 识别重复点、同一标本的多条记录、异常海上点和超出研究区的点。
- 地名解析得到的坐标标记解析器、边界数据版本和不确定性半径。
- 地图生成前输出 QC 报告；存在未处理异常时可以生成草图，但不能标记为最终图。

### 5.3 地图类型

支持类型标本地点图、文献记录点图、多物种分面图、属级点密度图、国家/地区摘要图，以及同一物种的历史与现行记录对比图。

默认不绘制未经证据支持的连续分布面、凸包或生态位范围。若用户明确要求估计范围，必须把它作为模型或推断图层，并在图例和 manifest 中标记方法、输入和不确定性。

### 5.4 Nature 风格 figure 预设

系统提供可复现的 `nature_like` figure preset，而不是声称使用或复制某个期刊模板。预设包括：

- 简洁无衬线字体、适合缩印的字号和线宽；
- 色盲友好的物种配色和灰度可读性；
- 统一图例、比例尺、指北针、经纬网和面板标签；
- 地图、分布表、分类状态和证据统计的多面板布局；
- 适合后续编辑的 SVG/PDF 矢量输出；
- 300 或 600 dpi PNG 输出；
- 图注、数据来源、边界数据版本和生成参数写入 figure manifest。

figure 规范与数据分离：同一分布数据集可以选择 `nature_like`、`plain_report` 或用户自定义主题重新渲染，而不会改变数据和证据。

### 5.5 地图输入与输出

`distribution_map` 的输入包括数据集 ID、物种/属筛选、坐标基础、研究区域、投影、底图版本、颜色主题、是否分面和输出格式。

输出包括 `map.svg`、`map.pdf`、`map.png`、`map_data.csv`、`map_qc.json`、`figure_manifest.json` 和可直接放入论文的图注草稿。

每个输出保存数据集哈希、范围快照 ID、底图数据版本、投影参数、渲染版本和来源统计。

## 6. MCP 工具分组

### 分类与范围

```text
resolve_taxon
search_taxa
freeze_taxon_scope
classification
name_history
taxon_record
type_info
distribution
taxonomic_references
figures
phylogeny
identification_keys
species_versions
```

### 文献与任务

```text
literature_plan
auth_start
auth_status
download_start
download_status
download_pause
download_resume
download_retry_failed
library_status
library_verify
library_merge
```

### 证据与研究输出

```text
library_search
edoc_search
extract_literature_evidence
build_evidence_table
distribution_dataset
distribution_map
figure_validate
compare_taxonomic_claims
summarize_literature
export_research_package
```

长任务工具只返回任务状态和 `job_id`，不把下载过程阻塞在单次 MCP 调用中。

## 7. 数据表与证据规则

文献目录、物种—文献关系、分布证据和分类意见比较表都保留原始值、规范化值和来源标识。最小证据链为：

```text
研究范围快照 -> accepted taxon -> BibObjID/EDocID -> 本地文件 SHA-256
-> PDF 页码 -> 原文摘录 -> 结构化字段 -> 图表或总结
```

没有页码或结构化 API 来源的结论标记为 `unverified`。类型坐标是类型标本地点，不等同于完整物种分布；分布概述不等同于单个标本地点。

## 8. 错误处理与安全

- LDL 公开接口失败时保留已完成缓存并报告具体端点。
- 会话过期时暂停下载任务，返回 `auth_required`，不自动反复提交登录。
- 单篇下载失败写入失败清单并继续后续项目。
- 写文件、索引和报告均采用临时文件加原子替换。
- 取消任务保留可恢复状态，不删除已验证文献。
- 日志遮蔽 Cookie、授权头和密码。
- 对限制访问的文献只返回用户已获授权的短上下文、页码和元数据。

## 9. 测试与验收标准

### 离线测试

- 使用固定 LDL JSON fixture 测试分类解析、三类 Citations、去重和证据字段。
- 使用小型 PDF fixture 测试页码检索、OCR 标记和上下文截取。
- 使用坐标 fixture 测试范围检查、重复点、异常点和地图数据集哈希。
- 使用图件 fixture 测试 SVG/PDF/PNG 输出、图例、比例尺和 manifest。
- 测试任务中断、恢复、失败补传和索引损坏恢复。

### 在线 smoke

- LDL 健康检查和已知物种解析。
- 授权会话状态检查，不打印秘密。
- 小规模文献计划和单篇下载验证。

### 验收条件

1. 首次运行可以启动独立浏览器并完成用户登录。
2. 同一文献在多物种、多类别出现时只下载一次。
3. 任务中断后可以从索引和断点恢复。
4. 研究结果中的每个分布点都能追溯到坐标来源或 PDF 页码。
5. 地图不会把类型坐标误标为完整分布，也不会静默绘制推断范围。
6. 图件可以从同一数据集重复生成，并包含完整 manifest。
7. 现有下载器生成的文献库无需重新下载即可被 MCP 读取。

## 10. 实施阶段

实现仍分阶段进行，但目标是完整科研闭环：

1. 迁移下载器核心、浏览器授权和任务状态。
2. 建立本地文献库适配器和完整性核验。
3. 增加全文检索、证据表和分布数据集。
4. 增加地图、`nature_like` figure preset 和 figure QC。
5. 增加总结、冲突比较和多格式导出。

本设计不包含实现代码、下载器迁移代码或地图底图文件。进入实施前，需要确定本地数据目录默认位置、是否启用 OCR，以及第一批支持的地图区域和导出格式。

## 11. 实施记录与修订（2026-10-27，v0.3）

### 已落地

- 下载器迁移：`downloader.py` 保留原脚本的三类 Citations 解析、r#/EDoc/SHA-256 三层去重、Range 分段、断点续传、补失败和完整性报告，原离线自检原样通过。浏览器只负责登录；下载通过 `ldl_http.RequestsAPIContext` 携带已保存的 LDL Cookie 发起请求。
- 授权：`auth_start` 启动独立 profile 的浏览器，用户点击页内按钮后只保存 `lacewing.tamu.edu` Cookie（0600）。不接受密码，返回值不含 Cookie 值。
- 后台任务：worker 以独立进程运行（`start_new_session`），不依附 stdio MCP 会话。状态、控制和日志写在 `jobs/<job_id>/`；进程消失时报告 `interrupted`，可以 resume。暂停/取消在请求边界生效，不会误记为下载失败。
- 文献库、全文检索、范围快照、文献计划、证据表、分布数据集与 QC、`nature_like` 地图、figure 校验和导出包均已实现，并有离线测试。

### 相对原设计的修订

1. **分工**：证据抽取由 agent 读取页面后提交，MCP 负责校验（摘录必须出现在所引页码）和入库；`accepted/rejected` 只能经 `review_evidence` 人工设定。`summarize_literature` 和 `compare_taxonomic_claims` 不做成工具，由 agent 基于证据表完成。
2. **坐标基础**：新增 `georeferenced`，与 `inferred` 区分。`country_summary` 和 `inferred` 在 `distribution_map` 中被拒绝绘制。
3. **工具合并**：暂停、取消、恢复、补失败合并为 `download_control`；`jobs_list` 并入 `download_status`；`library_find` 并入 `library_status`。共 43 个工具，其中 21 个为旧版兼容工具。
4. **底图**：内置 Natural Earth 1:50m 陆地和国界（约 0.9 MB gzip，公有领域），cartopy 为可选。国界采用 Natural Earth 的实际控制线画法，投稿前需按期刊政策核对。
5. **待定项的决定**：数据目录 `~/.local/share/ldl-mcp/`（`LDL_MCP_HOME` 覆盖）；OCR 暂不启用，无文本层的 PDF 在检索结果中单列；导出格式 SVG/PDF/PNG + CSV/TSV/Markdown + BibTeX。

### 第二轮补全（同日）

- OCR：`library_ocr` 用本机 Tesseract（经 pypdfium2 渲染）只补没有文本层的页面，结果单独缓存；检索命中和证据核验均标注 `text_source/quote_text_source = ocr`。
- 跨属合并：`library_merge` 跨平台、非破坏性，先 dry-run，执行时复制或硬链接并逐个 SHA-256 校验，不删除源文件；生成的 `global_library_index.json` 与原管理器字段兼容。
- 地理参照：`georeferenced` 行必须带 `georeference_source` 和 `coordinate_uncertainty_m`，否则在证据表中报告问题、在数据集 QC 中标记 `georeference_incomplete`，并且默认不上地图。MCP 不内置地名库，由 agent 或用户提供有出处的坐标。
- 安装：`install.sh` 支持 `LDL_MCP_BRANCH`；代码安装目录改用 `LDL_MCP_INSTALL_DIR`，与数据目录 `LDL_MCP_HOME` 不再冲突。README 增加中文使用教程。

### 第三轮（v0.4）

修订第二轮“MCP 不内置地名库”的决定：手工为大量物种地理参照不现实，因此加入受约束的离线地名解析，但规则仍是不猜测。

- 范围：`freeze_taxon_scope` 支持 `order`。`download_start(scope_id=...)` 为整个范围建一个共享文献库；`metadata_only=True` 不需登录，只抓物种记录和三类 Citations，生成 `species_catalog.csv`、`citations.csv` 和 `literature_plan.json`（`metadata.py`，可断点续跑，作为后台任务运行）。
- 规则抽取（`extract.py`）：识别材料/模式/分布段落（多语种标题），拆分记录，解析国家、地点、坐标、海拔、日期、采集人和模式类型；`evidence_quote` 必须是页面原文的子串。LDL 引用的印刷页码通过页眉换算为 PDF 页码。
- 地名解析（`gazetteer.py`）：GeoNames 国家级数据（CC BY 4.0），按国家首次下载后建 SQLite 索引。只在原文所写国家内解析，能确定省份时再限定省份；结果分 `resolved/probable/coarse/ambiguous/not_found`，只有前两类写坐标，并自动填写 `georeference_source` 和 `coordinate_uncertainty_m`；`ambiguous` 只返回候选。
- 交叉核对（`geo.py`）：内置 Natural Earth admin-0 增加名称和 ISO 字段，用于国家名规范化和点是否落在所述国家（海岸容差 15 km）；点所在国家不在 LDL 接受的分布国家中时标为 `needs_review`。
- 流水线（`lit_distribution.py`）与工具：`literature_distribution`、`georeference_locality`、`distribution_coverage`。
- GBIF（`gbif.py`）：`distribution_dataset(include_gbif=True)` 把出现记录作为独立 `occurrence` 图层，不并入文献证据；缓存 30 天；缺少不确定度或超过 10 km 的记录被标记。发表时应引用 gbif.org 生成的下载 DOI。
- 后台任务：判断 worker 是否存活时同时核对进程命令行，避免重启后 PID 被复用导致误判为运行中。
- 工具总数 48（上限 50）。

### 尚未完成

- 真实登录与 EDoc 下载未在沙箱中端到端验证，需要在本机执行一次 `auth_start` → `download_start`。
- GeoNames 下载和 GBIF API 未在沙箱中联网验证，需要在本机对一个小属跑一次 `literature_distribution` 和 `distribution_dataset(include_gbif=True)`。
- 规则抽取对排版混乱的老文献和 OCR 文本召回有限，未识别的段落仍需 agent 阅读页面后用 `record_evidence` 补录。
