param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
Set-Location -LiteralPath $appRoot
$venvPython = Join-Path $appRoot '.venv\Scripts\python.exe'
$serverScript = Join-Path $appRoot 'server.py'
$expectedBuild = (@('server.py', 'engine.py', 'renderer.py', 'blizzard_provider.py', 'local_library.py', 'card_library.py') | ForEach-Object {
    (Get-FileHash -LiteralPath (Join-Path $appRoot $_) -Algorithm SHA256).Hash.ToLowerInvariant()
}) -join ':'

function Get-VerifiedServerOwner {
    param([int]$ListenerId)
    $listener = Get-CimInstance Win32_Process -Filter "ProcessId = $ListenerId" -ErrorAction SilentlyContinue
    if (-not $listener -or $listener.CommandLine -notmatch '(?i)(?:^|[\s\\/"])server\.py(?:$|[\s"])') {
        return $null
    }
    $ancestor = $listener
    $visited = @{}
    for ($depth = 0; $depth -lt 8; $depth++) {
        if (-not $ancestor -or $visited.ContainsKey([int]$ancestor.ProcessId)) { break }
        $visited[[int]$ancestor.ProcessId] = $true
        # Windows' venv python.exe is a launcher; its base-Python child owns
        # the socket. Verify the exact app environment through the parent chain.
        if ($ancestor.ExecutablePath -eq $venvPython -and
            $ancestor.CommandLine -match '(?i)(?:^|[\s\\/"])server\.py(?:$|[\s"])') {
            return [pscustomobject]@{
                Listener = $listener
                VenvLauncher = $ancestor
            }
        }
        $ancestor = Get-CimInstance Win32_Process -Filter "ProcessId = $($ancestor.ParentProcessId)" -ErrorAction SilentlyContinue
    }
    return $null
}

function Stop-VerifiedServer {
    param($Owner)
    $listenerId = [int]$Owner.Listener.ProcessId
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerId" -ErrorAction SilentlyContinue
    # Do not act on a recycled PID after the ancestry check.
    if (-not $current -or $current.CreationDate -ne $Owner.Listener.CreationDate) { return }
    Stop-Process -Id $listenerId -ErrorAction Stop
    Wait-Process -Id $listenerId -Timeout 3 -ErrorAction SilentlyContinue
    $launcherId = [int]$Owner.VenvLauncher.ProcessId
    if ($launcherId -ne $listenerId) {
        Wait-Process -Id $launcherId -Timeout 3 -ErrorAction SilentlyContinue
        $launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $launcherId" -ErrorAction SilentlyContinue
        if ($launcher -and $launcher.CreationDate -eq $Owner.VenvLauncher.CreationDate -and $launcher.ExecutablePath -eq $venvPython) {
            Stop-Process -Id $launcherId -ErrorAction Stop
        }
    }
}

function Get-LoopbackListenerIds {
    param([int]$Port)
    # Windows CIM uses the address/port tuple as its row key, so duplicate
    # SO_REUSEADDR listeners can be collapsed to one misleading owner. Native
    # netstat reports every socket and lets us retire every verified old copy.
    $listenerIds = foreach ($line in (& netstat.exe -ano -p tcp)) {
        $columns = $line.Trim() -split '\s+'
        if ($columns.Count -ge 5 -and $columns[0] -eq 'TCP' -and
            $columns[1] -eq "127.0.0.1:$Port" -and $columns[3] -eq 'LISTENING' -and
            $columns[4] -match '^\d+$') {
            [int]$columns[4]
        }
    }
    return @($listenerIds | Select-Object -Unique)
}

# Inspect the saved address plus the app's fallback ports. A diagnostic run or
# an interrupted launch may have overwritten the saved address while an older
# instance remains on the default port.
$urlsToInspect = @()
if (Test-Path -LiteralPath (Join-Path $appRoot 'server-url.txt')) {
    $savedUrl = (Get-Content -LiteralPath (Join-Path $appRoot 'server-url.txt') -Raw).Trim()
    if ($savedUrl -match '^http://127\.0\.0\.1:87[0-9]{2}$') { $urlsToInspect += $savedUrl }
}
$urlsToInspect += @(8787..8796 | ForEach-Object { "http://127.0.0.1:$_" })
foreach ($instanceUrl in ($urlsToInspect | Select-Object -Unique)) {
    $instancePort = ([Uri]$instanceUrl).Port
    $instancePids = @(Get-LoopbackListenerIds -Port $instancePort)
    $owners = @($instancePids | ForEach-Object { Get-VerifiedServerOwner -ListenerId $_ } | Where-Object { $_ })
    if ($owners.Count -gt 0) {
        try {
            $health = Invoke-RestMethod -Uri "$instanceUrl/api/health" -TimeoutSec 2
            if ($health.app -eq 'deckview-local') {
                if (($health.build -eq $expectedBuild -and $instancePids.Count -eq 1) -or $health.activeJobs -gt 0) {
                    [IO.File]::WriteAllText((Join-Path $appRoot 'server-url.txt'), $instanceUrl, [Text.UTF8Encoding]::new($false))
                    if (-not $NoBrowser) { Start-Process $instanceUrl }
                    exit 0
                }
                # An updated renderer needs a fresh process, not a stale import.
                # Stop only this app's interpreter bound to its verified port.
                foreach ($owner in $owners) { Stop-VerifiedServer -Owner $owner }
            }
        } catch { }
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host 'Setting up Deck View (first launch only)...'
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -m venv (Join-Path $appRoot '.venv')
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw 'Install Python 3.12 or newer, then launch Start.cmd again.' }
        & $pythonCommand.Source -m venv (Join-Path $appRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}

& $venvPython -c "import importlib.util as u, sys; sys.exit(not all(u.find_spec(name) for name in ('PIL', 'hearthstone', 'requests', 'certifi')))"
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m pip install -r (Join-Path $appRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not download dependencies. Check your internet and launch Start.cmd again.' }
}

# A separate import-cache location for each source build also avoids Python's
# timestamp/size bytecode-cache ambiguity when edits occur within one second.
$buildHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $buildToken = ([BitConverter]::ToString($buildHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($expectedBuild)))).Replace('-', '').ToLowerInvariant()
} finally { $buildHasher.Dispose() }
$importCache = Join-Path $appRoot ("cache\python-" + $buildToken)
$serverArguments = '-B -u -X "pycache_prefix=' + $importCache + '" "' + $serverScript + '" --no-browser'
$started = Start-Process -FilePath $venvPython -ArgumentList $serverArguments -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $appRoot 'server.log') -RedirectStandardError (Join-Path $appRoot 'server-error.log') -PassThru

# Open only after confirming that this launch serves the current source build.
$readyUrl = $null
$deadline = [DateTime]::UtcNow.AddSeconds(20)
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-Path -LiteralPath (Join-Path $appRoot 'server-url.txt')) {
        $candidateUrl = (Get-Content -LiteralPath (Join-Path $appRoot 'server-url.txt') -Raw).Trim()
        if ($candidateUrl -match '^http://127\.0\.0\.1:87[0-9]{2}$') {
            try {
                $candidateHealth = Invoke-RestMethod -Uri "$candidateUrl/api/health" -TimeoutSec 2
                if ($candidateHealth.app -eq 'deckview-local' -and $candidateHealth.build -eq $expectedBuild) {
                    $candidateOwner = Get-VerifiedServerOwner -ListenerId ([int]$candidateHealth.pid)
                    if ($candidateOwner -and [int]$candidateOwner.VenvLauncher.ProcessId -eq $started.Id) {
                        $readyUrl = $candidateUrl
                        break
                    }
                }
            } catch { }
        }
    }
    $started.Refresh()
    if ($started.HasExited) { break }
    Start-Sleep -Milliseconds 150
}
if (-not $readyUrl) {
    throw 'Deck View could not load the current version. Check server-error.log and launch Start.cmd again.'
}
if (-not $NoBrowser) { Start-Process $readyUrl }
Write-Host "Deck View: $readyUrl"
