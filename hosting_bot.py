import os
import json
import sqlite3
import subprocess
import sys
import shutil
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
import threading
import secrets
import string
import re
import signal
import platform
import traceback
import hashlib
import base64
import tempfile
import random

# ========== CONFIGURATION ==========
# Get configuration from environment variables (SECURE)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    print("❌ ERROR: BOT_TOKEN environment variable not set!")
    print("Please set: export BOT_TOKEN='your_bot_token'")
    sys.exit(1)

# Get admin IDs from environment (comma-separated)
admin_ids_str = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]

# Channel verification settings (configure via env)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@gamerdroidbot2")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/gamerdroidbot2")

# Platform detection
IS_RENDER = os.environ.get("RENDER") == "true"
IS_HEROKU = os.environ.get("HEROKU") == "true"
IS_CHOREO = os.environ.get("CHOREO") == "true"
IS_ANDROID = 'pydroid' in sys.executable.lower() or 'termux' in sys.executable.lower()

# Set base directory based on platform
if IS_RENDER:
    BASE_DIR = Path("/opt/render/project/src/bot_hosting_data")
elif IS_HEROKU:
    BASE_DIR = Path("/app/bot_hosting_data")
elif IS_CHOREO:
    BASE_DIR = Path("/choreo/app/bot_hosting_data")
elif IS_ANDROID:
    BASE_DIR = Path("/storage/emulated/0/bot_hosting_data")
else:
    BASE_DIR = Path("./bot_hosting_data")

DEPLOYMENTS_DIR = BASE_DIR / "deployments"
DATABASE_FILE = BASE_DIR / "bot_database.db"
USER_FILES_DIR = BASE_DIR / "user_files"
LOGS_DIR = BASE_DIR / "logs"
PIP_CACHE_DIR = BASE_DIR / "pip_cache"

# Create directories
for dir_path in [BASE_DIR, DEPLOYMENTS_DIR, USER_FILES_DIR, LOGS_DIR, PIP_CACHE_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

# File size limit
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", 50))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Premium Subscription Pricing
PRICE_MONTHLY_STARS = int(os.environ.get("PRICE_MONTHLY_STARS", 50))
PRICE_YEARLY_STARS = int(os.environ.get("PRICE_YEARLY_STARS", 500))
PRICE_MONTHLY_COINS = PRICE_MONTHLY_STARS * 10
PRICE_YEARLY_COINS = PRICE_YEARLY_STARS * 10

# Free tier settings
FREE_USER_MAX_DEPLOYMENTS = int(os.environ.get("FREE_USER_MAX_DEPLOYMENTS", 3))
FREE_DEPLOYMENT_DURATION_HOURS = int(os.environ.get("FREE_DEPLOYMENT_DURATION_HOURS", 24))

# Exchange rate
STARS_PER_COIN = int(os.environ.get("STARS_PER_COIN", 10))

# Timeout settings
PIP_INSTALL_TIMEOUT = int(os.environ.get("PIP_INSTALL_TIMEOUT", 600))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
LAST_UPDATE_ID = 0

# Server status
server_running = True
active_deployments = {}
deployment_lock = threading.Lock()

# Webhook server
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")

print("=" * 70)
print("╔═════════════════════════════════════════════════════════════════════╗")
print("║         UNIVERSAL BOT HOSTING PLATFORM - ENTERPRISE EDITION         ║")
print("╚═════════════════════════════════════════════════════════════════════╝")
print("=" * 70)
print(f"📍 Platform: {'Render' if IS_RENDER else 'Heroku' if IS_HEROKU else 'Choreo' if IS_CHOREO else 'Android' if IS_ANDROID else 'Local'}")
print(f"📁 Data Directory: {BASE_DIR}")
print(f"💰 Monthly: {PRICE_MONTHLY_STARS}⭐ / {PRICE_MONTHLY_COINS}🪙")
print(f"💰 Yearly: {PRICE_YEARLY_STARS}⭐ / {PRICE_YEARLY_COINS}🪙")
print(f"🆓 Free Tier: {FREE_USER_MAX_DEPLOYMENTS} x {FREE_DEPLOYMENT_DURATION_HOURS}h")
print(f"📦 Max File Size: {MAX_FILE_SIZE_MB}MB")
print(f"🐍 Python Version: {platform.python_version()}")
print("=" * 70)
print("✨ ENHANCED FEATURES:")
print("   ✓ Supports ANY Python bot (Telegram, Discord, Flask, FastAPI, etc.)")
print("   ✓ ALL dependency types (PyPI, Git, Mercurial, Subversion, wheel, egg)")
print("   ✓ Auto-dependency detection from imports")
print("   ✓ Progress bars for installations")
print("   ✓ Environment variable injection with .env support")
print("   ✓ Premium/Free tier with Stars/Coins")
print("   ✓ Persistent storage with user file management")
print("   ✓ Framework detection and optimized launcher")
print("=" * 70)

# ========== DATABASE SETUP ==========
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        join_date TEXT,
        last_active TEXT,
        coins_balance INTEGER DEFAULT 0,
        stars_balance INTEGER DEFAULT 0,
        total_coins_earned INTEGER DEFAULT 0,
        total_coins_spent INTEGER DEFAULT 0,
        total_stars_earned INTEGER DEFAULT 0,
        total_stars_spent INTEGER DEFAULT 0,
        joined_channel INTEGER DEFAULT 0,
        free_deployment_count INTEGER DEFAULT 0,
        is_premium INTEGER DEFAULT 0,
        premium_expires TEXT,
        premium_plan TEXT,
        step TEXT,
        temp_file TEXT,
        requirements TEXT,
        env_vars TEXT,
        plan TEXT,
        payment_method TEXT,
        duration INTEGER,
        cost_coins INTEGER,
        cost_stars INTEGER,
        waiting_for_env INTEGER DEFAULT 0,
        waiting_for_reqs INTEGER DEFAULT 0,
        waiting_for_redeem INTEGER DEFAULT 0,
        temp_target_user TEXT,
        temp_coins_amount INTEGER,
        temp_stars_amount INTEGER,
        temp_expiry INTEGER,
        temp_reward_type TEXT,
        pending_payment_payload TEXT,
        last_expiry_notification TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan TEXT,
        amount_stars INTEGER,
        amount_coins INTEGER,
        start_date TEXT,
        end_date TEXT,
        status TEXT,
        renewal_count INTEGER DEFAULT 0,
        admin_notified INTEGER DEFAULT 0,
        payment_payload TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS deployments (
        deployment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        file_name TEXT,
        file_size INTEGER,
        requirements TEXT,
        env_vars TEXT,
        plan TEXT,
        payment_method TEXT,
        cost_coins INTEGER,
        cost_stars INTEGER,
        start_time TEXT,
        expire_time TEXT,
        status TEXT,
        proc_pid INTEGER,
        install_log TEXT,
        deploy_log TEXT,
        error_log TEXT,
        is_free INTEGER DEFAULT 0,
        is_paused INTEGER DEFAULT 0,
        last_expiry_notification TEXT,
        bot_type TEXT DEFAULT 'python_app',
        framework TEXT DEFAULT 'unknown',
        dependencies_installed TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS coin_transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        transaction_type TEXT,
        source TEXT,
        reference_id TEXT,
        timestamp TEXT,
        status TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS star_transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        transaction_type TEXT,
        source TEXT,
        reference_id TEXT,
        timestamp TEXT,
        status TEXT,
        payload TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
        code_id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        coins_amount INTEGER,
        stars_amount INTEGER,
        created_by INTEGER,
        created_at TEXT,
        expires_at TEXT,
        expiry_days INTEGER,
        max_uses INTEGER,
        used_count INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        used_by TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_premium_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        plan TEXT,
        duration_days INTEGER,
        cost_stars INTEGER,
        cost_coins INTEGER,
        payload TEXT,
        created_at TEXT,
        status TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        temp_file TEXT,
        requirements TEXT,
        env_vars TEXT,
        plan TEXT,
        duration INTEGER,
        cost_coins INTEGER,
        cost_stars INTEGER,
        payment_method TEXT,
        payload TEXT,
        created_at TEXT,
        status TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS system_stats (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        total_users INTEGER DEFAULT 0,
        total_deployments INTEGER DEFAULT 0,
        total_active_deployments INTEGER DEFAULT 0,
        total_paused_deployments INTEGER DEFAULT 0,
        total_free_deployments INTEGER DEFAULT 0,
        total_coins_created INTEGER DEFAULT 0,
        total_stars_created INTEGER DEFAULT 0,
        total_revenue_stars INTEGER DEFAULT 0,
        total_revenue_usd REAL DEFAULT 0,
        premium_users INTEGER DEFAULT 0,
        server_start_time TEXT,
        last_updated TEXT
    )''')
    
    c.execute('INSERT OR IGNORE INTO system_stats (id, server_start_time, last_updated) VALUES (1, ?, ?)', 
              (datetime.now().isoformat(), datetime.now().isoformat()))
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_deployments_user ON deployments(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deployments_expire ON deployments(expire_time)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_expire ON subscriptions(end_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_redeem_codes_code ON redeem_codes(code)')
    
    conn.commit()
    conn.close()
    print("✅ Database initialized with enhanced schema")

# ========== PROGRESS BAR FUNCTION ==========
def create_progress_bar(percentage: float, width: int = 30, filled_char: str = "█", empty_char: str = "░") -> str:
    filled = int(width * percentage / 100)
    empty = width - filled
    bar = filled_char * filled + empty_char * empty
    return f"[{bar}] {percentage:.1f}%"

# ========== HELPER FUNCTIONS ==========
def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_user_info(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return {'first_name': row[0] if row else 'User'}

def update_system_stats():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments")
        total_deployments = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments WHERE status='active'")
        active_deployments_count = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments WHERE status='paused'")
        paused_deployments = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments WHERE is_free=1")
        free_deployments = c.fetchone()[0] or 0
        c.execute("SELECT SUM(coins_amount) FROM redeem_codes")
        coins_created = c.fetchone()[0] or 0
        c.execute("SELECT SUM(stars_amount) FROM redeem_codes")
        stars_created = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM star_transactions WHERE transaction_type='subscription' AND status='completed'")
        revenue_stars = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM users WHERE is_premium=1")
        premium_users = c.fetchone()[0] or 0
        
        revenue_usd = revenue_stars * 0.01
        
        c.execute('''UPDATE system_stats SET 
            total_users=?, total_deployments=?, total_active_deployments=?,
            total_paused_deployments=?, total_free_deployments=?, total_coins_created=?,
            total_stars_created=?, total_revenue_stars=?, total_revenue_usd=?,
            premium_users=?, last_updated=?
            WHERE id=1''',
            (total_users, total_deployments, active_deployments_count, paused_deployments,
             free_deployments, coins_created, stars_created, revenue_stars, revenue_usd,
             premium_users, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Update system stats error: {e}")

def get_system_stats():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('SELECT * FROM system_stats WHERE id = 1')
        row = c.fetchone()
        conn.close()
        if row:
            return {
                'total_users': row[1], 'total_deployments': row[2],
                'active_deployments': row[3], 'paused_deployments': row[4],
                'free_deployments': row[5], 'coins_created': row[6],
                'stars_created': row[7], 'revenue_stars': row[8],
                'revenue_usd': row[9], 'premium_users': row[10],
                'server_start_time': row[11], 'last_updated': row[12]
            }
        return {}
    except Exception as e:
        print(f"❌ Get system stats error: {e}")
        return {}

def get_user_balances(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT coins_balance, stars_balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return {'coins': row[0] if row else 0, 'stars': row[1] if row else 0}

def update_user_coins(user_id, delta, transaction_type="balance_update", source="system"):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    c.execute("UPDATE users SET coins_balance = coins_balance + ? WHERE user_id = ?", (delta, user_id))
    if delta > 0:
        c.execute("UPDATE users SET total_coins_earned = total_coins_earned + ? WHERE user_id = ?", (delta, user_id))
    else:
        c.execute("UPDATE users SET total_coins_spent = total_coins_spent + ? WHERE user_id = ?", (abs(delta), user_id))
    conn.commit()
    conn.close()
    
    try:
        conn2 = sqlite3.connect(DATABASE_FILE)
        c2 = conn2.cursor()
        c2.execute('''INSERT INTO coin_transactions 
            (user_id, amount, transaction_type, source, reference_id, timestamp, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, delta, transaction_type, source, None, datetime.now().isoformat(), 'completed'))
        conn2.commit()
        conn2.close()
    except Exception as e:
        print(f"❌ Record coin transaction error: {e}")
    
    update_system_stats()
    return True

def update_user_stars(user_id, delta, transaction_type="balance_update", source="system", payload=None):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    c.execute("UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?", (delta, user_id))
    if delta > 0:
        c.execute("UPDATE users SET total_stars_earned = total_stars_earned + ? WHERE user_id = ?", (delta, user_id))
    else:
        c.execute("UPDATE users SET total_stars_spent = total_stars_spent + ? WHERE user_id = ?", (abs(delta), user_id))
    conn.commit()
    conn.close()
    
    try:
        conn2 = sqlite3.connect(DATABASE_FILE)
        c2 = conn2.cursor()
        c2.execute('''INSERT INTO star_transactions 
            (user_id, amount, transaction_type, source, reference_id, timestamp, status, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, delta, transaction_type, source, None, datetime.now().isoformat(), 'completed', payload))
        conn2.commit()
        conn2.close()
    except Exception as e:
        print(f"❌ Record star transaction error: {e}")
    
    update_system_stats()
    return True

def is_user_premium(user_id):
    if is_admin(user_id):
        return True
    
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''SELECT is_premium, premium_expires FROM users WHERE user_id = ?''', (user_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0] == 1 and row[1]:
            expires = datetime.fromisoformat(row[1])
            if expires > datetime.now():
                return True
        return False
    except Exception:
        return False

def get_free_deployment_used_count(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM deployments WHERE user_id = ? AND is_free = 1 AND status = 'active'", (user_id,))
    count = c.fetchone()[0] or 0
    conn.close()
    return count

def can_use_free_deployment(user_id):
    if is_admin(user_id):
        return True, "Admin - Unlimited"
    
    if is_user_premium(user_id):
        return True, "Premium - Unlimited"
    
    used_count = get_free_deployment_used_count(user_id)
    remaining = FREE_USER_MAX_DEPLOYMENTS - used_count
    
    if remaining > 0:
        return True, f"Free tier - {remaining} remaining (used {used_count}/{FREE_USER_MAX_DEPLOYMENTS})"
    else:
        return False, f"Free tier limit reached ({used_count}/{FREE_USER_MAX_DEPLOYMENTS})"

def activate_premium(user_id, plan, amount_stars, amount_coins, duration_days):
    try:
        end_date = datetime.now() + timedelta(days=duration_days)
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute('''UPDATE users SET is_premium = 1, premium_expires = ?, premium_plan = ? WHERE user_id = ?''',
                  (end_date.isoformat(), plan, user_id))
        
        c.execute('''INSERT INTO subscriptions (user_id, plan, amount_stars, amount_coins, start_date, end_date, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, plan, amount_stars, amount_coins, datetime.now().isoformat(), end_date.isoformat(), 'active'))
        
        conn.commit()
        
        resumed_count = resume_paused_deployments(user_id)
        
        conn.close()
        update_system_stats()
        
        user_info = get_user_info(user_id)
        admin_msg = f"🎉 NEW PREMIUM!\nUser: {user_info.get('first_name', 'Unknown')} ({user_id})\nPlan: {plan.upper()}\nAmount: {amount_stars}⭐\nResumed: {resumed_count}"
        notify_admin(admin_msg)
        
        return True, resumed_count
    except Exception as e:
        print(f"❌ Activate premium error: {e}")
        return False, 0

def resume_paused_deployments(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute('''SELECT deployment_id, file_name, env_vars
                     FROM deployments WHERE user_id = ? AND status = 'paused' AND is_paused = 1''', (user_id,))
        paused = c.fetchall()
        
        resumed_count = 0
        for dep_id, file_name, env_vars_json in paused:
            deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(dep_id)
            dest_script = deploy_folder / file_name
            
            if dest_script.exists():
                env_vars = json.loads(env_vars_json) if env_vars_json else {}
                env = os.environ.copy()
                for k, v in env_vars.items():
                    env[k] = v
                
                launcher_script = deploy_folder / "run.py"
                if launcher_script.exists():
                    with open(deploy_folder / "output.log", "a") as log_file:
                        proc = subprocess.Popen(
                            [sys.executable, str(launcher_script)],
                            cwd=str(deploy_folder),
                            env=env,
                            stdout=log_file,
                            stderr=subprocess.STDOUT
                        )
                else:
                    with open(deploy_folder / "output.log", "a") as log_file:
                        proc = subprocess.Popen(
                            [sys.executable, str(dest_script)],
                            cwd=str(deploy_folder),
                            env=env,
                            stdout=log_file,
                            stderr=subprocess.STDOUT
                        )
                
                c.execute('''UPDATE deployments SET status = 'active', is_paused = 0, proc_pid = ? 
                             WHERE deployment_id = ?''', (proc.pid, dep_id))
                resumed_count += 1
                
                with deployment_lock:
                    active_deployments[dep_id] = proc
        
        conn.commit()
        conn.close()
        return resumed_count
    except Exception as e:
        print(f"❌ Resume paused deployments error: {e}")
        return 0

def notify_admin(message):
    for admin_id in ADMIN_IDS:
        try:
            send_message(admin_id, message)
        except Exception as e:
            print(f"Failed to notify admin {admin_id}: {e}")

def send_message(chat_id, text, keyboard=None, parse_mode="Markdown"):
    url = f"{TELEGRAM_API}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    
    try:
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Send error: {e}")
        return None

def edit_message(chat_id, message_id, text, keyboard=None):
    url = f"{TELEGRAM_API}/editMessageText"
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    
    try:
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Edit error: {e}")
        return None

def answer_callback(callback_id, text=None, show_alert=False):
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    if show_alert:
        data["show_alert"] = True
    try:
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Answer callback error: {e}")

def http_get(url, params=None):
    try:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"HTTP error: {e}")
        return None

def format_file_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    
    return f"{size_bytes:.1f} {size_names[i]}"

def format_uptime(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

# ========== CHANNEL VERIFICATION ==========
def check_channel_membership(user_id):
    try:
        url = f"{TELEGRAM_API}/getChatMember"
        data = {"chat_id": REQUIRED_CHANNEL, "user_id": user_id}
        data_bytes = urllib.parse.urlencode(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, method='POST')
        
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('ok'):
                status = result['result']['status']
                return status in ['member', 'administrator', 'creator', 'restricted']
    except Exception as e:
        print(f"❌ Channel check error: {e}")
    return False

def mark_channel_joined(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('UPDATE users SET joined_channel = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ Error marking channel: {e}")
        return False

def is_user_verified(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('SELECT joined_channel FROM users WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        conn.close()
        return result and result[0] == 1
    except Exception:
        return False

def send_verification_required(chat_id, user_id, first_name, message_id=None):
    text = f"**🔐 CHANNEL VERIFICATION REQUIRED**\n\n━━━━━━━━━━━━━━━━━━━━━━\n👋 Welcome {first_name}!\n\n⚠️ **You must join our official channel to use this bot!**\n━━━━━━━━━━━━━━━━━━━━━━\n\n📢 **Channel:** {REQUIRED_CHANNEL}\n\n━━━━━━━━━━━━━━━━━━━━━━\n**How to verify:**\n1️⃣ Click the JOIN CHANNEL button below\n2️⃣ Join {REQUIRED_CHANNEL}\n3️⃣ Come back and click VERIFY JOIN"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "📢 JOIN CHANNEL", "url": CHANNEL_LINK},
             {"text": "✅ VERIFY JOIN", "callback_data": "verify_channel"}]
        ]
    }
    
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

# ========== REDEEM CODES ==========
def generate_redeem_code(length=12):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def create_redeem_code(admin_id, coins_amount=0, stars_amount=0, expiry_days=30, max_uses=1):
    code = generate_redeem_code()
    expires_at = datetime.now() + timedelta(days=expiry_days)
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO redeem_codes 
        (code, coins_amount, stars_amount, created_by, created_at, expires_at, expiry_days, max_uses, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (code, coins_amount, stars_amount, admin_id, datetime.now().isoformat(), 
         expires_at.isoformat(), expiry_days, max_uses, 1))
    conn.commit()
    conn.close()
    update_system_stats()
    return code

def redeem_code(user_id, code):
    if not is_user_verified(user_id):
        return False, f"❌ You must join {REQUIRED_CHANNEL} first!"
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT code_id, coins_amount, stars_amount, max_uses, used_count, expires_at, is_active 
                 FROM redeem_codes WHERE code = ?''', (code,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return False, "❌ Invalid redeem code!"
    
    code_id, coins_amount, stars_amount, max_uses, used_count, expires_at, is_active = row
    
    if datetime.fromisoformat(expires_at) < datetime.now():
        conn.close()
        return False, "❌ This redeem code has expired!"
    
    if not is_active:
        conn.close()
        return False, "❌ This redeem code is no longer active!"
    
    if max_uses > 0 and used_count >= max_uses:
        conn.close()
        return False, f"❌ This redeem code has reached its usage limit ({max_uses}/{max_uses})!"
    
    used_by = c.execute("SELECT used_by FROM redeem_codes WHERE code = ?", (code,)).fetchone()[0]
    if used_by and str(user_id) in used_by.split(','):
        conn.close()
        return False, "❌ You have already used this redeem code!"
    
    reward_msg = []
    if coins_amount > 0:
        update_user_coins(user_id, coins_amount, "redeem", f"code_{code}")
        reward_msg.append(f"{coins_amount} 🪙")
    if stars_amount > 0:
        update_user_stars(user_id, stars_amount, "redeem", f"code_{code}")
        reward_msg.append(f"{stars_amount} ⭐")
    
    new_used_count = used_count + 1
    new_used_by = f"{used_by},{user_id}" if used_by else str(user_id)
    c.execute('''UPDATE redeem_codes SET used_count = ?, used_by = ? WHERE code_id = ?''',
              (new_used_count, new_used_by, code_id))
    
    conn.commit()
    conn.close()
    update_system_stats()
    return True, f"✅ Redeemed: {', '.join(reward_msg)}!"

# ========== PREMIUM SUBSCRIPTION ==========
def create_premium_invoice(chat_id, user_id, plan, duration_days, cost_stars):
    try:
        payload = f"premium_{plan}_{user_id}_{int(datetime.now().timestamp())}"
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO pending_premium_purchases 
            (user_id, chat_id, plan, duration_days, cost_stars, cost_coins, payload, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, chat_id, plan, duration_days, cost_stars, 
             cost_stars * STARS_PER_COIN, payload, datetime.now().isoformat(), 'pending'))
        conn.commit()
        conn.close()
        
        title = f"Premium {plan.capitalize()} Subscription"
        description = f"Get premium status for {duration_days} days"
        
        url = f"{TELEGRAM_API}/sendInvoice"
        prices = [{"label": title, "amount": cost_stars}]
        
        invoice_data = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "currency": "XTR",
            "prices": prices,
            "need_name": False,
            "need_phone_number": False,
            "need_email": False,
            "need_shipping_address": False,
            "is_flexible": False
        }
        
        data_bytes = json.dumps(invoice_data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('ok'):
                return True, None
            else:
                return False, result.get('description', 'Unknown error')
    except Exception as e:
        return False, str(e)

def show_premium_menu(chat_id, user_id, message_id=None):
    is_premium = is_user_premium(user_id)
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM deployments WHERE user_id = ? AND status = 'paused'", (user_id,))
    paused_count = c.fetchone()[0] or 0
    conn.close()
    
    if is_premium:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT premium_expires, premium_plan FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        
        expires = datetime.fromisoformat(row[0]) if row and row[0] else None
        plan = row[1] if row else "monthly"
        
        if expires:
            days_left = (expires - datetime.now()).days
            text = (
                f"**⭐ PREMIUM MEMBER**\n\n"
                f"✅ Plan: `{plan.upper()}`\n"
                f"📅 Expires: `{expires.strftime('%Y-%m-%d')}`\n"
                f"⏰ Days left: `{days_left}`\n"
                f"⏸️ Paused: `{paused_count}`\n\n"
                f"✨ Benefits active!\n\n"
                f"💰 Monthly/Yearly deployments are **FREE** for you!"
            )
            keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]]}
        else:
            text = "❌ Premium expired. Renew now!"
            keyboard = {"inline_keyboard": [[{"text": "💰 Renew Premium", "callback_data": "subscribe_premium"}]]}
    else:
        text = (
            f"**⭐ PREMIUM SUBSCRIPTION**\n\n"
            f"✨ **Benefits:**\n"
            f"• ✅ Unlimited free deployments (24h)\n"
            f"• ✅ FREE Monthly/Yearly deployments\n"
            f"• ✅ Priority support\n"
            f"• ✅ Auto-resume paused bots\n\n"
            f"⏸️ Paused: `{paused_count}`\n\n"
            f"💰 **Pricing:**\n"
            f"📅 Monthly: `{PRICE_MONTHLY_STARS}⭐` / `{PRICE_MONTHLY_COINS}🪙`\n"
            f"🌟 Yearly: `{PRICE_YEARLY_STARS}⭐` / `{PRICE_YEARLY_COINS}🪙`\n\n"
            f"Choose payment method:"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": f"⭐ Monthly ({PRICE_MONTHLY_STARS}⭐)", "callback_data": "premium_monthly_stars"},
                 {"text": f"🪙 Monthly ({PRICE_MONTHLY_COINS}🪙)", "callback_data": "premium_monthly_coins"}],
                [{"text": f"⭐ Yearly ({PRICE_YEARLY_STARS}⭐)", "callback_data": "premium_yearly_stars"},
                 {"text": f"🪙 Yearly ({PRICE_YEARLY_COINS}🪙)", "callback_data": "premium_yearly_coins"}],
                [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
            ]
        }
    
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def purchase_premium_stars(chat_id, user_id, plan, duration_days, cost_stars):
    success, error = create_premium_invoice(chat_id, user_id, plan, duration_days, cost_stars)
    if success:
        send_message(chat_id, f"⭐ **Stars Payment Required**\n\nPlan: {plan.upper()}\nCost: {cost_stars}⭐\n\nPlease complete the payment using the invoice above.\n\nYour premium will activate automatically after payment.")
    else:
        send_message(chat_id, f"❌ Failed to create invoice: {error}")

def purchase_premium_coins(chat_id, user_id, plan, duration_days, cost_coins):
    balances = get_user_balances(user_id)
    
    if balances['coins'] >= cost_coins:
        update_user_coins(user_id, -cost_coins, "premium_subscription", f"plan_{plan}")
        success, resumed = activate_premium(user_id, plan, cost_coins // STARS_PER_COIN, cost_coins, duration_days)
        
        if success:
            resume_msg = f"\n\n✅ Resumed {resumed} paused deployment(s)!" if resumed > 0 else ""
            send_message(chat_id,
                f"✅ **PREMIUM ACTIVATED!**{resume_msg}\n\n"
                f"Plan: `{plan.upper()}`\n"
                f"Duration: `{duration_days}` days\n"
                f"Paid: `{cost_coins}🪙`\n\n"
                f"Thank you! 🙏",
                {"inline_keyboard": [[{"text": "🏠 Main Menu", "callback_data": "main_menu"}]]})
        else:
            send_message(chat_id, "❌ Failed to activate premium.")
    else:
        send_message(chat_id,
            f"❌ **INSUFFICIENT COINS**\n\nRequired: `{cost_coins}🪙`\nYour balance: `{balances['coins']}🪙`\n\nUse redeem codes or pay with Stars!",
            {"inline_keyboard": [[{"text": "⭐ Pay with Stars", "callback_data": f"premium_{plan}_stars"},
                                  {"text": "🎫 Redeem Code", "callback_data": "redeem_code"}]]})

# ========== PERMANENT USER FILE STORAGE ==========
def save_user_file(user_id, temp_file_path, original_filename):
    user_dir = USER_FILES_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved_filename = f"{timestamp}_{original_filename}"
    saved_path = user_dir / saved_filename
    
    shutil.copy2(temp_file_path, saved_path)
    
    latest_path = user_dir / "latest.py"
    shutil.copy2(temp_file_path, latest_path)
    
    return saved_path, saved_filename

def get_user_files(user_id):
    user_dir = USER_FILES_DIR / str(user_id)
    if not user_dir.exists():
        return []
    
    files = []
    for file_path in user_dir.glob("*.py"):
        stat = file_path.stat()
        files.append({
            'filename': file_path.name,
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime),
            'path': str(file_path)
        })
    
    files.sort(key=lambda x: x['modified'], reverse=True)
    return files

def delete_user_file(user_id, filename):
    user_dir = USER_FILES_DIR / str(user_id)
    file_path = user_dir / filename
    if file_path.exists():
        file_path.unlink()
        return True
    return False

# ========== UNIVERSAL DEPENDENCY INSTALLER ==========
class UniversalDependencyInstaller:
    """Supports ALL Python dependency types: PyPI, Git, Mercurial, Subversion, local, wheel, egg, etc."""
    
    DEPENDENCY_PATTERNS = {
        r'^([a-zA-Z0-9_\-\.]+)$': 'pypi',
        r'^([a-zA-Z0-9_\-\.]+)[=<>~!]+': 'pypi',
        r'^git\+https?://': 'git',
        r'^git\+ssh://': 'git',
        r'^git@': 'git',
        r'\.git(@|#|$|/| )': 'git',
        r'^hg\+https?://': 'mercurial',
        r'^hg\+ssh://': 'mercurial',
        r'^svn\+https?://': 'subversion',
        r'^svn\+ssh://': 'subversion',
        r'^\./': 'local',
        r'^\.\./': 'local',
        r'^/': 'local',
        r'^[A-Za-z]:\\': 'local',
        r'\.whl$': 'wheel',
        r'\.egg$': 'egg',
        r'\.(tar\.gz|tgz|tar\.bz2|zip)$': 'archive',
    }
    
    @classmethod
    def detect_dependency_type(cls, dependency: str) -> str:
        for pattern, dep_type in cls.DEPENDENCY_PATTERNS.items():
            if re.search(pattern, dependency, re.IGNORECASE):
                return dep_type
        return 'pypi'
    
    @classmethod
    def install_dependency(cls, dependency: str, update_logs: callable, retries: int = 3) -> tuple:
        dep_type = cls.detect_dependency_type(dependency)
        update_logs(f"   📦 Type: {dep_type.upper()} - {dependency[:60]}...")
        
        for attempt in range(retries):
            try:
                if dep_type == 'git':
                    return cls._install_git_dependency(dependency, update_logs)
                elif dep_type in ['mercurial', 'subversion']:
                    return cls._install_vcs_dependency(dependency, update_logs)
                elif dep_type in ['wheel', 'egg', 'archive', 'local']:
                    return cls._install_file_dependency(dependency, update_logs)
                else:
                    return cls._install_pypi_dependency(dependency, update_logs, attempt)
            except subprocess.TimeoutExpired:
                update_logs(f"   ⏰ Attempt {attempt + 1} timeout, retrying...")
                sleep(5)
            except Exception as e:
                update_logs(f"   ⚠️ Attempt {attempt + 1} failed: {str(e)[:80]}")
                if attempt < retries - 1:
                    sleep(3)
                else:
                    return False, str(e)
        return False, "Max retries exceeded"
    
    @classmethod
    def _install_pypi_dependency(cls, dependency: str, update_logs: callable, attempt: int) -> tuple:
        match = re.match(r'^([a-zA-Z0-9_\-\.]+)([=<>~!].*)?$', dependency)
        if match:
            package = match.group(1)
            version_spec = match.group(2) or ""
            full_package = package + version_spec
        else:
            full_package = dependency
        
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--cache-dir", str(PIP_CACHE_DIR), 
               "--timeout", str(PIP_INSTALL_TIMEOUT), full_package]
        
        if any(x in dependency.lower() for x in ['a', 'b', 'rc', 'dev', 'pre']):
            cmd.insert(4, "--pre")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        
        if result.returncode == 0:
            version_match = re.search(r'Successfully installed .*? ([\d\.]+)', result.stdout)
            version = version_match.group(1) if version_match else "unknown"
            return True, version
        else:
            error = result.stderr[:200] if result.stderr else "Unknown error"
            return False, error
    
    @classmethod
    def _install_git_dependency(cls, dependency: str, update_logs: callable) -> tuple:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", dependency]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        return result.returncode == 0, result.stderr[:200] if result.stderr else ""
    
    @classmethod
    def _install_vcs_dependency(cls, dependency: str, update_logs: callable) -> tuple:
        cmd = [sys.executable, "-m", "pip", "install", dependency]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        return result.returncode == 0, result.stderr[:200] if result.stderr else ""
    
    @classmethod
    def _install_file_dependency(cls, dependency: str, update_logs: callable) -> tuple:
        cmd = [sys.executable, "-m", "pip", "install", dependency]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        return result.returncode == 0, result.stderr[:200] if result.stderr else ""

# ========== AUTO-DEPENDENCY DETECTION ==========
class AutoDependencyDetector:
    """Automatically detect required dependencies by scanning code"""
    
    IMPORT_MAPPING = {
        'flask': 'flask', 'django': 'django', 'fastapi': 'fastapi',
        'aiohttp': 'aiohttp', 'tornado': 'tornado', 'sanic': 'sanic',
        'telegram': 'python-telegram-bot', 'aiogram': 'aiogram',
        'pyrogram': 'pyrogram', 'telethon': 'telethon',
        'discord': 'discord.py', 'nextcord': 'nextcord',
        'sqlalchemy': 'sqlalchemy', 'psycopg2': 'psycopg2-binary',
        'pymysql': 'pymysql', 'pymongo': 'pymongo', 'redis': 'redis',
        'requests': 'requests', 'httpx': 'httpx',
        'numpy': 'numpy', 'pandas': 'pandas', 'scipy': 'scipy',
        'PIL': 'Pillow', 'cv2': 'opencv-python',
        'bs4': 'beautifulsoup4', 'selenium': 'selenium',
        'dotenv': 'python-dotenv', 'click': 'click',
        'cryptography': 'cryptography', 'jwt': 'pyjwt',
        'yaml': 'pyyaml', 'toml': 'toml',
        'boto3': 'boto3', 'psutil': 'psutil', 'loguru': 'loguru',
        'rich': 'rich', 'tqdm': 'tqdm', 'uvicorn': 'uvicorn',
        'gunicorn': 'gunicorn', 'celery': 'celery',
    }
    
    @classmethod
    def scan_imports(cls, code_content: str, update_logs: callable) -> list:
        detected = set()
        lines = code_content.split('\n')
        
        for line in lines:
            line = line.strip()
            match = re.match(r'^(?:from|import)\s+([a-zA-Z0-9_\.]+)', line)
            if match:
                module = match.group(1).split('.')[0]
                if module in cls.IMPORT_MAPPING:
                    package = cls.IMPORT_MAPPING[module]
                    if package and package not in detected:
                        detected.add(package)
                        update_logs(f"   🔍 Detected: {module} → {package}")
                elif module not in ['os', 'sys', 'time', 'datetime', 'json', 're', 'math', 'random', 
                                     'string', 'collections', 'itertools', 'functools', 'typing', 'pathlib',
                                     'tempfile', 'subprocess', 'threading', 'multiprocessing', 'socket',
                                     'ssl', 'hashlib', 'base64', 'zipfile', 'tarfile', 'shutil', 'glob']:
                    guessed = module.replace('_', '-')
                    if module == 'bs4':
                        guessed = 'beautifulsoup4'
                    elif module == 'cv2':
                        guessed = 'opencv-python'
                    detected.add(guessed)
                    update_logs(f"   🔍 Detected: {module} → {guessed}")
        
        return list(detected)
    
    @classmethod
    def scan_requirements_file(cls, content: str, update_logs: callable) -> list:
        requirements = []
        for line in content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('-r'):
                if ';' in line:
                    line = line.split(';')[0].strip()
                requirements.append(line)
                update_logs(f"   📄 From requirements: {line[:60]}")
        return requirements

# ========== ENHANCED DEPENDENCY INSTALLATION ==========
def install_dependencies_enhanced(reqs_file, update_logs):
    if not reqs_file.exists():
        update_logs("✅ No requirements file found")
        return True, []
    
    try:
        with open(reqs_file, 'r') as f:
            requirements = f.read().strip()
        
        if not requirements:
            update_logs("⚠️ requirements.txt is empty")
            return True, []
        
        packages = [p.strip() for p in requirements.split('\n') if p.strip() and not p.startswith('#')]
        update_logs(f"📦 Found {len(packages)} package(s) to install")
        
        # Upgrade pip with progress
        update_logs("🔄 Upgrading pip...")
        update_logs(create_progress_bar(0, 25))
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], 
                      capture_output=True, timeout=60)
        update_logs(create_progress_bar(100, 25))
        update_logs("✅ Pip upgraded")
        
        # Install packages with progress tracking
        success_count = 0
        failed_packages = []
        
        for i, package in enumerate(packages):
            progress = (i / len(packages)) * 100
            update_logs(create_progress_bar(progress, 25))
            update_logs(f"   Installing: {package[:50]}...")
            
            success, message = UniversalDependencyInstaller.install_dependency(package, update_logs)
            
            if success:
                success_count += 1
                update_logs(f"   ✅ {package[:50]} installed")
            else:
                failed_packages.append(package)
                update_logs(f"   ❌ {package[:50]} failed: {message[:80]}")
        
        update_logs(create_progress_bar(100, 25))
        
        if failed_packages:
            update_logs(f"⚠️ Failed: {len(failed_packages)} package(s)")
            for pkg in failed_packages[:5]:
                update_logs(f"   • {pkg[:50]}")
        
        update_logs(f"✅ Installed {success_count}/{len(packages)} packages")
        
        success_rate = success_count / len(packages) if packages else 1.0
        return success_rate >= 0.8, failed_packages
        
    except Exception as e:
        update_logs(f"❌ Installation error: {str(e)}")
        return False, []

# ========== FRAMEWORK DETECTION ==========
def detect_bot_framework(code_content: str) -> list:
    frameworks = []
    
    if 'telegram' in code_content.lower() or 'telegram.ext' in code_content:
        frameworks.append('telegram')
    if 'discord' in code_content.lower():
        frameworks.append('discord')
    if 'flask' in code_content.lower():
        frameworks.append('flask')
    if 'fastapi' in code_content.lower():
        frameworks.append('fastapi')
    if 'django' in code_content.lower():
        frameworks.append('django')
    if 'aiohttp' in code_content.lower():
        frameworks.append('aiohttp')
    if 'aiogram' in code_content.lower():
        frameworks.append('aiogram')
    
    return frameworks if frameworks else ['generic']

# ========== ENHANCED LAUNCHER SCRIPT ==========
def create_enhanced_launcher_script(deploy_folder, dest_script, env_vars_dict, code_content, update_logs):
    frameworks = detect_bot_framework(code_content)
    framework_str = ', '.join(frameworks)
    update_logs(f"🔧 Framework detection: {framework_str}")
    
    launcher_script = deploy_folder / "run.py"
    
    env_set_code = []
    for k, v in env_vars_dict.items():
        escaped_v = v.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")
        env_set_code.append(f'os.environ["{k}"] = "{escaped_v}"')
    
    env_set_str = "\n".join(env_set_code) if env_set_code else "# No custom environment variables"
    
    with open(launcher_script, 'w', encoding='utf-8') as f:
        f.write(f'''#!/usr/bin/env python3
"""
UNIVERSAL BOT LAUNCHER - Auto-generated by Hosting Platform
Detected frameworks: {framework_str}
"""

import os
import sys
import time
import json
import signal
import threading
import traceback
from pathlib import Path

# Change to deployment directory
os.chdir(r"{deploy_folder}")

# ========== SET ENVIRONMENT VARIABLES ==========
{env_set_str}

# Try to load .env file
try:
    from dotenv import load_dotenv
    env_file = Path(r"{deploy_folder}") / ".env"
    if env_file.exists():
        load_dotenv(env_file)
        print("✅ Loaded .env file")
except ImportError:
    pass
except Exception as e:
    print(f"⚠️ Could not load .env: {{e}}")

# ========== HEARTBEAT THREAD ==========
heartbeat_running = True

def heartbeat():
    heartbeat_file = Path(r"{deploy_folder}") / ".heartbeat"
    while heartbeat_running:
        try:
            with open(heartbeat_file, 'w') as f:
                f.write(str(time.time()))
            time.sleep(30)
        except:
            pass

heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
heartbeat_thread.start()

# ========== VERIFY ENVIRONMENT ==========
print("=" * 50)
print("🔧 ENVIRONMENT VERIFICATION")
print("=" * 50)
print(f"Python: {{sys.version}}")
print(f"Working directory: {{os.getcwd()}}")

print("\\n📊 Environment Variables:")
env_count = 0
for key, value in os.environ.items():
    if key not in ['PATH', 'PYTHONPATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM', 'LANG', 'LC_ALL']:
        if any(s in key.upper() for s in ['TOKEN', 'SECRET', 'KEY', 'PASSWORD']):
            print(f"  ✅ {{key}} = {{value[:10]}}...")
        else:
            print(f"  ✅ {{key}} = {{value[:40]}}...")
        env_count += 1
print(f"\\n📊 Total custom env vars: {{env_count}}")

# Check for common bot tokens
for token_name in ['BOT_TOKEN', 'TOKEN', 'API_TOKEN', 'DISCORD_TOKEN']:
    if os.environ.get(token_name):
        print(f"✅ {{token_name}} found!")
        break

print("=" * 50)
print("🚀 STARTING BOT")
print("=" * 50)

# ========== METHOD 1: Import and run ==========
try:
    print("📌 Method 1: Importing as module...")
    sys.path.insert(0, r"{deploy_folder}")
    
    import importlib.util
    spec = importlib.util.spec_from_file_location("user_bot", r"{dest_script}")
    if spec is None:
        raise ImportError(f"Cannot load module")
    
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    # Try different entry points
    entry_points = ['main', 'run', 'start', 'setup', 'app', 'application', 'bot', 'client']
    
    for entry in entry_points:
        if hasattr(module, entry):
            attr = getattr(module, entry)
            if callable(attr):
                print(f"✅ Found {{entry}}(), calling...")
                try:
                    result = attr()
                    if result and hasattr(result, 'run_polling'):
                        result.run_polling()
                    elif result and hasattr(result, 'run'):
                        result.run()
                    elif result and hasattr(result, 'start'):
                        result.start()
                    elif result and hasattr(result, 'run_forever'):
                        result.run_forever()
                    elif result and hasattr(result, 'serve_forever'):
                        result.serve_forever()
                except KeyboardInterrupt:
                    print("\\n🛑 Bot stopped by user")
                except Exception as e:
                    print(f"❌ Error in {{entry}}: {{e}}")
                heartbeat_running = False
                sys.exit(0)
    
    print("⚠️ No entry point found, trying direct execution...")
    
except Exception as e:
    print(f"⚠️ Import method failed: {{e}}")

# ========== METHOD 2: Subprocess execution ==========
print("📌 Method 2: Running as subprocess...")
import subprocess

try:
    result = subprocess.run([sys.executable, r"{dest_script}"], env=os.environ.copy())
    sys.exit(result.returncode)
except KeyboardInterrupt:
    print("\\n🛑 Stopped by user")
    sys.exit(0)
except Exception as e:
    print(f"❌ Subprocess failed: {{e}}")
    sys.exit(1)
''')
    
    launcher_script.chmod(0o755)
    return launcher_script, frameworks

# ========== ENHANCED DEPLOYMENT FUNCTION ==========
def deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars, 
                     plan, duration, cost_coins, cost_stars, payment_method, is_free=False):
    status_msg = send_message(chat_id, "```\n🚀 STARTING DEPLOYMENT\n```", None)
    if not status_msg:
        return False
    status_message_id = status_msg.get('result', {}).get('message_id')
    
    logs = []
    
    def update_logs(new_log):
        logs.append(new_log)
        display_logs = logs[-25:]
        log_text = "\n".join(display_logs)
        if status_message_id:
            try:
                edit_message(chat_id, status_message_id, 
                            f"```\n🚀 DEPLOYMENT IN PROGRESS\n\n{log_text[-3000:]}\n```", None)
            except:
                pass
    
    try:
        update_logs("📁 Creating deployment folder...")
        
        deploy_id = int(datetime.now().timestamp())
        deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(deploy_id)
        deploy_folder.mkdir(parents=True, exist_ok=True)
        
        saved_path, saved_filename = save_user_file(user_id, temp_file, Path(temp_file).name)
        file_size = saved_path.stat().st_size
        
        dest_script = deploy_folder / saved_filename
        shutil.copy2(saved_path, dest_script)
        update_logs(f"📄 File saved: {saved_filename} ({format_file_size(file_size)})")
        
        # Parse environment variables
        env_vars_dict = {}
        if env_vars:
            if isinstance(env_vars, dict):
                env_vars_dict = env_vars
            elif isinstance(env_vars, str):
                for line in env_vars.strip().split('\n'):
                    line = line.strip()
                    if '=' in line:
                        first_eq = line.find('=')
                        key = line[:first_eq].strip()
                        value = line[first_eq+1:].strip()
                        if key:
                            env_vars_dict[key] = value
        
        # Read code content for analysis
        with open(dest_script, 'r', encoding='utf-8') as f:
            code_content = f.read()
        
        # Detect dependencies
        update_logs("🔍 Scanning for dependencies...")
        requirements_list = []
        
        if requirements_text and requirements_text.strip():
            requirements_list = AutoDependencyDetector.scan_requirements_file(requirements_text, update_logs)
        else:
            auto_detected = AutoDependencyDetector.scan_imports(code_content, update_logs)
            if auto_detected:
                requirements_list.extend(auto_detected)
                update_logs(f"📦 Auto-detected {len(auto_detected)} package(s)")
        
        # Install dependencies
        deps_success, failed = True, []
        if requirements_list:
            reqs_file = deploy_folder / "requirements.txt"
            with open(reqs_file, 'w') as f:
                f.write('\n'.join(requirements_list))
            deps_success, failed = install_dependencies_enhanced(reqs_file, update_logs)
        
        if not deps_success and requirements_list:
            update_logs("⚠️ Some dependencies failed to install - continuing anyway")
        
        # Create .env file
        env_file = deploy_folder / ".env"
        with open(env_file, 'w') as f:
            for k, v in env_vars_dict.items():
                f.write(f"{k}={v}\n")
        update_logs(f"📝 Created .env with {len(env_vars_dict)} variables")
        
        # Create enhanced launcher
        update_logs("🚀 Creating enhanced launcher...")
        launcher_script, frameworks = create_enhanced_launcher_script(deploy_folder, dest_script, env_vars_dict, code_content, update_logs)
        
        # Create start script
        start_script = deploy_folder / "start.sh"
        with open(start_script, 'w') as f:
            f.write(f"""#!/bin/bash
cd "{deploy_folder}"
export PYTHONUNBUFFERED=1
nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &
echo $! > pid.txt
""")
        start_script.chmod(0o755)
        
        # Start the bot
        update_logs("🚀 Starting bot process...")
        subprocess.run([str(start_script)], cwd=str(deploy_folder), capture_output=True, text=True)
        
        sleep(5)
        
        # Check if running
        pid_file = deploy_folder / "pid.txt"
        proc_pid = None
        if pid_file.exists():
            with open(pid_file, 'r') as f:
                try:
                    proc_pid = int(f.read().strip())
                except:
                    pass
        
        is_running = False
        if proc_pid:
            try:
                os.kill(proc_pid, 0)
                is_running = True
            except:
                pass
        
        # Check output
        log_file = deploy_folder / "output.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            with open(log_file, 'r') as f:
                output = f.read()
                if output:
                    update_logs("📋 Output preview:")
                    for line in output.split('\n')[:15]:
                        if line.strip():
                            update_logs(f"   {line[:120]}")
        
        if is_running:
            start_time = datetime.now()
            expire_time = start_time + timedelta(days=duration) if not is_free else start_time + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
            
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments 
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method, cost_coins, cost_stars, 
                 start_time, expire_time, status, proc_pid, install_log, deploy_log, is_free, is_paused, framework, dependencies_installed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, dest_script.name, file_size, requirements_text or "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 start_time.isoformat(), expire_time.isoformat(), "active", proc_pid,
                 "\n".join(logs[-100:]), "Bot running", 1 if is_free else 0, 0,
                 ', '.join(frameworks), json.dumps(requirements_list)))
            deployment_db_id = c.lastrowid
            conn.commit()
            conn.close()
            
            Path(temp_file).unlink(missing_ok=True)
            set_user_step(user_id, None)
            
            success_keyboard = {
                "inline_keyboard": [
                    [{"text": "📋 View Logs", "callback_data": f"view_install_logs_{deployment_db_id}"}],
                    [{"text": "📄 Runtime Logs", "callback_data": f"view_runtime_logs_{deployment_db_id}"}],
                    [{"text": "🔄 Restart", "callback_data": f"restart_deploy_{deployment_db_id}"}],
                    [{"text": "🗑️ Delete", "callback_data": f"delete_deploy_{deployment_db_id}"}],
                    [{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                    [{"text": "🏠 Menu", "callback_data": "main_menu"}]
                ]
            }
            
            success_text = (
                f"**🎉 DEPLOYMENT SUCCESSFUL!** 🎉\n\n"
                f"📁 **File:** `{dest_script.name}`\n"
                f"🔧 **Framework:** {', '.join(frameworks)}\n"
                f"📋 **Plan:** {plan.upper()}\n"
                f"⏱️ **Duration:** {duration if not is_free else FREE_DEPLOYMENT_DURATION_HOURS} {'days' if not is_free else 'hours'}\n"
                f"📅 **Expires:** {expire_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"📦 **Dependencies:** {len(requirements_list)} package(s)\n"
                f"🔧 **Env Vars:** {len(env_vars_dict)}\n"
                f"🆔 **ID:** `{deployment_db_id}`"
            )
            
            if failed:
                success_text += f"\n\n⚠️ **Partial Success:** {len(failed)} package(s) failed to install"
            
            edit_message(chat_id, status_message_id, success_text, success_keyboard)
            
            if proc_pid:
                with deployment_lock:
                    active_deployments[deployment_db_id] = proc_pid
            
            return True
        else:
            error_output = ""
            if log_file.exists() and log_file.stat().st_size > 0:
                with open(log_file, 'r') as f:
                    error_output = f.read()[:2000]
            
            error_msg = f"❌ **DEPLOYMENT FAILED**\n\n"
            if error_output:
                error_msg += f"**Output:**\n```\n{error_output}\n```"
            else:
                error_msg += "No output captured. Common issues:\n"
                error_msg += "• Missing required imports\n"
                error_msg += "• Syntax error in your code\n"
                error_msg += "• BOT_TOKEN not properly configured\n\n"
                error_msg += "**Tip:** Use `os.environ.get('BOT_TOKEN')` in your code"
            
            edit_message(chat_id, status_message_id, error_msg[:4000])
            
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments 
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method, cost_coins, cost_stars, 
                 start_time, expire_time, status, install_log, deploy_log, error_log, is_free, framework)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, dest_script.name, file_size, requirements_text or "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 datetime.now().isoformat(), datetime.now().isoformat(), "failed",
                 "\n".join(logs[-50:]), "Bot failed to start", error_msg[:2000], 1 if is_free else 0,
                 ', '.join(frameworks)))
            conn.commit()
            conn.close()
            
            Path(temp_file).unlink(missing_ok=True)
            return False
            
    except Exception as e:
        error_msg = f"❌ **DEPLOYMENT FAILED**\n\nException: {str(e)}\n{traceback.format_exc()}"
        edit_message(chat_id, status_message_id, error_msg[:4000])
        Path(temp_file).unlink(missing_ok=True)
        return False

# ========== WRAPPER FUNCTIONS ==========
def deploy_free_bot_with_logs(chat_id, user_id, temp_file, requirements_text, env_vars):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", None)
        return False
    
    can_deploy, reason = can_use_free_deployment(user_id)
    if not can_deploy:
        send_message(chat_id, f"❌ **FREE DEPLOYMENT LIMIT REACHED**\n\n{reason}\n\nUpgrade to Premium for unlimited deployments!",
                    {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        return False
    
    return deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars,
                           "free", FREE_DEPLOYMENT_DURATION_HOURS, 0, 0, "none", is_free=True)

def deploy_paid_bot(chat_id, user_id, temp_file, requirements_text, env_vars, plan, duration, cost_coins, cost_stars, payment_method):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", None)
        return False
    
    if is_user_premium(user_id) or is_admin(user_id):
        send_message(chat_id, 
            f"**✨ PREMIUM BENEFIT ACTIVE!**\n\n"
            f"As a premium user, your {plan.upper()} deployment is **FREE**!\n\n"
            f"Proceeding with deployment...")
        
        return deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars,
                               plan, duration, 0, 0, "premium_free", is_free=False)
    else:
        return deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars,
                               plan, duration, cost_coins, cost_stars, payment_method, is_free=False)

def get_payment_keyboard(plan, cost_stars, cost_coins):
    return {
        "inline_keyboard": [
            [{"text": f"⭐ Pay {cost_stars} Stars", "callback_data": f"pay_stars_{plan}"}],
            [{"text": f"🪙 Pay {cost_coins} Coins", "callback_data": f"pay_coins_{plan}"}],
            [{"text": "🔙 Back", "callback_data": "deploy_new"}]
        ]
    }

# ========== DEPLOYMENT MANAGEMENT ==========
def delete_deployment(deployment_id, user_id, chat_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute("SELECT proc_pid, file_name, user_id, is_free, plan FROM deployments WHERE deployment_id = ?", (deployment_id,))
        row = c.fetchone()
        
        if not row:
            send_message(chat_id, "❌ Deployment not found")
            return False
        
        proc_pid, file_name, owner_id, is_free, plan = row
        
        if owner_id != user_id and not is_admin(user_id):
            send_message(chat_id, "❌ Permission denied")
            return False
        
        if proc_pid:
            try:
                os.kill(proc_pid, signal.SIGTERM)
                sleep(1)
            except:
                pass
        
        deploy_folder = DEPLOYMENTS_DIR / str(owner_id) / str(deployment_id)
        if deploy_folder.exists():
            shutil.rmtree(deploy_folder)
        
        c.execute("DELETE FROM deployments WHERE deployment_id = ?", (deployment_id,))
        conn.commit()
        conn.close()
        
        if is_free:
            used_count = get_free_deployment_used_count(user_id)
            remaining = FREE_USER_MAX_DEPLOYMENTS - used_count
            send_message(chat_id, 
                f"✅ **Deployment `{deployment_id}` deleted!**\n\n"
                f"📁 File: `{file_name}`\n"
                f"🆓 Free slots left: `{remaining}/{FREE_USER_MAX_DEPLOYMENTS}`")
        else:
            send_message(chat_id, f"✅ **Deployment `{deployment_id}` deleted!**\n\n📁 File: `{file_name}`")
        
        with deployment_lock:
            if deployment_id in active_deployments:
                del active_deployments[deployment_id]
        
        update_system_stats()
        return True
        
    except Exception as e:
        send_message(chat_id, f"❌ Error deleting deployment: {str(e)}")
        return False

def restart_deployment(deployment_id, user_id, chat_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute("SELECT proc_pid, file_name, user_id, is_paused, env_vars, status, start_time, expire_time FROM deployments WHERE deployment_id = ?", (deployment_id,))
        row = c.fetchone()
        
        if not row:
            send_message(chat_id, "❌ Deployment not found")
            return False
        
        proc_pid, file_name, owner_id, is_paused, env_vars_json, status, start_time_str, expire_time_str = row
        
        if owner_id != user_id and not is_admin(user_id):
            send_message(chat_id, "❌ Permission denied")
            return False
        
        if is_paused:
            send_message(chat_id, "❌ This deployment is paused. Renew premium to resume.")
            return False
        
        deploy_folder = DEPLOYMENTS_DIR / str(owner_id) / str(deployment_id)
        dest_script = deploy_folder / file_name
        
        if not deploy_folder.exists():
            send_message(chat_id, f"❌ Deployment folder missing!")
            return False
        
        if not dest_script.exists():
            send_message(chat_id, f"❌ Bot file `{file_name}` missing!\n\nPlease delete this deployment and create a new one.")
            return False
        
        if proc_pid:
            try:
                os.kill(proc_pid, signal.SIGTERM)
                sleep(2)
            except:
                pass
        
        env_vars = json.loads(env_vars_json) if env_vars_json else {}
        
        # Read code content for launcher
        with open(dest_script, 'r', encoding='utf-8') as f:
            code_content = f.read()
        
        launcher_script, _ = create_enhanced_launcher_script(deploy_folder, dest_script, env_vars, code_content, lambda x: None)
        
        env_file = deploy_folder / ".env"
        with open(env_file, 'w') as f:
            for k, v in env_vars.items():
                f.write(f"{k}={v}\n")
        
        start_script = deploy_folder / "start.sh"
        with open(start_script, 'w') as f:
            f.write(f"""#!/bin/bash
cd "{deploy_folder}"
export PYTHONUNBUFFERED=1
nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &
echo $! > pid.txt
""")
        start_script.chmod(0o755)
        
        result = subprocess.run([str(start_script)], cwd=str(deploy_folder), capture_output=True, text=True)
        
        sleep(3)
        
        pid_file = deploy_folder / "pid.txt"
        new_pid = None
        if pid_file.exists():
            with open(pid_file, 'r') as f:
                try:
                    new_pid = int(f.read().strip())
                except:
                    pass
        
        is_running = False
        if new_pid:
            try:
                os.kill(new_pid, 0)
                is_running = True
            except:
                pass
        
        if is_running:
            c.execute("UPDATE deployments SET proc_pid = ?, status = 'active', is_paused = 0 WHERE deployment_id = ?",
                      (new_pid, deployment_id))
            conn.commit()
            conn.close()
            
            with deployment_lock:
                active_deployments[deployment_id] = new_pid
            
            send_message(chat_id, f"✅ Deployment `{deployment_id}` restarted successfully!")
            return True
        else:
            log_file = deploy_folder / "output.log"
            error_msg = ""
            if log_file.exists():
                with open(log_file, 'r') as f:
                    error_output = f.read()[-500:]
                    if error_output:
                        error_msg = f"\n\n**Error output:**\n```\n{error_output}\n```"
            
            c.execute("UPDATE deployments SET status = 'failed' WHERE deployment_id = ?", (deployment_id,))
            conn.commit()
            conn.close()
            
            send_message(chat_id, f"❌ Failed to restart deployment `{deployment_id}`{error_msg}")
            return False
        
    except Exception as e:
        send_message(chat_id, f"❌ Error restarting: {str(e)}")
        return False

def stop_deployment(deployment_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT proc_pid FROM deployments WHERE deployment_id = ?", (deployment_id,))
    row = c.fetchone()
    if row and row[0]:
        try:
            os.kill(row[0], signal.SIGTERM)
        except:
            pass
    c.execute("UPDATE deployments SET status = 'stopped', proc_pid = NULL WHERE deployment_id = ?", (deployment_id,))
    conn.commit()
    conn.close()
    
    with deployment_lock:
        if deployment_id in active_deployments:
            del active_deployments[deployment_id]
    
    update_system_stats()

# ========== LOG VIEWING FUNCTIONS ==========
def view_install_logs(chat_id, message_id, user_id, dep_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT install_log, error_log, file_name FROM deployments WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id))
    row = c.fetchone()
    conn.close()
    
    if not row:
        edit_message(chat_id, message_id, "❌ No logs found", 
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "my_deployments"}]]})
        return
    
    install_log, error_log, file_name = row
    log_text = install_log if install_log else "No installation logs available"
    if error_log:
        log_text += f"\n\n❌ ERROR:\n{error_log}"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "📄 View Runtime Logs", "callback_data": f"view_runtime_logs_{dep_id}"}],
            [{"text": "🔄 Restart", "callback_data": f"restart_deploy_{dep_id}"}],
            [{"text": "🗑️ Delete", "callback_data": f"delete_deploy_{dep_id}"}],
            [{"text": "🔙 Back", "callback_data": f"view_deploy_{dep_id}"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"```\n📋 INSTALLATION LOGS\n📁 {file_name}\n\n{log_text[-3000:]}\n```",
        keyboard)

def view_runtime_logs(chat_id, message_id, user_id, dep_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deploy_log, file_name, status, error_log, is_paused FROM deployments WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id))
    row = c.fetchone()
    conn.close()
    
    if not row:
        edit_message(chat_id, message_id, "❌ Not found", 
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "my_deployments"}]]})
        return
    
    deploy_log, file_name, status, error_log, is_paused = row
    log_file = DEPLOYMENTS_DIR / str(user_id) / str(dep_id) / "output.log"
    
    log_text = deploy_log if deploy_log else ""
    
    if log_file.exists():
        with open(log_file, 'r') as f:
            runtime_log = f.read()
            if runtime_log:
                log_text += f"\n\n📌 RUNTIME OUTPUT:\n{runtime_log[-3000:]}"
    
    if error_log:
        log_text += f"\n\n❌ ERROR:\n{error_log}"
    
    if status == "active":
        log_text += "\n\n✅ RUNNING"
    elif status == "paused":
        log_text += "\n\n⏸️ PAUSED"
    elif status == "stopped":
        log_text += "\n\n🛑 STOPPED"
    elif status == "failed":
        log_text += "\n\n❌ FAILED"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "🔄 Refresh", "callback_data": f"view_runtime_logs_{dep_id}"}],
            [{"text": "📋 Install Logs", "callback_data": f"view_install_logs_{dep_id}"}],
            [{"text": "🔄 Restart", "callback_data": f"restart_deploy_{dep_id}"}],
            [{"text": "🗑️ Delete", "callback_data": f"delete_deploy_{dep_id}"}],
            [{"text": "🔙 Back", "callback_data": f"view_deploy_{dep_id}"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"```\n📄 RUNTIME LOGS\n📁 {file_name} (Status: {status.upper()})\n\n{log_text[-3000:]}\n```",
        keyboard)

def view_deployment(chat_id, message_id, user_id, dep_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT file_name, file_size, plan, payment_method, cost_coins, cost_stars, start_time, expire_time, status, is_free, requirements, env_vars, error_log, is_paused, framework FROM deployments WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id))
    row = c.fetchone()
    conn.close()
    
    if not row:
        edit_message(chat_id, message_id, "❌ Not found", {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "my_deployments"}]]})
        return
    
    fname, fsize, plan, payment, cost_coins, cost_stars, start_str, expire_str, status, is_free, requirements, env_vars_json, error_log, is_paused, framework = row
    start_time = datetime.fromisoformat(start_str)
    expire_time = datetime.fromisoformat(expire_str)
    env_vars = json.loads(env_vars_json) if env_vars_json else {}
    
    if is_free:
        remaining = (expire_time - datetime.now()).total_seconds() / 3600
        cost_text = "FREE"
        remaining_text = f"{int(remaining)}h"
    else:
        remaining = (expire_time - datetime.now()).days
        if payment == "stars":
            cost_text = f"{cost_stars}⭐"
        elif payment == "coins":
            cost_text = f"{cost_coins}🪙"
        elif payment == "premium_free":
            cost_text = "FREE (Premium)"
        else:
            cost_text = f"{cost_coins}🪙"
        remaining_text = f"{remaining}d"
    
    if status == "active":
        status_emoji = "🟢 ACTIVE"
    elif status == "paused":
        status_emoji = "⏸️ PAUSED"
    elif status == "stopped":
        status_emoji = "🔴 STOPPED"
    elif status == "failed":
        status_emoji = "❌ FAILED"
    else:
        status_emoji = status.upper()
    
    size_str = format_file_size(fsize) if fsize else "Unknown"
    
    env_text = "\n".join([f"• `{k}`" for k in list(env_vars.keys())[:10]]) if env_vars else "None"
    if len(env_vars) > 10:
        env_text += f"\n• ... and {len(env_vars) - 10} more"
    
    reqs_text = requirements if requirements else "None"
    if len(reqs_text) > 200:
        reqs_text = reqs_text[:200] + "..."
    
    keyboard = {"inline_keyboard": []}
    if status == "active":
        keyboard["inline_keyboard"].append([{"text": "🛑 Stop Bot", "callback_data": f"stop_deploy_{dep_id}"}])
        keyboard["inline_keyboard"].append([{"text": "🔄 Restart Bot", "callback_data": f"restart_deploy_{dep_id}"}])
    elif status == "paused":
        keyboard["inline_keyboard"].append([{"text": "💰 Renew Premium to Resume", "callback_data": "subscribe_premium"}])
    
    keyboard["inline_keyboard"].append([{"text": "📋 View Install Logs", "callback_data": f"view_install_logs_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "📄 View Runtime Logs", "callback_data": f"view_runtime_logs_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🗑️ Delete Deployment", "callback_data": f"delete_deploy_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "my_deployments"}])
    keyboard["inline_keyboard"].append([{"text": "🏠 Menu", "callback_data": "main_menu"}])
    
    error_text = f"\n\n**❌ Error:**\n`{error_log[:500]}`" if error_log else ""
    
    if is_free and not is_user_premium(user_id) and not is_admin(user_id):
        used = get_free_deployment_used_count(user_id)
        remaining_slots = FREE_USER_MAX_DEPLOYMENTS - used
        free_status_text = f"\n\n**🆓 Free Slots Left:** `{remaining_slots}/{FREE_USER_MAX_DEPLOYMENTS}`"
    else:
        free_status_text = ""
    
    text = (
        f"**📄 DEPLOYMENT #{dep_id}**\n\n"
        f"📁 File: `{fname}` ({size_str})\n"
        f"🔧 Framework: `{framework}`\n"
        f"📋 Plan: `{plan.upper()}`\n"
        f"💰 Cost: {cost_text}\n"
        f"📅 Started: `{start_time.strftime('%Y-%m-%d %H:%M')}`\n"
        f"⏰ Expires: `{expire_time.strftime('%Y-%m-%d %H:%M')}`\n"
        f"📊 Remaining: `{remaining_text}`\n"
        f"🔘 Status: {status_emoji}\n"
        f"📦 Requirements:\n`{reqs_text}`\n\n"
        f"🔧 Environment Variables: {len(env_vars)}\n{env_text}{error_text}{free_status_text}"
    )
    edit_message(chat_id, message_id, text, keyboard)

# ========== HANDLERS ==========
def handle_start(chat_id, user_id, username, first_name):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, join_date) VALUES (?, ?, ?, ?)",
              (user_id, username, first_name, datetime.now().isoformat()))
    c.execute("UPDATE users SET last_active = ? WHERE user_id = ?", (datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()
    
    update_system_stats()
    
    if is_user_verified(user_id):
        balances = get_user_balances(user_id)
        is_premium = is_user_premium(user_id)
        used_free = get_free_deployment_used_count(user_id)
        free_remaining = FREE_USER_MAX_DEPLOYMENTS - used_free
        
        stats = get_system_stats()
        server_start = stats.get('server_start_time')
        uptime = 0
        if server_start:
            start_time = datetime.fromisoformat(server_start)
            uptime = (datetime.now() - start_time).total_seconds()
        
        premium_badge = "⭐ PREMIUM ⭐" if is_premium else "🆓 FREE"
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM deployments WHERE user_id = ? AND status = 'paused'", (user_id,))
        paused_count = c.fetchone()[0] or 0
        conn.close()
        
        welcome = (
            f"**🤖 BOT HOSTING SERVICE**\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Welcome **{first_name}**!\n"
            f"🪙 Coins: `{balances['coins']}`\n"
            f"⭐ Stars: `{balances['stars']}`\n"
            f"🎫 Status: {premium_badge}\n"
            f"🆓 Free Slots: `{free_remaining}/{FREE_USER_MAX_DEPLOYMENTS}`\n"
            f"⏸️ Paused: `{paused_count}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🖥️ **Server:** Uptime `{format_uptime(uptime)}`\n"
            f"💱 Exchange: `1⭐ = {STARS_PER_COIN}🪙`\n\n"
            f"**⭐ Premium Benefits:**\n"
            f"• Unlimited free deployments (24h)\n"
            f"• FREE Monthly/Yearly deployments\n"
            f"• Auto-resume paused bots\n\n"
            f"💰 Monthly: `{PRICE_MONTHLY_STARS}⭐` / `{PRICE_MONTHLY_COINS}🪙`\n"
            f"💰 Yearly: `{PRICE_YEARLY_STARS}⭐` / `{PRICE_YEARLY_COINS}🪙`\n"
            f"📦 Max file size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"Choose an option:"
        )
        send_message(chat_id, welcome, get_main_menu(user_id))
    else:
        send_verification_required(chat_id, user_id, first_name)

def handle_balance(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    balances = get_user_balances(user_id)
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT total_coins_earned, total_coins_spent, total_stars_earned, total_stars_spent FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    is_premium = is_user_premium(user_id)
    used_free = get_free_deployment_used_count(user_id)
    free_remaining = FREE_USER_MAX_DEPLOYMENTS - used_free
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "main_menu"}]]}
    premium_badge = "⭐ PREMIUM ⭐" if is_premium else "🆓 FREE"
    
    text = (
        f"**💰 YOUR BALANCE**\n\n"
        f"🪙 Coins: `{balances['coins']}`\n"
        f"⭐ Stars: `{balances['stars']}`\n"
        f"🎫 Status: {premium_badge}\n"
        f"🆓 Free Slots: `{free_remaining}/{FREE_USER_MAX_DEPLOYMENTS}`\n"
        f"🪙 Earned: `{row[0] if row else 0}`\n"
        f"🪙 Spent: `{row[1] if row else 0}`\n"
        f"⭐ Earned: `{row[2] if row else 0}`\n"
        f"⭐ Spent: `{row[3] if row else 0}`\n\n"
        f"**Premium Pricing:**\n"
        f"📅 Monthly: `{PRICE_MONTHLY_STARS}⭐` / `{PRICE_MONTHLY_COINS}🪙`\n"
        f"🌟 Yearly: `{PRICE_YEARLY_STARS}⭐` / `{PRICE_YEARLY_COINS}🪙`"
    )
    
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def handle_redeem(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    set_user_step(user_id, 'awaiting_redeem', waiting_for_redeem=1)
    send_message(chat_id,
        f"**🎫 REDEEM CODE**\n\nSend your code:",
        {"inline_keyboard": [[{"text": "🔙 Cancel", "callback_data": "main_menu"}]]})

def process_redeem(chat_id, user_id, code):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User")
        return
    
    success, message = redeem_code(user_id, code.upper().strip())
    if success:
        balances = get_user_balances(user_id)
        message += f"\n\n🪙 `{balances['coins']}` | ⭐ `{balances['stars']}`"
    
    send_message(chat_id, message, {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
    set_user_step(user_id, None, waiting_for_redeem=0)

def handle_free_deployment(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    can_deploy, reason = can_use_free_deployment(user_id)
    
    if not can_deploy:
        if message_id:
            edit_message(chat_id, message_id,
                f"❌ **FREE DEPLOYMENT LIMIT REACHED**\n\n{reason}\n\nUpgrade to Premium for unlimited free deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        else:
            send_message(chat_id,
                f"❌ **FREE DEPLOYMENT LIMIT REACHED**\n\n{reason}\n\nUpgrade to Premium for unlimited free deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        return
    
    set_user_step(user_id, 'awaiting_file', plan='free', duration=FREE_DEPLOYMENT_DURATION_HOURS,
                  cost_coins=0, cost_stars=0, payment_method='none')
    
    text = (
        f"**🆓 FREE DEPLOYMENT**\n\n"
        f"⏱️ Duration: `{FREE_DEPLOYMENT_DURATION_HOURS}` hours\n"
        f"💰 Cost: FREE\n"
        f"📊 Free slots left: {reason}\n"
        f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
        f"📤 **Send your Python file (.py)**\n\n"
        f"After sending the file, send:\n"
        f"• requirements.txt file (optional)\n"
        f"• Environment variables (KEY=VALUE, one per line)"
    )
    
    if message_id:
        edit_message(chat_id, message_id, text,
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
    else:
        send_message(chat_id, text,
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})

def handle_paid_deployment(chat_id, user_id, message_id, plan, duration, cost_coins, cost_stars):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    set_user_step(user_id, 'awaiting_file', plan=plan, duration=duration,
                  cost_coins=cost_coins, cost_stars=cost_stars, payment_method=None)
    
    if is_user_premium(user_id) or is_admin(user_id):
        text = (
            f"**✨ PREMIUM BENEFIT!**\n\n"
            f"Your {plan.upper()} deployment is **FREE** as a premium member!\n\n"
            f"📤 **Send your Python file (.py)**\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt file (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"**Note:** All environment variables will be available via os.environ.get('KEY')"
        )
    else:
        text = (
            f"**💰 {plan.capitalize()} DEPLOYMENT**\n\n"
            f"⏱️ Duration: `{duration}` days\n"
            f"⭐ Cost: `{cost_stars}⭐`\n"
            f"🪙 Cost: `{cost_coins}🪙`\n\n"
            f"📤 **Send your Python file (.py)**\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt file (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"**Note:** All environment variables will be available via os.environ.get('KEY')"
        )
    
    edit_message(chat_id, message_id, text,
                {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "deploy_new"}]]})

def handle_deployments_list(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deployment_id, file_name, file_size, plan, expire_time, status, is_free, payment_method, is_paused, framework FROM deployments WHERE user_id = ? ORDER BY deployment_id DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        keyboard = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "main_menu"}]]}
        if message_id:
            edit_message(chat_id, message_id, "📭 **No Deployments**", keyboard)
        else:
            send_message(chat_id, "📭 **No Deployments**", keyboard)
        return
    
    if not is_user_premium(user_id) and not is_admin(user_id):
        used = get_free_deployment_used_count(user_id)
        remaining = FREE_USER_MAX_DEPLOYMENTS - used
        header = f"📦 **Your Deployments**\n\n🆓 Free slots: `{remaining}/{FREE_USER_MAX_DEPLOYMENTS}`\n\n"
    else:
        header = "📦 **Your Deployments**\n\n"
    
    keyboard = {"inline_keyboard": []}
    for dep_id, fname, fsize, plan, exp_str, status, is_free, payment, is_paused, framework in rows:
        exp_time = datetime.fromisoformat(exp_str)
        if is_free:
            remaining = (exp_time - datetime.now()).total_seconds() / 3600
            remaining_text = f"{int(remaining)}h"
        else:
            remaining = (exp_time - datetime.now()).days
            remaining_text = f"{remaining}d"
        
        if status == "active":
            status_icon = "✅"
        elif status == "paused":
            status_icon = "⏸️"
        elif status == "stopped":
            status_icon = "🛑"
        elif status == "failed":
            status_icon = "❌"
        else:
            status_icon = "❓"
        
        icon = "🆓" if is_free else ("⭐" if payment == "stars" else "🪙")
        if payment == "premium_free":
            icon = "✨"
        if is_paused:
            icon = "⏸️"
        
        # Framework icon
        if 'telegram' in framework:
            fw_icon = "🤖"
        elif 'discord' in framework:
            fw_icon = "🎮"
        elif 'flask' in framework or 'fastapi' in framework:
            fw_icon = "🌐"
        else:
            fw_icon = "🐍"
        
        size_str = format_file_size(fsize) if fsize else "Unknown"
        keyboard["inline_keyboard"].append([{"text": f"{icon}{status_icon}{fw_icon} ID:{dep_id} - {fname[:20]} ({size_str}) [{remaining_text}]", 
                         "callback_data": f"view_deploy_{dep_id}"}])
    
    keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "main_menu"}])
    
    if message_id:
        edit_message(chat_id, message_id, header, keyboard)
    else:
        send_message(chat_id, header, keyboard)

# ==================== MENU FUNCTIONS ====================
def get_main_menu(user_id):
    balances = get_user_balances(user_id)
    is_verified = is_user_verified(user_id)
    is_premium = is_user_premium(user_id)
    verified_badge = "✅" if is_verified else "🔐"
    premium_badge = "⭐" if is_premium else "🆓"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": f"{verified_badge} Join Channel", "callback_data": "check_verification"}],
            [{"text": "📤 Deploy New Bot", "callback_data": "deploy_new"}],
            [{"text": f"{premium_badge} Free Deployment (24h)", "callback_data": "free_deployment"}],
            [{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
            [{"text": f"💰 {balances['coins']}🪙 | {balances['stars']}⭐", "callback_data": "my_balance"}],
            [{"text": "🎫 Redeem Code", "callback_data": "redeem_code"}],
            [{"text": "⭐ Premium Subscription", "callback_data": "subscribe_premium"}],
        ]
    }
    
    if is_admin(user_id):
        keyboard["inline_keyboard"].append([{"text": "🔧 Admin Panel", "callback_data": "admin_panel"}])
    
    return keyboard

def get_deploy_menu(user_id):
    is_premium = is_user_premium(user_id)
    
    if is_premium or is_admin(user_id):
        keyboard = {
            "inline_keyboard": [
                [{"text": "📅 Monthly (30 days) - FREE for Premium", "callback_data": "plan_monthly"}],
                [{"text": "🌟 Yearly (365 days) - FREE for Premium", "callback_data": "plan_yearly"}],
                [{"text": "🆓 Free Deployment (24h)", "callback_data": "free_deployment"}],
                [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
            ]
        }
    else:
        keyboard = {
            "inline_keyboard": [
                [{"text": f"📅 Monthly (30 days) - {PRICE_MONTHLY_STARS}⭐ / {PRICE_MONTHLY_COINS}🪙", "callback_data": "plan_monthly"}],
                [{"text": f"🌟 Yearly (365 days) - {PRICE_YEARLY_STARS}⭐ / {PRICE_YEARLY_COINS}🪙", "callback_data": "plan_yearly"}],
                [{"text": "🆓 Free Deployment (24h)", "callback_data": "free_deployment"}],
                [{"text": "⭐ Get Premium for FREE Monthly/Yearly", "callback_data": "subscribe_premium"}],
                [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
            ]
        }
    return keyboard

def get_reqs_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📦 Send requirements.txt", "callback_data": "reqs_yes"}],
            [{"text": "⚡ Auto-detect & Skip", "callback_data": "reqs_no"}],
            [{"text": "❌ Cancel Deployment", "callback_data": "cancel_deploy"}]
        ]
    }

def get_env_keyboard(env_count):
    return {
        "inline_keyboard": [
            [{"text": f"📝 View Variables ({env_count})", "callback_data": "view_env"}],
            [{"text": "📤 Send KEY=VALUE (one per line)", "callback_data": "env_send"}],
            [{"text": "⏭️ SKIP - No Environment Variables", "callback_data": "env_skip"}],
            [{"text": "✅ DEPLOY NOW", "callback_data": "env_done"}],
            [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]
        ]
    }

# ==================== ADMIN PANEL ====================
def show_admin_panel(chat_id, message_id):
    stats = get_system_stats()
    stats_text = (
        f"**📊 System Stats**\n\n"
        f"👥 Users: `{stats.get('total_users', 0)}`\n"
        f"📦 Deployments: `{stats.get('total_deployments', 0)}`\n"
        f"🟢 Active: `{stats.get('active_deployments', 0)}`\n"
        f"⏸️ Paused: `{stats.get('paused_deployments', 0)}`\n"
        f"🆓 Free: `{stats.get('free_deployments', 0)}`\n"
        f"⭐ Premium: `{stats.get('premium_users', 0)}`\n"
        f"💰 Revenue: `${stats.get('revenue_usd', 0):.2f}`\n"
        f"🪙 Coins Created: `{stats.get('coins_created', 0)}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "🎫 Create Redeem Code", "callback_data": "admin_create_code"}],
            [{"text": "🪙 Add Coins", "callback_data": "admin_add_coins"}],
            [{"text": "📋 List Redeem Codes", "callback_data": "admin_list_codes"}],
            [{"text": "👥 List Users", "callback_data": "admin_list_users"}],
            [{"text": "📊 View Subscriptions", "callback_data": "admin_subscriptions"}],
            [{"text": "🔙 Back to Main Menu", "callback_data": "main_menu"}]
        ]
    }
    
    edit_message(chat_id, message_id, f"**🔧 ADMIN PANEL**\n\n{stats_text}", keyboard)

def admin_list_codes(chat_id, message_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT code, coins_amount, used_count, max_uses, expires_at, is_active, created_at
                 FROM redeem_codes ORDER BY created_at DESC LIMIT 30''')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        text = "📭 No redeem codes found."
    else:
        text = "**🎫 REDEEM CODES**\n\n"
        for code, coins, used, max_uses, expires, active, created in rows:
            status = "✅" if active else "❌"
            max_text = "∞" if max_uses == 0 else str(max_uses)
            expires_short = expires[:10] if expires else "Never"
            created_short = created[:10] if created else "Unknown"
            text += f"• `{code}` → {coins}🪙 | {used}/{max_text} | {status}\n"
            text += f"  Created: {created_short} | Expires: {expires_short}\n\n"
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, keyboard)

def admin_list_users(chat_id, message_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT user_id, username, first_name, coins_balance, stars_balance, is_premium, join_date
                 FROM users ORDER BY join_date DESC LIMIT 30''')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        text = "👥 No users found."
    else:
        text = "**👥 USERS**\n\n"
        for uid, username, first, coins, stars, premium, join_date in rows:
            premium_icon = "⭐" if premium else "🆓"
            join_short = join_date[:10] if join_date else "Unknown"
            text += f"• `{uid}` - {first or username or 'Unknown'}\n"
            text += f"  🪙{coins} ⭐{stars} | {premium_icon} | Joined: {join_short}\n\n"
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, keyboard)

def admin_subscriptions(chat_id, message_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT s.subscription_id, s.user_id, u.first_name, s.plan, s.amount_stars, 
                        s.start_date, s.end_date, s.status
                 FROM subscriptions s
                 JOIN users u ON s.user_id = u.user_id
                 ORDER BY s.end_date DESC LIMIT 30''')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        text = "📭 No subscriptions found."
    else:
        text = "**📋 SUBSCRIPTIONS**\n\n"
        for sub_id, uid, name, plan, stars, start_date, end_date, status in rows:
            start_short = start_date[:10] if start_date else "Unknown"
            end_short = end_date[:10] if end_date else "Unknown"
            status_icon = "✅" if status == "active" else "❌"
            text += f"• #{sub_id} - {name or uid}\n"
            text += f"  Plan: {plan.upper()} ({stars}⭐) | {status_icon}\n"
            text += f"  {start_short} → {end_short}\n\n"
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, keyboard)

def admin_create_code(chat_id, message_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "100 🪙", "callback_data": "create_code_100"},
             {"text": "500 🪙", "callback_data": "create_code_500"}],
            [{"text": "1000 🪙", "callback_data": "create_code_1000"},
             {"text": "5000 🪙", "callback_data": "create_code_5000"}],
            [{"text": "10000 🪙", "callback_data": "create_code_10000"},
             {"text": "50000 🪙", "callback_data": "create_code_50000"}],
            [{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]
        ]
    }
    edit_message(chat_id, message_id, "**🎫 CREATE REDEEM CODE**\n\nSelect amount:", keyboard)

def process_create_code(admin_id, amount, chat_id, message_id):
    code = create_redeem_code(admin_id, amount, 0, 30, 1)
    edit_message(chat_id, message_id,
        f"✅ **CODE CREATED!**\n\n"
        f"🎫 `{code}`\n"
        f"💰 {amount}🪙\n"
        f"📅 30 days\n"
        f"🔢 1 use\n\n"
        f"Share with users!",
        {"inline_keyboard": [[{"text": "🎫 Create Another", "callback_data": "admin_create_code"},
                              {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})

# ==================== ADMIN ADD COINS ====================
def admin_add_coins_start(chat_id, message_id):
    set_user_step(chat_id, 'awaiting_coins_target')
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "target_digit_1"}, {"text": "2", "callback_data": "target_digit_2"}, {"text": "3", "callback_data": "target_digit_3"}],
            [{"text": "4", "callback_data": "target_digit_4"}, {"text": "5", "callback_data": "target_digit_5"}, {"text": "6", "callback_data": "target_digit_6"}],
            [{"text": "7", "callback_data": "target_digit_7"}, {"text": "8", "callback_data": "target_digit_8"}, {"text": "9", "callback_data": "target_digit_9"}],
            [{"text": "0", "callback_data": "target_digit_0"}, {"text": "⌫", "callback_data": "target_backspace"}, {"text": "✅", "callback_data": "target_confirm"}],
            [{"text": "📋 Use My ID", "callback_data": "use_my_id_target"}],
            [{"text": "❌ Cancel", "callback_data": "admin_panel"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"**🪙 ADD COINS**\n\n"
        f"Enter the **User ID** using the number pad below:\n\n"
        f"**User ID:** ` `\n\n"
        f"Example: `123456789`",
        keyboard)

def update_target_id_display(admin_id, digit, chat_id, message_id):
    user_step = get_user_step(admin_id)
    current_target = user_step.get('temp_target_user') or ""
    
    if digit == 'backspace':
        new_target = current_target[:-1]
    else:
        new_target = current_target + str(digit)
    
    set_user_step(admin_id, 'awaiting_coins_target', temp_target_user=new_target)
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "target_digit_1"}, {"text": "2", "callback_data": "target_digit_2"}, {"text": "3", "callback_data": "target_digit_3"}],
            [{"text": "4", "callback_data": "target_digit_4"}, {"text": "5", "callback_data": "target_digit_5"}, {"text": "6", "callback_data": "target_digit_6"}],
            [{"text": "7", "callback_data": "target_digit_7"}, {"text": "8", "callback_data": "target_digit_8"}, {"text": "9", "callback_data": "target_digit_9"}],
            [{"text": "0", "callback_data": "target_digit_0"}, {"text": "⌫", "callback_data": "target_backspace"}, {"text": "✅", "callback_data": "target_confirm"}],
            [{"text": "📋 Use My ID", "callback_data": "use_my_id_target"}],
            [{"text": "❌ Cancel", "callback_data": "admin_panel"}]
        ]
    }
    
    display_target = new_target if new_target else " "
    edit_message(chat_id, message_id,
        f"**🪙 ADD COINS**\n\n"
        f"Enter the **User ID** using the number pad below:\n\n"
        f"**User ID:** `{display_target}`\n\n"
        f"Click ✅ when done.",
        keyboard)

def confirm_target_user(admin_id, chat_id, message_id):
    user_step = get_user_step(admin_id)
    target_user_id = user_step.get('temp_target_user')
    
    if not target_user_id or not target_user_id.strip():
        edit_message(chat_id, message_id,
            f"❌ **No User ID entered!**\n\nPlease enter a user ID using the number pad.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    try:
        target_user_id = int(target_user_id)
    except ValueError:
        edit_message(chat_id, message_id,
            f"❌ **Invalid User ID!**\n\nPlease enter a valid numeric user ID.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (target_user_id,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        edit_message(chat_id, message_id,
            f"❌ **User not found!**\n\nUser ID `{target_user_id}` does not exist.\n\nPlease check and try again.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    first_name = user[0] or "User"
    
    set_user_step(admin_id, 'awaiting_coins_amount', temp_target_user=str(target_user_id), temp_coins_amount=0)
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "coin_digit_1"}, {"text": "2", "callback_data": "coin_digit_2"}, {"text": "3", "callback_data": "coin_digit_3"}],
            [{"text": "4", "callback_data": "coin_digit_4"}, {"text": "5", "callback_data": "coin_digit_5"}, {"text": "6", "callback_data": "coin_digit_6"}],
            [{"text": "7", "callback_data": "coin_digit_7"}, {"text": "8", "callback_data": "coin_digit_8"}, {"text": "9", "callback_data": "coin_digit_9"}],
            [{"text": "0", "callback_data": "coin_digit_0"}, {"text": "⌫", "callback_data": "coin_backspace"}, {"text": "✅", "callback_data": "coin_confirm"}],
            [{"text": "100", "callback_data": "coin_preset_100"}, {"text": "500", "callback_data": "coin_preset_500"}],
            [{"text": "1000", "callback_data": "coin_preset_1000"}, {"text": "5000", "callback_data": "coin_preset_5000"}],
            [{"text": "10000", "callback_data": "coin_preset_10000"}, {"text": "50000", "callback_data": "coin_preset_50000"}],
            [{"text": "🔙 Back", "callback_data": "admin_add_coins"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"**🪙 ADD COINS**\n\n"
        f"Target user: `{target_user_id}` ({first_name})\n"
        f"Current balance: `{get_user_balances(target_user_id)['coins']}🪙`\n\n"
        f"Enter the amount of coins to add using the number pad below:\n\n"
        f"**Amount: `0` 🪙**",
        keyboard)

def update_coin_amount_display(admin_id, digit, chat_id, message_id):
    user_step = get_user_step(admin_id)
    current_amount = user_step.get('temp_coins_amount') or 0
    target_user_id = user_step.get('temp_target_user')
    
    if digit == 'backspace':
        new_amount = current_amount // 10
    else:
        new_amount = current_amount * 10 + digit
    
    set_user_step(admin_id, 'awaiting_coins_amount', 
                  temp_target_user=target_user_id, 
                  temp_coins_amount=new_amount)
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (int(target_user_id),))
    user = c.fetchone()
    conn.close()
    first_name = user[0] if user else "User"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "coin_digit_1"}, {"text": "2", "callback_data": "coin_digit_2"}, {"text": "3", "callback_data": "coin_digit_3"}],
            [{"text": "4", "callback_data": "coin_digit_4"}, {"text": "5", "callback_data": "coin_digit_5"}, {"text": "6", "callback_data": "coin_digit_6"}],
            [{"text": "7", "callback_data": "coin_digit_7"}, {"text": "8", "callback_data": "coin_digit_8"}, {"text": "9", "callback_data": "coin_digit_9"}],
            [{"text": "0", "callback_data": "coin_digit_0"}, {"text": "⌫", "callback_data": "coin_backspace"}, {"text": "✅", "callback_data": "coin_confirm"}],
            [{"text": "100", "callback_data": "coin_preset_100"}, {"text": "500", "callback_data": "coin_preset_500"}],
            [{"text": "1000", "callback_data": "coin_preset_1000"}, {"text": "5000", "callback_data": "coin_preset_5000"}],
            [{"text": "10000", "callback_data": "coin_preset_10000"}, {"text": "50000", "callback_data": "coin_preset_50000"}],
            [{"text": "🔙 Back", "callback_data": "admin_add_coins"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"**🪙 ADD COINS**\n\n"
        f"Target user: `{target_user_id}` ({first_name})\n"
        f"Current balance: `{get_user_balances(int(target_user_id))['coins']}🪙`\n\n"
        f"Enter the amount of coins to add using the number pad below:\n\n"
        f"**Amount: `{new_amount} 🪙**",
        keyboard)

def process_coin_preset(admin_id, preset_amount, chat_id, message_id):
    user_step = get_user_step(admin_id)
    target_user_id = user_step.get('temp_target_user')
    
    if not target_user_id:
        edit_message(chat_id, message_id, "❌ Session expired. Please start over.",
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_add_coins"}]]})
        return
    
    set_user_step(admin_id, 'awaiting_coins_amount', 
                  temp_target_user=target_user_id, 
                  temp_coins_amount=preset_amount)
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (int(target_user_id),))
    user = c.fetchone()
    conn.close()
    first_name = user[0] if user else "User"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "coin_digit_1"}, {"text": "2", "callback_data": "coin_digit_2"}, {"text": "3", "callback_data": "coin_digit_3"}],
            [{"text": "4", "callback_data": "coin_digit_4"}, {"text": "5", "callback_data": "coin_digit_5"}, {"text": "6", "callback_data": "coin_digit_6"}],
            [{"text": "7", "callback_data": "coin_digit_7"}, {"text": "8", "callback_data": "coin_digit_8"}, {"text": "9", "callback_data": "coin_digit_9"}],
            [{"text": "0", "callback_data": "coin_digit_0"}, {"text": "⌫", "callback_data": "coin_backspace"}, {"text": "✅", "callback_data": "coin_confirm"}],
            [{"text": "100", "callback_data": "coin_preset_100"}, {"text": "500", "callback_data": "coin_preset_500"}],
            [{"text": "1000", "callback_data": "coin_preset_1000"}, {"text": "5000", "callback_data": "coin_preset_5000"}],
            [{"text": "10000", "callback_data": "coin_preset_10000"}, {"text": "50000", "callback_data": "coin_preset_50000"}],
            [{"text": "🔙 Back", "callback_data": "admin_add_coins"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"**🪙 ADD COINS**\n\n"
        f"Target user: `{target_user_id}` ({first_name})\n"
        f"Current balance: `{get_user_balances(int(target_user_id))['coins']}🪙`\n\n"
        f"Amount preset: `{preset_amount} 🪙**\n\n"
        f"Click ✅ to confirm or continue entering digits:",
        keyboard)

def confirm_add_coins(admin_id, chat_id, message_id):
    user_step = get_user_step(admin_id)
    target_user_id = user_step.get('temp_target_user')
    amount = user_step.get('temp_coins_amount')
    
    if not target_user_id or not amount or amount <= 0:
        edit_message(chat_id, message_id, "❌ Invalid amount. Please try again.",
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_add_coins"}]]})
        return
    
    update_user_coins(int(target_user_id), amount, "admin_add", f"by_admin_{admin_id}")
    
    new_balance = get_user_balances(int(target_user_id))['coins']
    
    set_user_step(admin_id, None, temp_target_user=None, temp_coins_amount=None)
    
    edit_message(chat_id, message_id,
        f"✅ **COINS ADDED SUCCESSFULLY!**\n\n"
        f"User: `{target_user_id}`\n"
        f"Added: `+{amount}🪙`\n"
        f"New balance: `{new_balance}🪙`",
        {"inline_keyboard": [[{"text": "➕ Add More", "callback_data": "admin_add_coins"},
                              {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
    
    send_message(int(target_user_id),
        f"🎉 **You received {amount} Coins!** 🎉\n\n"
        f"Your new balance: `{new_balance}🪙`\n\n"
        f"Use your coins to purchase premium subscription!",
        {"inline_keyboard": [[{"text": "⭐ Get Premium", "callback_data": "subscribe_premium"}]]})

# ==================== CALLBACK HANDLER ====================
def handle_callback(callback):
    callback_id = callback['id']
    user_id = callback['from']['id']
    message = callback.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    message_id = message.get('message_id')
    data = callback['data']
    
    answer_callback(callback_id)
    print(f"📨 Callback: {data} from user {user_id}")
    
    # ========== MAIN MENU ==========
    if data == "main_menu":
        if is_user_verified(user_id):
            balances = get_user_balances(user_id)
            welcome = f"**🤖 BOT HOSTING**\n\n🪙 `{balances['coins']}` | ⭐ `{balances['stars']}`\n\nChoose:"
            edit_message(chat_id, message_id, welcome, get_main_menu(user_id))
        else:
            user_info = get_user_info(user_id)
            send_verification_required(chat_id, user_id, user_info.get('first_name', 'User'), message_id)
        return
    
    # ========== ADMIN PANEL ==========
    if data == "admin_panel":
        if is_admin(user_id):
            show_admin_panel(chat_id, message_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return
    
    if data == "admin_list_codes":
        if is_admin(user_id):
            admin_list_codes(chat_id, message_id)
        return
    
    if data == "admin_list_users":
        if is_admin(user_id):
            admin_list_users(chat_id, message_id)
        return
    
    if data == "admin_subscriptions":
        if is_admin(user_id):
            admin_subscriptions(chat_id, message_id)
        return
    
    if data == "admin_create_code":
        if is_admin(user_id):
            admin_create_code(chat_id, message_id)
        return
    
    if data.startswith("create_code_"):
        if is_admin(user_id):
            amount = int(data.split("_")[2])
            process_create_code(user_id, amount, chat_id, message_id)
        return
    
    # ========== ADMIN ADD COINS ==========
    if data == "admin_add_coins":
        if is_admin(user_id):
            admin_add_coins_start(chat_id, message_id)
        return
    
    if data.startswith("target_digit_"):
        if is_admin(user_id):
            digit = int(data.split("_")[2])
            update_target_id_display(user_id, digit, chat_id, message_id)
        return
    
    if data == "target_backspace":
        if is_admin(user_id):
            update_target_id_display(user_id, 'backspace', chat_id, message_id)
        return
    
    if data == "target_confirm":
        if is_admin(user_id):
            confirm_target_user(user_id, chat_id, message_id)
        return
    
    if data == "use_my_id_target":
        if is_admin(user_id):
            set_user_step(user_id, 'awaiting_coins_target', temp_target_user=str(user_id))
            confirm_target_user(user_id, chat_id, message_id)
        return
    
    if data.startswith("coin_digit_"):
        if is_admin(user_id):
            digit = int(data.split("_")[2])
            update_coin_amount_display(user_id, digit, chat_id, message_id)
        return
    
    if data == "coin_backspace":
        if is_admin(user_id):
            update_coin_amount_display(user_id, 'backspace', chat_id, message_id)
        return
    
    if data == "coin_confirm":
        if is_admin(user_id):
            confirm_add_coins(user_id, chat_id, message_id)
        return
    
    if data.startswith("coin_preset_"):
        if is_admin(user_id):
            amount = int(data.split("_")[2])
            process_coin_preset(user_id, amount, chat_id, message_id)
        return
    
    # ========== PREMIUM SUBSCRIPTION ==========
    if data == "subscribe_premium":
        show_premium_menu(chat_id, user_id, message_id)
        return
    
    if data == "premium_monthly_stars":
        purchase_premium_stars(chat_id, user_id, "monthly", 30, PRICE_MONTHLY_STARS)
        return
    
    if data == "premium_yearly_stars":
        purchase_premium_stars(chat_id, user_id, "yearly", 365, PRICE_YEARLY_STARS)
        return
    
    if data == "premium_monthly_coins":
        purchase_premium_coins(chat_id, user_id, "monthly", 30, PRICE_MONTHLY_COINS)
        return
    
    if data == "premium_yearly_coins":
        purchase_premium_coins(chat_id, user_id, "yearly", 365, PRICE_YEARLY_COINS)
        return
    
    # ========== DEPLOYMENT PLANS ==========
    if data == "deploy_new":
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, "User", message_id)
            return
        edit_message(chat_id, message_id, "**💰 DEPLOYMENT OPTIONS**\n\nChoose your plan:", get_deploy_menu(user_id))
        return
    
    if data == "plan_monthly":
        handle_paid_deployment(chat_id, user_id, message_id, "monthly", 30, PRICE_MONTHLY_COINS, PRICE_MONTHLY_STARS)
        return
    
    if data == "plan_yearly":
        handle_paid_deployment(chat_id, user_id, message_id, "yearly", 365, PRICE_YEARLY_COINS, PRICE_YEARLY_STARS)
        return
    
    if data == "free_deployment":
        handle_free_deployment(chat_id, user_id, message_id)
        return
    
    # ========== PAYMENT METHODS ==========
    if data.startswith("pay_stars_"):
        plan = data.split("_")[2]
        user_step = get_user_step(user_id)
        
        if user_step.get('plan') != plan:
            return
        
        payload = f"{plan}_{user_id}_{int(datetime.now().timestamp())}"
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO pending_deployments 
            (user_id, chat_id, message_id, temp_file, requirements, env_vars, plan, duration, 
             cost_coins, cost_stars, payment_method, payload, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, chat_id, message_id, user_step.get('temp_file'), user_step.get('requirements'),
             json.dumps(user_step.get('env_vars', {})), plan, user_step.get('duration'),
             user_step.get('cost_coins'), user_step.get('cost_stars'), 'stars', payload,
             datetime.now().isoformat(), 'pending'))
        conn.commit()
        conn.close()
        
        title = f"Bot Deployment - {plan.capitalize()} Plan"
        description = f"Deploy your bot for {user_step.get('duration')} days"
        
        url = f"{TELEGRAM_API}/sendInvoice"
        prices = [{"label": title, "amount": user_step.get('cost_stars')}]
        
        invoice_data = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "currency": "XTR",
            "prices": prices,
            "need_name": False,
            "need_phone_number": False,
            "need_email": False,
            "need_shipping_address": False,
            "is_flexible": False
        }
        
        try:
            data_bytes = json.dumps(invoice_data).encode('utf-8')
            req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get('ok'):
                    edit_message(chat_id, message_id,
                        f"**⭐ STARS PAYMENT REQUIRED**\n\n"
                        f"Plan: {plan.capitalize()}\n"
                        f"Cost: {user_step.get('cost_stars')}⭐\n\n"
                        f"Please complete the payment.",
                        {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "deploy_new"}]]})
                else:
                    send_message(chat_id, f"❌ Failed: {result.get('description')}")
        except Exception as e:
            send_message(chat_id, "❌ Failed to create invoice.")
        return
    
    if data.startswith("pay_coins_"):
        plan = data.split("_")[2]
        user_step = get_user_step(user_id)
        
        if user_step.get('plan') != plan:
            return
        
        balances = get_user_balances(user_id)
        cost_coins = user_step.get('cost_coins')
        
        if balances['coins'] < cost_coins:
            edit_message(chat_id, message_id,
                f"❌ **INSUFFICIENT COINS**\n\n"
                f"Required: `{cost_coins}🪙`\n"
                f"Your balance: `{balances['coins']}🪙`\n\n"
                f"Use a redeem code to get more coins!",
                {"inline_keyboard": [[{"text": "🎫 Redeem Code", "callback_data": "redeem_code"},
                                      {"text": "🔙 Back", "callback_data": "deploy_new"}]]})
            return
        
        update_user_coins(user_id, -cost_coins, "deployment", f"plan_{plan}")
        deploy_paid_bot(chat_id, user_id, user_step.get('temp_file'),
                        user_step.get('requirements'), user_step.get('env_vars', {}),
                        plan, user_step.get('duration'), cost_coins, user_step.get('cost_stars'), 'coins')
        return
    
    # ========== REQUIREMENTS HANDLING ==========
    if data == "reqs_yes":
        user_step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_reqs', waiting_for_reqs=1,
                     temp_file=user_step.get('temp_file'),
                     plan=user_step.get('plan'), duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'),
                     env_vars=user_step.get('env_vars', {}))
        edit_message(chat_id, message_id,
            f"**📦 SEND requirements.txt FILE**\n\n"
            f"Please send your `requirements.txt` file now.\n\n"
            f"Example content:\n"
            f"```\npython-telegram-bot==20.7\nrequests==2.31.0\n```\n\n"
            f"Or click Auto-detect:",
            {"inline_keyboard": [[{"text": "⚡ Auto-detect & Skip", "callback_data": "reqs_no"}],
                                  [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return
    
    if data == "reqs_no":
        user_step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_env', waiting_for_env=1,
                     temp_file=user_step.get('temp_file'), requirements=None,
                     plan=user_step.get('plan'), duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'),
                     env_vars=user_step.get('env_vars', {}))
        edit_message(chat_id, message_id,
            f"**🔧 ENVIRONMENT VARIABLES (Optional)**\n\n"
            f"Send environment variables one per line:\n"
            f"```\nBOT_TOKEN=your_token_here\nAPI_KEY=your_api_key\nDATABASE_URL=postgresql://...\n```\n\n"
            f"**Note:** All variables will be available via `os.environ.get('KEY')`\n\n"
            f"Or use the buttons below:",
            get_env_keyboard(0))
        return
    
    # ========== ENVIRONMENT VARIABLES ==========
    if data == "view_env":
        user_step = get_user_step(user_id)
        env_vars = user_step.get('env_vars', {})
        if not env_vars:
            text = "📝 **No environment variables set**\n\nSend as KEY=VALUE (one per line)\n\nExample:\n```\nBOT_TOKEN=123456:ABC\nAPI_KEY=your_key\nDATABASE_URL=postgresql://...\n```"
        else:
            text = "📝 **Current Variables:**\n\n"
            for k, v in list(env_vars.items())[:15]:
                if any(s in k.upper() for s in ['TOKEN', 'SECRET', 'KEY', 'PASSWORD', 'HASH']):
                    display_v = v[:10] + "..." if len(v) > 15 else v
                else:
                    display_v = v[:30] + "..." if len(v) > 35 else v
                text += f"• `{k}` = `{display_v}`\n"
            if len(env_vars) > 15:
                text += f"\n... and {len(env_vars) - 15} more"
            text += f"\n\nTotal: `{len(env_vars)}`\n\nAll variables will be available via `os.environ.get()`"
        edit_message(chat_id, message_id, text, get_env_keyboard(len(env_vars)))
        return
    
    if data == "env_send":
        user_step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_env', waiting_for_env=1,
                     temp_file=user_step.get('temp_file'),
                     requirements=user_step.get('requirements'),
                     env_vars=user_step.get('env_vars', {}),
                     plan=user_step.get('plan'), duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'))
        edit_message(chat_id, message_id,
            f"**📤 SEND ENVIRONMENT VARIABLES**\n\n"
            f"Send your environment variables as text:\n"
            f"```\nBOT_TOKEN=your_actual_token\nAPI_KEY=your_api_key\nDATABASE_URL=postgresql://user:pass@localhost/db\n```\n\n"
            f"One variable per line. Use KEY=VALUE format.\n\n"
            f"**All variables will be available via `os.environ.get('KEY')`**\n\n"
            f"Click Skip if you have none:",
            {"inline_keyboard": [[{"text": "⏭️ Skip", "callback_data": "env_skip"}],
                                  [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return
    
    if data == "env_skip":
        user_step = get_user_step(user_id)
        
        if user_step.get('plan') == 'free':
            deploy_free_bot_with_logs(chat_id, user_id, user_step.get('temp_file'),
                                     user_step.get('requirements'), {})
        else:
            if is_user_premium(user_id) or is_admin(user_id):
                deploy_paid_bot(chat_id, user_id, user_step.get('temp_file'),
                                user_step.get('requirements'), {},
                                user_step.get('plan'), user_step.get('duration'), 0, 0, 'premium_free')
            else:
                edit_message(chat_id, message_id,
                    f"**💰 Choose Payment Method**\n\n"
                    f"Plan: {user_step.get('plan', 'monthly').upper()}\n"
                    f"Duration: {user_step.get('duration', 30)} days\n\n"
                    f"Cost: {user_step.get('cost_stars')}⭐ or {user_step.get('cost_coins')}🪙",
                    get_payment_keyboard(user_step.get('plan', 'monthly'), 
                                        user_step.get('cost_stars'), 
                                        user_step.get('cost_coins')))
        return
    
    if data == "env_done":
        user_step = get_user_step(user_id)
        
        if user_step.get('plan') == 'free':
            deploy_free_bot_with_logs(chat_id, user_id, user_step.get('temp_file'),
                                     user_step.get('requirements'), user_step.get('env_vars', {}))
        else:
            if is_user_premium(user_id) or is_admin(user_id):
                deploy_paid_bot(chat_id, user_id, user_step.get('temp_file'),
                                user_step.get('requirements'), user_step.get('env_vars', {}),
                                user_step.get('plan'), user_step.get('duration'), 0, 0, 'premium_free')
            else:
                edit_message(chat_id, message_id,
                    f"**💰 Choose Payment Method**\n\n"
                    f"Plan: {user_step.get('plan', 'monthly').upper()}\n"
                    f"Duration: {user_step.get('duration', 30)} days\n"
                    f"Environment variables: {len(user_step.get('env_vars', {}))}\n\n"
                    f"Cost: {user_step.get('cost_stars')}⭐ or {user_step.get('cost_coins')}🪙",
                    get_payment_keyboard(user_step.get('plan', 'monthly'), 
                                        user_step.get('cost_stars'), 
                                        user_step.get('cost_coins')))
        return
    
    if data == "cancel_deploy":
        user_step = get_user_step(user_id)
        if user_step.get('temp_file') and Path(user_step.get('temp_file')).exists():
            Path(user_step.get('temp_file')).unlink(missing_ok=True)
        set_user_step(user_id, None)
        edit_message(chat_id, message_id, "❌ Deployment cancelled",
                    {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
        return
    
    # ========== DEPLOYMENTS LIST ==========
    if data == "my_deployments":
        handle_deployments_list(chat_id, user_id, message_id)
        return
    
    if data.startswith("view_deploy_"):
        dep_id = int(data.split("_")[2])
        view_deployment(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("view_install_logs_"):
        dep_id = int(data.split("_")[3])
        view_install_logs(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("view_runtime_logs_"):
        dep_id = int(data.split("_")[3])
        view_runtime_logs(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("stop_deploy_"):
        dep_id = int(data.split("_")[2])
        stop_deployment(dep_id)
        send_message(chat_id, f"✅ Bot `{dep_id}` stopped",
                    {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        view_deployment(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("restart_deploy_"):
        dep_id = int(data.split("_")[2])
        restart_deployment(dep_id, user_id, chat_id)
        view_deployment(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("delete_deploy_"):
        dep_id = int(data.split("_")[2])
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ Yes, Delete", "callback_data": f"confirm_delete_{dep_id}"},
                 {"text": "❌ No", "callback_data": f"view_deploy_{dep_id}"}]
            ]
        }
        edit_message(chat_id, message_id,
            f"**⚠️ DELETE DEPLOYMENT**\n\n"
            f"Delete deployment `{dep_id}`?\n\n"
            f"This will:\n"
            f"• Stop the bot\n"
            f"• Delete all files\n"
            f"• Free a deployment slot\n\n"
            f"⚠️ Cannot be undone!",
            keyboard)
        return
    
    if data.startswith("confirm_delete_"):
        dep_id = int(data.split("_")[2])
        delete_deployment(dep_id, user_id, chat_id)
        handle_deployments_list(chat_id, user_id, message_id)
        return
    
    # ========== CHANNEL VERIFICATION ==========
    if data == "check_verification":
        if check_channel_membership(user_id):
            mark_channel_joined(user_id)
            balances = get_user_balances(user_id)
            edit_message(chat_id, message_id,
                f"✅ **VERIFIED!**\n\n"
                f"🪙 {balances['coins']} | ⭐ {balances['stars']}\n\n"
                f"Welcome!",
                get_main_menu(user_id))
        else:
            send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    if data == "verify_channel":
        if check_channel_membership(user_id):
            mark_channel_joined(user_id)
            balances = get_user_balances(user_id)
            edit_message(chat_id, message_id,
                f"✅ **VERIFIED!**\n\n"
                f"Thank you for joining {REQUIRED_CHANNEL}!",
                get_main_menu(user_id))
        else:
            edit_message(chat_id, message_id,
                f"❌ **NOT VERIFIED**\n\n"
                f"Please join {REQUIRED_CHANNEL} first.",
                {"inline_keyboard": [
                    [{"text": "📢 JOIN", "url": CHANNEL_LINK},
                     {"text": "✅ VERIFY", "callback_data": "verify_channel"}]
                ]})
        return
    
    # ========== MY BALANCE ==========
    if data == "my_balance":
        handle_balance(chat_id, user_id, message_id)
        return
    
    # ========== REDEEM CODE ==========
    if data == "redeem_code":
        handle_redeem(chat_id, user_id, message_id)
        return

# ==================== USER STEP FUNCTIONS ====================
def set_user_step(user_id, step, **kwargs):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    
    updates = ["step = ?"]
    values = [step]
    
    for key in ['temp_file', 'requirements', 'env_vars', 'plan', 'payment_method', 'duration', 
                'cost_coins', 'cost_stars', 'waiting_for_env', 'waiting_for_reqs', 'waiting_for_redeem', 
                'temp_target_user', 'temp_coins_amount', 'temp_stars_amount', 'temp_expiry', 'temp_reward_type']:
        if key in kwargs:
            updates.append(f"{key} = ?")
            if key == 'env_vars' and kwargs[key] is not None:
                values.append(json.dumps(kwargs[key]))
            else:
                values.append(kwargs[key])
    
    values.append(user_id)
    c.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", values)
    conn.commit()
    conn.close()

def get_user_step(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT step, temp_file, requirements, env_vars, plan, payment_method, duration, cost_coins, cost_stars, waiting_for_env, waiting_for_reqs, waiting_for_redeem, temp_target_user, temp_coins_amount, temp_stars_amount, temp_expiry, temp_reward_type FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        env_vars = {}
        if row[3]:
            try:
                env_vars = json.loads(row[3])
            except:
                env_vars = {}
        return {
            'step': row[0], 'temp_file': row[1], 'requirements': row[2],
            'env_vars': env_vars, 'plan': row[4], 'payment_method': row[5],
            'duration': row[6], 'cost_coins': row[7], 'cost_stars': row[8],
            'waiting_for_env': row[9] or 0, 'waiting_for_reqs': row[10] or 0,
            'waiting_for_redeem': row[11] or 0, 'temp_target_user': row[12],
            'temp_coins_amount': row[13], 'temp_stars_amount': row[14],
            'temp_expiry': row[15], 'temp_reward_type': row[16]
        }
    return {'step': None, 'temp_file': None, 'requirements': None, 'env_vars': {},
            'plan': None, 'payment_method': None, 'duration': None, 'cost_coins': None,
            'cost_stars': None, 'waiting_for_env': 0, 'waiting_for_reqs': 0,
            'waiting_for_redeem': 0, 'temp_target_user': None, 'temp_coins_amount': None,
            'temp_stars_amount': None, 'temp_expiry': None, 'temp_reward_type': None}

# ==================== MESSAGE HANDLER ====================
def handle_message(message):
    chat_id = message['chat']['id']
    user_id = message['from']['id']
    first_name = message['from'].get('first_name', 'User')
    
    print(f"📨 Message from {first_name} ({user_id})")
    
    if 'text' in message and message['text'] == '/start':
        handle_start(chat_id, user_id, message['from'].get('username', ''), first_name)
        return
    
    user_step = get_user_step(user_id)
    
    if 'text' in message:
        text = message['text']
        
        if user_step.get('waiting_for_redeem') == 1:
            process_redeem(chat_id, user_id, text)
            return
        
        if user_step.get('waiting_for_env') == 1 and text and not text.startswith('/'):
            env_vars = user_step.get('env_vars', {}).copy()
            added = 0
            
            lines = text.strip().split('\n')
            for line in lines:
                line = line.strip()
                if '=' in line:
                    first_eq = line.find('=')
                    key = line[:first_eq].strip()
                    value = line[first_eq+1:].strip()
                    if key:
                        env_vars[key] = value
                        added += 1
            
            if added > 0:
                set_user_step(user_id, 'awaiting_env', waiting_for_env=1,
                             temp_file=user_step.get('temp_file'),
                             requirements=user_step.get('requirements'),
                             env_vars=env_vars,
                             plan=user_step.get('plan'), duration=user_step.get('duration'),
                             cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                             payment_method=user_step.get('payment_method'))
                send_message(chat_id,
                    f"✅ Added `{added}` environment variable(s)!\n\n"
                    f"Total: `{len(env_vars)}`\n\n"
                    f"All variables will be available via `os.environ.get('KEY')`\n\n"
                    f"Send more or use buttons:",
                    get_env_keyboard(len(env_vars)))
            else:
                send_message(chat_id, 
                    f"❌ No valid KEY=VALUE pairs found.\n\n"
                    f"Format each variable on a new line:\n"
                    f"```\nKEY1=value1\nKEY2=value2\n```\n\n"
                    f"Example: BOT_TOKEN=123456:ABC\nAPI_KEY=your_key",
                    get_env_keyboard(len(user_step.get('env_vars', {}))))
            return
        
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, first_name, None)
            return
        
        send_message(chat_id, "❌ Unknown command. Use buttons below.", get_main_menu(user_id))
        return
    
    # Handle file upload
    if 'document' in message:
        doc = message['document']
        file_name = doc.get('file_name', 'unknown')
        file_size = doc.get('file_size', 0)
        print(f"📁 File: {file_name} ({format_file_size(file_size)})")
        
        if file_size > MAX_FILE_SIZE_BYTES:
            send_message(chat_id, f"❌ File too large! Max {MAX_FILE_SIZE_MB}MB")
            return
        
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, first_name, None)
            return
        
        # Requirements file
        if user_step.get('waiting_for_reqs') == 1 and file_name == 'requirements.txt':
            file_id = doc['file_id']
            file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
            
            if file_info and file_info.get('ok'):
                file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
                
                try:
                    with urllib.request.urlopen(file_url) as response:
                        requirements_text = response.read().decode('utf-8')
                    
                    set_user_step(user_id, 'awaiting_env', waiting_for_env=1, waiting_for_reqs=0,
                                 temp_file=user_step.get('temp_file'), requirements=requirements_text,
                                 env_vars=user_step.get('env_vars', {}),
                                 plan=user_step.get('plan'), duration=user_step.get('duration'),
                                 cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                                 payment_method=user_step.get('payment_method'))
                    
                    send_message(chat_id,
                        f"**✅ Requirements received!**\n\n"
                        f"```\n{requirements_text[:500]}\n```\n\n"
                        f"**🔧 ENVIRONMENT VARIABLES (Optional)**\n\n"
                        f"Send KEY=VALUE (one per line). All variables will be available via `os.environ.get('KEY')`\n\n"
                        f"Or use buttons:",
                        get_env_keyboard(0))
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {e}")
            return
        
        # Python file for deployment
        if user_step.get('step') == 'awaiting_file':
            if file_name.endswith('.py'):
                send_message(chat_id, "📥 **Downloading file...**", None)
                
                file_id = doc['file_id']
                file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
                
                if file_info and file_info.get('ok'):
                    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
                    temp_file = BASE_DIR / f"temp_{user_id}_{file_name}"
                    
                    try:
                        with urllib.request.urlopen(file_url) as response:
                            with open(temp_file, 'wb') as f:
                                f.write(response.read())
                        
                        send_message(chat_id, f"✅ File `{file_name}` received! ({format_file_size(file_size)})")
                        
                        set_user_step(user_id, 'awaiting_reqs', temp_file=str(temp_file),
                                     plan=user_step.get('plan'), duration=user_step.get('duration'),
                                     cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                                     payment_method=user_step.get('payment_method'),
                                     env_vars={})
                        
                        send_message(chat_id,
                            f"**✅ File received:** `{file_name}`\n\n"
                            f"📋 Plan: {user_step.get('plan', 'monthly').upper()}\n"
                            f"⏱️ Duration: {user_step.get('duration', 30)} days\n"
                            f"📦 File size: {format_file_size(file_size)}\n"
                            f"💰 Cost: {user_step.get('cost_stars')}⭐ or {user_step.get('cost_coins')}🪙\n\n"
                            f"**📦 Requirements?**\n"
                            f"Send requirements.txt or click Auto-detect:",
                            get_reqs_keyboard())
                    except Exception as e:
                        send_message(chat_id, f"❌ Error downloading: {str(e)}")
                else:
                    send_message(chat_id, "❌ Failed to download file.")
            else:
                send_message(chat_id, "❌ Please send a `.py` Python file")
            return
        else:
            send_message(chat_id, "❌ Please start deployment using the menu first.", get_main_menu(user_id))
        return
    
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, first_name, None)
    else:
        send_message(chat_id, "❌ Please use the buttons below.", get_main_menu(user_id))

# ==================== PAYMENT HANDLERS ====================
def handle_successful_payment(message):
    try:
        user_id = message['from']['id']
        chat_id = message['chat']['id']
        successful_payment = message['successful_payment']
        total_amount = successful_payment.get('total_amount', 0)
        invoice_payload = successful_payment.get('invoice_payload', '')
        
        print(f"💰 Payment: {total_amount}⭐ from user {user_id}")
        
        if invoice_payload.startswith("premium_"):
            parts = invoice_payload.split("_")
            plan = parts[1]
            
            if plan == "monthly":
                activate_premium(user_id, "monthly", total_amount, total_amount * STARS_PER_COIN, 30)
                send_message(chat_id, f"✅ **PREMIUM ACTIVATED!**\n\nMonthly plan active for 30 days!\nThank you! 🙏")
            elif plan == "yearly":
                activate_premium(user_id, "yearly", total_amount, total_amount * STARS_PER_COIN, 365)
                send_message(chat_id, f"✅ **PREMIUM ACTIVATED!**\n\nYearly plan active for 365 days!\nThank you! 🙏")
            return
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''SELECT temp_file, requirements, env_vars, plan, duration, cost_coins, cost_stars, payment_method 
                     FROM pending_deployments 
                     WHERE user_id = ? AND payload = ? AND status = 'pending'
                     ORDER BY id DESC LIMIT 1''', (user_id, invoice_payload))
        pending = c.fetchone()
        
        if pending:
            temp_file, requirements, env_vars_json, plan, duration, cost_coins, cost_stars, payment_method = pending
            env_vars = json.loads(env_vars_json) if env_vars_json else {}
            
            update_user_stars(user_id, total_amount, "deployment", "telegram_stars", invoice_payload)
            conn.commit()
            
            deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements, env_vars, 
                            plan, duration, cost_coins, cost_stars, payment_method)
            
            c.execute('UPDATE pending_deployments SET status = "completed" WHERE payload = ?', (invoice_payload,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Payment error: {e}")

def handle_pre_checkout_query(pre_checkout_query):
    try:
        query_id = pre_checkout_query['id']
        url = f"{TELEGRAM_API}/answerPreCheckoutQuery"
        data = {"pre_checkout_query_id": query_id, "ok": True}
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8')).get('ok', False)
    except Exception as e:
        print(f"❌ Pre-checkout error: {e}")
        return False

# ==================== HEALTH CHECK SERVER ====================
def start_health_server():
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"healthy","version":"2.0","timestamp":"' + datetime.now().isoformat().encode() + b'"}')
            def log_message(self, format, *args):
                pass
        port = int(os.environ.get("PORT", 8080))
        server = HTTPServer(('0.0.0.0', port), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"✅ Health check server on port {port}")
    except Exception as e:
        print(f"⚠️ Health server: {e}")

# ==================== MAIN ====================
def main():
    global LAST_UPDATE_ID
    
    print("=" * 70)
    print("╔═════════════════════════════════════════════════════════════════════╗")
    print("║         UNIVERSAL BOT HOSTING PLATFORM - ENTERPRISE EDITION         ║")
    print("╚═════════════════════════════════════════════════════════════════════╝")
    print("=" * 70)
    print(f"📍 Platform: {'Render' if IS_RENDER else 'Heroku' if IS_HEROKU else 'Choreo' if IS_CHOREO else 'Android' if IS_ANDROID else 'Local'}")
    print(f"📁 Data Directory: {BASE_DIR}")
    print(f"💰 Monthly: {PRICE_MONTHLY_STARS}⭐ | Yearly: {PRICE_YEARLY_STARS}⭐")
    print(f"🆓 Free Tier: {FREE_USER_MAX_DEPLOYMENTS} x {FREE_DEPLOYMENT_DURATION_HOURS}h")
    print(f"📦 Max File Size: {MAX_FILE_SIZE_MB}MB")
    print(f"🐍 Python Version: {platform.python_version()}")
    print("=" * 70)
    print("✨ ENHANCED FEATURES:")
    print("   ✓ Supports ANY Python bot (Telegram, Discord, Flask, FastAPI, etc.)")
    print("   ✓ ALL dependency types (PyPI, Git, Mercurial, Subversion, wheel, egg)")
    print("   ✓ Auto-dependency detection from imports")
    print("   ✓ Progress bars for installations")
    print("   ✓ Environment variable injection with .env support")
    print("   ✓ Premium/Free tier with Stars/Coins")
    print("   ✓ Persistent storage with user file management")
    print("   ✓ Framework detection and optimized launcher")
    print("=" * 70)
    
    init_db()
    
    # Start health check server for cloud platforms
    if IS_RENDER or IS_HEROKU or IS_CHOREO:
        start_health_server()
    
    try:
        me = http_get(f"{TELEGRAM_API}/getMe")
        if me and me.get('ok'):
            print(f"✅ Bot: @{me['result']['username']}")
        else:
            print("❌ Check BOT_TOKEN!")
            return
    except Exception as e:
        print(f"❌ Error: {e}")
        return
    
    print("=" * 70)
    print("✅ Bot running! Press Ctrl+C to stop")
    print("=" * 70)
    
    while True:
        try:
            params = {"offset": LAST_UPDATE_ID + 1, "timeout": 30}
            data = http_get(f"{TELEGRAM_API}/getUpdates", params)
            
            if data and data.get('ok'):
                for update in data['result']:
                    LAST_UPDATE_ID = update['update_id']
                    if 'callback_query' in update:
                        handle_callback(update['callback_query'])
                    elif 'message' in update:
                        handle_message(update['message'])
                    elif 'pre_checkout_query' in update:
                        handle_pre_checkout_query(update['pre_checkout_query'])
                    elif 'successful_payment' in update:
                        handle_successful_payment(update)
            sleep(0.5)
        except KeyboardInterrupt:
            print("\n🛑 Bot stopped")
            break
        except Exception as e:
            print(f"⚠️ Error: {e}")
            traceback.print_exc()
            sleep(5)

if __name__ == "__main__":
    main()
