# 在桌面创建启动快捷方式：双击 = 无控制台启动守护（托盘常驻）。
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

$pythonExe  = (Get-Command python -ErrorAction Stop).Source
$pythonwExe = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonwExe) {
    $targetExe = $pythonwExe
} else {
    $targetExe = $pythonExe
    Write-Host "提示：没找到 pythonw.exe，退回 python.exe（会带一个控制台窗口）。"
}

if (Test-Path -LiteralPath $lnkPath) {
    Write-Host "已存在同名快捷方式，覆盖：$lnkPath"
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath       = $targetExe
$lnk.Arguments        = '"' + $runGuard + '"'
$lnk.WorkingDirectory = $repoRoot
$lnk.IconLocation     = $pythonExe + ',0'
$lnk.WindowStyle      = 7
$lnk.Description      = 'OpenCode 键盘状态灯守护（托盘常驻）'
$lnk.Save()

if (-not (Test-Path -LiteralPath $lnkPath)) {
    throw "快捷方式写入失败：$lnkPath"
}

Write-Host ''
Write-Host '已创建桌面快捷方式：'
Write-Host "  位置：   $lnkPath"
Write-Host "  目标：   $targetExe"
Write-Host "  参数：   `"$runGuard`""
Write-Host "  工作目录：$repoRoot"
Write-Host ''
Write-Host '双击即可启动；退出走托盘右键菜单。'