@echo off
chcp 65001 >nul
rem ================================================================
rem  Ortak hazırlık - diğer .bat dosyaları bunu otomatik çağırır.
rem   1. Python'u bulur
rem   2. Sanal ortamı (venv) yoksa oluşturur ve etkinleştirir
rem   3. requirements.txt değiştiyse gerekli paketleri kurar
rem  Not: setlocal KULLANILMAZ; venv etkinliği çağıran pencerede kalmalı.
rem ================================================================
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" --version >nul 2>&1 && goto :venv_hazir
    echo ⚠️ Sanal ortam bozuk görünüyor, yeniden oluşturulacak...
)

set "PY_CMD="
py -3 --version >nul 2>&1 && set "PY_CMD=py -3"
if not defined PY_CMD (
    python --version >nul 2>&1 && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo.
    echo ❌ Python bulunamadı!
    echo.
    echo    1. https://www.python.org/downloads/ adresinden Python 3.10 veya üstünü indirip kurun.
    echo    2. Kurulumun ilk ekranında "Add Python to PATH" kutusunu MUTLAKA işaretleyin.
    echo    3. Kurulum bitince bu pencereyi kapatıp dosyayı yeniden çalıştırın.
    echo.
    pause
    exit /b 1
)

echo 📦 İlk kurulum: sanal ortam (venv) oluşturuluyor...
%PY_CMD% -m venv --clear venv
if errorlevel 1 goto :venv_hata
if not exist "venv\Scripts\activate.bat" goto :venv_hata

:venv_hazir
call "venv\Scripts\activate.bat"

set "REQ_OK="
if exist "venv\.req_installed" (
    fc /b "requirements.txt" "venv\.req_installed" >nul 2>&1 && set "REQ_OK=1"
)
if defined REQ_OK (
    python -c "import requests, dotenv, websocket" >nul 2>&1 || set "REQ_OK="
)
if not defined REQ_OK (
    echo 📦 Gerekli paketler kuruluyor, ilk seferde birkaç dakika sürebilir...
    python -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ❌ Paketler kurulamadı. İnternet bağlantınızı kontrol edip tekrar deneyin.
        echo.
        pause
        exit /b 1
    )
    copy /y "requirements.txt" "venv\.req_installed" >nul
)
set "REQ_OK="
set "PY_CMD="
exit /b 0

:venv_hata
echo.
echo ❌ Sanal ortam (venv) oluşturulamadı. Proje klasöründeki "venv" klasörünü silip tekrar deneyin.
echo.
pause
exit /b 1
