# ADR 0008：模型连接韧性与错误分类

- 状态：Accepted
- 日期：2026-07-01

## 背景

DeepSeek 的长耗时推理请求可能在上游已经开始响应后断开连接，客户端表现为
`RemoteProtocolError`。此前异步任务把 `LLMProviderError` 与
`ReportGenerationError` 合并展示为“未通过质量校验”，导致用户误判问题。

## 决策

1. 每次模型网络重试使用新连接，禁用 keep-alive 复用，避免继续使用已被上游关闭的
   socket。
2. 单次模型调用最多尝试 3 次，采用有上限的递增退避；不做无限重试。
3. 同一进程最多并发 2 个 DeepSeek 请求，可通过
   `DEEPSEEK_MAX_CONCURRENCY` 在 1–4 范围内调整。
4. 独立分析席位和日报分桌允许单席失败后降级；最终编辑调用仍需成功，避免用不完整
   JSON 冒充报告。
5. Provider 错误必须携带类型：`authentication`、`billing`、`rate_limit`、
   `bad_request`、`invalid_response` 或 `transient`。401/403 不重试，提示检查
   DeepSeek API Key；402 提示余额；429 与 5xx 才进入有界重试。
6. 对外区分证据不足、报告质量门失败、模型认证/余额/限流/连接失败和内部错误；
   日志只记录安全错误类型、状态码与任务 ID，不保存密钥或模型隐藏推理。
7. 产品状态接口区分“已配置”和“可生成”。一旦运行时观察到
   `authentication`、`billing` 或 `bad_request`，`generation_ready` 降为 false，
   并返回安全的 `model_status/model_issue`，直到服务重启或重新配置。
8. Deep 日报的最终总编辑阶段只接收压缩证据索引与分桌草稿，不重复接收完整证据包。
   分桌研究负责长上下文吸收，总编辑负责合稿、去重和结构化输出。
9. 最终推理阶段允许更长但有上限的模型等待时间：默认基础超时仍由
   `LLM_TIMEOUT_SECONDS` 控制，日报/预测的最终推理请求至少给 240 秒读超时；
   同时记录安全遥测：purpose、model、输入字符量、输出预算、超时和耗时。
10. 人物卡指标与比赛时间线属于编辑增强层。若模型给出的单个指标或分钟无法在引用
    证据中定位，系统移除该可选增强并写入 warning；正文事实、预测概率和引用 ID
    仍按严格质量门处理。
11. 日报最终阶段必须可恢复：高思考总编辑遇到 transient/timeout 时先切换到
    `stable_final` 稳定合稿；若稳定合稿仍因可恢复 provider 错误失败，Harness 使用
    已完成的分桌草稿确定性生成“保守合稿版”，并明确写入 warning。认证、余额、
    参数错误不走该降级路径。

## 后果

偶发断线可在单次任务内恢复，并发峰值更可控。连续三次失败时任务仍会停止并允许用户
重新生成，符合最大重试、超时、成本预算和人工回退要求。

## 验证

- 模拟连续两次 `RemoteProtocolError`、第三次成功，报告调用应恢复。
- 模型服务失败的异步任务不得显示为质量校验失败。
- DeepSeek 401 只能尝试一次，并显示为密钥或权限配置异常，不得显示为连接中断。
- 发生 401 后产品状态应变为 `needs_attention`，不能继续显示可生成。
- 模拟 24 条长证据的 deep 日报时，最终总编辑 prompt 必须使用压缩证据索引，
  输出预算不超过 4500 tokens。
- DeepSeek timeout 与 context/token overflow 必须分开分类，不能都显示为普通连接中断。
- 无证据支持的人物卡指标与时间线分钟应被移除并记录 warning，不能导致整份日报失败。
- 模拟高思考总编辑与稳定合稿均遇到 `RemoteProtocolError` 时，日报仍应由
  deterministic daily finalizer 完成，并标记为保守合稿版。
- 原有 schema、引用和概率质量门不得放宽。
