# Build the Windows release: dist\GoveeWin11\ (one-dir, windowed) and
# dist\GoveeWin11-v<version>-win64.zip.
#   powershell -ExecutionPolicy Bypass -File build.ps1
# Needs: pip install -r requirements-dev.txt
# The exe is only ever started with --selftest here (no window, no Bluetooth).
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$init = Get-Content -Raw -Encoding UTF8 "govee_win11\__init__.py"
if ($init -notmatch '__version__\s*=\s*"([^"]+)"') { throw "no __version__ in govee_win11\__init__.py" }
$version = $Matches[1]
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "== clean"
foreach ($d in @("build", "dist")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}

Write-Host "== PyInstaller (GoveeWin11 $version)"
& $python -m PyInstaller --noconfirm --clean GoveeWin11.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$app = "dist\GoveeWin11"
$exe = Join-Path $app "GoveeWin11.exe"
$report = Join-Path $app "selftest.txt"

Write-Host "== selftest"
$p = Start-Process -FilePath $exe -ArgumentList "--selftest" -Wait -PassThru
$text = if (Test-Path $report) { Get-Content -Raw -Encoding UTF8 $report } else { "(no selftest.txt)" }
Write-Host $text
if ($p.ExitCode -ne 0 -or -not $text.StartsWith("selftest ok $version")) {
    throw "selftest failed (exit code $($p.ExitCode))"
}
Remove-Item -Force $report  # not shipped

Write-Host "== zip"
$zip = "dist\GoveeWin11-v$version-win64.zip"
Compress-Archive -Path $app -DestinationPath $zip -CompressionLevel Optimal
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "built $zip ($mb MB)"
