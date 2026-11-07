@echo off
REM Double-click to check this gateway's install without a radio.
"%~dp0SARA-FM.exe" --selftest
echo.
echo (Result also saved to selftest_result.txt in this folder.)
pause
