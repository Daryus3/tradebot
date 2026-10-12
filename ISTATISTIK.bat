@echo off
chcp 65001 >nul
title Kripto Bot - İstatistikler
echo ================================================================
echo   📊 Kripto Al-Sat Botu - İSTATİSTİKLER
echo   İşlem geçmişinizin özetini gösterir (kâr/zarar, başarı oranı vb.).
echo ================================================================
echo.
call "%~dp0_hazirla.bat" || exit /b 1
python main.py --istatistik
echo.
pause
