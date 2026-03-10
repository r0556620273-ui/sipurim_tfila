# run_daily.ps1 — הרצה מקומית יומית + דחיפה לGitHub
# להגדרה ב-Task Scheduler: powershell -ExecutionPolicy Bypass -File "d:\sipurim_tfila\run_daily.ps1"

Set-Location "d:\sipurim_tfila"

# הפעלת venv אם קיים
if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

# הרצת הסקריפט
Write-Host "מריץ את הסקריפט..." -ForegroundColor Cyan
python tefilah_newsletter.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "הסקריפט נכשל — לא מבצע push" -ForegroundColor Red
    exit 1
}

# git push
$date = Get-Date -Format "dd-MM-yyyy"
git add tefilah_*.pdf

$staged = git diff --staged --name-only
if ($staged) {
    git commit -m "tefilah $date"
    git push
    Write-Host "PDF נדחף בהצלחה לGitHub" -ForegroundColor Green
} else {
    Write-Host "אין PDF חדש — דילוג על commit" -ForegroundColor Yellow
}
