# 内网离线安装 starrocks-mcp（在解压后的部署包根目录执行）
# 用法: powershell -ExecutionPolicy Bypass -File scripts/install-offline.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ((Split-Path -Leaf $Root) -eq "scripts") { $Root = Split-Path -Parent $Root }

$Wheels = Join-Path $Root "wheels"
if (-not (Test-Path $Wheels)) {
    Write-Error "未找到 wheels 目录: $Wheels"
}

Write-Host "==> 从本地 wheels 离线安装（建议 Python 3.10+）"
python -m pip install --no-index --find-links $Wheels starrocks-mcp

Write-Host ""
Write-Host "安装完成。下一步:"
Write-Host "  1. 复制 config/settings.production.example.yaml -> config/settings.yaml"
Write-Host "  2. 复制 config/apikeys.example.yaml -> config/apikeys.yaml 并填写用户/apikey 哈希"
Write-Host "  3. 设置环境变量 STARROCKS_READ_PASSWORD / STARROCKS_WRITE_PASSWORD"
Write-Host "  4. 运行 scripts/start-production.ps1"
