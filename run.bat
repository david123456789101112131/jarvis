@echo off
rem Starts JARVIS from this folder (run setup.bat once first). Extra options pass through, e.g. run.bat --no-voice
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo JARVIS is not set up yet: run setup.bat first.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -m jarvis %*
