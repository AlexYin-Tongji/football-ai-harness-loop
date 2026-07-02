$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "请先创建 .venv 并安装项目依赖。"
}

$loadedEnvKeys = New-Object System.Collections.Generic.List[string]

function Import-LocalDotEnv {
    $envPath = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path -LiteralPath $envPath)) {
        return
    }
    $entries = New-Object System.Collections.Generic.List[object]
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        $name = $parts[0].Trim()
        if ($name.StartsWith("export ")) {
            $name = $name.Substring(7).Trim()
        }
        if ($name -notmatch "^[A-Z][A-Z0-9_]*$") {
            continue
        }
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and $value[0] -eq $value[$value.Length - 1] -and ($value[0] -eq '"' -or $value[0] -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $entries.Add([PSCustomObject]@{ Name = $name; Value = $value }) | Out-Null
    }
    $override = $env:FOOTPULSE_DOTENV_OVERRIDE -eq "true"
    foreach ($entry in $entries) {
        if ($entry.Name -eq "FOOTPULSE_DOTENV_OVERRIDE" -and $entry.Value -eq "true") {
            $override = $true
            break
        }
    }
    foreach ($entry in $entries) {
        if (-not $override -and [Environment]::GetEnvironmentVariable($entry.Name, "Process")) {
            continue
        }
        [Environment]::SetEnvironmentVariable($entry.Name, $entry.Value, "Process")
        $loadedEnvKeys.Add($entry.Name) | Out-Null
    }
}

Import-LocalDotEnv

$keyPointer = [IntPtr]::Zero
$promptedDeepSeek = $false
if (-not $env:DEEPSEEK_API_KEY) {
    $secureKey = Read-Host "请输入 DeepSeek API Key（输入不会显示）" -AsSecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    $promptedDeepSeek = $true
}
$previousProvider = $env:LLM_PROVIDER

try {
    $env:LLM_PROVIDER = "deepseek"
    & $python -m uvicorn services.report_api.main:app --host 127.0.0.1 --port 8000
}
finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    if ($promptedDeepSeek) {
        Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
    }
    foreach ($name in $loadedEnvKeys) {
        Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    }
    if ($previousProvider) {
        $env:LLM_PROVIDER = $previousProvider
    }
    else {
        Remove-Item Env:LLM_PROVIDER -ErrorAction SilentlyContinue
    }
}
