@echo off
rem Installs Konus (Windows) for the current user: a private Python
rem environment in .venv, the packages it needs, and the libVLC runtime.
setlocal EnableExtensions
cd /d "%~dp0"
echo.
echo  Konus setup (Windows)
echo  ---------------------------
echo.

rem ---- 1. find Python (64-bit 3.10+)
set "PY="
where py >nul 2>nul && py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python isn't installed. Get the 64-bit installer from https://www.python.org/downloads/windows/
    echo and tick "Add python.exe to PATH" during setup. Then run this again.
    start "" https://www.python.org/downloads/windows/
    pause
    exit /b 1
)
%PY% -c "import sys, struct; ok = sys.version_info >= (3, 10) and struct.calcsize('P') == 8; print('Found Python', sys.version.split()[0], '64-bit' if struct.calcsize('P') == 8 else '32-bit'); sys.exit(0 if ok else 1)"
if errorlevel 1 (
    echo Konus needs 64-bit Python 3.10 or newer.
    pause
    exit /b 1
)

rem ---- 2. private environment + packages
if not exist ".venv\Scripts\pythonw.exe" (
    echo Creating a private Python environment...
    %PY% -m venv .venv || (echo Couldn't create it.& pause & exit /b 1)
)
echo Installing PyQt5 and mutagen (about 60 MB, first time only)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Installing failed. Check your internet connection and run setup.bat again.
    pause
    exit /b 1
)

rem ---- 3. libVLC runtime (so VLC itself doesn't have to be installed)
set "VLCDIR="
if exist "runtime\libvlc\libvlc.dll" (
    echo Using the libVLC runtime already in runtime\libvlc.
    goto :vlc_ready
)
echo.
choice /c YN /m "Download libVLC from videolan.org (about 80 MB, so VLC needn't be installed)"
if errorlevel 2 goto :look_for_installed
".venv\Scripts\python.exe" fetch_libvlc.py
if errorlevel 1 goto :fetch_failed
if not exist "runtime\libvlc\libvlc.dll" goto :fetch_failed
echo Using the libVLC runtime in runtime\libvlc.
goto :vlc_ready

:fetch_failed
echo.
echo Couldn't fetch the runtime. Looking for an installed VLC instead.

:look_for_installed
if exist "vlc_dir.txt" (
    set /p VLCDIR=<vlc_dir.txt
)
if defined VLCDIR if not exist "%VLCDIR%\libvlc.dll" set "VLCDIR="
if not defined VLCDIR (
    for /f "tokens=2,*" %%a in ('reg query "HKLM\SOFTWARE\VideoLAN\VLC" /v InstallDir 2^>nul ^| findstr /i InstallDir') do set "VLCDIR=%%b"
)
if not defined VLCDIR (
    for /f "tokens=2,*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\VideoLAN\VLC" /v InstallDir 2^>nul ^| findstr /i InstallDir') do set "VLCDIR=%%b"
)
if not defined VLCDIR (
    for /f "tokens=2,*" %%a in ('reg query "HKCU\SOFTWARE\VideoLAN\VLC" /v InstallDir 2^>nul ^| findstr /i InstallDir') do set "VLCDIR=%%b"
)
if not defined VLCDIR if exist "%ProgramFiles%\VideoLAN\VLC\libvlc.dll" set "VLCDIR=%ProgramFiles%\VideoLAN\VLC"
if not defined VLCDIR if exist "%ProgramFiles(x86)%\VideoLAN\VLC\libvlc.dll" set "VLCDIR=%ProgramFiles(x86)%\VideoLAN\VLC"
if not defined VLCDIR (
    echo.
    echo VLC wasn't found in the usual places. Install 64-bit VLC 3.0 from https://www.videolan.org,
    echo or type the folder that contains libvlc.dll below (for example E:\Programs\VLC).
    set /p VLCDIR="VLC folder: "
)
if not exist "%VLCDIR%\libvlc.dll" (
    echo "%VLCDIR%\libvlc.dll" doesn't exist. Run setup.bat again with a working connection, or
    echo install 64-bit VLC 3.0 and run setup.bat again.
    pause
    exit /b 1
)
> vlc_dir.txt echo %VLCDIR%
echo Using the VLC in "%VLCDIR%".

:vlc_ready

rem libVLC reads plugins\plugins.dat at startup and cannot write it itself: without the cache
rem every start walks the whole plugins folder. A runtime fetched before this file existed has
rem no cache, so build one here rather than leaving the player slow forever.
if not exist "runtime\libvlc\plugins\plugins.dat" (
    echo Building the libVLC plugin cache ^(it is what keeps the first start quick^)...
    ".venv\Scripts\python.exe" fetch_libvlc.py --cache-only
)

rem ---- 4. desktop shortcut (optional)
echo.
choice /c YN /m "Add a Konus shortcut to your desktop"
if not errorlevel 2 (
    powershell -NoProfile -Command ^
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Konus.lnk');" ^
        "$s.TargetPath = '%~dp0run-hidden.vbs';" ^
        "$s.WorkingDirectory = '%~dp0';" ^
        "$s.IconLocation = '%~dp0konus\assets\konus.ico';" ^
        "$s.Save()"
)

echo.
echo  Konus is ready. Start it with run.bat or the desktop shortcut.
echo.
echo  PyQt5 and mutagen are GPL, so what you run here is GPLv3 as a whole, while this
echo  project's own code stays MIT.
echo.
pause
