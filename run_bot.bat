@echo off
cd /d D:\BanditTour\Lids_from_TG

echo [%date% %time%] Starting bot hidden >> logs\startup.log

REM Запускаем через VBScript — окно не появится
cscript //nologo "D:\BanditTour\Lids_from_TG\run_bot_hidden.vbs"

REM Ждём 5 секунд
timeout /t 5 /nobreak > nul

REM Проверяем процесс
tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" > nul
if %errorlevel% equ 0 (
    echo [%date% %time%] Bot started OK >> logs\startup.log
) else (
    tasklist /FI "IMAGENAME eq pythonw.exe" 2>nul | find /I "pythonw.exe" > nul
    if %errorlevel% equ 0 (
        echo [%date% %time%] Bot started OK pythonw >> logs\startup.log
    ) else (
        echo [%date% %time%] WARNING python not found >> logs\startup.log
    )
)