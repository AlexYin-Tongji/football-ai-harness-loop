# Report API

第一个纵向切片：接收带来源的结构化资料，通过受限 Loop 调用 DeepSeek V4 或本地 mock，返回经过确定性校验的中文足球报告。

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn services.report_api.main:app --reload
```

默认 `LLM_PROVIDER=mock`，无需密钥。真实调用：

```powershell
$env:LLM_PROVIDER="deepseek"
$env:DEEPSEEK_API_KEY="your-key"
uvicorn services.report_api.main:app --reload
```

访问 `/docs` 查看交互式 API 文档。服务不会读取网页或发布社媒；调用方必须先提供已收集、带时间和来源的 evidence。

