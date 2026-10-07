@echo off
rem Sets JARVIS up on this PC: Python 3.11+, a .venv with everything JARVIS uses, a check of the computer.
rem Options: setup.bat -Dev (developer tools)   setup.bat -BuildUI (rebuild the interface)   setup.bat -Yes
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
set RESULT=%ERRORLEVEL%
echo.
pause
exit /b %RESULT%
