"""
BanditTour Lead Scanner Bot (hybrid: user-account + bot-account).
"""

import asyncio
import json
import re
import sys
import time
import logging
import warnings
import os
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

from google import generativeai as genai
from telethon import TelegramClient, events, Button

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from collections import Counter, defaultdict

plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Tahoma"]
plt.rcParams["axes.unicode_minus"] = False

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "test_config.json"
OUTPUT_PATH = BASE_DIR / "matches_found.json"
STATE_PATH = BASE_DIR / "scan_state.json"
SESSION_USER = str(BASE_DIR / "session_user")
SESSION_BOT = str(BASE_DIR / "session_bot")
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

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

def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_leads():
    if OUTPUT_PATH.exists():
        try:
            data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                data = [data]
            return data
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
- Чиангмай (Chiang Mai), Чианграй (Chiang Rai), Пай (Pai)
- Золотой треугольник (Golden Triangle), Мэхонгсон (Mae Hong Son)

ИСКЛЮЧЕНИЯ (это НЕ лиды, категория noise):
- Паттайя, Бангкок, Пхукет, Самуи, Краби, Хуахин, Ко Самет, Пханган
- любые другие регионы Таиланда вне севера

КАТЕГОРИИ (БУДЬ СТРОГИМ, не размечай warm без явных причин):

- hot: человек ПРЯМО СЕЙЧАС ищет экскурсию/тур/гида. Примеры:
  * "ищу гида по Чиангмаю"
  * "куда съездить на один день из Чиангмая?"
  * "посоветуйте тур на север"
  * "нужна экскурсия в Чианграй"

- warm: человек ПЛАНИРУЕТ ПОЕЗДКУ В БУДУЩЕМ на север Таиланда, но пока без конкретного запроса. Примеры:
  * "собираюсь в Чиангмай в ноябре, что посмотреть?"
  * "планирую тур на север, какие варианты?"
  * "буду в Пае через неделю, ищу компанию"
  НЕ СТАВЬ warm если человек просто живёт там, обсуждает жизнь, сравнивает с другими местами.

- noise: НЕ лид. Сюда относятся:
  * обсуждение жизни в Чиангмае (жильё, налоги, погода, цены)
  * упоминание Чиангмая в разговоре о другом
  * воспоминания о прошлых поездках без планов на новые
  * сравнения Чиангмая с другими местами
  * любые сообщения без явного намерения заказать тур/экскурсию/гида
  * сообщения про другие регионы Таиланда

- spam: рекламные посты от турфирм/конкурентов, коммерческие предложения

ВАЖНО: лучше отнести к noise, чем к warm. warm только если есть явное намерение поездки в будущем.

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

def classify_message(model, text, max_retries=5):
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
                wait = min(3 * (attempt + 1), 15)
                m = re.search(r"retry in ([\d.]+)s", msg.lower()) or re.search(r"seconds:\s*(\d+)", msg.lower())
                if m:
                    try:
                        parsed = int(float(m.group(1))) + 1
                        wait = min(parsed, 15)
                    except ValueError:
                        pass
                if attempt < max_retries - 1:
                    log.warning(f"  rate_limited, жду {wait}с (попытка {attempt+1}/{max_retries})")
                    # Неблокирующий sleep через threading.Event
                    import threading
                    threading.Event().wait(wait)
                    continue
                return {"is_lead": False, "category": "rate_limited", "reason": "лимит Gemini (5 попыток)"}
            return {"is_lead": False, "category": "error", "reason": f"{type(e).__name__}: {msg[:100]}"}
    return {"is_lead": False, "category": "error", "reason": "все попытки исчерпаны"}


class BotState:
    def __init__(self):
        self.config = load_config()
        self.patterns = build_patterns(self.config["keywords"])
        self.gemini = init_gemini(self.config["gemini_api_key"], self.config.get("gemini_model", "gemini-2.5-flash-lite"))
        self.leads = load_leads()
        self.known_ids = {x["id"] for x in self.leads}
        self.is_listening = True
        self.stats_today = {"hot": 0, "warm": 0, "spam": 0, "noise": 0, "rate_limited": 0, "error": 0}
        self.stats_total = dict(self.stats_today)
        self.user_client = None
        self.awaiting_channel_input = False
        self.scan_in_progress = False
        self.stop_scan = False
        self.state = load_state()
        self.last_auto_scan_date = None

    def reload_config(self):
        self.config = load_config()
        self.patterns = build_patterns(self.config["keywords"])

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
bot_client = None
notify_chat_id = None

CATEGORY_EMOJI = {"hot": "🔥", "warm": "🌤", "spam": "🗑", "noise": "❌"}

STATUS_EMOJI = {"new": "🆕", "contacted": "📞", "in_progress": "💬", "converted": "✅", "lost": "❌"}
STATUS_LABELS = {"new": "Новый", "contacted": "Связались", "in_progress": "В работе", "converted": "Клиент", "lost": "Отказ"}

NOTIFY_TEMPLATE = """🎯 НОВЫЙ ЛИД — BanditTour

📍 Канал: {channel}
📅 Дата: {date_part}
🕐 Время: {time_part}
🏷 Категория: {category_emoji} {category}
🔄 Статус: {status_emoji} {status_label}
👤 Sender ID: {sender_id}
🔢 Message ID: {msg_id}
🤖 Причина: {reason}

💬 Текст сообщения:
---
{text}
---
🔗 Ссылка: {link}"""


def fix_mojibake(text):
    if not text:
        return text
    if "Р" in text or "С" in text:
        try:
            return text.encode("windows-1251", errors="ignore").decode("utf-8", errors="replace")
        except:
            pass
    return text

async def send_notification(lead):
    try:
        date_str = lead.get("date", "?")
        if date_str and len(date_str) >= 16:
            try:
                dt = datetime.fromisoformat(date_str) + timedelta(hours=7)
                date_part = f"{dt.day:02d}.{dt.month:02d}.{str(dt.year)[2:]}"
                time_part = f"{dt.hour:02d}:{dt.minute:02d}"
            except:
                date_part = "?"
                time_part = "?"
        else:
            date_part = "?"
            time_part = "?"
            
        text = NOTIFY_TEMPLATE.format(
            channel=lead.get("channel_title", lead.get("channel", "?")),
            date_part=date_part,
            time_part=time_part,
            category_emoji=CATEGORY_EMOJI.get(lead.get("category", ""), "❓"),
            category=lead.get("category", "?").upper(),
            status_emoji=STATUS_EMOJI.get(lead.get("status", "new"), "❓"),
            status_label=STATUS_LABELS.get(lead.get("status", "new"), "new"),
            sender_id=lead.get("sender_id", "?"),
            msg_id=lead.get("id", "?"),
            reason=lead.get("reason", ""),
            text=lead.get("text", "")[:1500],
            link=f"https://t.me/{lead.get('channel', '').lstrip('@')}/{lead.get('id', '')}",
        )
        await bot_client.send_message(int(notify_chat_id), text, link_preview=False)
        return True
    except Exception as e:
        log.error(f"Ошибка отправки уведомления: {type(e).__name__}: {e}")
        return False


async def process_new_message(event):
    if not bs.is_listening:
        return
    msg = event.message
    text = msg.message or ""
    text = fix_mojibake(text)
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
            "status": "new",
            "reason": result["reason"],
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
        bs.add_lead(lead)
        await send_notification(lead)


async def subscribe_to_channel(user_client, channel):
    try:
        entity = await user_client.get_entity(channel)
        title = getattr(entity, "title", channel)

        @user_client.on(events.NewMessage(chats=entity))
        async def handler(event):
            await process_new_message(event)

        log.info(f"🎧 Слушаю канал: {title} ({channel})")
        return True, title
    except Exception as e:
        log.error(f"Не удалось подписаться на {channel}: {e}")
        return False, str(e)


_lead_view_state = {}

def format_lead_card(lead, index, total):
    emoji = {"hot": "🔥", "warm": "🌤", "spam": "🗑", "noise": "❌"}.get(lead.get("category", ""), "❓")
    date_str = lead.get("date", "")
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str) + timedelta(hours=7)
            date_part = f"{dt.day:02d}.{dt.month:02d}.{str(dt.year)[2:]}"
            time_part = f"{dt.hour:02d}:{dt.minute:02d}"
        except:
            date_part = "?"
            time_part = "?"
    else:
        date_part = "?"
        time_part = "?"
    status = lead.get("status", "new")
    status_emoji = STATUS_EMOJI.get(status, "❓")
    status_label = STATUS_LABELS.get(status, status)
    channel_title = lead.get("channel_title", lead.get("channel", "?"))
    channel = lead.get("channel", "")
    msg_id = lead.get("id", "?")
    sender_id = lead.get("sender_id", "?")
    reason = lead.get("reason", "")
    full_text = lead.get("text", "")
    if len(full_text) > 500:
        preview_text = full_text[:500] + "..."
        has_full = True
    else:
        preview_text = full_text
        has_full = False
    text_out = f"{emoji} **{lead.get('category', '?').upper()}** | {status_emoji} {status_label}\n"
    text_out += f"📅 {date_part}\n🕐 {time_part}\n"
    text_out += f"📍 {channel_title} (`{channel}`)\n"
    text_out += f"👤 Sender ID: `{sender_id}`\n"
    text_out += f"🔢 Message ID: `{msg_id}`\n"
    if reason:
        text_out += f"🤖 Причина: {reason}\n"
    text_out += f"\n💬 **Текст сообщения:**\n```\n{preview_text}\n```"
    text_out += f"\n\n_Лид {index + 1} из {total}_"
    return text_out, has_full

def get_lead_card_buttons(index, total, lead, has_full=True, username=None):
    buttons = []
    nav_row = [
        Button.inline("⬅️ Предыдущий", f"lead_prev:{index}".encode("utf-8")),
        Button.inline("Следующий ➡️", f"lead_next:{index}".encode("utf-8")),
    ]
    buttons.append(nav_row)
    if has_full:
        buttons.append([Button.inline("📖 Прочитать полностью", f"lead_full:{index}".encode("utf-8"))])
    buttons.append([Button.inline("🔄 Сменить статус", f"lead_status:{index}".encode("utf-8"))])
    sender_id = lead.get("sender_id")
    channel = lead.get("channel", "").lstrip("@")
    if channel and lead.get("id"):
        link = f"https://t.me/{channel}/{lead.get('id')}"
        buttons.append([Button.url("🔗 Открыть в канале", link)])
    if username:
        buttons.append([Button.url("💬 Написать автору", f"https://t.me/{username}")])
    if sender_id:
        buttons.append([Button.inline("💬 Открыть чат с автором", f"lead_author:{index}".encode("utf-8"))])
    buttons.append([Button.inline("◀️ В меню", b"menu")])
    return buttons

async def cmd_leads(event, index=0):
    if not bs.leads:
        await event.edit("📋 Лидов пока нет.", buttons=[Button.inline("◀️ Назад", b"menu")])
        return
    _lead_view_state[event.chat_id] = index
    total = len(bs.leads)
    if index >= total: index = total - 1
    if index < 0: index = 0
    lead = bs.leads[index]
    text, has_full = format_lead_card(lead, index, total)
    username = None
    sender_id = lead.get("sender_id")
    if sender_id and bs.user_client:
        try:
            entity = await bs.user_client.get_entity(sender_id)
            username = getattr(entity, "username", None)
        except Exception:
            username = None
    buttons = get_lead_card_buttons(index, total, lead, has_full, username)
    await event.edit(text, parse_mode="md", buttons=buttons)

async def cmd_leads_nav(event, action, current_index):
    total = len(bs.leads)
    if action == "prev":
        new_index = current_index - 1
        if new_index < 0:
            await event.answer("Это самый свежий лид", alert=True)
            return
    else:
        new_index = current_index + 1
        if new_index >= total:
            await event.answer("Это самый старый лид", alert=True)
            return
    await cmd_leads(event, new_index)

async def cmd_lead_full(event, index):
    if index >= len(bs.leads):
        await event.answer("Лид не найден", alert=True)
        return
    lead = bs.leads[index]
    full_text = lead.get("text", "")
    emoji = {"hot": "🔥", "warm": "🌤", "spam": "🗑", "noise": "❌"}.get(lead.get("category", ""), "❓")
    date_str = lead.get("date", "")
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str) + timedelta(hours=7)
            date_part = f"{dt.day:02d}.{dt.month:02d}.{str(dt.year)[2:]}"
            time_part = f"{dt.hour:02d}:{dt.minute:02d}"
        except:
            date_part = "?"
            time_part = "?"
    else:
        date_part = "?"
        time_part = "?"
    channel_title = lead.get("channel_title", lead.get("channel", "?"))
    header = f"{emoji} **ПОЛНОЕ СООБЩЕНИЕ**\n📅 {date_part} 🕐 {time_part}\n📍 {channel_title}\n\n```\n"
    footer = "\n```"
    max_chunk = 4000 - len(header) - len(footer)
    chunks = [full_text[i:i+max_chunk] for i in range(0, len(full_text), max_chunk)]
    for i, chunk in enumerate(chunks):
        if i == 0:
            text_to_send = header + chunk + footer
        else:
            text_to_send = f"```\n{chunk}\n```"
        await bot_client.send_message(event.chat_id, text_to_send, parse_mode="md")
        await asyncio.sleep(0.3)
    await event.answer("Полный текст отправлен выше ✓")

async def cmd_lead_author(event, index):
    if index >= len(bs.leads):
        await event.answer("Лид не найден", alert=True)
        return
    lead = bs.leads[index]
    sender_id = lead.get("sender_id")
    channel = lead.get("channel", "")
    msg_id = lead.get("id")
    if not sender_id or not channel or not msg_id:
        await event.answer("Нет данных об авторе", alert=True)
        return
    username = None
    if bs.user_client:
        try:
            entity = await bs.user_client.get_entity(sender_id)
            username = getattr(entity, "username", None)
        except Exception as e:
            log.warning(f"Не удалось получить автора {sender_id}: {e}")
    if username:
        await event.answer("Открываю чат с автором...", alert=False)
        await bot_client.send_message(event.chat_id, f"💬 Нажмите на ссылку, чтобы написать автору: https://t.me/{username}", link_preview=False)
    else:
        await event.answer("Пересылаю сообщение автора...", alert=False)
        try:
            channel_entity = await bs.user_client.get_entity(channel)
            await bs.user_client.forward_messages(entity=event.chat_id, messages=msg_id, from_peer=channel_entity)
            await bot_client.send_message(event.chat_id, "✅ Выше переслано сообщение от автора.\n\n👉 Нажмите на имя автора в пересланном сообщении, чтобы открыть его профиль и написать ему.", link_preview=False)
        except Exception as e:
            log.error(f"Ошибка пересылки: {e}")
            await bot_client.send_message(event.chat_id, f"⚠ Не удалось переслать сообщение.\n\nID автора: `{sender_id}`\n\nНайдите его через поиск в Telegram по ID.", parse_mode="md", link_preview=False)

async def cmd_lead_status_menu(event, index):
    if index >= len(bs.leads):
        await event.answer("Лид не найден", alert=True)
        return
    lead = bs.leads[index]
    current_status = lead.get("status", "new")
    text = f"🔄 **Смена статуса лида**\n\nТекущий статус: {STATUS_EMOJI.get(current_status, '❓')} {STATUS_LABELS.get(current_status, current_status)}\n\nВыберите новый статус:"
    buttons = [
        [Button.inline("🆕 Новый", f"set_status:{index}:new".encode("utf-8")),
         Button.inline("📞 Связались", f"set_status:{index}:contacted".encode("utf-8"))],
        [Button.inline("💬 В работе", f"set_status:{index}:in_progress".encode("utf-8")),
         Button.inline("✅ Клиент", f"set_status:{index}:converted".encode("utf-8"))],
        [Button.inline("❌ Отказ", f"set_status:{index}:lost".encode("utf-8"))],
        [Button.inline("◀️ Назад к лиду", f"leads_back:{index}".encode("utf-8"))],
    ]
    await event.edit(text, parse_mode="md", buttons=buttons)

async def cmd_set_status(event, index, new_status):
    if index >= len(bs.leads):
        await event.answer("Лид не найден", alert=True)
        return
    if new_status not in STATUS_EMOJI:
        await event.answer("Неверный статус", alert=True)
        return
    bs.leads[index]["status"] = new_status
    save_leads(bs.leads)
    await event.answer(f"Статус изменён на: {STATUS_LABELS[new_status]}", alert=True)
    await cmd_leads(event, index)


def _get_category_stats():
    return Counter(l.get("category", "unknown") for l in bs.leads)

def _get_days_stats(days=14):
    now = datetime.now(timezone.utc)
    days_list = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    counts = Counter()
    for l in bs.leads:
        date_str = l.get("date", "")
        if date_str:
            day = date_str[:10]
            if day in days_list:
                counts[day] += 1
    return [(d, counts.get(d, 0)) for d in days_list]

def _get_channels_stats():
    counts = Counter(l.get("channel_title", l.get("channel", "?")) for l in bs.leads)
    return counts.most_common()

def generate_chart(data_type, chart_type):
    try:
        if not bs.leads: return None
        chart_path = str(LOG_DIR / "chart.png")
        color_map = {"hot": "#ff6b6b", "warm": "#ffd93d", "spam": "#6c757d", "noise": "#adb5bd", "unknown": "#999"}
        emoji_map = {"hot": "🔥 Hot", "warm": "🌤 Warm", "spam": "🗑 Spam", "noise": "❌ Noise", "unknown": "❓ Unknown"}

        if data_type == "categories":
            stats = _get_category_stats()
            if not stats: return None
            labels = [f"{emoji_map.get(c, c)} ({n})" for c, n in stats.most_common()]
            sizes = [n for c, n in stats.most_common()]
            colors = [color_map.get(c, "#999") for c, n in stats.most_common()]
            if chart_type == "pie":
                fig, ax = plt.subplots(figsize=(8, 6), constrained_layout=True)
                ax.pie(sizes, labels=labels, colors=colors, autopct="%1.1f%%", startangle=90)
                ax.set_title("Распределение лидов по категориям", fontsize=14, fontweight="bold")
            elif chart_type == "bar":
                fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
                short_labels = [emoji_map.get(c, c) for c, n in stats.most_common()]
                ax.bar(short_labels, sizes, color=colors, edgecolor="black")
                ax.set_title("Количество лидов по категориям", fontsize=14, fontweight="bold")
                ax.set_ylabel("Количество")
                ax.grid(axis="y", alpha=0.3)
                for i, v in enumerate(sizes): ax.text(i, v + 0.1, str(v), ha="center", fontsize=10)
            elif chart_type == "line":
                fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
                short_labels = [emoji_map.get(c, c) for c, n in stats.most_common()]
                ax.plot(short_labels, sizes, marker="o", linewidth=2, color="#4dabf7", markersize=10)
                ax.fill_between(range(len(sizes)), sizes, alpha=0.2, color="#4dabf7")
                ax.set_title("Тренд по категориям", fontsize=14, fontweight="bold")
                ax.set_ylabel("Количество")
                ax.grid(alpha=0.3)
                for i, v in enumerate(sizes): ax.text(i, v + 0.3, str(v), ha="center", fontsize=10)
            else: return None

        elif data_type == "days":
            stats = _get_days_stats(14)
            if not stats: return None
            labels = [d[5:] for d, _ in stats]
            values = [n for _, n in stats]
            if chart_type == "pie":
                sorted_pairs = sorted(zip(labels, values), key=lambda x: -x[1])
                top = sorted_pairs[:7]
                rest = sum(n for _, n in sorted_pairs[7:])
                if rest > 0: top.append(("остальные", rest))
                pie_labels = [f"{l} ({n})" for l, n in top]
                pie_sizes = [n for _, n in top]
                fig, ax = plt.subplots(figsize=(9, 7), constrained_layout=True)
                ax.pie(pie_sizes, labels=pie_labels, autopct="%1.1f%%", startangle=90)
                ax.set_title("Лиды по дням (топ-7 + остальные)", fontsize=14, fontweight="bold")
            elif chart_type == "bar":
                fig, ax = plt.subplots(figsize=(11, 5), constrained_layout=True)
                bars = ax.bar(labels, values, color="#4dabf7", edgecolor="#1c7ed6")
                ax.set_title("Лиды по дням (14 дней)", fontsize=14, fontweight="bold")
                ax.set_xlabel("Дата")
                ax.set_ylabel("Количество лидов")
                ax.grid(axis="y", alpha=0.3)
                plt.xticks(rotation=45, ha="right")
                for bar, val in zip(bars, values):
                    if val > 0: ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1, str(val), ha="center", fontsize=9)
            elif chart_type == "line":
                fig, ax = plt.subplots(figsize=(11, 5), constrained_layout=True)
                ax.plot(labels, values, marker="o", linewidth=2, color="#4dabf7", markersize=8)
                ax.fill_between(range(len(labels)), values, alpha=0.2, color="#4dabf7")
                ax.set_title("Тренд лидов по дням (14 дней)", fontsize=14, fontweight="bold")
                ax.set_xlabel("Дата")
                ax.set_ylabel("Количество лидов")
                ax.grid(alpha=0.3)
                plt.xticks(rotation=45, ha="right")
                for i, v in enumerate(values):
                    if v > 0: ax.text(i, v + 0.1, str(v), ha="center", fontsize=9)
            else: return None

        elif data_type == "channels":
            stats = _get_channels_stats()
            if not stats: return None
            if chart_type == "pie":
                labels = [f"{(c[:20] + chr(8230) if len(c) > 20 else c)} ({n})" for c, n in stats]
                sizes = [n for _, n in stats]
                fig, ax = plt.subplots(figsize=(9, 7), constrained_layout=True)
                ax.pie(sizes, labels=labels, autopct="%1.1f%%", startangle=90)
                ax.set_title("Доли лидов по каналам", fontsize=14, fontweight="bold")
            elif chart_type == "bar":
                labels = [(c[:25] + "..." if len(c) > 25 else c) for c, _ in stats]
                values = [n for _, n in stats]
                fig, ax = plt.subplots(figsize=(10, max(4, len(labels) * 0.5)), constrained_layout=True)
                bars = ax.barh(labels, values, color="#51cf66", edgecolor="#2f9e44")
                ax.set_title("Лиды по каналам", fontsize=14, fontweight="bold")
                ax.set_xlabel("Количество лидов")
                ax.grid(axis="x", alpha=0.3)
                for bar, val in zip(bars, values): ax.text(bar.get_width() + 0.1, bar.get_y() + bar.get_height() / 2, str(val), va="center", fontsize=10)
            elif chart_type == "line":
                from collections import defaultdict
                now = datetime.now(timezone.utc)
                days_list = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
                by_channel = defaultdict(lambda: defaultdict(int))
                for l in bs.leads:
                    date_str = l.get("date", "")
                    if date_str:
                        day = date_str[:10]
                        if day in days_list:
                            ch = l.get("channel_title", l.get("channel", "?"))
                            by_channel[ch][day] += 1
                fig, ax = plt.subplots(figsize=(11, 5), constrained_layout=True)
                colors_list = ["#ff6b6b", "#4dabf7", "#51cf66", "#ffd93d", "#9775fa", "#ff922b"]
                for i, (ch, day_counts) in enumerate(by_channel.items()):
                    short_name = ch[:15] + "..." if len(ch) > 15 else ch
                    values = [day_counts.get(d, 0) for d in days_list]
                    ax.plot([d[5:] for d in days_list], values, marker="o", linewidth=2, color=colors_list[i % len(colors_list)], label=short_name, markersize=6)
                ax.set_title("Тренд лидов по каналам (14 дней)", fontsize=14, fontweight="bold")
                ax.set_xlabel("Дата")
                ax.set_ylabel("Количество лидов")
                ax.grid(alpha=0.3)
                ax.legend(loc="upper left", fontsize=9)
                plt.xticks(rotation=45, ha="right")
            else: return None
        else: return None

        plt.savefig(chart_path, dpi=100)
        plt.close()
        return chart_path
    except Exception as e:
        log.error(f"Ошибка генерации графика: {e}")
        return None


def main_menu_buttons():
    buttons = [
        [Button.inline("📊 Статистика", b"stats"), Button.inline("🌐 Дашборд", b"dashboard")],
        [Button.inline("📋 Последние лиды", b"leads"), Button.inline("🔄 Сканировать", b"scan")],
        [Button.inline("📡 Список каналов", b"channels"), Button.inline("➕ Добавить канал", b"add_channel")],
        [Button.inline("📈 Аналитика", b"analytics"), Button.inline("📡 Статус слушателя", b"listener_status")],
    ]
    if bs.is_listening:
        buttons.append([Button.inline("⏸ Пауза", b"pause")])
    else:
        buttons.append([Button.inline("▶️ Возобновить", b"resume")])
    buttons.append([Button.inline("❓ Помощь", b"help")])
    return buttons

async def send_main_menu(chat_id):
    text = """🤖 **BanditTour Lead Scanner**

Главное меню — выберите действие:"""
    await bot_client.send_message(chat_id, text, buttons=main_menu_buttons(), parse_mode="md")

async def cmd_dashboard(event):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "127.0.0.1"
    finally:
        s.close()
    url = f"http://{local_ip}:8080"
    text = f"🌐 **Web Dashboard**\n\nОткройте дашборд в браузере:\n{url}\n\n_Сервер должен быть запущен._"
    buttons = [
        [Button.url("📄 Открыть", url)],
        [Button.inline("◀️ В меню", b"menu")],
    ]
    await event.edit(text, parse_mode="md", buttons=buttons)

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
  📡 Слушатель: {'▶️ активен' if bs.is_listening else '⏸ на паузе'}
  📡 Каналов: {len(bs.config['channels'])}"""
    await event.edit(text, parse_mode="md", buttons=[Button.inline("◀️ Назад", b"menu")])

async def cmd_analytics_menu(event):
    text = """📈 **Аналитика**

**Шаг 1:** Выберите данные для графика:"""
    buttons = [
        [Button.inline("🥧 По категориям", b"data:categories")],
        [Button.inline("📅 По дням (14 дней)", b"data:days")],
        [Button.inline("📡 По каналам", b"data:channels")],
        [Button.inline("◀️ Назад в меню", b"menu")],
    ]
    await event.edit(text, parse_mode="md", buttons=buttons)

async def cmd_analytics_chart_type(event, data_type):
    names = {"categories": "🥧 По категориям", "days": "📅 По дням (14 дней)", "channels": "📡 По каналам"}
    text = f"""📈 **Аналитика → {names.get(data_type, data_type)}**

**Шаг 2:** Выберите тип графика:"""
    if data_type == "categories":
        buttons = [
            [Button.inline("🥧 Круговая", b"chart:categories:pie"), Button.inline("📊 Столбчатая", b"chart:categories:bar")],
            [Button.inline("📈 Линейная", b"chart:categories:line")],
        ]
    elif data_type == "days":
        buttons = [
            [Button.inline("📊 Столбчатая", b"chart:days:bar"), Button.inline("📈 Линейная", b"chart:days:line")],
            [Button.inline("🥧 Круговая (топ-7)", b"chart:days:pie")],
        ]
    elif data_type == "channels":
        buttons = [
            [Button.inline("🥧 Круговая", b"chart:channels:pie"), Button.inline("📊 Столбчатая", b"chart:channels:bar")],
            [Button.inline("📈 Линейная", b"chart:channels:line")],
        ]
    else:
        buttons = []
    buttons.append([Button.inline("◀️ Назад к данным", b"analytics")])
    await event.edit(text, parse_mode="md", buttons=buttons)

async def cmd_analytics_generate(event, data_type, chart_type):
    names = {
        ("categories", "pie"): "🥧 Распределение по категориям", ("categories", "bar"): "📊 Количество по категориям",
        ("categories", "line"): "📈 Тренд по категориям", ("days", "pie"): "🥧 Топ-7 дней по лидам",
        ("days", "bar"): "📊 Лиды по дням (14 дней)", ("days", "line"): "📈 Тренд лидов по дням",
        ("channels", "pie"): "🥧 Доли каналов", ("channels", "bar"): "📊 Лиды по каналам",
        ("channels", "line"): "📈 Тренд по каналам",
    }
    title = names.get((data_type, chart_type), f"{data_type} / {chart_type}")
    await event.edit(f"⏳ Генерирую график...")
    if not bs.leads:
        await event.edit("⚠ Лидов пока нет. График недоступен.", buttons=[Button.inline("◀️ Назад", b"analytics")])
        return
    chart_path = generate_chart(data_type, chart_type)
    if chart_path and os.path.exists(chart_path):
        await bot_client.send_file(event.chat_id, chart_path, caption=f"📈 {title}\n\nВсего лидов: {len(bs.leads)}")
        await cmd_analytics_menu(event)
    else:
        await event.edit("⚠ Не удалось сгенерировать график. Возможно, недостаточно данных.", buttons=[Button.inline("◀️ Назад", b"analytics")])

async def cmd_channels(event):
    channels = bs.config["channels"]
    if not channels:
        await event.edit("📡 Каналов нет. Добавьте через «➕ Добавить канал».", buttons=[Button.inline("◀️ Назад", b"menu")])
        return
    lines = ["📡 **Подключённые каналы:**\n"]
    buttons = []
    for i, ch in enumerate(channels, 1):
        lines.append(f"{i}. `{ch}`")
        buttons.append([Button.inline(f"🗑 Удалить {ch}", f"del_channel:{ch}".encode("utf-8"))])
    buttons.append([Button.inline("➕ Добавить канал", b"add_channel")])
    buttons.append([Button.inline("◀️ Назад", b"menu")])
    text = "\n".join(lines)
    await event.edit(text, parse_mode="md", buttons=buttons)

async def cmd_add_channel_start(event):
    bs.awaiting_channel_input = True
    text = """➕ **Добавление канала**

Отправьте @username канала (например, `@chiangmai_chat`) или ID канала.

⚠️ Требования:
• Канал должен быть публичным (иметь @username)
• User-аккаунт должен иметь к нему доступ

Для отмены нажмите кнопку ниже."""
    await event.edit(text, parse_mode="md", buttons=[Button.inline("❌ Отмена", b"cancel_add_channel")])

async def cmd_add_channel_cancel(event):
    bs.awaiting_channel_input = False
    await send_main_menu(event.chat_id)

async def handle_channel_input(event):
    text = event.message.message.strip()
    bs.awaiting_channel_input = False
    if not (text.startswith("@") or text.startswith("-")):
        await event.reply("⚠ Неверный формат. Отправьте @username (например, `@chiangmai_chat`).", parse_mode="md")
        await send_main_menu(event.chat_id)
        return
    if text in bs.config["channels"]:
        await event.reply(f"⚠ Канал `{text}` уже есть в списке.", parse_mode="md")
        await send_main_menu(event.chat_id)
        return
    await event.reply(f"🔄 Проверяю канал `{text}`...", parse_mode="md")
    ok, info = await subscribe_to_channel(bs.user_client, text)
    if not ok:
        await event.reply(f"❌ Не удалось подключиться к `{text}`:\n`{info}`", parse_mode="md")
        await send_main_menu(event.chat_id)
        return
    bs.config["channels"].append(text)
    save_config(bs.config)
    bs.reload_config()
    await event.reply(f"✅ Канал `{text}` ({info}) добавлен и слушается!\n\nВсего каналов: {len(bs.config['channels'])}", parse_mode="md", buttons=[Button.inline("◀️ В меню", b"menu")])

async def cmd_delete_channel(event, channel):
    if channel in bs.config["channels"]:
        bs.config["channels"].remove(channel)
        save_config(bs.config)
        bs.reload_config()
        await event.edit(f"✅ Канал `{channel}` удалён.\n⚠ Бот продолжит слушать его до перезапуска.", parse_mode="md", buttons=[Button.inline("◀️ Назад", b"channels")])
    else:
        await event.edit(f"⚠ Канал `{channel}` не найден.", parse_mode="md", buttons=[Button.inline("◀️ Назад", b"channels")])

async def cmd_listener_status(event):
    text = f"📡 Слушатель: {'▶️ активен' if bs.is_listening else '⏸ на паузе'}\n📡 Каналов: {len(bs.config['channels'])}"
    await event.edit(text, buttons=[Button.inline("◀️ Назад", b"menu")])

async def cmd_pause(event):
    bs.is_listening = False
    await event.edit("⏸ Слушатель на паузе. Новые сообщения не обрабатываются.", buttons=[Button.inline("◀️ Назад", b"menu")])

async def cmd_resume(event):
    bs.is_listening = True
    await event.edit("▶️ Слушатель возобновлён.", buttons=[Button.inline("◀️ Назад", b"menu")])

async def cmd_help(event):
    text = """❓ **Помощь**

**Команды:**
• `/start` — главное меню
• `/stats` — статистика
• `/leads` — последние 10 лидов
• `/pause` / `/resume` — управление слушателем

**Что делает бот:**
🎧 User-аккаунт слушает каналы
🔍 Фильтрует по keywords (34+ слов)
🤖 Классифицирует через Gemini (только север Таиланда)
🎯 Bot-аккаунт присылает уведомления о новых лидах

**Файлы:**
• `matches_found.json` — все лиды
• `logs/bot.log` — логи"""
    await event.edit(text, parse_mode="md", buttons=[Button.inline("◀️ Назад", b"menu")])

async def cmd_scan(event):
    if bs.scan_in_progress:
        await event.answer("Сканирование уже идёт", alert=True)
        return
    bs.scan_in_progress = True
    bs.stop_scan = False
    await event.edit("⏳ Анализирую каналы...")
    try:
        config = bs.config
        from datetime import datetime, timezone, timedelta
        initial_days = config.get("initial_scan_days", 60)
        min_date = datetime.now(timezone.utc) - timedelta(days=initial_days)
        preview = []
        total_msgs = 0
        for channel in config["channels"]:
            try:
                entity = await bs.user_client.get_entity(channel)
                title = getattr(entity, "title", channel)
                chan_state = bs.state.get(channel, {})
                last_id = chan_state.get("last_id", 0)
                is_first_scan = (last_id == 0)
                count = 0
                if is_first_scan:
                    async for msg in bs.user_client.iter_messages(entity, limit=2000, min_id=last_id):
                        if msg.date and msg.date < min_date: break
                        count += 1
                else:
                    async for msg in bs.user_client.iter_messages(entity, limit=2000, min_id=last_id):
                        count += 1
                preview.append({"channel": channel, "title": title, "count": count, "is_first": is_first_scan})
                total_msgs += count
            except Exception as e:
                log.error(f"Preview error {channel}: {e}")
                preview.append({"channel": channel, "title": channel, "count": 0, "is_first": True})

        estimated_leads = int(total_msgs * 0.07)
        estimated_time_min = int(estimated_leads * 5.5 / 60)

        out_lines = ["📊 **Прогноз сканирования**\n"]
        for pv in preview:
            mode_s = "первичный" if pv["is_first"] else "инкремент"
            est = int(pv["count"] * 0.07)
            short_t = pv["title"][:25]
            out_lines.append(f"{short_t} ({mode_s}): {pv['count']} msgs, ~{est}")
        out_lines.append("")
        out_lines.append(f"Всего сообщений: {total_msgs}")
        out_lines.append(f"Прогноз лидов: ~{estimated_leads}")
        out_lines.append(f"Прогноз времени: ~{estimated_time_min} мин")
        out_lines.append(f"Запросов Gemini: ~{estimated_leads}/1500")
        out_lines.append("")
        out_lines.append("Выберите режим:")
        text = "\n".join(out_lines)

        buttons = [
            [Button.inline("⚡ Быстро", b"scan_fast"), Button.inline("🐢 Постепенно", b"scan_slow"), Button.inline("🔀 Гибридно", b"scan_hybrid")],
            [Button.inline("◀️ Меню", b"menu")],
        ]
        await event.edit(text, buttons=buttons)
    except Exception as e:
        log.error(f"Preview error: {e}")
        await event.edit(f"Error: {e}", buttons=[Button.inline("◀️ Меню", b"menu")])
    finally:
        bs.scan_in_progress = False

async def cmd_scan_execute(event, mode):
    bs.scan_in_progress = True
    bs.stop_scan = False
    stop_btn = [Button.inline("⏹ Стоп", b"scan_stop")]
    await event.edit(f"⏳ Сканирую ({mode})...", buttons=stop_btn)
    try:
        config = bs.config
        from datetime import datetime, timezone, timedelta
        total_new = 0
        total_scanned = 0
        total_gemini = 0
        channels_processed = 0
        chs = config["channels"]
        total_channels = len(chs)
        scan_start_time = datetime.now(timezone.utc)

        last_edit_time = [0]
        spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spinner_idx = [0]
        progress_msg = event

        async def update_progress(title, status_line, chan_scanned=0, chan_leads=0, force=False):
            now_ts = datetime.now(timezone.utc).timestamp()
            if not force and now_ts - last_edit_time[0] < 2:
                return
            last_edit_time[0] = now_ts
            spinner = spinner_chars[spinner_idx[0] % len(spinner_chars)]
            spinner_idx[0] += 1
            elapsed = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
            if channels_processed > 0 and elapsed > 0:
                avg_per_chan = elapsed / channels_processed
                remaining_chans = total_channels - channels_processed
                eta_secs = int(avg_per_chan * remaining_chans)
                eta_mins = eta_secs // 60
                eta_s = eta_secs % 60
                eta = f"{eta_mins}m {eta_s}s"
            else:
                eta = "..."
            try:
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)
                pct = int((channels_processed / total_channels) * 100) if total_channels else 0
                bar_len = 20
                filled = int(bar_len * channels_processed / total_channels) if total_channels else 0
                bar = "▓" * filled + "░" * (bar_len - filled)
                text = (
                    f"🔄 *Скан в процессе* ({mode})\n\n"
                    f"{spinner} `{bar}` {channels_processed}/{total_channels} ({pct}%) - ETA: {eta}\n\n"
                    f"▶ Текущий: {title}\n"
                    f"  {status_line}\n\n"
                    f"✅ Готово каналов: {channels_processed}\n"
                    f"🎯 Найдено: {total_new} (+{chan_leads} в текущем)\n"
                    f"🤖 Gemini: {total_gemini}/1500\n"
                    f"📡 Проверено: {total_scanned + chan_scanned}\n"
                    f"⏱ Время: {mins}м {secs}с"
                )
                await progress_msg.edit(text, parse_mode="md", buttons=stop_btn)
            except Exception as e:
                log.debug(f"progress edit failed: {e}")

        for channel in chs:
            if bs.stop_scan: break
            try:
                entity = await bs.user_client.get_entity(channel)
                title = getattr(entity, "title", channel)
                chan_state = bs.state.get(channel, {})
                last_id = chan_state.get("last_id", 0)
                is_first_scan = (last_id == 0)
                initial_days = config.get("initial_scan_days", 60)
                min_date = None
                if is_first_scan:
                    min_date = datetime.now(timezone.utc) - timedelta(days=initial_days)
                log.info(f"Scan: {title}")
                candidates = []
                scanned = 0
                max_id_seen = last_id
                scan_limit = config.get("scan_limit", 1000)

                await update_progress(title, "📥 Скачиваю сообщения...", 0, 0, force=True)

                async for msg in bs.user_client.iter_messages(entity, limit=scan_limit, min_id=last_id):
                    if is_first_scan and min_date and msg.date:
                        if msg.date < min_date: break
                    scanned += 1
                    max_id_seen = max(max_id_seen, msg.id)
                    text = msg.message or ""
                    text = fix_mojibake(text)
                    if text_matches(text, bs.patterns):
                        candidates.append({
                            "id": msg.id, "date": msg.date.isoformat() if msg.date else None,
                            "text": text, "sender_id": msg.sender_id, "channel": channel, "channel_title": title,
                        })
                    if scanned % 25 == 0:
                        await update_progress(title, f"📥 Сканирую: {scanned} сообщ. | кандидатов: {len(candidates)}", scanned, 0)

                log.info(f"  Scanned: {scanned}, candidates: {len(candidates)}")
                new_leads_for_channel = 0
                if candidates:
                    total_candidates = len(candidates)
                    await update_progress(title, f"🤖 Классифицирую: 0/{total_candidates} | +0 лидов", scanned, 0, force=True)
                    for i, m in enumerate(candidates):
                        if bs.stop_scan: break
                        if bs.is_lead_known(m["id"]): continue
                        result = await asyncio.to_thread(classify_message, bs.gemini, m["text"])
                        cat = result["category"]
                        bs.add_stat(cat)
                        total_gemini += 1
                        log.info(f"  id={m['id']}: {cat}")
                        if result["is_lead"]:
                            m["category"] = cat
                            m["status"] = "new"
                            m["reason"] = result["reason"]
                            m["classified_at"] = datetime.now(timezone.utc).isoformat()
                            bs.add_lead(m)
                            await send_notification(m)
                            new_leads_for_channel += 1
                            total_new += 1

                        await update_progress(
                            title,
                            f"🤖 Классифицирую: {i+1}/{total_candidates} | +{new_leads_for_channel} лидов",
                            scanned, new_leads_for_channel
                        )
                        await asyncio.sleep(8.0)

                log.info(f"  New leads: {new_leads_for_channel}")
                bs.state[channel] = {
                    "last_id": max_id_seen,
                    "last_scan_at": datetime.now(timezone.utc).isoformat(),
                    "first_scan_at": chan_state.get("first_scan_at", datetime.now(timezone.utc).isoformat()),
                }
                save_state(bs.state)
                channels_processed += 1
                total_scanned += scanned

                await update_progress(title, f"✅ Готово (+{new_leads_for_channel} лидов, {scanned} сообщ.)", 0, new_leads_for_channel, force=True)

                if mode == "slow" and not bs.stop_scan:
                    await asyncio.sleep(30)
            except Exception as e:
                log.error(f"Scan error {channel}: {e}")

        elapsed = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        if bs.stop_scan:
            fin_text = f"⏹ *Остановлено*\n\n📡 Каналов: {channels_processed}/{len(chs)}\n🎯 Новых лидов: {total_new}\n🤖 Gemini: {total_gemini}/1500\n⏱ Время: {mins}м {secs}с"
        else:
            fin_text = (
                f"✅ *Скан завершён*\n\n"
                f"⏱ Время: {mins}м {secs}с\n"
                f"📡 Каналов: {channels_processed}/{len(chs)}\n"
                f"🎯 Новых лидов: {total_new}\n"
                f"🤖 Gemini: {total_gemini}/1500\n"
                f"📁 Всего в базе: {len(bs.leads)}"
            )
        await event.edit(fin_text, parse_mode="md", buttons=[Button.inline("◀️ В меню", b"menu")])

        await asyncio.sleep(3)
        try:
            await send_main_menu(event.chat_id)
        except Exception as e:
            log.warning(f"Не удалось показать меню после скана: {e}")
    except Exception as e:
        log.error(f"Scan execute error: {e}")
        await event.edit(f"Error: {e}", buttons=[Button.inline("◀️ В меню", b"menu")])
    finally:
        bs.scan_in_progress = False
        bs.stop_scan = False


async def check_and_run_auto_scan():
    """Проверяет, нужно ли запустить автоскан (1 раз в день при первом запуске)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if bs.last_auto_scan_date == today:
        return
    log.info(f"📅 Автоскан: первый запуск за день ({today})")
    bs.last_auto_scan_date = today

    fast_channels = []
    slow_channels = []
    for channel in bs.config["channels"]:
        chan_state = bs.state.get(channel, {})
        if chan_state.get("last_id", 0) > 0:
            fast_channels.append(channel)
        else:
            slow_channels.append(channel)

    chs = bs.config["channels"]
    total_channels = len(chs)
    scan_start_time = datetime.now(timezone.utc)

    progress_msg = None
    try:
        start_text = (
            f"📅 *Автоскан запущен*\n\n"
            f"⚡ Быстрых: {len(fast_channels)}\n"
            f"🐢 Медленных: {len(slow_channels)}\n\n"
            f"⏳ Подготовка..."
        )
        progress_msg = await bot_client.send_message(int(notify_chat_id), start_text, parse_mode="md")
    except Exception as e:
        log.warning(f"Не удалось создать прогресс-сообщение: {e}")

    total_new = 0
    total_gemini = 0
    total_scanned = 0
    channels_processed = 0

    last_edit_time = [0]
    spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    spinner_idx = [0]

    async def update_progress(chan_idx, title, status_line, chan_scanned=0, chan_leads=0, force=False):
        nonlocal progress_msg
        if progress_msg is None:
            return
        now_ts = datetime.now(timezone.utc).timestamp()
        if not force and now_ts - last_edit_time[0] < 2:
            return
        last_edit_time[0] = now_ts
        spinner = spinner_chars[spinner_idx[0] % len(spinner_chars)]
        spinner_idx[0] += 1
        elapsed = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
        if channels_processed > 0 and elapsed > 0:
            avg_per_chan = elapsed / channels_processed
            remaining_chans = total_channels - channels_processed
            eta_secs = int(avg_per_chan * remaining_chans)
            eta_mins = eta_secs // 60
            eta_s = eta_secs % 60
            eta = f"{eta_mins}m {eta_s}s"
        else:
            eta = "..."
        try:
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            pct = int((channels_processed / total_channels) * 100) if total_channels else 0
            bar_len = 20
            filled = int(bar_len * channels_processed / total_channels) if total_channels else 0
            bar = "▓" * filled + "░" * (bar_len - filled)
            text = (
                f"📅 *Автоскан в процессе*\n\n"
                f"{spinner} `{bar}` {channels_processed}/{total_channels} ({pct}%) - ETA: {eta}\n\n"
                f"▶ Текущий: {title}\n"
                f"  {status_line}\n\n"
                f"✅ Готово каналов: {channels_processed}\n"
                f"🎯 Лидов найдено: {total_new} (+{chan_leads} в текущем)\n"
                f"🤖 Gemini: {total_gemini}/1500\n"
                f"📡 Сообщений проверено: {total_scanned + chan_scanned}\n"
                f"⏱ Время: {mins}м {secs}с"
            )
            await progress_msg.edit(text, parse_mode="md")
        except Exception as e:
            log.debug(f"progress edit failed: {e}")

    for channel in fast_channels:
        if bs.stop_scan:
            break
        try:
            entity = await bs.user_client.get_entity(channel)
            title = getattr(entity, "title", channel)
            chan_state = bs.state.get(channel, {})
            last_id = chan_state.get("last_id", 0)
            log.info(f"⚡ Автоскан: {title}")
            candidates = []
            scanned = 0
            max_id_seen = last_id
            scan_limit = bs.config.get("scan_limit", 1000)

            chan_idx = channels_processed + 1
            await update_progress(chan_idx, title, "📥 Скачиваю сообщения...", 0, 0, force=True)

            async for msg in bs.user_client.iter_messages(entity, limit=scan_limit, min_id=last_id):
                scanned += 1
                max_id_seen = max(max_id_seen, msg.id)
                text = msg.message or ""
                text = fix_mojibake(text)
                if text_matches(text, bs.patterns):
                    candidates.append({
                        "id": msg.id,
                        "date": msg.date.isoformat() if msg.date else None,
                        "text": text,
                        "sender_id": msg.sender_id,
                        "channel": channel,
                        "channel_title": title
                    })
                if scanned % 25 == 0:
                    await update_progress(chan_idx, title, f"📥 Сканирую: {scanned} сообщ. | кандидатов: {len(candidates)}", scanned, 0)

            chan_new_leads = 0
            if candidates:
                total_candidates = len(candidates)
                await update_progress(chan_idx, title, f"🤖 Классифицирую: 0/{total_candidates} | +0 лидов", scanned, 0, force=True)
                for i, m in enumerate(candidates):
                    if bs.stop_scan:
                        break
                    if bs.is_lead_known(m["id"]):
                        continue
                    result = await asyncio.to_thread(classify_message, bs.gemini, m["text"])
                    cat = result["category"]
                    bs.add_stat(cat)
                    total_gemini += 1
                    if result["is_lead"]:
                        m["category"] = cat
                        m["status"] = "new"
                        m["reason"] = result["reason"]
                        m["classified_at"] = datetime.now(timezone.utc).isoformat()
                        bs.add_lead(m)
                        await send_notification(m)
                        total_new += 1
                        chan_new_leads += 1

                    await update_progress(
                        chan_idx, title,
                        f"🤖 Классифицирую: {i+1}/{total_candidates} | +{chan_new_leads} лидов",
                        scanned, chan_new_leads
                    )
                    await asyncio.sleep(8.0)

            bs.state[channel] = {
                "last_id": max_id_seen,
                "last_scan_at": datetime.now(timezone.utc).isoformat(),
                "first_scan_at": chan_state.get("first_scan_at", datetime.now(timezone.utc).isoformat())
            }
            save_state(bs.state)
            channels_processed += 1
            total_scanned += scanned
            await update_progress(chan_idx, title, f"✅ Готово (+{chan_new_leads} лидов, {scanned} сообщ.)", 0, chan_new_leads, force=True)
        except Exception as e:
            log.error(f"Автоскан error {channel}: {e}")

    if slow_channels and not bs.stop_scan:
        for channel in slow_channels:
            if bs.stop_scan:
                break
            try:
                entity = await bs.user_client.get_entity(channel)
                title = getattr(entity, "title", channel)
                chan_state = bs.state.get(channel, {})
                last_id = chan_state.get("last_id", 0)
                initial_days = bs.config.get("initial_scan_days", 60)
                min_date = datetime.now(timezone.utc) - timedelta(days=initial_days)
                log.info(f"🐢 Автоскан: {title} (первичный)")
                candidates = []
                scanned = 0
                max_id_seen = last_id
                scan_limit = bs.config.get("scan_limit", 1000)

                chan_idx = channels_processed + 1
                await update_progress(chan_idx, title, "📥 Скачиваю сообщения (первичный)...", 0, 0, force=True)

                async for msg in bs.user_client.iter_messages(entity, limit=scan_limit, min_id=last_id):
                    if msg.date and msg.date < min_date:
                        break
                    scanned += 1
                    max_id_seen = max(max_id_seen, msg.id)
                    text = msg.message or ""
                    text = fix_mojibake(text)
                    if text_matches(text, bs.patterns):
                        candidates.append({
                            "id": msg.id,
                            "date": msg.date.isoformat() if msg.date else None,
                            "text": text,
                            "sender_id": msg.sender_id,
                            "channel": channel,
                            "channel_title": title
                        })
                    if scanned % 25 == 0:
                        await update_progress(chan_idx, title, f"📥 Сканирую: {scanned} сообщ. | кандидатов: {len(candidates)}", scanned, 0)

                chan_new_leads = 0
                if candidates:
                    total_candidates = len(candidates)
                    await update_progress(chan_idx, title, f"🤖 Классифицирую: 0/{total_candidates} | +0 лидов", scanned, 0, force=True)
                    for i, m in enumerate(candidates):
                        if bs.stop_scan:
                            break
                        if bs.is_lead_known(m["id"]):
                            continue
                        result = await asyncio.to_thread(classify_message, bs.gemini, m["text"])
                        cat = result["category"]
                        bs.add_stat(cat)
                        total_gemini += 1
                        if result["is_lead"]:
                            m["category"] = cat
                            m["status"] = "new"
                            m["reason"] = result["reason"]
                            m["classified_at"] = datetime.now(timezone.utc).isoformat()
                            bs.add_lead(m)
                            await send_notification(m)
                            total_new += 1
                            chan_new_leads += 1

                        await update_progress(
                            chan_idx, title,
                            f"🤖 Классифицирую: {i+1}/{total_candidates} | +{chan_new_leads} лидов",
                            scanned, chan_new_leads
                        )
                        await asyncio.sleep(8.0)

                bs.state[channel] = {
                    "last_id": max_id_seen,
                    "last_scan_at": datetime.now(timezone.utc).isoformat(),
                    "first_scan_at": datetime.now(timezone.utc).isoformat()
                }
                save_state(bs.state)
                channels_processed += 1
                total_scanned += scanned
                await update_progress(chan_idx, title, f"✅ Готово (+{chan_new_leads} лидов, {scanned} сообщ.)", 0, chan_new_leads, force=True)
                if not bs.stop_scan:
                    await asyncio.sleep(30)
            except Exception as e:
                log.error(f"Автоскан error {channel}: {e}")

    try:
        elapsed = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        fin_text = (
            f"✅ *Автоскан завершён*\n\n"
            f"⏱ Время: {mins}м {secs}с\n"
            f"📡 Сообщений проверено: {total_scanned}\n"
            f"📡 Каналов: {channels_processed}/{total_channels}\n"
            f"🎯 Новых лидов: {total_new}\n"
            f"🤖 Gemini: {total_gemini}/1500\n"
            f"📁 Всего в базе: {len(bs.leads)}"
        )
        if progress_msg:
            await progress_msg.edit(fin_text, parse_mode="md")
        else:
            await bot_client.send_message(int(notify_chat_id), fin_text, parse_mode="md")
    except Exception as e:
        log.error(f"Не удалось отправить финальное сообщение: {e}")

    # Показать главное меню через 3 секунды
    await asyncio.sleep(3)
    try:
        await send_main_menu(int(notify_chat_id))
    except Exception as e:
        log.warning(f"Не удалось показать меню после автоскана: {e}")


async def main():
    global bs, bot_client, notify_chat_id

    config = load_config()
    api_id = config["api_id"]
    api_hash = config["api_hash"]
    channels = config["channels"]
    bot_token = config.get("bot_token")
    notify_chat_id = config.get("notify_chat_id")

    if not bot_token or not notify_chat_id:
        log.error("bot_token или notify_chat_id не заданы в test_config.json!")
        sys.exit(1)

    bs = BotState()

    user_client = TelegramClient(SESSION_USER, api_id, api_hash)
    log.info("Авторизация user-аккаунта...")
    await user_client.start()
    me_user = await user_client.get_me()
    log.info(f"✓ User: @{me_user.username} (id={me_user.id})")

    bot_client = TelegramClient(SESSION_BOT, api_id, api_hash)
    log.info("Авторизация bot-аккаунта...")
    await bot_client.start(bot_token=bot_token)
    me_bot = await bot_client.get_me()
    log.info(f"✓ Bot: @{me_bot.username} (id={me_bot.id})")

    bs.user_client = user_client

    for channel in channels:
        await subscribe_to_channel(user_client, channel)

    @bot_client.on(events.NewMessage(pattern="/start"))
    async def start_handler(event):
        if event.is_private:
            await send_main_menu(event.chat_id)

    @bot_client.on(events.NewMessage())
    async def text_handler(event):
        if not event.is_private: return
        if event.message.message and event.message.message.startswith("/"): return
        if bs.awaiting_channel_input:
            await handle_channel_input(event)

    @bot_client.on(events.CallbackQuery)
    async def callback_handler(event):
        data = event.data.decode("utf-8")
        if data == "menu":
            await send_main_menu(event.chat_id)
            await event.answer()
        elif data == "stats":
            await cmd_stats(event)
            await event.answer()
        elif data == "dashboard":
            await cmd_dashboard(event)
            await event.answer()
        elif data == "leads":
            await cmd_leads(event, 0)
            await event.answer()
        elif data.startswith("lead_prev:"):
            idx = int(data.split(":", 1)[1])
            await cmd_leads_nav(event, "prev", idx)
            await event.answer()
        elif data.startswith("lead_next:"):
            idx = int(data.split(":", 1)[1])
            await cmd_leads_nav(event, "next", idx)
            await event.answer()
        elif data.startswith("lead_full:"):
            idx = int(data.split(":", 1)[1])
            await cmd_lead_full(event, idx)
            await event.answer()
        elif data.startswith("lead_author:"):
            idx = int(data.split(":", 1)[1])
            await cmd_lead_author(event, idx)
            await event.answer()
        elif data.startswith("lead_status:"):
            idx = int(data.split(":", 1)[1])
            await cmd_lead_status_menu(event, idx)
            await event.answer()
        elif data.startswith("set_status:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                idx = int(parts[1])
                new_status = parts[2]
                await cmd_set_status(event, idx, new_status)
            await event.answer()
        elif data.startswith("leads_back:"):
            idx = int(data.split(":", 1)[1])
            await cmd_leads(event, idx)
            await event.answer()
        elif data == "channels":
            await cmd_channels(event)
            await event.answer()
        elif data == "add_channel":
            await cmd_add_channel_start(event)
            await event.answer()
        elif data == "cancel_add_channel":
            await cmd_add_channel_cancel(event)
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
        elif data == "analytics":
            await cmd_analytics_menu(event)
            await event.answer()
        elif data.startswith("data:"):
            data_type = data.split(":", 1)[1]
            await cmd_analytics_chart_type(event, data_type)
            await event.answer()
        elif data.startswith("chart:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                _, data_type, chart_type = parts
                await cmd_analytics_generate(event, data_type, chart_type)
            await event.answer()
        elif data.startswith("del_channel:"):
            channel = data.split(":", 1)[1]
            await cmd_delete_channel(event, channel)
            await event.answer()
        elif data == "scan":
            await cmd_scan(event)
            await event.answer()
        elif data == "scan_fast":
            await cmd_scan_execute(event, "fast")
            await event.answer()
        elif data == "scan_slow":
            await cmd_scan_execute(event, "slow")
            await event.answer()
        elif data == "scan_hybrid":
            await cmd_scan_execute(event, "hybrid")
            await event.answer()
        elif data == "scan_stop":
            bs.stop_scan = True
            await event.answer("Останавливаю...", alert=True)
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
        await bot_client.send_message(int(notify_chat_id), startup_text, parse_mode="md")
    except Exception as e:
        log.error(f"Не удалось отправить стартовое сообщение: {e}")

    # Запускаем автоскан (первый запуск за день)
    try:
        asyncio.create_task(check_and_run_auto_scan())
    except Exception as e:
        log.error(f"Автоскан не запустился: {e}")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен пользователем")
    except Exception as e:
        log.error(f"Фатальная ошибка: {e}", exc_info=True)
        sys.exit(1)