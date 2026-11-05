param([switch]$KillOnly)
if (-not $KillOnly) { & "C:\Users\mey\Desktop\web-api+tool\nssm.exe" stop OpenClawProxy | Out-Null }
Start-Sleep -Seconds 3
$targets = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  Where-Object { $_.CommandLine -notmatch '--type=' -and
                 $_.CommandLine -like '*web-api+tool\chrome_profiles*' }
foreach ($t in $targets) {
  Write-Output ("kill pid=" + $t.ProcessId)
  Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
if (-not $KillOnly) { & "C:\Users\mey\Desktop\web-api+tool\nssm.exe" start OpenClawProxy | Out-Null }
