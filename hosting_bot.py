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
import time
import threading
import secrets
import string
import re
import signal
import platform
import traceback
import hashlib
import zipfile
import hmac
import base64
import tempfile
import random
import queue

# ========== CONFIGURATION ==========
# Get configuration from environment variables (SECURE)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    for _alias in ("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN", "API_TOKEN", "TG_BOT_TOKEN"):
        BOT_TOKEN = os.environ.get(_alias, "")
        if BOT_TOKEN:
            os.environ["BOT_TOKEN"] = BOT_TOKEN
            print(f"ℹ️  BOT_TOKEN set from {_alias}")
            break
if not BOT_TOKEN:
    print("⚠️  WARNING: No bot token found. Set BOT_TOKEN env var before deploying.")
    BOT_TOKEN = "MISSING_TOKEN"

# Get admin IDs from environment (comma-separated, robust parsing)
admin_ids_str = os.environ.get("ADMIN_IDS", "7713987088")
ADMIN_IDS = set()
for _x in admin_ids_str.replace(";", ",").split(","):
    try:
        if _x.strip(): ADMIN_IDS.add(int(_x.strip()))
    except ValueError:
        pass
if not ADMIN_IDS:
    ADMIN_IDS = {7713987088}
    print("⚠️  ADMIN_IDS not set or invalid — using default admin ID")

def is_admin(user_id) -> bool:
    """Return True if user_id is a configured admin. Tolerant of str/int input."""
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False

# Channel verification settings (configure via env)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@NCK_Dev")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/NCK_Dev")

# Extra channels every user must ALSO join (in addition to REQUIRED_CHANNEL above).
_EXTRA_FORCE_JOIN = [
    ("@ERRORX404E",   "https://t.me/ERRORX404E"),
    ("@NCKDEVFreeSC", "https://t.me/NCKDEVFreeSC"),
]


def _build_required_channels():
    """
    The full force-join list: REQUIRED_CHANNEL (may itself be comma-separated) followed by
    the extra channels. If REQUIRED_CHANNEL is blank/None/false/0 the gate is deliberately
    OFF and stays off — the extras never switch it back on by themselves.
    Returns [{"id": "@handle-or-numeric-id", "link": "https://t.me/..."}, ...]
    """
    primary = [p.strip() for p in (REQUIRED_CHANNEL or '').split(',')]
    primary = [p for p in primary if p and p not in ('None', 'false', '0')]
    if not primary:
        return []
    out, seen = [], set()

    def add(handle, link=''):
        handle = handle.strip()
        if not handle.startswith('@') and not handle.lstrip('-').isdigit():
            handle = '@' + handle
        if handle.lower() in seen:
            return
        seen.add(handle.lower())
        if not link and handle.startswith('@'):
            link = f"https://t.me/{handle[1:]}"
        out.append({"id": handle, "link": (link or '').strip()})

    for i, h in enumerate(primary):
        add(h, CHANNEL_LINK if i == 0 else '')
    for h, l in _EXTRA_FORCE_JOIN:
        add(h, l)
    return out


REQUIRED_CHANNELS = _build_required_channels()
# Identifies WHICH set of channels a user verified against. When the list changes (as it just
# did), everyone's stored signature stops matching and they are asked to verify once more.
CHANNEL_SIGNATURE = ','.join(sorted(c["id"].lower() for c in REQUIRED_CHANNELS))

# Platform detection
IS_RENDER = os.environ.get("RENDER") == "true"
IS_HEROKU = os.environ.get("HEROKU") == "true"
IS_CHOREO = os.environ.get("CHOREO") == "true"
IS_ANDROID = 'pydroid' in sys.executable.lower() or 'termux' in sys.executable.lower()

# Set base directory based on platform
# ── Persistent disk override ──────────────────────────────────────────
# The paths below (e.g. /opt/render/project/src/...) live INSIDE the app's
# own source checkout. On Render (and most PaaS), that directory is rebuilt
# from git on every deploy and is not guaranteed to survive even a plain
# restart — only an explicitly attached persistent Disk, mounted at a fixed
# path, actually survives redeploys. If PERSISTENT_DISK_PATH is set (point
# it at your Render Disk's mount path, e.g. /var/data), we use that instead
# so deployment files, packages and the database genuinely persist.
_persistent_override = os.environ.get("PERSISTENT_DISK_PATH", "").strip()
if _persistent_override:
    BASE_DIR = Path(_persistent_override) / "bot_hosting_data"
elif IS_RENDER:
    BASE_DIR = Path("/opt/render/project/src/bot_hosting_data")
elif IS_HEROKU:
    BASE_DIR = Path("/app/bot_hosting_data")
elif IS_CHOREO:
    BASE_DIR = Path("/choreo/app/bot_hosting_data")
elif IS_ANDROID:
    BASE_DIR = Path("/storage/emulated/0/bot_hosting_data")
else:
    BASE_DIR = Path("./bot_hosting_data")

USING_PERSISTENT_DISK = bool(_persistent_override)

# Always make this absolute. A relative BASE_DIR (the "./bot_hosting_data"
# local/default case) breaks subprocess.run([str(script)], cwd=str(folder)):
# POSIX resolves a *relative* executable path against the CHILD's new cwd,
# not the parent's, so the same relative path ends up looked up twice-nested
# and every deploy/restart launch fails with a misleading FileNotFoundError.
BASE_DIR = BASE_DIR.resolve()

DEPLOYMENTS_DIR = BASE_DIR / "deployments"
DATABASE_FILE = BASE_DIR / "hosting_bot.db"
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

# ── Resource tiers: a purchased, guaranteed RAM allocation for 30 days ─────────────────────
# key -> (MB, label, stars). Override prices with TIER_PRICE_<KEY>_STARS (e.g. TIER_PRICE_1GB_STARS=450).
# Coins price = stars x STARS_PER_COIN. A tier also includes 30 days of Monthly Premium.
RESOURCE_TIER_DAYS = 30
TIER_YEARLY_MULTIPLIER = int(os.environ.get("TIER_YEARLY_MULTIPLIER", 10))   # yearly = monthly price x 10 (2 months free)
TIER_PERIODS = {"m": ("Monthly", 30, 1), "y": ("Yearly", 365, TIER_YEARLY_MULTIPLIER)}   # code -> (label, days, price multiplier)
def _tier_price(key, default):
    try:
        return int(os.environ.get(f"TIER_PRICE_{key.upper()}_STARS", default))
    except ValueError:
        return default
RESOURCE_TIERS = {
    "512mb": (512,  "512 MB", _tier_price("512mb", 30)),
    "1gb":   (1024, "1 GB",   _tier_price("1gb",   50)),
    "2gb":   (2048, "2 GB",   _tier_price("2gb",   90)),
    "4gb":   (4096, "4 GB",   _tier_price("4gb",  160)),
}
# Share of the pool that may be sold as tiers (default 80%). A size is on sale only while the
# sellable, UNALLOCATED RAM can cover it (so when less than 512 MB is sellable, nothing is purchasable).
# The other 20% is always shared by free users. Set to 1.0 to sell the whole pool.
TIER_POOL_MAX_FRACTION = float(os.environ.get("TIER_POOL_MAX_FRACTION", 0.8))

# Referral reward
REFERRAL_REWARD_COINS = int(os.environ.get("REFERRAL_REWARD_COINS", 50))

# ── GitHub Backup ─────────────────────────────────────────────────────────────
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO_OWNER   = os.environ.get("GITHUB_REPO_OWNER", "")
GITHUB_REPO_NAME    = os.environ.get("GITHUB_REPO_NAME", "")
GITHUB_BACKUP_BRANCH= os.environ.get("GITHUB_BACKUP_BRANCH", "main")
GITHUB_BACKUP_PATH  = os.environ.get("GITHUB_BACKUP_PATH", "backups/hosting_bot.db")
GITHUB_ENABLED      = bool(GITHUB_TOKEN and GITHUB_REPO_OWNER and GITHUB_REPO_NAME)

# ── GitHub FILES repo (every user's deployment files) ─────────────────────────
# A second, separate repository (same owner/token/branch as the DB backup) that
# stores, per deployment:  main file · requirements.txt / package.json · .env
# · the bot's own .db file(s) · manifest.json.  It MUST be a PRIVATE repo — the
# .env files hold users' bot tokens — and the bot refuses to write to a public one.
GITHUB_FILES_REPO_NAME = os.environ.get("GITHUB_FILES_REPO_NAME", "").strip()
FILES_REPO_ENABLED     = bool(GITHUB_TOKEN and GITHUB_REPO_OWNER and GITHUB_FILES_REPO_NAME)

# Tuning constants (deliberately not env vars — the env surface stays as documented)
GITHUB_API_BASE         = "https://api.github.com"
FILES_REPO_ROOT         = "deployments"   # repo layout: deployments/<user_id>/<deployment_id>/…
FILES_MAX_DB_MB         = 50              # per .db file   (GitHub warns >50 MB, refuses >100 MB)
FILES_MAX_SRC_FILE_MB   = 5               # per source/data file
FILES_MAX_TOTAL_MB      = 40              # per deployment, everything combined
FILES_SYNC_INTERVAL_SEC = 1800            # periodic push of changed files (30 min)
FILES_BACKUP_CHUNK      = 20              # deployments per commit during "backup all"
FILES_REPO_SQUASH_EVERY = 50              # collapse history every N commits so DB snapshots
                                          # can't bloat the repo forever (0 = keep full history)

# Relaunch queue (restarts run one-at-a-time so RAM is not spiked)
RELAUNCH_WORKERS        = 1
RELAUNCH_STAGGER_SEC    = 3               # pause between two relaunches
RELAUNCH_MIN_FREE_MB    = 256             # wait for this much free RAM before starting the next bot
RELAUNCH_MEM_WAIT_MAX   = 120             # …but never wait longer than this (seconds)

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
# PYTHON_VERSION is the version the host (Render etc.) is told to build with. Bots' packages
# are installed with THIS interpreter, so a mismatch means restored deployments may be
# rebuilt against a different Python than the one the host config promises.
_WANT_PY = os.environ.get("PYTHON_VERSION", "").strip()
if _WANT_PY:
    _have_py = platform.python_version()
    _want_parts = _WANT_PY.split(".")
    if _have_py.split(".")[:len(_want_parts)] == _want_parts:
        print(f"✅ PYTHON_VERSION={_WANT_PY} matches the running interpreter")
    else:
        print(f"⚠️  PYTHON_VERSION={_WANT_PY} but the running interpreter is {_have_py} — "
              f"the host is not using the version you configured")
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
        last_expiry_notification TEXT,
        referral_code TEXT UNIQUE,
        referred_by INTEGER DEFAULT NULL,
        total_referrals INTEGER DEFAULT 0,
        pending_json TEXT DEFAULT '{}'
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
        dependencies_installed TEXT,
        folder_name TEXT,
        source_type TEXT DEFAULT 'upload',
        github_repo TEXT,
        github_branch TEXT DEFAULT 'main'
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS expiry_reminders (
        deployment_id INTEGER,
        expire_time TEXT,
        stage INTEGER,
        sent_at TEXT,
        PRIMARY KEY (deployment_id, expire_time, stage)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS deploy_credits (
        credit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan TEXT,
        seconds_left INTEGER,
        source_deployment INTEGER,
        created_at TEXT,
        used INTEGER DEFAULT 0
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
    c.execute('''CREATE TABLE IF NOT EXISTS bug_reports (
        report_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        message TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        admin_reply TEXT,
        replied_by INTEGER,
        created_at TEXT,
        replied_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL UNIQUE,
        created_at TEXT,
        reward_coins INTEGER DEFAULT 0,
        reward_given INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS security_reviews (
        review_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        chat_id       INTEGER NOT NULL,
        file_name     TEXT NOT NULL,
        file_path     TEXT,
        critical_json TEXT,
        warnings_json TEXT,
        report_text   TEXT,
        source        TEXT DEFAULT 'upload',
        status        TEXT DEFAULT 'pending',
        reviewed_by   INTEGER,
        created_at    TEXT,
        reviewed_at   TEXT,
        user_step_json TEXT
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sec_reviews_status ON security_reviews(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sec_reviews_user   ON security_reviews(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_bug_reports_user   ON bug_reports(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)')
    
    # Migrations for existing databases
    for _col, _type in [
        ("folder_name",    "TEXT"),
        ("referral_code",  "TEXT"),
        ("referred_by",    "INTEGER"),
        ("total_referrals","INTEGER DEFAULT 0"),
        ("pending_json",   "TEXT DEFAULT '{}'"),
        ("channels_verified", "TEXT"),
        ("resource_tier_key",     "TEXT"),
        ("resource_tier_mb",      "INTEGER DEFAULT 0"),
        ("resource_tier_expires", "TEXT"),
        ("is_banned",     "INTEGER DEFAULT 0"),
        ("ban_reason",    "TEXT"),
        ("banned_at",     "TEXT"),
        ("banned_by",     "INTEGER"),
    ]:
        try:
            c.execute(f"ALTER TABLE deployments ADD COLUMN {_col} {_type}" if _col == "folder_name"
                      else f"ALTER TABLE users ADD COLUMN {_col} {_type}")
        except Exception:
            pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN tos_accepted INTEGER DEFAULT 0")
    except Exception:
        pass

    for _col, _type in [("source_type","TEXT DEFAULT 'upload'"),
                        ("github_repo", "TEXT"),
                        ("github_branch","TEXT DEFAULT 'main'"),
                        ("crash_restart_count", "INTEGER DEFAULT 0"),
                        ("last_crash_restart", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE deployments ADD COLUMN {_col} {_type}")
        except Exception:
            pass

    # security_reviews — backfill user_step_json for older DBs
    try:
        c.execute("ALTER TABLE security_reviews ADD COLUMN user_step_json TEXT")
    except Exception:
        pass

    _fp_init_tables(c)
    conn.commit()
    conn.close()
    print("✅ Database initialized with enhanced schema")

# ========== DEPLOY FOLDER HELPER ==========
def kill_deployment_process(pid, sig=None):
    """
    Terminate a deployment's process AND anything it spawned as children.

    A deployment's launcher (run.py) often runs the actual bot as a
    subprocess (Method 2 execution — the fallback when the launcher can't
    find a recognizable entry point to import directly, which is common).
    That child process shares the launcher's process group by default.
    Killing only the launcher's own PID (os.kill) leaves that child running
    forever, completely orphaned and untracked — silently consuming RAM/CPU
    with no record of what it even is. Killing the whole process GROUP
    instead takes the launcher and everything it spawned down together.

    Critical safety check: a target's process group must NEVER be killed if
    it matches OUR OWN group. Every deployment spawned via the current
    (setsid-based) start.sh gets its own isolated group, so this should
    never normally happen for a live deployment — but pre-existing orphans
    from before that fix was in place were spawned WITHOUT setsid, meaning
    they very likely still share the hosting bot's own inherited group.
    Group-killing those would take the whole hosting bot down as collateral
    damage. When target and caller share a group, fall back to a plain
    single-PID kill instead — safe either way, just less thorough about
    orphaned grandchildren in that specific legacy case.
    """
    if sig is None:
        sig = signal.SIGTERM
    if not pid:
        return
    try:
        target_pgid = os.getpgid(pid)
        own_pgid = os.getpgid(os.getpid())
        if target_pgid == own_pgid:
            os.kill(pid, sig)
            return
        os.killpg(target_pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            os.kill(pid, sig)
        except Exception:
            pass


def get_deploy_folder(user_id, deployment_id):
    """Return the correct deploy folder path using folder_name from DB (fixes timestamp vs DB-id mismatch)."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT folder_name, user_id FROM deployments WHERE deployment_id = ?", (deployment_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            owner_id = row[1] if row[1] else user_id
            return DEPLOYMENTS_DIR / str(owner_id) / str(row[0])
    except Exception:
        pass
    # Fallback: old behaviour (folder named by DB id) for records without folder_name
    return DEPLOYMENTS_DIR / str(user_id) / str(deployment_id)


# ==================== SECURITY SCANNER ====================

class SecurityScanner:
    """
    6-Layer Security Scanner for uploaded bot files.

    Layer 1  – AI pattern-based analysis
    Layer 2  – AST-level Python code analysis
    Layer 3  – Encoded / hidden content detection (base64, hex, url-enc)
    Layer 4  – Secure sandbox extraction for archives
    Layer 5  – Permission / path traversal / symlink / data-protection scan
    Layer 6  – Archive bomb & nested extraction protection

    For a hosting platform, patterns are calibrated in two tiers:
    CRITICAL  → deployment blocked entirely
    WARNING   → deployment proceeds with a notification to user and admin
    """

    MAX_FILES        = 1_000
    MAX_TOTAL_SIZE   = 100 * 1024 * 1024   # 100 MB extracted
    MAX_RECURSION    = 4
    B64_MIN_LEN      = 60   # ignore short base64 strings to cut false positives

    # ── CRITICAL patterns (block deployment) ──────────────────────────
    CRITICAL_PYTHON = [
        r'eval\s*\(\s*base64\.b64decode',            # obfuscated eval
        r'exec\s*\(\s*base64\.b64decode',            # obfuscated exec
        r'exec\s*\(\s*__import__\s*\(',              # import+exec combo
        r'stratum\+tcp',                              # crypto miner
        r'xmrig|minergate|nicehash|coinhive',         # crypto miner names
        r'socket\.connect\([^)]+\).*exec\(',          # reverse shell
        r'\.connect\(\(\s*["\'][0-9]+\.[0-9]+',      # hardcoded IP socket
        r"os\.system\s*\(\s*['\"]rm\s+-rf\s+/",      # rm -rf /
        r"shutil\.rmtree\s*\(\s*['\"][/\\]",         # rmtree on root
        r'urllib\.request\.urlopen.*base64\.b64decode',  # fetch+decode
    ]
    CRITICAL_JS = [
        r'stratum\+tcp',
        r'xmrig|minergate|coinhive',
        r'child_process.*exec.*base64',
        r'eval\s*\(\s*Buffer\.from',
        r'exec\s*\(\s*require\s*\(\s*["\']child_process',
    ]
    CRITICAL_PKG_HOOKS = [
        r'curl\s+http',
        r'wget\s+http',
        r'\|\s*bash',
        r'\|\s*sh\b',
        r'python\s+-c\s+["\']import',
        r'node\s+-e\s+',
        r'base64\s+--decode',
        r'chmod\s+\+x',
    ]

    # ── WARNING patterns (proceed with alert) ────────────────────────
    WARN_PYTHON = [
        r'\beval\s*\(',
        r'\bexec\s*\(',
        r'subprocess\.(Popen|call|check_output|run)\s*\(',
        r'os\.(system|popen)\s*\(',
        r'os\.(remove|unlink)\s*\(',
        r'shutil\.rmtree\s*\(',
        r'pickle\.loads\s*\(',
        r'marshal\.loads\s*\(',
        r'__import__\s*\(',
    ]
    WARN_JS = [
        r'\beval\s*\(',
        r'new\s+Function\s*\(',
        r'child_process\.(exec|spawn|execSync|spawnSync)\s*\(',
        r'fs\.(unlink|rmdir|rmdirSync|unlinkSync)\s*\(',
        r'require\s*\(\s*["\']child_process["\']\s*\)',
    ]

    # ── File types → scanner map ──────────────────────────────────────
    SUPPORTED_BOT_EXTS = {
        '.py':  'python',
        '.js':  'javascript',
        '.mjs': 'javascript',
        '.cjs': 'javascript',
        '.ts':  'typescript',
        '.tsx': 'typescript',
        '.jsx': 'javascript',
    }
    ARCHIVE_EXTS = {'.zip', '.tar', '.gz', '.tgz', '.7z', '.rar'}

    def __init__(self):
        self._reset()

    def _reset(self):
        self.critical = []
        self.warnings = []

    # ── Public API ────────────────────────────────────────────────────
    def scan(self, file_bytes: bytes, filename: str) -> tuple[bool, list, list]:
        """
        Returns (blocked, critical_list, warning_list).
        blocked=True means the file should NOT be deployed.
        """
        self._reset()
        filename = filename or 'unknown'
        ext = os.path.splitext(filename)[1].lower()
        if ext in self.ARCHIVE_EXTS:
            self._scan_archive(file_bytes, filename)
        else:
            c, w = self._scan_file(file_bytes, filename)
            self.critical.extend(c)
            self.warnings.extend(w)
        blocked = bool(self.critical)
        return blocked, self.critical[:], self.warnings[:]

    # ── Archive handling ──────────────────────────────────────────────
    def _scan_archive(self, data: bytes, filename: str):
        import tempfile, shutil, zipfile, tarfile, io as _io
        ext      = os.path.splitext(filename)[1].lower()
        tmp_dir  = tempfile.mkdtemp(prefix='sec_scan_')
        try:
            if ext == '.zip':
                with zipfile.ZipFile(_io.BytesIO(data)) as zf:
                    self._check_zip(zf)
                    zf.extractall(tmp_dir)
            elif ext in ('.tar', '.gz', '.tgz'):
                with tarfile.open(fileobj=_io.BytesIO(data), mode='r:*') as tf:
                    self._check_tar(tf)
                    tf.extractall(tmp_dir)
            elif ext == '.rar':
                try:
                    import rarfile
                    with rarfile.RarFile(_io.BytesIO(data)) as rf:
                        self._check_rar(rf)
                        rf.extractall(tmp_dir)
                except ImportError:
                    self.warnings.append("RAR archive: cannot deep-scan (rarfile not installed)")
                    return
            elif ext == '.7z':
                try:
                    import py7zr
                    with py7zr.SevenZipFile(_io.BytesIO(data)) as sz:
                        sz.extractall(tmp_dir)
                except ImportError:
                    self.warnings.append("7z archive: cannot deep-scan (py7zr not installed)")
                    return

            for root, _dirs, files in os.walk(tmp_dir):
                for fname in files:
                    fpath    = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, tmp_dir)
                    if os.path.islink(fpath):
                        self.critical.append(f"Symlink in archive: `{rel_path}`")
                        continue
                    if '..' in rel_path or rel_path.startswith('/'):
                        self.critical.append(f"Path traversal in archive: `{rel_path}`")
                        continue
                    try:
                        with open(fpath, 'rb') as fp:
                            content = fp.read()
                        c, w = self._scan_file(content, fname, rel_path)
                        self.critical.extend(c)
                        self.warnings.extend(w)
                    except Exception as e:
                        self.warnings.append(f"Could not scan `{rel_path}`: {e}")
        except Exception as e:
            self.critical.append(f"Archive error: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Single file dispatcher ────────────────────────────────────────
    def _scan_file(self, data: bytes, filename: str, rel: str = None) -> tuple[list, list]:
        critical, warnings = [], []
        ext  = os.path.splitext(filename)[1].lower()
        path = rel or filename

        # Path traversal guard
        if rel and ('..' in rel or rel.startswith('/')):
            critical.append(f"Path traversal: `{rel}`")
            return critical, warnings

        try:
            text = data.decode('utf-8', errors='ignore')
        except Exception:
            text = ''

        # Layer 3: encoded content
        ec, ew = self._check_encoded(text, path)
        critical.extend(ec); warnings.extend(ew)

        # Layer 1+2: language-specific
        lang = self.SUPPORTED_BOT_EXTS.get(ext, '')
        if lang == 'python':
            c, w = self._scan_python(data, text, path)
            critical.extend(c); warnings.extend(w)
            critical.extend(self._scan_data_access(text, path, 'python'))
        elif lang in ('javascript', 'typescript'):
            c, w = self._scan_js(text, path)
            critical.extend(c); warnings.extend(w)
            critical.extend(self._scan_data_access(text, path, 'js'))
        elif filename.lower() == 'package.json':
            c, w = self._scan_package_json(text)
            critical.extend(c); warnings.extend(w)
        elif text:
            c, w = self._scan_generic(text, path)
            critical.extend(c); warnings.extend(w)

        # Layer 5: sensitive file names
        base = os.path.basename(filename).lower()
        if base in ('credentials.json', 'service_account.json', 'gcp_key.json'):
            warnings.append(f"Sensitive credential file: `{path}`")

        return critical, warnings

    # ── Data-theft scanner: code that goes after OTHER users' data / the host's database ────────
    # Every finding is prefixed "[DATA-THEFT]" — such files are REJECTED outright (no admin review).
    _ABS_SENSITIVE = (r'(?:/proc/|/etc/(?:passwd|shadow|sudoers|hosts\.allow)|/root\b|/home\b|/opt\b|/app\b|'
                      r'/srv\b|/mnt\b|/data\b|/var\b|/usr/src\b|~/\.ssh|\.aws/credentials|\.git-credentials)')
    _THEFT_COMMON = [
        # another process' environment / command line / memory (leaks other users' secrets)
        (r'/proc/(?:self|\d+|\*|\{[^}]*\}|["\'`]\s*\+|%[sd])[^\n]{0,30}(?:environ|cmdline|maps|mem|fd\b|cwd|root)',
         "reads another process' environment/memory (/proc)"),
        (r'/proc/[^\s"\'`)]*/(?:environ|cmdline|mem|maps)', "reads another process' environment/memory (/proc)"),
        (r'/etc/(?:passwd|shadow|sudoers)', "reads system account files"),
        (r'(?:~|/root|/home/[^/\s"\']+)/\.(?:ssh|aws|git-credentials|netrc|docker|kube)', "reads credential directories"),
        # shell commands that read outside the bot's own folder
        (r'\b(?:cat|ls|find|tar|cp|zip|sqlite3|strings|grep|rsync|scp|curl\s+-F|base64)\s+(?:-[\w-]+\s+)*["\']?'
         r'(?:\.\./|/proc/|/etc/(?:passwd|shadow)|/root|/home|/data|/opt/|/app/|/var/|/mnt/|~/)',
         "shell command reading outside the bot's own folder"),
    ]
    _THEFT_COMMON += [
        # other deployed bots' runtime files, reached by an absolute / ../ path
        (r'["\'`](?:/|\.\./)[^"\'`\n]*(?:\.env\b|pid\.txt|output\.log|start\.sh|/run\.py|\.home/|\.tmp/|pending_review_)',
         "reads another deployed bot's files (.env / logs / launcher)"),
        # probing other bots' web ports on this machine (the platform hands out ports from 20000 up)
        (r'(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])\s*:\s*(?:2\d{4}|[3-6]\d{4})\b',
         "connects to another deployed bot's local port"),
        (r'\bconnect_ex\s*\(|\b(?:nmap|masscan)\b', "port-scans this machine"),
        (r'\bpsutil\.(?:pids|net_connections|wait_procs|pid_exists)\b|\bpsutil\.Process\s*\(\s*\d+',
         "inspects other deployed bots' processes"),
    ]
    _THEFT_PY = [
        # walking up/out of the bot's own folder
        (r'\b(?:open|listdir|scandir|walk|glob|iglob|rglob|iterdir|read_text|read_bytes|copy2?|copytree|move|'
         r'chdir|connect|send_file|FileInput)\s*\([^)\n]{0,200}["\'](?:\.\./|\.\.[\\/])',
         "path traversal out of the bot's own folder (..)"),
        (r'\bos\.chdir\s*\(\s*["\']\.\.', "changes directory out of the bot's own folder"),
        (r'\bos\.path\.join\s*\([^\n]{0,160}["\']\.\.["\']', "path traversal out of the bot's own folder (..)"),
        (r'\bPath\s*\([^\n]{0,160}["\']\.\.["\']', "path traversal out of the bot's own folder (..)"),
        (r'(?:getcwd\(\)|Path\.cwd\(\)|os\.getcwd\(\))\s*\)?\s*\.?(?:parent|parents)\b[^\n]{0,60}'
         r'(?:listdir|scandir|walk|glob|iterdir|open|connect)', "walks up from its own folder"),
        # listing / globbing absolute locations (other users' folders, temp files, the host's data)
        (r'\b(?:listdir|scandir|walk|glob|iglob|rglob|iterdir)\s*\(\s*(?:Path\s*\(\s*)?["\'](?:/|~)[^"\']*["\']',
         "lists/searches absolute locations outside the bot's own folder"),
        # reading sensitive absolute locations
        (r'\b(?:open|read_text|read_bytes|connect|copy2?|copytree|send_file)\s*\(\s*(?:Path\s*\(\s*)?["\']'
         + _ABS_SENSITIVE, "reads files outside the bot's own folder"),
        # inspecting other processes
        (r'\bpsutil\.process_iter\b', "inspects other users' processes"),
        (r'\.environ\s*\(\s*\)', "reads another process' environment"),
        (r'\bpsutil\.Process\s*\([^)]*\)\s*\.(?:cmdline|environ|cwd|open_files|connections)', "inspects other users' processes"),
        # climbing 4+ levels above the script = out of the deployment into the host's folders
        (r'(?:\.parent\s*){4,}|\.parents\s*\[\s*[3-9]\s*\]|dirname\s*\(\s*(?:os\.path\.)?dirname\s*\(\s*'
         r'(?:os\.path\.)?dirname\s*\(\s*(?:os\.path\.)?dirname\s*\(', "climbs out of its own folder into the host's folders"),
        (r'(?:\.parent\s*){3}[^\n]{0,40}(?:listdir|iterdir|glob|rglob|walk|open|read_text|read_bytes|scandir)',
         "climbs out of its own folder into the host's folders"),
        (r'os\.kill(?:pg)?\s*\(\s*(?:\d+|int\s*\()', "signals another process by number"),
    ]
    _THEFT_JS = [
        (r'\b(?:readFile|readFileSync|readdir|readdirSync|opendir|opendirSync|createReadStream|copyFile|copyFileSync|'
         r'cp|cpSync|rename|renameSync)\s*\(\s*[^)\n]{0,80}["\'`](?:\.\./|\.\.[\\/])',
         "path traversal out of the bot's own folder (..)"),
        (r'\bprocess\.chdir\s*\(\s*["\'`]\.\.', "changes directory out of the bot's own folder"),
        (r'\.(?:join|resolve)\s*\([^\n]{0,160}["\'`]\.\.["\'`]', "path traversal out of the bot's own folder (..)"),
        (r'\b(?:readdir|readdirSync|opendir|opendirSync|glob|globSync)\s*\(\s*["\'`](?:/|~)[^"\'`]*["\'`]',
         "lists/searches absolute locations outside the bot's own folder"),
        (r'\b(?:readFile|readFileSync|createReadStream|copyFile|copyFileSync)\s*\(\s*["\'`]' + _ABS_SENSITIVE,
         "reads files outside the bot's own folder"),
    ]

    def _host_names(self):
        """Names that only exist on the host: its database file, data directory, deployments root."""
        names = set()
        try:
            for n in (DATABASE_FILE.name, BASE_DIR.name, DEPLOYMENTS_DIR.name, USER_FILES_DIR.name, "hosting_bot"):
                if n and len(n) >= 6:
                    names.add(n)
        except Exception:
            names.add("hosting_bot")
        return names

    _VENDOR_DIRS = ("node_modules", "site-packages", "dist-packages", "packages", "venv", ".venv",
                    "vendor", "__pycache__", ".git", ".home", ".tmp", "bower_components")

    def _scan_data_access(self, text: str, path: str, lang: str) -> list:
        """Looks ONLY at what the bot's own code does to reach other deployed bots' data. Which packages
        it imports / lists as dependencies is never a reason to reject, and bundled third-party
        folders (node_modules, site-packages, vendor …) are not examined at all."""
        if not text:
            return []
        parts = re.split(r'[\\/]', str(path).lower())
        if any(d in parts[:-1] for d in self._VENDOR_DIRS):
            return []
        # drop comments so a "../" in a comment isn't flagged
        if lang == 'python':
            body = re.sub(r'(?m)^\s*#.*$', '', text)
            rules = self._THEFT_COMMON + self._THEFT_PY
        else:
            body = re.sub(r'(?m)^\s*//.*$', '', text)
            body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
            rules = self._THEFT_COMMON + self._THEFT_JS
        found = []
        for pat, why in rules:
            try:
                if re.search(pat, body, re.IGNORECASE | re.DOTALL):
                    msg = f"[DATA-THEFT] {why} in `{path}`"
                    if msg not in found:
                        found.append(msg)
            except re.error:
                continue
        # shell tools that list / control other processes or inspect the machine
        tool_re = re.compile(r'["\'`]\s*(?:sudo\s+)?(?:ps|pgrep|pkill|killall|lsof|netstat|ss|fuser|pidof|nsenter|strace|ltrace|gdb|'
                             r'nmap|masscan|docker|kubectl|top|htop)(?:\s[^"\'`\n]*)?["\'`]')
        exec_re = re.compile(r'(?:system|popen|subprocess|Popen|check_output|check_call|\.run|\bexec|execSync|spawn|spawnSync|shell)\b')
        for line in body.splitlines():
            if exec_re.search(line) and tool_re.search(line):
                found.append(f"[DATA-THEFT] runs a command that lists/controls other deployed bots' processes in `{path}`")
                break
        for name in self._host_names():
            if re.search(r'["\'`][^"\'`\n]*' + re.escape(name) + r'[^"\'`\n]*["\'`]', body):
                found.append(f"[DATA-THEFT] references the host's database / data folder (`{name}`) in `{path}`")
        return found

    # ── Python scanner (Layer 1 + 2) ─────────────────────────────────
    def _scan_python(self, data: bytes, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []

        # Regex critical
        for pat in self.CRITICAL_PYTHON:
            if re.search(pat, text, re.IGNORECASE | re.DOTALL):
                critical.append(f"[PY-CRITICAL] `{pat}` in `{path}`")

        # Regex warnings
        for pat in self.WARN_PYTHON:
            if re.search(pat, text, re.IGNORECASE):
                warnings.append(f"[PY-WARN] `{pat}` in `{path}`")

        # AST analysis
        try:
            import ast as _ast
            tree = _ast.parse(text, mode='exec')
            for node in _ast.walk(tree):
                if not isinstance(node, _ast.Call):
                    continue
                func = node.func

                # Name calls: eval(), exec(), __import__()
                if isinstance(func, _ast.Name):
                    name = func.id
                    if name in ('eval', 'exec'):
                        # Check if argument is base64 (obfuscation)
                        if node.args and isinstance(node.args[0], _ast.Call):
                            critical.append(f"[AST] Obfuscated `{name}()` call in `{path}`")
                        else:
                            warnings.append(f"[AST] `{name}()` call in `{path}`")

                # Attribute calls: os.system(), subprocess.Popen() etc.
                elif isinstance(func, _ast.Attribute):
                    attr     = func.attr
                    # Safely get the object name
                    obj_name = ''
                    if isinstance(func.value, _ast.Name):
                        obj_name = func.value.id

                    # Destructive OS ops
                    if attr in ('remove', 'unlink', 'rmdir') and obj_name == 'os':
                        warnings.append(f"[AST] `os.{attr}()` in `{path}`")
                    elif attr == 'rmtree' and obj_name == 'shutil':
                        warnings.append(f"[AST] `shutil.rmtree()` in `{path}`")
                    elif attr in ('Popen', 'call', 'check_output', 'run') and obj_name == 'subprocess':
                        warnings.append(f"[AST] `subprocess.{attr}()` in `{path}`")
                    elif attr in ('system', 'popen') and obj_name == 'os':
                        warnings.append(f"[AST] `os.{attr}()` in `{path}`")

                # Dangerous imports
                if isinstance(node, _ast.ImportFrom):
                    if node.module in ('subprocess', 'os'):
                        for alias in (node.names or []):
                            if alias.name in ('system', 'popen', 'remove', 'unlink',
                                              'Popen', 'call', 'check_output', 'rmtree'):
                                warnings.append(f"[AST] Dangerous import: `from {node.module} import {alias.name}` in `{path}`")

        except SyntaxError as e:
            warnings.append(f"[PY] Syntax error in `{path}`: {e}")
        except Exception:
            pass   # AST parse may fail on dynamic code; regex already covered it

        return critical, warnings

    # ── JavaScript / TypeScript / Node.js scanner ─────────────────────
    def _scan_js(self, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []
        if not text:
            return critical, warnings

        for pat in self.CRITICAL_JS:
            if re.search(pat, text, re.IGNORECASE | re.DOTALL):
                critical.append(f"[JS-CRITICAL] `{pat}` in `{path}`")

        for pat in self.WARN_JS:
            if re.search(pat, text, re.IGNORECASE):
                warnings.append(f"[JS-WARN] `{pat}` in `{path}`")

        # TypeScript-specific: dynamic import with obfuscated string
        if re.search(r'import\s*\(\s*atob\s*\(', text):
            critical.append(f"[TS-CRITICAL] Obfuscated dynamic import in `{path}`")

        # Crypto miner in JS
        if re.search(r'(stratum\+tcp|coinhive|cryptonight)', text, re.IGNORECASE):
            critical.append(f"[JS-CRITICAL] Crypto miner pattern in `{path}`")

        # Hardcoded token/secret
        if re.search(r'(?i)(token|api_key|secret|password)\s*=\s*["\'][A-Za-z0-9_\-]{8,}', text):
            warnings.append(f"[JS] Possible hardcoded credential in `{path}`")

        return critical, warnings

    # ── package.json scanner ──────────────────────────────────────────
    def _scan_package_json(self, text: str) -> tuple[list, list]:
        critical, warnings = [], []
        try:
            pkg = json.loads(text)
        except Exception:
            return critical, warnings

        scripts = pkg.get('scripts', {})
        dangerous_hooks = ('preinstall', 'postinstall', 'prepare',
                           'preuninstall', 'postuninstall')
        for hook in dangerous_hooks:
            cmd = scripts.get(hook, '')
            if not cmd:
                continue
            for pat in self.CRITICAL_PKG_HOOKS:
                if re.search(pat, cmd, re.IGNORECASE):
                    critical.append(
                        f"[NPM-CRITICAL] Dangerous `{hook}` hook: `{cmd[:80]}`")
                    break
            else:
                if cmd.strip():
                    warnings.append(f"[NPM] `{hook}` hook detected: `{cmd[:80]}`")

        return critical, warnings

    # ── Generic scanner ───────────────────────────────────────────────
    def _scan_generic(self, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []
        generic_critical = [
            r'stratum\+tcp', r'xmrig', r'minergate',
            r'rm\s+-rf\s+/',
        ]
        generic_warn = [
            r'\beval\s*\(',
            r'\bexec\s*\(',
            r'child_process',
        ]
        for pat in generic_critical:
            if re.search(pat, text, re.IGNORECASE):
                critical.append(f"[GENERIC-CRITICAL] `{pat}` in `{path}`")
        for pat in generic_warn:
            if re.search(pat, text, re.IGNORECASE):
                warnings.append(f"[GENERIC-WARN] `{pat}` in `{path}`")
        return critical, warnings

    # ── Encoded content detection (Layer 3) ───────────────────────────
    def _check_encoded(self, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []

        # Base64: only scan long strings to avoid noise
        b64_pat = r'[A-Za-z0-9+/]{' + str(self.B64_MIN_LEN) + r',}={0,2}'
        for match in re.findall(b64_pat, text):
            try:
                import base64 as _b64
                decoded = _b64.b64decode(match + '==')
                decoded_text = decoded.decode('utf-8', errors='ignore')
                for pat in self.CRITICAL_PYTHON + self.CRITICAL_JS:
                    if re.search(pat, decoded_text, re.IGNORECASE):
                        critical.append(
                            f"[ENCODED-CRITICAL] Dangerous content hidden in base64 in `{path}`")
                        break
            except Exception:
                pass

        # Hex strings (long)
        hex_pat = r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{80,}(?![0-9A-Fa-f])'
        for match in re.findall(hex_pat, text):
            try:
                decoded_text = bytes.fromhex(match).decode('utf-8', errors='ignore')
                for pat in self.CRITICAL_PYTHON:
                    if re.search(pat, decoded_text, re.IGNORECASE):
                        critical.append(
                            f"[ENCODED-CRITICAL] Dangerous content in hex string in `{path}`")
                        break
            except Exception:
                pass

        # URL-encoded blob (≥3 consecutive %xx tokens = suspicious)
        if re.search(r'(?:%[0-9A-Fa-f]{2}){4,}', text):
            warnings.append(f"[ENCODED-WARN] URL-encoded block detected in `{path}` (possible obfuscation)")

        return critical, warnings

    # ── Archive bomb protection (Layer 6) ────────────────────────────
    def _check_zip(self, zf):
        import zipfile
        total, n = 0, 0
        for info in zf.infolist():
            n += 1
            if n > self.MAX_FILES:
                raise Exception(f"ZIP contains >{self.MAX_FILES} files (archive bomb?)")
            total += info.file_size
            if total > self.MAX_TOTAL_SIZE:
                raise Exception("ZIP extracted size too large (archive bomb?)")
            name = info.filename
            if '..' in name or name.startswith('/') or name.startswith('\\'):
                raise Exception(f"Path traversal in ZIP: {name}")

    def _check_tar(self, tf):
        import tarfile
        total, n = 0, 0
        for m in tf:
            if m.isreg():
                n += 1
                if n > self.MAX_FILES:
                    raise Exception(f"TAR contains >{self.MAX_FILES} files (archive bomb?)")
                total += m.size
                if total > self.MAX_TOTAL_SIZE:
                    raise Exception("TAR extracted size too large (archive bomb?)")
            if m.issym():
                raise Exception(f"Symlink in TAR: {m.name}")
            if '..' in m.name or m.name.startswith('/'):
                raise Exception(f"Path traversal in TAR: {m.name}")

    def _check_rar(self, rf):
        total, n = 0, 0
        for info in rf.infolist():
            if not info.isdir():
                n += 1
                if n > self.MAX_FILES:
                    raise Exception(f"RAR contains >{self.MAX_FILES} files (archive bomb?)")
                total += info.file_size
                if total > self.MAX_TOTAL_SIZE:
                    raise Exception("RAR extracted size too large (archive bomb?)")
            if '..' in info.filename or info.filename.startswith('/'):
                raise Exception(f"Path traversal in RAR: {info.filename}")


SECURITY_SCANNER = SecurityScanner()


def run_security_scan(file_bytes: bytes, filename: str) -> tuple[bool, list, list, str]:
    """
    Wrapper around SecurityScanner.scan().
    Returns (blocked, critical_issues, warnings, report_text).
    `blocked` is True when critical issues are found and deployment must stop.
    """
    try:
        blocked, critical, warnings = SECURITY_SCANNER.scan(file_bytes, filename)
    except Exception as e:
        # Scanner itself crashed — log and let deployment proceed with a warning
        return False, [], [f"Scanner error: {e}"], f"⚠️ Security scan encountered an error: `{safe_md(e)[:200]}`"

    lines = [f"🔒 Security Scan — `{display_filename(filename)}`\n"]
    if critical:
        lines.append(f"🔴 *{len(critical)} CRITICAL issue(s) — BLOCKED:*")
        # Findings are raw regex/pattern text full of * _ [ ] — put each in a code span so
        # Telegram's Markdown parser can't choke on it (only backticks matter inside one).
        for i in critical[:10]:
            lines.append(f"  • `{safe_md(i)[:200]}`")
        if len(critical) > 10:
            lines.append(f"  … and {len(critical)-10} more")
    if warnings:
        lines.append(f"\n🟡 *{len(warnings)} warning(s):*")
        for w in warnings[:8]:
            lines.append(f"  • `{safe_md(w)[:200]}`")
        if len(warnings) > 8:
            lines.append(f"  … and {len(warnings)-8} more")
    if not critical and not warnings:
        lines.append("✅ No issues found — file is clean")

    return blocked, critical, warnings, '\n'.join(lines)


def _safe_remove_fresh_clone(folder, user_id, deploy_id):
    """Delete ONLY the just-created clone of a rejected repo: it must be exactly
    DEPLOYMENTS_DIR/<user>/<deploy_id> and must not belong to any deployment row. Anything else is left alone,
    so a rejection can never touch another bot's files."""
    try:
        folder = Path(folder).resolve()
        expected = (DEPLOYMENTS_DIR / str(user_id) / str(deploy_id)).resolve()
        if folder != expected:
            print(f"⚠️ refusing to delete {folder}: not the fresh clone {expected}")
            return
        if db_execute("SELECT 1 FROM deployments WHERE user_id = ? AND folder_name = ?",
                      (user_id, str(deploy_id)), fetch='one'):
            print(f"⚠️ refusing to delete {folder}: it belongs to an existing deployment")
            return
        shutil.rmtree(folder, ignore_errors=True)
    except Exception as e:
        print(f"⚠️ fresh-clone cleanup skipped: {e}")


# ---------- local files guard: self-heal a deployment whose files vanished from disk ----------
_guard_busy = set()
_guard_last = {}                       # dep_id -> last attempt (epoch)
FILES_GUARD_INTERVAL = 120
FILES_GUARD_COOLDOWN = 900


def files_guard_tick(max_per_cycle=3, now_ts=None):
    """Any non-expired bot whose script is missing locally is restored from its GitHub backup right away
    (no need for the owner to press Restart). Bots that were running are relaunched afterwards.
    Returns the list of deployment ids restored."""
    if not FILES_REPO_ENABLED:
        return []
    now_ts = now_ts if now_ts is not None else time.time()
    rows = db_execute("""SELECT deployment_id, user_id, file_name, status FROM deployments
                         WHERE status IN ('active','stopped','paused') AND file_name IS NOT NULL
                           AND (expire_time IS NULL OR expire_time > ?)""",
                      (datetime.now().isoformat(),), fetch='all') or []
    restored = []
    for dep_id, uid, fname, status in rows:
        if len(restored) >= max_per_cycle:
            break
        try:
            folder = get_deploy_folder(uid, dep_id)
            script = folder / fname
            if script.exists():
                _guard_last.pop(dep_id, None)
                continue
            if dep_id in _guard_busy or now_ts - _guard_last.get(dep_id, 0) < FILES_GUARD_COOLDOWN:
                continue
            if is_banned(uid):
                continue
            _guard_busy.add(dep_id)
            _guard_last[dep_id] = now_ts
            try:
                print(f"♻️ files guard: dep#{dep_id} files missing locally — restoring from GitHub")
                ok, _env = _files_restore_for_relaunch(uid, dep_id, folder, script,
                                                       log=lambda m, _d=dep_id: print(f"   [guard dep#{_d}] {m}"))
            finally:
                _guard_busy.discard(dep_id)
            if ok and script.exists():
                restored.append(dep_id)
                if status == 'active':
                    try:
                        enqueue_relaunch(dep_id, reason="startup", bypass_cap=True)
                    except Exception:
                        pass
                try:
                    send_message(uid, f"♻️ *Bot #{dep_id}*: its files were missing on the server, so they were "
                                      f"restored from your GitHub backup automatically."
                                      + ("\nIt is being started again." if status == 'active' else ""),
                                 {"inline_keyboard": [[{"text": f"📦 Open Bot #{dep_id}", "callback_data": f"view_deploy_{dep_id}"}]]})
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ files guard error (dep#{dep_id}): {e}")
    return restored


def files_guard_monitor():
    sleep(90)
    print("✅ Files guard started (auto-restores bots whose files vanished from disk)")
    while True:
        try:
            files_guard_tick()
        except Exception as e:
            print(f"⚠️ files guard error: {e}")
        sleep(FILES_GUARD_INTERVAL)


def data_theft_findings(critical) -> list:
    """The subset of scanner findings that mean 'trying to reach other users' data / the host's database'."""
    return [c for c in (critical or []) if str(c).startswith("[DATA-THEFT]")]


def scan_repo_data_theft(folder, skip=None, max_files=400) -> list:
    """Scan EVERY .py/.js/.ts file of a cloned repo (not just the entry point) for data-theft code."""
    out = []
    folder = Path(folder)
    skip_dirs = {"packages", "node_modules", "venv", ".venv", "__pycache__", ".git", ".home", ".tmp", "site-packages"}
    n = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SecurityScanner.SUPPORTED_BOT_EXTS:
                continue
            fp = Path(root) / fn
            if skip and fp == skip:
                continue
            n += 1
            if n > max_files:
                return out
            try:
                lang = 'python' if ext == '.py' else 'js'
                text = fp.read_bytes()[:2_000_000].decode('utf-8', errors='ignore')
                out.extend(SECURITY_SCANNER._scan_data_access(text, str(fp.relative_to(folder)), lang))
            except Exception:
                continue
    return out


def reject_data_theft(chat_id, user_id, file_name, findings, message_id=None):
    """Hard reject: no admin review queue. Tell the user, log the incident, alert admins (with a Ban button)."""
    try:
        db_execute("""CREATE TABLE IF NOT EXISTS security_incidents (
            incident_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, file_name TEXT,
            findings_json TEXT, created_at TEXT)""")
        db_execute("INSERT INTO security_incidents (user_id, file_name, findings_json, created_at) VALUES (?,?,?,?)",
                   (user_id, str(file_name)[:200], json.dumps(findings[:20]), datetime.now().isoformat()))
        strikes = (db_execute("SELECT COUNT(*) FROM security_incidents WHERE user_id = ?", (user_id,), fetch='one') or [1])[0]
    except Exception:
        strikes = 1
    text = (f"🚫 *File rejected*\n\n`{safe_md(file_name)}` tries to access data that isn't yours "
            f"(other users' files, the host's database or system secrets), so it was *automatically rejected*.\n\n"
            + "\n".join(f"• `{safe_md(f)[:160]}`" for f in findings[:6]) +
            "\n\nBots may only use their own folder. Remove that code and upload again. "
            "Repeated attempts can lead to a ban.")
    kb = {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]}
    try:
        if message_id:
            edit_message(chat_id, message_id, text, kb)
        else:
            send_message(chat_id, text, kb)
    except Exception:
        pass
    for a in ADMIN_IDS:
        try:
            send_message(a,
                f"🛡️ *Data-theft attempt blocked*\n\n👤 User `{user_id}` · strike *{strikes}*\n📁 `{safe_md(file_name)}`\n\n"
                + "\n".join(f"• `{safe_md(f)[:140]}`" for f in findings[:5]),
                {"inline_keyboard": [[{"text": "🚫 Ban user", "callback_data": f"admin_ban_{user_id}"},
                                      {"text": "👤 User", "callback_data": f"admin_user_view_{user_id}"}]]})
        except Exception:
            pass


# ==================== GITHUB BACKUP SYSTEM ====================

_github_push_lock   = threading.Lock()
_last_backup_time   = 0.0
_MIN_BACKUP_INTERVAL = 30          # minimum seconds between consecutive backups

def _gh_api(method, path, payload=None):
    """Raw GitHub Contents API call. Returns (http_status, response_dict)."""
    url     = (f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/"
               f"{GITHUB_REPO_NAME}/contents/{path}")
    if method == "GET" and "?" not in path:
        # read from the SAME branch the backup is written to (not the repo's default branch)
        url += "?ref=" + urllib.parse.quote(GITHUB_BACKUP_BRANCH, safe="")
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120 if payload else 30) as r:   # big uploads need time
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read())
        except: return e.code, {}
    except Exception as ex:
        return 0, {"error": str(ex)}

def _gh_repo_api(method, subpath, timeout=120):
    """GitHub REST call on the DB repo (not under /contents) — used for the Git blobs API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{subpath}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read())
        except: return e.code, {}
    except Exception as ex:
        return 0, {"error": str(ex)}


def _gh_contents_bytes(resp):
    """
    File bytes from a Contents-API response. GitHub only inlines the content for files up to
    1 MB — for anything bigger `content` is EMPTY (encoding "none"). That is exactly what made the
    bot think a >1 MB backup was 'too small', skip the restore and start with a fresh database.
    Larger files are fetched through the Git blobs API (works up to 100 MB).
    """
    raw = (resp.get("content") or "").replace("\n", "")
    if raw and resp.get("encoding", "base64") == "base64":
        data = base64.b64decode(raw)
    else:
        sha = resp.get("sha")
        if not sha:
            raise RuntimeError("backup has no content and no blob sha")
        status, blob = _gh_repo_api("GET", f"git/blobs/{sha}")
        if status != 200:
            raise RuntimeError(f"large-file download failed (HTTP {status}: {blob.get('message', blob)})")
        data = base64.b64decode((blob.get("content") or "").replace("\n", ""))
    expected = resp.get("size")
    if isinstance(expected, int) and expected and len(data) != expected:
        raise RuntimeError(f"incomplete download ({len(data)} of {expected} bytes)")
    return data


_db_restore_failed = False    # True when a backup EXISTS but could not be restored -> never overwrite it


def _db_has_data():
    """Return True only when the database contains real rows (not just schema)."""
    if not DATABASE_FILE.exists():
        return False
    if DATABASE_FILE.stat().st_size < 8192:   # < 8 KB → almost certainly empty
        return False
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM deployments")
        deps  = c.fetchone()[0]
        conn.close()
        return users > 0 or deps > 0
    except Exception:
        return False

def github_restore_db():
    """
    Download the database from GitHub and write it to DATABASE_FILE.
    Must be called BEFORE init_db() so existing data is never overwritten by
    a fresh schema.  Safe to call even when GitHub is not configured.
    """
    if not GITHUB_ENABLED:
        print("ℹ️  GitHub backup not configured — skipping restore")
        return False

    print(f"🔄 Restoring from GitHub → "
          f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{GITHUB_BACKUP_PATH} "
          f"[branch: {GITHUB_BACKUP_BRANCH}]")

    status, resp = _gh_api("GET", GITHUB_BACKUP_PATH)

    if status == 404:
        print("ℹ️  No backup found on GitHub — starting fresh")
        return False
    global _db_restore_failed
    if status != 200:
        print(f"⚠️  GitHub restore HTTP {status}: {resp.get('message', resp)}")
        _db_restore_failed = True      # a backup may exist — don't let a fresh DB overwrite it
        return False

    try:
        db_bytes = _gh_contents_bytes(resp)

        if len(db_bytes) < 1024:
            print("⚠️  GitHub backup is too small — skipping restore (corrupt?)")
            _db_restore_failed = True
            return False

        # Validate it is really a SQLite database before touching the live one
        if not db_bytes.startswith(b"SQLite format 3\x00"):
            print("⚠️  GitHub backup is not a valid SQLite file — skipping restore")
            _db_restore_failed = True
            return False

        DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file first and run an integrity check on it
        _tmp_restore = DATABASE_FILE.parent / (DATABASE_FILE.name + ".restore_tmp")
        with open(_tmp_restore, "wb") as f:
            f.write(db_bytes)
        try:
            _chk = sqlite3.connect(_tmp_restore)
            _ok = _chk.execute("PRAGMA integrity_check").fetchone()
            _chk.close()
            if not _ok or _ok[0] != "ok":
                print(f"⚠️  GitHub backup failed integrity check ({_ok}) — skipping restore")
                _tmp_restore.unlink(missing_ok=True)
                _db_restore_failed = True
                return False
        except Exception as _ie:
            print(f"⚠️  GitHub backup unreadable ({_ie}) — skipping restore")
            _tmp_restore.unlink(missing_ok=True)
            _db_restore_failed = True
            return False

        # Keep a safety copy of any existing live database before overwriting
        if DATABASE_FILE.exists() and DATABASE_FILE.stat().st_size > 0:
            try:
                _safety = DATABASE_FILE.parent / (
                    f"pre_github_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
                shutil.copy2(DATABASE_FILE, _safety)
            except Exception as _se:
                print(f"⚠️  Could not create safety copy before restore: {_se}")

        os.replace(_tmp_restore, DATABASE_FILE)

        size_kb = len(db_bytes) / 1024
        print(f"✅ Database restored from GitHub ({size_kb:.1f} KB)")
        return True
    except Exception as e:
        print(f"❌ GitHub restore error: {e}")
        _db_restore_failed = True
        return False

def github_backup_db(reason: str = "auto", force: bool = False, bypass_interval: bool = False):
    """
    Upload DATABASE_FILE to GitHub.  Thread-safe via push lock.
    Silently skips when:
      • GitHub is not configured
      • The database is empty / has no real rows
      • Another backup is already in flight (force=True waits briefly instead)
      • The previous backup was less than _MIN_BACKUP_INTERVAL seconds ago
        (force=True bypasses this — for explicit manual/admin-triggered backups)
    """
    global _last_backup_time

    if not GITHUB_ENABLED:
        return False
    if not _db_has_data():
        print(f"⏭️  Backup skipped ({reason}): database has no data")
        return False
    if _db_restore_failed and not force:
        print(f"🛡️  Backup skipped ({reason}): the GitHub backup could not be restored at startup, "
              f"so it is NOT being overwritten with this database")
        return False

    now = datetime.now().timestamp()
    if not force and not bypass_interval and now - _last_backup_time < _MIN_BACKUP_INTERVAL:
        return False   # too soon — silent

    if force or bypass_interval:
        acquired = _github_push_lock.acquire(blocking=True, timeout=15)
    else:
        acquired = _github_push_lock.acquire(blocking=False)
    if not acquired:
        return False   # another backup already running

    try:
        with open(DATABASE_FILE, "rb") as f:
            db_bytes = f.read()

        if len(db_bytes) < 1024:
            return False

        content_b64 = base64.b64encode(db_bytes).decode()

        # We need the current file's SHA to update it (GitHub API requirement)
        sha    = None
        status, resp = _gh_api("GET", GITHUB_BACKUP_PATH)
        if status == 200:
            sha = resp.get("sha")
            _remote_size = resp.get("size") or 0
            # Never replace a substantial backup with a drastically smaller database
            # (a reset/fresh DB would otherwise destroy the only good copy).
            if not force and _remote_size > 65536 and len(db_bytes) < _remote_size * 0.4:
                print(f"🛡️  Backup skipped ({reason}): local DB is {len(db_bytes)/1024:.0f} KB but the "
                      f"GitHub backup is {_remote_size/1024:.0f} KB — refusing to overwrite it")
                return False
        elif status not in (200, 404):
            print(f"⚠️  GitHub SHA lookup failed (HTTP {status}) — backup aborted")
            return False

        commit = {
            "message": f"backup: {reason} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "content": content_b64,
            "branch":  GITHUB_BACKUP_BRANCH,
        }
        if sha:
            commit["sha"] = sha

        status, resp = _gh_api("PUT", GITHUB_BACKUP_PATH, commit)

        if status in (200, 201):
            _last_backup_time = datetime.now().timestamp()
            print(f"✅ DB backed up to GitHub ({len(db_bytes)/1024:.1f} KB) — {reason}")
            return True
        else:
            print(f"⚠️  GitHub backup HTTP {status}: {resp.get('message', resp)}")
            return False

    except Exception as e:
        print(f"❌ GitHub backup error: {e}")
        return False
    finally:
        _github_push_lock.release()

_backup_state = {"scheduled": False, "dirty": False}
_backup_state_lock = threading.Lock()


def _coalesced_backup(reason):
    """Every change gets backed up: requests that arrive inside the minimum interval are merged into ONE trailing
    backup instead of being silently dropped (a dropped backup = a deployment lost on the next host redeploy)."""
    with _backup_state_lock:
        if _backup_state["scheduled"]:
            _backup_state["dirty"] = True
            return
        _backup_state["scheduled"] = True
    try:
        while True:
            wait = _MIN_BACKUP_INTERVAL - (datetime.now().timestamp() - _last_backup_time)
            if wait > 0:
                sleep(wait + 0.5)
            with _backup_state_lock:
                _backup_state["dirty"] = False
            github_backup_db(reason, bypass_interval=True)
            with _backup_state_lock:
                if not _backup_state["dirty"]:
                    _backup_state["scheduled"] = False
                    return
    except Exception as e:
        print(f"⚠️  coalesced backup error: {e}")
        with _backup_state_lock:
            _backup_state["scheduled"] = False


def async_backup(reason: str = "auto"):
    """Fire-and-forget backup — never blocks the bot's response path, never silently dropped."""
    if GITHUB_ENABLED:
        threading.Thread(target=_coalesced_backup, args=(reason,), daemon=True, name="GitHubBackup").start()

def _periodic_backup_thread():
    """Safety-net: flush a backup every 30 minutes regardless of other triggers."""
    sleep(300)   # wait 5 min before first periodic attempt
    while True:
        try:
            github_backup_db("periodic")
        except Exception as e:
            print(f"⚠️  Periodic backup error: {e}")
        sleep(1800)   # 30 minutes


# ==================== GITHUB FILES REPO (per-deployment storage) ====================
#
# Layout inside GITHUB_FILES_REPO_NAME (branch = GITHUB_BACKUP_BRANCH):
#
#   deployments/<user_id>/<deployment_id>/bot.py             main file (whatever the user called it)
#   deployments/<user_id>/<deployment_id>/requirements.txt   …or package.json for Node bots
#   deployments/<user_id>/<deployment_id>/.env              this deployment's env vars (paired by folder)
#   deployments/<user_id>/<deployment_id>/bot.db             the bot's own SQLite file(s), consistent snapshot
#   deployments/<user_id>/<deployment_id>/manifest.json      what is what (main file, runtime, plan, expiry…)
#
# Everything is written with the Git Data API, so one deployment (or a whole chunk of
# them) is a SINGLE commit, and only files whose content actually changed are uploaded.

_files_lock         = threading.RLock()     # serialises every write to the files repo
_files_remote       = {}                    # (uid, dep) -> {repo_path: blob_sha}  (our view of the repo)
_files_commit_count = 0
_files_repo_state   = {"checked": 0.0, "ok": False, "reason": "not checked yet"}
_files_public_warned = False

_NODE_EXTS = ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx')
_DB_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.db3'}
_FILES_SKIP_DIRS = {'packages', 'node_modules', '__pycache__', '.git', '.venv', 'venv',
                    '.cache', '.npm', '.pnpm-store', 'site-packages', '.idea', '.vscode',
                    '.tmp'}      # '.tmp' = the deployment's private TMPDIR (scratch space, never backed up)
_FILES_SKIP_NAMES = {'start.sh', 'pid.txt', 'output.log', 'nohup.out', '.env', '.DS_Store',
                     '.heartbeat'}     # .heartbeat is rewritten every 30 s — would force a commit per sync
_FILES_SKIP_SUFFIXES = {'.pyc', '.pyo', '.log', '.pid', '.lock', '.tmp', '.swp'}
_FILES_SKIP_ENDINGS = ('-wal', '-shm', '-journal')          # SQLite side files (merged by the snapshot)
_DEP_MANIFEST_NAMES = {'requirements.txt', 'requirements-dev.txt', 'requirements_prod.txt',
                       'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
                       'pyproject.toml', 'Pipfile', 'Pipfile.lock', 'runtime.txt'}


def _gh_files_api(method, path="", payload=None, timeout=60):
    """GitHub REST call against the FILES repo. Retries rate limits / 5xx. -> (status, json)."""
    base = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO_OWNER}/{GITHUB_FILES_REPO_NAME}"
    url = base + (("/" + path.lstrip("/")) if path else "")
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "BotHostingPlatform/1.0",
    }
    body = json.dumps(payload).encode() if payload is not None else None
    last = (0, {"error": "no attempt made"})
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read() or b"{}")
            except Exception:
                data = {}
            last = (e.code, data)
            msg = str(data.get("message", "")).lower()
            retry_after = e.headers.get("Retry-After") if e.headers else None
            transient = (e.code in (429, 500, 502, 503, 504)
                         or (e.code == 403 and ("rate limit" in msg or "abuse" in msg)))
            if transient and attempt < 3:
                wait = min(60, int(retry_after)) if retry_after and str(retry_after).isdigit() else 2 ** (attempt + 1)
                sleep(wait + random.uniform(0, 1))
                continue
            return last
        except Exception as ex:
            last = (0, {"error": str(ex)})
            if attempt < 3:
                sleep(2 ** attempt)
                continue
            return last
    return last


def _files_repo_ready(force=False) -> bool:
    """
    True only when the files repo exists, the token can reach it, and it is PRIVATE.
    A public repo is refused outright: it would publish every user's .env (bot tokens,
    API keys) and databases to the internet.
    """
    global _files_public_warned
    if not FILES_REPO_ENABLED:
        return False
    st = _files_repo_state
    now = datetime.now().timestamp()
    if not force:
        if st["ok"] and now - st["checked"] < 600:
            return True
        if not st["ok"] and now - st["checked"] < 60:
            return False
    status, repo = _gh_files_api("GET")
    st["checked"] = now
    if status == 200:
        if repo.get("private") is True:
            st.update(ok=True, reason="ok (private repo)")
            return True
        st.update(ok=False, reason="repository is PUBLIC — refusing to store .env files and databases in it")
        print(f"🚫 {GITHUB_REPO_OWNER}/{GITHUB_FILES_REPO_NAME} is PUBLIC — files backup disabled. "
              f"Make the repository private.")
        if not _files_public_warned:
            _files_public_warned = True
            try:
                notify_admin(f"🚫 *Files repo is PUBLIC*\n\n`{GITHUB_REPO_OWNER}/{GITHUB_FILES_REPO_NAME}` "
                             f"is public, so deployment backups are *disabled* (they contain users' "
                             f"`.env` secrets). Set the repository to *private* and they resume automatically.")
            except Exception:
                pass
        return False
    reasons = {
        404: "repository not found — create it (private) and give the token access to it",
        401: "GitHub token rejected (401)",
        403: "GitHub token has no access to the files repository (403)",
    }
    st.update(ok=False, reason=reasons.get(status, f"GitHub HTTP {status}: {repo.get('message', repo.get('error', ''))}"))
    print(f"⚠️  Files repo not usable: {st['reason']}")
    return False


def _files_prefix(user_id, deployment_id) -> str:
    return f"{FILES_REPO_ROOT}/{int(user_id)}/{int(deployment_id)}"


def _git_blob_sha(data: bytes) -> str:
    """The SHA GitHub would assign to this content as a git blob (lets us diff without downloading)."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _files_list_remote(prefix: str) -> dict:
    """{repo_path: blob_sha} for every file under `prefix` (Contents API, recursive). Raises on API errors."""
    out, stack = {}, [prefix]
    ref = urllib.parse.quote(GITHUB_BACKUP_BRANCH, safe="")
    while stack:
        d = stack.pop()
        status, items = _gh_files_api("GET", f"contents/{urllib.parse.quote(d)}?ref={ref}")
        if status == 404:
            continue
        if status != 200 or not isinstance(items, list):
            raise RuntimeError(f"listing {d} failed (HTTP {status})")
        for it in items:
            if it.get("type") == "dir":
                stack.append(it["path"])
            elif it.get("type") == "file":
                out[it["path"]] = it["sha"]
    return out


def _files_fetch_blob(sha: str):
    status, data = _gh_files_api("GET", f"git/blobs/{sha}", timeout=120)
    if status != 200:
        return None
    try:
        if data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", ""))
        return str(data.get("content", "")).encode()
    except Exception:
        return None


def _files_ensure_branch():
    """Return the head commit SHA of the backup branch, creating the branch / initialising an empty repo."""
    ref_path = "git/ref/heads/" + urllib.parse.quote(GITHUB_BACKUP_BRANCH)
    status, ref = _gh_files_api("GET", ref_path)
    if status == 200:
        return ref["object"]["sha"]
    status, repo = _gh_files_api("GET")
    default = (repo or {}).get("default_branch", "main") if status == 200 else "main"
    status, dref = _gh_files_api("GET", "git/ref/heads/" + urllib.parse.quote(default))
    if status != 200:
        # Completely empty repository: the Git Data API refuses to work until it has a first commit.
        _gh_files_api("PUT", "contents/README.md", {
            "message": "init: deployment storage",
            "content": base64.b64encode(
                b"# Deployment storage\n\nManaged automatically by the hosting bot. "
                b"Do not edit by hand. Keep this repository PRIVATE.\n").decode(),
        })
        status, dref = _gh_files_api("GET", "git/ref/heads/" + urllib.parse.quote(default))
        if status != 200:
            print(f"⚠️  Files repo: could not initialise (HTTP {status})")
            return None
    sha = dref["object"]["sha"]
    if default == GITHUB_BACKUP_BRANCH:
        return sha
    status, _ = _gh_files_api("POST", "git/refs", {"ref": f"refs/heads/{GITHUB_BACKUP_BRANCH}", "sha": sha})
    return sha if status in (200, 201) else None


def _files_squash():
    """Replace the branch history with one commit of the current tree (keeps the repo small)."""
    head = _files_ensure_branch()
    if not head:
        return False
    status, commit = _gh_files_api("GET", f"git/commits/{head}")
    if status != 200:
        return False
    status, new = _gh_files_api("POST", "git/commits", {
        "message": f"squash history — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "tree": commit["tree"]["sha"], "parents": []})
    if status not in (200, 201):
        return False
    status, _ = _gh_files_api("PATCH", "git/refs/heads/" + urllib.parse.quote(GITHUB_BACKUP_BRANCH),
                              {"sha": new["sha"], "force": True})
    if status == 200:
        print("🗜️  Files repo history squashed")
    return status == 200


def _files_commit(changes: dict, message: str) -> bool:
    """
    Apply {repo_path: bytes | None} as ONE commit (None = delete the path).
    Callers hold _files_lock. Retries if the branch moved underneath us.
    """
    global _files_commit_count
    if not changes:
        return True
    for _attempt in range(3):
        head = _files_ensure_branch()
        if not head:
            return False
        status, commit = _gh_files_api("GET", f"git/commits/{head}")
        if status != 200:
            return False
        entries = []
        for path, data in changes.items():
            if data is None:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            status, blob = _gh_files_api("POST", "git/blobs", {
                "content": base64.b64encode(data).decode(), "encoding": "base64"}, timeout=180)
            if status not in (200, 201):
                print(f"⚠️  Files repo: blob upload failed for {path} (HTTP {status})")
                return False
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        status, tree = _gh_files_api("POST", "git/trees", {"base_tree": commit["tree"]["sha"], "tree": entries})
        if status not in (200, 201):
            print(f"⚠️  Files repo: tree failed (HTTP {status}: {tree.get('message', '')})")
            return False
        status, new = _gh_files_api("POST", "git/commits", {
            "message": message[:200], "tree": tree["sha"], "parents": [head]})
        if status not in (200, 201):
            return False
        status, _ = _gh_files_api("PATCH", "git/refs/heads/" + urllib.parse.quote(GITHUB_BACKUP_BRANCH),
                                  {"sha": new["sha"], "force": False})
        if status == 200:
            _files_commit_count += 1
            if FILES_REPO_SQUASH_EVERY and _files_commit_count >= FILES_REPO_SQUASH_EVERY:
                if _files_squash():
                    _files_commit_count = 0
            return True
        if status != 422:       # 422 = branch moved; anything else is a real failure
            return False
    return False


def _is_platform_launcher(p: Path) -> bool:
    """run.py written by the platform (not the user's own run.py)."""
    try:
        if p.stat().st_size > 1_000_000:
            return False
        return b"_HOSTING_BOT_KEYS" in p.read_bytes()
    except Exception:
        return False


def _looks_like_sqlite(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except Exception:
        return False


def _sqlite_consistent_bytes(p: Path) -> bytes:
    """Snapshot a possibly-in-use SQLite file via SQLite's own backup API (WAL-safe, never torn)."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        src = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return Path(tmp).read_bytes()
    except Exception as e:
        print(f"⚠️  SQLite snapshot of {p.name} failed ({e}) — using a raw copy")
        return p.read_bytes()
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _env_dict_to_text(env_vars: dict) -> str:
    return ''.join(f"{k}={v}\n" for k, v in (env_vars or {}).items())


_ENV_KEY_RE   = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_ENV_COLON_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s+(\S.*)$')
_PLATFORM_ENV_KEYS = {'PORT', 'BOT_PORT', 'GITHUB_DEPLOY_TOKEN'}   # added by the platform, not typed by the user


def parse_env_input(text) -> tuple:
    """
    Parse environment variables a user typed or pasted. Returns (dict, ignored_count).
    Accepts, one per line:
        KEY=VALUE          export KEY=VALUE          KEY: VALUE
        KEY="VALUE"        KEY='VALUE'               (matching surrounding quotes are removed)
    Blank lines and # comments are skipped silently. Anything else counts as ignored — only the
    COUNT is reported, never the line, because a mistyped line may still contain a secret.
    Names must be shell-safe ([A-Za-z_][A-Za-z0-9_]*): the Node launcher `export`s them in bash.
    """
    out, ignored = {}, 0
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.lower().startswith('export '):
            line = line[7:].strip()
        if '=' in line:
            k, _, v = line.partition('=')
        else:
            m = _ENV_COLON_RE.match(line)
            if not m:
                ignored += 1
                continue
            k, v = m.group(1), m.group(2)
        k, v = k.strip(), v.strip()
        if not _ENV_KEY_RE.match(k):
            ignored += 1
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k] = v
    return out, ignored


def env_names_line(env_vars) -> str:
    """'🔧 Env vars saved (3): `A`, `B`, `C`' — NAMES ONLY, never values. Platform-added keys excluded."""
    names = [re.sub(r'[^A-Za-z0-9_]', '_', str(k)) for k in (env_vars or {})
             if k not in _PLATFORM_ENV_KEYS]
    if not names:
        return "🔧 *Env vars:* none set\n"
    shown = ', '.join(f"`{n}`" for n in names[:12])
    more = f" +{len(names) - 12} more" if len(names) > 12 else ""
    return f"🔧 *Env vars saved ({len(names)}):* {shown}{more}\n"


def _parse_env_text(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def _files_snapshot(deployment_id):
    """
    Build what should live in the repo for this deployment from local disk + the platform DB.
    Returns None when there is nothing trustworthy to save (no row, no folder, or main file
    missing — a wiped folder must never be allowed to delete the repo copy).
    """
    row = db_execute("""SELECT user_id, file_name, env_vars, plan, is_free, start_time, expire_time,
                               folder_name, source_type, github_repo, github_branch
                        FROM deployments WHERE deployment_id = ?""", (deployment_id,), fetch='one')
    if not row:
        return None
    (uid, file_name, env_json, plan, is_free, start_time, expire_time,
     folder_name, source_type, gh_repo, gh_branch) = row
    folder = get_deploy_folder(uid, deployment_id)
    main_rel = (file_name or "").replace("\\", "/")
    if not folder.exists() or not main_rel or not (folder / main_rel).exists():
        return None
    # Identity guard: the folder being backed up must be this OWNER's folder. Anything else
    # would file one user's data under another user's id.
    try:
        if folder.parent.name != str(int(uid)):
            print(f"🚫 dep#{deployment_id}: folder {folder} is not under user {uid} — refusing to back it up")
            return None
    except Exception:
        return None
    try:
        env_vars = json.loads(env_json) if env_json else {}
        if not isinstance(env_vars, dict):
            env_vars = {}
    except Exception:
        env_vars = {}

    candidates = []   # (priority, size, rel, path)
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _FILES_SKIP_DIRS]
        for name in names:
            p = Path(root) / name
            rel = p.relative_to(folder).as_posix()
            if (name in _FILES_SKIP_NAMES or p.suffix.lower() in _FILES_SKIP_SUFFIXES
                    or name.endswith(_FILES_SKIP_ENDINGS) or p.is_symlink()):
                continue
            if name == 'run.py' and _is_platform_launcher(p):
                continue
            try:
                size = p.stat().st_size
            except Exception:
                continue
            is_db = p.suffix.lower() in _DB_SUFFIXES and _looks_like_sqlite(p)
            if rel == main_rel:
                prio = 0
            elif name in _DEP_MANIFEST_NAMES:
                prio = 1
            elif is_db:
                prio = 2
            else:
                prio = 3
            candidates.append((prio, size, rel, p, is_db))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    files, skipped, total = {}, [], 0
    db_files, dep_files = [], []
    for prio, size, rel, p, is_db in candidates:
        limit = (FILES_MAX_DB_MB if is_db else FILES_MAX_SRC_FILE_MB) * 1024 * 1024
        if size > limit or total + size > FILES_MAX_TOTAL_MB * 1024 * 1024:
            skipped.append(rel)
            continue
        try:
            data = _sqlite_consistent_bytes(p) if is_db else p.read_bytes()
        except Exception:
            skipped.append(rel)
            continue
        files[rel] = data
        total += len(data)
        if is_db:
            db_files.append(rel)
        if p.name in _DEP_MANIFEST_NAMES:
            dep_files.append(rel)

    files['.env'] = _env_dict_to_text(env_vars).encode()
    is_node = Path(main_rel).suffix.lower() in _NODE_EXTS
    manifest = {
        "version": 1, "deployment_id": int(deployment_id), "user_id": int(uid),
        "file_name": main_rel, "folder_name": folder_name,
        "runtime": "node" if is_node else "python",
        "plan": plan, "is_free": bool(is_free),
        "start_time": start_time, "expire_time": expire_time,
        "source_type": source_type, "github_repo": gh_repo, "github_branch": gh_branch,
        "dependency_files": sorted(dep_files), "db_files": sorted(db_files),
        "env_keys": sorted(env_vars.keys()),
        "truncated": bool(skipped), "skipped": skipped[:50],
    }
    files['manifest.json'] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    return {"uid": int(uid), "deployment_id": int(deployment_id), "files": files,
            "skipped": skipped, "manifest": manifest}


def _files_plan(deployment_id):
    """Diff the local snapshot against the repo. Returns a plan dict, or None if nothing to save."""
    snap = _files_snapshot(deployment_id)
    if not snap:
        return None
    key = (snap["uid"], snap["deployment_id"])
    prefix = _files_prefix(*key)
    remote = _files_remote.get(key)
    if remote is None:
        remote = _files_list_remote(prefix)
        _files_remote[key] = remote
    changes, new_remote = {}, {}
    for rel, data in snap["files"].items():
        path = f"{prefix}/{rel}"
        sha = _git_blob_sha(data)
        new_remote[path] = sha
        if remote.get(path) != sha:
            changes[path] = data
    keep = {f"{prefix}/{s}" for s in snap["skipped"]}   # too big to upload now → keep the last good copy
    for path, sha in remote.items():
        if path in new_remote:
            continue
        if path in keep:
            new_remote[path] = sha
        else:
            changes[path] = None
    return {"key": key, "changes": changes, "new_remote": new_remote, "skipped": snap["skipped"]}


def files_repo_save_deployment(deployment_id, reason="auto") -> dict:
    """Push one deployment (changed files only) to the files repo. Never raises."""
    res = {"ok": False, "changed": 0, "skipped": [], "error": None}
    if not _files_repo_ready():
        res["error"] = _files_repo_state["reason"]
        return res
    with _files_lock:
        try:
            plan = _files_plan(deployment_id)
            if plan is None:
                res["error"] = "nothing to save (deployment or its main file is missing)"
                return res
            if plan["changes"] and not _files_commit(
                    plan["changes"], f"deployment {deployment_id} (user {plan['key'][0]}): {reason}"):
                res["error"] = "commit to GitHub failed"
                _files_remote.pop(plan["key"], None)     # re-list next time rather than trust a stale view
                return res
            _files_remote[plan["key"]] = plan["new_remote"]
            res.update(ok=True, changed=len(plan["changes"]), skipped=plan["skipped"])
            if plan["skipped"]:
                print(f"⚠️  dep#{deployment_id}: {len(plan['skipped'])} file(s) over the size caps were not saved")
        except Exception as e:
            res["error"] = str(e)
            print(f"⚠️  files_repo_save_deployment({deployment_id}): {e}")
    return res


def files_repo_backup(deployment_ids, reason="backup", progress=None, refresh=False) -> dict:
    """
    Save many deployments, batching them into few commits.
    progress(i, total, stats) is called after each deployment.
    refresh=True forgets our cached view of the repo and re-reads it from GitHub, so a
    repo that was wiped or edited outside the bot is fully re-populated (used by the
    manual admin backup — "already up to date" must mean up to date on GitHub itself).
    """
    ids = list(deployment_ids)
    stats = {"total": len(ids), "saved": 0, "unchanged": 0, "failed": 0,
             "no_files": 0, "oversized": 0, "error": None}
    if not _files_repo_ready(force=refresh):
        stats["error"] = _files_repo_state["reason"]
        return stats
    with _files_lock:
        if refresh:
            _files_remote.clear()
        batch_changes, batch_plans, batch_bytes = {}, [], 0

        def flush():
            nonlocal batch_changes, batch_plans, batch_bytes
            if not batch_plans:
                return
            ok = _files_commit(batch_changes, f"{reason}: {len(batch_plans)} deployment(s)")
            for p in batch_plans:
                if ok:
                    _files_remote[p["key"]] = p["new_remote"]
                    stats["saved"] += 1
                else:
                    _files_remote.pop(p["key"], None)
                    stats["failed"] += 1
            batch_changes, batch_plans, batch_bytes = {}, [], 0

        for i, dep in enumerate(ids, 1):
            try:
                plan = _files_plan(dep)
            except Exception as e:
                print(f"⚠️  backup dep#{dep}: {e}")
                stats["failed"] += 1
                plan = False
            if plan is None:
                stats["no_files"] += 1
            elif plan:
                stats["oversized"] += len(plan["skipped"])
                if not plan["changes"]:
                    _files_remote[plan["key"]] = plan["new_remote"]
                    stats["unchanged"] += 1
                else:
                    batch_plans.append(plan)
                    batch_changes.update(plan["changes"])
                    batch_bytes += sum(len(v) for v in plan["changes"].values() if v)
                    if len(batch_plans) >= FILES_BACKUP_CHUNK or batch_bytes >= 64 * 1024 * 1024:
                        flush()
            if progress:
                try:
                    progress(i, len(ids), stats)
                except Exception:
                    pass
        flush()
    return stats


def files_repo_delete_deployment(user_id, deployment_id) -> bool:
    """Remove a deployment's folder from the files repo (one commit). True when it is gone."""
    if not _files_repo_ready():
        return False
    with _files_lock:
        try:
            remote = _files_list_remote(_files_prefix(user_id, deployment_id))
            ok = True
            if remote:
                ok = _files_commit({p: None for p in remote},
                                   f"delete deployment {deployment_id} (user {user_id})")
            if ok:
                _files_remote.pop((int(user_id), int(deployment_id)), None)
            return ok
        except Exception as e:
            print(f"⚠️  files_repo_delete_deployment({deployment_id}): {e}")
            return False


def files_repo_restore_deployment(user_id, deployment_id, dest_folder: Path, log=print,
                                  expect_folder_name=None, progress=None):
    """
    Download a deployment's files from the repo into dest_folder.
    Returns {"files": n, "manifest": {...}} or None (nothing stored / any download failed /
    the backup does not belong to this exact deployment).

    Identity check BEFORE anything is written: the stored manifest must name this user and
    this deployment (and, when known, this deployment's folder_name — the unique stamp of
    one particular deployment). A backup that doesn't match is refused, so a database can
    never be restored into the wrong user's / the wrong deployment's folder, whatever the
    files happen to be called.
    """
    if not _files_repo_ready():
        return None
    prefix = _files_prefix(user_id, deployment_id)
    with _files_lock:
        try:
            remote = _files_list_remote(prefix)
        except Exception as e:
            log(f"⚠️ Could not list the backup: {e}")
            return None
        if not remote:
            return None
        man_sha = remote.get(f"{prefix}/manifest.json")
        man_data = _files_fetch_blob(man_sha) if man_sha else None
        try:
            man = json.loads(man_data.decode('utf-8', 'ignore')) if man_data else None
        except Exception:
            man = None
        if not isinstance(man, dict):
            log("🚫 Backup has no readable manifest — cannot verify it belongs to this deployment; refusing")
            return None
        if int(man.get("user_id", -1)) != int(user_id) or int(man.get("deployment_id", -1)) != int(deployment_id):
            log(f"🚫 Backup identity mismatch (manifest says user {man.get('user_id')} / "
                f"deployment {man.get('deployment_id')}) — refusing to restore")
            return None
        if expect_folder_name and man.get("folder_name") and str(man["folder_name"]) != str(expect_folder_name):
            log(f"🚫 Backup is from a different deployment that reused this id "
                f"(folder {man['folder_name']} ≠ {expect_folder_name}) — refusing to restore")
            return None
        dest_folder = Path(dest_folder)
        dest_folder.mkdir(parents=True, exist_ok=True)
        manifest, written = {}, 0
        _total = max(1, len(remote))
        for _i, (path, sha) in enumerate(sorted(remote.items())):
            if progress:
                try: progress(5 + int(70 * _i / _total), f"Downloading files ({_i}/{_total})")
                except Exception: pass
            rel = path[len(prefix) + 1:]
            parts = rel.split('/')
            if not rel or rel.startswith('/') or '..' in parts or '' in parts:
                log(f"⚠️ Skipping unsafe path in backup: {path!r}")
                continue
            data = _files_fetch_blob(sha)
            if data is None:
                log(f"❌ Could not download {rel} — aborting restore")
                return None          # all-or-nothing: a half-restored bot must not look complete
            target = dest_folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written += 1
            if rel == 'manifest.json':
                try:
                    manifest = json.loads(data.decode('utf-8', 'ignore'))
                except Exception:
                    manifest = {}
        _files_remote[(int(user_id), int(deployment_id))] = remote
        log(f"♻️ Restored {written} file(s) from the GitHub backup")
        return {"files": written, "manifest": manifest}


def _install_deps_for_folder(deploy_folder: Path, dest_script: Path, log=print, only_if_missing=False) -> bool:
    """
    (Re)install what the deployment needs, from the manifests/code now on disk:
      Python → requirements.txt / pyproject / Pipfile, then any import the manifests missed
      Node   → package.json (auto-generated from require()/import when absent) via npm/yarn/pnpm/bun
    only_if_missing=True skips the work when dependencies are already in place.
    """
    deploy_folder, dest_script = Path(deploy_folder), Path(dest_script)
    try:
        code = dest_script.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        code = ""
    try:
        if dest_script.suffix.lower() in _NODE_EXTS:
            pkg = deploy_folder / 'package.json'
            if not pkg.exists():
                js = scan_js_requires(code)
                if js:
                    build_package_json(deploy_folder, js, dest_script.name, log)
            if not pkg.exists():
                return True
            if only_if_missing and (deploy_folder / 'node_modules').exists():
                return True
            return bool(_install_npm(deploy_folder, log))

        packages_dir = deploy_folder / 'packages'
        if only_if_missing:
            has_manifest = any((deploy_folder / f).exists()
                               for f in ('requirements.txt', 'pyproject.toml', 'Pipfile'))
            populated = packages_dir.exists() and any(packages_dir.iterdir())
            if populated or not has_manifest:
                return True
        packages_dir.mkdir(exist_ok=True)
        installed = install_from_repo_requirements(deploy_folder, log) or []
        already = {re.split(r'[=<>!~\[\s]', str(r).strip(), 1)[0].lower() for r in installed}
        auto = UniversalDependencyInstaller.scan_imports(code, log, local_dir=deploy_folder)
        gap = [a for a in auto if re.split(r'[=<>!~\[\s]', a.strip(), 1)[0].lower() not in already]
        if gap:
            log(f"📦 Installing {len(gap)} import(s) not covered by the manifests: {', '.join(gap)}")
            tmp = deploy_folder / '_autodetect_gap_reqs.txt'
            tmp.write_text('\n'.join(gap))
            try:
                install_dependencies_enhanced(tmp, log, packages_dir)
            finally:
                tmp.unlink(missing_ok=True)
        return True
    except Exception as e:
        log(f"⚠️ Dependency install error: {e}")
        return False


def _files_restore_for_relaunch(owner_id, deployment_id, deploy_folder: Path, dest_script: Path, log=print,
                                progress=None):
    """
    Pull a deployment whose files are gone from the files repo and reinstall its dependencies.
    Returns (ok, env_dict_or_None).
    """
    if not FILES_REPO_ENABLED:
        return False, None
    existed = deploy_folder.exists()
    try:
        _row = db_execute("SELECT folder_name FROM deployments WHERE deployment_id = ?",
                          (deployment_id,), fetch='one')
        if progress:
            try: progress(2, "Checking your backup")
            except Exception: pass
        info = files_repo_restore_deployment(owner_id, deployment_id, deploy_folder, log,
                                             expect_folder_name=(_row[0] if _row else None),
                                             progress=progress)
    except Exception as e:
        log(f"⚠️ Restore error: {e}")
        info = None
    if not info or not dest_script.exists():
        if not existed:
            shutil.rmtree(deploy_folder, ignore_errors=True)   # never leave a half-restored folder behind
        return False, None
    if (info.get("manifest") or {}).get("truncated"):
        log("⚠️ This backup was truncated (some files exceeded the size caps)")
    if progress:
        try: progress(78, "Installing dependencies")
        except Exception: pass
    _install_deps_for_folder(deploy_folder, dest_script, log)
    if progress:
        try: progress(100, "Restore complete")
        except Exception: pass
    env = None
    try:
        env = _parse_env_text((deploy_folder / '.env').read_text(errors='replace'))
    except Exception:
        pass
    return True, env


def persist_new_deployment(dep_id, reason="deployed"):
    """Make a brand-new deployment durable BEFORE telling the user it worked: files → GitHub files repo, database →
    GitHub backup, both immediately (not queued, not debounced). Best effort; never raises."""
    try:
        if FILES_REPO_ENABLED:
            files_repo_save_deployment(dep_id, reason)
    except Exception as e:
        print(f"⚠️ persist files #{dep_id}: {e}")
    try:
        if GITHUB_ENABLED:
            github_backup_db(f"{reason}_{dep_id}", bypass_interval=True)
    except Exception as e:
        print(f"⚠️ persist db #{dep_id}: {e}")


def adopt_lost_deployments():
    """The database can roll back (host redeploy restores an older backup) while the GitHub files repo still holds
    the deployment. Re-create any such deployment's row from its manifest and relaunch it.
    -> {"adopted": [(uid, dep)], "error": str|None}"""
    res = {"adopted": [], "error": None}
    if not FILES_REPO_ENABLED:
        return res
    try:
        if not _files_repo_ready():
            res["error"] = "files repo not ready"
            return res
        with _files_lock:
            found = _files_remote_deployment_dirs()
        known = {r[0] for r in (db_execute("SELECT deployment_id FROM deployments", fetch='all') or [])}
        for (uid, dep), paths in sorted(found.items()):
            if dep in known:
                continue
            prefix = _files_prefix(uid, dep)
            try:
                with _files_lock:
                    remote = _files_list_remote(prefix)
                man_sha, env_sha = remote.get(f"{prefix}/manifest.json"), remote.get(f"{prefix}/.env")
                if not man_sha:
                    continue
                man = json.loads((_files_fetch_blob(man_sha) or b"{}").decode("utf-8", "replace"))
                env = _parse_env_text((_files_fetch_blob(env_sha) or b"").decode("utf-8", "replace")) if env_sha else {}
            except Exception as e:
                res["error"] = f"read #{dep}: {e}"
                continue
            fn = str(man.get("file_name") or "")
            if (int(man.get("user_id") or -1) != uid or int(man.get("deployment_id") or -1) != dep
                    or not fn or fn.startswith("/") or ".." in Path(fn).parts):
                continue                                   # not trustworthy → leave for the admin
            exp = man.get("expire_time")
            if exp:
                try:
                    if datetime.fromisoformat(exp) <= datetime.now():
                        continue                           # expired anyway → normal cleanup may remove it
                except Exception:
                    continue
            db_execute("INSERT OR IGNORE INTO users (user_id, join_date, last_active) VALUES (?, ?, ?)",
                       (uid, datetime.now().isoformat(), datetime.now().isoformat()))
            db_execute('''INSERT INTO deployments
                (deployment_id, user_id, file_name, file_size, requirements, env_vars, plan, payment_method,
                 cost_coins, cost_stars, start_time, expire_time, status, proc_pid, deploy_log, is_free, is_paused,
                 framework, dependencies_installed, folder_name, source_type, github_repo, github_branch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (dep, uid, fn, 0, "", json.dumps(env), man.get("plan") or "monthly", "restored", 0, 0,
                 man.get("start_time") or datetime.now().isoformat(), exp, "active", None,
                 "Recovered from the GitHub files repo", 1 if man.get("is_free") else 0, 0,
                 "unknown", json.dumps([]), str(man.get("folder_name") or dep),
                 man.get("source_type") or "upload", man.get("github_repo"), man.get("github_branch") or "main"))
            res["adopted"].append((uid, dep))
            print(f"♻️  Recovered deployment #{dep} (user {uid}) from the files repo")
            try:
                enqueue_relaunch(dep, reason="adopt", bypass_cap=True)
                send_message(uid, f"♻️ *Bot #{dep} recovered*\n\nThe host's database was rolled back, but your bot's "
                                  f"files were safe on GitHub. I've restored it and it's starting now.",
                             {"inline_keyboard": [[{"text": "📄 Deployment", "callback_data": f"view_deploy_{dep}"}]]})
            except Exception as e:
                print(f"⚠️ adopt notify #{dep}: {e}")
        if res["adopted"]:
            async_backup("adopted_deployments")
    except Exception as e:
        res["error"] = str(e)
        print(f"⚠️ adopt_lost_deployments: {e}")
    return res


# ---- background worker so saves/deletes never block the bot ------------------
_files_jobs         = queue.Queue()
_files_jobs_pending = set()
_files_jobs_lock    = threading.Lock()
_files_worker_on    = False


def _files_init_tables():
    db_execute("""CREATE TABLE IF NOT EXISTS files_repo_pending_delete (
                    user_id INTEGER, deployment_id INTEGER, created_at TEXT,
                    PRIMARY KEY (user_id, deployment_id))""")


def _files_worker():
    while True:
        job = _files_jobs.get()
        try:
            if job[0] == 'save':
                files_repo_save_deployment(job[1], job[2])
            elif job[0] == 'delete':
                if files_repo_delete_deployment(job[1], job[2]):
                    db_execute("DELETE FROM files_repo_pending_delete WHERE user_id=? AND deployment_id=?",
                               (job[1], job[2]))
        except Exception as e:
            print(f"⚠️  Files worker error: {e}")
        finally:
            with _files_jobs_lock:
                _files_jobs_pending.discard((job[0],) + tuple(job[1:3] if job[0] == 'delete' else job[1:2]))
            _files_jobs.task_done()


def _files_enqueue(job):
    global _files_worker_on
    if not FILES_REPO_ENABLED:
        return
    key = (job[0],) + tuple(job[1:3] if job[0] == 'delete' else job[1:2])
    with _files_jobs_lock:
        if key in _files_jobs_pending:
            return
        _files_jobs_pending.add(key)
        if not _files_worker_on:
            _files_worker_on = True
            threading.Thread(target=_files_worker, daemon=True, name="FilesRepoWorker").start()
    _files_jobs.put(job)


def files_repo_async_save(deployment_id, reason="auto"):
    """Fire-and-forget save of one deployment."""
    _files_enqueue(('save', int(deployment_id), reason))


def files_repo_async_delete(user_id, deployment_id):
    """Fire-and-forget removal. Remembered in the DB so a failed/lost attempt is retried later."""
    if not FILES_REPO_ENABLED:
        return
    db_execute("INSERT OR IGNORE INTO files_repo_pending_delete (user_id, deployment_id, created_at) VALUES (?,?,?)",
               (int(user_id), int(deployment_id), datetime.now().isoformat()))
    _files_enqueue(('delete', int(user_id), int(deployment_id)))


def _files_retry_pending_deletes():
    for uid, dep in db_execute("SELECT user_id, deployment_id FROM files_repo_pending_delete",
                               fetch='all') or []:
        _files_enqueue(('delete', uid, dep))


def _files_sync_thread():
    """Every FILES_SYNC_INTERVAL_SEC: push changed files (DB snapshots!) of live deployments."""
    sleep(120)
    print(f"✅ Files-repo sync started ({FILES_SYNC_INTERVAL_SEC // 60} min cycle)")
    first = True
    while True:
        try:
            _files_retry_pending_deletes()
            _adopt = adopt_lost_deployments()               # recover first, so cleanup never deletes a lost-but-live bot
            if _adopt["error"]:
                print(f"⚠️  Orphan cleanup skipped this cycle (recovery incomplete: {_adopt['error']})")
            else:
                files_repo_purge_orphans_auto()
            where = "" if first else "WHERE status IN ('active', 'paused')"
            ids = [r[0] for r in db_execute(f"SELECT deployment_id FROM deployments {where}", fetch='all') or []]
            first = False
            if ids:
                s = files_repo_backup(ids, "periodic")
                if s["saved"] or s["failed"] or s["error"]:
                    print(f"📤 Files sync: {s['saved']} updated · {s['unchanged']} unchanged · "
                          f"{s['failed']} failed" + (f" · {s['error']}" if s["error"] else ""))
        except Exception as e:
            print(f"⚠️  Files sync error: {e}")
        sleep(FILES_SYNC_INTERVAL_SEC)


def _files_remote_deployment_dirs():
    """{(user_id, deployment_id): [repo paths]} for everything stored under FILES_REPO_ROOT.
    One Git-tree call; falls back to walking the Contents API when GitHub truncates a huge tree.
    Anything not shaped like <root>/<digits>/<digits>/... is ignored (never touched)."""
    head = _files_ensure_branch()
    if not head:
        raise RuntimeError("backup branch not available")
    status, commit = _gh_files_api("GET", f"git/commits/{head}")
    if status != 200:
        raise RuntimeError(f"commit lookup failed (HTTP {status})")
    status, tree = _gh_files_api("GET", f"git/trees/{commit['tree']['sha']}?recursive=1", timeout=120)
    if status == 200 and not tree.get("truncated"):
        paths = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
    else:
        paths = list(_files_list_remote(FILES_REPO_ROOT).keys())
    found = {}
    pre = FILES_REPO_ROOT + "/"
    for pth in paths:
        if not pth.startswith(pre):
            continue
        parts = pth[len(pre):].split("/")
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            found.setdefault((int(parts[0]), int(parts[1])), []).append(pth)
    return found


def files_repo_purge_orphans(force=False, dry_run=False):
    """
    Delete every deployment folder in the files repo that no longer exists in the database
    (or belongs to a different user than the database says). One commit.
    Returns {"checked": n, "orphans": [(uid, dep), ...], "deleted": n, "error": str|None, "blocked": str|None}.

    Safety: if the database has NO deployments, or the orphans are most of the repo, the
    database is far more likely to be empty/restored-from-old than the repo to be full of
    junk — so the purge refuses (blocked) unless force=True (admin confirmed).
    """
    res = {"checked": 0, "orphans": [], "deleted": 0, "error": None, "blocked": None}
    if not _files_repo_ready():
        res["error"] = "files repo not configured / not private"
        return res
    with _files_lock:
        try:
            found = _files_remote_deployment_dirs()
            res["checked"] = len(found)
            if not found:
                return res
            known = {(int(u), int(d)) for u, d in
                     (db_execute("SELECT user_id, deployment_id FROM deployments", fetch='all') or [])}
            orphans = sorted(k for k in found if k not in known)
            res["orphans"] = orphans
            if not orphans or dry_run:
                return res
            if not force:
                if not known:
                    res["blocked"] = "the database lists no deployments at all (empty or just restored?)"
                elif len(orphans) > max(5, len(found) // 2):
                    res["blocked"] = (f"{len(orphans)} of {len(found)} stored deployments look orphaned "
                                      f"— that many is suspicious")
                if res["blocked"]:
                    return res
            changes = {pth: None for k in orphans for pth in found[k]}
            if _files_commit(changes, f"purge {len(orphans)} orphaned deployment(s)"):
                res["deleted"] = len(orphans)
                for (u, d) in orphans:
                    _files_remote.pop((u, d), None)
                    db_execute("DELETE FROM files_repo_pending_delete WHERE user_id=? AND deployment_id=?", (u, d))
            else:
                res["error"] = "commit failed"
        except Exception as e:
            res["error"] = str(e)
    return res


def files_repo_purge_orphans_auto():
    r = files_repo_purge_orphans()
    if r["deleted"]:
        print(f"🧹 Files repo: removed {r['deleted']} orphaned deployment(s): {r['orphans'][:10]}")
        try:
            notify_admin(f"🧹 Removed {r['deleted']} orphaned deployment folder(s) from the GitHub files repo "
                         f"(not in the database any more).")
        except Exception:
            pass
    elif r["blocked"]:
        print(f"⚠️  Files repo purge skipped: {r['blocked']}")
        try:
            notify_admin(f"⚠️ Orphan cleanup skipped: {r['blocked']}.\nUse Admin → Database → "
                         f"Clean Orphaned Files to review it manually.")
        except Exception:
            pass
    elif r["error"]:
        print(f"⚠️  Files repo purge error: {r['error']}")
    return r


def files_repo_status_line() -> str:
    if not FILES_REPO_ENABLED:
        return "❌ Not configured (set `GITHUB_FILES_REPO_NAME`)"
    _files_repo_ready()
    return "✅ Private repo OK" if _files_repo_state["ok"] else f"⚠️ {_files_repo_state['reason']}"


def _graceful_shutdown_flush(signum=None, frame=None):
    """SIGTERM (host redeploy/stop): push every live deployment + the DB before the disk disappears."""
    print("🛑 Shutdown signal — flushing deployments to GitHub…")
    done = threading.Event()

    def _work():
        try:
            if FILES_REPO_ENABLED:
                ids = [r[0] for r in db_execute(
                    "SELECT deployment_id FROM deployments WHERE status IN ('active','paused')", fetch='all') or []]
                if ids:
                    files_repo_backup(ids, "shutdown")
            if GITHUB_ENABLED:
                github_backup_db("shutdown", force=True)
        except Exception as e:
            print(f"⚠️  Shutdown flush error: {e}")
        finally:
            done.set()

    threading.Thread(target=_work, daemon=True, name="ShutdownFlush").start()
    done.wait(timeout=22)
    os._exit(0)


# ==================== RELAUNCH QUEUE ====================
# Every restart — host reboot, crash, watchdog — goes through ONE queue. Bots are brought
# back one at a time, each only after the previous one has settled and the machine has
# free RAM again, so a restart of 50 bots can't spike memory (or pip/npm installs) all at once.

_relaunch_q       = queue.Queue()
_relaunch_pending = set()
_relaunch_lock    = threading.Lock()
_relaunch_started = False
_relaunch_batch   = {"queued": 0, "done": 0, "results": {}}


def _available_memory_mb():
    """(total_mb, available_mb) — honours container (cgroup) limits, discounting reclaimable cache."""
    total, _used, avail = _read_proc_meminfo()
    try:
        v2 = Path('/sys/fs/cgroup/memory.max')
        if v2.exists():
            raw = v2.read_text().strip()
            if raw != 'max':
                limit = int(raw)
                cur = int(Path('/sys/fs/cgroup/memory.current').read_text())
                inactive = 0
                for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines():
                    if line.startswith('inactive_file '):
                        inactive = int(line.split()[1])
                return limit / 1048576, max(0.0, (limit - max(0, cur - inactive)) / 1048576)
        else:
            v1 = Path('/sys/fs/cgroup/memory/memory.limit_in_bytes')
            if v1.exists():
                limit = int(v1.read_text())
                if limit < 2 ** 50:
                    cur = int(Path('/sys/fs/cgroup/memory/memory.usage_in_bytes').read_text())
                    inactive = 0
                    try:
                        for line in Path('/sys/fs/cgroup/memory/memory.stat').read_text().splitlines():
                            if line.startswith('total_inactive_file '):
                                inactive = int(line.split()[1])
                    except Exception:
                        pass
                    return limit / 1048576, max(0.0, (limit - max(0, cur - inactive)) / 1048576)
    except Exception:
        pass
    return total, avail


def _wait_for_memory_headroom():
    total, avail = _available_memory_mb()
    if not total:
        return
    need = min(RELAUNCH_MIN_FREE_MB, total * 0.15)
    waited = 0
    while avail < need and waited < RELAUNCH_MEM_WAIT_MAX:
        sleep(5)
        waited += 5
        _, avail = _available_memory_mb()
    if waited:
        print(f"⏳ Relaunch queue waited {waited}s for free RAM ({avail:.0f} MB free, wanted {need:.0f} MB)")


def enqueue_relaunch(deployment_id, reason="crash", bypass_cap=False) -> bool:
    """Queue a relaunch. A deployment already queued is not queued twice. True if newly queued."""
    global _relaunch_started
    with _relaunch_lock:
        if deployment_id in _relaunch_pending:
            return False
        _relaunch_pending.add(deployment_id)
        if reason == "startup":
            _relaunch_batch["queued"] += 1
        _relaunch_q.put((deployment_id, reason, bypass_cap))
        if not _relaunch_started:
            _relaunch_started = True
            for i in range(max(1, RELAUNCH_WORKERS)):
                threading.Thread(target=_relaunch_worker, daemon=True, name=f"RelaunchQueue-{i + 1}").start()
    return True


def _relaunch_followup(dep_id, reason, result):
    """Same manual-restart prompt the health monitor always sent when auto-restart couldn't recover."""
    if reason not in ("crash", "watchdog") or result not in ("cap_exceeded", "launch_failed"):
        return
    row = db_execute("SELECT user_id FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
    if not row or not notif_allows(row[0], "crash", "failed"):
        return
    try:
        _tail = _log_tail_text(get_deploy_folder(row[0], dep_id), 15)
        _logs = f"\n\n*Last log lines:*\n```\n{_tail}\n```" if _tail else ""
        why = ("It crashed too many times in the last hour, so auto-restart stopped."
               if result == "cap_exceeded" else "Automatic restart didn't succeed.")
        _diag = crash_diagnose(_log_tail_text(get_deploy_folder(row[0], dep_id), 60, 6000))
        send_message(row[0],
            crash_alert_text(dep_id, f"⚠️ *Bot #{dep_id} Crashed*\n\n{why} Your database is safe.", _diag, _tail),
            {"inline_keyboard": crash_fix_rows(dep_id, _diag)})
    except Exception:
        pass


def _relaunch_worker():
    while True:
        dep_id, reason, bypass_cap = _relaunch_q.get()
        result = "error"
        try:
            _wait_for_memory_headroom()
            result = _auto_relaunch_deployment(dep_id, reason=reason, bypass_cap=bypass_cap)
            _relaunch_followup(dep_id, reason, result)
        except Exception as e:
            print(f"❌ Relaunch queue error (dep#{dep_id}): {e}")
            traceback.print_exc()
        finally:
            with _relaunch_lock:
                _relaunch_pending.discard(dep_id)
                if reason == "startup":
                    b = _relaunch_batch
                    b["done"] += 1
                    b["results"][result] = b["results"].get(result, 0) + 1
                    if b["done"] >= b["queued"]:
                        summary = ", ".join(f"{k}={v}" for k, v in b["results"].items())
                        print(f"✅ Reconciliation complete: {summary}")
                        if b["results"].get("files_missing"):
                            print(f"⚠️  {b['results']['files_missing']} deployment(s) had no files on disk and "
                                  f"no usable GitHub backup. Check GITHUB_FILES_REPO_NAME / PERSISTENT_DISK_PATH.")
                        b.update(queued=0, done=0, results={})
            _relaunch_q.task_done()
        sleep(RELAUNCH_STAGGER_SEC)


def _launch_and_check(start_script, deploy_folder):
    """Run a deployment's start.sh and wait until its process has stayed up. -> (is_running, pid)."""
    _run_start(start_script, deploy_folder, capture_output=True)
    _wait_for_launch(deploy_folder, timeout=20, settle=4)
    new_pid = None
    pid_file = Path(deploy_folder) / "pid.txt"
    if pid_file.exists():
        try:
            new_pid = int(pid_file.read_text().strip())
        except Exception:
            pass
    alive = False
    if new_pid:
        try:
            os.kill(new_pid, 0)
            alive = True
        except Exception:
            pass
    return alive, new_pid


def _log_shows_missing_dependency(deploy_folder) -> bool:
    try:
        tail = (Path(deploy_folder) / "output.log").read_text(errors="replace")[-4000:]
    except Exception:
        return False
    return any(s in tail for s in ("ModuleNotFoundError", "No module named", "ImportError",
                                   "Cannot find module", "MODULE_NOT_FOUND"))


def _log_tail_text(deploy_folder, lines=15, max_chars=1400) -> str:
    """Last `lines` lines of a bot's output.log, safe to drop inside a Markdown code block."""
    try:
        raw = (Path(deploy_folder) / "output.log").read_text(errors="replace")
    except Exception:
        return ""
    tail = "\n".join(l for l in raw.splitlines() if l.strip())
    tail = "\n".join(tail.splitlines()[-lines:])
    return tail.replace("`", "'")[-max_chars:]


_MISSING_PY  = re.compile(r"No module named ['\"]([A-Za-z0-9_\.\-]+)['\"]")
_MISSING_NODE = re.compile(r"Cannot find (?:module|package) ['\"]([^'\"]+)['\"]")


def _missing_modules_from_log(deploy_folder):
    """-> (python_modules, node_packages) named in the last lines of the bot's log."""
    try:
        tail = (Path(deploy_folder) / "output.log").read_text(errors="replace")[-6000:]
    except Exception:
        return [], []
    py = [m.split(".")[0] for m in _MISSING_PY.findall(tail)]
    node = []
    for m in _MISSING_NODE.findall(tail):
        if m.startswith((".", "/")) or m.startswith("node:"):
            continue                       # a relative file, not a package
        parts = m.split("/")
        node.append("/".join(parts[:2]) if m.startswith("@") else parts[0])
    return list(dict.fromkeys(py)), list(dict.fromkeys(node))


def _install_missing_from_log(deploy_folder, dest_script, log=lambda m: None):
    """The bot crashed on `No module named X` / `Cannot find module X` → install just that package.
    Returns the list of packages that were installed (empty when nothing was missing / installable)."""
    py, node = _missing_modules_from_log(deploy_folder)
    done = []
    deploy_folder = Path(deploy_folder)
    if py:
        try:
            reqs = []
            for mod in py:
                for r in UniversalDependencyInstaller.scan_imports(f"import {mod}\n", log, local_dir=deploy_folder):
                    if r not in reqs:
                        reqs.append(r)
            reqs = drop_stdlib_requirements(reqs) if 'drop_stdlib_requirements' in globals() else reqs
            if reqs:
                packages_dir = deploy_folder / "packages"
                packages_dir.mkdir(exist_ok=True)
                tmp = deploy_folder / "_missing_mod_reqs.txt"
                tmp.write_text("\n".join(reqs))
                try:
                    install_dependencies_enhanced(tmp, log, packages_dir)
                finally:
                    tmp.unlink(missing_ok=True)
                done += reqs
        except Exception as e:
            print(f"⚠️ missing-module install (python) failed: {e}")
    if node and _cmd_exists('npm'):
        try:
            ok, out = _run(['npm', 'install', '--no-audit', '--no-fund', *node], cwd=str(deploy_folder), timeout=300)
            if ok:
                done += node
        except Exception as e:
            print(f"⚠️ missing-module install (node) failed: {e}")
    return done


def _find_available_port(preferred: int = 20000) -> int:
    """Return an available TCP port, starting from preferred and sampling nearby."""
    import socket, random
    candidates = [preferred] + random.sample(
        range(max(20000, preferred - 500), min(39999, preferred + 500)), 40
    )
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('', port))
                return port
            except OSError:
                continue
    return preferred + 1  # best-effort fallback

# ========== PROGRESS BAR FUNCTION ==========
def create_progress_bar(percentage: float, width: int = 30, filled_char: str = "█", empty_char: str = "░") -> str:
    filled = int(width * percentage / 100)
    empty = width - filled
    bar = filled_char * filled + empty_char * empty
    return f"[{bar}] {percentage:.1f}%"

# ========== HELPER FUNCTIONS ==========
def db_execute(query, params=(), fetch='none', retries=5, delay=0.2):
    """
    Thread-safe SQLite helper with automatic retry on SQLITE_BUSY/LOCKED.
    fetch='none' → no return, 'one' → fetchone(), 'all' → fetchall()
    """
    for attempt in range(retries):
        conn = None
        try:
            conn = sqlite3.connect(DATABASE_FILE, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            if fetch == 'one':
                return c.fetchone()
            if fetch == 'all':
                return c.fetchall()
            return True
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() or 'busy' in str(e).lower():
                if attempt < retries - 1:
                    sleep(delay * (attempt + 1))
                    continue
            print(f"❌ DB OperationalError: {e} | Query: {query[:80]}")
            return None
        except sqlite3.IntegrityError as e:
            print(f"⚠️ DB IntegrityError (ignored): {e}")
            return None
        except Exception as e:
            print(f"❌ DB error: {e}")
            return None
        finally:
            if conn:
                try: conn.close()
                except Exception: pass
    return None

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

try:
    import psutil as _psutil
except ImportError:
    _psutil = None


def _read_proc_meminfo():
    """Total/available RAM in MB, straight from /proc (no dependency needed)."""
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                key, val = line.split(':', 1)
                info[key.strip()] = int(val.strip().split()[0])  # kB
        total_mb = info.get('MemTotal', 0) / 1024
        avail_mb = info.get('MemAvailable', info.get('MemFree', 0)) / 1024
        used_mb = total_mb - avail_mb
        return total_mb, used_mb, avail_mb
    except Exception:
        return 0, 0, 0


def _read_proc_cpu_times():
    """Cumulative CPU jiffies since boot, for computing a delta-based %."""
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        return total, idle
    except Exception:
        return None, None


def get_system_resources(sample_seconds=0.3):
    """
    Host-wide RAM/CPU usage. Uses psutil if available (no blocking sample
    needed for RAM; still samples briefly for an accurate CPU%), otherwise
    falls back to reading /proc directly — pure stdlib, works on any Linux
    container (Render included) with zero extra dependencies.
    """
    result = {'ram_total_mb': 0, 'ram_used_mb': 0, 'ram_percent': 0,
              'cpu_percent': 0, 'cpu_count': os.cpu_count() or 1,
              'load_avg': (0.0, 0.0, 0.0), 'source': 'proc'}

    if _psutil:
        try:
            vm = _psutil.virtual_memory()
            result['ram_total_mb'] = vm.total / (1024 * 1024)
            result['ram_used_mb'] = (vm.total - vm.available) / (1024 * 1024)
            result['ram_percent'] = vm.percent
            result['cpu_percent'] = _psutil.cpu_percent(interval=sample_seconds)
            result['cpu_count'] = _psutil.cpu_count() or result['cpu_count']
            result['source'] = 'psutil'
        except Exception:
            pass

    if result['source'] == 'proc':
        total_mb, used_mb, _ = _read_proc_meminfo()
        result['ram_total_mb'] = total_mb
        result['ram_used_mb'] = used_mb
        result['ram_percent'] = (used_mb / total_mb * 100) if total_mb else 0

        t0, idle0 = _read_proc_cpu_times()
        if t0 is not None:
            sleep(sample_seconds)
            t1, idle1 = _read_proc_cpu_times()
            if t1 and t1 > t0:
                busy_delta = (t1 - idle1) - (t0 - idle0)
                total_delta = t1 - t0
                result['cpu_percent'] = max(0, min(100, busy_delta / total_delta * 100)) if total_delta else 0

    try:
        result['load_avg'] = os.getloadavg()
    except (OSError, AttributeError):
        pass

    return result


def get_deployment_resource_usage(proc_pid):
    """RAM (MB) and CPU% for one deployment's process, best-effort."""
    if not proc_pid:
        return None
    if _psutil:
        try:
            p = _psutil.Process(proc_pid)
            return {
                'ram_mb': p.memory_info().rss / (1024 * 1024),
                'cpu_percent': p.cpu_percent(interval=0.1),
            }
        except Exception:
            return None
    # /proc fallback: RSS from status, skip CPU% (needs sampling per-process,
    # too slow to do for every deployment in a list — RAM alone is still useful)
    try:
        with open(f'/proc/{proc_pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return {'ram_mb': int(line.split()[1]) / 1024, 'cpu_percent': None}
    except Exception:
        pass
    return None


# ==================== PER-USER RESOURCE QUOTAS ====================
# The host's total resource pool is split equally between all registered
# (non-admin) users: e.g. 30 GB pool / 10 users = 3 GB each. Admins are not
# limited and do not count towards the divisor.
#   RESOURCE_POOL_GB   total pool in GB (default: 90% of the host's RAM)
#   QUOTA_ENFORCE      1 = stop a user's biggest bot after sustained overuse
#   QUOTA_STRIKES      consecutive 60 s checks over quota before stopping (3)
RESOURCE_POOL_GB = os.environ.get("RESOURCE_POOL_GB", "").strip()
QUOTA_ENFORCE    = os.environ.get("QUOTA_ENFORCE", "1") == "1"
QUOTA_STRIKES    = int(os.environ.get("QUOTA_STRIKES", 3))
_quota_strikes   = {}


def get_resource_pool_mb() -> float:
    """Total shareable pool in MB (RESOURCE_POOL_GB, else 90% of host RAM)."""
    if RESOURCE_POOL_GB:
        try:
            return max(64.0, float(RESOURCE_POOL_GB) * 1024)
        except ValueError:
            pass
    total_mb, _, _ = _read_proc_meminfo()
    if not total_mb and _psutil:
        try:
            total_mb = _psutil.virtual_memory().total / (1024 * 1024)
        except Exception:
            total_mb = 0
    return max(256.0, total_mb * 0.9) if total_mb else 1024.0


def count_quota_users() -> int:
    """Number of users the pool is divided between (non-admins, minimum 1)."""
    try:
        admin_list = list(ADMIN_IDS)
        if admin_list:
            q = ("SELECT COUNT(*) FROM users WHERE user_id NOT IN (%s)"
                 % ",".join("?" * len(admin_list)))
            row = db_execute(q, tuple(admin_list), fetch='one')
        else:
            row = db_execute("SELECT COUNT(*) FROM users", (), fetch='one')
        return max(1, int(row[0]) if row and row[0] else 1)
    except Exception:
        return 1


def get_user_tier(user_id):
    """(key, mb, expires_datetime) of the user's ACTIVE resource tier, or None."""
    try:
        row = db_execute("SELECT resource_tier_key, resource_tier_mb, resource_tier_expires "
                         "FROM users WHERE user_id = ?", (user_id,), fetch='one')
        if row and row[1] and row[2]:
            exp = datetime.fromisoformat(row[2])
            if exp > datetime.now():
                return row[0], int(row[1]), exp
    except Exception:
        pass
    return None


def tier_sold_mb():
    """(total MB, number of users) held by ACTIVE resource tiers (admins excluded)."""
    try:
        admin_list = list(ADMIN_IDS)
        q = ("SELECT COALESCE(SUM(resource_tier_mb),0), COUNT(*) FROM users "
             "WHERE resource_tier_mb > 0 AND resource_tier_expires > ?")
        params = [datetime.now().isoformat()]
        if admin_list:
            q += " AND user_id NOT IN (%s)" % ",".join("?" * len(admin_list))
            params += admin_list
        row = db_execute(q, tuple(params), fetch='one')
        return float(row[0] or 0), int(row[1] or 0)
    except Exception:
        return 0.0, 0


def count_pool_sharers() -> int:
    """Users who split the REMAINING pool equally: non-admins without an active tier (min 1)."""
    return max(1, count_quota_users() - tier_sold_mb()[1])


def tier_unallocated_mb(user_id=None) -> float:
    """RAM of the pool not held by anyone's tier (a user's OWN current tier counts as free for them,
    since a new purchase replaces it)."""
    sold, _ = tier_sold_mb()
    cur = get_user_tier(user_id) if user_id else None
    if cur:
        sold -= cur[1]
    return max(0.0, get_resource_pool_mb() * TIER_POOL_MAX_FRACTION - sold)


def tier_capacity_ok(user_id, new_mb) -> bool:
    """Can new_mb be sold to this user without exceeding TIER_POOL_MAX_FRACTION of the pool?
    (their own current tier is being replaced, so it doesn't count twice)"""
    sold, _ = tier_sold_mb()
    cur = get_user_tier(user_id)
    if cur:
        sold -= cur[1]
    return sold + new_mb <= get_resource_pool_mb() * TIER_POOL_MAX_FRACTION


def get_user_quota_mb(user_id) -> float:
    """Admins: the whole pool. Tier holders: their purchased amount. Everyone else: an equal
    share of what is left of the pool after tiers are carved out."""
    pool = get_resource_pool_mb()
    if is_admin(user_id):
        return pool
    tier = get_user_tier(user_id)
    if tier:
        return float(tier[1])
    sold, _ = tier_sold_mb()
    return max(64.0, (pool - sold) / count_pool_sharers())


def format_resource(mb: float) -> str:
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def get_process_tree_ram_mb(pid) -> float:
    """RSS (MB) of a deployment's launcher AND everything it spawned."""
    if not pid:
        return 0.0
    if _psutil:
        try:
            p = _psutil.Process(pid)
            total = p.memory_info().rss
            for ch in p.children(recursive=True):
                try:
                    total += ch.memory_info().rss
                except Exception:
                    pass
            return total / (1024 * 1024)
        except Exception:
            return 0.0
    # /proc fallback: deployments run under setsid, so pgid == launcher pid
    try:
        pgid = os.getpgid(pid)
        own_pgid = os.getpgid(os.getpid())
    except Exception:
        return 0.0
    only_self = (pgid == own_pgid)   # legacy un-isolated process: just itself
    total_kb = 0
    try:
        candidates = [d for d in os.listdir('/proc') if d.isdigit()]
    except Exception:
        return 0.0
    for d in candidates:
        cpid = int(d)
        if only_self and cpid != pid:
            continue
        try:
            if not only_self and os.getpgid(cpid) != pgid:
                continue
            with open(f'/proc/{cpid}/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        total_kb += int(line.split()[1])
                        break
        except Exception:
            continue
    return total_kb / 1024


def get_user_resource_usage_mb(user_id) -> float:
    rows = db_execute("SELECT proc_pid FROM deployments WHERE user_id = ? "
                      "AND status = 'active' AND proc_pid IS NOT NULL",
                      (user_id,), fetch='all') or []
    return sum(get_process_tree_ram_mb(r[0]) for r in rows)


def check_user_quota(user_id):
    """Returns (ok, used_mb, quota_mb). ok=False when the user has no headroom."""
    quota = get_user_quota_mb(user_id)
    if is_admin(user_id):
        return True, get_user_resource_usage_mb(user_id), quota
    used = get_user_resource_usage_mb(user_id)
    return used < quota, used, quota


def quota_exceeded_text(used_mb, quota_mb) -> str:
    return (f"⚠️ *Resource limit reached*\n\n"
            f"You are using `{format_resource(used_mb)}` of your "
            f"`{format_resource(quota_mb)}` share.\n"
            f"The host's resources are split equally between all users, so your "
            f"share shrinks as more people join.\n\n"
            f"Stop or delete a bot to free up resources, or get a guaranteed amount "
            f"with a *📈 Resource Plan* (512 MB - 4 GB).\n\n"
            f"Then try again.")


def quota_monitor():
    """Every 60 s: warn users over their share, stop their biggest bot if sustained."""
    sleep(90)
    print("✅ Resource quota monitor started")
    while True:
        try:
            rows = db_execute("SELECT DISTINCT user_id FROM deployments "
                              "WHERE status = 'active' AND proc_pid IS NOT NULL",
                              (), fetch='all') or []
            for (uid,) in rows:
                if is_admin(uid):
                    continue
                quota = get_user_quota_mb(uid)
                used = get_user_resource_usage_mb(uid)
                if used <= quota:
                    _quota_strikes.pop(uid, None)
                    continue

                strikes = _quota_strikes.get(uid, 0) + 1
                _quota_strikes[uid] = strikes
                if strikes == 1 and notif_allows(uid, "usage"):
                    try:
                        send_message(uid,
                            f"⚠️ *Over your resource share*\n\n"
                            f"Using `{format_resource(used)}` of `{format_resource(quota)}`.\n"
                            f"Reduce usage or your largest bot will be stopped in "
                            f"~{QUOTA_STRIKES} min.")
                    except Exception:
                        pass
                if QUOTA_ENFORCE and strikes >= QUOTA_STRIKES:
                    dep_rows = db_execute("SELECT deployment_id, proc_pid FROM deployments "
                                          "WHERE user_id = ? AND status = 'active' "
                                          "AND proc_pid IS NOT NULL",
                                          (uid,), fetch='all') or []
                    if dep_rows:
                        biggest = max(dep_rows, key=lambda r: get_process_tree_ram_mb(r[1]))
                        stop_deployment(biggest[0])
                        print(f"🛑 Quota: stopped deployment {biggest[0]} (user {uid})")
                        try:
                            send_message(uid,
                                f"🛑 *Bot #{biggest[0]} stopped*\n\n"
                                f"You stayed over your `{format_resource(quota)}` share.",
                                {"inline_keyboard": [[{"text": "📦 My Deployments",
                                                       "callback_data": "my_deployments"}]]})
                        except Exception:
                            pass
                    _quota_strikes[uid] = 0
        except Exception as e:
            print(f"❌ Quota monitor error: {e}")
        sleep(60)


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
    try:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute("UPDATE users SET coins_balance = coins_balance + ? WHERE user_id = ?", (delta, user_id))
        if delta > 0:
            c.execute("UPDATE users SET total_coins_earned = total_coins_earned + ? WHERE user_id = ?", (delta, user_id))
        else:
            c.execute("UPDATE users SET total_coins_spent = total_coins_spent + ? WHERE user_id = ?", (abs(delta), user_id))
        c.execute('''INSERT INTO coin_transactions 
            (user_id, amount, transaction_type, source, reference_id, timestamp, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, delta, transaction_type, source, None, datetime.now().isoformat(), 'completed'))
        conn.commit()
    except Exception as e:
        print(f"❌ update_user_coins error: {e}")
        conn.rollback()
    finally:
        conn.close()
    update_system_stats()
    return True

def update_user_stars(user_id, delta, transaction_type="balance_update", source="system", payload=None):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute("UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?", (delta, user_id))
        if delta > 0:
            c.execute("UPDATE users SET total_stars_earned = total_stars_earned + ? WHERE user_id = ?", (delta, user_id))
        else:
            c.execute("UPDATE users SET total_stars_spent = total_stars_spent + ? WHERE user_id = ?", (abs(delta), user_id))
        c.execute('''INSERT INTO star_transactions 
            (user_id, amount, transaction_type, source, reference_id, timestamp, status, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, delta, transaction_type, source, None, datetime.now().isoformat(), 'completed', payload))
        conn.commit()
    except Exception as e:
        print(f"❌ update_user_stars error: {e}")
        conn.rollback()
    finally:
        conn.close()
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

def get_premium_end(user_id):
    """When this user's CURRENT premium subscription ends (datetime), or None for admins /
    users without active premium (admins are never capped)."""
    if is_admin(user_id):
        return None
    try:
        row = db_execute("SELECT is_premium, premium_expires FROM users WHERE user_id = ?",
                         (user_id,), fetch='one')
        if row and row[0] == 1 and row[1]:
            end = datetime.fromisoformat(row[1])
            if end > datetime.now():
                return end
    except Exception:
        pass
    return None


def cap_expiry_to_premium(user_id, expire_time, covered_by_premium):
    """
    A deployment that is free BECAUSE of the owner's premium must not outlive that premium:
    a 30-day deployment started when the subscription has 2 days left runs for 2 days.
    Deployments the user paid for themselves (coins / Stars) and admin lifetime deployments
    are not capped. Returns (expire_time, was_capped).
    """
    if not covered_by_premium or expire_time is None:
        return expire_time, False
    end = get_premium_end(user_id)
    if end is not None and end < expire_time:
        return end, True
    return expire_time, False


def extend_premium_deployments(user_id, premium_end):
    """
    The user just renewed premium: deployments that were capped to the OLD subscription end
    get extended — up to the end of the new subscription, but never beyond their own plan
    window (30 days monthly / 365 days yearly, counted from when they started).
    Returns how many deployments were extended.
    """
    n = 0
    try:
        rows = db_execute("""SELECT deployment_id, plan, start_time, expire_time FROM deployments
                             WHERE user_id = ? AND payment_method = 'premium_free' AND is_free = 0
                               AND plan IN ('monthly', 'yearly') AND status IN ('active', 'paused')
                               AND expire_time IS NOT NULL""", (user_id,), fetch='all') or []
        for dep_id, plan, start_str, exp_str in rows:
            try:
                window_days = 30 if plan == 'monthly' else 365
                plan_end = datetime.fromisoformat(start_str) + timedelta(days=window_days)
                new_exp = min(plan_end, premium_end)
                if new_exp > datetime.fromisoformat(exp_str):
                    db_execute("UPDATE deployments SET expire_time = ? WHERE deployment_id = ?",
                               (new_exp.isoformat(), dep_id))
                    n += 1
            except Exception as e:
                print(f"⚠️  extend_premium_deployments dep#{dep_id}: {e}")
    except Exception as e:
        print(f"⚠️  extend_premium_deployments: {e}")
    return n


def _wait_for_launch(deploy_folder, timeout=15, settle=3):
    """
    Poll pid.txt instead of sleeping a fixed time. Returns once the process
    named in pid.txt has been alive for `settle` seconds (so an immediate
    crash is still caught), or when `timeout` seconds have passed. Slow hosts
    that need >5s to start Python no longer produce false "failed" results.
    """
    pid_file = Path(deploy_folder) / "pid.txt"
    deadline = datetime.now().timestamp() + timeout
    alive_since = None
    dead_polls = 0
    while datetime.now().timestamp() < deadline:
        pid = None
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except Exception:
                pid = None
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        if alive:
            dead_polls = 0
            if alive_since is None:
                alive_since = datetime.now().timestamp()
            elif datetime.now().timestamp() - alive_since >= settle:
                return True
        else:
            alive_since = None
            if pid:
                # A PID is recorded but the process is gone: it crashed on start. Fail fast
                # (after ~2s of re-checks) instead of idling until the timeout.
                dead_polls += 1
                if dead_polls >= 4:
                    return False
        sleep(0.5)
    return False

def _premium_covers_plan_core(user_id, plan) -> bool:
    """
    Whether the user's CURRENT premium tier includes a free deployment of
    `plan`. A monthly premium subscription covers monthly deployments only;
    a yearly subscription covers monthly and yearly. Admins cover everything.
    """
    if is_admin(user_id):
        return True
    if not is_user_premium(user_id):
        return False
    if plan in ("free", None):
        return True
    if plan == "lifetime":
        return False  # admin-only
    try:
        row = db_execute("SELECT premium_plan FROM users WHERE user_id = ?",
                         (user_id,), fetch='one')
        tier = (row[0] if row and row[0] else "monthly").lower()
    except Exception:
        tier = "monthly"
    if tier == "yearly":
        return plan in ("monthly", "yearly")
    return plan == "monthly"

def premium_covers_plan(user_id, plan) -> bool:
    """True when a deployment of `plan` costs the user nothing: their premium tier covers it, OR they
    hold an unused deployment credit for that plan (left over from deleting a bot they had paid for)."""
    return _premium_covers_plan_core(user_id, plan) or get_deploy_credit(user_id, plan) is not None


def get_deploy_credit(user_id, plan):
    """Oldest unused credit for this plan → (credit_id, seconds_left) or None."""
    if plan not in ("monthly", "yearly"):
        return None
    try:
        row = db_execute("""SELECT credit_id, seconds_left FROM deploy_credits
                            WHERE user_id = ? AND plan = ? AND used = 0 AND seconds_left > 60
                            ORDER BY credit_id LIMIT 1""", (user_id, plan), fetch='one')
        return (row[0], int(row[1])) if row else None
    except Exception:
        return None


def grant_deploy_credit(user_id, plan, source_dep, expire_time):
    """A PAID deployment is being deleted with time left → keep the unused time as a credit so
    re-deploying the edited bot doesn't charge again. Returns seconds granted (0 = none)."""
    try:
        if not expire_time or plan not in ("monthly", "yearly"):
            return 0
        left = int((expire_time - datetime.now()).total_seconds())
        if left <= 60:
            return 0
        db_execute("""INSERT INTO deploy_credits (user_id, plan, seconds_left, source_deployment, created_at, used)
                      VALUES (?,?,?,?,?,0)""", (user_id, plan, left, source_dep, datetime.now().isoformat()))
        return left
    except Exception as e:
        print(f"⚠️ grant_deploy_credit: {e}")
        return 0


def apply_deploy_credit(user_id, plan, start_time, expire_time, payment_method):
    """Called when a paid-plan deployment is actually going live and it is 'free'. If that is because
    of a credit (not real premium), the deployment runs for the credit's remaining time and the credit
    is consumed. Returns (expire_time, payment_method, used_credit)."""
    if payment_method != 'premium_free' or _premium_covers_plan_core(user_id, plan):
        return expire_time, payment_method, False
    cr = get_deploy_credit(user_id, plan)
    if not cr:
        return expire_time, payment_method, False
    try:
        db_execute("UPDATE deploy_credits SET used = 1 WHERE credit_id = ?", (cr[0],))
    except Exception:
        return expire_time, payment_method, False
    return start_time + timedelta(seconds=cr[1]), 'credit', True


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

def stop_free_deployments_for_user(user_id):
    """
    Stop all active/paused free-tier (is_free=1) deployments for a user.
    Called when premium is activated to eliminate duplicates.
    Returns the count of bots stopped.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT deployment_id, proc_pid FROM deployments
                 WHERE user_id = ? AND is_free = 1 AND status IN ('active','paused')""",
              (user_id,))
    rows = c.fetchall()
    stopped = 0
    for dep_id, proc_pid in rows:
        if proc_pid:
            try:
                kill_deployment_process(proc_pid)
            except Exception:
                pass
        c.execute("""UPDATE deployments SET status='stopped', proc_pid=NULL, is_paused=0
                     WHERE deployment_id=?""", (dep_id,))
        with deployment_lock:
            active_deployments.pop(dep_id, None)
        stopped += 1
    conn.commit()
    conn.close()
    return stopped


def resume_premium_deployment(user_id, duration_days):
    """
    Find the most recent stopped/paused premium deployment for this user,
    restart it with the new expiry window, and return (dep_id, success).
    This is called right after a new premium subscription is activated so the
    user's bot picks up exactly where it left off — database fully intact.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT deployment_id, file_name, env_vars, folder_name, proc_pid
                 FROM deployments
                 WHERE user_id = ? AND is_free = 0
                       AND status IN ('stopped','paused','failed')
                 ORDER BY start_time DESC LIMIT 1""", (user_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return None, False

    dep_id, file_name, env_vars_json, folder_name_val, old_pid = row

    # Kill stale process if any
    if old_pid:
        try:
            kill_deployment_process(old_pid)
        except Exception:
            pass

    deploy_folder = get_deploy_folder(user_id, dep_id)
    start_script  = deploy_folder / "start.sh"
    launcher      = deploy_folder / "run.py"

    if not start_script.exists() or not launcher.exists():
        return dep_id, False

    # Re-run the bot
    result = _run_start(start_script, deploy_folder,
                            capture_output=True, text=True)
    _wait_for_launch(deploy_folder, timeout=15)

    pid_file  = deploy_folder / "pid.txt"
    new_pid   = None
    if pid_file.exists():
        try:
            new_pid = int(pid_file.read_text().strip())
        except Exception:
            pass

    running = False
    if new_pid:
        try:
            os.kill(new_pid, 0)
            running = True
        except Exception:
            pass

    if running:
        new_expire = datetime.now() + timedelta(days=duration_days)
        conn2 = sqlite3.connect(DATABASE_FILE)
        conn2.execute("""UPDATE deployments
                         SET status='active', proc_pid=?, is_paused=0,
                             expire_time=?, start_time=?
                         WHERE deployment_id=?""",
                      (new_pid, new_expire.isoformat(), datetime.now().isoformat(), dep_id))
        conn2.commit()
        conn2.close()
        with deployment_lock:
            active_deployments[dep_id] = new_pid
        async_backup(f"premium_resume_{dep_id}")
        return dep_id, True

    return dep_id, False


def continue_deployment_as_free(deployment_id, user_id, chat_id):
    """
    Downgrade a stopped/expired premium deployment to a fresh 24-hr free slot.
    The deploy folder and database are untouched — the bot resumes from the
    exact state it was in when premium expired.
    """
    deploy_folder = get_deploy_folder(user_id, deployment_id)
    start_script  = deploy_folder / "start.sh"
    launcher      = deploy_folder / "run.py"

    if not start_script.exists() or not launcher.exists():
        send_message(chat_id,
            "❌ Deployment files not found — please deploy a new bot.",
            {"inline_keyboard": [[{"text": "🚀 Deploy New Bot", "callback_data": "deploy_new"}]]})
        return False

    # Kill stale PID first
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT proc_pid FROM deployments WHERE deployment_id=?", (deployment_id,))
    r = c.fetchone()
    conn.close()
    if r and r[0]:
        try:
            kill_deployment_process(r[0])
        except Exception:
            pass

    _run_start(start_script, deploy_folder, capture_output=True)
    _wait_for_launch(deploy_folder, timeout=15)

    pid_file = deploy_folder / "pid.txt"
    new_pid  = None
    if pid_file.exists():
        try:
            new_pid = int(pid_file.read_text().strip())
        except Exception:
            pass

    running = False
    if new_pid:
        try:
            os.kill(new_pid, 0)
            running = True
        except Exception:
            pass

    if running:
        new_expire = datetime.now() + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
        conn2 = sqlite3.connect(DATABASE_FILE)
        conn2.execute("""UPDATE deployments
                         SET status='active', proc_pid=?, is_paused=0,
                             is_free=1, expire_time=?, start_time=?
                         WHERE deployment_id=?""",
                      (new_pid, new_expire.isoformat(),
                       datetime.now().isoformat(), deployment_id))
        conn2.commit()
        conn2.close()
        with deployment_lock:
            active_deployments[deployment_id] = new_pid

        async_backup(f"continue_as_free_{deployment_id}")
        send_message(chat_id,
            f"✅ *Bot Resumed (Free 24h)*\n\n"
            f"Deployment `#{deployment_id}` is running again.\n"
            f"Your database and settings are intact.\n"
            f"Expires: `{new_expire.strftime('%Y-%m-%d %H:%M')}`",
            {"inline_keyboard": [
                [{"text": "📄 View Logs",       "callback_data": f"view_runtime_logs_{deployment_id}"}],
                [{"text": "⭐ Get Premium",      "callback_data": "subscribe_premium"}],
                [{"text": "🏠 Menu",            "callback_data": "main_menu"}],
            ]})
        return True
    else:
        log_tail = ""
        lf = deploy_folder / "output.log"
        if lf.exists():
            log_tail = lf.read_text(errors='replace')[-800:].strip()
        send_message(chat_id,
            f"❌ *Failed to resume bot*\n\n"
            f"```\n{log_tail[-500:]}\n```\n\n"
            "The process exited immediately. Check your env vars.",
            {"inline_keyboard": [
                [{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{deployment_id}"}],
                [{"text": "🚀 Deploy New Bot", "callback_data": "deploy_new"}],
            ]})
        return False


def count_github_deployments(user_id):
    """Count active/paused GitHub-sourced deployments for a user."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT COUNT(*) FROM deployments
                 WHERE user_id=? AND source_type='github'
                 AND status IN ('active','paused')""", (user_id,))
    n = c.fetchone()[0]
    conn.close()
    return n


def activate_premium(user_id, plan, amount_stars, amount_coins, duration_days):
    try:
        # Stack the new period onto any days still remaining (early renewal loses nothing)
        _base = datetime.now()
        try:
            _cur = get_premium_end(user_id)
            if _cur and _cur > _base:
                _base = _cur
        except Exception:
            pass
        end_date = _base + timedelta(days=duration_days)

        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET is_premium=1, premium_expires=?, premium_plan=? WHERE user_id=?",
                  (end_date.isoformat(), plan, user_id))
        c.execute("""INSERT INTO subscriptions
                     (user_id, plan, amount_stars, amount_coins, start_date, end_date, status)
                     VALUES (?,?,?,?,?,?,?)""",
                  (user_id, plan, amount_stars, amount_coins,
                   datetime.now().isoformat(), end_date.isoformat(), 'active'))
        conn.commit()
        conn.close()

        # 1. Stop all free-tier bots (no duplicates while premium runs)
        stopped_free = stop_free_deployments_for_user(user_id)

        # 2. Resume the most recent stopped/paused premium deployment
        resumed_dep_id, premium_resumed = resume_premium_deployment(user_id, duration_days)

        # 3. If no prior premium deployment exists, fall back to resuming paused ones
        paused_resumed = 0
        if not premium_resumed:
            paused_resumed = resume_paused_deployments(user_id)

        # 4. Deployments that were free-because-of-premium and capped to the previous
        #    subscription end keep running for the renewed period.
        extend_premium_deployments(user_id, end_date)

        update_system_stats()

        user_info   = get_user_info(user_id)
        notify_admin(
            f"🎉 NEW PREMIUM!\n"
            f"User: {user_info.get('first_name','?')} ({user_id})\n"
            f"Plan: {plan.upper()} | Amount: {amount_stars}⭐\n"
            f"Premium dep resumed: #{resumed_dep_id} ({premium_resumed})\n"
            f"Free bots stopped: {stopped_free}")

        async_backup(f"premium_{plan}_{user_id}")
        total_resumed = (1 if premium_resumed else paused_resumed)
        return True, total_resumed

    except Exception as e:
        print(f"❌ Activate premium error: {e}")
        return False, 0

def resume_paused_deployments(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute('''SELECT deployment_id, file_name, env_vars, folder_name
                     FROM deployments WHERE user_id = ? AND status = 'paused' AND is_paused = 1''', (user_id,))
        paused = c.fetchall()
        
        resumed_count = 0
        for dep_id, file_name, env_vars_json, folder_name_val in paused:
            # Resolve correct folder using stored folder_name
            if folder_name_val:
                deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(folder_name_val)
            else:
                deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(dep_id)
            file_name = _sane_entry(deploy_folder, file_name, dep_id)
            dest_script = deploy_folder / file_name
            
            if dest_script.exists():
                env_vars = json.loads(env_vars_json) if env_vars_json else {}
                env = {k: v for k, v in os.environ.items() if k not in _HOST_SECRET_KEYS}
                for k, v in env_vars.items():
                    env[k] = v
                
                launcher_script = deploy_folder / "run.py"
                log_file_path = deploy_folder / "output.log"
                _uid = _prepare_uid_folder(deploy_folder) if _uid_iso_ok() else None
                _pre = None
                if _uid is not None:
                    _d = _uid_drop(_uid)
                    def _pre(_d=_d):
                        _d()
                        _fp_preexec()
                try:
                    log_file_path.touch()
                    if _uid is not None:
                        os.chown(log_file_path, _uid, _uid)
                except Exception:
                    pass
                with open(log_file_path, "a") as log_f:
                    if launcher_script.exists():
                        proc = subprocess.Popen(
                            [sys.executable, str(launcher_script)],
                            cwd=str(deploy_folder),
                            env=env,
                            stdout=log_f,
                            stderr=subprocess.STDOUT,
                            preexec_fn=_pre
                        )
                    else:
                        proc = subprocess.Popen(
                            [sys.executable, str(dest_script)],
                            cwd=str(deploy_folder),
                            env=env,
                            stdout=log_f,
                            stderr=subprocess.STDOUT,
                            preexec_fn=_pre
                        )
                
                c.execute('''UPDATE deployments SET status = 'active', is_paused = 0, proc_pid = ? 
                             WHERE deployment_id = ?''', (proc.pid, dep_id))  # FIX: store proc.pid not proc
                resumed_count += 1
                
                with deployment_lock:
                    active_deployments[dep_id] = proc.pid  # FIX: store PID integer
        
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

# Records the last Telegram HTTP error code seen by send_message() on this
# thread, so callers (e.g. broadcast) can tell a 429 rate-limit (worth
# retrying with backoff) from a 403 "bot was blocked" (not worth retrying).
_send_err = threading.local()

def send_message(chat_id, text, keyboard=None, parse_mode="Markdown"):
    _send_err.code = None
    url  = f"{TELEGRAM_API}/sendMessage"
    text, keyboard = localize(chat_id, str(text or ''), keyboard)
    text = text[:4096]
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    for _attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                return result
        except urllib.error.HTTPError as e:
            _send_err.code = e.code
            if e.code == 429:          # rate limit
                sleep(2 * (_attempt + 1))
                continue
            body = ''
            try: body = e.read().decode()[:200]
            except Exception: pass
            print(f"Send HTTP {e.code}: {body}")
            # ── Fallback: bad/unbalanced Markdown must never mean "no reply" ──
            # Telegram rejects the WHOLE message if entity parsing fails (e.g. a
            # stray "_" or "*" in a username/first_name breaks bold/italic
            # pairing). Retry once as plain text so the user always gets a
            # response instead of silence.
            if parse_mode and "can't parse entities" in body.lower():
                data_plain = {k: v for k, v in data.items() if k != "parse_mode"}
                try:
                    req2 = urllib.request.Request(
                        url, data=json.dumps(data_plain).encode('utf-8'),
                        headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req2, timeout=30) as resp2:
                        print("⚠️  Sent as plain text after Markdown parse failure")
                        return json.loads(resp2.read().decode('utf-8'))
                except Exception as e2:
                    print(f"Plain-text fallback also failed: {e2}")
            return None
        except Exception as e:
            print(f"Send error (attempt {_attempt+1}): {e}")
            if _attempt < 2:
                sleep(1)
    return None

def edit_message(chat_id, message_id, text, keyboard=None):
    if not message_id:          # Error 31/10: guard against None message_id
        send_message(chat_id, str(text or '')[:4096], keyboard)
        return None
    url  = f"{TELEGRAM_API}/editMessageText"
    text, keyboard = localize(chat_id, str(text or ''), keyboard)
    data = {"chat_id": chat_id, "message_id": message_id,
            "text": str(text or '')[:4096], "parse_mode": "Markdown"}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    for _attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                sleep(2 * (_attempt + 1))
                continue
            if e.code == 400:
                body = ''
                try: body = e.read().decode()[:200]
                except Exception: pass
                # "can't parse entities" = broken Markdown (e.g. special chars
                # in user-supplied text). Retry as plain text instead of
                # silently dropping the edit. Other 400s (message not
                # modified / too old) are still ignored.
                if "can't parse entities" in body.lower():
                    data_plain = {k: v for k, v in data.items() if k != "parse_mode"}
                    try:
                        req2 = urllib.request.Request(
                            url, data=json.dumps(data_plain).encode('utf-8'),
                            headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req2, timeout=30) as resp2:
                            print("⚠️  Edited as plain text after Markdown parse failure")
                            return json.loads(resp2.read().decode('utf-8'))
                    except Exception as e2:
                        print(f"Plain-text edit fallback also failed: {e2}")
                return None
            print(f"Edit HTTP {e.code}")
            return None
        except Exception as e:
            print(f"Edit error (attempt {_attempt+1}): {e}")
            if _attempt < 2:
                sleep(1)
    return None

def answer_callback(callback_id, text=None, show_alert=False):
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = localize(getattr(_fp_ctx, "uid", None), text, None)[0][:200]
    if show_alert:
        data["show_alert"] = True
    try:
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Answer callback error: {e}")

def send_document(chat_id, file_path, caption=None, filename=None):
    """
    Upload a local file to Telegram via sendDocument. Pure urllib multipart
    encoding — no extra dependencies (mirrors send_message's style).
    """
    file_path = Path(file_path)
    if caption:
        caption = localize(chat_id, caption, None)[0]
    if not file_path.exists():
        print(f"❌ send_document: file not found: {file_path}")
        return None

    filename = filename or file_path.name
    file_bytes = file_path.read_bytes()

    def _build_body(use_markdown):
        boundary = f"----HostingBotBoundary{secrets.token_hex(16)}"

        def _field(name, value):
            return (f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f'{value}\r\n').encode('utf-8')

        body = b""
        body += _field("chat_id", chat_id)
        if caption:
            body += _field("caption", str(caption)[:1024])
            if use_markdown:
                body += _field("parse_mode", "Markdown")

        body += (f'--{boundary}\r\n'
                 f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
        body += file_bytes
        body += f'\r\n--{boundary}--\r\n'.encode('utf-8')
        return boundary, body

    url = f"{TELEGRAM_API}/sendDocument"

    def _attempt_send(use_markdown):
        boundary, body = _build_body(use_markdown)
        req = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))

    for _attempt in range(3):
        try:
            return _attempt_send(use_markdown=True)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                sleep(2 * (_attempt + 1))
                continue
            try:
                err_body = e.read().decode()[:300]
            except Exception:
                err_body = ''
            print(f"❌ send_document HTTP {e.code}: {err_body}")
            # Bad/unbalanced caption Markdown must never mean total silence —
            # retry once as plain text (same pattern as send_message).
            if caption and "can't parse entities" in err_body.lower():
                try:
                    print("⚠️  Retrying send_document as plain-text caption")
                    return _attempt_send(use_markdown=False)
                except Exception as e2:
                    print(f"❌ send_document plain-text retry also failed: {e2}")
            try:
                return json.loads(err_body)  # {"ok": False, "description": "..."}
            except Exception:
                return {"ok": False, "description": f"HTTP {e.code}"}
        except Exception as e:
            print(f"❌ send_document error (attempt {_attempt+1}): {e}")
            if _attempt < 2:
                sleep(1)
    return {"ok": False, "description": "request failed after 3 attempts (see server logs)"}

def http_get(url, params=None):
    try:
        # The socket timeout must be longer than any long-poll "timeout"
        # param we send Telegram, or the connection can time out right as
        # Telegram's response arrives at the edge of its own wait window,
        # silently dropping that poll (getUpdates uses timeout=30).
        socket_timeout = 30
        if params and 'timeout' in params:
            socket_timeout = int(params['timeout']) + 10
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=socket_timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"HTTP error: {e}")
        return None

_FILENAME_PREFIX_RE = re.compile(r'^\d{8}_\d{6}_temp_\d+_')

def display_filename(system_filename: str) -> str:
    """
    Deployment files on disk are named like
    "20260720_211725_temp_7713987088_index.js" (timestamp + temp + user id
    prefix). That's fine for storage, but showing it raw to the user is both
    ugly AND risky: legacy Telegram Markdown's handling of underscores inside
    backtick-wrapped text is notoriously unreliable (well-documented — even
    content inside backticks isn't always treated as truly literal the way
    it should be), and this prefix always contains multiple underscores.
    Strip it for display; fall back to the full name if it doesn't match
    (e.g. GitHub deployments, which aren't named this way).
    """
    if not system_filename:
        return system_filename
    return _FILENAME_PREFIX_RE.sub('', system_filename)


def escape_md_v1(text: str) -> str:
    """
    Escape legacy-Markdown special characters in text that will appear
    OUTSIDE of backticks/code spans — per Telegram's own documented rule,
    prefix _, *, `, [ with a backslash. Do NOT use this on text that's
    already inside a backtick span (code spans have different, stricter
    rules — escaping inside them shows a literal backslash instead of
    hiding it).
    """
    if not text:
        return text
    return re.sub(r'([_*`\[])', r'\\\1', str(text))


def safe_md(text) -> str:
    """
    Make arbitrary text (file names, scanner findings) safe to embed in a
    Telegram legacy-Markdown message, including inside `backtick` spans.
    Backticks and backslashes are the only characters that can break out of
    (or be mangled inside) a code span, so they are neutralised; any other
    stray markup is still covered by send_message()/edit_message()'s
    plain-text fallback on "can't parse entities".
    """
    if text is None:
        return ''
    s = str(text).replace('\\', '/').replace('`', "'")
    return s


def sanitize_upload_filename(name: str, default: str = "upload") -> str:
    """
    Reduce a user-supplied file name to a safe basename: no directories,
    no '..', no control characters, only [A-Za-z0-9._-]. Prevents path
    traversal when the name is used to build a path under BASE_DIR.
    """
    name = os.path.basename(str(name or '').replace('\\', '/'))
    name = re.sub(r'[^A-Za-z0-9._-]', '_', name).lstrip('.')
    return name[:120] or default


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

# ==================== CHANNEL VERIFICATION ====================

_channel_config_warned = set()


def _warn_channel_config(channel, detail):
    """A channel the bot cannot read members of (bot not admin / wrong name). Tell admins ONCE."""
    print(f"⚠️  getChatMember({channel}): {detail} — treating as joined")
    if channel in _channel_config_warned:
        return
    _channel_config_warned.add(channel)
    try:
        notify_admin(
            f"⚠️ *Force-join misconfigured: {escape_md_v1(channel)}*\n\n"
            f"The bot can't read this channel's member list, so the check is being *skipped* "
            f"and everyone passes it.\n"
            f"Fix: add the bot as an *administrator* of {escape_md_v1(channel)} "
            f"and make sure the name is right.\n\n`{str(detail)[:150]}`")
    except Exception:
        pass


def _check_single_channel(user_id, channel) -> bool:
    """
    True if user_id is a member of ONE channel.
    - Bot-permission / bad-channel errors (config problems) count as joined so a misconfigured
      channel can never lock everybody out — admins are told once via _warn_channel_config.
    - Network failure after 3 tries counts as NOT joined (fail closed; the user can retry).
    """
    url = (f"{TELEGRAM_API}/getChatMember"
           f"?chat_id={urllib.parse.quote(channel)}&user_id={user_id}")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode('utf-8'))
                if data.get('ok'):
                    status = (data.get('result') or {}).get('status', '')
                    return status in ('member', 'administrator', 'creator', 'restricted')
                desc = (data.get('description') or '').lower()
                if any(kw in desc for kw in ('bot is not', 'not a member',
                                              'need admin', 'rights', 'forbidden')):
                    _warn_channel_config(channel, data.get('description'))
                    return True
                return False
        except urllib.error.HTTPError as e:
            body = ''
            try: body = e.read().decode()[:200]
            except Exception: pass
            if e.code in (400, 403):
                _warn_channel_config(channel, f"HTTP {e.code}: {body}")
                return True
            print(f"⚠️  getChatMember({channel}) HTTP {e.code}: {body}")
            if attempt < 2:
                sleep(1)
        except Exception as e:
            print(f"⚠️  getChatMember({channel}) attempt {attempt+1}: {e}")
            if attempt < 2:
                sleep(1)
    print(f"⚠️  getChatMember({channel}) failed after 3 attempts for {user_id} — denying (retry later)")
    return False


def get_missing_channels(user_id) -> list:
    """The required channels (dicts from REQUIRED_CHANNELS) this user has NOT joined yet."""
    return [ch for ch in REQUIRED_CHANNELS if not _check_single_channel(user_id, ch["id"])]


def check_channel_membership(user_id) -> bool:
    """True only when the user is a member of EVERY required channel (or no gate is configured)."""
    if not REQUIRED_CHANNELS:
        return True
    return not get_missing_channels(user_id)


def mark_channel_joined(user_id):
    """Record that the user passed the check, together with WHICH channel set they passed."""
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute('UPDATE users SET joined_channel = 1, channels_verified = ? WHERE user_id = ?',
                  (CHANNEL_SIGNATURE, user_id))
        conn.commit()
        conn.close()
        grant_referral_reward(user_id)         # the referrer is paid now that this user is verified
        return True
    except Exception as e:
        print(f"❌ mark_channel_joined: {e}")
        return False


def is_user_verified(user_id) -> bool:
    """
    True if the user has verified against the CURRENT list of required channels
    (or no channel is required). Admins are always considered verified.
    Someone who verified before a channel was added is NOT verified until they pass again.
    """
    if is_admin(user_id):
        return True
    if not REQUIRED_CHANNELS:
        return True
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute('SELECT joined_channel, channels_verified FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0] and row[1] == CHANNEL_SIGNATURE)
    except Exception:
        return False


def _join_buttons(channels):
    """One 'Join' button per channel that has a link."""
    return [[{"text": f"📢 Join {ch['id']}", "url": ch["link"]}] for ch in channels if ch.get("link")]


def required_channels_text() -> str:
    return ", ".join(ch["id"] for ch in REQUIRED_CHANNELS) or "our channel"


def send_verification_required(chat_id, user_id, first_name, message_id=None):
    chan_lines = "\n".join(f"📢 *{escape_md_v1(ch['id'])}*" for ch in REQUIRED_CHANNELS) or "📢 *our channel*"
    n = len(REQUIRED_CHANNELS)
    text = (
        f"*🔐 VERIFICATION REQUIRED*\n\n"
        f"👋 Hi {escape_md_v1(first_name) if first_name else 'there'}!\n\n"
        f"To use this hosting platform you must join {'all of our channels' if n > 1 else 'our channel'}:\n"
        f"{chan_lines}\n\n"
        f"*Steps:*\n"
        f"1️⃣ Tap each *Join* button below\n"
        f"2️⃣ Join {'every channel' if n > 1 else 'the channel'}\n"
        f"3️⃣ Come back and click *✅ VERIFY*"
    )
    keyboard = {"inline_keyboard": _join_buttons(REQUIRED_CHANNELS)
                + [[{"text": "✅ VERIFY", "callback_data": "verify_channel"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def has_accepted_tos(user_id) -> bool:
    """Admins are exempt, same as channel verification."""
    if is_admin(user_id):
        return True
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute('SELECT tos_accepted FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def mark_tos_accepted(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute('UPDATE users SET tos_accepted = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ mark_tos_accepted: {e}")
        return False


def show_tos_prompt(chat_id, user_id, message_id=None):
    text = (
        f"*📜 TERMS OF SERVICE*\n\n"
        f"Before you can use this hosting platform, please read and accept:\n\n"
        f"1️⃣ This service provides infrastructure to run code you upload or "
        f"link from GitHub — we do not review, monitor, or endorse the "
        f"content or purpose of anything you host.\n\n"
        f"2️⃣ *You are solely responsible for anything you deploy.* "
        f"You confirm you have the right to host it and that it does not "
        f"violate any applicable law.\n\n"
        f"3️⃣ *We are not responsible for any illegal file, bot, or content "
        f"hosted through this platform — responsibility lies entirely with "
        f"the user who uploaded or deployed it.*\n\n"
        f"4️⃣ We reserve the right to remove any deployment and suspend any "
        f"account found to violate these terms, without notice.\n\n"
        f"5️⃣ Continued use of this bot after clicking Agree constitutes "
        f"acceptance of these terms.\n\n"
        f"👇 You must agree to continue."
    )
    keyboard = {"inline_keyboard": [[{"text": "✅ I Agree", "callback_data": "tos_agree"}]]}
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
        return False, f"❌ You must join {required_channels_text()} first!"
    
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
    async_backup(f"redeem_{user_id}")
    return True, f"✅ Redeemed: {', '.join(reward_msg)}!"

# ==================== REFERRAL SYSTEM ====================

def get_or_create_referral_code(user_id):
    """Return (and lazily create) a user's unique referral code."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    code = row[0] if row and row[0] else None
    if not code:
        code = f"ref{user_id}"
        c.execute("UPDATE users SET referral_code = ? WHERE user_id = ?", (code, user_id))
        conn.commit()
    conn.close()
    return code

def get_bot_username():
    """Fetch the bot's @username for building referral links."""
    try:
        with urllib.request.urlopen(f"{TELEGRAM_API}/getMe", timeout=10) as r:
            data = json.loads(r.read())
            return data.get("result", {}).get("username", "")
    except Exception:
        return ""

def process_referral(referrer_id, new_user_id):
    """Credit referrer when a new user joins via their link. Safe to call multiple times."""
    if referrer_id == new_user_id:
        return False
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    # Ensure the referrer exists
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
    if not c.fetchone():
        conn.close()
        return False
    # One referral per new user
    try:
        c.execute(
            "INSERT INTO referrals (referrer_id, referred_id, created_at, reward_coins, reward_given) "
            "VALUES (?, ?, ?, ?, 0)",
            (referrer_id, new_user_id, datetime.now().isoformat(), REFERRAL_REWARD_COINS))
        c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, new_user_id))
        c.execute("UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id = ?", (referrer_id,))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        conn.close()
        return False  # user already referred
    # The reward is NOT paid yet — it is released by grant_referral_reward() once the new user has
    # actually verified (joined the required channels). That stops throw-away accounts farming coins.
    async_backup(f"referral_{referrer_id}")
    return True


def grant_referral_reward(new_user_id):
    """Pay the referrer of `new_user_id` — once, and only after the new user is verified."""
    try:
        row = db_execute("SELECT referrer_id FROM referrals WHERE referred_id = ? AND reward_given = 0",
                         (new_user_id,), fetch='one')
        if not row:
            return False
        referrer_id = int(row[0])
        if is_banned(referrer_id) or is_banned(new_user_id):
            return False
        conn = sqlite3.connect(DATABASE_FILE)
        cur = conn.execute("UPDATE referrals SET reward_given = 1 WHERE referred_id = ? AND reward_given = 0",
                           (new_user_id,))
        conn.commit()
        claimed = cur.rowcount
        conn.close()
        if not claimed:
            return False                      # someone else already paid it
        update_user_coins(referrer_id, REFERRAL_REWARD_COINS, "referral_reward", f"referred_{new_user_id}")
        async_backup(f"referral_paid_{referrer_id}")
        try:
            send_message(referrer_id,
                f"🎉 *Referral Bonus!*\n\nYour invited friend verified their account.\n"
                f"You earned *{REFERRAL_REWARD_COINS} 🪙* coins.\n\nKeep sharing to earn more!",
                {"inline_keyboard": [[{"text": "👥 My Referrals", "callback_data": "my_referral"}]]})
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"⚠️ grant_referral_reward: {e}")
        return False

def show_referral_menu(chat_id, user_id, message_id=None):
    code    = get_or_create_referral_code(user_id)
    bot_username = get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start={code}" if bot_username else f"Your code: `{code}`"

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COALESCE(SUM(reward_coins),0) FROM referrals WHERE referrer_id = ? AND reward_given = 1", (user_id,))
    row = c.fetchone()
    total_refs   = row[0] or 0
    total_earned = row[1] or 0
    c.execute("SELECT referred_id, created_at FROM referrals WHERE referrer_id = ? ORDER BY created_at DESC LIMIT 5", (user_id,))
    recent = c.fetchall()
    conn.close()

    recent_text = ""
    for rid, rat in recent:
        dt = datetime.fromisoformat(rat).strftime("%d %b")
        recent_text += f"  • User `{rid}` — {dt}\n"

    text = (
        f"*👥 REFERRAL PROGRAMME*\n\n"
        f"Invite friends and earn *{REFERRAL_REWARD_COINS} 🪙* for every friend who joins *and verifies*!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 *Your referral link:*\n`{ref_link}`\n\n"
        f"📊 *Your stats:*\n"
        f"  👥 Total referrals: `{total_refs}`\n"
        f"  🪙 Total earned:    `{total_earned}🪙`\n\n"
        + (f"🕐 *Recent referrals:*\n{recent_text}\n" if recent_text else "")
        + f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Share your link — every new user who starts the bot through it earns you coins!"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "📤 Share Link", "switch_inline_query": f"Join this bot hosting platform! {ref_link}"}],
        [{"text": "🏆 Leaderboard", "callback_data": "p2_lb"}],
        [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
    ]}
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

# ==================== BUG REPORT SYSTEM ====================

def submit_bug_report(user_id, username, first_name, message_text):
    """Save a bug report and broadcast it to all admins."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO bug_reports (user_id, username, first_name, message, status, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (user_id, username, first_name, message_text, datetime.now().isoformat()))
    report_id = c.lastrowid
    conn.commit()
    conn.close()

    # Notify all admins
    user_link = f"@{username}" if username else f"User `{user_id}`"
    admin_text = (
        f"*🐛 NEW BUG REPORT #{report_id}*\n\n"
        f"From: {user_link} (`{user_id}`)\n"
        f"Time: `{datetime.now().strftime('%Y-%m-%d %H:%M')}`\n\n"
        f"*Message:*\n{message_text}"
    )
    admin_kb = {"inline_keyboard": [
        [{"text": f"✉️ Reply to #{report_id}", "callback_data": f"bug_reply_{report_id}"}],
        [{"text": f"✅ Close #{report_id}",    "callback_data": f"bug_close_{report_id}"}]
    ]}
    for admin_id in ADMIN_IDS:
        send_message(admin_id, admin_text, admin_kb)

    async_backup(f"bug_report_{report_id}")
    return report_id

def reply_to_bug_report(report_id, admin_id, reply_text):
    """Save admin reply, mark report replied, and notify the user."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name FROM bug_reports WHERE report_id = ?", (report_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False, "Report not found"
    target_user_id, username, first_name = row
    c.execute(
        "UPDATE bug_reports SET status='replied', admin_reply=?, replied_by=?, replied_at=? "
        "WHERE report_id = ?",
        (reply_text, admin_id, datetime.now().isoformat(), report_id))
    conn.commit()
    conn.close()

    # Notify the user
    send_message(target_user_id,
        f"*✉️ REPLY TO YOUR BUG REPORT #{report_id}*\n\n"
        f"An admin has replied to your report:\n\n"
        f"_{reply_text}_\n\n"
        f"Thank you for helping us improve the service! 🙏",
        {"inline_keyboard": [[{"text": "🎫 Open a support ticket", "callback_data": "p2_tk"}],
                              [{"text": "🏠 Main Menu",            "callback_data": "main_menu"}]]})
    return True, target_user_id

def show_admin_bug_reports(chat_id, admin_id, message_id=None):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT report_id, user_id, username, first_name, message, status, created_at "
              "FROM bug_reports ORDER BY created_at DESC LIMIT 20")
    reports = c.fetchall()
    c.execute("SELECT COUNT(*) FROM bug_reports WHERE status = 'open'")
    open_count = c.fetchone()[0]
    conn.close()

    if not reports:
        text = "*📋 BUG REPORTS*\n\nNo reports yet."
        kb   = {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]}
        if message_id: edit_message(chat_id, message_id, text, kb)
        else:          send_message(chat_id, text, kb)
        return

    text = f"*📋 BUG REPORTS* ({open_count} open)\n\n"
    buttons = []
    for rep_id, uid, uname, fname, msg, status, created in reports:
        icon   = "🔴" if status == "open" else "✅" if status == "replied" else "⚫"
        who    = f"@{uname}" if uname else f"#{uid}"
        dt     = datetime.fromisoformat(created).strftime("%d/%m %H:%M")
        preview = msg[:40].replace("\n", " ") + ("…" if len(msg) > 40 else "")
        text  += f"{icon} *#{rep_id}* {who} — {dt}\n   _{preview}_\n\n"
        row_btns = [{"text": f"✉️ #{rep_id}", "callback_data": f"bug_reply_{rep_id}"},
                    {"text": f"✅ Close",      "callback_data": f"bug_close_{rep_id}"}]
        buttons.append(row_btns)

    buttons.append([{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}])
    kb = {"inline_keyboard": buttons}
    if message_id: edit_message(chat_id, message_id, text, kb)
    else:          send_message(chat_id, text, kb)
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
                f"*⭐ PREMIUM MEMBER*\n\n"
                f"✅ Plan: `{plan.upper()}`\n"
                f"📅 Expires: `{expires.strftime('%Y-%m-%d')}`\n"
                f"⏰ Days left: `{days_left}`\n"
                f"⏸️ Paused: `{paused_count}`\n\n"
                f"✨ Benefits active!\n\n"
                f"💰 Monthly/Yearly deployments are *FREE* for you!"
            )
            keyboard = {"inline_keyboard": [[{"text": "📈 Resource Plans", "callback_data": "resource_tiers"}],
                                            [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]]}
        else:
            text = "❌ Premium expired. Renew now!"
            keyboard = {"inline_keyboard": [[{"text": "💰 Renew Premium", "callback_data": "subscribe_premium"}]]}
    else:
        return show_resource_tiers_menu(chat_id, user_id, message_id)
        text = (
            f"*⭐ PREMIUM SUBSCRIPTION*\n\n"
            f"✨ *Benefits:*\n"
            f"• ✅ Unlimited free deployments (24h)\n"
            f"• ✅ FREE Monthly/Yearly deployments\n"
            f"• ✅ Priority support\n"
            f"• ✅ Auto-resume paused bots\n\n"
            f"⏸️ Paused: `{paused_count}`\n\n"
            f"💰 *Pricing:*\n"
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
                [{"text": "📈 Resource Plans (512 MB - 4 GB)", "callback_data": "resource_tiers"}],
                [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
            ]
        }
    
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

# ==================== RESOURCE TIERS (purchase) ====================
def tier_price_stars(key, period="m"):
    return RESOURCE_TIERS[key][2] * TIER_PERIODS[period][2]


def tier_price_coins(key, period="m"):
    return tier_price_stars(key, period) * STARS_PER_COIN


def show_resource_tiers_menu(chat_id, user_id, message_id=None):
    """Step 1: pick a SIZE (the only way to get Premium: monthly/yearly is chosen next)."""
    if is_admin(user_id):
        text = "👑 Admins already have the whole resource pool."
        kb = {"inline_keyboard": [[{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]]}
    else:
        cur = get_user_tier(user_id)
        used = get_user_resource_usage_mb(user_id)
        quota = get_user_quota_mb(user_id)
        if cur:
            cur_line = (f"✅ Your plan: *{RESOURCE_TIERS.get(cur[0], (0, format_resource(cur[1]), 0))[1]}* "
                        f"until `{cur[2].strftime('%Y-%m-%d')}`\n")
        else:
            cur_line = f"🆓 You're on the shared pool: `{format_resource(quota)}`\n"
        free_mb = tier_unallocated_mb(user_id)
        avail = {k: tier_capacity_ok(user_id, v[0]) for k, v in RESOURCE_TIERS.items()}
        lines = [f"• *{label}* — `{stars}⭐`/month · `{tier_price_stars(key, 'y')}⭐`/year"
                 + ("" if avail[key] else "  🚫 _sold out_")
                 for key, (mb, label, stars) in RESOURCE_TIERS.items()]
        text = (f"*📈 RESOURCE PLANS*\n\n{cur_line}"
                f"💾 In use: `{format_resource(used)} / {format_resource(quota)}`\n"
                f"🧮 Available to buy: `{format_resource(free_mb)}`"
                + ("" if any(avail.values()) else " _(none right now)_") + "\n\n"
                f"Pick a size for a *guaranteed* amount of RAM that doesn't shrink when more users join. "
                f"Every plan includes *Premium* (free monthly deployments; yearly also covers yearly "
                f"deployments).\n\n" + "\n".join(lines) +
                f"\n\n_Yearly = {TIER_YEARLY_MULTIPLIER}× the monthly price. Buying again extends your time; "
                f"picking a different size switches to it (remaining time carries over)._")
        rows = []
        for key, (mb, label, stars) in RESOURCE_TIERS.items():
            if not avail[key]:
                rows.append([{"text": f"🚫 {label} · sold out", "callback_data": "resource_tiers"}])
                continue
            mark = "✅ " if cur and cur[0] == key else ""
            rows.append([{"text": f"{mark}{label} — from {stars}⭐", "callback_data": f"tier_pick_{key}"}])
        rows.append([{"text": "🔙 Back to Menu", "callback_data": "main_menu"}])
        kb = {"inline_keyboard": rows}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def show_tier_period_menu(chat_id, user_id, key, message_id=None):
    """Step 2: monthly or yearly for the chosen size, with prices."""
    why = _tier_blocked_reason(user_id, key)
    if why:
        back = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "resource_tiers"}]]}
        if message_id:
            edit_message(chat_id, message_id, f"❌ {why}", back)
        else:
            send_message(chat_id, f"❌ {why}", back)
        return
    mb, label, stars = RESOURCE_TIERS[key]
    m_s, y_s = tier_price_stars(key, "m"), tier_price_stars(key, "y")
    saving = m_s * 12 - y_s
    text = (f"*📈 {label.upper()} PLAN*\n\n"
            f"💾 Guaranteed RAM: `{label}`\n\n"
            f"📅 *Monthly* (30 days): `{m_s}⭐` / `{tier_price_coins(key, 'm')}🪙`\n"
            f"🌟 *Yearly* (365 days): `{y_s}⭐` / `{tier_price_coins(key, 'y')}🪙`"
            + (f"  — saves `{saving}⭐` vs 12 months" if saving > 0 else "") +
            f"\n\nChoose how long and how to pay:")
    kb = {"inline_keyboard": [
        [{"text": f"⭐ Monthly {m_s}⭐", "callback_data": f"tier_stars_{key}_m"},
         {"text": f"🪙 Monthly {tier_price_coins(key, 'm')}🪙", "callback_data": f"tier_coins_{key}_m"}],
        [{"text": f"⭐ Yearly {y_s}⭐", "callback_data": f"tier_stars_{key}_y"},
         {"text": f"🪙 Yearly {tier_price_coins(key, 'y')}🪙", "callback_data": f"tier_coins_{key}_y"}],
        [{"text": "🔙 Back", "callback_data": "resource_tiers"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def activate_resource_tier(user_id, key, period, stars_paid, coins_paid):
    """Grant/extend a tier for a monthly (30 d) or yearly (365 d) period. Same size: time is added on top
    of what is left. Different size: switch, keeping the remaining time. Premium of the same length is
    included (a Yearly premium holder is never downgraded to Monthly)."""
    mb, label, _price = RESOURCE_TIERS[key]
    pname, days, _mult = TIER_PERIODS[period]
    now = datetime.now()
    cur = get_user_tier(user_id)
    base = cur[2] if cur and cur[2] > now else now
    end = base + timedelta(days=days)
    db_execute("UPDATE users SET resource_tier_key=?, resource_tier_mb=?, resource_tier_expires=? WHERE user_id=?",
               (key, mb, end.isoformat(), user_id))
    prow = db_execute("SELECT premium_plan FROM users WHERE user_id = ?", (user_id,), fetch='one')
    holds_yearly = bool(is_user_premium(user_id) and prow and (prow[0] or "").lower() == "yearly")
    if holds_yearly and period == "m":
        db_execute("INSERT INTO subscriptions (user_id, plan, amount_stars, amount_coins, start_date, end_date, status) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (user_id, f"tier_{key}_{period}", stars_paid, coins_paid, now.isoformat(), end.isoformat(), 'active'))
    else:
        activate_premium(user_id, pname.lower(), stars_paid, coins_paid, days)
        db_execute("UPDATE subscriptions SET plan = ? WHERE subscription_id = "
                   "(SELECT MAX(subscription_id) FROM subscriptions WHERE user_id = ?)",
                   (f"tier_{key}_{period}", user_id))
    try:
        notify_admin(f"📈 RESOURCE PLAN\nUser: {user_id}\nPlan: {label} {pname} until {end.strftime('%Y-%m-%d')}\n"
                     f"Paid: {stars_paid}⭐ / {coins_paid}🪙")
    except Exception:
        pass
    async_backup(f"tier_{key}_{period}_{user_id}")
    return end


def _tier_blocked_reason(user_id, key):
    if key not in RESOURCE_TIERS:
        return "Unknown plan."
    if is_admin(user_id):
        return "Admins already have the whole resource pool."
    if not tier_capacity_ok(user_id, RESOURCE_TIERS[key][0]):
        left = format_resource(tier_unallocated_mb(user_id))
        return (f"This size is sold out — only {left} of the host's RAM is unallocated. "
                f"Try a smaller plan or check back later.")
    return None


def purchase_tier_stars(chat_id, user_id, key, period="m"):
    if period not in TIER_PERIODS:
        return
    why = _tier_blocked_reason(user_id, key)
    if why:
        send_message(chat_id, f"❌ {why}")
        return
    mb, label, _ = RESOURCE_TIERS[key]
    pname, days, _m = TIER_PERIODS[period]
    stars = tier_price_stars(key, period)
    payload = f"tier_{key}_{period}_{user_id}_{int(datetime.now().timestamp())}"
    invoice = {"chat_id": chat_id, "title": f"{label} Resource Plan — {pname}",
               "description": f"{label} guaranteed RAM for {days} days + Premium",
               "payload": payload, "currency": "XTR",
               "prices": [{"label": f"{label} {pname}", "amount": stars}]}
    try:
        req = urllib.request.Request(f"{TELEGRAM_API}/sendInvoice", data=json.dumps(invoice).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())
        if not res.get('ok'):
            send_message(chat_id, f"❌ Failed to create invoice: {res.get('description')}")
    except Exception as e:
        send_message(chat_id, f"❌ Failed to create invoice: {e}")


def purchase_tier_coins(chat_id, user_id, key, period="m"):
    if period not in TIER_PERIODS:
        return
    why = _tier_blocked_reason(user_id, key)
    if why:
        send_message(chat_id, f"❌ {why}")
        return
    mb, label, _ = RESOURCE_TIERS[key]
    pname = TIER_PERIODS[period][0]
    stars = tier_price_stars(key, period)
    cost = tier_price_coins(key, period)
    bal = get_user_balances(user_id)
    if bal['coins'] < cost:
        send_message(chat_id,
            f"❌ *INSUFFICIENT COINS*\n\nRequired: `{cost}🪙`\nYour balance: `{bal['coins']}🪙`",
            {"inline_keyboard": [[{"text": f"⭐ Pay {stars}⭐", "callback_data": f"tier_stars_{key}_{period}"},
                                  {"text": "🎫 Redeem Code", "callback_data": "redeem_code"}]]})
        return
    update_user_coins(user_id, -cost, "resource_tier", f"tier_{key}_{period}")
    end = activate_resource_tier(user_id, key, period, stars, cost)
    apply_discount_cashback(user_id, cost)
    send_message(chat_id,
        f"✅ *{label} {pname.upper()} PLAN ACTIVE*\n\n💾 Guaranteed: `{label}`\n📅 Until: `{end.strftime('%Y-%m-%d')}`\n"
        f"Paid: `{cost}🪙`\n\nThank you! 🙏",
        {"inline_keyboard": [[{"text": "🏠 Main Menu", "callback_data": "main_menu"}]]})


def purchase_premium_stars(chat_id, user_id, plan, duration_days, cost_stars):
    success, error = create_premium_invoice(chat_id, user_id, plan, duration_days, cost_stars)
    if success:
        send_message(chat_id, f"⭐ *Stars Payment Required*\n\nPlan: {plan.upper()}\nCost: {cost_stars}⭐\n\nPlease complete the payment using the invoice above.\n\nYour premium will activate automatically after payment.")
    else:
        send_message(chat_id, f"❌ Failed to create invoice: {error}")

def purchase_premium_coins(chat_id, user_id, plan, duration_days, cost_coins):
    balances = get_user_balances(user_id)
    
    if balances['coins'] >= cost_coins:
        update_user_coins(user_id, -cost_coins, "premium_subscription", f"plan_{plan}")
        success, resumed = activate_premium(user_id, plan, cost_coins // STARS_PER_COIN, cost_coins, duration_days)
        
        if success:
            apply_discount_cashback(user_id, cost_coins)
            resume_msg = f"\n\n✅ Resumed {resumed} paused deployment(s)!" if resumed > 0 else ""
            send_message(chat_id,
                f"✅ *PREMIUM ACTIVATED!*{resume_msg}\n\n"
                f"Plan: `{plan.upper()}`\n"
                f"Duration: `{duration_days}` days\n"
                f"Paid: `{cost_coins}🪙`\n\n"
                f"Thank you! 🙏",
                {"inline_keyboard": [[{"text": "🏠 Main Menu", "callback_data": "main_menu"}]]})
        else:
            send_message(chat_id, "❌ Failed to activate premium.")
    else:
        send_message(chat_id,
            f"❌ *INSUFFICIENT COINS*\n\nRequired: `{cost_coins}🪙`\nYour balance: `{balances['coins']}🪙`\n\nUse redeem codes or pay with Stars!",
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
# ── Modules that ship with Python: they are imported, never pip-installed ─────────────────────
_STDLIB_FALLBACK = set("""
__future__ abc aifc argparse array ast asynchat asyncio asyncore atexit audioop base64 bdb binascii bisect
builtins bz2 calendar cgi cgitb chunk cmath cmd code codecs codeop collections colorsys compileall concurrent
configparser contextlib contextvars copy copyreg cProfile crypt csv ctypes curses dataclasses datetime dbm
decimal difflib dis distutils doctest email encodings ensurepip enum errno faulthandler fcntl filecmp fileinput
fnmatch fractions ftplib functools gc getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html
http idlelib imaplib imghdr imp importlib inspect io ipaddress itertools json keyword lib2to3 linecache locale
logging lzma mailbox mailcap marshal math mimetypes mmap modulefinder msilib msvcrt multiprocessing netrc nis
nntplib ntpath numbers opcode operator optparse os ossaudiodev pathlib pdb pickle pickletools pipes pkgutil
platform plistlib poplib posix posixpath pprint profile pstats pty pwd py_compile pyclbr pydoc queue quopri
random re readline reprlib resource rlcompleter runpy sched secrets select selectors shelve shlex shutil signal
site smtpd smtplib sndhdr socket socketserver spwd sqlite3 ssl stat statistics string stringprep struct
subprocess sunau symtable sys sysconfig syslog tabnanny tarfile telnetlib tempfile termios textwrap threading
time timeit tkinter token tokenize tomllib trace traceback tracemalloc tty turtle types typing unicodedata
unittest urllib uu uuid venv warnings wave weakref webbrowser winreg winsound wsgiref xdrlib xml xmlrpc
zipapp zipfile zipimport zlib zoneinfo
""".split())
_STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", ())) | _STDLIB_FALLBACK
# ancient PyPI backports named like stdlib modules — installing them BREAKS Python
_STDLIB_MODULES |= {"sqlite", "futures", "enum34", "pathlib2"}
_STDLIB_LOWER = {m.lower() for m in _STDLIB_MODULES}


def is_stdlib_requirement(spec: str) -> bool:
    """True for a requirement line that names a built-in module (sqlite3, asyncio, json, pickle…)."""
    base = re.split(r'[=<>!~\[\s;@]', str(spec).strip(), 1)[0].strip()
    return bool(base) and base.lower().replace('-', '_') in _STDLIB_LOWER


def drop_stdlib_requirements(specs, update_logs=None):
    """Remove built-in modules from a requirement list (with a note), keeping everything else."""
    kept, dropped = [], []
    for sp in specs:
        (dropped if is_stdlib_requirement(sp) else kept).append(sp)
    if dropped and update_logs:
        update_logs(f"ℹ️ Built into Python — nothing to install: {', '.join(sorted(set(d.strip() for d in dropped)))}")
    return kept


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
        
        # Only add --pre when the version spec explicitly requests a pre-release (e.g. ==1.0a1, ==2.0b3, ==1.0rc1)
        if re.search(r'[=<>!]=?\s*\d[\d.]*\s*(a\d|b\d|rc\d|\.dev|\.post)', dependency.lower()):
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

    IMPORT_MAPPING = {
        'flask': 'flask', 'django': 'django', 'fastapi': 'fastapi',
        'aiohttp': 'aiohttp', 'tornado': 'tornado', 'sanic': 'sanic',
        # Pinned floor, not just a bare name: versions before 21.7 have a
        # confirmed __slots__/__dict__ AttributeError on modern Python
        # (github.com/python-telegram-bot/python-telegram-bot/issues/4127).
        # Auto-detection has no way to know which version pip would
        # otherwise pick, so pin a known-safe minimum explicitly.
        'telegram': 'python-telegram-bot>=22.0', 'aiogram': 'aiogram',
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
    def scan_imports(cls, code_content: str, update_logs: callable, local_dir=None) -> list:
        """Automatically detect required dependencies by scanning code.
        Built-in modules (sqlite3, asyncio…) and the user's own files/folders (local_dir) are never
        treated as pip packages."""
        detected = set()
        lines = code_content.split('\n')
        local_names = set()
        try:
            if local_dir:
                for it in Path(local_dir).iterdir():
                    if it.name in ('packages', 'node_modules') or it.name.startswith('.'):
                        continue
                    local_names.add(it.stem if it.is_file() else it.name)
        except Exception:
            pass
        
        for line in lines:
            line = line.strip()
            match = re.match(r'^(?:from|import)\s+([a-zA-Z0-9_\.]+)', line)
            if match:
                module = match.group(1).split('.')[0]
                if module.lower() in _STDLIB_LOWER or module in local_names:
                    continue
                if module in cls.IMPORT_MAPPING:
                    package = cls.IMPORT_MAPPING[module]
                    if package and package not in detected:
                        detected.add(package)
                        update_logs(f"   🔍 Detected: {module} → {package}")
                elif module not in ['os', 'sys', 'time', 'datetime', 'json', 're', 'math', 'random', 
                                     'string', 'collections', 'itertools', 'functools', 'typing', 'pathlib',
                                     'tempfile', 'subprocess', 'threading', 'multiprocessing', 'socket',
                                     'ssl', 'hashlib', 'base64', 'zipfile', 'tarfile', 'shutil', 'glob',
                                     'io', 'abc', 'copy', 'enum', 'struct', 'queue', 'weakref',
                                     'contextlib', 'dataclasses', 'inspect', 'logging', 'warnings',
                                     'argparse', 'configparser', 'csv', 'html', 'http', 'urllib',
                                     'uuid', 'decimal', 'fractions', 'statistics', 'operator',
                                     'concurrent', 'asyncio', 'signal', 'platform', 'traceback',
                                     'pprint', 'textwrap', 'binascii', 'hmac', 'secrets']:
                    # Only add if it looks like a real installable package (not a relative import or internal module)
                    if module and not module.startswith('_') and len(module) > 1:
                        guessed = module.replace('_', '-')
                        if module == 'bs4':
                            guessed = 'beautifulsoup4'
                        elif module == 'cv2':
                            guessed = 'opencv-python'
                        elif module == 'PIL':
                            guessed = 'Pillow'
                        elif module == 'sklearn':
                            guessed = 'scikit-learn'
                        elif module == 'wx':
                            guessed = 'wxPython'
                        # Only guess if it's plausibly a PyPI package name
                        if guessed and re.match(r'^[a-zA-Z][a-zA-Z0-9\-\.]+$', guessed):
                            detected.add(guessed)
                            update_logs(f"   🔍 Guessed: {module} → {guessed}")
        
        return list(detected)

    # Known cases where the PyPI package name doesn't match what people
    # naturally write for `import X` — most critically "telegram", which is
    # a real but completely unrelated PyPI package. Installing it alongside
    # python-telegram-bot corrupts the telegram/ module (mixed files from
    # two different packages) and produces cryptic __slots__/__dict__
    # AttributeErrors deep inside PTB's Updater — a well-known trap for
    # anyone who writes "telegram" in requirements.txt expecting PTB.
    _WRONG_PACKAGE_FIXES = {
        'telegram': 'python-telegram-bot>=22.0',
        'discord': 'discord.py',
        'cv2': 'opencv-python',
        'bs4': 'beautifulsoup4',
        'PIL': 'Pillow',
        'sklearn': 'scikit-learn',
    }

    @classmethod
    def scan_requirements_file(cls, content: str, update_logs: callable) -> list:
        requirements = []
        for line in content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('-r'):
                if ';' in line:
                    line = line.split(';')[0].strip()

                # Correct known wrong package names, preserving any version pin
                _base = re.split(r'[=<>!~\[]', line, 1)[0].strip()
                if _base in cls._WRONG_PACKAGE_FIXES:
                    _correct = cls._WRONG_PACKAGE_FIXES[_base]
                    update_logs(f"   ⚠️ '{_base}' is the wrong package for this import — "
                                f"using '{_correct}' instead")
                    line = _correct

                if is_stdlib_requirement(line):
                    update_logs(f"   ℹ️ '{line[:40]}' is built into Python — skipping (nothing to install)")
                    continue
                requirements.append(line)
                update_logs(f"   📄 From requirements: {line[:60]}")
        return requirements

# ==================== NODE.JS DEPENDENCY DETECTION ====================
# Mirrors UniversalDependencyInstaller.scan_imports, but for JavaScript's
# require()/import syntax instead of Python's import/from — the upload
# deploy path previously had zero Node.js dependency support at all: no way
# to submit a package.json, no detection of what a .js file actually needs,
# and nothing ever wrote package.json to disk for npm to find.
_JS_BUILTIN_MODULES = {
    'fs', 'path', 'http', 'https', 'os', 'crypto', 'util', 'events',
    'stream', 'url', 'querystring', 'child_process', 'assert', 'buffer',
    'net', 'dns', 'tls', 'zlib', 'readline', 'process', 'timers',
    'cluster', 'worker_threads', 'perf_hooks', 'v8', 'vm', 'module', 'dgram',
}

def scan_js_requires(code_content: str) -> list:
    """Detect npm packages actually referenced by a Node.js script."""
    found = set()
    # require('pkg') / require("pkg")
    for m in re.finditer(r'require\(\s*[\'"]([^\'"./][^\'"]*)[\'"]\s*\)', code_content):
        found.add(m.group(1))
    # ES module: import ... from 'pkg'
    for m in re.finditer(r'''\bfrom\s+['"]([^'"./][^'"]*)['"]''', code_content):
        found.add(m.group(1))
    # import 'pkg'; (side-effect only import)
    for m in re.finditer(r'''^\s*import\s+['"]([^'"./][^'"]*)['"]''', code_content, re.M):
        found.add(m.group(1))

    packages = set()
    for name in found:
        # Scoped packages (@org/pkg) or subpaths (pkg/sub) — keep only the
        # installable package root, not the specific file within it.
        if name.startswith('@'):
            parts = name.split('/')
            pkg = '/'.join(parts[:2]) if len(parts) >= 2 else name
        else:
            pkg = name.split('/')[0]
        if pkg in _JS_BUILTIN_MODULES or pkg.startswith('node:'):
            continue
        packages.add(pkg)
    return sorted(packages)


def build_package_json(deploy_folder: Path, packages: list, main_file_name: str, update_logs=None) -> Path:
    """Write a minimal package.json for auto-detected dependencies."""
    pkg_json = {
        "name": "hosted-bot",
        "version": "1.0.0",
        "private": True,
        "main": main_file_name,
        "dependencies": {pkg: "*" for pkg in packages}
    }
    path = deploy_folder / "package.json"
    path.write_text(json.dumps(pkg_json, indent=2))
    if update_logs:
        update_logs(f"📦 Generated package.json with {len(packages)} package(s): {', '.join(packages)}")
    return path


# ==================== VPS DEPENDENCY ENGINE ====================
# Supports every package manager a real hosting server would handle.

import shutil as _shutil

# ── helpers ──────────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, timeout=300, env_extra=None):
    """Run a shell command, return (ok, stdout+stderr text)."""
    import os as _os
    env = _os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=cwd, env=env)
        out = (r.stdout or '') + (r.stderr or '')
        return r.returncode == 0, out.strip()
    except subprocess.TimeoutExpired:
        return False, f"Timeout after {timeout}s"
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"
    except Exception as e:
        return False, str(e)

def _cmd_exists(name):
    return _shutil.which(name) is not None

def _log(update_logs, msg):
    if update_logs:
        update_logs(msg)
    else:
        print(msg)


def _normalize_pkg_name(name):
    return re.sub(r'[-_.]+', '-', name).strip().lower()


def _clean_stale_package(packages_dir, pkg_spec):
    """
    Remove any existing installed files for a package before (re)installing it.

    `pip install --target` does not properly clean up on upgrade the way a
    normal site-packages install does — if a package's file layout changes
    between versions, files removed in the new version can be left behind
    from the old one, producing a package with files mixed across two
    versions. This is a well-known, well-documented cause of cryptic
    AttributeErrors deep inside libraries with __slots__-based classes (e.g.
    python-telegram-bot's Updater) that have nothing to do with the user's
    own code. Best-effort and silent — never blocks the actual install.
    """
    try:
        packages_dir = Path(packages_dir)
        if not packages_dir.exists():
            return
        base = re.split(r'[=<>!~\[\s]', pkg_spec.strip(), 1)[0].strip()
        if not base or base.startswith(('git+', 'hg+', 'svn+', 'http://', 'https://', '.', '/')):
            return  # not a simple named package spec — nothing to clean by name
        target_norm = _normalize_pkg_name(base)

        for item in list(packages_dir.iterdir()):
            name = item.name
            if not (name.endswith('.dist-info') or name.endswith('.egg-info')):
                continue
            dist_base = re.sub(r'-[^-]+\.(dist-info|egg-info)$', '', name)
            if _normalize_pkg_name(dist_base) != target_norm:
                continue

            record = item / 'RECORD'
            if record.exists():
                try:
                    for line in record.read_text(errors='ignore').splitlines():
                        rel = line.split(',')[0].strip()
                        if not rel or rel.startswith('..'):
                            continue
                        f = packages_dir / rel
                        try:
                            if f.is_file() or f.is_symlink():
                                f.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass
            shutil.rmtree(item, ignore_errors=True)
    except Exception:
        pass


def _pip_base(packages_dir=None):
    cmd = [sys.executable, '-m', 'pip', 'install',
           '--quiet', '--no-warn-script-location',
           '--disable-pip-version-check', '--no-color']
    if packages_dir:
        cmd += ['--target', str(packages_dir)]
    return cmd


# ── per-package-manager installers ───────────────────────────────────────────

def _install_pip(packages_dir, deploy_folder, update_logs):
    """Install Python packages from every requirements variant."""
    installed = 0

    # Upgrade pip first (once, silently)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '--upgrade', 'pip',
                    '--disable-pip-version-check'], capture_output=True, timeout=60)

    # All requirements file variants, in priority order
    req_variants = [
        'requirements.txt', 'requirements-dev.txt', 'requirements-prod.txt',
        'requirements_dev.txt', 'requirements_prod.txt',
        'requirements/base.txt', 'requirements/main.txt',
        'requirements/prod.txt',  'requirements/dev.txt',
    ]

    for fname in req_variants:
        rp = deploy_folder / fname
        if not rp.exists():
            continue
        _log(update_logs, f"   📄 {fname}")
        raw = rp.read_text(errors='ignore').strip()
        pkgs = [ln.split(';')[0].strip() for ln in raw.splitlines()
                if ln.strip() and not ln.startswith(('#', '-r ', '-c ', '--'))]
        pkgs = drop_stdlib_requirements(pkgs, update_logs)
        if not pkgs:
            continue

        _log(update_logs, f"   📦 {len(pkgs)} package(s) from {fname}")
        for i, pkg in enumerate(pkgs):
            _log(update_logs, f"   ⬇️  [{i+1}/{len(pkgs)}] {pkg[:70]}")
            _clean_stale_package(packages_dir, pkg)
            # Try with --upgrade first, then a clean forced reinstall on failure
            # (never trust partially-written files from a failed first attempt)
            ok, out = _run(_pip_base(packages_dir) + ['--upgrade', pkg], timeout=PIP_TIMEOUT)
            if not ok:
                _clean_stale_package(packages_dir, pkg)
                ok, out = _run(_pip_base(packages_dir) + ['--upgrade', '--force-reinstall', pkg], timeout=PIP_TIMEOUT)
            if ok:
                installed += 1
            else:
                last = out.strip().split('\n')[-1][:100]
                _log(update_logs, f"   ⚠️  {pkg[:50]} → {last}")
        _log(update_logs, f"   ✅ {fname} done")

    # pyproject.toml
    pp = deploy_folder / 'pyproject.toml'
    if pp.exists():
        _log(update_logs, "   📄 pyproject.toml")
        # Try pip install . first (works for PEP 517 projects)
        ok, out = _run(_pip_base(packages_dir) + ['.'], cwd=str(deploy_folder), timeout=300)
        if ok:
            _log(update_logs, "   ✅ pyproject.toml (pip install .)")
            installed += 1
        else:
            # Manual parse as fallback
            text = pp.read_text(errors='ignore')
            deps = re.findall(r'"([A-Za-z0-9_\-]+)(?:[>=<!\[^~][^"]*)?"\s*,', text)
            deps += re.findall(r"^([A-Za-z0-9_\-]+)\s*=\s*['\"\^~]", text, re.M)
            skip = {'python','pip','setuptools','wheel','flit','hatchling','poetry'}
            deps = list(dict.fromkeys(d for d in deps if d.lower() not in skip))
            if deps:
                _log(update_logs, f"   📦 {len(deps)} deps parsed from pyproject.toml")
                for pkg in deps:
                    ok2, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
                    if ok2:
                        installed += 1

    # Pipfile
    pf = deploy_folder / 'Pipfile'
    if pf.exists():
        _log(update_logs, "   📄 Pipfile")
        text = pf.read_text(errors='ignore')
        in_pkgs = False
        pkgs = []
        for line in text.splitlines():
            if line.strip() in ('[packages]', '[dev-packages]'):
                in_pkgs = True
            elif line.startswith('['):
                in_pkgs = False
            elif in_pkgs:
                m = re.match(r'^([A-Za-z0-9_\-]+)\s*=', line)
                if m and m.group(1) not in ('python',):
                    pkgs.append(m.group(1))
        if pkgs:
            _log(update_logs, f"   📦 {len(pkgs)} packages from Pipfile")
            for pkg in pkgs:
                ok, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
                if ok:
                    installed += 1

    # conda environment.yml
    for env_file in ['environment.yml', 'environment.yaml', 'conda.yml']:
        ef = deploy_folder / env_file
        if not ef.exists():
            continue
        if _cmd_exists('conda'):
            _log(update_logs, f"   📄 {env_file} (conda)")
            ok, out = _run(['conda', 'env', 'update', '--file', str(ef),
                            '--prune', '-q'], timeout=300)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} conda env update: {out[-100:]}")
            installed += int(ok)
        else:
            # Parse pip dependencies from environment.yml and install them
            _log(update_logs, f"   📄 {env_file} (conda not found — installing pip deps)")
            text = ef.read_text(errors='ignore')
            in_pip = False
            pip_pkgs = []
            for line in text.splitlines():
                if '- pip:' in line:
                    in_pip = True
                    continue
                if line.startswith('  - ') and in_pip:
                    pkg = line.strip().lstrip('- ').strip()
                    if pkg and not pkg.startswith('#'):
                        pip_pkgs.append(pkg)
                elif not line.startswith(' ') and line.strip():
                    in_pip = False
            for pkg in pip_pkgs:
                ok2, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
                if ok2:
                    installed += 1

    # setup.py / setup.cfg
    for sf in ['setup.py', 'setup.cfg']:
        if (deploy_folder / sf).exists():
            _log(update_logs, f"   📄 {sf}")
            ok, _ = _run(_pip_base(packages_dir) + ['-e', '.'],
                         cwd=str(deploy_folder), timeout=300)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} {sf}")
            installed += int(ok)
            break

    return installed


def _install_npm(deploy_folder, update_logs):
    """Install Node.js dependencies from package.json using npm/yarn/pnpm/bun."""
    pkg_json = deploy_folder / 'package.json'
    if not pkg_json.exists():
        return 0

    # Detect which lock file / manager to use
    if (deploy_folder / 'bun.lockb').exists() and _cmd_exists('bun'):
        mgr = 'bun'
        cmd = ['bun', 'install', '--frozen-lockfile']
    elif (deploy_folder / 'pnpm-lock.yaml').exists() and _cmd_exists('pnpm'):
        mgr = 'pnpm'
        cmd = ['pnpm', 'install', '--frozen-lockfile', '--prefer-offline']
    elif (deploy_folder / 'yarn.lock').exists() and _cmd_exists('yarn'):
        mgr = 'yarn'
        cmd = ['yarn', 'install', '--frozen-lockfile', '--non-interactive']
    elif _cmd_exists('npm'):
        mgr = 'npm'
        # Use ci if lock file exists, else install
        lockfile = deploy_folder / 'package-lock.json'
        cmd = ['npm', 'ci'] if lockfile.exists() else ['npm', 'install', '--no-audit', '--no-fund']
    else:
        _log(update_logs, "   ⚠️ No Node.js package manager found (npm/yarn/pnpm/bun)")
        return 0

    _log(update_logs, f"   📦 npm install via {mgr}...")
    ok, out = _run(cmd, cwd=str(deploy_folder), timeout=300)
    if ok:
        _log(update_logs, f"   ✅ {mgr} install complete")
    else:
        _log(update_logs, f"   ⚠️ {mgr} install failed — {out[-200:]}")
    return int(ok)


def _install_cargo(deploy_folder, update_logs):
    """Build Rust project from Cargo.toml."""
    if not (deploy_folder / 'Cargo.toml').exists():
        return 0
    if not _cmd_exists('cargo'):
        _log(update_logs, "   ⚠️ Cargo not installed — skipping Rust build")
        return 0
    _log(update_logs, "   🦀 Building Rust project (cargo build --release)...")
    ok, out = _run(['cargo', 'build', '--release', '--quiet'],
                   cwd=str(deploy_folder), timeout=600)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} cargo build: {out[-150:]}")
    return int(ok)


def _install_go(deploy_folder, update_logs):
    """Download Go modules and build."""
    if not (deploy_folder / 'go.mod').exists():
        return 0
    if not _cmd_exists('go'):
        _log(update_logs, "   ⚠️ Go not installed — skipping Go modules")
        return 0
    _log(update_logs, "   🐹 Downloading Go modules...")
    ok, out = _run(['go', 'mod', 'download'], cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} go mod download: {out[-100:]}")
    if ok:
        ok2, _ = _run(['go', 'build', './...'], cwd=str(deploy_folder), timeout=300)
        _log(update_logs, f"   {'✅' if ok2 else '⚠️'} go build")
    return int(ok)


def _install_gem(deploy_folder, update_logs):
    """Install Ruby gems from Gemfile."""
    if not (deploy_folder / 'Gemfile').exists():
        return 0
    if not _cmd_exists('bundle'):
        if _cmd_exists('gem'):
            _log(update_logs, "   💎 Installing bundler...")
            _run(['gem', 'install', 'bundler', '--no-document'], timeout=120)
        else:
            _log(update_logs, "   ⚠️ Ruby/gem not installed — skipping Gemfile")
            return 0
    _log(update_logs, "   💎 Bundle install (Ruby gems)...")
    ok, out = _run(['bundle', 'install', '--jobs', '4', '--retry', '3'],
                   cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} bundle install: {out[-120:]}")
    return int(ok)


def _install_composer(deploy_folder, update_logs):
    """Install PHP packages from composer.json."""
    if not (deploy_folder / 'composer.json').exists():
        return 0
    composer = _cmd_exists('composer') and 'composer' or _cmd_exists('composer.phar') and 'composer.phar'
    if not composer:
        _log(update_logs, "   ⚠️ Composer not installed — skipping PHP deps")
        return 0
    _log(update_logs, "   🐘 Composer install (PHP packages)...")
    ok, out = _run([composer, 'install', '--no-dev', '--optimize-autoloader', '--no-interaction'],
                   cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} composer install: {out[-120:]}")
    return int(ok)


def _install_gradle(deploy_folder, update_logs):
    """Build Java project with Gradle or Maven."""
    # Gradle
    if (deploy_folder / 'build.gradle').exists() or (deploy_folder / 'build.gradle.kts').exists():
        gw = str(deploy_folder / 'gradlew')
        gradle = gw if Path(gw).exists() else ('gradle' if _cmd_exists('gradle') else None)
        if gradle:
            _log(update_logs, "   ☕ Gradle build (Java)...")
            ok, out = _run([gradle, 'dependencies', '--no-daemon', '-q'],
                           cwd=str(deploy_folder), timeout=600)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} gradle deps: {out[-100:]}")
            return int(ok)
        else:
            _log(update_logs, "   ⚠️ Gradle not installed — skipping")

    # Maven
    if (deploy_folder / 'pom.xml').exists():
        mvn = 'mvn' if _cmd_exists('mvn') else ('mvnw' if (deploy_folder / 'mvnw').exists() else None)
        if mvn:
            _log(update_logs, "   ☕ Maven dependency:resolve (Java)...")
            ok, out = _run([mvn, 'dependency:resolve', '-q', '--no-transfer-progress'],
                           cwd=str(deploy_folder), timeout=600)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} mvn resolve: {out[-100:]}")
            return int(ok)
        else:
            _log(update_logs, "   ⚠️ Maven not installed — skipping")
    return 0


def _install_mix(deploy_folder, update_logs):
    """Install Elixir dependencies."""
    if not (deploy_folder / 'mix.exs').exists():
        return 0
    if not _cmd_exists('mix'):
        _log(update_logs, "   ⚠️ Elixir/mix not installed — skipping")
        return 0
    _log(update_logs, "   💜 Mix deps.get (Elixir)...")
    ok, out = _run(['mix', 'deps.get'], cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} mix deps.get: {out[-100:]}")
    return int(ok)


def _install_pub(deploy_folder, update_logs):
    """Install Dart/Flutter dependencies."""
    pubspec = deploy_folder / 'pubspec.yaml'
    if not pubspec.exists():
        return 0
    runner = 'flutter' if _cmd_exists('flutter') else ('dart' if _cmd_exists('dart') else None)
    if not runner:
        _log(update_logs, "   ⚠️ Dart/Flutter not installed — skipping pubspec.yaml")
        return 0
    cmd = [runner, 'pub', 'get'] if runner == 'dart' else [runner, 'pub', 'get']
    _log(update_logs, f"   🎯 {runner} pub get (Dart/Flutter)...")
    ok, out = _run(cmd, cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} pub get: {out[-100:]}")
    return int(ok)


def _auto_detect_and_install_pip(code_file, packages_dir, update_logs):
    """
    Scan a Python/JS file for imports and auto-install any missing packages.
    Only installs what isn't already importable — avoids redundant reinstalls.
    """
    IMPORT_MAP = {
        'telebot':     'pyTelegramBotAPI',
        'telegram':    'python-telegram-bot',
        'aiogram':     'aiogram',
        'pyrogram':    'pyrogram',
        'telethon':    'telethon',
        'discord':     'discord.py',
        'nextcord':    'nextcord',
        'disnake':     'disnake',
        'flask':       'flask',
        'fastapi':     'fastapi',
        'uvicorn':     'uvicorn',
        'aiohttp':     'aiohttp',
        'requests':    'requests',
        'httpx':       'httpx',
        'dotenv':      'python-dotenv',
        'sqlalchemy':  'SQLAlchemy',
        'pymongo':     'pymongo',
        'motor':       'motor',
        'redis':       'redis',
        'celery':      'celery',
        'bs4':         'beautifulsoup4',
        'PIL':         'Pillow',
        'cv2':         'opencv-python',
        'sklearn':     'scikit-learn',
        'tweepy':      'tweepy',
        'slack_sdk':   'slack-sdk',
        'slack_bolt':  'slack-bolt',
        'linebot':     'line-bot-sdk',
        'yaml':        'pyyaml',
        'toml':        'toml',
        'boto3':       'boto3',
        'psutil':      'psutil',
        'loguru':      'loguru',
        'rich':        'rich',
        'pydantic':    'pydantic',
        'cryptography':'cryptography',
        'jwt':         'pyjwt',
        'aiosqlite':   'aiosqlite',
        'tortoise':    'tortoise-orm',
        'pywa':        'pywa',
        'viberbot':    'viberbot',
        'nio':         'matrix-nio',
        'pytz':        'pytz',
        'arrow':       'arrow',
        'pendulum':    'pendulum',
        'click':       'click',
        'typer':       'typer',
        'tqdm':        'tqdm',
        'matplotlib':  'matplotlib',
        'numpy':       'numpy',
        'pandas':      'pandas',
        'scipy':       'scipy',
        'openai':      'openai',
        'anthropic':   'anthropic',
        'transformers':'transformers',
        'torch':       'torch',
        'tensorflow':  'tensorflow',
        'paramiko':    'paramiko',
        'fabric':      'fabric',
        'invoke':      'invoke',
        'pyserial':    'pyserial',
        'serial':      'pyserial',
    }

    try:
        code = Path(code_file).read_text(errors='ignore') if isinstance(code_file, (str, Path)) else ''
    except Exception:
        code = ''

    if not code:
        return 0

    # Extract top-level module names
    imported = set()
    for m in re.findall(r'^import\s+([\w]+)', code, re.M):
        imported.add(m)
    for m in re.findall(r'^from\s+([\w]+)', code, re.M):
        imported.add(m)

    to_install = []
    for mod in imported:
        pkg = IMPORT_MAP.get(mod)
        if pkg:
            # Check if already importable (skip install if yes)
            try:
                import importlib.util as _ilu
                if _ilu.find_spec(mod) is None:
                    to_install.append(pkg)
            except Exception:
                to_install.append(pkg)

    installed = 0
    if to_install:
        _log(update_logs, f"   🔍 Auto-detected {len(to_install)} missing package(s)")
        for pkg in to_install:
            _log(update_logs, f"   ⬇️  {pkg}")
            ok, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
            if ok:
                installed += 1
                _log(update_logs, f"   ✅ {pkg}")
            else:
                _log(update_logs, f"   ⚠️ {pkg} failed")
    return installed


# ── main entry point ──────────────────────────────────────────────────────────

def install_all_dependencies(deploy_folder: Path, update_logs=None,
                              packages_dir=None, code_file=None) -> dict:
    """
    VPS-grade universal dependency installer.
    Scans the deploy folder for ALL known dependency manifests and installs them.
    Returns a summary dict: {manager: count_installed}
    """
    deploy_folder = Path(deploy_folder)
    if packages_dir:
        packages_dir = Path(packages_dir)
        packages_dir.mkdir(parents=True, exist_ok=True)

    summary = {}
    _log(update_logs, "═" * 50)
    _log(update_logs, "📦 VPS DEPENDENCY ENGINE — STARTING")
    _log(update_logs, "═" * 50)

    # ── Python ────────────────────────────────────────────────────────
    has_python = any([
        (deploy_folder / f).exists()
        for f in ['requirements.txt', 'pyproject.toml', 'Pipfile', 'setup.py',
                  'setup.cfg', 'environment.yml', 'environment.yaml']
    ])
    if has_python or code_file:
        _log(update_logs, "🐍 Python packages:")
        n = _install_pip(packages_dir, deploy_folder, update_logs)
        if code_file:
            n += _auto_detect_and_install_pip(code_file, packages_dir, update_logs)
        summary['pip'] = n
        _log(update_logs, f"   → {n} Python package(s) installed")

    # ── Node.js ───────────────────────────────────────────────────────
    if (deploy_folder / 'package.json').exists():
        _log(update_logs, "🟨 Node.js packages:")
        n = _install_npm(deploy_folder, update_logs)
        summary['npm'] = n

    # ── Rust ──────────────────────────────────────────────────────────
    if (deploy_folder / 'Cargo.toml').exists():
        _log(update_logs, "🦀 Rust (cargo):")
        n = _install_cargo(deploy_folder, update_logs)
        summary['cargo'] = n

    # ── Go ────────────────────────────────────────────────────────────
    if (deploy_folder / 'go.mod').exists():
        _log(update_logs, "🐹 Go modules:")
        n = _install_go(deploy_folder, update_logs)
        summary['go'] = n

    # ── Ruby ──────────────────────────────────────────────────────────
    if (deploy_folder / 'Gemfile').exists():
        _log(update_logs, "💎 Ruby gems:")
        n = _install_gem(deploy_folder, update_logs)
        summary['gem'] = n

    # ── PHP ───────────────────────────────────────────────────────────
    if (deploy_folder / 'composer.json').exists():
        _log(update_logs, "🐘 PHP (composer):")
        n = _install_composer(deploy_folder, update_logs)
        summary['composer'] = n

    # ── Java ──────────────────────────────────────────────────────────
    has_java = any((deploy_folder / f).exists()
                   for f in ['build.gradle', 'build.gradle.kts', 'pom.xml'])
    if has_java:
        _log(update_logs, "☕ Java (gradle/maven):")
        n = _install_gradle(deploy_folder, update_logs)
        summary['java'] = n

    # ── Elixir ───────────────────────────────────────────────────────
    if (deploy_folder / 'mix.exs').exists():
        _log(update_logs, "💜 Elixir (mix):")
        n = _install_mix(deploy_folder, update_logs)
        summary['mix'] = n

    # ── Dart / Flutter ────────────────────────────────────────────────
    if (deploy_folder / 'pubspec.yaml').exists():
        _log(update_logs, "🎯 Dart/Flutter (pub):")
        n = _install_pub(deploy_folder, update_logs)
        summary['pub'] = n

    total = sum(summary.values())
    _log(update_logs, "═" * 50)
    parts = ', '.join(f"{k}: {v}" for k, v in summary.items() if v)
    _log(update_logs, f"✅ DEPENDENCIES DONE — {total} item(s) installed ({parts or 'none detected'})")
    _log(update_logs, "═" * 50)
    return summary


# ========== ENHANCED DEPENDENCY INSTALLATION ==========
PIP_TIMEOUT = int(os.environ.get("PIP_TIMEOUT", 600))   # per package; big ones (aiogram + deps) need > 3 min on a slow host


def install_dependencies_enhanced(reqs_file, update_logs, packages_dir=None):
    """
    VPS-grade installer.
    - Installs into packages_dir (per-deployment isolation) when provided
    - Falls back to system-wide install
    - Handles requirements.txt, pyproject.toml, Pipfile, setup.py/cfg
    - Upgrades pip first, retries individual failures
    """
    if not reqs_file.exists():
        update_logs("✅ No requirements file found")
        return True, []

    try:
        with open(reqs_file, 'r') as f:
            raw = f.read().strip()
        if not raw:
            update_logs("⚠️ requirements.txt is empty")
            return True, []

        packages = [p.strip() for p in raw.split('\n')
                    if p.strip() and not p.startswith('#') and not p.startswith('-')]
        packages = drop_stdlib_requirements(packages, update_logs)     # sqlite3, asyncio, json…
        if not packages:
            update_logs("✅ Nothing to install (only built-in modules listed)")
            return True, []
        update_logs(f"📦 {len(packages)} package(s) to install")

        # Upgrade pip silently first
        update_logs("🔄 Upgrading pip...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
                       capture_output=True, timeout=60)

        # Build base pip command
        def _pip_cmd(pkg_or_flag, extra_flags=None):
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
                   "--no-warn-script-location", "--quiet"]
            if packages_dir:
                cmd += ["--target", str(packages_dir)]
            if extra_flags:
                cmd += extra_flags
            cmd.append(pkg_or_flag)
            return cmd

        success_count = 0
        failed_packages = []

        for i, package in enumerate(packages):
            pct = int((i / len(packages)) * 100)
            update_logs(create_progress_bar(pct, 25))
            update_logs(f"   ⬇️  {package[:60]}")

            if packages_dir:
                _clean_stale_package(packages_dir, package)

            # Determine install type
            pkg_lower = package.lower()
            if pkg_lower.startswith('git+') or pkg_lower.startswith('hg+') or pkg_lower.startswith('svn+'):
                cmd = _pip_cmd(package)
            elif pkg_lower.endswith('.whl') or pkg_lower.endswith('.tar.gz'):
                cmd = _pip_cmd(package)
            elif '.git' in package or 'github.com' in package:
                cmd = _pip_cmd(f"git+{package}" if not package.startswith('git+') else package)
            else:
                cmd = _pip_cmd(package)

            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT)
            except subprocess.TimeoutExpired:
                # one slow package must not abort the whole install (aiogram pulls in many dependencies)
                res = subprocess.CompletedProcess(cmd, 1, "", f"timed out after {PIP_TIMEOUT}s")
            if res.returncode == 0:
                success_count += 1
                update_logs(f"   ✅ {package[:60]}")
            else:
                # Never trust whatever partial files the failed attempt above
                # may have left behind — clean before the forced retry, or a
                # package can end up with files mixed across two versions
                # (a well-known cause of cryptic __slots__ AttributeErrors
                # deep inside libraries like python-telegram-bot).
                if packages_dir:
                    _clean_stale_package(packages_dir, package)
                try:
                    res2 = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "--quiet", "--force-reinstall",
                         *(["--target", str(packages_dir)] if packages_dir else []),
                         package],
                        capture_output=True, text=True, timeout=PIP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    res2 = subprocess.CompletedProcess([], 1, "", f"timed out after {PIP_TIMEOUT}s")
                if res2.returncode == 0:
                    success_count += 1
                    update_logs(f"   ✅ {package[:60]} (retry OK)")
                else:
                    failed_packages.append(package)
                    _err_lines = [l for l in (res2.stderr or res.stderr or res.stdout or "").strip().split('\n')
                                  if l.strip()]
                    err_line = next((l for l in reversed(_err_lines) if l.startswith('ERROR')), None) \
                               or (_err_lines[-1] if _err_lines else "unknown error")
                    update_logs(f"   ❌ {package[:50]}: {err_line[:80]}")

        update_logs(create_progress_bar(100, 25))
        if failed_packages:
            update_logs(f"⚠️ Failed ({len(failed_packages)}): {', '.join(p.split('==')[0][:20] for p in failed_packages[:5])}")
        update_logs(f"✅ {success_count}/{len(packages)} packages installed")

        return (success_count / len(packages)) >= 0.7 if packages else True, failed_packages

    except Exception as e:
        update_logs(f"❌ Installer error: {e}")
        return False, []


def install_from_repo_requirements(deploy_folder: Path, update_logs) -> list:
    """
    Scan a cloned repo directory for ALL supported requirement sources and
    install everything.  Returns a flat list of packages installed.
    """
    packages_dir = deploy_folder / 'packages'
    packages_dir.mkdir(exist_ok=True)

    installed = []

    # ── requirements.txt (and variants) ──────────────────────────────
    for fname in ['requirements.txt', 'requirements-dev.txt', 'requirements_prod.txt',
                  'requirements/base.txt', 'requirements/main.txt']:
        req_path = deploy_folder / fname
        if req_path.exists():
            update_logs(f"📋 Found {fname}")
            ok, failed = install_dependencies_enhanced(req_path, update_logs, packages_dir)
            installed.extend([l.strip() for l in req_path.read_text().splitlines()
                              if l.strip() and not l.startswith('#')])

    # ── pyproject.toml (PEP 517/518) ─────────────────────────────────
    pyproject = deploy_folder / 'pyproject.toml'
    if pyproject.exists():
        update_logs("📋 Found pyproject.toml — installing with pip...")
        res = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--quiet',
             '--no-warn-script-location', '--target', str(packages_dir), '.'],
            cwd=str(deploy_folder), capture_output=True, text=True, timeout=300)
        if res.returncode == 0:
            update_logs("✅ pyproject.toml dependencies installed")
        else:
            # Extract [tool.poetry.dependencies] or [project.dependencies] manually
            try:
                import re as _re
                text = pyproject.read_text()
                # PEP 621 style
                deps = _re.findall(r'^\s*"([a-zA-Z0-9_\-]+)[>=<!\[\]"]*"', text, _re.M)
                # Poetry style
                deps += _re.findall(r'^\s*([a-zA-Z0-9_\-]+)\s*=\s*["\^~]', text, _re.M)
                skip = {'python', 'pip', 'setuptools', 'wheel'}
                deps = [d for d in dict.fromkeys(deps) if d.lower() not in skip]
                if deps:
                    update_logs(f"📦 Parsed {len(deps)} deps from pyproject.toml")
                    tmp = deploy_folder / '_pyproject_reqs.txt'
                    tmp.write_text('\n'.join(deps))
                    install_dependencies_enhanced(tmp, update_logs, packages_dir)
                    tmp.unlink(missing_ok=True)
                    installed.extend(deps)
            except Exception as pe:
                update_logs(f"⚠️ pyproject.toml parse error: {pe}")

    # ── Pipfile ───────────────────────────────────────────────────────
    pipfile = deploy_folder / 'Pipfile'
    if pipfile.exists():
        update_logs("📋 Found Pipfile — extracting packages...")
        try:
            import re as _re
            text = pipfile.read_text()
            in_packages = False
            pkgs = []
            for line in text.splitlines():
                if line.strip() in ('[packages]', '[dev-packages]'):
                    in_packages = True
                elif line.startswith('['):
                    in_packages = False
                elif in_packages:
                    m = _re.match(r'^([a-zA-Z0-9_\-]+)\s*=', line)
                    if m:
                        pkgs.append(m.group(1))
            if pkgs:
                update_logs(f"📦 Parsed {len(pkgs)} deps from Pipfile")
                tmp = deploy_folder / '_pipfile_reqs.txt'
                tmp.write_text('\n'.join(pkgs))
                install_dependencies_enhanced(tmp, update_logs, packages_dir)
                tmp.unlink(missing_ok=True)
                installed.extend(pkgs)
        except Exception as pfe:
            update_logs(f"⚠️ Pipfile parse error: {pfe}")

    # ── setup.py / setup.cfg ──────────────────────────────────────────
    for setup_file in ['setup.py', 'setup.cfg']:
        sf = deploy_folder / setup_file
        if sf.exists():
            update_logs(f"📋 Found {setup_file} — installing package...")
            res = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--quiet',
                 '--no-warn-script-location', '--target', str(packages_dir), '-e', '.'],
                cwd=str(deploy_folder), capture_output=True, text=True, timeout=300)
            if res.returncode == 0:
                update_logs(f"✅ {setup_file} installed")
            else:
                update_logs(f"⚠️ {setup_file} install failed (check output log)")
            break

    return installed

# ========== FRAMEWORK DETECTION ==========
# ── Platform detection patterns ──────────────────────────────────────────────
_PLATFORM_PATTERNS = {
    # Telegram
    'telegram_telebot':  ['import telebot', 'from telebot', 'TeleBot('],
    'telegram_aiogram':  ['import aiogram', 'from aiogram', 'Dispatcher(', 'Router()'],
    'telegram_ptb':      ['from telegram', 'from telegram.ext', 'Application.builder', 'ApplicationBuilder'],
    'telegram_pyrogram': ['import pyrogram', 'from pyrogram', 'Client('],
    'telegram_telethon': ['import telethon', 'from telethon', 'TelegramClient('],
    # WhatsApp
    'whatsapp_pywa':     ['import pywa', 'from pywa', 'WhatsApp('],
    'whatsapp_heyoo':    ['import heyoo', 'from heyoo', 'WhatsApp('],
    'whatsapp_twilio':   ['from twilio', 'import twilio', 'MessagingResponse('],
    'whatsapp_cloud':    ['WHATSAPP_TOKEN', 'PHONE_NUMBER_ID', 'graph.facebook.com', 'whatsapp/messages'],
    # Discord
    'discord_py':        ['import discord', 'from discord', 'discord.Client(', 'commands.Bot('],
    'discord_nextcord':  ['import nextcord', 'from nextcord'],
    'discord_disnake':   ['import disnake', 'from disnake'],
    'discord_pycord':    ['import py_cord', 'from py_cord'],
    # Slack
    'slack':             ['from slack_sdk', 'import slack_sdk', 'from slack_bolt', 'import slack_bolt', 'WebClient('],
    # Twitter / X
    'twitter':           ['import tweepy', 'from tweepy', 'tweepy.Client', 'tweepy.API'],
    # Line
    'line':              ['from linebot', 'import linebot', 'LineBotApi(', 'WebhookHandler('],
    # Viber
    'viber':             ['from viberbot', 'import viberbot', 'ViberApi('],
    # Matrix
    'matrix':            ['from nio import', 'import nio', 'matrix_client', 'AsyncClient('],
    # IRC
    'irc':               ['import irc', 'from irc.bot', 'SingleServerIRCBot('],
    # Web frameworks (keep last — bot frameworks above take priority)
    'flask':             ['from flask import', 'import flask', 'Flask(__name__)'],
    'fastapi':           ['from fastapi', 'import fastapi', 'FastAPI()'],
    'django':            ['import django', 'from django', 'DJANGO_SETTINGS'],
    'aiohttp':           ['import aiohttp', 'from aiohttp'],
    'starlette':         ['from starlette', 'import starlette'],
}

# Human-readable labels for display
_PLATFORM_LABELS = {
    'telegram_telebot':  '📱 Telegram (pyTelegramBotAPI)',
    'telegram_aiogram':  '📱 Telegram (aiogram)',
    'telegram_ptb':      '📱 Telegram (python-telegram-bot)',
    'telegram_pyrogram': '📱 Telegram (Pyrogram)',
    'telegram_telethon': '📱 Telegram (Telethon)',
    'whatsapp_pywa':     '💬 WhatsApp (PyWA)',
    'whatsapp_heyoo':    '💬 WhatsApp (heyoo)',
    'whatsapp_twilio':   '💬 WhatsApp (Twilio)',
    'whatsapp_cloud':    '💬 WhatsApp (Cloud API)',
    'discord_py':        '🎮 Discord (discord.py)',
    'discord_nextcord':  '🎮 Discord (nextcord)',
    'discord_disnake':   '🎮 Discord (disnake)',
    'discord_pycord':    '🎮 Discord (py-cord)',
    'slack':             '💼 Slack',
    'twitter':           '🐦 Twitter/X (Tweepy)',
    'line':              '💚 Line',
    'viber':             '💜 Viber',
    'matrix':            '🔷 Matrix',
    'irc':               '📡 IRC',
    'flask':             '🌐 Flask',
    'fastapi':           '🌐 FastAPI',
    'django':            '🌐 Django',
    'aiohttp':           '🌐 aiohttp',
    'starlette':         '🌐 Starlette',
    # Node.js (from create_node_launcher_script's own detection — separate
    # from the Python-oriented _PLATFORM_PATTERNS above). Every one of these
    # previously fell through get_platform_label()'s fallback and displayed
    # as its raw internal key — several contain underscores, which breaks
    # legacy Markdown parsing outright when shown unprotected by backticks.
    'typescript':        '🟦 TypeScript',
    'node_esm':          '🟩 Node.js (ESM)',
    'node':              '🟩 Node.js',
    'discord_js':        '🎮 Discord (discord.js)',
    'telegram_node':     '📱 Telegram (node-telegram-bot-api)',
    'whatsapp_node':     '💬 WhatsApp (Node.js)',
    'web_server':        '🌐 Web Server',
}

# Required env vars hints per platform
PLATFORM_ENV_HINTS = {
    'telegram_telebot':  '`BOT_TOKEN` — your Telegram bot token from @BotFather',
    'telegram_aiogram':  '`BOT_TOKEN` — your Telegram bot token from @BotFather',
    'telegram_ptb':      '`BOT_TOKEN` — your Telegram bot token from @BotFather',
    'telegram_pyrogram': '`API_ID`, `API_HASH`, `BOT_TOKEN` — from my.telegram.org + @BotFather',
    'telegram_telethon': '`API_ID`, `API_HASH` — from my.telegram.org',
    'whatsapp_pywa':     '`WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` — from Meta Developer Portal',
    'whatsapp_heyoo':    '`WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` — from Meta Developer Portal',
    'whatsapp_twilio':   '`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER` — from Twilio Console',
    'whatsapp_cloud':    '`WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN` — from Meta Developer Portal',
    'discord_py':        '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'discord_nextcord':  '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'discord_disnake':   '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'discord_pycord':    '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'slack':             '`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET` — from api.slack.com',
    'twitter':           '`TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_BEARER_TOKEN` — from developer.twitter.com',
    'line':              '`LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` — from Line Developer Console',
    'viber':             '`VIBER_AUTH_TOKEN` — from Viber Admin Panel',
    'matrix':            '`MATRIX_HOMESERVER`, `MATRIX_USER`, `MATRIX_PASSWORD` — from your Matrix server',
    'irc':               '`IRC_SERVER`, `IRC_PORT`, `IRC_NICK`, `IRC_CHANNEL`',
    'flask':             '`PORT` (optional, default 5000)',
    'fastapi':           '`PORT` (optional, default 8000)',
    'django':            '`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`',
}

def detect_bot_framework(code_content: str) -> list:
    """Detect all bot frameworks/platforms used in the code."""
    detected = []
    code_lower = code_content.lower()

    for platform, patterns in _PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in code_lower:
                detected.append(platform)
                break   # one match per platform is enough

    # Deduplicate while preserving order
    seen = set()
    result = []
    for p in detected:
        if p not in seen:
            seen.add(p)
            result.append(p)

    return result if result else ['generic']


def get_platform_env_hint(frameworks: list) -> str:
    """Return env var hints for the detected platforms."""
    hints = []
    for fw in frameworks:
        hint = PLATFORM_ENV_HINTS.get(fw)
        if hint and hint not in hints:
            label = _PLATFORM_LABELS.get(fw, fw)
            hints.append(f"*{label}:*\n{hint}")
    return '\n\n'.join(hints) if hints else ''


def get_platform_label(frameworks: list) -> str:
    """Return a human-readable platform summary."""
    # Defense in depth: even for a framework key with no _PLATFORM_LABELS
    # entry (future additions, typos, etc.), never expose the raw internal
    # key verbatim — it's very likely to contain an underscore, which
    # breaks legacy Markdown parsing outright when unprotected by backticks.
    labels = [_PLATFORM_LABELS.get(fw, fw.replace('_', ' ').title())
              for fw in frameworks if fw != 'generic']
    return ', '.join(labels) if labels else '🤖 Generic Python Bot'

# ========== ENHANCED LAUNCHER SCRIPT ==========
def create_node_launcher_script(deploy_folder: Path, dest_script: Path,
                                env_vars_dict: dict, update_logs) -> tuple:
    """
    Create start.sh for Node.js / TypeScript bots.
    Installs npm packages, picks the right runtime (node / npx ts-node / npx tsx).
    Returns (start_script_path, detected_framework_list).
    """
    ext = dest_script.suffix.lower()

    if ext in ('.ts', '.tsx'):
        # Prefer tsx (faster) then ts-node
        # setsid gives this its own process group, separate from the
        # hosting bot's own — without it, stopping/restarting this
        # deployment could only ever kill THIS PID, leaving the actual
        # node process (a child, once npx/ts-node execs or forks) running
        # forever as an orphan, or worse, a bare killpg here would risk
        # taking down unrelated processes that share the inherited group.
        run_cmd = (
            f'if command -v npx &>/dev/null; then\n'
            f'    setsid npx --yes tsx "{dest_script}" >> output.log 2>&1 &\n'
            f'elif command -v ts-node &>/dev/null; then\n'
            f'    setsid ts-node "{dest_script}" >> output.log 2>&1 &\n'
            f'else\n'
            f'    echo "❌ No TypeScript runner found (install tsx or ts-node)" >> output.log\n'
            f'    exit 1\n'
            f'fi'
        )
        framework = ['typescript']
    elif ext in ('.mjs',):
        run_cmd = f'setsid node --input-type=module "{dest_script}" >> output.log 2>&1 &'
        framework = ['node_esm']
    else:
        run_cmd = f'setsid node "{dest_script}" >> output.log 2>&1 &'
        framework = ['node']

    # Detect framework from content
    try:
        code = dest_script.read_text(errors='ignore')
        if 'discord' in code.lower():
            framework.append('discord_js')
        if 'telegraf' in code.lower() or 'node-telegram' in code.lower():
            framework.append('telegram_node')
        if 'whatsapp' in code.lower() or 'baileys' in code.lower():
            framework.append('whatsapp_node')
        if 'express' in code.lower() or 'fastify' in code.lower():
            framework.append('web_server')
    except Exception:
        pass

    # Write .env
    env_file = deploy_folder / '.env'
    env_file.write_text('\n'.join(f'{k}={v}' for k, v in env_vars_dict.items()) + '\n')

    # Write start.sh
    # Single-quote every value so bash treats it as fully literal — no `$`
    # variable expansion, no backtick/`$()` command substitution, no
    # quote-breaking. This is the standard POSIX-safe way to embed arbitrary
    # values in a shell script: replace each ' with '\'' and wrap in ' '.
    # Without this, a value containing $ (extremely common in real DB
    # connection strings and API keys) is silently corrupted with no error
    # anywhere, and a value containing backticks/`$()` is a shell injection risk.
    def _sh_quote(value):
        return "'" + str(value).replace("'", "'\\''") + "'"

    env_lines = '\n'.join(f'export {k}={_sh_quote(v)}' for k, v in env_vars_dict.items())
    npm_install = (
        'if [ -f "package.json" ]; then\n'
        '    echo "📦 Installing npm packages..." >> output.log\n'
        '    npm install --no-audit --no-fund >> output.log 2>&1\n'
        'fi'
    )
    # Node bots are spawned from the hosting bot and would otherwise inherit its FULL
    # environment — including GITHUB_TOKEN, which can read every user's .env in the
    # files repo. Drop the platform's own secrets BEFORE applying the deployment's values
    # (same list the Python launcher clears).
    _platform_keys = ('BOT_TOKEN', 'TELEGRAM_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TOKEN', 'API_TOKEN',
                      'TG_BOT_TOKEN', 'TGBOT_TOKEN', 'DISCORD_TOKEN', 'SLACK_BOT_TOKEN',
                      'SLACK_APP_TOKEN', 'GITHUB_TOKEN', 'GITHUB_REPO_OWNER', 'GITHUB_REPO_NAME',
                      'GITHUB_BACKUP_BRANCH', 'GITHUB_BACKUP_PATH', 'GITHUB_FILES_REPO_NAME',
                      'DATABASE_URL', 'DATABASE_FILE', 'ADMIN_IDS', 'REQUIRED_CHANNEL', 'CHANNEL_LINK')
    start_content = (
        f'#!/bin/bash\n'
        f'cd "{deploy_folder}"\n'
        f'export PYTHONUNBUFFERED=1\n'
        # Private scratch space per deployment (see the Python launcher for why)
        f'mkdir -p "{deploy_folder}/.tmp"\n'
        f'export TMPDIR="{deploy_folder}/.tmp" TMP="{deploy_folder}/.tmp" TEMP="{deploy_folder}/.tmp"\n'
        f'mkdir -p "{deploy_folder}/.home/.cache" "{deploy_folder}/.home/.config" "{deploy_folder}/.home/.local/share"\n'
        f'export HOME="{deploy_folder}/.home" USERPROFILE="{deploy_folder}/.home" '
        f'XDG_CACHE_HOME="{deploy_folder}/.home/.cache" XDG_CONFIG_HOME="{deploy_folder}/.home/.config" '
        f'XDG_DATA_HOME="{deploy_folder}/.home/.local/share"\n'
        f'unset {" ".join(_platform_keys)}\n'
        f'{env_lines}\n\n'
        f'{npm_install}\n\n'
        f'{run_cmd}\n'
        f'echo $! > pid.txt\n'
    )
    start_script = deploy_folder / 'start.sh'
    start_script.write_text(start_content)
    start_script.chmod(0o755)

    update_logs(f"🟨 Node.js launcher created ({', '.join(framework)})")
    return start_script, framework


def create_enhanced_launcher_script(deploy_folder, dest_script, env_vars_dict, code_content, update_logs, packages_dir=None):
    frameworks = detect_bot_framework(code_content)
    framework_str = ', '.join(frameworks)
    update_logs(f"🔧 Framework detection: {framework_str}")
    
    launcher_script = deploy_folder / "run.py"
    
    env_set_code = []
    for k, v in env_vars_dict.items():
        # repr() is the correct, complete way to generate a valid Python
        # string literal from arbitrary content — handles embedded quotes,
        # backslashes, AND actual newlines (a hand-rolled quote/backslash
        # escape misses real newline characters, which breaks the entire
        # generated run.py with a syntax error for any multi-line value
        # such as a PEM private key or a JSON credentials blob).
        env_set_code.append(f'os.environ[{k!r}] = {v!r}')
    
    env_set_str = "\n".join(env_set_code) if env_set_code else "# No custom environment variables"
    
    # Build packages_dir injection line for launcher
    packages_dir_str = str(packages_dir) if packages_dir else str(deploy_folder / 'packages')
    
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

# ── Per-deployment packages directory (VPS isolation) ────────────────────────
# Packages installed with --target land here and take priority over system libs
_pkg_dir = r"{packages_dir_str}"
if os.path.isdir(_pkg_dir) and _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

# Change to deployment directory
os.chdir(r"{deploy_folder}")

# ── Private scratch space (data isolation) ───────────────────────────────────
# Relative paths already resolve inside THIS deployment's own folder (chdir above). The
# shared system temp dir is the other place two bots could meet — e.g. both creating a
# "bot.db" via tempfile or $TMPDIR — so every deployment gets its own.
try:
    _tmp_dir = Path(r"{deploy_folder}") / ".tmp"
    _tmp_dir.mkdir(exist_ok=True)
    for _tk in ("TMPDIR", "TMP", "TEMP"):
        os.environ[_tk] = str(_tmp_dir)
    # Private HOME too: ~/.cache, ~/.config, ~/.local, ~/.npm, ~/bot.db etc. are per deployment
    _home_dir = Path(r"{deploy_folder}") / ".home"
    for _sub in ("", ".cache", ".config", ".local/share"):
        (_home_dir / _sub).mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(_home_dir)
    os.environ["USERPROFILE"] = str(_home_dir)
    os.environ["XDG_CACHE_HOME"] = str(_home_dir / ".cache")
    os.environ["XDG_CONFIG_HOME"] = str(_home_dir / ".config")
    os.environ["XDG_DATA_HOME"] = str(_home_dir / ".local/share")
    import tempfile as _tempfile
    _tempfile.tempdir = str(_tmp_dir)
except Exception:
    pass

# ========== ENVIRONMENT ISOLATION ==========
# This process was spawned by the hosting bot itself and inherits its FULL
# environment — including the hosting bot's OWN BOT_TOKEN, GITHUB_TOKEN,
# ADMIN_IDS, DATABASE_FILE, etc. For any of these the deployment doesn't
# explicitly configure itself, that leaked value would otherwise silently
# persist into the deployed bot's process — at best confusing (a deployed
# bot inheriting the hosting platform's admin list), at worst a real
# credential leak (a deployed bot's code having access to the platform's
# own GitHub backup token). Clear them all before applying the user's own
# configured values, so an unset key is genuinely unset, not leaked.
_HOSTING_BOT_KEYS = {{
    'BOT_TOKEN', 'TELEGRAM_TOKEN', 'TELEGRAM_BOT_TOKEN',
    'TOKEN', 'API_TOKEN', 'TG_BOT_TOKEN', 'TGBOT_TOKEN',
    'DISCORD_TOKEN', 'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN',
    'GITHUB_TOKEN', 'GITHUB_REPO_OWNER', 'GITHUB_REPO_NAME',
    'GITHUB_BACKUP_BRANCH', 'GITHUB_BACKUP_PATH', 'GITHUB_FILES_REPO_NAME',
    'DATABASE_URL', 'DATABASE_FILE',
    'ADMIN_IDS', 'REQUIRED_CHANNEL', 'CHANNEL_LINK',
}}
for _hk in _HOSTING_BOT_KEYS:
    os.environ.pop(_hk, None)

# ========== SET ENVIRONMENT VARIABLES ==========
# The deployment's own configured values — applied after clearing inherited
# ones above, so they start from a genuinely clean slate.
{env_set_str}

# Try to load .env file (lowest priority — never overrides an explicitly
# configured deployment variable, only fills in what's still unset)
try:
    from dotenv import load_dotenv
    env_file = Path(r"{deploy_folder}") / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)
        print("✅ Loaded .env file (non-overriding)")
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
print("🔧 LAUNCHER READY")
print("=" * 50)
print(f"Python: {{sys.version.split()[0]}}")
print(f"Dir: {{os.getcwd()}}")
sys.stdout.flush()

# Skip noisy cloud/system env vars — only show what the user actually set
_SKIP_PREFIXES = ('KUBERNETES_', 'RENDER_', 'UV_', 'PIPENV_', 'NPM_', 'NODE_', 'HB_', 'MALLOC_', 'OMP_',
                   'OPENBLAS_', 'MKL_', 'NUMEXPR_', 'VECLIB_', 'TOKENIZERS_', 'PYTHONDONTWRITE', 'PYTHONOPTIMIZE',
                   'BUN_', 'GUNICORN_', 'YARN_', 'DEFAULT_', 'PYTHON_')
_SKIP_EXACT = frozenset([
    'PATH', 'PYTHONPATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM',
    'LANG', 'LC_ALL', 'PWD', 'HOSTNAME', 'RENDER', 'PORT',
    'IS_PULL_REQUEST', 'RENDER_ROOT', 'RENDER_ENV_IS_DOCKER',
    'RENDER_SERVICE_ID', 'RENDER_SERVICE_NS', 'RENDER_SERVICE_CONTEXT_ROOT',
    'RENDER_EXTERNAL_HOSTNAME', 'RENDER_GIT_REPO_SLUG',
    'RENDER_NODE_VERSION_DETECTED', 'RENDER_NODE_INSTALLED',
    'RENDER_PRE_RUN_COMMAND', 'RENDER_CPU_COUNT', 'USER_RUN_COMMAND',
    'BLACK', 'BLUE', 'CYAN', 'YELLOW', 'RESET', 'ENTER_STANDOUT',
    'PYTHONUNBUFFERED', 'UV_COMPILE_BYTECODE', 'ENTER_BOLD',
])

_shown_keys = []
for _k, _v in os.environ.items():
    if _k in _SKIP_EXACT:
        continue
    _skip = False
    for _pfx in _SKIP_PREFIXES:
        if _k.startswith(_pfx):
            _skip = True
            break
    if _skip:
        continue
    _shown_keys.append(_k)
    if any(_s in _k.upper() for _s in ['TOKEN', 'SECRET', 'KEY', 'PASSWORD', 'HASH']):
        _disp = (_v[:8] + '...') if len(_v) > 8 else '***'
    else:
        _disp = (_v[:60] + '...') if len(_v) > 60 else _v
    print(f"  ✅ {{_k}} = {{_disp}}")

if not _shown_keys:
    print("  ℹ️  No custom env vars (set KEY=VALUE when deploying)")

# Token check
_token_found = False
for _tname in ['BOT_TOKEN', 'TOKEN', 'API_TOKEN', 'DISCORD_TOKEN', 'TELEGRAM_TOKEN']:
    if os.environ.get(_tname):
        print(f"✅ {{_tname}} is set")
        _token_found = True
        break
if not _token_found:
    print("⚠️  WARNING: No bot token detected in env vars!")
    print("   Set BOT_TOKEN (or TOKEN) via env vars when deploying.")

sys.stdout.flush()
print("=" * 50)
print("🚀 STARTING BOT")
print("=" * 50)
sys.stdout.flush()

# ── Token aliasing ────────────────────────────────────────────────────────
# Bridge all common token env var names so the bot finds its token regardless
# of which variable name it reads.
_TOKEN_ALIASES = ['BOT_TOKEN', 'TELEGRAM_TOKEN', 'TELEGRAM_BOT_TOKEN',
                  'TOKEN', 'API_TOKEN', 'TG_BOT_TOKEN', 'TGBOT_TOKEN',
                  'DISCORD_TOKEN']
_found_token = None
for _alias in _TOKEN_ALIASES:
    _t = os.environ.get(_alias, '')
    if _t:
        _found_token = _t
        break
if _found_token:
    for _alias in _TOKEN_ALIASES:
        if not os.environ.get(_alias):
            os.environ[_alias] = _found_token
    print("✅ Token env vars bridged")
else:
    print("⚠️  No bot token found — set BOT_TOKEN or TELEGRAM_TOKEN in env vars")

# ── Heartbeat file ───────────────────────────────────────────────────────
# Written every 30s by a background thread. A hung/deadlocked process still
# passes a simple "is the PID alive" check — this gives the host a way to
# eventually detect "alive but stuck" too, not just "dead", by checking
# whether this file's mtime is stale.
_heartbeat_running = True
def _heartbeat():
    _heartbeat_file = Path(r"{deploy_folder}") / ".heartbeat"
    while _heartbeat_running:
        try:
            with open(_heartbeat_file, 'w') as _hf:
                _hf.write(str(time.time()))
        except Exception:
            pass
        time.sleep(30)
threading.Thread(target=_heartbeat, daemon=True, name='Heartbeat').start()

# ── Universal webhook pre-clear (raw HTTP, framework-agnostic) ───────────
# Runs unconditionally before anything else, using only the raw bot token —
# independent of which framework the bot uses or whether entry-point
# detection below finds a recognizable bot object at all. A bot can't
# receive updates via polling while Telegram still has a webhook registered
# for it, so any bot that was ever previously run in webhook mode elsewhere
# would otherwise silently receive nothing here, with no error anywhere.
_wh_token = os.environ.get('BOT_TOKEN', '').strip()
if _wh_token and ':' in _wh_token:
    try:
        import urllib.request as _ur, json as _jr
        _wh_info_url = f"https://api.telegram.org/bot{{_wh_token}}/getWebhookInfo"
        with _ur.urlopen(_ur.Request(_wh_info_url), timeout=10) as _r:
            _wh_data = _jr.loads(_r.read().decode('utf-8'))
        _active_wh = (_wh_data.get('result') or {{}}).get('url', '')
        if _active_wh:
            print(f"⚠️  Active webhook detected: {{_active_wh[:60]}}")
            _del_url = (f"https://api.telegram.org/bot{{_wh_token}}"
                        f"/deleteWebhook?drop_pending_updates=true")
            _ur.urlopen(_ur.Request(_del_url), timeout=10).read()
            time.sleep(1)
            print("✅ Webhook deleted — polling mode active")
        else:
            print("✅ No active webhook — polling ready")
    except Exception as _we:
        print(f"⚠️  Webhook pre-check skipped: {{_we}}")

# ========== METHOD 1: Import and run ==========
try:
    print("📌 Method 1: Importing as module...")
    sys.stdout.flush()
    sys.path.insert(0, r"{deploy_folder}")

    import importlib.util, inspect as _inspect, socket as _socket, threading as _thr

    spec = importlib.util.spec_from_file_location("user_bot", r"{dest_script}")
    if spec is None:
        raise ImportError(f"Cannot load module from {dest_script}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["user_bot"] = module

    # ----------------------------------------------------------------
    # Intercept bot.set_webhook() calls that fire at module level.
    # Many webhook-mode bots call set_webhook() during initialisation;
    # if we let that run it will point Telegram at the old server and
    # polling will receive nothing.  We monkey-patch the telebot module
    # BEFORE exec_module so any set_webhook() becomes a silent no-op.
    # We restore the real method immediately after import.
    # ----------------------------------------------------------------
    _webhook_patches = []   # list of (obj, attr, original_value)
    try:
        import telebot as _tb_mod
        _orig_sw = _tb_mod.TeleBot.set_webhook
        def _noop_set_webhook(self, *a, **kw):
            print("  ℹ️  [launcher] set_webhook() suppressed during import (will use polling)")
        _tb_mod.TeleBot.set_webhook = _noop_set_webhook
        _webhook_patches.append((_tb_mod.TeleBot, 'set_webhook', _orig_sw))
    except ImportError:
        pass   # telebot not installed — nothing to patch

    try:
        spec.loader.exec_module(module)
    finally:
        # Always restore original set_webhook regardless of import success
        for _obj, _attr, _orig in _webhook_patches:
            setattr(_obj, _attr, _orig)

    print("✅ Module loaded successfully")
    sys.stdout.flush()

    # ----------------------------------------------------------------
    # Helper: find a random free TCP port (so hosted bots never clash
    # with the hosting bot's PORT=10000 health server).
    # ----------------------------------------------------------------
    def _free_port():
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
            _s.bind(('', 0))
            return _s.getsockname()[1]

    # ----------------------------------------------------------------
    # Helper: True if the module contains any Telegram bot object
    # (telebot, aiogram, PTB, or anything else with a polling-style
    # method) — used to decide whether Flask should run as a keep-alive
    # thread rather than the main entry point. Scans ALL module-level
    # objects rather than a fixed name list: a real bot object under an
    # unexpected variable name (very common — "telegram_bot", "tb",
    # anything not literally "bot"/"dp"/"application"/etc.) would
    # otherwise go undetected, causing a co-located Flask app (used for
    # keep-alive/health-checks) to be wrongly treated as the ONLY thing
    # to run — the process looks healthy forever while never actually
    # polling Telegram.
    # ----------------------------------------------------------------
    def _module_has_telegram_bot():
        _poll_methods = ('polling', 'run_polling', 'start_polling', 'infinity_polling')
        try:
            _names = list(vars(module).keys())
        except Exception:
            _names = []
        for _n in _names:
            if _n.startswith('__'):
                continue
            try:
                _obj = getattr(module, _n, None)
            except Exception:
                continue
            if _obj is None or _obj is module:
                continue
            # Flask's context-local proxies (request, g, session,
            # current_app — near-universal in any Flask app's imports) raise
            # RuntimeError, not AttributeError, when ANY attribute is
            # touched outside an active request. getattr(..., default) only
            # catches AttributeError, so probing these unconditionally
            # crashes the whole scan. Skip them by type name, and wrap the
            # actual probe too as a second layer of defense for anything
            # else with similarly surprising __getattr__ behavior.
            if type(_obj).__name__ == 'LocalProxy':
                continue
            try:
                if any(callable(getattr(_obj, _m, None)) for _m in _poll_methods):
                    return True
            except Exception:
                continue
        return False

    # ----------------------------------------------------------------
    # _clear_webhook: remove any active Telegram webhook so polling
    # can receive updates.  Bots deployed on webhook platforms (Choreo,
    # Railway, Heroku, etc.) will have a live webhook that silently
    # swallows all updates — polling never sees anything until it's gone.
    # ----------------------------------------------------------------
    def _clear_webhook(bot_obj, label='bot'):
        import time as _time
        import asyncio as _aio
        import inspect as _inspect

        def _resolve(value):
            # telebot's methods are plain sync calls — value is already the
            # real result. PTB/aiogram methods are async — calling them
            # without awaiting returns an unresolved coroutine object (which
            # silently has no attributes at all, not an error), so any
            # existing webhook would never actually be detected or removed.
            # No event loop is running yet at this point (run_polling()
            # creates its own later), so a fresh one here is safe.
            if _inspect.iscoroutine(value):
                return _aio.run(value)
            return value

        try:
            get_info = getattr(bot_obj, 'get_webhook_info', None)
            if callable(get_info):
                _wh = _resolve(get_info())
                _url = getattr(_wh, 'url', '') or ''
                if _url:
                    print(f"  ⚠️ Active webhook on {{label}}: {{_url[:60]}}...")
                    print(f"  🔧 Removing webhook — switching to polling mode...")
                    # telebot names this remove_webhook(); PTB/aiogram use
                    # delete_webhook() (matching the Bot API method name).
                    remove_fn = (getattr(bot_obj, 'remove_webhook', None) or
                                 getattr(bot_obj, 'delete_webhook', None))
                    if callable(remove_fn):
                        _resolve(remove_fn())
                    _time.sleep(1)   # give Telegram a moment to propagate
                    print(f"  ✅ Webhook removed — polling mode active")
                else:
                    print(f"  ✅ No webhook set on {{label}} — polling ready")
            # PTB / aiogram: bot object lives at .bot
            elif hasattr(bot_obj, 'bot') and callable(getattr(bot_obj.bot, 'get_webhook_info', None)):
                _clear_webhook(bot_obj.bot, label + '.bot')
        except Exception as _we:
            print(f"  ⚠️ Could not check/remove webhook ({{_we}}) — trying polling anyway")

    # ----------------------------------------------------------------
    # _make_runner: return a zero-arg callable that starts the object
    # correctly, or None if it should be called as a plain function.
    # ----------------------------------------------------------------
    def _make_runner(name, obj):
        _cls     = type(obj).__name__.lower()
        _cls_mod = (getattr(type(obj), '__module__', '') or '').lower()

        # ── pyTelegramBotAPI (telebot) ────────────────────────────────
        if callable(getattr(obj, 'polling', None)) and callable(getattr(obj, 'stop_polling', None)):
            print(f"  📱 {{name}} → Telegram (pyTelegramBotAPI) → polling")
            def _run_telebot(_b=obj, _n=name):
                _clear_webhook(_b, _n)
                _b.polling(non_stop=True, timeout=60, long_polling_timeout=60)
            return _run_telebot

        # ── aiogram Application ───────────────────────────────────────
        if hasattr(obj, 'run_polling') and hasattr(obj, 'run_webhook'):
            print(f"  📱 {{name}} → Telegram (aiogram) → run_polling")
            def _run_aiogram(_a=obj, _n=name):
                _clear_webhook(_a, _n)
                _a.run_polling()
            return _run_aiogram

        # ── python-telegram-bot Application ──────────────────────────
        if hasattr(obj, 'run_polling') and hasattr(obj, 'initialize'):
            print(f"  📱 {{name}} → Telegram (PTB) → run_polling")
            def _run_ptb(_a=obj, _n=name):
                _clear_webhook(_a, _n)
                _a.run_polling()
            return _run_ptb

        # ── Generic run_polling ───────────────────────────────────────
        if callable(getattr(obj, 'run_polling', None)):
            print(f"  📱 {{name}} → has run_polling()")
            def _run_genpoll(_a=obj, _n=name):
                _clear_webhook(_a, _n)
                _a.run_polling()
            return _run_genpoll

        # ── Pyrogram / Telethon client ────────────────────────────────
        if _cls in ('client',) and 'pyrogram' in _cls_mod:
            print(f"  📱 {{name}} → Telegram (Pyrogram) → run")
            return lambda: obj.run()
        if 'telethon' in _cls_mod and hasattr(obj, 'run_until_disconnected'):
            print(f"  📱 {{name}} → Telegram (Telethon)")
            return lambda: obj.run_until_disconnected()

        # ── PyWA (WhatsApp Cloud API) ─────────────────────────────────
        if 'pywa' in _cls_mod or (hasattr(obj, 'run_forever') and 'whatsapp' in str(type(obj)).lower()):
            print(f"  💬 {{name}} → WhatsApp (PyWA) → run_forever")
            return lambda: obj.run_forever()

        # ── Discord.py / nextcord / disnake / py-cord ────────────────
        _is_discord = ('discord' in _cls_mod or 'nextcord' in _cls_mod
                       or 'disnake' in _cls_mod or 'py_cord' in _cls_mod
                       or 'pycord' in _cls_mod)
        if _is_discord and callable(getattr(obj, 'run', None)):
            _tok = (os.environ.get('DISCORD_TOKEN')
                    or os.environ.get('TOKEN')
                    or os.environ.get('BOT_TOKEN', ''))
            if _tok:
                print(f"  🎮 {{name}} → Discord → .run(token)")
                return lambda: obj.run(_tok)
            print(f"  ⚠️ Discord object found but no DISCORD_TOKEN env var set")

        # ── Slack Bolt App ────────────────────────────────────────────
        if 'slack' in _cls_mod and callable(getattr(obj, 'start', None)):
            _port = int(os.environ.get('PORT', _free_port()))
            print(f"  💼 {{name}} → Slack Bolt → .start(port={{_port}})")
            return lambda: obj.start(port=_port)

        # ── Tweepy StreamingClient / Stream ──────────────────────────
        if 'tweepy' in _cls_mod:
            if callable(getattr(obj, 'filter', None)):
                print(f"  🐦 {{name}} → Twitter/X (Tweepy Stream) → .filter()")
                return lambda: obj.filter()
            if callable(getattr(obj, 'sample', None)):
                print(f"  🐦 {{name}} → Twitter/X (Tweepy) → .sample()")
                return lambda: obj.sample()

        # ── Line SDK ─────────────────────────────────────────────────
        # Line bots use Flask/ASGI webhooks — Flask detection below handles them

        # ── Matrix (matrix-nio) ───────────────────────────────────────
        if 'nio' in _cls_mod and callable(getattr(obj, 'sync_forever', None)):
            print(f"  🔷 {{name}} → Matrix (nio) → sync_forever")
            return lambda: obj.sync_forever()

        # ── IRC (irc.bot) ─────────────────────────────────────────────
        if 'irc' in _cls_mod and callable(getattr(obj, 'start', None)):
            print(f"  📡 {{name}} → IRC → .start()")
            return lambda: obj.start()

        # ── Flask / Quart ─────────────────────────────────────────────
        if _cls in ('flask', 'quart') or 'flask' in _cls_mod or 'quart' in _cls_mod:
            if _module_has_telegram_bot():
                # Pure keep-alive/health-check endpoint running alongside real
                # polling — a random internal port is fine here since it's
                # never meant to receive real external traffic.
                _port = _free_port()
                print(f"  🌐 {{name}} → Flask keep-alive thread (port {{_port}})")
                def _flask_bg(_app=obj, _p=_port):
                    try:
                        _app.run(host='0.0.0.0', port=_p, debug=False, use_reloader=False)
                    except Exception as _fe:
                        print(f"  ⚠️ Flask keep-alive: {{_fe}}")
                _thr.Thread(target=_flask_bg, daemon=True, name='FlaskKeepAlive').start()
                return 'BACKGROUND'
            else:
                # This Flask app IS the whole program (e.g. a real webhook
                # receiver) — it must bind the platform-assigned PORT, or
                # nothing external can ever reach it, random port or not.
                _port = int(os.environ.get('PORT', os.environ.get('BOT_PORT', 0)) or _free_port())
                print(f"  🌐 {{name}} → Flask/Quart → .run(port={{_port}})")
                return lambda: obj.run(host='0.0.0.0', port=_port, debug=False, use_reloader=False)

        # ── FastAPI / Starlette ───────────────────────────────────────
        if _cls in ('fastapi', 'starlette') or 'fastapi' in _cls_mod or 'starlette' in _cls_mod:
            _port = int(os.environ.get('PORT', _free_port()))
            print(f"  🌐 {{name}} → FastAPI/Starlette → uvicorn (port={{_port}})")
            def _run_asgi():
                try:
                    import uvicorn
                    uvicorn.run(obj, host='0.0.0.0', port=_port, log_level='info')
                except ImportError:
                    try:
                        import asyncio, hypercorn.asyncio as _ha, hypercorn.config as _hc
                        _cfg = _hc.Config(); _cfg.bind = [f'0.0.0.0:{{_port}}']
                        asyncio.run(_ha.serve(obj, _cfg))
                    except ImportError:
                        print("❌ uvicorn/hypercorn not found — add uvicorn to requirements.txt")
                        sys.exit(1)
            return _run_asgi

        # ── Django WSGI/ASGI ──────────────────────────────────────────
        if 'django' in _cls_mod:
            _port = int(os.environ.get('PORT', _free_port()))
            print(f"  🌐 {{name}} → Django → runserver port {{_port}}")
            import subprocess as _sp
            return lambda: _sp.run([sys.executable, 'manage.py', 'runserver',
                                     f'0.0.0.0:{{_port}}', '--noreload'],
                                    cwd=str(os.getcwd()))

        # ── Async coroutine function ──────────────────────────────────
        if _inspect.iscoroutinefunction(obj):
            import asyncio as _aio
            print(f"  ⚡ {{name}} → async function → asyncio.run")
            return lambda: _aio.run(obj())

        # ── Plain sync function / method ──────────────────────────────
        if _inspect.isfunction(obj) or _inspect.isbuiltin(obj) or _inspect.ismethod(obj):
            return None   # caller does obj()

        # ── Generic .run() ────────────────────────────────────────────
        if callable(getattr(obj, 'run', None)):
            print(f"  ▶️  {{name}} → has .run()")
            return lambda: obj.run()

        # ── Fallback: callable ────────────────────────────────────────
        if callable(obj):
            return None

        return None

    # ----------------------------------------------------------------
    # Entry point search.
    # Telegram bot objects (bot, dp) come BEFORE web app objects (app)
    # so a bot that has both a TeleBot and a Flask keep-alive doesn't
    # accidentally start the web server as the main loop.
    # ----------------------------------------------------------------
    entry_points = ['main', 'run', 'start', 'bot', 'dp', 'dispatcher',
                    'updater', 'setup', 'client', 'app', 'application']

    for entry in entry_points:
        if not hasattr(module, entry):
            continue
        attr = getattr(module, entry)
        if not callable(attr):
            continue

        print(f"✅ Entry point: {{entry}}")
        sys.stdout.flush()
        runner = _make_runner(entry, attr)

        if runner == 'BACKGROUND':
            # Flask started as daemon thread; keep searching for the real bot
            continue

        try:
            if runner is not None:
                runner()
            else:
                # Plain function — call it, then handle any returned app
                result = attr()
                if result is not None:
                    sub = _make_runner(f"{{entry}}() result", result)
                    if sub and sub != 'BACKGROUND':
                        sub()
                    elif callable(getattr(result, 'polling', None)):
                        result.polling(non_stop=True, timeout=60)
                    elif callable(getattr(result, 'run_polling', None)):
                        result.run_polling()
                    elif callable(getattr(result, 'run', None)):
                        result.run()
                    elif callable(getattr(result, 'run_forever', None)):
                        result.run_forever()
                    elif callable(getattr(result, 'serve_forever', None)):
                        result.serve_forever()
        except KeyboardInterrupt:
            print("\\n🛑 Bot stopped by user")
        except SystemExit as _se:
            sys.exit(_se.code)
        except Exception as _err:
            print(f"❌ Error in {{entry}}: {{type(_err).__name__}}: {{_err}}")
            traceback.print_exc()
            sys.stdout.flush()
            heartbeat_running = False
            sys.exit(1)

        heartbeat_running = False
        sys.exit(0)

    print("⚠️ No recognised entry point — falling through to Method 2...")
    sys.stdout.flush()

except Exception as e:
    print(f"⚠️ Import method failed: {{type(e).__name__}}: {{e}}")
    traceback.print_exc()
    sys.stdout.flush()

# ========== METHOD 2: Subprocess execution ==========
print("📌 Method 2: Running as subprocess...")
sys.stdout.flush()
import subprocess

try:
    _child_env = os.environ.copy()
    # sys.path.insert(0, _pkg_dir) above only affected THIS process's import
    # path — a subprocess gets a fresh interpreter with its own default
    # sys.path, so packages installed into the per-deployment packages/ dir
    # (via `pip install --target`) would otherwise be invisible to it.
    # Propagate via PYTHONPATH so the child can find them too.
    if os.path.isdir(_pkg_dir):
        _existing_pp = _child_env.get('PYTHONPATH', '')
        _child_env['PYTHONPATH'] = _pkg_dir + (os.pathsep + _existing_pp if _existing_pp else '')
    result = subprocess.run([sys.executable, r"{dest_script}"], env=_child_env)
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
# ==================== GITHUB REPO DEPLOYMENT ====================

def parse_github_url(raw: str):
    """
    Parse any GitHub URL/shorthand into (owner, repo, branch).
    Supports:
      github.com/owner/repo
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/branch
      owner/repo
      owner/repo@branch
    Returns (None,None,None) on failure.
    """
    import re as _re
    raw = raw.strip().rstrip('/')

    # Pull branch from @branch suffix
    branch = None
    if '@' in raw and 'github.com' not in raw.split('@')[0]:
        raw, branch = raw.rsplit('@', 1)

    raw = _re.sub(r'^https?://', '', raw)
    raw = _re.sub(r'^github\.com/', '', raw)
    raw = raw.rstrip('/')

    parts = raw.split('/')
    if len(parts) < 2:
        return None, None, None

    owner = parts[0]
    repo  = parts[1].removesuffix('.git')

    if not branch:
        if len(parts) >= 4 and parts[2] == 'tree':
            branch = '/'.join(parts[3:])
        else:
            branch = None   # will be resolved from API

    return owner, repo, branch


def resolve_default_branch(owner: str, repo: str, token: str = None) -> str:
    """Ask GitHub API for the repo's default branch."""
    headers = {"User-Agent": "BotHostingPlatform/1.0",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data.get("default_branch", "main")
    except Exception:
        return "main"


def get_repo_info(owner: str, repo: str, token: str = None) -> dict:
    """Fetch basic repo metadata for display."""
    headers = {"User-Agent": "BotHostingPlatform/1.0",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def download_github_repo(owner: str, repo: str, branch: str,
                         dest_folder: Path, token: str = None,
                         update_logs=None) -> bool:
    """
    Download a GitHub repo as a tarball and extract it to dest_folder.
    Works for both public and private repos (private requires a valid token).
    Does NOT require git to be installed.
    """
    import tarfile, io as _io

    def _log(msg):
        if update_logs:
            update_logs(msg)
        else:
            print(msg)

    url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{branch}"
    headers = {"User-Agent": "BotHostingPlatform/1.0",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    _log(f"⬇️  Downloading {owner}/{repo}@{branch}...")

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            tarball = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _log("❌ GitHub auth failed — check your token")
        elif e.code == 404:
            _log("❌ Repo not found — is it private? Provide a token")
        else:
            _log(f"❌ GitHub download HTTP {e.code}")
        return False
    except Exception as e:
        _log(f"❌ Download error: {e}")
        return False

    size_kb = len(tarball) / 1024
    _log(f"✅ Downloaded {size_kb:.0f} KB — extracting...")

    try:
        with tarfile.open(fileobj=_io.BytesIO(tarball), mode='r:gz') as tar:
            members = tar.getmembers()
            # GitHub tarballs contain a single top-level dir like "owner-repo-sha/"
            # We strip it so files land directly in dest_folder
            if members:
                prefix = members[0].name.split('/')[0] + '/'
            else:
                prefix = ''

            for member in members:
                rel = member.name
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                if not rel:
                    continue
                member.name = rel
                # Safety: no absolute paths or path traversal
                if rel.startswith('/') or '..' in rel:
                    continue
                try:
                    tar.extract(member, dest_folder)
                except Exception:
                    pass

        _log(f"✅ Repo extracted to {dest_folder.name}/")
        return True

    except Exception as e:
        _log(f"❌ Extraction error: {e}")
        return False


# Priority order for auto-detecting the main bot file
_MAIN_FILE_CANDIDATES = [
    'main.py', 'bot.py', 'app.py', 'run.py', 'start.py',
    'index.py', 'server.py', 'handler.py', '__main__.py',
]

def _find_node_main(folder: Path):
    """Entry point of a Node.js repo: package.json "main" / "start" script, else common names."""
    try:
        pj = folder / "package.json"
        if pj.exists():
            data = json.loads(pj.read_text(errors="replace"))
            cand = []
            if isinstance(data, dict):
                if isinstance(data.get("main"), str):
                    cand.append(data["main"])
                m = re.search(r"node(?:mon)?\s+([\w./\\-]+\.(?:js|mjs|cjs))", str((data.get("scripts") or {}).get("start", "")))
                if m:
                    cand.append(m.group(1))
            for c in cand:
                f = (folder / c).resolve()
                if f.is_file() and str(f).startswith(str(folder.resolve())):
                    return f
        for n in ("index.js", "bot.js", "main.js", "app.js", "server.js", "index.mjs"):
            if (folder / n).is_file():
                return folder / n
    except Exception:
        pass
    return None


# Folders that hold installed libraries / tooling — never a bot's own entry point.
_JUNK_DIRS = {'packages', 'site-packages', 'dist-packages', '.home', '.local', '.cache', 'venv', '.venv', 'env',
              'node_modules', '__pycache__', '.git', '.github', 'tests', 'test', 'docs', 'build', 'dist',
              '.tmp', 'tmp', 'lib', 'lib64', 'bin', 'include'}


def _in_junk_dir(folder: Path, p: Path) -> bool:
    try:
        parts = p.relative_to(folder).parts[:-1]
    except ValueError:
        parts = p.parts[:-1]
    return any(x in _JUNK_DIRS or x.endswith(('.dist-info', '.egg-info')) for x in parts)


def _sane_entry(deploy_folder: Path, file_name: str, deployment_id=None) -> str:
    """A saved entry point that lives inside packages/ (an old bug picked e.g. packages/anyio/to_process.py) is
    replaced by a real one found in the bot's own files; the fix is saved so it sticks."""
    try:
        deploy_folder = Path(deploy_folder)
        if not file_name or not _in_junk_dir(deploy_folder, deploy_folder / file_name):
            return file_name
        cands = find_main_file(deploy_folder)
        new = cands[0] if cands else _find_node_main(deploy_folder)
        if not new:
            return file_name
        rel = str(Path(new).relative_to(deploy_folder))
        print(f"🔧 dep#{deployment_id}: entry point {file_name!r} was inside a library folder → using {rel!r}")
        if deployment_id:
            db_execute("UPDATE deployments SET file_name = ? WHERE deployment_id = ?", (rel, deployment_id))
        return rel
    except Exception as e:
        print(f"⚠️ _sane_entry: {e}")
        return file_name


def find_main_file(folder: Path) -> list[Path]:
    """
    Return a ranked list of Python file candidates for the bot entry point.
    Priority: known names → files with if __name__ == '__main__' (shallowest first) → other top-level .py.
    Library folders (packages/, venvs, node_modules …) are never searched.
    """
    found = []

    # 1. Known entry-point names first
    for name in _MAIN_FILE_CANDIDATES:
        p = folder / name
        if p.exists():
            found.append(p)

    # 2. Any .py file with an if __name__ == '__main__' block
    for p in sorted(folder.rglob('*.py'), key=lambda q: (len(q.relative_to(folder).parts), str(q))):
        if p in found or _in_junk_dir(folder, p):
            continue
        try:
            txt = p.read_text(errors='ignore')
            if "__name__" in txt and "__main__" in txt:
                found.append(p)
        except Exception:
            pass

    # 3. Any remaining top-level .py files
    for p in sorted(folder.glob('*.py')):
        if p not in found:
            found.append(p)

    return found


def deploy_from_github(chat_id, user_id, owner, repo, branch, token,
                       env_vars_dict, plan, duration,
                       cost_coins, cost_stars, payment_method,
                       is_free=False, main_file_name=None,
                       skip_security=False):
    """
    Full deployment flow for a GitHub repo:
    download → detect requirements → install → launch.
    """
    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False

    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        send_message(chat_id, quota_exceeded_text(_q_used, _q_total),
                     {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        return False

    status_msg = send_message(chat_id, "```\n🐙 GITHUB DEPLOYMENT STARTING\n```", None)
    status_message_id = status_msg.get('result', {}).get('message_id') if status_msg else None
    logs = []

    def update_logs(msg):
        logs.append(msg)
        if status_message_id:
            try:
                edit_message(chat_id, status_message_id,
                             f"```\n🐙 GITHUB DEPLOY\n\n{chr(10).join(logs[-25:])[-3000:]}\n```", None)
            except Exception:
                pass

    try:
        # ── Same rules as a file-upload deployment ──────────────────
        # Free GitHub bots use the SAME free slots (FREE_USER_MAX_DEPLOYMENTS) and the same
        # resource quota (checked above) as uploaded bots — no separate 1-bot limit.
        if is_free and not is_admin(user_id):
            _can, _why = can_use_free_deployment(user_id)
            if not _can:
                send_message(chat_id,
                    f"⚠️ *Free Deployment Limit Reached*\n\n{_why}\n\n"
                    "Stop or delete a bot, wait for one to expire, or get a plan for more.",
                    {"inline_keyboard": [
                        [{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                        [{"text": "⭐ Get Premium",     "callback_data": "subscribe_premium"}],
                    ]})
                return False

        deploy_id     = int(datetime.now().timestamp())
        deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(deploy_id)
        deploy_folder.mkdir(parents=True, exist_ok=True)
        packages_dir  = deploy_folder / 'packages'
        packages_dir.mkdir(exist_ok=True)

        # ── Download repo ────────────────────────────────────────────
        if not download_github_repo(owner, repo, branch, deploy_folder, token, update_logs):
            edit_message(chat_id, status_message_id,
                "❌ *GitHub download failed.*\n\n"
                "• For private repos: ensure the token has `repo` scope\n"
                "• Check the repo URL and branch name",
                {"inline_keyboard": [[{"text": "🔄 Try Again", "callback_data": "github_deploy"},
                                      {"text": "🏠 Menu",       "callback_data": "main_menu"}]]})
            return False

        # ── Install all requirement sources ──────────────────────────
        update_logs("📦 Installing all dependencies (VPS mode)...")
        _installed_from_manifests = install_from_repo_requirements(deploy_folder, update_logs)

        # ── Detect main file ─────────────────────────────────────────
        if main_file_name:
            dest_script = deploy_folder / main_file_name
        else:
            candidates = find_main_file(deploy_folder)
            dest_script = candidates[0] if candidates else None
            if dest_script is None:
                dest_script = _find_node_main(deploy_folder)      # Node.js repo (like an uploaded .js bot)

        if not dest_script or not dest_script.exists():
            update_logs("❌ No main Python or Node.js file found — cannot launch")
            return False
        _gh_is_node = dest_script.suffix.lower() in _NODE_EXTS

        update_logs(f"🎯 Entry point: {dest_script.relative_to(deploy_folder)}")

        # ── Security scan of the main file ───────────────────────────
        if skip_security:
            update_logs("⏭️  Security scan skipped (admin-approved deployment)")
        else:
          try:
            scan_bytes = dest_script.read_bytes()
            blocked, crit, scan_w, scan_report = run_security_scan(scan_bytes, dest_script.name)
            # the WHOLE repo is checked for data-theft code, not just the entry point
            _more = scan_repo_data_theft(deploy_folder, skip=dest_script)
            if _more:
                crit = list(crit) + _more
                blocked = True
            _theft = data_theft_findings(crit)
            if _theft:
                update_logs("🚫 REJECTED: code tries to access other users' data / the host database")
                _safe_remove_fresh_clone(deploy_folder, user_id, deploy_id)
                reject_data_theft(chat_id, user_id, f"{owner}/{repo}", _theft)
                return False
            if blocked:
                update_logs(f"🚫 SECURITY BLOCK: {len(crit)} critical issue(s)")
                for c in crit[:5]:
                    update_logs(f"  • {c}")

                # Persist blocked file for admin review
                try:
                    pending_path = BASE_DIR / f"pending_review_{user_id}_{sanitize_upload_filename(dest_script.name)}"
                    pending_path.write_bytes(dest_script.read_bytes())
                except Exception:
                    pending_path = None

                gh_step_json = json.dumps({
                    "source": "github", "owner": owner, "repo": repo,
                    "branch": branch, "token": token or "",
                    "env_vars": env_vars_dict, "plan": plan,
                    "duration": duration, "cost_coins": cost_coins,
                    "cost_stars": cost_stars, "payment_method": payment_method,
                    "is_free": is_free,
                })
                conn_r = sqlite3.connect(DATABASE_FILE, timeout=10)
                c_r = conn_r.cursor()
                c_r.execute("""INSERT INTO security_reviews
                    (user_id, chat_id, file_name, file_path, critical_json,
                     warnings_json, report_text, source, status, created_at, user_step_json)
                    VALUES (?,?,?,?,?,?,'github_deploy_blocked','github','pending',?,?)""",
                    (user_id, chat_id, dest_script.name,
                     str(pending_path) if pending_path else '',
                     json.dumps(crit), json.dumps(scan_w),
                     datetime.now().isoformat(), gh_step_json))
                review_id = c_r.lastrowid
                conn_r.commit()
                conn_r.close()

                edit_message(chat_id, status_message_id,
                    f"🔒 *Security Review Required*\n\n"
                    f"Repo `{owner}/{repo}` triggered security checks.\n"
                    f"An admin will review and approve or reject deployment.\n"
                    f"🆔 Review ID: `{review_id}`",
                    {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})

                admin_text = (
                    f"🚨 *SECURITY REVIEW #{review_id}* (GitHub)\n\n"
                    f"👤 User: `{user_id}`\n"
                    f"🐙 Repo: `{owner}/{repo}@{branch}`\n"
                    f"📁 File: `{safe_md(dest_script.name)}`\n"
                    f"⚠️ Critical: `{len(crit)}`\n\n"
                    + '\n'.join(f"• `{safe_md(c[:120])}`" for c in crit[:8])
                    + (f"\n_...and {len(crit)-8} more_" if len(crit) > 8 else "")
                )
                admin_kb = {"inline_keyboard": [
                    [{"text": "✅ Approve & Deploy",
                      "callback_data": f"sec_approve_{review_id}"},
                     {"text": "⚠️ Approve (warn user)",
                      "callback_data": f"sec_approve_warn_{review_id}"}],
                    [{"text": "❌ Reject",
                      "callback_data": f"sec_reject_{review_id}"}],
                    [{"text": "📋 View Full Report",
                      "callback_data": f"sec_report_{review_id}"}],
                ]}
                for admin_id in ADMIN_IDS:
                    try:
                        send_message(admin_id, admin_text, admin_kb)
                    except Exception:
                        pass
                return False
            elif scan_w:
                update_logs(f"⚠️ Security scan: {len(scan_w)} warning(s) — proceeding")
          except Exception as se:
              update_logs(f"⚠️ Security scan error: {se} — proceeding anyway")

        # ── Read code for framework detection ────────────────────────
        try:
            code_content = dest_script.read_text(errors='ignore')
        except Exception:
            code_content = ""

        # ── Fill any gaps between what's actually imported and what the
        # repo's manifest files declared ──────────────────────────────
        # A repo's requirements.txt (or lack of one) is very often
        # incomplete — e.g. `from dotenv import load_dotenv` with no
        # matching line anywhere, because it "just works" locally from
        # whatever's already installed globally. install_from_repo_requirements
        # only reads explicit manifest files; it has no equivalent of the
        # upload path's import-scanning fallback. Add one here too.
        try:
            if _gh_is_node:
                raise StopIteration      # no Python import scan for a Node bot
            _gh_already = {re.split(r'[=<>!~\[\s]', str(r).strip(), 1)[0].lower()
                           for r in (_installed_from_manifests or [])}
            _gh_auto = UniversalDependencyInstaller.scan_imports(code_content, update_logs, local_dir=deploy_folder)
            _gh_gap = [a for a in _gh_auto
                       if re.split(r'[=<>!~\[\s]', a.strip(), 1)[0].lower() not in _gh_already]
            if _gh_gap:
                update_logs(f"📦 Filling {len(_gh_gap)} import(s) missing from repo manifests: "
                            f"{', '.join(_gh_gap)}")
                _gh_packages_dir = deploy_folder / 'packages'
                _gh_tmp_reqs = deploy_folder / '_autodetect_gap_reqs.txt'
                _gh_tmp_reqs.write_text('\n'.join(_gh_gap))
                install_dependencies_enhanced(_gh_tmp_reqs, update_logs, _gh_packages_dir)
                _gh_tmp_reqs.unlink(missing_ok=True)
        except StopIteration:
            pass
        except Exception as _gh_gap_err:
            update_logs(f"⚠️ Gap-fill dependency scan error: {_gh_gap_err}")

        if _gh_is_node:
            try:
                _install_npm(deploy_folder, update_logs)
            except Exception as _ne:
                update_logs(f"⚠️ npm install error: {_ne}")

        file_size = dest_script.stat().st_size

        # ── Write .env ───────────────────────────────────────────────
        env_file = deploy_folder / ".env"
        # Assign unique port
        _hosting_port = int(os.environ.get('PORT', 10000))
        if int(env_vars_dict.get('PORT', 0) or 0) in (0, _hosting_port):
            _deploy_port = _find_available_port(20000 + (deploy_id % 9000))
            env_vars_dict['PORT'] = str(_deploy_port)
            env_vars_dict['BOT_PORT'] = str(_deploy_port)
        with open(env_file, 'w') as f:
            for k, v in env_vars_dict.items():
                f.write(f"{k}={v}\n")
        update_logs(f"📝 .env written ({len(env_vars_dict)} vars)")

        # ── Create launcher ──────────────────────────────────────────
        update_logs("🔧 Creating launcher...")
        if _gh_is_node:
            start_script, frameworks = create_node_launcher_script(
                deploy_folder, dest_script, env_vars_dict, update_logs)
            launcher_script = start_script
        else:
            launcher_script, frameworks = create_enhanced_launcher_script(
                deploy_folder, dest_script, env_vars_dict, code_content, update_logs,
                packages_dir=packages_dir)

            # ── Start script ─────────────────────────────────────────
            start_script = deploy_folder / "start.sh"
            start_script.write_text(
                f"#!/bin/bash\ncd \"{deploy_folder}\"\nexport PYTHONUNBUFFERED=1\n"
                f"setsid nohup {sys.executable} \"{launcher_script}\" > output.log 2>&1 &\necho $! > pid.txt\n")
            start_script.chmod(0o755)

        update_logs("🚀 Starting bot...")
        _run_start(start_script, deploy_folder,
                       capture_output=True, text=True)

        update_logs("⏳ Waiting for initialization (12s)...")
        sleep(12)

        # ── Check alive ──────────────────────────────────────────────
        pid_file  = deploy_folder / "pid.txt"
        proc_pid  = None
        log_file  = deploy_folder / "output.log"
        if pid_file.exists():
            try:
                proc_pid = int(pid_file.read_text().strip())
            except Exception:
                pass

        is_running = False
        if proc_pid:
            try:
                os.kill(proc_pid, 0)
                is_running = True
            except Exception:
                pass

        # ── Show last log lines ──────────────────────────────────────
        if log_file.exists() and log_file.stat().st_size > 0:
            lines = [l for l in log_file.read_text(errors='replace').split('\n') if l.strip()]
            for line in lines[-20:]:
                update_logs(f"   {line[:120]}")

        if is_running:
            start_time  = datetime.now()
            is_lifetime = (plan == "lifetime")
            if is_lifetime:
                expire_time = None
            elif is_free:
                expire_time = start_time + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
            else:
                expire_time = start_time + timedelta(hours=duration * 24)
                # Free-because-of-premium → lasts for the subscription, not a flat 30/365 days.
                expire_time, payment_method, _used_credit = apply_deploy_credit(
                    user_id, plan, start_time, expire_time, payment_method)
                if not _used_credit:
                    expire_time, _premium_capped = cap_expiry_to_premium(
                        user_id, expire_time, payment_method == 'premium_free')

            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method,
                 cost_coins, cost_stars, start_time, expire_time, status, proc_pid,
                 install_log, deploy_log, is_free, is_paused, framework,
                 dependencies_installed, folder_name, source_type, github_repo, github_branch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (user_id, str(dest_script.relative_to(deploy_folder)), file_size, "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 start_time.isoformat(), expire_time.isoformat() if expire_time else None, "active", proc_pid,
                 "\n".join(logs[-100:]), "GitHub bot running",
                 1 if is_free else 0, 0, ', '.join(frameworks),
                 json.dumps([]), str(deploy_id),
                 "github", f"{owner}/{repo}", branch))
            deployment_db_id = c.lastrowid
            conn.commit()
            conn.close()

            # Save code + manifests + .env (+ any db the bot has created) to GitHub — NOW, not queued
            update_logs("💾 Saving to GitHub…")
            persist_new_deployment(deployment_db_id, "deployed")

            set_user_step(user_id, None)
            with deployment_lock:
                active_deployments[deployment_db_id] = proc_pid

            expire_str = "Never (lifetime)" if is_lifetime else expire_time.strftime('%Y-%m-%d %H:%M')
            edit_message(chat_id, status_message_id,
                f"*🎉 GITHUB DEPLOYMENT SUCCESSFUL!*\n\n"
                f"🐙 *Repo:* `{owner}/{repo}`\n"
                f"🌿 *Branch:* `{branch}`\n"
                f"🎯 *Entry:* `{display_filename(dest_script.name)}`\n"
                f"🔧 *Framework:* {get_platform_label(frameworks)}\n"
                f"📋 *Plan:* {plan.upper()}\n"
                f"📅 *Expires:* `{expire_str}`\n"
                f"{env_names_line(env_vars_dict)}"
                f"🆔 *ID:* `{deployment_db_id}`",
                {"inline_keyboard": [
                    [{"text": "📄 Runtime Logs",  "callback_data": f"view_runtime_logs_{deployment_db_id}"}],
                    [{"text": "🔄 Restart",        "callback_data": f"restart_deploy_{deployment_db_id}"}],
                    [{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                    [{"text": "🏠 Menu",           "callback_data": "main_menu"}],
                ]})

            return True
        else:
            err_tail = ""
            if log_file.exists():
                err_tail = log_file.read_text(errors='replace')[-2500:].strip()
            edit_message(chat_id, status_message_id,
                f"❌ *GITHUB DEPLOYMENT FAILED*\n\n"
                f"Repo: `{owner}/{repo}@{branch}`\n\n"
                f"*Last output:*\n```\n{err_tail[-1500:]}\n```\n\n"
                "Common fixes:\n"
                "• Set `BOT_TOKEN` in env vars\n"
                "• Check the main file name\n"
                "• Verify requirements are correct",
                {"inline_keyboard": [[{"text": "🔄 Retry", "callback_data": "github_deploy"},
                                      {"text": "🏠 Menu",  "callback_data": "main_menu"}]]})
            return False

    except Exception as e:
        print(f"❌ deploy_from_github error: {e}")
        import traceback; traceback.print_exc()
        return False


def deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars, 
                     plan, duration, cost_coins, cost_stars, payment_method, is_free=False):
    # Defense in depth: lifetime deployments are admin-only no matter how this
    # function got called — never trust the caller alone for this check.
    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False

    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        send_message(chat_id, quota_exceeded_text(_q_used, _q_total),
                     {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        Path(temp_file).unlink(missing_ok=True)
        return False

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
        packages_dir = deploy_folder / 'packages'
        packages_dir.mkdir(exist_ok=True)
        
        saved_path, saved_filename = save_user_file(user_id, temp_file, Path(temp_file).name)
        file_size = saved_path.stat().st_size
        
        dest_script = deploy_folder / saved_filename
        shutil.copy2(saved_path, dest_script)
        update_logs(f"📄 File saved: {saved_filename} ({format_file_size(file_size)})")

        _ext = dest_script.suffix.lower()
        _is_node = _ext in ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx')
        
        # Parse environment variables
        env_vars_dict = {}
        if env_vars:
            if isinstance(env_vars, dict):
                env_vars_dict = env_vars
            elif isinstance(env_vars, str):
                env_vars_dict, _ = parse_env_input(env_vars)
        
        # Read code content for analysis
        with open(dest_script, 'r', encoding='utf-8') as f:
            code_content = f.read()
        
        # Detect dependencies
        update_logs("🔍 Scanning for dependencies...")
        requirements_list = []

        if _is_node:
            # ── Node.js: package.json + npm, not requirements.txt/pip ──
            if requirements_text and requirements_text.strip().startswith('{'):
                # User uploaded an actual package.json
                try:
                    _pkg_data = json.loads(requirements_text)
                    (deploy_folder / "package.json").write_text(json.dumps(_pkg_data, indent=2))
                    _dep_count = len(_pkg_data.get('dependencies', {})) + len(_pkg_data.get('devDependencies', {}))
                    update_logs(f"📦 Using uploaded package.json ({_dep_count} package(s))")
                except Exception as _pje:
                    update_logs(f"⚠️ Invalid package.json ({_pje}) — falling back to auto-detect")
                    requirements_text = None
            if not (requirements_text and requirements_text.strip().startswith('{')):
                _js_packages = scan_js_requires(code_content)
                if _js_packages:
                    build_package_json(deploy_folder, _js_packages, dest_script.name, update_logs)
                else:
                    update_logs("ℹ️ No external npm packages detected")
            deps_success, failed = True, []
        else:
            if requirements_text and requirements_text.strip():
                requirements_list = UniversalDependencyInstaller.scan_requirements_file(requirements_text, update_logs)
                # A user-provided requirements.txt is very often incomplete —
                # e.g. `from dotenv import load_dotenv` at the top of a script
                # with no matching line in requirements.txt, because it "just
                # works" locally from whatever's already installed globally.
                # Auto-detect too and fill in anything actually imported but
                # missing, without touching what the user explicitly listed.
                _already = {re.split(r'[=<>!~\[\s]', r.strip(), 1)[0].lower() for r in requirements_list}
                _auto = UniversalDependencyInstaller.scan_imports(code_content, update_logs)
                _gap_filled = [a for a in _auto
                               if re.split(r'[=<>!~\[\s]', a.strip(), 1)[0].lower() not in _already]
                if _gap_filled:
                    requirements_list.extend(_gap_filled)
                    update_logs(f"📦 Added {len(_gap_filled)} import(s) missing from requirements.txt: "
                                f"{', '.join(_gap_filled)}")
            else:
                auto_detected = UniversalDependencyInstaller.scan_imports(code_content, update_logs, local_dir=deploy_folder)
                if auto_detected:
                    requirements_list.extend(auto_detected)
                    update_logs(f"📦 Auto-detected {len(auto_detected)} package(s)")

            # Install dependencies
            deps_success, failed = True, []
            if requirements_list:
                reqs_file = deploy_folder / "requirements.txt"
                with open(reqs_file, 'w') as f:
                    f.write('\n'.join(requirements_list))
                deps_success, failed = install_dependencies_enhanced(reqs_file, update_logs, packages_dir)

        if not deps_success and requirements_list:
            update_logs("⚠️ Some dependencies failed to install - continuing anyway")
        
        # Create .env file
        env_file = deploy_folder / ".env"
        with open(env_file, 'w') as f:
            for k, v in env_vars_dict.items():
                f.write(f"{k}={v}\n")
        update_logs(f"📝 Created .env with {len(env_vars_dict)} variables")
        
        # ---- Unique port assignment ----
        # The hosting bot already holds the platform PORT (e.g. 10000 on Render).
        # Give each deployed bot its own port so Flask keep-alive servers don't collide.
        _hosting_port = int(os.environ.get('PORT', 10000))
        _user_port    = int(env_vars_dict.get('PORT', 0) or 0)
        if _user_port == 0 or _user_port == _hosting_port:
            _deploy_port = _find_available_port(20000 + (deploy_id % 9000))
            env_vars_dict['PORT']     = str(_deploy_port)
            env_vars_dict['BOT_PORT'] = str(_deploy_port)
            # Re-write .env with the updated PORT
            with open(env_file, 'w') as f:
                for k, v in env_vars_dict.items():
                    f.write(f"{k}={v}\n")
            update_logs(f"🔌 Assigned port {_deploy_port} (hosting bot uses {_hosting_port})")
        
        # Create launcher — Python or Node.js depending on file type
        update_logs("🚀 Creating launcher...")

        if _is_node:
            start_script, frameworks = create_node_launcher_script(
                deploy_folder, dest_script, env_vars_dict, update_logs)
            launcher_script = start_script   # start.sh is the entry for Node
        else:
            launcher_script, frameworks = create_enhanced_launcher_script(
                deploy_folder, dest_script, env_vars_dict, code_content, update_logs,
                packages_dir=packages_dir)
            # Python start.sh wraps the Python launcher
            start_script = deploy_folder / "start.sh"
            start_script.write_text(
                f'#!/bin/bash\ncd "{deploy_folder}"\nexport PYTHONUNBUFFERED=1\n'
                f'setsid nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &\necho $! > pid.txt\n')
            start_script.chmod(0o755)
        
        # Start the bot
        update_logs("🚀 Starting bot process...")
        _run_start(start_script, deploy_folder, capture_output=True, text=True)
        
        # Give the process time to start (or fail fast on syntax errors)
        update_logs("⏳ Waiting for bot to initialize (10s)...")
        sleep(10)
        
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
        
        # Show the TAIL of the log so the actual error/startup line is visible
        log_file = deploy_folder / "output.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            with open(log_file, 'r', errors='replace') as f:
                raw = f.read()
            all_lines = [l for l in raw.split('\n') if l.strip()]
            # Show last 20 meaningful lines — these contain the actual error/success
            tail_lines = all_lines[-20:]
            update_logs("📋 Bot output (last lines):")
            for line in tail_lines:
                update_logs(f"   {line[:120]}")
        
        if is_running:
            start_time = datetime.now()
            is_lifetime = (plan == "lifetime")
            if is_lifetime:
                expire_time = None
            elif is_free:
                expire_time = start_time + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
            else:
                expire_time = start_time + timedelta(days=duration)
                # Free-because-of-premium → lasts for the subscription, not a flat 30/365 days.
                expire_time, payment_method, _used_credit = apply_deploy_credit(
                    user_id, plan, start_time, expire_time, payment_method)
                if not _used_credit:
                    expire_time, _premium_capped = cap_expiry_to_premium(
                        user_id, expire_time, payment_method == 'premium_free')

            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments 
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method, cost_coins, cost_stars, 
                 start_time, expire_time, status, proc_pid, install_log, deploy_log, is_free, is_paused, framework, dependencies_installed, folder_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, dest_script.name, file_size, requirements_text or "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 start_time.isoformat(), expire_time.isoformat() if expire_time else None, "active", proc_pid,
                 "\n".join(logs[-100:]), "Bot running", 1 if is_free else 0, 0,
                 ', '.join(frameworks), json.dumps(requirements_list), str(deploy_id)))
            deployment_db_id = c.lastrowid
            conn.commit()
            conn.close()

            # Save code + manifests + .env (+ any db the bot has created) to GitHub — NOW, not queued
            update_logs("💾 Saving to GitHub…")
            persist_new_deployment(deployment_db_id, "deployed")

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
            
            expiry_line = "📅 *Expires:* `Never (lifetime)`" if is_lifetime \
                else f"📅 *Expires:* {expire_time.strftime('%Y-%m-%d %H:%M:%S')}"
            duration_line = "⏱️ *Duration:* Lifetime — never expires" if is_lifetime \
                else f"⏱️ *Duration:* {duration if not is_free else FREE_DEPLOYMENT_DURATION_HOURS} {'days' if not is_free else 'hours'}"
            if not is_lifetime and not is_free and _premium_capped:
                duration_line = "⏱️ *Duration:* until your premium subscription ends"
                expiry_line += "\n⭐ _Renew premium and it keeps running (up to your plan's length)._"

            success_text = (
                f"*🎉 DEPLOYMENT SUCCESSFUL!* 🎉\n\n"
                f"📁 *File:* `{display_filename(dest_script.name)}`\n"
                f"🤖 *Platform:* {get_platform_label(frameworks)}\n"
                f"📋 *Plan:* {plan.upper()}\n"
                f"{duration_line}\n"
                f"{expiry_line}\n"
                f"📦 *Dependencies:* {len(requirements_list)} package(s)\n"
                f"{env_names_line(env_vars_dict)}"
                f"🆔 *ID:* `{deployment_db_id}`"
            )
            
            if failed:
                success_text += f"\n\n⚠️ *Partial Success:* {len(failed)} package(s) failed to install"
            
            edit_message(chat_id, status_message_id, success_text, success_keyboard)
            
            if proc_pid:
                with deployment_lock:
                    active_deployments[deployment_db_id] = proc_pid

            async_backup(f"deployment_{deployment_db_id}")
            return True
        else:
            # Read the TAIL of the log — that's where the error actually is
            error_tail = ""
            if log_file.exists() and log_file.stat().st_size > 0:
                with open(log_file, 'r', errors='replace') as f:
                    raw = f.read()
                # Last 3000 chars contains the crash traceback
                error_tail = raw[-3000:].strip()
            
            # Try to detect common failure causes from the log
            diagnosis = ""
            if error_tail:
                low = error_tail.lower()
                if 'no bot token' in low or 'bot_token' in low and 'not' in low:
                    diagnosis = "\n\n💡 *Tip:* Your bot needs `BOT_TOKEN` — set it in env vars when deploying."
                elif 'modulenotfounderror' in low or 'importerror' in low:
                    import re as _re
                    m = _re.search(r"No module named '([^']+)'", error_tail)
                    pkg = m.group(1) if m else "unknown"
                    diagnosis = f"\n\n💡 *Tip:* Missing package `{pkg}` — add it to `requirements.txt` and redeploy."
                elif 'syntaxerror' in low:
                    diagnosis = "\n\n💡 *Tip:* Your bot has a Python syntax error — test it locally first."
                elif 'telegramapiexception' in low or 'unauthorized' in low:
                    diagnosis = "\n\n💡 *Tip:* Invalid bot token — check your `BOT_TOKEN` env var."
                elif 'address already in use' in low or 'port' in low and 'bind' in low:
                    diagnosis = "\n\n💡 *Tip:* Port conflict — another process is using the same port."
                elif 'permissionerror' in low:
                    diagnosis = "\n\n💡 *Tip:* File permission error — contact support."
                else:
                    diagnosis = "\n\n💡 *Tip:* Check the error above. Common fixes:\n• Add `BOT_TOKEN` env var\n• Add a `requirements.txt`\n• Make sure your bot has no syntax errors"
            
            error_msg = f"❌ *DEPLOYMENT FAILED*\n\n"
            if error_tail:
                error_msg += f"*Last output (tail):*\n```\n{error_tail}\n```{diagnosis}"
            else:
                error_msg += (
                    "No output captured. Common issues:\n"
                    "• Missing required env vars (e.g. `BOT_TOKEN`)\n"
                    "• Syntax error in your code\n"
                    "• Missing packages not in requirements.txt\n\n"
                    "*Tip:* Test your bot locally before deploying."
                )
            
            edit_message(chat_id, status_message_id, error_msg[:4000])
            
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments 
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method, cost_coins, cost_stars, 
                 start_time, expire_time, status, install_log, deploy_log, error_log, is_free, framework, folder_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, dest_script.name, file_size, requirements_text or "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 datetime.now().isoformat(), datetime.now().isoformat(), "failed",
                 "\n".join(logs[-50:]), "Bot failed to start", error_msg[:2000], 1 if is_free else 0,
                 ', '.join(frameworks), str(deploy_id)))
            conn.commit()
            conn.close()
            
            Path(temp_file).unlink(missing_ok=True)
            return False
            
    except Exception as e:
        error_msg = f"❌ *DEPLOYMENT FAILED*\n\nException: {str(e)}\n{traceback.format_exc()}"
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
        send_message(chat_id, f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited deployments!",
                    {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        return False
    
    return deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars,
                           "free", FREE_DEPLOYMENT_DURATION_HOURS, 0, 0, "none", is_free=True)

def deploy_paid_bot(chat_id, user_id, temp_file, requirements_text, env_vars, plan, duration, cost_coins, cost_stars, payment_method):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", None)
        return False
    
    # Central guard: lifetime is admin-only on EVERY entry point (stars, coins,
    # premium-free), and only known plans are accepted.
    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False
    if plan not in ("monthly", "yearly", "lifetime"):
        send_message(chat_id, "❌ Invalid deployment plan.")
        return False

    if premium_covers_plan(user_id, plan):
        send_message(chat_id,
            f"*✨ PREMIUM BENEFIT ACTIVE!*\n\n"
            f"As a premium user, your {plan.upper()} deployment is *FREE*!\n\n"
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
        
        c.execute("SELECT proc_pid, file_name, user_id, is_free, plan, payment_method, expire_time FROM deployments WHERE deployment_id = ?", (deployment_id,))
        row = c.fetchone()
        
        if not row:
            send_message(chat_id, "❌ Deployment not found")
            return False
        
        proc_pid, file_name, owner_id, is_free, plan, _pay_method, _exp_str = row
        _credit_secs = 0
        if not is_free and _pay_method in ('coins', 'stars', 'credit') and _exp_str:
            try:
                _credit_secs = grant_deploy_credit(owner_id, plan, deployment_id, datetime.fromisoformat(_exp_str))
            except Exception:
                _credit_secs = 0
        
        if owner_id != user_id and not is_admin(user_id):
            send_message(chat_id, "❌ Permission denied")
            return False
        
        if proc_pid:
            try:
                kill_deployment_process(proc_pid)
                sleep(1)
            except:
                pass
        
        deploy_folder = get_deploy_folder(owner_id, deployment_id)
        if deploy_folder.exists():
            shutil.rmtree(deploy_folder)
        
        c.execute("DELETE FROM deployments WHERE deployment_id = ?", (deployment_id,))
        conn.commit()
        conn.close()

        # Remove it from the GitHub files repo too (queued; remembered in the DB and
        # retried until it succeeds, so a deleted bot never lingers in the backup).
        files_repo_async_delete(owner_id, deployment_id)

        if is_free:
            used_count = get_free_deployment_used_count(user_id)
            remaining = FREE_USER_MAX_DEPLOYMENTS - used_count
            send_message(chat_id, 
                f"✅ Deployment `{deployment_id}` deleted!\n\n"
                f"📁 File: `{display_filename(file_name)}`\n"
                f"🆓 Free slots left: `{remaining}/{FREE_USER_MAX_DEPLOYMENTS}`")
        else:
            _extra = ""
            if _credit_secs:
                _d, _h = divmod(_credit_secs // 3600, 24)
                _extra = (f"\n\n💳 *Credit saved:* `{_d}d {_h}h` of your paid {plan} time. "
                          f"Deploy your updated bot on the *{plan.capitalize()}* plan and it's free — no second payment.")
            send_message(chat_id, f"✅ Deployment `{deployment_id}` deleted!\n\n📁 File: `{display_filename(file_name)}`{_extra}")
        
        with deployment_lock:
            if deployment_id in active_deployments:
                del active_deployments[deployment_id]
        
        update_system_stats()
        return True
        
    except Exception as e:
        send_message(chat_id, f"❌ Error deleting deployment: {str(e)}")
        return False

def progress_bar(pct, width=12):
    pct = max(0, min(100, int(pct)))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled) + f" {pct}%"


def make_restore_progress(chat_id, title="♻️ *Files are missing locally — restoring from your GitHub backup…*"):
    """Returns a progress(pct, label) callback that keeps ONE Telegram message updated with a bar.
    Edits are throttled (Telegram rate-limits them); the message id is taken from the first send."""
    state = {"mid": None, "last": 0.0, "pct": -1}
    def _render(pct, label):
        if str(label).startswith("❌"):
            pct = max(state["pct"], 0)       # a failure must not look like a full bar
        return f"{title}\n\n`{progress_bar(pct)}`\n_{label}_"
    def progress(pct, label=""):
        now = time.time()
        if state["mid"] is None:
            r = send_message(chat_id, _render(pct, label))
            try: state["mid"] = (r or {}).get("result", {}).get("message_id")
            except Exception: state["mid"] = None
            state["last"], state["pct"] = now, pct
            return
        if state["mid"] and (pct == 100 or (now - state["last"] >= 1.5 and pct != state["pct"])):
            edit_message(chat_id, state["mid"], _render(pct, label))
            state["last"] = now
            if not str(label).startswith("❌"):
                state["pct"] = pct
    return progress


def restart_deployment(deployment_id, user_id, chat_id, force_free_downgrade=False):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("""SELECT proc_pid, file_name, user_id, is_paused, env_vars,
                            status, start_time, expire_time, is_free, plan
                     FROM deployments WHERE deployment_id=?""", (deployment_id,))
        row = c.fetchone()

        if not row:
            conn.close()
            send_message(chat_id, "❌ Deployment not found")
            return False

        proc_pid, file_name, owner_id, is_paused, env_vars_json, \
            status, start_time_str, expire_time_str, is_free, plan = row

        if owner_id != user_id and not is_admin(user_id):
            conn.close()
            send_message(chat_id, "❌ Permission denied")
            return False

        # Paused premium bots require premium renewal, not a simple restart
        # (lifetime deployments never expire, so they never legitimately hit
        # this state — but guard it anyway rather than trust that invariant)
        if is_paused and not is_free and plan != "lifetime":
            conn.close()
            send_message(chat_id,
                "⏸️ This premium deployment is paused.\n\n"
                "Purchase or renew premium to resume it — your database stays intact.",
                {"inline_keyboard": [[{"text": "⭐ Renew Premium", "callback_data": "subscribe_premium"}]]})
            return False

        # ── Re-verify premium status before auto-extending a premium plan ──
        # A deployment's `plan` reflects what it was created under, not the
        # owner's CURRENT subscription — someone could restart a long-expired
        # "monthly" deployment and silently get another free extension of the
        # original duration even if their premium lapsed ages ago. Re-check
        # live status and let the user explicitly choose how to proceed
        # instead of assuming.
        needs_expiry_extension = False
        if expire_time_str and not is_free and plan != "lifetime":
            current_expire = datetime.fromisoformat(expire_time_str)
            needs_expiry_extension = status in ('stopped', 'failed') or current_expire < datetime.now()

        if needs_expiry_extension and not force_free_downgrade:
            conn.close()
            if not is_user_premium(owner_id):
                send_message(chat_id,
                    f"⭐ *This bot's plan (`{plan}`) has expired, and your premium "
                    f"subscription is no longer active.*\n\n"
                    f"Your database and files are safe. How would you like to restart it?",
                    {"inline_keyboard": [
                        [{"text": "⭐ Renew Premium", "callback_data": "subscribe_premium"}],
                        [{"text": "🆓 Restart as Free (24h)", "callback_data": f"restart_free_{deployment_id}"}],
                        [{"text": "❌ Cancel", "callback_data": f"view_deploy_{deployment_id}"}],
                    ]})
                return False
            # Still genuinely premium — reopen the connection and proceed
            # exactly as before.
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()

        deploy_folder = get_deploy_folder(owner_id, deployment_id)
        file_name     = _sane_entry(deploy_folder, file_name, deployment_id)
        dest_script   = deploy_folder / file_name

        # Files gone (disk wiped)? Bring them back from the GitHub files repo first.
        if (not deploy_folder.exists() or not dest_script.exists()) and FILES_REPO_ENABLED:
            _prog = make_restore_progress(chat_id)
            _prog(0, "Starting")
            _ok, _renv = _files_restore_for_relaunch(
                owner_id, deployment_id, deploy_folder, dest_script,
                log=lambda m, _d=deployment_id: print(f"   [restore dep#{_d}] {m}"),
                progress=_prog)
            if not _ok:
                try: _prog(100, "❌ No usable backup found")
                except Exception: pass
            if _ok and _renv and not (json.loads(env_vars_json) if env_vars_json else {}):
                env_vars_json = json.dumps(_renv)

        if not deploy_folder.exists():
            conn.close()
            send_message(chat_id, "❌ Deployment folder missing — please create a new deployment.")
            return False
        if not dest_script.exists():
            conn.close()
            send_message(chat_id,
                f"❌ Bot file `{file_name}` missing.\n\nPlease delete this deployment and create a new one.")
            return False

        # A bot that is NOT currently running needs fresh headroom to start
        if status != 'active':
            _q_ok, _q_used, _q_total = check_user_quota(owner_id)
            if not _q_ok:
                conn.close()
                send_message(chat_id, quota_exceeded_text(_q_used, _q_total),
                             {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
                return False

        # Kill any stale process
        if proc_pid:
            try:
                kill_deployment_process(proc_pid)
                sleep(1)
            except Exception:
                pass

        env_vars = json.loads(env_vars_json) if env_vars_json else {}

        # Rewrite .env to ensure it's fresh (paired with the DB's env vars)
        env_file = deploy_folder / ".env"
        env_file.write_text('\n'.join(f"{k}={v}" for k, v in env_vars.items()) + '\n')

        # Reinstall dependencies if they're missing (cheap no-op when present)
        _install_deps_for_folder(deploy_folder, dest_script, lambda m: None, only_if_missing=True)

        # Build start.sh for the right runtime. A Node.js / TypeScript bot must NOT go
        # through the Python launcher (it would try to run a .js file with python3) —
        # same branch the auto-relaunch path uses.
        if dest_script.suffix.lower() in _NODE_EXTS:
            start_script, _ = create_node_launcher_script(
                deploy_folder, dest_script, env_vars, lambda x: None)
        else:
            try:
                code_content = dest_script.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                code_content = ""
            launcher_script, _ = create_enhanced_launcher_script(
                deploy_folder, dest_script, env_vars, code_content, lambda x: None)
            start_script = deploy_folder / "start.sh"
            start_script.write_text(
                f'#!/bin/bash\ncd "{deploy_folder}"\nexport PYTHONUNBUFFERED=1\n'
                f'setsid nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &\necho $! > pid.txt\n')
            start_script.chmod(0o755)

        is_running, new_pid = _launch_and_check(start_script, deploy_folder)
        if not is_running and _log_shows_missing_dependency(deploy_folder):
            # Died on an import/require error — reinstall everything from the manifests, retry once.
            _install_deps_for_folder(deploy_folder, dest_script, lambda m: None)
            is_running, new_pid = _launch_and_check(start_script, deploy_folder)

        if is_running:
            if expire_time_str is None:
                # Lifetime deployment — never had an expiry and never gets one.
                c.execute("UPDATE deployments SET proc_pid=?, status='active', is_paused=0 WHERE deployment_id=?",
                          (new_pid, deployment_id))
            elif force_free_downgrade:
                # User explicitly chose to continue on the free tier rather
                # than renew — actually downgrade the plan, not just extend
                # the expiry, so it's consistently treated as free from now on
                # (future restarts, expiry monitor, resource accounting, etc.)
                new_expire = datetime.now() + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
                c.execute("""UPDATE deployments
                             SET proc_pid=?, status='active', is_paused=0, expire_time=?,
                                 start_time=?, is_free=1, plan='free'
                             WHERE deployment_id=?""",
                          (new_pid, new_expire.isoformat(), datetime.now().isoformat(), deployment_id))
            else:
                # For stopped/expired bots: extend expiry from now
                current_expire = datetime.fromisoformat(expire_time_str)
                if status in ('stopped', 'failed') or current_expire < datetime.now():
                    if is_free:
                        new_expire = datetime.now() + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
                    else:
                        # Premium restart: owner's active status was already
                        # re-verified above — keep the original duration window
                        original_hours = max(1, (current_expire - datetime.fromisoformat(start_time_str)).total_seconds() / 3600)
                        new_expire = datetime.now() + timedelta(hours=original_hours)
                        # This extension exists only because the owner is premium right now,
                        # so it can't run past the end of that subscription.
                        new_expire, _ = cap_expiry_to_premium(owner_id, new_expire, plan != "lifetime")
                    c.execute("""UPDATE deployments
                                 SET proc_pid=?, status='active', is_paused=0, expire_time=?, start_time=?
                                 WHERE deployment_id=?""",
                              (new_pid, new_expire.isoformat(), datetime.now().isoformat(), deployment_id))
                else:
                    c.execute("UPDATE deployments SET proc_pid=?, status='active', is_paused=0 WHERE deployment_id=?",
                              (new_pid, deployment_id))

            conn.commit()
            conn.close()
            with deployment_lock:
                active_deployments[deployment_id] = new_pid

            async_backup(f"restart_{deployment_id}")
            send_message(chat_id,
                f"✅ *Bot #{deployment_id} Restarted*\n\n"
                f"Your database and settings are preserved.\n"
                f"Bot is running from the same deployment folder.",
                {"inline_keyboard": [
                    [{"text": "📄 View Logs",       "callback_data": f"view_runtime_logs_{deployment_id}"}],
                    [{"text": "📦 My Deployments",  "callback_data": "my_deployments"}],
                ]})
            return True
        else:
            log_tail = ""
            lf = deploy_folder / "output.log"
            if lf.exists():
                log_tail = lf.read_text(errors='replace')[-600:].strip()
            c.execute("UPDATE deployments SET status='failed' WHERE deployment_id=?", (deployment_id,))
            conn.commit()
            conn.close()
            send_message(chat_id,
                f"❌ *Bot #{deployment_id} failed to start*\n\n"
                f"```\n{log_tail[-400:]}\n```\n\n"
                "Check your env vars and try again.",
                {"inline_keyboard": [
                    [{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{deployment_id}"}],
                    [{"text": "🔄 Retry",     "callback_data": f"restart_deploy_{deployment_id}"}],
                ]})
            return False

    except Exception as e:
        print(f"❌ restart_deployment error: {e}")
        send_message(chat_id, f"❌ Error restarting: {e}")
        return False

def stop_deployment(deployment_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT proc_pid FROM deployments WHERE deployment_id = ?", (deployment_id,))
    row = c.fetchone()
    if row and row[0]:
        try:
            kill_deployment_process(row[0])
        except:
            pass
    c.execute("UPDATE deployments SET status = 'stopped', proc_pid = NULL WHERE deployment_id = ?", (deployment_id,))
    conn.commit()
    conn.close()

    with deployment_lock:
        if deployment_id in active_deployments:
            del active_deployments[deployment_id]

    # The process is down, so its database files are now in their final, consistent state.
    files_repo_async_save(deployment_id, "stopped")

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
        f"```\n📋 INSTALLATION LOGS\n📁 {display_filename(file_name)}\n\n{log_text[-3000:]}\n```",
        keyboard)

def _build_full_log_text(user_id, dep_id):
    """Complete log (install log + entire runtime output + error) for one of the user's deployments.
    Returns (text, file_name) or (None, None) when the deployment isn't theirs."""
    row = db_execute("SELECT deploy_log, file_name, status, error_log FROM deployments "
                     "WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id), fetch='one')
    if not row:
        return None, None
    deploy_log, file_name, status, error_log = row
    parts = [f"=== DEPLOYMENT #{dep_id} — {display_filename(file_name)} — {str(status).upper()} ===\n"]
    if deploy_log:
        parts.append("--- INSTALL / DEPLOY LOG ---\n" + deploy_log)
    lf = get_deploy_folder(user_id, dep_id) / "output.log"
    try:
        if lf.exists():
            parts.append("--- RUNTIME OUTPUT (full) ---\n" + lf.read_text(errors="replace"))
    except Exception as e:
        parts.append(f"(could not read runtime log: {e})")
    if error_log:
        parts.append("--- ERROR ---\n" + error_log)
    return "\n\n".join(parts), file_name

def send_full_logs(chat_id, user_id, dep_id):
    text, fname = _build_full_log_text(user_id, dep_id)
    if text is None:
        send_message(chat_id, "❌ Deployment not found")
        return
    out = LOGS_DIR / f"logs_{user_id}_{dep_id}_{secrets.token_hex(4)}.txt"
    try:
        out.write_text(text, encoding="utf-8")
        send_document(chat_id, out, caption=f"📥 Download logs — {display_filename(fname)}\n"
                      "Open the file, then select all and copy.", filename=f"logs_{dep_id}.txt")
    finally:
        try: out.unlink()
        except Exception: pass

def send_copyable_logs(chat_id, user_id, dep_id, max_chunks=40, chunk=3900):
    """The COMPLETE log as consecutive tap-to-copy code blocks (Telegram copies a block on tap).
    Telegram caps a message at 4096 chars, so a long log is split on line boundaries."""
    text, fname = _build_full_log_text(user_id, dep_id)
    if text is None:
        send_message(chat_id, "❌ Deployment not found")
        return
    text = text.replace("`", "'")
    pieces, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > chunk:                      # a single huge line
            if cur: pieces.append(cur); cur = ""
            pieces.append(line[:chunk]); line = line[chunk:]
        if len(cur) + len(line) > chunk:
            pieces.append(cur); cur = ""
        cur += line
    if cur: pieces.append(cur)
    if len(pieces) > max_chunks:
        send_message(chat_id, f"ℹ️ This log is very long ({len(pieces)} parts). Sending the latest {max_chunks}; "
                              "use 📥 Download Logs for the complete file.")
        pieces = pieces[-max_chunks:]
    elif len(pieces) > 1:
        send_message(chat_id, f"📋 Full logs in {len(pieces)} parts — tap a block to copy it.")
    for piece in pieces:
        send_message(chat_id, f"```\n{piece.rstrip()}\n```")
        if len(pieces) > 3:
            sleep(0.4)                                # stay under Telegram's per-chat flood limit

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
    log_file = get_deploy_folder(user_id, dep_id) / "output.log"
    
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
            [{"text": "📥 Download Logs", "callback_data": f"full_logs_{dep_id}"},
             {"text": "📋 Copy Logs", "callback_data": f"copy_logs_{dep_id}"}],
            [{"text": "📋 Install Logs", "callback_data": f"view_install_logs_{dep_id}"}],
            [{"text": "🔄 Restart", "callback_data": f"restart_deploy_{dep_id}"}],
            [{"text": "🗑️ Delete", "callback_data": f"delete_deploy_{dep_id}"}],
            [{"text": "🔙 Back", "callback_data": f"view_deploy_{dep_id}"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"```\n📄 RUNTIME LOGS\n📁 {display_filename(file_name)} (Status: {status.upper()})\n\n{log_text[-3000:]}\n```",
        keyboard)

# ==================== EDIT DEPLOYMENT (env · requirements · code) ====================
EDIT_MAX_MANIFEST_BYTES = 1024 * 1024


def _edit_load(user_id, dep_id):
    """The deployment's editable state, or None if it isn't this user's."""
    row = db_execute("SELECT file_name, env_vars, status, framework FROM deployments "
                     "WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id), fetch='one')
    if not row:
        return None
    file_name, env_json, status, framework = row
    try:
        env = json.loads(env_json) if env_json else {}
        if not isinstance(env, dict):
            env = {}
    except Exception:
        env = {}
    folder = get_deploy_folder(user_id, dep_id)
    return {"file_name": file_name, "env": env, "status": status, "framework": framework,
            "folder": folder, "script": folder / file_name,
            "is_node": Path(file_name).suffix.lower() in _NODE_EXTS}


def _edit_keyhash(key):
    return hashlib.sha1(str(key).encode()).hexdigest()[:8]


def _edit_user_keys(env):
    """Keys the user manages (platform-added PORT etc. are hidden)."""
    return [k for k in env if k not in _PLATFORM_ENV_KEYS]


def _edit_push(dep_id, what):
    """Push the edited deployment to the GitHub files repo RIGHT NOW (and the platform DB, which holds
    env vars / requirements). Returns a one-line status for the user. A failed push is retried by the
    queue, so the edit is never lost."""
    try:
        async_backup(f"edit_{what}_{dep_id}")          # platform DB (env_vars / requirements columns)
    except Exception:
        pass
    if not FILES_REPO_ENABLED:
        return "ℹ️ GitHub files repo isn't configured — edit saved on the server only."
    try:
        r = files_repo_save_deployment(dep_id, f"edit {what}")
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    if r.get("ok"):
        n = r.get("changed", 0)
        return f"☁️ GitHub updated ({n} file(s) changed)." if n else "☁️ GitHub already up to date."
    try:
        files_repo_async_save(dep_id, f"edit {what} (retry)")
    except Exception:
        pass
    return f"⚠️ Saved here, but GitHub update failed ({str(r.get('error'))[:80]}) — will retry automatically."


def _edit_save_env(user_id, dep_id, env, folder):
    """Save env vars to the DB + .env, then push to GitHub. Returns the GitHub status line."""
    db_execute("UPDATE deployments SET env_vars = ? WHERE deployment_id = ? AND user_id = ?",
               (json.dumps(env), dep_id, user_id))
    try:
        (folder / ".env").write_text(''.join(f"{k}={v}\n" for k, v in env.items()))
    except Exception as e:
        print(f"⚠️ edit: could not rewrite .env for #{dep_id}: {e}")
    return _edit_push(dep_id, "env")


def _edit_after_kb(dep_id):
    return {"inline_keyboard": [
        [{"text": "🔄 Apply & Restart", "callback_data": f"restart_deploy_{dep_id}"}],
        [{"text": "✏️ Keep editing", "callback_data": f"edit_deploy_{dep_id}"}],
        [{"text": "🔙 Deployment", "callback_data": f"view_deploy_{dep_id}"}]]}


def _edit_reply(chat_id, message_id, text, kb):
    if message_id:
        edit_message(chat_id, message_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def show_edit_menu(chat_id, message_id, user_id, dep_id):
    d = _edit_load(user_id, dep_id)
    if not d:
        _edit_reply(chat_id, message_id, "❌ Deployment not found", None)
        return
    set_user_step(user_id, None)
    n_env = len(_edit_user_keys(d["env"]))
    reqs_label = "📦 package.json" if d["is_node"] else "📦 requirements.txt"
    text = (f"*✏️ EDIT DEPLOYMENT #{dep_id}*\n\n"
            f"📁 File: `{display_filename(d['file_name'])}`\n"
            f"🔑 Env vars: `{n_env}`\n"
            f"🔘 Status: `{d['status']}`\n\n"
            f"Pick what to change. Changes are saved straight away and take effect after a *restart*.")
    kb = {"inline_keyboard": [
        [{"text": f"🔑 Environment Variables ({n_env})", "callback_data": f"edit_env_{dep_id}"}],
        [{"text": reqs_label, "callback_data": f"edit_reqs_{dep_id}"}],
        [{"text": f"📄 Replace {display_filename(d['file_name'])[:24]}", "callback_data": f"edit_code_{dep_id}"}],
        [{"text": "🔄 Apply & Restart", "callback_data": f"restart_deploy_{dep_id}"}],
        [{"text": "🔙 Back", "callback_data": f"view_deploy_{dep_id}"}]]}
    _edit_reply(chat_id, message_id, text, kb)


# ---------- environment variables ----------
def show_edit_env(chat_id, message_id, user_id, dep_id, note=""):
    d = _edit_load(user_id, dep_id)
    if not d:
        _edit_reply(chat_id, message_id, "❌ Deployment not found", None)
        return
    set_user_step(user_id, None)
    keys = _edit_user_keys(d["env"])
    listing = "\n".join(f"• `{re.sub(r'[^A-Za-z0-9_]', '_', k)}`" for k in keys[:40]) or "_none set_"
    if len(keys) > 40:
        listing += f"\n_…and {len(keys) - 40} more_"
    text = (f"{note}*🔑 ENV VARIABLES — #{dep_id}*\n\n{listing}\n\n"
            f"Tap a name to *remove* it, or add / update variables. Values are never shown.")
    rows, row = [], []
    for k in keys[:40]:
        row.append({"text": f"🗑️ {k}"[:30], "callback_data": f"edit_envdel_{dep_id}_{_edit_keyhash(k)}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "➕ Add / Update variables", "callback_data": f"edit_envadd_{dep_id}"}])
    rows.append([{"text": "🔙 Back", "callback_data": f"edit_deploy_{dep_id}"}])
    _edit_reply(chat_id, message_id, text, {"inline_keyboard": rows})


def edit_env_delete(chat_id, message_id, user_id, dep_id, key_hash):
    d = _edit_load(user_id, dep_id)
    if not d:
        return
    target = next((k for k in _edit_user_keys(d["env"]) if _edit_keyhash(k) == key_hash), None)
    if not target:
        show_edit_env(chat_id, message_id, user_id, dep_id, note="⚠️ That variable was already removed.\n\n")
        return
    env = dict(d["env"]); env.pop(target, None)
    gh = _edit_save_env(user_id, dep_id, env, d["folder"])
    show_edit_env(chat_id, message_id, user_id, dep_id,
                  note=f"✅ Removed `{re.sub(r'[^A-Za-z0-9_]', '_', target)}` — restart to apply.\n{gh}\n\n")


def edit_env_prompt(chat_id, message_id, user_id, dep_id):
    if not _edit_load(user_id, dep_id):
        return
    set_user_step(user_id, 'edit_env', edit_dep=dep_id)
    _edit_reply(chat_id, message_id,
        "*➕ ADD / UPDATE VARIABLES*\n\nSend one per line:\n```\nBOT_TOKEN=12345:ABC\nADMIN_ID=123456\n```\n"
        "A name that already exists is *updated*; new names are *added*. Your message is deleted afterwards "
        "so secrets don't stay in the chat.\n\n📄 You can also *upload a `.env` file* instead.",
        {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"edit_env_{dep_id}"}]]})


def _edit_try_delete(chat_id, message_id):
    try:
        http_get(f"{TELEGRAM_API}/deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


def handle_edit_env_text(chat_id, user_id, text, user_message_id, dep_id):
    d = _edit_load(user_id, dep_id)
    if not d:
        set_user_step(user_id, None)
        send_message(chat_id, "❌ Deployment not found")
        return
    new_vars, ignored = parse_env_input(text)
    new_vars = {k: v for k, v in new_vars.items() if k not in _PLATFORM_ENV_KEYS}
    if user_message_id:
        _edit_try_delete(chat_id, user_message_id)
    if not new_vars:
        send_message(chat_id,
            "❌ I couldn't find any `KEY=VALUE` lines. Send them like:\n```\nAPI_KEY=abc123\n```",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"edit_env_{dep_id}"}]]})
        return
    env = dict(d["env"])
    added = [k for k in new_vars if k not in env]
    updated = [k for k in new_vars if k in env]
    env.update(new_vars)
    gh = _edit_save_env(user_id, dep_id, env, d["folder"])
    set_user_step(user_id, None)
    parts = []
    if added:   parts.append("➕ Added: " + ", ".join(f"`{k}`" for k in added[:15]))
    if updated: parts.append("♻️ Updated: " + ", ".join(f"`{k}`" for k in updated[:15]))
    if ignored: parts.append(f"⚠️ {ignored} line(s) ignored (not `KEY=VALUE`)")
    send_message(chat_id, "✅ *Saved*\n\n" + "\n".join(parts) + f"\n{gh}\n\nRestart to apply.", _edit_after_kb(dep_id))


# ---------- requirements / package.json ----------
def show_edit_reqs(chat_id, message_id, user_id, dep_id):
    """Clicking Edit → requirements/package.json goes straight to a FILE-UPLOAD prompt."""
    d = _edit_load(user_id, dep_id)
    if not d:
        _edit_reply(chat_id, message_id, "❌ Deployment not found", None)
        return
    name = "package.json" if d["is_node"] else "requirements.txt"
    mf = d["folder"] / name
    try:
        cur = mf.read_text(errors="replace").strip() if mf.exists() else ""
    except Exception:
        cur = ""
    shown = (cur[:900] + ("\n…" if len(cur) > 900 else "")).replace("`", "'") if cur else "(none)"
    set_user_step(user_id, 'edit_reqs', edit_dep=dep_id)
    _edit_reply(chat_id, message_id,
        f"*📦 REPLACE {name.upper()} — #{dep_id}*\n\nCurrent:\n```\n{shown}\n```\n"
        f"📤 *Upload the new `{name}` file now.* It replaces the old one and the packages are reinstalled.",
        {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"edit_deploy_{dep_id}"}]]})


def edit_reqs_prompt(chat_id, message_id, user_id, dep_id):
    show_edit_reqs(chat_id, message_id, user_id, dep_id)


def handle_edit_reqs_text(chat_id, user_id, text, dep_id):
    d = _edit_load(user_id, dep_id)
    name = "package.json" if (d and d["is_node"]) else "requirements.txt"
    send_message(chat_id, f"📤 Please *upload* the `{name}` file (attach it as a document) — pasted text isn't accepted.",
                 {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"edit_deploy_{dep_id}"}]]})


def handle_edit_reqs_document(chat_id, user_id, doc, file_name, dep_id):
    d = _edit_load(user_id, dep_id)
    if not d:
        set_user_step(user_id, None)
        send_message(chat_id, "❌ Deployment not found")
        return
    ok_name = (file_name == "package.json") if d["is_node"] else file_name.lower().endswith(".txt")
    if not ok_name:
        send_message(chat_id, "❌ Please upload " + ("`package.json`." if d["is_node"] else "a `requirements.txt` file."))
        return
    raw = _download_telegram_file_bytes(doc.get("file_id"))
    if raw is None:
        send_message(chat_id, "❌ Could not download the file from Telegram.")
        return
    _edit_apply_manifest(chat_id, user_id, dep_id, raw.decode("utf-8", errors="replace"))


def _edit_reinstall(chat_id, user_id, dep_id, d, what):
    """Reinstall dependencies for the edited deployment, reporting progress in one message."""
    r = send_message(chat_id, f"📦 Installing dependencies after the {what} change… this can take a few minutes.")
    mid = (r or {}).get("result", {}).get("message_id")
    lines = []
    ok = True
    try:
        ok = _install_deps_for_folder(d["folder"], d["script"], lambda m: lines.append(str(m)))
    except Exception as e:
        ok = False
        lines.append(f"error: {e}")
    failed = [l for l in lines if "❌" in l]
    if ok and not failed:
        msg = "✅ Dependencies are installed."
    else:
        tail = "\n".join(failed[-4:]) or "\n".join(lines[-4:])
        msg = f"⚠️ Some dependencies had problems:\n```\n{tail.replace('`', chr(39))[:700]}\n```\nCheck the names/versions and try again."
    if mid:
        edit_message(chat_id, mid, msg, None)
    else:
        send_message(chat_id, msg)


def _edit_apply_manifest(chat_id, user_id, dep_id, content):
    d = _edit_load(user_id, dep_id)
    if not d:
        send_message(chat_id, "❌ Deployment not found")
        return
    if len(content.encode("utf-8", "ignore")) > EDIT_MAX_MANIFEST_BYTES:
        send_message(chat_id, "❌ That file is too large for a requirements list (max 1 MB).")
        return
    if d["is_node"]:
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("top level must be an object")
        except Exception as e:
            send_message(chat_id, f"❌ That isn't a valid `package.json`: {safe_md(str(e))[:150]}",
                         {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"edit_deploy_{dep_id}"}]]})
            return
        (d["folder"] / "package.json").write_text(content)
        stored = content[:2000]
    else:
        lines = [ln.rstrip() for ln in content.splitlines()]
        pkgs = [ln for ln in lines if ln.strip() and not ln.strip().startswith('#')]
        removed = [p for p in pkgs if is_stdlib_requirement(p)]
        kept = [ln for ln in lines if not (ln.strip() and not ln.strip().startswith('#') and is_stdlib_requirement(ln))]
        (d["folder"] / "requirements.txt").write_text("\n".join(kept).strip() + "\n")
        stored = "\n".join(kept)[:2000]
        if removed:
            send_message(chat_id, "ℹ️ Skipped (built into Python): " + ", ".join(f"`{r}`" for r in removed[:10]))
    db_execute("UPDATE deployments SET requirements = ? WHERE deployment_id = ? AND user_id = ?",
               (stored, dep_id, user_id))
    set_user_step(user_id, None)
    gh = _edit_push(dep_id, "requirements")          # the new file goes to GitHub immediately
    _edit_reinstall(chat_id, user_id, dep_id, d, "requirements")
    send_message(chat_id, f"✅ *Requirements updated.*\n{gh}\n\nRestart to apply.", _edit_after_kb(dep_id))


# ---------- replace the bot's code ----------
def show_edit_code(chat_id, message_id, user_id, dep_id):
    """Clicking Edit → Replace code goes straight to a FILE-UPLOAD prompt."""
    d = _edit_load(user_id, dep_id)
    if not d:
        _edit_reply(chat_id, message_id, "❌ Deployment not found", None)
        return
    ext = Path(d["file_name"]).suffix.lower()
    size = d["script"].stat().st_size if d["script"].exists() else 0
    set_user_step(user_id, 'edit_code', edit_dep=dep_id)
    _edit_reply(chat_id, message_id,
        f"*📄 REPLACE CODE — #{dep_id}*\n\nCurrent: `{display_filename(d['file_name'])}` ({format_file_size(size)})\n\n"
        f"📤 *Upload the new `{ext}` file now.* It replaces the old one (same name), goes through the same "
        f"6-layer security scan, and its imports are installed automatically. Your data files, database and "
        f"env vars are untouched.",
        {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"edit_deploy_{dep_id}"}]]})


def edit_code_prompt(chat_id, message_id, user_id, dep_id):
    show_edit_code(chat_id, message_id, user_id, dep_id)


def handle_edit_code_document(chat_id, user_id, doc, file_name, dep_id):
    d = _edit_load(user_id, dep_id)
    if not d:
        set_user_step(user_id, None)
        send_message(chat_id, "❌ Deployment not found")
        return
    want = Path(d["file_name"]).suffix.lower()
    if Path(file_name).suffix.lower() != want:
        send_message(chat_id, f"❌ This deployment runs a `{want}` file — please send a `{want}` file.\n"
                              f"To switch language, create a new deployment instead.",
                     {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"edit_deploy_{dep_id}"}]]})
        return
    raw = _download_telegram_file_bytes(doc.get("file_id"))
    if raw is None:
        send_message(chat_id, "❌ Could not download the file from Telegram.")
        return
    send_message(chat_id, "🔒 *Running 6-layer security scan…*")
    blocked, critical, warnings, report = run_security_scan(raw, file_name)
    _theft = data_theft_findings(critical)
    if _theft:
        set_user_step(user_id, None)
        reject_data_theft(chat_id, user_id, file_name, _theft)
        return
    if blocked:
        set_user_step(user_id, None)
        send_message(chat_id,
            f"🚫 *The new file was blocked by the security scan, so nothing was changed.*\n\n{report}\n\n"
            f"If you believe this is a false alarm, upload it as a new deployment — that route sends it to "
            f"an admin for review.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": f"edit_deploy_{dep_id}"}]]})
        return
    try:
        d["script"].write_bytes(raw)
    except Exception as e:
        send_message(chat_id, f"❌ Could not save the file: {e}")
        return
    db_execute("UPDATE deployments SET file_size = ? WHERE deployment_id = ? AND user_id = ?",
               (len(raw), dep_id, user_id))
    set_user_step(user_id, None)
    note = (f"\n⚠️ {len(warnings)} warning(s) noted." if warnings else "\n✅ Security scan passed.")
    gh = _edit_push(dep_id, "code")                  # the new code goes to GitHub immediately
    send_message(chat_id, f"✅ *Code replaced* ({format_file_size(len(raw))}).{note}\n{gh}")
    _edit_reinstall(chat_id, user_id, dep_id, d, "code")
    gh2 = _edit_push(dep_id, "code deps")            # picks up anything the install added (e.g. package.json)
    send_message(chat_id, "Restart to run the new code.", _edit_after_kb(dep_id))


def _download_telegram_file_bytes(file_id):
    try:
        info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
        if not info or not info.get("ok"):
            return None
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}"
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read()
    except Exception as e:
        print(f"⚠️ download failed: {e}")
        return None


# ==================== LIVE STATS / BACKUP / REMINDERS / ABUSE GUARD ====================
_CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100


def _proc_tree_pids(root_pid):
    """root_pid + every descendant, from /proc (no psutil needed)."""
    children = {}
    try:
        for name in os.listdir('/proc'):
            if not name.isdigit():
                continue
            try:
                with open(f'/proc/{name}/stat') as f:
                    data = f.read()
                ppid = int(data[data.rindex(')') + 2:].split()[1])
                children.setdefault(ppid, []).append(int(name))
            except Exception:
                continue
    except Exception:
        return [root_pid]
    out, stack = [], [int(root_pid)]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _tree_cpu_seconds(root_pid):
    """Cumulative CPU time (user+system, seconds) of the process tree."""
    total = 0.0
    for pid in _proc_tree_pids(root_pid):
        try:
            with open(f'/proc/{pid}/stat') as f:
                d = f.read()
            fields = d[d.rindex(')') + 2:].split()
            total += (int(fields[11]) + int(fields[12])) / _CLK_TCK      # utime + stime
        except Exception:
            pass
    return total


def _process_uptime_seconds(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            d = f.read()
        start = int(d[d.rindex(')') + 2:].split()[19]) / _CLK_TCK
        with open('/proc/uptime') as f:
            up = float(f.read().split()[0])
        return max(0, int(up - start))
    except Exception:
        return None


def get_deployment_live_stats(pid, sample=0.5):
    """{'ram_mb','cpu_pct','uptime_s','procs'} for a deployment's process tree, or None if it's gone.
    cpu_pct: 100 = one full core."""
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except Exception:
        return None
    c0 = _tree_cpu_seconds(pid)
    sleep(sample)
    c1 = _tree_cpu_seconds(pid)
    return {"ram_mb": get_process_tree_ram_mb(pid),
            "cpu_pct": max(0.0, (c1 - c0) / sample * 100),
            "uptime_s": _process_uptime_seconds(pid),
            "procs": len(_proc_tree_pids(pid))}


def _fmt_uptime(sec):
    if sec is None:
        return "—"
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d}d " if d else "") + (f"{h}h " if (d or h) else "") + f"{m}m"


def _live_stats_block(dep_id, pid, quota_mb=None):
    st = get_deployment_live_stats(pid)
    if not st:
        return ""
    restarts = (db_execute("SELECT crash_restart_count FROM deployments WHERE deployment_id = ?",
                           (dep_id,), fetch='one') or [0])[0] or 0
    ram = f"`{format_resource(st['ram_mb'])}`" + (f" / `{format_resource(quota_mb)}`" if quota_mb else "")
    return (f"\n\n*📈 LIVE STATS*\n"
            f"🧠 RAM: {ram}\n"
            f"⚙️ CPU: `{st['cpu_pct']:.0f}%` of 1 core\n"
            f"⏱️ Uptime: `{_fmt_uptime(st['uptime_s'])}`\n"
            f"🧵 Processes: `{st['procs']}`\n"
            f"🔁 Auto-restarts (last hour): `{restarts}`")


# ---------- backup zip ----------
_BACKUP_SKIP_DIRS  = {"packages", "node_modules", "venv", ".venv", "__pycache__", ".cache", ".npm",
                      ".git", "site-packages", ".home", ".local", ".config"}
_BACKUP_SKIP_FILES = {"run.py", "start.sh", "pid.txt", "output.log", "install.log"}
BACKUP_ZIP_MAX_MB  = 48        # Telegram's bot upload cap is 50 MB


def build_deployment_backup_zip(folder, zip_path):
    """Zip a deployment's own files (code, data, database, manifests, .env) — not installed packages or
    generated launchers. -> (file_count, total_bytes) of the zip."""
    import zipfile
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in _BACKUP_SKIP_DIRS]
            for fn in files:
                if fn in _BACKUP_SKIP_FILES or fn.endswith((".pyc", ".sock")):
                    continue
                full = Path(root) / fn
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    z.write(full, str(full.relative_to(folder)))
                    n += 1
                except Exception:
                    continue
    return n, Path(zip_path).stat().st_size


def send_deployment_backup(chat_id, user_id, dep_id):
    d = _edit_load(user_id, dep_id)
    if not d:
        send_message(chat_id, "❌ Deployment not found")
        return
    folder = d["folder"]
    if not folder.exists():
        send_message(chat_id, "❌ This deployment's files aren't on the server right now (restore it first by restarting it).")
        return
    tmp_dir = Path(tempfile.mkdtemp(prefix="bk_"))
    try:
        zp = tmp_dir / f"deployment_{dep_id}_backup.zip"
        n, size = build_deployment_backup_zip(folder, zp)
        if n == 0:
            send_message(chat_id, "ℹ️ Nothing to back up yet.")
        elif size > BACKUP_ZIP_MAX_MB * 1024 * 1024:
            send_message(chat_id, f"⚠️ The backup is {format_file_size(size)} — over Telegram's upload limit "
                                  f"({BACKUP_ZIP_MAX_MB} MB). Delete large data files and try again.")
        else:
            send_document(chat_id, str(zp),
                          f"💾 Backup of #{dep_id} — {n} file(s), {format_file_size(size)}\n"
                          f"Includes your code, data/database and .env. Installed packages are rebuilt automatically.",
                          zp.name)
    except Exception as e:
        send_message(chat_id, f"❌ Backup failed: {str(e)[:150]}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------- expiry reminders ----------
_REMINDER_STAGES_H = (72, 24, 1)        # hours before expiry (a 24h free bot only ever hits the 1h one)


def expiry_reminder_monitor():
    """Every 10 min: tell owners their bot is about to expire (72h / 24h / 1h before)."""
    sleep(120)
    print("✅ Expiry reminder monitor started")
    while True:
        try:
            check_expiry_reminders()
        except Exception as e:
            print(f"⚠️ expiry reminder error: {e}")
        sleep(600)


def check_expiry_reminders(now=None):
    now = now or datetime.now()
    rows = db_execute("""SELECT deployment_id, user_id, plan, is_free, expire_time FROM deployments
                         WHERE status = 'active' AND expire_time IS NOT NULL AND expire_time > ?
                           AND expire_time <= ?""",
                      (now.isoformat(), (now + timedelta(hours=max(_REMINDER_STAGES_H))).isoformat()),
                      fetch='all') or []
    sent = 0
    for dep_id, uid, plan, is_free, exp_s in rows:
        if is_banned(uid):
            continue
        try:
            left_h = (datetime.fromisoformat(exp_s) - now).total_seconds() / 3600
        except Exception:
            continue
        stages = [x for x in _REMINDER_STAGES_H if left_h <= x]
        if not stages:
            continue
        stage = min(stages)                       # the tightest window we're in
        if is_free and stage != 1:
            continue                              # free 24h bots: only the last-hour warning
        if db_execute("SELECT 1 FROM expiry_reminders WHERE deployment_id = ? AND expire_time = ? AND stage <= ?",
                      (dep_id, exp_s, stage), fetch='one'):
            continue                              # this (or a tighter) stage was already sent
        db_execute("INSERT OR IGNORE INTO expiry_reminders (deployment_id, expire_time, stage, sent_at) "
                   "VALUES (?,?,?,?)", (dep_id, exp_s, stage, now.isoformat()))
        if not notif_allows(uid, "expiry", stage):
            continue                              # the user opted out of this reminder (it stays marked as handled)
        left = (f"{int(left_h * 60)} minutes" if left_h < 1.5 else
                f"{left_h:.0f} hours" if left_h < 48 else f"{left_h / 24:.0f} days")
        rows_kb = [[{"text": "📈 Resource Plans (renew)", "callback_data": "resource_tiers"}],
                   [{"text": f"📦 Open Bot #{dep_id}", "callback_data": f"view_deploy_{dep_id}"}]]
        if not is_free:
            rows_kb.insert(0, [{"text": "💾 Download backup", "callback_data": f"backup_deploy_{dep_id}"}])
        try:
            send_message(uid,
                f"⏰ *Bot #{dep_id} expires in about {left}*\n\n"
                f"Plan: `{str(plan).upper()}`. When it expires the bot stops (your data is kept).\n"
                + ("Get a resource plan to keep your bots running without limits." if is_free
                   else "Renew with a resource plan so it keeps running, or download a backup."),
                {"inline_keyboard": rows_kb})
            sent += 1
        except Exception:
            pass
    return sent


# ---------- abuse guard (sustained CPU) ----------
ABUSE_CPU_PERCENT = float(os.environ.get("ABUSE_CPU_PERCENT", 90))     # of one core
ABUSE_WARN_MIN    = int(os.environ.get("ABUSE_WARN_MINUTES", 5))
ABUSE_STOP_MIN    = int(os.environ.get("ABUSE_STOP_MINUTES", 10))
_abuse_state = {}      # dep_id -> {"pid","cpu","t","minutes","warned"}


def abuse_guard_tick(now_ts=None, cpu_probe=None):
    """One pass of the guard. cpu_probe(pid)->cumulative CPU seconds (injectable for tests)."""
    now_ts = now_ts if now_ts is not None else time.time()
    cpu_probe = cpu_probe or _tree_cpu_seconds
    rows = db_execute("SELECT deployment_id, user_id, proc_pid FROM deployments "
                      "WHERE status = 'active' AND proc_pid IS NOT NULL", fetch='all') or []
    live = set()
    actions = []
    for dep_id, uid, pid in rows:
        live.add(dep_id)
        if is_admin(uid):
            continue
        cpu = cpu_probe(pid)
        st = _abuse_state.get(dep_id)
        if not st or st["pid"] != pid:
            _abuse_state[dep_id] = {"pid": pid, "cpu": cpu, "t": now_ts, "minutes": 0.0, "warned": False}
            continue
        dt = now_ts - st["t"]
        if dt < 20:
            continue
        pct = max(0.0, (cpu - st["cpu"]) / dt * 100)
        st["cpu"], st["t"] = cpu, now_ts
        if pct >= ABUSE_CPU_PERCENT:
            st["minutes"] += dt / 60
        else:
            st["minutes"] = max(0.0, st["minutes"] - dt / 60 * 2)      # cools down twice as fast
            if st["minutes"] == 0:
                st["warned"] = False
        if st["minutes"] >= ABUSE_STOP_MIN:
            actions.append(("stop", dep_id, uid))
            st["minutes"] = 0.0
            st["warned"] = False
        elif st["minutes"] >= ABUSE_WARN_MIN and not st["warned"]:
            st["warned"] = True
            actions.append(("warn", dep_id, uid))
    for gone in [d for d in _abuse_state if d not in live]:
        _abuse_state.pop(gone, None)
    for kind, dep_id, uid in actions:
        try:
            if kind == "warn":
                if not notif_allows(uid, "usage"):
                    continue
                send_message(uid,
                    f"⚠️ *Bot #{dep_id} is using too much CPU*\n\n"
                    f"It has been running at ~100% of a core for {ABUSE_WARN_MIN}+ minutes. "
                    f"If this continues for {ABUSE_STOP_MIN} minutes it will be stopped automatically.\n"
                    f"Check for infinite loops or busy-waiting (use `sleep` / async waits).",
                    {"inline_keyboard": [[{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{dep_id}"}]]})
            else:
                stop_deployment(dep_id)
                send_message(uid,
                    f"🛑 *Bot #{dep_id} was stopped*\n\n"
                    f"It held the CPU at ~100% for {ABUSE_STOP_MIN} minutes, which slows down every other bot on the host.\n"
                    f"Fix the busy loop and restart it.",
                    {"inline_keyboard": [[{"text": "🔄 Restart Bot", "callback_data": f"restart_deploy_{dep_id}"}],
                                         [{"text": "📄 View Logs",   "callback_data": f"view_runtime_logs_{dep_id}"}]]})
                for a in ADMIN_IDS:
                    try:
                        send_message(a, f"🛡️ Abuse guard stopped bot #{dep_id} (user `{uid}`): sustained CPU.")
                    except Exception:
                        pass
        except Exception as e:
            print(f"⚠️ abuse guard action failed: {e}")
    return actions


def abuse_guard_monitor():
    sleep(150)
    print(f"✅ Abuse guard started (stop after {ABUSE_STOP_MIN} min ≥ {ABUSE_CPU_PERCENT:.0f}% of a core)")
    while True:
        try:
            abuse_guard_tick()
        except Exception as e:
            print(f"⚠️ abuse guard error: {e}")
        sleep(60)


def view_deployment(chat_id, message_id, user_id, dep_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT file_name, file_size, plan, payment_method, cost_coins, cost_stars, start_time, expire_time, status, is_free, requirements, env_vars, error_log, is_paused, framework, proc_pid FROM deployments WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id))
    row = c.fetchone()
    conn.close()
    
    if not row:
        edit_message(chat_id, message_id, "❌ Not found", {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "my_deployments"}]]})
        return
    _proc_pid = row[15]
    row = row[:15]
    
    fname, fsize, plan, payment, cost_coins, cost_stars, start_str, expire_str, status, is_free, requirements, env_vars_json, error_log, is_paused, framework = row
    start_time = datetime.fromisoformat(start_str)
    is_lifetime = (expire_str is None)
    expire_time = datetime.fromisoformat(expire_str) if expire_str else None
    env_vars = json.loads(env_vars_json) if env_vars_json else {}
    
    if is_lifetime:
        cost_text      = "FREE (Lifetime)"
        remaining_text = "Never expires"
    elif is_free:
        remaining_secs = max(0, (expire_time - datetime.now()).total_seconds())
        remaining_h    = int(remaining_secs / 3600)
        remaining_m    = int((remaining_secs % 3600) / 60)
        cost_text      = "FREE"
        remaining_text = f"{remaining_h}h {remaining_m}m" if remaining_secs > 0 else "Expired"
    else:
        remaining_secs = max(0, (expire_time - datetime.now()).total_seconds())
        remaining_days = int(remaining_secs / 86400)
        if payment == "stars":
            cost_text = f"{cost_stars}⭐"
        elif payment == "coins":
            cost_text = f"{cost_coins}🪙"
        elif payment == "premium_free":
            cost_text = "FREE (Premium)"
        else:
            cost_text = f"{cost_coins}🪙"
        remaining_text = f"{remaining_days}d" if remaining_secs > 0 else "Expired"
    
    if status == "active" and not dep_files_ok(user_id, dep_id, fname):
        status_emoji = "⚠️ FILES MISSING (not running)"
    elif status == "active":
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
        keyboard["inline_keyboard"].append(
            [{"text": "🛑 Stop Bot",    "callback_data": f"stop_deploy_{dep_id}"},
             {"text": "🔄 Restart",     "callback_data": f"restart_deploy_{dep_id}"}])

    elif status == "paused":
        if is_free:
            keyboard["inline_keyboard"].append(
                [{"text": "🔄 Restart (Free 24h)", "callback_data": f"restart_deploy_{dep_id}"}])
        else:
            keyboard["inline_keyboard"].append(
                [{"text": "⭐ Reactivate Premium",   "callback_data": f"reactivate_premium_{dep_id}"},
                 {"text": "🆓 Continue Free (24h)", "callback_data": f"continue_as_free_{dep_id}"}])

    elif status in ("stopped", "failed"):
        if is_free:
            keyboard["inline_keyboard"].append(
                [{"text": "🔄 Restart Bot (Free 24h)", "callback_data": f"restart_deploy_{dep_id}"}])
        else:
            # Premium deployment stopped — offer both paths
            keyboard["inline_keyboard"].append(
                [{"text": "⭐ Reactivate Premium",   "callback_data": f"reactivate_premium_{dep_id}"}])
            keyboard["inline_keyboard"].append(
                [{"text": "🆓 Continue Free (24h)", "callback_data": f"continue_as_free_{dep_id}"}])
    
    keyboard["inline_keyboard"].append([{"text": "✏️ Edit (env · requirements · code)", "callback_data": f"edit_deploy_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🧰 Tools (logs · usage · schedule · share…)", "callback_data": f"fp_tools_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "💾 Download Backup (zip)", "callback_data": f"backup_deploy_{dep_id}"}])
    if status == "active":
        keyboard["inline_keyboard"].append([{"text": "🔄 Refresh stats", "callback_data": f"view_deploy_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "📋 View Install Logs", "callback_data": f"view_install_logs_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "📄 View Runtime Logs", "callback_data": f"view_runtime_logs_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🗑️ Delete Deployment", "callback_data": f"delete_deploy_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "my_deployments"}])
    keyboard["inline_keyboard"].append([{"text": "🏠 Menu", "callback_data": "main_menu"}])
    
    error_text = f"\n\n*❌ Error:*\n`{error_log[:500]}`" if error_log else ""
    
    if is_free and not is_user_premium(user_id) and not is_admin(user_id):
        used = get_free_deployment_used_count(user_id)
        remaining_slots = FREE_USER_MAX_DEPLOYMENTS - used
        free_status_text = f"\n\n*🆓 Free Slots Left:* `{remaining_slots}/{FREE_USER_MAX_DEPLOYMENTS}`"
    else:
        free_status_text = ""
    
    text = (
        f"*📄 DEPLOYMENT #{dep_id}*\n\n"
        f"📁 File: `{display_filename(fname)}` ({size_str})\n"
        f"🔧 Framework: `{framework}`\n"
        f"📋 Plan: `{plan.upper()}`\n"
        f"💰 Cost: {cost_text}\n"
        f"📅 Started: `{start_time.strftime('%Y-%m-%d %H:%M')}`\n"
        f"⏰ Expires: `{'Never (lifetime)' if is_lifetime else ('paused' if is_paused == _PAUSE_FROZEN else expire_time.strftime('%Y-%m-%d %H:%M'))}`\n"
        f"📊 Remaining: `{'frozen' if is_paused == _PAUSE_FROZEN else remaining_text}`\n"
        f"🔘 Status: {status_emoji}\n"
        f"📦 Requirements:\n`{reqs_text}`\n\n"
        f"🔧 Environment Variables: {len(env_vars)}\n{env_text}{error_text}{free_status_text}"
    )
    if status == "active" and _proc_pid:
        try:
            text += _live_stats_block(dep_id, _proc_pid, None if is_admin(user_id) else get_user_quota_mb(user_id))
        except Exception as _e:
            print(f"⚠️ live stats failed: {_e}")
    try:
        text += uptime_block(dep_id, start_str)
    except Exception as _e:
        print(f"⚠️ uptime block failed: {_e}")
    try:
        _pzb = pause_view_block(dep_id)
        if _pzb:
            text += _pzb
            keyboard = {"inline_keyboard": [[{"text": "▶️ Resume plan", "callback_data": f"p2_pzres_{dep_id}"}],
                                            [{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{dep_id}"}],
                                            [{"text": "🔙 My Deployments", "callback_data": "my_deployments"}]]}
        elif pause_check(dep_id, user_id)[0]:
            keyboard["inline_keyboard"].insert(max(0, len(keyboard["inline_keyboard"]) - 1),
                                               [{"text": "⏸ Pause plan", "callback_data": f"p2_pz_{dep_id}"}])
    except Exception as _e:
        print(f"⚠️ pause block failed: {_e}")
    keyboard = _fp_filter_shared_kb(keyboard, dep_id)
    edit_message(chat_id, message_id, text[:4096], keyboard)

# ========== HANDLERS ==========
def build_main_menu_text(user_id, first_name=None) -> str:
    """Default main-menu header: name, plan, resources, coins, projects."""
    row = db_execute("SELECT username, first_name, premium_plan, premium_expires "
                     "FROM users WHERE user_id = ?", (user_id,), fetch='one')
    username, db_first, premium_plan, premium_expires = row if row else (None, None, None, None)
    display = f"@{username}" if username else (first_name or db_first or "User")
    display = escape_md_v1(display)

    # Plan
    if is_admin(user_id):
        plan_line = "👑 ADMIN (unlimited)"
    elif is_user_premium(user_id):
        days_left = ""
        try:
            d = (datetime.fromisoformat(premium_expires) - datetime.now()).days
            days_left = f" · {max(0, d)}d left"
        except Exception:
            pass
        plan_line = f"⭐ PREMIUM ({(premium_plan or 'monthly').capitalize()}){days_left}"
        _t = get_user_tier(user_id)
        if _t:
            plan_line += f" · 📈 {RESOURCE_TIERS.get(_t[0], (0, format_resource(_t[1]), 0))[1]} tier"
    else:
        used_free = get_free_deployment_used_count(user_id)
        plan_line = f"🆓 FREE ({used_free}/{FREE_USER_MAX_DEPLOYMENTS} slots)"

    # Resources
    used_mb = get_user_resource_usage_mb(user_id)
    quota_mb = get_user_quota_mb(user_id)
    pct = min(100.0, (used_mb / quota_mb * 100) if quota_mb else 0)
    filled = int(pct / 100 * 10)
    bar = "█" * filled + "░" * (10 - filled)
    n_users = count_quota_users()
    _tier = None if is_admin(user_id) else get_user_tier(user_id)
    if is_admin(user_id):
        share_note = "whole pool"
    elif _tier:
        share_note = f"📈 {format_resource(_tier[1])} tier · until {_tier[2].strftime('%Y-%m-%d')}"
    else:
        share_note = (f"free share: {format_resource(max(0.0, get_resource_pool_mb() - tier_sold_mb()[0]))}"
                      f" ÷ {count_pool_sharers()} user(s)")

    # Coins & projects
    bal = get_user_balances(user_id)
    counts = {}
    for status, n in (db_execute("SELECT status, COUNT(*) FROM deployments "
                                 "WHERE user_id = ? GROUP BY status",
                                 (user_id,), fetch='all') or []):
        counts[status] = n
    total_proj = sum(counts.values())
    active = counts.get('active', 0)
    other = total_proj - active

    return (
        f"*🤖 BOT HOSTING*\n\n"
        f"👤 *{display}*\n"
        f"🎫 Plan: {plan_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💾 *Resources:* `{format_resource(used_mb)} / {format_resource(quota_mb)}`\n"
        f"`{bar}` {pct:.0f}%\n"
        f"_{share_note}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Coins: `{bal['coins']}`   ⭐ Stars: `{bal['stars']}`\n"
        f"📦 Projects: `{total_proj}` (🟢 `{active}` running · ⏸️ `{other}` stopped/paused)\n\n"
        f"Choose an option:"
    )


def handle_start(chat_id, user_id, username, first_name, start_param=""):
    is_new = False
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    is_new = c.fetchone() is None
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, join_date) VALUES (?, ?, ?, ?)",
              (user_id, username, first_name, datetime.now().isoformat()))
    c.execute("UPDATE users SET last_active = ?, username = ?, first_name = ? WHERE user_id = ?",
              (datetime.now().isoformat(), username or None, first_name, user_id))
    conn.commit()
    conn.close()

    # ── Referral processing ──────────────────────────────────────────
    if is_new and start_param and start_param.startswith("ref"):
        try:
            referrer_id = int(start_param[3:])  # "ref7713987088" → 7713987088
            credited = process_referral(referrer_id, user_id)
            if credited and not is_user_verified(user_id):
                send_message(referrer_id,
                    f"👋 *Someone joined with your referral link!*\n\n"
                    f"You'll earn *{REFERRAL_REWARD_COINS} 🪙* as soon as they verify their account.",
                    {"inline_keyboard": [[{"text": "👥 My Referrals", "callback_data": "my_referral"}]]})
        except Exception as _re:
            print(f"⚠️ Referral processing error: {_re}")
    # ─────────────────────────────────────────────────────────────────

    if is_new:
        async_backup(f"new_user_{user_id}")
    # ─────────────────────────────────────────────────────────────────

    update_system_stats()
    get_or_create_referral_code(user_id)   # eagerly create code so it's ready

    if is_user_verified(user_id):
        grant_referral_reward(user_id)     # no channel gate (or already verified) → pay right away
        if not has_accepted_tos(user_id):
            show_tos_prompt(chat_id, user_id)
            return

        welcome = build_main_menu_text(user_id, first_name)
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
    
    keyboard = {"inline_keyboard": [
        [{"text": "📈 Resource Plans", "callback_data": "resource_tiers"}],
        [{"text": "🔙 Back", "callback_data": "main_menu"}]]}
    tier = get_user_tier(user_id)
    if is_admin(user_id):
        plan_line = "👑 Admin — whole resource pool"
    elif tier:
        t_label = RESOURCE_TIERS.get(tier[0], (0, format_resource(tier[1]), 0))[1]
        plan_line = f"⭐ *{t_label}* until `{tier[2].strftime('%Y-%m-%d')}`"
    else:
        plan_line = f"🆓 Shared pool (`{format_resource(get_user_quota_mb(user_id))}`)"
    premium_badge = "⭐ PREMIUM ⭐" if is_premium else "🆓 FREE"
    price_lines = "\n".join(
        f"• {label}: `{stars}⭐`/mo · `{tier_price_stars(key, 'y')}⭐`/yr"
        for key, (mb, label, stars) in RESOURCE_TIERS.items())

    text = (
        f"*💰 YOUR BALANCE*\n\n"
        f"🪙 Coins: `{balances['coins']}`\n"
        f"⭐ Stars: `{balances['stars']}`\n"
        f"🎫 Status: {premium_badge}\n"
        f"📈 Resource plan: {plan_line}\n"
        f"🆓 Free Slots: `{free_remaining}/{FREE_USER_MAX_DEPLOYMENTS}`\n"
        f"🪙 Earned: `{row[0] if row else 0}`\n"
        f"🪙 Spent: `{row[1] if row else 0}`\n"
        f"⭐ Earned: `{row[2] if row else 0}`\n"
        f"⭐ Spent: `{row[3] if row else 0}`\n\n"
        f"*Resource Plans (include Premium):*\n{price_lines}"
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
        f"*🎫 REDEEM CODE*\n\nSend your code:",
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
                f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited free deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        else:
            send_message(chat_id,
                f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited free deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        return
    
    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        _qt = quota_exceeded_text(_q_used, _q_total)
        _qkb = {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]}
        if message_id:
            edit_message(chat_id, message_id, _qt, _qkb)
        else:
            send_message(chat_id, _qt, _qkb)
        return

    set_user_step(user_id, 'awaiting_file', plan='free', duration=FREE_DEPLOYMENT_DURATION_HOURS,
                  cost_coins=0, cost_stars=0, payment_method='none')
    
    text = (
        f"*🆓 FREE DEPLOYMENT*\n\n"
        f"⏱️ Duration: `{FREE_DEPLOYMENT_DURATION_HOURS}` hours\n"
        f"💰 Cost: FREE\n"
        f"📊 Free slots left: {reason}\n"
        f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
        f"📤 *Send your Python file (.py)*\n\n"
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

    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return
    
    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        edit_message(chat_id, message_id, quota_exceeded_text(_q_used, _q_total),
                     {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        return

    set_user_step(user_id, 'awaiting_file', plan=plan, duration=duration,
                  cost_coins=cost_coins, cost_stars=cost_stars, payment_method=None)
    
    if plan == "lifetime":
        text = (
            f"*♾️ LIFETIME DEPLOYMENT (ADMIN)*\n\n"
            f"⏱️ Duration: `Never expires`\n"
            f"💰 Cost: FREE\n\n"
            f"📤 *Send your file (.py, .js, etc.)*\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt / package.json (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"*Note:* All environment variables will be available via os.environ.get('KEY')"
        )
    elif (not _premium_covers_plan_core(user_id, plan)) and get_deploy_credit(user_id, plan):
        _cr = get_deploy_credit(user_id, plan)
        text = (
            f"*💳 SAVED CREDIT*\n\n"
            f"You deleted a paid {plan} bot, so its unused time is kept: *{_cr[1]//86400}d {(_cr[1]//3600)%24}h*.\n"
            f"This deployment is *FREE* and runs for that remaining time.\n\n"
            f"📤 *Send your file (.py, .js, etc.)*\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt / package.json (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)"
        )
    elif premium_covers_plan(user_id, plan):
        text = (
            f"*✨ PREMIUM BENEFIT!*\n\n"
            f"Your {plan.upper()} deployment is *FREE* as a premium member!\n\n"
            f"📤 *Send your Python file (.py)*\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt file (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"*Note:* All environment variables will be available via os.environ.get('KEY')"
        )
    else:
        text = (
            f"*💰 {plan.capitalize()} DEPLOYMENT*\n\n"
            f"⏱️ Duration: `{duration}` days\n"
            f"⭐ Cost: `{cost_stars}⭐`\n"
            f"🪙 Cost: `{cost_coins}🪙`\n\n"
            f"📤 *Send your Python file (.py)*\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt file (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"*Note:* All environment variables will be available via os.environ.get('KEY')"
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
            edit_message(chat_id, message_id, "📭 *No Deployments*", keyboard)
        else:
            send_message(chat_id, "📭 *No Deployments*", keyboard)
        return
    
    if not is_user_premium(user_id) and not is_admin(user_id):
        used = get_free_deployment_used_count(user_id)
        remaining = FREE_USER_MAX_DEPLOYMENTS - used
        header = f"📦 *Your Deployments*\n\n🆓 Free slots: `{remaining}/{FREE_USER_MAX_DEPLOYMENTS}`\n\n"
    else:
        header = "📦 *Your Deployments*\n\n"
    
    keyboard = {"inline_keyboard": []}
    _any_missing = False
    for dep_id, fname, fsize, plan, exp_str, status, is_free, payment, is_paused, framework in rows:
        if exp_str is None:
            remaining_text = "♾️"
        else:
            exp_time = datetime.fromisoformat(exp_str)
            if is_free:
                remaining = (exp_time - datetime.now()).total_seconds() / 3600
                remaining_text = f"{int(remaining)}h"
            else:
                remaining = (exp_time - datetime.now()).days
                remaining_text = f"{remaining}d"
        
        if status == "active" and not dep_files_ok(user_id, dep_id, fname):
            status_icon = "⚠️"
            _any_missing = True
        elif status == "active":
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
        clean_fname = display_filename(fname)
        keyboard["inline_keyboard"].append([{"text": f"{icon}{status_icon}{fw_icon} ID:{dep_id} - {clean_fname[:20]} ({size_str}) [{remaining_text}]", 
                         "callback_data": f"view_deploy_{dep_id}"}])
    
    if _any_missing:
        header += "⚠️ = files missing on the server (not running). Press the bot, then Restart to bring it back.\n\n"
    keyboard["inline_keyboard"].append([{"text": "🔄 Restart all", "callback_data": "p2_rall"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "main_menu"}])
    
    if message_id:
        edit_message(chat_id, message_id, header, keyboard)
    else:
        send_message(chat_id, header, keyboard)

# ==================== MENU FUNCTIONS ====================
def get_main_menu(user_id):
    balances  = get_user_balances(user_id)
    is_verified = is_user_verified(user_id)
    is_premium  = is_user_premium(user_id)
    verified_badge = "✅" if is_verified else "🔐"
    premium_badge  = "⭐" if is_premium  else "🆓"

    # Count open bug reports for admin badge
    open_reports = 0
    if is_admin(user_id):
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            open_reports = conn.execute(
                "SELECT COUNT(*) FROM bug_reports WHERE status='open'").fetchone()[0]
            conn.close()
        except Exception:
            pass

    keyboard = {
        "inline_keyboard": [
            [{"text": f"{verified_badge} Join Channel",         "callback_data": "check_verification"}],
            [{"text": "📤 Deploy New Bot",                       "callback_data": "deploy_new"}],
            [{"text": "🐙 Deploy from GitHub",                   "callback_data": "github_deploy"}],
            [{"text": f"{premium_badge} Free Deployment (24h)", "callback_data": "free_deployment"}],
            [{"text": "📦 My Deployments",                      "callback_data": "my_deployments"},
             {"text": "🧰 More",                                 "callback_data": "fp_more"}],
            [{"text": f"💰 {balances['coins']}🪙 | {balances['stars']}⭐", "callback_data": "my_balance"}],
            [{"text": "🎫 Redeem Code",                         "callback_data": "redeem_code"},
             {"text": "👥 Referral",                             "callback_data": "my_referral"}],
            [{"text": "⭐ Premium Subscription",                "callback_data": "subscribe_premium"},
             {"text": "📈 Resource Plans",                      "callback_data": "resource_tiers"}],
            [{"text": "🎫 Support",                            "callback_data": "p2_tk"}],
        ]
    }
    _dash = _p2_dash_button()
    if _dash:
        keyboard["inline_keyboard"].insert(len(keyboard["inline_keyboard"]) - 1, [_dash])

    if is_admin(user_id):
        badge = f" ({open_reports} open)" if open_reports else ""
        keyboard["inline_keyboard"].append(
            [{"text": f"🔧 Admin Panel{badge}", "callback_data": "admin_panel"}])

    return keyboard

def get_deploy_menu(user_id):
    is_premium = is_user_premium(user_id)
    covers_monthly = premium_covers_plan(user_id, "monthly")
    covers_yearly  = premium_covers_plan(user_id, "yearly")
    _cm = (not _premium_covers_plan_core(user_id, "monthly")) and get_deploy_credit(user_id, "monthly")
    _cy = (not _premium_covers_plan_core(user_id, "yearly")) and get_deploy_credit(user_id, "yearly")

    if covers_monthly or covers_yearly:
        monthly_label = (f"📅 Monthly - FREE (saved credit {_cm[1]//86400}d)" if _cm else
                         "📅 Monthly (30 days) - FREE for Premium" if covers_monthly
                         else f"📅 Monthly (30 days) - {PRICE_MONTHLY_STARS}⭐ / {PRICE_MONTHLY_COINS}🪙")
        yearly_label  = (f"🌟 Yearly - FREE (saved credit {_cy[1]//86400}d)" if _cy else
                         "🌟 Yearly (365 days) - FREE for Premium" if covers_yearly
                         else f"🌟 Yearly (365 days) - {PRICE_YEARLY_STARS}⭐ / {PRICE_YEARLY_COINS}🪙")
        rows = [
            [{"text": monthly_label, "callback_data": "plan_monthly"}],
            [{"text": yearly_label,  "callback_data": "plan_yearly"}],
            [{"text": "🆓 Free Deployment (24h)", "callback_data": "free_deployment"}],
        ]
        if not covers_yearly:
            rows.append([{"text": "⭐ Upgrade to Yearly Premium", "callback_data": "subscribe_premium"}])
        rows.append([{"text": "🔙 Back to Menu", "callback_data": "main_menu"}])
        keyboard = {"inline_keyboard": rows}
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

    # ── Lifetime deployment: admins only ────────────────────────────
    if is_admin(user_id):
        keyboard["inline_keyboard"].insert(
            -1, [{"text": "♾️ Lifetime (never expires) — ADMIN", "callback_data": "plan_lifetime"}])

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
def scan_orphaned_processes():
    """
    Find processes that clearly belong to this hosting platform (their
    command line references a path under DEPLOYMENTS_DIR) but aren't the
    currently-tracked proc_pid for any active deployment. These are almost
    always leftovers from before process-group isolation was added — a
    stopped/restarted/redeployed bot's actual process, orphaned because
    only its launcher's single PID was ever killed, not the whole tree.
    Deliberately conservative: only matches on the DEPLOYMENTS_DIR path
    appearing in the process's own command line, never touches anything
    else, and always excludes this very process.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT proc_pid FROM deployments WHERE proc_pid IS NOT NULL")
    tracked_pids = {row[0] for row in c.fetchall()}
    conn.close()

    own_pid = os.getpid()
    orphans = []
    deployments_dir_str = str(DEPLOYMENTS_DIR)

    try:
        pid_dirs = [d for d in os.listdir('/proc') if d.isdigit()]
    except Exception:
        return []

    for pid_str in pid_dirs:
        pid = int(pid_str)
        if pid in tracked_pids or pid == own_pid:
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().replace(b'\x00', b' ').decode(errors='ignore').strip()
        except Exception:
            continue
        if deployments_dir_str not in cmdline:
            continue

        ram_mb = 0
        try:
            with open(f'/proc/{pid}/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        ram_mb = int(line.split()[1]) / 1024
                        break
        except Exception:
            pass

        orphans.append({'pid': pid, 'cmdline': cmdline[:120], 'ram_mb': ram_mb})

    return orphans


def admin_scan_orphans_view(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    edit_message(chat_id, message_id, "🧹 Scanning for orphaned processes...")

    orphans = scan_orphaned_processes()

    if not orphans:
        edit_message(chat_id, message_id,
            "✅ No orphaned processes found — nothing to clean up.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_resources"}]]})
        return

    total_ram = sum(o['ram_mb'] for o in orphans)
    text = (f"🧹 *Found {len(orphans)} orphaned process(es)* using "
            f"~`{total_ram:.0f}MB` RAM combined:\n\n")
    for o in orphans[:15]:
        text += f"• PID `{o['pid']}` — `{o['ram_mb']:.0f}MB`\n  `{o['cmdline'][:70]}`\n"
    if len(orphans) > 15:
        text += f"\n… and {len(orphans) - 15} more"
    text += ("\n\nThese are process trees left behind by stop/restart/redeploy actions "
             "from before process-group isolation was in place. Safe to kill — they're "
             "not attached to any currently-tracked deployment.")

    edit_message(chat_id, message_id, text,
        {"inline_keyboard": [
            [{"text": f"🗑️ Kill All {len(orphans)} Orphans", "callback_data": "admin_kill_orphans"}],
            [{"text": "🔙 Back", "callback_data": "admin_resources"}]]})


def admin_kill_orphans_action(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    orphans = scan_orphaned_processes()
    killed = 0
    for o in orphans:
        try:
            kill_deployment_process(o['pid'], signal.SIGKILL)
            killed += 1
        except Exception:
            pass

    edit_message(chat_id, message_id,
        f"✅ Killed {killed}/{len(orphans)} orphaned process(es).\n\n"
        f"Give it a few seconds, then check *📊 Resource Usage* again.",
        {"inline_keyboard": [[{"text": "📊 Resource Usage", "callback_data": "admin_resources"}]]})


def admin_resources_view(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return

    edit_message(chat_id, message_id, "📊 Measuring resource usage...")

    sys_res = get_system_resources(sample_seconds=0.5)

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deployment_id, user_id, file_name, proc_pid FROM deployments "
              "WHERE status = 'active' AND proc_pid IS NOT NULL")
    active = c.fetchall()
    conn.close()

    per_deploy = []
    for dep_id, owner_id, fname, pid in active:
        usage = get_deployment_resource_usage(pid)
        if usage:
            per_deploy.append((dep_id, owner_id, fname, usage['ram_mb'], usage['cpu_percent']))

    # Heaviest RAM users first — the ones most likely to matter if something's
    # starving other bots for resources.
    per_deploy.sort(key=lambda x: x[3], reverse=True)

    bar_len = 20
    def _bar(pct):
        filled = int(pct / 100 * bar_len)
        return '█' * filled + '░' * (bar_len - filled)

    text = (
        f"*📊 SYSTEM RESOURCE USAGE*\n\n"
        f"🖥️ CPU: `{sys_res['cpu_percent']:.1f}%` ({sys_res['cpu_count']} core(s))\n"
        f"`{_bar(sys_res['cpu_percent'])}`\n\n"
        f"💾 RAM: `{sys_res['ram_used_mb']:.0f} / {sys_res['ram_total_mb']:.0f} MB` "
        f"(`{sys_res['ram_percent']:.1f}%`)\n"
        f"`{_bar(sys_res['ram_percent'])}`\n\n"
        f"⚖️ Load average: `{sys_res['load_avg'][0]:.2f}, {sys_res['load_avg'][1]:.2f}, "
        f"{sys_res['load_avg'][2]:.2f}`\n"
        f"🤖 Active deployments: `{len(active)}`\n"
    )

    if per_deploy:
        text += f"\n*Top RAM usage by deployment:*\n"
        for dep_id, owner_id, fname, ram_mb, cpu_pct in per_deploy[:10]:
            cpu_str = f"{cpu_pct:.1f}%" if cpu_pct is not None else "n/a"
            text += f"• #{dep_id} `{display_filename(fname)[:25]}` — `{ram_mb:.0f}MB` / CPU `{cpu_str}`\n"

    if sys_res['source'] == 'proc':
        text += f"\n_ℹ️ Per-deployment CPU% unavailable without psutil (RAM still accurate)_"

    edit_message(chat_id, message_id, text,
        {"inline_keyboard": [
            [{"text": "🧹 Scan for Orphaned Processes", "callback_data": "admin_scan_orphans"}],
            [{"text": "🔄 Refresh", "callback_data": "admin_resources"}],
            [{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})


def admin_database_menu(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    try:
        size_kb = DATABASE_FILE.stat().st_size / 1024 if DATABASE_FILE.exists() else 0
    except Exception:
        size_kb = 0
    gh_status = "✅ Configured" if GITHUB_ENABLED else "❌ Not configured"
    last_backup = (f"{int(datetime.now().timestamp() - _last_backup_time)}s ago"
                   if _last_backup_time else "never (this run)")
    text = (
        f"*💾 DATABASE BACKUP*\n\n"
        f"📦 Current size: `{size_kb:.1f} KB`\n"
        f"☁️ GitHub backup: {gh_status}\n"
        f"🗂️ User files repo: {files_repo_status_line()}\n"
        f"🕐 Last GitHub push: `{last_backup}`\n\n"
        f"• *Backup Now* — pushes the database AND every user's deployment files "
        f"(code, requirements/package.json, .env, bot .db files) to GitHub immediately.\n"
        f"• *Download* — sends you the raw `.db` file here in chat.\n"
        f"• *Restore* — replaces the live database with a `.db` file you "
        f"upload. A safety copy of the current database is made first."
    )
    keyboard = {"inline_keyboard": [
        [{"text": "🔄 Backup to GitHub Now", "callback_data": "admin_db_backup_now"}],
        [{"text": "🧹 Clean Orphaned Files", "callback_data": "admin_purge_orphans"}],
        [{"text": "⬇️ Download DB File",      "callback_data": "admin_db_download"}],
        [{"text": "⬆️ Restore from File",     "callback_data": "admin_db_restore_start"}],
        [{"text": "🔙 Back to Admin Panel",   "callback_data": "admin_panel"}],
    ]}
    edit_message(chat_id, message_id, text, keyboard)


def admin_purge_orphans(chat_id, message_id, user_id, force=False):
    if not is_admin(user_id):
        return
    back_kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]}
    if not FILES_REPO_ENABLED:
        edit_message(chat_id, message_id, "❌ `GITHUB_FILES_REPO_NAME` isn't configured.", back_kb)
        return
    edit_message(chat_id, message_id, "🧹 Checking the files repo for orphaned deployments…")
    r = files_repo_purge_orphans(force=force)
    names = ", ".join(f"{u}/{d}" for u, d in r["orphans"][:15]) + (" …" if len(r["orphans"]) > 15 else "")
    if r["error"]:
        text = f"❌ Cleanup failed: {safe_md(r['error'])}"
    elif r["blocked"]:
        text = (f"⚠️ *Cleanup paused*\n\n{safe_md(r['blocked'])}.\n\nOrphans found: `{len(r['orphans'])}` of "
                f"`{r['checked']}`\n`{names}`\n\nIf you're sure the database is correct, delete them anyway.")
        back_kb = {"inline_keyboard": [[{"text": "🗑️ Delete them anyway", "callback_data": "admin_purge_orphans_force"}],
                                       [{"text": "🔙 Back", "callback_data": "admin_database"}]]}
    elif r["deleted"]:
        text = (f"✅ Removed `{r['deleted']}` orphaned deployment(s) from the files repo.\n`{names}`\n"
                f"Checked `{r['checked']}` stored deployment(s).")
    else:
        text = f"✅ Nothing to clean — all `{r['checked']}` stored deployment(s) exist in the database."
    edit_message(chat_id, message_id, text, back_kb)


def admin_db_backup_now(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    back_kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]}
    if not GITHUB_ENABLED and not FILES_REPO_ENABLED:
        edit_message(chat_id, message_id,
            "❌ GitHub backup isn't configured.\n\n"
            "Set `GITHUB_TOKEN`, `GITHUB_REPO_OWNER`, `GITHUB_REPO_NAME` (database) and "
            "`GITHUB_FILES_REPO_NAME` (user files) to enable it.", back_kb)
        return

    # 1) the platform database
    db_line = "⏭️ Database: GitHub DB backup not configured"
    if GITHUB_ENABLED:
        edit_message(chat_id, message_id, "🔄 Backing up the database to GitHub…")
        db_ok = github_backup_db(reason=f"manual_admin_{user_id}", force=True)
        db_line = "✅ Database pushed" if db_ok else "❌ Database backup failed (see server logs)"

    # 2) every user's deployment files
    files_line = "⏭️ User files: `GITHUB_FILES_REPO_NAME` not configured"
    if FILES_REPO_ENABLED:
        _pr = files_repo_purge_orphans()
        if _pr["deleted"]:
            db_line += f"\n🧹 Removed `{_pr['deleted']}` orphaned deployment(s) from the repo"
        ids = [r[0] for r in db_execute("SELECT deployment_id FROM deployments ORDER BY deployment_id",
                                        fetch='all') or []]
        last_edit = [0.0]

        def _progress(i, total, s):
            now = datetime.now().timestamp()
            if now - last_edit[0] >= 3 or i == total:      # Telegram edit rate-limit friendly
                last_edit[0] = now
                edit_message(chat_id, message_id,
                    f"🔄 Backing up user files…\n\n{db_line}\n📦 Deployments: `{i}/{total}`")

        s = files_repo_backup(ids, f"manual_admin_{user_id}", progress=_progress, refresh=True) if ids else \
            {"total": 0, "saved": 0, "unchanged": 0, "failed": 0, "no_files": 0, "oversized": 0, "error": None}
        if s["error"]:
            files_line = f"❌ User files: {s['error']}"
        else:
            files_line = (f"{'✅' if not s['failed'] else '⚠️'} User files: `{s['total']}` deployment(s) — "
                          f"`{s['saved']}` updated, `{s['unchanged']}` already up to date"
                          + (f", `{s['no_files']}` without files on disk" if s['no_files'] else "")
                          + (f", `{s['failed']}` FAILED" if s['failed'] else "")
                          + (f"\n   ⚠️ `{s['oversized']}` file(s) over the size caps were not saved" if s['oversized'] else ""))

    edit_message(chat_id, message_id, f"*Backup finished*\n\n{db_line}\n{files_line}", back_kb)


def admin_db_download(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    if not DATABASE_FILE.exists():
        edit_message(chat_id, message_id, "❌ No database file found.",
                     {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
        return

    size_mb = DATABASE_FILE.stat().st_size / (1024 * 1024)
    if size_mb > 50:
        # Telegram's bot API hard-caps sendDocument at 50MB regardless of
        # this platform's own MAX_FILE_SIZE_MB setting — without this check
        # a too-large DB just fails silently with a generic error and no
        # way to tell what actually went wrong.
        edit_message(chat_id, message_id,
            f"❌ *Database is too large to send via Telegram* ({size_mb:.1f} MB).\n\n"
            f"Telegram's bot API caps file uploads at 50 MB. Use "
            f"*🔄 Backup to GitHub Now* instead, or download it directly "
            f"from the server's disk.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
        return

    edit_message(chat_id, message_id, "⬇️ Preparing your database file...")
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    ts_display = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  # no underscores — safe for Markdown captions
    result = send_document(chat_id, DATABASE_FILE,
                            caption=f"💾 Database backup — {ts_display}",
                            filename=f"hosting_bot_backup_{ts}.db")
    if result and result.get('ok'):
        edit_message(chat_id, message_id, "✅ Database file sent above.",
                     {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
    else:
        err_detail = ""
        if isinstance(result, dict):
            err_detail = f"\n\nTelegram said: `{result.get('description', 'unknown error')}`"
        edit_message(chat_id, message_id,
            f"❌ Failed to send the database file ({size_mb:.1f} MB).{err_detail}\n\n"
            f"Check the server console for a `send_document` error line.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})


def admin_db_restore_start(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    set_user_step(user_id, 'awaiting_db_restore')
    edit_message(chat_id, message_id,
        "*⬆️ RESTORE DATABASE*\n\n"
        "Send the `.db` file to restore.\n\n"
        "⚠️ This replaces ALL current data (users, deployments, coins, "
        "everything) with the contents of the file you send. A safety copy "
        "of the current database is made automatically before the swap.\n\n"
        "Send the file now, or cancel below.",
        {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_database"}]]})


def handle_db_restore_upload(message, user_id, chat_id):
    """Admin uploaded a .db file in response to admin_db_restore_start."""
    if not is_admin(user_id):
        return

    doc = message['document']
    file_size = doc.get('file_size', 0)
    if file_size > 200 * 1024 * 1024:  # sanity cap — a real DB shouldn't be this big
        send_message(chat_id, "❌ File too large to be a plausible database restore (>200MB).")
        return

    file_id = doc['file_id']
    file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
    if not (file_info and file_info.get('ok')):
        send_message(chat_id, "❌ Could not download the file from Telegram.")
        return

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
    try:
        with urllib.request.urlopen(file_url, timeout=60) as resp:
            new_db_bytes = resp.read()
    except Exception as e:
        send_message(chat_id, f"❌ Download failed: {e}")
        return

    # Validate it's actually a SQLite database before touching anything live.
    if not new_db_bytes.startswith(b"SQLite format 3\x00"):
        send_message(chat_id,
            "❌ That file doesn't look like a valid SQLite database (missing "
            "the SQLite file header). Restore aborted — nothing was changed.")
        return
    if len(new_db_bytes) < 1024:
        send_message(chat_id, "❌ That file is suspiciously small to be a real database. Restore aborted.")
        return

    set_user_step(user_id, None)

    # Safety net: snapshot the CURRENT database before overwriting it — both
    # locally and (if configured) to GitHub — so a bad restore is recoverable
    # and no in-flight information gets lost without a way back.
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    safety_path = DATABASE_FILE.parent / f"pre_restore_backup_{ts}.db"
    try:
        if DATABASE_FILE.exists():
            shutil.copy2(DATABASE_FILE, safety_path)
    except Exception as e:
        send_message(chat_id, f"⚠️ Could not create a local safety copy ({e}) — continuing anyway.")

    if GITHUB_ENABLED:
        github_backup_db(reason=f"pre_restore_safety_{user_id}", force=True)

    try:
        DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DATABASE_FILE, "wb") as f:
            f.write(new_db_bytes)
    except Exception as e:
        send_message(chat_id, f"❌ Restore failed while writing the file: {e}")
        return

    # Re-run migrations in case the uploaded DB predates newer columns.
    try:
        init_db()
    except Exception as e:
        send_message(chat_id, f"⚠️ Database restored, but schema migration hit an error: {e}")

    try:
        conn = sqlite3.connect(DATABASE_FILE)
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        deploys_count = conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
        conn.close()
        send_message(chat_id,
            f"✅ *Database restored successfully!*\n\n"
            f"👥 Users: `{users_count}`\n"
            f"📦 Deployments: `{deploys_count}`\n\n"
            f"A safety copy of the previous database was kept as "
            f"`{safety_path.name}` on disk"
            + (" and pushed to GitHub." if GITHUB_ENABLED else "."),
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
    except Exception as e:
        send_message(chat_id,
            f"⚠️ Database file was replaced, but it doesn't look like a valid "
            f"hosting-bot database (schema check failed: {e}). "
            f"Your previous database is saved at `{safety_path.name}`.",
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})


def show_admin_panel(chat_id, message_id):
    stats = get_system_stats()

    # Bug report counts
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        open_bugs    = conn.execute("SELECT COUNT(*) FROM bug_reports WHERE status='open'").fetchone()[0]
        total_bugs   = conn.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0]
        total_refs   = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        top_referrer = conn.execute(
            "SELECT referrer_id, COUNT(*) c FROM referrals GROUP BY referrer_id ORDER BY c DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception:
        open_bugs = total_bugs = total_refs = 0
        top_referrer = None

    stats_text = (
        f"*📊 System Stats*\n\n"
        f"👥 Users: `{stats.get('total_users', 0)}`\n"
        f"📦 Deployments: `{stats.get('total_deployments', 0)}`\n"
        f"🟢 Active: `{stats.get('active_deployments', 0)}`\n"
        f"⏸️ Paused: `{stats.get('paused_deployments', 0)}`\n"
        f"🆓 Free: `{stats.get('free_deployments', 0)}`\n"
        f"⭐ Premium: `{stats.get('premium_users', 0)}`\n"
        f"💰 Revenue: `${stats.get('revenue_usd', 0):.2f}`\n"
        f"🪙 Coins Created: `{stats.get('coins_created', 0)}`\n"
        f"👥 Referrals: `{total_refs}` total\n"
        + (f"🏆 Top Referrer: `{top_referrer[0]}` ({top_referrer[1]} refs)\n" if top_referrer else "")
        + f"\n━━━━━━━━━━━━━━━━━━━━━━"
    )

    open_badge = f" ({open_bugs})" if open_bugs else ""

    # Count pending security reviews
    sec_pending = 0
    try:
        conn_sp = sqlite3.connect(DATABASE_FILE, timeout=5)
        sec_pending = conn_sp.execute(
            "SELECT COUNT(*) FROM security_reviews WHERE status='pending'").fetchone()[0]
        conn_sp.close()
    except Exception:
        pass
    sec_badge = f" ({sec_pending})" if sec_pending else ""

    keyboard = {
        "inline_keyboard": [
            [{"text": f"🔒 Security Reviews{sec_badge}", "callback_data": "admin_sec_reviews"}],
            [{"text": "📊 Dashboard",                    "callback_data": "admin_dashboard"}],
            *_fp_admin_rows(),
            [{"text": "👥 Referral Stats",               "callback_data": "admin_referral_stats"},
             {"text": "📢 Broadcast",                    "callback_data": "admin_broadcast"}],
            [{"text": "🎫 Create Redeem Code",           "callback_data": "admin_create_code"}],
            [{"text": "🪙 Add Coins",                    "callback_data": "admin_add_coins"}],
            [{"text": "📋 List Redeem Codes",            "callback_data": "admin_list_codes"}],
            [{"text": "👥 List Users",                   "callback_data": "admin_list_users"},
             {"text": "🚫 Banned Users",                 "callback_data": "admin_banned_list"}],
            [{"text": "📊 View Subscriptions",           "callback_data": "admin_subscriptions"}],
            [{"text": "💾 Database Backup",              "callback_data": "admin_database"}],
            [{"text": "📊 Resource Usage",               "callback_data": "admin_resources"}],
            [{"text": "🔙 Back to Main Menu",            "callback_data": "main_menu"}]
        ]
    }

    edit_message(chat_id, message_id, f"*🔧 ADMIN PANEL*\n\n{stats_text}", keyboard)

CODES_PER_PAGE = 8

def _code_state(expires, active, used, max_uses):
    """(emoji, word) describing why/if a redeem code can still be used."""
    try:
        if expires and datetime.fromisoformat(expires) < datetime.now():
            return "⌛", "expired"
    except Exception:
        pass
    if not active:
        return "❌", "inactive"
    if max_uses and max_uses > 0 and used >= max_uses:
        return "🔒", "used up"
    return "✅", "active"


def admin_list_codes(chat_id, message_id, page=0):
    total = (db_execute("SELECT COUNT(*) FROM redeem_codes", fetch='one') or [0])[0] or 0
    pages = max(1, -(-total // CODES_PER_PAGE))
    page = max(0, min(int(page), pages - 1))
    rows = db_execute("SELECT code_id, code, coins_amount, stars_amount, used_count, max_uses, expires_at, "
                      "is_active, created_at FROM redeem_codes ORDER BY created_at DESC LIMIT ? OFFSET ?",
                      (CODES_PER_PAGE, page * CODES_PER_PAGE), fetch='all') or []
    buttons = []
    if not rows:
        text = "📭 No redeem codes found."
    else:
        text = f"*🎫 REDEEM CODES*  —  page {page + 1}/{pages}  (total `{total}`)\n\n"
        for code_id, code, coins, stars, used, max_uses, expires, active, created in rows:
            emoji, word = _code_state(expires, active, used or 0, max_uses)
            max_text = "∞" if not max_uses else str(max_uses)
            reward = " + ".join(x for x in (f"{coins}🪙" if coins else "", f"{stars}⭐" if stars else "") if x) or "—"
            text += (f"{emoji} `{code}` → {reward} | {used or 0}/{max_text} | _{word}_\n"
                     f"   Created {(created or '?')[:10]} · Expires {(expires or 'Never')[:10]}\n\n")
            buttons.append([{"text": f"🗑️ Delete {code}"[:40], "callback_data": f"admin_code_del_{code_id}"}])
    nav = []
    if page > 0:         nav.append({"text": "⬅️ Prev", "callback_data": f"admin_codes_p_{page - 1}"})
    if page < pages - 1: nav.append({"text": "Next ➡️", "callback_data": f"admin_codes_p_{page + 1}"})
    if nav:
        buttons.append(nav)
    if total:
        buttons.append([{"text": "🧹 Delete all expired / used-up", "callback_data": "admin_codes_clean"}])
    buttons.append([{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}])
    edit_message(chat_id, message_id, text, {"inline_keyboard": buttons})


def admin_code_delete_confirm(chat_id, message_id, code_id):
    row = db_execute("SELECT code, used_count, max_uses FROM redeem_codes WHERE code_id = ?",
                     (code_id,), fetch='one')
    if not row:
        edit_message(chat_id, message_id, "❌ That code no longer exists.",
                     {"inline_keyboard": [[{"text": "🔙 Codes", "callback_data": "admin_list_codes"}]]})
        return
    edit_message(chat_id, message_id,
                 f"🗑️ *Delete redeem code?*\n\n`{row[0]}`\nUsed `{row[1] or 0}` time(s)\n\n"
                 f"It can't be redeemed afterwards. Coins already given stay with users.",
                 {"inline_keyboard": [[{"text": "✅ Yes, delete", "callback_data": f"admin_code_delok_{code_id}"},
                                       {"text": "❌ Cancel", "callback_data": "admin_list_codes"}]]})


def admin_code_delete(chat_id, message_id, code_id):
    row = db_execute("SELECT code FROM redeem_codes WHERE code_id = ?", (code_id,), fetch='one')
    if row:
        db_execute("DELETE FROM redeem_codes WHERE code_id = ?", (code_id,))
        try: update_system_stats()
        except Exception: pass
        try: async_backup("code_deleted")
        except Exception: pass
        print(f"🗑️ Redeem code deleted: {row[0]}")
    admin_list_codes(chat_id, message_id)


def admin_codes_clean(chat_id, message_id, confirmed=False):
    now = datetime.now().isoformat()
    where = ("(expires_at IS NOT NULL AND expires_at < ?) OR is_active = 0 "
             "OR (max_uses > 0 AND used_count >= max_uses)")
    n = (db_execute(f"SELECT COUNT(*) FROM redeem_codes WHERE {where}", (now,), fetch='one') or [0])[0] or 0
    if not confirmed:
        if not n:
            edit_message(chat_id, message_id, "✅ Nothing to clean — no expired, inactive or used-up codes.",
                         {"inline_keyboard": [[{"text": "🔙 Codes", "callback_data": "admin_list_codes"}]]})
            return
        edit_message(chat_id, message_id,
                     f"🧹 Delete `{n}` expired / inactive / used-up code(s)?\n\nActive codes are not touched.",
                     {"inline_keyboard": [[{"text": "✅ Yes, delete", "callback_data": "admin_codes_cleanok"},
                                           {"text": "❌ Cancel", "callback_data": "admin_list_codes"}]]})
        return
    db_execute(f"DELETE FROM redeem_codes WHERE {where}", (now,))
    try: update_system_stats()
    except Exception: pass
    try: async_backup("codes_cleaned")
    except Exception: pass
    admin_list_codes(chat_id, message_id)

USERS_PER_PAGE = 8

def _ago(iso):
    try:
        secs = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except Exception:
        return "never"
    if secs < 90:      return "just now"
    if secs < 3600:    return f"{int(secs // 60)}m ago"
    if secs < 86400:   return f"{int(secs // 3600)}h ago"
    if secs < 86400*60: return f"{int(secs // 86400)}d ago"
    return iso[:10]


def _user_plan_label(uid, premium, expires, plan):
    if is_admin(uid):
        return "👑 Admin"
    if premium and expires:
        try:
            if datetime.fromisoformat(expires) > datetime.now():
                return f"⭐ Premium{(' ' + plan.title()) if plan else ''} → {expires[:10]}"
        except Exception:
            pass
    return "🆓 Free"


def _user_card(row, number=None):
    uid, username, first, coins, stars, premium, expires, plan, join_date, last_active = row[:10]
    banned = bool(row[10]) if len(row) > 10 else False
    name = safe_md(first or 'Unknown')
    head = f"*{number}.* " if number else ""
    lines = [f"{head}👤 `{name}`" + (f"  `@{safe_md(username)}`" if username else "") + ("  🚫 *BANNED*" if banned else ""),
             f"🆔 `{uid}`",
             f"💎 {_user_plan_label(uid, premium, expires, plan)}",
             f"🪙 `{coins or 0}`   ⭐ `{stars or 0}`",
             f"📅 Joined `{(join_date or 'Unknown')[:10]}`  ·  🕒 Active `{_ago(last_active) if last_active else 'never'}`"]
    return "\n".join(lines)


_USER_COLS = ("user_id, username, first_name, coins_balance, stars_balance, is_premium, "
              "premium_expires, premium_plan, join_date, last_active, is_banned")


def admin_list_users(chat_id, message_id, page=0):
    total = (db_execute("SELECT COUNT(*) FROM users", fetch='one') or [0])[0] or 0
    pages = max(1, -(-total // USERS_PER_PAGE))
    page = max(0, min(int(page), pages - 1))
    rows = db_execute(f"SELECT {_USER_COLS} FROM users ORDER BY join_date DESC LIMIT ? OFFSET ?",
                      (USERS_PER_PAGE, page * USERS_PER_PAGE), fetch='all') or []
    if not rows:
        text = "👥 No users found."
    else:
        premium_n = (db_execute("SELECT COUNT(*) FROM users WHERE is_premium=1", fetch='one') or [0])[0]
        text = (f"*👥 USERS*  —  page {page + 1}/{pages}\n"
                f"Total `{total}`  ·  ⭐ Premium `{premium_n}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n")
        text += "\n\n".join(_user_card(r, page * USERS_PER_PAGE + i + 1) for i, r in enumerate(rows))
    nav = []
    if page > 0:         nav.append({"text": "⬅️ Prev", "callback_data": f"admin_users_p_{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": f"admin_users_p_{page}"})
    if page < pages - 1: nav.append({"text": "Next ➡️", "callback_data": f"admin_users_p_{page + 1}"})
    keyboard = {"inline_keyboard": [
        [{"text": "🔍 Search by User ID", "callback_data": "admin_search_user"}],
        nav,
        [{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, keyboard)


def admin_prompt_user_search(chat_id, message_id, admin_id):
    set_user_step(admin_id, 'awaiting_admin_user_search')
    edit_message(chat_id, message_id,
        "*🔍 SEARCH USER*\n\nSend the user's *numeric ID* (e.g. `8935687148`).\n"
        "_You can also send an @username or part of a name._",
        {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_list_users"}]]})


def admin_user_detail(chat_id, target_id):
    row = db_execute(f"SELECT {_USER_COLS} FROM users WHERE user_id = ?", (target_id,), fetch='one')
    if not row:
        send_message(chat_id, f"❌ No user with ID `{target_id}`.",
                     {"inline_keyboard": [[{"text": "🔍 Search again", "callback_data": "admin_search_user"}],
                                          [{"text": "👥 All Users", "callback_data": "admin_list_users"}]]})
        return
    dep = db_execute("SELECT COUNT(*), SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) "
                     "FROM deployments WHERE user_id = ?", (target_id,), fetch='one') or (0, 0)
    text = (f"*👤 USER DETAILS*\n━━━━━━━━━━━━━━━━━━━━━━\n{_user_card(row)}\n"
            f"📦 Deployments: `{dep[0] or 0}` (🟢 `{dep[1] or 0}` running)" + _p2_user_extra(target_id))
    info = get_ban_info(target_id)
    if info:
        text += f"\n🚫 Ban reason: `{safe_md(info['reason'] or 'none given')}`"
    if is_admin(target_id):
        ban_row = []
    elif info:
        ban_row = [{"text": "✅ Unban user", "callback_data": f"admin_unban_{target_id}"}]
    else:
        ban_row = [{"text": "🚫 Ban user", "callback_data": f"admin_ban_{target_id}"}]
    send_message(chat_id, text,
                 {"inline_keyboard": ([ban_row] if ban_row else []) + [[{"text": "⚙️ Limits, note & PIN", "callback_data": f"p2_au_{target_id}"}],
                                      [{"text": "🔄 Refresh", "callback_data": f"admin_user_view_{target_id}"}],
                                      [{"text": "🔍 Search again", "callback_data": "admin_search_user"}],
                                      [{"text": "👥 All Users", "callback_data": "admin_list_users"}]]})


# ==================== BANNING ====================
def get_ban_info(user_id):
    """None if the user isn't banned, else {'reason','at','by'}. Admins can never be banned."""
    if is_admin(user_id):
        return None
    try:
        row = db_execute("SELECT is_banned, ban_reason, banned_at, banned_by FROM users WHERE user_id = ?",
                         (int(user_id),), fetch='one')
        if row and row[0]:
            return {"reason": row[1], "at": row[2], "by": row[3]}
    except Exception:
        pass
    return None


def is_banned(user_id) -> bool:
    return get_ban_info(user_id) is not None


def banned_text(info=None):
    t = "🚫 *Your account has been banned.*\n\nYou can no longer use this bot."
    if info and info.get("reason"):
        t += f"\n\n📝 Reason: `{safe_md(info['reason'])[:300]}`"
    t += "\n\nIf you think this is a mistake, contact the admin."
    return t


def ban_user(admin_id, target_id, reason=None):
    """Ban a user: flag the account, STOP every one of their bots, notify them. -> (ok, message)."""
    target_id = int(target_id)
    if is_admin(target_id):
        return False, "⛔ Admins can't be banned."
    if target_id == int(admin_id):
        return False, "⛔ You can't ban yourself."
    if not db_execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,), fetch='one'):
        return False, f"❌ No user with ID `{target_id}`."
    reason = (reason or "").strip()[:300] or None
    db_execute("UPDATE users SET is_banned = 1, ban_reason = ?, banned_at = ?, banned_by = ?, step = NULL, "
               "pending_json = '{}' WHERE user_id = ?",
               (reason, datetime.now().isoformat(), int(admin_id), target_id))
    stopped = 0
    for (dep_id,) in (db_execute("SELECT deployment_id FROM deployments WHERE user_id = ? "
                                 "AND status IN ('active','paused')", (target_id,), fetch='all') or []):
        try:
            stop_deployment(dep_id)
            stopped += 1
        except Exception as e:
            print(f"⚠️ ban: could not stop dep#{dep_id}: {e}")
    try:
        send_message(target_id, banned_text({"reason": reason}))
    except Exception:
        pass
    async_backup(f"ban_{target_id}")
    return True, f"🚫 User `{target_id}` banned. `{stopped}` bot(s) stopped."


def unban_user(admin_id, target_id):
    target_id = int(target_id)
    if not get_ban_info(target_id):
        return False, "ℹ️ That user isn't banned."
    db_execute("UPDATE users SET is_banned = 0, ban_reason = NULL, banned_at = NULL, banned_by = NULL "
               "WHERE user_id = ?", (target_id,))
    try:
        send_message(target_id, "✅ *Your account has been unbanned.* You can use the bot again.\n\n"
                                "Your stopped bots were not restarted automatically — start them from *My Deployments*.",
                     {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
    except Exception:
        pass
    async_backup(f"unban_{target_id}")
    return True, f"✅ User `{target_id}` unbanned."


def admin_prompt_ban(chat_id, message_id, admin_id, target_id):
    if is_admin(target_id):
        send_message(chat_id, "⛔ Admins can't be banned.")
        return
    set_user_step(admin_id, 'awaiting_ban_reason', ban_target=int(target_id))
    edit_message(chat_id, message_id,
        f"*🚫 BAN USER* `{target_id}`\n\nSend the *reason* (shown to the user), or ban without one.\n"
        f"_Their running bots will be stopped immediately._",
        {"inline_keyboard": [
            [{"text": "🚫 Ban without reason", "callback_data": f"admin_ban_go_{target_id}"}],
            [{"text": "❌ Cancel", "callback_data": f"admin_user_view_{target_id}"}]]})


def admin_prompt_ban_by_id(chat_id, message_id, admin_id):
    set_user_step(admin_id, 'awaiting_ban_id')
    edit_message(chat_id, message_id,
        "*🚫 BAN A USER*\n\nSend the user's *numeric ID*.",
        {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_banned_list"}]]})


def admin_list_banned(chat_id, message_id):
    rows = db_execute("SELECT user_id, first_name, username, ban_reason, banned_at FROM users "
                      "WHERE is_banned = 1 ORDER BY banned_at DESC LIMIT 25", fetch='all') or []
    if not rows:
        text = "*🚫 BANNED USERS*\n\nNobody is banned."
        kb = {"inline_keyboard": [[{"text": "➕ Ban a user by ID", "callback_data": "admin_ban_by_id"}],
                                  [{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    else:
        lines, btns = [], []
        for uid, fn, un, why, at in rows:
            lines.append(f"• `{safe_md(fn or 'Unknown')}`" + (f" `@{safe_md(un)}`" if un else "") +
                         f" — `{uid}`\n   📝 {safe_md(why or 'no reason')[:80]}  ·  `{(at or '')[:10]}`")
            btns.append([{"text": f"✅ Unban {uid}", "callback_data": f"admin_unban_{uid}"}])
        text = f"*🚫 BANNED USERS* ({len(rows)})\n\n" + "\n".join(lines)
        kb = {"inline_keyboard": btns + [[{"text": "➕ Ban a user by ID", "callback_data": "admin_ban_by_id"}],
                                         [{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, kb)


def admin_handle_user_search(chat_id, admin_id, text):
    q = (text or '').strip()
    set_user_step(admin_id, None)
    if q.lstrip('-').isdigit():
        admin_user_detail(chat_id, int(q))
        return
    like = f"%{q.lstrip('@')}%"
    rows = db_execute("SELECT user_id, first_name, username FROM users "
                      "WHERE username LIKE ? OR first_name LIKE ? ORDER BY last_active DESC LIMIT 8",
                      (like, like), fetch='all') or []
    if not rows:
        send_message(chat_id, "❌ No matching user.",
                     {"inline_keyboard": [[{"text": "🔍 Search again", "callback_data": "admin_search_user"}],
                                          [{"text": "👥 All Users", "callback_data": "admin_list_users"}]]})
    elif len(rows) == 1:
        admin_user_detail(chat_id, rows[0][0])
    else:
        send_message(chat_id, f"*🔍 {len(rows)} matches* — pick one:",
                     {"inline_keyboard": [[{"text": f"{(r[1] or 'Unknown')[:24]} · {r[0]}",
                                            "callback_data": f"admin_user_view_{r[0]}"}] for r in rows]
                                         + [[{"text": "👥 All Users", "callback_data": "admin_list_users"}]]})

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
        text = "*📋 SUBSCRIPTIONS*\n\n"
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
    edit_message(chat_id, message_id, "*🎫 CREATE REDEEM CODE*\n\nSelect amount:", keyboard)

def ask_code_max_uses(admin_id, amount, chat_id, message_id):
    set_user_step(admin_id, 'awaiting_code_uses', temp_code_amount=amount)
    keyboard = {
        "inline_keyboard": [
            [{"text": "1 use",   "callback_data": "code_uses_1"},
             {"text": "5 uses",  "callback_data": "code_uses_5"}],
            [{"text": "10 uses", "callback_data": "code_uses_10"},
             {"text": "50 uses", "callback_data": "code_uses_50"}],
            [{"text": "100 uses", "callback_data": "code_uses_100"},
             {"text": "♾️ Unlimited", "callback_data": "code_uses_0"}],
            [{"text": "✍️ Custom number", "callback_data": "code_uses_custom"}],
            [{"text": "🔙 Back", "callback_data": "admin_create_code"}]
        ]
    }
    text = f"*🎫 CREATE REDEEM CODE*\n\n💰 Amount: `{amount}🪙`\n\nHow many times can this code be used in total?"
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def ask_code_expiry(admin_id, max_uses, chat_id, message_id):
    set_user_step(admin_id, 'awaiting_code_expiry', temp_code_max_uses=max_uses)
    keyboard = {
        "inline_keyboard": [
            [{"text": "1 day",   "callback_data": "code_expiry_1"},
             {"text": "7 days",  "callback_data": "code_expiry_7"}],
            [{"text": "30 days", "callback_data": "code_expiry_30"},
             {"text": "90 days", "callback_data": "code_expiry_90"}],
            [{"text": "365 days", "callback_data": "code_expiry_365"},
             {"text": "♾️ Never expires", "callback_data": "code_expiry_36500"}],
            [{"text": "✍️ Custom days", "callback_data": "code_expiry_custom"}],
            [{"text": "🔙 Back", "callback_data": "admin_create_code"}]
        ]
    }
    uses_label = "Unlimited" if max_uses == 0 else str(max_uses)
    text = f"*🎫 CREATE REDEEM CODE*\n\n🔢 Max uses: `{uses_label}`\n\nHow many days until this code expires?"
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def finalize_create_code(admin_id, expiry_days, chat_id, message_id):
    user_step = get_user_step(admin_id)
    amount = user_step.get('temp_code_amount')
    max_uses = user_step.get('temp_code_max_uses')

    if not amount or max_uses is None:
        err_text = "❌ Session expired. Please start over."
        err_kb = {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_create_code"}]]}
        if message_id:
            edit_message(chat_id, message_id, err_text, err_kb)
        else:
            send_message(chat_id, err_text, err_kb)
        return

    set_user_step(admin_id, None)
    code = create_redeem_code(admin_id, amount, 0, expiry_days, max_uses)

    uses_label = "Unlimited" if max_uses == 0 else f"{max_uses} use(s)"
    expiry_label = "Never expires" if expiry_days >= 36500 else f"{expiry_days} day(s)"

    text = (f"✅ *CODE CREATED!*\n\n"
            f"🎫 `{code}`\n"
            f"💰 {amount}🪙\n"
            f"📅 {expiry_label}\n"
            f"🔢 {uses_label}\n\n"
            f"Share with users!")
    keyboard = {"inline_keyboard": [[{"text": "🎫 Create Another", "callback_data": "admin_create_code"},
                                     {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def process_create_code(admin_id, amount, chat_id, message_id):
    ask_code_max_uses(admin_id, amount, chat_id, message_id)

# ==================== ADMIN ADD COINS ====================
def admin_add_coins_start(admin_id, chat_id, message_id):
    set_user_step(admin_id, 'awaiting_coins_target')
    
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
        f"*🪙 ADD COINS*\n\n"
        f"Enter the *User ID* using the number pad below:\n\n"
        f"*User ID:* ` `\n\n"
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
        f"*🪙 ADD COINS*\n\n"
        f"Enter the *User ID* using the number pad below:\n\n"
        f"*User ID:* `{display_target}`\n\n"
        f"Click ✅ when done.",
        keyboard)

def confirm_target_user(admin_id, chat_id, message_id):
    user_step = get_user_step(admin_id)
    target_user_id = user_step.get('temp_target_user')
    
    if not target_user_id or not target_user_id.strip():
        edit_message(chat_id, message_id,
            f"❌ *No User ID entered!*\n\nPlease enter a user ID using the number pad.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    try:
        target_user_id = int(target_user_id)
    except ValueError:
        edit_message(chat_id, message_id,
            f"❌ *Invalid User ID!*\n\nPlease enter a valid numeric user ID.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (target_user_id,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        edit_message(chat_id, message_id,
            f"❌ *User not found!*\n\nUser ID `{target_user_id}` does not exist.\n\nPlease check and try again.",
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
        f"*🪙 ADD COINS*\n\n"
        f"Target user: `{target_user_id}` ({first_name})\n"
        f"Current balance: `{get_user_balances(target_user_id)['coins']}🪙`\n\n"
        f"Enter the amount of coins to add using the number pad below:\n\n"
        f"Amount: `0` 🪙",
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
    
    targets = _parse_coin_targets(target_user_id)
    if len(targets) == 1:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT first_name FROM users WHERE user_id = ?", (targets[0],))
        user = c.fetchone()
        conn.close()
        first_name = user[0] if user else "User"
        target_line = f"Target user: `{target_user_id}` ({first_name})\n"
        balance_line = f"Current balance: `{get_user_balances(targets[0])['coins']}🪙`\n\n"
    else:
        target_line = f"Target: *{len(targets)} users*\n"
        balance_line = ""

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
        f"*🪙 ADD COINS*\n\n"
        f"{target_line}"
        f"{balance_line}"
        f"Enter the amount of coins to add using the number pad below:\n\n"
        f"Amount: `{new_amount}` 🪙",
        keyboard)

def _parse_coin_targets(target_str):
    """
    temp_target_user holds either a single numeric ID (legacy/keypad flow)
    or a JSON list of IDs (multi-user text flow). Returns a list of ints
    either way, so every caller handles both formats identically instead
    of each re-implementing (and risking a crash on) the distinction.
    """
    if not target_str:
        return []
    try:
        parsed = json.loads(target_str)
        if isinstance(parsed, list):
            return [int(x) for x in parsed]
        return [int(parsed)]
    except (json.JSONDecodeError, ValueError, TypeError):
        try:
            return [int(target_str)]
        except (ValueError, TypeError):
            return []


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
    
    targets = _parse_coin_targets(target_user_id)
    if len(targets) == 1:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT first_name FROM users WHERE user_id = ?", (targets[0],))
        user = c.fetchone()
        conn.close()
        first_name = user[0] if user else "User"
    else:
        first_name = f"{len(targets)} users"
    
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
    
    if len(targets) == 1:
        balance_line = f"Current balance: `{get_user_balances(targets[0])['coins']}🪙`\n\n"
        target_line = f"Target user: `{target_user_id}` ({first_name})\n"
    else:
        balance_line = ""
        target_line = f"Target: *{first_name}*\n"

    edit_message(chat_id, message_id,
        f"*🪙 ADD COINS*\n\n"
        f"{target_line}"
        f"{balance_line}"
        f"Amount preset: `{preset_amount}` 🪙\n\n"
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

    targets = _parse_coin_targets(target_user_id)
    if not targets:
        edit_message(chat_id, message_id, "❌ No valid target user(s). Please try again.",
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_add_coins"}]]})
        return

    set_user_step(admin_id, None, temp_target_user=None, temp_coins_amount=None)

    succeeded, failed = [], []
    for uid in targets:
        try:
            update_user_coins(uid, amount, "admin_add", f"by_admin_{admin_id}")
            new_balance = get_user_balances(uid)['coins']
            succeeded.append((uid, new_balance))
        except Exception as e:
            failed.append((uid, str(e)))
            continue

        # Best-effort notification — a user who blocked the bot shouldn't
        # stop the rest of the batch from being credited.
        try:
            send_message(uid,
                f"🎉 *You received {amount} Coins!* 🎉\n\n"
                f"Your new balance: `{new_balance}🪙`\n\n"
                f"Use your coins to purchase premium subscription!",
                {"inline_keyboard": [[{"text": "⭐ Get Premium", "callback_data": "subscribe_premium"}]]})
        except Exception:
            pass

    async_backup(f"admin_coins_{admin_id}")

    if len(targets) == 1:
        if succeeded:
            uid, new_balance = succeeded[0]
            edit_message(chat_id, message_id,
                f"✅ *COINS ADDED SUCCESSFULLY!*\n\n"
                f"User: `{uid}`\n"
                f"Added: `+{amount}🪙`\n"
                f"New balance: `{new_balance}🪙`",
                {"inline_keyboard": [[{"text": "➕ Add More", "callback_data": "admin_add_coins"},
                                      {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
        else:
            edit_message(chat_id, message_id,
                f"❌ Failed to add coins to `{targets[0]}`: {failed[0][1]}",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
    else:
        summary = f"✅ *COINS ADDED TO {len(succeeded)}/{len(targets)} USERS!*\n\n" \
                  f"Amount each: `+{amount}🪙`\n\n"
        summary += "\n".join(f"• `{uid}` → `{bal}🪙`" for uid, bal in succeeded[:20])
        if len(succeeded) > 20:
            summary += f"\n… and {len(succeeded) - 20} more"
        if failed:
            summary += f"\n\n⚠️ Failed: " + ", ".join(f"`{uid}`" for uid, _ in failed[:10])
        edit_message(chat_id, message_id, summary,
            {"inline_keyboard": [[{"text": "➕ Add More", "callback_data": "admin_add_coins"},
                                  {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})

# ==================== GITHUB DEPLOY HELPERS ====================

def _start_github_deploy(chat_id, user_id, message_id, step, token=None):
    """
    After we have owner/repo/branch (and optionally token):
    fetch repo info, resolve branch, ask for env vars.
    """
    owner  = step.get('temp_github_owner', '')
    repo   = step.get('temp_github_repo', '')
    branch = step.get('temp_github_branch', '') or None

    if not owner or not repo:
        send_message(chat_id, "❌ Missing repo info. Start again.",
                     {"inline_keyboard": [[{"text": "🐙 GitHub Deploy", "callback_data": "github_deploy"}]]})
        return

    # Resolve branch
    if not branch:
        send_message(chat_id, f"🔄 Fetching repo info for `{owner}/{repo}`...")
        info = get_repo_info(owner, repo, token)
        branch = info.get('default_branch', 'main')

    description = ""
    try:
        info = get_repo_info(owner, repo, token)
        description = info.get('description', '') or ''
        lang = info.get('language', '') or ''
        stars = info.get('stargazers_count', 0)
        description = f"_{description[:80]}_\n" if description else ''
    except Exception:
        lang = ''; stars = 0

    summary = (
        f"Repo:   `{owner}/{repo}`\n"
        f"Branch: `{branch}`\n"
        + (f"Lang:   `{lang}` | ⭐ {stars}\n" if lang else "")
        + description
    )

    # Save state and let the user pick a plan (same choices as a file upload)
    set_user_step(user_id, 'awaiting_github_plan',
                  temp_github_owner=owner,
                  temp_github_repo=repo,
                  temp_github_branch=branch,
                  temp_github_token=token or '',
                  temp_source='github',
                  temp_gh_summary=summary)
    _show_github_plan_menu(chat_id, user_id, message_id, summary)


def _gh_plan_prices(plan):
    """Returns (duration_days_or_0, cost_coins, cost_stars) for a paid plan."""
    if plan == 'monthly':
        return 30, PRICE_MONTHLY_COINS, PRICE_MONTHLY_STARS
    if plan == 'yearly':
        return 365, PRICE_YEARLY_COINS, PRICE_YEARLY_STARS
    return 0, 0, 0


def _show_github_plan_menu(chat_id, user_id, message_id, summary):
    """Plan picker for GitHub deployments: 24h free / monthly / yearly / lifetime (admin)."""
    def _label(plan, title, days):
        if premium_covers_plan(user_id, plan):
            return f"{title} ({days}) - FREE for Premium"
        _, coins, stars = _gh_plan_prices(plan)
        return f"{title} ({days}) - {stars}⭐ / {coins}🪙"

    rows = [
        [{"text": f"🆓 Free ({FREE_DEPLOYMENT_DURATION_HOURS}h)", "callback_data": "gh_plan_free"}],
        [{"text": _label('monthly', '📅 Monthly', '30 days'), "callback_data": "gh_plan_monthly"}],
        [{"text": _label('yearly',  '🌟 Yearly',  '365 days'), "callback_data": "gh_plan_yearly"}],
    ]
    if is_admin(user_id):
        rows.append([{"text": "♾️ Lifetime (never expires) — ADMIN", "callback_data": "gh_plan_lifetime"}])
    rows.append([{"text": "❌ Cancel", "callback_data": "main_menu"}])

    msg = f"*🐙 REPO CONFIRMED*\n\n{summary}\n*💰 Choose a deployment plan:*"
    kb = {"inline_keyboard": rows}
    if message_id:
        edit_message(chat_id, message_id, msg, kb)
    else:
        send_message(chat_id, msg, kb)


def _ask_github_env_vars(chat_id, message_id, summary, plan):
    plan_label = {'free': f"Free ({FREE_DEPLOYMENT_DURATION_HOURS}h)", 'monthly': "Monthly (30 days)",
                  'yearly': "Yearly (365 days)", 'lifetime': "Lifetime (never expires)"}.get(plan, plan)
    msg = (
        f"*🐙 REPO CONFIRMED*\n\n{summary}"
        f"📋 Plan: *{plan_label}*\n\n"
        f"✅ Now send environment variables (one per line):\n"
        f"`BOT_TOKEN=xxxx`\n`OTHER_VAR=value`\n\n"
        f"Or send a dot `.` to skip:"
    )
    kb = {"inline_keyboard": [[{"text": "⏭️ Skip (no env vars)", "callback_data": "github_skip_env"}],
                               [{"text": "❌ Cancel", "callback_data": "main_menu"}]]}
    if message_id:
        edit_message(chat_id, message_id, msg, kb)
    else:
        send_message(chat_id, msg, kb)


def _launch_github_deploy(chat_id, user_id, step, main_file_name=None, skip_security=False,
                          paid_method=None):
    """Final step: kick off the actual GitHub deployment."""
    owner  = step.get('temp_github_owner', '')
    repo   = step.get('temp_github_repo', '')
    branch = step.get('temp_github_branch', 'main')
    token  = step.get('temp_github_token') or None
    env_raw = step.get('temp_env_vars', '')

    env_vars_dict = {}
    if env_raw and env_raw.strip() != '.':
        env_vars_dict, _ign = parse_env_input(env_raw)
        if _ign:
            send_message(chat_id, f"⚠️ `{_ign}` line(s) in your variables were not understood and were "
                                  f"skipped. Use one `KEY=VALUE` per line.")
    if token:
        env_vars_dict['GITHUB_DEPLOY_TOKEN'] = token

    # Plan chosen by the user in the plan picker (default: free)
    plan = step.get('temp_plan') or 'free'
    if plan not in ('free', 'monthly', 'yearly', 'lifetime'):
        plan = 'free'

    if plan == 'lifetime' and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False

    cost_coins = cost_stars = 0
    if plan == 'free':
        can_deploy, reason = can_use_free_deployment(user_id)
        if not can_deploy:
            send_message(chat_id,
                f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
            return False
        duration, payment_method, is_free = FREE_DEPLOYMENT_DURATION_HOURS, 'free', True
    else:
        duration, price_coins, price_stars = _gh_plan_prices(plan)
        is_free = False
        if plan == 'lifetime' or premium_covers_plan(user_id, plan):
            payment_method = 'premium_free'
        elif paid_method in ('coins', 'stars'):
            payment_method = paid_method
            cost_coins = price_coins if paid_method == 'coins' else 0
            cost_stars = price_stars if paid_method == 'stars' else 0
        else:
            # Needs payment first — persist everything collected so far and ask.
            set_user_step(user_id, 'awaiting_github_payment',
                          temp_env_vars=env_raw,
                          temp_main_file=main_file_name or '',
                          temp_plan=plan)
            send_message(chat_id,
                f"*💰 Choose Payment Method*\n\n"
                f"Repo: `{owner}/{repo}`\n"
                f"Plan: *{plan.upper()}* ({duration} days)\n\n"
                f"Cost: `{price_stars}⭐` or `{price_coins}🪙`",
                {"inline_keyboard": [
                    [{"text": f"⭐ Pay {price_stars} Stars", "callback_data": "gh_pay_stars"}],
                    [{"text": f"🪙 Pay {price_coins} Coins", "callback_data": "gh_pay_coins"}],
                    [{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
            return False

    set_user_step(user_id, None)
    ok = deploy_from_github(
        chat_id, user_id,
        owner, repo, branch, token,
        env_vars_dict,
        plan=plan, duration=duration,
        cost_coins=cost_coins, cost_stars=cost_stars,
        payment_method=payment_method,
        is_free=is_free, main_file_name=main_file_name,
        skip_security=skip_security)

    if not ok and paid_method == 'coins' and cost_coins:
        update_user_coins(user_id, cost_coins, "deployment_refund", f"github_{plan}")
        send_message(chat_id, f"↩️ Deployment didn't complete — `{cost_coins}🪙` refunded.")
    return ok


# ==================== BROADCAST SYSTEM ====================

def _send_broadcast_preview(chat_id, btype, file_id, caption, buttons, audience="all"):
    """Send a live preview of the broadcast to the admin before confirming."""
    if not isinstance(buttons, list):
        buttons = []
    audience = _broadcast_audience.get(chat_id, audience)

    confirm_kb = {"inline_keyboard": (buttons if buttons else []) + [
        [{"text": f"✅ Send to: {BROADCAST_AUDIENCES.get(audience, 'Everyone')}", "callback_data": "broadcast_confirm"}],
        [{"text": "✏️ Edit Caption",       "callback_data": "broadcast_edit_caption"}],
        [{"text": "❌ Cancel",              "callback_data": "admin_panel"}],
    ]}

    send_message(chat_id, "*👁 PREVIEW — exactly what users will see:*", None)
    try:
        if btype == 'photo' and file_id:
            _tg_send_media(chat_id, 'photo', file_id, caption or '', confirm_kb)
        elif btype == 'video' and file_id:
            _tg_send_media(chat_id, 'video', file_id, caption or '', confirm_kb)
        else:
            msg_text = caption or '⚠️ (no caption / empty message)'
            send_message(chat_id, msg_text, confirm_kb)
    except Exception as e:
        send_message(chat_id,
            f"⚠️ Preview error: `{e}`\n\n*Caption:*\n{caption or '(empty)'}",
            confirm_kb)


def _tg_send_media(chat_id, media_type, file_id, caption, keyboard=None):
    """Send a photo or video — with retry on 429 and proper UTF-8 decoding."""
    endpoint = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    field    = 'photo'    if media_type == 'photo' else 'video'

    if not file_id:
        print(f"❌ _tg_send_media: file_id is empty for {media_type}")
        return None

    _send_err.code = None
    payload = {"chat_id": chat_id, field: file_id, "parse_mode": "Markdown"}
    if caption:
        payload["caption"] = str(caption)[:1024]
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"{TELEGRAM_API}/{endpoint}",
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            _send_err.code = e.code
            if e.code == 429:
                sleep(3 * (attempt + 1))
                continue
            body = ''
            try: body = e.read().decode()[:300]
            except Exception: pass
            print(f"❌ _tg_send_media HTTP {e.code}: {body}")
            return None
        except Exception as e:
            print(f"❌ _tg_send_media error (attempt {attempt+1}): {e}")
            if attempt < 3:
                sleep(1)
    return None


BROADCAST_AUDIENCES = {
    "all":      "👥 Everyone",
    "premium":  "⭐ Premium / plan holders",
    "free":     "🆓 Free users",
    "running":  "🟢 Users with a running bot",
    "expiring": "⏰ Bots expiring within 72h",
    "recent":   "🕒 Active in the last 7 days",
}
_broadcast_audience = {}          # admin_id -> audience key chosen for the broadcast being composed


def broadcast_audience_ids(key="all"):
    """User IDs for an audience. Banned users and admins are never included."""
    now = datetime.now()
    nowi = now.isoformat()
    queries = {
        "all":      ("SELECT user_id FROM users WHERE user_id IS NOT NULL AND user_id != 0", ()),
        "premium":  ("SELECT user_id FROM users WHERE (is_premium = 1 AND premium_expires > ?) "
                     "OR (resource_tier_mb > 0 AND resource_tier_expires > ?)", (nowi, nowi)),
        "free":     ("SELECT user_id FROM users WHERE user_id != 0 AND NOT ((is_premium = 1 AND premium_expires > ?) "
                     "OR (resource_tier_mb > 0 AND resource_tier_expires > ?))", (nowi, nowi)),
        "running":  ("SELECT DISTINCT user_id FROM deployments WHERE status = 'active'", ()),
        "expiring": ("SELECT DISTINCT user_id FROM deployments WHERE status = 'active' AND expire_time IS NOT NULL "
                     "AND expire_time > ? AND expire_time <= ?", (nowi, (now + timedelta(hours=72)).isoformat())),
        "recent":   ("SELECT user_id FROM users WHERE last_active > ?", ((now - timedelta(days=7)).isoformat(),)),
    }
    q, params = queries.get(key, queries["all"])
    ids = [r[0] for r in (db_execute(q, params, fetch='all') or []) if r and r[0]]
    banned = {r[0] for r in (db_execute("SELECT user_id FROM users WHERE is_banned = 1", fetch='all') or [])}
    return sorted({int(i) for i in ids if int(i) not in banned and int(i) not in ADMIN_IDS})


def show_broadcast_audience_menu(chat_id, message_id):
    rows = [[{"text": f"{label} ({len(broadcast_audience_ids(k))})", "callback_data": f"broadcast_aud_{k}"}]
            for k, label in BROADCAST_AUDIENCES.items()]
    rows.append([{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}])
    edit_message(chat_id, message_id, "*📢 BROADCAST*\n\nWho should receive it?\n_(banned users are never included)_",
                 {"inline_keyboard": rows})


# ==================== ADMIN DASHBOARD ====================
def admin_dashboard_text():
    now = datetime.now()
    d1, d7, d30 = ((now - timedelta(days=n)).isoformat() for n in (1, 7, 30))
    q1 = lambda sql, *a: (db_execute(sql, a, fetch='one') or [0])[0] or 0
    users_total  = q1("SELECT COUNT(*) FROM users")
    new_24h      = q1("SELECT COUNT(*) FROM users WHERE join_date > ?", d1)
    new_7d       = q1("SELECT COUNT(*) FROM users WHERE join_date > ?", d7)
    active_24h   = q1("SELECT COUNT(*) FROM users WHERE last_active > ?", d1)
    premium_n    = len(broadcast_audience_ids("premium"))
    banned_n     = q1("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    bots_active  = max(0, q1("SELECT COUNT(*) FROM deployments WHERE status = 'active'") - len(missing_active_ids()))
    bots_total   = q1("SELECT COUNT(*) FROM deployments")
    expiring     = q1("SELECT COUNT(*) FROM deployments WHERE status = 'active' AND expire_time IS NOT NULL "
                      "AND expire_time > ? AND expire_time <= ?", now.isoformat(), (now + timedelta(hours=72)).isoformat())
    pool_mb      = get_resource_pool_mb()
    sold_mb, sold_users = tier_sold_mb()
    sellable     = pool_mb * TIER_POOL_MAX_FRACTION
    unalloc      = max(0.0, sellable - sold_mb)
    shared       = max(0.0, pool_mb - sold_mb)
    sharers      = count_pool_sharers()
    used_mb = sum(get_process_tree_ram_mb(r[0]) for r in
                  (db_execute("SELECT proc_pid FROM deployments WHERE status = 'active' AND proc_pid IS NOT NULL",
                              fetch='all') or []))

    def revenue(since):
        # real money = Stars paid for plans + Stars paid per deployment; coins are tracked separately
        sub = db_execute("SELECT COALESCE(SUM(amount_stars),0), COALESCE(SUM(amount_coins),0), COUNT(*) "
                         "FROM subscriptions WHERE start_date > ?", (since,), fetch='one') or (0, 0, 0)
        dep = db_execute("SELECT COALESCE(SUM(CASE WHEN payment_method='stars' THEN cost_stars ELSE 0 END),0), "
                         "COALESCE(SUM(CASE WHEN payment_method='coins' THEN cost_coins ELSE 0 END),0), COUNT(*) "
                         "FROM deployments WHERE payment_method IN ('stars','coins') AND is_free = 0 AND start_time > ? "
                         "AND (cost_stars > 0 OR cost_coins > 0)",
                         (since,), fetch='one') or (0, 0, 0)
        return int(sub[0]) + int(dep[0]), int(sub[1]) + int(dep[1]), int(sub[2]) + int(dep[2])

    r1, r7, r30 = revenue(d1), revenue(d7), revenue(d30)
    line = lambda label, r: f"{label}: `{r[0]}⭐` · `{r[1]}🪙` · {r[2]} sale(s)"
    return (
        "*📊 DASHBOARD*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*👥 Users*\nTotal `{users_total}` · New 24h `{new_24h}` · 7d `{new_7d}`\n"
        f"Active 24h `{active_24h}` · ⭐ Plan holders `{premium_n}` · 🚫 Banned `{banned_n}`\n"
        f"🛠 Maintenance: `{'ON' if maintenance_on() else 'off'}` · 🛡 Isolation: `{isolation_status()}`\n"
        f"🌐 Translation: `{translation_status()}`\n\n"
        f"*🤖 Bots*\nRunning `{bots_active}` of `{bots_total}` · ⏰ Expiring ≤72h `{expiring}`\n\n"
        f"*🧮 RAM pool*\nTotal `{format_resource(pool_mb)}` · In use now `{format_resource(used_mb)}`\n"
        f"Sold as plans `{format_resource(sold_mb)}` ({sold_users} user(s)) of `{format_resource(sellable)}` sellable\n"
        f"Still unallocated `{format_resource(unalloc)}`\n"
        f"Shared by free users `{format_resource(shared)}` ({sharers} sharer(s))\n\n"
        f"*💰 Revenue*\n{line('Today', r1)}\n{line('7 days', r7)}\n{line('30 days', r30)}"
    )


def do_broadcast(admin_id, btype, file_id, caption, buttons_json, audience="all"):
    """
    Send a message/photo/video to all users.
    Returns (success_count, fail_count, skipped_count).
    """
    # Parse inline buttons
    try:
        buttons = json.loads(buttons_json) if buttons_json else []
        if not isinstance(buttons, list):
            buttons = []
    except Exception:
        buttons = []

    kb = {"inline_keyboard": buttons} if buttons else None

    # Validate text content before starting
    if btype == 'text' and not (caption or '').strip():
        print("⚠️ do_broadcast: text broadcast has empty caption — aborting")
        return 0, 0, 0

    if btype in ('photo', 'video') and not file_id:
        print(f"⚠️ do_broadcast: {btype} broadcast has no file_id — aborting")
        return 0, 0, 0

    try:
        all_users = broadcast_audience_ids(audience)
    except Exception as e:
        print(f"❌ do_broadcast user fetch error: {e}")
        return 0, 0, 0

    total    = len(all_users)
    success  = fail = skipped = 0
    print(f"📢 Starting broadcast to {total} users | audience={audience} | type={btype} | file_id={bool(file_id)}")

    for i, uid in enumerate(all_users):
        # Skip the admin who triggered the broadcast
        if int(uid) in ADMIN_IDS:
            skipped += 1
            continue

        try:
            text_to_send = (caption or '').strip()
            if btype == 'text' and not text_to_send:
                skipped += 1
                continue

            def _send_once():
                if btype == 'photo' and file_id:
                    return _tg_send_media(uid, 'photo', file_id, caption, kb)
                if btype == 'video' and file_id:
                    return _tg_send_media(uid, 'video', file_id, caption, kb)
                return send_message(uid, text_to_send, kb)

            # Exponential backoff with jitter, but only when the failure was
            # a 429 rate-limit / transient error — never for blocked users.
            result = None
            for _try in range(5):
                result = _send_once()
                if isinstance(result, dict) and result.get('ok'):
                    break
                _code = getattr(_send_err, 'code', None)
                if result is None and (_code == 429 or _code is None) and _try < 4:
                    sleep(min(60, (2 ** _try)) + random.uniform(0, 1))
                    continue
                break

            # Both send_message and _tg_send_media return {'ok': True/False, ...}
            if isinstance(result, dict) and result.get('ok'):
                success += 1
            else:
                fail += 1
                if result:
                    desc = result.get('description', '') if isinstance(result, dict) else str(result)
                    if 'blocked' in str(desc).lower() or 'deactivated' in str(desc).lower():
                        skipped += 1
                        fail -= 1

        except Exception as ex:
            print(f"❌ Broadcast to {uid}: {ex}")
            fail += 1

        # Conservative rate limiting: 25 msgs/s with backoff
        sleep(0.04)
        # Log progress every 50 users
        if (i + 1) % 50 == 0:
            print(f"  📢 Progress: {i+1}/{total} | ✅{success} ❌{fail} ⏭{skipped}")

    print(f"✅ Broadcast done: {success} sent | {fail} failed | {skipped} skipped")
    async_backup(f"broadcast_{admin_id}")
    return success, fail, skipped


def _run_broadcast_and_notify(chat_id, admin_id, btype, file_id, caption, btns_raw, audience="all"):
    """Thread target: run broadcast and notify the admin with results."""
    try:
        success, fail, skipped = do_broadcast(admin_id, btype, file_id, caption, btns_raw, audience)
        total = success + fail
        send_message(chat_id,
            f"✅ *Broadcast Complete*\n\n"
            f"✅ Delivered: `{success}`\n"
            f"❌ Failed:    `{fail}`\n"
            f"⏭ Skipped:   `{skipped}` (blocked/admin)\n"
            f"🎯 Audience: {BROADCAST_AUDIENCES.get(audience, audience)}\n"
            f"📊 Recipients: `{total + skipped}`",
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
    except Exception as e:
        print(f"❌ _run_broadcast_and_notify error: {e}")
        try:
            send_message(chat_id,
                f"❌ *Broadcast failed with error:*\n`{e}`",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
        except Exception:
            pass


def _dispatch_deploy(chat_id, user_id, message_id, user_step, env_vars):
    """Central dispatch from env_done/env_skip to the correct deploy path."""
    plan         = user_step.get('plan') or 'free'
    temp_file    = user_step.get('temp_file')
    requirements = user_step.get('requirements')
    duration     = user_step.get('duration')
    cost_stars   = user_step.get('cost_stars')
    cost_coins   = user_step.get('cost_coins')

    # Stale button (session expired / bot restarted): no pending upload.
    if not temp_file or not os.path.exists(str(temp_file)):
        set_user_step(user_id, None)
        send_message(chat_id, "⚠️ *Session expired*\n\nYour pending upload is no longer available. "
                              "Please start the deployment again.")
        return

    if plan == 'free':
        set_user_step(user_id, None)
        deploy_free_bot_with_logs(chat_id, user_id, temp_file, requirements, env_vars)
    elif premium_covers_plan(user_id, plan):
        set_user_step(user_id, None)
        deploy_paid_bot(chat_id, user_id, temp_file, requirements, env_vars,
                        plan, duration, 0, 0, 'premium_free')
    else:
        # Non-premium paid plan — show payment options
        edit_message(chat_id, message_id,
            f"*💰 Choose Payment Method*\n\n"
            f"Plan: *{plan.upper()}*\n"
            f"Duration: {duration} days\n"
            f"Env vars: {len(env_vars)}\n\n"
            f"Cost: `{cost_stars}⭐` or `{cost_coins}🪙`",
            get_payment_keyboard(plan, cost_stars, cost_coins))


# ==================== CALLBACK HANDLER ====================
def touch_user(tg_user):
    """Keep a user's stored details accurate: called for EVERY message / button press, so a changed
    display name or @username (and last-active time) is picked up immediately, not just on /start."""
    try:
        uid = int(tg_user['id'])
        now = datetime.now().isoformat()
        name  = (tg_user.get('first_name') or '').strip() or None
        uname = tg_user.get('username') or None
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, join_date) VALUES (?,?,?,?)",
                  (uid, uname, name, now))
        c.execute("UPDATE users SET username = ?, first_name = COALESCE(?, first_name), last_active = ? "
                  "WHERE user_id = ?", (uname, name, now, uid))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ touch_user failed: {e}")


def handle_callback(callback):
    touch_user(callback.get('from') or {})
    _fp_ctx.uid = (callback.get('from') or {}).get('id')
    _fp_ctx.shared = False
    callback = _fp_share_remap(callback)
    if _fp_pre_callback(callback):
        return
    callback_id = callback['id']
    user_id = callback['from']['id']
    _ban = get_ban_info(user_id)
    if _ban:
        answer_callback(callback_id, "🚫 Your account is banned.", True)
        return
    message = callback.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    message_id = message.get('message_id')
    data = callback['data']
    
    answer_callback(callback_id)
    print(f"📨 Callback: {data} from user {user_id}")
    
    # ========== MAIN MENU ==========
    if (data in ("main_menu", "my_deployments") or data.startswith("view_deploy_")) \
            and str(get_user_step(user_id).get('step') or '').startswith('edit_'):
        set_user_step(user_id, None)           # leaving the edit prompt cancels it

    if data in ("main_menu", "admin_panel") and is_admin(user_id):
        if get_user_step(user_id).get('step') == 'awaiting_admin_user_search':
            set_user_step(user_id, None)           # leaving the search prompt cancels it

    if data == "main_menu":
        if is_user_verified(user_id):
            welcome = build_main_menu_text(user_id, callback['from'].get('first_name'))
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
    
    if data.startswith("admin_codes_p_"):
        if is_admin(user_id):
            admin_list_codes(chat_id, message_id, int(data.rsplit("_", 1)[1]))
        return

    if data.startswith("admin_code_del_"):
        if is_admin(user_id):
            admin_code_delete_confirm(chat_id, message_id, int(data.rsplit("_", 1)[1]))
        return

    if data.startswith("admin_code_delok_"):
        if is_admin(user_id):
            admin_code_delete(chat_id, message_id, int(data.rsplit("_", 1)[1]))
        return

    if data in ("admin_codes_clean", "admin_codes_cleanok"):
        if is_admin(user_id):
            admin_codes_clean(chat_id, message_id, confirmed=(data == "admin_codes_cleanok"))
        return

    if data == "admin_list_codes":
        if is_admin(user_id):
            admin_list_codes(chat_id, message_id)
        return
    
    if data == "admin_list_users":
        if is_admin(user_id):
            set_user_step(user_id, None)
            admin_list_users(chat_id, message_id)
        return

    if data.startswith("admin_users_p_"):
        if is_admin(user_id):
            admin_list_users(chat_id, message_id, int(data.rsplit("_", 1)[1]))
        return

    if data == "admin_search_user":
        if is_admin(user_id):
            admin_prompt_user_search(chat_id, message_id, user_id)
        return

    if data.startswith("admin_user_view_"):
        if is_admin(user_id):
            set_user_step(user_id, None)
            admin_user_detail(chat_id, int(data.rsplit("_", 1)[1]))
        return

    if data.startswith("admin_ban_go_"):
        if is_admin(user_id):
            set_user_step(user_id, None)
            ok, msg = ban_user(user_id, int(data.rsplit("_", 1)[1]))
            send_message(chat_id, msg, {"inline_keyboard": [[{"text": "👤 User", "callback_data": f"admin_user_view_{data.rsplit('_', 1)[1]}"}]]})
        return

    if data.startswith("admin_ban_") and data.rsplit("_", 1)[1].lstrip("-").isdigit():
        if is_admin(user_id):
            admin_prompt_ban(chat_id, message_id, user_id, int(data.rsplit("_", 1)[1]))
        return

    if data.startswith("admin_unban_"):
        if is_admin(user_id):
            tid = int(data.rsplit("_", 1)[1])
            ok, msg = unban_user(user_id, tid)
            send_message(chat_id, msg, {"inline_keyboard": [[{"text": "👤 User", "callback_data": f"admin_user_view_{tid}"}],
                                                            [{"text": "🚫 Banned list", "callback_data": "admin_banned_list"}]]})
        return

    if data == "admin_banned_list":
        if is_admin(user_id):
            set_user_step(user_id, None)
            admin_list_banned(chat_id, message_id)
        return

    if data == "admin_ban_by_id":
        if is_admin(user_id):
            admin_prompt_ban_by_id(chat_id, message_id, user_id)
        return
    
    if data == "admin_subscriptions":
        if is_admin(user_id):
            admin_subscriptions(chat_id, message_id)
        return

    # ========== ADMIN DATABASE BACKUP/RESTORE ==========
    if data == "admin_database":
        if is_admin(user_id):
            admin_database_menu(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data == "admin_resources":
        if is_admin(user_id):
            admin_resources_view(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data == "admin_scan_orphans":
        if is_admin(user_id):
            admin_scan_orphans_view(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data == "admin_kill_orphans":
        if is_admin(user_id):
            admin_kill_orphans_action(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data in ("admin_purge_orphans", "admin_purge_orphans_force"):
        if is_admin(user_id):
            admin_purge_orphans(chat_id, message_id, user_id, force=data.endswith("_force"))
        return

    if data == "admin_db_backup_now":
        if is_admin(user_id):
            admin_db_backup_now(chat_id, message_id, user_id)
        return

    if data == "admin_db_download":
        if is_admin(user_id):
            admin_db_download(chat_id, message_id, user_id)
        return

    if data == "admin_db_restore_start":
        if is_admin(user_id):
            admin_db_restore_start(chat_id, message_id, user_id)
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

    if data == "code_uses_custom":
        if is_admin(user_id):
            set_user_step(user_id, 'awaiting_code_uses_custom',
                          temp_code_amount=get_user_step(user_id).get('temp_code_amount'))
            edit_message(chat_id, message_id,
                "*🎫 CREATE REDEEM CODE*\n\nSend the max number of uses as a number "
                "(send `0` for unlimited):")
        return

    if data.startswith("code_uses_"):
        if is_admin(user_id):
            max_uses = int(data.split("_")[2])
            ask_code_expiry(user_id, max_uses, chat_id, message_id)
        return

    if data == "code_expiry_custom":
        if is_admin(user_id):
            us = get_user_step(user_id)
            set_user_step(user_id, 'awaiting_code_expiry_custom',
                          temp_code_amount=us.get('temp_code_amount'),
                          temp_code_max_uses=us.get('temp_code_max_uses'))
            edit_message(chat_id, message_id,
                "*🎫 CREATE REDEEM CODE*\n\nSend the number of days until expiry "
                "(send `0` for never expires):")
        return

    if data.startswith("code_expiry_"):
        if is_admin(user_id):
            expiry_days = int(data.split("_")[2])
            if expiry_days <= 0:
                expiry_days = 36500  # "never" — a century is a practical forever
            finalize_create_code(user_id, expiry_days, chat_id, message_id)
        return
    
    # ========== ADMIN ADD COINS ==========
    if data == "admin_add_coins":
        if is_admin(user_id):
            admin_add_coins_start(user_id, chat_id, message_id)
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

    if data == "resource_tiers":
        show_resource_tiers_menu(chat_id, user_id, message_id)
        return

    if data.startswith("tier_pick_"):
        show_tier_period_menu(chat_id, user_id, data.split("_", 2)[2], message_id)
        return

    if data.startswith("tier_stars_") or data.startswith("tier_coins_"):
        _tp = data.split("_")                      # tier, stars|coins, <key>, <m|y>
        _key, _per = (_tp[2] if len(_tp) > 2 else ""), (_tp[3] if len(_tp) > 3 else "m")
        if data.startswith("tier_stars_"):
            purchase_tier_stars(chat_id, user_id, _key, _per)
        else:
            purchase_tier_coins(chat_id, user_id, _key, _per)
        return

    if data.startswith("reactivate_premium_"):
        dep_id = int(data.split("_")[2])
        if is_user_premium(user_id):
            # Already premium (renewed elsewhere, or never actually lapsed) —
            # reactivate immediately, no reason to send them through a
            # purchase flow for something they already have.
            restart_deployment(dep_id, user_id, chat_id)
            view_deployment(chat_id, message_id, user_id, dep_id)
        else:
            # Not currently premium — send to the purchase flow. Once
            # payment completes, activate_premium() already auto-resumes
            # the user's most recent stopped/paused premium deployment.
            show_premium_menu(chat_id, user_id, message_id)
        return
    
    if data in ("premium_monthly_stars", "premium_yearly_stars", "premium_monthly_coins", "premium_yearly_coins"):
        # Standalone Premium is gone: Premium now comes with a Resource Plan (old buttons land here).
        show_resource_tiers_menu(chat_id, user_id, message_id)
        return

    # ========== DEPLOYMENT PLANS ==========
    if data == "deploy_new":
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, "User", message_id)
            return
        edit_message(chat_id, message_id, "*💰 DEPLOYMENT OPTIONS*\n\nChoose your plan:", get_deploy_menu(user_id))
        return
    
    if data == "plan_monthly":
        handle_paid_deployment(chat_id, user_id, message_id, "monthly", 30, PRICE_MONTHLY_COINS, PRICE_MONTHLY_STARS)
        return
    
    if data == "plan_yearly":
        handle_paid_deployment(chat_id, user_id, message_id, "yearly", 365, PRICE_YEARLY_COINS, PRICE_YEARLY_STARS)
        return

    if data == "plan_lifetime":
        if not is_admin(user_id):
            answer_callback(callback_id, "⛔ Admins only", show_alert=True)
            return
        handle_paid_deployment(chat_id, user_id, message_id, "lifetime", 0, 0, 0)
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
                        f"*⭐ STARS PAYMENT REQUIRED*\n\n"
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
                f"❌ *INSUFFICIENT COINS*\n\n"
                f"Required: `{cost_coins}🪙`\n"
                f"Your balance: `{balances['coins']}🪙`\n\n"
                f"Use a redeem code to get more coins!",
                {"inline_keyboard": [[{"text": "🎫 Redeem Code", "callback_data": "redeem_code"},
                                      {"text": "🔙 Back", "callback_data": "deploy_new"}]]})
            return
        
        if plan == "lifetime" and not is_admin(user_id):
            send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
            return
        if plan not in ("monthly", "yearly", "lifetime"):
            return

        update_user_coins(user_id, -cost_coins, "deployment", f"plan_{plan}")
        _deployed = deploy_paid_bot(chat_id, user_id, user_step.get('temp_file'),
                        user_step.get('requirements'), user_step.get('env_vars', {}),
                        plan, user_step.get('duration'), cost_coins, user_step.get('cost_stars'), 'coins')
        if not _deployed and not premium_covers_plan(user_id, plan):
            # Deployment failed after coins were charged — refund them.
            update_user_coins(user_id, cost_coins, "deployment_refund", f"plan_{plan}")
            send_message(chat_id, f"↩️ Deployment failed — `{cost_coins}🪙` refunded.")
        elif _deployed:
            apply_discount_cashback(user_id, cost_coins)
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
            f"*📦 SEND requirements.txt FILE*\n\n"
            f"Please send your `requirements.txt` file now.\n\n"
            f"Example content:\n"
            f"```\npython-telegram-bot==20.7\nrequests==2.31.0\n```\n\n"
            f"Or click Auto-detect:",
            {"inline_keyboard": [[{"text": "⚡ Auto-detect & Skip", "callback_data": "reqs_no"}],
                                  [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return
    
    if data == "reqs_no":
        user_step = get_user_step(user_id)
        # Detect platform for env var hints
        platform_hint = ""
        try:
            tf = user_step.get('temp_file')
            if tf and Path(tf).exists():
                code   = Path(tf).read_text(errors='ignore')
                fws    = detect_bot_framework(code)
                plabel = get_platform_label(fws)
                phint  = get_platform_env_hint(fws)
                if fws and fws != ['generic']:
                    platform_hint = f"\n\n🔍 *Detected: {plabel}*"
                if phint:
                    platform_hint += f"\n\n*Required env vars:*\n{phint}"
        except Exception:
            pass
        set_user_step(user_id, 'awaiting_env',
                     waiting_for_env=1, waiting_for_reqs=0,
                     temp_file=user_step.get('temp_file'),
                     requirements=None,
                     plan=user_step.get('plan'),
                     duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'),
                     cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'),
                     env_vars=user_step.get('env_vars') or {})
        edit_message(chat_id, message_id,
            f"*🔧 ENVIRONMENT VARIABLES*{platform_hint}\n\n"
            "Send variables one per line:\n"
            "```\nBOT_TOKEN=your_token\nAPI_KEY=abc123\n```\n\n"
            "Or deploy without any:",
            get_env_keyboard(len(user_step.get('env_vars') or {})))
        return
    
    # ========== ENVIRONMENT VARIABLES ==========
    if data == "view_env":
        user_step = get_user_step(user_id)
        env_vars = user_step.get('env_vars', {})
        if not env_vars:
            text = "📝 *No environment variables set*\n\nSend as KEY=VALUE (one per line)\n\nExample:\n```\nBOT_TOKEN=123456:ABC\nAPI_KEY=your_key\nDATABASE_URL=postgresql://...\n```"
        else:
            text = "📝 *Current Variables:*\n\n"
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
            f"*📤 SEND ENVIRONMENT VARIABLES*\n\n"
            f"Send your environment variables as text:\n"
            f"```\nBOT_TOKEN=your_actual_token\nAPI_KEY=your_api_key\nDATABASE_URL=postgresql://user:pass@localhost/db\n```\n\n"
            f"One variable per line. Use KEY=VALUE format.\n\n"
            f"All variables will be available via `os.environ.get('KEY')`\n\n"
            f"Click Skip if you have none:",
            {"inline_keyboard": [[{"text": "⏭️ Skip", "callback_data": "env_skip"}],
                                  [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return
    
    if data == "env_skip":
        user_step = get_user_step(user_id)
        env_vars  = {}   # skip means deploy with no vars
        _dispatch_deploy(chat_id, user_id, message_id, user_step, env_vars)
        return

    if data == "env_done":
        user_step = get_user_step(user_id)
        raw = user_step.get('env_vars') or {}
        if isinstance(raw, str):
            try:    raw = json.loads(raw)
            except Exception: raw = {}
        _dispatch_deploy(chat_id, user_id, message_id, user_step, dict(raw))
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

    if data.startswith("backup_deploy_"):
        try:
            _bd = int(data.rsplit("_", 1)[1])
        except ValueError:
            return
        send_message(chat_id, "💾 Preparing your backup…")
        send_deployment_backup(chat_id, user_id, _bd)
        return

    # ── Edit deployment (env · requirements · code) ──
    if data.startswith("edit_"):
        try:
            _p = data.split("_")
            _act, _dep = _p[1], int(_p[2])
        except Exception:
            return
        if _act == "deploy":
            show_edit_menu(chat_id, message_id, user_id, _dep)
        elif _act == "env":
            show_edit_env(chat_id, message_id, user_id, _dep)
        elif _act == "envadd":
            edit_env_prompt(chat_id, message_id, user_id, _dep)
        elif _act == "envdel" and len(_p) > 3:
            edit_env_delete(chat_id, message_id, user_id, _dep, _p[3])
        elif _act == "reqs":
            show_edit_reqs(chat_id, message_id, user_id, _dep)
        elif _act == "reqsnew":
            edit_reqs_prompt(chat_id, message_id, user_id, _dep)
        elif _act == "code":
            show_edit_code(chat_id, message_id, user_id, _dep)
        elif _act == "codenew":
            edit_code_prompt(chat_id, message_id, user_id, _dep)
        return
    
    if data.startswith("view_install_logs_"):
        dep_id = int(data.split("_")[3])
        view_install_logs(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("full_logs_"):
        send_full_logs(chat_id, user_id, int(data.split("_")[2]))
        return

    if data.startswith("copy_logs_"):
        send_copyable_logs(chat_id, user_id, int(data.split("_")[2]))
        return

    if data.startswith("view_runtime_logs_"):
        dep_id = int(data.split("_")[3])
        view_runtime_logs(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("stop_deploy_"):
        dep_id = int(data.split("_")[2])
        stop_deployment(dep_id)
        view_deployment(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("restart_deploy_"):
        dep_id = int(data.split("_")[2])
        restart_deployment(dep_id, user_id, chat_id)
        view_deployment(chat_id, message_id, user_id, dep_id)
        return

    if data.startswith("restart_free_"):
        dep_id = int(data.split("_")[2])
        restart_deployment(dep_id, user_id, chat_id, force_free_downgrade=True)
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
            f"*⚠️ DELETE DEPLOYMENT*\n\n"
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
            if not has_accepted_tos(user_id):
                show_tos_prompt(chat_id, user_id, message_id)
                return
            balances = get_user_balances(user_id)
            edit_message(chat_id, message_id,
                f"✅ *VERIFIED!*\n\n"
                f"🪙 {balances['coins']} | ⭐ {balances['stars']}\n\n"
                f"Welcome!",
                get_main_menu(user_id))
        else:
            send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    if data == "verify_channel":
        if check_channel_membership(user_id):
            mark_channel_joined(user_id)
            if not has_accepted_tos(user_id):
                show_tos_prompt(chat_id, user_id, message_id)
                return
            balances = get_user_balances(user_id)
            edit_message(chat_id, message_id,
                f"✅ *VERIFIED!*\n\n"
                f"Thank you for joining {escape_md_v1(required_channels_text())}!",
                get_main_menu(user_id))
        else:
            _missing = get_missing_channels(user_id) or REQUIRED_CHANNELS
            edit_message(chat_id, message_id,
                f"❌ *NOT VERIFIED*\n\n"
                f"You still need to join:\n"
                + "\n".join(f"• {escape_md_v1(ch['id'])}" for ch in _missing)
                + "\n\nJoin, then tap VERIFY again.",
                {"inline_keyboard": _join_buttons(_missing)
                 + [[{"text": "✅ VERIFY", "callback_data": "verify_channel"}]]})
        return

    # ========== TERMS OF SERVICE ==========
    if data == "tos_agree":
        mark_tos_accepted(user_id)
        balances = get_user_balances(user_id)
        edit_message(chat_id, message_id,
            f"✅ *Thanks for confirming!*\n\n"
            f"🪙 {balances['coins']} | ⭐ {balances['stars']}\n\n"
            f"Welcome!",
            get_main_menu(user_id))
        return
    
    # ========== MY BALANCE ==========
    if data == "my_balance":
        handle_balance(chat_id, user_id, message_id)
        return
    
    # ========== GITHUB DEPLOY ==========
    if data == "github_deploy":
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, "User", message_id)
            return
        set_user_step(user_id, 'awaiting_github_url')
        edit_message(chat_id, message_id,
            "*🐙 DEPLOY FROM GITHUB*\n\n"
            "Send me the GitHub repository URL or shorthand:\n\n"
            "*Examples:*\n"
            "`https://github.com/owner/repo`\n"
            "`github.com/owner/repo`\n"
            "`owner/repo`\n"
            "`owner/repo@branch` ← specific branch\n\n"
            "Supports *public and private* repos.\n"
            "For private repos you'll be asked for a token next.",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
        return

    # ---- GitHub: plan picker (free 24h / monthly / yearly / lifetime-admin) ----
    if data.startswith("gh_plan_"):
        plan = data[len("gh_plan_"):]
        if plan not in ('free', 'monthly', 'yearly', 'lifetime'):
            return
        step = get_user_step(user_id)
        if step.get('step') != 'awaiting_github_plan':
            return   # stale button
        if plan == 'lifetime' and not is_admin(user_id):
            answer_callback(callback_id, "⛔ Admins only", show_alert=True)
            return
        # Resource quota is checked BEFORE any payment is offered, so nobody
        # pays (especially with non-refundable Stars) for a deployment that
        # would then be refused for lack of resources.
        _q_ok, _q_used, _q_total = check_user_quota(user_id)
        if not _q_ok:
            edit_message(chat_id, message_id, quota_exceeded_text(_q_used, _q_total),
                {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                                     [{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
            return
        if plan == 'free':
            can_deploy, reason = can_use_free_deployment(user_id)
            if not can_deploy:
                edit_message(chat_id, message_id,
                    f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nPick another plan or upgrade to Premium.",
                    {"inline_keyboard": [
                        [{"text": "🔙 Choose Plan", "callback_data": "github_replan"}],
                        [{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
                return
        set_user_step(user_id, 'awaiting_env_vars', temp_plan=plan, temp_source='github')
        _ask_github_env_vars(chat_id, message_id, step.get('temp_gh_summary') or '', plan)
        return

    if data == "github_replan":
        step = get_user_step(user_id)
        if step.get('temp_github_owner'):
            set_user_step(user_id, 'awaiting_github_plan')
            _show_github_plan_menu(chat_id, user_id, message_id, step.get('temp_gh_summary') or '')
        return

    # ---- GitHub: pay for a monthly/yearly plan with coins ----
    if data == "gh_pay_coins":
        step = get_user_step(user_id)
        plan = step.get('temp_plan')
        if step.get('step') != 'awaiting_github_payment' or plan not in ('monthly', 'yearly'):
            return
        _days, price_coins, _stars = _gh_plan_prices(plan)
        bal = get_user_balances(user_id)
        if bal['coins'] < price_coins:
            edit_message(chat_id, message_id,
                f"❌ *INSUFFICIENT COINS*\n\nRequired: `{price_coins}🪙`\n"
                f"Your balance: `{bal['coins']}🪙`\n\nUse a redeem code or pay with Stars!",
                {"inline_keyboard": [[{"text": "⭐ Pay with Stars", "callback_data": "gh_pay_stars"},
                                      {"text": "🎫 Redeem Code", "callback_data": "redeem_code"}]]})
            return
        # Lock the step so a double-tap can't charge twice
        set_user_step(user_id, 'github_paying')
        update_user_coins(user_id, -price_coins, "deployment", f"github_{plan}")
        edit_message(chat_id, message_id, "🪙 Payment received — starting deployment…", None)
        if _launch_github_deploy(chat_id, user_id, step,
                                 main_file_name=step.get('temp_main_file') or None,
                                 paid_method='coins'):
            apply_discount_cashback(user_id, price_coins)
        return

    # ---- GitHub: pay for a monthly/yearly plan with Telegram Stars ----
    if data == "gh_pay_stars":
        step = get_user_step(user_id)
        plan = step.get('temp_plan')
        if step.get('step') != 'awaiting_github_payment' or plan not in ('monthly', 'yearly'):
            return
        days, price_coins, price_stars = _gh_plan_prices(plan)
        payload = f"ghdeploy_{plan}_{user_id}_{int(datetime.now().timestamp())}"
        saved = json.dumps({k: step.get(k) for k in (
            'temp_github_owner', 'temp_github_repo', 'temp_github_branch',
            'temp_github_token', 'temp_env_vars', 'temp_main_file', 'temp_plan')})
        db_execute('''INSERT INTO pending_deployments
            (user_id, chat_id, message_id, temp_file, requirements, env_vars, plan, duration,
             cost_coins, cost_stars, payment_method, payload, created_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (user_id, chat_id, message_id, 'github', saved, '{}', plan, days,
             price_coins, price_stars, 'stars', payload, datetime.now().isoformat(), 'pending'))
        invoice = {
            "chat_id": chat_id,
            "title": f"GitHub Deployment - {plan.capitalize()} Plan",
            "description": f"Deploy {step.get('temp_github_owner')}/{step.get('temp_github_repo')} for {days} days",
            "payload": payload, "currency": "XTR",
            "prices": [{"label": f"{plan.capitalize()} plan", "amount": price_stars}],
        }
        try:
            req = urllib.request.Request(f"{TELEGRAM_API}/sendInvoice",
                data=json.dumps(invoice).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode('utf-8'))
            if res.get('ok'):
                edit_message(chat_id, message_id,
                    f"*⭐ STARS PAYMENT REQUIRED*\n\nPlan: {plan.capitalize()}\n"
                    f"Cost: {price_stars}⭐\n\nComplete the payment above — your repo deploys automatically afterwards.",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
            else:
                send_message(chat_id, f"❌ Failed: {res.get('description')}")
        except Exception as e:
            send_message(chat_id, f"❌ Failed to create invoice: {e}")
        return

    if data == "github_public":
        step = get_user_step(user_id)
        _start_github_deploy(chat_id, user_id, message_id, step, token=None)
        return

    if data == "github_skip_env":
        step = get_user_step(user_id)
        step['temp_env_vars'] = '.'
        _launch_github_deploy(chat_id, user_id, step)
        return

    if data == "github_private":
        step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_github_token',
                      temp_github_owner=step.get('temp_github_owner'),
                      temp_github_repo=step.get('temp_github_repo'),
                      temp_github_branch=step.get('temp_github_branch'))
        edit_message(chat_id, message_id,
            "*🔑 PRIVATE REPO — GitHub Token*\n\n"
            "Send your *GitHub Personal Access Token* (PAT).\n\n"
            "The token needs the `repo` scope.\n"
            "Create one at: `github.com/settings/tokens`\n\n"
            "⚠️ Stored only in `.env` inside the deployment folder, never logged.",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
        return

    if data.startswith("github_main_"):
        # Admin selected main file from multi-file picker
        step = get_user_step(user_id)
        file_name = data[len("github_main_"):]
        _launch_github_deploy(chat_id, user_id, step, main_file_name=file_name)
        return

    # ========== REDEEM CODE ==========
    if data == "redeem_code":
        handle_redeem(chat_id, user_id, message_id)
        return

    # ========== ENV SEND (prompt user to type vars) ==========
    if data == "env_send":
        user_step = get_user_step(user_id)
        # Keep all existing state, just remind the user to type
        env_vars = user_step.get('env_vars') or {}
        if isinstance(env_vars, str):
            try: env_vars = json.loads(env_vars)
            except: env_vars = {}
        set_user_step(user_id, 'awaiting_env',
                     waiting_for_env=1,
                     temp_file=user_step.get('temp_file'),
                     requirements=user_step.get('requirements'),
                     env_vars=env_vars,
                     plan=user_step.get('plan'),
                     duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'),
                     cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'))
        edit_message(chat_id, message_id,
            "*📝 TYPE YOUR VARIABLES*\n\n"
            "Send one or more `KEY=VALUE` pairs, one per line:\n\n"
            "```\nBOT_TOKEN=7712345:AAFabcXYZ\nAPI_KEY=sk-abc123\nDATABASE_URL=sqlite:///bot.db\n```\n\n"
            "Send them now 👇",
            {"inline_keyboard": [
                [{"text": "⏭️ Skip", "callback_data": "env_skip"}],
                [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return

    # ========== REFERRALS ==========
    if data == "my_referral":
        show_referral_menu(chat_id, user_id, message_id)
        return

    # ========== BROADCAST ==========
    if data == "admin_broadcast":
        if not is_admin(user_id): return
        _broadcast_audience.pop(user_id, None)
        show_broadcast_audience_menu(chat_id, message_id)
        return

    if data.startswith("broadcast_aud_"):
        if not is_admin(user_id): return
        key = data[len("broadcast_aud_"):]
        if key not in BROADCAST_AUDIENCES:
            return
        _broadcast_audience[user_id] = key
        edit_message(chat_id, message_id,
            f"*📢 BROADCAST — {BROADCAST_AUDIENCES[key]}*\n"
            f"Recipients: `{len(broadcast_audience_ids(key))}`\n\nChoose message type:",
            {"inline_keyboard": [
                [{"text": "✉️ Text Message",   "callback_data": "broadcast_type_text"}],
                [{"text": "🖼 Photo + Caption", "callback_data": "broadcast_type_photo"}],
                [{"text": "🎬 Video + Caption", "callback_data": "broadcast_type_video"}],
                [{"text": "🎯 Change audience", "callback_data": "admin_broadcast"}],
                [{"text": "🔙 Admin Panel",     "callback_data": "admin_panel"}],
            ]})
        return

    if data == "admin_dashboard":
        if not is_admin(user_id): return
        edit_message(chat_id, message_id, admin_dashboard_text(),
                     {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "admin_dashboard"}],
                                          [{"text": "📊 Host Resources", "callback_data": "admin_resources"}],
                                          [{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_type_text":
        if not is_admin(user_id): return
        set_user_step(user_id, 'awaiting_broadcast_text')
        edit_message(chat_id, message_id,
            "*✉️ TEXT BROADCAST*\n\nType the message to send to all users.\n"
            "Supports *bold*, _italic_, `code`:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_type_photo":
        if not is_admin(user_id): return
        set_user_step(user_id, 'awaiting_broadcast_media', temp_broadcast_type='photo')
        edit_message(chat_id, message_id,
            "*🖼 PHOTO BROADCAST*\n\nSend the photo now:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_type_video":
        if not is_admin(user_id): return
        set_user_step(user_id, 'awaiting_broadcast_media', temp_broadcast_type='video')
        edit_message(chat_id, message_id,
            "*🎬 VIDEO BROADCAST*\n\nSend the video now:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_no_buttons":
        if not is_admin(user_id): return
        step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_broadcast_confirm',
                     temp_broadcast_type=step.get('temp_broadcast_type'),
                     temp_broadcast_file=step.get('temp_broadcast_file', ''),
                     temp_broadcast_caption=step.get('temp_broadcast_caption', ''),
                     temp_broadcast_buttons='[]')
        _send_broadcast_preview(chat_id,
            step.get('temp_broadcast_type'),
            step.get('temp_broadcast_file', ''),
            step.get('temp_broadcast_caption', ''), [])
        return

    if data == "broadcast_edit_caption":
        if not is_admin(user_id): return
        step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_broadcast_caption',
                     temp_broadcast_type=step.get('temp_broadcast_type'),
                     temp_broadcast_file=step.get('temp_broadcast_file', ''))
        send_message(chat_id, "*✏️ New Caption*\n\nSend the new caption text:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_confirm":
        if not is_admin(user_id): return
        step     = get_user_step(user_id)
        btype    = step.get('temp_broadcast_type', 'text')
        file_id  = step.get('temp_broadcast_file', '')
        caption  = step.get('temp_broadcast_caption', '')
        btns_raw = step.get('temp_broadcast_buttons', '[]')
        aud = _broadcast_audience.pop(user_id, 'all')
        set_user_step(user_id, None)
        send_message(chat_id, f"📤 *Sending to {BROADCAST_AUDIENCES.get(aud, 'all users')}…*", None)
        threading.Thread(
            target=_run_broadcast_and_notify,
            args=(chat_id, user_id, btype, file_id, caption, btns_raw, aud),
            daemon=True, name="Broadcast").start()
        return

    # ========== SECURITY REVIEW (admin approve / reject) ==========
    if data.startswith("sec_approve_") or data.startswith("sec_approve_warn_") \
            or data.startswith("sec_reject_") or data.startswith("sec_report_"):

        if not is_admin(user_id):
            answer_callback(callback_id, "⛔ Admins only", show_alert=True)
            return

        # Parse action and review_id from callback data
        if data.startswith("sec_approve_warn_"):
            action     = "approve_warn"
            review_id  = int(data[len("sec_approve_warn_"):])
        elif data.startswith("sec_approve_"):
            action    = "approve"
            review_id = int(data[len("sec_approve_"):])
        elif data.startswith("sec_reject_"):
            action    = "reject"
            review_id = int(data[len("sec_reject_"):])
        else:
            action    = "report"
            review_id = int(data[len("sec_report_"):])

        # Fetch review from DB
        conn_rv = sqlite3.connect(DATABASE_FILE, timeout=10)
        c_rv = conn_rv.cursor()
        c_rv.execute("""SELECT user_id, chat_id, file_name, file_path,
                               critical_json, warnings_json, report_text,
                               source, status, user_step_json
                        FROM security_reviews WHERE review_id=?""", (review_id,))
        row = c_rv.fetchone()

        if not row:
            conn_rv.close()
            edit_message(chat_id, message_id,
                f"❌ Review `#{review_id}` not found.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        (rv_user_id, rv_chat_id, rv_file_name, rv_file_path,
         rv_crit_json, rv_warn_json, rv_report,
         rv_source, rv_status, rv_step_json) = row

        # Show full report
        if action == "report":
            conn_rv.close()
            try:
                crits = json.loads(rv_crit_json or "[]")
                warns = json.loads(rv_warn_json or "[]")
            except Exception:
                crits, warns = [], []
            full = (
                f"📋 *Full Security Report — Review #{review_id}*\n\n"
                f"*Critical ({len(crits)}):*\n"
                + '\n'.join(f"• `{safe_md(c[:150])}`" for c in crits)
                + (f"\n\n*Warnings ({len(warns)}):*\n"
                   + '\n'.join(f"• `{safe_md(w[:150])}`" for w in warns[:10])
                   if warns else "")
            )
            send_message(chat_id, full[:4000],
                {"inline_keyboard": [
                    [{"text": "✅ Approve & Deploy",   "callback_data": f"sec_approve_{review_id}"},
                     {"text": "⚠️ Approve (warn)",      "callback_data": f"sec_approve_warn_{review_id}"}],
                    [{"text": "❌ Reject",              "callback_data": f"sec_reject_{review_id}"}],
                ]})
            return

        # Already actioned?
        if rv_status != "pending":
            conn_rv.close()
            edit_message(chat_id, message_id,
                f"ℹ️ Review `#{review_id}` is already *{rv_status}*.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        # ── REJECT ──────────────────────────────────────────────────────
        if action == "reject":
            c_rv.execute(
                "UPDATE security_reviews SET status='rejected', reviewed_by=?, reviewed_at=? WHERE review_id=?",
                (user_id, datetime.now().isoformat(), review_id))
            conn_rv.commit()
            conn_rv.close()
            # Clean up pending file
            try:
                if rv_file_path and Path(rv_file_path).exists():
                    Path(rv_file_path).unlink()
            except Exception:
                pass
            # Tell the user
            send_message(rv_chat_id,
                f"❌ *Deployment Rejected*\n\n"
                f"An admin reviewed file `{safe_md(rv_file_name)}` "
                f"and decided it cannot be deployed due to security concerns.\n\n"
                f"Please review the security issues and try again with a clean file.\n"
                f"🆔 Review `#{review_id}`",
                {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
            # Update admin message
            edit_message(chat_id, message_id,
                f"✅ Review `#{review_id}` — *Rejected*\n"
                f"User `{rv_user_id}` has been notified.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        # ── APPROVE (with or without warning) ──────────────────────────
        c_rv.execute(
            "UPDATE security_reviews SET status=?, reviewed_by=?, reviewed_at=? WHERE review_id=?",
            (action, user_id, datetime.now().isoformat(), review_id))
        conn_rv.commit()
        conn_rv.close()

        # Notify user of approval
        warn_note = (
            "\n\n⚠️ *Note:* This file triggered security warnings. "
            "Use it responsibly — misuse may result in account suspension."
            if action == "approve_warn" else ""
        )
        if rv_source == "github":
            send_message(rv_chat_id,
                f"✅ *Deployment Approved!*\n\n"
                f"Admin approved `{safe_md(rv_file_name)}`."
                f"{warn_note}\n\n"
                f"🚀 Starting deployment now…",
                {"inline_keyboard": [[{"text": "⏳ Deploying…", "callback_data": "main_menu"}]]})
            _admin_note = f"Deploying for user `{rv_user_id}`…"
        else:
            # Uploads still need requirements / env vars / payment from the user — see below.
            send_message(rv_chat_id,
                f"✅ *Deployment Approved!*\n\n"
                f"Admin approved `{safe_md(rv_file_name)}`."
                f"{warn_note}\n\n"
                f"➡️ Continue setting it up in the next message.",
                {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
            _admin_note = f"User `{rv_user_id}` has been asked to finish setup (requirements, env vars)."

        # Update admin message immediately
        edit_message(chat_id, message_id,
            f"✅ Review `#{review_id}` — *Approved*\n"
            f"{_admin_note}",
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})

        # Run the actual deployment in a background thread
        def _run_approved_deployment(
                rv_id=review_id, rv_uid=rv_user_id, rv_cid=rv_chat_id,
                rv_fn=rv_file_name, rv_fp=rv_file_path,
                rv_src=rv_source, rv_step=rv_step_json):
            try:
                step = json.loads(rv_step) if rv_step else {}

                if rv_src == "github":
                    # Re-run GitHub deployment without the security block
                    # `step` was stored by deploy_from_github itself (owner/repo/
                    # branch/plan/...), not the temp_* keys _launch_github_deploy
                    # reads, so call deploy_from_github directly with them.
                    deploy_from_github(
                        rv_cid, rv_uid,
                        step.get('owner', ''), step.get('repo', ''),
                        step.get('branch') or 'main', step.get('token') or None,
                        step.get('env_vars') or {},
                        plan=step.get('plan') or 'free',
                        duration=step.get('duration') or FREE_DEPLOYMENT_DURATION_HOURS,
                        cost_coins=step.get('cost_coins') or 0,
                        cost_stars=step.get('cost_stars') or 0,
                        payment_method=step.get('payment_method') or 'free',
                        is_free=bool(step.get('is_free')),
                        main_file_name=None,
                        skip_security=True)

                else:
                    # File-upload path — read saved pending file
                    pending = Path(rv_fp) if rv_fp else None
                    if not pending or not pending.exists():
                        send_message(rv_cid,
                            "❌ Approved file no longer on disk.\n"
                            "Please upload your file again.",
                            {"inline_keyboard": [[{"text": "🏠 Menu",
                                                   "callback_data": "main_menu"}]]})
                        return
                    # Save to temp path the normal deploy flow expects
                    temp_path = BASE_DIR / f"temp_{rv_uid}_{sanitize_upload_filename(rv_fn)}"
                    temp_path.write_bytes(pending.read_bytes())
                    pending.unlink(missing_ok=True)

                    # The security scan runs right after the upload — BEFORE the user has been
                    # asked for requirements, environment variables or payment. So there is
                    # nothing to deploy yet: deploying here would start the bot with NO env
                    # vars (empty .env, no BOT_TOKEN) and always as a free 24h bot whatever
                    # plan they chose. Put the user back at exactly the point a clean upload
                    # reaches, and let the normal flow take over:
                    #   requirements → env vars → DEPLOY NOW → plan / payment → deploy.
                    set_user_step(rv_uid, 'awaiting_reqs',
                                  temp_file=str(temp_path),
                                  plan=step.get('plan'),
                                  duration=step.get('duration'),
                                  cost_coins=step.get('cost_coins'),
                                  cost_stars=step.get('cost_stars'),
                                  payment_method=step.get('payment_method'),
                                  env_vars={})

                    _lang = {
                        '.py': '🐍 Python', '.js': '🟨 Node.js', '.mjs': '🟨 Node.js (ESM)',
                        '.cjs': '🟨 Node.js (CJS)', '.ts': '🔷 TypeScript',
                        '.tsx': '🔷 TypeScript/React', '.jsx': '🟨 JavaScript/React',
                    }.get(temp_path.suffix.lower(), '📄 Script')
                    send_message(rv_cid,
                        f"*✅ File approved* — {_lang}\n\n"
                        f"📁 `{safe_md(rv_fn)}`\n"
                        f"🔒 An admin reviewed and approved this file.\n\n"
                        f"📋 Plan: `{(step.get('plan') or 'free').upper()}`\n"
                        f"⏱️ Duration: `{step.get('duration', 'N/A')}` days\n\n"
                        "*📦 Add requirements?*\nUpload `requirements.txt` or `package.json`, "
                        "or click Auto-detect — then you'll add your environment variables.",
                        get_reqs_keyboard())

            except Exception as e:
                print(f"❌ Approved deployment failed (review #{rv_id}): {e}")
                send_message(rv_cid,
                    f"❌ Deployment failed after approval:\n`{e}`\n\n"
                    "Please try deploying your file again.",
                    {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})

        threading.Thread(target=_run_approved_deployment, daemon=True,
                         name=f"SecApprove-{review_id}").start()
        return

    # ========== BUG REPORT (user) ==========
    if data == "report_bug":
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, "User", message_id)
            return
        set_user_step(user_id, 'awaiting_bug_report')
        edit_message(chat_id, message_id,
            "*🐛 REPORT A BUG*\n\n"
            "Please describe the issue you're experiencing in as much detail as possible:\n\n"
            "• What were you trying to do?\n"
            "• What happened instead?\n"
            "• Any error messages?\n\n"
            "Type your report and send it:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
        return

    # ========== ADMIN BUG REPORTS ==========
    if data == "admin_sec_reviews":
        if not is_admin(user_id):
            return
        try:
            conn_sr = sqlite3.connect(DATABASE_FILE, timeout=10)
            c_sr = conn_sr.cursor()
            c_sr.execute("""SELECT review_id, user_id, file_name, source,
                                   status, created_at
                            FROM security_reviews
                            ORDER BY created_at DESC LIMIT 20""")
            reviews = c_sr.fetchall()
            pending_count = conn_sr.execute(
                "SELECT COUNT(*) FROM security_reviews WHERE status='pending'").fetchone()[0]
            conn_sr.close()
        except Exception as e:
            send_message(chat_id, f"❌ DB error: {e}")
            return

        if not reviews:
            edit_message(chat_id, message_id,
                "📋 *Security Reviews*\n\nNo reviews yet.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        icons = {"pending": "⏳", "approve": "✅", "approve_warn": "⚠️", "rejected": "❌"}
        text = f"🔒 *Security Reviews* ({pending_count} pending)\n\n"
        buttons = []
        for rv_id, rv_uid, rv_fn, rv_src, rv_status, rv_at in reviews:
            icon = icons.get(rv_status, "❓")
            dt   = datetime.fromisoformat(rv_at).strftime("%d/%m %H:%M") if rv_at else "?"
            src_icon = "🐙" if rv_src == "github" else "📤"
            text += f"{icon} *#{rv_id}* {src_icon} `{safe_md(rv_fn[:30])}` — user `{rv_uid}` — {dt}\n"
            if rv_status == "pending":
                buttons.append([
                    {"text": f"✅ #{rv_id}",  "callback_data": f"sec_approve_{rv_id}"},
                    {"text": f"⚠️ #{rv_id}",  "callback_data": f"sec_approve_warn_{rv_id}"},
                    {"text": f"❌ #{rv_id}",  "callback_data": f"sec_reject_{rv_id}"},
                ])

        buttons.append([{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}])
        edit_message(chat_id, message_id, text[:3900], {"inline_keyboard": buttons})
        return

    if data == "admin_bug_reports":
        if is_admin(user_id):
            show_admin_bug_reports(chat_id, user_id, message_id)
        return

    if data == "admin_referral_stats":
        if not is_admin(user_id):
            return
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute("""SELECT r.referrer_id, u.username, u.first_name, COUNT(*) cnt,
                                COALESCE(SUM(r.reward_coins),0) earned
                         FROM referrals r
                         LEFT JOIN users u ON u.user_id = r.referrer_id
                         GROUP BY r.referrer_id ORDER BY cnt DESC LIMIT 15""")
            rows = c.fetchall()
            total = c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
            coins_paid = c.execute("SELECT COALESCE(SUM(reward_coins),0) FROM referrals WHERE reward_given=1").fetchone()[0]
            conn.close()

            text = (f"*👥 REFERRAL STATS*\n\n"
                    f"Total referrals: `{total}`\n"
                    f"Coins paid out:  `{coins_paid}🪙`\n"
                    f"Reward per ref:  `{REFERRAL_REWARD_COINS}🪙`\n\n"
                    f"*Top Referrers:*\n")
            for i, (rid, uname, fname, cnt, earned) in enumerate(rows, 1):
                who = f"@{uname}" if uname else (fname or f"#{rid}")
                text += f"{i}. {who} — `{cnt}` refs → `{earned}🪙`\n"
            if not rows:
                text += "_No referrals yet_"
        except Exception as e:
            text = f"❌ Error: {e}"
        edit_message(chat_id, message_id, text,
                     {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
        return

    if data.startswith("continue_as_free_"):
        dep_id = int(data.split("_")[3])
        # Verify ownership
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id FROM deployments WHERE deployment_id=?", (dep_id,))
        row = c.fetchone()
        conn.close()
        if row and (row[0] == user_id or is_admin(user_id)):
            continue_deployment_as_free(dep_id, row[0], chat_id)
        else:
            send_message(chat_id, "❌ Permission denied")
        return

    if data.startswith("bug_reply_"):
        if is_admin(user_id):
            report_id = int(data.split("_")[2])
            set_user_step(user_id, f'awaiting_bug_reply_{report_id}')
            # Get report info
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute("SELECT user_id, username, first_name, message FROM bug_reports WHERE report_id = ?", (report_id,))
            row = c.fetchone()
            conn.close()
            if row:
                who = f"@{row[1]}" if row[1] else f"User {row[0]}"
                preview = row[3][:100]
                edit_message(chat_id, message_id,
                    f"*✉️ REPLY TO REPORT #{report_id}*\n\n"
                    f"From: {who}\n"
                    f"Message: _{preview}_\n\n"
                    f"Type your reply and send it:",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_bug_reports"}]]})
            else:
                edit_message(chat_id, message_id, "❌ Report not found.",
                             {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_bug_reports"}]]})
        return

    if data.startswith("bug_close_"):
        if is_admin(user_id):
            report_id = int(data.split("_")[2])
            conn = sqlite3.connect(DATABASE_FILE)
            conn.execute("UPDATE bug_reports SET status='closed' WHERE report_id = ?", (report_id,))
            conn.commit()
            conn.close()
            show_admin_bug_reports(chat_id, user_id, message_id)
        return

# ==================== USER STEP FUNCTIONS ====================
def set_user_step(user_id, step, **kwargs):
    """
    Persist workflow state for a user.
    Known high-frequency fields are stored in dedicated columns for query speed.
    ALL kwargs are also stored in pending_json so nothing is ever silently discarded.
    When step=None (clear state) pending_json is fully reset to prevent
    stale state (e.g. temp_source='github') from leaking into future flows.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

    _KNOWN = {'temp_file', 'requirements', 'env_vars', 'plan', 'payment_method',
              'duration', 'cost_coins', 'cost_stars', 'waiting_for_env',
              'waiting_for_reqs', 'waiting_for_redeem', 'temp_target_user',
              'temp_coins_amount', 'temp_stars_amount', 'temp_expiry', 'temp_reward_type'}

    updates = ["step = ?"]
    values  = [step]

    for key in _KNOWN:
        if key in kwargs:
            updates.append(f"{key} = ?")
            val = kwargs[key]
            if key == 'env_vars' and val is not None and not isinstance(val, str):
                val = json.dumps(val)
            values.append(val)

    # When clearing state, wipe pending_json entirely so no stale keys remain
    if step is None:
        pending = json.dumps({})
    else:
        c.execute("SELECT pending_json FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        try:
            existing = json.loads(row[0] or '{}') if row else {}
        except Exception:
            existing = {}
        existing.update(kwargs)
        existing['_step'] = step
        pending = json.dumps(existing)

    updates.append("pending_json = ?")
    values.append(pending)

    values.append(user_id)
    c.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", values)
    conn.commit()
    conn.close()


def get_user_step(user_id):
    """
    Return full workflow state for a user.
    Merges dedicated column values and pending_json into one dict so callers
    can read any kwarg that was ever passed to set_user_step.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT step, temp_file, requirements, env_vars, plan, payment_method,
                        duration, cost_coins, cost_stars, waiting_for_env,
                        waiting_for_reqs, waiting_for_redeem, temp_target_user,
                        temp_coins_amount, temp_stars_amount, temp_expiry,
                        temp_reward_type, pending_json
                 FROM users WHERE user_id = ?""", (user_id,))
    row = c.fetchone()
    conn.close()

    _DEFAULTS = {
        'step': None, 'temp_file': None, 'requirements': None, 'env_vars': {},
        'plan': None, 'payment_method': None, 'duration': None,
        'cost_coins': None, 'cost_stars': None,
        'waiting_for_env': 0, 'waiting_for_reqs': 0, 'waiting_for_redeem': 0,
        'temp_target_user': None, 'temp_coins_amount': None,
        'temp_stars_amount': None, 'temp_expiry': None, 'temp_reward_type': None,
    }

    if not row:
        return _DEFAULTS.copy()

    # Parse pending_json first (lower priority)
    try:
        pj = json.loads(row[17] or '{}')
    except Exception:
        pj = {}

    # Build result: pending_json as base, then overlay dedicated columns
    result = {**_DEFAULTS, **pj}

    col_map = ['step','temp_file','requirements','env_vars','plan','payment_method',
               'duration','cost_coins','cost_stars','waiting_for_env','waiting_for_reqs',
               'waiting_for_redeem','temp_target_user','temp_coins_amount',
               'temp_stars_amount','temp_expiry','temp_reward_type']

    for i, key in enumerate(col_map):
        val = row[i]
        if val is None:
            continue
        if key == 'env_vars':
            try:
                val = json.loads(val)
            except Exception:
                val = {}
        elif key in ('waiting_for_env','waiting_for_reqs','waiting_for_redeem'):
            val = int(val or 0)
        result[key] = val

    return result

# ==================== MESSAGE HANDLER ====================
def handle_message(message):
    touch_user(message.get('from') or {})
    chat_id = message['chat']['id']
    user_id = message['from']['id']
    first_name = message['from'].get('first_name', 'User')
    _ban = get_ban_info(user_id)
    if _ban:
        send_message(chat_id, banned_text(_ban))
        return

    
    print(f"📨 Message from {first_name} ({user_id})")
    
    if 'text' in message and message['text'].startswith('/start'):
        parts = message['text'].split(maxsplit=1)
        start_param = parts[1].strip() if len(parts) > 1 else ""
        handle_start(chat_id, user_id, message['from'].get('username', ''), first_name, start_param)
        return
    
    user_step = get_user_step(user_id)

    if 'text' in message and _fp_text(message, user_step):
        return
    
    if 'text' in message:
        text = message['text']

        # ── Edit deployment: env / requirements text ─────────────────
        if user_step.get('step') == 'edit_env' and user_step.get('edit_dep'):
            handle_edit_env_text(chat_id, user_id, text, message.get('message_id'), int(user_step['edit_dep']))
            return
        if user_step.get('step') == 'edit_reqs' and user_step.get('edit_dep'):
            handle_edit_reqs_text(chat_id, user_id, text, int(user_step['edit_dep']))
            return

        # ── Admin: ban by user ID (button flow) ──────────────────────
        if user_step.get('step') == 'awaiting_ban_id':
            set_user_step(user_id, None)
            if is_admin(user_id):
                q = text.strip()
                if q.lstrip('-').isdigit() and db_execute("SELECT 1 FROM users WHERE user_id = ?", (int(q),), fetch='one'):
                    admin_prompt_ban(chat_id, None, user_id, int(q))
                else:
                    send_message(chat_id, "❌ No user with that ID.",
                                 {"inline_keyboard": [[{"text": "➕ Try again", "callback_data": "admin_ban_by_id"}],
                                                      [{"text": "🚫 Banned list", "callback_data": "admin_banned_list"}]]})
            return

        # ── Admin: ban reason ────────────────────────────────────────
        if user_step.get('step') == 'awaiting_ban_reason':
            tgt = user_step.get('ban_target')
            set_user_step(user_id, None)
            if is_admin(user_id) and tgt:
                ok, msg = ban_user(user_id, int(tgt), text)
                send_message(chat_id, msg, {"inline_keyboard": [[{"text": "👤 User", "callback_data": f"admin_user_view_{tgt}"}]]})
            return

        # ── Admin: search a user ─────────────────────────────────────
        if user_step.get('step') == 'awaiting_admin_user_search':
            if is_admin(user_id):
                admin_handle_user_search(chat_id, user_id, text)
            else:
                set_user_step(user_id, None)
            return

        # ── GitHub deploy: env vars (from _start_github_deploy) ──────
        if user_step.get('step') == 'awaiting_env_vars' and user_step.get('temp_source') == 'github':
            user_step['temp_env_vars'] = text.strip()
            _launch_github_deploy(chat_id, user_id, user_step)
            return

        # ── GitHub deploy: URL ───────────────────────────────────────
        if user_step.get('step') == 'awaiting_github_url':
            owner, repo, branch = parse_github_url(text.strip())
            if not owner or not repo:
                send_message(chat_id,
                    "❌ Couldn't parse that URL. Try `owner/repo` or full GitHub URL:",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
                return
            set_user_step(user_id, 'awaiting_github_visibility',
                          temp_github_owner=owner,
                          temp_github_repo=repo,
                          temp_github_branch=branch or '')
            send_message(chat_id,
                f"*🐙 Repo detected:* `{owner}/{repo}`"
                + (f"\n*Branch:* `{branch}`" if branch else ""),
                {"inline_keyboard": [
                    [{"text": "🌐 Public repo",  "callback_data": "github_public"}],
                    [{"text": "🔒 Private repo", "callback_data": "github_private"}],
                    [{"text": "❌ Cancel",        "callback_data": "main_menu"}],
                ]})
            return

        # ── GitHub deploy: PAT for private repo ──────────────────────
        if user_step.get('step') == 'awaiting_github_token':
            token_val = text.strip()
            if not token_val.startswith('gh'):
                send_message(chat_id,
                    "⚠️ That doesn't look like a GitHub token (should start with `ghp_` or `github_pat_`).\n"
                    "Send it again or cancel:",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
                return
            step = get_user_step(user_id)
            step['temp_github_token'] = token_val
            _start_github_deploy(chat_id, user_id, None, step, token=token_val)
            return

        # ── Bug report submission ────────────────────────────────────
        if user_step.get('step') == 'awaiting_bug_report':
            if text.strip():
                uname = message['from'].get('username', '')
                report_id = submit_bug_report(user_id, uname, first_name, text.strip())
                set_user_step(user_id, None)
                send_message(chat_id,
                    f"✅ *Bug Report #{report_id} Submitted!*\n\n"
                    f"Thank you for your report. An admin will review it and reply to you here.\n\n"
                    f"You'll receive a notification when there's a reply.",
                    {"inline_keyboard": [[{"text": "🏠 Main Menu", "callback_data": "main_menu"}]]})
            else:
                send_message(chat_id, "❌ Report cannot be empty. Please describe the bug:",
                             {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
            return

        # ── Admin replying to a bug report ───────────────────────────
        if is_admin(user_id) and (user_step.get('step') or '').startswith('awaiting_bug_reply_'):
            report_id = int(user_step['step'].split('_')[-1])
            if text.strip():
                ok, result = reply_to_bug_report(report_id, user_id, text.strip())
                set_user_step(user_id, None)
                if ok:
                    send_message(chat_id,
                        f"✅ Reply sent to user `{result}` for report #{report_id}",
                        {"inline_keyboard": [
                            [{"text": "📋 All Reports", "callback_data": "admin_bug_reports"}],
                            [{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
                else:
                    send_message(chat_id, f"❌ Error: {result}")
            else:
                send_message(chat_id, "❌ Reply cannot be empty.")
            return

        # ── Redeem code ──────────────────────────────────────────────
        if user_step.get('waiting_for_redeem') == 1 and text and not text.startswith('/'):
            process_redeem(chat_id, user_id, text)
            return

        # ── Admin coins: awaiting target user(s) ─────────────────────
        # Accepts one user ID per line, so an admin can add coins to many
        # users in a single operation instead of repeating the flow.
        if is_admin(user_id) and user_step.get('step') == 'awaiting_coins_target':
            lines = [ln.strip().lstrip('@') for ln in text.strip().split('\n')]
            lines = [ln for ln in lines if ln]

            valid_ids, invalid_lines = [], []
            for ln in lines:
                if ln.isdigit():
                    valid_ids.append(int(ln))
                else:
                    invalid_lines.append(ln)

            if not valid_ids:
                send_message(chat_id,
                    "❌ No valid numeric user ID found. Send one user ID per "
                    "line (usernames aren't supported here — use the numeric "
                    "Telegram user ID).")
                return

            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute(f"SELECT user_id FROM users WHERE user_id IN ({','.join('?' * len(valid_ids))})",
                      valid_ids)
            found_ids = {row[0] for row in c.fetchall()}
            conn.close()
            not_found = [uid for uid in valid_ids if uid not in found_ids]
            found = [uid for uid in valid_ids if uid in found_ids]

            if not found:
                send_message(chat_id,
                    f"❌ None of those user ID(s) were found in the database:\n"
                    f"`{', '.join(str(u) for u in valid_ids)}`\n\n"
                    f"They need to have started the bot at least once. Try again.")
                return

            target_payload = json.dumps(found) if len(found) > 1 else str(found[0])
            set_user_step(user_id, 'awaiting_coins_amount', temp_target_user=target_payload)

            warn = ""
            if not_found:
                warn += f"\n⚠️ Not found, skipped: `{', '.join(str(u) for u in not_found)}`"
            if invalid_lines:
                warn += f"\n⚠️ Not numeric, skipped: `{', '.join(invalid_lines[:10])}`"

            target_desc = f"`{found[0]}`" if len(found) == 1 else f"*{len(found)} users*"
            send_message(chat_id,
                f"🪙 *Add coins to {target_desc}*{warn}\n\nEnter amount or pick preset:",
                {"inline_keyboard": [
                    [{"text": "50",   "callback_data": "coin_preset_50"},
                     {"text": "100",  "callback_data": "coin_preset_100"},
                     {"text": "500",  "callback_data": "coin_preset_500"}],
                    [{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
            return

        # ── Admin coins: awaiting amount ──────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_coins_amount':
            try:
                amount = int(text.strip())
                if amount <= 0:
                    raise ValueError("non-positive")
                target = user_step.get('temp_target_user', '?')
                set_user_step(user_id, 'awaiting_coins_confirm',
                             temp_target_user=target, temp_coins_amount=amount)
                send_message(chat_id,
                    f"Confirm: add *{amount}🪙* to `{target}`?",
                    {"inline_keyboard": [
                        [{"text": "✅ Confirm", "callback_data": "coin_confirm"},
                         {"text": "❌ Cancel",  "callback_data": "admin_panel"}]]})
            except (ValueError, TypeError):
                send_message(chat_id, "❌ Send a positive whole number (e.g. `50`).")
            return

        # ── Redeem code: custom max uses ──────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_code_uses_custom':
            try:
                max_uses = int(text.strip())
                if max_uses < 0:
                    raise ValueError("negative")
                ask_code_expiry(user_id, max_uses, chat_id, None)
            except (ValueError, TypeError):
                send_message(chat_id, "❌ Send a whole number (`0` for unlimited).")
            return

        # ── Redeem code: custom expiry ─────────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_code_expiry_custom':
            try:
                expiry_days = int(text.strip())
                if expiry_days < 0:
                    raise ValueError("negative")
                if expiry_days == 0:
                    expiry_days = 36500  # "never" — a century is a practical forever
                finalize_create_code(user_id, expiry_days, chat_id, None)
            except (ValueError, TypeError):
                send_message(chat_id, "❌ Send a whole number of days (`0` for never expires).")
            return

        # ── Broadcast steps (admin) ───────────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_caption':
            set_user_step(user_id, 'awaiting_broadcast_buttons',
                         temp_broadcast_caption=text.strip(),
                         temp_broadcast_type=user_step.get('temp_broadcast_type'),
                         temp_broadcast_file=user_step.get('temp_broadcast_file'))
            send_message(chat_id,
                "*🔘 Inline Buttons (Optional)*\n\n"
                "Send buttons in this format (one per line):\n"
                "`Button Text | https://url.com`\n\n"
                "Or skip for no buttons:",
                {"inline_keyboard": [
                    [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                    [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_buttons':
            buttons = []
            for line in text.strip().splitlines():
                if '|' in line:
                    label, _, url = line.partition('|')
                    label = label.strip(); url = url.strip()
                    if label and url.startswith('http'):
                        buttons.append([{"text": label, "url": url}])
            set_user_step(user_id, 'awaiting_broadcast_confirm',
                         temp_broadcast_type=user_step.get('temp_broadcast_type'),
                         temp_broadcast_file=user_step.get('temp_broadcast_file'),
                         temp_broadcast_caption=user_step.get('temp_broadcast_caption', ''),
                         temp_broadcast_buttons=json.dumps(buttons))
            _send_broadcast_preview(chat_id, user_step.get('temp_broadcast_type'),
                                    user_step.get('temp_broadcast_file'),
                                    user_step.get('temp_broadcast_caption', ''),
                                    buttons)
            return

        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_text':
            set_user_step(user_id, 'awaiting_broadcast_buttons',
                         temp_broadcast_type='text',
                         temp_broadcast_file='',
                         temp_broadcast_caption=text.strip())
            send_message(chat_id,
                "*🔘 Inline Buttons (Optional)*\n\nFormat: `Label | https://url`\n\nOr skip:",
                {"inline_keyboard": [
                    [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                    [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

        # ── Env vars (waiting_for_env=1 OR step in awaiting_env* ) ───
        _wants_env = (
            user_step.get('waiting_for_env') == 1 or
            user_step.get('step') in ('awaiting_env', 'awaiting_env_vars')
        )
        if _wants_env and text and not text.startswith('/'):
            # GitHub path
            if user_step.get('temp_source') == 'github':
                user_step['temp_env_vars'] = text.strip()
                _launch_github_deploy(chat_id, user_id, user_step)
                return
            # Regular path – initialise env_vars safely
            raw = user_step.get('env_vars') or {}
            if isinstance(raw, str):
                try:    raw = json.loads(raw)
                except Exception: raw = {}
            env_vars = dict(raw)
            _parsed, _ignored = parse_env_input(text)
            env_vars.update(_parsed)
            added = len(_parsed)
            set_user_step(user_id, 'awaiting_env',
                         waiting_for_env=1,
                         temp_file=user_step.get('temp_file'),
                         requirements=user_step.get('requirements'),
                         env_vars=env_vars,
                         plan=user_step.get('plan'),
                         duration=user_step.get('duration'),
                         cost_coins=user_step.get('cost_coins'),
                         cost_stars=user_step.get('cost_stars'),
                         payment_method=user_step.get('payment_method'))
            if added:
                _ign_note = (f"\n⚠️ `{_ignored}` line(s) not understood and skipped "
                             f"(use one `KEY=VALUE` per line)." if _ignored else "")
                send_message(chat_id,
                    f"✅ *{added}* variable(s) saved — total: `{len(env_vars)}`\n"
                    f"{env_names_line(env_vars)}{_ign_note}\nSend more, or:",
                    get_env_keyboard(len(env_vars)))
            else:
                send_message(chat_id,
                    "❌ No valid `KEY=VALUE` pairs found.\n\n"
                    "Format (one per line):\n```\nBOT_TOKEN=123:ABC\nAPI_KEY=xyz\n```",
                    get_env_keyboard(len(env_vars)))
            return

        # ── Waiting for requirements file ─────────────────────────────
        if user_step.get('waiting_for_reqs') == 1 and text and not text.startswith('/'):
            send_message(chat_id,
                "📦 Please *upload* your `requirements.txt` file, or click *Auto-detect*.",
                get_reqs_keyboard())
            return
        
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, first_name, None)
            return
        
        send_message(chat_id, "❌ Unknown command. Use buttons below.", get_main_menu(user_id))
        return
    
    # Handle photo upload (broadcast)
    if 'photo' in message:
        user_step = get_user_step(user_id)
        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_media' and user_step.get('temp_broadcast_type') == 'photo':
            # Pick highest-resolution photo
            file_id = message['photo'][-1]['file_id']
            caption = message.get('caption', '')
            set_user_step(user_id, 'awaiting_broadcast_caption',
                         temp_broadcast_type='photo',
                         temp_broadcast_file=file_id,
                         temp_broadcast_caption=caption)
            if caption:
                # Caption included — go straight to buttons step
                set_user_step(user_id, 'awaiting_broadcast_buttons',
                             temp_broadcast_type='photo',
                             temp_broadcast_file=file_id,
                             temp_broadcast_caption=caption)
                send_message(chat_id,
                    f"✅ Photo received! Caption: _{caption[:80]}_\n\n"
                    "*🔘 Add inline buttons?* Format: `Label | https://url`\n\nOr skip:",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            else:
                send_message(chat_id,
                    "✅ Photo received!\n\n*Add a caption* (or skip):",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Caption", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

    # Handle video upload (broadcast)
    if 'video' in message:
        user_step = get_user_step(user_id)
        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_media' and user_step.get('temp_broadcast_type') == 'video':
            file_id = message['video']['file_id']
            caption = message.get('caption', '')
            if caption:
                set_user_step(user_id, 'awaiting_broadcast_buttons',
                             temp_broadcast_type='video',
                             temp_broadcast_file=file_id,
                             temp_broadcast_caption=caption)
                send_message(chat_id,
                    f"✅ Video received! Caption: _{caption[:80]}_\n\n"
                    "*🔘 Add inline buttons?* Format: `Label | https://url`\n\nOr skip:",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            else:
                set_user_step(user_id, 'awaiting_broadcast_caption',
                             temp_broadcast_type='video',
                             temp_broadcast_file=file_id)
                send_message(chat_id,
                    "✅ Video received!\n\n*Add a caption* (or skip):",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Caption", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

    # Handle file upload
    if 'document' in message:
        doc = message['document']
        file_name = sanitize_upload_filename(doc.get('file_name', 'unknown'), default='unknown')
        file_size = doc.get('file_size', 0)
        print(f"📁 File: {file_name} ({format_file_size(file_size)})")

        # ── Admin database restore — checked first, before the normal bot-file
        # size/type gates, since a .db backup isn't subject to those limits ──
        if is_admin(user_id) and user_step.get('step') == 'awaiting_db_restore':
            handle_db_restore_upload(message, user_id, chat_id)
            return
        
        if file_size > MAX_FILE_SIZE_BYTES:
            send_message(chat_id, f"❌ File too large! Max {MAX_FILE_SIZE_MB}MB")
            return
        
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, first_name, None)
            return

        if maintenance_on() and not is_admin(user_id):
            send_message(chat_id, MAINT_TEXT)
            return
        if _fp_document(message, user_step, doc, file_name):
            return

        # ── Edit deployment: new requirements / package.json / code file ──
        if user_step.get('step') == 'edit_reqs' and user_step.get('edit_dep'):
            handle_edit_reqs_document(chat_id, user_id, doc, file_name, int(user_step['edit_dep']))
            return
        if user_step.get('step') == 'edit_code' and user_step.get('edit_dep'):
            handle_edit_code_document(chat_id, user_id, doc, file_name, int(user_step['edit_dep']))
            return
        
        # Requirements file — accept requirements.txt/.txt (Python) or
        # package.json/.json (Node.js) when waiting for a manifest upload
        if user_step.get('waiting_for_reqs') == 1 and (
                file_name == 'requirements.txt' or
                file_name == 'package.json' or
                file_name.endswith('.json') or
                (file_name.endswith('.txt') and 'req' in file_name.lower()) or
                file_name.endswith('.txt')):
            file_id = doc['file_id']
            file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
            if file_info and file_info.get('ok'):
                file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
                try:
                    with urllib.request.urlopen(file_url) as resp:
                        requirements_text = resp.read().decode('utf-8', errors='replace')
                    set_user_step(user_id, 'awaiting_env',
                                 waiting_for_env=1, waiting_for_reqs=0,
                                 temp_file=user_step.get('temp_file'),
                                 requirements=requirements_text,
                                 env_vars=user_step.get('env_vars') or {},
                                 plan=user_step.get('plan'),
                                 duration=user_step.get('duration'),
                                 cost_coins=user_step.get('cost_coins'),
                                 cost_stars=user_step.get('cost_stars'),
                                 payment_method=user_step.get('payment_method'))
                    is_json_manifest = requirements_text.strip().startswith('{')
                    if is_json_manifest:
                        try:
                            _pkg_preview = json.loads(requirements_text)
                            pkg_count = (len(_pkg_preview.get('dependencies', {})) +
                                         len(_pkg_preview.get('devDependencies', {})))
                        except Exception:
                            pkg_count = 0
                    else:
                        pkg_count = len([l for l in requirements_text.splitlines()
                                         if l.strip() and not l.startswith('#')])
                    send_message(chat_id,
                        f"✅ *{'package.json' if is_json_manifest else 'Requirements'} received!* "
                        f"({pkg_count} package(s))\n\n"
                        f"```\n{requirements_text[:400]}\n```\n\n"
                        "Now set environment variables, or deploy directly:",
                        get_env_keyboard(0))
                except Exception as e:
                    send_message(chat_id, f"❌ Error reading file: {e}")
            else:
                send_message(chat_id, "❌ Could not download file from Telegram.")
            return
        
        # Python / JavaScript / TypeScript bot file for deployment
        if user_step.get('step') == 'awaiting_file':
            ALLOWED_BOT_EXTS = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx'}
            ext = os.path.splitext(file_name)[1].lower()

            if ext not in ALLOWED_BOT_EXTS:
                send_message(chat_id,
                    f"❌ Unsupported file type `{ext}`.\n\n"
                    "Supported:\n"
                    "• `.py` — Python bots (Telegram, Discord, WhatsApp…)\n"
                    "• `.js` / `.mjs` — Node.js bots\n"
                    "• `.ts` / `.tsx` / `.jsx` — TypeScript / JSX bots\n\n"
                    "For Node.js projects with multiple files, zip them and send the `.zip`.")
                return

            scan_msg = send_message(chat_id, "🔒 *Running 6-layer security scan…*", None)
            scan_msg_id = (scan_msg or {}).get('result', {}).get('message_id')

            file_id   = doc['file_id']
            file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})

            if not file_info or not file_info.get('ok'):
                send_message(chat_id, "❌ Failed to download file from Telegram.")
                return

            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
            try:
                with urllib.request.urlopen(file_url) as resp:
                    file_bytes_content = resp.read()
            except Exception as dl_err:
                send_message(chat_id, f"❌ Download error: {dl_err}")
                return

            # ── Security scan ─────────────────────────────────────────
            blocked, critical, scan_warnings, report = run_security_scan(
                file_bytes_content, file_name)

            _theft = data_theft_findings(critical)
            if _theft:
                set_user_step(user_id, None)
                reject_data_theft(chat_id, user_id, file_name, _theft, scan_msg_id)
                return

            if blocked:
                # Save blocked file to disk so admin can approve and deploy it later
                pending_path = BASE_DIR / f"pending_review_{user_id}_{file_name}"
                try:
                    pending_path.write_bytes(file_bytes_content)
                except Exception:
                    pending_path = None

                # Store review record in DB
                current_step_json = json.dumps(user_step)
                conn_r = sqlite3.connect(DATABASE_FILE, timeout=10)
                c_r = conn_r.cursor()
                c_r.execute("""INSERT INTO security_reviews
                    (user_id, chat_id, file_name, file_path, critical_json,
                     warnings_json, report_text, source, status, created_at, user_step_json)
                    VALUES (?,?,?,?,?,?,?,'upload','pending',?,?)""",
                    (user_id, chat_id, file_name,
                     str(pending_path) if pending_path else '',
                     json.dumps(critical), json.dumps(scan_warnings),
                     report, datetime.now().isoformat(), current_step_json))
                review_id = c_r.lastrowid
                conn_r.commit()
                conn_r.close()

                # Tell the user their file is under review
                review_text = (
                    f"🔒 *SECURITY REVIEW REQUIRED*\n\n"
                    f"Your file `{safe_md(file_name)}` triggered security checks:\n\n"
                    f"{report}\n\n"
                    f"Your file has been sent to an admin for review.\n"
                    f"You'll be notified once a decision is made.\n"
                    f"🆔 Review ID: `{review_id}`"
                )
                if scan_msg_id:
                    edit_message(chat_id, scan_msg_id, review_text,
                                 {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
                else:
                    send_message(chat_id, review_text,
                                 {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})

                # Notify every admin with Approve / Reject / Approve with Warning buttons
                admin_text = (
                    f"🚨 *SECURITY REVIEW #{review_id}*\n\n"
                    f"👤 User: `{user_id}`\n"
                    f"📁 File: `{safe_md(file_name)}`\n"
                    f"⚠️ Critical issues: `{len(critical)}`\n"
                    f"🔔 Warnings: `{len(scan_warnings)}`\n\n"
                    f"*Issues found:*\n"
                    + '\n'.join(f"• `{safe_md(c[:120])}`" for c in critical[:8])
                    + (f"\n_...and {len(critical)-8} more_" if len(critical) > 8 else "")
                    + f"\n\n*What would you like to do?*"
                )
                admin_kb = {"inline_keyboard": [
                    [{"text": "✅ Approve & Deploy",
                      "callback_data": f"sec_approve_{review_id}"},
                     {"text": "⚠️ Approve (warn user)",
                      "callback_data": f"sec_approve_warn_{review_id}"}],
                    [{"text": "❌ Reject",
                      "callback_data": f"sec_reject_{review_id}"}],
                    [{"text": "📋 View Full Report",
                      "callback_data": f"sec_report_{review_id}"}],
                ]}
                for admin_id in ADMIN_IDS:
                    try:
                        send_message(admin_id, admin_text, admin_kb)
                    except Exception:
                        pass
                return

            # ── Save file ─────────────────────────────────────────────
            temp_file = BASE_DIR / f"temp_{user_id}_{file_name}"
            try:
                with open(temp_file, 'wb') as f:
                    f.write(file_bytes_content)
            except Exception as save_err:
                send_message(chat_id, f"❌ Could not save file: {save_err}")
                return

            set_user_step(user_id, 'awaiting_reqs',
                         temp_file=str(temp_file),
                         plan=user_step.get('plan'),
                         duration=user_step.get('duration'),
                         cost_coins=user_step.get('cost_coins'),
                         cost_stars=user_step.get('cost_stars'),
                         payment_method=user_step.get('payment_method'),
                         env_vars={})

            # Build scan summary line
            if scan_warnings:
                scan_note = f"⚠️ {len(scan_warnings)} warning(s) noted — review logs after deployment."
            else:
                scan_note = "✅ Security scan passed."

            lang_label = {
                '.py': '🐍 Python', '.js': '🟨 Node.js', '.mjs': '🟨 Node.js (ESM)',
                '.cjs': '🟨 Node.js (CJS)', '.ts': '🔷 TypeScript',
                '.tsx': '🔷 TypeScript/React', '.jsx': '🟨 JavaScript/React',
            }.get(ext, '📄 Script')

            summary = (
                f"*✅ File accepted* — {lang_label}\n\n"
                f"📁 `{file_name}` ({format_file_size(file_size)})\n"
                f"🔒 {scan_note}\n\n"
            )
            if scan_warnings:
                summary += f"*Warnings:*\n" + '\n'.join(f'• `{safe_md(w)[:200]}`' for w in scan_warnings[:4]) + "\n\n"
            summary += (
                f"📋 Plan: `{(user_step.get('plan') or 'free').upper()}`\n"
                f"⏱️ Duration: `{user_step.get('duration', 'N/A')}` days\n\n"
                "*📦 Add requirements?*\nUpload `requirements.txt` or `package.json`, or click Auto-detect:"
            )

            if scan_msg_id:
                edit_message(chat_id, scan_msg_id, summary, get_reqs_keyboard())
            else:
                send_message(chat_id, summary, get_reqs_keyboard())
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
        
        if invoice_payload.startswith("tier_"):
            tparts = invoice_payload.split("_")          # tier, <key>, <m|y>, <user_id>, <ts>
            tkey = tparts[1] if len(tparts) > 1 else ""
            tper = tparts[2] if len(tparts) > 2 else ""
            if (tkey not in RESOURCE_TIERS or tper not in TIER_PERIODS
                    or str(user_id) != (tparts[3] if len(tparts) > 3 else "")):
                send_message(chat_id, "⚠️ Payment received but the plan could not be verified. "
                                      "Please contact an admin.")
                return
            if total_amount < tier_price_stars(tkey, tper):
                send_message(chat_id, "⚠️ Payment amount doesn't match the plan price. Please contact an admin.")
                return
            end = activate_resource_tier(user_id, tkey, tper, total_amount, total_amount * STARS_PER_COIN)
            send_message(chat_id,
                f"✅ *{RESOURCE_TIERS[tkey][1]} {TIER_PERIODS[tper][0].upper()} PLAN ACTIVE*\n\n💾 Guaranteed: `{RESOURCE_TIERS[tkey][1]}`\n"
                f"📅 Until: `{end.strftime('%Y-%m-%d')}`\n\nThank you! 🙏",
                {"inline_keyboard": [[{"text": "🏠 Main Menu", "callback_data": "main_menu"}]]})
            return

        if invoice_payload.startswith("premium_"):
            parts = invoice_payload.split("_")
            plan = parts[1]
            
            if plan == "monthly":
                activate_premium(user_id, "monthly", total_amount, total_amount * STARS_PER_COIN, 30)
                send_message(chat_id, f"✅ *PREMIUM ACTIVATED!*\n\nMonthly plan active for 30 days!\nThank you! 🙏")
            elif plan == "yearly":
                activate_premium(user_id, "yearly", total_amount, total_amount * STARS_PER_COIN, 365)
                send_message(chat_id, f"✅ *PREMIUM ACTIVATED!*\n\nYearly plan active for 365 days!\nThank you! 🙏")
            return

        # ── Paid GitHub deployment ───────────────────────────────────
        if invoice_payload.startswith("ghdeploy_"):
            row = db_execute('''SELECT requirements FROM pending_deployments
                                WHERE user_id = ? AND payload = ? AND status = 'pending'
                                ORDER BY id DESC LIMIT 1''', (user_id, invoice_payload), fetch='one')
            if not row:
                send_message(chat_id, "⚠️ Payment received but the deployment details were not found. "
                                      "Please contact an admin.")
                return
            db_execute("UPDATE pending_deployments SET status = 'completed' WHERE payload = ?",
                       (invoice_payload,))
            db_execute('''INSERT INTO star_transactions
                (user_id, amount, transaction_type, source, reference_id, timestamp, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, -total_amount, "deployment_payment", "telegram_stars",
                 None, datetime.now().isoformat(), 'completed', invoice_payload))
            try:
                gh_step = json.loads(row[0])
            except Exception:
                gh_step = {}
            _launch_github_deploy(chat_id, user_id, gh_step,
                                  main_file_name=gh_step.get('temp_main_file') or None,
                                  paid_method='stars')
            async_backup(f"stars_github_{user_id}")
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
            
            # Record outgoing star transaction (user spent real Stars — do NOT credit their internal balance)
            try:
                conn2 = sqlite3.connect(DATABASE_FILE)
                c2 = conn2.cursor()
                c2.execute('''INSERT INTO star_transactions 
                    (user_id, amount, transaction_type, source, reference_id, timestamp, status, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (user_id, -total_amount, "deployment_payment", "telegram_stars",
                     None, datetime.now().isoformat(), 'completed', invoice_payload))
                conn2.commit()
                conn2.close()
            except Exception as tx_e:
                print(f"⚠️ Star transaction record error: {tx_e}")
            
            deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements, env_vars, 
                            plan, duration, cost_coins, cost_stars, payment_method)
            async_backup(f"stars_payment_{user_id}")
            
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
        _pl = str(pre_checkout_query.get('invoice_payload', ''))
        if is_banned(pre_checkout_query['from']['id']):
            data = {"pre_checkout_query_id": query_id, "ok": False, "error_message": "Your account is banned."}
        elif _pl.startswith("tier_"):
            try:
                _why = _tier_blocked_reason(int(pre_checkout_query['from']['id']), _pl.split("_")[1])
            except Exception:
                _why = "Could not verify this plan."
            if _why:
                data = {"pre_checkout_query_id": query_id, "ok": False, "error_message": _why[:200]}
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8')).get('ok', False)
    except Exception as e:
        print(f"❌ Pre-checkout error: {e}")
        return False

# ==================== BACKGROUND MONITORS ====================

def deployment_expiry_monitor():
    """
    Runs every 60 s. Stops bots whose expire_time has passed and updates their
    status. Free-tier bots are simply stopped; paid bots with an expired premium
    owner are paused (so they can be resumed on renewal).
    """
    print("✅ Deployment expiry monitor started")
    while True:
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            now_iso = datetime.now().isoformat()

            # Find all active/paused deployments that have expired.
            # expire_time IS NULL means a lifetime (never-expiring) deployment —
            # SQLite's NULL <= x is already NULL/false so these are naturally
            # excluded, but the explicit check makes the intent unambiguous.
            c.execute("""SELECT deployment_id, user_id, proc_pid, is_free, plan, folder_name
                         FROM deployments
                         WHERE status IN ('active', 'paused')
                           AND expire_time IS NOT NULL AND expire_time <= ?""",
                      (now_iso,))
            expired = c.fetchall()

            for dep_id, uid, proc_pid, is_free, plan, folder_name_val in expired:
                if proc_pid:
                    try:
                        kill_deployment_process(proc_pid)
                    except Exception:
                        pass

                c.execute("""UPDATE deployments
                             SET status='stopped', proc_pid=NULL, is_paused=0
                             WHERE deployment_id=?""", (dep_id,))
                with deployment_lock:
                    active_deployments.pop(dep_id, None)

                print(f"⏰ Expired deployment {dep_id} (user {uid}) stopped")

                # Notify user with relevant action buttons
                try:
                    if is_free:
                        # Free bot expired: offer restart for another 24h
                        send_message(uid,
                            f"⏰ *Free Bot #{dep_id} Expired*\n\n"
                            f"Your 24-hour deployment has ended.\n"
                            f"Restart for another 24h — your database is intact.",
                            {"inline_keyboard": [
                                [{"text": "🔄 Restart (Free 24h)",  "callback_data": f"restart_deploy_{dep_id}"}],
                                [{"text": "⭐ Get Premium",          "callback_data": "subscribe_premium"}],
                                [{"text": "📦 My Deployments",       "callback_data": "my_deployments"}],
                            ]})
                    else:
                        # Premium bot expired: reactivate or continue free
                        send_message(uid,
                            f"⏰ *Premium Bot #{dep_id} Expired*\n\n"
                            f"Your `{plan.upper()}` plan has ended.\n\n"
                            f"*Options:*\n"
                            f"• ⭐ Reactivate Premium — bot resumes immediately with full history\n"
                            f"• 🆓 Continue as Free — bot runs 24h more, database preserved",
                            {"inline_keyboard": [
                                [{"text": "⭐ Reactivate Premium",   "callback_data": f"reactivate_premium_{dep_id}"}],
                                [{"text": "🆓 Continue Free (24h)", "callback_data": f"continue_as_free_{dep_id}"}],
                                [{"text": "📦 My Deployments",       "callback_data": "my_deployments"}],
                            ]})
                except Exception:
                    pass

            conn.commit()
            conn.close()

            update_system_stats()
        except Exception as e:
            print(f"❌ Expiry monitor error: {e}")

        sleep(60)


# Crash-loop protection: cap automatic restarts within a rolling window so a
# permanently-broken script can't burn CPU restarting forever.
_AUTO_RESTART_MAX_ATTEMPTS  = 5
_AUTO_RESTART_WINDOW_SECONDS = 3600  # 1 hour


def _auto_relaunch_deployment(deployment_id, reason="crash", bypass_cap=False):
    """
    System-initiated relaunch — no chat_id/ownership check, used by the
    health monitor (crash recovery) and startup reconciliation (host process
    restart). Unlike the user-facing restart_deployment(), this NEVER extends
    expire_time — it keeps exactly the expiry the deployment already had, and
    an already-expired deployment is left alone rather than resurrected.

    Returns a status string: 'restarted', 'expired', 'paused_premium',
    'files_missing', 'cap_exceeded', 'launch_failed', 'not_found'.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT user_id, file_name, is_paused, env_vars, status,
                        start_time, expire_time, is_free, plan,
                        crash_restart_count, last_crash_restart
                 FROM deployments WHERE deployment_id=?""", (deployment_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "not_found"

    (owner_id, file_name, is_paused, env_vars_json, status,
     start_time_str, expire_time_str, is_free, plan,
     crash_count, last_crash_str) = row
    crash_count = crash_count or 0

    if get_ban_info(owner_id):
        c.execute("UPDATE deployments SET status='stopped', proc_pid=NULL WHERE deployment_id=?", (deployment_id,))
        conn.commit()
        conn.close()
        return "banned"

    # Never resurrect a deployment whose time is genuinely up.
    if expire_time_str:
        try:
            if datetime.fromisoformat(expire_time_str) < datetime.now():
                conn.close()
                return "expired"
        except Exception:
            pass

    if is_paused and not is_free and plan != "lifetime":
        conn.close()
        return "paused_premium"

    now = datetime.now()
    if not bypass_cap:
        if last_crash_str:
            try:
                if (now - datetime.fromisoformat(last_crash_str)).total_seconds() > _AUTO_RESTART_WINDOW_SECONDS:
                    crash_count = 0  # rolling window expired — reset the budget
            except Exception:
                crash_count = 0
        if crash_count >= _AUTO_RESTART_MAX_ATTEMPTS:
            conn.close()
            return "cap_exceeded"

    deploy_folder = get_deploy_folder(owner_id, deployment_id)
    file_name     = _sane_entry(deploy_folder, file_name, deployment_id)
    dest_script   = deploy_folder / file_name
    restored_env  = None

    if not deploy_folder.exists() or not dest_script.exists():
        # The files are gone — almost always the host's disk being wiped on a redeploy.
        # Pull the deployment (code + manifests + .env + db) back from the GitHub files
        # repo and reinstall its dependencies before giving up on it.
        print(f"♻️  dep#{deployment_id}: files missing — restoring from the GitHub files repo…")
        _restored_ok, restored_env = _files_restore_for_relaunch(
            owner_id, deployment_id, deploy_folder, dest_script,
            log=lambda m, _d=deployment_id: print(f"   [restore dep#{_d}] {m}"))

    if not deploy_folder.exists() or not dest_script.exists():
        # Nothing on disk and no usable backup — nothing to relaunch from.
        c.execute("UPDATE deployments SET status='stopped', proc_pid=NULL WHERE deployment_id=?",
                  (deployment_id,))
        conn.commit()
        conn.close()
        try:
            send_message(owner_id,
                f"⚠️ *Bot #{deployment_id} could not be restarted*\n\n"
                f"Its files are missing on disk (most likely lost during a host "
                f"restart/redeploy). Please redeploy this bot — your account "
                f"data and coin balance are unaffected.",
                {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        except Exception:
            pass
        return "files_missing"

    env_vars = json.loads(env_vars_json) if env_vars_json else {}
    if not env_vars and restored_env:
        # The platform DB has no env for this deployment but its backup does — use the paired .env.
        env_vars = restored_env
        c.execute("UPDATE deployments SET env_vars=? WHERE deployment_id=?",
                  (json.dumps(env_vars), deployment_id))
        conn.commit()

    env_file = deploy_folder / ".env"
    env_file.write_text('\n'.join(f"{k}={v}" for k, v in env_vars.items()) + '\n')

    # Make sure the dependencies are actually there (cheap no-op when they are).
    _install_deps_for_folder(deploy_folder, dest_script, lambda m: None, only_if_missing=True)

    # The previous run may have died on a package that isn't in any manifest (an import the
    # author never listed). Read its log and install exactly that package BEFORE relaunching.
    auto_installed = []
    crash_tail = _log_tail_text(deploy_folder, 15) if reason in ("crash", "watchdog") else ""   # before the launch overwrites it
    if reason in ("crash", "watchdog"):
        auto_installed = _install_missing_from_log(deploy_folder, dest_script)
        # Exponential backoff between crash restarts: 5s, 10s, 20s, 40s, then 60s.
        if crash_count > 0 and last_crash_str:
            try:
                wait = min(60, 5 * 2 ** (crash_count - 1)) - (now - datetime.fromisoformat(last_crash_str)).total_seconds()
                if wait > 0:
                    sleep(min(wait, 60))
            except Exception:
                pass

    # Branch on file type exactly like the original deploy path does — a
    # Node.js deployment must NOT be regenerated through the Python launcher,
    # or its start.sh gets silently overwritten with a script that tries to
    # run a .js file via `python3`, which fails outright.
    ext = dest_script.suffix.lower()
    is_node = ext in ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx')

    if is_node:
        start_script, _ = create_node_launcher_script(
            deploy_folder, dest_script, env_vars, lambda x: None)
    else:
        try:
            code_content = dest_script.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            code_content = ""
        launcher_script, _ = create_enhanced_launcher_script(
            deploy_folder, dest_script, env_vars, code_content, lambda x: None)
        start_script = deploy_folder / "start.sh"
        start_script.write_text(
            f'#!/bin/bash\ncd "{deploy_folder}"\nexport PYTHONUNBUFFERED=1\n'
            f'setsid nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &\necho $! > pid.txt\n')
        start_script.chmod(0o755)

    is_running, new_pid = _launch_and_check(start_script, deploy_folder)
    if not is_running and _log_shows_missing_dependency(deploy_folder):
        # Died on an import/require error — install the exact missing package(s), reinstall the
        # manifests, and try once more.
        print(f"📦 dep#{deployment_id}: launch failed on a missing dependency — fixing and retrying once")
        auto_installed += _install_missing_from_log(deploy_folder, dest_script)
        _install_deps_for_folder(deploy_folder, dest_script, lambda m: None)
        is_running, new_pid = _launch_and_check(start_script, deploy_folder)

    if is_running:
        c.execute("""UPDATE deployments
                     SET proc_pid=?, status='active', is_paused=0,
                         crash_restart_count=?, last_crash_restart=?
                     WHERE deployment_id=?""",
                  (new_pid, crash_count + 1, now.isoformat(), deployment_id))
        conn.commit()
        conn.close()
        with deployment_lock:
            active_deployments[deployment_id] = new_pid
        print(f"🔄 Auto-restarted deployment {deployment_id} ({reason})")
        try:
            if not notif_allows(owner_id, "crash", "recovered"):
                return "restarted"
            _inst = (f"\n📦 Installed missing package(s): `{', '.join(dict.fromkeys(auto_installed))[:200]}`"
                     if auto_installed else "")
            if crash_tail:
                _diag = crash_diagnose(crash_tail)
                if auto_installed and _diag["code"] == "missing_module":
                    _diag = dict(_diag, code="unknown", title="A package was missing (now installed)", detail="")
                _rows = crash_fix_rows(deployment_id, _diag) if _diag["code"] not in ("unknown",) else [
                    [{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{deployment_id}"}]]
                send_message(owner_id,
                    crash_alert_text(deployment_id, f"🔄 *Bot #{deployment_id} auto-restarted*\n\nIt stopped unexpectedly and is back online.{_inst}\nYour database and settings are untouched.",
                                     _diag, crash_tail) if _diag["code"] != "unknown" or _diag["detail"] else
                    f"🔄 *Bot #{deployment_id} auto-restarted*\n\nIt stopped unexpectedly and has been brought back online automatically.{_inst}\nYour database and settings are untouched.\n\n*Why it crashed (last log lines):*\n```\n{crash_tail}\n```",
                    {"inline_keyboard": _rows})
            else:
                send_message(owner_id,
                    f"🔄 *Bot #{deployment_id} auto-restarted*\n\nIt stopped unexpectedly and has been brought back online automatically.{_inst}\nYour database and settings are untouched.",
                    {"inline_keyboard": [[{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{deployment_id}"}]]})
        except Exception:
            pass
        return "restarted"
    else:
        c.execute("""UPDATE deployments
                     SET status='stopped', proc_pid=NULL,
                         crash_restart_count=?, last_crash_restart=?
                     WHERE deployment_id=?""",
                  (crash_count + 1, now.isoformat(), deployment_id))
        conn.commit()
        conn.close()
        print(f"❌ Auto-restart launch failed for deployment {deployment_id}")
        return "launch_failed"


# ==================== FEATURE PACK ====================
# Auto-deploy webhook · scheduled restarts · isolation · log search · versions/restore · .env import
# promo codes/receipts · usage graphs · audit log · maintenance mode · bot sharing · confirmations
# · languages · health checks.   Everything here is button-driven (callbacks prefixed "fp_").
_fp_ctx = threading.local()          # .shared = True while a collaborator is viewing someone else's bot


def _fp_init_tables(c):
    for ddl in (
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor INTEGER, "
        "action TEXT, target TEXT, detail TEXT)",
        "CREATE TABLE IF NOT EXISTS dep_settings (deployment_id INTEGER PRIMARY KEY, restart_hours INTEGER DEFAULT 0, "
        "last_sched TEXT, hook_secret TEXT, auto_deploy INTEGER DEFAULT 0, health_url TEXT, health_fails INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS dep_shares (deployment_id INTEGER, user_id INTEGER, added_by INTEGER, "
        "added_at TEXT, PRIMARY KEY (deployment_id, user_id))",
        "CREATE TABLE IF NOT EXISTS dep_metrics (deployment_id INTEGER, ts INTEGER, cpu REAL, ram REAL)",
        "CREATE INDEX IF NOT EXISTS idx_dep_metrics ON dep_metrics (deployment_id, ts)",
        "CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, kind TEXT, value INTEGER, "
        "max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1, created_by INTEGER)",
        "CREATE TABLE IF NOT EXISTS promo_redemptions (code TEXT, user_id INTEGER, ts TEXT, PRIMARY KEY (code, user_id))",
        "CREATE TABLE IF NOT EXISTS user_discounts (user_id INTEGER PRIMARY KEY, percent INTEGER, code TEXT)",
        "CREATE TABLE IF NOT EXISTS tr_cache (lang TEXT, src TEXT, out TEXT, PRIMARY KEY (lang, src))",
    ):
        c.execute(ddl)
    for _ddl in ("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'en'",
                 "ALTER TABLE dep_settings ADD COLUMN hang_off INTEGER DEFAULT 0",
                 "ALTER TABLE dep_settings ADD COLUMN compress INTEGER DEFAULT 0"):
        try:
            c.execute(_ddl)
        except Exception:
            pass


def kv_get(key, default=None):
    r = db_execute("SELECT value FROM kv WHERE key = ?", (key,), fetch='one')
    return r[0] if r else default


def kv_set(key, value):
    db_execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, str(value)))


def audit(actor, action, target="", detail=""):
    """Append an entry to the admin audit log (never raises)."""
    try:
        db_execute("INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now().isoformat(timespec='seconds'), actor, str(action)[:60], str(target)[:80],
                    str(detail)[:300]))
    except Exception:
        pass


def _fp_kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


def _fp_btn(text, data):
    return {"text": text, "callback_data": data}


def _fp_dep(dep_id):
    r = db_execute("SELECT user_id, file_name, status, source_type, github_repo, github_branch, env_vars, proc_pid, "
                   "expire_time FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
    if not r:
        return None
    return {"id": dep_id, "owner": r[0], "file": r[1], "status": r[2], "source": r[3], "repo": r[4],
            "branch": r[5] or "main", "env": (json.loads(r[6]) if r[6] else {}), "pid": r[7], "expire": r[8],
            "folder": get_deploy_folder(r[0], dep_id)}


def _fp_own(uid, dep_id):
    """The deployment if `uid` owns it (or is an admin), else None."""
    d = _fp_dep(dep_id)
    if d and (d["owner"] == uid or is_admin(uid)):
        return d
    return None


def _fp_settings(dep_id):
    db_execute("INSERT OR IGNORE INTO dep_settings (deployment_id) VALUES (?)", (dep_id,))
    r = db_execute("SELECT restart_hours, last_sched, hook_secret, auto_deploy, health_url, health_fails, hang_off "
                   "FROM dep_settings WHERE deployment_id = ?", (dep_id,), fetch='one')
    r = tuple(r) + (db_execute("SELECT compress FROM dep_settings WHERE deployment_id = ?", (dep_id,), fetch='one')[0],)
    return {"restart_hours": r[0] or 0, "last_sched": r[1], "secret": r[2], "auto": r[3] or 0,
            "health_url": r[4], "fails": r[5] or 0, "hang_off": r[6] or 0, "compress": r[7] or 0}


# ---------------------------------------------------------------- maintenance mode
def maintenance_on():
    return kv_get("maintenance") == "1"


MAINT_TEXT = ("🛠️ *Maintenance mode*\n\nThe host is being updated, so new deployments, uploads and restarts are "
              "paused for a moment. Your running bots are *not* affected. Please try again shortly.")

_MAINT_BLOCKED = ("deploy_new", "github_deploy", "free_deployment", "restart_deploy_", "restart_free_",
                  "reactivate_premium_", "continue_as_free_", "gh_pay_", "fp_rbok_", "fp_restore_")


# ---------------------------------------------------------------- isolation
_bwrap_state = {"ok": None}


def _bwrap_ok():
    if _bwrap_state["ok"] is None:
        ok = False
        try:
            if shutil.which("bwrap"):
                ok = subprocess.run(["bwrap", "--bind", "/", "/", "true"], capture_output=True,
                                    timeout=8).returncode == 0
        except Exception:
            ok = False
        _bwrap_state["ok"] = ok
        print("🛡️ Container isolation (bubblewrap): " + ("ENABLED" if ok else
              "unavailable on this host — using resource limits + file-scan guard"))
    return _bwrap_state["ok"]


_HOST_SECRET_KEYS = ('BOT_TOKEN', 'TELEGRAM_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TOKEN', 'API_TOKEN', 'TG_BOT_TOKEN',
                     'TGBOT_TOKEN', 'DISCORD_TOKEN', 'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'GITHUB_TOKEN',
                     'GITHUB_REPO_OWNER', 'GITHUB_REPO_NAME', 'GITHUB_BACKUP_BRANCH', 'GITHUB_BACKUP_PATH',
                     'GITHUB_FILES_REPO_NAME', 'DATABASE_URL', 'DATABASE_FILE', 'ADMIN_IDS', 'REQUIRED_CHANNEL',
                     'CHANNEL_LINK', 'PUBLIC_BASE_URL')
_UID_BASE = 200000
_uid_state = {"ok": None}


def _bot_uid(user_id):
    return _UID_BASE + (int(user_id) % 1_000_000)


def _uid_drop(uid):
    def _f():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return _f


def _uid_iso_ok():
    """True when the host is root and a throw-away unprivileged user can run our interpreters."""
    if _uid_state["ok"] is None:
        ok = False
        try:
            if os.name == "posix" and os.geteuid() == 0 and not os.environ.get("DISABLE_UID_ISOLATION"):
                ok = subprocess.run([sys.executable, "-c", "import os, sys"], capture_output=True, timeout=15,
                                    preexec_fn=_uid_drop(_UID_BASE - 1)).returncode == 0
                node = shutil.which("node")
                if ok and node:
                    ok = subprocess.run([node, "--version"], capture_output=True, timeout=15,
                                        preexec_fn=_uid_drop(_UID_BASE - 1)).returncode == 0
        except Exception:
            ok = False
        _uid_state["ok"] = ok
        print("🛡️ Per-user unix-user isolation: " + ("ENABLED" if ok else "unavailable (host isn't root or interpreters aren't "
              "world-executable)"))
    return _uid_state["ok"]


def _harden_perms():
    """Platform data is root-only; bots can traverse to their own folder but not list or read anything else."""
    if not _uid_iso_ok():
        return
    try:
        for d in (BASE_DIR, DEPLOYMENTS_DIR):
            os.chmod(d, 0o711)
        for ud in DEPLOYMENTS_DIR.iterdir():          # lock every owner's dir now, not only at their next launch
            if ud.is_dir() and ud.name.isdigit():
                u = _bot_uid(ud.name)
                os.chown(ud, u, u)
                os.chmod(ud, 0o700)
        for d in (USER_FILES_DIR, LOGS_DIR, PIP_CACHE_DIR, BASE_DIR / "snapshots"):
            if Path(d).exists():
                os.chmod(d, 0o700)
        for suffix in ("", "-wal", "-shm", "-journal"):
            f = Path(str(DATABASE_FILE) + suffix)
            if f.exists():
                os.chmod(f, 0o600)
    except Exception as e:
        print(f"⚠️ harden perms: {e}")


def strip_foreign_symlinks(folder):
    """Delete symlinks inside a bot folder that point outside it. A bot could otherwise plant `.env -> /etc/...`
    and trick the (root) host into writing through the link. Returns how many were removed."""
    n = 0
    try:
        root = os.path.realpath(folder)
        for dp, dns, fns in os.walk(folder, topdown=True, followlinks=False):
            for name in list(dns) + list(fns):
                fp = os.path.join(dp, name)
                if os.path.islink(fp):
                    tgt = os.path.realpath(fp)
                    if tgt != root and not tgt.startswith(root + os.sep):
                        os.unlink(fp)
                        n += 1
                        if name in dns:
                            dns.remove(name)
    except Exception as e:
        print(f"⚠️ strip symlinks: {e}")
    if n:
        print(f"🔒 removed {n} symlink(s) pointing outside {folder}")
    return n


def _prepare_uid_folder(deploy_folder):
    """chown the bot's folder tree to its owner's private unix user and lock the owner's dir to that user.
    -> uid, or None if the folder isn't reachable by that user (isolation then isn't applied to this launch)."""
    try:
        df = Path(deploy_folder).resolve()
        strip_foreign_symlinks(df)
        user_dir = df.relative_to(DEPLOYMENTS_DIR.resolve()).parts[0]
        uid = _bot_uid(user_dir)
        ud = DEPLOYMENTS_DIR / user_dir
        os.chown(ud, uid, uid)
        os.chmod(ud, 0o700)
        for root, dirs, files in os.walk(df):
            for n in dirs + files:
                fp = os.path.join(root, n)
                try:
                    st = os.lstat(fp)
                    if st.st_uid != uid:
                        os.lchown(fp, uid, uid)
                except OSError:
                    pass
        os.chown(df, uid, uid)
        for pre in ("pid.txt", "output.log"):
            fp = df / pre
            if fp.exists() and not fp.is_symlink():
                os.lchown(fp, uid, uid)
        if subprocess.run(["test", "-x", str(df)], preexec_fn=_uid_drop(uid)).returncode != 0:
            print(f"⚠️ unix-user isolation skipped for {df}: not reachable by the bot user (check parent dir permissions)")
            return None
        return uid
    except Exception as e:
        print(f"⚠️ prepare uid folder: {e}")
        return None


def isolation_status():
    parts = []
    if _uid_iso_ok():
        parts.append("per-user unix users ✅")
    if _bwrap_ok():
        parts.append("bubblewrap ✅")
    parts.append("resource limits")
    if len(parts) == 1:
        parts.append("data-theft scanner (no OS-level sandbox on this host)")
    return " + ".join(parts)


def _fp_preexec(compress=False):
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 << 30, 2 << 30))
        resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
        os.nice(19 if compress else 5)
    except Exception:
        pass
    if compress:                                     # one CPU core only → can't hog the host's CPUs
        try:
            cpus = sorted(os.sched_getaffinity(0))
            os.sched_setaffinity(0, {cpus[os.getpid() % len(cpus)]})
        except Exception:
            pass


# ---- "Ultra compress": lowest RAM / CPU footprint for a hosted bot --------------------------------------
_SITE_CODE = '''# Loaded automatically by every Python process of a compressed bot (via PYTHONPATH).
import os
if os.environ.get("HB_COMPRESS") == "1":
    try:
        import sys, gc, threading, time
        sys.setswitchinterval(0.05)              # fewer GIL hand-offs -> less CPU
        gc.collect()
        try:
            gc.freeze()                          # startup objects never need re-scanning
        except Exception:
            pass
        def _hb_trim():
            try:
                import ctypes
                libc = ctypes.CDLL("libc.so.6")
            except Exception:
                libc = None
            every = float(os.environ.get("HB_TRIM_SECS", "45"))
            while True:
                time.sleep(every)
                try:
                    gc.collect()
                    if libc is not None:
                        libc.malloc_trim(0)      # hand freed heap pages back to the OS
                except Exception:
                    pass
        threading.Thread(target=_hb_trim, daemon=True, name="hb-trim").start()
    except Exception:
        pass
'''


def _site_dir():
    d = BASE_DIR / "_hb_site"
    f = d / "sitecustomize.py"
    try:
        if not f.exists() or f.read_text() != _SITE_CODE:
            d.mkdir(parents=True, exist_ok=True)
            f.write_text(_SITE_CODE)
            os.chmod(d, 0o755)
            os.chmod(f, 0o644)
    except Exception as e:
        print(f"⚠️ site dir: {e}")
    return str(d)


def compress_env(base_env=None):
    """Environment variables that shrink a bot's memory and CPU use (Python + Node + native libs)."""
    e = {
        "HB_COMPRESS": "1",
        "MALLOC_ARENA_MAX": "1",                 # glibc: one heap instead of one per thread
        "MALLOC_TRIM_THRESHOLD_": "65536",
        "MALLOC_TOP_PAD_": "0",
        "MALLOC_MMAP_THRESHOLD_": "131072",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONOPTIMIZE": "1",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false",
        "UV_THREADPOOL_SIZE": "2",
        "NODE_OPTIONS": "--max-old-space-size=128 --max-semi-space-size=1",
    }
    pp = (base_env or {}).get("PYTHONPATH", "")
    e["PYTHONPATH"] = _site_dir() + (os.pathsep + pp if pp else "")
    return e


def compress_on(dep_id):
    try:
        return bool(dep_id) and bool(_fp_settings(dep_id)["compress"])
    except Exception:
        return False


def _dep_id_from_folder(folder):
    try:
        f = Path(folder).resolve()
        user, name = f.relative_to(DEPLOYMENTS_DIR.resolve()).parts[:2]
        r = db_execute("SELECT deployment_id FROM deployments WHERE user_id = ? AND (folder_name = ? OR deployment_id = ?) "
                       "ORDER BY deployment_id DESC LIMIT 1", (int(user), name, int(name) if name.isdigit() else -1),
                       fetch='one')
        return r[0] if r else None
    except Exception:
        return None


def _run_start(start_script, deploy_folder, **kw):
    """Run a deployment's start.sh with per-process limits and, when the host allows it, inside a bubblewrap
    sandbox that hides the platform's data dir (database, other users' bots) — only this bot's folder is visible."""
    cmd = [str(start_script)]
    comp = compress_on(_dep_id_from_folder(deploy_folder))
    if comp:
        _env = dict(kw.get("env") or os.environ)
        _env.update(compress_env(_env))
        kw["env"] = _env
    def _base_pre(_c=comp):
        _fp_preexec(_c)
    uid = _prepare_uid_folder(deploy_folder) if _uid_iso_ok() else None
    if uid is not None:
        _harden_perms()
        _drop = _uid_drop(uid)
        def _pre(_drop=_drop, _c=comp):
            _drop()
            _fp_preexec(_c)
        kw.setdefault("preexec_fn", _pre)
        kw.setdefault("cwd", str(deploy_folder))
        return subprocess.run(cmd, **kw)
    if _bwrap_ok():
        df = str(Path(deploy_folder).resolve())
        cmd = ["bwrap", "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc",
               "--tmpfs", str(BASE_DIR), "--bind", df, df, "--chdir", df, str(start_script)]
    kw.setdefault("cwd", str(deploy_folder))
    kw.setdefault("preexec_fn", _base_pre)
    return subprocess.run(cmd, **kw)


# ---------------------------------------------------------------- snapshots / restore
SNAP_KEEP = 5


def _snap_dir(dep_id):
    d = BASE_DIR / "snapshots" / str(dep_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


_snap_synced = set()
SNAP_REMOTE_MAX_MB = 20


def _snap_remote_prefix(dep_id):
    return f"{FILES_REPO_ROOT}/_snapshots/{int(dep_id)}"     # not <digits>/<digits>: the orphan purge never touches it


def _snap_remote_push(dep_id, path):
    """Copy a rollback point to the GitHub files repo (so a redeploy/disk wipe can't lose it) and prune old ones."""
    try:
        if not _files_repo_ready() or path.stat().st_size > SNAP_REMOTE_MAX_MB << 20:
            return
        pre = _snap_remote_prefix(dep_id)
        with _files_lock:
            changes = {f"{pre}/{path.name}": path.read_bytes()}
            names = sorted((k for k in _files_list_remote(pre) if k.endswith(".zip")), reverse=True)
            names = sorted(set(names) | set(changes), reverse=True)
            for old in names[SNAP_KEEP:]:
                changes[old] = None
            _files_commit(changes, f"rollback point #{dep_id}")
    except Exception as e:
        print(f"⚠️ snapshot push failed: {e}")


def _snap_remote_pull(dep_id):
    """After a disk wipe: bring the saved rollback points back from the files repo."""
    try:
        if not _files_repo_ready():
            return
        pre = _snap_remote_prefix(dep_id)
        with _files_lock:
            remote = _files_list_remote(pre)
            for pth, sha in sorted(remote.items(), reverse=True)[:SNAP_KEEP]:
                if not pth.endswith(".zip"):
                    continue
                dest = _snap_dir(dep_id) / pth.rsplit("/", 1)[-1]
                if not dest.exists():
                    data = _files_fetch_blob(sha)
                    if data:
                        dest.write_bytes(data)
    except Exception as e:
        print(f"⚠️ snapshot pull failed: {e}")


def snapshot_list(dep_id):
    d = BASE_DIR / "snapshots" / str(dep_id)
    if (not d.exists() or not any(d.glob("*.zip"))) and dep_id not in _snap_synced:
        _snap_synced.add(dep_id)
        _snap_remote_pull(dep_id)
    return sorted(d.glob("*.zip"), reverse=True) if d.exists() else []


def snapshot_create(dep_id, label="manual"):
    """Zip the deployment's own files into a rollback point (last SNAP_KEEP kept). -> path or None."""
    try:
        d = _fp_dep(dep_id)
        if not d or not d["folder"].exists():
            return None
        slug = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:24] or "snap"
        p = _snap_dir(dep_id) / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}__{slug}.zip"
        n, size = build_deployment_backup_zip(d["folder"], p)
        if n == 0:
            p.unlink(missing_ok=True)
            return None
        for old in snapshot_list(dep_id)[SNAP_KEEP:]:
            old.unlink(missing_ok=True)
        threading.Thread(target=_snap_remote_push, args=(dep_id, p), daemon=True, name="SnapPush").start()
        return p
    except Exception as e:
        print(f"⚠️ snapshot failed: {e}")
        return None


def _snap_label(p):
    stamp, _, slug = p.stem.partition("__")
    try:
        t = datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%d %b %H:%M")
    except Exception:
        t = stamp
    return f"{t} · {slug.replace('-', ' ')}"


def safe_extract_zip(zip_path, dest, max_files=5000, max_bytes=300 << 20):
    """Extract a zip into `dest` refusing traversal, absolute paths, symlinks and zip bombs. -> (ok, error)."""
    import zipfile
    dest = Path(dest).resolve()
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            if len(infos) > max_files:
                return False, "too many files in the zip"
            if sum(i.file_size for i in infos) > max_bytes:
                return False, "the zip is too large when unpacked"
            for i in infos:
                name = i.filename
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    return False, f"unsafe path `{name[:60]}`"
                if (i.external_attr >> 16) & 0o170000 == 0o120000:
                    return False, "the zip contains a symlink"
                target = (dest / name).resolve()
                if dest not in target.parents and target != dest:
                    return False, f"unsafe path `{name[:60]}`"
            z.extractall(dest)
        return True, ""
    except zipfile.BadZipFile:
        return False, "that isn't a valid zip file"
    except Exception as e:
        return False, str(e)[:120]


def apply_zip_to_deployment(dep, zip_path, label="restore"):
    """Validate (path safety + data-theft scan) then overlay a zip onto a deployment. -> (ok, message)."""
    tmp = Path(tempfile.mkdtemp(prefix="rs_"))
    try:
        ok, err = safe_extract_zip(zip_path, tmp)
        if not ok:
            return False, f"❌ {err}"
        inner = [p for p in tmp.iterdir()]
        root = inner[0] if len(inner) == 1 and inner[0].is_dir() else tmp
        try:
            findings = scan_repo_data_theft(root)
        except Exception:
            findings = []
        if findings:
            return False, "🚫 Rejected: the files try to access other users' bots or data."
        snapshot_create(dep["id"], f"before-{label}")
        dep["folder"].mkdir(parents=True, exist_ok=True)
        strip_foreign_symlinks(dep["folder"])
        shutil.copytree(root, dep["folder"], dirs_exist_ok=True)
        envf = root / ".env"
        if envf.exists():
            new_env, _ = parse_env_input(envf.read_text(errors="replace"))
            new_env = {k: v for k, v in new_env.items() if k not in _PLATFORM_ENV_KEYS}
            if new_env:
                env = dict(dep["env"])
                env.update(new_env)
                _edit_save_env(dep["owner"], dep["id"], env, dep["folder"])
        return True, "✅ Files restored."
    except Exception as e:
        return False, f"❌ Restore failed: {str(e)[:120]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- auto-deploy (GitHub webhook)
def _public_base_url():
    u = os.environ.get("PUBLIC_BASE_URL") or kv_get("public_url") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    if not u and os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
        u = "https://" + os.environ["RENDER_EXTERNAL_HOSTNAME"]
    return u.rstrip("/")


def handle_github_webhook(dep_id, body, signature, event):
    """Called by the HTTP server. -> (status_code, text)."""
    d = _fp_dep(dep_id)
    st = _fp_settings(dep_id) if d else None
    if not d or not st or not st["auto"] or not st["secret"]:
        return 404, "not found"
    import hmac as _hmac
    want = "sha256=" + _hmac.new(st["secret"].encode(), body, hashlib.sha256).hexdigest()
    if not signature or not _hmac.compare_digest(want, signature):
        return 401, "bad signature"
    if event == "ping":
        return 200, "pong"
    if event != "push":
        return 202, "ignored"
    try:
        ref = (json.loads(body.decode("utf-8", "replace")) or {}).get("ref", "")
    except Exception:
        return 400, "bad json"
    if ref and not ref.endswith("/" + d["branch"]):
        return 202, "other branch ignored"
    threading.Thread(target=_autodeploy_run, args=(dep_id,), daemon=True, name=f"AutoDeploy-{dep_id}").start()
    return 202, "deploying"


_autodeploy_busy = set()


def _autodeploy_run(dep_id):
    if dep_id in _autodeploy_busy:
        return
    _autodeploy_busy.add(dep_id)
    d = _fp_dep(dep_id)
    tmp = None
    try:
        if not d or not d["repo"] or "/" not in d["repo"]:
            return
        owner_id = d["owner"]
        if get_ban_info(owner_id):
            return
        gh_owner, gh_repo = d["repo"].split("/", 1)
        token = d["env"].get("GITHUB_DEPLOY_TOKEN")
        tmp = Path(tempfile.mkdtemp(prefix="ad_", dir=str(BASE_DIR)))
        if not download_github_repo(gh_owner, gh_repo, d["branch"], tmp, token, lambda m: None):
            send_message(owner_id, f"⚠️ *Auto-deploy #{dep_id}* — couldn't download `{d['repo']}`. Nothing changed.")
            return
        if scan_repo_data_theft(tmp):
            send_message(owner_id, f"🚫 *Auto-deploy #{dep_id} rejected* — the new commit tries to access other "
                                   f"users' bots or data. Your running bot was left unchanged.")
            audit(owner_id, "autodeploy_rejected", dep_id, d["repo"])
            return
        snapshot_create(dep_id, "before-auto-deploy")
        strip_foreign_symlinks(d["folder"])
        shutil.copytree(tmp, d["folder"], dirs_exist_ok=True)
        try:
            install_from_repo_requirements(d["folder"], lambda m: None)
        except Exception as e:
            print(f"⚠️ autodeploy deps: {e}")
        stop_deployment(dep_id)
        res = _auto_relaunch_deployment(dep_id, reason="update", bypass_cap=True)
        try:
            files_repo_async_save(dep_id, "auto-deploy")
        except Exception:
            pass
        ok = res == "restarted"
        send_message(owner_id, (f"🚀 *Auto-deploy #{dep_id} done* — pushed code from `{d['repo']}` is live."
                                if ok else f"⚠️ *Auto-deploy #{dep_id}*: code updated but the restart returned `{res}`. "
                                           f"Open the deployment and press Restart."),
                     _fp_kb([_fp_btn("📄 Deployment", f"view_deploy_{dep_id}")]))
    except Exception as e:
        print(f"❌ autodeploy {dep_id}: {e}")
    finally:
        _autodeploy_busy.discard(dep_id)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- monitors
def _fp_restart(dep_id, reason):
    stop_deployment(dep_id)
    return _auto_relaunch_deployment(dep_id, reason=reason, bypass_cap=True)


def scheduled_restart_monitor():
    sleep(90)
    print("✅ Scheduled-restart monitor started")
    while True:
        try:
            rows = db_execute("SELECT s.deployment_id, s.restart_hours, s.last_sched FROM dep_settings s "
                              "JOIN deployments d ON d.deployment_id = s.deployment_id "
                              "WHERE s.restart_hours > 0 AND d.status = 'active'", fetch='all') or []
            now = datetime.now()
            for dep_id, hours, last in rows:
                try:
                    due = (not last) or (now - datetime.fromisoformat(last)).total_seconds() >= hours * 3600
                except Exception:
                    due = True
                if not last:               # first sight: start the clock instead of restarting immediately
                    db_execute("UPDATE dep_settings SET last_sched = ? WHERE deployment_id = ?",
                               (now.isoformat(), dep_id))
                    continue
                if due:
                    db_execute("UPDATE dep_settings SET last_sched = ? WHERE deployment_id = ?",
                               (now.isoformat(), dep_id))
                    print(f"⏰ scheduled restart of #{dep_id}")
                    _fp_restart(dep_id, "scheduled")
        except Exception as e:
            print(f"❌ schedule monitor: {e}")
        sleep(300)


_metric_prev = {}


def metrics_sampler():
    sleep(45)
    print("✅ Usage sampler started (5 min)")
    n = 0
    while True:
        try:
            rows = db_execute("SELECT deployment_id, proc_pid FROM deployments WHERE status='active' "
                              "AND proc_pid IS NOT NULL", fetch='all') or []
            now = time.time()
            for dep_id, pid in rows:
                try:
                    os.kill(int(pid), 0)
                    cpu_s = _tree_cpu_seconds(pid)
                    ram = get_process_tree_ram_mb(pid)
                except Exception:
                    continue
                prev = _metric_prev.get(dep_id)
                _metric_prev[dep_id] = (now, cpu_s)
                pct = 0.0
                if prev and now > prev[0] and cpu_s >= prev[1]:
                    pct = (cpu_s - prev[1]) / (now - prev[0]) * 100
                db_execute("INSERT INTO dep_metrics (deployment_id, ts, cpu, ram) VALUES (?, ?, ?, ?)",
                           (dep_id, int(now), round(pct, 1), round(float(ram or 0), 1)))
            n += 1
            if n % 12 == 0:
                db_execute("DELETE FROM dep_metrics WHERE ts < ?", (int(now) - 8 * 86400,))
                # keep the database (which is backed up to GitHub) small
                if (db_execute("SELECT COUNT(*) FROM tr_cache", fetch='one') or (0,))[0] > 20000:
                    db_execute("DELETE FROM tr_cache WHERE rowid IN (SELECT rowid FROM tr_cache ORDER BY RANDOM() LIMIT 8000)")
                db_execute("DELETE FROM audit_log WHERE id < (SELECT COALESCE(MAX(id), 0) - 5000 FROM audit_log)")
        except Exception as e:
            print(f"❌ metrics sampler: {e}")
        sleep(300)


def _is_zombie(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        return s[s.rindex(")") + 2] == "Z"
    except Exception:
        return False


def _health_target(d, st):
    """The URL to ping, or None. 'local' = this bot's own PORT on localhost."""
    u = st["health_url"]
    if not u:
        return None
    if u == "local":
        port = d["env"].get("PORT") or d["env"].get("BOT_PORT")
        return f"http://127.0.0.1:{int(port)}/" if str(port).isdigit() else None
    return u


def _url_is_public(url):
    import socket, ipaddress
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        return True
    except Exception:
        return False


_TG_TOKEN_RX = re.compile(r'^\d{6,12}:[A-Za-z0-9_-]{30,}$')
_pending_hist = {}


def _telegram_token(env):
    for v in (env or {}).values():
        if isinstance(v, str) and _TG_TOKEN_RX.match(v.strip()):
            return v.strip()
    return None


def _tg_pending(token):
    """(pending_update_count, uses_webhook) from Telegram for a bot token, or None. Read-only: never consumes updates."""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10) as r:
            res = (json.loads(r.read().decode()) or {}).get("result") or {}
        return int(res.get("pending_update_count") or 0), bool(res.get("url"))
    except Exception:
        return None


def hang_check_tick():
    """Polling bots have no URL to ping, but a hung one stops reading updates: Telegram's queue for its token keeps
    growing. Growing for 3 checks in a row → restart once; if it still doesn't drain, detection switches itself off."""
    rows = db_execute("SELECT deployment_id FROM deployments WHERE status='active' AND proc_pid IS NOT NULL",
                      fetch='all') or []
    for (dep_id,) in rows:
        st = _fp_settings(dep_id)
        if st["hang_off"] or st["health_url"]:
            continue
        d = _fp_dep(dep_id)
        tok = _telegram_token(d["env"]) if d else None
        if not tok:
            continue
        res = _tg_pending(tok)
        if not res or res[1]:
            continue                      # no answer, or the bot uses a webhook instead of polling
        hist = _pending_hist.setdefault(dep_id, [])
        hist.append(res[0])
        del hist[:-4]
        if len(hist) < 4 or hist[-1] < 5 or not (hist[1] <= hist[2] <= hist[3] and hist[3] > hist[0]):
            continue
        _pending_hist[dep_id] = []
        strikes = _pending_hist.get(("s", dep_id), 0) + 1
        _pending_hist[("s", dep_id)] = strikes
        if strikes >= 2:
            db_execute("UPDATE dep_settings SET hang_off = 1 WHERE deployment_id = ?", (dep_id,))
            send_message(d["owner"], f"ℹ️ *Bot #{dep_id}*: a restart didn't make it read its Telegram updates, so it may "
                                     f"simply not use polling. I turned hang detection off for this bot.",
                         _fp_kb([_fp_btn("🩺 Health check", f"fp_health_{dep_id}")]))
            continue
        tail = ""
        try:
            tail = _log_tail_text(d["folder"], 8)
        except Exception:
            pass
        res2 = _fp_restart(dep_id, "hang")
        audit(d["owner"], "hang_restart", dep_id, f"pending={hist and res[0]}")
        send_message(d["owner"], f"🩺 *Bot #{dep_id} looks hung* — Telegram's queue for it kept growing (`{res[0]}` updates "
                                 f"waiting) so I restarted it (`{res2}`)." +
                     (f"\n\nLast log lines:\n```\n{tail}\n```" if tail else ""),
                     _fp_kb([_fp_btn("📄 Deployment", f"view_deploy_{dep_id}")]))


def health_check_monitor():
    sleep(120)
    print("✅ Health-ping monitor started (2 min)")
    while True:
        try:
            try:
                hang_check_tick()
            except Exception as _e:
                print(f"❌ hang check: {_e}")
            rows = db_execute("SELECT s.deployment_id FROM dep_settings s JOIN deployments d "
                              "ON d.deployment_id = s.deployment_id WHERE d.status='active' "
                              "AND s.health_url IS NOT NULL", fetch='all') or []
            for (dep_id,) in rows:
                d, st = _fp_dep(dep_id), _fp_settings(dep_id)
                url = _health_target(d, st) if d else None
                if not url:
                    continue
                if url != f"http://127.0.0.1:{d['env'].get('PORT')}/" and not _url_is_public(url):
                    continue
                ok = False
                try:
                    class _NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, *a, **k):
                            return None
                    req = urllib.request.Request(url, headers={"User-Agent": "BotHosting-HealthCheck/1.0"})
                    with urllib.request.build_opener(_NoRedirect).open(req, timeout=10) as r:
                        ok = r.status < 500
                except urllib.error.HTTPError as e:
                    ok = e.code < 500
                except Exception:
                    ok = False
                if d["pid"] and _is_zombie(d["pid"]):
                    ok = False
                if ok:
                    if st["fails"]:
                        db_execute("UPDATE dep_settings SET health_fails = 0 WHERE deployment_id = ?", (dep_id,))
                    continue
                fails = st["fails"] + 1
                if fails < 3:
                    db_execute("UPDATE dep_settings SET health_fails = ? WHERE deployment_id = ?", (fails, dep_id))
                    continue
                db_execute("UPDATE dep_settings SET health_fails = 0 WHERE deployment_id = ?", (dep_id,))
                tail = ""
                try:
                    tail = _log_tail_text(d["folder"], 8)
                except Exception:
                    pass
                res = _fp_restart(dep_id, "health")
                send_message(d["owner"], f"🩺 *Bot #{dep_id} stopped answering* its health check 3 times in a row, so I "
                                         f"restarted it (`{res}`)." + (f"\n\nLast log lines:\n```\n{tail}\n```" if tail else ""),
                             _fp_kb([_fp_btn("📄 Deployment", f"view_deploy_{dep_id}")]))
        except Exception as e:
            print(f"❌ health monitor: {e}")
        sleep(120)


# ---------------------------------------------------------------- usage graphs
_SPARK = "▁▂▃▄▅▆▇█"


def _spark(vals):
    real = [v for v in vals if v is not None]
    if not real:
        return "—"
    hi = max(real) or 1
    return "".join("·" if v is None else _SPARK[min(7, int(v / hi * 7.999))] for v in vals)


def usage_text(dep_id, hours=24):
    now = int(time.time())
    buckets = 24 if hours <= 24 else 28
    span = hours * 3600 // buckets
    rows = db_execute("SELECT ts, cpu, ram FROM dep_metrics WHERE deployment_id = ? AND ts > ? ORDER BY ts",
                      (dep_id, now - hours * 3600), fetch='all') or []
    if not rows:
        return "📈 *USAGE*\n\nNo samples yet — usage is recorded every 5 minutes while the bot is running."
    cpu, ram = [[] for _ in range(buckets)], [[] for _ in range(buckets)]
    for ts, c, r in rows:
        i = min(buckets - 1, (ts - (now - hours * 3600)) // span)
        cpu[i].append(c)
        ram[i].append(r)
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None
    cs, rs = [avg(x) for x in cpu], [avg(x) for x in ram]
    allc, allr = [r[1] for r in rows], [r[2] for r in rows]
    label = "last 24 hours" if hours <= 24 else "last 7 days"
    return (f"📈 *USAGE — {label}*\n\n"
            f"⚙️ CPU (% of 1 core)\n`{_spark(cs)}`\navg `{sum(allc)/len(allc):.0f}%` · peak `{max(allc):.0f}%`\n\n"
            f"🧠 RAM\n`{_spark(rs)}`\navg `{format_resource(sum(allr)/len(allr))}` · peak `{format_resource(max(allr))}`\n\n"
            f"_Left = oldest, right = now · {len(rows)} samples_")


# ---------------------------------------------------------------- log search
_ERR_RE = re.compile(r'error|exception|traceback|critical|fatal|failed|refused|denied', re.I)
_WARN_RE = re.compile(r'warn|error|exception|traceback|critical|fatal|failed', re.I)


def _read_log_lines(folder):
    p = Path(folder) / "output.log"
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            return f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def log_search_text(folder, pattern=None, mode="kw"):
    lines = _read_log_lines(folder)
    if not lines:
        return "📄 No runtime log yet."
    if mode == "err":
        rx, title = _ERR_RE, "errors"
    elif mode == "warn":
        rx, title = _WARN_RE, "warnings & errors"
    else:
        try:
            rx = re.compile(re.escape(pattern), re.I)
        except Exception:
            return "❌ Bad search text."
        title = f"“{pattern[:30]}”"
    hits = [l for l in lines if rx.search(l)]
    if not hits:
        return f"🔎 No matches for {title} in the last {len(lines)} log lines."
    out, total = [], 0
    for l in reversed(hits):
        l = l.replace("`", "'")[:220]
        if total + len(l) > 3200:
            break
        out.append(l)
        total += len(l) + 1
    out.reverse()
    return (f"🔎 *{len(hits)} match(es) for {title}* (showing last {len(out)})\n```\n" + "\n".join(out) + "\n```")


# ---------------------------------------------------------------- promo codes / receipts
def redeem_promo(user_id, code):
    code = (code or "").strip().upper()
    r = db_execute("SELECT kind, value, max_uses, used, expires_at, active FROM promo_codes WHERE code = ?",
                   (code,), fetch='one')
    if not r or not r[5]:
        return False, "❌ That promo code isn't valid."
    kind, value, max_uses, used, exp, _a = r
    if exp:
        try:
            if datetime.fromisoformat(exp) < datetime.now():
                return False, "⌛ That promo code has expired."
        except Exception:
            pass
    if max_uses and used >= max_uses:
        return False, "🔒 That promo code has been fully used."
    if db_execute("SELECT 1 FROM promo_redemptions WHERE code = ? AND user_id = ?", (code, user_id), fetch='one'):
        return False, "ℹ️ You've already used that code."
    # Claim the code atomically BEFORE paying out, so two simultaneous taps can't both win.
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("UPDATE promo_codes SET used = used + 1 WHERE code = ? AND active = 1 "
                           "AND (max_uses = 0 OR used < max_uses)", (code,))
        if cur.rowcount != 1:
            conn.rollback()
            return False, "🔒 That promo code has been fully used."
        try:
            conn.execute("INSERT INTO promo_redemptions (code, user_id, ts) VALUES (?, ?, ?)",
                         (code, user_id, datetime.now().isoformat(timespec='seconds')))
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "ℹ️ You've already used that code."
        conn.commit()
    finally:
        conn.close()
    if kind == "discount":
        db_execute("INSERT INTO user_discounts (user_id, percent, code) VALUES (?, ?, ?) "
                   "ON CONFLICT(user_id) DO UPDATE SET percent = excluded.percent, code = excluded.code",
                   (user_id, int(value), code))
        msg = f"🏷️ *{int(value)}% discount* saved — it's applied automatically to your next coin payment."
    else:
        update_user_coins(user_id, int(value), "promo_code", code)
        msg = f"🪙 *+{int(value)} coins* added to your balance!"
    return True, f"✅ Code `{code}` accepted!\n\n{msg}"


def apply_discount_cashback(user_id, cost_coins):
    """After a successful coin payment: return the saved discount % of it as coins (one-time). -> coins refunded."""
    try:
        r = db_execute("SELECT percent, code FROM user_discounts WHERE user_id = ?", (user_id,), fetch='one')
        if not r or not cost_coins or cost_coins <= 0:
            return 0
        back = int(cost_coins * min(90, int(r[0])) // 100)
        db_execute("DELETE FROM user_discounts WHERE user_id = ?", (user_id,))
        if back > 0:
            update_user_coins(user_id, back, "promo_discount", r[1])
            send_message(user_id, f"🏷️ Promo `{r[1]}`: *{r[0]}% off* applied — `{back}🪙` returned to your balance.")
        return back
    except Exception as e:
        print(f"⚠️ cashback: {e}")
        return 0


def _tx_rows(user_id, limit=15):
    rows = []
    for tbl, cur in (("coin_transactions", "🪙"), ("star_transactions", "⭐")):
        for tid, amt, typ, src, ts in (db_execute(
                f"SELECT transaction_id, amount, transaction_type, source, timestamp FROM {tbl} WHERE user_id = ? "
                f"ORDER BY transaction_id DESC LIMIT ?", (user_id, limit), fetch='all') or []):
            rows.append((str(ts or ""), tid, amt, cur, typ, src, "C" if cur == "🪙" else "S"))
    rows.sort(reverse=True)
    return rows[:limit]


def payment_history_text(user_id):
    rows = _tx_rows(user_id)
    if not rows:
        return "🧾 *PAYMENT HISTORY*\n\nNo transactions yet."
    out = ["🧾 *PAYMENT HISTORY* (latest)\n"]
    for ts, tid, amt, cur, typ, src, k in rows:
        sign = "+" if (amt or 0) > 0 else ""
        out.append(f"`{ts[:16]}` {sign}{amt}{cur} · {typ}" + (f" ({src})" if src else "") + f" · RCP-{k}{tid}")
    return "\n".join(out)[:3900]


def send_receipts_file(chat_id, user_id):
    rows = _tx_rows(user_id, 200)
    if not rows:
        send_message(chat_id, "🧾 Nothing to export yet.")
        return
    lines = [f"RECEIPTS — user {user_id}", f"Generated {datetime.now():%Y-%m-%d %H:%M}", "=" * 50]
    for ts, tid, amt, cur, typ, src, k in rows:
        lines.append(f"RCP-{k}{tid} | {ts[:19]} | {amt:+d} {'coins' if cur == '🪙' else 'stars'} | {typ} | {src or '-'}")
    p = Path(tempfile.mkdtemp(prefix="rc_")) / "receipts.txt"
    p.write_text("\n".join(lines) + "\n")
    try:
        send_document(chat_id, str(p), "🧾 Your receipts", "receipts.txt")
    finally:
        shutil.rmtree(p.parent, ignore_errors=True)


# ---------------------------------------------------------------- languages
LANGS = {"en": "🇬🇧 English", "es": "🇪🇸 Español", "fr": "🇫🇷 Français", "pt": "🇧🇷 Português", "de": "🇩🇪 Deutsch",
         "it": "🇮🇹 Italiano", "nl": "🇳🇱 Nederlands", "pl": "🇵🇱 Polski", "uk": "🇺🇦 Українська", "ru": "🇷🇺 Русский",
         "tr": "🇹🇷 Türkçe", "ar": "🇸🇦 العربية", "fa": "🇮🇷 فارسی", "hi": "🇮🇳 हिन्दी", "bn": "🇧🇩 বাংলা",
         "ur": "🇵🇰 اردو", "id": "🇮🇩 Indonesia", "vi": "🇻🇳 Tiếng Việt", "th": "🇹🇭 ไทย", "zh-CN": "🇨🇳 中文",
         "ja": "🇯🇵 日本語", "ko": "🇰🇷 한국어", "sw": "🇰🇪 Kiswahili", "ha": "🇳🇬 Hausa", "yo": "🇳🇬 Yorùbá",
         "ig": "🇳🇬 Igbo", "am": "🇪🇹 አማርኛ"}
_TR = {
    "es": {"Deploy New Bot": "Desplegar nuevo bot", "Deploy from GitHub": "Desplegar desde GitHub",
           "Free Deployment": "Despliegue gratis", "My Deployments": "Mis despliegues", "Back": "Atrás",
           "Menu": "Menú", "Delete Deployment": "Eliminar despliegue", "Stop Bot": "Detener bot",
           "Restart": "Reiniciar", "Download Backup (zip)": "Descargar copia (zip)",
           "View Install Logs": "Ver logs de instalación", "View Runtime Logs": "Ver logs de ejecución",
           "Refresh stats": "Actualizar estadísticas", "Cancel": "Cancelar", "Yes, Delete": "Sí, eliminar",
           "DEPLOYMENT": "DESPLIEGUE", "Status:": "Estado:", "Expires:": "Expira:", "Remaining:": "Restante:",
           "Started:": "Inicio:", "Plan:": "Plan:", "Cost:": "Costo:", "Framework:": "Framework:",
           "Environment Variables:": "Variables de entorno:", "Requirements:": "Requisitos:",
           "LIVE STATS": "ESTADÍSTICAS EN VIVO", "Uptime:": "Tiempo activo:", "ACTIVE": "ACTIVO",
           "STOPPED": "DETENIDO", "PAUSED": "PAUSADO", "YOUR BALANCE": "TU SALDO", "Admin Panel": "Panel admin",
           "Language": "Idioma", "Promo Code": "Código promo", "Payment History": "Historial de pagos",
           "More": "Más", "Shared with me": "Compartidos conmigo", "Tools": "Herramientas",
           "Session expired": "Sesión expirada", "Get Premium": "Obtener Premium"},
    "fr": {"Deploy New Bot": "Déployer un bot", "Deploy from GitHub": "Déployer depuis GitHub",
           "Free Deployment": "Déploiement gratuit", "My Deployments": "Mes déploiements", "Back": "Retour",
           "Menu": "Menu", "Delete Deployment": "Supprimer le déploiement", "Stop Bot": "Arrêter le bot",
           "Restart": "Redémarrer", "Download Backup (zip)": "Télécharger la sauvegarde (zip)",
           "View Install Logs": "Logs d'installation", "View Runtime Logs": "Logs d'exécution",
           "Refresh stats": "Actualiser", "Cancel": "Annuler", "Yes, Delete": "Oui, supprimer",
           "DEPLOYMENT": "DÉPLOIEMENT", "Status:": "Statut :", "Expires:": "Expire :", "Remaining:": "Restant :",
           "Started:": "Début :", "Plan:": "Offre :", "Cost:": "Coût :", "Framework:": "Framework :",
           "Environment Variables:": "Variables d'environnement :", "Requirements:": "Dépendances :",
           "LIVE STATS": "STATS EN DIRECT", "Uptime:": "Disponibilité :", "ACTIVE": "ACTIF",
           "STOPPED": "ARRÊTÉ", "PAUSED": "EN PAUSE", "YOUR BALANCE": "VOTRE SOLDE", "Admin Panel": "Panneau admin",
           "Language": "Langue", "Promo Code": "Code promo", "Payment History": "Historique des paiements",
           "More": "Plus", "Shared with me": "Partagés avec moi", "Tools": "Outils",
           "Session expired": "Session expirée", "Get Premium": "Passer Premium"},
    "pt": {"Deploy New Bot": "Implantar novo bot", "Deploy from GitHub": "Implantar do GitHub",
           "Free Deployment": "Implantação grátis", "My Deployments": "Minhas implantações", "Back": "Voltar",
           "Menu": "Menu", "Delete Deployment": "Excluir implantação", "Stop Bot": "Parar bot",
           "Restart": "Reiniciar", "Download Backup (zip)": "Baixar backup (zip)",
           "View Install Logs": "Logs de instalação", "View Runtime Logs": "Logs de execução",
           "Refresh stats": "Atualizar", "Cancel": "Cancelar", "Yes, Delete": "Sim, excluir",
           "DEPLOYMENT": "IMPLANTAÇÃO", "Status:": "Status:", "Expires:": "Expira:", "Remaining:": "Restante:",
           "Started:": "Início:", "Plan:": "Plano:", "Cost:": "Custo:", "Framework:": "Framework:",
           "Environment Variables:": "Variáveis de ambiente:", "Requirements:": "Requisitos:",
           "LIVE STATS": "ESTATÍSTICAS AO VIVO", "Uptime:": "Tempo ativo:", "ACTIVE": "ATIVO",
           "STOPPED": "PARADO", "PAUSED": "PAUSADO", "YOUR BALANCE": "SEU SALDO", "Admin Panel": "Painel admin",
           "Language": "Idioma", "Promo Code": "Código promo", "Payment History": "Histórico de pagamentos",
           "More": "Mais", "Shared with me": "Compartilhados comigo", "Tools": "Ferramentas",
           "Session expired": "Sessão expirada", "Get Premium": "Obter Premium"},
    "ru": {"Deploy New Bot": "Развернуть бота", "Deploy from GitHub": "Развернуть из GitHub",
           "Free Deployment": "Бесплатный деплой", "My Deployments": "Мои деплои", "Back": "Назад",
           "Menu": "Меню", "Delete Deployment": "Удалить деплой", "Stop Bot": "Остановить бота",
           "Restart": "Перезапуск", "Download Backup (zip)": "Скачать бэкап (zip)",
           "View Install Logs": "Логи установки", "View Runtime Logs": "Логи работы",
           "Refresh stats": "Обновить", "Cancel": "Отмена", "Yes, Delete": "Да, удалить",
           "DEPLOYMENT": "ДЕПЛОЙ", "Status:": "Статус:", "Expires:": "Истекает:", "Remaining:": "Осталось:",
           "Started:": "Запущен:", "Plan:": "План:", "Cost:": "Цена:", "Framework:": "Фреймворк:",
           "Environment Variables:": "Переменные окружения:", "Requirements:": "Зависимости:",
           "LIVE STATS": "СТАТИСТИКА", "Uptime:": "Аптайм:", "ACTIVE": "АКТИВЕН",
           "STOPPED": "ОСТАНОВЛЕН", "PAUSED": "НА ПАУЗЕ", "YOUR BALANCE": "ВАШ БАЛАНС", "Admin Panel": "Админ-панель",
           "Language": "Язык", "Promo Code": "Промокод", "Payment History": "История платежей",
           "More": "Ещё", "Shared with me": "Доступные мне", "Tools": "Инструменты",
           "Session expired": "Сессия истекла", "Get Premium": "Получить Premium"},
    "ar": {"Deploy New Bot": "نشر بوت جديد", "Deploy from GitHub": "النشر من GitHub",
           "Free Deployment": "نشر مجاني", "My Deployments": "عمليات النشر الخاصة بي", "Back": "رجوع",
           "Menu": "القائمة", "Delete Deployment": "حذف النشر", "Stop Bot": "إيقاف البوت",
           "Restart": "إعادة تشغيل", "Download Backup (zip)": "تنزيل نسخة احتياطية (zip)",
           "View Install Logs": "سجلات التثبيت", "View Runtime Logs": "سجلات التشغيل",
           "Refresh stats": "تحديث", "Cancel": "إلغاء", "Yes, Delete": "نعم، احذف",
           "DEPLOYMENT": "النشر", "Status:": "الحالة:", "Expires:": "ينتهي:", "Remaining:": "المتبقي:",
           "Started:": "بدأ:", "Plan:": "الخطة:", "Cost:": "التكلفة:", "Framework:": "الإطار:",
           "Environment Variables:": "متغيرات البيئة:", "Requirements:": "المتطلبات:",
           "LIVE STATS": "إحصائيات مباشرة", "Uptime:": "مدة التشغيل:", "ACTIVE": "نشط",
           "STOPPED": "متوقف", "PAUSED": "متوقف مؤقتًا", "YOUR BALANCE": "رصيدك", "Admin Panel": "لوحة المشرف",
           "Language": "اللغة", "Promo Code": "رمز ترويجي", "Payment History": "سجل المدفوعات",
           "More": "المزيد", "Shared with me": "مشاركة معي", "Tools": "الأدوات",
           "Session expired": "انتهت الجلسة", "Get Premium": "احصل على بريميوم"},
}

_TR_EXTRA = {
 "es": {"Search logs": "Buscar en logs", "Usage": "Uso", "Schedule": "Programar", "Health check": "Chequeo de salud",
        "Auto-deploy": "Auto-despliegue", "Share": "Compartir", "Versions / rollback": "Versiones / revertir",
        "Restore zip": "Restaurar zip", "Maintenance": "Mantenimiento", "Promo Codes": "Códigos promo",
        "Audit Log": "Registro de auditoría", "Are you sure?": "¿Seguro?", "Yes": "Sí", "Errors only": "Solo errores",
        "Full log": "Log completo", "Roll back": "Revertir", "Continue": "Continuar", "Turn off": "Desactivar",
        "Turn on": "Activar", "Add by user ID": "Añadir por ID", "Save one now": "Guardar uno ahora"},
 "fr": {"Search logs": "Chercher dans les logs", "Usage": "Utilisation", "Schedule": "Planifier", "Health check": "Contrôle de santé",
        "Auto-deploy": "Déploiement auto", "Share": "Partager", "Versions / rollback": "Versions / retour arrière",
        "Restore zip": "Restaurer un zip", "Maintenance": "Maintenance", "Promo Codes": "Codes promo",
        "Audit Log": "Journal d'audit", "Are you sure?": "Êtes-vous sûr ?", "Yes": "Oui", "Errors only": "Erreurs seulement",
        "Full log": "Log complet", "Roll back": "Revenir", "Continue": "Continuer", "Turn off": "Désactiver",
        "Turn on": "Activer", "Add by user ID": "Ajouter par ID", "Save one now": "En sauvegarder un"},
 "pt": {"Search logs": "Buscar nos logs", "Usage": "Uso", "Schedule": "Agendar", "Health check": "Verificação de saúde",
        "Auto-deploy": "Auto-implantação", "Share": "Compartilhar", "Versions / rollback": "Versões / reverter",
        "Restore zip": "Restaurar zip", "Maintenance": "Manutenção", "Promo Codes": "Códigos promo",
        "Audit Log": "Registro de auditoria", "Are you sure?": "Tem certeza?", "Yes": "Sim", "Errors only": "Só erros",
        "Full log": "Log completo", "Roll back": "Reverter", "Continue": "Continuar", "Turn off": "Desativar",
        "Turn on": "Ativar", "Add by user ID": "Adicionar por ID", "Save one now": "Salvar um agora"},
 "ru": {"Search logs": "Поиск по логам", "Usage": "Нагрузка", "Schedule": "Расписание", "Health check": "Проверка здоровья",
        "Auto-deploy": "Автодеплой", "Share": "Поделиться", "Versions / rollback": "Версии / откат",
        "Restore zip": "Восстановить из zip", "Maintenance": "Техобслуживание", "Promo Codes": "Промокоды",
        "Audit Log": "Журнал аудита", "Are you sure?": "Вы уверены?", "Yes": "Да", "Errors only": "Только ошибки",
        "Full log": "Полный лог", "Roll back": "Откатить", "Continue": "Продолжить", "Turn off": "Выключить",
        "Turn on": "Включить", "Add by user ID": "Добавить по ID", "Save one now": "Сохранить сейчас"},
 "ar": {"Search logs": "بحث في السجلات", "Usage": "الاستخدام", "Schedule": "جدولة", "Health check": "فحص الصحة",
        "Auto-deploy": "نشر تلقائي", "Share": "مشاركة", "Versions / rollback": "الإصدارات / تراجع",
        "Restore zip": "استعادة zip", "Maintenance": "الصيانة", "Promo Codes": "رموز ترويجية",
        "Audit Log": "سجل التدقيق", "Are you sure?": "هل أنت متأكد؟", "Yes": "نعم", "Errors only": "الأخطاء فقط",
        "Full log": "السجل الكامل", "Roll back": "تراجع", "Continue": "متابعة", "Turn off": "إيقاف",
        "Turn on": "تشغيل", "Add by user ID": "إضافة بالمعرّف", "Save one now": "احفظ الآن"},
}
for _l, _d in _TR_EXTRA.items():
    _TR[_l].update(_d)

_tr_rx = {}
_lang_cache = {}


def user_lang(chat_id):
    try:
        cid = int(chat_id)
    except Exception:
        return "en"
    hit = _lang_cache.get(cid)
    if hit and time.time() - hit[1] < 120:
        return hit[0]
    r = db_execute("SELECT lang FROM users WHERE user_id = ?", (cid,), fetch='one') if cid > 0 else None
    lang = (r[0] if r and r[0] else "en")
    _lang_cache[cid] = (lang, time.time())
    return lang


def set_user_lang(user_id, lang):
    db_execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
    _lang_cache[int(user_id)] = (lang, time.time())


def _tr_text(lang, s):
    tbl = _TR.get(lang)
    if not tbl or not s:
        return s
    rx = _tr_rx.get(lang)
    if rx is None:
        keys = sorted(tbl, key=len, reverse=True)
        rx = _tr_rx[lang] = re.compile(r'(?<![\w`])(' + '|'.join(re.escape(k) for k in keys) + r')(?![\w`])')
    return rx.sub(lambda m: tbl[m.group(1)], s)


# --- full-message machine translation (cached). Dictionary above = quality override + offline fallback.
_mt_state = {"fails": 0, "until": 0.0, "last_error": "", "ok": 0}
_mt_cache = {}
_mt_lock = threading.Lock()
_TOK = "ZQ{}Z"
_CODE_OR_MARK = re.compile(r'(`[^`\n]*`)|(\*[^*\n]+\*)|((?<![\w])_[^_\n]+_(?![\w]))')
_PROT = re.compile(r'(`[^`\n]*`|https?://\S+|[\w.+-]+@[\w-]+\.[\w.-]+|@\w+|'
                   r'(?<![\w])[A-Za-z0-9_\-:.=]{20,}(?![\w])|(?<![\w.])\d[\d,.:/-]*(?!\w))')
_HAS_LETTER = re.compile(r'[^\W\d_]')


def _mt_google(text, lang):
    from urllib.parse import quote
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=" + quote(lang) +
           "&dt=t&q=" + quote(text))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=4) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _mt_libre(text, lang):
    base = os.environ["LIBRETRANSLATE_URL"].rstrip("/")
    body = {"q": text, "source": "en", "target": lang.split("-")[0], "format": "text"}
    if os.environ.get("LIBRETRANSLATE_KEY"):
        body["api_key"] = os.environ["LIBRETRANSLATE_KEY"]
    req = urllib.request.Request(base + "/translate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))["translatedText"]


def machine_translate(text, lang):
    """One string -> translated string, or None (backend down / disabled). Fails fast after repeated errors."""
    if os.environ.get("DISABLE_TRANSLATION") or time.time() < _mt_state["until"]:
        return None
    try:
        out = (_mt_libre if os.environ.get("LIBRETRANSLATE_URL") else _mt_google)(text, lang)
        if out and out.strip():
            _mt_state["fails"] = 0
            _mt_state["ok"] += 1
            return out
        raise RuntimeError("empty result")
    except Exception as e:
        _mt_state["fails"] += 1
        _mt_state["last_error"] = str(e)[:100]
        if _mt_state["fails"] >= 4:
            _mt_state["until"] = time.time() + 300
            _mt_state["fails"] = 0
        return None


def translation_status():
    if os.environ.get("DISABLE_TRANSLATION"):
        return "off"
    b = "LibreTranslate" if os.environ.get("LIBRETRANSLATE_URL") else "Google"
    if time.time() < _mt_state["until"]:
        return f"{b}: paused after errors ({_mt_state['last_error']})"
    return f"{b}: {_mt_state['ok']} ok" + (f", last error: {_mt_state['last_error']}" if _mt_state["last_error"] else "")


def _mask_line(line):
    """-> (masked, toks, pairs). toks: [(token, original)], pairs: [(open_token, close_token)]."""
    toks, pairs = [], []
    def add(orig):
        t = _TOK.format(len(toks))
        toks.append((t, orig))
        return t
    def pass1(m):
        if m.group(1):
            return m.group(1)
        span = m.group(2) or m.group(3)
        o, c = add(span[0]), add(span[-1])
        pairs.append((o, c))
        return o + span[1:-1] + c
    line = _CODE_OR_MARK.sub(pass1, line)
    line = _PROT.sub(lambda m: add(m.group(0)), line)
    return line, toks, pairs


def _unmask(out, toks, pairs):
    """Put originals back; None if the translator dropped/duplicated a token or flipped a marker pair."""
    for t, _o in toks:
        if out.count(t) != 1:
            return None
    for o, c in pairs:
        if out.index(o) > out.index(c):
            return None
    for o, c in pairs:                                   # '* word *' -> '*word*' so Markdown still parses
        out = re.sub(re.escape(o) + r'\s*', o, out)
        out = re.sub(r'\s*' + re.escape(c), c, out)
    for t, orig in toks:
        out = out.replace(t, orig)
    return re.sub(r'(?<=\S)  +(?=\S)', ' ', out)


def _cache_get(lang, key):
    k = (lang, key)
    hit = _mt_cache.get(k)
    if hit is not None:
        return hit
    r = db_execute("SELECT out FROM tr_cache WHERE lang = ? AND src = ?", (lang, key), fetch='one')
    if r:
        _mt_cache[k] = r[0]
        return r[0]
    return None


def _cache_put(lang, key, out):
    with _mt_lock:
        if len(_mt_cache) > 20000:
            _mt_cache.clear()
        _mt_cache[(lang, key)] = out
    db_execute("INSERT OR REPLACE INTO tr_cache (lang, src, out) VALUES (?, ?, ?)", (lang, key[:1500], out))


def _tr_one(lang, masked):
    """Translate one masked line (cache → dictionary override → machine). Caches machine results."""
    hit = _cache_get(lang, masked)
    if hit is not None:
        return hit
    tbl = _TR.get(lang) or {}
    bare = re.sub(r'^[^\w]+', '', masked).strip()
    if bare in tbl:
        return masked.replace(bare, tbl[bare], 1)
    out = machine_translate(masked, lang)
    if out is not None:
        _cache_put(lang, masked, out)
    return out


def _tr_batch(lang, masked_lines, deadline=6.0):
    """{masked: translated|None} — unseen lines are translated in parallel; stragglers finish in the background."""
    res, todo = {}, []
    for m in set(masked_lines):
        hit = _cache_get(lang, m)
        if hit is not None:
            res[m] = hit
        else:
            todo.append(m)
    if todo:
        from concurrent.futures import ThreadPoolExecutor, wait
        ex = ThreadPoolExecutor(max_workers=min(8, len(todo)))
        futs = {ex.submit(_tr_one, lang, m): m for m in todo}
        done, _pending = wait(futs, timeout=deadline)
        for f in done:
            try:
                res[futs[f]] = f.result()
            except Exception:
                res[futs[f]] = None
        for f in _pending:
            res[futs[f]] = None
        ex.shutdown(wait=False)
    return res


def _prep_lines(text):
    """Split text into [(kind, payload)]: 'raw' (code fences / blank / untranslatable) or 'line' (lead, masked, toks, pairs, trail)."""
    items = []
    in_fence = False
    for raw in str(text).split("\n"):
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            items.append(("raw", raw)); continue
        if in_fence or not raw.strip():
            items.append(("raw", raw)); continue
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        masked, toks, pairs = _mask_line(raw.strip())
        probe = masked
        for t, _o in toks:
            probe = probe.replace(t, "")
        if not _HAS_LETTER.search(probe):
            items.append(("raw", raw)); continue
        items.append(("line", (lead, masked, toks, pairs, trail, raw)))
    return items


def localize(chat_id, text, keyboard):
    """Translate a whole message (and its buttons) into the user's language. Never raises; falls back to English."""
    try:
        lang = user_lang(chat_id)
        if lang == "en" or lang not in LANGS:
            return text, keyboard
        items = _prep_lines(text)
        btns = []
        if keyboard and keyboard.get("inline_keyboard"):
            for row in keyboard["inline_keyboard"]:
                for b in row:
                    if str(b.get("callback_data", "")).startswith("fp_setlang_"):
                        btns.append(("raw", b.get("text", "")))          # language names stay in their own language
                    else:
                        btns.append(_prep_lines(b.get("text", ""))[0])
        want = [it[1][1] for it in items + btns if it[0] == "line"]
        res = _tr_batch(lang, want) if want else {}

        def render(it):
            if it[0] == "raw":
                return it[1]
            lead, masked, toks, pairs, trail, raw = it[1]
            out = res.get(masked)
            done = _unmask(out, toks, pairs) if out else None
            if done is None:                              # offline / bad output → dictionary → English
                return _tr_text(lang, raw) if lang in _TR else raw
            return lead + done.strip() + trail
        text = "\n".join(render(it) for it in items)
        if btns:
            it_iter = iter(btns)
            keyboard = {"inline_keyboard": [[dict(b, text=render(next(it_iter))) for b in row]
                                            for row in keyboard["inline_keyboard"]]}
    except Exception as e:
        print(f"⚠️ localize: {e}")
    return text, keyboard


# ---------------------------------------------------------------- sharing / permissions
_SHARE_ALLOWED = re.compile(r'^(view_deploy|restart_deploy|stop_deploy|view_runtime_logs|view_install_logs|'
                            r'fp_logs|fp_lgm|fp_lgk|fp_usage)_(\d+)(?:_\d+)?$')


def _fp_share_remap(callback):
    """If a collaborator presses an allowed button for a bot shared with them, act as the owner for that call."""
    try:
        data = callback.get('data') or ''
        m = _SHARE_ALLOWED.match(data)
        if not m:
            return callback
        uid = callback['from']['id']
        dep_id = int(m.group(2))
        d = _fp_dep(dep_id)
        if not d or d["owner"] == uid or get_ban_info(uid):
            return callback
        if not db_execute("SELECT 1 FROM dep_shares WHERE deployment_id = ? AND user_id = ?", (dep_id, uid), fetch='one'):
            return callback
        cb = dict(callback)
        cb['from'] = dict(callback['from'], id=d["owner"])
        _fp_ctx.shared = True
        audit(uid, "shared_action", dep_id, data)
        return cb
    except Exception:
        return callback


def _fp_filter_shared_kb(keyboard, dep_id):
    """For a collaborator: keep only the actions they're allowed to use."""
    if not getattr(_fp_ctx, 'shared', False) or not keyboard:
        return keyboard
    keep = []
    for row in keyboard["inline_keyboard"]:
        row = [b for b in row if _SHARE_ALLOWED.match(b.get("callback_data", "")) and not b["callback_data"].startswith("edit_")]
        if row:
            keep.append(row)
    keep.append([_fp_btn("🔎 Search logs", f"fp_logs_{dep_id}"), _fp_btn("📈 Usage", f"fp_usage_{dep_id}")])
    keep.append([_fp_btn("🤝 Shared with me", "fp_shared")])
    return {"inline_keyboard": keep}


# ---------------------------------------------------------------- confirmation gate
_CONFIRM = (
    (re.compile(r'^stop_deploy_(\d+)$'), "🛑 *Stop this bot?*\n\nIt will go offline until you restart it.", "view_deploy_{0}"),
    (re.compile(r'^edit_envdel_(\d+)_\w+$'), "🗑️ *Delete this environment variable?*", "edit_env_{0}"),
    (re.compile(r'^fp_pcdel_([A-Za-z0-9_-]{3,24})$'),
     "🗑️ *Delete this promo code?*\n\nIt stops working immediately and any unused discount it gave users is removed.",
     "fp_apromo"),
)


def _fp_confirm_gate(callback):
    """-> True if a confirmation prompt was shown (caller stops). 'fpc:<data>' = confirmed → unwraps."""
    data = callback.get('data') or ''
    msg = callback.get('message') or {}
    chat_id, mid = (msg.get('chat') or {}).get('id'), msg.get('message_id')
    if data.startswith("fpc:"):
        callback['data'] = data[4:]
        return False
    for rx, text, back in _CONFIRM:
        m = rx.match(data)
        if m and len(data) < 58:
            answer_callback(callback['id'])
            edit_message(chat_id, mid, text, _fp_kb([_fp_btn("✅ Yes", "fpc:" + data),
                                                    _fp_btn("❌ Cancel", back.format(*m.groups()))]))
            return True
    return False


# ---------------------------------------------------------------- audit of admin callbacks
_ADMIN_ACT = re.compile(r'(approve|reject|unban|ban|delete|revoke|grant|add_|create|restore|broadcast|confirm|'
                        r'toggle|refund|reset|remove|send|kick|reply|resolve)', re.I)


def _fp_audit_cb(uid, data):
    if is_admin(uid) and data.startswith(("admin_", "sec_", "bug_")) and _ADMIN_ACT.search(data):
        audit(uid, "admin_click", data[:40], "")


# ---------------------------------------------------------------- UI screens
def fp_more_menu(chat_id, message_id):
    edit_message(chat_id, message_id, "*🧰 MORE*\n\nExtras for your account.", _fp_kb(
        [_fp_btn("🎟️ Promo Code", "fp_promo"), _fp_btn("🧾 Payment History", "fp_history")],
        [_fp_btn("🌐 Language", "fp_lang"), _fp_btn("🤝 Shared with me", "fp_shared")],
        *_p2_more_rows(),
        [_fp_btn("🔙 Menu", "main_menu")]))


def fp_tools_menu(chat_id, message_id, uid, dep_id):
    d = _fp_own(uid, dep_id)
    if not d:
        edit_message(chat_id, message_id, "❌ Not found", _fp_kb([_fp_btn("🔙 Back", "my_deployments")]))
        return
    st = _fp_settings(dep_id)
    sched = f"every {st['restart_hours']}h" if st["restart_hours"] else "off"
    edit_message(chat_id, message_id, f"*🛠 TOOLS — #{dep_id}*\n\n"
                 f"⏰ Scheduled restart: `{sched}`\n🩺 Health check: `{'on' if st['health_url'] else 'off'}`\n"
                 f"🔗 Auto-deploy: `{'on' if st['auto'] else 'off'}`\n"
                 f"🕘 Rollback points: `{len(snapshot_list(dep_id))}`",
                 _fp_kb([_fp_btn("🔎 Search logs", f"fp_logs_{dep_id}"), _fp_btn("📈 Usage", f"fp_usage_{dep_id}")],
                        [_fp_btn("⏰ Schedule", f"fp_sched_{dep_id}"), _fp_btn("🩺 Health check", f"fp_health_{dep_id}")],
                        [_fp_btn("🔗 Auto-deploy", f"fp_hook_{dep_id}"), _fp_btn("🤝 Share", f"fp_share_{dep_id}")],
                        [_fp_btn("🕘 Versions / rollback", f"fp_ver_{dep_id}"), _fp_btn("♻️ Restore zip", f"fp_restore_{dep_id}")],
                        [_fp_btn(f"🗜 Ultra compress: {'ON' if st['compress'] else 'OFF'}", f"fp_comp_{dep_id}")],
                        [_fp_btn("🔍 Scan for secrets", f"p2_scan_{dep_id}"),
                         _fp_btn(f"🛡 Safe deploy: {'OFF' if _p2_safe_off(dep_id) else 'ON'}", f"p2_safe_{dep_id}")],
                        [_fp_btn("🌍 Public URL", f"p2_pub_{dep_id}"),
                         _fp_btn(f"🔁 Auto-renew: {'ON' if p2_autorenew_on(dep_id) else 'OFF'}", f"p2_ren_{dep_id}")],
                        [_fp_btn("🔙 Deployment", f"view_deploy_{dep_id}")]))


def fp_sched_menu(chat_id, message_id, dep_id, note=""):
    st = _fp_settings(dep_id)
    cur = st["restart_hours"]
    mk = lambda h, t: _fp_btn(("✅ " if cur == h else "") + t, f"fp_schedset_{dep_id}_{h}")
    edit_message(chat_id, message_id, f"*⏰ SCHEDULED RESTART*\n\nRestart this bot automatically on a timer — handy "
                 f"for bots that leak memory or need a fresh start.\n\nNow: `{'every ' + str(cur) + 'h' if cur else 'off'}`{note}",
                 _fp_kb([mk(0, "Off"), mk(6, "6h"), mk(12, "12h")], [mk(24, "24h"), mk(72, "3 days"), mk(168, "7 days")],
                        [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))


def fp_health_menu(chat_id, message_id, dep_id):
    d, st = _fp_dep(dep_id), _fp_settings(dep_id)
    cur = st["health_url"]
    shown = ("this bot's own PORT" if cur == "local" else cur) if cur else "off"
    port = d["env"].get("PORT") or d["env"].get("BOT_PORT")
    rows = [[_fp_btn("🌐 Set URL", f"fp_healthurl_{dep_id}")]]
    if port:
        rows.append([_fp_btn(f"🔌 Use my PORT ({port})", f"fp_healthlocal_{dep_id}")])
    if cur:
        rows.append([_fp_btn("🗑️ Turn off", f"fp_healthoff_{dep_id}")])
    has_tok = bool(_telegram_token(d["env"]))
    if has_tok:
        rows.append([_fp_btn(("🟢 Hang detection: ON" if not st["hang_off"] else "⚪ Hang detection: OFF"), f"fp_hangtog_{dep_id}")])
    rows.append([_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")])
    edit_message(chat_id, message_id, f"*🩺 HEALTH CHECK*\n\nIf your bot serves a web page/endpoint, I'll ping it every "
                 f"2 minutes. After 3 failures in a row I restart the bot and send you the last log lines. "
                 f"A zombie (dead-but-listed) process also triggers a restart.\n\n"
                 f"*Telegram polling bots* (no web page) are covered too: if I see your bot's update queue growing for "
                 f"~6 minutes it isn't reading messages, so I restart it ({'auto-detected token ✅' if has_tok else 'no bot token found in env'}).\n\n"
                 f"Now: `{shown}`\n\n"
                 f"_Only public http(s) URLs are allowed._", _fp_kb(*rows))


def fp_hook_menu(chat_id, message_id, dep_id):
    d, st = _fp_dep(dep_id), _fp_settings(dep_id)
    if d["source"] != "github" and not d["repo"]:
        edit_message(chat_id, message_id, "*🔗 AUTO-DEPLOY*\n\nThis works for bots deployed *from GitHub*. "
                     "Redeploy this bot from a repo to use it.", _fp_kb([_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))
        return
    base = _public_base_url() or "https://YOUR-HOST.onrender.com"
    info = ""
    if st["auto"]:
        info = (f"\n\n*Add this webhook in GitHub → Settings → Webhooks:*\n"
                f"Payload URL:\n`{base}/gh/{dep_id}`\nContent type: `application/json`\n"
                f"Secret:\n`{st['secret']}`\nEvents: *Just the push event*")
    edit_message(chat_id, message_id, f"*🔗 AUTO-DEPLOY ON PUSH*\n\nRepo: `{d['repo']}` (branch `{d['branch']}`)\n"
                 f"Status: `{'ON' if st['auto'] else 'OFF'}`\n\nEvery push redeploys the bot — checked by the security "
                 f"scan first, with an automatic rollback point.{info}",
                 _fp_kb([_fp_btn("🔴 Turn off" if st["auto"] else "🟢 Turn on", f"fp_hooktog_{dep_id}")],
                        *([[_fp_btn("🔑 New secret", f"fp_hooknew_{dep_id}")]] if st["auto"] else []),
                        [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))


def fp_logs_menu(chat_id, message_id, dep_id):
    edit_message(chat_id, message_id, f"*🔎 LOG SEARCH — #{dep_id}*\n\nSearch the runtime log or filter it.",
                 _fp_kb([_fp_btn("🔤 Search keyword", f"fp_lgk_{dep_id}")],
                        [_fp_btn("❗ Errors only", f"fp_lgm_{dep_id}_1"), _fp_btn("⚠️ Warnings+", f"fp_lgm_{dep_id}_2")],
                        [_fp_btn("📥 Full log", f"view_runtime_logs_{dep_id}")],
                        [_fp_btn("🔙 Back", f"view_deploy_{dep_id}")]))


def fp_share_menu(chat_id, message_id, uid, dep_id):
    d = _fp_own(uid, dep_id)
    if not d:
        return
    rows = db_execute("SELECT s.user_id, COALESCE(u.username, u.first_name, '') FROM dep_shares s "
                      "LEFT JOIN users u ON u.user_id = s.user_id WHERE s.deployment_id = ?", (dep_id,), fetch='all') or []
    btns = [[_fp_btn(f"❌ {(n or '')[:14]} ({u})", f"fp_unshare_{dep_id}_{u}")] for u, n in rows]
    edit_message(chat_id, message_id, f"*🤝 SHARE BOT #{dep_id}*\n\nCo-owners can view the bot, read its logs and usage, "
                 f"and stop/restart it. They can't edit, delete or see your env vars.\n\nShared with: "
                 f"`{len(rows)}`", _fp_kb(*btns, [_fp_btn("➕ Add by user ID", f"fp_shareadd_{dep_id}")],
                                          [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))


def fp_versions_menu(chat_id, message_id, dep_id):
    snaps = snapshot_list(dep_id)
    rows = [[_fp_btn(f"⏪ {_snap_label(p)}"[:40], f"fp_rb_{dep_id}_{i}")] for i, p in enumerate(snaps)]
    edit_message(chat_id, message_id, f"*🕘 VERSIONS — #{dep_id}*\n\nRollback points are saved automatically before "
                 f"edits, restores and auto-deploys (last {SNAP_KEEP}).\n\n" + ("" if snaps else "_None yet._"),
                 _fp_kb(*rows, [_fp_btn("📸 Save one now", f"fp_snap_{dep_id}")], [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))


def fp_shared_list(chat_id, message_id, uid):
    rows = db_execute("SELECT s.deployment_id, d.file_name, d.status FROM dep_shares s JOIN deployments d "
                      "ON d.deployment_id = s.deployment_id WHERE s.user_id = ?", (uid,), fetch='all') or []
    btns = [[_fp_btn(f"{'🟢' if st == 'active' else '🔴'} #{i} · {display_filename(f)}"[:40], f"view_deploy_{i}")]
            for i, f, st in rows]
    edit_message(chat_id, message_id, "*🤝 SHARED WITH ME*\n\n" + ("" if rows else "Nobody has shared a bot with you yet."),
                 _fp_kb(*btns, [_fp_btn("🔙 More", "fp_more")]))


def fp_lang_menu(chat_id, message_id, uid):
    cur = user_lang(uid)
    items = [_fp_btn(("✅ " if c == cur else "") + n, f"fp_setlang_{c}") for c, n in LANGS.items()]
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    edit_message(chat_id, message_id, "*🌐 LANGUAGE*\n\nChoose your language — every message and button is translated "
                 "automatically.", _fp_kb(*rows, [_fp_btn("🔙 More", "fp_more")]))


def fp_admin_promo_menu(chat_id, message_id):
    rows = db_execute("SELECT code, kind, value, max_uses, used, active FROM promo_codes ORDER BY rowid DESC LIMIT 10",
                      fetch='all') or []
    lines = [f"{'✅' if a else '❌'} `{c}` · {k} {v}{'%' if k == 'discount' else '🪙'} · {u}/{m or '∞'}"
             for c, k, v, m, u, a in rows]
    btns = [[_fp_btn(f"{'⏸ Disable' if a else '▶️ Enable'} {c}", f"fp_pctog_{c}"),
             _fp_btn("🗑 Delete", f"fp_pcdel_{c}")] for c, k, v, m, u, a in rows[:8]]
    edit_message(chat_id, message_id, "*🏷️ PROMO CODES*\n\n" + ("\n".join(lines) or "_None yet._"),
                 _fp_kb([_fp_btn("➕ New code", "fp_pcnew")], *btns, [_fp_btn("🔙 Admin Panel", "admin_panel")]))


def fp_admin_audit(chat_id, message_id, page=0):
    page = max(0, int(page))
    rows = db_execute("SELECT ts, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT 12 OFFSET ?",
                      (page * 12,), fetch='all') or []
    lines = [f"`{ts[5:16]}` `{a}` {act} {tg} {dt}".strip()[:110] for ts, a, act, tg, dt in rows]
    nav = []
    if page:
        nav.append(_fp_btn("⬅️ Newer", f"fp_audit_{page - 1}"))
    if len(rows) == 12:
        nav.append(_fp_btn("Older ➡️", f"fp_audit_{page + 1}"))
    edit_message(chat_id, message_id, "*🧾 AUDIT LOG*\n\n" + ("\n".join(lines) or "_Empty._"),
                 _fp_kb(nav, [_fp_btn("🔙 Admin Panel", "admin_panel")]) if nav else
                 _fp_kb([_fp_btn("🔙 Admin Panel", "admin_panel")]))


def _fp_admin_rows():
    on = maintenance_on()
    return [[_fp_btn(f"🛠 Maintenance: {'ON 🔴' if on else 'OFF 🟢'}", "fp_maint")],
            [_fp_btn("🏷️ Promo Codes", "fp_apromo"), _fp_btn("🧾 Audit Log", "fp_audit_0")],
            [_fp_btn("🧪 Test translation", "fp_trtest")], *_P2_ADMIN_ROWS]


# ---------------------------------------------------------------- callback router
def _fp_pre_callback(callback):
    """Runs first for every callback. -> True if fully handled here."""
    data = callback.get('data') or ''
    uid = callback['from']['id']
    if get_ban_info(uid):
        return False
    msg = callback.get('message') or {}
    chat_id, mid = (msg.get('chat') or {}).get('id'), msg.get('message_id')

    step = get_user_step(uid).get('step') or ''
    if step.startswith('fp_'):
        set_user_step(uid, None)              # leaving a prompt cancels it

    if not is_admin(uid) and maintenance_on() and data.startswith(_MAINT_BLOCKED):
        answer_callback(callback['id'])
        edit_message(chat_id, mid, MAINT_TEXT, _fp_kb([_fp_btn("🏠 Menu", "main_menu")]))
        return True

    _fp_audit_cb(uid, data)
    if _fp_confirm_gate(callback):
        return True
    data = callback.get('data') or ''
    if _p2_pin_gate(data, uid, chat_id, mid):
        answer_callback(callback['id'])
        return True
    if data.startswith("p2_"):
        answer_callback(callback['id'])
        try:
            return _p2_route(callback, data, uid, chat_id, mid)
        except Exception as e:
            print(f"❌ p2 route {data}: {e}")
            traceback.print_exc()
            send_message(chat_id, "⚠️ Something went wrong with that. Please try again.")
            return True
    if not data.startswith("fp_"):
        return False

    answer_callback(callback['id'])
    try:
        return _fp_route(callback, data, uid, chat_id, mid)
    except Exception as e:
        print(f"❌ fp route {data}: {e}")
        traceback.print_exc()
        send_message(chat_id, "⚠️ Something went wrong with that. Please try again.")
        return True


def _fp_route(callback, data, uid, chat_id, mid):
    p = data.split("_")
    cmd = p[1] if len(p) > 1 else ""
    dep_id = int(p[2]) if len(p) > 2 and p[2].isdigit() else None

    # ---- account-level
    if data == "fp_more":
        fp_more_menu(chat_id, mid); return True
    if data == "fp_promo":
        set_user_step(uid, 'fp_promo')
        edit_message(chat_id, mid, "*🎟️ PROMO CODE*\n\nSend your promo code as a message.",
                     _fp_kb([_fp_btn("❌ Cancel", "fp_more")])); return True
    if data == "fp_history":
        edit_message(chat_id, mid, payment_history_text(uid),
                     _fp_kb([_fp_btn("📄 Download receipts", "fp_receipts")], [_fp_btn("🔙 More", "fp_more")])); return True
    if data == "fp_receipts":
        send_receipts_file(chat_id, uid); return True
    if data == "fp_lang":
        fp_lang_menu(chat_id, mid, uid); return True
    if cmd == "setlang" and len(p) == 3 and p[2] in LANGS:
        set_user_lang(uid, p[2]); fp_lang_menu(chat_id, mid, uid); return True
    if data == "fp_shared":
        fp_shared_list(chat_id, mid, uid); return True

    # ---- admin-level
    if data in ("fp_maint", "fp_apromo", "fp_pcnew", "fp_trtest") or cmd in ("audit", "pctog", "pcdel"):
        if not is_admin(uid):
            return True
        if data == "fp_trtest":
            r = machine_translate("Hello! Your bot is running.", "es")
            edit_message(chat_id, mid, f"*🧪 TRANSLATION TEST*\n\nEnglish → Spanish:\n`{r or 'FAILED'}`\n\n"
                         f"Status: `{translation_status()}`", _fp_kb([_fp_btn("🔙 Admin Panel", "admin_panel")]))
        elif data == "fp_maint":
            kv_set("maintenance", "0" if maintenance_on() else "1")
            audit(uid, "maintenance", "on" if maintenance_on() else "off")
            show_admin_panel(chat_id, mid)
        elif data == "fp_apromo":
            fp_admin_promo_menu(chat_id, mid)
        elif data == "fp_pcnew":
            set_user_step(uid, 'fp_pcnew')
            edit_message(chat_id, mid, "*➕ NEW PROMO CODE*\n\nSend one line:\n`CODE KIND VALUE [MAX_USES] [DAYS]`\n\n"
                         "KIND = `coins` (bonus coins) or `discount` (percent off the user's next coin payment)\n"
                         "Examples:\n`WELCOME50 coins 50 100 30`\n`SAVE20 discount 20 50`",
                         _fp_kb([_fp_btn("❌ Cancel", "fp_apromo")]))
        elif cmd == "pcdel":
            code = "_".join(p[2:]).upper()
            existed = db_execute("SELECT 1 FROM promo_codes WHERE code = ?", (code,), fetch='one')
            db_execute("DELETE FROM promo_codes WHERE code = ?", (code,))
            db_execute("DELETE FROM promo_redemptions WHERE code = ?", (code,))
            db_execute("DELETE FROM user_discounts WHERE code = ?", (code,))
            if existed:
                audit(uid, "promo_delete", code)
            fp_admin_promo_menu(chat_id, mid)
        elif cmd == "pctog":
            code = "_".join(p[2:])
            db_execute("UPDATE promo_codes SET active = 1 - active WHERE code = ?", (code,))
            audit(uid, "promo_toggle", code)
            fp_admin_promo_menu(chat_id, mid)
        else:
            fp_admin_audit(chat_id, mid, int(p[2]) if len(p) > 2 and p[2].isdigit() else 0)
        return True

    # ---- per-deployment (owner/admin)
    if dep_id is None:
        return True
    if cmd in ("logs", "usage", "lgm", "lgk"):      # also reachable by collaborators (remapped to owner)
        d = _fp_own(uid, dep_id)
        if not d:
            edit_message(chat_id, mid, "❌ Not found", _fp_kb([_fp_btn("🔙 Back", "my_deployments")])); return True
        if cmd == "logs":
            fp_logs_menu(chat_id, mid, dep_id)
        elif cmd == "lgk":
            set_user_step(uid, 'fp_lgk', edit_dep=dep_id)
            edit_message(chat_id, mid, "🔤 Send the text to search for in the log.",
                         _fp_kb([_fp_btn("❌ Cancel", f"fp_logs_{dep_id}")]))
        elif cmd == "lgm":
            mode = "err" if (len(p) > 3 and p[3] == "1") else "warn"
            edit_message(chat_id, mid, log_search_text(d["folder"], mode=mode),
                         _fp_kb([_fp_btn("🔙 Log search", f"fp_logs_{dep_id}")]))
        else:
            hrs = 168 if (len(p) > 3 and p[3] == "168") else 24
            edit_message(chat_id, mid, usage_text(dep_id, hrs),
                         _fp_kb([_fp_btn("24h", f"fp_usage_{dep_id}_24"), _fp_btn("7d", f"fp_usage_{dep_id}_168")],
                                [_fp_btn("🔙 Back", f"view_deploy_{dep_id}")]))
        return True

    d = _fp_own(uid, dep_id)
    if not d:
        edit_message(chat_id, mid, "❌ Not found", _fp_kb([_fp_btn("🔙 Back", "my_deployments")])); return True

    if cmd == "tools":
        fp_tools_menu(chat_id, mid, uid, dep_id)
    elif cmd == "comp":
        st = _fp_settings(dep_id)
        edit_message(chat_id, mid, f"*🗜 ULTRA COMPRESS — #{dep_id}*\n\nStatus: `{'ON' if st['compress'] else 'OFF'}`\n\n"
                     "Squeezes the bot's RAM and CPU as far as it will go:\n"
                     "• memory handed back to the system every ~45s, one memory arena, startup objects frozen\n"
                     "• one CPU core, lowest priority, single-threaded math libraries\n"
                     "• Node.js heap capped at 128 MB, no bytecode files, asserts stripped\n\n"
                     "_Trade-off: the bot may respond a little slower under heavy load, and a Node.js bot needing more "
                     "than 128 MB of heap will crash — turn it off if that happens. Takes effect on the next restart._",
                     _fp_kb([_fp_btn("🔴 Turn off" if st["compress"] else "🟢 Turn on", f"fp_compset_{dep_id}")],
                            [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))
    elif cmd == "compset":
        st = _fp_settings(dep_id)
        db_execute("UPDATE dep_settings SET compress = ? WHERE deployment_id = ?", (0 if st["compress"] else 1, dep_id))
        audit(uid, "compress", dep_id, "off" if st["compress"] else "on")
        edit_message(chat_id, mid, f"✅ Ultra compress is now *{'OFF' if st['compress'] else 'ON'}* for bot #{dep_id}.\n\n"
                     "Restart the bot to apply it.", _fp_kb([_fp_btn("🔄 Restart now", f"fp_comprs_{dep_id}")],
                                                            [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))
    elif cmd == "comprs":
        res = _fp_restart(dep_id, "compress")
        edit_message(chat_id, mid, f"🔄 Restart: `{res}`", _fp_kb([_fp_btn("📄 Deployment", f"view_deploy_{dep_id}")]))
    elif cmd == "sched":
        fp_sched_menu(chat_id, mid, dep_id)
    elif cmd == "schedset" and len(p) > 3:
        hrs = int(p[3]) if p[3].isdigit() and int(p[3]) in (0, 6, 12, 24, 72, 168) else 0
        db_execute("UPDATE dep_settings SET restart_hours = ?, last_sched = ? WHERE deployment_id = ?",
                   (hrs, datetime.now().isoformat() if hrs else None, dep_id))
        _fp_settings(dep_id)
        fp_sched_menu(chat_id, mid, dep_id, "\n\n✅ Saved." if hrs else "\n\n✅ Turned off.")
    elif cmd == "health":
        fp_health_menu(chat_id, mid, dep_id)
    elif cmd == "healthurl":
        set_user_step(uid, 'fp_healthurl', edit_dep=dep_id)
        edit_message(chat_id, mid, "🌐 Send the public URL to ping (e.g. `https://mybot.example.com/health`).",
                     _fp_kb([_fp_btn("❌ Cancel", f"fp_health_{dep_id}")]))
    elif cmd == "healthlocal":
        db_execute("UPDATE dep_settings SET health_url = 'local', health_fails = 0 WHERE deployment_id = ?", (dep_id,))
        fp_health_menu(chat_id, mid, dep_id)
    elif cmd == "hangtog":
        db_execute("UPDATE dep_settings SET hang_off = 1 - COALESCE(hang_off, 0) WHERE deployment_id = ?", (dep_id,))
        _pending_hist.pop(("s", dep_id), None)
        fp_health_menu(chat_id, mid, dep_id)
    elif cmd == "healthoff":
        db_execute("UPDATE dep_settings SET health_url = NULL, health_fails = 0 WHERE deployment_id = ?", (dep_id,))
        fp_health_menu(chat_id, mid, dep_id)
    elif cmd == "hook":
        fp_hook_menu(chat_id, mid, dep_id)
    elif cmd in ("hooktog", "hooknew"):
        st = _fp_settings(dep_id)
        if cmd == "hooknew" or not st["auto"]:
            db_execute("UPDATE dep_settings SET hook_secret = ? WHERE deployment_id = ?",
                       (secrets.token_urlsafe(18), dep_id))
        if cmd == "hooktog":
            db_execute("UPDATE dep_settings SET auto_deploy = ? WHERE deployment_id = ?", (0 if st["auto"] else 1, dep_id))
        fp_hook_menu(chat_id, mid, dep_id)
    elif cmd == "share":
        fp_share_menu(chat_id, mid, uid, dep_id)
    elif cmd == "shareadd":
        set_user_step(uid, 'fp_shareadd', edit_dep=dep_id)
        edit_message(chat_id, mid, "➕ Send the Telegram *user ID* of the person to share with (they must have used "
                     "this bot).", _fp_kb([_fp_btn("❌ Cancel", f"fp_share_{dep_id}")]))
    elif cmd == "unshare" and len(p) > 3 and p[3].lstrip('-').isdigit():
        db_execute("DELETE FROM dep_shares WHERE deployment_id = ? AND user_id = ?", (dep_id, int(p[3])))
        audit(uid, "unshare", dep_id, p[3])
        fp_share_menu(chat_id, mid, uid, dep_id)
    elif cmd == "ver":
        fp_versions_menu(chat_id, mid, dep_id)
    elif cmd == "snap":
        ok = snapshot_create(dep_id, "manual")
        send_message(chat_id, "📸 Rollback point saved." if ok else "ℹ️ Nothing to save yet.")
        fp_versions_menu(chat_id, mid, dep_id)
    elif cmd == "rb" and len(p) > 3:
        snaps = snapshot_list(dep_id)
        i = int(p[3])
        if i < len(snaps):
            edit_message(chat_id, mid, f"⏪ *Roll back to* `{_snap_label(snaps[i])}`?\n\nCurrent files are saved as a new "
                         f"rollback point first.", _fp_kb([_fp_btn("✅ Roll back", f"fp_rbok_{dep_id}_{i}"),
                                                          _fp_btn("❌ Cancel", f"fp_ver_{dep_id}")]))
    elif cmd == "rbok" and len(p) > 3:
        snaps = snapshot_list(dep_id)
        i = int(p[3])
        if i < len(snaps):
            ok, m = apply_zip_to_deployment(d, snaps[i], "rollback")
            if ok:
                m += "\n" + _edit_push(dep_id, "rollback")
            edit_message(chat_id, mid, m, _edit_after_kb(dep_id) if ok else _fp_kb([_fp_btn("🔙 Versions", f"fp_ver_{dep_id}")]))
    elif cmd == "restore":
        if len(p) > 3 and p[3] == "go":
            set_user_step(uid, 'fp_restore', edit_dep=dep_id)
            edit_message(chat_id, mid, "♻️ Send your backup `.zip` (from *Download Backup*).",
                         _fp_kb([_fp_btn("❌ Cancel", f"fp_tools_{dep_id}")]))
        else:
            edit_message(chat_id, mid, "♻️ *Restore from a zip?*\n\nFiles in the zip overwrite the current ones (a rollback "
                         "point is saved first). It's scanned like any upload.",
                         _fp_kb([_fp_btn("✅ Continue", f"fp_restore_{dep_id}_go"), _fp_btn("❌ Cancel", f"fp_tools_{dep_id}")]))
    return True


# ---------------------------------------------------------------- text / document steps
def _fp_text(message, user_step):
    """Text input for fp_* prompts. -> True if handled."""
    step = user_step.get('step') or ''
    if not step.startswith('fp_'):
        return False
    if step in _P2_TEXT:
        return _p2_text(message, user_step)
    chat_id, uid = message['chat']['id'], message['from']['id']
    text = (message.get('text') or '').strip()
    if text.startswith('/'):
        return False
    dep_id = int(user_step['edit_dep']) if str(user_step.get('edit_dep') or '').isdigit() else None
    set_user_step(uid, None)

    if step == 'fp_promo':
        ok, m = redeem_promo(uid, text)
        send_message(chat_id, m, _fp_kb([_fp_btn("🔙 More", "fp_more")]))
    elif step == 'fp_pcnew' and is_admin(uid):
        parts = text.split()
        try:
            code, kind, val = parts[0].upper(), parts[1].lower(), int(parts[2])
            mx = int(parts[3]) if len(parts) > 3 else 0
            days = int(parts[4]) if len(parts) > 4 else 0
            assert kind in ("coins", "discount") and val > 0 and re.fullmatch(r'[A-Z0-9_-]{3,24}', code)
            if kind == "discount":
                val = min(val, 90)
        except Exception:
            send_message(chat_id, "❌ Format: `CODE coins|discount VALUE [MAX_USES] [DAYS]`",
                         _fp_kb([_fp_btn("➕ Try again", "fp_pcnew")]))
            return True
        exp = (datetime.now() + timedelta(days=days)).isoformat() if days else None
        db_execute("INSERT OR REPLACE INTO promo_codes (code, kind, value, max_uses, used, expires_at, active, created_by) "
                   "VALUES (?, ?, ?, ?, COALESCE((SELECT used FROM promo_codes WHERE code = ?), 0), ?, 1, ?)",
                   (code, kind, val, mx, code, exp, uid))
        audit(uid, "promo_create", code, f"{kind} {val} max={mx} days={days}")
        send_message(chat_id, f"✅ Promo `{code}` created.", _fp_kb([_fp_btn("🏷️ Promo codes", "fp_apromo")]))
    elif step == 'fp_lgk' and dep_id:
        d = _fp_own(uid, dep_id) or (_fp_dep(dep_id) if db_execute(
            "SELECT 1 FROM dep_shares WHERE deployment_id = ? AND user_id = ?", (dep_id, uid), fetch='one') else None)
        if d:
            send_message(chat_id, log_search_text(d["folder"], text[:80]), _fp_kb([_fp_btn("🔙 Log search", f"fp_logs_{dep_id}")]))
    elif step == 'fp_healthurl' and dep_id and _fp_own(uid, dep_id):
        if _url_is_public(text):
            db_execute("UPDATE dep_settings SET health_url = ?, health_fails = 0 WHERE deployment_id = ?", (text[:300], dep_id))
            send_message(chat_id, "✅ Health check saved.", _fp_kb([_fp_btn("🩺 Health check", f"fp_health_{dep_id}")]))
        else:
            send_message(chat_id, "❌ That must be a *public* http(s) URL (private/internal addresses aren't allowed).",
                         _fp_kb([_fp_btn("🔁 Try again", f"fp_healthurl_{dep_id}")]))
    elif step == 'fp_shareadd' and dep_id and _fp_own(uid, dep_id):
        d = _fp_dep(dep_id)
        if text.lstrip('-').isdigit() and db_execute("SELECT 1 FROM users WHERE user_id = ?", (int(text),), fetch='one') \
                and int(text) != d["owner"] and not get_ban_info(int(text)):
            db_execute("INSERT OR IGNORE INTO dep_shares (deployment_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
                       (dep_id, int(text), uid, datetime.now().isoformat(timespec='seconds')))
            audit(uid, "share", dep_id, text)
            send_message(int(text), f"🤝 A bot (#{dep_id}) was shared with you. You can view it, read its logs/usage and "
                                    f"restart it.", _fp_kb([_fp_btn("🤝 Shared with me", "fp_shared")]))
            send_message(chat_id, "✅ Shared.", _fp_kb([_fp_btn("🤝 Sharing", f"fp_share_{dep_id}")]))
        else:
            send_message(chat_id, "❌ No such user (they must have started this bot).",
                         _fp_kb([_fp_btn("🔁 Try again", f"fp_shareadd_{dep_id}")]))
    return True


def _fp_document(message, user_step, doc, file_name):
    """Documents for fp_restore, and .env files for any env-entry step. -> True if handled."""
    chat_id, uid = message['chat']['id'], message['from']['id']
    step = user_step.get('step') or ''
    lname = (file_name or '').lower()
    if step == 'fp_restore' and user_step.get('edit_dep'):
        dep_id = int(user_step['edit_dep'])
        d = _fp_own(uid, dep_id)
        if not d:
            set_user_step(uid, None); send_message(chat_id, "❌ Deployment not found"); return True
        if not lname.endswith('.zip'):
            send_message(chat_id, "❌ Please send a `.zip` backup."); return True
        raw = _download_telegram_file_bytes(doc.get("file_id"))
        if raw is None:
            send_message(chat_id, "❌ Could not download the file from Telegram."); return True
        set_user_step(uid, None)
        tmpz = Path(tempfile.mkdtemp(prefix="rz_")) / "r.zip"
        try:
            tmpz.write_bytes(raw)
            ok, m = apply_zip_to_deployment(d, tmpz, "restore")
        finally:
            shutil.rmtree(tmpz.parent, ignore_errors=True)
        if ok:
            m += "\n" + _edit_push(dep_id, "restore")
            audit(uid, "restore_zip", dep_id)
        send_message(chat_id, m, _edit_after_kb(dep_id) if ok else _fp_kb([_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))
        return True
    env_name = lname in ('.env', 'env', 'env.txt', 'dotenv') or lname.endswith('.env')
    wants_env = step in ('edit_env', 'awaiting_env', 'awaiting_env_vars') or user_step.get('waiting_for_env') == 1
    if env_name and wants_env:
        raw = _download_telegram_file_bytes(doc.get("file_id"))
        if raw is None:
            send_message(chat_id, "❌ Could not download the file from Telegram."); return True
        fake = {k: v for k, v in message.items() if k not in ('document', 'caption')}
        fake['text'] = raw.decode('utf-8', 'replace')[:20000]
        send_message(chat_id, "📄 Importing variables from your `.env` file…")
        handle_message(fake)
        return True
    return False


# =====================================================================================================
# FEATURE PACK 2 — PIN, secret warnings, safe deploys, public URLs, payments, auto-renew, commissions,
# gifts, tickets, admin overrides/notes, scheduled broadcasts, data export / deletion, Mini App.
# All UI uses `p2_` callbacks and `fp_p2*` text steps. Modules register themselves in the tables below.
# =====================================================================================================
_P2 = {"init": False}
_P2_LOCK = threading.Lock()
_P2_ROUTES = []            # (prefix, fn(callback, data, uid, chat_id, mid) -> bool)
_P2_TEXT = {}              # step -> fn(message, user_step, uid, chat_id, text) -> bool
_P2_MORE_ROWS = []         # rows appended to the "More" menu
_P2_ADMIN_ROWS = []        # rows appended to the admin panel
_P2_DDL = [
    "CREATE TABLE IF NOT EXISTS user_pins (user_id INTEGER PRIMARY KEY, salt BLOB, pin_hash TEXT, set_at TEXT, "
    "fails INTEGER DEFAULT 0, locked_until TEXT)",
    "CREATE TABLE IF NOT EXISTS payment_orders (ref TEXT PRIMARY KEY, user_id INTEGER, provider TEXT, coins INTEGER, "
    "amount_minor INTEGER, currency TEXT, status TEXT, created_at TEXT, paid_at TEXT, email TEXT, extra TEXT)",
    "CREATE TABLE IF NOT EXISTS renew_log (deployment_id INTEGER, expire_from TEXT, renewed_at TEXT, "
    "PRIMARY KEY (deployment_id, expire_from))",
    "CREATE TABLE IF NOT EXISTS commission_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, referrer INTEGER, "
    "referred INTEGER, coins INTEGER, spend INTEGER, ts TEXT, reversed INTEGER DEFAULT 0, tx_ref TEXT)",
    "CREATE TABLE IF NOT EXISTS gift_codes (code TEXT PRIMARY KEY, plan TEXT, buyer INTEGER, cost INTEGER, "
    "created_at TEXT, redeemed_by INTEGER, redeemed_at TEXT, cancelled INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, subject TEXT, "
    "status TEXT DEFAULT 'open', created_at TEXT, updated_at TEXT, unread_admin INTEGER DEFAULT 1, "
    "unread_user INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS ticket_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, sender TEXT, "
    "text TEXT, ts TEXT)",
    "CREATE TABLE IF NOT EXISTS user_overrides (user_id INTEGER PRIMARY KEY, max_deps INTEGER, quota_mb INTEGER, "
    "note TEXT, updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS sched_broadcasts (id INTEGER PRIMARY KEY AUTOINCREMENT, audience TEXT, text TEXT, "
    "run_at TEXT, status TEXT DEFAULT 'pending', created_by INTEGER, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, "
    "finished_at TEXT)",
    "CREATE TABLE IF NOT EXISTS export_log (user_id INTEGER PRIMARY KEY, last_at TEXT)",
]


def _p2_ensure():
    if _P2["init"]:
        return
    with _P2_LOCK:
        if _P2["init"]:
            return
        for ddl in _P2_DDL:
            db_execute(ddl)
        try:
            db_execute("ALTER TABLE dep_settings ADD COLUMN safe_off INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            db_execute("ALTER TABLE dep_settings ADD COLUMN autorenew INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            db_execute("ALTER TABLE dep_settings ADD COLUMN public INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            db_execute("ALTER TABLE payment_orders ADD COLUMN bonus INTEGER DEFAULT 0")
        except Exception:
            pass
        _P2["init"] = True


def _q(query, params=(), fetch='none'):
    _p2_ensure()
    return db_execute(query, params, fetch=fetch)


def _now():
    return datetime.now().isoformat(timespec='seconds')


def _p2_del_msg(chat_id, message_id):
    try:
        if message_id:
            http_get(f"{TELEGRAM_API}/deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


# ---------------------------------------------------------------- PIN
_P2_PEND = {}              # uid -> {"data","chat","mid","ts"}   action waiting on a PIN
_P2_TMP = {}               # uid -> (first_pin, ts)              PIN being confirmed
_P2_OK = {}                # uid -> (callback data, expiry)      one-shot approval after a correct PIN
PIN_MAX_FAILS = 5
PIN_LOCK_MIN = 15
_PIN_NEEDED_FOR_DEPLOY = ("deploy_new", "github_deploy", "free_deployment", "plan_monthly", "plan_yearly",
                          "gh_plan_monthly", "gh_plan_yearly", "gh_plan_free")
_PIN_SENSITIVE = re.compile(r'^(confirm_delete_\d+|p2_delacct_go|p2_exp_go|p2_pinchg_go)$')


def _pin_hash(pin, salt):
    return hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def pin_is_set(uid):
    r = _q("SELECT pin_hash FROM user_pins WHERE user_id = ?", (uid,), fetch='one')
    return bool(r and r[0])


def pin_valid_format(pin):
    if not re.fullmatch(r'\d{4,6}', pin or ''):
        return "PIN must be 4–6 digits."
    if pin == "0000" or len(set(pin)) == 1:
        return "That PIN is too easy (0000 / repeated digits). Pick another."
    return None


def pin_set(uid, pin):
    salt = os.urandom(16)
    _q("INSERT INTO user_pins (user_id, salt, pin_hash, set_at, fails, locked_until) VALUES (?, ?, ?, ?, 0, NULL) "
       "ON CONFLICT(user_id) DO UPDATE SET salt=excluded.salt, pin_hash=excluded.pin_hash, set_at=excluded.set_at, "
       "fails=0, locked_until=NULL", (uid, salt, _pin_hash(pin, salt), _now()))


def pin_reset(uid):
    _q("DELETE FROM user_pins WHERE user_id = ?", (uid,))


def pin_lock_left(uid):
    r = _q("SELECT locked_until FROM user_pins WHERE user_id = ?", (uid,), fetch='one')
    if r and r[0]:
        try:
            left = (datetime.fromisoformat(r[0]) - datetime.now()).total_seconds()
            return int(left) if left > 0 else 0
        except Exception:
            pass
    return 0


def pin_check(uid, pin):
    """-> (ok, message). Unset PIN == default '0000'. 5 wrong tries lock for 15 minutes."""
    left = pin_lock_left(uid)
    if left:
        return False, f"🔒 Too many wrong PINs. Try again in {left // 60 + 1} min."
    r = _q("SELECT salt, pin_hash, fails FROM user_pins WHERE user_id = ?", (uid,), fetch='one')
    if not r or not r[1]:
        return (pin == "0000"), "❌ Wrong PIN."
    ok = hmac.compare_digest(_pin_hash(pin, bytes(r[0])), r[1])
    if ok:
        _q("UPDATE user_pins SET fails = 0, locked_until = NULL WHERE user_id = ?", (uid,))
        return True, ""
    fails = (r[2] or 0) + 1
    if fails >= PIN_MAX_FAILS:
        _q("UPDATE user_pins SET fails = 0, locked_until = ? WHERE user_id = ?",
           ((datetime.now() + timedelta(minutes=PIN_LOCK_MIN)).isoformat(timespec='seconds'), uid))
        audit(uid, "pin_locked", uid)
        return False, f"🔒 Too many wrong PINs. Locked for {PIN_LOCK_MIN} minutes."
    _q("UPDATE user_pins SET fails = ? WHERE user_id = ?", (fails, uid))
    return False, f"❌ Wrong PIN. {PIN_MAX_FAILS - fails} attempt(s) left."


def _p2_pin_start_set(uid, chat_id, mid, pend=None, reason=""):
    _P2_PEND[uid] = dict(pend or {}, chat=chat_id, mid=mid, ts=time.time())
    _P2_TMP.pop(uid, None)
    set_user_step(uid, 'fp_p2set1')
    txt = (f"*🔐 SET YOUR PIN*\n\n{reason}Choose a 4–6 digit PIN. It protects sensitive actions (deleting bots, data export, "
           f"account deletion) and you must set one before deploying.\n\nSend it as a message — it's deleted right "
           f"after, and stored only as a salted hash.")
    kb = _fp_kb([_fp_btn("❌ Cancel", "main_menu")])
    if mid:
        edit_message(chat_id, mid, txt, kb)
    else:
        send_message(chat_id, txt, kb)


def _p2_pin_start_ask(uid, chat_id, mid, data):
    _P2_PEND[uid] = {"data": data, "chat": chat_id, "mid": mid, "ts": time.time()}
    set_user_step(uid, 'fp_p2ask')
    edit_message(chat_id, mid, "*🔐 ENTER YOUR PIN*\n\nThis action needs your PIN. Send it as a message (it's deleted right after).",
                 _fp_kb([_fp_btn("❌ Cancel", "main_menu")]))


def _p2_redispatch(uid, pend):
    data = pend.get("data")
    if not data:
        return False
    cb = {"id": "0", "from": {"id": uid}, "data": data,
          "message": {"chat": {"id": pend["chat"]}, "message_id": pend.get("mid")}}
    try:
        handle_callback(cb)
    except Exception as e:
        print(f"⚠️ p2 redispatch {data}: {e}")
    return True


def _p2_pin_gate(data, uid, chat_id, mid):
    """-> True if the callback was intercepted (PIN prompt shown)."""
    if data in _PIN_NEEDED_FOR_DEPLOY or data.startswith(("plan_", "gh_plan_")):
        if not pin_is_set(uid):
            _p2_pin_start_set(uid, chat_id, mid, {"data": data}, "You must set a PIN before deploying.\n\n")
            return True
        return False
    if _PIN_SENSITIVE.match(data):
        ok = _P2_OK.get(uid)
        if ok and ok[0] == data and ok[1] > time.time():
            _P2_OK.pop(uid, None)
            return False
        if not pin_is_set(uid):
            _p2_pin_start_set(uid, chat_id, mid, {"data": data}, "You need a PIN for this action.\n\n")
        else:
            _p2_pin_start_ask(uid, chat_id, mid, data)
        return True
    return False


def _p2_text_pin(message, user_step, uid, chat_id, text):
    step = user_step.get('step')
    _p2_del_msg(chat_id, message.get('message_id'))
    pend = _P2_PEND.get(uid) or {}
    if pend and time.time() - pend.get("ts", 0) > 900:
        _P2_PEND.pop(uid, None); pend = {}
    cancel = _fp_kb([_fp_btn("❌ Cancel", "main_menu")])
    if step == 'fp_p2set1':
        err = pin_valid_format(text)
        if err:
            set_user_step(uid, 'fp_p2set1')
            send_message(chat_id, f"❌ {err}\n\nSend a new PIN:", cancel); return True
        _P2_TMP[uid] = (text, time.time())
        set_user_step(uid, 'fp_p2set2')
        send_message(chat_id, "🔁 Send the same PIN again to confirm.", cancel); return True
    if step == 'fp_p2set2':
        first = _P2_TMP.get(uid)
        if not first or first[0] != text:
            _P2_TMP.pop(uid, None)
            set_user_step(uid, 'fp_p2set1')
            send_message(chat_id, "❌ The PINs didn't match. Send a new PIN to start again:", cancel); return True
        _P2_TMP.pop(uid, None)
        pin_set(uid, text)
        audit(uid, "pin_set", uid)
        _P2_PEND.pop(uid, None)
        send_message(chat_id, "✅ *PIN saved.* Keep it safe — nobody can see it, only an admin can reset it.")
        if pend.get("data"):
            _p2_redispatch(uid, pend)
        else:
            send_message(chat_id, "🔐 Security", _fp_kb([_fp_btn("🔙 More", "fp_more")]))
        return True
    if step == 'fp_p2old':                     # change PIN: verify old one first
        ok, m = pin_check(uid, text)
        if not ok:
            set_user_step(uid, 'fp_p2old') if "left" in m else None
            send_message(chat_id, m, cancel); return True
        _p2_pin_start_set(uid, chat_id, None, {}, "Old PIN accepted.\n\n"); return True
    if step == 'fp_p2ask':
        ok, m = pin_check(uid, text)
        if not ok:
            if "left" in m:
                set_user_step(uid, 'fp_p2ask')
            send_message(chat_id, m, cancel); return True
        _P2_PEND.pop(uid, None)
        _P2_OK[uid] = (pend.get("data"), time.time() + 120)
        _p2_redispatch(uid, pend); return True
    return False


_P2_TEXT.update({k: _p2_text_pin for k in ('fp_p2set1', 'fp_p2set2', 'fp_p2old', 'fp_p2ask')})


def p2_security_menu(chat_id, mid, uid):
    s = pin_is_set(uid)
    edit_message(chat_id, mid, f"*🔐 PIN & SECURITY*\n\nPIN: `{'set ✅' if s else 'not set (default 0000) ⚠️'}`\n\n"
                 f"Your PIN is required before deploying and for deleting bots, exporting or deleting your data.",
                 _fp_kb([_fp_btn("🔑 Change PIN" if s else "🔑 Set PIN", "p2_pinchg")],
                        [_fp_btn("📦 My data & account", "p2_data")],
                        [_fp_btn("🔙 More", "fp_more")]))


def _p2_r_pin(callback, data, uid, chat_id, mid):
    if data == "p2_sec":
        p2_security_menu(chat_id, mid, uid); return True
    if data == "p2_pinchg":
        if not pin_is_set(uid):
            _p2_pin_start_set(uid, chat_id, mid, {}); return True
        set_user_step(uid, 'fp_p2old')
        edit_message(chat_id, mid, "*🔑 CHANGE PIN*\n\nSend your *current* PIN.", _fp_kb([_fp_btn("❌ Cancel", "p2_sec")])); return True
    return False


_P2_ROUTES.append(("p2_sec", _p2_r_pin))
_P2_ROUTES.append(("p2_pinchg", _p2_r_pin))
_P2_MORE_ROWS.append([_fp_btn("🔐 PIN & Security", "p2_sec")])



# ---- admin: reset a user's PIN (button flow, no commands)
def _p2_r_apin(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    set_user_step(uid, 'fp_p2apin')
    edit_message(chat_id, mid, "*🔑 RESET USER PIN*\n\nSend the user's Telegram ID. Their PIN is cleared and they must set a new one before deploying.",
                 _fp_kb([_fp_btn("❌ Cancel", "admin_panel")]))
    return True


def _p2_text_apin(message, user_step, uid, chat_id, text):
    if not is_admin(uid):
        return True
    if text.lstrip('-').isdigit() and _q("SELECT 1 FROM users WHERE user_id = ?", (int(text),), fetch='one'):
        pin_reset(int(text))
        audit(uid, "pin_reset", text)
        send_message(chat_id, f"✅ PIN cleared for `{text}`. They must set a new one before deploying.",
                     _fp_kb([_fp_btn("🔙 Admin Panel", "admin_panel")]))
        try:
            send_message(int(text), "🔐 An admin reset your PIN. You'll be asked to set a new one next time you deploy.")
        except Exception:
            pass
    else:
        send_message(chat_id, "❌ No such user.", _fp_kb([_fp_btn("🔙 Admin Panel", "admin_panel")]))
    return True


_P2_ROUTES.append(("p2_apin", _p2_r_apin))
_P2_TEXT['fp_p2apin'] = _p2_text_apin
_P2_ADMIN_ROWS.append([_fp_btn("🔑 Reset user PIN", "p2_apin")])


# ---------------------------------------------------------------- hardcoded-secret warning (read-only)
_P2_SECRET_RX = [
    ("Telegram bot token", re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{30,40}\b')),
    ("GitHub token", re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b')),
    ("OpenAI-style API key", re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')),
    ("AWS access key", re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ("Google API key", re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ("Slack token", re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b')),
    ("Database URL with password", re.compile(r'\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/\'"]+:[^\s@/\'"]+@')),
    ("Private key", re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')),
]
_P2_SCAN_EXT = {'.py', '.js', '.mjs', '.cjs', '.ts', '.json', '.yml', '.yaml', '.toml', '.cfg', '.ini', '.txt', '.sh'}
_P2_ENV_LINE = re.compile(r'os\.environ|getenv|process\.env|environ\.get|config\(', re.I)


def _p2_mask(s):
    s = s.strip()
    return (s[:4] + "…" + s[-2:]) if len(s) > 10 else "…"


def scan_secrets(folder, max_files=400, max_hits=40):
    """READ-ONLY scan. -> [(relative path, line number, kind, masked match)]. Never modifies a file."""
    hits = []
    folder = Path(folder)
    n = 0
    try:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in _JUNK_DIRS and not d.startswith('.')]
            for f in files:
                if f.startswith('.env') or Path(f).suffix.lower() not in _P2_SCAN_EXT:
                    continue
                fp = Path(root) / f
                try:
                    if fp.is_symlink() or fp.stat().st_size > 300_000:
                        continue
                    n += 1
                    if n > max_files:
                        return hits
                    for i, line in enumerate(fp.read_text('utf-8', 'ignore').splitlines(), 1):
                        if len(line) > 2000 or _P2_ENV_LINE.search(line):
                            continue
                        for kind, rx in _P2_SECRET_RX:
                            m = rx.search(line)
                            if m:
                                hits.append((str(fp.relative_to(folder)), i, kind, _p2_mask(m.group(0))))
                                break
                        if len(hits) >= max_hits:
                            return hits
                except Exception:
                    continue
    except Exception as e:
        print(f"⚠️ secret scan: {e}")
    return hits


def secret_report(dep_id, hits):
    lines = [f"⚠️ *Possible hardcoded secrets in #{dep_id}*", ""]
    for path, ln, kind, mask in hits[:8]:
        lines.append(f"• `{path}:{ln}` — {kind} `{mask}`")
    if len(hits) > 8:
        lines.append(f"…and {len(hits) - 8} more")
    lines += ["", "Anyone who can read your code (or your GitHub repo) can use these. Move them to *Environment "
              "Variables* and read them with `os.environ` / `process.env`, then rotate any key that was exposed.",
              "_I never change your files — this is only a warning._"]
    return "\n".join(lines)


def p2_secret_check(dep_id, notify=True):
    d = _fp_dep(dep_id)
    if not d or not d["folder"].exists():
        return None
    hits = scan_secrets(d["folder"])
    fp = hashlib.sha1(repr(hits).encode()).hexdigest()[:12]
    if notify and hits and kv_get(f"secscan_{dep_id}") != fp:
        send_message(d["owner"], secret_report(dep_id, hits),
                     _fp_kb([_fp_btn("🔧 Open env editor", f"edit_env_{dep_id}")],
                            [_fp_btn("📄 Deployment", f"view_deploy_{dep_id}")]))
    kv_set(f"secscan_{dep_id}", fp if hits else "")
    return hits


def _p2_r_scan(callback, data, uid, chat_id, mid):
    p = data.split("_")
    dep_id = int(p[2]) if len(p) > 2 and p[2].isdigit() else None
    d = _fp_own(uid, dep_id) if dep_id else None
    if not d:
        return True
    hits = scan_secrets(d["folder"])
    txt = secret_report(dep_id, hits) if hits else f"✅ *No hardcoded secrets found in #{dep_id}.*"
    edit_message(chat_id, mid, txt, _fp_kb([_fp_btn("🔧 Env editor", f"edit_env_{dep_id}")] if hits else [],
                                         [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))
    return True


_P2_ROUTES.append(("p2_scan_", _p2_r_scan))


def _p2_after_persist(dep_id):
    def run():
        try:
            sleep(3)
            p2_secret_check(dep_id)
        except Exception as e:
            print(f"⚠️ secret check #{dep_id}: {e}")
    threading.Thread(target=run, daemon=True, name="SecretScan").start()



def _p2_install_scan_hooks():
    g = globals()
    _pnd, _push = g['persist_new_deployment'], g['_edit_push']

    def pnd(dep_id, reason="deployed"):
        r = _pnd(dep_id, reason)
        _p2_after_persist(dep_id)
        return r

    def push(dep_id, what):
        r = _push(dep_id, what)
        if what in ("code", "restore", "rollback"):
            _p2_after_persist(dep_id)
        return r
    g['persist_new_deployment'], g['_edit_push'] = pnd, push


_p2_install_scan_hooks()


# ---------------------------------------------------------------- safe deploys (verify + automatic rollback)
_P2_WATCH = set()
SAFE_VERIFY_SECS = int(os.environ.get("SAFE_VERIFY_SECS", "40"))
SAFE_SNAP_MAX_AGE = 15 * 60


def _p2_alive(dep_id):
    r = db_execute("SELECT proc_pid, status FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
    if not r or not r[0] or r[1] != 'active':
        return False
    try:
        os.kill(int(r[0]), 0)
    except Exception:
        return False
    try:
        if _psutil and _psutil.Process(int(r[0])).status() == _psutil.STATUS_ZOMBIE:
            return False
    except Exception:
        pass
    return True


def _p2_recent_before_snapshot(dep_id):
    """Newest recent snapshot taken right before a code change (edit / auto-deploy / restore).
    None if the latest event was a rollback (nothing to fall back to) or nothing recent exists."""
    now = time.time()
    for s in snapshot_list(dep_id):                      # newest first
        stamp, _, slug = s.stem.partition("__")
        if not slug.startswith("before"):
            continue
        try:
            t = datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()
        except Exception:
            continue
        if now - t > SAFE_SNAP_MAX_AGE:
            return None
        if slug.startswith(("before-rollback", "before-auto-rollback")):
            return None
        return s
    return None


def _p2_safe_watch(dep_id, snap):
    """After a deploy/restart that followed code changes: if the bot isn't alive and stable, roll back."""
    if dep_id in _P2_WATCH:
        return
    _P2_WATCH.add(dep_id)
    try:
        def crashes():
            r = db_execute("SELECT COALESCE(crash_restart_count,0) FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
            return r[0] if r else 0
        c0 = crashes()
        sleep(SAFE_VERIFY_SECS)
        stable = _p2_alive(dep_id) and crashes() <= c0
        if stable:
            sleep(max(5, SAFE_VERIFY_SECS // 4))
            if _p2_alive(dep_id) and crashes() <= c0:
                return
        d = _fp_dep(dep_id)
        if not d or d["status"] not in ('active', 'crashed'):
            return
        ok, msg = apply_zip_to_deployment(d, snap, "auto-rollback")
        if not ok:
            send_message(d["owner"], f"⚠️ *Bot #{dep_id}* crashed after the update and the automatic rollback failed: {msg[:120]}")
            return
        res = _fp_restart(dep_id, "rollback")
        audit(d["owner"], "auto_rollback", dep_id, snap.name)
        sleep(SAFE_VERIFY_SECS // 2)
        alive = _p2_alive(dep_id)
        send_message(d["owner"], (f"↩️ *Safe deploy — automatic rollback (#{dep_id})*\n\nYour latest change made the bot crash, "
                                  f"so I restored the previous working version (`{_snap_label(snap)}`) and restarted it. "
                                  f"{'It is running again ✅' if alive else 'It is still not running — check the logs.'}\n\n"
                                  f"Check Logs to see what went wrong before trying again."),
                     _fp_kb([_fp_btn("📜 Logs", f"fp_logs_{dep_id}"), _fp_btn("📄 Deployment", f"view_deploy_{dep_id}")]))
    except Exception as e:
        print(f"⚠️ safe watch #{dep_id}: {e}")
    finally:
        _P2_WATCH.discard(dep_id)


def _p2_safe_off(dep_id):
    r = _q("SELECT COALESCE(safe_off, 0) FROM dep_settings WHERE deployment_id = ?", (dep_id,), fetch='one')
    return bool(r and r[0])


def _p2_maybe_watch(dep_id):
    try:
        st = _fp_settings(dep_id)
        off = db_execute("SELECT safe_off FROM dep_settings WHERE deployment_id = ?", (dep_id,), fetch='one')
        if off and off[0]:
            return
        snap = _p2_recent_before_snapshot(dep_id)
        if snap:
            threading.Thread(target=_p2_safe_watch, args=(dep_id, snap), daemon=True, name=f"SafeWatch{dep_id}").start()
    except Exception as e:
        print(f"⚠️ maybe_watch: {e}")


def _p2_r_safe(callback, data, uid, chat_id, mid):
    p = data.split("_")
    dep_id = int(p[2]) if len(p) > 2 and p[2].isdigit() else None
    if not dep_id or not _fp_own(uid, dep_id):
        return True
    _fp_settings(dep_id)
    _q("UPDATE dep_settings SET safe_off = 1 - COALESCE(safe_off, 0) WHERE deployment_id = ?", (dep_id,))
    fp_tools_menu(chat_id, mid, uid, dep_id)
    return True


_P2_ROUTES.append(("p2_safe_", _p2_r_safe))

def _p2_install_safe_hooks():
    g = globals()
    _rd, _ar = g['restart_deployment'], g['_auto_relaunch_deployment']

    def rd(deployment_id, *a, **k):
        r = _rd(deployment_id, *a, **k)
        _p2_maybe_watch(deployment_id)
        return r

    def ar(deployment_id, reason="crash", bypass_cap=False):
        r = _ar(deployment_id, reason=reason, bypass_cap=bypass_cap)
        if reason == "update" and r == "restarted":
            _p2_maybe_watch(deployment_id)
        return r
    g['restart_deployment'], g['_auto_relaunch_deployment'] = rd, ar


_p2_install_safe_hooks()


# ---------------------------------------------------------------- HTTP layer: public URLs, payment webhooks, Mini App
import http.client as _hc
_P2_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers',
           'transfer-encoding', 'upgrade', 'host', 'content-length'}
_P2_PROXY_SEM = {}
_P2_PROXY_RATE = {}


def _p2_send(h, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
    if isinstance(body, str):
        body = body.encode()
    try:
        h.send_response(code)
        h.send_header('Content-Type', ctype)
        h.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if getattr(h, 'command', '') != 'HEAD':
            h.wfile.write(body)
    except Exception:
        pass


def _p2_body(h, limit=5_000_000):
    try:
        n = int(h.headers.get('Content-Length') or 0)
    except Exception:
        n = 0
    return h.rfile.read(min(n, limit)) if n > 0 else b''


def public_url_for(dep_id):
    base = _public_base_url()
    return f"{base}/b/{dep_id}/" if base else None


def p2_proxy(h, method, dep_id, rest):
    r = _q("SELECT public FROM dep_settings WHERE deployment_id = ?", (dep_id,), fetch='one')
    d = _fp_dep(dep_id)
    if not r or not r[0] or not d or d["status"] != 'active' or get_ban_info(d["owner"]):
        _p2_send(h, 404, "Not found"); return
    port = d["env"].get("PORT") or d["env"].get("BOT_PORT")
    if not str(port).isdigit():
        _p2_send(h, 502, "This bot has no web port"); return
    now = time.time()
    hits = [t for t in _P2_PROXY_RATE.get(dep_id, []) if now - t < 10]
    if len(hits) >= 200:
        _p2_send(h, 429, "Too many requests"); return
    hits.append(now)
    _P2_PROXY_RATE[dep_id] = hits
    sem = _P2_PROXY_SEM.setdefault(dep_id, threading.BoundedSemaphore(8))
    if not sem.acquire(timeout=5):
        _p2_send(h, 503, "Busy"); return
    try:
        body = _p2_body(h) if method in ("POST", "PUT", "PATCH", "DELETE") else None
        hdrs = {k: v for k, v in h.headers.items() if k.lower() not in _P2_HOP}
        hdrs["X-Forwarded-For"] = h.client_address[0]
        hdrs["X-Forwarded-Prefix"] = f"/b/{dep_id}"
        conn = _hc.HTTPConnection("127.0.0.1", int(port), timeout=25)
        try:
            conn.request(method, rest or "/", body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read(10_000_000)
            h.send_response(resp.status)
            for k, v in resp.getheaders():
                kl = k.lower()
                if kl in _P2_HOP:
                    continue
                if kl == "set-cookie":                       # keep every bot's cookies inside its own /b/<id>/ path
                    v = re.sub(r';\s*Domain=[^;]*', '', v, flags=re.I)
                    v = (re.sub(r'Path=[^;]*', f'Path=/b/{dep_id}/', v, flags=re.I) if re.search(r'Path=', v, re.I)
                         else v + f'; Path=/b/{dep_id}/')
                elif kl == "location" and v.startswith("/") and not v.startswith("//"):
                    v = f"/b/{dep_id}" + v
                h.send_header(k, v)
            h.send_header('Content-Length', str(len(data)))
            h.end_headers()
            if method != "HEAD":
                h.wfile.write(data)
        finally:
            conn.close()
    except Exception:
        _p2_send(h, 502, "The bot did not respond")
    finally:
        sem.release()


_P2_HTTP = []              # extra handlers: fn(h, method, path) -> bool


def p2_http(h, method):
    """Entry for HealthHandler. -> True if the request was handled here."""
    try:
        path = h.path.split('?')[0]
        m = re.match(r'^/b/(\d+)(/.*)?$', path)
        if m:
            q = h.path.split('?', 1)[1] if '?' in h.path else ''
            p2_proxy(h, method, int(m.group(1)), (m.group(2) or "/") + (("?" + q) if q else ""))
            return True
        for fn in _P2_HTTP:
            if fn(h, method, path):
                return True
    except Exception as e:
        print(f"⚠️ p2_http: {e}")
        _p2_send(h, 500, "error"); return True
    return False


def _p2_r_pub(callback, data, uid, chat_id, mid):
    p = data.split("_")
    dep_id = int(p[2]) if len(p) > 2 and p[2].isdigit() else None
    d = _fp_own(uid, dep_id) if dep_id else None
    if not d or (d["owner"] != uid and not is_admin(uid)):
        return True
    _fp_settings(dep_id)
    if p[1] == "pubtog":
        _q("UPDATE dep_settings SET public = 1 - COALESCE(public, 0) WHERE deployment_id = ?", (dep_id,))
        audit(uid, "public_url_toggle", dep_id)
    r = _q("SELECT public FROM dep_settings WHERE deployment_id = ?", (dep_id,), fetch='one')
    on = bool(r and r[0])
    url = public_url_for(dep_id)
    port = d["env"].get("PORT") or d["env"].get("BOT_PORT")
    txt = (f"*🌍 PUBLIC URL — #{dep_id}*\n\nStatus: `{'ON 🟢' if on else 'OFF ⚪'}`\n"
           + (f"URL: `{url}`\n" if on and url else "")
           + ("\nThis forwards web traffic to your bot's web server (port `%s`). Use it for webhooks, APIs and dashboards. "
              "Anyone with the link can reach it, so keep secrets in your own auth.\n" % port if str(port).isdigit() else "")
           + ("\n⚠️ No public base URL is configured on the host (set `PUBLIC_BASE_URL`)." if not url else ""))
    edit_message(chat_id, mid, txt, _fp_kb([_fp_btn("🔴 Turn OFF" if on else "🟢 Turn ON", f"p2_pubtog_{dep_id}")],
                                          [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")]))
    return True


_P2_ROUTES.append(("p2_pub", _p2_r_pub))



# ---------------------------------------------------------------- local payments (Paystack / Flutterwave / USDT)
def _p2_packs(kind="local"):
    raw = os.environ.get("COIN_PACKS_USD" if kind == "usd" else "COIN_PACKS", "")
    out = []
    for part in raw.split(","):
        try:
            c, pr = part.strip().split(":")
            if int(c) > 0 and float(pr) > 0:
                out.append((int(c), float(pr)))
        except Exception:
            continue
    return out


def _p2_providers():
    cur = os.environ.get("PAY_CURRENCY", "NGN").upper()
    out = []
    if os.environ.get("PAYSTACK_SECRET_KEY") and _p2_packs():
        out.append(("paystack", "💳 Paystack", cur))
    if os.environ.get("FLW_SECRET_KEY") and os.environ.get("FLW_SECRET_HASH") and _p2_packs():
        out.append(("flutterwave", "🦋 Flutterwave", cur))
    if os.environ.get("NOWPAYMENTS_API_KEY") and os.environ.get("NOWPAYMENTS_IPN_SECRET") and _p2_packs("usd"):
        out.append(("usdt", "🪙 USDT (crypto)", "USD"))
    return out


def _p2_api(method, url, headers=None, body=None, timeout=20):
    """-> (status, parsed json or {})"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict({"Content-Type": "application/json", "User-Agent": "BotHosting/2"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        print(f"⚠️ pay api {url[:40]}: {e}")
        return 0, {}


def p2_create_order(uid, provider, coins, price, email=None):
    """-> (ok, url_or_error)"""
    base = _public_base_url()
    if not base:
        return False, "Payments need a public URL on the host (PUBLIC_BASE_URL)."
    ref = f"HB{uid}-{secrets.token_hex(5)}"
    if provider == "paystack":
        cur = os.environ.get("PAY_CURRENCY", "NGN").upper()
        minor = int(round(price * 100))
        st, j = _p2_api("POST", "https://api.paystack.co/transaction/initialize",
                        {"Authorization": "Bearer " + os.environ["PAYSTACK_SECRET_KEY"]},
                        {"email": email, "amount": minor, "currency": cur, "reference": ref,
                         "callback_url": base + "/pay/done", "metadata": {"user_id": uid, "coins": coins}})
        url = (j.get("data") or {}).get("authorization_url")
    elif provider == "flutterwave":
        cur = os.environ.get("PAY_CURRENCY", "NGN").upper()
        minor = int(round(price * 100))
        st, j = _p2_api("POST", "https://api.flutterwave.com/v3/payments",
                        {"Authorization": "Bearer " + os.environ["FLW_SECRET_KEY"]},
                        {"tx_ref": ref, "amount": price, "currency": cur, "redirect_url": base + "/pay/done",
                         "customer": {"email": email}, "customizations": {"title": "Buy coins"}})
        url = (j.get("data") or {}).get("link")
    elif provider == "usdt":
        cur, minor = "USD", int(round(price * 100))
        st, j = _p2_api("POST", "https://api.nowpayments.io/v1/invoice",
                        {"x-api-key": os.environ["NOWPAYMENTS_API_KEY"]},
                        {"price_amount": price, "price_currency": "usd", "pay_currency": "usdttrc20", "order_id": ref,
                         "ipn_callback_url": base + "/pay/usdt", "success_url": base + "/pay/done",
                         "order_description": f"{coins} coins"})
        url = j.get("invoice_url")
    else:
        return False, "Unknown provider."
    if not url:
        return False, "The payment provider didn't accept the request. Try again later."
    _q("INSERT INTO payment_orders (ref, user_id, provider, coins, amount_minor, currency, status, created_at, email, bonus) "
       "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)", (ref, uid, provider, coins, minor, cur, _now(), email, bonus_for(coins)))
    return True, url


def p2_credit_order(ref, provider, paid_minor, currency):
    """Idempotent. Amount/currency must match what we asked for. -> True if credited now."""
    o = _q("SELECT user_id, coins, amount_minor, currency, status, provider, COALESCE(bonus, 0) FROM payment_orders WHERE ref = ?", (ref,), fetch='one')
    if not o or o[5] != provider or o[4] == 'paid':
        return False
    if currency and str(currency).upper() != o[3].upper():
        print(f"⚠️ pay {ref}: currency mismatch {currency} != {o[3]}"); return False
    if paid_minor is None or int(paid_minor) < int(o[2]):
        print(f"⚠️ pay {ref}: underpaid {paid_minor} < {o[2]}"); return False
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("UPDATE payment_orders SET status='paid', paid_at=? WHERE ref=? AND status != 'paid'", (_now(), ref))
        conn.commit()
        if cur.rowcount != 1:
            return False
    finally:
        conn.close()
    update_user_coins(o[0], int(o[1]) + int(o[6]), "local_payment", ref)
    audit("system", "payment_paid", o[0], f"{provider} {ref} +{o[1]}" + (f" +{o[6]} bonus" if o[6] else ""))
    try:
        async_backup(f"payment_{ref}")
    except Exception:
        pass
    send_message(o[0], f"✅ *Payment received!*\n\n+{o[1]} 🪙 added to your balance." + (f"\n🎁 Bonus: *+{o[6]} 🪙* free!" if o[6] else ""), _fp_kb([_fp_btn("🏠 Menu", "main_menu")]))
    return True


def p2_payment_webhook(provider, body, headers):
    """-> (http code, text). Signature checked FIRST; money only moves after provider-side verification."""
    try:
        if provider == "done":
            return 200, "Payment received. You can return to Telegram."
        if provider == "paystack":
            key = os.environ.get("PAYSTACK_SECRET_KEY", "")
            sig = headers.get("x-paystack-signature", "")
            if not key or not hmac.compare_digest(hmac.new(key.encode(), body, hashlib.sha512).hexdigest(), sig):
                return 401, "bad signature"
            ev = json.loads(body)
            if ev.get("event") != "charge.success":
                return 200, "ignored"
            ref = (ev.get("data") or {}).get("reference")
            st, j = _p2_api("GET", f"https://api.paystack.co/transaction/verify/{urllib.parse.quote(str(ref))}",
                            {"Authorization": "Bearer " + key})
            if st == 0:
                return 503, "retry"
            dd = j.get("data") or {}
            if dd.get("status") == "success":
                p2_credit_order(ref, "paystack", dd.get("amount"), dd.get("currency"))
            return 200, "ok"
        if provider == "flutterwave":
            want = os.environ.get("FLW_SECRET_HASH", "")
            if not want or not hmac.compare_digest(headers.get("verif-hash", ""), want):
                return 401, "bad signature"
            ev = json.loads(body)
            ref = (ev.get("data") or {}).get("tx_ref") or ev.get("txRef")
            if not ref:
                return 200, "ignored"
            st, j = _p2_api("GET", "https://api.flutterwave.com/v3/transactions/verify_by_reference?tx_ref="
                            + urllib.parse.quote(str(ref)), {"Authorization": "Bearer " + os.environ.get("FLW_SECRET_KEY", "")})
            if st == 0:
                return 503, "retry"
            dd = j.get("data") or {}
            if dd.get("status") == "successful":
                p2_credit_order(ref, "flutterwave", int(round(float(dd.get("amount", 0)) * 100)), dd.get("currency"))
            return 200, "ok"
        if provider == "usdt":
            key = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
            ev = json.loads(body)
            canon = json.dumps(ev, sort_keys=True, separators=(',', ':'))
            sig = headers.get("x-nowpayments-sig", "")
            if not key or not hmac.compare_digest(hmac.new(key.encode(), canon.encode(), hashlib.sha512).hexdigest(), sig):
                return 401, "bad signature"
            if ev.get("payment_status") == "finished" and str(ev.get("price_currency", "")).lower() == "usd":
                p2_credit_order(ev.get("order_id"), "usdt", int(round(float(ev.get("price_amount", 0)) * 100)), "USD")
            return 200, "ok"
    except Exception as e:
        print(f"⚠️ pay webhook {provider}: {e}")
        return 500, "error"
    return 404, "not found"


def p2_buy_menu(chat_id, mid, uid):
    provs = _p2_providers()
    if not provs:
        edit_message(chat_id, mid, "*💳 BUY COINS*\n\nLocal payments aren't set up on this host yet.",
                     _fp_kb([_fp_btn("🔙 More", "fp_more")])); return
    edit_message(chat_id, mid, "*💳 BUY COINS*\n\nPick a payment method:",
                 _fp_kb(*[[_fp_btn(label, f"p2_bprov_{k}")] for k, label, _ in provs], [_fp_btn("🔙 More", "fp_more")]))


def _p2_r_buy(callback, data, uid, chat_id, mid):
    p = data.split("_")
    if data == "p2_buy":
        p2_buy_menu(chat_id, mid, uid); return True
    provs = {k: (label, cur) for k, label, cur in _p2_providers()}
    if p[1] == "bprov" and len(p) > 2 and p[2] in provs:
        packs = _p2_packs("usd" if p[2] == "usdt" else "local")
        cur = provs[p[2]][1]
        edit_message(chat_id, mid, f"*{provs[p[2]][0]}*\n\nChoose a coin pack:",
                     _fp_kb(*[[_fp_btn(f"{c} 🪙" + (f" +{bonus_for(c)} 🎁" if bonus_for(c) else "") + f" — {pr:g} {cur}", f"p2_bpack_{p[2]}_{i}")] for i, (c, pr) in enumerate(packs)],
                            [_fp_btn("🔙 Back", "p2_buy")])); return True
    if p[1] == "bpack" and len(p) > 3 and p[2] in provs and p[3].isdigit():
        packs = _p2_packs("usd" if p[2] == "usdt" else "local")
        i = int(p[3])
        if i >= len(packs):
            return True
        coins, price = packs[i]
        if p[2] != "usdt":
            em = kv_get(f"email_{uid}")
            if not em:
                set_user_step(uid, 'fp_p2email')
                _P2_PEND[uid] = {"data": data, "chat": chat_id, "mid": mid, "ts": time.time()}
                edit_message(chat_id, mid, "📧 Send the email address for your payment receipt.",
                             _fp_kb([_fp_btn("❌ Cancel", "p2_buy")])); return True
        else:
            em = None
        edit_message(chat_id, mid, "⏳ Creating your payment link…")
        ok, res = p2_create_order(uid, p[2], coins, price, em)
        if ok:
            edit_message(chat_id, mid, f"*💳 PAY — {coins} 🪙*" + (f" *+ {bonus_for(coins)} 🎁 bonus*" if bonus_for(coins) else "") + "\n\nOpen the secure payment page below. Coins are added automatically "
                         f"a moment after you pay.", _fp_kb([{"text": "💳 Pay now", "url": res}], [_fp_btn("🔙 Back", "p2_buy")]))
        else:
            edit_message(chat_id, mid, f"❌ {res}", _fp_kb([_fp_btn("🔙 Back", "p2_buy")]))
        return True
    return False


def _p2_text_email(message, user_step, uid, chat_id, text):
    if not re.fullmatch(r'[^@\s]{1,64}@[^@\s]{1,120}\.[A-Za-z]{2,}', text):
        set_user_step(uid, 'fp_p2email')
        send_message(chat_id, "❌ That doesn't look like an email. Try again:", _fp_kb([_fp_btn("❌ Cancel", "p2_buy")])); return True
    kv_set(f"email_{uid}", text)
    pend = _P2_PEND.pop(uid, None)
    if pend:
        _p2_redispatch(uid, pend)
    return True


_P2_TEXT['fp_p2email'] = _p2_text_email
_P2_ROUTES.append(("p2_buy", _p2_r_buy))
_P2_ROUTES.append(("p2_bprov_", _p2_r_buy))
_P2_ROUTES.append(("p2_bpack_", _p2_r_buy))
_P2_MORE_ROWS.append([_fp_btn("💳 Buy coins (local)", "p2_buy")])


def _p2_http_pay(h, method, path):
    if not path.startswith("/pay/"):
        return False
    if method == "POST":
        code, txt = p2_payment_webhook(path[5:].strip('/'), _p2_body(h, 1_000_000), h.headers)
        _p2_send(h, code, txt)
    elif path.rstrip('/') == "/pay/done":
        _p2_send(h, 200, "<html><body style='font-family:sans-serif;text-align:center;padding:3em'><h2>Thank you!</h2>"
                         "<p>Your payment is being confirmed. Return to Telegram - your coins appear automatically.</p></body></html>",
                 "text/html; charset=utf-8")
    else:
        _p2_send(h, 404, "not found")
    return True


_P2_HTTP.append(_p2_http_pay)


def p2_admin_pay(chat_id, mid):
    rows = _q("SELECT ref, user_id, provider, coins, status, created_at FROM payment_orders ORDER BY created_at DESC LIMIT 12",
              fetch='all') or []
    prov = ", ".join(k for k, _, _ in _p2_providers()) or "none configured"
    paid = _q("SELECT COUNT(*), COALESCE(SUM(coins),0) FROM payment_orders WHERE status='paid'", fetch='one')
    lines = [f"*💳 LOCAL PAYMENTS*", "", f"Providers: `{prov}`", f"Paid orders: `{paid[0]}` · coins sold: `{paid[1]}`", ""]
    for ref, u, p, c, s, t in rows:
        lines.append(f"{'✅' if s == 'paid' else '⏳'} `{u}` {p} +{c}🪙 {t[5:16]}")
    if not rows:
        lines.append("_No orders yet._")
    edit_message(chat_id, mid, "\n".join(lines), _fp_kb([_fp_btn("🔙 Admin Panel", "admin_panel")]))


def _p2_r_apay(callback, data, uid, chat_id, mid):
    if is_admin(uid):
        p2_admin_pay(chat_id, mid)
    return True


_P2_ROUTES.append(("p2_apay", _p2_r_apay))
_P2_ADMIN_ROWS.append([_fp_btn("💳 Local payments", "p2_apay")])


# ---------------------------------------------------------------- auto-renew with coins
_P2_RENEW_PLANS = {"monthly": (30, "PRICE_MONTHLY_COINS"), "yearly": (365, "PRICE_YEARLY_COINS")}


def _p2_plan_price(plan):
    days, name = _P2_RENEW_PLANS[plan]
    return days, int(globals()[name])


def p2_autorenew_on(dep_id):
    r = _q("SELECT COALESCE(autorenew, 0) FROM dep_settings WHERE deployment_id = ?", (dep_id,), fetch='one')
    return bool(r and r[0])


def p2_autorenew_tick():
    """Renew coin-paid plans 24h before they expire; remind 3 days before. Idempotent. -> (renewed, warned)"""
    renewed = warned = 0
    now = datetime.now()
    rows = _q("SELECT d.deployment_id, d.user_id, d.plan, d.expire_time FROM deployments d "
              "JOIN dep_settings s ON s.deployment_id = d.deployment_id "
              "WHERE s.autorenew = 1 AND d.status = 'active' AND COALESCE(d.is_free, 0) = 0 "
              "AND d.plan IN ('monthly', 'yearly') AND d.expire_time IS NOT NULL", fetch='all') or []
    for dep_id, uid, plan, exp_s in rows:
        try:
            exp = datetime.fromisoformat(exp_s)
            left = exp - now
            if left.total_seconds() <= 0 or left > timedelta(days=3) or get_ban_info(uid):
                continue
            if _premium_covers_plan_core(uid, plan):
                continue                                   # the premium subscription already extends this bot
            days, price = _p2_plan_price(plan)
            bal = get_user_balances(uid)['coins']
            if left > timedelta(hours=24):                 # 3-day heads-up, once per expiry
                key = f"renewrem_{dep_id}_{exp_s}"
                if not kv_get(key):
                    kv_set(key, "1")
                    if not notif_allows(uid, "expiry", 72):
                        continue
                    send_message(uid, f"🔁 *Auto-renew #{dep_id}* — in about {int(left.total_seconds() // 3600 // 24) + 1} day(s) "
                                      f"I'll renew this {plan} bot for `{price}🪙`.\nYour balance: `{bal}🪙`"
                                      + ("" if bal >= price else "\n⚠️ That's not enough — top up so it doesn't expire."),
                                 _fp_kb([_fp_btn("🔁 Settings", f"p2_ren_{dep_id}")]))
                    warned += 1
                continue
            if bal < price:
                key = f"renewlow_{dep_id}_{exp_s}"
                if not kv_get(key):
                    kv_set(key, "1")
                    send_message(uid, f"⚠️ *Auto-renew #{dep_id} failed* — it needs `{price}🪙` but you have `{bal}🪙`. "
                                      f"Top up before it expires on `{exp_s[:16]}`.",
                                 _fp_kb([_fp_btn("🔁 Settings", f"p2_ren_{dep_id}"), _fp_btn("🏠 Menu", "main_menu")]))
                    warned += 1
                continue
            conn = sqlite3.connect(DATABASE_FILE, timeout=10)
            try:
                cur = conn.execute("INSERT OR IGNORE INTO renew_log (deployment_id, expire_from, renewed_at) VALUES (?, ?, ?)",
                                   (dep_id, exp_s, _now()))
                conn.commit()
                claimed = cur.rowcount == 1
            finally:
                conn.close()
            if not claimed:                                  # this expiry was already renewed
                continue
            update_user_coins(uid, -price, "deployment", f"autorenew_{dep_id}")
            if get_user_balances(uid)['coins'] < 0:         # lost a race with another spend → undo
                update_user_coins(uid, price, "deployment_refund", f"autorenew_{dep_id}")
                db_execute("DELETE FROM renew_log WHERE deployment_id = ? AND expire_from = ?", (dep_id, exp_s))
                continue
            new_exp = exp + timedelta(days=days)
            db_execute("UPDATE deployments SET expire_time = ? WHERE deployment_id = ? AND expire_time = ?",
                       (new_exp.isoformat(), dep_id, exp_s))
            audit("system", "autorenew", dep_id, f"{plan} -{price} -> {new_exp.date()}")
            try:
                async_backup(f"autorenew_{dep_id}")
            except Exception:
                pass
            send_message(uid, f"✅ *Auto-renewed #{dep_id}* — {plan} plan extended to `{new_exp.strftime('%Y-%m-%d')}` "
                              f"for `{price}🪙`.\nBalance: `{get_user_balances(uid)['coins']}🪙`",
                         _fp_kb([_fp_btn("📄 Deployment", f"view_deploy_{dep_id}")]))
            renewed += 1
        except Exception as e:
            print(f"⚠️ autorenew #{dep_id}: {e}")
    return renewed, warned


def p2_autorenew_monitor():
    sleep(120)
    print("✅ Auto-renew monitor started")
    while True:
        try:
            p2_autorenew_tick()
        except Exception as e:
            print(f"⚠️ autorenew monitor: {e}")
        sleep(1800)


def _p2_r_renew(callback, data, uid, chat_id, mid):
    p = data.split("_")
    dep_id = int(p[2]) if len(p) > 2 and p[2].isdigit() else None
    d = _fp_own(uid, dep_id) if dep_id else None
    if not d:
        return True
    row = _q("SELECT plan, COALESCE(is_free, 0) FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
    plan, is_free = row[0], row[1]
    eligible = (plan in _P2_RENEW_PLANS) and not is_free
    _fp_settings(dep_id)
    if p[1] == "rentog" and eligible:
        _q("UPDATE dep_settings SET autorenew = 1 - COALESCE(autorenew, 0) WHERE deployment_id = ?", (dep_id,))
        audit(uid, "autorenew_toggle", dep_id)
    on = p2_autorenew_on(dep_id)
    if eligible:
        days, price = _p2_plan_price(plan)
        txt = (f"*🔁 AUTO-RENEW — #{dep_id}*\n\nStatus: `{'ON 🟢' if on else 'OFF ⚪'}`\nPlan: `{plan}` · renews for `{price}🪙` "
               f"(+{days} days)\n\nWhen ON: you get a reminder 3 days before expiry, and the plan is renewed from your coin "
               f"balance 24h before it ends. If your balance is too low you're warned instead — nothing is charged.\n"
               f"Balance: `{get_user_balances(d['owner'])['coins']}🪙`")
        kb = _fp_kb([_fp_btn("🔴 Turn OFF" if on else "🟢 Turn ON", f"p2_rentog_{dep_id}")], [_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")])
    else:
        txt = (f"*🔁 AUTO-RENEW — #{dep_id}*\n\nOnly paid *monthly* and *yearly* bots can auto-renew. "
               f"This bot's plan is `{'free' if is_free else plan}`.")
        kb = _fp_kb([_fp_btn("🔙 Tools", f"fp_tools_{dep_id}")])
    edit_message(chat_id, mid, txt, kb)
    return True


_P2_ROUTES.append(("p2_ren", _p2_r_renew))


# ---------------------------------------------------------------- referral commission
_COM_SPEND_TYPES = ("deployment", "premium_subscription", "resource_tier")


def com_settings():
    try:
        pct = max(0, min(50, int(kv_get("commission_pct", "10"))))
    except Exception:
        pct = 10
    try:
        months = max(1, min(24, int(kv_get("commission_months", "3"))))
    except Exception:
        months = 3
    return pct, months


def p2_commission_on_spend(uid, spent, source=""):
    pct, months = com_settings()
    if pct <= 0 or spent <= 0:
        return 0
    r = _q("SELECT referrer_id, created_at FROM referrals WHERE referred_id = ? AND reward_given = 1", (uid,), fetch='one')
    if not r or int(r[0]) == uid or get_ban_info(int(r[0])):
        return 0
    try:
        if datetime.now() - datetime.fromisoformat(r[1]) > timedelta(days=30 * months):
            return 0
    except Exception:
        return 0
    coins = int(spent * pct / 100)
    if coins < 1:
        return 0
    referrer = int(r[0])
    _p2_coins_orig(referrer, coins, "referral_commission", f"from_{uid}")
    _q("INSERT INTO commission_ledger (referrer, referred, coins, spend, ts, reversed, tx_ref) VALUES (?, ?, ?, ?, ?, 0, ?)",
       (referrer, uid, coins, int(spent), _now(), source))
    try:
        send_message(referrer, f"💸 *Referral commission* +{coins}🪙 — a friend you invited just spent {int(spent)}🪙.",
                     _fp_kb([_fp_btn("💸 My commissions", "p2_com")]))
    except Exception:
        pass
    return coins


def p2_commission_reverse(uid, refunded):
    """A purchase by `uid` was refunded → take the matching commission back."""
    row = _q("SELECT id, referrer, coins FROM commission_ledger WHERE referred = ? AND reversed = 0 AND spend = ? "
             "ORDER BY id DESC LIMIT 1", (uid, int(refunded)), fetch='one')
    if not row:
        return 0
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("UPDATE commission_ledger SET reversed = 1 WHERE id = ? AND reversed = 0", (row[0],))
        conn.commit()
        if cur.rowcount != 1:
            return 0
    finally:
        conn.close()
    _p2_coins_orig(int(row[1]), -int(row[2]), "referral_commission_reversal", f"from_{uid}")
    return int(row[2])


def _p2_install_commission():
    g = globals()
    orig = g['update_user_coins']

    def uuc(user_id, delta, transaction_type="balance_update", source="system"):
        r = orig(user_id, delta, transaction_type, source)
        try:
            if delta < 0 and transaction_type in _COM_SPEND_TYPES:
                p2_commission_on_spend(user_id, -delta, source)
            elif delta > 0 and transaction_type == "deployment_refund":
                p2_commission_reverse(user_id, delta)
        except Exception as e:
            print(f"⚠️ commission: {e}")
        return r
    g['_p2_coins_orig'] = orig
    g['update_user_coins'] = uuc


_p2_install_commission()


def p2_commission_screen(chat_id, mid, uid):
    pct, months = com_settings()
    tot = _q("SELECT COALESCE(SUM(coins), 0), COUNT(*) FROM commission_ledger WHERE referrer = ? AND reversed = 0", (uid,), fetch='one')
    nref = _q("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND reward_given = 1", (uid,), fetch='one')[0]
    rows = _q("SELECT coins, ts, reversed FROM commission_ledger WHERE referrer = ? ORDER BY id DESC LIMIT 8", (uid,), fetch='all') or []
    lines = ["*💸 REFERRAL COMMISSIONS*", ""]
    if pct <= 0:
        lines.append("Commissions are currently switched off.")
    else:
        lines.append(f"Earn *{pct}%* of every coin spent by people you invited, for *{months} month(s)* after they join.")
    lines += ["", f"Verified referrals: `{nref}`", f"Earned so far: `{tot[0]}🪙` from `{tot[1]}` purchase(s)", ""]
    for c, ts, rev in rows:
        lines.append(f"{'↩️' if rev else '✅'} {'+' if not rev else '−'}{c}🪙 · {ts[5:16]}")
    edit_message(chat_id, mid, "\n".join(lines), _fp_kb([_fp_btn("👥 My referrals", "my_referral")], [_fp_btn("🔙 More", "fp_more")]))


def p2_admin_commission(chat_id, mid):
    pct, months = com_settings()
    tot = _q("SELECT COALESCE(SUM(coins), 0), COUNT(*) FROM commission_ledger WHERE reversed = 0", fetch='one')
    mk = lambda v, cur, key: _fp_btn(("✅ " if v == cur else "") + (f"{v}%" if key == "pct" else f"{v} mo"), f"p2_acom_{key}_{v}")
    edit_message(chat_id, mid, f"*💸 REFERRAL COMMISSION*\n\nRate: `{pct}%` of coins spent by referred users (0 = off)\n"
                 f"Window: `{months}` month(s) from the day they joined\n\nPaid so far: `{tot[0]}🪙` ({tot[1]} payouts)",
                 _fp_kb([mk(v, pct, "pct") for v in (0, 5, 10, 15, 20)],
                        [mk(v, months, "mo") for v in (1, 3, 6, 12)],
                        [_fp_btn("🔙 Admin Panel", "admin_panel")]))


def _p2_r_com(callback, data, uid, chat_id, mid):
    if data == "p2_com":
        p2_commission_screen(chat_id, mid, uid); return True
    if data.startswith("p2_acom"):
        if not is_admin(uid):
            return True
        p = data.split("_")
        if len(p) == 4 and p[3].isdigit():
            if p[2] == "pct" and int(p[3]) in (0, 5, 10, 15, 20):
                kv_set("commission_pct", p[3]); audit(uid, "commission_pct", p[3])
            elif p[2] == "mo" and int(p[3]) in (1, 3, 6, 12):
                kv_set("commission_months", p[3]); audit(uid, "commission_months", p[3])
        p2_admin_commission(chat_id, mid); return True
    return False


_P2_ROUTES.append(("p2_com", _p2_r_com))
_P2_ROUTES.append(("p2_acom", _p2_r_com))
_P2_MORE_ROWS.append([_fp_btn("💸 Referral commissions", "p2_com")])
_P2_ADMIN_ROWS.append([_fp_btn("💸 Commission settings", "p2_acom")])


# ---------------------------------------------------------------- gift a resource plan
GIFT_VALID_DAYS = 90
_GIFT_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_GIFT_FAILS = {}


def _gift_split(plan):
    """'1gb_m' -> ('1gb', 'm'); legacy 'monthly' / 'yearly' (deploy credit gifts) -> (None, plan)."""
    if plan in ("monthly", "yearly"):
        return None, plan
    key, _, per = (plan or "").partition("_")
    return (key, per) if key in RESOURCE_TIERS and per in TIER_PERIODS else (None, None)


def _gift_price(plan):
    """-> (days, coins)"""
    key, per = _gift_split(plan)
    if key:
        return TIER_PERIODS[per][1], tier_price_coins(key, per)
    return {"monthly": (30, PRICE_MONTHLY_COINS), "yearly": (365, PRICE_YEARLY_COINS)}[plan]


def _gift_label(plan):
    key, per = _gift_split(plan)
    if key:
        return f"{RESOURCE_TIERS[key][1]} {TIER_PERIODS[per][0]} resource plan"
    return f"{plan} deployment"


def _gift_new_code():
    r = "".join(secrets.choice(_GIFT_ALPHA) for _ in range(8))
    return f"GIFT-{r[:4]}-{r[4:]}"


def _gift_norm(text):
    t = re.sub(r'[^A-Z0-9]', '', (text or '').upper())
    if t.startswith("GIFT"):
        t = t[4:]
    return f"GIFT-{t[:4]}-{t[4:]}" if len(t) == 8 else None


def gift_buy(uid, plan):
    """Charge coins and create a code for a resource plan like '1gb_m'. -> (ok, code_or_error)"""
    key, per = _gift_split(plan)
    if not key:
        return False, "Unknown plan."
    days, cost = _gift_price(plan)
    if get_user_balances(uid)['coins'] < cost:
        return False, f"You need {cost}🪙 (balance {get_user_balances(uid)['coins']}🪙)."
    code = _gift_new_code()
    update_user_coins(uid, -cost, "gift_purchase", code)
    if get_user_balances(uid)['coins'] < 0:                      # raced with another spend
        update_user_coins(uid, cost, "gift_refund", code)
        return False, "Your balance changed — try again."
    _q("INSERT INTO gift_codes (code, plan, buyer, cost, created_at) VALUES (?, ?, ?, ?, ?)", (code, plan, uid, cost, _now()))
    audit(uid, "gift_buy", code, f"{plan} {cost}")
    return True, code


def _gift_block_reason(uid, plan):
    """Why this user can't receive this gift right now (the code is NOT consumed in that case)."""
    key, per = _gift_split(plan)
    if not key:
        return None
    why = _tier_blocked_reason(uid, key)
    if why:
        return why
    cur = get_user_tier(uid)
    if cur and cur[1] > RESOURCE_TIERS[key][0]:
        return (f"You already have a bigger plan ({format_resource(cur[1])}) active until {cur[2].strftime('%Y-%m-%d')}. "
                f"Redeem this gift after it ends — the code stays valid for {GIFT_VALID_DAYS} days from purchase.")
    return None


def gift_redeem(uid, text):
    """-> (ok, message)"""
    now = time.time()
    fails = [t for t in _GIFT_FAILS.get(uid, []) if now - t < 3600]
    if len(fails) >= 5:
        return False, "⛔ Too many wrong codes. Try again in an hour."
    code = _gift_norm(text)
    row = _q("SELECT plan, buyer, created_at, redeemed_by, cancelled FROM gift_codes WHERE code = ?", (code,), fetch='one') if code else None
    bad = None
    if not row or row[3] or row[4]:
        bad = "❌ That gift code isn't valid (it may have been used or cancelled)."
    else:
        try:
            if datetime.now() - datetime.fromisoformat(row[2]) > timedelta(days=GIFT_VALID_DAYS):
                bad = "❌ That gift code has expired."
        except Exception:
            pass
    if bad:
        fails.append(now); _GIFT_FAILS[uid] = fails
        return False, bad
    plan = row[0]
    why = _gift_block_reason(uid, plan)
    if why:
        return False, f"❌ {why}"
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("UPDATE gift_codes SET redeemed_by = ?, redeemed_at = ? WHERE code = ? AND redeemed_by IS NULL "
                           "AND cancelled = 0", (uid, _now(), code))
        conn.commit()
        claimed = cur.rowcount == 1
    finally:
        conn.close()
    if not claimed:
        return False, "❌ That gift code was just used."
    key, per = _gift_split(plan)
    days, _ = _gift_price(plan)
    if key:
        try:
            end = activate_resource_tier(uid, key, per, 0, 0)
        except Exception as e:                                   # never lose a gift to an error
            print(f"⚠️ gift activate: {e}")
            conn = sqlite3.connect(DATABASE_FILE, timeout=10)
            conn.execute("UPDATE gift_codes SET redeemed_by = NULL, redeemed_at = NULL WHERE code = ?", (code,))
            conn.commit(); conn.close()
            return False, "❌ Couldn't activate the gift right now. Your code is still valid — try again in a moment."
        msg = (f"🎁 *Gift redeemed!* Your *{_gift_label(plan)}* is active until `{end.strftime('%Y-%m-%d')}` "
               f"(guaranteed {RESOURCE_TIERS[key][1]} RAM + Premium).")
    else:
        _q("INSERT INTO deploy_credits (user_id, plan, seconds_left, source_deployment, created_at, used) VALUES (?, ?, ?, 0, ?, 0)",
           (uid, plan, days * 86400, _now()))
        msg = (f"🎁 *Gift redeemed!* You now have a free *{plan}* deployment ({days} days). "
               f"Tap *Deploy* and pick the {plan} plan — it won't cost you anything.")
    audit(uid, "gift_redeem", code, plan)
    try:
        send_message(row[1], f"🎁 Your gift `{code}` ({_gift_label(plan)}) was redeemed!")
    except Exception:
        pass
    try:
        async_backup(f"gift_{code}")
    except Exception:
        pass
    return True, msg


def gift_cancel(uid, code):
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        r = conn.execute("SELECT cost FROM gift_codes WHERE code = ? AND buyer = ? AND redeemed_by IS NULL AND cancelled = 0",
                         (code, uid)).fetchone()
        if not r:
            return False
        cur = conn.execute("UPDATE gift_codes SET cancelled = 1 WHERE code = ? AND redeemed_by IS NULL AND cancelled = 0", (code,))
        conn.commit()
        if cur.rowcount != 1:
            return False
    finally:
        conn.close()
    update_user_coins(uid, int(r[0]), "gift_refund", code)
    audit(uid, "gift_cancel", code)
    return True


def _g8(code):
    return code.replace("GIFT-", "").replace("-", "")


def _p2_gift_menu(chat_id, mid, uid):
    edit_message(chat_id, mid, "*🎁 GIFT A RESOURCE PLAN*\n\nBuy a guaranteed-RAM plan (with Premium) for a friend using your coins. "
                 "You get a code to send them; they redeem it here and it activates instantly.\n\nPick a size:",
                 _fp_kb(*[[_fp_btn(f"💾 {label} — from {tier_price_coins(k, 'm')}🪙", f"p2_gsz_{k}")] for k, (mb, label, _) in RESOURCE_TIERS.items()],
                        [_fp_btn("🎟 Redeem a gift code", "p2_gredeem"), _fp_btn("📋 My gifts", "p2_gmine")],
                        [_fp_btn("🔙 More", "fp_more")]))


def _p2_r_gift(callback, data, uid, chat_id, mid):
    p = data.split("_")
    if data == "p2_gift":
        _p2_gift_menu(chat_id, mid, uid); return True
    if data == "p2_gredeem":
        set_user_step(uid, 'fp_p2gift')
        edit_message(chat_id, mid, "*🎟 REDEEM GIFT*\n\nSend the gift code (looks like `GIFT-ABCD-2345`).",
                     _fp_kb([_fp_btn("❌ Cancel", "p2_gift")])); return True
    if p[1] == "gsz" and len(p) == 3 and p[2] in RESOURCE_TIERS:
        k = p[2]
        edit_message(chat_id, mid, f"*🎁 {RESOURCE_TIERS[k][1]} PLAN*\n\nChoose how long the gift lasts:",
                     _fp_kb(*[[_fp_btn(f"{TIER_PERIODS[per][0]} ({TIER_PERIODS[per][1]} days) — {tier_price_coins(k, per)}🪙", f"p2_gbuy_{k}_{per}")]
                              for per in TIER_PERIODS], [_fp_btn("🔙 Back", "p2_gift")])); return True
    if p[1] == "gbuy" and len(p) == 4 and _gift_split(f"{p[2]}_{p[3]}")[0]:
        plan = f"{p[2]}_{p[3]}"
        days, cost = _gift_price(plan)
        edit_message(chat_id, mid, f"*🎁 GIFT — {_gift_label(plan).upper()}*\n\nPrice: `{cost}🪙` ({days} days)\n"
                     f"Your balance: `{get_user_balances(uid)['coins']}🪙`\n\nCodes are valid for {GIFT_VALID_DAYS} days. "
                     f"You can cancel an unused gift for a full refund.",
                     _fp_kb([_fp_btn("✅ Buy gift", f"p2_gbuyok_{p[2]}_{p[3]}")], [_fp_btn("❌ Cancel", "p2_gift")])); return True
    if p[1] == "gbuyok" and len(p) == 4:
        ok_, res = gift_buy(uid, f"{p[2]}_{p[3]}")
        if ok_:
            edit_message(chat_id, mid, f"*🎁 GIFT READY*\n\nCode: `{res}`\n\nSend this code to your friend. They open the bot → "
                         f"More → 🎁 Gifts → Redeem.", _fp_kb([_fp_btn("📋 My gifts", "p2_gmine")], [_fp_btn("🔙 Gifts", "p2_gift")]))
        else:
            edit_message(chat_id, mid, f"❌ {res}", _fp_kb([_fp_btn("🔙 Gifts", "p2_gift")]))
        return True
    if data == "p2_gmine":
        rows = _q("SELECT code, plan, created_at, redeemed_by, cancelled FROM gift_codes WHERE buyer = ? ORDER BY created_at DESC LIMIT 10",
                  (uid,), fetch='all') or []
        lines = ["*📋 MY GIFTS*", ""] + [
            f"`{c}` · {_gift_label(pl)} · {'✅ redeemed' if rb else ('🚫 cancelled' if cx else '🟢 unused')}" for c, pl, ca, rb, cx in rows]
        if not rows:
            lines.append("_You haven't bought any gifts yet._")
        kb = [[_fp_btn(f"🚫 Cancel {c[-4:]}", f"p2_gcxq_{_g8(c)}")] for c, pl, ca, rb, cx in rows if not rb and not cx]
        edit_message(chat_id, mid, "\n".join(lines), _fp_kb(*kb, [_fp_btn("🔙 Gifts", "p2_gift")])); return True
    if p[1] == "gcxq" and len(p) == 3 and len(p[2]) == 8:
        edit_message(chat_id, mid, f"🚫 *Cancel gift* `GIFT-{p[2][:4]}-{p[2][4:]}`?\n\nYou get the coins back and the code stops working.",
                     _fp_kb([_fp_btn("✅ Yes, cancel", f"p2_gcx_{p[2]}"), _fp_btn("❌ No", "p2_gmine")])); return True
    if p[1] == "gcx" and len(p) == 3 and len(p[2]) == 8:
        code = f"GIFT-{p[2][:4]}-{p[2][4:]}"
        ok_ = gift_cancel(uid, code)
        edit_message(chat_id, mid, "✅ Gift cancelled and coins refunded." if ok_ else "❌ That gift can't be cancelled (already used or cancelled).",
                     _fp_kb([_fp_btn("📋 My gifts", "p2_gmine")])); return True
    return False


def _p2_text_gift(message, user_step, uid, chat_id, text):
    ok_, msg = gift_redeem(uid, text)
    send_message(chat_id, msg, _fp_kb([_fp_btn("🚀 Deploy", "deploy_new")] if ok_ else [_fp_btn("🔁 Try again", "p2_gredeem")],
                                     [_fp_btn("🔙 Gifts", "p2_gift")]))
    return True


_P2_TEXT['fp_p2gift'] = _p2_text_gift
_P2_ROUTES.append(("p2_g", _p2_r_gift))
_P2_MORE_ROWS.append([_fp_btn("🎁 Gifts", "p2_gift")])


# ---------------------------------------------------------------- support tickets
TK_MAX_OPEN = 3
TK_NEW_COOLDOWN = 600          # seconds between new tickets
TK_REPLY_COOLDOWN = 20         # seconds between a user's messages
TK_MAX_USER_MSGS = 20          # per ticket
TK_MAX_LEN = 1500
_TK_CTX = {}                   # uid -> ticket id being answered
_TK_LAST = {}                  # (uid, kind) -> time


def _tk_esc(s):
    return re.sub(r'([_*`\[])', r'\\\1', s or '')


def tk_create(uid, text):
    """-> (ok, ticket id or error)"""
    text = (text or '').strip()[:TK_MAX_LEN]
    if not text:
        return False, "Please send some text (photos and files aren't supported)."
    n_open = _q("SELECT COUNT(*) FROM tickets WHERE user_id = ? AND status = 'open'", (uid,), fetch='one')[0]
    if n_open >= TK_MAX_OPEN:
        return False, f"You already have {TK_MAX_OPEN} open tickets. Close one first."
    last = _TK_LAST.get((uid, 'new'), 0)
    if time.time() - last < TK_NEW_COOLDOWN:
        return False, f"Please wait {int(TK_NEW_COOLDOWN - (time.time() - last)) // 60 + 1} min before opening another ticket."
    subject = text.split("\n")[0][:60]
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("INSERT INTO tickets (user_id, subject, status, created_at, updated_at, unread_admin, unread_user) "
                           "VALUES (?, ?, 'open', ?, ?, 1, 0)", (uid, subject, _now(), _now()))
        tid = cur.lastrowid
        conn.execute("INSERT INTO ticket_msgs (ticket_id, sender, text, ts) VALUES (?, 'user', ?, ?)", (tid, text, _now()))
        conn.commit()
    finally:
        conn.close()
    _TK_LAST[(uid, 'new')] = time.time()
    _tk_notify_admins(tid, uid, text, new=True)
    return True, tid


def tk_add(tid, sender, uid, text):
    """sender 'user'|'admin'. -> (ok, error)"""
    text = (text or '').strip()[:TK_MAX_LEN]
    if not text:
        return False, "Please send some text."
    t = _q("SELECT user_id, status FROM tickets WHERE id = ?", (tid,), fetch='one')
    if not t or (sender == 'user' and t[0] != uid):
        return False, "Ticket not found."
    if t[1] != 'open':
        return False, "This ticket is closed."
    if sender == 'user':
        if time.time() - _TK_LAST.get((uid, 'rep'), 0) < TK_REPLY_COOLDOWN:
            return False, "You're sending messages too fast. Wait a few seconds."
        if _q("SELECT COUNT(*) FROM ticket_msgs WHERE ticket_id = ? AND sender = 'user'", (tid,), fetch='one')[0] >= TK_MAX_USER_MSGS:
            return False, "This ticket reached its message limit. Close it and open a new one."
        _TK_LAST[(uid, 'rep')] = time.time()
    _q("INSERT INTO ticket_msgs (ticket_id, sender, text, ts) VALUES (?, ?, ?, ?)", (tid, sender, text, _now()))
    _q("UPDATE tickets SET updated_at = ?, unread_admin = ?, unread_user = ? WHERE id = ?",
       (_now(), 1 if sender == 'user' else 0, 1 if sender == 'admin' else 0, tid))
    if sender == 'user':
        _tk_notify_admins(tid, t[0], text)
    else:
        try:
            send_message(t[0], f"🎫 *Support replied — ticket #{tid}*\n\n{_tk_esc(text)}",
                         _fp_kb([_fp_btn("💬 Open ticket", f"p2_tkv_{tid}")]))
        except Exception:
            pass
    return True, ""


def _tk_notify_admins(tid, uid, text, new=False):
    for a in list(ADMIN_IDS):
        try:
            send_message(a, f"🎫 *{'New ticket' if new else 'Reply on ticket'} #{tid}* from `{uid}`\n\n{_tk_esc(text[:600])}",
                         _fp_kb([_fp_btn("💬 Open", f"p2_atkv_{tid}")]))
        except Exception:
            pass


def tk_close(tid, by_admin, uid):
    t = _q("SELECT user_id, status FROM tickets WHERE id = ?", (tid,), fetch='one')
    if not t or t[1] != 'open' or (not by_admin and t[0] != uid):
        return False
    _q("UPDATE tickets SET status = 'closed', updated_at = ?, unread_admin = 0 WHERE id = ?", (_now(), tid))
    if by_admin:
        try:
            send_message(t[0], f"🎫 Ticket #{tid} was closed by support. Open a new ticket if you still need help.")
        except Exception:
            pass
    return True


def _tk_thread(tid, limit=8):
    rows = _q("SELECT sender, text, ts FROM ticket_msgs WHERE ticket_id = ? ORDER BY id DESC LIMIT ?", (tid, limit), fetch='all') or []
    return "\n\n".join(f"{'🧑 You' if s == 'user' else '🛟 Support'} · {ts[5:16]}\n{_tk_esc(tx)}" for s, tx, ts in reversed(rows))


def _p2_tk_menu(chat_id, mid, uid):
    rows = _q("SELECT id, subject, status, unread_user FROM tickets WHERE user_id = ? ORDER BY updated_at DESC LIMIT 8", (uid,), fetch='all') or []
    kb = [[_fp_btn(f"{'🔴' if u else ('🟢' if s == 'open' else '⚪')} #{i} {_tk_esc(sub)[:28]}", f"p2_tkv_{i}")] for i, sub, s, u in rows]
    edit_message(chat_id, mid, "*🎫 SUPPORT*\n\nNeed help? Open a ticket and our team will reply here in the bot."
                 + ("" if rows else "\n\n_You have no tickets yet._"),
                 _fp_kb([_fp_btn("➕ New ticket", "p2_tknew")], *kb, [_fp_btn("🔙 Menu", "main_menu")]))


def _p2_r_tk(callback, data, uid, chat_id, mid):
    p = data.split("_")
    if data == "p2_tk":
        _p2_tk_menu(chat_id, mid, uid); return True
    if data == "p2_tknew":
        set_user_step(uid, 'fp_p2tknew')
        edit_message(chat_id, mid, "*➕ NEW TICKET*\n\nDescribe your problem in one message (text only, up to "
                     f"{TK_MAX_LEN} characters). Include your deployment number if it's about a bot.",
                     _fp_kb([_fp_btn("❌ Cancel", "p2_tk")])); return True
    if len(p) == 3 and p[2].isdigit():
        tid = int(p[2])
        t = _q("SELECT user_id, subject, status FROM tickets WHERE id = ?", (tid,), fetch='one')
        if not t or t[0] != uid:
            return True
        if p[1] == "tkv":
            _q("UPDATE tickets SET unread_user = 0 WHERE id = ?", (tid,))
            kb = ([[_fp_btn("✍️ Reply", f"p2_tkr_{tid}"), _fp_btn("✅ Close", f"p2_tkc_{tid}")]] if t[2] == 'open' else [])
            edit_message(chat_id, mid, f"*🎫 Ticket #{tid}* — {'open 🟢' if t[2] == 'open' else 'closed ⚪'}\n\n{_tk_thread(tid)}",
                         _fp_kb(*kb, [_fp_btn("🔙 Support", "p2_tk")])); return True
        if p[1] == "tkr" and t[2] == 'open':
            _TK_CTX[uid] = tid
            set_user_step(uid, 'fp_p2tkrep')
            edit_message(chat_id, mid, f"✍️ Send your reply for ticket #{tid} (text only).",
                         _fp_kb([_fp_btn("❌ Cancel", f"p2_tkv_{tid}")])); return True
        if p[1] == "tkc":
            tk_close(tid, False, uid)
            edit_message(chat_id, mid, f"✅ Ticket #{tid} closed.", _fp_kb([_fp_btn("🔙 Support", "p2_tk")])); return True
    return False


def _p2_text_tk(message, user_step, uid, chat_id, text):
    step = user_step.get('step')
    if step == 'fp_p2tknew':
        ok_, res = tk_create(uid, text)
        send_message(chat_id, (f"✅ *Ticket #{res} sent.* We'll reply here as soon as we can." if ok_ else f"❌ {res}"),
                     _fp_kb([_fp_btn("🎫 My tickets", "p2_tk")]))
    elif step == 'fp_p2tkrep':
        tid = _TK_CTX.get(uid)
        ok_, res = tk_add(tid, 'user', uid, text) if tid else (False, "Ticket not found.")
        send_message(chat_id, "✅ Sent." if ok_ else f"❌ {res}", _fp_kb([_fp_btn("💬 Ticket", f"p2_tkv_{tid}")] if tid else [_fp_btn("🎫 Support", "p2_tk")]))
    elif step == 'fp_p2atkrep' and is_admin(uid):
        tid = _TK_CTX.get(uid)
        ok_, res = tk_add(tid, 'admin', uid, text) if tid else (False, "Ticket not found.")
        if ok_:
            audit(uid, "ticket_reply", tid)
        send_message(chat_id, "✅ Reply sent." if ok_ else f"❌ {res}", _fp_kb([_fp_btn("💬 Ticket", f"p2_atkv_{tid}")] if tid else [_fp_btn("🎫 Queue", "p2_atk")]))
    return True


def _p2_r_atk(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    p = data.split("_")
    if data == "p2_atk":
        rows = _q("SELECT id, user_id, subject, unread_admin FROM tickets WHERE status = 'open' ORDER BY unread_admin DESC, updated_at DESC LIMIT 12",
                  fetch='all') or []
        total = _q("SELECT COUNT(*) FROM tickets WHERE status = 'open'", fetch='one')[0]
        kb = [[_fp_btn(f"{'🔴' if u else '🟢'} #{i} {_tk_esc(sub)[:24]}", f"p2_atkv_{i}")] for i, uu, sub, u in rows]
        edit_message(chat_id, mid, f"*🎫 SUPPORT QUEUE*\n\nOpen tickets: `{total}` (🔴 = needs reply)" + ("" if rows else "\n\n_Nothing open. 🎉_"),
                     _fp_kb(*kb, [_fp_btn("🔙 Admin Panel", "admin_panel")])); return True
    if len(p) == 3 and p[2].isdigit():
        tid = int(p[2])
        t = _q("SELECT user_id, subject, status FROM tickets WHERE id = ?", (tid,), fetch='one')
        if not t:
            return True
        if p[1] == "atkv":
            _q("UPDATE tickets SET unread_admin = 0 WHERE id = ?", (tid,))
            kb = ([[_fp_btn("✍️ Reply", f"p2_atkr_{tid}"), _fp_btn("✅ Close", f"p2_atkc_{tid}")]] if t[2] == 'open'
                  else [[_fp_btn("🔓 Reopen", f"p2_atko_{tid}")]])
            edit_message(chat_id, mid, f"*🎫 #{tid}* · user `{t[0]}` · {'open 🟢' if t[2] == 'open' else 'closed ⚪'}\n\n{_tk_thread(tid, 10)}",
                         _fp_kb(*kb, [_fp_btn("⚙️ User controls", f"p2_au_{t[0]}")], [_fp_btn("🔙 Queue", "p2_atk")])); return True
        if p[1] == "atkr" and t[2] == 'open':
            _TK_CTX[uid] = tid
            set_user_step(uid, 'fp_p2atkrep')
            edit_message(chat_id, mid, f"✍️ Send your reply to ticket #{tid}. The user is notified instantly.",
                         _fp_kb([_fp_btn("❌ Cancel", f"p2_atkv_{tid}")])); return True
        if p[1] == "atkc":
            tk_close(tid, True, uid); audit(uid, "ticket_close", tid)
            edit_message(chat_id, mid, f"✅ Ticket #{tid} closed.", _fp_kb([_fp_btn("🔙 Queue", "p2_atk")])); return True
        if p[1] == "atko":
            _q("UPDATE tickets SET status = 'open', updated_at = ? WHERE id = ?", (_now(), tid))
            edit_message(chat_id, mid, f"🔓 Ticket #{tid} reopened.", _fp_kb([_fp_btn("💬 Open", f"p2_atkv_{tid}")])); return True
    return False


_P2_TEXT.update({'fp_p2tknew': _p2_text_tk, 'fp_p2tkrep': _p2_text_tk, 'fp_p2atkrep': _p2_text_tk})
_P2_ROUTES.append(("p2_atk", _p2_r_atk))
_P2_ROUTES.append(("p2_tk", _p2_r_tk))
_P2_ADMIN_ROWS.append([_fp_btn("🎫 Support tickets", "p2_atk")])


# ---------------------------------------------------------------- per-user overrides + private admin notes
_OV_FREE_PRESETS = (0, 1, 2, 3, 5, 10, 20)
_OV_QUOTA_PRESETS = (128, 256, 512, 1024, 2048, 4096)
_AOV_CTX = {}


def ov_get(uid):
    """-> dict(max_deps, quota_mb, note) — None where there is no override."""
    r = _q("SELECT max_deps, quota_mb, note FROM user_overrides WHERE user_id = ?", (int(uid),), fetch='one')
    return {"max_deps": r[0], "quota_mb": r[1], "note": r[2]} if r else {"max_deps": None, "quota_mb": None, "note": None}


def ov_set(uid, field, value):
    assert field in ("max_deps", "quota_mb", "note")
    _q("INSERT OR IGNORE INTO user_overrides (user_id, updated_at) VALUES (?, ?)", (int(uid), _now()))
    _q(f"UPDATE user_overrides SET {field} = ?, updated_at = ? WHERE user_id = ?", (value, _now(), int(uid)))


def _p2_install_overrides():
    g = globals()
    _can, _quota = g['can_use_free_deployment'], g['get_user_quota_mb']

    def can(user_id):
        if not is_admin(user_id):
            try:
                lim = ov_get(user_id)["max_deps"]
            except Exception:
                lim = None
            if lim is not None:
                used = get_free_deployment_used_count(user_id)
                if used < lim:
                    return True, f"Admin limit - {lim - used} remaining (used {used}/{lim})"
                return False, f"Free tier limit reached ({used}/{lim})"
        return _can(user_id)

    def quota(user_id):
        if not is_admin(user_id):
            try:
                q = ov_get(user_id)["quota_mb"]
            except Exception:
                q = None
            if q is not None:
                return float(q)
        return _quota(user_id)
    g['can_use_free_deployment'], g['get_user_quota_mb'] = can, quota


_p2_install_overrides()


def _p2_user_extra(uid):
    o = ov_get(uid)
    s = ""
    if o["max_deps"] is not None:
        s += f"\n🤖 Free-bot limit override: `{o['max_deps']}`"
    if o["quota_mb"] is not None:
        s += f"\n💾 Quota override: `{o['quota_mb']} MB`"
    if o["note"]:
        s += f"\n📝 Note: _{_tk_esc(o['note'][:200])}_"
    return s


def p2_user_controls(chat_id, mid, target):
    o = ov_get(target)
    row = _q("SELECT first_name, username FROM users WHERE user_id = ?", (target,), fetch='one')
    name = safe_md(f"{row[0] or ''} @{row[1]}" if row and row[1] else (row[0] if row else str(target)))
    txt = (f"*⚙️ USER CONTROLS* — `{target}`\n{name}\n\n"
           f"🤖 Free-bot limit: `{'default' if o['max_deps'] is None else o['max_deps']}` (default {FREE_USER_MAX_DEPLOYMENTS})\n"
           f"💾 Memory quota: `{'default' if o['quota_mb'] is None else str(o['quota_mb']) + ' MB'}`\n"
           f"🔐 PIN: `{'set' if pin_is_set(target) else 'not set'}`\n"
           f"📝 Note (admins only): {('_' + _tk_esc(o['note']) + '_') if o['note'] else '`none`'}")
    edit_message(chat_id, mid, txt, _fp_kb(
        [_fp_btn("🤖 Free-bot limit", f"p2_auf_{target}"), _fp_btn("💾 Quota", f"p2_auq_{target}")],
        [_fp_btn("📝 Edit note", f"p2_aun_{target}"), _fp_btn("🔑 Reset PIN", f"p2_aupin_{target}")],
        [_fp_btn("👤 User page", f"admin_user_view_{target}")]))


def _p2_ov_picker(chat_id, mid, target, kind):
    presets, cur = ((_OV_FREE_PRESETS, "max_deps") if kind == "f" else (_OV_QUOTA_PRESETS, "quota_mb"))
    now = ov_get(target)[cur]
    unit = "" if kind == "f" else " MB"
    btns = [_fp_btn(("✅ " if now == v else "") + f"{v}{unit}", f"p2_au{kind}s_{target}_{v}") for v in presets]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    title = "FREE-BOT LIMIT" if kind == "f" else "MEMORY QUOTA"
    edit_message(chat_id, mid, f"*{'🤖' if kind == 'f' else '💾'} {title}* — `{target}`\n\nPick a value, or reset to the default.",
                 _fp_kb(*rows, [_fp_btn(("✅ " if now is None else "") + "Default", f"p2_au{kind}s_{target}_d"),
                                _fp_btn("✏️ Custom", f"p2_au{kind}c_{target}")], [_fp_btn("🔙 Back", f"p2_au_{target}")]))


def _p2_r_au(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    p = data.split("_")
    if len(p) < 3 or not p[2].lstrip("-").isdigit():
        return True
    target, k = int(p[2]), p[1]
    if not _q("SELECT 1 FROM users WHERE user_id = ?", (target,), fetch='one'):
        edit_message(chat_id, mid, "❌ No such user.", _fp_kb([_fp_btn("🔙 Admin Panel", "admin_panel")])); return True
    if k == "au":
        p2_user_controls(chat_id, mid, target); return True
    if k in ("auf", "auq"):
        _p2_ov_picker(chat_id, mid, target, k[-1]); return True
    if k in ("aufs", "auqs") and len(p) == 4:
        field = "max_deps" if k == "aufs" else "quota_mb"
        val = None if p[3] == "d" else (int(p[3]) if p[3].isdigit() else "bad")
        if val != "bad":
            ov_set(target, field, val)
            audit(uid, f"override_{field}", target, str(val))
        p2_user_controls(chat_id, mid, target); return True
    if k in ("aufc", "auqc"):
        _AOV_CTX[uid] = (target, "f" if k == "aufc" else "q")
        set_user_step(uid, 'fp_p2aov')
        edit_message(chat_id, mid, "✏️ Send the number" + (" (0–100 free bots)." if k == "aufc" else " in MB (16–100000)."),
                     _fp_kb([_fp_btn("❌ Cancel", f"p2_au_{target}")])); return True
    if k == "aun":
        _AOV_CTX[uid] = (target, "n")
        set_user_step(uid, 'fp_p2aov')
        edit_message(chat_id, mid, "📝 Send the note for this user (max 500 chars). Only admins can see it. Send `-` to clear it.",
                     _fp_kb([_fp_btn("❌ Cancel", f"p2_au_{target}")])); return True
    if k == "aupin":
        edit_message(chat_id, mid, f"🔑 Reset the PIN of `{target}`? They must set a new one before deploying.",
                     _fp_kb([_fp_btn("✅ Reset", f"p2_aupinok_{target}"), _fp_btn("❌ No", f"p2_au_{target}")])); return True
    if k == "aupinok":
        pin_reset(target); audit(uid, "pin_reset", target)
        try:
            send_message(target, "🔐 An admin reset your PIN. You'll be asked to set a new one next time you deploy.")
        except Exception:
            pass
        p2_user_controls(chat_id, mid, target); return True
    return False


def _p2_text_aov(message, user_step, uid, chat_id, text):
    if not is_admin(uid):
        return True
    ctx = _AOV_CTX.pop(uid, None)
    if not ctx:
        return True
    target, kind = ctx
    back = _fp_kb([_fp_btn("⚙️ User controls", f"p2_au_{target}")])
    if kind == "n":
        note = None if text == "-" else text[:500]
        ov_set(target, "note", note)
        audit(uid, "user_note", target, "cleared" if note is None else "set")
        send_message(chat_id, "✅ Note cleared." if note is None else "✅ Note saved.", back); return True
    if not text.isdigit():
        send_message(chat_id, "❌ Send a whole number.", back); return True
    n = int(text)
    lo, hi = (0, 100) if kind == "f" else (16, 100000)
    if not lo <= n <= hi:
        send_message(chat_id, f"❌ Must be between {lo} and {hi}.", back); return True
    field = "max_deps" if kind == "f" else "quota_mb"
    ov_set(target, field, n)
    audit(uid, f"override_{field}", target, str(n))
    send_message(chat_id, f"✅ Saved: `{n}`", back)
    return True


_P2_TEXT['fp_p2aov'] = _p2_text_aov
_P2_ROUTES.append(("p2_au", _p2_r_au))


# ---------------------------------------------------------------- scheduled broadcasts
SB_MAX_PENDING = 20
SB_LATE_GRACE = timedelta(hours=6)
_SB_DRAFT = {}


def _sb_tz():
    try:
        return float(os.environ.get("ADMIN_TZ_OFFSET", "0"))
    except Exception:
        return 0.0


def _sb_clock_skew():
    d = datetime.now() - datetime.utcnow()               # server local clock vs UTC (a whole number of minutes)
    return timedelta(minutes=round(d.total_seconds() / 60))


def sb_admin_to_server(dt):
    """A wall-clock time typed by the admin (ADMIN_TZ_OFFSET hours from UTC) → the server's datetime.now() clock."""
    return dt - timedelta(hours=_sb_tz()) + _sb_clock_skew()


def sb_server_to_admin(dt):
    return dt - _sb_clock_skew() + timedelta(hours=_sb_tz())


def sb_fmt(run_at_iso):
    return sb_server_to_admin(datetime.fromisoformat(run_at_iso)).strftime("%Y-%m-%d %H:%M")


def sb_parse_time(text):
    """Admin text → server datetime, or None. Accepts 'YYYY-MM-DD HH:MM' or 'HH:MM' (next occurrence)."""
    text = (text or '').strip()
    now_admin = sb_server_to_admin(datetime.now())
    try:
        if re.fullmatch(r'\d{1,2}:\d{2}', text):
            h, m = map(int, text.split(":"))
            dt = now_admin.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now_admin:
                dt += timedelta(days=1)
        else:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except Exception:
        return None
    return sb_admin_to_server(dt)


def sb_schedule(admin_id, audience, run_at, text):
    """-> (ok, id or error)"""
    if audience not in BROADCAST_AUDIENCES:
        return False, "Unknown audience."
    text = (text or '').strip()
    if not text or len(text) > 3500:
        return False, "The message must be 1–3500 characters."
    now = datetime.now()
    if run_at < now + timedelta(seconds=45):
        return False, "That time is in the past — pick a later time."
    if run_at > now + timedelta(days=60):
        return False, "Schedule at most 60 days ahead."
    if _q("SELECT COUNT(*) FROM sched_broadcasts WHERE status = 'pending'", fetch='one')[0] >= SB_MAX_PENDING:
        return False, f"There are already {SB_MAX_PENDING} scheduled broadcasts. Cancel one first."
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("INSERT INTO sched_broadcasts (audience, text, run_at, status, created_by) VALUES (?, ?, ?, 'pending', ?)",
                           (audience, text, run_at.isoformat(timespec='seconds'), admin_id))
        conn.commit()
        sid = cur.lastrowid
    finally:
        conn.close()
    audit(admin_id, "broadcast_schedule", sid, f"{audience} @ {run_at.isoformat(timespec='minutes')}")
    return True, sid


def sb_run_due():
    """Claim and send every due broadcast. -> number executed"""
    n = 0
    now = datetime.now()
    rows = _q("SELECT id, audience, text, run_at, created_by FROM sched_broadcasts WHERE status = 'pending' AND run_at <= ? ORDER BY run_at",
              (now.isoformat(timespec='seconds'),), fetch='all') or []
    for sid, aud, text, run_at, by in rows:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        try:
            cur = conn.execute("UPDATE sched_broadcasts SET status = 'running' WHERE id = ? AND status = 'pending'", (sid,))
            conn.commit()
            claimed = cur.rowcount == 1
        finally:
            conn.close()
        if not claimed:
            continue
        if now - datetime.fromisoformat(run_at) > SB_LATE_GRACE:
            _q("UPDATE sched_broadcasts SET status = 'missed', finished_at = ? WHERE id = ?", (_now(), sid))
            try:
                send_message(by, f"⚠️ Scheduled broadcast #{sid} was *not sent*: the bot was offline at {sb_fmt(run_at)} "
                                 f"and it's now too late. Schedule it again if you still want it.")
            except Exception:
                pass
            continue
        try:
            ok_n, fail_n, skip_n = do_broadcast(by, 'text', None, text, None, aud)
        except Exception as e:
            print(f"❌ scheduled broadcast #{sid}: {e}")
            ok_n = fail_n = skip_n = 0
        _q("UPDATE sched_broadcasts SET status = 'done', sent = ?, failed = ?, finished_at = ? WHERE id = ?",
           (ok_n, fail_n, _now(), sid))
        audit("system", "broadcast_sent", sid, f"{ok_n} ok / {fail_n} failed")
        try:
            send_message(by, f"📢 *Scheduled broadcast #{sid} sent*\n\n✅ Delivered: `{ok_n}`\n❌ Failed: `{fail_n}`\n⏭ Skipped (blocked): `{skip_n}`")
        except Exception:
            pass
        n += 1
    return n


def sb_recover_interrupted():
    """A broadcast that was 'running' when the process died may be half-sent — never resend it."""
    rows = _q("SELECT id, created_by FROM sched_broadcasts WHERE status = 'running'", fetch='all') or []
    for sid, by in rows:
        _q("UPDATE sched_broadcasts SET status = 'interrupted', finished_at = ? WHERE id = ?", (_now(), sid))
        try:
            send_message(by, f"⚠️ Scheduled broadcast #{sid} was interrupted by a restart; some users may have received it. "
                             f"It will not be re-sent automatically.")
        except Exception:
            pass
    return len(rows)


def p2_sched_broadcast_monitor():
    sleep(20)
    try:
        sb_recover_interrupted()
    except Exception as e:
        print(f"⚠️ sb recover: {e}")
    print("✅ Scheduled-broadcast worker started")
    while True:
        try:
            sb_run_due()
        except Exception as e:
            print(f"⚠️ scheduled broadcasts: {e}")
        sleep(30)


def _p2_sb_list(chat_id, mid):
    pend = _q("SELECT id, audience, run_at, text FROM sched_broadcasts WHERE status = 'pending' ORDER BY run_at LIMIT 10", fetch='all') or []
    done = _q("SELECT id, status, sent, failed FROM sched_broadcasts WHERE status != 'pending' ORDER BY id DESC LIMIT 4", fetch='all') or []
    lines = [f"*🗓 SCHEDULED BROADCASTS*", f"_Times shown in your timezone (UTC{_sb_tz():+g})._", ""]
    for sid, aud, run_at, text in pend:
        lines.append(f"⏳ #{sid} · {sb_fmt(run_at)} · {BROADCAST_AUDIENCES.get(aud, aud)}\n   _{_tk_esc(text[:50])}_")
    if not pend:
        lines.append("_Nothing scheduled._")
    if done:
        lines.append("")
        lines += [f"{'✅' if s == 'done' else '⚠️'} #{i} {s}" + (f" · {a} sent, {b} failed" if s == 'done' else "") for i, s, a, b in done]
    kb = [[_fp_btn(f"🗑 Cancel #{sid}", f"p2_sbc_{sid}")] for sid, *_ in pend]
    edit_message(chat_id, mid, "\n".join(lines), _fp_kb([_fp_btn("➕ Schedule new", "p2_sbn")], *kb, [_fp_btn("🔙 Admin Panel", "admin_panel")]))


def _p2_sb_preview(chat_id, mid_or_none, admin):
    d = _SB_DRAFT.get(admin)
    if not d or not all(k in d for k in ("aud", "run_at", "text")):
        return
    n = len(broadcast_audience_ids(d["aud"]))
    txt = (f"*🗓 CONFIRM SCHEDULE*\n\n👥 {BROADCAST_AUDIENCES[d['aud']]} — `{n}` users\n⏰ {sb_fmt(d['run_at'].isoformat())} (UTC{_sb_tz():+g})\n\n"
           f"━━━━━━━━━━\n{d['text'][:1200]}")
    kb = _fp_kb([_fp_btn("📨 Send test to me", "p2_sbtest")], [_fp_btn("✅ Schedule", "p2_sbgo"), _fp_btn("❌ Cancel", "p2_sb")])
    if mid_or_none:
        edit_message(chat_id, mid_or_none, txt, kb)
    else:
        send_message(chat_id, txt, kb)


def _p2_r_sb(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    p = data.split("_")
    if data == "p2_sb":
        _SB_DRAFT.pop(uid, None); set_user_step(uid, None)
        _p2_sb_list(chat_id, mid); return True
    if data == "p2_sbn":
        _SB_DRAFT[uid] = {}
        rows = [[_fp_btn(f"{label} ({len(broadcast_audience_ids(k))})", f"p2_sba_{k}")] for k, label in BROADCAST_AUDIENCES.items()]
        edit_message(chat_id, mid, "*🗓 SCHEDULE — 1/3*\n\nWho should receive it?", _fp_kb(*rows, [_fp_btn("🔙 Back", "p2_sb")])); return True
    if p[1] == "sba" and len(p) == 3 and p[2] in BROADCAST_AUDIENCES:
        _SB_DRAFT.setdefault(uid, {})["aud"] = p[2]
        now_a = sb_server_to_admin(datetime.now())
        edit_message(chat_id, mid, f"*🗓 SCHEDULE — 2/3*\n\nWhen? (your time: UTC{_sb_tz():+g}, now `{now_a.strftime('%H:%M')}`)",
                     _fp_kb([_fp_btn("In 1 hour", "p2_sbt_1h"), _fp_btn("In 3 hours", "p2_sbt_3h")],
                            [_fp_btn("In 12 hours", "p2_sbt_12h"), _fp_btn("Tomorrow 09:00", "p2_sbt_tom")],
                            [_fp_btn("✏️ Custom time", "p2_sbt_cus")], [_fp_btn("🔙 Back", "p2_sbn")])); return True
    if p[1] == "sbt" and len(p) == 3 and uid in _SB_DRAFT:
        if p[2] == "cus":
            set_user_step(uid, 'fp_p2sbtime')
            edit_message(chat_id, mid, "✏️ Send the time as `YYYY-MM-DD HH:MM` (e.g. `2026-12-01 18:30`) or just `HH:MM` for the next "
                         "occurrence.", _fp_kb([_fp_btn("❌ Cancel", "p2_sb")])); return True
        now = datetime.now()
        if p[2] in ("1h", "3h", "12h"):
            run = now + timedelta(hours=int(p[2][:-1]))
        else:
            tom = sb_server_to_admin(now).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
            run = sb_admin_to_server(tom)
        _SB_DRAFT[uid]["run_at"] = run.replace(microsecond=0)
        set_user_step(uid, 'fp_p2sbtext')
        edit_message(chat_id, mid, "*🗓 SCHEDULE — 3/3*\n\nSend the message text (Markdown allowed, up to 3500 characters).",
                     _fp_kb([_fp_btn("❌ Cancel", "p2_sb")])); return True
    if data == "p2_sbtest":
        d = _SB_DRAFT.get(uid)
        if d and d.get("text"):
            send_message(uid, d["text"])
        return True
    if data == "p2_sbgo":
        d = _SB_DRAFT.get(uid)
        if not d or not all(k in d for k in ("aud", "run_at", "text")):
            return True
        ok_, res = sb_schedule(uid, d["aud"], d["run_at"], d["text"])
        _SB_DRAFT.pop(uid, None)
        edit_message(chat_id, mid, (f"✅ *Broadcast #{res} scheduled* for {sb_fmt(d['run_at'].isoformat())}." if ok_ else f"❌ {res}"),
                     _fp_kb([_fp_btn("🗓 Scheduled", "p2_sb")])); return True
    if p[1] == "sbc" and len(p) == 3 and p[2].isdigit():
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        try:
            cur = conn.execute("UPDATE sched_broadcasts SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'pending'",
                               (_now(), int(p[2])))
            conn.commit()
            done = cur.rowcount == 1
        finally:
            conn.close()
        if done:
            audit(uid, "broadcast_cancel", p[2])
        _p2_sb_list(chat_id, mid); return True
    return False


def _p2_text_sb(message, user_step, uid, chat_id, text):
    if not is_admin(uid) or uid not in _SB_DRAFT:
        return True
    step = user_step.get('step')
    if step == 'fp_p2sbtime':
        run = sb_parse_time(text)
        if not run or run < datetime.now() + timedelta(seconds=45):
            set_user_step(uid, 'fp_p2sbtime')
            send_message(chat_id, "❌ Couldn't read that, or it's in the past. Send `YYYY-MM-DD HH:MM` or `HH:MM`.",
                         _fp_kb([_fp_btn("❌ Cancel", "p2_sb")])); return True
        _SB_DRAFT[uid]["run_at"] = run.replace(microsecond=0)
        set_user_step(uid, 'fp_p2sbtext')
        send_message(chat_id, "*🗓 SCHEDULE — 3/3*\n\nSend the message text (Markdown allowed, up to 3500 characters).",
                     _fp_kb([_fp_btn("❌ Cancel", "p2_sb")])); return True
    if step == 'fp_p2sbtext':
        if not text or len(text) > 3500:
            set_user_step(uid, 'fp_p2sbtext')
            send_message(chat_id, "❌ The message must be 1–3500 characters of text. Send it again:", _fp_kb([_fp_btn("❌ Cancel", "p2_sb")])); return True
        _SB_DRAFT[uid]["text"] = text
        _p2_sb_preview(chat_id, None, uid)
    return True


_P2_TEXT.update({'fp_p2sbtime': _p2_text_sb, 'fp_p2sbtext': _p2_text_sb})
_P2_ROUTES.append(("p2_sb", _p2_r_sb))
_P2_ADMIN_ROWS.append([_fp_btn("🗓 Scheduled broadcasts", "p2_sb")])


# ---------------------------------------------------------------- data export + account deletion
EXPORT_COOLDOWN_S = 3600
EXPORT_MAX_FILE = 5 << 20
EXPORT_MAX_TOTAL = 40 << 20
_ACCOUNT_COLS = ("user_id, username, first_name, join_date, last_active, coins_balance, stars_balance, total_coins_earned, "
                 "total_coins_spent, total_stars_earned, total_stars_spent, is_premium, premium_expires, premium_plan, "
                 "referral_code, referred_by, total_referrals, lang")


def _rows_dicts(query, params=()):
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute(query, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def build_user_export(uid):
    """-> path of a zip with everything we hold about this user (env VALUES are never included)."""
    out_dir = BASE_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"export_{uid}_{int(time.time())}.zip"
    acct = (_rows_dicts(f"SELECT {_ACCOUNT_COLS} FROM users WHERE user_id = ?", (uid,)) or [{}])[0]
    acct["pin_set"] = pin_is_set(uid)
    deps = _rows_dicts("SELECT deployment_id, file_name, plan, payment_method, status, is_free, start_time, expire_time, "
                       "source_type, github_repo, github_branch, env_vars FROM deployments WHERE user_id = ?", (uid,))
    for d in deps:
        try:
            d["env_variable_names"] = sorted((json.loads(d.pop("env_vars") or "{}") or {}).keys())
        except Exception:
            d["env_variable_names"] = []
    tickets = _rows_dicts("SELECT id, subject, status, created_at FROM tickets WHERE user_id = ?", (uid,))
    for t in tickets:
        t["messages"] = _rows_dicts("SELECT sender, text, ts FROM ticket_msgs WHERE ticket_id = ? ORDER BY id", (t["id"],))
    parts = {
        "account.json": acct,
        "deployments.json": deps,
        "coin_transactions.json": _rows_dicts("SELECT amount, transaction_type, source, timestamp, status FROM coin_transactions "
                                              "WHERE user_id = ? ORDER BY transaction_id DESC LIMIT 1000", (uid,)),
        "payments.json": _rows_dicts("SELECT ref, provider, coins, amount_minor, currency, status, created_at, paid_at, email "
                                     "FROM payment_orders WHERE user_id = ?", (uid,)),
        "tickets.json": tickets,
        "gifts.json": {"bought": _rows_dicts("SELECT code, plan, cost, created_at, redeemed_at, cancelled FROM gift_codes WHERE buyer = ?", (uid,)),
                       "redeemed": _rows_dicts("SELECT code, plan, redeemed_at FROM gift_codes WHERE redeemed_by = ?", (uid,))},
        "referrals.json": {"invited": _rows_dicts("SELECT referred_id, created_at, reward_given FROM referrals WHERE referrer_id = ?", (uid,)),
                           "commissions": _rows_dicts("SELECT coins, ts, reversed FROM commission_ledger WHERE referrer = ?", (uid,))},
    }
    total = 0
    skipped = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, obj in parts.items():
            z.writestr(name, json.dumps(obj, indent=2, default=str, ensure_ascii=False))
        for d in deps:
            folder = get_deploy_folder(uid, d["deployment_id"])
            if not folder.exists():
                continue
            for root, dirs, files in os.walk(folder):
                dirs[:] = [x for x in dirs if x not in _JUNK_DIRS and not x.startswith(".git")]
                for f in files:
                    fp = Path(root) / f
                    try:
                        if fp.is_symlink():
                            continue
                        sz = fp.stat().st_size
                        rel = f"files/{d['deployment_id']}/{fp.relative_to(folder)}"
                        if sz > EXPORT_MAX_FILE or total + sz > EXPORT_MAX_TOTAL:
                            skipped.append(f"{rel} ({sz // 1024} KB)")
                            continue
                        z.write(fp, rel)
                        total += sz
                    except Exception:
                        continue
        z.writestr("README.txt",
                   "Your data export.\n\n- Environment variable VALUES are not included for safety (only their names). "
                   "View them in the env editor.\n- Files larger than 5 MB, or beyond the 40 MB total, are listed below "
                   "and were left out.\n\n" + ("\n".join(skipped) if skipped else "(nothing was left out)") + "\n")
    return path


def _p2_data_menu(chat_id, mid, uid):
    edit_message(chat_id, mid, "*📦 MY DATA & ACCOUNT*\n\n• *Export*: a zip with your account details, deployments, "
                 "coin and payment history, tickets and your bot files. (Env variable values are left out for safety.)\n"
                 "• *Delete account*: permanently removes your bots and files and anonymises your account.\n\nBoth need your PIN.",
                 _fp_kb([_fp_btn("📤 Export my data", "p2_exp")], [_fp_btn("🗑 Delete my account", "p2_delacct")],
                        [_fp_btn("🔙 Security", "p2_sec")]))


def _p2_r_data(callback, data, uid, chat_id, mid):
    if data == "p2_data":
        _p2_data_menu(chat_id, mid, uid); return True
    if data == "p2_exp":
        edit_message(chat_id, mid, "*📤 EXPORT MY DATA*\n\nI'll build a zip and send it here. You can export once per hour.",
                     _fp_kb([_fp_btn("✅ Export now", "p2_exp_go")], [_fp_btn("🔙 Back", "p2_data")])); return True
    if data == "p2_exp_go":
        r = _q("SELECT last_at FROM export_log WHERE user_id = ?", (uid,), fetch='one')
        if r and r[0]:
            wait = EXPORT_COOLDOWN_S - (datetime.now() - datetime.fromisoformat(r[0])).total_seconds()
            if wait > 0:
                edit_message(chat_id, mid, f"⏳ You can export again in {int(wait // 60) + 1} min.", _fp_kb([_fp_btn("🔙 Back", "p2_data")])); return True
        _q("INSERT OR REPLACE INTO export_log (user_id, last_at) VALUES (?, ?)", (uid, _now()))
        edit_message(chat_id, mid, "⏳ Building your export…")

        def run():
            path = None
            try:
                path = build_user_export(uid)
                if path.stat().st_size > 48 << 20:
                    send_message(chat_id, "❌ The export is too large to send through Telegram. Delete old files and try again.")
                else:
                    send_document(chat_id, path, "📦 Your data export", f"my_data_{uid}.zip")
                audit(uid, "data_export", uid)
            except Exception as e:
                print(f"⚠️ export {uid}: {e}")
                send_message(chat_id, "❌ The export failed. Please try again later.")
            finally:
                try:
                    if path:
                        path.unlink()
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True, name="DataExport").start()
        return True
    if data == "p2_delacct":
        n = _q("SELECT COUNT(*) FROM deployments WHERE user_id = ?", (uid,), fetch='one')[0]
        paid = _q("SELECT COUNT(*) FROM deployments WHERE user_id = ? AND COALESCE(is_free,0)=0", (uid,), fetch='one')[0]
        if is_admin(uid):
            edit_message(chat_id, mid, "⛔ Admin accounts can't be deleted from here.", _fp_kb([_fp_btn("🔙 Back", "p2_data")])); return True
        edit_message(chat_id, mid, f"*🗑 DELETE MY ACCOUNT*\n\nThis is *permanent*:\n• `{n}` deployment(s) are stopped and deleted "
                     f"with all files and rollback points{f' (including {paid} paid — no refunds)' if paid else ''}\n"
                     f"• Your coin balance (`{get_user_balances(uid)['coins']}🪙`) is forfeited\n• Your tickets and personal details are erased\n"
                     "• Payment and coin records are kept without your name, for accounting\n\nTip: export your data first.",
                     _fp_kb([_fp_btn("📤 Export first", "p2_exp")], [_fp_btn("🗑 Continue", "p2_delacct_go")], [_fp_btn("❌ Cancel", "p2_data")])); return True
    if data == "p2_delacct_go":
        if is_admin(uid):
            return True
        set_user_step(uid, 'fp_p2delconf')
        edit_message(chat_id, mid, "⚠️ *Last step.* Type `DELETE` (capitals) to permanently delete your account.",
                     _fp_kb([_fp_btn("❌ Cancel", "p2_data")])); return True
    return False


def _p2_purge_snapshots(dep_id):
    try:
        shutil.rmtree(BASE_DIR / "snapshots" / str(dep_id), ignore_errors=True)
    except Exception:
        pass

    def remote():
        try:
            if _files_repo_ready():
                pre = _snap_remote_prefix(dep_id)
                with _files_lock:
                    names = [k for k in _files_list_remote(pre)]
                    if names:
                        _files_commit({k: None for k in names}, f"remove rollback points #{dep_id}")
        except Exception as e:
            print(f"⚠️ snapshot purge #{dep_id}: {e}")
    threading.Thread(target=remote, daemon=True, name="SnapPurge").start()


def delete_account(uid):
    """Stop + delete all bots and files, erase personal data, keep the (anonymised) ledger and any ban."""
    uid = int(uid)
    for dep_id, pid in (_q("SELECT deployment_id, proc_pid FROM deployments WHERE user_id = ?", (uid,), fetch='all') or []):
        try:
            if pid:
                kill_deployment_process(pid)
        except Exception:
            pass
        shutil.rmtree(get_deploy_folder(uid, dep_id), ignore_errors=True)
        try:
            files_repo_async_delete(uid, dep_id)
        except Exception:
            pass
        _p2_purge_snapshots(dep_id)
        for tbl in ("dep_settings", "dep_metrics", "dep_shares"):
            _q(f"DELETE FROM {tbl} WHERE deployment_id = ?", (dep_id,))
        _q("DELETE FROM deployments WHERE deployment_id = ?", (dep_id,))
    _q("DELETE FROM dep_shares WHERE user_id = ?", (uid,))
    try:
        shutil.rmtree(DEPLOYMENTS_DIR / str(uid), ignore_errors=True)
    except Exception:
        pass
    for t in (_q("SELECT id FROM tickets WHERE user_id = ?", (uid,), fetch='all') or []):
        _q("DELETE FROM ticket_msgs WHERE ticket_id = ?", (t[0],))
    for sql in ("DELETE FROM tickets WHERE user_id = ?", "DELETE FROM user_pins WHERE user_id = ?",
                "DELETE FROM user_overrides WHERE user_id = ?", "DELETE FROM deploy_credits WHERE user_id = ?",
                "DELETE FROM user_discounts WHERE user_id = ?", "DELETE FROM export_log WHERE user_id = ?",
                "UPDATE payment_orders SET email = NULL WHERE user_id = ?"):
        _q(sql, (uid,))
    try:
        _q("DELETE FROM kv WHERE key = ?", (f"email_{uid}",))
    except Exception:
        pass
    _q("UPDATE users SET username = NULL, first_name = 'Deleted user', coins_balance = 0, stars_balance = 0, is_premium = 0, "
       "premium_expires = NULL, premium_plan = NULL, step = NULL, temp_file = NULL, requirements = NULL, env_vars = NULL, "
       "plan = NULL, payment_method = NULL, temp_target_user = NULL, pending_payment_payload = NULL, referral_code = NULL, "
       "referred_by = NULL, resource_tier_mb = 0, resource_tier_key = NULL, resource_tier_expires = NULL, channels_verified = NULL, pending_json = '{}', last_active = ? WHERE user_id = ?", (_now(), uid))
    audit("system", "account_deleted", uid)
    try:
        async_backup(f"account_deleted_{uid}")
    except Exception:
        pass


def _p2_text_delconf(message, user_step, uid, chat_id, text):
    if is_admin(uid):
        return True
    if text != "DELETE":
        send_message(chat_id, "Cancelled — nothing was deleted. (You must type DELETE exactly.)",
                     _fp_kb([_fp_btn("🏠 Menu", "main_menu")]))
        return True
    delete_account(uid)
    send_message(chat_id, "✅ *Your account has been deleted.* Your bots, files and personal details are gone. "
                          "You're welcome back any time with /start.")
    return True


_P2_TEXT['fp_p2delconf'] = _p2_text_delconf
_P2_ROUTES.append(("p2_data", _p2_r_data))
_P2_ROUTES.append(("p2_exp", _p2_r_data))
_P2_ROUTES.append(("p2_delacct", _p2_r_data))


def _p2_install_delete_hook():
    g = globals()
    _dd = g['delete_deployment']

    def dd(deployment_id, user_id, chat_id):
        r = _dd(deployment_id, user_id, chat_id)
        if r:
            _p2_purge_snapshots(deployment_id)
        return r
    g['delete_deployment'] = dd


_p2_install_delete_hook()


# ---------------------------------------------------------------- Telegram Mini App dashboard
from urllib.parse import parse_qsl as _parse_qsl
MA_AUTH_MAX_AGE = 86400
MA_TAIL_CHARS = 6000
_MA_RATE = {}


def ma_verify_init_data(init_data, token=None, max_age=MA_AUTH_MAX_AGE, now=None):
    """Validate Telegram WebApp initData (HMAC, see core.telegram.org/bots/webapps). -> user dict or None."""
    token = token if token is not None else (BOT_TOKEN or "")
    try:
        if not init_data or not token or len(init_data) > 4096:
            return None
        pairs = dict(_parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(want, got):
            return None
        auth = int(pairs.get("auth_date", "0"))
        if (now or time.time()) - auth > max_age or auth - (now or time.time()) > 300:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if isinstance(user, dict) and user.get("id") else None
    except Exception:
        return None


def ma_access(uid, dep_id):
    """The deployment if `uid` owns it or it is shared with them (admins get no special access here)."""
    d = _fp_dep(dep_id)
    if not d:
        return None
    if d["owner"] == uid:
        return d
    if _q("SELECT 1 FROM dep_shares WHERE deployment_id = ? AND user_id = ?", (dep_id, uid), fetch='one'):
        return d
    return None


def _ma_rate_ok(uid, kind, limit, window=60):
    now = time.time()
    k = (uid, kind)
    hits = [t for t in _MA_RATE.get(k, []) if now - t < window]
    if len(hits) >= limit:
        _MA_RATE[k] = hits
        return False
    hits.append(now)
    _MA_RATE[k] = hits
    return True


def _ma_series(dep_id, hours=24, points=48):
    since = int(time.time()) - hours * 3600
    rows = _q("SELECT ts, cpu, ram FROM dep_metrics WHERE deployment_id = ? AND ts >= ? ORDER BY ts", (dep_id, since), fetch='all') or []
    if not rows:
        return {"cpu": [], "ram": []}
    step = hours * 3600 / points
    buckets = {}
    for ts, cpu, ram in rows:
        b = int((ts - since) // step)
        buckets.setdefault(b, []).append((cpu or 0, ram or 0))
    cpu, ram = [], []
    for b in range(points):
        v = buckets.get(b)
        cpu.append(round(sum(x[0] for x in v) / len(v), 1) if v else None)
        ram.append(round(sum(x[1] for x in v) / len(v), 1) if v else None)
    return {"cpu": cpu, "ram": ram}


def _ma_latest(dep_id):
    r = _q("SELECT cpu, ram FROM dep_metrics WHERE deployment_id = ? ORDER BY ts DESC LIMIT 1", (dep_id,), fetch='one')
    return (r[0], r[1]) if r else (None, None)


def _ma_dep_summary(d, uid):
    cpu, ram = _ma_latest(d["id"])
    if d["status"] == "paused" and pause_row(d["id"]):
        d = dict(d, expire=None)
    _st = _q("SELECT start_time FROM deployments WHERE deployment_id = ?", (d["id"],), fetch='one')
    _u = bot_uptime(d["id"], _st[0] if _st else None)
    return {"uptime": _u["pct"] if _u else None, "uptime_days": _u["days"] if _u else None, "id": d["id"], "name": display_filename(d["file"]), "status": effective_status(d["owner"], d["id"], d["file"], d["status"]), "expire": d["expire"],
            "shared": d["owner"] != uid, "cpu": cpu, "ram": ram}


def ma_handle_api(uid, method, path):
    """-> (status, json-able). Pure function of (user, request): no env values ever leave here."""
    if get_ban_info(uid):
        return 403, {"error": "banned"}
    if not _ma_rate_ok(uid, "api", 90):
        return 429, {"error": "slow down"}
    if path == "/api/me" and method == "GET":
        bal = get_user_balances(uid)
        tier = get_user_tier(uid)
        n = _q("SELECT COUNT(*) FROM deployments WHERE user_id = ?", (uid,), fetch='one')[0]
        return 200, {"id": uid, "coins": bal['coins'], "stars": bal['stars'], "deployments": n,
                     "tier": (RESOURCE_TIERS.get(tier[0], (0, format_resource(tier[1]), 0))[1] if tier else None),
                     "tier_until": tier[2].strftime("%Y-%m-%d") if tier else None,
                     "premium": bool(is_user_premium(uid))}
    if path == "/api/deployments" and method == "GET":
        rows = _q("SELECT deployment_id FROM deployments WHERE user_id = ? UNION SELECT deployment_id FROM dep_shares WHERE user_id = ?",
                  (uid, uid), fetch='all') or []
        out = [_ma_dep_summary(d, uid) for d in (ma_access(uid, r[0]) for r in rows) if d]
        return 200, {"deployments": sorted(out, key=lambda x: -x["id"])}
    if path == "/api/restart-all" and method == "POST":
        if not _ma_rate_ok(uid, "rall", 3):
            return 429, {"error": "too many actions"}
        started, wait = restart_all(uid, uid, RALL_USER_COOLDOWN)
        if wait:
            return 429, {"error": f"wait {wait}s"}
        return 202, {"ok": True, "started": started or 0}
    m = re.match(r'^/api/deployment/(\d+)(?:/(restart|stop))?$', path)
    if m:
        dep_id, action = int(m.group(1)), m.group(2)
        d = ma_access(uid, dep_id)
        if not d:
            return 404, {"error": "not found"}
        if action is None and method == "GET":
            tail = ""
            try:
                lf = d["folder"] / "output.log"
                if lf.exists():
                    with open(lf, "rb") as f:
                        f.seek(0, 2); f.seek(max(0, f.tell() - MA_TAIL_CHARS))
                        tail = f.read().decode("utf-8", "replace")
            except Exception:
                pass
            r = _ma_dep_summary(d, uid)
            meta = _q("SELECT plan, start_time FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one') or (None, None)
            r.update({"plan": meta[0], "started": meta[1], "source": d["source"]})
            r.update({"usage": _ma_series(dep_id), "logs": tail[-MA_TAIL_CHARS:], "can_control": r["status"] in ("active", "missing", "stopped", "crashed", "failed", "error")})
            return 200, r
        if action and method == "POST":
            if not _ma_rate_ok(uid, "act", 6):
                return 429, {"error": "too many actions"}
            if d["status"] == "expired":
                return 409, {"error": "expired"}

            def run():
                try:
                    if action == "restart":
                        _fp_restart(dep_id, "miniapp")
                    else:
                        stop_deployment(dep_id)
                    audit(uid, f"miniapp_{action}", dep_id)
                except Exception as e:
                    print(f"⚠️ miniapp {action} #{dep_id}: {e}")
            threading.Thread(target=run, daemon=True, name=f"MiniApp{action}").start()
            return 202, {"ok": True}
    return 404, {"error": "not found"}


_MA_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Dashboard</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--line:#e4e7ec;--fg:#101828;--mut:#667085;--acc:#4353ff;--accfg:#fff;--ok:#12a150;--okbg:#e3f6ec;--bad:#d92d20;--badbg:#fde7e5;--warn:#b54708;--warnbg:#fef0c7;--idle:#667085;--idlebg:#eceff3;--term:#0d1117;--termfg:#d0d7de}
body.dark{--bg:#0b0d12;--panel:#14171f;--line:#262b36;--fg:#eef0f4;--mut:#8b93a3;--acc:#6b79ff;--okbg:#10301f;--badbg:#3a1613;--warnbg:#3a2a0a;--idlebg:#1e2330;--ok:#3ccf7e;--bad:#ff6b5e;--warn:#f5b84a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.top{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;align-items:center;gap:12px}
.logo{font-weight:800;letter-spacing:-.02em;font-size:16px;display:flex;align-items:center;gap:8px}.logo i{width:22px;height:22px;border-radius:6px;background:var(--acc);display:inline-block}
.sp{flex:1}.wrap{max-width:980px;margin:0 auto;padding:18px 16px 60px}
h1{font-size:22px;margin:0;letter-spacing:-.02em}h2{font-size:12px;margin:22px 0 8px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.mut{color:var(--mut);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}.big{font-size:24px;font-weight:700;letter-spacing:-.02em}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:16px 0 10px}
input.s{flex:1;min-width:140px;padding:9px 12px;border-radius:9px;border:1px solid var(--line);background:var(--panel);color:var(--fg);font:inherit}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--mut);border-radius:999px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}.chip.on{background:var(--acc);color:var(--accfg);border-color:var(--acc)}
button.b{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:9px;padding:8px 14px;cursor:pointer}
button.b.p{background:var(--acc);color:var(--accfg);border-color:var(--acc)}button.b.d{color:var(--bad)}button.b:disabled{opacity:.5;cursor:default}
.svc{display:flex;align-items:center;gap:12px;padding:14px;border:1px solid var(--line);background:var(--panel);border-radius:12px;margin-bottom:8px;cursor:pointer}
.svc:hover{border-color:var(--acc)}.svc .nm{font-weight:600;word-break:break-all}.svc .meta{flex:1;min-width:0}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:600;background:var(--idlebg);color:var(--idle);white-space:nowrap}
.pill:before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.active{background:var(--okbg);color:var(--ok)}.pill.crashed,.pill.failed,.pill.error{background:var(--badbg);color:var(--bad)}.pill.paused,.pill.expired,.pill.missing{background:var(--warnbg);color:var(--warn)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:16px 0}.tab{background:none;border:0;border-bottom:2px solid transparent;color:var(--mut);padding:9px 12px;font:inherit;font-weight:600;cursor:pointer}.tab.on{color:var(--fg);border-color:var(--acc)}
.kv{display:grid;grid-template-columns:130px 1fr;gap:8px 12px}.kv div:nth-child(odd){color:var(--mut)}
pre{margin:0;background:var(--term);color:var(--termfg);border-radius:10px;padding:12px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-all;max-height:62vh;overflow:auto}
svg{width:100%;height:110px;display:block}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.err{color:var(--bad)}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:9px 16px;border-radius:9px;font-weight:600;z-index:9}
</style></head><body><div class="top"><div class="logo"><i></i>Dashboard</div><div class="sp"></div><div class="mut" id="who"></div></div>
<div class="wrap" id="app"><div class="mut">Loading…</div></div>
<script>
const tg=window.Telegram&&Telegram.WebApp;if(tg){tg.ready();tg.expand()}
if((tg&&tg.colorScheme==='dark')||(!tg&&matchMedia('(prefers-color-scheme: dark)').matches))document.body.classList.add('dark');
const H={'X-Init-Data':tg?tg.initData:''};let cur=null,timer=null,filt='all',q='',tab='overview',DEPS=[];
async function api(p,m){const r=await fetch(p,{method:m||'GET',headers:H,cache:'no-store'});let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function toast(m){const t=el('div','toast',m);document.body.append(t);setTimeout(()=>t.remove(),2600)}
function ask(msg,go){if(tg&&tg.showConfirm)tg.showConfirm(msg,ok=>ok&&go());else if(confirm(msg))go()}
function pill(s){return el('span','pill '+s,s==='active'?'Running':s==='missing'?'Files missing':s.charAt(0).toUpperCase()+s.slice(1))}
function chart(vals,color,unit){const pts=vals.filter(x=>x!=null);const w=el('div','card');if(pts.length<2){w.append(el('div','mut','Not enough data yet'));return w}
const mx=Math.max(...pts,1),last=pts[pts.length-1],avg=pts.reduce((a,b)=>a+b,0)/pts.length;
const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 200 60');s.setAttribute('preserveAspectRatio','none');
let d='';vals.forEach((x,i)=>{if(x==null)return;d+=(d&&vals[i-1]!=null?'L':'M')+(i/(vals.length-1)*200).toFixed(1)+' '+(57-(x/mx)*52).toFixed(1)});
const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',color);p.setAttribute('stroke-width','2');p.setAttribute('vector-effect','non-scaling-stroke');s.append(p);
const r=el('div','row');r.style.justifyContent='space-between';r.append(el('span','mut','now '+last.toFixed(1)+unit),el('span','mut','avg '+avg.toFixed(1)+unit),el('span','mut','peak '+mx.toFixed(1)+unit));w.append(s,r);return w}
async function home(){cur=null;clearTimeout(timer);const a=document.getElementById('app');
try{const[m,l]=await Promise.all([api('/api/me'),api('/api/deployments')]);DEPS=l.deployments;document.getElementById('who').textContent=m.tier?m.tier:(m.premium?'Premium':'Free');
a.replaceChildren();const hd=el('div','row');hd.style.justifyContent='space-between';const t=el('div');t.append(el('h1','','Services'),el('div','mut',DEPS.length+' deployment'+(DEPS.length===1?'':'s')));hd.append(t);
const ra=el('button','b','↻ Restart all');ra.onclick=()=>ask('Restart all your running bots one by one?',async()=>{ra.disabled=true;try{const r=await api('/api/restart-all','POST');toast(r.started?'Restarting '+r.started+' bot(s)…':'Nothing to restart');setTimeout(home,8000)}catch(e){toast(e.message)}ra.disabled=false});hd.append(ra);a.append(hd);
const cnt=k=>DEPS.filter(d=>k==='bad'?['crashed','failed','error','missing'].includes(d.status):d.status===k).length;
const g=el('div','grid');g.style.marginTop='14px';[['Running',cnt('active')],['Failed',cnt('bad')],['Stopped',cnt('stopped')],['Coins',m.coins],['Stars',m.stars]].forEach(x=>{const c=el('div','card');c.append(el('div','mut',x[0]),el('div','big',x[1]));g.append(c)});a.append(g);
const bar=el('div','bar');const si=el('input','s');si.placeholder='Search services…';si.value=q;si.oninput=()=>{q=si.value;list()};bar.append(si);
[['all','All'],['active','Running'],['bad','Failed'],['stopped','Stopped']].forEach(f=>{const c=el('button','chip'+(filt===f[0]?' on':''),f[1]);c.onclick=()=>{filt=f[0];home()};bar.append(c)});a.append(bar);
const box=el('div');box.id='list';a.append(box);list()}
catch(e){a.replaceChildren(el('div','err','Could not load: '+e.message+'. Open this page from the bot.'))}}
function list(){const box=document.getElementById('list');if(!box)return;box.replaceChildren();
const f=DEPS.filter(d=>(filt==='all'||(filt==='bad'?['crashed','failed','error','missing'].includes(d.status):d.status===filt))&&(!q||(d.name+' #'+d.id).toLowerCase().includes(q.toLowerCase())));
if(!f.length)box.append(el('div','mut','No services match.'));
f.forEach(d=>{const c=el('div','svc');const m=el('div','meta');m.append(el('div','nm',d.name),el('div','mut','#'+d.id+(d.shared?' · shared':'')+(d.expire?' · renews '+d.expire.slice(0,10):'')+(d.uptime!=null?' · '+d.uptime+'% up':'')+(d.ram!=null?' · '+d.ram.toFixed(0)+' MB':'')+(d.cpu!=null?' · '+d.cpu.toFixed(0)+'% CPU':'')));c.append(m,pill(d.status),el('span','mut','›'));c.onclick=()=>{tab='overview';detail(d.id)};box.append(c)})}
async function detail(id){cur=id;const a=document.getElementById('app');
try{const d=await api('/api/deployment/'+id);if(cur!==id)return;a.replaceChildren();const b=el('button','b','‹ All services');b.onclick=home;a.append(b);
const hd=el('div','row');hd.style.cssText='margin-top:14px;justify-content:space-between';const t=el('div');t.append(el('h1','',d.name),el('div','mut','#'+d.id+(d.shared?' · shared with you':'')));
const r=el('div','row');r.append(pill(d.status));
if(d.can_control){const rb=el('button','b p','↻ Restart'),sb=el('button','b d','■ Stop');
const act=k=>ask((k==='restart'?'Restart':'Stop')+' this bot?',async()=>{rb.disabled=sb.disabled=true;try{await api('/api/deployment/'+id+'/'+k,'POST');toast(k==='restart'?'Restarting…':'Stopping…');setTimeout(()=>detail(id),k==='restart'?6000:2500)}catch(e){toast(e.message);rb.disabled=sb.disabled=false}});
rb.onclick=()=>act('restart');sb.onclick=()=>act('stop');r.append(rb,sb)}hd.append(t,r);a.append(hd);
const tb=el('div','tabs');[['overview','Overview'],['metrics','Metrics'],['logs','Logs']].forEach(x=>{const c=el('button','tab'+(tab===x[0]?' on':''),x[1]);c.onclick=()=>{tab=x[0];detail(id)};tb.append(c)});a.append(tb);
if(tab==='overview'){const c=el('div','card kv');[['Status',d.status==='active'?'Running':d.status],['Plan',d.plan||'—'],['Source',d.source||'upload'],['Started',d.started?d.started.slice(0,16).replace('T',' '):'—'],['Expires',d.expire?d.expire.slice(0,10):'never'],['Uptime (7d)',d.uptime!=null?d.uptime+'%':'collecting…'],['CPU now',d.cpu!=null?d.cpu.toFixed(1)+'%':'—'],['RAM now',d.ram!=null?d.ram.toFixed(0)+' MB':'—']].forEach(x=>c.append(el('div','',x[0]),el('div','',x[1])));a.append(c)}
if(tab==='metrics'){a.append(el('h2','','CPU · last 24h'),chart(d.usage.cpu,'#e08a1e','%'),el('h2','','Memory · last 24h'),chart(d.usage.ram,'#4353ff',' MB'))}
if(tab==='logs'){const row=el('div','row');row.style.marginBottom='8px';const cp=el('button','b','Copy');cp.onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(d.logs||'').then(()=>toast('Copied'))};row.append(cp,el('span','mut','auto-refreshing · last '+(d.logs||'').length+' chars'));const pr=el('pre','',d.logs||'(no output yet)');a.append(row,pr);pr.scrollTop=pr.scrollHeight}
clearTimeout(timer);timer=setTimeout(()=>cur===id&&detail(id),tab==='logs'?8000:20000)}
catch(e){a.replaceChildren(el('div','err',e.message));const b=el('button','b','‹ Back');b.onclick=home;a.append(b)}}
home();
</script></body></html>"""


def p2_app_http(h, method, path):
    if path == "/app":
        _p2_send(h, 200, _MA_HTML, "text/html; charset=utf-8",
                 {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                  "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
                                             "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                                             "frame-ancestors https://*.telegram.org https://telegram.org"})
        return True
    if path.startswith("/api/"):
        user = ma_verify_init_data(h.headers.get("X-Init-Data", ""))
        if not user:
            _p2_send(h, 401, json.dumps({"error": "unauthorized"}), "application/json", {"Cache-Control": "no-store"})
            return True
        code, body = ma_handle_api(int(user["id"]), method, path)
        _p2_send(h, code, json.dumps(body), "application/json", {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return True
    return False


def _p2_http_app(h, method, path):
    return p2_app_http(h, method, path) if (path == "/app" or path.startswith("/api/")) else False


_P2_HTTP.append(_p2_http_app)


def _p2_dash_button():
    base = _public_base_url()
    return {"text": "📱 Dashboard", "web_app": {"url": base + "/app"}} if base.startswith("https://") else None


def _p2_more_rows():
    b = _p2_dash_button()
    return ([[b]] if b else []) + list(_P2_MORE_ROWS)


# ---------------------------------------------------------------- UPGRADE 1: admin health dashboard + alerts
_P2_BOOT = time.time()
HEALTH_DISK_PCT = float(os.environ.get("HEALTH_DISK_PCT", "85"))
HEALTH_RAM_PCT = float(os.environ.get("HEALTH_RAM_PCT", "92"))
HEALTH_BACKUP_STALE_H = float(os.environ.get("HEALTH_BACKUP_STALE_H", "6"))
HEALTH_ALERT_COOLDOWN = 6 * 3600
_HEALTH_ALERTED = {}           # key -> time of the last alert (present while the problem persists)
_HEALTH_RAM_HITS = [0]


def host_stats():
    """RAM / CPU / disk of the host. Every field is None when it can't be read."""
    s = {"ram_pct": None, "ram_used": None, "ram_total": None, "cpu_pct": None, "load": None,
         "disk_pct": None, "disk_free_gb": None, "disk_total_gb": None}
    total, used, _avail = _read_proc_meminfo()
    if not total and _psutil:
        try:
            vm = _psutil.virtual_memory(); total, used = vm.total / 1048576, (vm.total - vm.available) / 1048576
        except Exception:
            pass
    if total:
        s.update(ram_total=round(total), ram_used=round(used), ram_pct=round(used / total * 100, 1))
    try:
        s["cpu_pct"] = round(_psutil.cpu_percent(interval=0.3), 1) if _psutil else None
    except Exception:
        pass
    try:
        s["load"] = round(os.getloadavg()[0], 2)
    except Exception:
        pass
    try:
        du = shutil.disk_usage(BASE_DIR if Path(BASE_DIR).exists() else "/")
        s.update(disk_pct=round(du.used / du.total * 100, 1), disk_free_gb=round(du.free / 2 ** 30, 2),
                 disk_total_gb=round(du.total / 2 ** 30, 1))
    except Exception:
        pass
    return s


def backup_status():
    """-> (state, text). state: 'off' | 'ok' | 'stale' | 'never'"""
    if not GITHUB_ENABLED:
        return "off", "not configured"
    last = _last_backup_time
    try:
        db_mtime = Path(DATABASE_FILE).stat().st_mtime
    except Exception:
        db_mtime = 0
    if not last:
        return ("stale" if time.time() - _P2_BOOT > HEALTH_BACKUP_STALE_H * 3600 and db_mtime > _P2_BOOT else "never"), "no backup since start"
    age_h = (time.time() - last) / 3600
    changed_since = db_mtime > last + 1800
    ago = f"{int(age_h * 60)} min ago" if age_h < 1 else f"{age_h:.1f} h ago"
    if changed_since and age_h > HEALTH_BACKUP_STALE_H:
        return "stale", f"last OK {ago}, newer data NOT backed up"
    return "ok", f"last OK {ago}"


def _bar(pct, n=10):
    if pct is None:
        return "n/a"
    f = max(0, min(n, round(pct / 100 * n)))
    return "▓" * f + "░" * (n - f) + f" {pct:g}%"


def health_report():
    h = host_stats()
    bots = dict(_q("SELECT status, COUNT(*) FROM deployments GROUP BY status", fetch='all') or [])
    users = _q("SELECT COUNT(*) FROM users", fetch='one')[0]
    new24 = _q("SELECT COUNT(*) FROM users WHERE join_date >= ?", ((datetime.now() - timedelta(days=1)).isoformat(),), fetch='one')[0]
    tickets = _q("SELECT COUNT(*) FROM tickets WHERE status = 'open'", fetch='one')[0]
    try:
        bugs = db_execute("SELECT COUNT(*) FROM bug_reports WHERE status = 'open'", fetch='one')[0]
    except Exception:
        bugs = 0
    bstate, btxt = backup_status()
    up = int(time.time() - _P2_BOOT)
    nmiss = len(missing_active_ids())
    running, down = max(0, bots.get("active", 0) - nmiss), sum(v for k, v in bots.items() if k in ("crashed", "failed", "error"))
    lines = [
        "*🩺 SYSTEM HEALTH*", "",
        f"🖥 CPU   `{_bar(h['cpu_pct'])}`" + (f"  load {h['load']}" if h['load'] is not None else ""),
        f"🧠 RAM   `{_bar(h['ram_pct'])}`" + (f"  {h['ram_used']}/{h['ram_total']} MB" if h['ram_total'] else ""),
        f"💽 Disk  `{_bar(h['disk_pct'])}`" + (f"  {h['disk_free_gb']} GB free" if h['disk_free_gb'] is not None else ""),
        "",
        f"🤖 Bots: 🟢 `{running}` running · 🔴 `{down}` crashed · 📂 `{nmiss}` files missing · ⏹ `{bots.get('stopped', 0)}` stopped · ⌛ `{bots.get('expired', 0)}` expired",
        f"👥 Users: `{users}` (+`{new24}` in 24h)",
        f"🧾 Deploy queue: `{len(_DQ)}` waiting · `{_DQ_INFLIGHT[0]}` deploying",
        f"🎫 Open tickets: `{tickets}` · 🐛 bug reports: `{bugs}`",
        f"💾 DB backup: {'✅' if bstate == 'ok' else ('⚠️' if bstate == 'stale' else '➖')} {btxt}",
        f"⏱ Process uptime: `{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m`",
    ]
    alerts = health_problems(h, bstate)
    if alerts:
        lines += ["", "*⚠️ Active problems*"] + [f"• {a[1]}" for a in alerts]
    return "\n".join(lines)


def health_problems(h=None, bstate=None):
    """-> [(key, text)] for the conditions that currently need attention."""
    h = h or host_stats()
    bstate = bstate or backup_status()[0]
    out = []
    if h["disk_pct"] is not None and h["disk_pct"] >= HEALTH_DISK_PCT:
        out.append(("disk", f"Disk is {h['disk_pct']:g}% full ({h['disk_free_gb']} GB free)"))
    if h["ram_pct"] is not None and h["ram_pct"] >= HEALTH_RAM_PCT:
        out.append(("ram", f"RAM is {h['ram_pct']:g}% used"))
    if bstate == "stale":
        out.append(("backup", "The GitHub database backup is stale — newer data isn't being saved"))
    return out


def health_check_once(now=None):
    """Alert admins about NEW problems (rate-limited) and tell them when a problem is gone. -> messages sent"""
    now = now or time.time()
    h = host_stats()
    probs = health_problems(h)
    if any(k == "ram" for k, _ in probs):                  # one RAM spike isn't an alert; two in a row is
        _HEALTH_RAM_HITS[0] += 1
        if _HEALTH_RAM_HITS[0] < 2:
            probs = [p for p in probs if p[0] != "ram"]
    else:
        _HEALTH_RAM_HITS[0] = 0
    sent = 0
    if kv_get("health_alerts", "1") != "0":
        for key, text in probs:
            last = _HEALTH_ALERTED.get(key)
            if last is None or now - last >= HEALTH_ALERT_COOLDOWN:
                _HEALTH_ALERTED[key] = now
                for a in list(ADMIN_IDS):
                    try:
                        send_message(a, f"🚨 *Host alert*\n\n{text}", _fp_kb([_fp_btn("🩺 System health", "p2_ah")]))
                    except Exception:
                        pass
                sent += 1
    active = {k for k, _ in probs}
    for key in [k for k in _HEALTH_ALERTED if k not in active]:
        del _HEALTH_ALERTED[key]
        if kv_get("health_alerts", "1") != "0":
            for a in list(ADMIN_IDS):
                try:
                    send_message(a, f"✅ *Resolved:* the {key} problem is gone.")
                except Exception:
                    pass
            sent += 1
    return sent


def p2_health_monitor():
    sleep(150)
    print("✅ Host-health monitor started")
    while True:
        try:
            health_check_once()
        except Exception as e:
            print(f"⚠️ health monitor: {e}")
        sleep(300)


def _p2_r_ah(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    if data == "p2_ahtog":
        kv_set("health_alerts", "0" if kv_get("health_alerts", "1") != "0" else "1")
    on = kv_get("health_alerts", "1") != "0"
    edit_message(chat_id, mid, health_report(),
                 _fp_kb([_fp_btn("🔄 Refresh", "p2_ah")], [_fp_btn(f"🔔 Alerts: {'ON' if on else 'OFF'}", "p2_ahtog")],
                        [_fp_btn("🔙 Admin Panel", "admin_panel")]))
    return True


_P2_ROUTES.append(("p2_ah", _p2_r_ah))
_P2_ADMIN_ROWS.insert(0, [_fp_btn("🩺 System health", "p2_ah")])


# ---------------------------------------------------------------- FILES-ON-DISK CHECK
def dep_files_ok(owner, dep_id, fname):
    """True when the bot's main script is really on the disk (a wiped/ephemeral disk leaves 'active' rows with no files)."""
    try:
        if not fname:
            return False
        return (get_deploy_folder(owner, dep_id) / fname).is_file()
    except Exception:
        return False


def effective_status(owner, dep_id, fname, status):
    """DB status, except an 'active' bot whose files are gone is 'missing' - never counted as running."""
    return "missing" if status == "active" and not dep_files_ok(owner, dep_id, fname) else status


def missing_active_ids(owner=None):
    q = "SELECT deployment_id, user_id, file_name FROM deployments WHERE status = 'active'"
    p = ()
    if owner is not None:
        q += " AND user_id = ?"; p = (owner,)
    return [r[0] for r in (db_execute(q, p, fetch='all') or []) if not dep_files_ok(r[1], r[0], r[2])]


# ---------------------------------------------------------------- RESTART ALL
RESTART_ALL_STATUSES = ("active", "crashed", "failed", "error")
_RALL_LAST = {}
_RALL_LOCK = threading.Lock()
RALL_USER_COOLDOWN = 120
RALL_ADMIN_COOLDOWN = 600


def restartable_ids(owner=None):
    """Bots a 'restart all' touches: running or crashed ones (never stopped, paused or expired)."""
    q = "SELECT deployment_id FROM deployments WHERE status IN (%s)" % ",".join("?" * len(RESTART_ALL_STATUSES))
    p = list(RESTART_ALL_STATUSES)
    if owner is not None:
        q += " AND user_id = ?"; p.append(owner)
    gone = set(missing_active_ids(owner))
    ids = [r[0] for r in (db_execute(q + " ORDER BY deployment_id", tuple(p), fetch='all') or [])]
    if FILES_REPO_ENABLED:
        return ids                          # bots with missing files are brought back from the GitHub backup first
    return [i for i in ids if i not in gone]


RALL_GAP = 2.0


def restart_all(scope, requester, cooldown, on_done=None, gap=None):
    """Restart many bots one after another (a gap avoids a RAM spike). scope = owner id or None for every bot.
    -> (started, wait_seconds): started False means cooldown or nothing to restart."""
    key = ("all" if scope is None else scope)
    with _RALL_LOCK:
        left = cooldown - (time.time() - _RALL_LAST.get(key, 0))
        if left > 0:
            return False, int(left) + 1
        ids = restartable_ids(scope)
        if not ids:
            return False, 0
        _RALL_LAST[key] = time.time()

    def run():
        ok = bad = restored = 0
        nofiles = []
        for i in ids:
            try:
                row = db_execute("SELECT user_id, file_name FROM deployments WHERE deployment_id = ?", (i,), fetch='one')
                if row and not dep_files_ok(row[0], i, row[1]):
                    folder = get_deploy_folder(row[0], i)
                    got, _env = _files_restore_for_relaunch(row[0], i, folder, folder / row[1],
                                                            log=lambda m, _d=i: print(f"   [restart-all dep#{_d}] {m}"))
                    if not (got and dep_files_ok(row[0], i, row[1])):
                        bad += 1
                        nofiles.append(i)
                        continue
                    restored += 1
                _fp_restart(i, "restart_all")
                ok += 1
            except Exception as e:
                bad += 1
                print(f"⚠️ restart-all #{i}: {e}")
            sleep(RALL_GAP if gap is None else gap)
        try:
            audit(requester, "restart_all", f"{'all' if scope is None else scope}:{ok}ok/{restored}restored/{bad}fail")
        except Exception:
            pass
        if on_done:
            try:
                on_done(ok, bad, restored, nofiles)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True, name="RestartAll").start()
    return len(ids), 0


def _p2_r_rall(callback, data, uid, chat_id, mid):
    admin = data.startswith("p2_arall")
    if admin and not is_admin(uid):
        return True
    scope = None if admin else uid
    ids_ = restartable_ids(scope)
    n = len(ids_)
    nmiss = len(set(missing_active_ids(scope)) & set(ids_))
    back = "admin_panel" if admin else "my_deployments"
    if data in ("p2_rall", "p2_arall"):
        txt = (f"🔄 *Restart ALL platform bots?*\n\n`{n}` bot(s) of every user will be restarted one by one (about 2s apart). "
               "Stopped, paused and expired bots are skipped." if admin else
               f"🔄 *Restart all your bots?*\n\n`{n}` bot(s) will be restarted one by one. Stopped, paused and expired bots are skipped.")
        if nmiss:
            txt += f"\n📥 `{nmiss}` of them have missing files — those will be re-downloaded from the GitHub backup first."
        elif FILES_REPO_ENABLED is False and missing_active_ids(scope):
            txt += "\n⚠️ Bots with missing files are skipped (no GitHub file backup is configured to restore them)."
        rows = [[_fp_btn(f"✅ Restart {n}" if n else "Nothing to restart", data + "go" if n else back)],
                [_fp_btn("🔙 Cancel", back)]]
        edit_message(chat_id, mid, txt, _fp_kb(*rows))
        return True
    if data in ("p2_rallgo", "p2_arallgo"):
        def done(ok, bad, restored=0, nofiles=()):
            msg = f"✅ Restart all finished: `{ok}` restarted"
            if restored:
                msg += f" (`{restored}` had missing files and were re-downloaded from the backup)"
            if bad:
                msg += f", `{bad}` failed"
            msg += "."
            if nofiles:
                msg += "\n⚠️ No usable backup for bot(s): " + ", ".join(f"#{i}" for i in nofiles[:20]) + " — re-upload or re-deploy them."
            send_message(chat_id, msg)
        started, wait = restart_all(scope, uid, RALL_ADMIN_COOLDOWN if admin else RALL_USER_COOLDOWN, done)
        if started:
            edit_message(chat_id, mid, f"🔄 Restarting `{started}` bot(s)… I'll message you when it's done.",
                         _fp_kb([_fp_btn("📦 My Deployments" if not admin else "🔙 Admin Panel", back)]))
        elif wait:
            edit_message(chat_id, mid, f"⏳ Restart all was just used. Try again in `{wait}`s.", _fp_kb([_fp_btn("🔙 Back", back)]))
        else:
            edit_message(chat_id, mid, "Nothing to restart.", _fp_kb([_fp_btn("🔙 Back", back)]))
        return True
    return False


_P2_ROUTES.append(("p2_rall", _p2_r_rall))
_P2_ROUTES.append(("p2_arall", _p2_r_rall))
_P2_ADMIN_ROWS.insert(1, [_fp_btn("🔄 Restart ALL bots", "p2_arall")])


# ---------------------------------------------------------------- UPGRADE 2: crash alerts with reason + Fix buttons
_CF_RULES = [
    ("missing_module", re.compile(r"No module named ['\"]?([A-Za-z0-9_.\-]+)|Cannot find module ['\"]([^'\"\n]+)['\"]")),
    ("conflict", re.compile(r"terminated by other getUpdates|Conflict: terminated|409 Conflict", re.I)),
    ("bad_token", re.compile(r"InvalidToken|Improper token|LoginFailure|Invalid token|token is invalid|TokenValidationError|"
                             r"401:? Unauthorized|Unauthorized\b.*token|token.*(?:rejected|not valid)|ETELEGRAM: 401", re.I)),
    ("missing_env", re.compile(r"KeyError: ['\"]([A-Z][A-Z0-9_]{2,})['\"]|\b([A-Z][A-Z0-9_]{2,}) (?:environment variable )?(?:is )?(?:not set|missing|required)\b")),
    ("syntax", re.compile(r"SyntaxError|IndentationError|TabError")),
    ("port", re.compile(r"Address already in use|EADDRINUSE")),
    ("memory", re.compile(r"MemoryError|Cannot allocate memory|heap out of memory|OOMKilled|\bKilled\b")),
]
_CF_TITLES = {
    "missing_module": "A package is missing",
    "conflict": "The same bot token is running somewhere else",
    "bad_token": "Telegram/Discord rejected the bot token",
    "missing_env": "A required variable isn't set",
    "syntax": "There's a syntax error in the code",
    "port": "The port is already in use",
    "memory": "The bot ran out of memory",
    "unknown": "The bot hit an error",
}
_CF_BUSY = set()


def crash_diagnose(text):
    """Classify a crash from the tail of its log. -> {'code','title','detail','pkg','var'}"""
    text = (text or "")[-6000:]
    out = {"code": "unknown", "title": _CF_TITLES["unknown"], "detail": "", "pkg": None, "var": None}
    for code, rx in _CF_RULES:
        ms = list(rx.finditer(text))
        if not ms:
            continue
        m = ms[-1]
        out["code"], out["title"] = code, _CF_TITLES[code]
        g = [x for x in m.groups() if x]
        if code == "missing_module" and g:
            if m.group(2):      # node: keep the full package name (discord.js, @scope/pkg)
                out["pkg"] = "/".join(g[0].split("/")[:2 if g[0].startswith("@") else 1])
            else:
                out["pkg"] = g[0].split(".")[0]
            out["detail"] = f"`{out['pkg']}` isn't installed."
        elif code == "missing_env" and g:
            out["var"] = g[0].upper()
            out["detail"] = f"`{out['var']}` is not set."
        elif code == "syntax":
            loc = re.findall(r'File "([^"]+)", line (\d+)', text)
            if loc:
                out["detail"] = f"`{Path(loc[-1][0]).name}` line {loc[-1][1]}."
        break
    if out["code"] == "unknown":
        errs = [l.strip() for l in text.splitlines() if re.search(r"\b\w*(Error|Exception)\b", l) and not l.startswith(" ")]
        if errs:
            out["detail"] = "`" + errs[-1][:140].replace("`", "'") + "`"
    return out


_CF_ADVICE = {
    "missing_module": "Tap *Install* and I'll add it and restart the bot.",
    "conflict": "Another copy of this bot is polling with the same token. Stop the other copy (or this one).",
    "bad_token": "Check the token in your variables (get a fresh one from @BotFather if needed).",
    "missing_env": "Add the variable and restart.",
    "syntax": "Fix the line in the code editor and restart.",
    "port": "Use the PORT variable the host gives you instead of a fixed port.",
    "memory": "Your plan's RAM limit was hit. Trim memory use or upgrade your resources.",
    "unknown": "Open the logs to see the full error.",
}


def crash_fix_rows(dep_id, diag):
    rows = []
    c = diag["code"]
    if c == "missing_module":
        rows.append([_fp_btn(f"📦 Install {diag['pkg'] or 'package'} & restart", f"p2_cf_pkg_{dep_id}")])
    elif c == "bad_token":
        rows.append([_fp_btn("🔑 Update token / variables", f"edit_env_{dep_id}")])
    elif c == "missing_env":
        rows.append([_fp_btn(f"➕ Set {diag['var'] or 'variable'}", f"edit_envadd_{dep_id}")])
    elif c == "syntax":
        rows.append([_fp_btn("✏️ Edit code", f"edit_code_{dep_id}")])
    elif c == "memory":
        rows.append([_fp_btn("📈 Upgrade resources", "resource_tiers")])
    elif c == "conflict":
        rows.append([_fp_btn("🛑 Stop this bot", f"stop_deploy_{dep_id}")])
    rows.append([_fp_btn("🔄 Restart", f"restart_deploy_{dep_id}"), _fp_btn("📄 Logs", f"view_runtime_logs_{dep_id}")])
    return rows


def crash_alert_text(dep_id, headline, diag, tail=""):
    t = f"{headline}\n\n🔎 *Reason:* {diag['title']}" + (f" — {diag['detail']}" if diag["detail"] else "")
    t += f"\n💡 {_CF_ADVICE[diag['code']]}"
    if tail:
        t += f"\n\n*Last log lines:*\n```\n{tail}\n```"
    return t


def _p2_r_cf(callback, data, uid, chat_id, mid):
    m = re.match(r"^p2_cf_pkg_(\d+)$", data)
    if not m:
        return False
    dep_id = int(m.group(1))
    d = _fp_own(uid, dep_id)
    if not d:
        return True
    if dep_id in _CF_BUSY:
        edit_message(chat_id, mid, "⏳ Already installing…", None)
        return True
    _CF_BUSY.add(dep_id)
    edit_message(chat_id, mid, f"📦 Installing the missing package for bot #{dep_id}…\nThis can take a minute.", None)

    def run():
        try:
            script = d["folder"] / d["file"]
            pkgs = _install_missing_from_log(d["folder"], script)
            if pkgs:
                req = d["folder"] / "requirements.txt"
                try:
                    have = req.read_text(errors="replace") if req.exists() else ""
                    names = {re.split(r"[=<>!~\[ ;]", l.strip(), 1)[0].lower() for l in have.splitlines() if l.strip() and not l.startswith("#")}
                    add = [p for p in pkgs if re.split(r"[=<>!~\[ ;]", p, 1)[0].lower() not in names]
                    if add and d["source"] != "github":
                        req.write_text(have.rstrip("\n") + ("\n" if have.strip() else "") + "\n".join(add) + "\n")
                except Exception:
                    pass
            _fp_restart(dep_id, "fix")
            audit(uid, "crash_fix_pkg", f"{dep_id}:{','.join(pkgs)[:80]}")
            if pkgs:
                send_message(chat_id, f"✅ Installed `{', '.join(pkgs)[:200]}` and restarted bot #{dep_id}.",
                             _fp_kb([_fp_btn("📄 Logs", f"view_runtime_logs_{dep_id}")]))
            else:
                send_message(chat_id, f"⚠️ I couldn't work out which package to install for bot #{dep_id}, so I only restarted it. "
                                      "Check the logs and add the package to requirements.txt.",
                             _fp_kb([_fp_btn("📄 Logs", f"view_runtime_logs_{dep_id}")]))
        except Exception as e:
            send_message(chat_id, f"❌ Fix failed: {str(e)[:150]}")
        finally:
            _CF_BUSY.discard(dep_id)
    threading.Thread(target=run, daemon=True, name="CrashFixPkg").start()
    return True


_P2_ROUTES.append(("p2_cf", _p2_r_cf))


# ---------------------------------------------------------------- UPGRADE 4: deploy queue
DEPLOY_MAX_CONCURRENT = int(os.environ.get("DEPLOY_MAX_CONCURRENT", "2"))      # deploys running at once (0 = queue disabled)
DEPLOY_QUEUE_RAM_PCT = float(os.environ.get("DEPLOY_QUEUE_RAM_PCT", "92"))     # host RAM % at which new deploys wait
DEPLOY_QUEUE_MAX = int(os.environ.get("DEPLOY_QUEUE_MAX", "20"))
DEPLOY_QUEUE_PER_USER = int(os.environ.get("DEPLOY_QUEUE_PER_USER", "2"))
DEPLOY_QUEUE_MAX_WAIT = int(os.environ.get("DEPLOY_QUEUE_MAX_WAIT", str(25 * 60)))
DEPLOY_EST_SECS = 120
_DQ = []                      # waiting jobs, oldest first
_DQ_LOCK = threading.Lock()
_DQ_INFLIGHT = [0]
_DQ_SECRETS = {}              # job id -> GitHub token (memory only, never written to the DB)
_DQ_ORIG = {}                 # kind -> the real deploy function
_DQ_STARTED = [False]
_P2_DDL.append("CREATE TABLE IF NOT EXISTS deploy_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, "
               "kind TEXT, args TEXT, cost_coins INTEGER DEFAULT 0, method TEXT, is_free INTEGER DEFAULT 0, "
               "created TEXT, status TEXT DEFAULT 'queued')")


def _ram_pct():
    total, used, _a = _read_proc_meminfo()
    return (used / total * 100) if total else None


def dq_busy():
    if _DQ_INFLIGHT[0] >= DEPLOY_MAX_CONCURRENT:
        return True
    p = _ram_pct()
    return p is not None and p >= DEPLOY_QUEUE_RAM_PCT


def dq_position(job_id):
    with _DQ_LOCK:
        for i, j in enumerate(_DQ):
            if j["id"] == job_id:
                return i + 1
    return 0


def _dq_eta_min(pos):
    return max(1, -(-pos // max(1, DEPLOY_MAX_CONCURRENT)) * DEPLOY_EST_SECS // 60)


def _dq_refund(job, why):
    a = job["args"]
    if a.get("payment_method") == "coins" and (a.get("cost_coins") or 0) > 0:
        try:
            update_user_coins(job["uid"], a["cost_coins"], "deployment_refund", f"queue_{job['id']}")
            send_message(job["chat"], f"↩️ {why} `{a['cost_coins']}🪙` refunded.")
            return
        except Exception as e:
            print(f"⚠️ queue refund failed for job {job['id']}: {e}")
    send_message(job["chat"], f"ℹ️ {why}")


def _dq_set(job_id, status):
    try:
        _q("UPDATE deploy_queue SET status = ? WHERE id = ?", (status, job_id))
    except Exception:
        pass


def _dq_drop_temp(job):
    try:
        tf = job["args"].get("temp_file")
        if tf:
            Path(tf).unlink(missing_ok=True)
    except Exception:
        pass


def _dq_enqueue(kind, args):
    uid, chat = args.get("user_id"), args.get("chat_id")
    with _DQ_LOCK:
        mine = [j for j in _DQ if j["uid"] == uid]
        full = len(_DQ) >= DEPLOY_QUEUE_MAX
        dup_free = args.get("is_free") and any(j["args"].get("is_free") for j in mine)
        too_many = len(mine) >= DEPLOY_QUEUE_PER_USER
    if full or dup_free or too_many:
        send_message(chat, "⏳ " + ("The deploy queue is full — please try again in a few minutes." if full else
                                    "You already have a deployment waiting in the queue."))
        if args.get("temp_file"):
            Path(args["temp_file"]).unlink(missing_ok=True)
        return False
    stored = dict(args)
    token = stored.pop("token", None) if kind == "github" else None
    cur_id = None
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        cur = conn.execute("INSERT INTO deploy_queue (user_id, chat_id, kind, args, cost_coins, method, is_free, created) VALUES (?,?,?,?,?,?,?,?)",
                           (uid, chat, kind, json.dumps(stored, default=str), args.get("cost_coins") or 0,
                            args.get("payment_method"), 1 if args.get("is_free") else 0, _now()))
        cur_id = cur.lastrowid
        conn.commit(); conn.close()
    except Exception as e:
        print(f"⚠️ deploy_queue insert: {e}")
    job = {"id": cur_id or int(time.time() * 1000), "kind": kind, "uid": uid, "chat": chat, "args": stored, "ts": time.time()}
    if token:
        _DQ_SECRETS[job["id"]] = token
    with _DQ_LOCK:
        _DQ.append(job)
        pos = len(_DQ)
    send_message(chat, f"⏳ *The host is busy right now*\n\nYour deployment is in the queue — position *#{pos}* "
                       f"(about {_dq_eta_min(pos)} min). It will start by itself and I'll message you.\n"
                       + ("Your coins are held and refunded if it can't start." if (args.get('cost_coins') or 0) and args.get('payment_method') == 'coins' else ""),
                 _fp_kb([_fp_btn("🔄 Position", f"p2_dq_pos_{job['id']}"), _fp_btn("❌ Cancel", f"p2_dq_cancel_{job['id']}")]))
    return True


def _dq_wrap(kind, orig):
    import inspect
    sig = inspect.signature(orig)
    _DQ_ORIG[kind] = orig

    def wrapper(*a, **k):
        try:
            b = sig.bind(*a, **k); b.apply_defaults(); args = dict(b.arguments)
        except TypeError:
            return orig(*a, **k)
        uid = args.get("user_id")
        immune = DEPLOY_MAX_CONCURRENT <= 0 or is_admin(uid) or args.get("payment_method") == "stars"
        if not immune:
            with _DQ_LOCK:
                waiting = bool(_DQ)
            if waiting or dq_busy():
                return _dq_enqueue(kind, args)
        _DQ_INFLIGHT[0] += 1
        try:
            return orig(**args)
        finally:
            _DQ_INFLIGHT[0] = max(0, _DQ_INFLIGHT[0] - 1)
    wrapper.__wrapped__ = orig
    return wrapper


def _dq_run_job(job):
    try:
        _dq_set(job["id"], "running")
        send_message(job["chat"], "🚀 *Your turn!* Starting your deployment now…")
        args = dict(job["args"])
        if job["kind"] == "github":
            args["token"] = _DQ_SECRETS.pop(job["id"], None)
        try:
            ok = _DQ_ORIG[job["kind"]](**args)
        except Exception as e:
            print(f"❌ queued deploy {job['id']}: {e}")
            ok = False
        _dq_set(job["id"], "done" if ok else "failed")
        if not ok:
            _dq_refund(job, "Your queued deployment didn't complete.")
    finally:
        _DQ_INFLIGHT[0] = max(0, _DQ_INFLIGHT[0] - 1)


def dq_tick(now=None):
    """One scheduling pass: expire stale jobs, then start as many as capacity allows. -> started job ids"""
    now = now or time.time()
    with _DQ_LOCK:
        stale = [j for j in _DQ if now - j["ts"] > DEPLOY_QUEUE_MAX_WAIT]
        for j in stale:
            _DQ.remove(j)
    for j in stale:
        _DQ_SECRETS.pop(j["id"], None)
        _dq_set(j["id"], "expired"); _dq_drop_temp(j)
        _dq_refund(j, "The host stayed busy too long, so your deployment was cancelled.")
    started = []
    while True:
        with _DQ_LOCK:
            if not _DQ or dq_busy():
                break
            job = _DQ.pop(0)
            _DQ_INFLIGHT[0] += 1
        started.append(job["id"])
        threading.Thread(target=_dq_run_job, args=(job,), daemon=True, name=f"QueuedDeploy-{job['id']}").start()
    return started


def dq_resume():
    """After a restart: queued uploads continue; queued GitHub jobs (token not stored) are refunded."""
    rows = _q("SELECT id, user_id, chat_id, kind, args, created FROM deploy_queue WHERE status = 'queued' ORDER BY id", fetch='all') or []
    for jid, uid, chat, kind, a, created in rows:
        try:
            args = json.loads(a)
        except Exception:
            args = {}
        job = {"id": jid, "kind": kind, "uid": uid, "chat": chat, "args": args, "ts": time.time()}
        if kind == "upload" and args.get("temp_file") and Path(args["temp_file"]).exists():
            with _DQ_LOCK:
                _DQ.append(job)
        else:
            _dq_set(jid, "lost")
            _dq_refund(job, "The server restarted while you were queued." if kind == "github" else "Your upload was lost in a server restart.")
    _q("UPDATE deploy_queue SET status = 'lost' WHERE status = 'running'")


def p2_deploy_queue_worker():
    try:
        dq_resume()
    except Exception as e:
        print(f"⚠️ deploy queue resume: {e}")
    print("✅ Deploy queue worker started")
    while True:
        try:
            dq_tick()
        except Exception as e:
            print(f"⚠️ deploy queue: {e}")
        sleep(3)


def _p2_r_dq(callback, data, uid, chat_id, mid):
    m = re.match(r"^p2_dq_(pos|cancel)_(\d+)$", data)
    if not m:
        return False
    jid = int(m.group(2))
    with _DQ_LOCK:
        job = next((j for j in _DQ if j["id"] == jid), None)
    if not job or job["uid"] != uid:
        edit_message(chat_id, mid, "✅ This deployment has already started (or isn't in the queue anymore).", None)
        return True
    if m.group(1) == "pos":
        pos = dq_position(jid)
        edit_message(chat_id, mid, f"⏳ *In the deploy queue* — position *#{pos}* (about {_dq_eta_min(pos)} min).",
                     _fp_kb([_fp_btn("🔄 Position", f"p2_dq_pos_{jid}"), _fp_btn("❌ Cancel", f"p2_dq_cancel_{jid}")]))
        return True
    with _DQ_LOCK:
        if job not in _DQ:
            edit_message(chat_id, mid, "✅ It just started — too late to cancel.", None)
            return True
        _DQ.remove(job)
    _DQ_SECRETS.pop(jid, None)
    _dq_set(jid, "cancelled"); _dq_drop_temp(job)
    edit_message(chat_id, mid, "❌ Queued deployment cancelled.", None)
    _dq_refund(job, "Cancelled.")
    return True


_P2_ROUTES.append(("p2_dq", _p2_r_dq))
deploy_with_logs_enhanced = _dq_wrap("upload", deploy_with_logs_enhanced)
deploy_from_github = _dq_wrap("github", deploy_from_github)


# ---------------------------------------------------------------- UPGRADE 6: bot health score (uptime this week)
UPTIME_SLOT = 300                  # the usage sampler writes one point every 5 minutes while a bot is alive
UPTIME_MIN_SECS = 3600             # don't show a percentage before there is an hour of history


def _start_epoch(start_time):
    try:
        return datetime.fromisoformat(start_time).timestamp() if start_time else None
    except Exception:
        return None


def bot_uptime(dep_id, start_time=None, days=7, now=None):
    """Share of the last `days` days (or since the bot started) in which the bot was alive, from the 5-minute usage
    samples. -> {'pct','grade','label','down_secs','days':[pct per day, oldest first]} or None when there isn't an hour of data."""
    now = now or time.time()
    w0 = now - days * 86400
    st = _start_epoch(start_time)
    if st:
        w0 = max(w0, st)
    secs = now - w0
    if secs < UPTIME_MIN_SECS:
        return None
    rows = _q("SELECT DISTINCT ts / ? FROM dep_metrics WHERE deployment_id = ? AND ts >= ?", (UPTIME_SLOT, dep_id, int(w0)), fetch='all') or []
    if not rows:
        return None
    slots = {r[0] for r in rows}
    expected = secs / UPTIME_SLOT
    pct = min(100.0, len(slots) / expected * 100)
    per_day = []
    for i in range(days):
        d0, d1 = now - (days - i) * 86400, now - (days - i - 1) * 86400
        lo, hi = max(d0, w0), d1
        if hi - lo < 3600:
            per_day.append(None)
            continue
        n = sum(1 for s in slots if lo <= s * UPTIME_SLOT < hi)
        per_day.append(min(100.0, n / ((hi - lo) / UPTIME_SLOT) * 100))
    grade, label = (("🟢", "Excellent") if pct >= 99.5 else ("🟢", "Good") if pct >= 98 else
                    ("🟡", "Fair") if pct >= 90 else ("🔴", "Poor"))
    return {"pct": round(pct, 1), "grade": grade, "label": label, "down_secs": int(max(0, secs - len(slots) * UPTIME_SLOT)),
            "days": per_day}


def uptime_bar(per_day):
    blocks = "▁▂▃▄▅▆▇█"
    return "".join("·" if p is None else blocks[min(7, int(p / 100 * 8 - 1e-9))] for p in per_day)


def uptime_block(dep_id, start_time):
    u = bot_uptime(dep_id, start_time)
    if not u:
        return "\n\n*🩺 HEALTH (7 days)*\n📶 Uptime: _collecting data (needs 1 hour)_"
    dn = u["down_secs"]
    down = f"{dn // 3600}h {dn % 3600 // 60}m" if dn >= 3600 else f"{dn // 60}m"
    return (f"\n\n*🩺 HEALTH (7 days)*\n📶 Uptime: `{u['pct']:g}%` {u['grade']} {u['label']}\n"
            f"⏳ Estimated downtime: `{down}`\n📅 Per day: `{uptime_bar(u['days'])}`")


# ---------------------------------------------------------------- UPGRADE 7: notification preferences
_P2_DDL.append("CREATE TABLE IF NOT EXISTS user_notif (user_id INTEGER PRIMARY KEY, expiry INTEGER DEFAULT 1, "
               "crash INTEGER DEFAULT 1, usage INTEGER DEFAULT 1)")
# category -> {mode: (label, explanation)}; mode 1 is the default and sends everything
NOTIF_CATS = {
    "expiry": ("⏰ Expiry reminders", {1: ("All (3 days, 1 day, 1 hour)", "You're warned 3 days, 1 day and 1 hour before a bot expires."),
                                       2: ("Last day only", "Only the 1-day and 1-hour warnings."),
                                       0: ("Off", "No expiry reminders. Bots stop at expiry without warning.")}),
    "crash": ("💥 Crash alerts", {1: ("All", "Every crash, including ones that fixed themselves."),
                                  2: ("Only when it can't recover", "Silent when a bot restarts by itself; you hear about the ones that stay down."),
                                  0: ("Off", "No crash alerts at all.")}),
    "usage": ("📊 Usage warnings", {1: ("On", "Warnings about CPU hogging and going over your resource share."),
                                    0: ("Off", "No usage warnings (a bot can still be stopped if the limit is ignored).")}),
}


def notif_mode(uid, cat):
    try:
        r = _q(f"SELECT {cat} FROM user_notif WHERE user_id = ?", (uid,), fetch='one') if cat in NOTIF_CATS else None
        return 1 if r is None or r[0] is None else int(r[0])
    except Exception:
        return 1


def notif_allows(uid, cat, level=None):
    """Should a `cat` notification (of importance `level`) be sent to this user?
    expiry level = hours-stage (72/24/1); crash level = 'recovered' | 'failed'; usage has no level."""
    m = notif_mode(uid, cat)
    if m == 0:
        return False
    if cat == "expiry":
        return m == 1 or (level is not None and level <= 24)
    if cat == "crash":
        return m == 1 or level == "failed"
    return True


def notif_set(uid, cat, mode):
    _q("INSERT OR IGNORE INTO user_notif (user_id) VALUES (?)", (uid,))
    _q(f"UPDATE user_notif SET {cat} = ? WHERE user_id = ?", (int(mode), uid))


def _nt_screen(uid):
    lines = ["*🔔 NOTIFICATIONS*", "", "Choose which reminders the bot sends you. Tap one to change it.", ""]
    rows = []
    off = []
    for cat, (title, modes) in NOTIF_CATS.items():
        m = notif_mode(uid, cat)
        label, expl = modes[m]
        lines.append(f"{title}: *{label}*\n_{expl}_\n")
        rows.append([_fp_btn(f"{title}: {label}", f"p2_nt_{cat}")])
        if m == 0:
            off.append(title)
    lines.append("Security and payment messages (PIN changes, purchases, bans) are always sent.")
    if off:
        lines.append("\n⚠️ Off: " + ", ".join(off))
    rows.append([_fp_btn("🔕 Turn all off", "p2_nt_all0"), _fp_btn("🔔 Restore all", "p2_nt_all1")])
    rows.append([_fp_btn("🔙 More", "fp_more")])
    return "\n".join(lines), _fp_kb(*rows)


def _p2_r_nt(callback, data, uid, chat_id, mid):
    if data == "p2_nt_all0" or data == "p2_nt_all1":
        for cat in NOTIF_CATS:
            notif_set(uid, cat, 0 if data == "p2_nt_all0" else 1)
    else:
        m = re.match(r"^p2_nt_(expiry|crash|usage)$", data)
        if m:
            cat = m.group(1)
            order = list(NOTIF_CATS[cat][1].keys())             # e.g. [1, 2, 0]: cycles All → limited → Off → All
            notif_set(uid, cat, order[(order.index(notif_mode(uid, cat)) + 1) % len(order)])
        elif data != "p2_nt":
            return False
    txt, kb = _nt_screen(uid)
    edit_message(chat_id, mid, txt, kb)
    return True


_P2_ROUTES.append(("p2_nt", _p2_r_nt))
_P2_MORE_ROWS.append([_fp_btn("🔔 Notifications", "p2_nt")])


# ---------------------------------------------------------------- UPGRADE 9: coin top-up bonus tiers
_P2_DDL.append("CREATE TABLE IF NOT EXISTS coin_bonus (min_coins INTEGER PRIMARY KEY, pct INTEGER)")
BONUS_SUGGESTED = ((500, 5), (1000, 10), (2500, 15))


def bonus_enabled():
    return kv_get("bonus_on", "1") != "0"


def bonus_tiers():
    return [(r[0], r[1]) for r in (_q("SELECT min_coins, pct FROM coin_bonus ORDER BY min_coins", fetch='all') or [])]


def bonus_for(coins):
    """Extra coins for buying a `coins` pack: the highest tier whose minimum the pack reaches."""
    if not bonus_enabled():
        return 0
    best = 0
    for mn, pct in bonus_tiers():
        if coins >= mn:
            best = pct
    return int(coins * best // 100)


def _bn_screen():
    tiers = bonus_tiers()
    on = bonus_enabled()
    lines = ["*🎁 TOP-UP BONUSES*", "",
             f"Status: *{'ON' if on else 'OFF'}* — extra coins when a user buys a bigger coin pack (Paystack / Flutterwave / USDT).", ""]
    rows = []
    if tiers:
        for mn, pct in tiers:
            lines.append(f"• Buy `{mn}`+ coins → *+{pct}%*  (e.g. {mn} → {mn + mn * pct // 100})")
            rows.append([_fp_btn(f"🗑 Remove {mn}+ (+{pct}%)", f"p2_abn_del_{mn}")])
    else:
        lines.append("_No tiers yet._")
    packs = _p2_packs()
    if packs and tiers:
        lines += ["", "*Your current packs:*"] + [f"• {c} 🪙 → {'+%d bonus' % bonus_for(c) if bonus_for(c) else 'no bonus'}" for c, _ in packs]
    rows.insert(0, [_fp_btn("➕ Add tier", "p2_abn_add"), _fp_btn("🔴 Turn OFF" if on else "🟢 Turn ON", "p2_abn_tog")])
    if not tiers:
        rows.insert(1, [_fp_btn("✨ Load suggested (500→5%, 1000→10%, 2500→15%)", "p2_abn_sug")])
    rows.append([_fp_btn("🔙 Admin Panel", "admin_panel")])
    return "\n".join(lines), _fp_kb(*rows)


def _p2_r_abn(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    if data == "p2_abn_tog":
        kv_set("bonus_on", "0" if bonus_enabled() else "1")
    elif data == "p2_abn_sug":
        for mn, pct in BONUS_SUGGESTED:
            _q("INSERT OR REPLACE INTO coin_bonus (min_coins, pct) VALUES (?, ?)", (mn, pct))
        audit(uid, "bonus_tiers", "suggested")
    elif data == "p2_abn_add":
        set_user_step(uid, 'fp_p2bn')
        edit_message(chat_id, mid, "➕ *Add a bonus tier*\n\nSend: `minimum_coins percent`\nExample: `500 10` → anyone buying a pack of 500+ coins gets 10% extra (500 → 550).\n"
                     "Sending a minimum that already exists replaces it.", _fp_kb([_fp_btn("❌ Cancel", "p2_abn")]))
        return True
    else:
        m = re.match(r"^p2_abn_del_(\d+)$", data)
        if m:
            _q("DELETE FROM coin_bonus WHERE min_coins = ?", (int(m.group(1)),))
            audit(uid, "bonus_tiers", f"del {m.group(1)}")
        elif data != "p2_abn":
            return False
    set_user_step(uid, None)
    txt, kb = _bn_screen()
    edit_message(chat_id, mid, txt, kb)
    return True


def _p2_text_bn(message, user_step, uid, chat_id, text):
    if not is_admin(uid):
        return True
    m = re.fullmatch(r"\s*(\d{1,7})\s+(\d{1,3})\s*%?\s*", text or "")
    if not m or not (1 <= int(m.group(2)) <= 100) or int(m.group(1)) < 1:
        set_user_step(uid, 'fp_p2bn')
        send_message(chat_id, "❌ Send two numbers, e.g. `500 10` (minimum coins, then percent 1-100).", _fp_kb([_fp_btn("❌ Cancel", "p2_abn")]))
        return True
    mn, pct = int(m.group(1)), int(m.group(2))
    _q("INSERT OR REPLACE INTO coin_bonus (min_coins, pct) VALUES (?, ?)", (mn, pct))
    audit(uid, "bonus_tiers", f"{mn}:{pct}")
    txt, kb = _bn_screen()
    send_message(chat_id, f"✅ Saved: `{mn}`+ coins → +{pct}%\n\n" + txt, kb)
    return True


_P2_TEXT['fp_p2bn'] = _p2_text_bn
_P2_ROUTES.append(("p2_abn", _p2_r_abn))
_P2_ADMIN_ROWS.append([_fp_btn("🎁 Top-up bonuses", "p2_abn")])


# ---------------------------------------------------------------- UPGRADE 10: subscription pause
PAUSE_MAX_DAYS = int(os.environ.get("PAUSE_MAX_DAYS", "14"))                 # longest single pause (auto-resumes after)
PAUSE_MAX_TOTAL_DAYS = int(os.environ.get("PAUSE_MAX_TOTAL_DAYS", "30"))     # paused days allowed per bot per rolling year
PAUSE_COOLDOWN_DAYS = int(os.environ.get("PAUSE_COOLDOWN_DAYS", "7"))        # gap required between pauses
PAUSE_MIN_LEFT_DAYS = int(os.environ.get("PAUSE_MIN_LEFT_DAYS", "2"))        # plan must have this much time left
_P2_DDL.append("CREATE TABLE IF NOT EXISTS plan_pauses (id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id INTEGER, user_id INTEGER, "
               "paused_at TEXT, resume_by TEXT, remaining_secs INTEGER, resumed_at TEXT, state TEXT DEFAULT 'paused')")
_P2_DDL.append("CREATE INDEX IF NOT EXISTS idx_plan_pauses_dep ON plan_pauses (deployment_id, state)")
_PAUSE_FROZEN = 2                    # deployments.is_paused value for a user pause (1 = premium lapsed, handled elsewhere)


def pause_row(dep_id):
    r = _q("SELECT id, paused_at, resume_by, remaining_secs FROM plan_pauses WHERE deployment_id = ? AND state = 'paused' "
           "ORDER BY id DESC LIMIT 1", (dep_id,), fetch='one')
    return None if not r else {"id": r[0], "paused_at": r[1], "resume_by": r[2], "remaining": r[3]}


def _pause_used_days(dep_id, now):
    used = 0.0
    for pa, ra in (_q("SELECT paused_at, COALESCE(resumed_at, ?) FROM plan_pauses WHERE deployment_id = ? AND paused_at >= ?",
                      (now.isoformat(), dep_id, (now - timedelta(days=365)).isoformat()), fetch='all') or []):
        try:
            used += (datetime.fromisoformat(ra) - datetime.fromisoformat(pa)).total_seconds() / 86400
        except Exception:
            pass
    return used


def pause_check(dep_id, uid, now=None):
    """-> (ok, reason_or_days_allowed). When ok the second value is how many days this pause may last."""
    now = now or datetime.now()
    r = _q("SELECT user_id, status, is_free, plan, payment_method, expire_time FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
    if not r or r[0] != uid:
        return False, "Only the owner can pause a plan."
    owner, status, is_free, plan, pm, exp = r
    if pause_row(dep_id) or (status == "paused" and True and _q("SELECT is_paused FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')[0] == _PAUSE_FROZEN):
        return False, "This plan is already paused."
    if is_free or plan not in ("monthly", "yearly"):
        return False, "Only paid monthly/yearly plans can be paused."
    if pm == "premium_free":
        return False, "This bot runs on your Premium subscription, so it can't be paused separately."
    if status not in ("active", "stopped"):
        return False, f"A {status} bot can't be paused."
    try:
        left = datetime.fromisoformat(exp) - now
    except Exception:
        return False, "This plan has no valid expiry."
    if left < timedelta(days=PAUSE_MIN_LEFT_DAYS):
        return False, f"Less than {PAUSE_MIN_LEFT_DAYS} days are left, so it can't be paused. Renew instead."
    last = _q("SELECT resumed_at FROM plan_pauses WHERE deployment_id = ? AND resumed_at IS NOT NULL ORDER BY id DESC LIMIT 1", (dep_id,), fetch='one')
    if last and last[0]:
        wait = datetime.fromisoformat(last[0]) + timedelta(days=PAUSE_COOLDOWN_DAYS) - now
        if wait > timedelta(0):
            return False, f"You can pause again in {max(1, int(wait.total_seconds() // 86400) + 1)} day(s)."
    allowed = min(PAUSE_MAX_DAYS, int(PAUSE_MAX_TOTAL_DAYS - _pause_used_days(dep_id, now)))
    if allowed < 1:
        return False, f"This bot has used its {PAUSE_MAX_TOTAL_DAYS} paused days for the year."
    return True, allowed


def pause_plan(dep_id, uid, now=None):
    """Freeze the remaining time and stop the bot. -> (ok, msg)"""
    now = now or datetime.now()
    ok, info = pause_check(dep_id, uid, now)
    if not ok:
        return False, info
    exp = datetime.fromisoformat(_q("SELECT expire_time FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')[0])
    remaining = int((exp - now).total_seconds())
    try:
        stop_deployment(dep_id)
    except Exception as e:
        print(f"⚠️ pause stop #{dep_id}: {e}")
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("UPDATE deployments SET status='paused', is_paused=?, proc_pid=NULL, expire_time=? "
                           "WHERE deployment_id=? AND user_id=? AND status IN ('active','stopped') AND is_paused IN (0)",
                           (_PAUSE_FROZEN, (now + timedelta(days=3650)).isoformat(), dep_id, uid))
        conn.commit()
        if cur.rowcount != 1:
            return False, "This plan can't be paused right now."
    finally:
        conn.close()
    resume_by = now + timedelta(days=info)
    _q("INSERT INTO plan_pauses (deployment_id, user_id, paused_at, resume_by, remaining_secs) VALUES (?,?,?,?,?)",
       (dep_id, uid, now.isoformat(), resume_by.isoformat(), remaining))
    audit(uid, "plan_pause", f"{dep_id}: {remaining // 86400}d left frozen, resume by {resume_by.date()}")
    try:
        with deployment_lock:
            active_deployments.pop(dep_id, None)
    except Exception:
        pass
    return True, (f"⏸ *Plan paused.* The bot is stopped and your remaining *{remaining // 86400}d {remaining % 86400 // 3600}h* is frozen.\n"
                  f"It resumes automatically on `{resume_by.strftime('%Y-%m-%d')}` (or whenever you tap Resume).")


def resume_plan(dep_id, auto=False, now=None):
    """Unfreeze: expiry = now + the saved remaining time, then start the bot. -> (ok, msg)"""
    now = now or datetime.now()
    pr = pause_row(dep_id)
    if not pr:
        return False, "This plan isn't paused."
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    try:
        cur = conn.execute("UPDATE plan_pauses SET state='done', resumed_at=? WHERE id=? AND state='paused'", (now.isoformat(), pr["id"]))
        conn.commit()
        if cur.rowcount != 1:
            return False, "Already resumed."
        new_exp = now + timedelta(seconds=int(pr["remaining"]))
        conn.execute("UPDATE deployments SET status='stopped', is_paused=0, proc_pid=NULL, expire_time=? WHERE deployment_id=?",
                     (new_exp.isoformat(), dep_id))
        conn.commit()
    finally:
        conn.close()
    row = _q("SELECT user_id, file_name FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
    started = False
    try:
        if row and not dep_files_ok(row[0], dep_id, row[1]) and FILES_REPO_ENABLED:
            f = get_deploy_folder(row[0], dep_id)
            _files_restore_for_relaunch(row[0], dep_id, f, f / row[1])
        _fp_restart(dep_id, "resume")
        started = True
    except Exception as e:
        print(f"⚠️ resume start #{dep_id}: {e}")
    audit(row[0] if row else 0, "plan_resume", f"{dep_id} auto={auto} expires {new_exp.date()}")
    return True, (f"▶️ *Plan resumed*{' automatically' if auto else ''}. Your plan now expires on `{new_exp.strftime('%Y-%m-%d')}`."
                  + (" The bot is starting." if started else " Start the bot from its page."))


def pause_view_block(dep_id):
    pr = pause_row(dep_id)
    if not pr:
        return None
    rem = int(pr["remaining"])
    return (f"\n\n⏸ *Plan frozen* — `{rem // 86400}d {rem % 86400 // 3600}h` of your plan are saved.\n"
            f"Resumes automatically on `{pr['resume_by'][:10]}`.")


def _p2_r_pz(callback, data, uid, chat_id, mid):
    m = re.match(r"^p2_pz(go|res)?_(\d+)$", data)
    if not m:
        return False
    kind, dep_id = m.group(1), int(m.group(2))
    back = _fp_kb([_fp_btn("📦 Open bot", f"view_deploy_{dep_id}")])
    if kind == "go":
        ok_, msg = pause_plan(dep_id, uid)
        edit_message(chat_id, mid, msg if ok_ else f"❌ {msg}", _fp_kb([_fp_btn("▶️ Resume now", f"p2_pzres_{dep_id}")] if ok_ else [],
                                                                          [_fp_btn("📦 Open bot", f"view_deploy_{dep_id}")]))
        return True
    if kind == "res":
        d = _q("SELECT user_id FROM deployments WHERE deployment_id = ?", (dep_id,), fetch='one')
        if not d or d[0] != uid:
            return True
        ok_, msg = resume_plan(dep_id)
        edit_message(chat_id, mid, msg if ok_ else f"❌ {msg}", back)
        return True
    ok_, info = pause_check(dep_id, uid)
    if not ok_:
        edit_message(chat_id, mid, f"⏸ *Pause plan*\n\n❌ {info}", back)
        return True
    edit_message(chat_id, mid,
                 f"⏸ *Pause plan for bot #{dep_id}?*\n\n• The bot is *stopped* while paused.\n• Your remaining plan time is *frozen* and nothing is lost.\n"
                 f"• You can pause for up to *{info} days*; it resumes by itself after that (or tap Resume any time).\n"
                 f"• Limits: {PAUSE_MAX_TOTAL_DAYS} paused days per bot per year, and {PAUSE_COOLDOWN_DAYS} days between pauses.",
                 _fp_kb([_fp_btn("⏸ Yes, pause", f"p2_pzgo_{dep_id}")], [_fp_btn("🔙 Cancel", f"view_deploy_{dep_id}")]))
    return True


def p2_pause_monitor():
    sleep(200)
    print("✅ Plan-pause monitor started")
    while True:
        try:
            pause_monitor_tick()
        except Exception as e:
            print(f"⚠️ pause monitor: {e}")
        sleep(1800)


def pause_monitor_tick(now=None):
    now = now or datetime.now()
    n = 0
    for dep_id, uid, resume_by in (_q("SELECT deployment_id, user_id, resume_by FROM plan_pauses WHERE state = 'paused'", fetch='all') or []):
        try:
            rb = datetime.fromisoformat(resume_by)
        except Exception:
            rb = now
        if rb <= now:
            ok_, msg = resume_plan(dep_id, auto=True, now=now)
            if ok_:
                n += 1
                send_message(uid, f"⏰ Bot #{dep_id}: your pause limit was reached.\n\n{msg}", _fp_kb([_fp_btn("📦 Open bot", f"view_deploy_{dep_id}")]))
        elif rb - now <= timedelta(hours=24) and not kv_get(f"pzwarn_{dep_id}_{resume_by}"):
            kv_set(f"pzwarn_{dep_id}_{resume_by}", "1")
            send_message(uid, f"⏸ Bot #{dep_id} resumes automatically in under 24 hours.", _fp_kb([_fp_btn("▶️ Resume now", f"p2_pzres_{dep_id}")]))
    return n


_P2_ROUTES.append(("p2_pz", _p2_r_pz))
_pz_orig_restart = restart_deployment


def restart_deployment(deployment_id, user_id, chat_id, *a, **k):       # noqa: F811 - a paused plan must not be restarted by hand
    if pause_row(deployment_id):
        send_message(chat_id, "⏸ This plan is paused. Resume it first to start the bot.",
                     _fp_kb([_fp_btn("▶️ Resume now", f"p2_pzres_{deployment_id}")]))
        return False
    return _pz_orig_restart(deployment_id, user_id, chat_id, *a, **k)


# ---------------------------------------------------------------- UPGRADE 11: admin revenue report (+ CSV)
REV_DAYS = 14
REV_WEEKS = 8
_PROV_LABEL = {"paystack": "Paystack", "flutterwave": "Flutterwave", "usdt": "USDT"}


def _rev_orders(since_admin=None):
    """Paid orders with paid_at converted to the admin's clock. -> [dict]"""
    rows = _q("SELECT ref, user_id, provider, coins, COALESCE(bonus,0), amount_minor, currency, paid_at FROM payment_orders "
              "WHERE status = 'paid' AND paid_at IS NOT NULL ORDER BY paid_at", fetch='all') or []
    out = []
    for ref, uid, prov, coins, bonus, minor, cur, pa in rows:
        try:
            at = sb_server_to_admin(datetime.fromisoformat(pa))
        except Exception:
            continue
        if since_admin and at < since_admin:
            continue
        out.append({"ref": ref, "uid": uid, "provider": prov, "coins": int(coins or 0), "bonus": int(bonus or 0),
                    "amount": (minor or 0) / 100.0, "currency": cur or "", "at": at})
    return out


def _rev_bucket_key(at, mode):
    d = at.date()
    return d if mode == "d" else d - timedelta(days=d.weekday())


def rev_report(mode="d"):
    """-> {'rows': [(bucket_date, provider, orders, coins, bonus, amount, currency)], 'buckets': [...], 'orders': n, ...}"""
    now_a = sb_server_to_admin(datetime.now())
    today = now_a.date()
    if mode == "d":
        first = today - timedelta(days=REV_DAYS - 1)
        buckets = [first + timedelta(days=i) for i in range(REV_DAYS)]
    else:
        monday = today - timedelta(days=today.weekday())
        buckets = [monday - timedelta(weeks=REV_WEEKS - 1 - i) for i in range(REV_WEEKS)]
    orders = _rev_orders(datetime.combine(buckets[0], datetime.min.time()))
    agg = {}
    for o in orders:
        k = (_rev_bucket_key(o["at"], mode), o["provider"], o["currency"])
        a = agg.setdefault(k, [0, 0, 0, 0.0])
        a[0] += 1; a[1] += o["coins"]; a[2] += o["bonus"]; a[3] += o["amount"]
    rows = [(b, p, c, *v) for (b, p, c), v in sorted(agg.items(), key=lambda kv: (kv[0][0], kv[0][1]))]
    return {"mode": mode, "buckets": buckets, "rows": rows, "today": today}


def _fmt_money(v, cur):
    return f"{v:,.2f}".rstrip("0").rstrip(".") + (f" {cur}" if cur else "")


def rev_text(mode="d", detail=True):
    r = rev_report(mode)
    title = f"Daily — last {REV_DAYS} days" if mode == "d" else f"Weekly — last {REV_WEEKS} weeks"
    lines = [f"*📈 REVENUE — {title}*", ""]
    by_b = {}
    for b, p, cur, n, coins, bonus, amt in r["rows"]:
        by_b.setdefault(b, []).append((p, cur, n, coins, bonus, amt))
    tot = {}
    for b in reversed(r["buckets"]):
        items = by_b.get(b, [])
        label = b.strftime("%a %d %b") if mode == "d" else f"Week of {b.strftime('%d %b')}"
        if not items:
            lines.append(f"`{label}` — no sales")
            continue
        n = sum(i[2] for i in items); c = sum(i[3] for i in items); bo = sum(i[4] for i in items)
        money = {}
        for p, cur, nn, cc, bb, amt in items:
            money[cur] = money.get(cur, 0) + amt
        lines.append(f"`{label}` — *{n}* order(s) · *{c:,}*🪙" + (f" (+{bo:,} bonus)" if bo else "") + " · " + ", ".join(_fmt_money(v, k) for k, v in money.items()))
        for p, cur, nn, cc, bb, amt in items:
            if detail:
                lines.append(f"   ↳ {_PROV_LABEL.get(p, p)}: {nn} · {cc:,}🪙 · {_fmt_money(amt, cur)}")
            t = tot.setdefault(p, [0, 0, 0, {}]); t[0] += nn; t[1] += cc; t[2] += bb; t[3][cur] = t[3].get(cur, 0) + amt
    lines += ["", "*Total in this view*"]
    if tot:
        for p, (n, c, bo, money) in sorted(tot.items()):
            lines.append(f"• {_PROV_LABEL.get(p, p)}: {n} order(s) · {c:,}🪙" + (f" (+{bo:,} bonus)" if bo else "") + " · " + ", ".join(_fmt_money(v, k) for k, v in money.items()))
    else:
        lines.append("_No paid orders in this period._")
    free = _q("SELECT COALESCE(SUM(amount),0) FROM coin_transactions WHERE amount > 0 AND transaction_type IN ('admin_add','redeem','referral_reward') AND timestamp >= ?",
              ((datetime.now() - timedelta(days=REV_DAYS if mode == 'd' else REV_WEEKS * 7)).isoformat(),), fetch='one')
    lines.append(f"\n🎁 Free coins issued in the same window (admin / redeem / referral): `{int(free[0]):,}`")
    text = "\n".join(lines)
    return text if (len(text) <= 3900 or not detail) else rev_text(mode, detail=False)


def rev_csv(kind="d"):
    """-> (filename, csv_text). kind: 'd' | 'w' (per period & provider) | 'all' (every paid order)."""
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "all":
        w.writerow(["ref", "user_id", "provider", "coins", "bonus_coins", "total_credited", "amount", "currency", "paid_at"])
        for o in _rev_orders():
            w.writerow([o["ref"], o["uid"], o["provider"], o["coins"], o["bonus"], o["coins"] + o["bonus"], f"{o['amount']:.2f}", o["currency"], o["at"].isoformat(timespec="seconds")])
        return "orders_all.csv", buf.getvalue()
    r = rev_report(kind)
    w.writerow(["period_start", "period", "provider", "orders", "coins_sold", "bonus_coins", "total_credited", "amount", "currency"])
    for b, p, cur, n, coins, bonus, amt in r["rows"]:
        w.writerow([b.isoformat(), "day" if kind == "d" else "week", p, n, coins, bonus, coins + bonus, f"{amt:.2f}", cur])
    return ("revenue_daily.csv" if kind == "d" else "revenue_weekly.csv"), buf.getvalue()


def _rev_kb(mode):
    return _fp_kb([_fp_btn(("✅ " if mode == "d" else "") + "📅 Daily", "p2_rev"), _fp_btn(("✅ " if mode == "w" else "") + "🗓 Weekly", "p2_rev_w")],
                  [_fp_btn("⬇️ CSV (this view)", f"p2_revcsv_{mode}"), _fp_btn("⬇️ CSV (all orders)", "p2_revcsv_all")],
                  [_fp_btn("🔙 Admin Panel", "admin_panel")])


def _p2_r_rev(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    m = re.match(r"^p2_revcsv_(d|w|all)$", data)
    if m:
        name, text = rev_csv(m.group(1))
        path = Path(tempfile.gettempdir()) / f"{secrets.token_hex(4)}_{name}"
        try:
            path.write_text(text, encoding="utf-8")
            send_document(chat_id, str(path), caption=f"📈 {name} — {text.count(chr(10)) - 1} row(s)", filename=name)
            audit(uid, "revenue_csv", name)
        finally:
            path.unlink(missing_ok=True)
        return True
    if data in ("p2_rev", "p2_rev_w"):
        mode = "w" if data == "p2_rev_w" else "d"
        edit_message(chat_id, mid, rev_text(mode), _rev_kb(mode))
        return True
    return False


_P2_ROUTES.append(("p2_rev", _p2_r_rev))
_P2_ADMIN_ROWS.append([_fp_btn("📈 Revenue report", "p2_rev")])


# ---------------------------------------------------------------- UPGRADE 12: referral leaderboard (monthly) + prizes
_P2_DDL.append("CREATE TABLE IF NOT EXISTS lb_awards (month TEXT, rank INTEGER, user_id INTEGER, coins INTEGER, awarded_at TEXT, "
               "PRIMARY KEY (month, rank))")
LB_TOP = 10
LB_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _lb_month(dt):
    return dt.strftime("%Y-%m")


def lb_prizes():
    try:
        v = [max(0, int(x)) for x in (kv_get("lb_prizes", "") or "").split(",") if x.strip() != ""]
    except Exception:
        v = []
    return v[:3]


def lb_standings(month_key=None):
    """Verified referrals per referrer in an admin-clock month. -> [(user_id, count)] best first (banned users excluded)."""
    month_key = month_key or _lb_month(sb_server_to_admin(datetime.now()))
    counts, last = {}, {}
    for rid, ca in (_q("SELECT referrer_id, created_at FROM referrals WHERE reward_given = 1", fetch='all') or []):
        try:
            at = sb_server_to_admin(datetime.fromisoformat(ca))
        except Exception:
            continue
        if _lb_month(at) != month_key:
            continue
        counts[rid] = counts.get(rid, 0) + 1
        last[rid] = max(last.get(rid, at), at)
    ranked = sorted(((u, n) for u, n in counts.items() if not is_banned(u)), key=lambda x: (-x[1], last[x[0]], x[0]))
    return ranked


def _lb_name(uid, viewer=None):
    if uid == viewer:
        return "*You*"
    r = _q("SELECT first_name FROM users WHERE user_id = ?", (uid,), fetch='one')
    n = (r[0] if r and r[0] else "User")
    return _tk_esc(n[:2]) + "•••"


def _month_label(key):
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    except Exception:
        return key


def _lb_prize_line():
    pz = lb_prizes()
    if not pz or not any(pz) or kv_get("lb_on", "1") == "0":
        return ""
    line = "🎁 *Prizes:* " + "  ".join(f"{LB_MEDALS[i + 1]} {p}🪙" for i, p in enumerate(pz) if p)
    note = kv_get("lb_note", "")
    return line + (f"\n_{_tk_esc(note)}_" if note else "")


def lb_user_text(uid):
    mk = _lb_month(sb_server_to_admin(datetime.now()))
    st = lb_standings(mk)
    lines = [f"*🏆 REFERRAL LEADERBOARD — {_month_label(mk)}*", ""]
    pl = _lb_prize_line()
    if pl:
        lines += [pl, ""]
    if not st:
        lines.append("_No verified referrals yet this month — be the first!_")
    for i, (u, n) in enumerate(st[:LB_TOP], 1):
        lines.append(f"{LB_MEDALS.get(i, f'{i}.')} {_lb_name(u, uid)} — *{n}*")
    mine = next(((i, n) for i, (u, n) in enumerate(st, 1) if u == uid), None)
    lines.append("")
    if mine:
        lines.append(f"Your rank: *#{mine[0]}* with *{mine[1]}* referral(s)" + ("" if mine[0] <= LB_TOP else ""))
    else:
        lines.append("You have no verified referrals this month yet.")
    now_a = sb_server_to_admin(datetime.now())
    nxt = (now_a.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    left = nxt - now_a
    lines.append(f"\n⏳ Resets in {left.days}d {left.seconds // 3600}h. Only friends who join *and verify* count.")
    return "\n".join(lines)


def lb_award(month_key, now=None):
    """Pay the configured prizes for a finished month — each rank at most once. -> [(rank, uid, coins)]"""
    pz = lb_prizes()
    st = lb_standings(month_key)
    paid = []
    for rank, (uid, n) in enumerate(st[:len(pz)], 1):
        coins = pz[rank - 1]
        if coins <= 0:
            continue
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        try:
            cur = conn.execute("INSERT OR IGNORE INTO lb_awards (month, rank, user_id, coins, awarded_at) VALUES (?,?,?,?,?)",
                               (month_key, rank, uid, coins, _now()))
            conn.commit()
            claimed = cur.rowcount == 1
        finally:
            conn.close()
        if not claimed:
            continue
        update_user_coins(uid, coins, "leaderboard_prize", f"lb_{month_key}_{rank}")
        audit("system", "lb_prize", uid, f"{month_key} #{rank} +{coins}")
        paid.append((rank, uid, coins))
        try:
            send_message(uid, f"🏆 *You placed #{rank} on the {_month_label(month_key)} referral leaderboard!*\n\n"
                              f"{n} verified referral(s) — *+{coins} 🪙* added to your balance."
                              + (f"\n\n_{_tk_esc(kv_get('lb_note', ''))}_" if kv_get("lb_note", "") else ""),
                         _fp_kb([_fp_btn("🏆 Leaderboard", "p2_lb")]))
        except Exception:
            pass
    return paid


def lb_monthly_tick(now=None):
    """Once the admin-clock month has rolled over, pay (or skip) last month's prizes. -> paid list or None"""
    now_a = sb_server_to_admin(now or datetime.now())
    prev = _lb_month(now_a.replace(day=1) - timedelta(days=1))
    if kv_get(f"lb_done_{prev}"):
        return None
    kv_set(f"lb_done_{prev}", "1")
    if kv_get("lb_on", "1") == "0" or not any(lb_prizes()):
        kv_set(f"lb_done_{prev}", "skipped")
        return None
    paid = lb_award(prev)
    if paid:
        txt = f"🏆 Leaderboard prizes for {_month_label(prev)} paid:\n" + "\n".join(f"#{r} `{u}` +{c}🪙" for r, u, c in paid)
        for a in list(ADMIN_IDS):
            try:
                send_message(a, txt)
            except Exception:
                pass
    return paid


def p2_lb_monitor():
    sleep(240)
    print("✅ Leaderboard monitor started")
    while True:
        try:
            lb_monthly_tick()
        except Exception as e:
            print(f"⚠️ leaderboard monitor: {e}")
        sleep(3600)


def _alb_screen():
    mk = _lb_month(sb_server_to_admin(datetime.now()))
    st = lb_standings(mk)
    pz = lb_prizes()
    on = kv_get("lb_on", "1") != "0"
    lines = [f"*🏆 REFERRAL LEADERBOARD — admin*", "",
             f"Month: *{_month_label(mk)}* · prizes: *{'ON' if on else 'OFF'}*",
             "Prizes: " + (", ".join(f"{LB_MEDALS[i + 1]} {p}🪙" for i, p in enumerate(pz)) if pz else "_none set_"),
             "Note: " + (f"_{_tk_esc(kv_get('lb_note', ''))}_" if kv_get("lb_note", "") else "_none_"), "", "*Current top 10*"]
    for i, (u, n) in enumerate(st[:LB_TOP], 1):
        lines.append(f"{LB_MEDALS.get(i, f'{i}.')} `{u}` — {n}")
    if not st:
        lines.append("_No verified referrals yet._")
    prev = _lb_month(sb_server_to_admin(datetime.now()).replace(day=1) - timedelta(days=1))
    done = kv_get(f"lb_done_{prev}")
    lines.append(f"\nLast month ({_month_label(prev)}): " + ("paid ✅" if done == "1" else "skipped (no prizes were set)" if done == "skipped" else "not processed yet"))
    return "\n".join(lines), _fp_kb([_fp_btn("💰 Set prizes", "p2_alb_pz"), _fp_btn("📝 Set note", "p2_alb_note")],
                                    [_fp_btn("🔴 Turn OFF" if on else "🟢 Turn ON", "p2_alb_tog"), _fp_btn("🏁 Pay last month now", "p2_alb_pay")],
                                    [_fp_btn("🔙 Admin Panel", "admin_panel")])


def _p2_r_alb(callback, data, uid, chat_id, mid):
    if not is_admin(uid):
        return True
    if data == "p2_alb_tog":
        kv_set("lb_on", "0" if kv_get("lb_on", "1") != "0" else "1")
    elif data == "p2_alb_pz":
        set_user_step(uid, 'fp_p2lbpz')
        edit_message(chat_id, mid, "💰 Send prize coins for ranks 1, 2, 3 separated by spaces, e.g. `300 150 50`.\n"
                     "Use `0` to skip a rank, or send just `500` to reward only the winner. Send `none` to clear.",
                     _fp_kb([_fp_btn("❌ Cancel", "p2_alb")]))
        return True
    elif data == "p2_alb_note":
        set_user_step(uid, 'fp_p2lbnote')
        edit_message(chat_id, mid, "📝 Send a short note shown with the prizes (e.g. `Plus a free month for #1!`). Send `none` to clear.",
                     _fp_kb([_fp_btn("❌ Cancel", "p2_alb")]))
        return True
    elif data == "p2_alb_pay":
        prev = _lb_month(sb_server_to_admin(datetime.now()).replace(day=1) - timedelta(days=1))
        if kv_get(f"lb_done_{prev}") == "1":
            edit_message(chat_id, mid, f"ℹ️ {_month_label(prev)} was already paid.", _fp_kb([_fp_btn("🔙 Back", "p2_alb")]))
            return True
        if not any(lb_prizes()):
            edit_message(chat_id, mid, "⚠️ Set prizes first.", _fp_kb([_fp_btn("🔙 Back", "p2_alb")]))
            return True
        kv_set(f"lb_done_{prev}", "1")
        paid = lb_award(prev)
        edit_message(chat_id, mid, f"🏁 Paid {len(paid)} prize(s) for {_month_label(prev)}." + "".join(f"\n#{r} `{u}` +{c}🪙" for r, u, c in paid),
                     _fp_kb([_fp_btn("🔙 Back", "p2_alb")]))
        return True
    elif data != "p2_alb":
        return False
    set_user_step(uid, None)
    txt, kb = _alb_screen()
    edit_message(chat_id, mid, txt, kb)
    return True


def _p2_text_lbpz(message, user_step, uid, chat_id, text):
    if not is_admin(uid):
        return True
    t = (text or "").strip().lower()
    if t == "none":
        kv_set("lb_prizes", "")
    else:
        parts = t.replace(",", " ").split()
        if not (1 <= len(parts) <= 3 and all(p.isdigit() and int(p) <= 1_000_000 for p in parts)):
            set_user_step(uid, 'fp_p2lbpz')
            send_message(chat_id, "❌ Send 1-3 whole numbers, e.g. `300 150 50`.", _fp_kb([_fp_btn("❌ Cancel", "p2_alb")]))
            return True
        kv_set("lb_prizes", ",".join(str(int(p)) for p in parts))
    audit(uid, "lb_prizes", kv_get("lb_prizes", ""))
    txt, kb = _alb_screen()
    send_message(chat_id, "✅ Saved.\n\n" + txt, kb)
    return True


def _p2_text_lbnote(message, user_step, uid, chat_id, text):
    if not is_admin(uid):
        return True
    t = (text or "").strip()
    kv_set("lb_note", "" if t.lower() == "none" else t[:200])
    txt, kb = _alb_screen()
    send_message(chat_id, "✅ Saved.\n\n" + txt, kb)
    return True


def _p2_r_lb(callback, data, uid, chat_id, mid):
    if data != "p2_lb":
        return False
    edit_message(chat_id, mid, lb_user_text(uid), _fp_kb([_fp_btn("👥 My referral link", "my_referral")], [_fp_btn("🔙 Menu", "main_menu")]))
    return True


_P2_TEXT['fp_p2lbpz'] = _p2_text_lbpz
_P2_TEXT['fp_p2lbnote'] = _p2_text_lbnote
_P2_ROUTES.append(("p2_alb", _p2_r_alb))
_P2_ROUTES.append(("p2_lb", _p2_r_lb))
_P2_ADMIN_ROWS.append([_fp_btn("🏆 Referral leaderboard", "p2_alb")])


def _p2_route(callback, data, uid, chat_id, mid):
    for prefix, fn in _P2_ROUTES:
        if data.startswith(prefix):
            if fn(callback, data, uid, chat_id, mid):
                return True
    return False


def _p2_text(message, user_step):
    step = user_step.get('step') or ''
    fn = _P2_TEXT.get(step)
    if not fn:
        return False
    chat_id, uid = message['chat']['id'], message['from']['id']
    text = (message.get('text') or '').strip()
    if text.startswith('/'):
        return False
    set_user_step(uid, None)
    return fn(message, user_step, uid, chat_id, text)


def process_health_monitor():
    """
    Runs every 120 s. Checks whether PIDs recorded as 'active' are still
    alive. If a process has died, it is automatically relaunched (unless the
    deployment has expired, is paused pending premium renewal, its files are
    missing, or it has hit the crash-loop cap) so bots stay up on their own
    instead of requiring the owner to notice and click Restart.
    """
    print("✅ Process health monitor started")
    while True:
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute("SELECT deployment_id, user_id, proc_pid FROM deployments WHERE status = 'active' AND proc_pid IS NOT NULL")
            active = c.fetchall()
            conn.close()

            for dep_id, uid, proc_pid in active:
                alive = False
                try:
                    os.kill(proc_pid, 0)   # signal 0 — just checks existence
                    alive = True
                except (ProcessLookupError, PermissionError):
                    alive = False
                except Exception:
                    alive = True  # unknown error → assume alive

                if alive:
                    continue

                with deployment_lock:
                    active_deployments.pop(dep_id, None)
                # Hand the relaunch to the queue: bots come back one at a time (so a mass
                # crash can't spike RAM), restoring files from GitHub first if they're gone.
                # If auto-restart ultimately can't recover it, the queue worker sends the
                # owner the manual "Restart Bot" prompt.
                if enqueue_relaunch(dep_id, reason="crash"):
                    print(f"💀 Dead process for deployment {dep_id} (user {uid}) — queued for auto-restart")

        except Exception as e:
            print(f"❌ Health monitor error: {e}")

        sleep(120)


# ==================== HEALTH CHECK SERVER ====================
def start_health_server():
    try:
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        class HealthHandler(BaseHTTPRequestHandler):
            def _proxy_other(self):
                if not p2_http(self, self.command):
                    _p2_send(self, 404, "not found")
            do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _proxy_other

            def do_GET(self):
                if p2_http(self, 'GET'):
                    return
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                # Return live deployment count so monitors know the service is up
                try:
                    conn = sqlite3.connect(DATABASE_FILE, timeout=5)
                    active = conn.execute(
                        "SELECT COUNT(*) FROM deployments WHERE status='active'").fetchone()[0]
                    conn.close()
                except Exception:
                    active = -1
                payload = (
                    b'{"status":"healthy","version":"2.0",'
                    b'"active_deployments":' + str(active).encode() +
                    b',"timestamp":"' + datetime.now().isoformat().encode() + b'"}'
                )
                self.wfile.write(payload)
            def do_POST(self):
                if p2_http(self, 'POST'):
                    return
                try:
                    m = re.match(r'^/gh/(\d+)/?$', self.path.split('?')[0])
                    n = int(self.headers.get('Content-Length') or 0)
                    body = self.rfile.read(min(n, 2_000_000)) if n else b''
                    if not m:
                        code, txt = 404, "not found"
                    else:
                        code, txt = handle_github_webhook(int(m.group(1)), body,
                                                          self.headers.get('X-Hub-Signature-256'),
                                                          self.headers.get('X-GitHub-Event', ''))
                except Exception as e:
                    code, txt = 500, "error"
                    print(f"⚠️ webhook: {e}")
                self.send_response(code)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(txt.encode())
            def log_message(self, format, *args):
                pass
        port = int(os.environ.get("PORT", 8080))
        ThreadingHTTPServer.daemon_threads = True
        server = ThreadingHTTPServer(('0.0.0.0', port), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="HealthServer").start()
        print(f"✅ Health check server on port {port}")
    except Exception as e:
        print(f"⚠️ Health server: {e}")


def _self_ping_thread():
    """
    Pings the bot's own health endpoint every 10 minutes so cloud platforms
    (Render free tier, Heroku eco dynos, Fly.io, etc.) never spin the service
    down for inactivity.  Without this, the host dies after ~15 min of silence
    and takes every hosted subprocess with it.
    """
    port = int(os.environ.get("PORT", 8080))

    # Try RENDER_EXTERNAL_HOSTNAME first (works even behind the load-balancer),
    # fall back to localhost for other platforms.
    ext_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    if ext_host:
        urls = [f"https://{ext_host}/", f"http://localhost:{port}/"]
    else:
        urls = [f"http://localhost:{port}/"]

    # Give the health server 20 s to start before the first ping
    sleep(20)
    print(f"✅ Self-ping keep-alive started (every 10 min) → {urls[0]}")

    while True:
        for url in urls:
            try:
                req = urllib.request.Request(url,
                    headers={"User-Agent": "BotHosting-KeepAlive/2.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    _ = r.read()
                print(f"🏓 Keep-alive ping OK ({url})")
                break                 # first successful URL is enough
            except Exception as e:
                print(f"⚠️  Keep-alive ping failed ({url}): {e}")
        sleep(600)   # 10 minutes


def _hosted_bot_watchdog():
    """
    Checks every 90 seconds that every 'active' deployment's process is still
    alive.  If a hosted bot has crashed (dead PID), it is automatically
    relaunched so the user's bot stays up 24/7 without manual intervention.
    Bots that have genuinely expired are left to the expiry monitor.
    """
    sleep(60)   # let startup reconciliation finish first
    print("✅ Hosted-bot watchdog started (90 s cycle)")

    while True:
        try:
            conn = sqlite3.connect(DATABASE_FILE, timeout=10)
            c = conn.cursor()
            c.execute("""SELECT deployment_id, proc_pid, expire_time
                         FROM deployments
                         WHERE status = 'active' AND proc_pid IS NOT NULL""")
            rows = c.fetchall()
            conn.close()

            for dep_id, pid, expire_time_str in rows:
                # Skip expired deployments — the expiry monitor handles those
                if expire_time_str:
                    try:
                        if datetime.fromisoformat(expire_time_str) < datetime.now():
                            continue
                    except Exception:
                        pass

                # Check if PID is actually alive
                alive = False
                try:
                    os.kill(pid, 0)
                    alive = True
                except (ProcessLookupError, OSError):
                    alive = False
                except Exception:
                    alive = True   # unknown error — assume alive

                if not alive:
                    # Queued (and de-duplicated against the health monitor) — never launched inline.
                    if enqueue_relaunch(dep_id, reason="watchdog"):
                        print(f"⚠️  Watchdog: dep#{dep_id} PID {pid} is dead — queued for relaunch")

        except Exception as e:
            print(f"⚠️  Watchdog cycle error: {e}")

        sleep(90)

# ==================== MAIN ====================
def reconcile_deployments_on_startup():
    """
    Runs once at boot, after the (possibly GitHub-restored) database is
    loaded. Any deployment marked 'active' in the DB has a proc_pid from the
    PREVIOUS process — meaningless here, since this is a fresh interpreter
    (whether from a crash, a manual restart, or a redeploy). Without this,
    those bots would just sit in the DB as "active" while nothing is actually
    running, until someone happens to notice and click Restart. Non-expired
    ones are relaunched; ones whose files didn't survive (see
    PERSISTENT_DISK_PATH) are marked stopped with a clear explanation.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deployment_id FROM deployments WHERE status IN ('active', 'paused')")
    rows = c.fetchall()
    conn.close()

    if not rows:
        return

    # Queue them all and return immediately: the bot starts answering Telegram right away while a
    # single worker brings the deployments back one at a time (restoring files from the GitHub
    # files repo where the disk was wiped), waiting for free RAM between each one.
    print(f"🔄 Queueing {len(rows)} deployment(s) from the previous run for one-at-a-time relaunch…")
    for (dep_id,) in rows:
        enqueue_relaunch(dep_id, reason="startup", bypass_cap=True)


def setup_memory_compression():
    """
    Best-effort RAM compression via zram (compressed swap held in RAM).

    Needs Linux, root, and a kernel with the zram module. Inside most managed
    containers (Render, Heroku, Choreo, Android) this is simply not permitted,
    so every step is guarded and failure is reported once, never raised.
    Controlled by env:
      ZRAM_ENABLE=1|0        (default 1)
      ZRAM_PERCENT=50        zram device size as % of total RAM
      ZRAM_ALGO=zstd         zstd | lz4 | lzo-rle
    On hosts where this can't run, the same effect is achieved from outside
    the app (e.g. the `zram-tools` package on a VPS, or simply more RAM).
    """
    if os.environ.get("ZRAM_ENABLE", "1") != "1":
        return False
    if platform.system() != "Linux" or IS_ANDROID:
        return False
    try:
        if os.geteuid() != 0:
            print("ℹ️  zram skipped: not running as root")
            return False
    except AttributeError:
        return False

    try:
        # Already active? Don't stack a second device.
        with open('/proc/swaps') as f:
            if any(line.startswith('/dev/zram') for line in f):
                print("✅ zram already active")
                return True

        percent = max(10, min(100, int(os.environ.get("ZRAM_PERCENT", 50))))
        algo = os.environ.get("ZRAM_ALGO", "zstd")
        total_mb, _, _ = _read_proc_meminfo()
        if not total_mb:
            return False
        size_bytes = int(total_mb * 1024 * 1024 * percent / 100)

        def _sh(cmd):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return r.returncode == 0

        if not os.path.exists('/sys/block/zram0'):
            if not _sh(['modprobe', 'zram', 'num_devices=1']):
                print("ℹ️  zram skipped: kernel module unavailable")
                return False
        if not os.path.exists('/sys/block/zram0'):
            print("ℹ️  zram skipped: /sys/block/zram0 not present")
            return False

        # Device must be reset before (re)configuring
        try:
            with open('/sys/block/zram0/reset', 'w') as f:
                f.write('1')
        except Exception:
            pass

        try:
            with open('/sys/block/zram0/comp_algorithm', 'w') as f:
                f.write(algo)
        except Exception:
            pass  # unsupported algorithm → kernel default is fine
        with open('/sys/block/zram0/disksize', 'w') as f:
            f.write(str(size_bytes))

        if not _sh(['mkswap', '/dev/zram0']):
            print("ℹ️  zram skipped: mkswap failed")
            return False
        # High priority so it is preferred over any disk swap
        if not _sh(['swapon', '-p', '100', '/dev/zram0']):
            print("ℹ️  zram skipped: swapon failed")
            return False

        # zram is cheap, so let the kernel prefer it over dropping page cache
        try:
            with open('/proc/sys/vm/swappiness', 'w') as f:
                f.write('100')
            with open('/proc/sys/vm/page-cluster', 'w') as f:
                f.write('0')
        except Exception:
            pass

        print(f"✅ zram enabled: {size_bytes/1024/1024:.0f} MB ({algo}), swappiness=100")
        return True
    except Exception as e:
        print(f"ℹ️  zram setup skipped: {e}")
        return False


def _fp_install_wrappers():
    """Wrap existing functions with rollback snapshots (edits) and audit entries (admin actions)."""
    def snap_wrap(fn, dep_pos, label):
        def inner(*a, **k):
            try:
                snapshot_create(int(a[dep_pos]), label)
            except Exception:
                pass
            return fn(*a, **k)
        inner.__name__ = fn.__name__
        return inner
    g = globals()
    g['handle_edit_code_document'] = snap_wrap(g['handle_edit_code_document'], 4, "before-code-edit")
    g['handle_edit_reqs_document'] = snap_wrap(g['handle_edit_reqs_document'], 4, "before-reqs-edit")
    _ban, _unban = g['ban_user'], g['unban_user']
    def ban_a(admin_id, target_id, reason=None):
        r = _ban(admin_id, target_id, reason)
        audit(admin_id, "ban", target_id, reason or "")
        return r
    def unban_a(admin_id, target_id):
        r = _unban(admin_id, target_id)
        audit(admin_id, "unban", target_id)
        return r
    g['ban_user'], g['unban_user'] = ban_a, unban_a


_fp_install_wrappers()


def _fp_startup():
    try:
        _p2_ensure()
        _bwrap_ok()
        _harden_perms()
    except Exception as e:
        print(f"⚠️ isolation startup: {e}")


def main():
    global LAST_UPDATE_ID

    # Try to enable compressed RAM (no-op where the host doesn't allow it)
    setup_memory_compression()
    
    if (IS_RENDER or IS_HEROKU or IS_CHOREO) and not USING_PERSISTENT_DISK:
        print("=" * 70)
        print("⚠️  WARNING: no persistent disk configured (PERSISTENT_DISK_PATH unset).")
        print("   Deployment files live inside the app's own source checkout, which")
        print("   most cloud platforms rebuild from git on every redeploy. Deployed")
        print("   bots' code/packages will NOT survive a redeploy unless you attach")
        print("   a real persistent disk and point PERSISTENT_DISK_PATH at its mount.")
        print("   (The database itself is separately backed up to GitHub if configured.)")
        print("=" * 70)

    # ── Restore database from GitHub BEFORE init_db() ──────────────
    # This must happen first so we never overwrite existing data with
    # a fresh empty schema.
    github_restore_db()
    # ───────────────────────────────────────────────────────────────
    
    init_db()
    _fp_startup()

    # ── GitHub files repo (per-deployment code / requirements / .env / db) ──
    if FILES_REPO_ENABLED:
        _files_init_tables()
        threading.Thread(target=lambda: (sleep(20), adopt_lost_deployments()), daemon=True, name="AdoptLost").start()
        if _files_repo_ready(force=True):
            print(f"✅ Files repo ready: {GITHUB_REPO_OWNER}/{GITHUB_FILES_REPO_NAME} "
                  f"[branch: {GITHUB_BACKUP_BRANCH}] (private)")
        # Push live deployments + the DB when the host stops us (redeploy / scale-down)
        try:
            signal.signal(signal.SIGTERM, _graceful_shutdown_flush)
        except Exception as _se:
            print(f"ℹ️  Could not register SIGTERM flush: {_se}")
    else:
        print("ℹ️  GITHUB_FILES_REPO_NAME not set — user deployment files are NOT backed up to GitHub")

    # ── Bring back whatever was running before this process started ──
    # (queued: restores missing files from GitHub, then relaunches one bot at a time)
    reconcile_deployments_on_startup()
    # ───────────────────────────────────────────────────────────────
    
    # Start health check server for cloud platforms
    if IS_RENDER or IS_HEROKU or IS_CHOREO or os.environ.get("PUBLIC_BASE_URL"):
        start_health_server()
    
    # Start background monitors
    threading.Thread(target=deployment_expiry_monitor,  daemon=True, name="ExpiryMonitor").start()
    threading.Thread(target=process_health_monitor,     daemon=True, name="HealthMonitor").start()
    threading.Thread(target=_periodic_backup_thread,    daemon=True, name="PeriodicBackup").start()
    threading.Thread(target=_self_ping_thread,          daemon=True, name="KeepAlive").start()
    threading.Thread(target=_hosted_bot_watchdog,       daemon=True, name="BotWatchdog").start()
    threading.Thread(target=quota_monitor,              daemon=True, name="QuotaMonitor").start()
    threading.Thread(target=expiry_reminder_monitor,    daemon=True, name="ExpiryReminders").start()
    threading.Thread(target=abuse_guard_monitor,        daemon=True, name="AbuseGuard").start()
    threading.Thread(target=files_guard_monitor,        daemon=True, name="FilesGuard").start()
    threading.Thread(target=scheduled_restart_monitor,  daemon=True, name="SchedRestart").start()
    threading.Thread(target=metrics_sampler,            daemon=True, name="UsageSampler").start()
    threading.Thread(target=health_check_monitor,       daemon=True, name="HealthPing").start()
    threading.Thread(target=p2_autorenew_monitor,       daemon=True, name="AutoRenew").start()
    threading.Thread(target=p2_sched_broadcast_monitor, daemon=True, name="SchedBroadcast").start()
    threading.Thread(target=p2_health_monitor,          daemon=True, name="HostHealth").start()
    threading.Thread(target=p2_deploy_queue_worker,     daemon=True, name="DeployQueue").start()
    threading.Thread(target=p2_pause_monitor,           daemon=True, name="PlanPause").start()
    threading.Thread(target=p2_lb_monitor,              daemon=True, name="Leaderboard").start()
    if FILES_REPO_ENABLED:
        threading.Thread(target=_files_sync_thread,     daemon=True, name="FilesRepoSync").start()

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

                    def _handle_one(update=update):
                        # Each update is isolated: if handling one throws, we log
                        # it and move on instead of silently dropping that
                        # user's message with no reply and no trace.
                        try:
                            if 'callback_query' in update:
                                handle_callback(update['callback_query'])
                            elif 'pre_checkout_query' in update:
                                handle_pre_checkout_query(update['pre_checkout_query'])
                            elif 'message' in update:
                                msg = update['message']
                                if 'successful_payment' in msg:
                                    handle_successful_payment(msg)
                                else:
                                    handle_message(msg)
                        except Exception as ue:
                            print(f"❌ Error handling update {update['update_id']}: {ue}")
                            traceback.print_exc()
                            # Best-effort: let the user know something broke instead
                            # of leaving them with total silence.
                            try:
                                fail_chat_id = None
                                if 'message' in update:
                                    fail_chat_id = update['message']['chat']['id']
                                elif 'callback_query' in update:
                                    fail_chat_id = update['callback_query']['message']['chat']['id']
                                if fail_chat_id:
                                    send_message(fail_chat_id,
                                        "⚠️ Something went wrong processing that. Please try again.")
                            except Exception:
                                pass

                    # Run in a background thread — a slow operation for one
                    # user (deployment installs, GitHub downloads, broadcasts)
                    # must never block the main loop from polling and
                    # dispatching updates for every OTHER user in the
                    # meantime. Each DB call already opens its own fresh
                    # sqlite3 connection, so this is safe across threads.
                    threading.Thread(target=_handle_one, daemon=True,
                                     name=f"Update-{update['update_id']}").start()
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