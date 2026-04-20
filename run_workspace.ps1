param(
    [switch]$BuildUi
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$apiEntry = Join-Path $repoRoot ".venv\Scripts\povgen-api.exe"
$uiRoot = Join-Path $repoRoot "ui\workspace"
$uiDist = Join-Path $uiRoot "dist"

if (-not (Test-Path $python)) {
    throw "Не найден интерпретатор .venv. Сначала создайте окружение и установите зависимости."
}

if (-not (Test-Path $apiEntry)) {
    throw "Не найден povgen-api. Выполните '.\.venv\Scripts\python -m pip install -e .[dev]'."
}

if ($BuildUi -or -not (Test-Path $uiDist)) {
    Write-Host "Сборка UI..." -ForegroundColor Cyan
    Push-Location $uiRoot
    try {
        npm install
        npm run build
    }
    finally {
        Pop-Location
    }
}

Write-Host "Запуск PoV Generator Workspace..." -ForegroundColor Green
Write-Host "UI:    http://127.0.0.1:8788/" -ForegroundColor Gray
Write-Host "Docs:  http://127.0.0.1:8788/docs" -ForegroundColor Gray
Write-Host "Health:http://127.0.0.1:8788/api/health" -ForegroundColor Gray

& $apiEntry
