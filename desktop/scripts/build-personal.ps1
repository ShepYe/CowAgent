param([string]$Python = ".venv\Scripts\python.exe", [switch]$BackendOnly)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repo
& node desktop/scripts/apply-flavor.mjs personal
if ($LASTEXITCODE -ne 0) { throw '个人实例配置准备失败' }
if (!(Test-Path -LiteralPath $Python)) { throw "缺少 Python 环境：$Python" }
# 与 release workflow 一致：后端和安装器共享 package.json 的版本。
$packageVersion = (Get-Content -LiteralPath 'desktop/package.json' -Encoding UTF8 | ConvertFrom-Json).version
if (!$packageVersion) { throw 'package.json 缺少版本号' }
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $repo 'cli/VERSION'), "$packageVersion`n", $utf8NoBom)
& $Python -m PyInstaller desktop/build/cowagent-backend.spec --noconfirm --distpath desktop/build/dist --workpath desktop/build/build-work
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
$backend = Join-Path $repo 'desktop\build\dist\cowagent-backend\cowagent-backend.exe'
if (!(Test-Path -LiteralPath $backend)) { throw '后端产物缺失' }
$bundledVersion = Join-Path $repo 'desktop/build/dist/cowagent-backend/_internal/cli/VERSION'
if (!(Test-Path -LiteralPath $bundledVersion)) { throw '后端版本文件缺失' }
if ((Get-Content -LiteralPath $bundledVersion -Encoding UTF8 -Raw).Trim() -ne $packageVersion) {
    throw '后端版本与安装器版本不一致'
}
if ($BackendOnly) { exit 0 }
Set-Location (Join-Path $repo 'desktop')
& npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw 'Electron 编译失败' }
& node node_modules/electron-builder/cli.js --win --x64 --publish never
if ($LASTEXITCODE -ne 0) { throw 'NSIS 构建失败' }
