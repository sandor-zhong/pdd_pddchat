# Default: reuse staged runtimes, refresh application sources, build lite installer.
param([string]$CompilerPath = '', [string]$Version = '', [string]$Build = '', [switch]$CodeOnly)
$ErrorActionPreference = 'Stop'
if (-not $CompilerPath) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $CompilerPath = $command.Source }
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )) {
        if (-not $CompilerPath -and (Test-Path -LiteralPath $candidate)) { $CompilerPath = $candidate }
    }
}
if (-not $CodeOnly -and (-not $CompilerPath -or -not (Test-Path -LiteralPath $CompilerPath))) {
    throw 'Inno Setup compiler not found. Supply -CompilerPath with the path to ISCC.exe.'
}
& (Join-Path $PSScriptRoot 'prune_runtime.ps1')
$guiPython = Join-Path $PSScriptRoot 'stage\runtime\gui\python.exe'
& $guiPython -B -c "from PyQt5.QtWidgets import QApplication, QFileDialog; from PyQt5.QtChart import QChart, QChartView; from PyQt5.QtSvg import QSvgRenderer; from PyQt5.QtGui import QPixmap; a=QApplication(['packaging-check', '-platform', 'offscreen']); v=QChartView(QChart()); d=QFileDialog(); p=QPixmap(32,32); assert not p.isNull(); print('Qt runtime check OK')"
if ($LASTEXITCODE -ne 0) { throw 'Qt runtime validation failed; installer build stopped.' }
& (Join-Path $PSScriptRoot 'stage_app.ps1')
$pipelinePython = Join-Path $PSScriptRoot 'stage\runtime\pipeline\python.exe'
$stampArgs = @((Join-Path $PSScriptRoot 'release_tools.py'), 'stamp')
if ($Version) { $stampArgs += @('--version', $Version) }
if ($Build) { $stampArgs += @('--build', $Build) }
& $pipelinePython -B @stampArgs
if ($LASTEXITCODE -ne 0) { throw 'Release stamping failed; installer build stopped.' }
$release = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'stage\app\app_release.json') -Raw | ConvertFrom-Json
& $pipelinePython -B -c "import native_mouse; from stealth import create_stealth_context; print('Pipeline mouse module import check OK')"
if ($LASTEXITCODE -ne 0) { throw 'Pipeline module validation failed; installer build stopped.' }
& $guiPython -B -c "from app.services.project_paths import PROJECT_PATHS; from app.dialogs.update_dialog import UpdateDialog; from app.main_window import MainWindow; print('GUI updater import check OK')"
if ($LASTEXITCODE -ne 0) { throw 'GUI updater module validation failed; installer build stopped.' }
if ($CodeOnly) {
    Write-Output ('Code-only build completed: ' + $release.version + ' / ' + $release.build)
    return
}
& $CompilerPath ("/DMyAppVersion=" + $release.version) (Join-Path $PSScriptRoot 'pddchat.iss')
if ($LASTEXITCODE -ne 0) { throw "Installer build failed: $LASTEXITCODE" }
$installer = Join-Path (Split-Path -Parent $PSScriptRoot) ("dist\pddchatSetup-" + $release.version + "-lite.exe")
& $pipelinePython -B (Join-Path $PSScriptRoot 'release_tools.py') receipt --installer $installer
if ($LASTEXITCODE -ne 0) { throw 'Installer release receipt failed.' }
