python -m venv venv

venv/scripts/activate

pip install -r requirements.txt


powershell -ExecutionPolicy Bypass -File .\run_local.ps1 

*** tunner host ***

cd C:\cloudflared

.\cloudflared.exe --version

.\cloudflared.exe tunnel --url http://localhost:8000