import sqlite3
import logging
import re
import html
import time
from datetime import datetime, timedelta, timezone
from calendar import monthrange
import telebot
from telebot import types, custom_filters
from telebot.handler_backends import State, StatesGroup
from telebot.storage import StateMemoryStorage

# ========== КОНФИГ ==========
BOT_TOKEN = "8657823190:AAHS9jUiKzG0ycm-X9Lp_8mg-B70BCRTQAU"
MASTER_ADMIN_IDS = [8484944484]  # Жестко заданные ID суперадминов (для Лвл 4 и скачивания БД)
CHAT_ID = -1003975292023  # ID вашего чата/группы
CHAT_INVITE_LINK = "https://t.me/+rqod3GyElkwxYzYy"
DB_PATH = "cult_flood.db"

# Названия уровней админки
LEVEL_NAMES = {
    1: "Админ",
    2: "Ст.админ",
    3: "Куратор",
    4: "Владелец"
}

# МСК Часовой пояс (UTC+3)
MSK_TIMEZONE = timezone(timedelta(hours=3))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== ВСПАМОГАТЕЛЬНАЯ ФУНКЦИЯ ПОДКЛЮЧЕНИЯ ==========
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

# ========== ЭКРАНИРОВАНИЕ HTML ==========
def clean_html(text: str) -> str:
    if not text:
        return ""
    return html.escape(str(text))

# ========== ВРЕМЯ ПО МСК ==========
def get_moscow_time() -> datetime:
    return datetime.now(MSK_TIMEZONE)

# ========== ПАРСЕР ВРЕМЕНИ ==========
def parse_time_string(time_str: str) -> timedelta:
    time_str = time_str.strip().lower()
    total_seconds = 0
    matches = re.findall(r'(\d+)([dhmywM])', time_str)
    for value, unit in matches:
        value = int(value)
        if unit == 'm':
            total_seconds += value * 60
        elif unit == 'h':
            total_seconds += value * 3600
        elif unit == 'd':
            total_seconds += value * 86400
        elif unit == 'w':
            total_seconds += value * 604800
        elif unit == 'M':
            total_seconds += value * 2592000
        elif unit == 'y':
            total_seconds += value * 31536000
    return timedelta(seconds=total_seconds)

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self):
        self._init_db()

    def _init_db(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                text TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                reviewed_at TEXT,
                review_reason TEXT,
                reviewed_by INTEGER
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category TEXT DEFAULT 'Другое',
                status TEXT DEFAULT 'open',
                created_at TEXT,
                updated_at TEXT,
                closed_by INTEGER,
                closed_at TEXT,
                rating INTEGER DEFAULT 0,
                rating_feedback TEXT,
                assigned_to INTEGER DEFAULT NULL
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                user_id INTEGER,
                message TEXT,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                level INTEGER DEFAULT 1,
                added_by INTEGER,
                added_at TEXT,
                expires_at TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS admin_codes (
                code TEXT PRIMARY KEY,
                level INTEGER,
                duration TEXT,
                max_uses INTEGER DEFAULT 1,
                used INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS used_promos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                code TEXT,
                used_at TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS banned_user_promos (
                user_id INTEGER,
                code TEXT,
                PRIMARY KEY (user_id, code)
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS promo_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                code TEXT,
                status TEXT DEFAULT 'pending',
                processed_by INTEGER
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_at TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                item_id INTEGER,
                admin_id INTEGER,
                message_id INTEGER
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS appeals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                text TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS appeal_bans (
                user_id INTEGER PRIMARY KEY,
                banned_by INTEGER,
                banned_at TEXT
            )''')
            
            migrations = [
                ("applications", "reviewed_at", "TEXT"),
                ("applications", "review_reason", "TEXT"),
                ("applications", "reviewed_by", "INTEGER"),
                ("tickets", "category", "TEXT DEFAULT 'Другое'"),
                ("tickets", "closed_by", "INTEGER"),
                ("tickets", "closed_at", "TEXT"),
                ("tickets", "rating", "INTEGER DEFAULT 0"),
                ("tickets", "rating_feedback", "TEXT"),
                ("tickets", "assigned_to", "INTEGER DEFAULT NULL"),
                ("admins", "expires_at", "TEXT")
            ]
            for table, column, col_type in migrations:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                except sqlite3.OperationalError:
                    pass

            for admin_id in MASTER_ADMIN_IDS:
                c.execute("INSERT OR REPLACE INTO admins (user_id, level, added_by, added_at) VALUES (?, 4, ?, ?)",
                         (admin_id, admin_id, get_moscow_time().isoformat()))
            conn.commit()

    # ----- ПОЛЬЗОВАТЕЛИ -----
    def add_user(self, user_id, username):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
                     (user_id, username, get_moscow_time().isoformat()))
            conn.commit()

    def get_all_users(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users")
            return [row[0] for row in c.fetchall()]

    def get_username(self, user_id):
        if not user_id: return "Нет"
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            return row[0] if row and row[0] else str(user_id)

    def find_user_id(self, query: str):
        query = query.strip().lstrip('@')
        if query.isdigit():
            return int(query)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (query,))
            row = c.fetchone()
            return row[0] if row else None

    # ----- УВЕДОМЛЕНИЯ -----
    def add_notification(self, notif_type, item_id, admin_id, message_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO admin_notifications (type, item_id, admin_id, message_id) VALUES (?, ?, ?, ?)",
                     (notif_type, item_id, admin_id, message_id))
            conn.commit()

    def get_notifications(self, notif_type, item_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT admin_id, message_id FROM admin_notifications WHERE type = ? AND item_id = ?",
                     (notif_type, item_id))
            return c.fetchall()

    def delete_notifications(self, notif_type, item_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM admin_notifications WHERE type = ? AND item_id = ?", (notif_type, item_id))
            conn.commit()

    # ----- АДМИНЫ -----
    def get_admin_level(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT level, expires_at FROM admins WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            if not row:
                return 0
            level, expires_at = row
            if expires_at:
                expire_date = datetime.fromisoformat(expires_at)
                if expire_date < get_moscow_time():
                    c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
                    conn.commit()
                    return 0
            return level

    def add_admin(self, user_id, level, added_by, delta: timedelta = None):
        expires_at = None
        if delta:
            expires_at = (get_moscow_time() + delta).isoformat()
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO admins (user_id, level, added_by, added_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                     (user_id, level, added_by, get_moscow_time().isoformat(), expires_at))
            conn.commit()

    def remove_admin(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_all_admins(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, level, added_at, expires_at FROM admins ORDER BY level DESC")
            return c.fetchall()

    # ----- КОДЫ И ПРОМОКОДЫ -----
    def create_admin_code(self, code, level, duration, max_uses, created_by):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM used_promos WHERE code = ?", (code,))
            c.execute("DELETE FROM banned_user_promos WHERE code = ?", (code,))
            c.execute("INSERT OR REPLACE INTO admin_codes (code, level, duration, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (code, level, duration, max_uses, created_by, get_moscow_time().isoformat()))
            conn.commit()

    def is_promo_banned(self, user_id, code):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM banned_user_promos WHERE user_id = ? AND code = ?", (user_id, code))
            return c.fetchone() is not None

    def ban_user_promo(self, user_id, code):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO banned_user_promos (user_id, code) VALUES (?, ?)", (user_id, code))
            conn.commit()

    def delete_banned_user_promo(self, code):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM banned_user_promos WHERE code = ?", (code,))
            conn.commit()

    def has_user_used_code(self, user_id, code):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM used_promos WHERE user_id = ? AND code = ?", (user_id, code))
            return c.fetchone() is not None

    def record_code_use(self, user_id, code):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO used_promos (user_id, code, used_at) VALUES (?, ?, ?)",
                     (user_id, code, get_moscow_time().isoformat()))
            c.execute("UPDATE admin_codes SET used = used + 1 WHERE code = ?", (code,))
            
            c.execute("SELECT max_uses, used FROM admin_codes WHERE code = ?", (code,))
            row = c.fetchone()
            if row and row[1] >= row[0]:
                c.execute("DELETE FROM admin_codes WHERE code = ?", (code,))
                c.execute("DELETE FROM banned_user_promos WHERE code = ?", (code,))
                c.execute("DELETE FROM used_promos WHERE code = ?", (code,))
            conn.commit()

    def use_admin_code(self, code, user_id, allow_repeat=False):
        if self.is_promo_banned(user_id, code):
            return "banned", "❌ Вам запрещен ввод этого промокода!"

        code_info = None
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT level, duration, max_uses, used FROM admin_codes WHERE code = ?", (code,))
            code_info = c.fetchone()

        if not code_info:
            return "error", "❌ Код не найден"
        
        code_level, duration_str, max_uses, used = code_info
        if used >= max_uses:
            self.delete_code(code)
            return "error", "❌ Код не найден"

        current_level = self.get_admin_level(user_id)
        if not allow_repeat and current_level >= code_level and current_level > 0:
            return "error", f"Ваш текущий уровень ({current_level}) равен или выше уровня кода ({code_level})"

        if not allow_repeat and self.has_user_used_code(user_id, code):
            return "repeat", code_level

        delta = parse_time_string(duration_str) if duration_str else None
        self.add_admin(user_id, code_level, 0, delta)
        self.record_code_use(user_id, code)
        return "success", f"✅ Поздравляем! Вы получили уровень: {LEVEL_NAMES.get(code_level, 'Админ')}!"

    def create_promo_request(self, user_id, code):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO promo_requests (user_id, code) VALUES (?, ?)", (user_id, code))
            conn.commit()
            return c.lastrowid

    def get_all_codes(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT code, level, duration, max_uses, used, created_at FROM admin_codes ORDER BY created_at DESC")
            return c.fetchall()

    def delete_code(self, code):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM admin_codes WHERE code = ?", (code,))
            c.execute("DELETE FROM banned_user_promos WHERE code = ?", (code,))
            c.execute("DELETE FROM used_promos WHERE code = ?", (code,))
            conn.commit()

    # ----- СТАТИСТИКА И РЕЙТИНГ АДМИНИСТРАТОРОВ -----
    def get_admin_stats(self, admin_id, period_days=None):
        with get_db_connection() as conn:
            c = conn.cursor()
            params_msg = [admin_id]
            params_close = [admin_id]
            params_apps = [admin_id]
            
            if period_days is not None:
                start_date = (get_moscow_time() - timedelta(days=period_days)).isoformat()
                where_msg = "WHERE user_id = ? AND is_admin = 1 AND created_at >= ?"
                where_close = "WHERE closed_by = ? AND closed_at >= ?"
                where_apps = "WHERE reviewed_by = ? AND reviewed_at >= ?"
                params_msg.append(start_date)
                params_close.append(start_date)
                params_apps.append(start_date)
            else:
                where_msg = "WHERE user_id = ? AND is_admin = 1"
                where_close = "WHERE closed_by = ?"
                where_apps = "WHERE reviewed_by = ?"

            c.execute(f"SELECT COUNT(*) FROM ticket_messages {where_msg}", params_msg)
            ticket_msgs = c.fetchone()[0]

            c.execute(f"SELECT COUNT(*) FROM tickets {where_close}", params_close)
            closed_tickets = c.fetchone()[0]

            c.execute(f"SELECT COUNT(*) FROM applications {where_apps} AND status = 'approved'", params_apps)
            approved_apps = c.fetchone()[0]

            c.execute(f"SELECT COUNT(*) FROM applications {where_apps} AND status = 'rejected'", params_apps)
            rejected_apps = c.fetchone()[0]

            c.execute(f"SELECT AVG(rating), COUNT(rating) FROM tickets {where_close} AND rating > 0", params_close)
            avg_row = c.fetchone()
            avg_rating = round(avg_row[0], 1) if avg_row and avg_row[0] else 0.0
            rating_count = avg_row[1] if avg_row else 0

            return {
                'ticket_messages': ticket_msgs,
                'closed_tickets': closed_tickets,
                'approved_apps': approved_apps,
                'rejected_apps': rejected_apps,
                'avg_rating': avg_rating,
                'rating_count': rating_count
            }

    def get_admin_reviews(self, admin_id, limit=5, offset=0):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, user_id, rating, rating_feedback, closed_at 
                FROM tickets 
                WHERE closed_by = ? AND rating > 0 
                ORDER BY closed_at DESC LIMIT ? OFFSET ?
            """, (admin_id, limit, offset))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) FROM tickets WHERE closed_by = ? AND rating > 0", (admin_id,))
            total = c.fetchone()[0]
            return rows, total

    # ----- АНКЕТЫ -----
    def create_application(self, user_id, username, text):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO applications (user_id, username, text, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, username, text, get_moscow_time().isoformat()))
            conn.commit()
            return c.lastrowid

    def get_pending_applications(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, username, text, created_at FROM applications WHERE status = 'pending' ORDER BY created_at ASC")
            return c.fetchall()

    def get_application(self, app_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, username, text, status, created_at, reviewed_at, review_reason, reviewed_by FROM applications WHERE id = ?", (app_id,))
            return c.fetchone()

    def approve_application(self, app_id, admin_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE applications SET status = 'approved', reviewed_at = ?, reviewed_by = ? WHERE id = ?", 
                     (get_moscow_time().isoformat(), admin_id, app_id))
            conn.commit()

    def reject_application(self, app_id, admin_id, reason):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE applications SET status = 'rejected', reviewed_at = ?, review_reason = ?, reviewed_by = ? WHERE id = ?", 
                     (get_moscow_time().isoformat(), reason, admin_id, app_id))
            conn.commit()

    def get_applications_stats_by_date(self, date_str):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT status FROM applications WHERE date(created_at) = ?", (date_str,))
            rows = c.fetchall()
            total = len(rows)
            pending = sum(1 for r in rows if r[0] == 'pending')
            approved = sum(1 for r in rows if r[0] == 'approved')
            rejected = sum(1 for r in rows if r[0] == 'rejected')
            return {'total': total, 'pending': pending, 'approved': approved, 'rejected': rejected, 'processed': approved + rejected}

    def get_user_applications_by_date(self, user_id, date_str):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT status FROM applications WHERE user_id = ? AND date(created_at) = ?", (user_id, date_str))
            return c.fetchall()

    # ----- ТИКЕТЫ (С НАЗНАЧЕНИЕМ АДМИНА) -----
    def create_ticket(self, user_id, category):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO tickets (user_id, category, created_at, updated_at) VALUES (?, ?, ?, ?)",
                     (user_id, category, get_moscow_time().isoformat(), get_moscow_time().isoformat()))
            conn.commit()
            return c.lastrowid

    def get_ticket(self, ticket_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, status, created_at, closed_by, closed_at, category, rating, rating_feedback, assigned_to FROM tickets WHERE id = ?", (ticket_id,))
            return c.fetchone()

    def assign_ticket(self, ticket_id, admin_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE tickets SET assigned_to = ? WHERE id = ? AND assigned_to IS NULL", (admin_id, ticket_id))
            conn.commit()

    def unassign_ticket(self, ticket_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE tickets SET assigned_to = NULL WHERE id = ?", (ticket_id,))
            conn.commit()

    def get_user_open_tickets(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, status, created_at, category, assigned_to FROM tickets WHERE user_id = ? AND status = 'open' ORDER BY created_at DESC", (user_id,))
            return c.fetchall()

    def get_all_open_tickets(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, created_at, category, assigned_to FROM tickets WHERE status = 'open' ORDER BY created_at ASC")
            return c.fetchall()

    def add_ticket_message(self, ticket_id, user_id, message, is_admin=False):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO ticket_messages (ticket_id, user_id, message, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                     (ticket_id, user_id, message, 1 if is_admin else 0, get_moscow_time().isoformat()))
            c.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (get_moscow_time().isoformat(), ticket_id))
            conn.commit()

    def get_ticket_messages(self, ticket_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, message, is_admin, created_at FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at ASC", 
                     (ticket_id,))
            return c.fetchall()

    def close_ticket(self, ticket_id, admin_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE tickets SET status = 'closed', closed_by = ?, closed_at = ? WHERE id = ?", 
                     (admin_id, get_moscow_time().isoformat(), ticket_id))
            conn.commit()

    def set_ticket_rating(self, ticket_id, rating, feedback=None):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE tickets SET rating = ?, rating_feedback = ? WHERE id = ?", (rating, feedback, ticket_id))
            conn.commit()

    # ----- АПЕЛЛЯЦИИ -----
    def is_appeal_blocked(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM appeal_bans WHERE user_id = ?", (user_id,))
            return c.fetchone() is not None

    def block_appeals_for_user(self, user_id, admin_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO appeal_bans (user_id, banned_by, banned_at) VALUES (?, ?, ?)",
                     (user_id, admin_id, get_moscow_time().isoformat()))
            conn.commit()

    def has_pending_appeal(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM appeals WHERE user_id = ? AND status = 'pending'", (user_id,))
            return c.fetchone() is not None

    def get_last_rejected_appeal(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT reviewed_at FROM appeals WHERE user_id = ? AND status = 'rejected' ORDER BY reviewed_at DESC LIMIT 1", (user_id,))
            row = c.fetchone()
            return row[0] if row else None

    def create_appeal(self, user_id, username, text):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO appeals (user_id, username, text, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, username, text, get_moscow_time().isoformat()))
            conn.commit()
            return c.lastrowid

    def get_pending_appeals(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, username, text, created_at FROM appeals WHERE status = 'pending' ORDER BY created_at ASC")
            return c.fetchall()

    def get_appeal(self, appeal_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, username, text, status, created_at, reviewed_by, reviewed_at FROM appeals WHERE id = ?", (appeal_id,))
            return c.fetchone()

    def approve_appeal(self, appeal_id, admin_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE appeals SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                     (admin_id, get_moscow_time().isoformat(), appeal_id))
            conn.commit()

    def reject_appeal(self, appeal_id, admin_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE appeals SET status = 'rejected', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                     (admin_id, get_moscow_time().isoformat(), appeal_id))
            conn.commit()

    # ----- АРХИВЫ -----
    def get_archived_applications(self, limit=5, offset=0):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, username, status, created_at, reviewed_by FROM applications ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) FROM applications")
            total = c.fetchone()[0]
            return rows, total

    def get_archived_tickets(self, limit=5, offset=0):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, status, created_at, closed_by, category FROM tickets ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) FROM tickets")
            total = c.fetchone()[0]
            return rows, total

    # ----- ЧЁРНЫЙ СПИСОК -----
    def is_banned(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,))
            return c.fetchone() is not None

    def add_to_blacklist(self, user_id, reason):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO blacklist (user_id, reason, banned_at) VALUES (?, ?, ?)",
                     (user_id, reason, get_moscow_time().isoformat()))
            conn.commit()

    def remove_from_blacklist(self, user_id):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_blacklist(self):
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, reason, banned_at FROM blacklist")
            return c.fetchall()

db = Database()

# ========== ИНИЦИАЛИЗА БОТА ==========
storage = StateMemoryStorage()
bot = telebot.TeleBot(BOT_TOKEN, state_storage=storage)
bot.add_custom_filter(custom_filters.StateFilter(bot))

# ========== FSM СТЕЙТЫ ==========
class ApplicationState(StatesGroup):
    waiting_for_text = State()

class AppealState(StatesGroup):
    waiting_for_text = State()

class TicketState(StatesGroup):
    waiting_for_category = State()
    waiting_for_message = State()

class TicketRatingState(StatesGroup):
    waiting_for_feedback = State()

class UserTicketReplyState(StatesGroup):
    waiting_for_message = State()

class CodeState(StatesGroup):
    waiting_for_code = State()

class AdminReplyState(StatesGroup):
    waiting_for_reply = State()

class RejectReasonState(StatesGroup):
    waiting_for_reason = State()

class AddAdminState(StatesGroup):
    waiting_for_username = State()
    waiting_for_time = State()

class RemoveAdminState(StatesGroup):
    waiting_for_username = State()

class CreateCodeState(StatesGroup):
    waiting_for_code_name = State()
    waiting_for_time = State()
    waiting_for_max_uses = State()

class DeleteCodeState(StatesGroup):
    waiting_for_code = State()

class BlacklistAddState(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()

class BlacklistRemoveState(StatesGroup):
    waiting_for_username = State()

class BroadcastState(StatesGroup):
    waiting_for_post = State()

# ========== ХЕЛПЕР СТАТУС-БАРА ТИКЕТА ==========
def get_ticket_status_badge(status: str, assigned_to: int) -> str:
    if status == 'closed':
        return "🔴 [Закрыт]"
    elif assigned_to:
        admin_uname = db.get_username(assigned_to)
        return f"🔵 [В работе: @{clean_html(admin_uname)}]"
    else:
        return "🟡 [В ожидании]"

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📝 Написать анкету", "📩 Написать тикет")
    kb.row("🛡 Апелляция", "📅 Календарь анкет", "🎫 Код")
    if db.get_admin_level(user_id) >= 1:
        kb.row("👑 Админ панель")
    return kb

def get_admin_keyboard(level, user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if level >= 1:
        kb.row("📝 Анкеты (новые)")
    if level >= 2:
        kb.row("📋 Тикеты (открытые)")
    if level >= 3:
        kb.row("🛡 Апелляции (новые)")
        kb.row("📊 Рейтинг администратора", "🗄 Архив")
        kb.row("🚫 Чёрный список")
    if level >= 4:
        kb.row("📢 Рассылка", "👑 Управление админами")
        kb.row("🎫 Коды для админки")
        if user_id in MASTER_ADMIN_IDS:
            kb.row("💾 Скачать БД")
    kb.row("🔙 Выйти в меню")
    return kb

def get_ticket_categories_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("❓ Вопрос", callback_data="ticket_cat_Вопрос"),
        types.InlineKeyboardButton("⚠️ Жалоба", callback_data="ticket_cat_Жалоба"),
        types.InlineKeyboardButton("🤝 Сотрудничество", callback_data="ticket_cat_Сотрудничество"),
        types.InlineKeyboardButton("💡 Предложение", callback_data="ticket_cat_Предложение"),
        types.InlineKeyboardButton("📁 Другое", callback_data="ticket_cat_Другое")
    )
    return kb

def get_ticket_rating_keyboard(ticket_id):
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.add(
        types.InlineKeyboardButton("⭐ 1", callback_data=f"rate_ticket_{ticket_id}_1"),
        types.InlineKeyboardButton("⭐ 2", callback_data=f"rate_ticket_{ticket_id}_2"),
        types.InlineKeyboardButton("⭐ 3", callback_data=f"rate_ticket_{ticket_id}_3"),
        types.InlineKeyboardButton("⭐ 4", callback_data=f"rate_ticket_{ticket_id}_4"),
        types.InlineKeyboardButton("⭐ 5", callback_data=f"rate_ticket_{ticket_id}_5")
    )
    return kb

def get_level_inline_keyboard(prefix: str):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("1 — Админ", callback_data=f"{prefix}_lvl_1"),
        types.InlineKeyboardButton("2 — Ст.админ", callback_data=f"{prefix}_lvl_2"),
        types.InlineKeyboardButton("3 — Куратор", callback_data=f"{prefix}_lvl_3"),
        types.InlineKeyboardButton("4 — Владелец", callback_data=f"{prefix}_lvl_4")
    )
    return kb

def get_admin_manage_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("➕ Добавить админа", callback_data="add_admin"),
        types.InlineKeyboardButton("➖ Убрать админа", callback_data="remove_admin")
    )
    kb.row(
        types.InlineKeyboardButton("📋 Список админов", callback_data="list_admins"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin")
    )
    return kb

def get_codes_manage_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("➕ Создать код", callback_data="create_code"),
        types.InlineKeyboardButton("📋 Список кодов", callback_data="list_codes")
    )
    kb.row(
        types.InlineKeyboardButton("🗑 Удалить код", callback_data="delete_code"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin")
    )
    return kb

def get_blacklist_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("➕ Добавить в ЧС", callback_data="blacklist_add"),
        types.InlineKeyboardButton("➖ Удалить из ЧС", callback_data="blacklist_remove")
    )
    kb.row(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin"))
    return kb

def get_ticket_actions_keyboard(ticket_id, assigned_to=None, current_admin_level=1):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("💬 Ответить", callback_data=f"ticket_reply_{ticket_id}"),
        types.InlineKeyboardButton("❌ Закрыть", callback_data=f"ticket_close_{ticket_id}")
    )
    if assigned_to:
        kb.row(types.InlineKeyboardButton("🆘 Попросить помощь", callback_data=f"ticket_help_{ticket_id}"))
    if assigned_to and current_admin_level >= 4:
        kb.row(types.InlineKeyboardButton("🔓 Снять лок (Сбросить)", callback_data=f"ticket_unlock_{ticket_id}"))
    return kb

def get_application_actions_keyboard(app_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"app_approve_{app_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"app_reject_{app_id}")
    )
    return kb

def get_appeal_actions_keyboard(appeal_id, user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"appeal_approve_{appeal_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"appeal_reject_{appeal_id}")
    )
    kb.add(
        types.InlineKeyboardButton("🚫 Заблокировать апелляции", callback_data=f"appeal_block_user_{appeal_id}_{user_id}")
    )
    return kb

# ========== КАЛЕНДАРЬ ==========
MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}

def build_calendar_keyboard(year, month):
    kb = types.InlineKeyboardMarkup(row_width=7)
    kb.add(types.InlineKeyboardButton(text=f"📅 {MONTHS_RU[month]} {year}", callback_data=f"cal_select_ym_{year}_{month}"))
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    kb.add(*[types.InlineKeyboardButton(text=day, callback_data="cal_ignore") for day in week_days])
    
    first_day, days_in_month = monthrange(year, month)
    today = get_moscow_time().date()
    
    row = []
    for _ in range(first_day - 1):
        row.append(types.InlineKeyboardButton(text=" ", callback_data="cal_ignore"))
    
    for day in range(1, days_in_month + 1):
        date_obj = datetime(year, month, day).date()
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        
        if date_obj > today:
            btn_text = f"{day}"
        else:
            stats = db.get_applications_stats_by_date(date_str)
            processed = stats['processed']
            total = stats['total']
            percent = round((processed / total * 100)) if total > 0 else 0
            
            if total == 0: color = "⬛"
            elif percent == 0: color = "🟥"
            elif 1 <= percent <= 20: color = "🟧"
            elif 21 <= percent <= 40: color = "🟨"
            elif 41 <= percent <= 60: color = "🟩"
            elif 61 <= percent <= 80: color = "🟦"
            elif 81 <= percent <= 99: color = "🟪"
            else: color = "⬜"
            btn_text = f"{color} {day}"
        
        row.append(types.InlineKeyboardButton(text=btn_text, callback_data=f"cal_day_{date_str}"))
        if len(row) == 7:
            kb.add(*row)
            row = []
    if row: kb.add(*row)
    kb.add(
        types.InlineKeyboardButton("◀️", callback_data=f"cal_prev_{year}_{month}"),
        types.InlineKeyboardButton("Сегодня", callback_data="cal_today"),
        types.InlineKeyboardButton("▶️", callback_data=f"cal_next_{year}_{month}")
    )
    return kb

def build_month_year_selector(year):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.row(
        types.InlineKeyboardButton("◀️", callback_data=f"cal_year_change_{year - 1}"),
        types.InlineKeyboardButton(f"🗓 {year} год", callback_data="cal_ignore"),
        types.InlineKeyboardButton("▶️", callback_data=f"cal_year_change_{year + 1}")
    )
    month_buttons = [types.InlineKeyboardButton(text=MONTHS_RU[m], callback_data=f"cal_set_month_{year}_{m}") for m in range(1, 13)]
    kb.add(*month_buttons)
    now = get_moscow_time()
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data=f"cal_back_to_{now.year}_{now.month}"))
    return kb

# ========== СТАРТ ==========
@bot.message_handler(commands=['start'], chat_types=['private'])
def cmd_start(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"User_{user_id}"
    db.add_user(user_id, username)
    
    if db.is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 **Вы в чёрном списке!** При необходимости подайте апелляцию.", reply_markup=get_main_keyboard(user_id))
        return
    
    bot.send_message(
        message.chat.id,
        "👋 **Добро пожаловать в приёмную Cult Flood!**\n\n"
        "✨ Здесь ты можешь:\n"
        "• **📝 Написать анкету** — подать заявку на вступление\n"
        "• **📩 Написать тикет** — задать вопрос или решить проблему\n"
        "• **🛡 Апелляция** — обжаловать решение\n"
        "• **📅 Календарь анкет** — посмотреть статистику по дням\n"
        "• **🎫 Код** — ввести специальный код для админки\n\n"
        "📌 После одобрения анкеты ты получишь ссылку на вход в чат!",
        reply_markup=get_main_keyboard(user_id)
    )

# ========== СКАЧАТЬ БД (LEVEL 4 + MASTER ADMIN) ==========
@bot.message_handler(func=lambda msg: msg.text == "💾 Скачать БД", chat_types=['private'])
def download_db(message):
    user_id = message.from_user.id
    if user_id not in MASTER_ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Доступ запрещен (Только для добавленных по ID)!")
        return
    try:
        with open(DB_PATH, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption="💾 Актуальная выгрузка базы данных.")
    except Exception as e:
        logger.error(f"Ошибка при выгрузке БД: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка отправки файла БД: {e}")

# ========== РАССЫЛКА (LEVEL 4) ==========
@bot.message_handler(func=lambda msg: msg.text == "📢 Рассылка", chat_types=['private'])
def start_broadcast(message):
    if db.get_admin_level(message.from_user.id) < 4:
        bot.send_message(message.chat.id, "❌ Доступ запрещен (нужен уровень 4)!")
        return
    
    bot.set_state(message.from_user.id, BroadcastState.waiting_for_post, message.chat.id)
    bot.send_message(
        message.chat.id,
        "📢 **Отправьте пост для рассылки всем пользователям бота:**\n\n"
        "Поддерживаются: текст, фото/видео с подписью или одиночные медиа без текста.\n\n"
        "❌ Отмена — /cancel"
    )

@bot.message_handler(state=BroadcastState.waiting_for_post, content_types=['text', 'photo', 'video', 'animation'], chat_types=['private'])
def process_broadcast(message):
    admin_id = message.from_user.id
    if message.text == "/cancel":
        bot.delete_state(admin_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_admin_keyboard(4, admin_id))
        return

    users = db.get_all_users()
    bot.send_message(message.chat.id, f"🚀 Начинаю рассылку на **{len(users)}** пользователей...")
    
    success = 0
    failed = 0
    for uid in users:
        try:
            bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            time.sleep(0.05)
        except Exception:
            failed += 1

    bot.delete_state(admin_id, message.chat.id)
    bot.send_message(
        message.chat.id,
        f"✅ **Рассылка завершена!**\n\nУспешно отправлено: `{success}`\nОшибок/заблокировали: `{failed}`",
        reply_markup=get_admin_keyboard(4, admin_id),
        parse_mode="Markdown"
    )

# ========== РЕЙТИНГ АДМИНИСТРАТОРОВ (LEVEL 3+) ==========
@bot.message_handler(func=lambda msg: msg.text == "📊 Рейтинг администратора", chat_types=['private'])
def admin_rating_menu(message):
    if db.get_admin_level(message.from_user.id) < 3:
        bot.send_message(message.chat.id, "❌ Нет доступа (нужен уровень 3+ Куратор)")
        return
    
    admins = db.get_all_admins()
    kb = types.InlineKeyboardMarkup()
    for aid, lvl, _, _ in admins:
        uname = db.get_username(aid)
        kb.add(types.InlineKeyboardButton(f"👑 @{uname} ({LEVEL_NAMES.get(lvl, 'Админ')})", callback_data=f"stat_adm_{aid}"))
    
    bot.send_message(message.chat.id, "📊 **Выберите администратора для просмотра статистики:**", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("stat_adm_"))
def select_admin_stat_period(call):
    admin_id = int(call.data.split("_")[2])
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📅 За день", callback_data=f"stat_period_{admin_id}_1"),
        types.InlineKeyboardButton("🗓 За неделю", callback_data=f"stat_period_{admin_id}_7")
    )
    kb.row(
        types.InlineKeyboardButton("📆 За месяц", callback_data=f"stat_period_{admin_id}_30"),
        types.InlineKeyboardButton("♾ За всё время", callback_data=f"stat_period_{admin_id}_all")
    )
    kb.row(types.InlineKeyboardButton("🔙 Назад к выбору", callback_data="back_to_rating_list"))
    
    uname = db.get_username(admin_id)
    bot.edit_message_text(f"📊 Выберите период статистики для @{clean_html(uname)}:", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_rating_list")
def back_to_rating_list_cb(call):
    admins = db.get_all_admins()
    kb = types.InlineKeyboardMarkup()
    for aid, lvl, _, _ in admins:
        uname = db.get_username(aid)
        kb.add(types.InlineKeyboardButton(f"👑 @{uname} ({LEVEL_NAMES.get(lvl, 'Админ')})", callback_data=f"stat_adm_{aid}"))
    bot.edit_message_text("📊 **Выберите администратора для просмотра статистики:**", call.message.chat.id, call.message.message_id, reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("stat_period_"))
def show_admin_stats(call):
    parts = call.data.split("_")
    admin_id = int(parts[2])
    period = parts[3]
    
    days = int(period) if period != "all" else None
    period_title = "за все время" if period == "all" else f"за последние {days} дн."
    
    stats = db.get_admin_stats(admin_id, days)
    uname = db.get_username(admin_id)
    lvl = db.get_admin_level(admin_id)
    
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("💬 Все отзывы", callback_data=f"adm_reviews_{admin_id}_0"))
    kb.row(types.InlineKeyboardButton("🔙 Изменить период", callback_data=f"stat_adm_{admin_id}"))
    
    text = (
        f"📊 <b>СТАТИСТИКА АДМИНИСТРАТОРА</b>\n\n"
        f"👑 <b>Админ:</b> @{clean_html(uname)} (ID: <code>{admin_id}</code>)\n"
        f"🔰 <b>Уровень:</b> {LEVEL_NAMES.get(lvl, 'Админ')}\n"
        f"⏳ <b>Период:</b> {period_title}\n\n"
        f"⭐ <b>Средняя оценка:</b> {stats['avg_rating']} / 5.0 ({stats['rating_count']} оценок)\n"
        f"💬 <b>Ответов в тикетах:</b> {stats['ticket_messages']}\n"
        f"🔒 <b>Закрыто тикетов:</b> {stats['closed_tickets']}\n"
        f"✅ <b>Принято анкет:</b> {stats['approved_apps']}\n"
        f"❌ <b>Отказано анкет:</b> {stats['rejected_apps']}"
    )
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_reviews_"))
def show_admin_reviews(call):
    parts = call.data.split("_")
    admin_id = int(parts[2])
    page = int(parts[3])
    limit = 5
    offset = page * limit

    reviews, total = db.get_admin_reviews(admin_id, limit, offset)
    uname = db.get_username(admin_id)

    if not reviews:
        bot.answer_callback_query(call.id, "У этого администратора пока нет отзывов!", show_alert=True)
        return

    text = f"💬 <b>ОТЗЫВЫ ПОДДЕРЖКИ — @{clean_html(uname)} (Стр. {page + 1}):</b>\n\n"
    for tid, uid, rating, feedback, closed_at in reviews:
        user_uname = db.get_username(uid)
        fb_text = f"\n💬 <i>«{clean_html(feedback)}»</i>" if feedback else ""
        text += f"🎫 <b>Тикет #{tid}</b> | От: @{clean_html(user_uname)}\n⭐ Оценка: {rating}/5{fb_text}\n\n"

    kb = types.InlineKeyboardMarkup()
    nav_btns = []
    if page > 0:
        nav_btns.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"adm_reviews_{admin_id}_{page - 1}"))
    if (page + 1) * limit < total:
        nav_btns.append(types.InlineKeyboardButton("Вперёд ▶️", callback_data=f"adm_reviews_{admin_id}_{page + 1}"))
    if nav_btns:
        kb.row(*nav_btns)
    kb.row(types.InlineKeyboardButton("🔙 К статистике", callback_data=f"stat_adm_{admin_id}"))

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

# ========== АНКЕТА ==========
@bot.message_handler(func=lambda msg: msg.text == "📝 Написать анкету", chat_types=['private'])
def start_application(message):
    user_id = message.from_user.id
    if db.is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 Вы в чёрном списке!")
        return
    
    bot.set_state(user_id, ApplicationState.waiting_for_text, message.chat.id)
    bot.send_message(
        message.chat.id,
        "📝 **Заполни анкету по шаблону:**\n\n"
        "```\n1. Ваш @username:\n2. Ваша роль:\n3. Ваш фандом:\n```\n\n"
        "📌 Отправь заполненный шаблон **одним сообщением**.\n"
        "❌ Отмена — /cancel",
        parse_mode="Markdown"
    )

@bot.message_handler(state=ApplicationState.waiting_for_text, chat_types=['private'])
def process_application(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"User_{user_id}"
    text = message.text
    
    if text == "/cancel":
        bot.delete_state(user_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return
    
    app_id = db.create_application(user_id, username, text)
    bot.delete_state(user_id, message.chat.id)
    
    safe_username = clean_html(username)
    safe_text = clean_html(text)
    
    admins = db.get_all_admins()
    for admin_id, level, _, _ in admins:
        if level >= 1:
            try:
                msg = bot.send_message(
                    admin_id,
                    f"📝 <b>НОВАЯ АНКЕТА #{app_id}</b>\n\n"
                    f"👤 От: @{safe_username} (ID: <code>{user_id}</code>)\n"
                    f"📅 Время: {get_moscow_time().strftime('%d.%m.%Y %H:%M')} МСК\n\n"
                    f"📋 <b>Текст:</b>\n{safe_text[:1000]}",
                    parse_mode="HTML",
                    reply_markup=get_application_actions_keyboard(app_id)
                )
                db.add_notification("application", app_id, admin_id, msg.message_id)
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    
    bot.send_message(
        message.chat.id,
        f"✅ **Анкета #{app_id} отправлена!**\n\nАдмины расссмотрят её в ближайшее время.",
        reply_markup=get_main_keyboard(user_id)
    )

# ========== АПЕЛЛЯЦИИ (ТОЛЬКО ДЛЯ ЧС + 24 ЧАСА ОГРАНИЧЕНИЕ) ==========
@bot.message_handler(func=lambda msg: msg.text == "🛡 Апелляция", chat_types=['private'])
def start_appeal(message):
    user_id = message.from_user.id
    
    # 1. Проверка: Доступ только заблокированным в ЧС!
    if not db.is_banned(user_id):
        bot.send_message(
            message.chat.id,
            "❌ **Апелляция доступна только заблокированным пользователям (находящимся в чёрном списке)!**",
            reply_markup=get_main_keyboard(user_id)
        )
        return

    # 2. Проверка: Заблокированы ли апелляции кураторами навсегда
    if db.is_appeal_blocked(user_id):
        bot.send_message(
            message.chat.id,
            "❌ **Доступ к подаче апелляций заблокирован для вас администрацией!**",
            reply_markup=get_main_keyboard(user_id)
        )
        return

    # 3. Проверка: Заявка уже находится в процессе рассмотрения
    if db.has_pending_appeal(user_id):
        bot.send_message(
            message.chat.id,
            "❌ **Ваша предыдущая апелляция уже находится на рассмотрении!**\nОжидайте ответа кураторов.",
            reply_markup=get_main_keyboard(user_id)
        )
        return

    # 4. Проверка: 24 часа с момента последнего отклонения
    last_rejected = db.get_last_rejected_appeal(user_id)
    if last_rejected:
        rejected_time = datetime.fromisoformat(last_rejected)
        diff = get_moscow_time() - rejected_time
        if diff < timedelta(hours=24):
            remaining = timedelta(hours=24) - diff
            hours, remainder = divmod(remaining.seconds, 3600)
            minutes = remainder // 60
            bot.send_message(
                message.chat.id,
                f"❌ **Вы можете подать повторную апелляцию только через {hours} ч. {minutes} мин.**\n"
                f"📌 Повторная подача возможна ровно через 24 часа после отклонения.",
                reply_markup=get_main_keyboard(user_id)
            )
            return

    bot.set_state(user_id, AppealState.waiting_for_text, message.chat.id)
    bot.send_message(
        message.chat.id,
        "🛡 **Опишите суть вашей апелляции:**\n\n"
        "Укажите причину обращения и подробности ситуации.\n\n"
        "❌ Отмена — /cancel"
    )

@bot.message_handler(state=AppealState.waiting_for_text, chat_types=['private'])
def process_appeal(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"User_{user_id}"
    text = message.text

    if text == "/cancel":
        bot.delete_state(user_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return

    appeal_id = db.create_appeal(user_id, username, text)
    bot.delete_state(user_id, message.chat.id)

    safe_username = clean_html(username)
    safe_text = clean_html(text)

    admins = db.get_all_admins()
    for admin_id, level, _, _ in admins:
        if level >= 3:  # Кураторам и выше
            try:
                msg = bot.send_message(
                    admin_id,
                    f"🛡 <b>НОВАЯ АПЕЛЛЯЦИЯ #{appeal_id}</b>\n\n"
                    f"👤 Пользователь: @{safe_username} (ID: <code>{user_id}</code>)\n"
                    f"📅 Время: {get_moscow_time().strftime('%d.%m.%Y %H:%M')} МСК\n\n"
                    f"📝 <b>Текст апелляции:</b>\n{safe_text[:1000]}",
                    parse_mode="HTML",
                    reply_markup=get_appeal_actions_keyboard(appeal_id, user_id)
                )
                db.add_notification("appeal", appeal_id, admin_id, msg.message_id)
            except Exception as e:
                logger.error(f"Не удалось отправить апелляцию админу {admin_id}: {e}")

    bot.send_message(
        message.chat.id,
        f"✅ **Апелляция #{appeal_id} отправлена!**\n\nКураторы расссмотрят её в ближайшее время.",
        reply_markup=get_main_keyboard(user_id)
    )

# ========== ОБРАБОТЧИКИ ДЕЙСТВИЙ С АПЕЛЛЯЦИЯМИ ==========
@bot.message_handler(func=lambda msg: msg.text == "🛡 Апелляции (новые)", chat_types=['private'])
def admin_appeals(message):
    if db.get_admin_level(message.from_user.id) < 3:
        return
    appeals = db.get_pending_appeals()
    if not appeals:
        bot.send_message(message.chat.id, "📭 Нет новых апелляций")
        return
    for appeal_id, uid, uname, text, created_at in appeals:
        date_formatted = datetime.fromisoformat(created_at).strftime('%d.%m.%Y %H:%M')
        bot.send_message(
            message.chat.id,
            f"🛡 <b>Апелляция #{appeal_id}</b>\n"
            f"👤 @{clean_html(uname)} (ID: <code>{uid}</code>)\n"
            f"📅 {date_formatted} МСК\n\n"
            f"📝 {clean_html(text)}",
            reply_markup=get_appeal_actions_keyboard(appeal_id, uid),
            parse_mode="HTML"
        )

# Одобрение апелляции (Снятие ЧС)
@bot.callback_query_handler(func=lambda call: call.data.startswith("appeal_approve_"))
def approve_appeal_cb(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 3:
        bot.answer_callback_query(call.id, "❌ Только для уровня 3+ (Куратор)", show_alert=True)
        return

    appeal_id = int(call.data.split("_")[2])
    appeal = db.get_appeal(appeal_id)
    if not appeal or appeal[4] != 'pending':
        bot.answer_callback_query(call.id, "❌ Апелляция уже была рассмотрена!", show_alert=True)
        return

    target_user_id = appeal[1]
    db.approve_appeal(appeal_id, admin_id)
    db.remove_from_blacklist(target_user_id)

    admin_uname = call.from_user.username or call.from_user.first_name
    target_uname = db.get_username(target_user_id)

    updated_text = (
        f"✅ <b>АПЕЛЛЯЦИЯ #{appeal_id} ОДОБРЕНА</b>\n\n"
        f"👤 Пользователь: @{clean_html(target_uname)} (ID: <code>{target_user_id}</code>)\n"
        f"👑 Одобрил куратор: @{clean_html(admin_uname)}\n\n"
        f"🔓 <b>Пользователь успешно удалён из ЧЁРНОГО СПИСКА!</b>"
    )

    notifications = db.get_notifications("appeal", appeal_id)
    for notif_admin_id, msg_id in notifications:
        try:
            bot.edit_message_text(updated_text, notif_admin_id, msg_id, parse_mode="HTML", reply_markup=None)
        except Exception as e:
            logger.error(f"Ошибка редактирования карты апелляции: {e}")

    db.delete_notifications("appeal", appeal_id)

    try:
        bot.send_message(target_user_id, "🎉 **Ваша апелляция одобрена!**\n\nВы успешно удалены из чёрного списка и снова можете пользоваться функциями бота.")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление {target_user_id}: {e}")

    bot.answer_callback_query(call.id, "Апелляция одобрена, пользователь разбанен!", show_alert=True)

# Отклонение апелляции (Таймер 24 часа)
@bot.callback_query_handler(func=lambda call: call.data.startswith("appeal_reject_"))
def reject_appeal_cb(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 3:
        bot.answer_callback_query(call.id, "❌ Только для уровня 3+ (Куратор)", show_alert=True)
        return

    appeal_id = int(call.data.split("_")[2])
    appeal = db.get_appeal(appeal_id)
    if not appeal or appeal[4] != 'pending':
        bot.answer_callback_query(call.id, "❌ Апелляция уже была рассмотрена!", show_alert=True)
        return

    target_user_id = appeal[1]
    db.reject_appeal(appeal_id, admin_id)

    admin_uname = call.from_user.username or call.from_user.first_name
    target_uname = db.get_username(target_user_id)

    updated_text = (
        f"❌ <b>АПЕЛЛЯЦИЯ #{appeal_id} ОТКЛОНЕНА</b>\n\n"
        f"👤 Пользователь: @{clean_html(target_uname)} (ID: <code>{target_user_id}</code>)\n"
        f"👑 Отклонил куратор: @{clean_html(admin_uname)}\n\n"
        f"⏳ Повторная подача возможна только через 24 часа."
    )

    notifications = db.get_notifications("appeal", appeal_id)
    for notif_admin_id, msg_id in notifications:
        try:
            bot.edit_message_text(updated_text, notif_admin_id, msg_id, parse_mode="HTML", reply_markup=None)
        except Exception as e:
            logger.error(f"Ошибка редактирования карты апелляции: {e}")

    db.delete_notifications("appeal", appeal_id)

    try:
        bot.send_message(target_user_id, "❌ **Ваша апелляция была отклонена.**\n\nПовторную апелляцию вы сможете подать ровно через 24 часа.")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление {target_user_id}: {e}")

    bot.answer_callback_query(call.id, "Апелляция отклонена!", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("appeal_block_user_"))
def block_appeal_user_cb(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 3:
        bot.answer_callback_query(call.id, "❌ Заблокировать апелляции может только Куратор (Level 3+)!", show_alert=True)
        return

    parts = call.data.split("_")
    appeal_id = int(parts[3])
    target_user_id = int(parts[4])

    db.block_appeals_for_user(target_user_id, admin_id)
    admin_uname = call.from_user.username or call.from_user.first_name

    notifications = db.get_notifications("appeal", appeal_id)
    target_uname = db.get_username(target_user_id)

    updated_text = (
        f"🚫 <b>ДОСТУП К АПЕЛЛЯЦИЯМ ЗАБЛОКИРОВАН</b>\n\n"
        f"👤 Пользователь: @{clean_html(target_uname)} (ID: <code>{target_user_id}</code>)\n"
        f"👑 Заблокировал куратор: @{clean_html(admin_uname)}"
    )

    for notif_admin_id, msg_id in notifications:
        try:
            bot.edit_message_text(updated_text, notif_admin_id, msg_id, parse_mode="HTML", reply_markup=None)
        except Exception as e:
            logger.error(f"Ошибка редактирования карты апелляции: {e}")

    db.delete_notifications("appeal", appeal_id)
    bot.answer_callback_query(call.id, "Пользователю заблокирован доступ к апелляциям!", show_alert=True)

# ========== ТИКЕТЫ (С КАТЕГОРИЯМИ, СТАТУС-БАРОМ И ЛОКОМ) ==========
@bot.message_handler(func=lambda msg: msg.text == "📩 Написать тикет", chat_types=['private'])
def start_ticket(message):
    user_id = message.from_user.id
    if db.is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 Вы в чёрном списке!")
        return
    
    open_tickets = db.get_user_open_tickets(user_id)
    if not open_tickets:
        bot.set_state(user_id, TicketState.waiting_for_category, message.chat.id)
        bot.send_message(
            message.chat.id,
            "📩 **Выберите тему/категорию вашего тикета:**",
            reply_markup=get_ticket_categories_keyboard()
        )
    else:
        kb = types.InlineKeyboardMarkup()
        for tid, status, created_at, category, assigned_to in open_tickets:
            badge = get_ticket_status_badge(status, assigned_to)
            kb.add(types.InlineKeyboardButton(f"🎫 #{tid} [{category}] {badge}", callback_data=f"user_view_ticket_{tid}"))
        kb.add(types.InlineKeyboardButton("➕ Создать новый тикет", callback_data="user_create_new_ticket"))
        bot.send_message(message.chat.id, "📩 **У вас есть открытые тикеты:**", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "user_create_new_ticket")
def user_create_new_ticket_cb(call):
    user_id = call.from_user.id
    bot.set_state(user_id, TicketState.waiting_for_category, call.message.chat.id)
    bot.send_message(call.message.chat.id, "📩 **Выберите тему/категорию вашего тикета:**", reply_markup=get_ticket_categories_keyboard())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ticket_cat_"))
def select_ticket_category_cb(call):
    category = call.data.split("_")[2]
    user_id = call.from_user.id
    
    with bot.retrieve_data(user_id, call.message.chat.id) as data:
        data['ticket_category'] = category
        
    bot.set_state(user_id, TicketState.waiting_for_message, call.message.chat.id)
    bot.edit_message_text(
        f"🏷 <b>Категория:</b> {clean_html(category)}\n\n"
        f"✏️ **Напишите ваше сообщение для администрации:**\n\n❌ Отмена — /cancel",
        call.message.chat.id, call.message.message_id, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(state=TicketState.waiting_for_message, chat_types=['private'])
def process_ticket(message):
    user_id = message.from_user.id
    msg_text = message.text
    
    if msg_text == "/cancel":
        bot.delete_state(user_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return
    
    category = "Другое"
    with bot.retrieve_data(user_id, message.chat.id) as data:
        category = data.get('ticket_category', 'Другое')

    ticket_id = db.create_ticket(user_id, category)
    db.add_ticket_message(ticket_id, user_id, msg_text)
    bot.delete_state(user_id, message.chat.id)
    
    username = db.get_username(user_id)
    safe_username = clean_html(username)
    safe_text = clean_html(msg_text)
    
    badge = get_ticket_status_badge('open', None)
    
    admins = db.get_all_admins()
    for admin_id, level, _, _ in admins:
        if level >= 2:
            try:
                msg = bot.send_message(
                    admin_id,
                    f"🆕 <b>НОВЫЙ ТИКЕТ #{ticket_id}</b>\n"
                    f"🏷 <b>Тег:</b> [{clean_html(category)}] | {badge}\n\n"
                    f"👤 Пользователь: @{safe_username} (ID: <code>{user_id}</code>)\n"
                    f"📅 Время: {get_moscow_time().strftime('%d.%m.%Y %H:%M')} МСК\n\n"
                    f"📝 Сообщение:\n{safe_text[:1000]}",
                    parse_mode="HTML",
                    reply_markup=get_ticket_actions_keyboard(ticket_id, None, level)
                )
                db.add_notification("ticket", ticket_id, admin_id, msg.message_id)
            except Exception as e:
                logger.error(f"Не удалось отправить тикет админу {admin_id}: {e}")
    
    bot.send_message(message.chat.id, f"✅ **Тикет #{ticket_id} [{category}] создан!**\n\nАдмины ответят вам в ближайшее время.", reply_markup=get_main_keyboard(user_id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_view_ticket_"))
def user_view_ticket(call):
    tid = int(call.data.split("_")[3])
    ticket = db.get_ticket(tid)
    if ticket:
        messages = db.get_ticket_messages(tid)
        category = ticket[6] if len(ticket) > 6 else "Другое"
        assigned_to = ticket[9] if len(ticket) > 9 else None
        badge = get_ticket_status_badge(ticket[2], assigned_to)

        text = f"📋 <b>Тикет #{tid}</b> | 🏷 Тег: [{clean_html(category)}] | {badge}\n\n"
        for msg in messages:
            author_id, is_admin = msg[1], msg[3]
            author_uname = db.get_username(author_id)
            role = f"👑 Админ (@{clean_html(author_uname)})" if is_admin else f"👤 Вы"
            text += f"[{role}]: {clean_html(msg[2])}\n\n"
        
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("💬 Ответить", callback_data=f"user_ticket_reply_{tid}"),
            types.InlineKeyboardButton("❌ Закрыть тикет", callback_data=f"user_ticket_close_{tid}")
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_ticket_reply_"))
def user_ticket_reply_start(call):
    tid = int(call.data.split("_")[3])
    bot.set_state(call.from_user.id, UserTicketReplyState.waiting_for_message, call.message.chat.id)
    with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        data['ticket_id'] = tid
    bot.send_message(call.message.chat.id, "✏️ Введите ваш ответ для админов:\n\n❌ Отмена — /cancel")
    bot.answer_callback_query(call.id)

@bot.message_handler(state=UserTicketReplyState.waiting_for_message, chat_types=['private'])
def user_ticket_reply_process(message):
    user_id = message.from_user.id
    if message.text == "/cancel":
        bot.delete_state(user_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return

    with bot.retrieve_data(user_id, message.chat.id) as data:
        tid = data.get('ticket_id')

    ticket = db.get_ticket(tid)
    if ticket:
        category = ticket[6] if len(ticket) > 6 else "Другое"
        assigned_to = ticket[9] if len(ticket) > 9 else None
        badge = get_ticket_status_badge(ticket[2], assigned_to)

        db.add_ticket_message(tid, user_id, message.text, is_admin=False)
        bot.send_message(message.chat.id, f"✅ Ответ отправлен в тикет #{tid}!", reply_markup=get_main_keyboard(user_id))
        
        username = db.get_username(user_id)
        admins = db.get_all_admins()
        for admin_id, level, _, _ in admins:
            if level >= 2:
                try:
                    msg = bot.send_message(
                        admin_id,
                        f"💬 <b>НОВОЕ СООБЩЕНИЕ В ТИКЕТЕ #{tid}</b>\n"
                        f"🏷 <b>Тег:</b> [{clean_html(category)}] | {badge}\n\n"
                        f"👤 От: @{clean_html(username)} (ID: <code>{user_id}</code>)\n\n"
                        f"📝 {clean_html(message.text)}",
                        parse_mode="HTML",
                        reply_markup=get_ticket_actions_keyboard(tid, assigned_to, level)
                    )
                    db.add_notification("ticket", tid, admin_id, msg.message_id)
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    bot.delete_state(user_id, message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_ticket_close_"))
def user_ticket_close_cb(call):
    tid = int(call.data.split("_")[3])
    db.close_ticket(tid, call.from_user.id)
    bot.edit_message_text(f"✅ Вы закрыли тикет #{tid}.", call.message.chat.id, call.message.message_id)
    bot.send_message(
        call.message.chat.id,
        "⭐ <b>Пожалуйста, оцените качество работы поддержки:</b>",
        reply_markup=get_ticket_rating_keyboard(tid),
        parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

# ----- ОЦЕНКА РАБОТЫ АДМИНИСТРАЦИИ -----
@bot.callback_query_handler(func=lambda call: call.data.startswith("rate_ticket_"))
def process_ticket_rating_cb(call):
    parts = call.data.split("_")
    tid = int(parts[2])
    stars = int(parts[3])
    user_id = call.from_user.id

    db.set_ticket_rating(tid, stars)

    if stars <= 3:
        bot.set_state(user_id, TicketRatingState.waiting_for_feedback, call.message.chat.id)
        with bot.retrieve_data(user_id, call.message.chat.id) as data:
            data['rating_ticket_id'] = tid

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➡️ Пропустить", callback_data=f"skip_rating_feedback_{tid}"))

        bot.edit_message_text(
            f"⭐ Вы поставили оценку: <b>{stars} / 5</b>\n\n"
            f"✍️ Напишите короткий отзыв — что именно вам не понравилось или как мы можем улучшить сервис?",
            call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML"
        )
    else:
        bot.edit_message_text(
            f"⭐ Вы поставили оценку: <b>{stars} / 5</b>\n\n❤️ Спасибо за ваш отзыв! Нам приятно становиться лучше.",
            call.message.chat.id, call.message.message_id, parse_mode="HTML"
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("skip_rating_feedback_"))
def skip_rating_feedback_cb(call):
    user_id = call.from_user.id
    bot.delete_state(user_id, call.message.chat.id)
    bot.edit_message_text("❤️ Спасибо за вашу оценку!", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.message_handler(state=TicketRatingState.waiting_for_feedback, chat_types=['private'])
def process_rating_feedback_text(message):
    user_id = message.from_user.id
    with bot.retrieve_data(user_id, message.chat.id) as data:
        tid = data.get('rating_ticket_id')

    if tid:
        ticket = db.get_ticket(tid)
        rating = ticket[7] if ticket and len(ticket) > 7 else 0
        db.set_ticket_rating(tid, rating, message.text)

    bot.delete_state(user_id, message.chat.id)
    bot.send_message(message.chat.id, "✅ Ваш отзыв сохранён. Спасибо за помощь в развитии проекта!", reply_markup=get_main_keyboard(user_id))

# ========== ОБРАБОТКА ТИКЕТОВ АДМИНИСТРАЦИЕЙ ==========
@bot.message_handler(func=lambda msg: msg.text == "📋 Тикеты (открытые)", chat_types=['private'])
def admin_open_tickets(message):
    admin_id = message.from_user.id
    admin_level = db.get_admin_level(admin_id)
    if admin_level < 2:
        return
    tickets = db.get_all_open_tickets()
    if not tickets:
        bot.send_message(message.chat.id, "📭 Нет открытых тикетов")
        return
    for tid, uid, created_at, category, assigned_to in tickets:
        badge = get_ticket_status_badge('open', assigned_to)
        user_uname = db.get_username(uid)
        messages = db.get_ticket_messages(tid)
        last_msg = messages[-1][2] if messages else "Нет сообщений"
        date_formatted = datetime.fromisoformat(created_at).strftime('%d.%m.%Y %H:%M')
        
        bot.send_message(
            message.chat.id,
            f"🎫 <b>Тикет #{tid}</b> | 🏷 [{clean_html(category)}] | {badge}\n"
            f"👤 От: @{clean_html(user_uname)} (ID: <code>{uid}</code>)\n"
            f"📅 {date_formatted} МСК\n\n"
            f"📝 Последнее сообщение:\n{clean_html(last_msg)}",
            reply_markup=get_ticket_actions_keyboard(tid, assigned_to, admin_level),
            parse_mode="HTML"
        )

# Ответ админа в тикет (с автоматическим закреплением тикета)
@bot.callback_query_handler(func=lambda call: call.data.startswith("ticket_reply_"))
def admin_ticket_reply_start(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 2:
        bot.answer_callback_query(call.id, "❌ Доступно со уровня 2 (Ст.админ)", show_alert=True)
        return

    tid = int(call.data.split("_")[2])
    ticket = db.get_ticket(tid)
    if not ticket or ticket[2] == 'closed':
        bot.answer_callback_query(call.id, "❌ Тикет закрыт!", show_alert=True)
        return

    assigned_to = ticket[9]
    if assigned_to and assigned_to != admin_id:
        admin_owner = db.get_username(assigned_to)
        bot.answer_callback_query(call.id, f"❌ Тикет уже забронирован админом @{admin_owner}!", show_alert=True)
        return

    if not assigned_to:
        db.assign_ticket(tid, admin_id)

    bot.set_state(admin_id, AdminReplyState.waiting_for_reply, call.message.chat.id)
    with bot.retrieve_data(admin_id, call.message.chat.id) as data:
        data['ticket_id'] = tid

    bot.send_message(call.message.chat.id, f"✏️ Введите ваш ответ для пользователя по тикету #{tid}:\n\n❌ Отмена — /cancel")
    bot.answer_callback_query(call.id)

@bot.message_handler(state=AdminReplyState.waiting_for_reply, chat_types=['private'])
def process_admin_reply(message):
    admin_id = message.from_user.id
    if message.text == "/cancel":
        bot.delete_state(admin_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_admin_keyboard(db.get_admin_level(admin_id), admin_id))
        return

    with bot.retrieve_data(admin_id, message.chat.id) as data:
        tid = data.get('ticket_id')

    ticket = db.get_ticket(tid)
    if ticket:
        user_id = ticket[1]
        category = ticket[6]
        db.add_ticket_message(tid, admin_id, message.text, is_admin=True)

        admin_uname = message.from_user.username or message.from_user.first_name
        try:
            bot.send_message(
                user_id,
                f"💬 <b>ОТВЕТ АДМИНИСТРАТОРА ПО ТИКЕТУ #{tid}</b>\n\n"
                f"👑 От: @{clean_html(admin_uname)}\n\n"
                f"{clean_html(message.text)}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить ответ юзеру {user_id}: {e}")

        bot.send_message(message.chat.id, f"✅ Ответ отправлен пользователю по тикету #{tid}!")
    bot.delete_state(admin_id, message.chat.id)

# Кнопка «Попросить помощь» в тикетах
@bot.callback_query_handler(func=lambda call: call.data.startswith("ticket_help_"))
def ticket_help_cb(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 2:
        bot.answer_callback_query(call.id, "❌ Нет прав", show_alert=True)
        return

    tid = int(call.data.split("_")[2])
    db.unassign_ticket(tid)
    admin_uname = call.from_user.username or call.from_user.first_name

    admins = db.get_all_admins()
    for aid, lvl, _, _ in admins:
        if lvl >= 2:
            try:
                bot.send_message(
                    aid,
                    f"🆘 <b>ВНИМАНИЕ! ПОМОЩЬ В ТИКЕТЕ #{tid}</b>\n\n"
                    f"👑 Админ @{clean_html(admin_uname)} попросил помощь по тикету #{tid}!\n"
                    f"🔓 Лок снят. Любой свободный администратор может подключиться к решению.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    bot.answer_callback_query(call.id, "Запрос о помощи отправлен всем администраторам!", show_alert=True)

# Кнопка владельца «Снять лок»
@bot.callback_query_handler(func=lambda call: call.data.startswith("ticket_unlock_"))
def ticket_unlock_cb(call):
    owner_id = call.from_user.id
    if db.get_admin_level(owner_id) < 4:
        bot.answer_callback_query(call.id, "❌ Снимать лок может только Владелец (Level 4)!", show_alert=True)
        return

    tid = int(call.data.split("_")[2])
    db.unassign_ticket(tid)
    owner_uname = call.from_user.username or call.from_user.first_name

    admins = db.get_all_admins()
    for aid, lvl, _, _ in admins:
        if lvl >= 2:
            try:
                bot.send_message(
                    aid,
                    f"🔓 <b>СБРОС ЛОКА В ТИКЕТЕ #{tid}</b>\n\n"
                    f"👑 Владелец @{clean_html(owner_uname)} снял лок с тикета #{tid}.\n"
                    f"Тикет снова свободен для работы!",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    bot.answer_callback_query(call.id, "Лок успешно снят, администрация уведомлена!", show_alert=True)

# Закрытие тикета администратором
@bot.callback_query_handler(func=lambda call: call.data.startswith("ticket_close_"))
def admin_ticket_close_cb(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 2:
        bot.answer_callback_query(call.id, "❌ Нет прав", show_alert=True)
        return

    tid = int(call.data.split("_")[2])
    ticket = db.get_ticket(tid)
    if ticket:
        db.close_ticket(tid, admin_id)
        user_id = ticket[1]
        try:
            bot.send_message(user_id, f"🔒 Ваш тикет #{tid} был закрыт администратором.")
            bot.send_message(
                user_id,
                "⭐ <b>Пожалуйста, оцените качество работы поддержки:</b>",
                reply_markup=get_ticket_rating_keyboard(tid),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Ошибка при закрытии тикета юзеру: {e}")

    bot.answer_callback_query(call.id, "Тикет закрыт!", show_alert=True)

# ========== КОД (АКТИВАЦИЯ) ==========
@bot.message_handler(func=lambda msg: msg.text == "🎫 Код", chat_types=['private'])
def start_code(message):
    user_id = message.from_user.id
    bot.set_state(user_id, CodeState.waiting_for_code, message.chat.id)
    bot.send_message(message.chat.id, "🎫 **Введи код для активации админки:**\n\n❌ Отмена — /cancel")

@bot.message_handler(state=CodeState.waiting_for_code, chat_types=['private'])
def process_code(message):
    user_id = message.from_user.id
    code = message.text.strip().upper()
    if code == "/cancel":
        bot.delete_state(user_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return
    
    status, res = db.use_admin_code(code, user_id)
    bot.delete_state(user_id, message.chat.id)

    if status in ["error", "banned", "success"]:
        bot.send_message(message.chat.id, res, reply_markup=get_main_keyboard(user_id))

    elif status == "repeat":
        req_id = db.create_promo_request(user_id, code)
        bot.send_message(message.chat.id, "⏳ Вы уже активировали этот код ранее. Запрос на повторный ввод отправлен администрации (Level 4)!", reply_markup=get_main_keyboard(user_id))
        
        uname = db.get_username(user_id)
        admins = db.get_all_admins()
        
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✅ Принять", callback_data=f"promo_req_accept_{req_id}"),
            types.InlineKeyboardButton("❌ Отказать", callback_data=f"promo_req_reject_{req_id}")
        )
        kb.row(types.InlineKeyboardButton("⛔️ Отказать с запретом", callback_data=f"promo_req_ban_{req_id}"))

        for admin_id, level, _, _ in admins:
            if level >= 4:
                try:
                    msg = bot.send_message(
                        admin_id,
                        f"⚠️ <b>ПОВТОРНЫЙ ВВОД КОДА</b>\n\n"
                        f"👤 Пользователь: @{clean_html(uname)} (ID: <code>{user_id}</code>)\n"
                        f"🎫 Код: <code>{clean_html(code)}</code>",
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                    db.add_notification("promo_req", req_id, admin_id, msg.message_id)
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление Lvl 4 админу {admin_id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("promo_req_"))
def handle_promo_request_action(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 4:
        bot.answer_callback_query(call.id, "❌ Только для уровня 4", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[2]
    req_id = int(parts[3])

    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, code, status FROM promo_requests WHERE id = ?", (req_id,))
        req = c.fetchone()
        if not req or req[2] != 'pending':
            bot.answer_callback_query(call.id, "❌ Запрос уже обработан!", show_alert=True)
            return

        user_id, code, _ = req
        c.execute("UPDATE promo_requests SET status = ?, processed_by = ? WHERE id = ?", (action, admin_id, req_id))
        conn.commit()

    admin_uname = call.from_user.username or call.from_user.first_name
    user_uname = db.get_username(user_id)

    safe_admin = clean_html(admin_uname)
    safe_user = clean_html(user_uname)
    safe_code = clean_html(code)

    if action == "accept":
        verdict_str = "принято"
        status, res = db.use_admin_code(code, user_id, allow_repeat=True)
        user_msg = (
            f"✅ <b>Запрос на повторный ввод кода одобрен!</b>\n\n"
            f"🎫 <b>Код:</b> <code>{safe_code}</code>\n"
            f"📊 <b>Решение:</b> Одобрено\n"
            f"👑 <b>Вердикт вынес:</b> @{safe_admin}\n\n"
            f"{res}"
        )

    elif action == "reject":
        verdict_str = "отказано"
        user_msg = (
            f"❌ <b>Запрос на повторный ввод кода отклонен</b>\n\n"
            f"🎫 <b>Код:</b> <code>{safe_code}</code>\n"
            f"📊 <b>Решение:</b> Отказано\n"
            f"👑 <b>Вердикт вынес:</b> @{safe_admin}"
        )

    elif action == "ban":
        verdict_str = "отказано с запретом"
        db.ban_user_promo(user_id, code)
        user_msg = (
            f"⛔️ <b>Запрос на повторный ввод кода отклонен!</b>\n\n"
            f"🎫 <b>Код:</b> <code>{safe_code}</code>\n"
            f"📊 <b>Решение:</b> Отказано с запретом\n"
            f"👑 <b>Вердикт вынес:</b> @{safe_admin}\n\n"
            f"❌ Вам запрещено использовать промокод <code>{safe_code}</code>!"
        )

    try:
        bot.send_message(user_id, user_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    status_text = (
        f"⚠️ <b>РЕЗУЛЬТАТ РАССМОТРЕНИЯ ПОВТОРНОГО ВВОДА</b>\n\n"
        f"👑 <b>Кто рассмотрел:</b> @{safe_admin}\n"
        f"👤 <b>Кто подал:</b> @{safe_user} (ID: <code>{user_id}</code>)\n"
        f"🎫 <b>Код:</b> <code>{safe_code}</code>\n"
        f"📊 <b>Вывод:</b> {verdict_str}"
    )

    notifications = db.get_notifications("promo_req", req_id)
    for notif_admin_id, msg_id in notifications:
        try:
            bot.edit_message_text(status_text, notif_admin_id, msg_id, parse_mode="HTML", reply_markup=None)
        except Exception as e:
            logger.error(f"Ошибка обновления карточки промо: {e}")

    db.delete_notifications("promo_req", req_id)
    bot.answer_callback_query(call.id, "Решение принято!")

# ========== КАЛЕНДАРЬ (ОБРАБОТЧИКИ) ==========
@bot.message_handler(func=lambda msg: msg.text == "📅 Календарь анкет", chat_types=['private'])
def show_calendar(message):
    now = get_moscow_time()
    kb = build_calendar_keyboard(now.year, now.month)
    bot.send_message(
        message.chat.id,
        "📅 **Календарь анкет (Время МСК)**\n\n"
        "⬛ — нет анкет  |  🟥 — 0%\n"
        "🟧 — 1-20%  |  🟨 — 21-40%\n"
        "🟩 — 41-60%  |  🟦 — 61-80%\n"
        "🟪 — 81-99%  |  ⬜ — 100%\n"
        "💡 Нажми на день для статистики",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("cal_"))
def calendar_callback(call):
    data = call.data
    if data == "cal_ignore":
        bot.answer_callback_query(call.id)
        return
    
    if data == "cal_today":
        now = get_moscow_time()
        kb = build_calendar_keyboard(now.year, now.month)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    
    if data.startswith("cal_select_ym_"):
        _, _, _, year_str, _ = data.split("_")
        kb = build_month_year_selector(int(year_str))
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    
    if data.startswith("cal_year_change_"):
        year = int(data.split("_")[3])
        kb = build_month_year_selector(year)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("cal_set_month_"):
        _, _, _, year_str, month_str = data.split("_")
        kb = build_calendar_keyboard(int(year_str), int(month_str))
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("cal_prev_"):
        _, _, year_str, month_str = data.split("_")
        year, month = int(year_str), int(month_str)
        if month == 1: year, month = year - 1, 12
        else: month -= 1
        kb = build_calendar_keyboard(year, month)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("cal_next_"):
        _, _, year_str, month_str = data.split("_")
        year, month = int(year_str), int(month_str)
        if month == 12: year, month = year + 1, 1
        else: month += 1
        kb = build_calendar_keyboard(year, month)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("cal_day_"):
        date_str = data.split("_")[2]
        stats = db.get_applications_stats_by_date(date_str)
        user_apps = db.get_user_applications_by_date(call.from_user.id, date_str)
        
        total, pending, approved, rejected = stats['total'], stats['pending'], stats['approved'], stats['rejected']
        processed = stats['processed']
        percent = round((processed / total * 100)) if total > 0 else 0
        
        user_text = "нет" if not user_apps else f"{len(user_apps)} анкет"
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"cal_back_to_{date_obj.year}_{date_obj.month}"))
        
        bot.edit_message_text(
            f"📅 **{date_obj.strftime('%d.%m.%Y')}**\n\n"
            f"📊 Всего анкет: {total}\n"
            f"⏳ На рассмотрении: {pending}\n"
            f"📢 Одобрено: {approved}\n"
            f"❌ Отклонено: {rejected}\n"
            f"📈 Обработано: {processed} ({percent}%)\n\n"
            f"👤 Ваши анкеты за этот день: {user_text}",
            call.message.chat.id, call.message.message_id, reply_markup=kb
        )
        bot.answer_callback_query(call.id)
        return

    if data.startswith("cal_back_to_"):
        _, _, _, year_str, month_str = data.split("_")
        kb = build_calendar_keyboard(int(year_str), int(month_str))
        bot.edit_message_text(
            "📅 **Календарь анкет (Время МСК)**\n\n"
            "⬛ — нет анкет  |  🟥 — 0%\n"
            "🟧 — 1-20%  |  🟨 — 21-40%\n"
            "🟩 — 41-60%  |  🟦 — 61-80%\n"
            "🟪 — 81-99%  |  ⬜ — 100%\n"
            "💡 Нажми на день для статистики",
            call.message.chat.id, call.message.message_id, reply_markup=kb
        )
        bot.answer_callback_query(call.id)
        return

# ========== АДМИН ПАНЕЛЬ ==========
@bot.message_handler(func=lambda msg: msg.text == "👑 Админ панель", chat_types=['private'])
def admin_panel(message):
    level = db.get_admin_level(message.from_user.id)
    if level < 1:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к админ-панели.")
        return
    bot.send_message(message.chat.id, "👑 **Админ панель**", reply_markup=get_admin_keyboard(level, message.from_user.id))

@bot.message_handler(func=lambda msg: msg.text == "🔙 Выйти в меню", chat_types=['private'])
def exit_admin(message):
    bot.send_message(message.chat.id, "👋 Возврат в главное меню", reply_markup=get_main_keyboard(message.from_user.id))

# ========== УПРАВЛЕНИЕ АДМИНАМИ (LEVEL 4) ==========
@bot.message_handler(func=lambda msg: msg.text == "👑 Управление админами", chat_types=['private'])
def admin_manage(message):
    if db.get_admin_level(message.from_user.id) < 4:
        bot.send_message(message.chat.id, "❌ Нет доступа (нужен уровень 4 — Владелец)")
        return
    bot.send_message(message.chat.id, "👑 **Управление админами**", reply_markup=get_admin_manage_keyboard())

@bot.callback_query_handler(func=lambda call: call.data in ["add_admin", "remove_admin", "list_admins"])
def admin_manage_callbacks(call):
    if db.get_admin_level(call.from_user.id) < 4:
        bot.answer_callback_query(call.id, "❌ Нет доступа (нужен уровень 4)", show_alert=True)
        return
    
    if call.data == "add_admin":
        bot.set_state(call.from_user.id, AddAdminState.waiting_for_username, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите @username или Telegram ID пользователя:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

    elif call.data == "remove_admin":
        bot.set_state(call.from_user.id, RemoveAdminState.waiting_for_username, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите @username или Telegram ID админа для удаления:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

    elif call.data == "list_admins":
        admins = db.get_all_admins()
        if not admins:
            bot.send_message(call.message.chat.id, "📭 Список админов пуст.")
            bot.answer_callback_query(call.id)
            return
        
        text = "👑 <b>Список администраторов:</b>\n\n"
        for admin_id, lvl, added_at, expires_at in admins:
            lvl_name = LEVEL_NAMES.get(lvl, "Неизвестно")
            username = db.get_username(admin_id)
            
            if expires_at:
                expire_date = datetime.fromisoformat(expires_at)
                if expire_date < get_moscow_time(): time_text = "❌ Истёк"
                else:
                    delta = expire_date - get_moscow_time()
                    time_text = f"осталось {delta.days}д {delta.seconds // 3600}ч"
            else: time_text = "бессрочно"
            
            text += f"🆔 <code>{admin_id}</code> | @{clean_html(username)}\n📊 Уровень: {lvl_name} ({time_text})\n\n"
        
        bot.send_message(call.message.chat.id, text, parse_mode="HTML")
        bot.answer_callback_query(call.id)

@bot.message_handler(state=AddAdminState.waiting_for_username, chat_types=['private'])
def add_admin_username(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    target_id = db.find_user_id(message.text)
    if not target_id:
        bot.send_message(message.chat.id, "❌ Пользователь не найден в БД. Попросите его сначала запустить бота /start!")
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['target_id'] = target_id
        
    bot.send_message(
        message.chat.id,
        "✏️ **Выберите уровень админа:**",
        reply_markup=get_level_inline_keyboard("add_admin_set")
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_admin_set_lvl_"))
def add_admin_level_cb(call):
    level = int(call.data.split("_")[4])
    user_id = call.from_user.id
    with bot.retrieve_data(user_id, call.message.chat.id) as data:
        data['admin_level'] = level

    bot.set_state(user_id, AddAdminState.waiting_for_time, call.message.chat.id)
    bot.edit_message_text(
        f"Выбран уровень: **{LEVEL_NAMES[level]}**\n\n"
        "✏️ Введите срок действия (например: `5d`, `12h`, `1y`) или `0` для бессрочного решения:",
        call.message.chat.id, call.message.message_id, parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(state=AddAdminState.waiting_for_time, chat_types=['private'])
def add_admin_time(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    user_id = message.from_user.id
    time_str = message.text.strip()
    delta = None if time_str == "0" else parse_time_string(time_str)
    
    with bot.retrieve_data(user_id, message.chat.id) as data:
        target_id = data.get('target_id')
        level = data.get('admin_level')
    
    db.add_admin(target_id, level, user_id, delta)
    bot.delete_state(user_id, message.chat.id)
    bot.send_message(message.chat.id, f"✅ Пользователь `{target_id}` назначен: **{LEVEL_NAMES[level]}**", parse_mode="Markdown")

@bot.message_handler(state=RemoveAdminState.waiting_for_username, chat_types=['private'])
def remove_admin_username(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    target_id = db.find_user_id(message.text)
    if not target_id:
        bot.send_message(message.chat.id, "❌ Пользователь не найден.")
        bot.delete_state(message.from_user.id, message.chat.id)
        return
    
    if target_id in MASTER_ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ Нельзя удалить главного владельца!")
        bot.delete_state(message.from_user.id, message.chat.id)
        return
    
    db.remove_admin(target_id)
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, f"✅ Админ `{target_id}` был удалён.", parse_mode="Markdown")

# ========== ЧЁРНЫЙ СПИСОК (LEVEL 3+) ==========
@bot.message_handler(func=lambda msg: msg.text == "🚫 Чёрный список", chat_types=['private'])
def admin_blacklist(message):
    if db.get_admin_level(message.from_user.id) < 3:
        bot.send_message(message.chat.id, "❌ Нет доступа (нужен уровень 3+ Куратор)")
        return
    
    blacklist = db.get_blacklist()
    if not blacklist:
        bot.send_message(message.chat.id, "📭 Чёрный список пуст", reply_markup=get_blacklist_keyboard())
        return
    
    text = "🚫 **ЧЁРНЫЙ СПИСОК:**\n\n"
    for uid, reason, banned_at in blacklist:
        text += f"🆔 `{uid}` | @{clean_html(db.get_username(uid))}\n📝 {clean_html(reason)}\n\n"
    bot.send_message(message.chat.id, text, reply_markup=get_blacklist_keyboard(), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data in ["blacklist_add", "blacklist_remove"])
def blacklist_callbacks(call):
    if db.get_admin_level(call.from_user.id) < 3:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return

    if call.data == "blacklist_add":
        bot.set_state(call.from_user.id, BlacklistAddState.waiting_for_username, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите @username или Telegram ID для бана:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

    elif call.data == "blacklist_remove":
        bot.set_state(call.from_user.id, BlacklistRemoveState.waiting_for_username, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите @username или Telegram ID для разбана:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

@bot.message_handler(state=BlacklistAddState.waiting_for_username, chat_types=['private'])
def blacklist_add_username(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    target_id = db.find_user_id(message.text)
    if not target_id:
        bot.send_message(message.chat.id, "❌ Пользователь не найден в БД. Попросите его написать боту!")
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['target_id'] = target_id
        
    bot.set_state(message.from_user.id, BlacklistAddState.waiting_for_reason, message.chat.id)
    bot.send_message(message.chat.id, "✏️ Укажите причину блокировки:")

@bot.message_handler(state=BlacklistAddState.waiting_for_reason, chat_types=['private'])
def blacklist_add_reason(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        target_id = data.get('target_id')
        
    db.add_to_blacklist(target_id, message.text)
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, f"✅ Пользователь `{target_id}` заблокирован.", parse_mode="Markdown")

@bot.message_handler(state=BlacklistRemoveState.waiting_for_username, chat_types=['private'])
def blacklist_remove_username(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    target_id = db.find_user_id(message.text)
    if not target_id:
        bot.send_message(message.chat.id, "❌ Пользователь не найден.")
        bot.delete_state(message.from_user.id, message.chat.id)
        return
    
    db.remove_from_blacklist(target_id)
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, f"✅ Пользователь `{target_id}` разблокирован.", parse_mode="Markdown")

# ========== КОДЫ ДЛЯ АДМИНКИ (LEVEL 4) ==========
@bot.message_handler(func=lambda msg: msg.text == "🎫 Коды для админки", chat_types=['private'])
def admin_codes(message):
    if db.get_admin_level(message.from_user.id) < 4:
        bot.send_message(message.chat.id, "❌ Нет доступа (нужен уровень 4)")
        return
    bot.send_message(message.chat.id, "🎫 **Управление кодами для админки**", reply_markup=get_codes_manage_keyboard())

@bot.callback_query_handler(func=lambda call: call.data in ["create_code", "list_codes", "delete_code"])
def codes_callbacks(call):
    if db.get_admin_level(call.from_user.id) < 4:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return

    if call.data == "create_code":
        bot.set_state(call.from_user.id, CreateCodeState.waiting_for_code_name, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ **Введите название кода** (буквы, цифры):\nПример: `PROMO2026`\n\n❌ Отмена — /cancel", parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    elif call.data == "list_codes":
        codes = db.get_all_codes()
        if not codes:
            bot.send_message(call.message.chat.id, "📭 Созданных кодов нет.")
            bot.answer_callback_query(call.id)
            return
        
        text = "🎫 **Список кодов:**\n\n"
        for code, lvl, duration, max_uses, used, _ in codes:
            lvl_name = LEVEL_NAMES.get(lvl, "Неизвестно")
            time_text = duration if duration else "бессрочно"
            text += f"🔹 `{code}` | {lvl_name} | {used}/{max_uses} исп. ({time_text})\n"
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    elif call.data == "delete_code":
        bot.set_state(call.from_user.id, DeleteCodeState.waiting_for_code, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите код для удаления:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

@bot.message_handler(state=CreateCodeState.waiting_for_code_name, chat_types=['private'])
def create_code_name(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    code = message.text.strip().upper()
    if not re.match(r'^[A-Z0-9_-]{3,20}$', code):
        bot.send_message(message.chat.id, "❌ Некорректное название кода! Попробуйте еще раз.")
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['code'] = code

    bot.send_message(
        message.chat.id,
        "✏️ **Выберите уровень привилегий для кода:**",
        reply_markup=get_level_inline_keyboard("create_code_set")
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("create_code_set_lvl_"))
def create_code_level_cb(call):
    level = int(call.data.split("_")[4])
    user_id = call.from_user.id
    with bot.retrieve_data(user_id, call.message.chat.id) as data:
        data['code_level'] = level

    bot.set_state(user_id, CreateCodeState.waiting_for_time, call.message.chat.id)
    bot.edit_message_text(
        f"Выбран уровень: **{LEVEL_NAMES[level]}**\n\n"
        "✏️ Введите срок действия админки (например: `5d`, `12h`) или `0` для бессрочной:",
        call.message.chat.id, call.message.message_id, parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(state=CreateCodeState.waiting_for_time, chat_types=['private'])
def create_code_time(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    time_str = message.text.strip()
    duration = "" if time_str == "0" else time_str
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['duration'] = duration

    bot.set_state(message.from_user.id, CreateCodeState.waiting_for_max_uses, message.chat.id)
    bot.send_message(message.chat.id, "✏️ Введите максимальное число активаций этого кода (например `1` или `10`):")

@bot.message_handler(state=CreateCodeState.waiting_for_max_uses, chat_types=['private'])
def create_code_max_uses(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    try:
        max_uses = int(message.text.strip())
        if max_uses <= 0: raise ValueError
    except:
        bot.send_message(message.chat.id, "❌ Введите целое положительное число:")
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        code = data['code']
        code_level = data['code_level']
        duration = data['duration']

    db.create_admin_code(code, code_level, duration, max_uses, message.from_user.id)
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, f"✅ Код `{code}` успешно создан!", parse_mode="Markdown")

@bot.message_handler(state=DeleteCodeState.waiting_for_code, chat_types=['private'])
def delete_code_process(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    db.delete_code(message.text.strip().upper())
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, "✅ Код был успешно удалён!")

# ========== ОБРАБОТКА АНКЕТ ==========
@bot.message_handler(func=lambda msg: msg.text == "📝 Анкеты (новые)", chat_types=['private'])
def admin_applications(message):
    if db.get_admin_level(message.from_user.id) < 1: return
    apps = db.get_pending_applications()
    if not apps:
        bot.send_message(message.chat.id, "📭 Нет новых анкет")
        return
    for app_id, uid, uname, text, created_at in apps:
        bot.send_message(
            message.chat.id,
            f"📝 <b>Анкета #{app_id}</b>\n👤 @{clean_html(uname)} (ID: <code>{uid}</code>)\n\n{clean_html(text)}",
            reply_markup=get_application_actions_keyboard(app_id),
            parse_mode="HTML"
        )

# Одобрение анкеты
@bot.callback_query_handler(func=lambda call: call.data.startswith("app_approve_"))
def approve_app(call):
    try:
        admin_id = call.from_user.id
        if db.get_admin_level(admin_id) < 1:
            bot.answer_callback_query(call.id, "❌ Нет прав", show_alert=True)
            return

        app_id = int(call.data.split("_")[2])
        app = db.get_application(app_id)
        if not app:
            bot.answer_callback_query(call.id, "❌ Анкета не найдена!", show_alert=True)
            return

        db.approve_application(app_id, admin_id)
        admin_uname = call.from_user.username or call.from_user.first_name
        
        try:
            invite = bot.create_chat_invite_link(
                chat_id=CHAT_ID,
                member_limit=1,
                expire_date=datetime.now() + timedelta(days=1)
            )
            invite_link = invite.invite_link
        except Exception as e:
            logger.error(f"Ошибка при создании ссылки: {e}")
            invite_link = CHAT_INVITE_LINK

        notifications = db.get_notifications("application", app_id)
        updated_text = (
            f"✅ <b>АНКЕТА #{app_id} ОДОБРЕНА</b>\n"
            f"👤 Пользователь: @{clean_html(app[2])} (ID: <code>{app[1]}</code>)\n"
            f"👑 Одобрил админ: @{clean_html(admin_uname)}\n\n"
            f"📋 <b>Текст анкеты:</b>\n{clean_html(app[3])}"
        )
        
        for notif_admin_id, msg_id in notifications:
            try:
                bot.edit_message_text(updated_text, notif_admin_id, msg_id, parse_mode="HTML", reply_markup=None)
            except Exception as e:
                logger.error(f"Не удалось обновить карточку: {e}")
        
        db.delete_notifications("application", app_id)
        
        user_msg = (
            "🎉 <b>Ваша анкета одобрена!</b>\n\n"
            f"🔗 <b>Ссылка на вступление:</b> {invite_link}\n\n"
            "📌 <i>Обратите внимание: эта ссылка одноразовая!</i>"
        )
        try:
            bot.send_message(app[1], user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить юзеру: {e}")

        bot.answer_callback_query(call.id, "✅ Анкета успешно одобрена!", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при одобрении анкеты: {e}")
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}", show_alert=True)

# Отклонение анкеты
@bot.callback_query_handler(func=lambda call: call.data.startswith("app_reject_"))
def reject_app_start(call):
    admin_id = call.from_user.id
    if db.get_admin_level(admin_id) < 1:
        bot.answer_callback_query(call.id, "❌ Нет прав", show_alert=True)
        return

    app_id = int(call.data.split("_")[2])
    bot.set_state(admin_id, RejectReasonState.waiting_for_reason, call.message.chat.id)
    with bot.retrieve_data(admin_id, call.message.chat.id) as data:
        data['app_id'] = app_id

    bot.send_message(call.message.chat.id, f"✏️ Введите причину отказа для анкеты #{app_id}:\n\n❌ Отмена — /cancel")
    bot.answer_callback_query(call.id)

@bot.message_handler(state=RejectReasonState.waiting_for_reason, chat_types=['private'])
def reject_app_process(message):
    admin_id = message.from_user.id
    if message.text == "/cancel":
        bot.delete_state(admin_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_admin_keyboard(db.get_admin_level(admin_id), admin_id))
        return

    with bot.retrieve_data(admin_id, message.chat.id) as data:
        app_id = data.get('app_id')

    app = db.get_application(app_id)
    if app:
        reason = message.text
        db.reject_application(app_id, admin_id, reason)
        admin_uname = message.from_user.username or message.from_user.first_name

        notifications = db.get_notifications("application", app_id)
        updated_text = (
            f"❌ <b>АНКЕТА #{app_id} ОТКЛОНЕНА</b>\n"
            f"👤 Пользователь: @{clean_html(app[2])} (ID: <code>{app[1]}</code>)\n"
            f"👑 Отклонил админ: @{clean_html(admin_uname)}\n"
            f"📝 Причина: {clean_html(reason)}\n\n"
            f"📋 <b>Текст анкеты:</b>\n{clean_html(app[3])}"
        )

        for notif_admin_id, msg_id in notifications:
            try:
                bot.edit_message_text(updated_text, notif_admin_id, msg_id, parse_mode="HTML", reply_markup=None)
            except Exception as e:
                logger.error(f"Не удалось обновить карточку: {e}")

        db.delete_notifications("application", app_id)

        try:
            bot.send_message(app[1], f"❌ **Ваша анкета была отклонена.**\n\nПричина: {reason}")
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение юзеру: {e}")

        bot.send_message(message.chat.id, f"✅ Анкета #{app_id} отклонена.")
    bot.delete_state(admin_id, message.chat.id)

# ========== АРХИВ (LEVEL 3+) ==========
@bot.message_handler(func=lambda msg: msg.text == "🗄 Архив", chat_types=['private'])
def admin_archive_menu(message):
    if db.get_admin_level(message.from_user.id) < 3:
        bot.send_message(message.chat.id, "❌ Нет доступа (нужен уровень 3+ Куратор)")
        return
    
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📝 Архив анкет", callback_data="arch_apps_0"),
        types.InlineKeyboardButton("🎫 Архив тикетов", callback_data="arch_tickets_0")
    )
    bot.send_message(message.chat.id, "🗄 **Выберите раздел архива:**", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("arch_apps_"))
def show_archived_apps(call):
    page = int(call.data.split("_")[2])
    limit = 5
    offset = page * limit
    apps, total = db.get_archived_applications(limit, offset)

    if not apps:
        bot.answer_callback_query(call.id, "Архив анкет пуст!", show_alert=True)
        return

    text = f"🗄 <b>АРХИВ АНКЕТ (Стр. {page + 1}):</b>\n\n"
    for aid, uid, uname, status, created_at, reviewed_by in apps:
        st_icon = "✅" if status == 'approved' else "❌" if status == 'rejected' else "⏳"
        rev_uname = db.get_username(reviewed_by) if reviewed_by else "—"
        text += f"{st_icon} <b>Анкета #{aid}</b> | От: @{clean_html(uname)}\nСтатус: {status} | Проверил: @{clean_html(rev_uname)}\n\n"

    kb = types.InlineKeyboardMarkup()
    nav_btns = []
    if page > 0:
        nav_btns.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"arch_apps_{page - 1}"))
    if (page + 1) * limit < total:
        nav_btns.append(types.InlineKeyboardButton("Вперёд ▶️", callback_data=f"arch_apps_{page + 1}"))
    if nav_btns:
        kb.row(*nav_btns)

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("arch_tickets_"))
def show_archived_tickets(call):
    page = int(call.data.split("_")[2])
    limit = 5
    offset = page * limit
    tickets, total = db.get_archived_tickets(limit, offset)

    if not tickets:
        bot.answer_callback_query(call.id, "Архив тикетов пуст!", show_alert=True)
        return

    text = f"🗄 <b>АРХИВ ТИКЕТОВ (Стр. {page + 1}):</b>\n\n"
    for tid, uid, status, created_at, closed_by, category in tickets:
        st_icon = "🔴" if status == 'closed' else "🟢"
        closed_uname = db.get_username(closed_by) if closed_by else "—"
        text += f"{st_icon} <b>Тикет #{tid}</b> [{clean_html(category)}]\nОт: @{clean_html(db.get_username(uid))} | Закрыл: @{clean_html(closed_uname)}\n\n"

    kb = types.InlineKeyboardMarkup()
    nav_btns = []
    if page > 0:
        nav_btns.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"arch_tickets_{page - 1}"))
    if (page + 1) * limit < total:
        nav_btns.append(types.InlineKeyboardButton("Вперёд ▶️", callback_data=f"arch_tickets_{page + 1}"))
    if nav_btns:
        kb.row(*nav_btns)

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

# ========== ЗАПУСК БОТА ==========
if __name__ == "__main__":
    logger.info("Бот успешно запущен...")
    bot.infinity_polling(skip_pending=True)