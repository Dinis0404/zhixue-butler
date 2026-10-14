@echo off
REM Remove the autostart scheduled task
schtasks /Delete /TN "ZhixueHomeworkMonitor" /F
pause
