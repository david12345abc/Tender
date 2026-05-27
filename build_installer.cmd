@echo off
setlocal
if "%~1"=="" (
  echo Usage: build_installer.cmd VERSION [PUBLISH_URL]
  echo Example: build_installer.cmd 1.0.1 \\server\share\ETP_GPB_Search
  exit /b 2
)
set "VERSION=%~1"
set "PUBLISH_URL=%~2"
py "%~dp0build_installer.py" --version "%VERSION%" --publish-url "%PUBLISH_URL%"
exit /b %ERRORLEVEL%
