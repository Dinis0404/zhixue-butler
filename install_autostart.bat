@echo off
REM Install a scheduled task: monitor starts automatically (hidden) at every logon
for /f "delims=" %%i in ('python -c "import sys; print(sys.executable)"') do set PYEXE=%%i
call set PYW=%%PYEXE:python.exe=pythonw.exe%%
schtasks /Create /F /TN "ZhixueHomeworkMonitor" /TR "\"%PYW%\" \"%~dp0monitor.py\"" /SC ONLOGON
if errorlevel 1 (
  echo === Failed to create scheduled task ===
) else (
  echo === Autostart installed. The monitor will run hidden at every logon. ===
  echo    To start it now, run: schtasks /Run /TN ZhixueHomeworkMonitor
)
pause
