@echo off
REM One-click WeChat bot test: starts monitor (bot on 8080) + public tunnel
cd /d "%~dp0"

echo [1/3] Starting public tunnel...
start "tunnel" /min cmd /c "tools\cloudflared.exe tunnel --url http://localhost:8080 > data\tunnel.log 2>&1"
timeout /t 12 /nobreak >nul

set URL=
for /f "tokens=*" %%u in ('findstr /c:"trycloudflare.com" data\tunnel.log') do set URL=%%u

echo [2/3] Starting monitor + WeChat bot (port 8080)...
start /min cmd /c "python monitor.py > data\run.out 2>&1"
timeout /t 4 /nobreak >nul

echo ============================================================
echo  Public callback URL (paste into WeCom console):
echo     %URL%
echo  If empty, open data\tunnel.log and copy the trycloudflare.com URL.
echo  Token / EncodingAESKey: use the values in config.json (wecom_bot)
echo ============================================================
echo  Monitor is running in background. Check data\logs\monitor.log
pause
