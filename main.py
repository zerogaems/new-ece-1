import os
import time
import sqlite3
import logging
import threading
from datetime import datetime
from html import escape
from collections import defaultdict, deque
from flask import Flask
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# ============================================================
# SECURE CONFIGURATION
# ============================================================
# IMPORTANT: never put the bot token in this file or GitHub.
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Add it to Render Environment Variables.")

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@Hamak456").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "7547218555").split(",")
    if x.strip().isdigit()
}
DB_PATH = os.environ.get("DB_PATH", "bot_database.db")
RATE_WINDOW = 10
RATE_LIMIT = 8
FEEDBACK_WINDOW = 3600
FEEDBACK_LIMIT = 3
SUB_CACHE_SECONDS = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("student_bot")

bot = telebot.TeleBot(TOKEN, threaded=True)

# ============================================================
# IN-MEMORY STATE (short-lived only; persistent data stays in DB)
# ============================================================
user_states = {}
admin_states = {}
rate_buckets = defaultdict(deque)
feedback_buckets = defaultdict(deque)
subscription_cache = {}
state_lock = threading.RLock()

app = Flask(__name__)

@app.route("/")
def home():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

threading.Thread(target=run_web, daemon=True, name="web-server").start()

# ============================================================
# DATABASE
# ============================================================
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db_connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT,
            text_content TEXT,
            file_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            db_key TEXT NOT NULL,
            material_name TEXT NOT NULL,
            lecture_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            db_key TEXT NOT NULL,
            material_name TEXT NOT NULL,
            lecture_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, db_key, material_name, lecture_name)
        );
        CREATE TABLE IF NOT EXISTS recent_views (
            user_id INTEGER NOT NULL,
            db_key TEXT NOT NULL,
            material_name TEXT NOT NULL,
            lecture_name TEXT NOT NULL,
            viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, db_key, material_name, lecture_name)
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            first_name TEXT,
            message TEXT NOT NULL,
            admin_reply TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            replied_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS broadcast_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            total INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id);
        CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at);
        CREATE INDEX IF NOT EXISTS idx_recent_user ON recent_views(user_id, viewed_at);
        CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id);
        """)

init_db()

# ============================================================
# HELPERS / SECURITY
# ============================================================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def audit(admin_id, action, target="", details=""):
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO audit_logs (admin_id, action, target, details) VALUES (?, ?, ?, ?)",
                         (admin_id, action, str(target)[:200], str(details)[:1000]))
    except Exception:
        logger.exception("Audit log failed")

def is_user_banned(user_id):
    with db_connect() as conn:
        return conn.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,)).fetchone() is not None

def ban_user(user_id):
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO banned_users(user_id) VALUES(?)", (user_id,))

def unban_user(user_id):
    with db_connect() as conn:
        cur = conn.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        return cur.rowcount > 0

def add_user(user_id, first_name):
    with db_connect() as conn:
        conn.execute("""INSERT INTO users(user_id, first_name) VALUES(?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name,
                       last_seen=CURRENT_TIMESTAMP, is_active=1""", (user_id, first_name[:100]))

def mark_user_inactive(user_id):
    with db_connect() as conn:
        conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (user_id,))

def get_all_users():
    with db_connect() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users WHERE is_active=1 ORDER BY user_id")]

def get_users_count():
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def rate_allowed(user_id):
    now = time.monotonic()
    with state_lock:
        q = rate_buckets[user_id]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True

def feedback_allowed(user_id):
    now = time.monotonic()
    with state_lock:
        q = feedback_buckets[user_id]
        while q and now - q[0] > FEEDBACK_WINDOW:
            q.popleft()
        if len(q) >= FEEDBACK_LIMIT:
            return False
        q.append(now)
        return True

def cleanup_states():
    while True:
        time.sleep(600)
        cutoff = time.monotonic() - 3600
        with state_lock:
            # States are timestamped as (state, updated_at).
            for store in (user_states, admin_states):
                for uid in list(store):
                    value = store[uid]
                    if isinstance(value, dict) and value.get("_updated", time.monotonic()) < cutoff:
                        store.pop(uid, None)
            for uid in list(subscription_cache):
                if subscription_cache[uid][1] < time.time():
                    subscription_cache.pop(uid, None)

threading.Thread(target=cleanup_states, daemon=True, name="state-cleaner").start()

def set_user_state(chat_id, **values):
    values["_updated"] = time.monotonic()
    with state_lock:
        user_states[chat_id] = values

def reset_user_state(chat_id):
    set_user_state(chat_id, year=None, sem=None, material=None)

def set_admin_state(user_id, **values):
    values["_updated"] = time.monotonic()
    with state_lock:
        admin_states[user_id] = values

def clear_admin_state(user_id):
    with state_lock:
        admin_states.pop(user_id, None)

def escape_md_text(text):
    return str(text).replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")

# ============================================================
# STATIC COURSE CATALOG
# This remains the source of truth for lecture names + File IDs.
# To add/change/delete a lecture, edit the database below and redeploy.
# To obtain a File ID: use the admin button "📎 استخراج File ID" and send the file.
# ============================================================
database = {'year_1_sem_1': [{'name': 'رياضيات1 📐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'فيزيا ⚡', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'تقانة 💻', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'خوارزميات 🧩', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'لغة1 🇬🇧', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'حماية بيئة 🌿', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'لغة عربية 📖', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}],
            'year_1_sem_2': [{'name': 'رياضيات2 📐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'ميكانيك ⚙️', 'content': {'مقرر كامل 📄': 'BQACAgQAAxkBAAIOtGqdKyBeezocYUtVXHhDULWaWzCPAAIgJQACyZjYUDRABVPmiW0iPQQ', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'برمجة 💻', 'content': {'برمجة نظري 📄': 'BQACAgQAAxkBAAIOsGqdKoTRvXpOkmdzyoXAfBKyWai8AAKUHwACWH24UMSW_RHmBGYLPQQ', 'برمجة عملي  📄': 'BQACAgQAAxkBAAIOsmqdKr_TnzATC2p-7k8Q3FK4gk9qAAKVHwACWH24UI3OBlPAzSO3PQQ', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'لغة2 🇬🇧', 'content': {'محاضرة 1 📄': 'BQACAgQAAxkBAAIKMmqTI84jkGbJC5fxdGfoHc4BQVpwAAIlHQACi-ohUs7uMgV_4xttPQQ', '📝 دورات سابقة': 'BQACAgQAAxkBAAIKNGqTI9eq2hpghJ4Hz6EUSOSXgj6WAAK8HAACIOEoUlA1QE3oHcC4PQQ'}}, 
                             {'name': 'اسس كهربا 🔌', 'content': {'تجارب المخبر 📄': 'BQACAgQAAxkBAAIOumqdLJNTOGfaF5wwywKJGiHvmGkGAAIwJQACyZjYUJ3pW6Tcx1HcPQQ', 'المقرر كامل 📄': 'BQACAgQAAxkBAAIOuGqdLHb4thNX8PAs74WqgxtVv8f5AAIuJQACyZjYUH7TU6EiFxwQPQQ'}},
                             {'name': 'صحة عامة 🏥', 'content': {'محاضرة 1 📄': 'BQACAgQAAxkBAAILBmqTtVV44UnTVNziyt5eGPEiS9MOAAI3GwACi-opUlPyu_XPA5P6PQQ', 'محاضرة 2 📄': 'BQACAgQAAxkBAAILCGqTtWCLZ-OZXbP0azVHlVqoqeKQAAI8GwACi-opUoL3FQEJSgdXPQQ', 'محاضرة 3 📄': 'BQACAgQAAxkBAAILCmqTtWpCoN1hl10jb6IFCp7h5-pqAAJhGwACi-opUi-rIxtCrslIPQQ', 'محاضرة 4 📄': 'BQACAgQAAxkBAAILDGqTtZE0qctHfdmmd0mohFhuBVLyAAI5GwACi-opUkf8n5XUbQWdPQQ', 'محاضرة 5 📄': 'BQACAgQAAxkBAAILDmqTtZsoAs_kCN2KwUHOZgZbCuH5AAI6GwACi-opUmHqmlpqR5JEPQQ', 'محاضرة 6 📄': 'BQACAgQAAxkBAAILEGqTtaL6wm_UNP3v9WbvxGUpdmCTAAI7GwACi-opUtsx3V-7tWYRPQQ', 'محاضرة 7 📄': 'BQACAgQAAxkBAAILCGqTtWCLZ-OZXbP0azVHlVqoqeKQAAI8GwACi-opUoL3FQEJSgdXPQQ', 'محاضرة 8 📄': 'BQACAgQAAxkBAAILE2qTtbi90VrAKzA8pocIPCfhDYm3AAI9GwACi-opUkh-wmkKxZzJPQQ', 'محاضرة 9 📄': 'BQACAgQAAxkBAAILFGqTtbgD6BEwSqY7OEZAK_UQ8a2IAAJiGwACi-opUiASFxjf4koBPQQ', 'محاضرة 10 📄': 'BQACAgQAAxkBAAILFWqTtbiXR5dgzJlaP35rZy3MfRmIAAK9HAACIOEoUty_CbP9uj8bPQQ', '📝 دورات سابقة': 'BQACAgQAAxkBAAILFWqTtbiXR5dgzJlaP35rZy3MfRmIAAK9HAACIOEoUty_CbP9uj8bPQQ'}}, 
                             {'name': 'ثقافة 📚', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}], 
            'year_2_sem_1': [{'name': 'رياضيات3 📐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'الكترون ⚡', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'oop 💻', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'تصميم 🧩', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'لغة3 🇬🇧', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': ' بني تحتية 🏥', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': ' اسس دارات 🔌', 'content': {'المقرر كامل  📄': 'BQACAgQAAxkBAAIMBWqW4VdzqhT85RVoyOSsij6ZFQ1_AAIVKQACWH2wUGuzKitNjlZEPQQ'}}],
            'year_2_sem_2': [{'name': 'رياضيات4 📐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'تمثيلية ⚡', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'اشارات 💻', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'انتشار 🧩', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'لغة4 🇬🇧', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'اقتصاد🌿', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'مضخمات 🔌', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}], 

            
            'year_3_sem_1': [{'name': 'قياسات 📏', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'دارات منطقية 🔢', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'معالجة اشارة 📡', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'نظرية الاحتمالات 🎲', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'بنية حاسب 💻', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'دارات خطية ولا خطية 📈', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'اتصالات رقمية 🔢', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}], 
            'year_3_sem_2': [{'name': 'ترميز - نظم معلومات 🔐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'تحكم الي 🤖', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'مكروية - هندسة امواج 🌊', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'دارات متكاملة 🔲', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'معالجة ومتحكمات 🎛️', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'تراسل معطيات 📲', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'الرسم باستخدام الحاسب 🎨', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}],
            'year_4_sem_1': [{'name': 'تلفزيون 📺', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'رادار و سونار 🛰️', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'شبكات 🌐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'نمذجة 📊', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'هوائيات 📡', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'مقاسم 📞', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}],
            'year_4_sem_2': [{'name': 'معالجة اشارة 📶', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'تقانة الانترنت 🌐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'بروتوكلات الانترنت 🔌', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'اتصالات ضوئية 💡', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'كهرصوت - تطبيقات امواج 🔊', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'خليوي 1 📱', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'تدريب ميداني 🛠️', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}], 
            'year_5_sem_1': [{'name': 'امن الشبكات 🛡️', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'دارات مكروية 🔬', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'خليوي 2 📱', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'تصميم شبكات 📐', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'تقانات ثانوية ⚡', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'مخبر شبكات 💻', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}], 
            'year_5_sem_2': [{'name': 'حساسات - شبكات لاسلكية 📟', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}},
                             {'name': 'برمجة وادارة الشبكات ⚙️', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'الوثوقية والمعايرة ⚖️', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'ذكاء اصطناعي 🧠', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'اتصالات فضائية 🚀', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}, 
                             {'name': 'تطبيقات برمجية 🖥️', 'content': {'محاضرة 1 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 2 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 3 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 4 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 5 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 6 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 7 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 8 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 9 📄': 'PASTE_FILE_ID_HERE', 'محاضرة 10 📄': 'PASTE_FILE_ID_HERE', '📝 دورات سابقة': 'PASTE_FILE_ID_HERE'}}]}

# ============================================================
# CATALOG HELPERS
# ============================================================
def catalog_materials(db_key):
    return database.get(db_key, [])

def find_material(db_key, material_name):
    return next((m for m in catalog_materials(db_key) if m["name"] == material_name), None)

def find_file_id(db_key, material_name, lecture_name):
    mat = find_material(db_key, material_name)
    if not mat:
        return None
    value = mat.get("content", {}).get(lecture_name)
    if value in (None, "", "PASTE_FILE_ID_HERE", "ضع_هنا_file_id"):
        return None
    return value

def catalog_search(query, limit=15):
    q = query.casefold().strip()
    results = []
    for db_key, materials in database.items():
        for mat in materials:
            for lecture, file_id in mat.get("content", {}).items():
                if q in mat["name"].casefold() or q in lecture.casefold():
                    if file_id not in (None, "", "PASTE_FILE_ID_HERE", "ضع_هنا_file_id"):
                        results.append((db_key, mat["name"], lecture))
                        if len(results) >= limit:
                            return results
    return results

def code_snippet(db_key, material, lecture, file_id=None):
    value = file_id or "PASTE_FILE_ID_HERE"
    return f'"{lecture}": "{value}"'

def find_catalog_location(material_name, lecture_name):
    matches = []
    for db_key, materials in database.items():
        for mat in materials:
            if mat["name"] == material_name and lecture_name in mat.get("content", {}):
                matches.append(db_key)
    return matches

# ============================================================
# KEYBOARDS
# ============================================================
def get_years_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("السنة الأولى 1️⃣", "السنة الثانية 2️⃣")
    markup.row("السنة الثالثة 3️⃣", "السنة الرابعة 4️⃣")
    markup.row("السنة الخامسة 5️⃣")
    markup.row("💬 أرسل ملاحظة/استفسار", "📅 برنامج الامتحان")
    return markup

def get_semesters_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("الفصل الأول 📘", "الفصل الثاني 📙")
    markup.row("🏠 العودة للرئيسية")
    return markup

def get_materials_keyboard(materials_list):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    row = []
    for item in materials_list:
        row.append(KeyboardButton(item["name"]))
        if len(row) == 2:
            markup.row(*row); row = []
    if row: markup.row(*row)
    markup.row("🔙 رجوع للفصول", "🏠 العودة للرئيسية")
    return markup

def get_content_keyboard(content_dict, db_key, material_name, user_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    row = []
    for key, value in content_dict.items():
        if key == "📝 دورات سابقة":
            continue
        row.append(KeyboardButton(key))
        if len(row) == 2:
            markup.row(*row); row = []
    if row: markup.row(*row)
    if "📝 دورات سابقة" in content_dict:
        markup.row("📝 دورات سابقة")
    markup.row("🔙 رجوع للمواد", "🏠 العودة للرئيسية")
    return markup

def get_admin_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📊 إحصائيات البوت", "📢 إرسال إذاعة")
    markup.row("📎 استخراج File ID", "🧾 تجهيز سطر ملف")
    markup.row("🗑️ تجهيز حذف من الكود", "📸 برنامج الامتحان")
    markup.row("📦 نسخة احتياطية", "🚫 حظر / إلغاء حظر")
    markup.row("📜 سجل الإدارة", "🏠 العودة للرئيسية")
    return markup

def get_sub_inline_markup():
    markup = InlineKeyboardMarkup()
    clean_username = CHANNEL_USERNAME.lstrip("@")
    markup.add(InlineKeyboardButton("📢 رابط القناة", url=f"https://t.me/{clean_username}"))
    markup.add(InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_subscription"))
    return markup

def check_sub(user_id, force=False):
    now = time.time()
    cached = subscription_cache.get(user_id)
    if cached and not force and cached[1] > now:
        return cached[0]
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        ok = member.status in ["creator", "administrator", "member", "restricted"]
        subscription_cache[user_id] = (ok, now + SUB_CACHE_SECONDS)
        return ok
    except Exception:
        logger.exception("Subscription check failed")
        return False

def require_private(message):
    if message.chat.type != "private":
        try:
            bot.reply_to(message, "ℹ️ استخدم البوت في المحادثة الخاصة فقط.")
        except Exception:
            pass
        return False
    return True

# ============================================================
# PERSISTENT USER FEATURES
# ============================================================
def record_download(user_id, db_key, material, lecture):
    with db_connect() as conn:
        conn.execute("INSERT INTO downloads(user_id,db_key,material_name,lecture_name) VALUES(?,?,?,?)",
                     (user_id, db_key, material, lecture))
        conn.execute("""INSERT INTO recent_views(user_id,db_key,material_name,lecture_name)
                       VALUES(?,?,?,?,?) ON CONFLICT(user_id,db_key,material_name,lecture_name)
                       DO UPDATE SET viewed_at=CURRENT_TIMESTAMP""", (user_id, db_key, material, lecture))

def toggle_favorite(user_id, db_key, material, lecture):
    with db_connect() as conn:
        exists = conn.execute("SELECT 1 FROM favorites WHERE user_id=? AND db_key=? AND material_name=? AND lecture_name=?",
                              (user_id, db_key, material, lecture)).fetchone()
        if exists:
            conn.execute("DELETE FROM favorites WHERE user_id=? AND db_key=? AND material_name=? AND lecture_name=?",
                         (user_id, db_key, material, lecture))
            return False
        conn.execute("INSERT INTO favorites(user_id,db_key,material_name,lecture_name) VALUES(?,?,?,?)",
                     (user_id, db_key, material, lecture))
        return True

def get_favorites(user_id, limit=30):
    with db_connect() as conn:
        return conn.execute("SELECT db_key,material_name,lecture_name FROM favorites WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                            (user_id, limit)).fetchall()

def get_recent(user_id, limit=10):
    with db_connect() as conn:
        return conn.execute("SELECT db_key,material_name,lecture_name FROM recent_views WHERE user_id=? ORDER BY viewed_at DESC LIMIT ?",
                            (user_id, limit)).fetchall()

def get_stats():
    with db_connect() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now','-7 day')").fetchone()[0]
        downloads = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        favs = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        banned = conn.execute("SELECT COUNT(*) FROM banned_users").fetchone()[0]
        top = conn.execute("""SELECT material_name, lecture_name, COUNT(*) c FROM downloads
                            GROUP BY material_name, lecture_name ORDER BY c DESC LIMIT 5""").fetchall()
        return users, active, downloads, favs, banned, top

# ============================================================
# ADMIN BROADCAST (persistent job + safe pacing)
# ============================================================
def run_broadcast(job_id, admin_id, text, users):
    success = failed = 0
    for uid in users:
        try:
            bot.send_message(uid, text)
            success += 1
        except Exception as exc:
            failed += 1
            msg = str(exc).lower()
            if "blocked" in msg or "forbidden" in msg or "chat not found" in msg:
                mark_user_inactive(uid)
        # Conservative pacing; do not try to bypass Telegram limits.
        time.sleep(0.08)
        if success % 50 == 0 and success:
            with db_connect() as conn:
                conn.execute("UPDATE broadcast_jobs SET success=?, failed=? WHERE id=?", (success, failed, job_id))
    with db_connect() as conn:
        conn.execute("UPDATE broadcast_jobs SET status='completed',success=?,failed=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                     (success, failed, job_id))
    audit(admin_id, "broadcast_completed", job_id, f"success={success}, failed={failed}")
    try:
        bot.send_message(admin_id, f"✅ اكتملت الإذاعة\n• الإجمالي: {len(users)}\n• نجاح: {success}\n• فشل: {failed}")
    except Exception:
        pass

def start_broadcast(admin_id, text):
    users = get_all_users()
    with db_connect() as conn:
        cur = conn.execute("INSERT INTO broadcast_jobs(admin_id,content,status,total) VALUES(?,?,?,?)",
                           (admin_id, text, "sending", len(users)))
        job_id = cur.lastrowid
    audit(admin_id, "broadcast_started", job_id, f"total={len(users)}")
    threading.Thread(target=run_broadcast, args=(job_id, admin_id, text, users), daemon=True).start()
    return len(users)

# ============================================================
# BACKUP
# ============================================================
def send_backup(chat_id):
    if not os.path.exists(DB_PATH):
        bot.send_message(chat_id, "❌ قاعدة البيانات غير موجودة.")
        return
    try:
        with open(DB_PATH, "rb") as doc:
            bot.send_document(chat_id, doc, caption=f"📦 نسخة احتياطية\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        audit(chat_id, "manual_backup")
    except Exception:
        logger.exception("Backup failed")
        bot.send_message(chat_id, "❌ فشل إنشاء النسخة الاحتياطية.")

# ============================================================
# MEDIA HANDLER — FILE ID EXTRACTION, NO DB COURSE MODIFICATION
# ============================================================
@bot.message_handler(content_types=["document", "photo", "video", "audio", "voice"])
def handle_media(message):
    if not require_private(message):
        return
    uid = message.from_user.id
    if not is_admin(uid):
        return
    state = admin_states.get(uid, {})
    file_id = None
    file_type = None
    if message.document:
        file_id, file_type = message.document.file_id, "document"
    elif message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.video:
        file_id, file_type = message.video.file_id, "video"
    elif message.audio:
        file_id, file_type = message.audio.file_id, "audio"
    elif message.voice:
        file_id, file_type = message.voice.file_id, "voice"
    if not file_id:
        bot.reply_to(message, "❌ لم أستطع استخراج File ID.")
        return
    if state.get("action") == "exam_schedule":
        with db_connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('exam_schedule_file_id',?)", (file_id,))
        clear_admin_state(uid)
        audit(uid, "exam_schedule_updated", "", f"type={file_type}")
        bot.reply_to(message, "✅ تم حفظ ملف برنامج الامتحان.")
        return
    if state.get("action") == "get_file_id":
        clear_admin_state(uid)
        audit(uid, "file_id_extracted", "", f"type={file_type}")
        bot.reply_to(message, f"🔑 File ID:\n\n<code>{escape(file_id)}</code>\n\n📌 النوع: {file_type}\n\nانسخه وضعه بجانب اسم المحاضرة داخل <code>database</code> في الكود.", parse_mode="HTML")
        return
    # Safe default: any admin media gives the ID, but never edits course data automatically.
    bot.reply_to(message, f"🔑 File ID:\n\n<code>{escape(file_id)}</code>\n\n📌 النوع: {file_type}", parse_mode="HTML")

# ============================================================
# TEXT HANDLER
# ============================================================
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    if not require_private(message):
        return
    uid = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    first_name = (message.from_user.first_name or "المهندس")[:100]

    if is_user_banned(uid) and not is_admin(uid):
        bot.send_message(chat_id, "🚫 تم حظرك من استخدام هذا البوت.")
        return
    if not rate_allowed(uid) and not is_admin(uid):
        bot.send_message(chat_id, "⏳ طلبات كثيرة بسرعة. انتظر قليلًا ثم حاول مجددًا.")
        return

    add_user(uid, first_name)

    try:
        # ---------------- ADMIN ----------------
        if is_admin(uid):
            state = admin_states.get(uid, {})

            if text == "/admin":
                clear_admin_state(uid)
                bot.send_message(chat_id, "🔧 <b>لوحة تحكم الأدمن</b>", reply_markup=get_admin_keyboard(), parse_mode="HTML")
                return

            if text == "📊 إحصائيات البوت":
                users, active, downloads, favs, banned, top = get_stats()
                msg = [f"📊 <b>إحصائيات البوت</b>", f"👥 المستخدمون: <code>{users}</code>", f"🟢 نشطون خلال 7 أيام: <code>{active}</code>",
                       f"📥 التحميلات: <code>{downloads}</code>", f"⭐ المفضلة: <code>{favs}</code>", f"🚫 المحظورون: <code>{banned}</code>"]
                if top:
                    msg.append("\n🏆 <b>الأكثر طلبًا:</b>")
                    msg += [f"• {escape(m)} — {escape(l)}: {c}" for m,l,c in top]
                bot.send_message(chat_id, "\n".join(msg), parse_mode="HTML")
                return

            if text == "📎 استخراج File ID":
                set_admin_state(uid, action="get_file_id")
                bot.send_message(chat_id, "📎 أرسل الآن الملف/الصورة/الفيديو. سأعطيك File ID فقط ولن أغيّر الكود تلقائيًا.")
                return

            if text == "📸 برنامج الامتحان":
                set_admin_state(uid, action="exam_schedule")
                bot.send_message(chat_id, "📸 أرسل صورة/ملف برنامج الامتحان. سيُحفظ في قاعدة الإعدادات.")
                return

            if text == "📢 إرسال إذاعة":
                set_admin_state(uid, action="broadcast")
                bot.send_message(chat_id, "📢 أرسل نص الإذاعة الآن. ستعمل كـ Job مستقل مع تسجيل النجاح والفشل.\n\n❌ للإلغاء: اكتب /cancel")
                return

            if text == "📦 نسخة احتياطية":
                send_backup(chat_id); return

            if text == "🚫 حظر / إلغاء حظر مستخدم":
                set_admin_state(uid, action="ban")
                bot.send_message(chat_id, "أرسل User ID رقمي. إذا كان محظورًا سأفك الحظر، وإلا سأحظره.\n\n❌ /cancel")
                return

            if text == "📜 سجل الإدارة":
                with db_connect() as conn:
                    rows = conn.execute("SELECT action,target,created_at FROM audit_logs ORDER BY id DESC LIMIT 15").fetchall()
                if not rows:
                    bot.send_message(chat_id, "لا يوجد سجل بعد.")
                else:
                    bot.send_message(chat_id, "📜 <b>آخر عمليات الإدارة:</b>\n" + "\n".join(
                        f"• {escape(a)} | {escape(str(t))} | {escape(str(d))}" for a,t,d in rows), parse_mode="HTML")
                return

            if text == "🧾 تجهيز سطر ملف":
                set_admin_state(uid, action="code_line", step="year")
                bot.send_message(chat_id, "أرسل السنة: 1 أو 2 أو 3 أو 4 أو 5.\n❌ /cancel")
                return

            if text == "🗑️ تجهيز حذف من الكود":
                set_admin_state(uid, action="code_delete", step="year")
                bot.send_message(chat_id, "أرسل السنة: 1 أو 2 أو 3 أو 4 أو 5.\n❌ /cancel")
                return

            if text == "🏠 العودة للرئيسية":
                clear_admin_state(uid)
                reset_user_state(chat_id)
                bot.send_message(chat_id, "صلي على النبي ", reply_markup=get_years_keyboard())
                return

            # Persistent admin workflows.
            if state.get("action") in ("broadcast", "ban", "code_line", "code_delete"):
                if text == "/cancel":
                    clear_admin_state(uid); bot.send_message(chat_id, "✅ تم إلغاء العملية."); return

                if state["action"] == "broadcast":
                    clear_admin_state(uid)
                    total = start_broadcast(uid, text)
                    bot.send_message(chat_id, f"🚀 بدأت الإذاعة كعملية مستقلة. عدد المستلمين: <code>{total}</code>", parse_mode="HTML")
                    return

                if state["action"] == "ban":
                    clear_admin_state(uid)
                    try:
                        target = int(text)
                        if target == uid:
                            bot.send_message(chat_id, "❌ لا يمكنك حظر نفسك."); return
                        if is_user_banned(target):
                            unban_user(target); audit(uid,"unban_user",target); bot.send_message(chat_id, "✅ تم إلغاء الحظر.")
                        else:
                            ban_user(target); audit(uid,"ban_user",target); bot.send_message(chat_id, "🚫 تم الحظر.")
                    except ValueError:
                        bot.send_message(chat_id, "❌ User ID يجب أن يكون رقمًا.")
                    return

                action = state["action"]
                step = state.get("step")
                if step == "year":
                    if text not in {"1","2","3","4","5"}:
                        bot.send_message(chat_id, "❌ أرسل رقم السنة من 1 إلى 5."); return
                    set_admin_state(uid, action=action, step="sem", year=int(text))
                    bot.send_message(chat_id, "أرسل الفصل: 1 أو 2")
                    return
                if step == "sem":
                    if text not in {"1","2"}:
                        bot.send_message(chat_id, "❌ أرسل 1 أو 2."); return
                    set_admin_state(uid, action=action, step="material", year=state["year"], sem=int(text))
                    mats = catalog_materials(f"year_{state['year']}_sem_{int(text)}")
                    bot.send_message(chat_id, "اختر/اكتب اسم المادة حرفيًا:\n\n" + "\n".join(f"• {m['name']}" for m in mats))
                    return
                if step == "material":
                    db_key = f"year_{state['year']}_sem_{state['sem']}"
                    mat = find_material(db_key, text)
                    if not mat:
                        bot.send_message(chat_id, "❌ اسم المادة غير موجود. انسخه كما يظهر في القائمة."); return
                    set_admin_state(uid, action=action, step="lecture", year=state["year"], sem=state["sem"], material=text)
                    bot.send_message(chat_id, "أرسل اسم المحاضرة حرفيًا، مثل: محاضرة 1 📄")
                    return
                if step == "lecture":
                    db_key = f"year_{state['year']}_sem_{state['sem']}"
                    mat = find_material(db_key, state["material"])
                    if not mat or text not in mat.get("content", {}):
                        bot.send_message(chat_id, "❌ اسم المحاضرة غير موجود داخل المادة."); return
                    clear_admin_state(uid)
                    if action == "code_line":
                        current = mat["content"].get(text)
                        bot.send_message(chat_id, f"🧾 <b>السطر الجاهز:</b>\n\n<code>{escape(code_snippet(db_key, state['material'], text, current if current not in ('PASTE_FILE_ID_HERE','ضع_هنا_file_id') else None))}</code>\n\n📌 إذا أردت تحديثه: أرسل الملف أولًا عبر 📎 استخراج File ID ثم ضع الـ ID مكان القيمة.", parse_mode="HTML")
                        audit(uid,"code_line_generated",f"{db_key}/{state['material']}/{text}")
                    else:
                        snippet = code_snippet(db_key, state["material"], text, "PASTE_FILE_ID_HERE")
                        bot.send_message(chat_id, f"🗑️ <b>لحذف الملف من الكود:</b> استبدل السطر الحالي بهذا السطر، ثم احذف السطر بالكامل إذا أردت إزالة زر المحاضرة أيضًا:\n\n<code>{escape(snippet)}</code>\n\nثم ارفع التعديل إلى GitHub.", parse_mode="HTML")
                        audit(uid,"delete_snippet_generated",f"{db_key}/{state['material']}/{text}")
                    return

        # ---------------- COMMON / USER ----------------
        if text in ("/cancel",):
            with state_lock:
                user_states.pop(chat_id, None)
            bot.send_message(chat_id, "✅ تم الإلغاء.")
            return

        if text == "/start" or text == "ابدأ من جديد 🔄" or text == "🏠 العودة للرئيسية":
            reset_user_state(chat_id)

            # عند الضغط على Start: افحص الاشتراك أولاً.
            # إذا لم يكن مشتركاً، لا تظهر رسالة الترحيب ولا السنوات.
            if not check_sub(uid, force=True):
                bot.send_message(
                    chat_id,
                    f"⚠️ <b>يجب عليك الاشتراك في القناة أولاً لاستخدام البوت.</b>\n\n"
                    f"📢 القناة: {escape(CHANNEL_USERNAME)}\n\n"
                    f"👇 اشترك بالقناة ثم اضغط «تحقق من الاشتراك»." ,
                    reply_markup=get_sub_inline_markup(),
                    parse_mode="HTML"
                )
                return

            # إذا كان مشتركاً، تظهر رسالة الترحيب الكاملة وتظهر السنوات تحتها.
            welcome = (
                f"👋 <b>أهلاً بك يا {escape(first_name)}!</b>\n"
                f"🎓 مرحباً بك في بوت الاتصالات المساعد.\n"
                f"📚 يمكنك الوصول لجميع المحاضرات والمقررات بسهولة.\n\n"
                f"🎓 صنع هذا البوت @Y0USSEF_SABRA.\n"
                f"👇 <b>يرجى اختيار السنة الدراسية للبدء:</b>"
            )
            bot.send_message(
                chat_id,
                welcome,
                reply_markup=get_years_keyboard(),
                parse_mode="HTML"
            )
            return

        if not check_sub(uid):
            bot.send_message(chat_id, f"⚠️ يجب عليك الاشتراك في القناة أولاً لاستخدام البوت:\n{escape(CHANNEL_USERNAME)}",
                             reply_markup=get_sub_inline_markup(), parse_mode="HTML")
            return

        # Exam schedule
        if text == "📅 برنامج الامتحان":
            with db_connect() as conn:
                row = conn.execute("SELECT value FROM settings WHERE key='exam_schedule_file_id'").fetchone()
            if not row:
                bot.send_message(chat_id, "ℹ️ لم يتم رفع برنامج الامتحان بعد.")
            else:
                try:
                    bot.send_photo(chat_id, row[0], caption="📅 برنامج الامتحانات الرسمية:")
                except Exception:
                    bot.send_document(chat_id, row[0], caption="📅 برنامج الامتحانات الرسمية:")
            return

        # Feedback
        if text == "💬 أرسل ملاحظة/استفسار":
            if not feedback_allowed(uid):
                bot.send_message(chat_id, "⏳ وصلت للحد المسموح للملاحظات حاليًا. حاول لاحقًا.")
                return
            set_user_state(chat_id, feedback=True)
            bot.send_message(chat_id, "✍️ اكتب رسالتك الآن. /cancel للإلغاء.")
            return
        state = user_states.get(chat_id, {})
        if state.get("feedback"):
            if text == "/cancel":
                reset_user_state(chat_id); bot.send_message(chat_id,"✅ تم الإلغاء."); return
            reset_user_state(chat_id)
            with db_connect() as conn:
                cur = conn.execute("INSERT INTO feedback(user_id,first_name,message) VALUES(?,?,?)", (uid, first_name, text[:4000]))
                fid = cur.lastrowid
            safe = escape(text[:4000])
            bot.send_message(ADMIN_IDS and next(iter(ADMIN_IDS)),
                             f"📩 <b>ملاحظة #{fid}</b>\nمن: {escape(first_name)}\nID: <code>{uid}</code>\n\n{safe}\n\nللرد: استخدم Reply على هذه الرسالة ثم اكتب الرد.", parse_mode="HTML")
            bot.send_message(chat_id, "✅ تم إرسال ملاحظتك للإدارة. شكرًا لك!")
            return

        # Search
        if text == "🔎 بحث":
            set_user_state(chat_id, search=True)
            bot.send_message(chat_id, "🔎 اكتب اسم المادة أو المحاضرة للبحث. /cancel للإلغاء.")
            return
        if state.get("search"):
            if text == "/cancel":
                reset_user_state(chat_id); bot.send_message(chat_id,"✅ تم الإلغاء."); return
            reset_user_state(chat_id)
            results = catalog_search(text)
            if not results:
                bot.send_message(chat_id, "❌ لم أجد نتائج."); return
            lines = ["🔎 <b>نتائج البحث:</b>"]
            for db_key, mat, lec in results:
                lines.append(f"• <b>{escape(mat)}</b> — {escape(lec)}\n  <code>{db_key}</code>")
            bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
            return

        # Navigation
        years_map = {"السنة الأولى 1️⃣":1, "السنة الثانية 2️⃣":2, "السنة الثالثة 3️⃣":3, "السنة الرابعة 4️⃣":4, "السنة الخامسة 5️⃣":5}
        if text in years_map:
            set_user_state(chat_id, year=years_map[text], sem=None, material=None)
            bot.send_message(chat_id, "اختر الفصل الدراسي:", reply_markup=get_semesters_keyboard())
            return

        sems_map = {"الفصل الأول 📘":1, "الفصل الثاني 📙":2}
        if text in sems_map:
            year = state.get("year")
            if not year:
                bot.send_message(chat_id, "اختر السنة الدراسية أولاً.", reply_markup=get_years_keyboard()); return
            sem = sems_map[text]
            db_key = f"year_{year}_sem_{sem}"
            set_user_state(chat_id, year=year, sem=sem, material=None)
            bot.send_message(chat_id, f"إليك مواد السنة {year} - الفصل {sem}:", reply_markup=get_materials_keyboard(catalog_materials(db_key)))
            return

        if text == "🔙 رجوع للفصول":
            year = state.get("year")
            if year:
                set_user_state(chat_id, year=year, sem=None, material=None)
                bot.send_message(chat_id, "اختر الفصل الدراسي:", reply_markup=get_semesters_keyboard())
            else:
                bot.send_message(chat_id, "اختر السنة الدراسية:", reply_markup=get_years_keyboard())
            return

        if text == "🔙 رجوع للمواد":
            year, sem = state.get("year"), state.get("sem")
            if year and sem:
                set_user_state(chat_id, year=year, sem=sem, material=None)
                bot.send_message(chat_id, "اختر المادة:", reply_markup=get_materials_keyboard(catalog_materials(f"year_{year}_sem_{sem}")))
            else:
                bot.send_message(chat_id, "اختر السنة الدراسية:", reply_markup=get_years_keyboard())
            return

        year, sem = state.get("year"), state.get("sem")
        if year and sem:
            db_key = f"year_{year}_sem_{sem}"
            mat = find_material(db_key, text)
            if mat:
                set_user_state(chat_id, year=year, sem=sem, material=text)
                bot.send_message(chat_id, f"📚 اختر المحاضرة المتاحة لمادة {escape(text)}:",
                                 reply_markup=get_content_keyboard(mat.get("content", {}), db_key, text, uid), parse_mode="HTML")
                return

       

            material = state.get("material")
            if material:
                mat = find_material(db_key, material)
                if mat and text in mat.get("content", {}):
                    file_id = find_file_id(db_key, material, text)
                    if not file_id:
                        bot.send_message(chat_id, "⏳ هذه المحاضرة لم تُرفع بعد.")
                        return
                    try:
                        bot.send_chat_action(chat_id, "upload_document")
                        bot.send_document(chat_id, file_id, caption=f"📌 المادة: {material}\n📑 المحتوى: {text}\n\n")
                        record_download(uid, db_key, material, text)
                    except Exception:
                        logger.exception("Document send failed")
                        bot.send_message(chat_id, "✨ بالتوفيق والنجاح!")
                    return

    except Exception:
        logger.exception("Unhandled message error")
        try:
            bot.send_message(chat_id, "⚠️ حدث خطأ مؤقت. حاول مرة أخرى لاحقًا.")
        except Exception:
            pass

# ============================================================
# CALLBACKS — always authorize callback user
# ============================================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        uid = call.from_user.id
        if call.data == "check_subscription":
            ok = check_sub(uid, force=True)
            if ok:
                bot.answer_callback_query(call.id, "✅ تم التحقق من الاشتراك!")
                try: bot.delete_message(call.message.chat.id, call.message.message_id)
                except Exception: pass
                reset_user_state(call.message.chat.id)
                first_name = (call.from_user.first_name or "المهندس")[:100]
                welcome = (
                    f"👋 <b>أهلاً بك يا {escape(first_name)}!</b>\n"
                    f"🎓 مرحباً بك في بوت الاتصالات المساعد.\n"
                    f"📚 يمكنك الوصول لجميع المحاضرات والمقررات بسهولة.\n"
                    f"🎓 صنع هذا البوت @Y0USSEF_SABRA.\n\n"
                    f"👇 <b>يرجى اختيار السنة الدراسية للبدء:</b>"
                )
                bot.send_message(
                    call.message.chat.id,
                    welcome,
                    reply_markup=get_years_keyboard(),
                    parse_mode="HTML"
                )
            else:
                bot.answer_callback_query(call.id, "❌ لم يتم العثور على اشتراكك.", show_alert=True)
        else:
            # Future admin callbacks must pass this gate.
            if not is_admin(uid):
                bot.answer_callback_query(call.id, "❌ غير مصرح.", show_alert=True)
                return
            bot.answer_callback_query(call.id, "تم")
    except Exception:
        logger.exception("Callback error")

# ============================================================
# SAFE STARTUP
# ============================================================
if __name__ == "__main__":
    logger.info("Student bot started")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=10, allowed_updates=None)
    except Exception:
        logger.exception("Polling stopped unexpectedly")
        raise
