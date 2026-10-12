@echo off
chcp 65001 >nul
title Kripto Bot - Self-test
echo ================================================================
echo   Kripto Al-Sat Botu - SELF-TEST
echo   Ayarları, risk kurallarını, Binance ve Telegram bağlantısını kontrol eder.
echo   Gerçek işlem yapılmaz.
echo ================================================================
echo.
call "%~dp0_hazirla.bat" || exit /b 1
python main.py --selftest
echo.
pause
