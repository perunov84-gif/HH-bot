import os
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

import database
import telegram_bot
import agent
import chat_responder

load_dotenv(Path(__file__).parent / ".env")
LOG_PATH = Path(__file__).parent / "telegram_service.log"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

is_running = False
job_lock = threading.Lock()
log_lock = threading.Lock()


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    with log_lock:
        print(formatted, flush=True)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
        except Exception:
            pass


def run_chat_responder_in_background():
    global is_running
    try:
        log("Запуск Chat Responder в фоновом потоке...")
        telegram_bot.send_telegram_message("🔍 Начинаю проверку входящих сообщений и автоответов на HH.ru...")
        summary = chat_responder.run_chat_responder()
        telegram_bot.send_telegram_message(
            f"✅ <b>Проверка чатов завершена.</b>\n\n{summary}",
            reply_markup=telegram_bot.get_inline_keyboard()
        )
    except Exception as e:
        err = f"❌ Ошибка проверки чатов: {e}"
        log(err)
        telegram_bot.send_telegram_message(err, reply_markup=telegram_bot.get_inline_keyboard())
    finally:
        with job_lock:
            is_running = False
        log("Chat Responder завершил работу.")


def delayed_chat_check(delay_minutes: int = 25):
    """Waits for specified minutes and then checks chats automatically."""
    log(f"Автопроверка чатов запланирована через {delay_minutes} минут.")
    time.sleep(delay_minutes * 60)
    with job_lock:
        global is_running
        if is_running:
            return
        is_running = True
    log("Запуск отложенной проверки чатов...")
    run_chat_responder_in_background()


def run_agent_in_background():
    global is_running
    try:
        log("Запуск приоритетной проверки чатов перед поиском вакансий...")
        chat_summary = chat_responder.run_chat_responder()
        telegram_bot.send_telegram_message(
            f"💬 <b>Приоритетная проверка чатов завершена:</b>\n\n{chat_summary}"
        )

        log("Запуск Agent в фоновом потоке...")
        report = agent.run_agent_job()
        telegram_bot.send_telegram_message(
            "✅ <b>Работа HH Auto Agent завершена!</b>\n"
            "⏳ <i>Через 25 минут я автоматически проверю чаты на наличие вопросов от HR-ботов и автоответов.</i>",
            reply_markup=telegram_bot.get_inline_keyboard()
        )
        timer_thread = threading.Thread(target=delayed_chat_check, args=(25,), daemon=True)
        timer_thread.start()

    except Exception as e:
        err = f"❌ Ошибка при выполнении агента: {e}"
        log(err)
        telegram_bot.send_telegram_message(err, reply_markup=telegram_bot.get_inline_keyboard())
    finally:
        with job_lock:
            is_running = False
        log("Agent завершил работу.")


def trigger_agent_run():
    global is_running
    with job_lock:
        if is_running:
            telegram_bot.send_telegram_message(
                "⚠️ <b>Агент уже выполняет задачу прямо сейчас!</b>\n"
                "Пожалуйста, подождите завершения текущей сессии.",
                reply_markup=telegram_bot.get_inline_keyboard()
            )
            return
        is_running = True

    log("Получена команда на запуск HH Auto Agent!")
    telegram_bot.send_telegram_message(
        "🚀 <b>HH Auto Agent запущен!</b>\n"
        "Начинаю сбор вчерашних приглашений и поиск подходящих вакансий...\n"
        "<i>(Это займет несколько минут, по окончании пришлю полный отчет)</i>",
        reply_markup=telegram_bot.get_inline_keyboard()
    )

    worker = threading.Thread(target=run_agent_in_background, daemon=True)
    worker.start()


def run_bump_resume_in_background():
    global is_running
    try:
        log("Запуск поднятия резюме в фоновом потоке...")
        telegram_bot.send_telegram_message("⏳ Проверяю резюме на HH.ru и отправляю запрос на поднятие...")
        res = agent.bump_resume()
        msg = res.get("message", "Готово.")
        telegram_bot.send_telegram_message(msg, reply_markup=telegram_bot.get_inline_keyboard())
    except Exception as e:
        err = f"❌ Ошибка при поднятии резюме: {e}"
        log(err)
        telegram_bot.send_telegram_message(err, reply_markup=telegram_bot.get_inline_keyboard())
    finally:
        with job_lock:
            is_running = False
        log("Поднятие резюме завершено.")


def trigger_resume_bump():
    global is_running
    with job_lock:
        if is_running:
            telegram_bot.send_telegram_message(
                "⚠️ Браузер сейчас занят другой задачей. Пожалуйста, подождите.",
                reply_markup=telegram_bot.get_inline_keyboard()
            )
            return
        is_running = True

    log("Получена команда на поднятие резюме!")
    worker = threading.Thread(target=run_bump_resume_in_background, daemon=True)
    worker.start()


def trigger_chat_check():
    global is_running
    with job_lock:
        if is_running:
            telegram_bot.send_telegram_message(
                "⚠️ Браузер сейчас занят другой задачей. Пожалуйста, подождите.",
                reply_markup=telegram_bot.get_inline_keyboard()
            )
            return
        is_running = True

    log("Получена команда на проверку чатов!")
    worker = threading.Thread(target=run_chat_responder_in_background, daemon=True)
    worker.start()


def show_stats():
    database.init_db()
    applied_today = database.get_applied_today_count()
    remaining = max(0, agent.MAX_APPLIES_PER_DAY - applied_today)

    stats_text = (
        f"📊 <b>Статистика за сегодня ({datetime.now().strftime('%d.%m.%Y')}):</b>\n\n"
        f"• Отправлено откликов сегодня: <b>{applied_today}</b>\n"
        f"• Доступный остаток лимита на сегодня: <b>{remaining}</b> / {agent.MAX_APPLIES_PER_DAY}\n"
        f"• Статус авторизации: <i>Сессия активна</i>\n"
    )
    telegram_bot.send_telegram_message(stats_text, reply_markup=telegram_bot.get_inline_keyboard())


def show_status():
    with job_lock:
        current_status = "В процессе выполнения ⏳" if is_running else "В режиме ожидания 🟢"

    profile = chat_responder.load_profile()
    status_text = (
        f"⚙️ <b>Статус системы:</b>\n\n"
        f"• Состояние агента: <b>{current_status}</b>\n"
        f"• Целевое резюме: <code>{agent.HH_RESUME_TITLE}</code>\n"
        f"• Дневной лимит: <code>{agent.MAX_APPLIES_PER_DAY}</code> откликов\n"
        f"• Поисковый запрос: <code>QA тестировщик</code>\n"
        f"• Зарплатные ожидания: <code>{profile.get('salary_expectation')}</code>\n"
        f"• Формат работы: <code>{profile.get('work_format')}</code>\n"
    )
    telegram_bot.send_telegram_message(status_text, reply_markup=telegram_bot.get_inline_keyboard())


def handle_callback_query(cb: dict):
    cb_id = cb.get("id")
    data = cb.get("data", "")
    from_id = str(cb.get("from", {}).get("id", ""))
    log(f"Нажата Inline-кнопка: {data} от ID: {from_id}")

    if from_id != CHAT_ID:
        telegram_bot.answer_callback_query(cb_id, text="Доступ запрещен.")
        return

    # Acknowledge callback immediately to remove loading spinner in Telegram
    telegram_bot.answer_callback_query(cb_id, text="Команда принята!")

    try:
        if data == "run_agent":
            trigger_agent_run()
        elif data == "bump_resume":
            trigger_resume_bump()
        elif data == "check_chats":
            trigger_chat_check()
        elif data == "stats":
            show_stats()
        elif data == "status":
            show_status()
    except Exception as e:
        log(f"Ошибка при обработке callback {data}: {e}")


def handle_message(text: str, user_id: str):
    text_clean = text.strip()
    log(f"Входящее текстовое сообщение от {user_id}: {text_clean}")

    if user_id != CHAT_ID:
        log(f"Сообщение от чужого ID {user_id}. Игнорируем.")
        return

    keyboard = telegram_bot.get_inline_keyboard()

    if text_clean in ("/start", "/help", "меню", "Меню"):
        welcome_text = (
            "👋 <b>Приветствую, Андрей!</b>\n\n"
            "Я ваш персональный ассистент по поиску вакансий на <b>hh.ru</b>.\n\n"
            "📌 <b>Параметры:</b>\n"
            "• Поиск: <code>QA тестировщик</code> (1-3 года опыта, Москва/Удаленка)\n"
            f"• Резюме: <i>{agent.HH_RESUME_TITLE}</i>\n"
            "• Сопроводительное письмо: <i>Включено (гарантировано)</i>\n"
            "• Поднятие резюме в поиске: <i>Доступно по кнопке</i>\n"
            "• Автоответчик на вопросы: <i>Настроен</i>\n\n"
            "Нажимайте на кнопки ниже для управления:"
        )
        telegram_bot.send_telegram_message(welcome_text, reply_markup=keyboard)

    elif "HH Auto Agent" in text_clean or text_clean == "/run":
        trigger_agent_run()

    elif "Поднять резюме" in text_clean or text_clean == "/bump":
        trigger_resume_bump()

    elif "Проверить чаты" in text_clean or text_clean == "/chats":
        trigger_chat_check()

    elif "Статистика" in text_clean or text_clean == "/stats":
        show_stats()

    elif "статус" in text_clean.lower() or text_clean == "/status":
        show_status()

    else:
        telegram_bot.send_telegram_message(
            "Команда не распознана. Используйте кнопки меню ниже 👇",
            reply_markup=keyboard
        )


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("ОШИБКА: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заполнены в .env!")
        sys.exit(1)

    log("=" * 60)
    log("Telegram Сервис HH Auto Agent запущен и ожидает команд...")
    log(f"Привязан к Chat ID: {CHAT_ID}")
    log("=" * 60)

    # Send menu with interactive inline buttons
    telegram_bot.send_telegram_message(
        "🤖 <b>Панель управления HH Auto Agent на связи:</b>\nНажмите нужную кнопку ниже 👇",
        reply_markup=telegram_bot.get_inline_keyboard()
    )

    offset = 0
    while True:
        try:
            updates = telegram_bot.get_updates(offset=offset, timeout=10)
            for upd in updates:
                offset = upd.get("update_id", 0) + 1

                if "callback_query" in upd:
                    handle_callback_query(upd["callback_query"])

                elif "message" in upd:
                    msg = upd["message"]
                    text = msg.get("text", "")
                    sender_id = str(msg.get("from", {}).get("id", ""))
                    if text:
                        handle_message(text, sender_id)

        except KeyboardInterrupt:
            log("Остановка Telegram сервиса...")
            break
        except Exception as e:
            log(f"Ошибка polling: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
