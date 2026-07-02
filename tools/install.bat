@echo off
rem AmiFUSE installer launcher.
rem
rem Role: launch install-windows.ps1 UNELEVATED, as the current (standard) user,
rem and hold the window open so any error / exit code stays readable after a
rem double-click (double-clicking the .ps1 directly opens an editor, so this
rem .bat remains the entry point).
rem
rem The installer deliberately runs UNELEVATED. Everything user-scoped -- the
rem per-user Python, the venv under %LOCALAPPDATA%, pip, and the HKCU shell
rem registration -- must land in the double-clicking user's own profile and
rem hive. If the whole installer were elevated via over-the-shoulder admin
rem credentials, all of that would land in the ADMIN's profile and the standard
rem user would get nothing. The ONE action that needs admin (the machine-wide
rem WinFSP kernel driver) elevates itself from inside install-windows.ps1.
rem
rem Pass-through args (e.g. -Uninstall) are forwarded verbatim.

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
set "_rc=%errorlevel%"
echo.
if not "%_rc%"=="0" (
    echo Installer exited with error code %_rc%.
) else (
    echo Installer finished successfully.
)
echo.
rem Hold the window open so the outcome above is readable after a double-click.
pause
