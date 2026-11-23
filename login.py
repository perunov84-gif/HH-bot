import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

USER_DATA_DIR = Path(__file__).parent / "user_data"


def main():
    print("=" * 60)
    print("Запуск браузера для авторизации на hh.ru")
    print(f"Данные сессии будут сохранены в: {USER_DATA_DIR}")
    print("=" * 60)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized"
            ],
            no_viewport=True
        )

        page = context.new_page()
        page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")

        print("\nПожалуйста, выполните вход в свой аккаунт hh.ru в открывшемся браузере.")
        print("Введите номер телефона/почту, код из СМС и решите капчу, если появится.")
        print("Убедитесь, что вы вошли и видите страницу со своими резюме или главную.")
        print("\nКогда вход будет завершен, вернитесь в эту консоль и нажмите [ENTER]...")
        
        try:
            input()
        except KeyboardInterrupt:
            pass

        print("\nСохраняем сессию и закрываем браузер...")
        context.close()
        print("Готово! Сессия сохранена в папку user_data.")
        print("Теперь можно запускать автоматического агента (agent.py).")


if __name__ == "__main__":
    main()
