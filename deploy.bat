@echo off
rem ===========================================================================
rem  deploy.bat - copy this project's CODE to the shared network folder.
rem
rem  Usage, from inside the project folder:
rem    deploy.bat "<destination>"              copy
rem    deploy.bat "<destination>" /L           dry run, shows what would copy
rem    deploy.bat "<destination>" /CONFIG      also copy config.json
rem    deploy.bat "<destination>" /Y           skip the "Pro closed?" prompt
rem
rem  Only files the project tracks are copied. data\, PDF_Output\,
rem  Temp GIS Maps\, geodatabases and anything else already in the
rem  destination are never touched and never deleted.
rem
rem  Git is used ONLY on a machine that already has the clone, to work out
rem  which files are code. It is never required on the destination's machine:
rem  each copy carries a deploy_manifest.txt so it can deploy onward without
rem  git installed anywhere near the share.
rem ===========================================================================
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "DEST=%~1"
set "DRYRUN="
set "WITHCONFIG="
set "NOPROMPT="
for %%A in (%2 %3 %4) do (
  if /i "%%~A"=="/L" set "DRYRUN=1"
  if /i "%%~A"=="/CONFIG" set "WITHCONFIG=1"
  if /i "%%~A"=="/Y" set "NOPROMPT=1"
)

if "%DEST%"=="" goto usage
if not exist "%ROOT%\SASD_HFML.pyt" (
  echo ERROR: SASD_HFML.pyt not found next to deploy.bat.
  echo        Run deploy.bat from the project folder.
  exit /b 1
)

echo.
echo   Source      : %ROOT%
echo   Destination : %DEST%
if defined DRYRUN echo   Mode        : DRY RUN - nothing will be written
echo.

if defined DRYRUN goto :afterprompt
if defined NOPROMPT goto :afterprompt
echo   ArcGIS Pro holds locks on the project and reads SASD_HFML.pyt at run
echo   time. Everyone using the shared tool should close Pro first.
echo.
set "OK="
set /p "OK=  Is ArcGIS Pro closed for all users? [y/N] "
if /i not "%OK%"=="y" (
  echo   Cancelled - nothing was copied.
  exit /b 1
)
echo.
:afterprompt

rem --- scratch area -----------------------------------------------------------
set "STAGE=%TEMP%\hfml_deploy_%RANDOM%%RANDOM%"
set "MANIFEST=%STAGE%.manifest"
mkdir "%STAGE%" 2>nul
if not exist "%STAGE%" (
  echo ERROR: could not create a staging folder under %TEMP%.
  exit /b 1
)

rem --- which files are code? --------------------------------------------------
set "HAVEGIT="
if exist "%ROOT%\.git" (
  where git >nul 2>&1 && set "HAVEGIT=1"
)

if defined HAVEGIT (
  git -C "%ROOT%" ls-files > "%MANIFEST%" 2>nul
) else (
  if exist "%ROOT%\deploy_manifest.txt" (
    copy /y "%ROOT%\deploy_manifest.txt" "%MANIFEST%" >nul
  ) else (
    echo ERROR: this folder has no git clone and no deploy_manifest.txt, so
    echo        there is no way to tell code files from data files.
    echo.
    echo        Run deploy.bat on the machine that has the clone first, with a
    echo        staging folder as the destination ^(a USB drive, say^). That copy
    echo        carries its own deploy_manifest.txt, so running deploy.bat from
    echo        there to the share needs no git at all.
    rd /s /q "%STAGE%" 2>nul
    exit /b 1
  )
)

for %%Z in ("%MANIFEST%") do if %%~zZ EQU 0 (
  echo ERROR: the file list came back empty. Nothing copied.
  rd /s /q "%STAGE%" 2>nul
  del "%MANIFEST%" 2>nul
  exit /b 1
)

rem --- stage the listed files -------------------------------------------------
set "SKIPPED="
for /f "usebackq delims=" %%F in ("%MANIFEST%") do call :stageone "%%F"
copy /y "%MANIFEST%" "%STAGE%\deploy_manifest.txt" >nul

rem --- stamp the version ------------------------------------------------------
call :writeversion

rem --- copy -------------------------------------------------------------------
set "RCFLAGS=/E /NFL /NDL /NJH /NJS /R:2 /W:5"
if defined DRYRUN set "RCFLAGS=%RCFLAGS% /L"
robocopy "%STAGE%" "%DEST%" %RCFLAGS%
set "RC=%ERRORLEVEL%"

if %RC% GEQ 8 (
  echo.
  echo ERROR: robocopy failed with code %RC% - nothing can be assumed copied.
  echo        Common causes: no permission on the share, the path is not
  echo        reachable from this machine, or a file is locked by ArcGIS Pro.
  rd /s /q "%STAGE%" 2>nul
  del "%MANIFEST%" 2>nul
  exit /b 1
)

rem --- report files in the destination that the project no longer has ---------
echo.
echo Files present in the destination but not in this project
echo ^(listed only - deploy.bat never deletes anything^):
robocopy "%STAGE%" "%DEST%" /E /MIR /L /FP /NJH /NJS /NDL 2>nul | findstr /i /c:"EXTRA File"
echo   ^(nothing listed above means none^)

rd /s /q "%STAGE%" 2>nul
del "%MANIFEST%" 2>nul

echo.
if defined DRYRUN (
  echo Dry run finished. Re-run without /L to copy.
) else (
  echo Deployed. Each user should now right-click the toolbox in the ArcGIS Pro
  echo Catalog pane and choose Refresh to pick up SASD_HFML.pyt.
  echo The deployed commit is recorded in VERSION.txt in the destination.
)
if defined SKIPPED echo Note: config.json was NOT copied. Pass /CONFIG to include it.
exit /b 0

rem ===========================================================================
:stageone
set "REL=%~1"
if "%REL%"=="" goto :eof
set "REL=%REL:/=\%"
if /i "%REL%"=="config.json" if not defined WITHCONFIG (
  set "SKIPPED=1"
  goto :eof
)
if not exist "%ROOT%\%REL%" (
  echo WARNING: listed but not on disk, skipped: %REL%
  goto :eof
)
for %%D in ("%STAGE%\%REL%") do if not exist "%%~dpD" mkdir "%%~dpD" 2>nul
copy /y "%ROOT%\%REL%" "%STAGE%\%REL%" >nul
goto :eof

rem ===========================================================================
:writeversion
set "COMMIT=unknown"
set "BRANCH=unknown"
set "DIRTY="
if defined HAVEGIT (
  for /f "delims=" %%I in ('git -C "%ROOT%" rev-parse --short HEAD 2^>nul') do set "COMMIT=%%I"
  for /f "delims=" %%I in ('git -C "%ROOT%" rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%I"
  for /f "delims=" %%I in ('git -C "%ROOT%" status --porcelain 2^>nul') do set "DIRTY=1"
)
set "VF=%STAGE%\VERSION.txt"
if defined HAVEGIT (
  echo SASD HFML - deployed code> "%VF%"
  echo commit : %COMMIT%>> "%VF%"
  echo branch : %BRANCH%>> "%VF%"
  if defined DIRTY echo WARNING: the source clone had uncommitted changes at deploy time.>> "%VF%"
) else (
  if exist "%ROOT%\VERSION.txt" (
    copy /y "%ROOT%\VERSION.txt" "%VF%" >nul
  ) else (
    echo SASD HFML - deployed code> "%VF%"
    echo commit : unknown ^(deployed from a copy with no git^)>> "%VF%"
  )
)
echo deployed : %DATE% %TIME%>> "%VF%"
echo by       : %USERNAME% on %COMPUTERNAME%>> "%VF%"
echo from     : %ROOT%>> "%VF%"
if defined WITHCONFIG (echo config.json : included>> "%VF%") else (echo config.json : not copied - destination copy left as it was>> "%VF%")
goto :eof

rem ===========================================================================
:usage
echo.
echo Usage: deploy.bat "^<destination folder^>" [/L] [/CONFIG] [/Y]
echo.
echo   /L        dry run - list what would be copied, write nothing
echo   /CONFIG   also copy config.json ^(overwrites the destination's^)
echo   /Y        do not ask whether ArcGIS Pro is closed
echo.
echo Example:
echo   deploy.bat "\\sdfiles\Shared\...\High Frequency PM Project\Code" /L
echo.
exit /b 1
