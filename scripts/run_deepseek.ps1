$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "请先创建 .venv 并安装项目依赖。"
}

$secureKey = Read-Host "请输入 DeepSeek API Key（输入不会显示）" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    $env:LLM_PROVIDER = "deepseek"
    & $python -m uvicorn services.report_api.main:app --host 127.0.0.1 --port 8000
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:LLM_PROVIDER -ErrorAction SilentlyContinue
}

