@echo off
REM Manually import a login cookie (fallback when browser login is blocked)
setlocal
set /p LABEL=Account label (the label written in config.json):
set /p CK=Paste the Cookie string (from browser DevTools, see README):
python "%~dp0monitor.py" --import-cookie "%LABEL%" "%CK%"
pause
