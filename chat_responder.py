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

load_dotenv(Path(__file__).parent / ".env")

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

    # 1. Check if it's an explicit rejection (even if it contains words like "готовы" or "пригласить")
    rejection_patterns = [
        r"к сожалению.*не готовы",
        r"вынуждены отказать",
        r"сделали выбор в пользу другого",
        r"пока не готовы пригласить",
        r"не можем предложить вам",
    ]
    has_question_mark = "?" in text
    if any(re.search(pat, text_lower) for pat in rejection_patterns) and not has_question_mark:
        return {"action": "ignore_info", "reply": ""}

    # 2. Check if it's purely informational (acknowledgment, no questions)
    info_patterns = [
        r"спасибо за отклик",
        r"спасибо за ваше резюме",
        r"благодарю за ответы",
        r"спасибо за ответы",
        r"мы передали их работодателю",
        r"ваше резюме передано",
        r"мы внимательно ознакомились",
        r"мы получили ваше резюме",
        r"изучим его более внимательно",
        r"ознакомится с вашим резюме",
        r"в случае положительного решения",
        r"мы свяжемся с вами",
        r"резюме находится на рассмотрении",
        r"рассмотрим ваше резюме",
        r"если ваш опыт покажется нам интересным",
    ]

    question_keywords = ["уточните", "подскажите", "ответьте", "напишите", "расскажите", "укажите", "сколько", "какой", "когда"]
    has_question_words = (
        any(qk in text_lower for qk in question_keywords)
        or bool(re.search(r"(?<!не\s)\bготовы\b", text_lower))
    )

    is_pure_info = any(re.search(pat, text_lower) for pat in info_patterns) and not (has_question_mark or has_question_words)

    if is_pure_info:
        return {"action": "ignore_info", "reply": ""}

    # If message contains an external link (e.g. test task, form, calendar), alert human
    if re.search(r"https?://", text_lower):
        return {"action": "notify_human", "reply": ""}

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


def _close_chatik(page: Page):
    """Closes the embedded chatik widget if open and waits for it to unload."""
    try:
        close_btn = page.locator("[data-qa='chatik-close-chatik']").first
        if close_btn.is_visible(timeout=1000):
            close_btn.click()
            time.sleep(1.2)
            # Wait for chatik iframe to disappear from frames
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not any("chatik.hh.ru" in (f.url or "") for f in page.frames):
                    break
                time.sleep(0.3)
    except Exception:
        pass


def _find_chatik_frame(page: Page, timeout_sec: float = 12.0):
    """Waits for and returns the loaded chatik.hh.ru Frame object."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for f in page.frames:
            if "chatik.hh.ru" in (f.url or ""):
                try:
                    if f.locator("[data-qa='chatik-layout'], [data-qa='text-input'], [data-qa^='chatik-chat-message-']").first.count() > 0:
                        return f
                except Exception:
                    pass
        time.sleep(0.4)
    return None


def _collect_chat_list_items(page: Page, limit: int = 30, skip_no_reply: bool = False) -> list:
    """Collects (chat_num, company, title, chat_url) from the currently visible hh.ru/chat list."""
    items = []
    links = page.locator("a[data-qa^='chatik-open-chat-']").all()
    for link in links[:limit]:
        try:
            qa = link.get_attribute("data-qa") or ""
            m = re.search(r"chatik-open-chat-(\d+)", qa)
            if not m:
                continue
            chat_num = m.group(1)

            raw_text = link.inner_text().replace("\xa0", " ")
            lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]

            title_el = link.locator("[data-qa='chat-cell-title']").first
            title = title_el.inner_text().replace("\xa0", " ").strip() if title_el.count() > 0 else (lines[0] if lines else "Вакансия")
            company = lines[2] if len(lines) >= 3 else "Компания"
            preview = lines[3] if len(lines) >= 4 else ""

            if skip_no_reply and preview.lower().startswith("отклик на вакансию"):
                continue

            items.append({
                "chat_num": chat_num,
                "chat_id": f"chatik_{chat_num}",
                "company": company,
                "title": title,
                "url": f"https://hh.ru/chat/{chat_num}"
            })
        except Exception:
            continue
    return items


def check_and_respond_chats(page: Page, profile: dict, status_callback=None) -> list:
    """Checks hh.ru/chat, prioritizing unread chats and employer replies."""
    print("[Chat Responder] Переход в раздел чатов hh.ru/chat...")
    results = []

    try:
        page.goto("https://hh.ru/chat", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3.0)

        # 1. Priority pass: enable "Только непрочитанные" filter
        candidates = []
        seen_nums = set()
        unread_filter = page.locator(":text('Только непрочитанные')").first
        if unread_filter.count() > 0 and unread_filter.is_visible(timeout=3000):
            unread_filter.click()
            time.sleep(2.5)
            unread_items = _collect_chat_list_items(page, limit=50, skip_no_reply=False)
            print(f"[Chat Responder] Найдено непрочитанных чатов: {len(unread_items)}")
            for item in unread_items:
                if item["chat_num"] not in seen_nums:
                    seen_nums.add(item["chat_num"])
                    candidates.append(item)

            # Turn off filter to also inspect top recent chats with replies
            unread_filter.click()
            time.sleep(2.0)

        # 2. Secondary pass: top 15 recent chats that have a reply (not plain "Отклик на вакансию")
        recent_items = _collect_chat_list_items(page, limit=15, skip_no_reply=True)
        for item in recent_items:
            if item["chat_num"] not in seen_nums:
                seen_nums.add(item["chat_num"])
                candidates.append(item)

        print(f"[Chat Responder] Всего чатов к проверке (непрочитанные в приоритете): {len(candidates)}")

        for i, item in enumerate(candidates):
            chat_id = item["chat_id"]
            company = item["company"]
            title = item["title"]
            chat_url = item["url"]

            try:
                print(f"\n[Чат #{i+1}] {company} — {title} ({chat_url})")
                page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.0)

                # Wait for messages or input to render
                try:
                    page.locator("[data-qa^='chatik-chat-message-'], textarea[data-qa='text-input']").first.wait_for(
                        state="visible", timeout=7000
                    )
                except Exception:
                    pass
                time.sleep(0.8)

                # Collect actual bubble messages (skip system notices without bubble/text)
                raw_items = page.locator("[data-qa^='chatik-chat-message-']:not([data-qa*='bubble'])").all()
                bubble_items = []
                for msg_el in raw_items:
                    try:
                        if msg_el.locator("[data-qa*='chatik-chat-message-bubble']").count() > 0 and msg_el.locator("[data-qa='chat-bubble-text']").count() > 0:
                            bubble_items.append(msg_el)
                    except Exception:
                        continue

                if not bubble_items:
                    print("  -> Сообщений в чате пока нет.")
                    continue

                last_msg = bubble_items[-1]
                bubble_elem = last_msg.locator("[data-qa*='chatik-chat-message-bubble']").first
                bubble_cls = bubble_elem.get_attribute("class") or ""
                has_read_icon = last_msg.locator("[data-qa='chat-bubble-icon-read']").count() > 0
                has_avatar = last_msg.locator("[data-qa='chat-bubble-avatar']").count() > 0

                is_outgoing = ("outgoing" in bubble_cls.lower()) or (has_read_icon and not has_avatar)
                if is_outgoing:
                    print("  -> Последнее сообщение от вас. Ждем ответа работодателя.")
                    continue

                last_msg_text = last_msg.locator("[data-qa='chat-bubble-text']").first.inner_text().strip()
                last_msg_text = re.sub(r"\n{2,}", "\n", last_msg_text)
                msg_snippet = last_msg_text[:120]

                if database.is_chat_processed(chat_id, msg_snippet):
                    print("  -> Это сообщение уже было обработано ранее.")
                    continue

                print(f"  -> Входящее сообщение от работодателя:\n  «{last_msg_text[:120]}»")

                analysis = analyze_employer_message(last_msg_text, profile)
                action = analysis.get("action")

                if action == "ignore_info":
                    print("  -> Информационное сообщение (автоответ без вопросов). Зафиксировано.")
                    database.save_processed_chat(chat_id, company, title, msg_snippet, "", "acknowledged")
                    results.append({
                        "type": "acknowledged",
                        "company": company,
                        "title": title,
                        "snippet": last_msg_text[:140],
                        "url": chat_url
                    })

                elif action == "reply":
                    reply_text = analysis.get("reply", "")
                    print(f"  -> Отправляем автоответ в чат:\n{reply_text}")

                    input_box = page.locator(
                        "textarea[data-qa='text-input'], "
                        "textarea[placeholder*='Сообщение'], "
                        "textarea"
                    ).first

                    if input_box.is_visible(timeout=3000):
                        input_box.fill(reply_text)
                        time.sleep(0.5)

                        send_btn = page.locator(
                            "button[data-qa='chatik-do-send-message'], "
                            "button[data-qa='chat-submit-button'], "
                            "button:has-text('Отправить')"
                        ).first

                        if send_btn.is_visible(timeout=2000):
                            send_btn.click()
                        else:
                            input_box.press("Enter")

                        time.sleep(2)
                        database.save_processed_chat(chat_id, company, title, msg_snippet, reply_text, "replied")

                        tg_text = (
                            f"💬 <b>Автоответ в чате HH.ru!</b>\n\n"
                            f"🏢 <b>Компания:</b> {company}\n"
                            f"💼 <b>Вакансия:</b> {title}\n"
                            f"❓ <b>Вопрос работодателя:</b>\n<i>{last_msg_text}</i>\n\n"
                            f"🤖 <b>Отправленный ответ:</b>\n<i>{reply_text}</i>\n\n"
                            f"👉 <a href='{chat_url}'>Открыть диалог на HH.ru</a>"
                        )
                        telegram_bot.send_telegram_message(tg_text)
                        results.append({"type": "replied", "company": company, "title": title, "url": chat_url})
                    else:
                        print("  -> Поле ввода в чате недоступно (возможно, чат закрыт работодателем).")
                        database.save_processed_chat(chat_id, company, title, msg_snippet, "", "input_unavailable")

                elif action == "notify_human":
                    print("  -> Нестандартный вопрос или приглашение. Требуется личный ответ.")
                    database.save_processed_chat(chat_id, company, title, msg_snippet, "", "notified_human")

                    tg_text = (
                        f"🔔 <b>Внимание! Новое сообщение от работодателя</b>\n\n"
                        f"🏢 <b>Компания:</b> {company}\n"
                        f"💼 <b>Вакансия:</b> {title}\n"
                        f"📩 <b>Текст сообщения:</b>\n<i>{last_msg_text}</i>\n\n"
                        f"👉 <a href='{chat_url}'>Перейти в чат и ответить лично</a>"
                    )
                    telegram_bot.send_telegram_message(tg_text)
                    results.append({"type": "alert", "company": company, "title": title, "url": chat_url})

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

            replied = [r for r in results if r.get("type") == "replied"]
            alerts = [r for r in results if r.get("type") == "alert"]
            acked = [r for r in results if r.get("type") == "acknowledged"]

            lines = [
                f"Проверено новых входящих событий: <b>{len(results)}</b>",
                f"• Автоответов отправлено: <b>{len(replied)}</b>",
                f"• Требуют вашего ответа: <b>{len(alerts)}</b>",
                f"• Информационных автоответов (без вопросов): <b>{len(acked)}</b>",
            ]
            if acked:
                lines.append("\n📩 <b>Новые автоответы от компаний:</b>")
                for item in acked[:10]:
                    lines.append(f"• {item['company']} — {item['title']}\n  <i>«{item['snippet']}»</i>")

            summary = "\n".join(lines)
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
