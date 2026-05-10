@echo off
title ATVLoader - Build EXE
echo.
echo     ATVLoader v1.0 - Building EXE
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found.
    echo   Download from https://python.org
    echo   Check "Add to PATH" during install.
    pause
    exit /b 1
)

echo   [1/4] Installing dependencies...
echo.
python -m pip install customtkinter pymobiledevice3 zeroconf Pillow pyinstaller
echo.

echo   [2/4] Locating packages...
for /f "delims=" %%i in ('python -c "import customtkinter,os;print(os.path.dirname(customtkinter.__file__))"') do set CTK_PATH=%%i
if "%CTK_PATH%"=="" (
    echo   [ERROR] customtkinter not found.
    pause
    exit /b 1
)
echo          customtkinter: %CTK_PATH%
echo.

echo   [3/4] Building ATVLoader.exe...
echo         This takes a few minutes.
echo.

python -m PyInstaller ^
    --noconfirm ^
    --onedir ^
    --console ^
    --name "ATVLoader" ^
    --add-data "%CTK_PATH%;customtkinter/" ^
    --collect-all "pymobiledevice3" ^
    --collect-all "customtkinter" ^
    --collect-all "zeroconf" ^
    --collect-all "ifaddr" ^
    --collect-all "srptools" ^
    --collect-all "qh3" ^
    --collect-all "pytun_pmd3" ^
    --collect-all "opack2" ^
    --collect-all "construct" ^
    --collect-all "bpylist2" ^
    --collect-all "cryptography" ^
    --collect-all "ipsw_parser" ^
    --collect-all "pyimg4" ^
    --collect-all "hyperframe" ^
    --collect-all "inquirer3" ^
    --hidden-import "pymobiledevice3" ^
    --hidden-import "pymobiledevice3.remote" ^
    --hidden-import "pymobiledevice3.remote.tunnel_service" ^
    --hidden-import "pymobiledevice3.remote.remote_service_discovery" ^
    --hidden-import "pymobiledevice3.services.installation_proxy" ^
    --hidden-import "pymobiledevice3.services.afc" ^
    --hidden-import "pymobiledevice3.lockdown" ^
    --hidden-import "pymobiledevice3.exceptions" ^
    --hidden-import "pymobiledevice3.cli" ^
    --hidden-import "pymobiledevice3.cli.remote" ^
    --hidden-import "pymobiledevice3.cli.apps" ^
    --hidden-import "pymobiledevice3.__main__" ^
    --hidden-import "zeroconf" ^
    --hidden-import "zeroconf._utils" ^
    --hidden-import "PIL" ^
    --hidden-import "plistlib" ^
    --hidden-import "customtkinter" ^
    --hidden-import "asyncio" ^
    --hidden-import "srptools" ^
    --hidden-import "qh3" ^
    --hidden-import "opack2" ^
    --hidden-import "construct" ^
    --hidden-import "bpylist2" ^
    --hidden-import "cryptography" ^
    --hidden-import "pytun_pmd3" ^
    --hidden-import "hyperframe" ^
    --hidden-import "ifaddr" ^
    --hidden-import "inquirer3" ^
    "%~dp0atvloader.py"

if errorlevel 1 (
    echo.
    echo   [ERROR] Build failed. Check errors above.
    pause
    exit /b 1
)

echo.
echo   [4/4] Creating setup script for end users...

REM setup script for end users
(
echo @echo off
echo title ATVLoader - First Time Setup
echo echo.
echo echo     ATVLoader - Installing Dependencies
echo echo     This only needs to run once.
echo echo.
echo python --version ^>nul 2^>^&1
echo if errorlevel 1 ^(
echo     echo   [ERROR] Python is required.
echo     echo   Download from https://python.org
echo     echo   Check "Add to PATH" during install.
echo     pause
echo     exit /b 1
echo ^)
echo echo   Installing pymobiledevice3...
echo python -m pip install pymobiledevice3
echo echo.
echo echo   Done! You can now run ATVLoader.exe
echo pause
) > "dist\ATVLoader\SETUP_DEPS.bat"

echo.
echo     Build complete!
echo.
echo   Your app is at: dist\ATVLoader\
echo.
pause
