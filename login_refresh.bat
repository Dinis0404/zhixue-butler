@echo off
REM Re-login all accounts: a browser window will pop up for each account.
REM Complete the login (including captcha) in the window, this script does the rest.
python "%~dp0monitor.py" --relogin
pause
