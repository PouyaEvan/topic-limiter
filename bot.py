import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from telegram.constants import ChatMemberStatus
from dotenv import load_dotenv

load_dotenv()

# Setup logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
TOPIC_ID = int(os.getenv("TOPIC_ID", "0"))
MESSAGE_COOLDOWN_HOURS = int(os.getenv("MESSAGE_COOLDOWN_HOURS", "24"))
WARNING_DELETE_SECONDS = int(os.getenv("WARNING_DELETE_SECONDS", "10"))
ADMIN_CACHE_TTL = int(os.getenv("ADMIN_CACHE_TTL", "300"))  # 5 minutes default

# Allowed groups - comma-separated list of group IDs (empty = allow all)
ALLOWED_GROUPS_STR = os.getenv("ALLOWED_GROUPS", "")
ALLOWED_GROUPS = [int(g.strip()) for g in ALLOWED_GROUPS_STR.split(",") if g.strip()]

# Telegram's anonymous admin bot ID
GROUP_ANONYMOUS_BOT_ID = 1087968824

# Data file to persist message records (use /app/data in Docker)
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "message_records.json")
CUSTOM_ADMINS_FILE = os.path.join(DATA_DIR, "custom_admins.json")
USER_COOLDOWNS_FILE = os.path.join(DATA_DIR, "user_cooldowns.json")

def ensure_data_dir():
    """Ensure the data directory exists."""
    if DATA_DIR and DATA_DIR != "." and not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
        logger.info(f"Created data directory: {DATA_DIR}")

def ensure_data_file(file_path: str):
    """Ensure the data file exists and is valid (not a directory)."""
    ensure_data_dir()
    
    # Check if file is accidentally a directory (Docker mount issue)
    if os.path.isdir(file_path):
        logger.warning(f"{file_path} is a directory! This can happen if Docker mounted a non-existent file.")
        logger.warning(f"Removing directory and creating file...")
        import shutil
        shutil.rmtree(file_path)
    
    # Create empty JSON file if it doesn't exist
    if not os.path.exists(file_path):
        with open(file_path, 'w') as f:
            json.dump({}, f)
        logger.info(f"Created data file: {file_path}")

def load_records():
    """Load message records from file."""
    ensure_data_file(DATA_FILE)
    try:
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading records: {e}. Starting with empty records.")
        return {}

def save_records(records):
    """Save message records to file."""
    ensure_data_file(DATA_FILE)
    with open(DATA_FILE, 'w') as f:
        json.dump(records, f, indent=2)

def load_custom_admins():
    """Load custom admins from file. Returns dict keyed by chat_id."""
    ensure_data_file(CUSTOM_ADMINS_FILE)
    try:
        with open(CUSTOM_ADMINS_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading custom admins: {e}. Starting with empty list.")
        return {}

def save_custom_admins(admins: dict):
    """Save custom admins to file."""
    ensure_data_file(CUSTOM_ADMINS_FILE)
    with open(CUSTOM_ADMINS_FILE, 'w') as f:
        json.dump(admins, f, indent=2)

def load_user_cooldowns():
    """Load user cooldowns from file. Returns dict keyed by chat_id, then user_id."""
    ensure_data_file(USER_COOLDOWNS_FILE)
    try:
        with open(USER_COOLDOWNS_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading user cooldowns: {e}. Starting with empty list.")
        return {}

def save_user_cooldowns(cooldowns: dict):
    """Save user cooldowns to file."""
    ensure_data_file(USER_COOLDOWNS_FILE)
    with open(USER_COOLDOWNS_FILE, 'w') as f:
        json.dump(cooldowns, f, indent=2)

def clean_old_records(records, chat_id: int = None):
    """Remove records older than the cooldown period. Considers custom cooldowns."""
    now = datetime.now()
    cleaned = {}
    user_cooldowns = load_user_cooldowns()
    chat_cooldowns = user_cooldowns.get(str(chat_id), {}) if chat_id else {}
    
    for user_id, timestamp_str in records.items():
        timestamp = datetime.fromisoformat(timestamp_str)
        # Get custom cooldown for this user, or use default
        user_cooldown_hours = chat_cooldowns.get(user_id, MESSAGE_COOLDOWN_HOURS)
        if now - timestamp < timedelta(hours=user_cooldown_hours):
            cleaned[user_id] = timestamp_str
    return cleaned

def get_user_cooldown_hours(chat_id: int, user_id: int) -> int:
    """Get the cooldown hours for a specific user (green card support)."""
    user_cooldowns = load_user_cooldowns()
    chat_cooldowns = user_cooldowns.get(str(chat_id), {})
    return chat_cooldowns.get(str(user_id), MESSAGE_COOLDOWN_HOURS)

def can_user_send_message(user_id: int, records: dict, chat_id: int = None) -> tuple[bool, timedelta | None]:
    """Check if user can send a message. Returns (can_send, time_remaining)."""
    user_id_str = str(user_id)
    if user_id_str not in records:
        return True, None
    
    # Get custom cooldown for this user
    cooldown_hours = get_user_cooldown_hours(chat_id, user_id) if chat_id else MESSAGE_COOLDOWN_HOURS
    
    # Green card: 0 hours cooldown means unlimited messages
    if cooldown_hours == 0:
        return True, None
    
    last_message_time = datetime.fromisoformat(records[user_id_str])
    time_since_last = datetime.now() - last_message_time
    
    if time_since_last >= timedelta(hours=cooldown_hours):
        return True, None
    
    time_remaining = timedelta(hours=cooldown_hours) - time_since_last
    return False, time_remaining

def check_duplicate_users_today(records: dict) -> list[str]:
    """Check for any duplicate user messages within the same day."""
    today = datetime.now().date()
    users_today = []
    duplicates = []
    
    for user_id, timestamp_str in records.items():
        timestamp = datetime.fromisoformat(timestamp_str)
        if timestamp.date() == today:
            if user_id in users_today:
                duplicates.append(user_id)
            else:
                users_today.append(user_id)
    
    return duplicates

# Cache for admin list (to avoid too many API calls)
admin_cache = {}

# Track users who recently received a warning (to avoid spam warnings)
recent_warnings = {}

async def delete_message_later(bot, chat_id: int, message_id: int, delay: int):
    """Delete a message after a delay without blocking."""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.debug(f"Could not delete warning message: {e}")

def is_custom_admin(chat_id: int, user_id: int) -> bool:
    """Check if user is in the custom admin list for this chat."""
    custom_admins = load_custom_admins()
    chat_admins = custom_admins.get(str(chat_id), [])
    return user_id in chat_admins

async def is_admin(bot, chat_id: int, user_id: int, sender_chat=None) -> bool:
    """Check if user is an admin in the group. 
    
    Supports:
    - Regular admins via Telegram API
    - Custom admins added via /addadmin
    - Anonymous admins (detected via sender_chat)
    """
    # Check for anonymous admin (sender_chat matches the group)
    if sender_chat and sender_chat.id == chat_id:
        logger.debug(f"Anonymous admin detected via sender_chat (chat_id: {chat_id})")
        return True
    
    # Check if this is the GroupAnonymousBot (ID: 1087968824)
    if user_id == GROUP_ANONYMOUS_BOT_ID:
        logger.debug(f"Anonymous admin detected via GroupAnonymousBot ID")
        return True
    
    # Check custom admin list first
    if is_custom_admin(chat_id, user_id):
        return True
    
    # Check cache
    cache_key = f"{chat_id}"
    now = datetime.now()
    
    if cache_key in admin_cache:
        cached_data = admin_cache[cache_key]
        if (now - cached_data['timestamp']).total_seconds() < ADMIN_CACHE_TTL:
            return user_id in cached_data['admin_ids']
    
    # Fetch fresh admin list from the group
    try:
        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in admins]
        
        # Update cache
        admin_cache[cache_key] = {
            'admin_ids': admin_ids,
            'timestamp': now
        }
        
        return user_id in admin_ids
    except Exception as e:
        logger.error(f"Error fetching admins: {e}")
        return False

async def is_group_admin_or_creator(bot, chat_id: int, user_id: int) -> bool:
    """Check if user is a real Telegram group admin or creator (for /addadmin, /removeadmin permissions)."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

def is_allowed_group(chat_id: int) -> bool:
    """Check if the chat is in the allowed groups list."""
    if not ALLOWED_GROUPS:
        return True  # If no groups specified, allow all
    return chat_id in ALLOWED_GROUPS


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages in the topic."""
    message = update.message
    
    if not message:
        return
    
    # Check if it's from the correct group and topic
    chat_id = message.chat_id
    message_thread_id = message.message_thread_id
    
    # Debug logging
    logger.debug(f"Message from chat: {chat_id}, thread: {message_thread_id}, user: {message.from_user.id}")
    
    # Only process messages from allowed groups
    if not is_allowed_group(chat_id):
        logger.debug(f"Ignoring message from non-allowed group: {chat_id}")
        return
    
    # Only process messages in the specific topic
    if message_thread_id != TOPIC_ID:
        return
    
    user = message.from_user
    user_id = user.id
    username = user.username or user.first_name or "Unknown"
    
    # Get sender_chat for anonymous admin detection
    sender_chat = message.sender_chat
    
    # Skip admin messages (including anonymous admins and custom admins)
    if await is_admin(context.bot, chat_id, user_id, sender_chat):
        logger.debug(f"Skipping admin message from {username} (ID: {user_id})")
        return
    
    # Load and clean records
    records = load_records()
    records = clean_old_records(records, chat_id)
    
    # Check if user can send a message (with custom cooldown support)
    can_send, time_remaining = can_user_send_message(user_id, records, chat_id)
    
    if not can_send:
        # Delete the message immediately
        try:
            await message.delete()
            logger.debug(f"Deleted spam message from {username} (ID: {user_id})")
        except Exception as e:
            logger.error(f"Error deleting message: {e}")
        
        # Check if we recently warned this user (avoid warning spam)
        user_warning_key = f"{chat_id}:{user_id}"
        now = datetime.now()
        
        if user_warning_key in recent_warnings:
            last_warning_time = recent_warnings[user_warning_key]
            # Only send a new warning if the last one was more than WARNING_DELETE_SECONDS ago
            if (now - last_warning_time).total_seconds() < WARNING_DELETE_SECONDS + 2:
                # Skip sending another warning, just delete the message
                return
        
        # Record that we're warning this user
        recent_warnings[user_warning_key] = now
        
        try:
            # Calculate remaining time
            hours = int(time_remaining.total_seconds() // 3600)
            minutes = int((time_remaining.total_seconds() % 3600) // 60)
            
            # Get user's cooldown (might be custom)
            user_cooldown = get_user_cooldown_hours(chat_id, user_id)
            
            # Send a warning (will be deleted after a few seconds)
            warning = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=TOPIC_ID,
                text=f"⚠️ @{username}, you can only send 1 message per {user_cooldown} hours.\n"
                     f"Please wait {hours}h {minutes}m before sending another message.",
            )
            
            # Schedule warning deletion without blocking (non-blocking)
            asyncio.create_task(delete_message_later(context.bot, chat_id, warning.message_id, WARNING_DELETE_SECONDS))
            
        except Exception as e:
            logger.error(f"Error sending warning: {e}")
        
        return
    
    # Record this message
    records[str(user_id)] = datetime.now().isoformat()
    save_records(records)
    
    logger.info(f"Message from {username} (ID: {user_id}) recorded")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status and current records."""
    message = update.message
    
    # Only respond in allowed groups
    if not is_allowed_group(message.chat_id):
        return
    
    # Check if user is admin in the group
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    records = load_records()
    records = clean_old_records(records)
    
    if not records:
        await message.reply_text("📊 No messages recorded in the last 24 hours.")
        return
    
    status_text = "📊 **Message Records (Last 24 Hours)**\n\n"
    for user_id, timestamp_str in records.items():
        timestamp = datetime.fromisoformat(timestamp_str)
        time_ago = datetime.now() - timestamp
        hours_ago = int(time_ago.total_seconds() // 3600)
        minutes_ago = int((time_ago.total_seconds() % 3600) // 60)
        status_text += f"• User ID `{user_id}`: {hours_ago}h {minutes_ago}m ago\n"
    
    status_text += f"\n**Total: {len(records)} users**"
    
    await message.reply_text(status_text, parse_mode="Markdown")

async def check_duplicates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check for duplicate user messages today."""
    message = update.message
    
    # Only respond in allowed groups
    if not is_allowed_group(message.chat_id):
        return
    
    # Check if user is admin in the group
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    records = load_records()
    duplicates = check_duplicate_users_today(records)
    
    if duplicates:
        await message.reply_text(
            f"⚠️ **Duplicate Users Found Today:**\n" + 
            "\n".join([f"• User ID: `{uid}`" for uid in duplicates]),
            parse_mode="Markdown"
        )
    else:
        await message.reply_text("✅ No duplicate user messages found today.")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset a specific user's cooldown (admin only)."""
    message = update.message
    
    # Only respond in allowed groups
    if not is_allowed_group(message.chat_id):
        return
    
    # Check if user is admin in the group
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    # Get user ID from command arguments
    if not context.args:
        await message.reply_text("Usage: /reset <user_id>")
        return
    
    try:
        target_user_id = str(context.args[0])
        records = load_records()
        
        if target_user_id in records:
            del records[target_user_id]
            save_records(records)
            await message.reply_text(f"✅ Reset cooldown for user ID: `{target_user_id}`", parse_mode="Markdown")
        else:
            await message.reply_text(f"ℹ️ User ID `{target_user_id}` not found in records.", parse_mode="Markdown")
    
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a user to the custom admin list (real group admins only)."""
    message = update.message
    
    if not is_allowed_group(message.chat_id):
        return
    
    # Only real Telegram admins/creators can add custom admins
    if not await is_group_admin_or_creator(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    
    if not context.args:
        await message.reply_text("Usage: /addadmin <user_id>\n\nReply to a user's message or provide their user ID.")
        return
    
    try:
        target_user_id = int(context.args[0])
        chat_id = str(message.chat_id)
        
        custom_admins = load_custom_admins()
        if chat_id not in custom_admins:
            custom_admins[chat_id] = []
        
        if target_user_id in custom_admins[chat_id]:
            await message.reply_text(f"ℹ️ User ID `{target_user_id}` is already a custom admin.", parse_mode="Markdown")
            return
        
        custom_admins[chat_id].append(target_user_id)
        save_custom_admins(custom_admins)
        
        await message.reply_text(f"✅ Added user ID `{target_user_id}` to custom admin list.", parse_mode="Markdown")
        logger.info(f"Added custom admin {target_user_id} in chat {chat_id}")
    
    except ValueError:
        await message.reply_text("❌ Invalid user ID. Please provide a numeric user ID.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a user from the custom admin list (real group admins only)."""
    message = update.message
    
    if not is_allowed_group(message.chat_id):
        return
    
    # Only real Telegram admins/creators can remove custom admins
    if not await is_group_admin_or_creator(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    
    if not context.args:
        await message.reply_text("Usage: /removeadmin <user_id>")
        return
    
    try:
        target_user_id = int(context.args[0])
        chat_id = str(message.chat_id)
        
        custom_admins = load_custom_admins()
        
        if chat_id not in custom_admins or target_user_id not in custom_admins[chat_id]:
            await message.reply_text(f"ℹ️ User ID `{target_user_id}` is not a custom admin.", parse_mode="Markdown")
            return
        
        custom_admins[chat_id].remove(target_user_id)
        save_custom_admins(custom_admins)
        
        await message.reply_text(f"✅ Removed user ID `{target_user_id}` from custom admin list.", parse_mode="Markdown")
        logger.info(f"Removed custom admin {target_user_id} from chat {chat_id}")
    
    except ValueError:
        await message.reply_text("❌ Invalid user ID. Please provide a numeric user ID.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all custom admins for this chat."""
    message = update.message
    
    if not is_allowed_group(message.chat_id):
        return
    
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    chat_id = str(message.chat_id)
    custom_admins = load_custom_admins()
    chat_admins = custom_admins.get(chat_id, [])
    
    if not chat_admins:
        await message.reply_text("📋 No custom admins configured for this chat.")
        return
    
    admin_list = "\n".join([f"• `{uid}`" for uid in chat_admins])
    await message.reply_text(
        f"📋 **Custom Admins:**\n{admin_list}\n\n**Total: {len(chat_admins)}**",
        parse_mode="Markdown"
    )

async def setcooldown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a custom cooldown for a specific user (green card feature)."""
    message = update.message
    
    if not is_allowed_group(message.chat_id):
        return
    
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    if len(context.args) < 2:
        await message.reply_text(
            "Usage: /setcooldown <user_id> <hours>\n\n"
            "Examples:\n"
            "• `/setcooldown 123456789 12` - Set 12 hour cooldown\n"
            "• `/setcooldown 123456789 0` - No cooldown (unlimited messages)",
            parse_mode="Markdown"
        )
        return
    
    try:
        target_user_id = str(context.args[0])
        cooldown_hours = int(context.args[1])
        
        if cooldown_hours < 0:
            await message.reply_text("❌ Cooldown hours must be 0 or greater.")
            return
        
        chat_id = str(message.chat_id)
        user_cooldowns = load_user_cooldowns()
        
        if chat_id not in user_cooldowns:
            user_cooldowns[chat_id] = {}
        
        user_cooldowns[chat_id][target_user_id] = cooldown_hours
        save_user_cooldowns(user_cooldowns)
        
        if cooldown_hours == 0:
            await message.reply_text(
                f"🎫 **Green Card Granted!**\nUser ID `{target_user_id}` can now send unlimited messages.",
                parse_mode="Markdown"
            )
        else:
            await message.reply_text(
                f"✅ Set cooldown for user ID `{target_user_id}` to **{cooldown_hours} hours**.",
                parse_mode="Markdown"
            )
        
        logger.info(f"Set cooldown for user {target_user_id} to {cooldown_hours}h in chat {chat_id}")
    
    except ValueError:
        await message.reply_text("❌ Invalid arguments. User ID and hours must be numbers.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

async def resetcooldown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset a user's cooldown to the default value."""
    message = update.message
    
    if not is_allowed_group(message.chat_id):
        return
    
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    if not context.args:
        await message.reply_text("Usage: /resetcooldown <user_id>")
        return
    
    try:
        target_user_id = str(context.args[0])
        chat_id = str(message.chat_id)
        
        user_cooldowns = load_user_cooldowns()
        
        if chat_id in user_cooldowns and target_user_id in user_cooldowns[chat_id]:
            del user_cooldowns[chat_id][target_user_id]
            save_user_cooldowns(user_cooldowns)
            await message.reply_text(
                f"✅ Reset cooldown for user ID `{target_user_id}` to default ({MESSAGE_COOLDOWN_HOURS} hours).",
                parse_mode="Markdown"
            )
        else:
            await message.reply_text(
                f"ℹ️ User ID `{target_user_id}` already has default cooldown.",
                parse_mode="Markdown"
            )
    
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

async def listcooldowns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all custom cooldowns for this chat."""
    message = update.message
    
    if not is_allowed_group(message.chat_id):
        return
    
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    chat_id = str(message.chat_id)
    user_cooldowns = load_user_cooldowns()
    chat_cooldowns = user_cooldowns.get(chat_id, {})
    
    if not chat_cooldowns:
        await message.reply_text(f"📋 No custom cooldowns configured. Default: {MESSAGE_COOLDOWN_HOURS} hours.")
        return
    
    cooldown_list = []
    for uid, hours in chat_cooldowns.items():
        if hours == 0:
            cooldown_list.append(f"• `{uid}`: 🎫 Green Card (unlimited)")
        else:
            cooldown_list.append(f"• `{uid}`: {hours} hours")
    
    await message.reply_text(
        f"📋 **Custom Cooldowns:**\n" + "\n".join(cooldown_list) + f"\n\n**Default: {MESSAGE_COOLDOWN_HOURS} hours**",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    message = update.message
    
    # Only respond in allowed groups
    if not is_allowed_group(message.chat_id):
        return
    
    # Check if user is admin in the group
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    help_text = f"""
🤖 **Topic Message Limiter Bot**

This bot limits users to 1 message per {MESSAGE_COOLDOWN_HOURS} hours in the monitored topic.

**Commands (Admin Only):**
• `/status` - View current message records
• `/check_duplicates` - Check for duplicate users today
• `/reset <user_id>` - Reset a user's message record
• `/help` - Show this message

**Admin Management:**
• `/addadmin <user_id>` - Add a custom admin (exempt from limits)
• `/removeadmin <user_id>` - Remove a custom admin
• `/listadmins` - List all custom admins

**Green Card (Custom Cooldowns):**
• `/setcooldown <user_id> <hours>` - Set custom cooldown
• `/resetcooldown <user_id>` - Reset to default cooldown
• `/listcooldowns` - List all custom cooldowns

_Tip: Set cooldown to 0 for unlimited messages (Green Card)._

**How it works:**
1. Admins (including anonymous admins) are exempt from limits
2. Users can send only 1 message per {MESSAGE_COOLDOWN_HOURS} hours in the topic
3. Additional messages are automatically deleted
4. A temporary warning is shown to the user

**Current Config:**
• Topic ID: `{TOPIC_ID}`
• Cooldown: `{MESSAGE_COOLDOWN_HOURS}` hours
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

def main():
    """Start the bot."""
    # Validate required configuration
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables!")
        logger.error("Please create a .env file with BOT_TOKEN=your_token_here")
        return
    
    if not TOPIC_ID:
        logger.error("TOPIC_ID not found in environment variables!")
        logger.error("Please set TOPIC_ID in your .env file")
        return
    
    logger.info("Starting Topic Message Limiter Bot...")
    logger.info(f"Monitoring Topic ID: {TOPIC_ID}")
    logger.info(f"Message cooldown: {MESSAGE_COOLDOWN_HOURS} hours")
    logger.info(f"Warning delete delay: {WARNING_DELETE_SECONDS} seconds")
    logger.info(f"Admin cache TTL: {ADMIN_CACHE_TTL} seconds")
    if ALLOWED_GROUPS:
        logger.info(f"Allowed groups: {ALLOWED_GROUPS}")
    else:
        logger.info("Allowed groups: ALL (no restriction)")
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("check_duplicates", check_duplicates_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("addadmin", addadmin_command))
    application.add_handler(CommandHandler("removeadmin", removeadmin_command))
    application.add_handler(CommandHandler("listadmins", listadmins_command))
    application.add_handler(CommandHandler("setcooldown", setcooldown_command))
    application.add_handler(CommandHandler("resetcooldown", resetcooldown_command))
    application.add_handler(CommandHandler("listcooldowns", listcooldowns_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    # Start the bot
    logger.info("Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
