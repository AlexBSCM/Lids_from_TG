# BanditTour Bot Watchdog
# Проверяет, что бот жив, и перезапускает его при необходимости

 $ProjectDir = "D:\BanditTour\Lids_from_TG"
 $BotScript = "$ProjectDir\bot.py"
 $LogFile = "$ProjectDir\logs\watchdog.log"
 $MaxLogSize = 1MB

# Функция логирования
function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    # Ротация лога
    if ((Get-Item $LogFile -ErrorAction SilentlyContinue).Length -gt $MaxLogSize) {
        Remove-Item $LogFile -Force
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    }
}

# Функция отправки уведомления в Telegram (через bot_token)
function Send-TelegramAlert {
    param([string]$Message)
    try {
        $config = Get-Content "$ProjectDir\test_config.json" -Raw -Encoding UTF8 | ConvertFrom-Json
        $botToken = $config.bot_token
        $chatId = $config.notify_chat_id
        $url = "https://api.telegram.org/bot$botToken/sendMessage"
        $body = @{
            chat_id = $chatId
            text = $Message
        } | ConvertTo-Json
        Invoke-RestMethod -Uri $url -Method Post -Body $body -ContentType "application/json; charset=utf-8" | Out-Null
    } catch {
        Write-Log "Не удалось отправить Telegram уведомление: $_"
    }
}

# Проверяем, запущен ли бот
 $botProcess = Get-Process -Name pythonw -ErrorAction SilentlyContinue
if (-not $botProcess) {
    # Пробуем и python, и pythonw
    $botProcess = Get-Process -Name python -ErrorAction SilentlyContinue
}

if ($botProcess) {
    # Бот работает — ничего не делаем
    Write-Log "OK: bot running (PID $($botProcess.Id))"
    
    # Обновляем дашборд
    try {
        $pyExe = "C:\Users\AVZ\AppData\Local\Python\pythoncore-3.12-64\python.exe"
        $dashScript = "$ProjectDir\generate_dashboard.py"
        if (Test-Path $dashScript) {
            & $pyExe $dashScript 2>&1 | Out-Null
        }
    } catch {
        Write-Log "Dashboard update error: $_"
    }
    exit 0
}

# Бот не работает — перезапускаем
Write-Log "WARNING: bot not running, restarting..."

# Удаляем session-journal (от database is locked)
Get-ChildItem $ProjectDir -Filter "*.session-journal" | Remove-Item -Force -ErrorAction SilentlyContinue

# Запускаем бота через run_bot.bat (скрыто)
 $batPath = "$ProjectDir\run_bot.bat"
if (Test-Path $batPath) {
    Start-Process -FilePath $batPath -WorkingDirectory $ProjectDir
    Write-Log "OK: bot restart initiated via run_bot.bat"
    
    # Ждём 20 секунд и проверяем, что бот запустился
    Start-Sleep -Seconds 20
    $botProcess = Get-Process -Name pythonw -ErrorAction SilentlyContinue
    if (-not $botProcess) {
        $botProcess = Get-Process -Name python -ErrorAction SilentlyContinue
    }
    
    if ($botProcess) {
        Write-Log "OK: bot successfully restarted (PID $($botProcess.Id))"
        Send-TelegramAlert -Message "⚠️ BanditTour Bot был остановлен и автоматически перезапущен (watchdog).`n`nВремя: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`nPID: $($botProcess.Id)"
    } else {
        Write-Log "ERROR: bot failed to start after restart attempt"
        Send-TelegramAlert -Message "🚨 BanditTour Bot НЕ удалось перезапустить!`n`nВремя: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`nНужно проверить вручную."
    }
} else {
    Write-Log "ERROR: run_bot.bat not found at $batPath"
    Send-TelegramAlert -Message "🚨 BanditTour: run_bot.bat не найден! Путь: $batPath"
}