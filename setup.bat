@echo off
REM Install Python dependencies for the Zhixue homework monitor
echo === Installing Python dependencies ===
python -m pip install --upgrade zhixuewang
if errorlevel 1 goto fail
python -m playwright install chromium
if errorlevel 1 goto fail
echo.
echo === Setup finished ===
pause
exit /b 0
:fail
echo.
echo === Setup FAILED. Please check the messages above ===
pause
exit /b 1
