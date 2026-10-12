@echo off
chcp 65001 >nul
title Kripto Bot - Kurulum Sihirbazı
echo ================================================================
echo   🛠️ Kripto Al-Sat Botu - KURULUM SİHİRBAZI
echo   .env ayar dosyasını sorular sorarak adım adım hazırlar
echo   (Telegram, Binance API anahtarları, sermaye ve risk ayarları).
echo ================================================================
echo.
call "%~dp0_hazirla.bat" || exit /b 1
python main.py --kurulum
echo.
pause
