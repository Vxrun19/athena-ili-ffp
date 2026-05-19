@echo off
REM ============================================================================
REM  Athena ILI FFP Tool — Windows build script
REM
REM  Builds the PyInstaller dist folder + Inno Setup installer in one go.
REM  Run from the project root or from packaging\ — auto-detects either.
REM
REM  Output: dist\Setup_AthenaIliFfp_v<VERSION>.exe
REM ============================================================================

setlocal enableextensions enabledelayedexpansion

REM ---- Resolve project root regardless of where this script is invoked from --
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%\.." >nul
set "PROJECT_ROOT=%CD%"
popd >nul

echo.
echo ====================================================================
echo   Athena ILI FFP Tool - Windows build
echo   project root: %PROJECT_ROOT%
echo ====================================================================
echo.

cd /d "%PROJECT_ROOT%"

REM ---- 0. Refuse to run if a previously-built GUI / CLI exe is still open ----
REM
REM PyInstaller's COLLECT step calls shutil.rmtree on dist\athena_ili_ffp\
REM before writing the new bundle. If AthenaIliFfp.exe or athena_ili_ffp.exe
REM is currently running, .pyd files under _internal\ are locked and the
REM rmtree raises PermissionError [WinError 5] ~3 minutes into the build.
REM Fail fast at second 0 instead.
REM
REM `tasklist /NH` strips the header so the row-filter only matches
REM actual process rows. When no process matches, tasklist writes the
REM "INFO: No tasks..." line which the row-filter won't match — so
REM RUNNING_PID stays empty.
REM
REM Filter is `findstr` (NOT `find`): on this Windows-11 + Python-
REM subprocess invocation path, plain `find /I` silently failed to
REM match the tasklist output (likely an OEM-codepage / pipe-buffering
REM mismatch in the subprocess environment). `findstr` is more robust
REM and matches reliably.
set "RUNNING_PID="
for /f "tokens=2" %%P in ('tasklist /NH /FI "IMAGENAME eq AthenaIliFfp.exe" 2^>nul ^| findstr /I "AthenaIliFfp.exe"') do set "RUNNING_PID=%%P"
if defined RUNNING_PID (
    echo.
    echo ERROR: AthenaIliFfp.exe is currently running ^(PID !RUNNING_PID!^).
    echo PyInstaller cannot overwrite locked .pyd files in dist\athena_ili_ffp\_internal\.
    echo Close the running application and re-run build.bat.
    endlocal & exit /b 1
)
set "RUNNING_PID="
for /f "tokens=2" %%P in ('tasklist /NH /FI "IMAGENAME eq athena_ili_ffp.exe" 2^>nul ^| findstr /I "athena_ili_ffp.exe"') do set "RUNNING_PID=%%P"
if defined RUNNING_PID (
    echo.
    echo ERROR: athena_ili_ffp.exe is currently running ^(PID !RUNNING_PID!^).
    echo PyInstaller cannot overwrite locked .pyd files in dist\athena_ili_ffp\_internal\.
    echo Close the running application and re-run build.bat.
    endlocal & exit /b 1
)

REM ---- 1. Activate virtual environment ----------------------------------------
if exist ".venv\Scripts\activate.bat" (
    echo [1/5] Activating .venv ...
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    echo [1/5] Activating venv ...
    call venv\Scripts\activate.bat
) else (
    echo [1/5] No .venv detected at "%PROJECT_ROOT%\.venv" — proceeding with
    echo       the system Python. If PyInstaller is missing, install with:
    echo           pip install pyinstaller
)

REM ---- 2. Stamp the build date into _version.py ------------------------------
REM
REM Old versions of this script used `wmic os get localdatetime` to read the
REM clock. wmic was removed from Windows 11 24H2; on those machines the for
REM loop silently produced empty output, %DT:~0,4% expansion mangled the
REM string, and _version.py was left with "~0,4DT:~4,2..." junk that the
REM restore-step regex at the bottom couldn't undo. We now use PowerShell
REM (always present on Win10+) which gives a clean ISO-8601 timestamp.
REM
REM Quoting note: `for /f "usebackq"` uses backticks instead of single
REM quotes around the command, which lets the inner PowerShell argument
REM stay a normal double-quoted string. The earlier form
REM   for /f "delims=" %%I in ('powershell ... "Get-Date ..."')
REM nested cmd's single-quoted command-region around double-quoted
REM PowerShell args; on some Windows-11 26100+ builds the cmd parser
REM choked on the nesting with "The filename, directory name, or volume
REM label syntax is incorrect" before powershell ever launched. The
REM usebackq form sidesteps the nesting entirely.
echo [2/5] Stamping build date into src\_version.py ...
set "BUILD_DATE="
for /f "usebackq tokens=* delims=" %%I in (`powershell -NoProfile -Command "Get-Date -Format yyyy-MM-ddTHH:mm:ss"`) do set "BUILD_DATE=%%I"

REM Strip any trailing whitespace that may sneak in via the pipe.
if defined BUILD_DATE for /f "tokens=* delims= " %%T in ("%BUILD_DATE%") do set "BUILD_DATE=%%T"

if "%BUILD_DATE%"=="" (
    echo     WARNING: PowerShell didn't return a timestamp; using fallback.
    set "BUILD_DATE=unknown"
)
echo     -^> %BUILD_DATE%

REM Replace the literal "auto" with the timestamp. Restored at end of script
REM so checked-in source is untouched.
powershell -NoProfile -Command "(Get-Content 'src\_version.py') -replace '__build_date__ = \"auto\"', ('__build_date__ = \"' + '%BUILD_DATE%' + '\"') | Set-Content 'src\_version.py'"

REM Save the resolved version for the installer filename.
for /f "tokens=2 delims==" %%V in ('findstr "__version__" src\_version.py') do (
    set "VERSION_RAW=%%V"
)
set "VERSION=%VERSION_RAW: =%"
set "VERSION=%VERSION:"=%"
echo     resolved version: %VERSION%

REM ---- 3. Run PyInstaller -----------------------------------------------------
echo.
echo [3/5] Running PyInstaller ...
if exist "dist\athena_ili_ffp" rmdir /s /q "dist\athena_ili_ffp"
if exist "build" rmdir /s /q "build"

pyinstaller --clean --noconfirm packaging\build_windows.spec
if errorlevel 1 (
    echo.
    echo ===== PyInstaller build FAILED =====
    call :restore_version_file
    endlocal & exit /b 1
)

if not exist "dist\athena_ili_ffp\athena_ili_ffp.exe" (
    echo.
    echo ===== PyInstaller did not produce dist\athena_ili_ffp\athena_ili_ffp.exe =====
    call :restore_version_file
    endlocal & exit /b 1
)
echo     -^> dist\athena_ili_ffp\athena_ili_ffp.exe

REM ---- 4. Run Inno Setup ------------------------------------------------------
echo.
echo [4/5] Compiling Inno Setup installer ...

REM Candidate locations in priority order. The first two pick up a
REM portable Inno Setup 6 install dropped alongside the project (or
REM one level above it) — useful for Athena's build machine where
REM Inno Setup ships as a folder next to ili_ffp_tool/ rather than
REM as a registered Program Files install. The latter two are the
REM standard Windows install paths.
REM
REM `%~dp0` expands to the directory of THIS script (packaging\)
REM including a trailing backslash, so `%~dp0..` resolves to the
REM project root.
set "ISCC="
if exist "%~dp0..\Inno Setup 6\ISCC.exe"             set "ISCC=%~dp0..\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%~dp0..\..\Inno Setup 6\ISCC.exe"     set "ISCC=%~dp0..\..\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe"      set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" (
    echo.
    echo     ISCC.exe not found in the usual Inno Setup 6 locations.
    echo     Install from https://jrsoftware.org/isdl.php
    echo     PyInstaller dist folder is ready at: dist\athena_ili_ffp\
    call :restore_version_file
    endlocal & exit /b 1
)

"%ISCC%" packaging\installer.iss
if errorlevel 1 (
    echo.
    echo ===== Inno Setup compile FAILED =====
    call :restore_version_file
    endlocal & exit /b 1
)

REM ---- 5. Restore _version.py for clean git status ----------------------------
echo.
echo [5/5] Restoring src\_version.py (build date -^> "auto") ...
call :restore_version_file

REM ---- Done -------------------------------------------------------------------
echo.
echo ====================================================================
echo   BUILD COMPLETE
echo ====================================================================
echo.
echo   PyInstaller dist:  dist\athena_ili_ffp\
echo   Installer:         dist\Setup_AthenaIliFfp_v%VERSION%.exe
echo.
endlocal & exit /b 0


:restore_version_file
REM Helper — put _version.py back to the "auto" placeholder so the build
REM doesn't dirty the working tree.
powershell -NoProfile -Command "(Get-Content 'src\_version.py') -replace '__build_date__ = \"[0-9T\-:]+\"', '__build_date__ = \"auto\"' | Set-Content 'src\_version.py'"
exit /b 0
