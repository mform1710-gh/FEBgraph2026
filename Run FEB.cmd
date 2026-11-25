@echo off
powershell.exe -NoProfile -STA -ExecutionPolicy RemoteSigned -File "%~dp0feb_gui.ps1" -OutputDirectory "%~dp0."
if errorlevel 1 pause
