import os
import re
import sys
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

import database
import telegram_bot

load_dotenv()

USER_DATA_DIR = Path(__file__).parent / "user_data"
COVER_LETTER_FILE = Path(__file__).parent / "cover_letter.txt"

HH_SEARCH_URL = os.getenv(
    "HH_SEARCH_URL",
    "https://hh.ru/search/vacancy?area=1&from=header-menu&hhtmFromLabel=drawer_filter&hhtmFrom=vacancy_search_list&text=QA+%D1%82%D0%B5%D1%81%D1%82%D0%B8%D1%80%D0%BE%D0%B2%D1%89%D0%B8%D0%BA&excluded_text=java+1c+1+c+senior&experience=between1And3&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&hhtmSource=vacancy_search_list&hhtmSourceLabel=vacancy_search_list"
)
HH_RESUME_ID = os.getenv("HH_RESUME_ID", "")
HH_RESUME_TITLE = os.getenv("HH_RESUME_TITLE", "QA Engineer")
MAX_APPLIES_PER_DAY = int(os.getenv("MAX_APPLIES_PER_DAY", "40"))
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")


def load_cover_letter() -> str:
    if COVER_LETTER_FILE.exists():
        with open(COVER_LETTER_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    example_file = Path(__file__).parent / "cover_letter.example.txt"
    if example_file.exists():
        with open(example_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def check_auth(page: Page) -> bool:
    """Verifies whether the user is logged into hh.ru."""
    print("[Auth] Проверка авторизации...")
    try:
        page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        current_url = page.url
        if "login" in current_url or "account/login" in current_url:
            print("[Auth] Сессия не найдена. Требуется авторизация через login.py.")
            return False
        return True
    except Exception as e:
        print(f"[Auth] Ошибка проверки авторизации: {e}")
        return False


def collect_invitations(page: Page) -> list:
    """Checks the negotiations tab for invitations received yesterday."""
    print("[Invitations] Проверка приглашений на собеседования...")
    invitations = []
    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%d.%m")

    months_ru = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"
    }
    yesterday_ru = f"{yesterday.day} {months_ru.get(yesterday.month, '')}"

    try:
        page.goto("https://hh.ru/applicant/negotiations?filter=invitations", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        negotiation_cards = page.locator("[data-qa='negotiations-item'], [data-qa='negotiation-card'], .negotiations-item").all()
        if not negotiation_cards:
            negotiation_cards = page.locator("div[class*='negotiation']").all()

        print(f"[Invitations] Найдено элементов приглашений: {len(negotiation_cards)}")

        for card in negotiation_cards:
            try:
                card_text = card.inner_text()
                is_yesterday = ("Вчера" in card_text) or (yesterday_ru in card_text) or (yesterday_str in card_text)

                if is_yesterday or "сегодня" in card_text.lower():
                    title_elem = card.locator("a[data-qa='negotiations-item__vacancy-name'], a[data-qa='vacancy-name']").first
                    company_elem = card.locator("[data-qa='negotiations-item__company-name'], [data-qa='vacancy-company']").first

                    title = title_elem.inner_text().strip() if title_elem.count() > 0 else "Вакансия"
                    company = company_elem.inner_text().strip() if company_elem.count() > 0 else "Компания"

                    inv_id = re.sub(r"\W+", "_", f"{company}_{title}")
                    if not database.is_invitation_saved(inv_id):
                        database.save_invitation(inv_id, title, company, yesterday.strftime("%Y-%m-%d"))
                        invitations.append({"title": title, "company": company})
            except Exception:
                continue

    except Exception as e:
        print(f"[Invitations] Ошибка при сборе приглашений: {e}")

    return invitations


def select_correct_resume(page: Page) -> bool:
    """Strictly verifies and selects the target resume in modal."""
    print(f"  [Resume] Проверка выбора резюме: '{HH_RESUME_TITLE}' (ID: {HH_RESUME_ID})")
    
    # 1. Check if multiple resume options / radios exist
    try:
        # Check by ID in input value or data-qa
        id_inputs = page.locator(f"input[value*='{HH_RESUME_ID}'], [data-qa*='{HH_RESUME_ID}']").all()
        if id_inputs:
            for inp in id_inputs:
                try:
                    inp.check(timeout=1500)
                    print("  [Resume] Резюме выбрано по ID.")
                    return True
                except Exception:
                    inp.click(timeout=1500)
                    return True
    except Exception:
        pass

    # 2. Check by Title matching keywords (Junior QA Automation Engineer / Manual Tester)
    keywords = ["Junior QA", "QA Automation", "Manual Tester", "QA Engineer"]
    try:
        resume_items = page.locator("label, div[data-qa*='resume'], li[data-qa*='resume'], div[role='radio']").all()
        for item in resume_items:
            text = item.inner_text()
            if any(k.lower() in text.lower() for k in keywords):
                try:
                    item.click(timeout=1500)
                    print(f"  [Resume] Резюме выбрано по совпадению названия: {text[:40]}...")
                    return True
                except Exception:
                    pass
    except Exception:
        pass

    # If only 1 resume exists in account or already auto-selected
    print("  [Resume] Отдельный выбор резюме не требуется или резюме уже выбрано по умолчанию.")
    return True


def attach_cover_letter(page: Page, cover_letter: str) -> bool:
    """Strictly ensures the cover letter is expanded and entered into textarea."""
    print("  [Letter] Подготовка и прикрепление сопроводительного письма...")

    # 1. Expand letter if button / toggle exists
    toggle_selectors = [
        "button[data-qa='vacancy-response-letter-toggle']",
        "[data-qa='vacancy-response-letter-toggle-collapsed']",
        "button:has-text('Написать сопроводительное')",
        "button:has-text('Добавить сопроводительное')",
        "[role='button']:has-text('сопроводительное')",
        "span:has-text('Написать сопроводительное')"
    ]

    for sel in toggle_selectors:
        try:
            toggle = page.locator(sel).first
            if toggle.is_visible(timeout=1000):
                toggle.click()
                time.sleep(0.5)
                break
        except Exception:
            continue

    # 2. Locate textarea
    textarea_selectors = [
        "textarea[data-qa='vacancy-response-popup-form-letter-input']",
        "textarea[name='message']",
        "textarea[data-qa='cover-letter-textarea']",
        "div[data-qa='common-popup'] textarea",
        "textarea"
    ]

    textarea = None
    for sel in textarea_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                textarea = loc
                break
        except Exception:
            continue

    if not textarea:
        print("  [Letter] ❌ Текстовое поле для письма НЕ найдено! Отклик без письма запрещен.")
        return False

    # 3. Fill textarea
    try:
        textarea.fill(cover_letter)
        time.sleep(0.5)

        # 4. Strict verification: ensure textarea contains content
        val = textarea.input_value()
        if not val or len(val.strip()) < 20:
            # Fallback: type via JS
            page.evaluate(
                """([el, text]) => {
                    el.value = text;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                [textarea.element_handle(), cover_letter]
            )
            val = textarea.input_value()

        if val and len(val.strip()) >= 20:
            print("  [Letter] ✅ Сопроводительное письмо успешно прикреплено!")
            return True
        else:
            print("  [Letter] ❌ Проверка значения поля не прошла (текст пустой).")
            return False

    except Exception as e:
        print(f"  [Letter] ❌ Ошибка заполнения сопроводительного письма: {e}")
        return False


def handle_response_modal(page: Page, cover_letter: str) -> bool:
    """Handles modal: verifies resume, guarantees cover letter, clicks submit."""
    time.sleep(1.5)

    # 1. Strictly verify/select resume
    select_correct_resume(page)
    time.sleep(0.5)

    # 2. Strictly attach cover letter
    letter_ok = attach_cover_letter(page, cover_letter)
    if not letter_ok:
        print("  [Modal] ⛔ Отмена отклика: письмо не прикреплено!")
        return False

    # 3. Click Submit button
    submit_selectors = [
        "button[data-qa='vacancy-response-submit-popup']",
        "button:has-text('Отправить отклик')",
        "button:has-text('Откликнуться')",
        "div[data-qa='common-popup'] button[type='submit']"
    ]

    for sel in submit_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                time.sleep(2.5)
                return True
        except Exception:
            continue

    print("  [Modal] Кнопка отправки отклика не найдена.")
    return False


def apply_to_vacancy(page: Page, vacancy_url: str, cover_letter: str) -> dict:
    """Opens a vacancy and submits application strictly with cover letter and target resume."""
    result = {"success": False, "status": "unknown", "title": "", "company": ""}

    try:
        page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(2.0, 3.5))

        title_elem = page.locator("h1[data-qa='vacancy-title'], h1").first
        if title_elem.count() > 0:
            result["title"] = title_elem.inner_text().strip()

        company_elem = page.locator("[data-qa='vacancy-company-name'], a[data-qa='vacancy-company-name']").first
        if company_elem.count() > 0:
            result["company"] = company_elem.inner_text().strip()

        # Check if already responded
        already_applied = page.locator(
            "[data-qa='vacancy-response-already-applied'], "
            ":has-text('Вы уже откликнулись'), "
            ":has-text('Вы откликнулись')"
        ).first
        if already_applied.is_visible(timeout=1000):
            print(f"  -> Уже был отклик ранее: {result['title']}")
            result["status"] = "already_applied"
            return result

        # Check if employer redirects to external site
        external_btn = page.locator("a[data-qa='vacancy-response-link-employer']").first
        if external_btn.is_visible(timeout=1000):
            print("  -> Отклик только на внешнем сайте работодателя, пропускаем.")
            result["status"] = "external_redirect"
            return result

        # Click Response button
        apply_btn = page.locator(
            "a[data-qa='vacancy-response-link-top'], "
            "button[data-qa='vacancy-response-link-top'], "
            "[data-qa='vacancy-response-link-bottom'], "
            ":has-text('Откликнуться')"
        ).first

        if not apply_btn.is_visible(timeout=3000):
            print("  -> Кнопка 'Откликнуться' не найдена.")
            result["status"] = "no_button"
            return result

        apply_btn.click()
        time.sleep(1.5)

        # Modal dialog check
        if page.locator("div[role='dialog'], [data-qa='common-popup']").first.is_visible(timeout=2500):
            # Check for questionnaire / test
            if page.locator("text='тестирование'").first.is_visible(timeout=500) or page.locator("text='вопрос'").first.is_visible(timeout=500):
                print("  -> Вакансия требует прохождения отдельного теста/опроса, пропускаем.")
                result["status"] = "test_required"
                return result

            success = handle_response_modal(page, cover_letter)
            if success:
                result["success"] = True
                result["status"] = "applied"
                return result
            else:
                result["status"] = "modal_failed_or_no_letter"
                return result

        # Check if direct application completed without modal
        time.sleep(1)
        if "Вы откликнулись" in page.content():
            result["success"] = True
            result["status"] = "applied"

    except Exception as e:
        print(f"  [Apply Error] Ошибка: {e}")
        result["status"] = f"error: {str(e)[:100]}"

    return result


def search_and_apply(page: Page, search_url: str, cover_letter: str, status_callback=None) -> list:
    """Parses search results and applies to new vacancies."""
    applied_list = []
    applied_today = database.get_applied_today_count()
    remaining_limit = MAX_APPLIES_PER_DAY - applied_today

    print(f"[Search] Уже отправлено откликов сегодня: {applied_today}. Лимит на запуск: {remaining_limit}")
    if remaining_limit <= 0:
        print("[Search] Дневной лимит откликов исчерпан.")
        if status_callback:
            status_callback(f"⚠️ Дневной лимит ({MAX_APPLIES_PER_DAY}) уже исчерпан.")
        return applied_list

    current_page = 0
    while remaining_limit > 0:
        paged_url = f"{search_url}&page={current_page}"
        print(f"\n[Search] Открываем страницу выдачи #{current_page + 1}: {paged_url}")
        page.goto(paged_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        vacancy_links = page.locator("a[data-qa='serp-item__title'], a[href*='/vacancy/']").all()
        if not vacancy_links:
            print("[Search] Вакансий на странице не найдено или конец выдачи.")
            break

        vacancy_urls = []
        for link in vacancy_links:
            try:
                href = link.get_attribute("href")
                if href and "/vacancy/" in href:
                    clean_url = href.split("?")[0]
                    if clean_url not in vacancy_urls:
                        vacancy_urls.append(clean_url)
            except Exception:
                continue

        print(f"[Search] Найдено уникальных вакансий на странице: {len(vacancy_urls)}")

        for url in vacancy_urls:
            if remaining_limit <= 0:
                print("[Search] Достигнут лимит откликов.")
                break

            match = re.search(r"/vacancy/(\d+)", url)
            if not match:
                continue
            vacancy_id = match.group(1)

            if database.is_vacancy_applied(vacancy_id):
                continue

            print(f"\n[Обработка вакансии] ID: {vacancy_id} ({url})")
            res = apply_to_vacancy(page, url, cover_letter)

            status = res.get("status", "unknown")
            title = res.get("title") or "QA инженер"
            company = res.get("company") or "Работодатель"

            database.save_applied_vacancy(vacancy_id, title, company, url, status)

            if res.get("success"):
                remaining_limit -= 1
                applied_list.append({"title": title, "company": company, "url": url})
                print(f"  ✅ Успешный отклик: {title} в {company}! (Осталось в лимите: {remaining_limit})")

                if status_callback and len(applied_list) % 5 == 0:
                    status_callback(f"🚀 Отправлено откликов: {len(applied_list)} (в очереди...")

                # Human delay between applications
                delay = random.uniform(6.0, 11.0)
                time.sleep(delay)

        # Move to next page (modern HH has numbered pagination up to ~20 pages)
        current_page += 1
        if current_page >= 20:
            print("[Search] Проверено 20 страниц выдачи. Завершаем текущий поиск.")
            break

    return applied_list


def run_agent_job(status_callback=None) -> str:
    """Executes the full agent workflow and sends/returns report."""
    database.init_db()
    cover_letter = load_cover_letter()

    if not cover_letter:
        msg = "❌ Файл cover_letter.txt пуст или отсутствует."
        print(f"[Error] {msg}")
        telegram_bot.send_telegram_message(f"⚠️ {msg}")
        return msg

    print("=" * 60)
    print("Запуск агента HH.ru")
    print(f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Целевое резюме: {HH_RESUME_TITLE}")
    print(f"Строка поиска: QA тестировщик")
    print("=" * 60)

    errors = []
    invitations = []
    applied_vacancies = []

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

            # 1. Auth check
            if not check_auth(page):
                msg = "❌ Ошибка авторизации на HH.ru! Запустите скрипт login.py на компьютере для входа в аккаунт."
                print(msg)
                telegram_bot.send_telegram_message(f"⚠️ <b>Внимание:</b> {msg}")
                context.close()
                return msg

            # 2. Check invitations
            invitations = collect_invitations(page)

            # 3. Search and apply
            applied_vacancies = search_and_apply(page, HH_SEARCH_URL, cover_letter, status_callback=status_callback)

            context.close()

        except Exception as e:
            err_msg = f"Критический сбой в работе браузера: {e}"
            print(f"[Fatal] {err_msg}")
            errors.append(err_msg)

    # 4. Format & send Telegram report
    report_text = telegram_bot.format_daily_report(invitations, applied_vacancies, errors)
    print("\n" + "=" * 60)
    print("Отчет:")
    print(report_text)
    print("=" * 60)

    telegram_bot.send_telegram_message(report_text)
    return report_text


def main():
    run_agent_job()


if __name__ == "__main__":
    main()
