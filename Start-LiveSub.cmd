@echo off
setlocal

rem Portable LiveSub entry point. It starts the launcher beside this file without arguments;
rem the launcher discovers config.toml and .venv in this directory by itself.
for %%I in ("%~dp0.") do set "ROOT=%%~fI"
set "RUN=%ROOT%\LiveSub.Launcher.exe"
set "MASTER=%ROOT%\launcher\dist\LiveSub.Launcher.exe"

call :size "%RUN%" HAVE
if not "%HAVE%"=="" if not "%HAVE%"=="0" goto :launch

call :size "%MASTER%" MASTER_SIZE
if "%MASTER_SIZE%"=="" goto :missing
if "%MASTER_SIZE%"=="0" goto :missing
copy /y "%MASTER%" "%RUN%" >nul || goto :missing

:launch
start "" "%RUN%"
endlocal
exit /b 0

:missing
echo ERROR: LiveSub.Launcher.exe is missing or empty.
echo Build it with launcher\publish.ps1 or download it from GitHub Releases.
pause
exit /b 1

:size
set "%~2="
if exist "%~1" for %%A in ("%~1") do set "%~2=%%~zA"
exit /b 0
