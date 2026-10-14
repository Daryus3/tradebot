@echo off
chcp 65001 >nul
title Kripto Bot - Backtest
echo ================================================================
echo   Kripto Al-Sat Botu - BACKTEST (geriye dönük test)
echo   .env'deki stratejiyi geçmiş fiyat verileriyle dener; gerçek işlem yapılmaz.
echo   (5 dk stratejilerinde son 30 gün, uzun vadeli swing stratejilerinde son 365 gün)
echo   İnternet bağlantısı gerekir, birkaç dakika sürebilir.
echo ================================================================
echo.
call "%~dp0_hazirla.bat" || exit /b 1
python main.py --backtest
echo.
pause
