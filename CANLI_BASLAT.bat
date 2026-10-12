@echo off
chcp 65001 >nul
title Kripto Bot - CANLI MOD
echo ================================================================
echo   ⚠️  DİKKAT: CANLI MOD - Bot GERÇEK PARANIZLA Binance'te işlem yapacak!
echo.
echo   - Önce TEST_ET.bat ve birkaç gün BASLAT.bat (test modu) ile deneyin.
echo   - Binance API anahtarında para çekme (Withdraw) izni KAPALI olmalı.
echo   - .env dosyasındaki MAX_TOTAL_LOSS_USDT kadar zarar olursa bot kendini durdurur.
echo   - Birazdan onay için EVET yazmanız istenecek.
echo   - Bot çökerse bekçi onu onay sormadan yeniden başlatır.
echo   - Vazgeçmek için pencereyi kapatın veya Ctrl+C'ye basın.
echo ================================================================
echo.
call "%~dp0_hazirla.bat" || exit /b 1
python watchdog.py --mode live
echo.
pause
