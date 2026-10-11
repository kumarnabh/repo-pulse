$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw 'The .NET 8 SDK is required to build. Install it from https://dotnet.microsoft.com/download/dotnet/8.0 and run Build again.'
}
$python = $null
foreach ($c in @('py', 'python')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') { $python = $c; break }
}
if (-not $python) { throw 'Install Python 3.10+ from python.org (include the Python launcher), then run Build again.' }

$out = Join-Path $root 'build\release'
dotnet build (Join-Path $root 'source\RepoPulse.csproj') -c Release -o $out --nologo
if ($LASTEXITCODE -ne 0) { throw 'Build failed. Copy the errors above into the chat.' }
Get-ChildItem -LiteralPath $out -Filter 'RepoPulse*' -File | Copy-Item -Destination $root -Force

Write-Host ''
Write-Host 'Running core self-test (read-only)...'
if ($python -eq 'py') { & py -3 (Join-Path $root 'repopulse.py') --version } else { & python (Join-Path $root 'repopulse.py') --version }
if ($LASTEXITCODE -ne 0) { throw 'Python core did not start.' }
Write-Host 'Built RepoPulse.exe. Start it with "Launch Repo Pulse.cmd".'
