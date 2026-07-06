# PR 草案：Daily brief evidence guardrails

## 变更内容

本 PR 将《今日球脉》日报从“模型创作长文”进一步收束成“证据驱动的关键信息整合”：

- 新增 Key Brief 结构：每个 section 统一输出【核心】【细节】【背景】【下一步】【边界】。
- 关闭日报链路中的图片、视频、GIF、高光规划，避免未完成许可链路影响文本可靠性。
- 强化 claim ledger 和 validation：支持比分方向、金额单位、日期数字和中文紧凑比分。
- 在最终校验前进行本地可确定修复：补同场结构化赛果引用、修正比分方向、泛化无证数字。
- 修复点球大战比分污染：保留常规时间、点球比分和总比分的各自含义。
- 更新设计说明、README 和日报写法基准，说明受控搜索 MCP 的后续方向。

## 用户 / 运营影响

- 用户看到的日报更像关键信息 briefing，而不是拉长的来源复述。
- 比赛基础信息、转会基础信息和缺口说明更稳定。
- 缺少进球者、分钟、红黄牌或合同细节时，报告会明确写【边界】，不会让模型硬编。
- 媒体能力暂时不出现在日报交付中；后续需单独恢复许可媒体链路。

## Root Cause

前一版主要瓶颈不只是模型能力，而是系统边界不够清晰：

- 最终模型承担了过多事实修复、取舍和格式稳定任务。
- 覆盖门槛用英文词边界识别比分，漏掉中文紧凑比分。
- 比分修复器没有区分整场结果、常规时间比分和点球比分。
- 无证数字只能触发重试/兜底，缺少本地安全泛化路径。

## 验证

- [x] 自动测试

```powershell
.venv\Scripts\python.exe -m pytest tests\report_api -q
.venv\Scripts\python.exe -m ruff check services\report_api tests\report_api
```

- [x] 手工验证

DeepSeek live 服务真实跑通日报任务：

- Job: `d859eb9f-13c4-4f77-8925-ac767bff957e`
- Evidence: 25 条
- Desk drafts: 4 个栏目、8 个 section
- Final synthesis: attempt 1 accepted
- Final status: completed

- [x] 失败与降级路径

测试覆盖 final LLM 失败、coverage 缺失、claim 修复、本地数字泛化和 deterministic fallback。

## 风险检查

- [x] 不涉及新数据源、抓取或许可变化。
- [x] 涉及提示词和模型控制，已附设计说明和测试结果。
- [x] 不涉及数据库迁移。
- [x] 不含密钥、付费原始数据、完整新闻正文或用户数据。

## 截图 / Trace / 模型卡

- 设计说明：`docs/architecture/DAILY_BRIEF_EVIDENCE_GUARDRAILS.md`
- ADR：`docs/adr/0022-key-brief-integrator-framework.md`
- 真实任务 trace：`d859eb9f-13c4-4f77-8925-ac767bff957e`
- 本地服务：`http://127.0.0.1:8000/health` 返回 `provider=deepseek`
