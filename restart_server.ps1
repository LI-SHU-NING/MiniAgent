# 重启 MiniAgent 服务，使 agent/tools 下的代码改动在真实会话生效。
# 背景：server.py 使用 uvicorn.run(app) 且未开启 reload；运行中的进程只在启动时导入一次
#       agent.tools.browser.*，此后磁盘上的修复不会自动加载。运行本脚本即可重载最新代码。
#
# 用法（在 Windows PowerShell 中，位于项目根目录）：
#     powershell -ExecutionPolicy Bypass -File .\restart_server.ps1
#
# 注意：若服务正被当前会话使用，重启会短暂中断该会话；请在会话间歇执行。

$ErrorActionPreference = "Stop"
$root = (Get-Location).Path
$py   = (Get-Command python).Source
$port = 8799

Write-Host "[1/3] 查找监听 $port 的旧进程 ..."
$conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
if ($conn) {
    $old = $conn.OwningProcess
    Write-Host "      正在停止旧服务 PID=$old ..."
    Stop-Process -Id $old -Force
    Start-Sleep -Seconds 2
} else {
    Write-Host "      未发现监听进程，直接启动。"
}

Write-Host "[2/3] 清理可能残留的 Playwright 无头子进程 ..."
Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='node.exe'" |
    Where-Object { $_.ExecutablePath -like '*ms-playwright*' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }

Write-Host "[3/3] 启动新的 server.py ..."
Start-Process -FilePath $py -ArgumentList "-u", "server.py" -WorkingDirectory $root
Start-Sleep -Seconds 3
$ok = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($ok) { Write-Host "OK：服务已在 127.0.0.1:$port 重新监听（已加载最新代码）。" }
else     { Write-Host "警告：端口未监听，请检查启动输出。" }
