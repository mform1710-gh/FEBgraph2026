@echo off
powershell.exe -NoProfile -STA -ExecutionPolicy RemoteSigned -File "%~dp0feb_newick_launcher.ps1" %*
if errorlevel 1 pause
