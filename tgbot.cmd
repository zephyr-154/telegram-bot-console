@echo off
setlocal
chcp 65001 >nul
if /i "%~1"=="--install" goto source
rem Prefer the installed CLI so this file does not shadow it in a checkout.
set "TGBOT_TOOL="
for /f "delims=" %%I in ('where.exe tgbot.exe 2^>nul') do if not defined TGBOT_TOOL set "TGBOT_TOOL=%%I"
if defined TGBOT_TOOL goto installed
:source
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Python environment missing. Run: py -3.12 -m venv .venv
    echo Then run: .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)
pushd "%~dp0" || exit /b 1
"%~dp0.venv\Scripts\python.exe" -X utf8 -m app.cli %*
set "TGBOT_EXIT=%errorlevel%"
popd
exit /b %TGBOT_EXIT%
:installed
"%TGBOT_TOOL%" %*
exit /b %errorlevel%
