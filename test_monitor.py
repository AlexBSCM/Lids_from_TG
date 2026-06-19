"""
Сканер Telegram-каналов для BanditTour.

Особенности:
1. Поддержка нескольких каналов (channels: [...] в config.json).
2. Инкрементальное сканирование: для нового канала берёт последние N дней,
   для уже сканированного — только сообщения с id > last_id.
3. Keyword-фильтр с границами слов для коротких английских.
4. Gemini-классификатор с гео-правилом: только север Таиланда
   (Чиангмай, Чианграй, Пай, Золотой треугольник, Мэхонгсон).
   Сообщения про Паттайю/Бангкок/Пхукет/Самуи/Краби → noise.
5. Состояние сохраняется в scan_state.json (last_id по каждому каналу).
6. Найденные лиды сохраняются в matches_found.json с категорией hot/warm.
"""

import asyncio
import json
import re
import sys
import time
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

from google import generativeai as genai
from telethon import TelegramClient

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "test_config.json"
OUTPUT_PATH = BASE_DIR / "matches_found.json"
STATE_PATH = BASE_DIR / "scan_state.json"
SESSION_NAME = str(BASE_DIR / "session")


# ---------- Утилиты ----------

def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_patterns(keywords):
    """Компилируем keywords в regex с границами слов для коротких английских."""
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


# ---------- Gemini-классификатор ----------

CLASSIFY_PROMPT = """Ты — ассистент, который отбирает лиды для турфирмы BanditTour.

ГЕОГРАФИЯ: нас интересует ТОЛЬКО север Таиланда:
- Чиангмай (Chiang Mai)
- Чианграй (Chiang Rai)
- Пай (Pai)
- Золотой треугольник (Golden Triangle)
- Мэхонгсон (Mae Hong Son)
- и другие локации севера Таиланда

ИСКЛЮЧЕНИЯ (это НЕ лиды, категория noise):
- Паттайя (Pattaya)
- Бангкок (Bangkok)
- Пхукет (Phuket)
- Самуи (Koh Samui)
- Краби (Krabi)
- Хуахин (Hua Hin)
- Ко Самет, Пханган
- любые другие регионы Таиланда вне севера

КАТЕГОРИИ:
- hot: человек прямо сейчас ищет экскурсию/тура/гида на севере Таиланда,
       задаёт вопрос "куда поехать", "есть ли гиды", "посоветуйте экскурсии по Чиангмаю"
- warm: человек упоминает планы на будущее по северу Таиланда,
        интересуется Чиангмаем/Чианграем/Паем, но конкретного запроса пока нет
- spam: повторяющиеся рекламные посты от турфирм/конкурентов, коммерческие предложения
- noise: не лид (флуд, обсуждение без запроса, упоминание слова в другом контексте,
         сообщение про другие регионы Таиланда вне севера)

Сообщение из Telegram-чата:
---
{message}
---

Ответь СТРОГО в формате JSON (без markdown, без пояснений):
{{"is_lead": true/false, "category": "hot|warm|spam|noise", "reason": "короткая причина на русском (до 100 символов)"}}

is_lead=true только если category=hot или warm."""


def init_gemini(api_key, model_name):
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)


def classify_message(model, text, max_retries=3):
    """Возвращает dict: {is_lead, category, reason} или None при ошибке.
    При 429 (rate_limit) ждёт и пробует снова."""
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
                # Достаём retry_delay из ошибки
                wait = 30
                m = re.search(r"retry in ([\d.]+)s", msg.lower()) or re.search(r"seconds:\s*(\d+)", msg.lower())
                if m:
                    try:
                        wait = int(float(m.group(1))) + 2
                    except ValueError:
                        pass
                if attempt < max_retries - 1:
                    print(f"     ⏳ rate_limited, жду {wait}с и повторяю (попытка {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                return {"is_lead": False, "category": "rate_limited", "reason": "лимит Gemini после всех попыток"}
            return {"is_lead": False, "category": "error", "reason": f"{type(e).__name__}: {msg[:100]}"}

    return {"is_lead": False, "category": "error", "reason": "все попытки исчерпаны"}


# ---------- Уведомления ----------

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


CATEGORY_EMOJI = {
    "hot": "🔥",
    "warm": "🌤",
    "spam": "🗑",
    "noise": "❌",
}


async def send_notification(client, lead):
    """Отправляет уведомление о лиде в Saved Messages (me).
    Возвращает True при успехе, False при ошибке."""
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
        await client.send_message("me", text, link_preview=False)
        return True
    except Exception as e:
        print(f"  ⚠ Ошибка отправки уведомления: {type(e).__name__}: {e}")
        return False


# ---------- Сканирование одного канала ----------


async def scan_channel(client, channel, config, patterns, gemini_model, state):
    """Сканирует один канал, возвращает (new_leads, stats, max_id_seen, scanned)."""
    scan_limit = config.get("scan_limit", 1000)
    initial_days = config.get("initial_scan_days", 60)

    chan_state = state.get(channel, {})
    last_id = chan_state.get("last_id", 0)
    is_first_scan = (last_id == 0)

    min_date = None
    if is_first_scan:
        min_date = datetime.now(timezone.utc) - timedelta(days=initial_days)

    entity = await client.get_entity(channel)
    title = getattr(entity, "title", channel)
    mode = f"первичный скан за {initial_days} дней" if is_first_scan else f"инкремент (id > {last_id})"
    print(f"\n{'='*60}")
    print(f"КАНАЛ: {title}")
    print(f"Режим: {mode}")

    candidates = []
    scanned = 0
    max_id_seen = last_id

    async for msg in client.iter_messages(entity, limit=scan_limit, min_id=last_id):
        if is_first_scan and min_date and msg.date:
            if msg.date < min_date:
                break
        scanned += 1
        max_id_seen = max(max_id_seen, msg.id)
        text = msg.message or ""
        if text_matches(text, patterns):
            candidates.append({
                "id": msg.id,
                "date": msg.date.isoformat() if msg.date else None,
                "text": text,
                "sender_id": msg.sender_id,
                "channel": channel,
                "channel_title": title,
            })

    print(f"Просмотрено: {scanned}")
    print(f"Прошло keyword-фильтр: {len(candidates)}")

    if not candidates:
        return [], {"hot": 0, "warm": 0, "spam": 0, "noise": 0, "rate_limited": 0, "error": 0}, max_id_seen, scanned

    print("\nКлассификация через Gemini...")
    print("-" * 60)

    leads = []
    stats = {"hot": 0, "warm": 0, "spam": 0, "noise": 0, "rate_limited": 0, "error": 0}

    for i, m in enumerate(candidates, 1):
        preview = m["text"][:80].replace("\n", " ")
        print(f"  [{i}/{len(candidates)}] id={m['id']}: {preview}...")

        result = classify_message(gemini_model, m["text"])
        cat = result["category"]
        stats[cat] = stats.get(cat, 0) + 1
        print(f"     → {cat}: {result['reason']}")

        if result["is_lead"]:
            m["category"] = cat
            m["reason"] = result["reason"]
            m["classified_at"] = datetime.now(timezone.utc).isoformat()
            leads.append(m)

        # Пауза 5.5с (15 RPM лимит бесплатного Gemini, с запасом)
        time.sleep(5.5)

    return leads, stats, max_id_seen, scanned


# ---------- Основной цикл ----------

async def run():
    config = load_config()
    api_id = config["api_id"]
    api_hash = config["api_hash"]
    channels = config["channels"]
    keywords = config["keywords"]
    gemini_model_name = config.get("gemini_model", "gemini-2.5-flash-lite")

    patterns = build_patterns(keywords)
    gemini_model = init_gemini(config["gemini_api_key"], gemini_model_name)

    state = load_state()

    print(f"BanditTour — сканер Telegram-каналов")
    print(f"Каналов: {len(channels)}")
    print(f"Keywords: {len(keywords)}")
    print(f"Модель Gemini: {gemini_model_name}")
    print(f"Дата запуска: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.start()

    all_new_leads = []
    total_stats = {"hot": 0, "warm": 0, "spam": 0, "noise": 0, "rate_limited": 0, "error": 0}
    total_scanned = 0

    for channel in channels:
        try:
            leads, stats, max_id_seen, scanned = await scan_channel(
                client, channel, config, patterns, gemini_model, state
            )
            all_new_leads.extend(leads)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v
            total_scanned += scanned

            # Обновляем состояние для этого канала
            chan_state = state.get(channel, {})
            state[channel] = {
                "last_id": max_id_seen,
                "last_scan_at": datetime.now(timezone.utc).isoformat(),
                "first_scan_at": chan_state.get("first_scan_at", datetime.now(timezone.utc).isoformat()),
            }
            save_state(state)

            print(f"\n  → Канал {channel}: найдено лидов {len(leads)}, last_id → {max_id_seen}")

            # Отправляем уведомления о новых лидах
            if leads:
                print(f"  📨 Отправка уведомлений ({len(leads)} шт.)...")
                sent = 0
                for lead in leads:
                    ok = await send_notification(client, lead)
                    if ok:
                        sent += 1
                    # Пауза между уведомлениями, чтобы не получить flood_wait
                    await asyncio.sleep(1)
                print(f"  ✓ Уведомлений отправлено: {sent}/{len(leads)}")

        except Exception as e:
            print(f"\n  ✗ Ошибка при сканировании {channel}: {type(e).__name__}: {e}")

    await client.disconnect()

    # Сохраняем все лиды
    existing = []
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = []

    existing_ids = {x["id"] for x in existing}
    new_leads = [l for l in all_new_leads if l["id"] not in existing_ids]
    all_leads = existing + new_leads
    all_leads.sort(key=lambda x: x.get("date", ""), reverse=True)
    OUTPUT_PATH.write_text(
        json.dumps(all_leads, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Итог
    print(f"\n{'='*60}")
    print(f"ИТОГ ПО ВСЕМ КАНАЛАМ")
    print(f"{'='*60}")
    print(f"Просмотрено сообщений: {total_scanned}")
    print(f"Классификация Gemini:")
    for k, v in total_stats.items():
        if v > 0:
            print(f"  {k:15s}: {v}")
    print()
    print(f"Найдено новых лидов: {len(new_leads)} (hot+warm)")
    print(f"Всего лидов в matches_found.json: {len(all_leads)}")
    print(f"Состояние: {STATE_PATH}")
    print(f"Лиды:      {OUTPUT_PATH}")


if __name__ == "__main__":
    if not CONFIG_PATH.exists():
        print(f"[!] Не найден {CONFIG_PATH}.")
        sys.exit(1)
    asyncio.run(run())