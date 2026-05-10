# OnCall Agent 🚨

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

**智能 OnCall 告警分析 Agent** — HTTP 触发 → 三步编排推理 → 自动分析根因 → 通知团队。集成 ADX Kusto / GitHub / Teams。

## ✨ 核心特性

- **三步编排推理** — Triage 分诊 → WoW 环比分析 → Reason & Act 决策
- **MCP 工具集成** — ADX Kusto 查询、GitHub PR 关联、Teams 通知
- **记忆学习** — 每次运行积累上下文，自动提升分析质量
- **多 Provider** — 支持 OpenAI / Anthropic / GitHub Copilot LLM
- **TUI 终端界面** — 交互式终端操作
- **Workspace 管理** — 多项目/多仓库工作空间隔离
- **可观测性** — 全链路 trace、结构化日志

## 快速开始

```bash
# 安装
pip install -e .

# 启动 API 服务
python -m oncall_agent.api

# 或使用 CLI
oncall-agent --help
```

## 触发告警分析

```bash
curl -X POST http://localhost:8090/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "signal_name": "HighCPUAlert",
    "repo": "microsoft/edge",
    "teams_channel": "oncall-alerts"
  }'
```

## 架构

```
HTTP POST /trigger
    │
    ▼
┌─────────────────────────────────────┐
│           Orchestrator               │
│                                      │
│  Step 1: Triage 分诊                 │
│  ├─ ADX Kusto 查询告警数据            │
│  └─ 判断影响范围（全局 vs 局部）       │
│                                      │
│  Step 2: WoW 环比分析                │
│  ├─ ADX: 本周 vs 上周对比             │
│  └─ GitHub: 关联近期 PR/变更          │
│                                      │
│  Step 3: Reason + Act 决策           │
│  ├─ LLM 推理（结合历史记忆）          │
│  ├─ 生成根因分析 + 严重级别            │
│  └─ Teams 通知相关团队               │
└─────────────────────────────────────┘
    │
    ▼
  Memory (JSON) ← 每次运行积累经验
```

## 项目结构

```
oncall_agent/
├── api.py              # FastAPI HTTP 入口
├── cli.py              # 命令行入口
├── orchestrator.py     # 三步编排核心引擎
├── config.py           # 配置管理
├── providers.py        # LLM Provider 抽象
├── routing.py          # 告警路由策略
├── copilot_proxy.py    # GitHub Copilot API 代理
├── workspace.py        # 工作空间管理
├── onboard.py          # 初始化引导
├── tui.py              # 终端交互界面
├── trace.py            # 链路追踪
├── errors.py           # 错误定义
└── logging_config.py   # 日志配置

tests/                  # 测试用例
logs/                   # 运行日志
```

## 配置

```yaml
# config.yaml
llm:
  provider: copilot          # openai / anthropic / copilot
  model: gpt-4o

mcp:
  adx:
    cluster: https://xxx.kusto.windows.net
    database: signals
  github:
    repo: microsoft/edge
  teams:
    webhook: https://xxx.webhook.office.com/...

memory:
  path: ./memory.json
  max_entries: 1000
```

## 技术栈

- **后端**：Python 3.10+ / FastAPI / uvicorn
- **LLM**：OpenAI / Anthropic / GitHub Copilot
- **集成**：ADX Kusto（数据查询）/ GitHub（PR 关联）/ Teams（通知）
- **存储**：JSON（记忆 & 配置），无外部数据库依赖

## License

MIT
