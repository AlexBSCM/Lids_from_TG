@echo off
chcp 65001 > nul
cd /d D:\BanditTour\Lids_from_TG

REM Полный путь к настоящему Python 3.12 (не PyManager!)
set PYTHON_EXE=C:\Users\AVZ\AppData\Local\Python\pythoncore-3.12-64\python.exe

REM Записываем время запуска
echo ============================================ >> logs\startup.log
echo [%date% %time%] Starting bot.py with %PYTHON_EXE% >> logs\startup.log
echo ============================================ >> logs\startup.log

REM Запускаем бота с логированием
"%PYTHON_EXE%" bot.py >> logs\bot.log 2>&1

REM Если бот упал — записываем код ошибки
echo [%date% %time%] Bot exited with code %errorlevel% >> logs\startup.log