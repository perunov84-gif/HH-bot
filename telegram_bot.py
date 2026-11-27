import os
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def get_inline_keyboard():
    """Returns interactive inline keyboard buttons attached to messages."""
    return {
        "inline_keyboard": [
            [{"text": "🚀 Запустить HH Auto Agent", "callback_data": "run_agent"}],
            [{"text": "🔝 Поднять резюме в поиске", "callback_data": "bump_resume"}],
            [{"text": "💬 Проверить чаты и автоответы", "callback_data": "check_chats"}],
            [
                {"text": "📊 Статистика за сегодня", "callback_data": "stats"},
                {"text": "⚙️ Проверить статус", "callback_data": "status"}
            ]
        ]
    }


def get_reply_keyboard():
    """Returns persistent reply keyboard under chat input."""
    return {
        "keyboard": [
            [{"text": "🚀 HH Auto Agent"}],
            [{"text": "🔝 Поднять резюме в поиске"}],
            [{"text": "💬 Проверить чаты и автоответы"}],
            [{"text": "📊 Статистика за сегодня"}, {"text": "⚙️ Проверить статус"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


def send_telegram_message(message: str, reply_markup: dict = None, retries: int = 3) -> bool:
    """Sends HTML-formatted message to Telegram with optional keyboard and auto-retry."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[Telegram] Ошибка: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не настроены в .env")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=20)
            response_data = response.json()
            if response_data.get("ok"):
                print("[Telegram] Сообщение успешно отправлено в Telegram.")
                return True
            else:
                print(f"[Telegram] Ошибка API Telegram: {response_data.get('description')}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"[Telegram] Попытка {attempt}/{retries} не удалась: {e}")
            if attempt < retries:
                time.sleep(2)

    return False


def answer_callback_query(callback_query_id: str, text: str = None) -> bool:
    """Acknowledges an inline button click."""
    if not BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload, timeout=8)
        return True
    except Exception:
        return False


def get_updates(offset: int = 0, timeout: int = 15) -> list:
    """Fetches updates from Telegram bot using long-polling with proper socket timeout."""
    if not BOT_TOKEN:
        return []

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {
        "offset": offset,
        "timeout": timeout
    }

    try:
        response = requests.get(url, params=params, timeout=timeout + 20)
        data = response.json()
        if data.get("ok"):
            return data.get("result", [])
    except requests.exceptions.Timeout:
        # Standard long-polling timeout (no messages during window)
        return []
    except Exception as e:
        print(f"[Telegram Polling] Ошибка получения обновлений: {e}")

    return []


def format_daily_report(
    invitations_yesterday: list,
    applied_today: list,
    errors: list = None
) -> str:
    from datetime import datetime, timedelta

    today_str = datetime.now().strftime("%d.%m.%Y")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")

    lines = [
        f"🤖 <b>Отчёт HH-агента за {today_str}</b>\n",
        f"📩 <b>Приглашений за вчера ({yesterday_str}):</b> {len(invitations_yesterday)}"
    ]

    if invitations_yesterday:
        for i, inv in enumerate(invitations_yesterday, start=1):
            title = inv.get("title", "Вакансия")
            company = inv.get("company", "Компания")
            lines.append(f"  {i}. {company} — {title}")
    lines.append("")

    lines.append(f"🚀 <b>Найдено и отправлено откликов сегодня:</b> {len(applied_today)}")
    if applied_today:
        for i, app in enumerate(applied_today[:15], start=1):
            title = app.get("title", "Вакансия")
            company = app.get("company", "Компания")
            url = app.get("url", "")
            if url:
                lines.append(f"  {i}. <a href='{url}'>{title}</a> ({company})")
            else:
                lines.append(f"  {i}. {title} ({company})")
        if len(applied_today) > 15:
            lines.append(f"  <i>...и ещё {len(applied_today) - 15} откликов</i>")

    if errors:
        lines.append(f"\n⚠️ <b>Предупреждения/Ошибки:</b>")
        for err in errors[:3]:
            lines.append(f"  • {err}")

    return "\n".join(lines)
