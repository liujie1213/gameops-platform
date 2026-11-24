@echo off
setlocal
chcp 65001 >nul
title GameOps - local operations console
call "%~dp0run-python.cmd" "%~dp0service.py" --open %*
if errorlevel 1 pause
