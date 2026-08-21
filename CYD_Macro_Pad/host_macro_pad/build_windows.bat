@echo off
rem ============================================================
rem  CYD Macro Pad host - Windows build script
rem
rem  Builds (in order):
rem    1) dist\macro_input_helper.exe  - input helper (onefile)
rem    2) dist\CYD Macro Pad.exe       - GUI, helper embedded inside
rem
rem  Usage:
rem    build_windows.bat              clean build (removes build/ dist/)
rem    build_windows.bat --skip-clean incremental build (faster)
rem
rem  Prereq on this Windows machine:
rem    pip install -r requirements.txt pyinstaller
rem    (requirements.txt already lists pynput + Pillow)
rem
rem  NOTE: PyInstaller cannot cross-compile - run this ON Windows.
rem  Output is self-contained: copy dist\CYD Macro Pad.exe alone to
rem  any Windows PC and run. No extra helper file needed.
rem ============================================================
setlocal
cd /d "%~dp0"

rem --- resolve Python + PyInstaller (python -> py -3 fallback) ---
set "PYCMD=python -m PyInstaller"
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    set "PYCMD=py -3 -m PyInstaller"
    py -3 -m PyInstaller --version >nul 2>&1
    if errorlevel 1 (
        echo [ERR] Python + PyInstaller not found. Run:
        echo       pip install -r requirements.txt pyinstaller
        exit /b 1
    )
)

rem --- clean (default) or incremental ---
if not "%1"=="--skip-clean" (
    echo ==^> clean build: removing build/ dist/
    if exist build rmdir /s /q build
    if exist dist  rmdir /s /q dist
) else (
    echo ==^> --skip-clean: keeping build/ dist/
)

rem --- 1/2 helper first (the GUI embeds it) ---
echo ==^> [1/2] building helper (macro_input_helper.spec)
%PYCMD% --noconfirm macro_input_helper.spec
if errorlevel 1 (
    echo [ERR] helper build failed
    exit /b 1
)
if not exist "dist\macro_input_helper.exe" (
    echo [ERR] helper output missing: dist\macro_input_helper.exe
    exit /b 1
)

rem --- 2/2 GUI onefile ---
echo ==^> [2/2] building GUI (CYD Macro Pad.spec)
%PYCMD% --noconfirm "CYD Macro Pad.spec"
if errorlevel 1 (
    echo [ERR] GUI build failed
    exit /b 1
)

rem --- verify output ---
set "OUT=dist\CYD Macro Pad.exe"
if not exist "%OUT%" (
    echo [ERR] build output missing: %OUT%
    exit /b 1
)

echo.
echo [OK] build complete: %OUT%
echo      Single self-contained exe. Copy it to any Windows PC and run.
echo      SmartScreen may warn for unsigned apps - click "More info - Run anyway".
endlocal
