# Talkio launcher — Windows. Run from the project root: .\run_local.ps1
Set-Location $PSScriptRoot

if (-not (Test-Path ".\venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv venv
}

.\venv\Scripts\Activate.ps1
pip install -q -r requirements.txt

if (-not (Test-Path ".\.env")) {
    Write-Host "No .env found -- copying .env.example. Edit it if you need real Google/Anthropic keys."
    Copy-Item ".env.example" ".env"
}

# Load .env into this process so main.py/auth.py/bot.py can read them via os.environ
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim())
    }
}

Set-Location backend
Write-Host "Starting Talkio on 0.0.0.0:8000 (reachable from other devices on this WiFi)..."
uvicorn main:app --host 0.0.0.0 --port 8000
