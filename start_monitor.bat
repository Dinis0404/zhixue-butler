@echo off
REM Start the always-running homework monitor (keeps a console window open)
title Zhixue Homework Monitor
python "%~dp0monitor.py"
echo.
echo Monitor stopped.
pause
