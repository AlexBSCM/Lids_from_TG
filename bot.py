"""\n\nBanditTour Lead Scanner Bot (hybrid: user-account + bot-account).\n"""

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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from collections import Counter, defaultdict
import os

# Настройка шрифта для русского
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


CLASSIFY_PROMPT = """Ты — ассистент, который отбирает лиды для турфирмы BanditTour.\n\nГЕОГРАФИЯ: нас интересует ТОЛЬКО север Таиланда:\n- Чиангмай (Chiang Mai), Чианграй (Chiang Rai), Пай (Pai)\n- Золотой треугольник (Golden Triangle), Мэхонгсон (Mae Hong Son)\nИСКЛЮЧЕНИЯ (это НЕ лиды, категория noise):\n- Паттайя, Бангкок, Пхукет, Самуи, Краби, Хуахин, Ко Самет, Пханган\n- любые другие регионы Таиланда вне севера\nКАТЕГОРИИ:\n- hot: человек прямо сейчас ищет экскурсию/тура/гида на севере Таиланда\n- warm: человек упоминает планы по северу Таиланда, но конкретного запроса пока нет\n- spam: повторяющиеся рекламные посты от турфирм/конкурентов\n- noise: не лид, упоминание в другом контексте, другие регионы\nСообщение из Telegram-чата:\n---\n{message}\n---\nОтветь СТРОГО в формате JSON (без markdown):\n{{"is_lead": true/false, "category": "hot|warm|spam|noise", "reason": "короткая причина на русском (до 100 символов)"}}\nis_lead=true только если category=hot или warm."""


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
        self.is_listening = True
        self.stats_today = {"hot": 0, "warm": 0, "spam": 0, "noise": 0, "rate_limited": 0, "error": 0}
        self.stats_total = dict(self.stats_today)
        self.user_client = None
        self.awaiting_channel_input = False

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

NOTIFY_TEMPLATE = """🎯 НОВЫЙ ЛИД — BanditTour\n\n📍 Канал: {channel}\n📅 Дата: {date}\n🏷 Категория: {category_emoji} {category}\n👤 Sender ID: {sender_id}\n🔢 Message ID: {msg_id}\n🤖 Причина: {reason}\n💬 Текст сообщения:\n---\n{text}\n---\n🔗 Ссылка: {link}"""


async def send_notification(lead):
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




def _get_category_stats():
    """Возвращает Counter по категориям."""
    from collections import Counter
    return Counter(l.get("category", "unknown") for l in bs.leads)


def _get_days_stats(days=14):
    """Возвращает list кортежей (date_str, count) за последние N дней."""
    from collections import Counter
    from datetime import datetime, timedelta
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
    """Возвращает list кортежей (channel_title, count) отсортированный по убыванию."""
    from collections import Counter
    counts = Counter(l.get("channel_title", l.get("channel", "?")) for l in bs.leads)
    return counts.most_common()


def generate_chart(data_type, chart_type):
    """Генерирует PNG-график.\n\n    data_type: categories | days | channels\n    chart_type: pie | bar | line\n    """
    try:
        if not bs.leads:
            return None

        chart_path = str(LOG_DIR / "chart.png")
        color_map = {"hot": "#ff6b6b", "warm": "#ffd93d", "spam": "#6c757d", "noise": "#adb5bd", "unknown": "#999"}
        emoji_map = {"hot": "🔥 Hot", "warm": "🌤 Warm", "spam": "🗑 Spam", "noise": "❌ Noise", "unknown": "❓ Unknown"}

        # === Данные: По категориям ===
        if data_type == "categories":
            stats = _get_category_stats()
            if not stats:
                return None
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
                for i, v in enumerate(sizes):
                    ax.text(i, v + 0.1, str(v), ha="center", fontsize=10)
            elif chart_type == "line":
                fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
                short_labels = [emoji_map.get(c, c) for c, n in stats.most_common()]
                ax.plot(short_labels, sizes, marker="o", linewidth=2, color="#4dabf7", markersize=10)
                ax.fill_between(range(len(sizes)), sizes, alpha=0.2, color="#4dabf7")
                ax.set_title("Тренд по категориям", fontsize=14, fontweight="bold")
                ax.set_ylabel("Количество")
                ax.grid(alpha=0.3)
                for i, v in enumerate(sizes):
                    ax.text(i, v + 0.3, str(v), ha="center", fontsize=10)
            else:
                return None

        # === Данные: По дням ===
        elif data_type == "days":
            stats = _get_days_stats(14)
            if not stats:
                return None
            labels = [d[5:] for d, _ in stats]  # MM-DD
            values = [n for _, n in stats]

            if chart_type == "pie":
                # Pie не очень подходит для дней, но покажем топ-7 + "остальные"
                sorted_pairs = sorted(zip(labels, values), key=lambda x: -x[1])
                top = sorted_pairs[:7]
                rest = sum(n for _, n in sorted_pairs[7:])
                if rest > 0:
                    top.append(("остальные", rest))
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
                    if val > 0:
                        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1, str(val), ha="center", fontsize=9)
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
                    if v > 0:
                        ax.text(i, v + 0.1, str(v), ha="center", fontsize=9)
            else:
                return None

        # === Данные: По каналам ===
        elif data_type == "channels":
            stats = _get_channels_stats()
            if not stats:
                return None

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
                for bar, val in zip(bars, values):
                    ax.text(bar.get_width() + 0.1, bar.get_y() + bar.get_height() / 2, str(val), va="center", fontsize=10)
            elif chart_type == "line":
                # Накопительный тренд по каналам (по каждому каналу — линия)
                from collections import defaultdict
                from datetime import datetime, timedelta
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
                    ax.plot([d[5:] for d in days_list], values, marker="o", linewidth=2,
                            color=colors_list[i % len(colors_list)], label=short_name, markersize=6)
                ax.set_title("Тренд лидов по каналам (14 дней)", fontsize=14, fontweight="bold")
                ax.set_xlabel("Дата")
                ax.set_ylabel("Количество лидов")
                ax.grid(alpha=0.3)
                ax.legend(loc="upper left", fontsize=9)
                plt.xticks(rotation=45, ha="right")
            else:
                return None
        else:
            return None

        plt.savefig(chart_path, dpi=100)
        plt.close()
        return chart_path
    except Exception as e:
        log.error(f"Ошибка генерации графика: {e}")
        import traceback
        log.error(traceback.format_exc())
        return None


def main_menu_buttons():
    buttons = [
        [Button.inline("📊 Статистика", b"stats")],
        [Button.inline("📋 Последние лиды", b"leads")],
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
    text = "🤖 **BanditTour Lead Scanner**\n\nГлавное меню — выберите действие:"
    await bot_client.send_message(chat_id, text, buttons=main_menu_buttons(), parse_mode="md")


async def cmd_stats(event):
    t = bs.stats_today
    total = bs.stats_total
    text = f"""📊 **Статистика**\n\n\n\n**Сегодня:**\n  🔥 Hot: {t.get('hot', 0)}\n  🌤 Warm: {t.get('warm', 0)}\n  🗑 Spam: {t.get('spam', 0)}\n  ❌ Noise: {t.get('noise', 0)}\n  ⚠ Ошибки: {t.get('error', 0) + t.get('rate_limited', 0)}\n**За сессию:**\n  🔥 Hot: {total.get('hot', 0)}\n  🌤 Warm: {total.get('warm', 0)}\n  🗑 Spam: {total.get('spam', 0)}\n  ❌ Noise: {total.get('noise', 0)}\n**Хранилище:**\n  📁 Всего лидов: {len(bs.leads)}\n  📡 Слушатель: {'▶️ активен' if bs.is_listening else '⏸ на паузе'}\n  📡 Каналов: {len(bs.config['channels'])}"""
    await event.edit(text, parse_mode="md", buttons=[Button.inline("◀️ Назад", b"menu")])



async def cmd_channels(event):
    channels = bs.config["channels"]
    if not channels:
        text = "📡 Каналов нет. Добавьте через «➕ Добавить канал»."
        await event.edit(text, buttons=[Button.inline("◀️ Назад", b"menu")])
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
    text = """➕ **Добавление канала**\n\n\n\nОтправьте @username канала (например, `@chiangmai_chat`) или ID канала.\n⚠️ Требования:\n• Канал должен быть публичным (иметь @username)\n• User-аккаунт должен иметь к нему доступ\nДля отмены нажмите кнопку ниже."""
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

    await event.reply(
        f"✅ Канал `{text}` ({info}) добавлен и слушается!\n\n"
        f"Всего каналов: {len(bs.config['channels'])}",
        parse_mode="md",
        buttons=[Button.inline("◀️ В меню", b"menu")]
    )


async def cmd_delete_channel(event, channel):
    if channel in bs.config["channels"]:
        bs.config["channels"].remove(channel)
        save_config(bs.config)
        bs.reload_config()
        await event.edit(
            f"✅ Канал `{channel}` удалён.\n⚠ Бот продолжит слушать его до перезапуска.",
            parse_mode="md",
            buttons=[Button.inline("◀️ Назад", b"channels")]
        )
    else:
        await event.edit(f"⚠ Канал `{channel}` не найден.", parse_mode="md",
                         buttons=[Button.inline("◀️ Назад", b"channels")])


async def cmd_listener_status(event):
    text = f"📡 Слушатель: {'▶️ активен' if bs.is_listening else '⏸ на паузе'}\n📡 Каналов: {len(bs.config['channels'])}"
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
    text = """❓ **Помощь**\n\n\n\n**Команды:**\n• `/start` — главное меню\n• `/stats` — статистика\n• `/leads` — последние 10 лидов\n**Управление каналами:**\n• 📡 Список каналов — посмотреть и удалить\n• ➕ Добавить канал — добавить новый через @username\n**Что делает бот:**\n🎧 User-аккаунт слушает каналы\n🔍 Фильтрует по keywords (34+ слов)\n🤖 Классифицирует через Gemini (только север Таиланда)\n🎯 Bot-аккаунт присылает уведомления о новых лидах"""
    await event.edit(text, parse_mode="md", buttons=[Button.inline("◀️ Назад", b"menu")])




async def cmd_analytics_menu(event):
    """Показывает подменю аналитики — выбор данных."""
    text = "📈 **Аналитика**\n\n\n\n**Шаг 1:** Выберите данные для графика:"
    buttons = [
        [Button.inline("🥧 По категориям", b"data:categories")],
        [Button.inline("📅 По дням (14 дней)", b"data:days")],
        [Button.inline("📡 По каналам", b"data:channels")],
        [Button.inline("◀️ Назад в меню", b"menu")],
    ]
    await event.edit(text, parse_mode="md", buttons=buttons)


async def cmd_analytics_chart_type(event, data_type):
    """Показывает выбор типа графика для выбранных данных."""
    names = {
        "categories": "🥧 По категориям",
        "days": "📅 По дням (14 дней)",
        "channels": "📡 По каналам",
    }
    text = f"📈 **Аналитика → {names.get(data_type, data_type)}**\n\n\n\n**Шаг 2:** Выберите тип графика:"

    # Для разных данных — разные доступные типы
    if data_type == "categories":
        buttons = [
            [Button.inline("🥧 Круговая", b"chart:categories:pie"),
             Button.inline("📊 Столбчатая", b"chart:categories:bar")],
            [Button.inline("📈 Линейная", b"chart:categories:line")],
        ]
    elif data_type == "days":
        buttons = [
            [Button.inline("📊 Столбчатая", b"chart:days:bar"),
             Button.inline("📈 Линейная", b"chart:days:line")],
            [Button.inline("🥧 Круговая (топ-7)", b"chart:days:pie")],
        ]
    elif data_type == "channels":
        buttons = [
            [Button.inline("🥧 Круговая", b"chart:channels:pie"),
             Button.inline("📊 Столбчатая", b"chart:channels:bar")],
            [Button.inline("📈 Линейная", b"chart:channels:line")],
        ]
    else:
        buttons = []

    buttons.append([Button.inline("◀️ Назад к данным", b"analytics")])
    await event.edit(text, parse_mode="md", buttons=buttons)


async def cmd_analytics_generate(event, data_type, chart_type):
    """Генерирует и отправляет график."""
    names = {
        ("categories", "pie"): "🥧 Распределение по категориям",
        ("categories", "bar"): "📊 Количество по категориям",
        ("categories", "line"): "📈 Тренд по категориям",
        ("days", "pie"): "🥧 Топ-7 дней по лидам",
        ("days", "bar"): "📊 Лиды по дням (14 дней)",
        ("days", "line"): "📈 Тренд лидов по дням",
        ("channels", "pie"): "🥧 Доли каналов",
        ("channels", "bar"): "📊 Лиды по каналам",
        ("channels", "line"): "📈 Тренд по каналам",
    }
    title = names.get((data_type, chart_type), f"{data_type} / {chart_type}")

    await event.edit(f"⏳ Генерирую график...")

    if not bs.leads:
        await event.edit("⚠ Лидов пока нет. График недоступен.",
                         buttons=[Button.inline("◀️ Назад", b"analytics")])
        return

    chart_path = generate_chart(data_type, chart_type)
    if chart_path and os.path.exists(chart_path):
        await bot_client.send_file(
            event.chat_id,
            chart_path,
            caption=f"📈 {title}\n\n\n\nВсего лидов: {len(bs.leads)}",
        )
        # Возвращаем меню аналитики
        await cmd_analytics_menu(event)
    else:
        await event.edit("⚠ Не удалось сгенерировать график. Возможно, недостаточно данных.",
                         buttons=[Button.inline("◀️ Назад", b"analytics")])



# Глобальное состояние: какой лид сейчас просматривает каждый chat_id
_lead_view_state = {}  # {chat_id: current_index}


def format_lead_card(lead, index, total):
    emoji = {"hot": "🔥", "warm": "🌦", "spam": "🗑", "noise": "❌"}.get(lead.get("category", ""), "❓")
    date = lead.get("date", "?")[:16].replace("T", " ")
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
    text_out = f"{emoji} **{lead.get(chr(39) + chr(99) + chr(97) + chr(116) + chr(101) + chr(103) + chr(111) + chr(114) + chr(121) + chr(39), chr(63)).upper()}** | {date}" + chr(10)
    text_out += f"📍 {channel_title} (`{channel}`)" + chr(10)
    text_out += f"👤 Sender ID: `{sender_id}`" + chr(10)
    text_out += f"🔢 Message ID: `{msg_id}`" + chr(10)
    if reason:
        text_out += f"🤖 Причина: {reason}" + chr(10)
    text_out += chr(10) + "💬 **Текст сообщения:**" + chr(10) + "```" + chr(10) + preview_text + chr(10) + "```"
    text_out += chr(10) + chr(10) + f"_Лид {index + 1} из {total}_"
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
    sender_id = lead.get("sender_id")
    channel = lead.get("channel", "").lstrip("@")
    if channel and lead.get("id"):
        link = f"https://t.me/{channel}/{lead.get(chr(105) + chr(100))}"
        buttons.append([Button.url("🔗 Открыть в канале", link)])
    if username:
        buttons.append([Button.url("💬 Написать автору", f"https://t.me/{username}")])
    if sender_id:
        buttons.append([Button.inline("👤 Открыть чат с автором", f"lead_author:{index}".encode("utf-8"))])
    buttons.append([Button.inline("◀️ В меню", b"menu")])
    return buttons


async def cmd_leads(event, index=0):
    if not bs.leads:
        await event.edit("📋 Лидов пока нет.", buttons=[Button.inline("◀️ Назад", b"menu")])
        return
    _lead_view_state[event.chat_id] = index
    total = len(bs.leads)
    if index >= total:
        index = total - 1
    if index < 0:
        index = 0
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
    """Отправляет полное сообщение лида отдельным сообщением."""
    if index >= len(bs.leads):
        await event.answer("Лид не найден", alert=True)
        return

    lead = bs.leads[index]
    full_text = lead.get("text", "")
    emoji = {"hot": "🔥", "warm": "🌤", "spam": "🗑", "noise": "❌"}.get(lead.get("category", ""), "❓")
    date = lead.get("date", "?")[:16].replace("T", " ")
    channel_title = lead.get("channel_title", lead.get("channel", "?"))

    # Разбиваем длинный текст на части (Telegram лимит ~4096 символов)
    header = f"{emoji} **ПОЛНОЕ СООБЩЕНИЕ** | {date}\n\n📍 {channel_title}\n```\n"
    footer = "\n\n```"

    max_chunk = 4000 - len(header) - len(footer)
    chunks = [full_text[i:i+max_chunk] for i in range(0, len(full_text), max_chunk)]

    for i, chunk in enumerate(chunks):
        if i == 0:
            text_to_send = header + chunk + footer
        else:
            text_to_send = f"```\n\n{chunk}\n```"
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
        await bot_client.send_message(
            event.chat_id,
            f"\U0001f4ac Нажмите на ссылку, чтобы написать автору: https://t.me/{username}",
            link_preview=False
        )
    else:
        await event.answer("Пересылаю сообщение автора...", alert=False)
        try:
            channel_entity = await bs.user_client.get_entity(channel)
            await bs.user_client.forward_messages(
                entity=event.chat_id,
                messages=msg_id,
                from_peer=channel_entity
            )
            await bot_client.send_message(
                event.chat_id,
                "\u2705 Выше переслано сообщение от автора.\n\n\U0001f449 Нажмите на имя автора в пересланном сообщении, чтобы открыть его профиль и написать ему.",
                link_preview=False
            )
        except Exception as e:
            log.error(f"Ошибка пересылки: {e}")
            await bot_client.send_message(
                event.chat_id,
                f"\u26a0 Не удалось переслать сообщение.\n\nID автора: `{sender_id}`\n\nНайдите его через поиск в Telegram по ID.",
                parse_mode="md",
                link_preview=False
            )


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
        if not event.is_private:
            return
        if event.message.message and event.message.message.startswith("/"):
            return
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
        elif data == "help":
            await cmd_help(event)
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

    log.info("✅ Бот готов. Нажмите Ctrl+C для остановки.")
    log.info(f"📝 Логи: {LOG_PATH}")

    try:
        startup_text = f"""🚀 **BanditTour Lead Scanner запущен**\n\n📡 Каналов: {len(channels)}\n🔍 Keywords: {len(config['keywords'])}\n🤖 Gemini: {config.get('gemini_model', 'gemini-2.5-flash-lite')}\n📁 Лидов в файле: {len(bs.leads)}\nОтправьте /start для главного меню."""
        await bot_client.send_message(int(notify_chat_id), startup_text, parse_mode="md")
    except Exception as e:
        log.error(f"Не удалось отправить стартовое сообщение: {e}")

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
