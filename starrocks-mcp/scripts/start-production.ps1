# 启动 starrocks-mcp 生产服务（在部署包根目录执行）
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ((Split-Path -Leaf $Root) -eq "scripts") { $Root = Split-Path -Parent $Root }

Set-Location $Root
$Config = Join-Path $Root "config\settings.yaml"
if (-not (Test-Path $Config)) {
    Write-Error "缺少 config/settings.yaml，请从 config/settings.production.example.yaml 复制并修改"
}

New-Item -ItemType Directory -Path (Join-Path $Root "logs") -Force | Out-Null

Write-Host "==> 启动 starrocks-mcp"
Write-Host "    配置: $Config"
Write-Host "    健康检查: http://127.0.0.1:8080/healthz"
Write-Host "    MCP 端点: http://127.0.0.1:8080/mcp"

python -m starrocks_mcp.main --config $Config --log-level INFO
