# 数据分析智能体

面向普通财务人员的本地表格分析工具。上传 `.xlsx` 或 `.csv`，输入自然语言需求，系统会通过本地 Ollama `qwen3.5:4b` 完成规划、计算、证据核验和报告生成。

## 启动

最简单的方式是双击项目根目录的 `start.cmd`。也可以在 PowerShell 中运行：

```powershell
cd D:\数据分析智能体\analyse_agent
.\start.ps1
```

脚本会检查 Ollama 和 `qwen3.5:4b`，同步 Python 依赖，在前端源码更新后自动重新构建，并在服务就绪后打开 `http://127.0.0.1:8000`。运行窗口需要保持开启，按 `Ctrl+C` 停止服务。

可选参数：

```powershell
.\start.ps1 -NoBrowser       # 不自动打开浏览器
.\start.ps1 -Port 8001       # 改用其他端口
.\start.ps1 -SkipBuild       # 已确认 dist 最新时跳过前端检查
```

开发模式可分别运行：

```powershell
uv run uvicorn main:app --reload --host 127.0.0.1 --port 8000
cd web
npm run dev
```

## 环境配置

所有配置项均可用 `ANALYSE_AGENT_` 前缀覆盖，例如：

```powershell
$env:ANALYSE_AGENT_OLLAMA_MODEL = "qwen3.5:4b"
$env:ANALYSE_AGENT_OLLAMA_HOST = "http://127.0.0.1:11434"
```

模型请求默认使用 16384 tokens；长会话由模型摘要，复杂节点在完整输入放不下时最多提升到 32768 tokens，不会静默替换为删减后的数据表或证据目录。每个节点只记录状态和耗时，不保存节点输入、输出、提示词或模型诊断快照。可参考 `.env.example` 覆盖这些参数。

当前 RTX 4060 Laptop 8GB 环境实测 `qwen3.5:4b` 的 16K 和 32K 上下文均可完整加载，Ollama 分别报告约 6.4GB 和 7.1GB。32K 仅在完整节点输入超过 16K 时启用；低显存设备应保持单路并发，并可通过环境变量调低两个上下文参数。

`start.ps1` 在需要自行启动本地 Ollama 时，会默认启用 Flash Attention、`q8_0` KV cache 和单路并发，降低长上下文的显存占用。如果 Ollama 已由桌面程序或其他服务启动，这三个服务端参数应在该 Ollama 进程的环境中配置并重启后才会生效。

任务、文件、证据和报告持久化保存在 `data` 目录；空任务不会出现在历史任务列表中。数据库启动时执行带编号的幂等迁移，并在首次升级前创建 SQLite 在线备份。

任务只会在实际选择上传文件后创建。上传新数据会增加任务的数据版本，并立即作废旧的活动结果和旧证据导出。批量上传会逐文件返回成功或失败；全批失败时保留任务原状态。

分析进行中可以在工作台点击“中止分析”。系统会在当前模型调用或工作流节点结束后安全停止本次运行，保留已有正式结果；没有历史结果的任务会标记为“已中止”，可直接重新提问。

XLSX 解析采用只读流式扫描，只在内存中保留非空单元格。系统会在每个数据区域的前 100 个候选行内识别表头，支持最多三层合并表头，并按连续空行或空列将同一工作表拆为多个独立数据区域。每个数据集会记录来源工作表、单元格范围、表头行和数据行范围。区域识别阈值可通过 `ANALYSE_AGENT_HEADER_SEARCH_ROWS`、`ANALYSE_AGENT_REGION_BLANK_ROW_GAP` 和 `ANALYSE_AGENT_REGION_BLANK_COLUMN_GAP` 调整。

较长会话会在本机通过同一个 `qwen3.5:4b` 模型增量整理为结构化长期记忆。原始消息始终完整保存在 SQLite；分析时组合长期记忆与近期原文，并校验摘要中的证据编号。上下文预算可通过 `ANALYSE_AGENT_MODEL_CONTEXT_TOKENS` 等对应配置覆盖。

分析运行会保留任务状态、错误、工具结果、证据和最终报告，不保存工作流节点输入输出或提示词快照。最终结果按“事实、分析结论、交付状态”三层组织；每条比率、排名、比较和风险结论都绑定原始证据或计算输入。失败时可直接根据错误提示重试，已完成的报告和证据不会被覆盖。

模型提交结构化查询规格，后端校验数据集、字段、聚合、过滤和排序后生成只读 SQL。结论通过模型填写的证据 ID、行号、字段和原始值建立追溯关系；后端只校验这些显式引用是否真实存在，不解析结论文字，也不判断其业务含义。

任务页的“数据工作区”提供分页预览、字段画像、单元格纠错、小批量增删行以及字段语义和单位维护。修改通过乐观锁发布为不可变数据版本，不覆盖旧 Parquet；旧分析结果会立即失效。标量查询会确定性转换为 KPI，相关关系请求支持最多 200 个稳定抽样点的散点图。

侧栏“数据集”保存可复用的数据资产及完整修订历史，可从任意指定版本创建新的分析任务，任务不会自动跟随最新版。“报告库”保存正式报告及版本指纹。两个库均提供永久删除，不提供归档或恢复；删除会移除全部版本和对应文件。数据集仍被任务引用时需先删除相关任务，任务仍关联报告时需先删除报告。

分析执行过程会持久化 `query`、`table`、`chart`、`validation` 和 `error` 五类附件，并通过现有 SSE 事件流增量展示。普通建议、解释和闲聊由意图模型直接回答；报表分析的工具、数据集、查询、结论和图表均由模型选择，后端只执行模型计划并校验字段、只读查询和显式证据坐标。正式报告通过持久化作业队列生成，进程重启后会自动恢复中断作业。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
cd web
npm test
npm run build
npm run test:e2e
```

日常测试默认跳过真实模型回归。需要验证当前 Ollama 模型、提示词和结构化输出时运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -m ollama --run-ollama -q
```

## 核心工作流

详细模块边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。

项目采用混合工作流。意图识别后，只有明确要求查询、统计、比较或计算上传报表的请求才进入严格分析路径；建议、解释、原因和日常对话由模型直接回答。

```text
用户问题
  ├─ 普通问题 → 直接回答 → 结束
  └─ 报表分析 → 准备上下文 → 规划 → 执行查询
                         → 生成报告和分析结论
                         → 证据坐标校验 → 完成
```

证据坐标无效时由模型复核并选择重新规划或重写，轮数只受资源上限约束。后端不会根据结论文字、指标含义或业务关键词改写模型决定。节点输入、输出、提示词和模型思考过程不会持久化。

## 开源开发

核心运行只依赖本地 Ollama、SQLite 和上传的数据文件。长期记忆、报告发布和 Excel 导出属于可选能力，不影响最小分析路径。提交代码前运行：

```powershell
uv sync --dev
\.venv\Scripts\python.exe -m pytest -q
\.venv\Scripts\ruff.exe check app tests
cd web
npm install
npm test -- --run
npm run typecheck
npm run build
```

默认配置见 [.env.example](.env.example)。不要提交 `.env`、`data/`、模型输出、用户上传文件或本地运行日志。

运行日志写入 `data/logs/app.jsonl` 并自动轮转。日志包含请求、任务、运行、节点和耗时标识，
不记录原始单元格、知识正文或完整提示词。

Playwright 使用 `8011` 端口和系统临时目录中的独立数据目录，不会写入日常使用的 `data`。
