@echo off
setlocal
call "%~dp0run-python.cmd" "%~dp0integration\simulate.py" %*
set "gameops_result=%errorlevel%"
pause
exit /b %gameops_result%
