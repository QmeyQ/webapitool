while ($true) {
    $conn = Get-NetTCPConnection -LocalPort 8199 -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) {
        Set-Location c:\Users\mey\Desktop\universalweb-api-main
        & .\venv\Scripts\python.exe main.py >> server.log 2>&1
    }
    Start-Sleep 3
}
