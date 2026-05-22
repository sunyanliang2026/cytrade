param(
    [string[]]$Targets = @("tests"),
    [string]$PythonPath = "C:\Users\ysun\miniconda3\envs\cytrade311\python.exe",
    [string]$BaseTemp = ".tmp_pytest_run"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python executable not found: $PythonPath"
}

if (-not (Test-Path -LiteralPath $BaseTemp)) {
    New-Item -ItemType Directory -Path $BaseTemp | Out-Null
}

& $PythonPath -m pytest -q @Targets --basetemp $BaseTemp
exit $LASTEXITCODE
