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
    & $buildPython -m PyInstaller --noconfirm --clean --onefile --windowed --name FunPayManager --paths src @runtimeData scripts/entrypoint.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
}
finally { $env:PATH = $originalPath }
if (-not (Test-Path -LiteralPath dist/FunPayManager.exe)) { throw 'EXE missing' }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath dist/FunPayManager.exe).Hash.ToLowerInvariant()
Set-Content -LiteralPath dist/SHA256SUMS.txt -Value "$hash  FunPayManager.exe" -Encoding ascii
