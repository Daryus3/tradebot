@echo off
chcp 65001 >nul
title Kripto Bot - Backtest
echo ================================================================
echo   🔬 Kripto Al-Sat Botu - BACKTEST (geriye dönük test)
echo   Stratejiyi son 30 günün geçmiş fiyat verileriyle dener; gerçek işlem yapılmaz.
echo   İnternet bağlantısı gerekir, birkaç dakika sürebilir.
echo ================================================================
echo.
call "%~dp0_hazirla.bat" || exit /b 1
python main.py --backtest --gun 30
echo.
pause
