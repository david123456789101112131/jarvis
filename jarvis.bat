@echo off
rem The jarvis command for this folder: jarvis check --full, jarvis say "...", jarvis stop, jarvis keys ...
if not exist "%~dp0.venv\Scripts\jarvis.exe" (
    echo JARVIS is not set up yet: run setup.bat first.
    exit /b 1
)
"%~dp0.venv\Scripts\jarvis.exe" %*
