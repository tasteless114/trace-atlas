param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$port = [int]$config.port
$url = "http://127.0.0.1:$port"
$existing = $null
try { $existing = Invoke-RestMethod "$url/api/status" -TimeoutSec 2 } catch {}
if ($existing -and $existing.app -ne 'trace-atlas') { throw 'Port is already in use. Change port in config.json.' }
if ($existing -and $existing.project_folder -and $existing.project_folder -ne $PSScriptRoot) { throw 'Another Trace Atlas folder is using this port. Stop it or change port in config.json.' }
if (-not $existing) {
    # No virtual environment and no packages: the app is standard-library only.
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $pythonArguments = @()
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
        $pythonArguments = @('-3')
    }
    if (-not $pythonCommand) { throw 'Python 3.10 or newer was not found. Install Python, tick "Add Python to PATH", then start again.' }
    $pythonCode = & $pythonCommand.Source @pythonArguments -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $pythonCode -or [int]$pythonCode -lt 310) { throw 'Python 3.10 or newer is required. Check your Python installation.' }
    $dataDir = Join-Path $PSScriptRoot 'data'
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $scriptPath = Join-Path $PSScriptRoot 'server.py'
    Start-Process -FilePath $pythonCommand.Source -ArgumentList (@($pythonArguments) + @('-u', ('"' + $scriptPath + '"'), '--import-folder')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDir 'server.log') -RedirectStandardError (Join-Path $dataDir 'server-error.log')
    $ready = $false
    # First start after a grid-resolution change rebuilds the heat grids from the stored
    # geometry, which can take a while on a large library.
    for ($i = 0; $i -lt 480; $i++) {
        Start-Sleep -Milliseconds 250
        try {
            $state = Invoke-RestMethod "$url/api/status" -TimeoutSec 1
            if ($state.app -eq 'trace-atlas') { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) { throw 'Server did not start. See data/server-error.log.' }
}
if (-not $NoBrowser) { Start-Process $url }
Write-Output "Trace Atlas is running at $url"
