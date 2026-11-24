@echo off
setlocal
if defined GAMEOPS_PYTHON if exist "%GAMEOPS_PYTHON%" goto configured
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
  if not errorlevel 1 goto pylauncher
)
where python.exe >nul 2>&1
if not errorlevel 1 (
  python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
  if not errorlevel 1 goto pythonpath
)
set "gameops_bundled=%ProgramFiles%\WindowsApps\OpenAI.CodexPrimaryRuntime.v26-1118-904-0_26.1118.904.0_x64__3k8sg7r9htsxt\dependencies\python\python.exe"
if exist "%gameops_bundled%" goto bundled
for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -Command "Get-AppxPackage -Name 'OpenAI.CodexPrimaryRuntime*' ^| Sort-Object Version -Descending ^| ForEach-Object { Join-Path $_.InstallLocation 'dependencies\python\python.exe' } ^| Where-Object { Test-Path -LiteralPath $_ } ^| Select-Object -First 1"`) do set "gameops_bundled=%%P"
if exist "%gameops_bundled%" goto bundled
echo Python 3.10+ is required. Install Python or set GAMEOPS_PYTHON to python.exe.
exit /b 1
:configured
"%GAMEOPS_PYTHON%" -B -X utf8 %*
exit /b %errorlevel%
:pylauncher
py -3 -B -X utf8 %*
exit /b %errorlevel%
:pythonpath
python -B -X utf8 %*
exit /b %errorlevel%
:bundled
"%gameops_bundled%" -B -X utf8 %*
exit /b %errorlevel%
