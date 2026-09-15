@echo off
setlocal
cd /d "%~dp0"
echo Personal Digital Assistant will check Python, Node.js and Git, then install the core runtime.
echo Models and licenses are configured later in the local Setup Center.
choice /M "Continue"
if errorlevel 2 exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\quickstart.ps1" --install-system-deps
endlocal
