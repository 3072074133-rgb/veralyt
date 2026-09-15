# Veralyt

> **Analytics you can verify.** 让每个数据结论都能回到原始证据。

[![CI](https://github.com/3072074133-rgb/veralyt/actions/workflows/ci.yml/badge.svg)](https://github.com/3072074133-rgb/veralyt/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-black.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)](pyproject.toml)
[![Vue 3](https://img.shields.io/badge/Vue-3-42b883.svg)](web/package.json)

Veralyt 是一个本地优先、证据可追溯的表格分析智能体。上传 Excel 或 CSV，用自然语言描述问题，系统会规划查询、执行只读计算、核验结果并生成带证据引用的结构化报告。

项目面向财务和经营分析场景，既可使用本地 Ollama，也支持 DeepSeek、OpenAI、通义千问、Kimi 及其他 OpenAI 兼容服务。

> [!IMPORTANT]
> Veralyt 当前处于早期公开版本。模型输出可能存在错误；重要业务决策应结合报告中的证据与原始数据复核。

## 核心能力

- **自然语言分析**：模型根据问题自主规划 SQL、补查数据并判断何时生成报告。
- **证据可追溯**：比率、排名、比较和风险结论可以关联原始行或计算输入。
- **复杂工作簿解析**：支持多工作表、多层合并表头和同一工作表中的多个数据区域。
- **只读查询边界**：使用 SQL AST 校验、任务数据表作用域、超时、内存和返回行数限制。
- **结构化报告**：报告章节由模型根据用户要求组织，页面、HTML 和 Excel 使用相同内容顺序。
- **数据版本管理**：数据修订形成不可变版本，历史分析不会自动切换到新数据。
- **持久化任务**：会话、查询、证据、报告和队列状态保存在本地，进程重启后可以恢复。
- **多模型支持**：可在界面中切换本地模型或 OpenAI 兼容云端模型。

## 工作流程

```mermaid
flowchart LR
    A[上传 Excel / CSV] --> B[解析数据区域与 Schema]
    B --> C[提出自然语言问题]
    C --> D{意图识别}
    D -->|普通问题| E[直接回答]
    D -->|数据分析| F[模型规划只读 SQL]
    F --> G[安全校验与执行]
    G --> H{结果是否充分}
    H -->|需要补查| F
    H -->|可以交付| I[生成结构化报告]
    I --> J[模型终审]
    J --> K[页面 / HTML / Excel]
```

详细的模块边界和设计原则见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+
- npm 10+
- 本地推理需要 [Ollama](https://ollama.com/) 和可用模型；使用云端模型时不要求 Ollama

默认模型为 `qwen3.5:4b`。低显存设备可以在 `.env` 中降低上下文容量。

## 快速开始

### Windows 一键启动

```powershell
git clone https://github.com/3072074133-rgb/veralyt.git
cd veralyt
copy .env.example .env
.\start.ps1
```

也可以双击 `start.cmd`。脚本会同步依赖、检查前端构建并打开 `http://127.0.0.1:8000`。

常用参数：

```powershell
.\start.ps1 -NoBrowser
.\start.ps1 -Port 8001
.\start.ps1 -SkipBuild
```

### 手动启动（Windows、Linux、macOS）

安装后端和前端依赖：

```bash
git clone https://github.com/3072074133-rgb/veralyt.git
cd veralyt
cp .env.example .env
uv sync --dev
cd web
npm ci
cd ..
```

分别启动两个开发服务：

```bash
uv run uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

```bash
cd web
npm run dev
```

前端开发地址为 `http://127.0.0.1:5173`，API 地址为 `http://127.0.0.1:8000`。

## 模型配置

启动后可以在“模型设置”中配置供应商、模型名称、Base URL、API Key、温度和最大输出长度。配置保存在本机 `data/model-settings.json`，API 只返回密钥掩码。

也可以复制 `.env.example` 为 `.env` 后编辑。为兼容早期版本，环境变量继续使用 `ANALYSE_AGENT_` 前缀：

```env
ANALYSE_AGENT_OLLAMA_HOST=http://127.0.0.1:11434
ANALYSE_AGENT_OLLAMA_MODEL=qwen3.5:4b
```

任务、文件、证据和报告默认保存在 `data/`。该目录、`.env`、日志和上传文件均已被 Git 忽略。

## 使用方式

1. 点击“新建分析”并上传 `.xlsx` 或 `.csv` 文件。
2. 在数据工作区确认字段、单位和数据范围。
3. 输入分析目标，例如“分析收入、利润和经营现金流的变化，并给出行动建议”。
4. 检查报告中的证据入口和风险说明。
5. 将正式结果发布到报告库，或导出 HTML、Excel。

修改数据会创建新的数据版本，并使旧的活动结果失效。修改报告会保留旧版本，再生成新的结构化结果。

## 数据与隐私

- 本地 Ollama 模式下，表格和提示词不需要发送给第三方模型服务。
- 使用云端模型时，生成请求所需的数据上下文会发送给所配置的服务商。
- 系统不持久化模型思考过程、节点输入输出或完整提示词快照。
- 运行日志不记录原始单元格、知识正文或完整提示词。
- 删除数据集、任务或报告属于永久删除，界面会在执行前明确提示。

请勿使用未经授权的数据测试云端模型。提交 Issue 时不要附带真实报表、数据库、日志或访问凭据。

## 验证

后端：

```bash
uv sync --dev
uv run pytest -q
uv run ruff check app tests
uv run pytest --cov=app --cov-report=term-missing -q
```

前端：

```bash
cd web
npm ci
npm test
npm run typecheck
npm run build
npm run test:e2e
```

日常测试默认跳过真实模型回归。已安装本地 Ollama 和指定模型后，可运行：

```bash
uv run pytest -m ollama --run-ollama -q
```

## 项目结构

```text
veralyt/
├── app/                  # FastAPI、工作流、数据、报告与导出服务
├── prompts/              # 模型提示词与结构化输出约定
├── scripts/              # OpenAPI 和维护脚本
├── tests/                # 后端、工作流和真实模型回归测试
├── web/                  # Vue 3 前端及 Playwright 测试
├── ARCHITECTURE.md       # 架构边界
├── CONTRIBUTING.md       # 贡献指南
└── SECURITY.md           # 安全问题报告方式
```

## 当前限制

- 默认启动脚本主要针对 Windows；其他平台请使用手动启动方式。
- 当前输入格式为 Excel 和 CSV，尚未提供远程数据库连接器。
- 模型分析质量取决于数据质量、问题描述、模型能力和可用上下文。
- 真实模型回归需要单独准备 Ollama 服务，不在公共 CI 中运行。

## 路线图

- 提供 Linux/macOS 一键启动脚本和容器化部署
- 增加数据库连接器和更多文件格式
- 增加英文界面与英文文档
- 增加可复用的分析模板和评测数据集
- 完善性能基准与模型质量评测

## 参与贡献

欢迎提交 Issue 和 Pull Request。开始开发前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。安全问题请按照 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

Veralyt 使用 [MIT License](LICENSE)。
