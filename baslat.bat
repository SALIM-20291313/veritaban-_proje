@echo off
title ScoutX Pro - Futbol Ligi Sunucusu
color 0B

echo ===================================================
echo           ScoutX Pro Baslatiliyor...
echo ===================================================
echo.
echo Lutfen bu siyah pencereyi Kapatmayin!
echo Site arka planda calismasi icin bu ekranin acik kalmasi gerekir.
echo.
echo Tarayiciniz otomatik olarak acilacaktir, lutfen bekleyin...

:: Tarayıcıyı 3 saniye gecikmeli açmak için ping hilesi (sunucunun ayağa kalkmasını beklemek için)
start "" /B cmd /c "ping localhost -n 4 > nul & start http://127.0.0.1:5000"

:: Flask sunucusunu başlat
python app.py

pause
