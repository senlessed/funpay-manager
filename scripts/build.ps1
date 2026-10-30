param([string]$OutputDirectory = 'dist')

$ErrorActionPreference = 'Stop'
$buildPython = (Get-Command python -ErrorAction Stop).Source
$originalPath = $env:PATH
try {
    # Prevent unrelated SDKs (e.g. Poppler/Conda ICU) from shadowing Windows DLLs.
    $env:PATH = "$(Split-Path $buildPython);$env:SystemRoot\System32;$env:SystemRoot"
    $runtimeData = @()
    # Some managed Python runtimes ship an additional SSL trust-policy resource.
    # Preserve it when freezing; never disable certificate verification.
    $sslPolicy = & $buildPython -c "import ssl; from pathlib import Path; print(Path(ssl.__file__).with_name('codex-rejected-cas.pem'))"
    if (Test-Path -LiteralPath $sslPolicy) { $runtimeData = @('--add-data', "$sslPolicy;.") }
    & $buildPython -m PyInstaller --noconfirm --clean --onefile --windowed --name FunPayManager --distpath $OutputDirectory --paths src @runtimeData scripts/entrypoint.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
}
finally { $env:PATH = $originalPath }
$executable = Join-Path $OutputDirectory 'FunPayManager.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'EXE missing' }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $executable).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt') -Value "$hash  FunPayManager.exe" -Encoding ascii
