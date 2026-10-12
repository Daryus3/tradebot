@echo off
chcp 65001 >nul
title Kripto Bot - TEST MODU
echo ================================================================
echo   Kripto Al-Sat Botu - TEST MODU (paper trading)
echo   Sanal bakiye ile canlı fiyatlardan al-sat yapar, GERÇEK emir göndermez.
echo   Bot çökerse bekçi (watchdog) onu otomatik olarak yeniden başlatır.
echo   Durdurmak için: Ctrl+C
echo ================================================================
echo.
call "%~dp0_hazirla.bat" || exit /b 1
python watchdog.py --mode paper
echo.
pause
