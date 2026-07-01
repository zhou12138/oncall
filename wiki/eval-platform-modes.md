# Eval 平台两种构建方式：后评估 vs 主动评估

> 适用背景：Agent / Skill / LLM App 的质量评估、回归验证、线上监控与版本对比。

## 一句话总结

| 类型 | 核心价值 |
|---|---|
| 后评估平台 Post-hoc Eval | 发现问题：线上已经发生的结果好不好 |
| 主动评估平台 Active Eval | 防止问题复发：新版本上线前能不能稳定做好 |

完整闭环：

```text
后评估发现 bad case
→ 加入 Dataset
→ 主动评估做回归
→ 新版本通过后上线
→ 继续后评估监控
```

---

## 对比表格

| 维度 | 后评估平台 Post-hoc Eval | 主动评估平台 Active Eval |
|---|---|---|
| 核心定义 | 对已经产生的 Trace / Observation / Output 做评估 | 主动用 Dataset / Eval Case 触发 Agent / Skill 执行，再评估结果 |
| 基本流程 | 线上执行 → Trace → Evaluator → Score | Eval Case → Runner → Agent/Skill 执行 → Trace → Evaluator → Score |
| 起点 | 已有执行结果 | 测试用例 / Dataset / Skill / Agent 版本 |
| 是否触发 Agent | 不触发 | 触发 |
| 是否执行 Skill | 不执行，只评估已有结果 | 执行指定 Skill，并记录过程 |
| 数据来源 | 线上真实流量、历史 traces、已有 observations | 人工构造测试集、线上 bad case 沉淀、CI eval cases |
| 主要回答的问题 | “线上已经发生的结果好不好？” | “这个新版本上线前能不能稳定做好？” |
| 典型产出 | Scores、低分 traces、质量趋势、异常样本 | Experiment 对比、回归结果、pass/fail、上线门禁结果 |
| 真实性 | 高，来自真实用户/真实任务 | 中等，取决于测试集是否接近真实场景 |
| 可控性 | 低，输入和场景由线上流量决定 | 高，输入、环境、模型、skill、评分规则都可控 |
| 可复现性 | 中/低，线上数据和环境可能变化 | 高，同一批 case 可重复跑 |
| 适合线上监控 | 很适合 | 不主要用于线上监控 |
| 适合发布前验证 | 弱，问题通常是事后发现 | 强，适合 PR / CI / Release 门禁 |
| 发现未知问题能力 | 强，能从真实流量中发现意外问题 | 弱/中，依赖测试集覆盖范围 |
| 验证边界 case | 弱，线上不一定碰到边界场景 | 强，可以专门构造安全、异常、失败、权限等 case |
| 版本对比能力 | 中，受线上流量分布影响 | 强，可对比 skill v1/v2、prompt A/B、model A/B |
| Ground truth 要求 | 不一定有，常靠 LLM judge / 规则 / 人工标注 | 可以预置 expected output / scoring rules |
| 接入成本 | 低，接入 trace 上报和 evaluator 即可 | 高，需要 Eval Runner、执行环境、trace 上报、评分编排 |
| 运行成本 | 较低，主要是评估已有数据 | 较高，会主动消耗模型、工具、机器和时间 |
| 环境依赖 | 低/中，主要依赖已有 trace 数据 | 高，依赖 Agent、Skill、工具、Gateway、Worker、测试环境 |
| 稳定性风险 | 评估本身较稳定，但数据分布不可控 | 容易受网络、工具、环境、远程机器状态影响 |
| 对 Skill Eval 的作用 | 评估 skill 在线上真实执行后的结果 | 主动验证某个 `SKILL.md` 是否符合预期 |
| 对 LandGod/MCPHub 的作用 | 评估真实工具调用 trace，观察安全性、成功率、延迟和成本 | 主动测试分布式工具调度、worker 路由、权限、安全边界 |
| 平台复杂度 | 较低 | 较高 |
| 适合阶段 | 运行期、上线后、持续监控 | 开发期、测试期、上线前、回归验证 |
| 代表能力 | Langfuse 当前原生强项 | 需要 OpenClaw / Skill Eval Runner / LandGod 配合构建 |
| 最大优点 | 真实、低成本、易接入、能发现线上未知问题 | 可控、可复现、可做版本对比和上线门禁 |
| 最大缺点 | 事后发现，不能阻止坏版本上线 | 构建复杂，成本高，测试集质量决定上限 |

---

## 后评估平台：Post-hoc Eval

### 定义

系统先真实运行，产生 trace / observation / output，平台再对这些结果做评估。

```text
Production Agent / LLM App
        ↓
Trace / Observation
        ↓
Langfuse / Eval Platform
        ↓
Evaluator
        ↓
Scores / Logs / Dashboards
```

### 优点

- **真实**：来自真实用户、真实任务、真实工具环境。
- **低成本**：只需接入 trace 上报和 evaluator。
- **易接入**：不需要先构建复杂 runner。
- **适合持续监控**：能观察质量、成本、延迟、错误率趋势。
- **能发现未知问题**：线上真实输入经常覆盖人工测试集没想到的场景。
- **适合 bad case mining**：低分 trace 可以沉淀进 Dataset，变成主动评估样本。

### 缺点

- **事后发现**：问题已经发生，不能阻止坏版本上线。
- **不可控**：输入、环境、流量分布由线上决定。
- **难复现**：线上状态变化后，问题不一定能稳定重现。
- **Ground truth 缺失**：真实线上数据通常没有标准答案。
- **不适合强门禁**：适合监控，不适合作为发布前唯一依据。

---

## 主动评估平台：Active Eval

### 定义

平台主动拿测试用例触发 Agent / Skill 执行，然后评估这次执行结果。

```text
Langfuse Dataset
        ↓
Skill Eval Runner
        ↓
OpenClaw Agent
        ↓
SKILL.md
        ↓
LandGod / MCPHub / Tools
        ↓
Trace / Observation
        ↓
Langfuse Evaluator
        ↓
Score / Experiment
```

### 优点

- **可控**：输入、环境、模型、skill、评分规则都可以固定。
- **可复现**：同一批 case 可以反复跑。
- **适合版本对比**：skill v1/v2、prompt A/B、model A/B 都能做实验对比。
- **适合上线门禁**：可以接入 PR / CI / Release preflight。
- **适合边界测试**：可专门构造权限、安全、离线、失败恢复等 case。
- **适合 Skill 验收**：能验证某个 `SKILL.md` 是否按预期驱动 Agent 使用工具。

### 缺点

- **构建复杂**：需要 Eval Runner、Agent 执行接口、环境准备、trace 上报和 scorer。
- **运行成本高**：会主动消耗模型、工具、远程机器和时间。
- **依赖测试集质量**：测试集覆盖差会带来虚假的安全感。
- **环境容易 flaky**：Gateway、Worker、网络、浏览器、测试账号都可能影响结果。
- **真实性不如线上**：人工 case 不一定代表真实用户输入分布。

---

## 推荐组合方式

成熟的 Agent/Skill Eval 平台不应该二选一，而应该组合：

```text
线上运行
  ↓
后评估持续监控 traces / observations
  ↓
发现低分、异常、安全风险 bad cases
  ↓
把 bad cases 加入 Dataset
  ↓
主动评估在 CI/发布前做回归
  ↓
新版本通过后上线
  ↓
继续线上后评估
```

分工：

| 组件 | 职责 |
|---|---|
| Langfuse | Trace、Observation、Score、Dataset、Experiment、Dashboard |
| OpenClaw | Agent runtime、Skill 加载、Session 执行 |
| LandGod / MCPHub | 分布式工具能力执行 |
| Skill Eval Runner | 把 Dataset → Agent 执行 → Trace/Score 串起来 |

---

## 最终判断

- **先做后评估**：接入快，立刻能看到真实运行质量。
- **再做主动评估**：用于 skill/prompt/model 改版的回归测试和上线门禁。
- **两者闭环**：后评估负责发现问题，主动评估负责防止问题复发。
