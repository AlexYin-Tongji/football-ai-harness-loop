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

默认使用 `LLM_PROVIDER=mock`，页面、Harness 和测试均不需要真实密钥。

## 安全调用 DeepSeek

不要把密钥粘贴到聊天、代码、截图、Issue 或 PR。推荐使用只对当前进程有效的安全启动脚本：

```powershell
.\scripts\run_deepseek.ps1
```

脚本使用隐藏输入读取密钥，只写入当前服务进程的环境，服务退出后清理变量。密钥不会进入浏览器或 Git。

## 当前边界

- 当前 MCP 能力表已定义，但真实新闻和赛事 MCP 尚未连接。
- V2 页面使用明确标识的演示 evidence 跑通闭环。
- 运行历史暂存在当前进程内；生产版将使用 PostgreSQL/Temporal 检查点。
- 服务不读取社媒凭据，也不提供自动发布工具。
