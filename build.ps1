[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$SkipInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
if ([string]::IsNullOrWhiteSpace($Python)) {
    $localPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $localPython) { $localPython } else { "python" }
}

Push-Location $projectRoot
try {
    & $Python -c "import sys; assert sys.platform == 'win32', 'Build on Windows'; assert sys.version_info[:2] == (3, 12), 'Use Python 3.12'; assert sys.maxsize > 2**32, 'Use 64-bit Python'"
    if ($LASTEXITCODE -ne 0) { throw "A 64-bit Python 3.12 installation is required." }
    if (-not $SkipInstall) {
        & $Python -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) { throw "Installing build dependencies failed." }
    }
    if (-not $SkipTests) {
        & $Python -m unittest discover -p "test_*.py" -q
        if ($LASTEXITCODE -ne 0) { throw "Tests failed; no release was packaged." }
    }

    & $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath build deckviewer.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
    $bundleRoot = Join-Path $projectRoot "dist\deckviewer"
    $executable = Join-Path $bundleRoot "DeckViewer.exe"
    if (-not (Test-Path -LiteralPath $executable)) { throw "The executable was not created." }

    # The frozen app has no console; a JSON file confirms startup, resources,
    # loopback requests and orderly shutdown using its bundled interpreter.
    $smokeData = Join-Path $projectRoot "build\smoke-data"
    $smokeOutput = Join-Path $projectRoot "build\smoke-result.json"
    if (Test-Path -LiteralPath $smokeOutput) { Remove-Item -LiteralPath $smokeOutput }
    $smokeArgs = '--smoke-test --data-dir "' + $smokeData + '" --smoke-output "' + $smokeOutput + '"'
    $process = Start-Process -FilePath $executable -ArgumentList $smokeArgs -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $smokeOutput)) {
        throw "The packaged executable failed its startup check. See build\smoke-data\logs."
    }
    $smoke = Get-Content -LiteralPath $smokeOutput -Raw | ConvertFrom-Json
    if (-not $smoke.ok) { throw "The packaged executable reported a failed startup check." }

    $readme = @'
DeckViewer for Windows 10/11 x64

Extract the entire ZIP to a folder, then run DeckViewer.exe.
Python is included. Keep the _internal folder beside the executable.
Microsoft Edge WebView2 Runtime must be installed (normally included with Windows).
https://developer.microsoft.com/microsoft-edge/webview2/

Paste a Hearthstone deck code and create a PNG. Russian and English are supported.
No Battle.net API keys are needed. Card data updates automatically when online.
Images are downloaded as needed and cached (100 MiB limit).
App data: %LOCALAPPDATA%\deckviewer
Use the Save PNG button to select a location for your image.

Includes adapted MIT-licensed code from hextract/HearthstoneDeckView.
Hearthstone artwork and card data belong to Blizzard Entertainment.
'@
    $readme | Set-Content -LiteralPath (Join-Path $bundleRoot "README.txt") -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $bundleRoot -Force
    $archive = Join-Path $projectRoot "dist\deckviewer-windows-x64.zip"
    Compress-Archive -LiteralPath $bundleRoot -DestinationPath $archive -CompressionLevel Optimal -Force
    $digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$digest  deckviewer-windows-x64.zip" | Set-Content -LiteralPath ($archive + ".sha256") -Encoding ascii
    $sizeMiB = [Math]::Round((Get-Item -LiteralPath $archive).Length / 1MB, 2)
    Write-Host "Built $archive ($sizeMiB MiB)"
}
finally {
    Pop-Location
}
