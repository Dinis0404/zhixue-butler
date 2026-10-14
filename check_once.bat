@echo off
REM Check all accounts once, then exit
python "%~dp0monitor.py" --once
pause
