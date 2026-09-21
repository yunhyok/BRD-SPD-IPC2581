param(
    [string]$Version = "0.1.0",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$DistDir = Join-Path $ProjectRoot "dist"
$BuildDir = Join-Path $ProjectRoot "build"
$SpecDir = Join-Path $ProjectRoot "packaging\pyinstaller"
$ThirdPartyLicenseDir = Join-Path $BuildDir "third-party-licenses"
$InnoSetupCandidates = @(
    (Join-Path ${env:ProgramFiles} "Inno Setup 7\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
)
$InnoSetup = $InnoSetupCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $InnoSetup) {
    $InnoCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($InnoCommand) { $InnoSetup = $InnoCommand.Source }
}
$SchemaData = "$(Join-Path $ProjectRoot 'schemas\IPC-2581B1.xsd');schemas"

Push-Location $ProjectRoot
try {
    python packaging\collect_licenses.py --output $ThirdPartyLicenseDir
    if ($LASTEXITCODE -ne 0) { throw "Third-party license collection failed with exit code $LASTEXITCODE." }
    python -m PyInstaller --noconfirm --clean --windowed --onefile --collect-submodules brd_spd --add-data $SchemaData --name "BRD-SPD-IPC2581" --distpath $DistDir --workpath $BuildDir --specpath $SpecDir launch_gui.py
    if ($LASTEXITCODE -ne 0) { throw "GUI PyInstaller build failed with exit code $LASTEXITCODE." }
    python -m PyInstaller --noconfirm --clean --console --onefile --collect-submodules brd_spd --add-data $SchemaData --name "brd-spd-ipc2581-cli" --distpath $DistDir --workpath $BuildDir --specpath $SpecDir launch_cli.py
    if ($LASTEXITCODE -ne 0) { throw "CLI PyInstaller build failed with exit code $LASTEXITCODE." }

    if (-not $SkipInstaller) {
        if (-not $InnoSetup) {
            throw "Inno Setup was not found. Install Inno Setup 6 or 7, or run with -SkipInstaller."
        }
        & $InnoSetup "/DMyAppVersion=$Version" (Join-Path $ProjectRoot "packaging\BRD-SPD-IPC2581.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }
    }
}
finally {
    Pop-Location
}
