import os
import re
import sys
import time
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

import database
import telegram_bot

load_dotenv()

USER_DATA_DIR = Path(__file__).parent / "user_data"
PROFILE_PATH = Path(__file__).parent / "candidate_profile.json"
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")


def load_profile() -> dict:
    if PROFILE_PATH.exists():
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def analyze_employer_message(text: str, profile: dict) -> dict:
    """Analyzes employer's message text and generates an answer or marks for human review."""
    text_lower = text.lower()

    # 1. Check if it's purely informational (acknowledgment, no questions)
    info_patterns = [
        r"спасибо за отклик",
        r"ваше резюме передано",
        r"мы внимательно ознакомились",
        r"в случае положительного решения",
        r"мы свяжемся с вами",
        r"резюме находится на рассмотрении",
        r"рассмотрим ваше резюме",
        r"если ваш опыт покажется нам интересным"
    ]

    has_question_mark = "?" in text
    question_keywords = ["уточните", "подскажите", "ответьте", "напишите", "расскажите", "укажите", "сколько", "какой", "когда", "готовы"]
    has_question_words = any(qk in text_lower for qk in question_keywords)

    is_pure_info = any(re.search(pat, text_lower) for pat in info_patterns) and not (has_question_mark or has_question_words)

    if is_pure_info:
        return {"action": "ignore_info", "reply": ""}

    # 2. Check for standard questions matching our candidate profile
    detected_questions = []
    answers = []

    # Question: Salary expectations
    if any(k in text_lower for k in ["зарплат", "вилка", "ожидан", "доход", "рубл", "зп", "ставки", "желаем"]):
        detected_questions.append("зарплата")
        answers.append(f"Зарплатные ожидания: {profile.get('salary_expectation', 'от 70 000 руб. на руки')}.")

    # Question: Work format / location
    if any(k in text_lower for k in ["формат", "удален", "гибрид", "офис", "локаци", "город", "москв", "прожива"]):
        detected_questions.append("формат")
        answers.append(f"Формат работы: {profile.get('work_format', 'полная удаленка или гибрид в Москве')}.")

    # Question: Start readiness / notice period
    if any(k in text_lower for k in ["приступить", "выйти", "готовность к работ", "срок", "отработк", "когда готов"]):
        detected_questions.append("старт")
        answers.append(f"Готовность к выходу: {profile.get('start_readiness', 'готов приступить сразу')}.")

    # Question: English level
    if any(k in text_lower for k in ["английск", "english", "язык", "уровень яз"]):
        detected_questions.append("английский")
        answers.append(f"Уровень английского языка: {profile.get('english_level', 'A2 (читаю техническую документацию)')}.")

    # Question: Citizenship / residency
    if any(k in text_lower for k in ["гражданств", "паспорт", "резидент", "рф"]):
        detected_questions.append("гражданство")
        answers.append(f"Гражданство: {profile.get('citizenship', 'РФ, проживаю в Москве')}.")

    # Question: Test task readiness
    if any(k in text_lower for k in ["тестов", "задани", "выполнить тз"]):
        detected_questions.append("тестовое")
        answers.append(f"Тестовое задание: {profile.get('test_task_ready', 'Да, готов выполнить')}.")

    # If standard questions detected:
    if detected_questions:
        if len(answers) == 1:
            reply = f"Здравствуйте!\n{answers[0]}"
        else:
            numbered_answers = "\n".join(f"{i}. {ans}" for i, ans in enumerate(answers, start=1))
            reply = f"Здравствуйте! Отвечаю на ваши вопросы:\n{numbered_answers}\n\nБуду рад обсудить подробности!"

        return {
            "action": "reply",
            "detected": detected_questions,
            "reply": reply
        }

    # 3. Human alert keywords (interviews, meetings, non-standard questions, links)
    human_alert_keywords = [
        "собеседован", "интервью", "приглаш", "созвон", "zoom", "зум", "meet", 
        "звон", "встреч", "ссылк", "тест", "форма", "опрос", "telegram", "тг", 
        "whatsapp", "телефон", "почт", "время", "дату", "удобно ли", "предлагаем"
    ]
    needs_human = (
        has_question_mark or 
        has_question_words or 
        any(k in text_lower for k in human_alert_keywords)
    )

    if needs_human:
        return {"action": "notify_human", "reply": ""}

    # Default to acknowledgment/silence if none of the above
    return {"action": "ignore_info", "reply": ""}


def check_and_respond_chats(page: Page, profile: dict, status_callback=None) -> list:
    """Checks recent negotiations for unread messages and auto-responds."""
    print("[Chat Responder] Переход в раздел откликов и сообщений...")
    processed_count = 0
    results = []

    try:
        page.goto("https://hh.ru/applicant/negotiations", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # Get negotiation cards
        cards = page.locator("[data-qa='negotiations-item'], [data-qa='negotiation-card'], .negotiations-item").all()
        if not cards:
            cards = page.locator("div[class*='negotiation']").all()

        print(f"[Chat Responder] Найдено откликов на странице: {len(cards)}")

        # Check the top 15 most recent negotiations
        for i, card in enumerate(cards[:15]):
            try:
                card_text = card.inner_text()

                # Extract company & title
                title_elem = card.locator("a[data-qa='negotiations-item__vacancy-name'], a[data-qa='vacancy-name']").first
                company_elem = card.locator("[data-qa='negotiations-item__company-name'], [data-qa='vacancy-company']").first
                title = title_elem.inner_text().strip() if title_elem.count() > 0 else "Вакансия"
                company = company_elem.inner_text().strip() if company_elem.count() > 0 else "Компания"

                # Check if there is an unread badge or chat link
                chat_link = card.locator("a[data-qa='negotiations-item__chat'], a[href*='/applicant/negotiations/']").first
                if not chat_link.is_visible(timeout=1000):
                    # Card itself might be clickable
                    chat_link = title_elem

                if not chat_link.is_visible(timeout=1000):
                    continue

                href = chat_link.get_attribute("href") or ""
                chat_url = f"https://hh.ru{href}" if href.startswith("/") else href

                # If no href, click into it
                # For safety, let's open chat in new tab or navigate
                print(f"\n[Чат #{i+1}] {company} — {title}")

                # Open chat page
                chat_page = page.context.new_page()
                try:
                    chat_page.goto(chat_url, wait_until="domcontentloaded", timeout=25000)
                    time.sleep(2.5)

                    # Extract all messages
                    message_locators = chat_page.locator(
                        "[data-qa*='chat-message'], [data-qa*='message-item'], div[class*='message_']"
                    ).all()

                    if not message_locators:
                        chat_page.close()
                        continue

                    # Find the last message
                    last_msg_elem = message_locators[-1]
                    last_msg_text = last_msg_elem.inner_text().strip()

                    # Check if the last message is from the candidate or employer
                    # Typically candidate messages have class 'outbox' or 'outgoing' or contain candidate name
                    class_attr = last_msg_elem.get_attribute("class") or ""
                    is_outgoing = ("outbox" in class_attr.lower()) or ("outgoing" in class_attr.lower()) or ("applicant" in class_attr.lower())

                    if is_outgoing:
                        print("  -> Последнее сообщение от вас. Ждем ответа работодателя.")
                        chat_page.close()
                        continue

                    # Unique identifier for this message
                    chat_id = re.sub(r"\W+", "_", f"{company}_{title}")
                    msg_snippet = last_msg_text[:120]

                    if database.is_chat_processed(chat_id, msg_snippet):
                        print("  -> Это сообщение уже было обработано ранее.")
                        chat_page.close()
                        continue

                    print(f"  -> Входящее сообщение от работодателя:\n  «{last_msg_text[:100]}...»")

                    analysis = analyze_employer_message(last_msg_text, profile)
                    action = analysis.get("action")

                    if action == "ignore_info":
                        print("  -> Информационное сообщение (автоответ без вопросов). Зафиксировано.")
                        database.save_processed_chat(chat_id, company, title, msg_snippet, "", "acknowledged")

                    elif action == "reply":
                        reply_text = analysis.get("reply", "")
                        print(f"  -> Отправляем автоответ в чат:\n{reply_text}")

                        # Find chat input
                        input_box = chat_page.locator(
                            "textarea[data-qa='chat-input-textarea'], "
                            "textarea[placeholder*='сообщение'], "
                            "div[contenteditable='true'], "
                            "textarea"
                        ).first

                        if input_box.is_visible(timeout=3000):
                            input_box.fill(reply_text)
                            time.sleep(0.5)

                            send_btn = chat_page.locator(
                                "button[data-qa='chat-submit-button'], "
                                "button:has-text('Отправить')"
                            ).first

                            if send_btn.is_visible(timeout=2000):
                                send_btn.click()
                            else:
                                chat_page.keyboard.press("Enter")

                            time.sleep(2)
                            database.save_processed_chat(chat_id, company, title, msg_snippet, reply_text, "replied")
                            processed_count += 1

                            # Send Telegram notification
                            tg_text = (
                                f"💬 <b>Автоответ в чате HH.ru!</b>\n\n"
                                f"🏢 <b>Компания:</b> {company}\n"
                                f"💼 <b>Вакансия:</b> {title}\n"
                                f"❓ <b>Вопрос работодателя:</b>\n<i>{last_msg_text}</i>\n\n"
                                f"🤖 <b>Отправленный ответ:</b>\n<i>{reply_text}</i>\n\n"
                                f"👉 <a href='{chat_url}'>Открыть диалог на HH.ru</a>"
                            )
                            telegram_bot.send_telegram_message(tg_text)
                            results.append({"type": "replied", "company": company, "title": title})

                    elif action == "notify_human":
                        print("  -> Нестандартный вопрос или приглашение. Требуется личный ответ.")
                        database.save_processed_chat(chat_id, company, title, msg_snippet, "", "notified_human")
                        processed_count += 1

                        tg_text = (
                            f"🔔 <b>Внимание! Новое сообщение от работодателя</b>\n\n"
                            f"🏢 <b>Компания:</b> {company}\n"
                            f"💼 <b>Вакансия:</b> {title}\n"
                            f"📩 <b>Текст сообщения:</b>\n<i>{last_msg_text}</i>\n\n"
                            f"👉 <a href='{chat_url}'>Перейти в чат и ответить лично</a>"
                        )
                        telegram_bot.send_telegram_message(tg_text)
                        results.append({"type": "alert", "company": company, "title": title})

                finally:
                    chat_page.close()

            except Exception as e:
                print(f"  [Chat Item Error] {e}")
                continue

    except Exception as e:
        print(f"[Chat Responder Error] Ошибка: {e}")

    return results


def run_chat_responder(status_callback=None) -> str:
    """Executes chat responder cycle."""
    database.init_db()
    profile = load_profile()

    print("=" * 60)
    print("Запуск модуля проверки чатов и автоответов (Chat Responder)")
    print(f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=HEADLESS,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox"
                ]
            )
            page = context.new_page()

            # Verify session
            page.goto("https://hh.ru/applicant/negotiations", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            if "login" in page.url:
                msg = "❌ Сессия HH.ru не авторизована! Запустите login.py."
                print(msg)
                telegram_bot.send_telegram_message(msg)
                context.close()
                return msg

            results = check_and_respond_chats(page, profile, status_callback=status_callback)
            context.close()

            summary = f"Проверка чатов завершена. Обработано событий: {len(results)}"
            print(summary)
            if status_callback:
                status_callback(summary)
            return summary

        except Exception as e:
            err = f"Ошибка в работе браузера Chat Responder: {e}"
            print(err)
            return err


def main():
    run_chat_responder()


if __name__ == "__main__":
    main()
