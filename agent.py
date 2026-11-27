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

load_dotenv(Path(__file__).parent / ".env")

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


def is_relevant_vacancy_title(title: str) -> bool:
    """Filters out clearly non-QA / non-IT or mismatched-level vacancy titles."""
    t = title.lower()
    qa_keywords = ("qa", "тестир", "sdet", "test", "quality assurance", "qaa")
    if not any(k in t for k in qa_keywords):
        return False

    excluded_keywords = (
        "отк", "продавец", "консультант", "монтажник", "парфюм", "космет",
        "изделий", "senior", "сеньор", "тимлид", "руководител", "начальник",
        "1с", "1c", "на java", "java)"
    )
    if any(k in t for k in excluded_keywords):
        return False
    return True


def collect_invitations(page: Page) -> list:
    """Checks the negotiations tab for interview invitations received yesterday or today."""
    print("[Invitations] Проверка приглашений на собеседования...")
    invitations = []
    now = datetime.now()
    yesterday = now - timedelta(days=1)

    months_ru = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"
    }
    yesterday_str = yesterday.strftime("%d.%m")
    today_str = now.strftime("%d.%m")
    yesterday_ru = f"{yesterday.day} {months_ru.get(yesterday.month, '')}"
    today_ru = f"{now.day} {months_ru.get(now.month, '')}"

    try:
        page.goto("https://hh.ru/applicant/negotiations?filter=invitations", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        negotiation_cards = page.locator("[data-qa='negotiations-item'], [data-qa='negotiation-card'], .negotiations-item").all()
        print(f"[Invitations] Найдено карточек откликов: {len(negotiation_cards)}")

        for card in negotiation_cards:
            try:
                tag_elem = card.locator("[data-qa^='negotiations-tag']").first
                tag_qa = (tag_elem.get_attribute("data-qa") or "") if tag_elem.count() > 0 else ""
                tag_text = tag_elem.inner_text().strip().lower() if tag_elem.count() > 0 else ""

                if "interview" not in tag_qa and "приглаш" not in tag_text:
                    continue

                card_text = card.inner_text().replace("\xa0", " ")
                date_elem = card.locator("[data-qa='negotiations-item-date']").first
                date_text = date_elem.inner_text().replace("\xa0", " ").strip() if date_elem.count() > 0 else card_text

                is_recent = any(
                    marker in date_text.lower()
                    for marker in ("вчера", "сегодня", yesterday_ru, today_ru, yesterday_str, today_str)
                )

                if is_recent:
                    title_elem = card.locator(
                        "[data-qa='negotiations-item-vacancy'], "
                        "a[data-qa='negotiations-item__vacancy-name'], "
                        "a[data-qa='vacancy-name']"
                    ).first
                    company_elem = card.locator(
                        "[data-qa='negotiations-item-company'], "
                        "[data-qa='negotiations-item__company-name'], "
                        "[data-qa='vacancy-company']"
                    ).first

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


def select_correct_resume(page: Page, container=None) -> bool:
    """Strictly verifies and selects the target resume in modal."""
    scope = container or page
    print(f"  [Resume] Проверка выбора резюме: '{HH_RESUME_TITLE}' (ID: {HH_RESUME_ID})")

    # 0. Check if target resume is already shown as the active single selection (no radio list)
    try:
        radios = scope.locator("input[type='radio'], div[role='radio']").all()
        active_titles = scope.locator("[data-qa='resume-title']").all()
        if not radios and len(active_titles) == 1:
            current_title = active_titles[0].inner_text().replace("\xa0", " ").strip()
            if HH_RESUME_TITLE and HH_RESUME_TITLE.lower() in current_title.lower():
                print(f"  [Resume] Целевое резюме уже выбрано: {current_title}")
                return True
    except Exception:
        pass

    # 1. Check by ID in input value or data-qa
    if HH_RESUME_ID:
        try:
            id_inputs = scope.locator(f"input[value*='{HH_RESUME_ID}'], [data-qa*='{HH_RESUME_ID}']").all()
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

    # 2. Check radio/list items by Title matching target title or QA keywords
    keywords = [HH_RESUME_TITLE, "Junior QA", "QA Automation", "Manual Tester", "QA Engineer"]
    keywords = [k for k in keywords if k]
    try:
        resume_items = scope.locator(
            "label, div[role='radio'], li[data-qa*='resume']"
        ).all()
        for item in resume_items:
            text = item.inner_text()
            if any(k.lower() in text.lower() for k in keywords):
                try:
                    item.click(timeout=1500)
                    print(f"  [Resume] Резюме выбрано: {text.splitlines()[0][:50]}...")
                    return True
                except Exception:
                    pass
    except Exception:
        pass

    print("  [Resume] Отдельный выбор резюме не требуется или резюме уже выбрано по умолчанию.")
    return True


def attach_cover_letter(page: Page, cover_letter: str, container=None) -> bool:
    """Strictly ensures the cover letter is expanded and entered into textarea."""
    scope = container or page
    print("  [Letter] Подготовка и прикрепление сопроводительного письма...")

    # 1. Expand letter if button / toggle exists
    toggle_selectors = [
        "button[data-qa='add-cover-letter']",
        "button[data-qa='vacancy-response-letter-toggle']",
        "[data-qa='vacancy-response-letter-toggle-collapsed']",
        "button:has-text('Добавить сопроводительное')",
        "button:has-text('Написать сопроводительное')",
        "[role='button']:has-text('сопроводительное')",
        "span:has-text('Написать сопроводительное')"
    ]

    for sel in toggle_selectors:
        try:
            toggle = scope.locator(sel).first
            if toggle.count() > 0 and toggle.is_visible():
                toggle.scroll_into_view_if_needed()
                toggle.click()
                time.sleep(0.8)
                break
        except Exception:
            continue

    # 2. Locate textarea (wait up to 3s for animation)
    textarea_selectors = [
        "textarea[data-qa='vacancy-response-popup-form-letter-input']",
        "textarea[name='message']",
        "textarea[data-qa='cover-letter-textarea']",
        "textarea"
    ]

    textarea = None
    for sel in textarea_selectors:
        try:
            loc = scope.locator(sel).first
            loc.wait_for(state="visible", timeout=2500)
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


def is_application_confirmed_on_page(page: Page) -> bool:
    """Checks visible UI state (never raw HTML) to confirm application was accepted."""
    confirmed_locators = [
        "[data-qa='vacancy-response-already-applied']",
        "[data-qa='vacancy-response-link-view-topic']",
        "a:has-text('Перейти в чат')",
        ":text-is('Вы откликнулись')",
        ":text-is('Вы уже откликнулись')",
        ":text('Резюме отправлено')"
    ]
    for sel in confirmed_locators:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            continue

    # Fallback: top 'Откликнуться' button disappeared after modal closed
    try:
        top_btn = page.locator("a[data-qa='vacancy-response-link-top'], button[data-qa='vacancy-response-link-top']").first
        if top_btn.count() == 0 or not top_btn.is_visible(timeout=500):
            return True
    except Exception:
        pass

    return False


def handle_response_modal(page: Page, cover_letter: str, captcha_hit: list = None) -> tuple:
    """Handles modal: checks visibility/tests, attaches letter, submits, and verifies result."""
    time.sleep(1.0)
    dlg = page.locator("div[role='dialog'], [data-qa='common-popup']").first

    # 0. Check hidden resume warning (use JS rendered height - is_visible alone is unreliable for
    # max-height:0 elements which Playwright sometimes misreads)
    try:
        warn = dlg.locator("[data-qa='hidden-resume-warning']").first
        if warn.count() > 0:
            warn_rendered_h = warn.evaluate("e => e.getBoundingClientRect().height")
            if warn_rendered_h > 0:
                warn_text = warn.inner_text().replace("\xa0", " ").strip()
                print(f"  [Modal] ⛔ HH.ru заблокировал отклик (резюме скрыто): {warn_text[:100]}")
                return False, "resume_hidden"
    except Exception:
        pass

    # 1. Check for questionnaire / screening test INSIDE the modal
    try:
        has_questions = (
            dlg.locator("[data-qa='task-body'], textarea[name^='task_'], [data-qa*='question']").first.is_visible(timeout=400)
            or "ответьте на вопрос" in dlg.inner_text().lower()
            or "пройдите тест" in dlg.inner_text().lower()
        )
        if has_questions:
            print("  -> Вакансия требует прохождения отдельного теста/опроса, пропускаем.")
            return False, "test_required"
    except Exception:
        pass

    # 2. Strictly verify/select resume
    select_correct_resume(page, container=dlg)
    time.sleep(0.5)

    # 3. Strictly attach cover letter
    letter_ok = attach_cover_letter(page, cover_letter, container=dlg)
    if not letter_ok:
        print("  [Modal] ⛔ Отмена отклика: письмо не прикреплено!")
        return False, "modal_failed_or_no_letter"

    # 4. Click Submit button
    submit_selectors = [
        "button[data-qa='vacancy-response-submit-popup']",
        "button:has-text('Отправить отклик')",
        "button:has-text('Откликнуться')",
        "button[type='submit']"
    ]

    clicked = False
    for sel in submit_selectors:
        try:
            btn = dlg.locator(sel).first
            if btn.is_visible(timeout=2000):
                if btn.is_disabled():
                    print("  [Modal] ⛔ Кнопка отправки отклика заблокирована (disabled).")
                    return False, "submit_disabled"
                btn.scroll_into_view_if_needed()
                time.sleep(0.3)
                btn.click()
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        print("  [Modal] Кнопка отправки отклика не найдена.")
        return False, "submit_button_not_found"

    # 5. Strictly verify modal closed or transitioned to post-apply screen
    try:
        dlg.wait_for(state="hidden", timeout=4000)
    except PlaywrightTimeoutError:
        dlg_text = ""
        try:
            dlg_text = dlg.inner_text().replace("\xa0", " ").strip()
        except Exception:
            pass
        dlg_lower = dlg_text.lower()

        # HH.ru often shows a post-apply survey inside the same modal after successful submit
        post_apply_markers = ("какой формат удобнее", "сохранить и продолжить", "отклик отправлен")
        submit_gone = dlg.locator("button[data-qa='vacancy-response-submit-popup']").count() == 0

        if any(m in dlg_lower for m in post_apply_markers) or (submit_gone and is_application_confirmed_on_page(page)):
            print("  [Modal] ✅ Отклик отправлен (HH.ru показал опросник после отправки, закрываем окно).")
            try:
                close_btn = dlg.locator("button[data-qa='response-popup-close']").first
                if close_btn.is_visible(timeout=1000):
                    close_btn.click()
                else:
                    page.keyboard.press("Escape")
                time.sleep(0.8)
            except Exception:
                pass
            return True, "applied"

        # Check if resume warning is now ACTUALLY rendered (may expand after failed submit)
        warn_actually_shown = False
        try:
            warn = dlg.locator("[data-qa='hidden-resume-warning']").first
            if warn.count() > 0:
                warn_actually_shown = warn.evaluate("e => e.getBoundingClientRect().height") > 0
        except Exception:
            pass

        print(f"  [Modal] ❌ Модальное окно НЕ закрылось! Отклик не отправлен. warn_shown={warn_actually_shown}. Текст: {dlg_text[:180]}")
        if warn_actually_shown:
            return False, "resume_hidden"
        if captcha_hit and captcha_hit[0]:
            print("  [Modal] ⛔ HH.ru запросил капчу (антибот-проверка 403 hhcaptcha).")
            try:
                page.keyboard.press("Escape")
                time.sleep(0.5)
            except Exception:
                pass
            return False, "captcha_required"
        # Close modal to avoid blocking next vacancy
        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass
        return False, "submit_rejected_in_modal"

    time.sleep(1.5)
    if is_application_confirmed_on_page(page):
        return True, "applied"

    print("  [Modal] ⚠️ Модальное окно закрылось, но подтверждение отклика на странице не найдено.")
    return False, "submit_not_confirmed"


def apply_to_vacancy(page: Page, vacancy_url: str, cover_letter: str) -> dict:
    """Opens a vacancy and submits application strictly with cover letter and target resume."""
    result = {"success": False, "status": "unknown", "title": "", "company": ""}
    captcha_hit = [False]

    def _on_response(resp):
        if resp.status == 403 and "vacancy_response" in resp.url:
            try:
                if "hhcaptcha" in resp.text():
                    captcha_hit[0] = True
            except Exception:
                captcha_hit[0] = True

    page.on("response", _on_response)
    try:
        page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(2.0, 3.5))

        title_elem = page.locator("h1[data-qa='vacancy-title'], h1").first
        if title_elem.count() > 0:
            result["title"] = title_elem.inner_text().strip()

        company_elem = page.locator("[data-qa='vacancy-company-name'], a[data-qa='vacancy-company-name']").first
        if company_elem.count() > 0:
            result["company"] = company_elem.inner_text().strip()

        # Check if already responded (strict visible elements only)
        already_applied = page.locator(
            "[data-qa='vacancy-response-already-applied'], "
            "[data-qa='vacancy-response-link-view-topic'], "
            ":text-is('Вы уже откликнулись'), "
            ":text-is('Вы откликнулись')"
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
            "[data-qa='vacancy-response-link-bottom']"
        ).first

        if not apply_btn.is_visible(timeout=3000):
            print("  -> Кнопка 'Откликнуться' не найдена.")
            result["status"] = "no_button"
            return result

        apply_btn.click()
        time.sleep(1.8)

        # Modal dialog check
        if page.locator("div[role='dialog'], [data-qa='common-popup']").first.is_visible(timeout=2500):
            success, status = handle_response_modal(page, cover_letter, captcha_hit=captcha_hit)
            result["success"] = success
            result["status"] = status
            return result

        # Dedicated /applicant/vacancy_response page check (e.g. screening questions)
        if "/applicant/vacancy_response" in page.url:
            if page.locator("[data-qa='task-body'], textarea[name^='task_']").first.is_visible(timeout=1000):
                print("  -> Вакансия требует прохождения отдельного теста/опроса, пропускаем.")
                result["status"] = "test_required"
                return result
            result["status"] = "redirect_response_page"
            return result

        # Check if direct application completed without modal
        time.sleep(1.0)
        if is_application_confirmed_on_page(page):
            result["success"] = True
            result["status"] = "applied"
        else:
            result["status"] = "submit_not_confirmed"

    except Exception as e:
        print(f"  [Apply Error] Ошибка: {e}")
        result["status"] = f"error: {str(e)[:100]}"
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

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

        vacancy_items = []
        seen_urls = set()
        for link in vacancy_links:
            try:
                href = link.get_attribute("href")
                if href and "/vacancy/" in href:
                    clean_url = href.split("?")[0]
                    if clean_url not in seen_urls:
                        seen_urls.add(clean_url)
                        link_title = link.inner_text().replace("\xa0", " ").strip()
                        vacancy_items.append((clean_url, link_title))
            except Exception:
                continue

        print(f"[Search] Найдено уникальных вакансий на странице: {len(vacancy_items)}")

        for url, link_title in vacancy_items:
            if remaining_limit <= 0:
                print("[Search] Достигнут лимит откликов.")
                break

            match = re.search(r"/vacancy/(\d+)", url)
            if not match:
                continue
            vacancy_id = match.group(1)

            if database.is_vacancy_applied(vacancy_id):
                continue

            if link_title and not is_relevant_vacancy_title(link_title):
                print(f"  [Skip] Нерелевантное название вакансии: {link_title}")
                continue

            print(f"\n[Обработка вакансии] ID: {vacancy_id} ({url})")
            res = apply_to_vacancy(page, url, cover_letter)

            status = res.get("status", "unknown")
            title = res.get("title") or link_title or "QA инженер"
            company = res.get("company") or "Работодатель"

            database.save_applied_vacancy(vacancy_id, title, company, url, status)

            if res.get("success"):
                remaining_limit -= 1
                applied_list.append({"title": title, "company": company, "url": url})
                print(f"  ✅ Успешный отклик: {title} в {company}! (Осталось в лимите: {remaining_limit})")

                # Human delay between applications
                delay = random.uniform(6.0, 11.0)
                time.sleep(delay)
            elif status == "resume_hidden":
                print("  ⛔ Остановка поиска: резюме скрыто на HH.ru, дальнейшие отклики будут отклоняться.")
                if status_callback:
                    status_callback("⛔ Отклики заблокированы: проверьте видимость резюме на HH.ru.")
                return applied_list
            elif status == "captcha_required":
                print("  ⛔ Остановка поиска: HH.ru запросил капчу (проверку на робота).")
                msg = (
                    "⛔ <b>Отклики приостановлены: HH.ru запросил капчу.</b>\n"
                    "Откройте hh.ru в браузере (или запустите <code>login.py</code>), "
                    "пройдите проверку вручную и повторите запуск."
                )
                telegram_bot.send_telegram_message(msg)
                if status_callback:
                    status_callback(msg)
                return applied_list

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


def bump_resume(resume_title: str = None, resume_id: str = None) -> dict:
    """Updates / bumps the specified resume on hh.ru to place it at the top of search results."""
    target_title = resume_title or HH_RESUME_TITLE
    target_id = resume_id or HH_RESUME_ID
    result = {"success": False, "status": "unknown", "message": ""}

    print("=" * 60)
    print(f"[Resume Bump] Поднятие резюме в поиске: {target_title}")
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
            page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)

            if not check_auth(page):
                result["message"] = "❌ Ошибка авторизации на HH.ru! Запустите login.py для входа."
                context.close()
                return result

            # Find target resume card by ID or title
            title_link = page.locator(f"a[href*='{target_id}']").first if target_id else page.locator(f"a:has-text('{target_title}')").first
            if title_link.count() == 0:
                title_link = page.locator("a:has-text('Junior QA')").first
            if title_link.count() > 0:
                card = title_link.locator("xpath=./ancestor::div[contains(@class, 'resume') or contains(@data-qa, 'resume')][1]")
                if card.count() == 0:
                    card = title_link.locator("xpath=./ancestor::div[3]")
            else:
                card = page

            card_text = card.inner_text().replace("\xa0", " ")

            # Check if countdown is active (e.g. "Поднять в 14:17" or "Поднять через...")
            time_match = re.search(r"Поднять в\s*(\d{1,2}:\d{2})", card_text)
            if time_match:
                next_time = time_match.group(1)
                result["status"] = "already_bumped"
                result["message"] = (
                    f"⏳ <b>Поднятие пока недоступно</b>\n\n"
                    f"HH.ru разрешает бесплатное поднятие 1 раз в 4 часа.\n"
                    f"⏰ Следующее поднятие резюме будет доступно <b>в {next_time}</b>."
                )
                context.close()
                return result

            if "через" in card_text.lower():
                result["status"] = "already_bumped"
                result["message"] = (
                    f"⏳ <b>Поднятие пока недоступно</b>\n\n"
                    f"HH.ru разрешает бесплатное поднятие 1 раз в 4 часа."
                )
                context.close()
                return result

            # Otherwise look for active "Поднять в поиске" button
            update_btn = card.locator("button:has-text('Поднять в поиске'), [data-qa*='resume-update'], button:has-text('Поднять')").first
            if update_btn.is_visible(timeout=3000):
                update_btn.click()
                time.sleep(2)
                result["success"] = True
                result["status"] = "bumped"
                result["message"] = (
                    f"🔝 <b>Резюме успешно поднято в поиске!</b>\n\n"
                    f"📄 <i>{target_title}</i>\n"
                    f"⏰ Следующее бесплатное поднятие будет доступно через 4 часа."
                )
                context.close()
                return result

            result["message"] = "⚠️ Кнопка поднятия резюме не найдена на странице."
            context.close()
            return result

        except Exception as e:
            err = f"Ошибка при поднятии резюме: {e}"
            print(f"[Resume Bump Error] {err}")
            result["message"] = err
            return result


def main():
    run_agent_job()


if __name__ == "__main__":
    main()
