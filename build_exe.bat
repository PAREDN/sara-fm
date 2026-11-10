@echo off
REM ======================================================================
REM  build_exe.bat -- build the standalone SARA FM for Windows gateways.
REM
REM  Run ONCE per release on any Windows PC that has Python 3.12 installed
REM  (e.g. DW1IRF or K6IRF). It produces dist\SARA-FM\ and a ready-to-ship
REM  zip. Gateways that receive the zip need NO Python at all.
REM
REM  Usage:   build_exe.bat          (runs the 129-check self-test first)
REM           build_exe.bat notest   (skip the test suite, faster)
REM ======================================================================
setlocal
cd /d "%~dp0"

set PY=
python --version >nul 2>nul && set PY=python
if not defined PY ( py --version >nul 2>nul && set PY=py )
if not defined PY (
  echo ERROR: Python not found. Install Python 3.12 from python.org on THIS build PC.
  exit /b 1
)
echo Using: %PY%
%PY% --version

echo.
echo === Installing build requirements ===
%PY% -m pip install --upgrade pip || goto :fail
%PY% -m pip install -r requirements.txt pyinstaller || goto :fail

if /i not "%1"=="notest" (
  echo.
  echo === Running the offline test suite ^(a few minutes^) ===
  %PY% test_offline.py || ( echo TESTS FAILED -- not building. & goto :fail )
)

for /f "delims=" %%v in ('%PY% -c "import ofdm_core; print(ofdm_core.VERSION)"') do set VER=%%v

echo.
echo === Building %VER% ===
if exist build rmdir /s /q build
if exist dist\SARA-FM rmdir /s /q dist\SARA-FM
%PY% -m PyInstaller --noconfirm --clean --onedir --console --name SARA-FM ^
  --hidden-import hid --hidden-import serial --hidden-import serial.tools.list_ports --hidden-import fec_cc --hidden-import session_stats --hidden-import gauges --hidden-import chat --hidden-import chat_window --hidden-import ax25_term --hidden-import term_window --hidden-import update_check ^
  --exclude-module test_offline --exclude-module reedsolo ^
  sara_pingpong.py || goto :fail

copy /y README.md dist\SARA-FM\ >nul
copy /y HISTORY.md dist\SARA-FM\ >nul
copy /y LICENSE dist\SARA-FM\LICENSE.txt >nul
copy /y BPQ32.cfg.example dist\SARA-FM\ >nul
copy /y QUICKSTART_GATEWAY.txt dist\SARA-FM\ >nul
copy /y "SARA-FM Selftest.bat" dist\SARA-FM\ >nul

echo.
echo === Self-test of the BUILT program ===
dist\SARA-FM\SARA-FM.exe --selftest
REM (A sound-device FAIL here only means this build PC has no audio interface.)

set ZIP=%VER%-win64.zip
if exist "%ZIP%" del "%ZIP%"
REM v0.8.2: never ship THIS PC's identity or history to other stations --
REM settings carry "My callsign" (a copied settings file once gave two test
REM stations the same callsign, and pings went unanswered).
for %%f in (sara_chat.json sara_chat.json.tmp sara_pp_settings.json sara_pp_settings.json.tmp sara_pp_speed.json sara_pp_speed.json.tmp sara_pingpong_log.csv selftest_result.txt sara_pp_crash.log) do (
  if exist "dist\SARA-FM\%%f" del /q "dist\SARA-FM\%%f"
)
if exist "dist\SARA-FM\logs" rmdir /s /q "dist\SARA-FM\logs"
if exist "dist\SARA-FM\chat_files" rmdir /s /q "dist\SARA-FM\chat_files"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\SARA-FM' -DestinationPath '%ZIP%'" || goto :fail

echo.
echo ======================================================================
echo  DONE.  Ship this to gateways:  %CD%\%ZIP%
echo ======================================================================
exit /b 0

:fail
echo.
echo BUILD FAILED.
exit /b 1
