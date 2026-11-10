#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$SetupOnly,
    [switch]$CheckOnly,
    [string]$DataDir
)

$ErrorActionPreference = 'Stop'
$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExe = Join-Path $repo '.venv\Scripts\python.exe'
$desktop = Join-Path $repo 'desktop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令失败（退出码 $LASTEXITCODE）：$Program" }
}

function Test-Python311 {
    param([string]$Program, [string[]]$Prefix = @())
    & $Program @Prefix -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,11) else 1)' 2>$null
    return $LASTEXITCODE -eq 0
}

try {
    if ($SetupOnly -and $CheckOnly) { throw '-SetupOnly 与 -CheckOnly 不能同时使用' }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (!$nodeCommand -or !$npmCommand) { throw '请先安装 Node.js 18 或更高版本（包含 npm），然后重新运行。' }
    $nodeExe = $nodeCommand.Source
    Invoke-Checked $nodeExe @('-e', 'if(parseInt(process.versions.node)<18)process.exit(1)')

    if (!(Test-Path -LiteralPath $pythonExe)) {
        if ($CheckOnly) { throw '缺少仓库 .venv；运行 start-personal.cmd 自动准备环境。' }
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($launcher -and (Test-Python311 $launcher.Source @('-3.11'))) {
            Invoke-Checked $launcher.Source @('-3.11', '-m', 'venv', (Join-Path $repo '.venv'))
        } elseif ($systemPython -and (Test-Python311 $systemPython.Source)) {
            Invoke-Checked $systemPython.Source @('-m', 'venv', (Join-Path $repo '.venv'))
        } else { throw '请先安装 Python 3.11；本项目优先使用 py -3.11 创建独立 .venv。' }
    }
    if (!(Test-Python311 $pythonExe)) { throw '仓库 .venv 需要使用已验证的 Python 3.11；请调整环境后重试。' }

    $probe = "import importlib.util,sys; names=['web','requests','aiohttp','numpy','markdown_it','regex','lark_oapi','pypdf','docx','openpyxl','zai','dashscope','tiktoken','playwright']; missing=[n for n in names if importlib.util.find_spec(n) is None]; print(', '.join(missing)); sys.exit(bool(missing))"
    $missing = & $pythonExe -c $probe
    $pythonReady = $LASTEXITCODE -eq 0
    $nodeReady = (Test-Path -LiteralPath (Join-Path $desktop 'node_modules\electron\dist\electron.exe')) -and
                 (Test-Path -LiteralPath (Join-Path $desktop 'node_modules\vite\bin\vite.js')) -and
                 (Test-Path -LiteralPath (Join-Path $desktop 'node_modules\typescript\bin\tsc'))
    if ($CheckOnly -and (!$pythonReady -or !$nodeReady)) {
        throw "依赖未就绪（Python 缺少：$missing；桌面依赖就绪：$nodeReady），请先运行 start-personal.cmd。"
    }
    if (!$CheckOnly -and (!$pythonReady -or $SetupOnly)) {
        Write-Host '正在准备 Python 依赖…'
        Invoke-Checked $pythonExe @('-m', 'pip', 'install', '-r', (Join-Path $repo 'requirements.txt'), '-r', (Join-Path $desktop 'build\requirements-desktop.txt'))
        Invoke-Checked $pythonExe @('-c', $probe)
    }
    if (!$CheckOnly -and (!$nodeReady -or $SetupOnly)) {
        Push-Location $desktop
        try { Invoke-Checked $npmCommand.Source @('ci') }
        finally { Pop-Location }
    }

    if (!$DataDir) { $DataDir = $env:COW_PERSONAL_DATA_DIR }
    if (!$DataDir) { $DataDir = Join-Path $env:USERPROFILE '.cow-personal' }
    if (![System.IO.Path]::IsPathRooted($DataDir)) { $DataDir = Join-Path $repo $DataDir }
    $env:COW_PERSONAL_DATA_DIR = [System.IO.Path]::GetFullPath($DataDir)
    Write-Host "源码：$repo"
    Write-Host "数据：$env:COW_PERSONAL_DATA_DIR"
    Write-Host "日志：$(Join-Path $env:COW_PERSONAL_DATA_DIR 'run.log')"
    if ($CheckOnly -or $SetupOnly) { Write-Host '环境检查完成。'; exit 0 }

    Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    Invoke-Checked $nodeExe @((Join-Path $desktop 'scripts\apply-flavor.mjs'), 'personal')
    Push-Location $desktop
    try {
        $buildTask = if ($Dev) { 'build:main' } else { 'build' }
        Invoke-Checked $npmCommand.Source @('run', $buildTask)
        Write-Host '正在启动本地桌面与 Python 源码后端；按 Ctrl+C 停止。'
        $launchArguments = @((Join-Path $desktop 'scripts\start-local.mjs'))
        if ($Dev) { $launchArguments += '--dev' }
        Invoke-Checked $nodeExe $launchArguments
    } finally { Pop-Location }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
