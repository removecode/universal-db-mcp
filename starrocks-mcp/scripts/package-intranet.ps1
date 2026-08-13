# 打包 starrocks-mcp 内网离线部署包（Windows 执行，产出面向 Linux 的 tar.gz）
#
# 两种依赖来源：
#
#   1) 复用既有部署包的 wheels（依赖没变时首选，零升级风险）
#      powershell -ExecutionPolicy Bypass -File scripts/package-intranet.ps1 `
#          -ReuseWheels dist/_reuse/<旧包目录>/wheels
#
#   2) 按锁定文件重新下载（需要访问 pip 源）
#      powershell -ExecutionPolicy Bypass -File scripts/package-intranet.ps1
#
# 为什么必须用锁定文件而不是 requirements-prod.txt：后者全是 `>=` 下界，
# 每重打一次包都会拿到当时的最新版。mcp 目前最新已是 2.0.0，而本项目验证过的
# 是 1.28.x，直接重新解析会把大版本一起换掉。

param(
    [string]$ReuseWheels = "",
    [string]$LockFile = "requirements-lock-linux-py312.txt",
    [string]$PlatformLabel = "linux-x86_64",
    [string]$TargetPlatform = "manylinux_2_17_x86_64",
    [string]$PythonVersion = "3.12",
    [string]$Abi = "cp312"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Version = "0.1.0"
$Stamp = Get-Date -Format "yyyyMMdd"
$PyTag = "py" + $PythonVersion.Replace(".", "")
$OutName = "starrocks-mcp-${Version}-intranet-${PlatformLabel}-${PyTag}-${Stamp}"
$Stage = Join-Path (Join-Path $Root "dist") $OutName
$Wheels = Join-Path $Stage "wheels"

Write-Host "==> 清理并创建目录: $Stage"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
foreach ($d in @("wheels", "config", "scripts", "docs", "logs")) {
    New-Item -ItemType Directory -Path (Join-Path $Stage $d) -Force | Out-Null
}

Write-Host "==> 构建项目 wheel"
python -m pip install --upgrade build wheel pip -q
python -m build --wheel --outdir $Wheels
if ($LASTEXITCODE -ne 0) { throw "build wheel failed" }

if ($ReuseWheels) {
    if (-not (Test-Path $ReuseWheels)) { throw "ReuseWheels 目录不存在: $ReuseWheels" }
    Write-Host "==> 复用既有依赖 wheel: $ReuseWheels"
    # 排除 starrocks_mcp 自身，那个用上面刚构建的新版本
    Get-ChildItem -Path $ReuseWheels -File |
        Where-Object { $_.Name -notlike "starrocks_mcp-*" -and ($_.Extension -in ".whl", ".gz") } |
        ForEach-Object { Copy-Item $_.FullName $Wheels }
} else {
    if (-not (Test-Path $LockFile)) { throw "找不到锁定文件: $LockFile" }
    Write-Host "==> 按锁定文件下载 Linux 依赖 wheel: $LockFile"
    # --no-deps 是必须的：pip 的 --platform 不改变环境标记的求值，带依赖解析时
    # 仍会按 Windows 去要 pywin32，直接 ResolutionImpossible
    python -m pip download -r $LockFile -d $Wheels --no-deps --only-binary=:all: `
        --platform $TargetPlatform --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 `
        --python-version $PythonVersion --implementation cp --abi $Abi
    if ($LASTEXITCODE -ne 0) { throw "pip download failed" }
}

Write-Host "==> 复制配置模板、文档与依赖清单"
Copy-Item config/settings.example.yaml, config/settings.production.example.yaml, `
          config/apikeys.example.yaml (Join-Path $Stage "config")
Copy-Item README.md $Stage
Copy-Item docs/api.md, docs/local-cursor.md, docs/deploy-intranet.md (Join-Path $Stage "docs")
Copy-Item requirements-prod.txt, requirements-prod.constraints.txt $Stage
if (Test-Path $LockFile) { Copy-Item $LockFile $Stage }

Write-Host "==> 复制安装/启动脚本"
Copy-Item scripts/install-offline.ps1, scripts/install-offline.sh, `
          scripts/start-production.ps1, scripts/start-production.sh (Join-Path $Stage "scripts")

# Windows 上打出来的 .sh 若带 CRLF，在 Linux 会报 bad interpreter: ...^M
Get-ChildItem -Path (Join-Path $Stage "scripts") -Filter *.sh | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes).Replace("`r`n", "`n")
    [System.IO.File]::WriteAllBytes($_.FullName, [System.Text.Encoding]::UTF8.GetBytes($text))
}

New-Item -ItemType File -Path (Join-Path $Stage "logs/.gitkeep") -Force | Out-Null

$depSource = if ($ReuseWheels) { "复用自 $ReuseWheels" } else { $LockFile }
$versionText = @"
starrocks-mcp $Version
packaged_at: $(Get-Date -Format o)
platform: $PlatformLabel
target_python: $PythonVersion
build_python: $(python --version 2>&1)
deps: $depSource

wheels:
$((Get-ChildItem $Wheels -File | Sort-Object Name | ForEach-Object { "  " + $_.Name }) -join "`n")
"@
[System.IO.File]::WriteAllText((Join-Path $Stage "VERSION.txt"), $versionText.Replace("`r`n", "`n"))

$Tarball = Join-Path (Join-Path $Root "dist") "${OutName}.tar.gz"
if (Test-Path $Tarball) { Remove-Item -Force $Tarball }
tar -czf $Tarball -C (Join-Path $Root "dist") $OutName
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

Write-Host ""
Write-Host "完成: $Tarball"
Write-Host ("大小: {0} MB" -f [math]::Round((Get-Item $Tarball).Length / 1MB, 2))
