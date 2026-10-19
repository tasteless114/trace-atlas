$ErrorActionPreference = 'Stop'
$recordPath = Join-Path $PSScriptRoot 'data\server-process.json'
if (-not (Test-Path -LiteralPath $recordPath)) { Write-Output 'No running server record.'; exit }
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
$expectedScript = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'server.py'))
if ($record.script -ne $expectedScript) { throw 'Process record does not belong to this project.' }
$rideProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$record.pid)"
if ($rideProcess -and $rideProcess.CommandLine -and $rideProcess.CommandLine.Contains($expectedScript) -and $rideProcess.Name -match '^python(w)?\.exe$') {
    Stop-Process -Id ([int]$record.pid)
    Write-Output 'Trace Atlas stopped.'
} else { Write-Output 'Server is already stopped.' }
Remove-Item -LiteralPath $recordPath -ErrorAction SilentlyContinue
