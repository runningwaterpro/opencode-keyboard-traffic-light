# 安装登录自启：在用户启动文件夹创建快捷方式。无需管理员权限，可随时撤销。
# 撤销用 tools\uninstall_autostart.ps1。文件编码：UTF-8（带 BOM）。

$ErrorActionPreference = 'Stop'

$repoRoot  = Split-Path -Parent $PSScriptRoot          # tools\ 的上一级 = 仓库根
$runGuard  = Join-Path $PSScriptRoot 'run_guard.py'
$startup   = [Environment]::GetFolderPath('Startup')
$lnkPath   = Join-Path $startup 'OpenCode 键盘状态灯.lnk'

if (-not (Test-Path -LiteralPath $runGuard)) {
    throw "找不到启动器：$runGuard"
}

# python.exe 同目录下的 pythonw.exe（无控制台窗口）；找不到就退回 python.exe
$pythonExe  = (Get-Command python -ErrorAction Stop).Source
$pythonwExe = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonwExe) {
    $targetExe = $pythonwExe
} else {
    $targetExe = $pythonExe
    Write-Host "提示：没找到 pythonw.exe，退回 python.exe（登录后会出现一个控制台窗口）。"
}

if (Test-Path -LiteralPath $lnkPath) {
    Write-Host "已存在同名快捷方式，覆盖：$lnkPath"
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath       = $targetExe
$lnk.Arguments        = '"' + $runGuard + '"'
$lnk.WorkingDirectory = $repoRoot
$lnk.WindowStyle      = 7
$lnk.Description      = 'OpenCode 键盘状态灯守护（登录自启，最小化）'
$lnk.Save()

if (-not (Test-Path -LiteralPath $lnkPath)) {
    throw "快捷方式写入失败：$lnkPath"
}

Write-Host ''
Write-Host '已安装登录自启：'
Write-Host "  快捷方式：$lnkPath"
Write-Host "  目标：    $targetExe"
Write-Host "  参数：    `"$runGuard`""
Write-Host "  工作目录：$repoRoot"
Write-Host ''
Write-Host '下次登录自动启动。现在想立刻启动，双击上面的快捷方式即可。'
Write-Host '撤销：powershell -ExecutionPolicy Bypass -File tools\uninstall_autostart.ps1'
