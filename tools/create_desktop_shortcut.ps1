# 在桌面创建启动快捷方式：双击 = 打开可见控制台并运行守护（Ctrl+C 停止）。
# 不设置开机自启（那是 tools\install_autostart.ps1）。重复运行会覆盖同名快捷方式。
# 文件编码：UTF-8（带 BOM）。

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$runGuard = Join-Path $PSScriptRoot 'run_guard.py'
$desktop  = [Environment]::GetFolderPath('Desktop')
$lnkPath  = Join-Path $desktop 'OpenCode 键盘状态灯.lnk'

if (-not (Test-Path -LiteralPath $runGuard)) {
    throw "找不到启动器：$runGuard"
}

$pythonExe = (Get-Command python -ErrorAction Stop).Source
$psExe     = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $psExe)) { throw "找不到 powershell.exe：$psExe" }

if (Test-Path -LiteralPath $lnkPath) {
    Write-Host "已存在同名快捷方式，覆盖：$lnkPath"
}

$inner = "Set-Location '{0}'; python '{1}'" -f $repoRoot, $runGuard

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath       = $psExe
$lnk.Arguments        = '-NoExit -ExecutionPolicy Bypass -Command "' + $inner + '"'
$lnk.WorkingDirectory = $repoRoot
$lnk.IconLocation     = $pythonExe + ',0'
$lnk.WindowStyle      = 1
$lnk.Description      = 'OpenCode 键盘状态灯守护（可见窗口，Ctrl+C 停止）'
$lnk.Save()

if (-not (Test-Path -LiteralPath $lnkPath)) {
    throw "快捷方式写入失败：$lnkPath"
}

Write-Host ''
Write-Host '已创建桌面快捷方式：'
Write-Host "  位置：   $lnkPath"
Write-Host "  目标：   $psExe"
Write-Host "  参数：   -NoExit -ExecutionPolicy Bypass -Command `"$inner`""
Write-Host "  工作目录：$repoRoot"
Write-Host ''
Write-Host '双击即可启动。'