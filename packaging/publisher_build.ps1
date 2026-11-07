param([Parameter(Mandatory=$true)][string]$Version, [switch]$CodeOnly)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
& (Join-Path $PSScriptRoot 'build_installer.ps1') -Version $Version -CodeOnly:$CodeOnly
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
