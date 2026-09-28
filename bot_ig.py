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

# Tải cấu hình biến môi trường từ file .env
load_dotenv()

# =====================================================================
# 🛠️ CẤU HÌNH TÀI NGUYÊN VÀ QUẢN TRỊ
# =====================================================================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
bot = telebot.TeleBot(TOKEN)

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))
DB_NAME = "ig_commercial_radar.db"

# Kho hàng chờ API: Tải từ .env (phân tách bởi dấu phẩy nếu có nhiều key)
raw_keys = os.getenv("SCRAPE_API_KEY", "")
API_POOL = [k.strip() for k in raw_keys.split(",") if k.strip()]

CURRENT_API_INDEX = 0
DEFAULT_MONITOR_DAYS = 7
INTERNAL_USAGE_COUNTER = 0
IS_QUOTA_ALERT_SENT = False

def get_vietnam_time():
    """Đồng bộ múi giờ UTC+7."""
    return datetime.utcnow() + timedelta(hours=7)

def get_current_token():
    """Lấy token hiện hành ở đầu hàng chờ."""
    global API_POOL, CURRENT_API_INDEX
    if API_POOL and 0 <= CURRENT_API_INDEX < len(API_POOL):
        return API_POOL[CURRENT_API_INDEX].strip()
    return ""

def fetch_remaining_quota():
    """Tính toán dung lượng quota còn lại."""
    global INTERNAL_USAGE_COUNTER
    remaining = 1000 - INTERNAL_USAGE_COUNTER
    if remaining < 0:
        remaining = 0
    return remaining, f"{remaining}/1000"

def switch_to_next_api_logic():
    """Xóa token hết hạn và chuyển sang token tiếp theo."""
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
                f"🔄 **HỆ THỐNG TỰ ĐỘNG XOAY TUA POOL!**\n\n"
                f"🗑️ Đã xóa 1 token hết hạn.\n"
                f"🗂️ Số token còn lại: `{len(API_POOL)}`"
            )
        except:
            pass
        return True
    return False

def format_duration(seconds):
    """Quy đổi số giây sang định dạng ..h ..p ..s."""
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}h {minutes}p {secs}s"

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
            return "Hết hạn"
        delta = expire_date - vn_now
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        secs = delta.seconds % 60
        return f"{days} ngày {hours:02d}:{minutes:02d}:{secs:02d}"
    except:
        return "0 ngày 00:00:00"

# =====================================================================
# ⚡ LÕI KIỂM TRA TRẠNG THÁI INSTAGRAM
# =====================================================================
def check_instagram_status_via_gateway(username):
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
        
        if response.status_code in [429, 403]:
            if switch_to_next_api_logic():
                return check_instagram_status_via_gateway(username)
            raise ValueError("ALL_API_POOL_EXCEEDED")
            
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
                        f"🚨 **POOL API ĐÃ CẠN KIỆT TOÀN BỘ!**\n\n"
                        f"⏰ Thời điểm: [{now_str} VN]\n"
                        f"Vui lòng nạp thêm token bằng lệnh `/setpool`.",
                        parse_mode="Markdown"
                    )
                    IS_QUOTA_ALERT_SENT = True
                except:
                    pass
        return "UNKNOWN"

# =====================================================================
# 💾 QUẢN LÝ DỮ LIỆU SQLITE (HỖ TRỢ ĐẾM THỜI GIAN HỒI SINH)
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
    # Bổ sung các cột nếu db đã tồn tại từ trước
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
        print(f"Lỗi thêm database: {e}")
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
# 🔄 LUỒNG TUẦN TRA RADAR NGẦM
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
                            f"⏰ **THÔNG BÁO HẾT HẠN!**\n\n"
                            f"👤 Tài khoản `@{target}` đã hết hạn giám sát.\n"
                            f"Hệ thống đã dừng theo dõi mục tiêu này."
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
                print(f"\n🚀 [RADAR SCAN] - {now_time} VN | [Kho: {total_keys} Key | Đang dùng: 1/{total_keys} | Quota: {quota_str}] | Tổng: {total_tasks} nick")
                
                for index, (user_id, target_value, last_status, added_at_str) in enumerate(all_targets, 1):
                    current_time_str = get_vietnam_time().strftime('%H:%M:%S')
                    current_result = check_instagram_status_via_gateway(target_value)
                    
                    icon_terminal = "🟢 LIVE" if current_result == "LIVE" else "🔴 DIE"
                    if current_result == "UNKNOWN": 
                        icon_terminal = "🟡 UNKNOWN"
                    print(f"👉 [{current_time_str}] [{index}/{total_tasks}] @{target_value} ➔ {icon_terminal}")
                    
                    # Phát hiện trạng thái DIE -> LIVE (Hồi sinh)
                    if last_status == "DIE" and current_result == "LIVE":
                        time.sleep(2)
                        res2 = check_instagram_status_via_gateway(target_value)
                        time.sleep(2)
                        res3 = check_instagram_status_via_gateway(target_value)
                        
                        if current_result == res2 == res3 == "LIVE":
                            vn_now = get_vietnam_time()
                            now = vn_now.strftime("%Y-%m-%d %H:%M:%S")
                            
                            # Tính thời gian chờ từ lúc nạp
                            times_taken_str = "0h 0p 0s"
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
                                f"🔔 **THÔNG BÁO TÀI KHOẢN HỒI SINH!** 🎉\n\n"
                                f"👤 Username: `{target_value}`\n"
                                f"🟢 Trạng thái: [LIVE]\n"
                                f"⏱️ Times Taken: `{times_taken_str}`\n"
                                f"⏰ Thời gian: [{now} VN]", 
                                parse_mode="Markdown"
                            )
                        else:
                            bot.send_message(user_id, f"⚙️ **Hệ thống:** Tài khoản `@{target_value}` đang phát sinh dữ liệu nhiễu...")
                            markup = InlineKeyboardMarkup()
                            btn_live = InlineKeyboardButton("🟢 Báo LIVE cho khách", callback_data=f"force_live|{user_id}|{target_value}")
                            btn_die = InlineKeyboardButton("🔴 Báo DIE cho khách", callback_data=f"force_die|{user_id}|{target_value}")
                            markup.add(btn_live, btn_die)
                            bot.send_message(
                                ADMIN_CHAT_ID, 
                                f"👑 **DUYỆT THỦ CÔNG:**\n\n📌 Mục tiêu: `@{target_value}`\nChọn trạng thái:", 
                                reply_markup=markup, 
                                parse_mode="Markdown"
                            )
                            
                    elif last_status == "LIVE" and current_result == "DIE":
                        update_status_db(user_id, target_value, "DIE")
                        now = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
                        bot.send_message(
                            user_id, 
                            f"⚠️ **CẢNH BÁO TÀI KHOẢN ĐỘT TỬ!** 😭\n\n"
                            f"👤 Username: `{target_value}`\n"
                            f"❌ Trạng thái: [DIE]\n"
                            f"⏰ Thời gian: [{now} VN]", 
                            parse_mode="Markdown"
                        )
                    time.sleep(random.uniform(2, 4))
                
                finish_time = get_vietnam_time().strftime('%H:%M:%S')
                print(f"🏁 Hoàn thành chu kỳ lúc {finish_time} VN. Chờ 5 phút...\n")
            time.sleep(300)
        except Exception as e:
            time.sleep(15)

# =====================================================================
# 🎛️ XỬ LÝ NÚT BẤM DUYỆT TAY CỦA ADMIN
# =====================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith(('force_live', 'force_die')))
def handle_admin_decision(call):
    action, user_id, target_value = call.data.split('|')
    user_id = int(user_id)
    vn_now = get_vietnam_time()
    now = vn_now.strftime("%Y-%m-%d %H:%M:%S")
    
    if action == "force_live":
        db_info = get_single_target(user_id, target_value)
        times_taken_str = "0h 0p 0s"
        if db_info and db_info[3]:
            try:
                added_dt = datetime.strptime(db_info[3], "%Y-%m-%d %H:%M:%S")
                duration_sec = (vn_now - added_dt).total_seconds()
                times_taken_str = format_duration(duration_sec)
            except:
                pass
                
        update_status_db(user_id, target_value, "LIVE", times_taken_str)
        bot.edit_message_text(f"✅ Đã duyệt `@{target_value}`: LIVE.", call.message.chat.id, call.message.message_id)
        bot.send_message(
            user_id, 
            f"🔔 **THÔNG BÁO TÀI KHOẢN HỒI SINH!** 🎉\n\n"
            f"👤 Username: `{target_value}`\n"
            f"🟢 Trạng thái: [LIVE]\n"
            f"⏱️ Times Taken: `{times_taken_str}`\n"
            f"⏰ Thời gian: [{now} VN]", 
            parse_mode="Markdown"
        )
    elif action == "force_die":
        update_status_db(user_id, target_value, "DIE")
        bot.edit_message_text(f"✅ Đã duyệt `@{target_value}`: DIE.", call.message.chat.id, call.message.message_id)
        bot.send_message(
            user_id, 
            f"⚠️ **CẢNH BÁO HOÀN TẤT RÀ SOÁT:**\n"
            f"Tài khoản `@{target_value}` ngưng hoạt động [DIE]!\n"
            f"⏰ Thời gian: [{now} VN]", 
            parse_mode="Markdown"
        )

# =====================================================================
# 👑 LỆNH QUẢN TRỊ VIÊN (ADMIN)
# =====================================================================
@bot.message_handler(commands=['admin'])
def handle_admin_panel(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        bot.reply_to(message, "❌ Lệnh chỉ dành cho Admin!")
        return
    _, quota_str = fetch_remaining_quota()
    active_key = get_current_token()
    total_keys = len(API_POOL)
    key_display = f"`{active_key[:8]}...{active_key[-5:]}`" if active_key else "`Trống`"
    
    menu_admin = (
        "👑 **BẢNG ĐIỀU KHIỂN QUẢN TRỊ**\n"
        "----------------------------------------\n"
        "⚙️ `/settoken [Key]` : Chèn 1 token lên đầu danh sách trực chiến\n"
        "⚙️ `/setpool [k1 k2 ...]` : Nạp hàng loạt key vào hàng chờ\n"
        "📊 `/viewdata` : Xuất danh sách theo dõi của khách hàng\n"
        "----------------------------------------\n"
        f"🎯 *Số key trong hàng chờ:* `{total_keys}`\n"
        f"🔑 *Key đang chạy:* {key_display}\n"
        f"📊 *Quota key hiện tại:* `{quota_str}`"
    )
    bot.reply_to(message, menu_admin, parse_mode="Markdown")

@bot.message_handler(commands=['settoken'])
def handle_set_token(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    msg_parts = message.text.split()
    if len(msg_parts) < 2:
        bot.reply_to(message, "⚠️ Cú pháp: `/settoken [mã_token]`")
        return
    global API_POOL, CURRENT_API_INDEX, INTERNAL_USAGE_COUNTER, IS_QUOTA_ALERT_SENT
    clean_token = re.sub(r'[\s\n\r]', '', msg_parts[1])
    API_POOL.insert(0, clean_token)
    CURRENT_API_INDEX = 0
    INTERNAL_USAGE_COUNTER = 0
    IS_QUOTA_ALERT_SENT = False
    bot.reply_to(message, f"✅ Đã chèn token mới lên vị trí đầu hàng chờ (Tổng: `{len(API_POOL)}` key) và đặt lại quota!", parse_mode="Markdown")

@bot.message_handler(commands=['setpool'])
def handle_set_pool(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    targets = parse_bulk_inputs(message.text, '/setpool')
    if not targets:
        bot.reply_to(message, "⚠️ Cú pháp: `/setpool key1 key2 ...`")
        return
    global API_POOL, CURRENT_API_INDEX, INTERNAL_USAGE_COUNTER, IS_QUOTA_ALERT_SENT
    API_POOL = [re.sub(r'[\s\n\r]', '', k) for k in targets]
    CURRENT_API_INDEX = 0
    INTERNAL_USAGE_COUNTER = 0
    IS_QUOTA_ALERT_SENT = False
    bot.reply_to(message, f"✅ Đã nạp `{len(API_POOL)}` token vào hàng chờ tuần tự!", parse_mode="Markdown")

@bot.message_handler(commands=['viewdata'])
def handle_view_data(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    all_rows = get_all_targets_for_admin()
    if not all_rows:
        bot.reply_to(message, "📊 Hiện chưa có mục tiêu nào trong hệ thống.")
        return
    now_str = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
    report = f"📊 **DỮ LIỆU THEO DÕI REAL-TIME**\n"
    report += f"⏰ Cập nhật: [{now_str} VN] | Tổng: `{len(all_rows)}` acc\n"
    report += "----------------------------------------\n\n"
    
    grouped_data = {}
    for user_id, target_value, last_status in all_rows:
        grouped_data.setdefault(user_id, []).append((target_value, last_status))
        
    for client_id, targets in grouped_data.items():
        report += f"👤 **Khách hàng (`{client_id}`):**\n"
        for idx, (username, status) in enumerate(targets, 1):
            icon = "🟢 Live" if status == "LIVE" else "🔴 Die"
            report += f"   {idx}. {icon} - `@{username}`\n"
        report += "\n"
        
    for text in util.smart_split(report, chars_per_string=3000):
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

# =====================================================================
# 🤖 LỆNH NGƯỜI DÙNG
# =====================================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    menu = (
        "📸 **INSTAGRAM RADAR SYSTEM**\n\n"
        "⚡ `/check [User]` : Kiểm tra trạng thái tức thì\n"
        "👤 `/user [List_User] [Số_Ngày] [Ghi_Chú]` : Nạp nick (Mặc định: 7 ngày)\n"
        "📝 `/note [User] [Ghi chú]` : Bổ sung ghi chú\n"
        "⏳ `/updatetime [User] [Số ngày]` : Cộng dồn hạn giám sát\n"
        "📋 `/list` : Xem danh sách tài khoản theo dõi\n"
        "🗑️ `/del [List_User]` : Xóa nick khỏi hệ thống"
    )
    bot.reply_to(message, menu, parse_mode="Markdown")

@bot.message_handler(commands=['check'])
def handle_quick_check(message):
    msg_parts = message.text.split()
    if len(msg_parts) < 2:
        bot.reply_to(message, "⚠️ Cú pháp: `/check [username]`")
        return
    username = extract_username_from_link(msg_parts[1].strip())
    status_msg = bot.reply_to(message, f"🔍 Đang kiểm tra `@{username}`...")
    
    res = check_instagram_status_via_gateway(username)
    icon = "🟢 LIVE" if res == "LIVE" else ("🟡 UNKNOWN (Nghẽn mạng)" if res == "UNKNOWN" else "🔴 DIE")
    now = get_vietnam_time().strftime("%Y-%m-%d %H:%M:%S")
    
    db_info = get_single_target(message.from_user.id, username)
    time_report = "Chưa lưu trong DB"
    note_report = "Trống"
    created_report = "Chưa ghi nhận"
    duration_report = "Chưa hoàn tất"
    
    if db_info:
        time_report = get_time_remaining_string(db_info[0])
        note_report = db_info[1] if db_info[1] else "Trống"
        created_report = f"{db_info[2]} VN" if db_info[2] else "Chưa ghi nhận"
        duration_report = db_info[4] if db_info[4] else "Đang đếm thời gian..."
        
    output = (
        f"📊 **KẾT QUẢ KIỂM TRA:**\n"
        f"----------------------------------------\n"
        f"👤 Mục tiêu: `@{username}`\n"
        f"⚡ Trạng thái: {icon}\n"
        f"📆 Ngày nạp: `{created_report}`\n"
        f"⏳ Hạn còn lại: `{time_report}`\n"
        f"⏱️ Times Taken: `{duration_report}`\n"
        f"📝 Ghi chú: `{note_report}`\n"
        f"⏰ Kiểm tra lúc: [{now} VN]\n"
        f"----------------------------------------"
    )
    bot.edit_message_text(output, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown")

@bot.message_handler(commands=['user'])
def handle_user_check(message):
    raw_text = message.text.replace('/user', '', 1).strip()
    if not raw_text:
        bot.reply_to(message, "⚠️ Cú pháp:\n`/user [list_nick] [số_ngày] [ghi_chú]`", parse_mode="Markdown")
        return
        
    match_parser = re.search(r'\s+(\d+)\s+(.+)$', raw_text)
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
        bot.reply_to(message, "❌ Danh sách tài khoản trống!")
        return
        
    note_msg = f" |\nGhi chú: *\"{note_text}\"*" if note_text else ""
    bot.reply_to(message, f"⚙️ Tiếp nhận {len(targets_to_add)} mục tiêu. Hạn: **{days} ngày**{note_msg}...\nBắt đầu kích hoạt bộ đếm thời gian từ 00:00:00!", parse_mode="Markdown")
    
    report = f"📋 **KẾT QUẢ NẠP ({days} ngày):**\n"
    if note_text:
        report += f"📝 Ghi chú: `{note_text}`\n"
    report += "----------------------------------------\n\n"
    
    customer_id = message.from_user.id
    for item in targets_to_add:
        username = extract_username_from_link(item)
        res = check_instagram_status_via_gateway(username)
        status_str = "LIVE" if res == "LIVE" else "DIE"
        
        # Nạp mục tiêu kèm mốc thời gian bắt đầu
        add_to_customer_list(customer_id, username, "USER", status_str, days, note_text)
        
        icon = "🟢 Live" if res == "LIVE" else "🔴 Die"
        report += f"👤 `@{username}` ➔ {icon}\n"
        time.sleep(1)
        
    report += f"\n✅ Đã đồng bộ vào cơ sở dữ liệu giám sát!"
    for text in util.smart_split(report, chars_per_string=3000):
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['note'])
def handle_add_note(message):
    msg_parts = message.text.split(maxsplit=2)
    if len(msg_parts) < 3:
        bot.reply_to(message, "⚠️ Cú pháp: `/note [Tên_User] [Nội_dung]`")
        return
    username = extract_username_from_link(msg_parts[1].strip())
    note_text = msg_parts[2].strip()
    if update_target_note(message.from_user.id, username, note_text):
        bot.reply_to(message, f"📝 Đã cập nhật ghi chú cho `@{username}`:\n➔ *\"{note_text}\"*", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Không tìm thấy tài khoản trong danh sách.")

@bot.message_handler(commands=['updatetime'])
def handle_update_time(message):
    msg_parts = message.text.split()
    if len(msg_parts) < 3:
        bot.reply_to(message, "⚠️ Cú pháp: `/updatetime [Tên_User] [Số_Ngày]`")
        return
    username = extract_username_from_link(msg_parts[1].strip())
    days_str = msg_parts[2].strip()
    if not days_str.isdigit():
        bot.reply_to(message, "❌ Số ngày phải là số nguyên dương!")
        return
    days = int(days_str)
    if update_target_time_cumulative(message.from_user.id, username, days):
        bot.reply_to(message, f"⏳ Đã cộng dồn hạn cho `@{username}` thêm **{days} ngày**!", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Không tìm thấy tài khoản trong danh sách.")

@bot.message_handler(commands=['del'])
def handle_delete_target(message):
    targets = parse_bulk_inputs(message.text, '/del')
    if not targets:
        bot.reply_to(message, "⚠️ Cú pháp: `/del nick1 nick2 ...`")
        return
    customer_id = message.from_user.id
    deleted_count = 0
    report = "🗑️ **KẾT QUẢ GỠ BỎ MỤC TIÊU:**\n\n"
    for item in targets:
        username = extract_username_from_link(item)
        if delete_from_customer_list(customer_id, username):
            report += f"✅ Đã gỡ: `@{username}`\n"
            deleted_count += 1
        else:
            report += f"❌ Không tìm thấy: `@{username}`\n"
    report += f"\n🗑️ Tổng cộng đã xóa: `{deleted_count}` tài khoản."
    bot.reply_to(message, report, parse_mode="Markdown")

@bot.message_handler(commands=['list'])
def handle_list_users(message):
    rows = get_targets_by_customer(message.from_user.id)
    if not rows:
        bot.reply_to(message, "📭 Danh sách theo dõi hiện đang trống!")
        return
    report = "📋 **DANH SÁCH MỤC TIÊU ĐANG THEO DÕI:**\n\n"
    for index, (username, last_status, expire_date, note) in enumerate(rows, 1):
        icon = "🟢 Live" if last_status == "LIVE" else "🔴 Die"
        days_left = get_days_only(expire_date)
        ig_link = f"https://www.instagram.com/{username}/"
        report += f"**{index}**. {icon} - [{username}]({ig_link}) (Còn `{days_left} ngày`)\n"
        
    for text in util.smart_split(report, chars_per_string=3000):
        bot.send_message(message.chat.id, text, parse_mode="Markdown", disable_web_page_preview=True)

# =====================================================================
# 🏁 KHỞI CHẠY HỆ THỐNG (BỌC CHỐNG CRASH CHO MÁY CÁ NHÂN)
# =====================================================================
if __name__ == "__main__":
    init_db()
    radar_thread = threading.Thread(target=auto_scan_commercial_radar_v8, daemon=True)
    radar_thread.start()
    print("🚀 [HỆ THỐNG] Radar v22.0 đã kích hoạt. Bắt đầu nhận lệnh...")
    
    # Vòng lặp duy trì kết nối polling liên tục, tự khôi phục khi mất mạng
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=20)
        except Exception as e:
            print(f"⚠️ [MẠNG] Telegram Polling bị ngắt: {e}. Thử kết nối lại sau 5s...")
            time.sleep(5)