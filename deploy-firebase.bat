@echo off
setlocal
cd /d "%~dp0"
if not exist "firebase.json" (
  echo Missing firebase.json in this folder.
  pause
  exit /b 1
)
if not exist "static\config.js" (
  echo Missing static\config.js. Extract all contents of the website ZIP here.
  pause
  exit /b 1
)
call firebase deploy --only hosting
if errorlevel 1 (
  echo Deployment failed. Review the message above.
  pause
  exit /b 1
)
echo Deployment finished.
pause
