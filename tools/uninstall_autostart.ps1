# 撤销登录自启：删除用户启动文件夹里的快捷方式（不存在则提示）。无需管理员权限。
# 文件编码：UTF-8（带 BOM）。

$ErrorActionPreference = 'Stop'

$lnkPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'OpenCode 键盘状态灯.lnk'

if (Test-Path -LiteralPath $lnkPath) {
    Remove-Item -LiteralPath $lnkPath -Force
    Write-Host "已撤销登录自启，删除：$lnkPath"
} else {
    Write-Host "没找到快捷方式，无需撤销：$lnkPath"
}

Write-Host '注意：这里只停自启；已经在跑的守护请用任务管理器结束 pythonw.exe（或 taskkill /IM pythonw.exe /F）。'
