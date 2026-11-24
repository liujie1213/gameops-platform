@echo off
setlocal
chcp 65001 >nul
title GameOps - practice
set "GAMEOPS_DB=%~dp0data\practice\ops.sqlite3"
call "%~dp0run-python.cmd" "%~dp0service.py" --port 60105 --local-name localhost --open %*
if errorlevel 1 pause
