import sqlite3
import logging
import re
import html
from datetime import datetime, timedelta, timezone
from calendar import monthrange
import telebot
from telebot import types, custom_filters
from telebot.handler_backends import State, StatesGroup
from telebot.storage import StateMemoryStorage

# ========== КОНФИГ ==========
BOT_TOKEN = "8657823190:AAHBJt43cMQd5wCZE5IyPvRmtp7xN0NUs84"
MASTER_ADMIN_IDS = [8484944484]  # твой ID (владелец)
CHAT_INVITE_LINK = "https://t.me/+rqod3GyElkwxYzYy"
TARGET_GROUP_ID = -1003975292023  # ID целевой группы для одноразовых ссылок
DB_PATH = "cult_flood.db"

# МСК Часовой пояс (UTC+3)
MSK_TIMEZONE = timezone(timedelta(hours=3))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== ЭКРАНИРОВАНИЕ HTML ==========
def clean_html(text: str) -> str:
    """Безопасное экранирование текста для HTML-разметки Telegram"""
    if not text:
        return ""
    return html.escape(str(text))

# ========== ВРЕМЯ ПО МСК ==========
def get_moscow_time() -> datetime:
    return datetime.now(MSK_TIMEZONE)

# ========== ПАРСЕР ВРЕМЕНИ ДЛЯ АДМИНОВ ==========
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
            total_seconds += value * 2592000  # 30 дней
        elif unit == 'y':
            total_seconds += value * 31536000  # 365 дней
    return timedelta(seconds=total_seconds)

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self):
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(DB_PATH) as conn:
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
                review_reason TEXT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                status TEXT DEFAULT 'open',
                created_at TEXT,
                updated_at TEXT
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
            c.execute('''CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_at TEXT
            )''')
            
            migrations = [
                ("applications", "reviewed_at", "TEXT"),
                ("applications", "review_reason", "TEXT"),
                ("admins", "expires_at", "TEXT")
            ]
            for table, column, col_type in migrations:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                except sqlite3.OperationalError:
                    pass

            for admin_id in MASTER_ADMIN_IDS:
                c.execute("INSERT OR IGNORE INTO admins (user_id, level, added_by, added_at) VALUES (?, 3, ?, ?)",
                         (admin_id, admin_id, get_moscow_time().isoformat()))
            conn.commit()
    
    def add_user(self, user_id, username):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
                     (user_id, username, get_moscow_time().isoformat()))
            conn.commit()
    
    def get_username(self, user_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            return row[0] if row and row[0] else str(user_id)

    def find_user_id(self, query: str):
        query = query.strip().lstrip('@')
        if query.isdigit():
            return int(query)
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (query,))
            row = c.fetchone()
            return row[0] if row else None
    
    def get_admin_level(self, user_id):
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO admins (user_id, level, added_by, added_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                     (user_id, level, added_by, get_moscow_time().isoformat(), expires_at))
            conn.commit()
    
    def remove_admin(self, user_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            conn.commit()
    
    def get_all_admins(self):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, level, added_at, expires_at FROM admins ORDER BY level DESC")
            return c.fetchall()
    
    def create_admin_code(self, code, level, duration, max_uses, created_by):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO admin_codes (code, level, duration, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (code, level, duration, max_uses, created_by, get_moscow_time().isoformat()))
            conn.commit()
    
    def use_admin_code(self, code, user_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT level, duration, max_uses, used FROM admin_codes WHERE code = ?", (code,))
            row = c.fetchone()
            if not row:
                return False, "❌ Код не найден"
            
            code_level, duration_str, max_uses, used = row
            if used >= max_uses:
                return False, "❌ Код уже использован максимальное число раз"
            
            current_level = self.get_admin_level(user_id)
            
            if current_level == 0 or code_level > current_level:
                delta = parse_time_string(duration_str) if duration_str else None
                self.add_admin(user_id, code_level, 0, delta)
                c.execute("UPDATE admin_codes SET used = used + 1 WHERE code = ?", (code,))
                conn.commit()
                level_name = {1: "Админ", 2: "Ст.админ", 3: "Владелец"}[code_level]
                return True, f"✅ Поздравляем! Вы получили уровень: {level_name}!"
            else:
                return False, f"Ваш текущий уровень ({current_level}) равен или выше уровня кода ({code_level})"
    
    def get_all_codes(self):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT code, level, duration, max_uses, used, created_at FROM admin_codes ORDER BY created_at DESC")
            return c.fetchall()
    
    def delete_code(self, code):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM admin_codes WHERE code = ?", (code,))
            conn.commit()
    
    def create_application(self, user_id, username, text):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO applications (user_id, username, text, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, username, text, get_moscow_time().isoformat()))
            conn.commit()
            return c.lastrowid
    
    def get_pending_applications(self):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, username, text, created_at FROM applications WHERE status = 'pending' ORDER BY created_at ASC")
            return c.fetchall()
    
    def get_application(self, app_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, username, text, status, created_at FROM applications WHERE id = ?", (app_id,))
            return c.fetchone()
    
    def approve_application(self, app_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("UPDATE applications SET status = 'approved', reviewed_at = ? WHERE id = ?", 
                     (get_moscow_time().isoformat(), app_id))
            conn.commit()
    
    def reject_application(self, app_id, reason):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("UPDATE applications SET status = 'rejected', reviewed_at = ?, review_reason = ? WHERE id = ?", 
                     (get_moscow_time().isoformat(), reason, app_id))
            conn.commit()
    
    def get_applications_stats_by_date(self, date_str):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT status FROM applications WHERE date(created_at) = ?", (date_str,))
            rows = c.fetchall()
            total = len(rows)
            pending = sum(1 for r in rows if r[0] == 'pending')
            approved = sum(1 for r in rows if r[0] == 'approved')
            rejected = sum(1 for r in rows if r[0] == 'rejected')
            return {'total': total, 'pending': pending, 'approved': approved, 'rejected': rejected, 'processed': approved + rejected}
    
    def get_user_applications_by_date(self, user_id, date_str):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT status FROM applications WHERE user_id = ? AND date(created_at) = ?", (user_id, date_str))
            return c.fetchall()
    
    def create_ticket(self, user_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO tickets (user_id, created_at, updated_at) VALUES (?, ?, ?)",
                     (user_id, get_moscow_time().isoformat(), get_moscow_time().isoformat()))
            conn.commit()
            return c.lastrowid
    
    def get_ticket(self, ticket_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, status, created_at FROM tickets WHERE id = ?", (ticket_id,))
            return c.fetchone()

    def get_user_open_tickets(self, user_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, status, created_at FROM tickets WHERE user_id = ? AND status = 'open' ORDER BY created_at DESC", (user_id,))
            return c.fetchall()
    
    def get_all_open_tickets(self):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, created_at FROM tickets WHERE status = 'open' ORDER BY created_at ASC")
            return c.fetchall()
    
    def add_ticket_message(self, ticket_id, user_id, message, is_admin=False):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO ticket_messages (ticket_id, user_id, message, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                     (ticket_id, user_id, message, 1 if is_admin else 0, get_moscow_time().isoformat()))
            c.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (get_moscow_time().isoformat(), ticket_id))
            conn.commit()
    
    def get_ticket_messages(self, ticket_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, message, is_admin, created_at FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at ASC", 
                     (ticket_id,))
            return c.fetchall()
    
    def close_ticket(self, ticket_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))
            conn.commit()
    
    def is_banned(self, user_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,))
            return c.fetchone() is not None
    
    def add_to_blacklist(self, user_id, reason):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO blacklist (user_id, reason, banned_at) VALUES (?, ?, ?)",
                     (user_id, reason, get_moscow_time().isoformat()))
            conn.commit()
    
    def remove_from_blacklist(self, user_id):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
            conn.commit()
    
    def get_blacklist(self):
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, reason, banned_at FROM blacklist")
            return c.fetchall()

db = Database()

# ========== БОТ С ИНИЦИАЛИЗАЦИЕЙ СТЕЙТОВ ==========
storage = StateMemoryStorage()
bot = telebot.TeleBot(BOT_TOKEN, state_storage=storage)
bot.add_custom_filter(custom_filters.StateFilter(bot))

# ========== FSM СТЕЙТЫ ==========
class ApplicationState(StatesGroup):
    waiting_for_text = State()

class TicketState(StatesGroup):
    waiting_for_message = State()

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
    waiting_for_level = State()
    waiting_for_time = State()

class RemoveAdminState(StatesGroup):
    waiting_for_username = State()

class CreateCodeState(StatesGroup):
    waiting_for_code_name = State()
    waiting_for_level = State()
    waiting_for_time = State()
    waiting_for_max_uses = State()

class DeleteCodeState(StatesGroup):
    waiting_for_code = State()

class BlacklistAddState(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()

class BlacklistRemoveState(StatesGroup):
    waiting_for_username = State()

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📝 Написать анкету", "📩 Написать тикет")
    kb.row("📅 Календарь анкет", "🎫 Код")
    if db.get_admin_level(user_id) >= 1:
        kb.row("👑 Админ панель")
    return kb

def get_admin_keyboard(level):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if level >= 1:
        kb.row("📝 Анкеты (новые)")
    if level >= 2:
        kb.row("📋 Тикеты (открытые)")
    if level >= 3:
        kb.row("🚫 Чёрный список")
        kb.row("👑 Управление админами")
        kb.row("🎫 Коды для админки")
    kb.row("🔙 Выйти в меню")
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

def get_ticket_actions_keyboard(ticket_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("💬 Ответить", callback_data=f"ticket_reply_{ticket_id}"),
        types.InlineKeyboardButton("❌ Закрыть", callback_data=f"ticket_close_{ticket_id}")
    )
    return kb

def get_application_actions_keyboard(app_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"app_approve_{app_id}"),
        types.InlineKeyboardButton("🎟 Одобрить с одн. слк.", callback_data=f"app_approve_one_{app_id}")
    )
    kb.row(
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"app_reject_{app_id}")
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
    
    kb.add(types.InlineKeyboardButton(
        text=f"📅 {MONTHS_RU[month]} {year}",
        callback_data=f"cal_select_ym_{year}_{month}"
    ))
    
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
            
            if total == 0:
                color = "⬛"
            elif percent == 0:
                color = "🟥"
            elif 1 <= percent <= 20:
                color = "🟧"
            elif 21 <= percent <= 40:
                color = "🟨"
            elif 41 <= percent <= 60:
                color = "🟩"
            elif 61 <= percent <= 80:
                color = "🟦"
            elif 81 <= percent <= 99:
                color = "🟪"
            else:
                color = "⬜"
            btn_text = f"{color} {day}"
        
        row.append(types.InlineKeyboardButton(
            text=btn_text,
            callback_data=f"cal_day_{date_str}"
        ))
        
        if len(row) == 7:
            kb.add(*row)
            row = []
    
    if row:
        kb.add(*row)
    
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
    month_buttons = [
        types.InlineKeyboardButton(text=MONTHS_RU[m], callback_data=f"cal_set_month_{year}_{m}")
        for m in range(1, 13)
    ]
    kb.add(*month_buttons)
    now = get_moscow_time()
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data=f"cal_back_to_{now.year}_{now.month}"))
    return kb

# ========== СТАРТ ==========
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"User_{user_id}"
    db.add_user(user_id, username)
    
    if db.is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 **Вы в чёрном списке!** Обратитесь к владельцу.")
        return
    
    bot.send_message(
        message.chat.id,
        "👋 **Добро пожаловать в приёмную Cult Flood!**\n\n"
        "✨ Здесь ты можешь:\n"
        "• **📝 Написать анкету** — подать заявку на вступление\n"
        "• **📩 Написать тикет** — задать вопрос или решить проблему\n"
        "• **📅 Календарь анкет** — посмотреть статистику по дням\n"
        "• **🎫 Код** — ввести специальный код для админки\n\n"
        "📌 После одобрения анкеты ты получишь ссылку на вход в чат!",
        reply_markup=get_main_keyboard(user_id)
    )

# ========== АНКЕТА ==========
@bot.message_handler(func=lambda msg: msg.text == "📝 Написать анкету")
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

@bot.message_handler(state=ApplicationState.waiting_for_text)
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
                bot.send_message(
                    admin_id,
                    f"📝 <b>НОВАЯ АНКЕТА #{app_id}</b>\n\n"
                    f"👤 От: @{safe_username} (ID: <code>{user_id}</code>)\n"
                    f"📅 Время: {get_moscow_time().strftime('%d.%m.%Y %H:%M')} МСК\n\n"
                    f"📋 <b>Текст:</b>\n{safe_text[:1000]}",
                    parse_mode="HTML",
                    reply_markup=get_application_actions_keyboard(app_id)
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    
    bot.send_message(
        message.chat.id,
        f"✅ **Анкета #{app_id} отправлена!**\n\nАдмины рассмотрят её в ближайшее время.",
        reply_markup=get_main_keyboard(user_id)
    )

# ========== ТИКЕТЫ ==========
@bot.message_handler(func=lambda msg: msg.text == "📩 Написать тикет")
def start_ticket(message):
    user_id = message.from_user.id
    if db.is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 Вы в чёрном списке!")
        return
    
    open_tickets = db.get_user_open_tickets(user_id)
    if not open_tickets:
        bot.set_state(user_id, TicketState.waiting_for_message, message.chat.id)
        bot.send_message(
            message.chat.id,
            "✏️ **Напиши своё сообщение для админов:**\n\n❌ Отмена — /cancel"
        )
    else:
        kb = types.InlineKeyboardMarkup()
        for tid, status, created_at in open_tickets:
            kb.add(types.InlineKeyboardButton(f"🎫 Тикет #{tid} (Открыт)", callback_data=f"user_view_ticket_{tid}"))
        kb.add(types.InlineKeyboardButton("➕ Создать новый тикет", callback_data="user_create_new_ticket"))
        
        bot.send_message(
            message.chat.id,
            "📩 **У вас есть открытые тикеты:**\nВы можете просмотреть диалог с админом или создать новый тикет.",
            reply_markup=kb
        )

@bot.callback_query_handler(func=lambda call: call.data == "user_create_new_ticket")
def user_create_new_ticket_cb(call):
    user_id = call.from_user.id
    bot.set_state(user_id, TicketState.waiting_for_message, call.message.chat.id)
    bot.send_message(call.message.chat.id, "✏️ **Напиши своё сообщение для админов:**\n\n❌ Отмена — /cancel")
    bot.answer_callback_query(call.id)

@bot.message_handler(state=TicketState.waiting_for_message)
def process_ticket(message):
    user_id = message.from_user.id
    msg_text = message.text
    
    if msg_text == "/cancel":
        bot.delete_state(user_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return
    
    ticket_id = db.create_ticket(user_id)
    db.add_ticket_message(ticket_id, user_id, msg_text)
    bot.delete_state(user_id, message.chat.id)
    
    username = db.get_username(user_id)
    safe_username = clean_html(username)
    safe_text = clean_html(msg_text)
    
    admins = db.get_all_admins()
    for admin_id, level, _, _ in admins:
        if level >= 1:
            try:
                bot.send_message(
                    admin_id,
                    f"🆕 <b>НОВЫЙ ТИКЕТ #{ticket_id}</b>\n\n"
                    f"👤 Пользователь: @{safe_username} (ID: <code>{user_id}</code>)\n"
                    f"📅 Время: {get_moscow_time().strftime('%d.%m.%Y %H:%M')} МСК\n\n"
                    f"📝 Сообщение:\n{safe_text[:1000]}",
                    parse_mode="HTML",
                    reply_markup=get_ticket_actions_keyboard(ticket_id)
                )
            except Exception as e:
                logger.error(f"Не удалось отправить тикет админу {admin_id}: {e}")
    
    bot.send_message(
        message.chat.id,
        f"✅ **Тикет #{ticket_id} создан!**\n\nАдмины ответят вам в ближайшее время.",
        reply_markup=get_main_keyboard(user_id)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_view_ticket_"))
def user_view_ticket(call):
    tid = int(call.data.split("_")[3])
    ticket = db.get_ticket(tid)
    if ticket:
        messages = db.get_ticket_messages(tid)
        text = f"📋 <b>Тикет #{tid}</b>\n\n"
        for msg in messages:
            author_id = msg[1]
            is_admin = msg[3]
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

@bot.message_handler(state=UserTicketReplyState.waiting_for_message)
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
        db.add_ticket_message(tid, user_id, message.text, is_admin=False)
        bot.send_message(message.chat.id, f"✅ Ответ отправлен в тикет #{tid}!", reply_markup=get_main_keyboard(user_id))
        
        username = db.get_username(user_id)
        admins = db.get_all_admins()
        for admin_id, level, _, _ in admins:
            if level >= 1:
                try:
                    bot.send_message(
                        admin_id,
                        f"💬 <b>НОВОЕ СООБЩЕНИЕ В ТИКЕТЕ #{tid}</b>\n\n"
                        f"👤 От: @{clean_html(username)} (ID: <code>{user_id}</code>)\n\n"
                        f"📝 {clean_html(message.text)}",
                        parse_mode="HTML",
                        reply_markup=get_ticket_actions_keyboard(tid)
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    bot.delete_state(user_id, message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_ticket_close_"))
def user_ticket_close_cb(call):
    tid = int(call.data.split("_")[3])
    db.close_ticket(tid)
    bot.edit_message_text(f"✅ Вы закрыли тикет #{tid}.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

# ========== КОД (АКТИВАЦИЯ) ==========
@bot.message_handler(func=lambda msg: msg.text == "🎫 Код")
def start_code(message):
    user_id = message.from_user.id
    bot.set_state(user_id, CodeState.waiting_for_code, message.chat.id)
    bot.send_message(
        message.chat.id,
        "🎫 **Введи код для активации админки:**\n\n❌ Отмена — /cancel"
    )

@bot.message_handler(state=CodeState.waiting_for_code)
def process_code(message):
    user_id = message.from_user.id
    code = message.text.strip().upper()
    
    if code == "/cancel":
        bot.delete_state(user_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return
    
    success, msg = db.use_admin_code(code, user_id)
    bot.delete_state(user_id, message.chat.id)
    bot.send_message(message.chat.id, msg, reply_markup=get_main_keyboard(user_id))

# ========== КАЛЕНДАРЬ ==========
@bot.message_handler(func=lambda msg: msg.text == "📅 Календарь анкет")
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
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
        kb = build_calendar_keyboard(year, month)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("cal_next_"):
        _, _, year_str, month_str = data.split("_")
        year, month = int(year_str), int(month_str)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        kb = build_calendar_keyboard(year, month)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("cal_day_"):
        date_str = data.split("_")[2]
        stats = db.get_applications_stats_by_date(date_str)
        user_apps = db.get_user_applications_by_date(call.from_user.id, date_str)
        
        total = stats['total']
        pending = stats['pending']
        approved = stats['approved']
        rejected = stats['rejected']
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
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
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
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)
        return

# ========== АДМИН ПАНЕЛЬ ==========
@bot.message_handler(func=lambda msg: msg.text == "👑 Админ панель")
def admin_panel(message):
    level = db.get_admin_level(message.from_user.id)
    if level < 1:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к админ-панели.")
        return
    
    bot.send_message(
        message.chat.id,
        "👑 **Админ панель**",
        reply_markup=get_admin_keyboard(level)
    )

@bot.message_handler(func=lambda msg: msg.text == "🔙 Выйти в меню")
def exit_admin(message):
    user_id = message.from_user.id
    bot.send_message(
        message.chat.id,
        "👋 Возврат в главное меню",
        reply_markup=get_main_keyboard(user_id)
    )

# ========== УПРАВЛЕНИЕ АДМИНАМИ ==========
@bot.message_handler(func=lambda msg: msg.text == "👑 Управление админами")
def admin_manage(message):
    level = db.get_admin_level(message.from_user.id)
    if level < 3:
        bot.send_message(message.chat.id, "❌ Нет доступа (нужен уровень 3)")
        return
    
    bot.send_message(message.chat.id, "👑 **Управление админами**", reply_markup=get_admin_manage_keyboard())

@bot.callback_query_handler(func=lambda call: call.data in ["add_admin", "remove_admin"])
def admin_manage_callbacks(call):
    level = db.get_admin_level(call.from_user.id)
    if level < 3:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return
    
    if call.data == "add_admin":
        bot.set_state(call.from_user.id, AddAdminState.waiting_for_username, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите @username или Telegram ID пользователя:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

    elif call.data == "remove_admin":
        bot.set_state(call.from_user.id, RemoveAdminState.waiting_for_username, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите @username или Telegram ID админа для удаления:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

# 🔥 ИСПРАВЛЕННЫЙ ОБРАБОТЧИК КНОПКИ "Список админов"
@bot.callback_query_handler(func=lambda call: call.data == "list_admins")
def list_admins_callback(call):
    level = db.get_admin_level(call.from_user.id)
    if level < 3:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return

    admins = db.get_all_admins()
    if not admins:
        bot.answer_callback_query(call.id, "📭 Список админов пуст.", show_alert=True)
        return
    
    text = "👑 <b>Список администраторов:</b>\n\n"
    for admin_id, lvl, added_at, expires_at in admins:
        lvl_name = {1: "Админ", 2: "Ст.админ", 3: "Владелец"}.get(lvl, "Неизвестно")
        username = db.get_username(admin_id)
        
        if expires_at:
            expire_date = datetime.fromisoformat(expires_at)
            if expire_date < get_moscow_time():
                time_text = "❌ Истёк"
            else:
                delta = expire_date - get_moscow_time()
                time_text = f"осталось {delta.days}д {delta.seconds // 3600}ч"
        else:
            time_text = "бессрочно"
        
        text += f"🆔 <code>{admin_id}</code> | @{clean_html(username)}\n📊 Уровень: <b>{lvl_name}</b> ({time_text})\n\n"
    
    try:
        bot.send_message(call.message.chat.id, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка отправки списка админов: {e}")
    
    bot.answer_callback_query(call.id)

@bot.message_handler(state=AddAdminState.waiting_for_username)
def add_admin_username(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    target_id = db.find_user_id(message.text)
    if not target_id:
        bot.send_message(message.chat.id, "❌ Пользователь не найден в БД бота. Попросите его сначала запустить бота /start!")
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['target_id'] = target_id
        
    bot.set_state(message.from_user.id, AddAdminState.waiting_for_level, message.chat.id)
    bot.send_message(
        message.chat.id,
        "✏️ Введите уровень админа:\n\n1 — Админ\n2 — Ст.админ\n3 — Владелец"
    )

@bot.message_handler(state=AddAdminState.waiting_for_level)
def add_admin_level(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    try:
        level = int(message.text.strip())
        if level not in [1, 2, 3]:
            raise ValueError
    except:
        bot.send_message(message.chat.id, "❌ Введите число 1, 2 или 3")
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['admin_level'] = level

    bot.set_state(message.from_user.id, AddAdminState.waiting_for_time, message.chat.id)
    bot.send_message(
        message.chat.id,
        "✏️ Введите срок действия (например: `5d`, `12h`, `1y`) или `0` для бессрочного решения:",
        parse_mode="Markdown"
    )

@bot.message_handler(state=AddAdminState.waiting_for_time)
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
    
    level_name = {1: "Админ", 2: "Ст.админ", 3: "Владелец"}[level]
    bot.send_message(message.chat.id, f"✅ Пользователь `{target_id}` назначен: {level_name}", parse_mode="Markdown")

@bot.message_handler(state=RemoveAdminState.waiting_for_username)
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
        bot.send_message(message.chat.id, "❌ Нельзя удалить владельца!")
        bot.delete_state(message.from_user.id, message.chat.id)
        return
    
    db.remove_admin(target_id)
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, f"✅ Админ `{target_id}` был удалён.", parse_mode="Markdown")

# ========== ЧЁРНЫЙ СПИСОК ==========
@bot.message_handler(func=lambda msg: msg.text == "🚫 Чёрный список")
def admin_blacklist(message):
    level = db.get_admin_level(message.from_user.id)
    if level < 3:
        bot.send_message(message.chat.id, "❌ Нет доступа")
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
    level = db.get_admin_level(call.from_user.id)
    if level < 3:
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

@bot.message_handler(state=BlacklistAddState.waiting_for_username)
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

@bot.message_handler(state=BlacklistAddState.waiting_for_reason)
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

@bot.message_handler(state=BlacklistRemoveState.waiting_for_username)
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

# ========== КОДЫ ДЛЯ АДМИНКИ ==========
@bot.message_handler(func=lambda msg: msg.text == "🎫 Коды для админки")
def admin_codes(message):
    level = db.get_admin_level(message.from_user.id)
    if level < 3:
        bot.send_message(message.chat.id, "❌ Нет доступа")
        return
    
    bot.send_message(
        message.chat.id,
        "🎫 **Управление кодами для админки**",
        reply_markup=get_codes_manage_keyboard()
    )

@bot.callback_query_handler(func=lambda call: call.data in ["create_code", "list_codes", "delete_code"])
def codes_callbacks(call):
    level = db.get_admin_level(call.from_user.id)
    if level < 3:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return

    if call.data == "create_code":
        bot.set_state(call.from_user.id, CreateCodeState.waiting_for_code_name, call.message.chat.id)
        bot.send_message(
            call.message.chat.id,
            "✏️ **Введите название кода** (буквы, цифры, дефисы или подчёркивания):\n\n"
            "Пример: `PROMO2026` или `ADM_CODE`\n\n❌ Отмена — /cancel",
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)

    elif call.data == "list_codes":
        codes = db.get_all_codes()
        if not codes:
            bot.send_message(call.message.chat.id, "📭 Созданных кодов нет.")
            bot.answer_callback_query(call.id)
            return
        
        text = "🎫 **Список кодов:**\n\n"
        for code, lvl, duration, max_uses, used, _ in codes:
            lvl_name = {1: "Админ", 2: "Ст.админ", 3: "Владелец"}[lvl]
            time_text = duration if duration else "бессрочно"
            text += f"🔹 `{code}` | {lvl_name} | {used}/{max_uses} исп. ({time_text})\n"
        
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    elif call.data == "delete_code":
        bot.set_state(call.from_user.id, DeleteCodeState.waiting_for_code, call.message.chat.id)
        bot.send_message(call.message.chat.id, "✏️ Введите код для удаления:\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)

@bot.message_handler(state=CreateCodeState.waiting_for_code_name)
def create_code_name(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    code = message.text.strip().upper()
    if not re.match(r'^[A-Z0-9_-]{3,20}$', code):
        bot.send_message(
            message.chat.id,
            "❌ **Некорректное название кода!**\n\n"
            "Используйте только английские буквы, цифры, дефис или подчёркивание (от 3 до 20 символов).\n"
            "Пример: `PROMO2026`, `CULT_ADMIN`\n\n"
            "Попробуйте ещё раз или отмените — /cancel",
            parse_mode="Markdown"
        )
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['code'] = code

    bot.set_state(message.from_user.id, CreateCodeState.waiting_for_level, message.chat.id)
    bot.send_message(
        message.chat.id,
        "✏️ **Выберите уровень привилегий для кода:**\n\n"
        "1 — Админ\n2 — Ст.админ\n3 — Владелец\n\n"
        "Введите число 1, 2 или 3:"
    )

@bot.message_handler(state=CreateCodeState.waiting_for_level)
def create_code_level(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    try:
        level = int(message.text.strip())
        if level not in [1, 2, 3]:
            raise ValueError
    except:
        bot.send_message(message.chat.id, "❌ Пожалуйста, введите число 1, 2 или 3:")
        return
    
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['code_level'] = level

    bot.set_state(message.from_user.id, CreateCodeState.waiting_for_time, message.chat.id)
    bot.send_message(
        message.chat.id,
        "✏️ Введите срок действия админки (например: `5d`, `12h`, `30d`) или `0` для бессрочной:",
        parse_mode="Markdown"
    )

@bot.message_handler(state=CreateCodeState.waiting_for_time)
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

@bot.message_handler(state=CreateCodeState.waiting_for_max_uses)
def create_code_max_uses(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    try:
        max_uses = int(message.text.strip())
        if max_uses <= 0:
            raise ValueError
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

@bot.message_handler(state=DeleteCodeState.waiting_for_code)
def delete_code_process(message):
    if message.text == "/cancel":
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(message.from_user.id))
        return

    db.delete_code(message.text.strip().upper())
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, "✅ Код был успешно удалён!")

# ========== ОБРАБОТКА АНКЕТ И ТИКЕТОВ (АДМИН) ==========
@bot.message_handler(func=lambda msg: msg.text == "📝 Анкеты (новые)")
def admin_applications(message):
    if db.get_admin_level(message.from_user.id) < 1:
        return
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

@bot.message_handler(func=lambda msg: msg.text == "📋 Тикеты (открытые)")
def admin_tickets(message):
    if db.get_admin_level(message.from_user.id) < 1:
        return
    tickets = db.get_all_open_tickets()
    if not tickets:
        bot.send_message(message.chat.id, "📭 Нет открытых тикетов")
        return
    for tid, uid, created_at in tickets:
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("💬 Читать", callback_data=f"view_ticket_{tid}"),
            types.InlineKeyboardButton("❌ Закрыть", callback_data=f"ticket_close_{tid}")
        )
        bot.send_message(
            message.chat.id,
            f"🆔 <b>Тикет #{tid}</b>\n👤 @{clean_html(db.get_username(uid))}",
            reply_markup=kb,
            parse_mode="HTML"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("app_approve_") and not call.data.startswith("app_approve_one_"))
def approve_app(call):
    try:
        app_id = int(call.data.split("_")[2])
        app = db.get_application(app_id)
        
        if not app:
            bot.answer_callback_query(call.id, "❌ Анкета не найдена в базе!", show_alert=True)
            return

        db.approve_application(app_id)
        admin_uname = call.from_user.username or call.from_user.first_name
        
        try:
            bot.edit_message_text(
                f"✅ <b>Анкета #{app_id} ОДОБРЕНА</b>\n"
                f"👤 Пользователь: @{clean_html(app[2])} (ID: <code>{app[1]}</code>)\n"
                f"👑 Одобрил админ: @{clean_html(admin_uname)}",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось обновить текст карточки анкеты: {e}")
        
        user_msg = (
            "🎉 <b>Ваша анкета одобрена!</b>\n\n"
            f"🔗 <b>Ссылка на вступление:</b> {CHAT_INVITE_LINK}\n\n"
            "📌 <i>После подачи заявки по ссылке админы одобрят её!</i>"
        )
        try:
            bot.send_message(app[1], user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить юзеру {app[1]}: {e}")
            bot.send_message(call.message.chat.id, f"⚠️ Анкета #{app_id} одобрена, но не удалось написать пользователю в ЛС (возможно, бот заблокирован).")

        bot.answer_callback_query(call.id, "✅ Анкета успешно одобрена!", show_alert=True)

    except Exception as e:
        logger.error(f"Ошибка при одобрении анкеты: {e}")
        bot.answer_callback_query(call.id, f"❌ Произошла ошибка: {e}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("app_approve_one_"))
def approve_app_with_one_time_link(call):
    try:
        app_id = int(call.data.split("_")[3])
        app = db.get_application(app_id)
        
        if not app:
            bot.answer_callback_query(call.id, "❌ Анкета не найдена в базе!", show_alert=True)
            return

        try:
            invite_link = bot.create_chat_invite_link(
                chat_id=TARGET_GROUP_ID,
                member_limit=1
            )
            one_time_url = invite_link.invite_link
        except Exception as e:
            logger.error(f"Не удалось создать одноразовую ссылку: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка создания ссылки: убедитесь, что бот админ в группе с правом приглашений!", show_alert=True)
            return

        db.approve_application(app_id)
        admin_uname = call.from_user.username or call.from_user.first_name
        
        try:
            bot.edit_message_text(
                f"✅ <b>Анкета #{app_id} ОДОБРЕНА (с одн. ссылкой)</b>\n"
                f"👤 Пользователь: @{clean_html(app[2])} (ID: <code>{app[1]}</code>)\n"
                f"👑 Одобрил админ: @{clean_html(admin_uname)}",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось обновить текст карточки анкеты: {e}")
        
        user_msg = (
            "🎉 <b>Ваша анкета одобрена!</b>\n\n"
            f"🔗 <b>Ваша личная одноразовая ссылка на вступление:</b> {one_time_url}\n\n"
            "📌 <i>Ссылка активна только для одного использования.</i>"
        )
        try:
            bot.send_message(app[1], user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить юзеру {app[1]}: {e}")
            bot.send_message(call.message.chat.id, f"⚠️ Анкета #{app_id} одобрена, но не удалось написать пользователю в ЛС (возможно, бот заблокирован).")

        bot.answer_callback_query(call.id, "✅ Анкета одобрена, одноразовая ссылка отправлена!", show_alert=True)

    except Exception as e:
        logger.error(f"Ошибка при одобрении анкеты с одноразовой ссылкой: {e}")
        bot.answer_callback_query(call.id, f"❌ Произошла ошибка: {e}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("app_reject_"))
def reject_app_start(call):
    try:
        app_id = int(call.data.split("_")[2])
        admin_id = call.from_user.id
        
        app = db.get_application(app_id)
        if not app:
            bot.answer_callback_query(call.id, "❌ Анкета не найдена!", show_alert=True)
            return

        bot.delete_state(admin_id, call.message.chat.id)
        bot.set_state(admin_id, RejectReasonState.waiting_for_reason, call.message.chat.id)
        
        with bot.retrieve_data(admin_id, call.message.chat.id) as data:
            data['app_id'] = app_id
            data['orig_msg_id'] = call.message.message_id
            
        bot.send_message(call.message.chat.id, f"✏️ **Введите причину отклонения анкеты #{app_id}:**\n\n❌ Отмена — /cancel")
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при запуске отклонения: {e}")
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}", show_alert=True)

@bot.message_handler(state=RejectReasonState.waiting_for_reason)
def reject_app_process(message):
    admin_id = message.from_user.id
    reason = message.text
    
    if reason == "/cancel":
        bot.delete_state(admin_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(admin_id))
        return

    app_id = None
    orig_msg_id = None
    try:
        with bot.retrieve_data(admin_id, message.chat.id) as data:
            app_id = data.get('app_id')
            orig_msg_id = data.get('orig_msg_id')
    except Exception as e:
        logger.error(f"Ошибка чтения FSM: {e}")
        
    if not app_id:
        bot.send_message(message.chat.id, "❌ Ошибка: не удалось найти ID анкеты. Нажмите «Отклонить» повторно.")
        bot.delete_state(admin_id, message.chat.id)
        return

    try:
        app = db.get_application(app_id)
        if app:
            db.reject_application(app_id, reason)
            admin_uname = message.from_user.username or message.from_user.first_name
            
            if orig_msg_id:
                try:
                    bot.edit_message_text(
                        f"❌ <b>Анкета #{app_id} ОТКЛОНЕНА</b>\n"
                        f"👤 Пользователь: @{clean_html(app[2])} (ID: <code>{app[1]}</code>)\n"
                        f"👑 Отклонил админ: @{clean_html(admin_uname)}\n"
                        f"📝 Причина: {clean_html(reason)}",
                        message.chat.id,
                        orig_msg_id,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Ошибка редактирования карточки анкеты: {e}")

            bot.send_message(message.chat.id, f"❌ Анкета #{app_id} отклонена.", reply_markup=get_main_keyboard(admin_id))
            
            user_msg = (
                "❌ <b>Ваша анкета была отклонена</b>\n\n"
                f"📝 <b>Причина:</b> {clean_html(reason)}\n\n"
                "💡 Вы можете исправить ошибки и подать анкету повторно."
            )
            try:
                bot.send_message(app[1], user_msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю: {e}")
        else:
            bot.send_message(message.chat.id, "❌ Анкета не найдена в базе данных.")
    except Exception as e:
        logger.error(f"Ошибка при отклонении анкеты #{app_id}: {e}")
        bot.send_message(message.chat.id, f"❌ Произошла ошибка при отклонении: {e}")
    finally:
        bot.delete_state(admin_id, message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_ticket_"))
def view_ticket(call):
    tid = int(call.data.split("_")[2])
    ticket = db.get_ticket(tid)
    if ticket:
        messages = db.get_ticket_messages(tid)
        text = f"📋 <b>Тикет #{tid}</b> | Пользователь: <code>{ticket[1]}</code> (@{clean_html(db.get_username(ticket[1]))})\n\n"
        for msg in messages:
            author_id = msg[1]
            is_admin = msg[3]
            author_uname = db.get_username(author_id)
            
            role = f"👑 Админ (@{clean_html(author_uname)})" if is_admin else f"👤 @{clean_html(author_uname)}"
            text += f"[{role}]: {clean_html(msg[2])}\n\n"
            
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=get_ticket_actions_keyboard(tid), parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ticket_reply_"))
def ticket_reply_start(call):
    tid = int(call.data.split("_")[2])
    bot.delete_state(call.from_user.id, call.message.chat.id)
    bot.set_state(call.from_user.id, AdminReplyState.waiting_for_reply, call.message.chat.id)
    with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        data['ticket_id'] = tid

    bot.send_message(call.message.chat.id, "✏️ Введите ваш ответ:\n\n❌ Отмена — /cancel")
    bot.answer_callback_query(call.id)

@bot.message_handler(state=AdminReplyState.waiting_for_reply)
def process_ticket_reply(message):
    admin_id = message.from_user.id
    if message.text == "/cancel":
        bot.delete_state(admin_id, message.chat.id)
        bot.send_message(message.chat.id, "❌ Отменено", reply_markup=get_main_keyboard(admin_id))
        return

    with bot.retrieve_data(admin_id, message.chat.id) as data:
        tid = data.get('ticket_id')

    ticket = db.get_ticket(tid)
    if ticket:
        db.add_ticket_message(tid, admin_id, message.text, is_admin=True)
        bot.send_message(message.chat.id, f"✅ Ответ отправлен в тикет #{tid}.")
        
        admin_uname = db.get_username(admin_id)
        try:
            bot.send_message(
                ticket[1], 
                f"📩 <b>Ответ от администрации (Тикет #{tid}):</b>\n"
                f"👑 <b>Админ (@{clean_html(admin_uname)}):</b>\n\n{clean_html(message.text)}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить ответ автору тикета: {e}")
            
    bot.delete_state(admin_id, message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ticket_close_"))
def close_ticket(call):
    tid = int(call.data.split("_")[2])
    db.close_ticket(tid)
    bot.edit_message_text(f"✅ Тикет #{tid} успешно закрыт.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

# ========== НАВИГАЦИЯ И ОТМЕНА ==========
@bot.callback_query_handler(func=lambda call: call.data == "back_to_admin")
def back_to_admin(call):
    lvl = db.get_admin_level(call.from_user.id)
    bot.edit_message_text("👑 **Админ панель**", call.message.chat.id, call.message.message_id, reply_markup=get_admin_keyboard(lvl))
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['cancel'])
def cancel_action(message):
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, "❌ Действие отменено", reply_markup=get_main_keyboard(message.from_user.id))

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    logger.info("✅ Бот успешно запущен!")
    bot.infinity_polling(skip_pending=True)