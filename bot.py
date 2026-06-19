"""
BanditTour Lead Scanner Bot (real-time + inline buttons).

Перед запуском:
1. Заполнить test_config.json (bot_token, notify_chat_id)
2. Запустить: python bot.py
3. Авторизоваться по телефону+коду (первый раз)
4. Открыть бота в Telegram, отправить /start
"""

import asyncio
import json
import re
import sys
import time
import logging
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

from google import generativeai as genai
from telethon import TelegramClient, events, Button

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "test_config.json"
OUTPUT_PATH = BASE_DIR / "matches_found.json"
STATE_PATH = BASE_DIR / "scan_state.json"
SESSION_NAME = str(BASE_DIR / "session")
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_leads():
    if OUTPUT_PATH.exists():
        try:
            return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_leads(leads):
    leads.sort(key=lambda x: x.get("date", ""), reverse=True)
    OUTPUT_PATH.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")


def build_patterns(keywords):
    patterns = []
    for kw in keywords:
        if re.fullmatch(r"[a-zA-Z\s]+", kw) and len(kw) <= 15:
            pat = r"\b" + re.escape(kw) + r"\b"
        else:
            pat = re.escape(kw)
        patterns.append(re.compile(pat, re.IGNORECASE))
    return patterns


def text_matches(text, patterns):
    if not text:
        return False
    return any(p.search(text) for p in patterns)


CLASSIFY_PROMPT = """Ты — ассистент, который отбирает лиды для турфирмы BanditTour.

ГЕОГРАФИЯ: нас интересует ТОЛЬКО север Таиланда:
- Чиангмай (Chiang Mai)
- Чианграй (Chiang Rai)
- Пай (Pai)
- Золотой треугольник (Golden Triangle)
- Мэхонгсон (Mae Hong Son)

ИСКЛЮЧЕНИЯ (это НЕ лиды, категория noise):
- Паттайя (Pattaya), Бангкок (Bangkok), Пхукет (Phuket), Самуи (Koh Samui),
  Краби (Krabi), Хуахин (Hua Hin), Ко Самет, Пханган
- любые другие регионы Таиланда вне севера

КАТЕГОРИИ:
- hot: человек прямо сейчас ищет экскурсию/тура/гида на севере Таиланда
- warm: человек упоминает планы по северу Таиланда, но конкретного запроса пока нет
- spam: повторяющиеся рекламные посты от турфирм/конкурентов
- noise: не лид, упоминание в другом контексте, другие регионы

Сообщение из Telegram-чата:
---
{message}
---

Ответь СТРОГО в формате JSON (без markdown):
{{"is_lead": true/false, "category": "hot|warm|spam|noise", "reason": "короткая причина на русском (до 100 символов)"}}

is_lead=true только если category=hot или warm."""


def init_gemini(api_key, model_name):
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)


def classify_message(model, text, max_retries=3):
    if not text or len(text.strip()) < 10:
        return {"is_lead": False, "category": "noise", "reason": "слишком короткое"}

    prompt = CLASSIFY_PROMPT.format(message=text[:1500])
    for attempt in range(max_retries):
        try:
            resp = model.generate_content(prompt)
            raw = resp.text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            return {
                "is_lead": bool(data.get("is_lead", False)),
                "category": data.get("category", "noise"),
                "reason": data.get("reason", "")[:200],
            }
        except json.JSONDecodeError as e:
            return {"is_lead": False, "category": "noise", "reason": f"неверный JSON: {e}"}
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait = 30
                m = re.search(r"retry in ([\d.]+)s", msg.lower()) or re.search(r"seconds:\s*(\d+)", msg.lower())
                if m:
                    try:
                        wait = int(float(m.group(1))) + 2
                    except ValueError:
                        pass
                if attempt < max_retries - 1:
                    log.warning(f"  rate_limited, жду {wait}с (попытка {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                return {"is_lead": False, "category": "rate_limited", "reason": "лимит Gemini"}
            return {"is_lead": False, "category": "error", "reason": f"{type(e).__name__}: {msg[:100]}"}
    return {"is_lead": False, "category": "error", "reason": "все попытки исчерпаны"}


class BotState:
    def __init__(self):
        self.config = load_config()
        self.patterns = build_patterns(self.config["keywords"])
        self.gemini = init_gemini(self.config["gemini_api_key"], self.config.get("gemini_model", "gemini-2.5-flash-lite"))
        self.leads = load_leads()
        self.known_ids = {x["id"] for x in self.leads}
        self.state = load_state()
        self.is_listening = True
        self.stats_today = {"hot": 0, "warm": 0, "spam": 0, "noise": 0, "rate_limited": 0, "error": 0}
        self.stats_total = dict(self.stats_today)
        self.scan_in_progress = False

    def is_lead_known(self, msg_id):
        return msg_id in self.known_ids

    def add_lead(self, lead):
        if lead["id"] not in self.known_ids:
            self.leads.append(lead)
            self.known_ids.add(lead["id"])
            save_leads(self.leads)

    def add_stat(self, category):
        self.stats_today[category] = self.stats_today.get(category, 0) + 1
        self.stats_total[category] = self.stats_total.get(category, 0) + 1


bs = None


CATEGORY_EMOJI = {"hot": "🔥", "warm": "🌤", "spam": "🗑", "noise": "❌"}

NOTIFY_TEMPLATE = """🎯 НОВЫЙ ЛИД — BanditTour

📍 Канал: {channel}
📅 Дата: {date}
🏷 Категория: {category_emoji} {category}
👤 Sender ID: {sender_id}
🔢 Message ID: {msg_id}
🤖 Причина: {reason}

💬 Текст сообщения:
---
{text}
---
🔗 Ссылка: {link}"""


async def send_notification(client, lead):
    try:
        text = NOTIFY_TEMPLATE.format(
            channel=lead.get("channel_title", lead.get("channel", "?")),
            date=lead.get("date", "?"),
            category_emoji=CATEGORY_EMOJI.get(lead.get("category", ""), "❓"),
            category=lead.get("category", "?").upper(),
            sender_id=lead.get("sender_id", "?"),
            msg_id=lead.get("id", "?"),
            reason=lead.get("reason", ""),
            text=lead.get("text", "")[:1500],
            link=f"https://t.me/{lead.get('channel', '').lstrip('@')}/{lead.get('id', '')}",
        )
        chat_id = bs.config.get("notify_chat_id")
        if chat_id:
            await client.send_message(int(chat_id), text, link_preview=False)
        else:
            await client.send_message("me", text, link_preview=False)
        return True
    except Exception as e:
        log.error(f"Ошибка отправки уведомления: {type(e).__name__}: {e}")
        return False


async def process_new_message(client, event):
    if not bs.is_listening:
        return
    msg = event.message
    text = msg.message or ""
    if not text_matches(text, bs.patterns):
        return

    try:
        entity = await event.get_chat()
        channel = f"@{entity.username}" if entity.username else str(entity.id)
        channel_title = getattr(entity, "title", channel)
    except Exception:
        channel = "?"
        channel_title = "?"

    if bs.is_lead_known(msg.id):
        return

    log.info(f"Новое сообщение в {channel_title} (id={msg.id}): {text[:80]}...")

    result = classify_message(bs.gemini, text)
    cat = result["category"]
    bs.add_stat(cat)
    log.info(f"  → {cat}: {result['reason']}")

    if result["is_lead"]:
        lead = {
            "id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "text": text,
            "sender_id": msg.sender_id,
            "channel": channel,
            "channel_title": channel_title,
            "category": cat,
            "reason": result["reason"],
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
        bs.add_lead(lead)
        await send_notification(client, lead)


def main_menu_buttons():
    buttons = [
        [Button.inline("📊 Статистика", b"stats")],
        [Button.inline("📋 Последние лиды", b"leads"), Button.inline("🔄 Сканировать", b"scan")],
        [Button.inline("📡 Статус слушателя", b"listener_status")],
    ]
    if bs.is_listening:
        buttons.append([Button.inline("⏸ Пауза", b"pause")])
    else:
        buttons.append([Button.inline("▶️ Возобновить", b"resume")])
    buttons.append([Button.inline("❓ Помощь", b"help")])
    return buttons


async def send_main_menu(client, chat_id):
    text = "🤖 **BanditTour Lead Scanner**\n\nГлавное меню — выберите действие:"
    await client.send_message(chat_id, text, buttons=main_menu_buttons(), parse_mode="md")


async def cmd_stats(event):
    t = bs.stats_today
    total = bs.stats_total
    text = f"""📊 **Статистика**

**Сегодня:**
  🔥 Hot: {t.get('hot', 0)}
  🌤 Warm: {t.get('warm', 0)}
  🗑 Spam: {t.get('spam', 0)}
  ❌ Noise: {t.get('noise', 0)}
  ⚠ Ошибки: {t.get('error', 0) + t.get('rate_limited', 0)}

**За сессию:**
  🔥 Hot: {total.get('hot', 0)}
  🌤 Warm: {total.get('warm', 0)}
  🗑 Spam: {total.get('spam', 0)}
  ❌ Noise: {total.get('noise', 0)}

**Хранилище:**
  📁 Всего лидов: {len(bs.leads)}
  📡 Слушатель: {'▶️ активен' if bs.is_listening else '⏸ на паузе'}"""
    await event.edit(text, parse_mode="md", buttons=[Button.inline("◀️ Назад", b"menu")])


async def cmd_leads(event):
    recent = bs.leads[:10]
    if not recent:
        text = "📋 Лидов пока нет."
    else:
        lines = ["📋 **Последние 10 лидов:**\n"]
        for i, lead in enumerate(recent, 1):
            emoji = CATEGORY_EMOJI.get(lead.get("category", ""), "❓")
            date = lead.get("date", "?")[:16].replace("T", " ")
            preview = lead.get("text", "")[:80].replace("\n", " ")
            lines.append(f"{i}. {emoji} {date} | {lead.get('channel_title', '?')}\n   _{preview}..._")
        text = "\n".join(lines)
    await event.edit(text, parse_mode="md", buttons=[Button.inline("◀️ Назад", b"menu")])


async def cmd_listener_status(event):
    text = f"📡 Слушатель: {'▶️ активен' if bs.is_listening else '⏸ на паузе'}"
    await event.edit(text, buttons=[Button.inline("◀️ Назад", b"menu")])


async def cmd_pause(event):
    bs.is_listening = False
    await event.edit("⏸ Слушатель на паузе. Новые сообщения не обрабатываются.",
                     buttons=[Button.inline("◀️ Назад", b"menu")])


async def cmd_resume(event):
    bs.is_listening = True
    await event.edit("▶️ Слушатель возобновлён.",
                     buttons=[Button.inline("◀️ Назад", b"menu")])


async def cmd_help(event):
    text = """❓ **Помощь**

**Команды:**
• `/start` — главное меню
• `/stats` — статистика
• `/leads` — последние 10 лидов
• `/scan` — ручной скан
• `/pause` / `/resume` — управление слушателем

**Что делает бот:**
🎧 Слушает новые сообщения в каналах
🔍 Фильтрует по keywords (34+ слов)
🤖 Классифицирует через Gemini (только север Таиланда)
🎯 Присылает уведомления о новых лидах

**Файлы:**
• `matches_found.json` — все лиды
• `scan_state.json` — состояние
• `logs/bot.log` — логи"""
    await event.edit(text, parse_mode="md", buttons=[Button.inline("◀️ Назад", b"menu")])


async def main():
    global bs
    config = load_config()
    api_id = config["api_id"]
    api_hash = config["api_hash"]
    channels = config["channels"]
    bot_token = config.get("bot_token")
    notify_chat_id = config.get("notify_chat_id")

    if not bot_token or not notify_chat_id:
        log.error("bot_token или notify_chat_id не заданы в test_config.json!")
        log.error("Добавьте эти поля в конфиг. См. config.example.json")
        sys.exit(1)

    bs = BotState()

    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.start(bot_token=bot_token)

    me = await client.get_me()
    log.info(f"Бот запущен как @{me.username} (id={me.id})")

    for channel in channels:
        try:
            entity = await client.get_entity(channel)
            title = getattr(entity, "title", channel)
            log.info(f"🎧 Слушаю канал: {title} ({channel})")

            @client.on(events.NewMessage(chats=entity))
            async def handler(event):
                await process_new_message(client, event)
        except Exception as e:
            log.error(f"Не удалось подписаться на {channel}: {e}")

    @client.on(events.NewMessage(pattern="/start"))
    async def start_handler(event):
        if event.is_private:
            await send_main_menu(client, event.chat_id)

    @client.on(events.CallbackQuery)
    async def callback_handler(event):
        data = event.data.decode("utf-8")
        if data == "menu":
            await send_main_menu(client, event.chat_id)
            await event.answer()
        elif data == "stats":
            await cmd_stats(event)
            await event.answer()
        elif data == "leads":
            await cmd_leads(event)
            await event.answer()
        elif data == "listener_status":
            await cmd_listener_status(event)
            await event.answer()
        elif data == "pause":
            await cmd_pause(event)
            await event.answer()
        elif data == "resume":
            await cmd_resume(event)
            await event.answer()
        elif data == "help":
            await cmd_help(event)
            await event.answer()

    log.info("✅ Бот готов. Нажмите Ctrl+C для остановки.")
    log.info(f"📝 Логи: {LOG_PATH}")

    try:
        startup_text = f"""🚀 **BanditTour Lead Scanner запущен**

📡 Каналов: {len(channels)}
🔍 Keywords: {len(config['keywords'])}
🤖 Gemini: {config.get('gemini_model', 'gemini-2.5-flash-lite')}
📁 Лидов в файле: {len(bs.leads)}

Отправьте /start для главного меню."""
        await client.send_message(int(notify_chat_id), startup_text, parse_mode="md")
    except Exception as e:
        log.error(f"Не удалось отправить стартовое сообщение: {e}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен пользователем")
    except Exception as e:
        log.error(f"Фатальная ошибка: {e}", exc_info=True)
        sys.exit(1)