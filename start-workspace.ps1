$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    throw 'Create the environment first: uv sync --frozen'
}
& '.venv/Scripts/python.exe' -X utf8 -m scripts.run_workspace
exit $LASTEXITCODE
