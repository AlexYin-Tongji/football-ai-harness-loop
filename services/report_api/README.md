# Report API 与 Web 工作台

V2 已能从网页提交报告请求，经 Skill 路由、上下文构建、模型生成、确定性质量门和检查点记忆后返回结果。

## 本地运行（无需密钥）

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn services.report_api.main:app --reload
```

访问：

- 工作台：`http://127.0.0.1:8000/`
- API 文档：`http://127.0.0.1:8000/docs`
- 能力清单：`http://127.0.0.1:8000/v1/system/capabilities`

公共页面使用持久化任务接口：

- `POST /v1/research/jobs` 创建任务并立即返回任务 ID。
- `GET /v1/research/jobs/{id}` 返回真实阶段、进度和最终报告。
- 同步兼容接口 `POST /v1/research/reports` 仍保留给内部测试。
- 本地任务数据库默认为 `data/footpulse.db`，不会进入 Git。

默认使用 `LLM_PROVIDER=mock`，页面、Harness 和测试均不需要真实密钥。

## 安全调用 DeepSeek

不要把密钥粘贴到聊天、代码、截图、Issue 或 PR。推荐使用只对当前进程有效的安全启动脚本：

```powershell
.\scripts\run_deepseek.ps1
```

脚本会先读取 Git 忽略的本地 `.env`。如果 `.env` 或进程环境中没有
`DEEPSEEK_API_KEY`，才使用隐藏输入读取密钥。密钥只写入当前服务进程环境，服务退出后
清理变量，不会进入浏览器或 Git。

可用状态检查：

- `/health`：服务是否启动，以及当前是 mock 还是 DeepSeek。
- `/v1/product/status`：DeepSeek、Sportmonks、football-data、NewsAPI、YouTube
  白名单和许可媒体开关是否已配置；只返回布尔值，不返回密钥。

## 当前边界

- 当前 MCP 能力表已定义；Sportmonks、football-data 与许可媒体工具按密钥安全降级。
- 页面使用批准来源和结构化 evidence 跑通闭环。
- 运行历史暂存在当前进程内；生产版将使用 PostgreSQL/Temporal 检查点。
- 服务不读取社媒凭据，也不提供自动发布工具。
