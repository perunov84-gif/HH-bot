import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "hh_agent.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS applied_vacancies (
                vacancy_id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                url TEXT,
                status TEXT,
                applied_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                received_date TEXT,
                detected_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_chats (
                chat_id TEXT PRIMARY KEY,
                company TEXT,
                vacancy_title TEXT,
                last_message_snippet TEXT,
                bot_reply TEXT,
                status TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()


def is_vacancy_applied(vacancy_id: str) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM applied_vacancies WHERE vacancy_id = ?", (vacancy_id,))
        return cursor.fetchone() is not None


def save_applied_vacancy(vacancy_id: str, title: str, company: str, url: str, status: str = "applied"):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO applied_vacancies (vacancy_id, title, company, url, status, applied_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (vacancy_id, title, company, url, status, now))
        conn.commit()


def get_applied_today_count() -> int:
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM applied_vacancies 
            WHERE status = 'applied' AND applied_at LIKE ?
        """, (f"{today_prefix}%",))
        return cursor.fetchone()[0]


def save_invitation(invitation_id: str, title: str, company: str, received_date: str):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO invitations (id, title, company, received_date, detected_at)
            VALUES (?, ?, ?, ?, ?)
        """, (invitation_id, title, company, received_date, now))
        conn.commit()


def is_invitation_saved(invitation_id: str) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM invitations WHERE id = ?", (invitation_id,))
        return cursor.fetchone() is not None


def is_chat_processed(chat_id: str, last_message_snippet: str) -> bool:
    """Checks if this specific message in chat was already answered/processed."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM processed_chats 
            WHERE chat_id = ? AND last_message_snippet = ?
        """, (chat_id, last_message_snippet[:150]))
        return cursor.fetchone() is not None


def save_processed_chat(chat_id: str, company: str, vacancy_title: str, last_message_snippet: str, bot_reply: str, status: str):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO processed_chats 
            (chat_id, company, vacancy_title, last_message_snippet, bot_reply, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, company, vacancy_title, last_message_snippet[:150], bot_reply, status, now))
        conn.commit()
