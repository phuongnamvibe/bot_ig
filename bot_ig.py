import os
import telebot
import requests
import sqlite3
import re
import time
import threading
import random
from datetime import datetime, timedelta
from telebot import util
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =====================================================================
# 🛠️ CONFIGURATION & ADMINISTRATION
# =====================================================================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
bot = telebot.TeleBot(TOKEN)

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))
DB_NAME = "ig_commercial_radar.db"

# API Pool queue: Loaded from .env (comma-separated keys)
raw_keys = os.getenv("SCRAPE_API_KEY", "")
API_POOL = [k.strip() for k in raw_keys.split(",") if k.strip()]

CURRENT_API_INDEX = 0
DEFAULT_MONITOR_DAYS = 7
INTERNAL_USAGE_COUNTER = 0
IS_QUOTA_ALERT_SENT = False

def get_vietnam_time():
    """Synchronize UTC+7 timezone."""
    return datetime.utcnow() + timedelta(hours=7)

def get_current_token():
    """Retrieve the active token at the head of the queue."""
    global API_POOL, CURRENT_API_INDEX
    if API_POOL and 0 <= CURRENT_API_INDEX < len(API_POOL):
        return API_POOL[CURRENT_API_INDEX].strip()
    return ""

def fetch_remaining_quota():
    """Calculate remaining quota based on 1000 base requests."""
    global INTERNAL_USAGE_COUNTER
    remaining = 1000 - INTERNAL_USAGE_COUNTER
    if remaining < 0:
        remaining = 0
    return remaining, f"{remaining}/1000"

def switch_to_next_api_logic():
    """Remove exhausted token and rotate to the next one."""
    global CURRENT_API_INDEX, API_POOL, INTERNAL_USAGE_COUNTER
    if not API_POOL:
        return False

    try:
        API_POOL.pop(CURRENT_API_INDEX)
    except:
        pass

    INTERNAL_USAGE_COUNTER = 0
    if API_POOL and CURRENT_API_INDEX < len(API_POOL):
        try:
            bot.send_message(
                ADMIN_CHAT_ID, 
                f"🔄 **API POOL AUTO-ROTATION!**\n\n"
                f"🗑️ Removed 1 exhausted token.\n"
                f"🗂️ Remaining tokens in pool: `{len(API_POOL)}`"
            )
        except:
            pass
        return True
    return False

def format_duration(seconds):
    """Format duration in seconds into ..h ..m ..s format."""
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}h {minutes}m {secs}s"

def calculate_expire_date(days, base_time=None):
    if base_time is None:
        base_time = get_vietnam_time()
    target_date = base_time + timedelta(days=int(days))
    return target_date.strftime("%Y-%m-%d %H:%M:%S")

def get_days_only(expire_date_str):
    try:
        vn_now = get_vietnam_time()
        expire_date = datetime.strptime(expire_date_str, "%Y-%m-%d %H:%M:%S")
        if expire_date < vn_now:
            return 0
        delta = expire_date - vn_now
        days_left = delta.total_seconds() / 86400
        return int(days_left) if days_left >= 1 else 0
    except:
        return 0

def get_time_remaining_string(expire_date_str):
    try:
        vn_now = get_vietnam_time()
        expire_date = datetime.strptime(expire_date_str, "%Y-%m-%d %H:%M:%S")
        if expire_date < vn_now:
            return "Expired"
        delta = expire_date - vn_now
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        secs = delta.seconds % 60
        return f"{days} days {hours:02d}:{minutes:02d}:{secs:02d}"
    except:
        return "0 days 00:00:00"

# =====================================================================
# ⚡ INSTAGRAM GATEWAY ENGINE
# =====================================================================
def check_instagram_status_via_gateway(username, retry_count=0):
    global INTERNAL_USAGE_COUNTER, IS_QUOTA_ALERT_SENT
    active_token = get_current_token()
    if not active_token:
        return "UNKNOWN"

    target_url = f"https://www.instagram.com/{username}/"
    api_url = f"https://api.scrape.do/?token={active_token}&url={target_url}&geo=us"

    quota_val, _ = fetch_remaining_quota()
    if quota_val <= 0:
        if switch_to_next_api_logic():
            return check_instagram_status_via_gateway(username)
        raise ValueError("ALL_API_POOL_EXCEEDED")

    try:
        response = requests.get(api_url, timeout=20)
        INTERNAL_USAGE_COUNTER += 1
        # Xử lý bẫy lỗi HTTP 429 và 403
        if response.status_code in [429, 403]:
            err_text = response.text.lower()
            # Kiểm tra xem có phải thực sự hết cước tháng hay token chết không
            is_real_quota_dead = any(keyword in err_text for keyword in [
                "quota", "limit reached", "out of credit", "subscription", "expired", "inactive", "invalid token"
            ])

            if is_real_quota_dead:
                print(f"⚠️ [QUOTA EXHAUSTED] Token thực sự cạn hạn mức: {response.text}")
                if switch_to_next_api_logic():
                    return check_instagram_status_via_gateway(username)
                raise ValueError("ALL_API_POOL_EXCEEDED")
            else:
                # Chỉ bị Rate-limit / Quá tải concurrency tạm thời -> Không xóa key!
                if retry_count < 2:
                    print(f"⏳ [RATE LIMIT] Bị giới hạn tốc độ tức thời (Status {response.status_code}). Chờ 6s rồi thử lại...")
                    time.sleep(6)
                    return check_instagram_status_via_gateway(username, retry_count + 1)
                else:
                    return "UNKNOWN"

        if response.status_code in [500, 502, 504]:
            return "UNKNOWN"
        if response.status_code == 404:
            return "DIE"
        html_content = response.text
        low_html = html_content.lower()
        user_lower = username.lower()
        if f'"{user_lower}"' not in low_html and f'://www.instagram.com/{user_lower}' not in low_html:
            return "DIE"
        title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
        page_title = title_match.group(1).strip() if title_match else ""
        low_title = page_title.lower()
        if "login" in low_title or "đăng nhập" in low_title or "iniciar" in low_title or "connexion" in low_title or not page_title:
            return "UNKNOWN"
        if 'profilepage' in low_html or user_lower in low_title or "@" in page_title:
            return "LIVE"
        return "UNKNOWN"
    except Exception as e:
        if "ALL_API_POOL_EXCEEDED" in str(e) or not API_POOL:
            if not IS_QUOTA_ALERT_SENT:
                try:
                    now_str = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
                    bot.send_message(
                        ADMIN_CHAT_ID,
                        f"🚨 **ALL API TOKENS IN POOL ARE EXHAUSTED!**\n\n"
                        f"⏰ Timestamp: [{now_str} UTC+7]\n"
                        f"Please add more tokens using `/setpool` command.",
                        parse_mode="Markdown"
                    )
                    IS_QUOTA_ALERT_SENT = True
                except:
                    pass
        return "UNKNOWN"
# =====================================================================
# 💾 SQLITE DATABASE ENGINE (WITH REVIVAL DURATION TRACKING)
# =====================================================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS customer_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            target_value TEXT,
            target_type TEXT,
            last_status TEXT DEFAULT 'LIVE',
            expire_date TEXT,
            note TEXT DEFAULT "",
            created_at TEXT,
            added_at TEXT,
            revive_duration TEXT DEFAULT "",
            UNIQUE(user_id, target_value)
        )
    ''')
    for col, definition in [
        ("expire_date", "TEXT"),
        ("note", "TEXT DEFAULT ''"),
        ("created_at", "TEXT"),
        ("added_at", "TEXT"),
        ("revive_duration", "TEXT DEFAULT ''")
    ]:
        try:
            cursor.execute(f'ALTER TABLE customer_targets ADD COLUMN {col} {definition};')
        except:
            pass
    conn.commit()
    conn.close()

def add_to_customer_list(user_id, target_value, target_type="USER", current_status="LIVE", days=30, note_text=""):
    now_vn_str = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
    expire_date = calculate_expire_date(days)
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO customer_targets 
            (user_id, target_value, target_type, last_status, expire_date, created_at, added_at, note, revive_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, "")
            ON CONFLICT(user_id, target_value) DO UPDATE SET 
                last_status = ?, expire_date = ?, note = ?, added_at = ?
        ''', (user_id, target_value, target_type, current_status, expire_date, now_vn_str, now_vn_str, note_text,
              current_status, expire_date, note_text, now_vn_str))
        conn.commit()
    except Exception as e:
        print(f"Database insertion error: {e}")
    finally:
        conn.close()

def update_status_db(user_id, target_value, status, revive_duration=""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if revive_duration:
        cursor.execute('''
            UPDATE customer_targets 
            SET last_status = ?, revive_duration = ? 
            WHERE user_id = ? AND target_value = ?
        ''', (status, revive_duration, user_id, target_value))
    else:
        cursor.execute('''
            UPDATE customer_targets 
            SET last_status = ? 
            WHERE user_id = ? AND target_value = ?
        ''', (status, user_id, target_value))
    conn.commit()
    conn.close()

def delete_from_customer_list(user_id, target_value):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM customer_targets WHERE user_id = ? AND target_value = ?', (user_id, target_value))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def get_targets_by_customer(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT target_value, last_status, expire_date, note FROM customer_targets WHERE user_id = ? AND target_type = "USER"', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_single_target(user_id, target_value):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT expire_date, note, created_at, added_at, revive_duration FROM customer_targets WHERE user_id = ? AND target_value = ?', (user_id, target_value))
    row = cursor.fetchone()
    conn.close()
    return row

def get_all_targets_for_admin():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, target_value, last_status FROM customer_targets WHERE target_type = "USER"')
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_target_note(user_id, target_value, note_text):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE customer_targets SET note = ? WHERE user_id = ? AND target_value = ?', (note_text, user_id, target_value))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def update_target_time_cumulative(user_id, target_value, days):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT expire_date FROM customer_targets WHERE user_id = ? AND target_value = ?', (user_id, target_value))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    old_expire_str = row[0]
    vn_now = get_vietnam_time()
    try:
        old_expire_date = datetime.strptime(old_expire_str, "%Y-%m-%d %H:%M:%S")
        base_goc = old_expire_date if old_expire_date > vn_now else vn_now
    except:
        base_goc = vn_now
    new_expire_date = calculate_expire_date(days, base_time=base_goc)
    cursor.execute('UPDATE customer_targets SET expire_date = ? WHERE user_id = ? AND target_value = ?', (new_expire_date, user_id, target_value))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def parse_bulk_inputs(text, command_prefix):
    clean_text = text.replace(command_prefix, "", 1).strip()
    return [item.strip() for item in re.split(r'[\s,\n]+', clean_text) if item.strip()]

def extract_username_from_link(input_str):
    if "instagram.com" in input_str.lower():
        clean_url = re.sub(r'https?:\/\/(www\.)?', '', input_str, flags=re.IGNORECASE)
        match = re.search(r'instagram\.com\/([a-zA-Z0-9_\.]+)', clean_url, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return input_str.replace("@", "").replace("/", "").strip()

# =====================================================================
# 🔄 BACKGROUND RADAR MONITORING THREAD
# =====================================================================
def auto_scan_commercial_radar_v8():
    global IS_QUOTA_ALERT_SENT
    while True:
        try:
            IS_QUOTA_ALERT_SENT = False
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, target_value, expire_date FROM customer_targets WHERE target_type = "USER"')
            all_checks = cursor.fetchall()
            vn_now_str = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
            for u_id, target, exp_date in all_checks:
                if exp_date and exp_date < vn_now_str:
                    cursor.execute('DELETE FROM customer_targets WHERE user_id = ? AND target_value = ?', (u_id, target))
                    try:
                        bot.send_message(
                            u_id,
                            f"⏰ **MONITORING EXPIRED!**\n\n"
                            f"👤 Target `@{target}` has reached its monitoring expiration date.\n"
                            f"The system has automatically removed this target from background monitoring."
                        )
                    except:
                        pass
            conn.commit()
            cursor.execute('SELECT user_id, target_value, last_status, added_at FROM customer_targets WHERE target_type = "USER"')
            all_targets = cursor.fetchall()
            conn.close()
            total_tasks = len(all_targets)
            if total_tasks > 0:
                now_time = get_vietnam_time().strftime('%H:%M:%S')
                _, quota_str = fetch_remaining_quota()
                total_keys = len(API_POOL)
                print(f"\n🚀 [RADAR SCAN] - {now_time} UTC+7 | [Pool: {total_keys} Keys | Active: 1/{total_keys} | Current Quota: {quota_str}] | Total: {total_tasks} accounts")
                for index, (user_id, target_value, last_status, added_at_str) in enumerate(all_targets, 1):
                    current_time_str = get_vietnam_time().strftime('%H:%M:%S')
                    current_result = check_instagram_status_via_gateway(target_value)
                    icon_terminal = "🟢 LIVE" if current_result == "LIVE" else "🔴 DIE"
                    if current_result == "UNKNOWN":
                        icon_terminal = "🟡 UNKNOWN"
                    print(f"👉 [{current_time_str}] [{index}/{total_tasks}] @{target_value} ➔ {icon_terminal}")
                    # Detect status transition: DIE -> LIVE (Revived)
                    if last_status == "DIE" and current_result == "LIVE":
                        time.sleep(2)
                        res2 = check_instagram_status_via_gateway(target_value)
                        time.sleep(2)
                        res3 = check_instagram_status_via_gateway(target_value)
                        if current_result == res2 == res3 == "LIVE":
                            vn_now = get_vietnam_time()
                            now = vn_now.strftime("%Y-%m-%d %H:%M:%S")
                            times_taken_str = "0h 0m 0s"
                            if added_at_str:
                                try:
                                    added_dt = datetime.strptime(added_at_str, "%Y-%m-%d %H:%M:%S")
                                    duration_sec = (vn_now - added_dt).total_seconds()
                                    times_taken_str = format_duration(duration_sec)
                                except:
                                    pass
                            update_status_db(user_id, target_value, "LIVE", times_taken_str)
                            bot.send_message(
                                user_id, 
                                f"🔔 **ACCOUNT REVIVED NOTIFICATION!** 🎉\n\n"
                                f"👤 Username: `{target_value}`\n"
                                f"🟢 Status: [LIVE]\n"
                                f"⏱️ Times Taken: `{times_taken_str}`\n"
                                f"⏰ Timestamp: [{now} UTC+7]", 
                                parse_mode="Markdown"
                            )
                        else:
                            bot.send_message(user_id, f"⚙️ **System:** Fluctuating status detected for target `@{target_value}`...")
                            markup = InlineKeyboardMarkup()
                            btn_live = InlineKeyboardButton("🟢 Report LIVE to User", callback_data=f"force_live|{user_id}|{target_value}")
                            btn_die = InlineKeyboardButton("🔴 Report DIE to User", callback_data=f"force_die|{user_id}|{target_value}")
                            markup.add(btn_live, btn_die)
                            bot.send_message(
                                ADMIN_CHAT_ID, 
                                f"👑 **MANUAL REVIEW REQUIRED:**\n\n📌 Target: `@{target_value}`\nSelect status:", 
                                reply_markup=markup, 
                                parse_mode="Markdown"
                            )

                    elif last_status == "LIVE" and current_result == "DIE":
                        update_status_db(user_id, target_value, "DIE")
                        now = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
                        bot.send_message(
                            user_id, 
                            f"⚠️ **ACCOUNT DOWN ALERT!** 😭\n\n"
                            f"👤 Username: `{target_value}`\n"
                            f"❌ Status: [DIE]\n"
                            f"⏰ Timestamp: [{now} UTC+7]", 
                            parse_mode="Markdown"
                        )
                    time.sleep(random.uniform(2, 4))

                finish_time = get_vietnam_time().strftime('%H:%M:%S')
                print(f"🏁 Patrol cycle completed at {finish_time} UTC+7. Sleeping for 5 minutes...\n")
            time.sleep(300)
        except Exception as e:
            time.sleep(15)

# =====================================================================
# 🎛️ ADMIN INLINE CALLBACK HANDLER
# =====================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith(('force_live', 'force_die')))
def handle_admin_decision(call):
    action, user_id, target_value = call.data.split('|')
    user_id = int(user_id)
    vn_now = get_vietnam_time()
    now = vn_now.strftime("%Y-%m-%d %H:%M:%S")

    if action == "force_live":
        db_info = get_single_target(user_id, target_value)
        times_taken_str = "0h 0m 0s"
        if db_info and db_info[3]:
            try:
                added_dt = datetime.strptime(db_info[3], "%Y-%m-%d %H:%M:%S")
                duration_sec = (vn_now - added_dt).total_seconds()
                times_taken_str = format_duration(duration_sec)
            except:
                pass

        update_status_db(user_id, target_value, "LIVE", times_taken_str)
        bot.edit_message_text(f"✅ Approved `@{target_value}`: LIVE.", call.message.chat.id, call.message.message_id)
        bot.send_message(
            user_id, 
            f"🔔 **ACCOUNT REVIVED NOTIFICATION!** 🎉\n\n"
            f"👤 Username: `{target_value}`\n"
            f"🟢 Status: [LIVE]\n"
            f"⏱️ Times Taken: `{times_taken_str}`\n"
            f"⏰ Timestamp: [{now} UTC+7]", 
            parse_mode="Markdown"
        )
    elif action == "force_die":
        update_status_db(user_id, target_value, "DIE")
        bot.edit_message_text(f"✅ Approved `@{target_value}`: DIE.", call.message.chat.id, call.message.message_id)
        bot.send_message(
            user_id, 
            f"⚠️ **ACCOUNT REVIEW COMPLETED:**\n"
            f"Target `@{target_value}` confirmed inactive [DIE]!\n"
            f"⏰ Timestamp: [{now} UTC+7]", 
            parse_mode="Markdown"
        )

# =====================================================================
# 👑 ADMIN COMMANDS
# =====================================================================
@bot.message_handler(commands=['admin'])
def handle_admin_panel(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        bot.reply_to(message, "❌ Access denied: Admin command only!")
        return
    _, quota_str = fetch_remaining_quota()
    active_key = get_current_token()
    total_keys = len(API_POOL)
    key_display = f"`{active_key[:8]}...{active_key[-5:]}`" if active_key else "`Empty`"

    menu_admin = (
        " **ADMIN CONTROL PANEL**\n"
        "----------------------------------------\n"
        " `/settoken [Key]` : Prepend 1 new token to the front of queue\n"
        " `/setpool [k1 k2 ...]` : Load multiple tokens into the queue\n"
        " `/deltoken [Key]` : Remove a specific token from queue\n"
        " `/clearpool` : Wipe all tokens from the queue\n"
        " `/viewdata` : Export target monitoring list\n"
        "----------------------------------------\n"
        f" *Tokens in queue:* `{total_keys}`\n"
        f" *Active Token:* {key_display}\n"
        f" *Current Quota:* `{quota_str}`"
    )
    bot.reply_to(message, menu_admin, parse_mode="Markdown")

@bot.message_handler(commands=['settoken'])
def handle_set_token(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    msg_parts = message.text.split()
    if len(msg_parts) < 2:
        bot.reply_to(message, "⚠️ Usage: `/settoken [token_key]`")
        return
    global API_POOL, CURRENT_API_INDEX, INTERNAL_USAGE_COUNTER, IS_QUOTA_ALERT_SENT
    clean_token = re.sub(r'[\s\n\r]', '', msg_parts[1])
    API_POOL.insert(0, clean_token)
    CURRENT_API_INDEX = 0
    INTERNAL_USAGE_COUNTER = 0
    IS_QUOTA_ALERT_SENT = False
    bot.reply_to(message, f"✅ Successfully added new active token to queue front (Total: `{len(API_POOL)}` keys) and reset usage quota!", parse_mode="Markdown")

@bot.message_handler(commands=['setpool'])
def handle_set_pool(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    targets = parse_bulk_inputs(message.text, '/setpool')
    if not targets:
        bot.reply_to(message, "⚠️ Usage: `/setpool key1 key2 ...`")
        return
    global API_POOL, CURRENT_API_INDEX, INTERNAL_USAGE_COUNTER, IS_QUOTA_ALERT_SENT
    API_POOL = [re.sub(r'[\s\n\r]', '', k) for k in targets if k.strip()]
    added_keys = [k for k in new_keys if k not in API_POOL]

    if not added_keys:
        bot.reply_to(message, "⚠️ All provided tokens already exist in the queue!", parse_mode="Markdown")
        return
    was_empty = len(API_POOL) == 0
    API_POOL.extend(added_keys)
    if was_empty:
        CURRENT_API_INDEX = 0
        INTERNAL_USAGE_COUNTER = 0
        IS_QUOTA_ALERT_SENT = False

    bot.reply_to(
        message, 
        f"✅ **POOL QUEUE UPDATED SUCCESSFULLY!**\n\n"
        f"➕ Added to queue: `{len(added_keys)}` new tokens\n"
        f"🗂️ Total tokens in queue: `{len(API_POOL)}`\n"
        f"🚀 Active Token: `{API_POOL[CURRENT_API_INDEX][:8]}...{API_POOL[CURRENT_API_INDEX][-5:]}`",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['clearpool'])
def handle_clear_pool(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    global API_POOL, CURRENT_API_INDEX, INTERNAL_USAGE_COUNTER, IS_QUOTA_ALERT_SENT
    
    API_POOL.clear()
    CURRENT_API_INDEX = 0
    INTERNAL_USAGE_COUNTER = 0
    IS_QUOTA_ALERT_SENT = True  # Đánh dấu cạn pool để tránh spam alert thừa
    
    bot.reply_to(
        message,
        "🗑️ **API POOL CLEARED!**\n\n"
        "All tokens have been wiped from the queue. Background scans will be held on `UNKNOWN` until new tokens are added via `/setpool` or `/settoken`.",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['deltoken'])
def handle_del_token(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    msg_parts = message.text.split()
    if len(msg_parts) < 2:
        bot.reply_to(message, "⚠️ Usage: `/deltoken [token_key]`")
        return
        
    global API_POOL, CURRENT_API_INDEX, INTERNAL_USAGE_COUNTER
    target_token = re.sub(r'[\s\n\r]', '', msg_parts[1])
    
    # Tìm token khớp trong pool (so sánh chính xác hoặc tìm token chứa chuỗi con)
    matched = [k for k in API_POOL if k == target_token or target_token in k]
    if not matched:
        bot.reply_to(message, f"❌ Token not found in current pool: `{target_token}`", parse_mode="Markdown")
        return
        
    removed_token = matched[0]
    token_idx = API_POOL.index(removed_token)
    API_POOL.remove(removed_token)
    
    # Nếu xóa đúng token đang chạy ở đầu hàng chờ (index 0), reset lại quota cho token kế tiếp
    if token_idx == CURRENT_API_INDEX:
        CURRENT_API_INDEX = 0
        INTERNAL_USAGE_COUNTER = 0
        
    key_preview = f"`{removed_token[:8]}...{removed_token[-5:]}`" if len(removed_token) > 13 else f"`{removed_token}`"
    bot.reply_to(
        message,
        f"✅ **TOKEN REMOVED SUCCESSFULLY!**\n\n"
        f"🗑️ Deleted: {key_preview}\n"
        f"🗂️ Remaining tokens in pool: `{len(API_POOL)}`",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['viewdata'])
def handle_view_data(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    all_rows = get_all_targets_for_admin()
    if not all_rows:
        bot.reply_to(message, "📊 No active targets found in the database.")
        return
    now_str = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
    report = f"📊 **REAL-TIME MONITORING DATABASE**\n"
    report += f"⏰ Updated: [{now_str} UTC+7] | Total: `{len(all_rows)}` targets\n"
    report += "----------------------------------------\n\n"
    
    grouped_data = {}
    for user_id, target_value, last_status in all_rows:
        grouped_data.setdefault(user_id, []).append((target_value, last_status))
        
    for client_id, targets in grouped_data.items():
        report += f"👤 **Client (`{client_id}`):**\n"
        for idx, (username, status) in enumerate(targets, 1):
            icon = "🟢 Live" if status == "LIVE" else "🔴 Die"
            report += f"   {idx}. {icon} - `@{username}`\n"
        report += "\n"
        
    for text in util.smart_split(report, chars_per_string=3000):
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

# =====================================================================
# 🤖 USER COMMANDS
# =====================================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    menu = (
        "📸 **INSTAGRAM RADAR MONITORING BOT**\n\n"
        "⚡ `/check [User]` : Instant real-time status check\n"
        "👤 `/user [List_User] [Days] [Note]` : Add targets (Default: 7 days)\n"
        "📝 `/note [User] [Note]` : Add or update target note\n"
        "⏳ `/updatetime [User] [Days]` : Extend monitoring duration\n"
        "📋 `/list` : View monitored targets\n"
        "🗑️ `/del [List_User]` : Remove targets from monitoring"
    )
    bot.reply_to(message, menu, parse_mode="Markdown")

@bot.message_handler(commands=['check'])
def handle_quick_check(message):
    msg_parts = message.text.split()
    if len(msg_parts) < 2:
        bot.reply_to(message, "⚠️ Usage: `/check [username]`")
        return
    username = extract_username_from_link(msg_parts[1].strip())
    status_msg = bot.reply_to(message, f"🔍 Checking status for `@{username}`...")
    
    res = check_instagram_status_via_gateway(username)
    icon = "🟢 LIVE" if res == "LIVE" else ("🟡 UNKNOWN (Network Congestion)" if res == "UNKNOWN" else "🔴 DIE")
    now = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
    
    db_info = get_single_target(message.from_user.id, username)
    time_report = "Not in database"
    note_report = "None"
    created_report = "Not recorded"
    duration_report = "Pending"
    
    if db_info:
        time_report = get_time_remaining_string(db_info[0])
        note_report = db_info[1] if db_info[1] else "None"
        created_report = f"{db_info[2]} UTC+7" if db_info[2] else "Not recorded"
        duration_report = db_info[4] if db_info[4] else "Timer active..."
        
    output = (
        f"📊 **TARGET STATUS REPORT:**\n"
        f"----------------------------------------\n"
        f"👤 Target: `@{username}`\n"
        f"⚡ Status: {icon}\n"
        f"📆 Added Date: `{created_report}`\n"
        f"⏳ Time Left: `{time_report}`\n"
        f"⏱️ Times Taken: `{duration_report}`\n"
        f"📝 Note: `{note_report}`\n"
        f"⏰ Checked At: [{now} UTC+7]\n"
        f"----------------------------------------"
    )
    bot.edit_message_text(output, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown")

@bot.message_handler(commands=['user'])
def handle_user_check(message):
    raw_text = message.text.replace('/user', '', 1).strip()
    if not raw_text:
        bot.reply_to(message, "⚠️ Usage:\n`/user [username_list] [days] [note]`", parse_mode="Markdown")
        return
        
    match_parser = re.search(r'\s+(\d+)\s+(.+)\$', raw_text)
    if match_parser:
        days = int(match_parser.group(1))
        note_text = match_parser.group(2).strip()
        list_raw_part = raw_text[:match_parser.start()].strip()
    else:
        msg_parts = raw_text.split()
        if msg_parts[-1].isdigit():
            days = int(msg_parts[-1])
            list_raw_part = " ".join(msg_parts[:-1])
        else:
            days = DEFAULT_MONITOR_DAYS
            list_raw_part = raw_text
        note_text = ""
        
    targets_to_add = [item.strip() for item in re.split(r'[\s,\n]+', list_raw_part) if item.strip()]
    if not targets_to_add:
        bot.reply_to(message, "❌ Target list is empty!")
        return
        
    note_msg = f" |\nNote: *\"{note_text}\"*" if note_text else ""
    bot.reply_to(message, f"⚙️ Received {len(targets_to_add)} targets. Duration: **{days} days**{note_msg}...\nStopwatch started from 00:00:00!", parse_mode="Markdown")
    
    report = f"📋 **TARGET REGISTRATION REPORT ({days} days):**\n"
    if note_text:
        report += f"📝 Note: `{note_text}`\n"
    report += "----------------------------------------\n\n"
    
    customer_id = message.from_user.id
    for item in targets_to_add:
        username = extract_username_from_link(item)
        res = check_instagram_status_via_gateway(username)
        status_str = "LIVE" if res == "LIVE" else "DIE"
        
        add_to_customer_list(customer_id, username, "USER", status_str, days, note_text)
        
        icon = "🟢 Live" if res == "LIVE" else "🔴 Die"
        report += f"👤 `@{username}` ➔ {icon}\n"
        time.sleep(1)
        
    report += f"\n✅ Successfully synchronized with radar database!"
    for text in util.smart_split(report, chars_per_string=3000):
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['note'])
def handle_add_note(message):
    msg_parts = message.text.split(maxsplit=2)
    if len(msg_parts) < 3:
        bot.reply_to(message, "⚠️ Usage: `/note [username] [content]`")
        return
    username = extract_username_from_link(msg_parts[1].strip())
    note_text = msg_parts[2].strip()
    if update_target_note(message.from_user.id, username, note_text):
        bot.reply_to(message, f"📝 Updated note for `@{username}`:\n➔ *\"{note_text}\"*", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Target not found in your monitoring list.")

@bot.message_handler(commands=['updatetime'])
def handle_update_time(message):
    msg_parts = message.text.split()
    if len(msg_parts) < 3:
        bot.reply_to(message, "⚠️ Usage: `/updatetime [username] [days]`")
        return
    username = extract_username_from_link(msg_parts[1].strip())
    days_str = msg_parts[2].strip()
    if not days_str.isdigit():
        bot.reply_to(message, "❌ Number of days must be a positive integer!")
        return
    days = int(days_str)
    if update_target_time_cumulative(message.from_user.id, username, days):
        bot.reply_to(message, f"⏳ Successfully extended duration for `@{username}` by **{days} days**!", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Target not found in your monitoring list.")

@bot.message_handler(commands=['del'])
def handle_delete_target(message):
    targets = parse_bulk_inputs(message.text, '/del')
    if not targets:
        bot.reply_to(message, "⚠️ Usage: `/del user1 user2 ...`")
        return
    customer_id = message.from_user.id
    deleted_count = 0
    report = "🗑️ **BATCH REMOVAL REPORT:**\n\n"
    for item in targets:
        username = extract_username_from_link(item)
        if delete_from_customer_list(customer_id, username):
            report += f"✅ Removed: `@{username}`\n"
            deleted_count += 1
        else:
            report += f"❌ Not found: `@{username}`\n"
    report += f"\n🗑️ Total removed: `{deleted_count}` targets."
    bot.reply_to(message, report, parse_mode="Markdown")

@bot.message_handler(commands=['list'])
def handle_list_users(message):
    rows = get_targets_by_customer(message.from_user.id)
    if not rows:
        bot.reply_to(message, "📭 Your monitoring list is currently empty!")
        return
    report = "📋 **MONITORED TARGETS LIST:**\n\n"
    for index, (username, last_status, expire_date, note) in enumerate(rows, 1):
        icon = "🟢 Live" if last_status == "LIVE" else "🔴 Die"
        days_left = get_days_only(expire_date)
        ig_link = f"https://www.instagram.com/{username}/"
        report += f"**{index}**. {icon} - [{username}]({ig_link}) (`{days_left} days left`)\n"
        
    for text in util.smart_split(report, chars_per_string=3000):
        bot.send_message(message.chat.id, text, parse_mode="Markdown", disable_web_page_preview=True)

# =====================================================================
# 🏁 SYSTEM ENTRY POINT (CRASH-RESISTANT POLLING LOOP)
# =====================================================================
if __name__ == "__main__":
    init_db()
    radar_thread = threading.Thread(target=auto_scan_commercial_radar_v8, daemon=True)
    radar_thread.start()
    print("🚀 [SYSTEM] Radar v22.0 active. Listening for commands...")
    
    # Resilient polling loop: Auto-reconnect on network interruptions
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=20)
        except Exception as e:
            print(f"⚠️ [NETWORK] Polling interrupted: {e}. Retrying in 5 seconds...")
            time.sleep(5)
