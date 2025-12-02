import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
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

# Data file to persist message records (use /app/data in Docker)
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "message_records.json")

def ensure_data_file():
    """Ensure the data file exists and is valid (not a directory)."""
    # Create data directory if it doesn't exist
    if DATA_DIR and DATA_DIR != "." and not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
        logger.info(f"Created data directory: {DATA_DIR}")
    
    # Check if DATA_FILE is accidentally a directory (Docker mount issue)
    if os.path.isdir(DATA_FILE):
        logger.warning(f"{DATA_FILE} is a directory! This can happen if Docker mounted a non-existent file.")
        logger.warning(f"Removing directory and creating file...")
        import shutil
        shutil.rmtree(DATA_FILE)
    
    # Create empty JSON file if it doesn't exist
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'w') as f:
            json.dump({}, f)
        logger.info(f"Created data file: {DATA_FILE}")

def load_records():
    """Load message records from file."""
    ensure_data_file()
    try:
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading records: {e}. Starting with empty records.")
        return {}

def save_records(records):
    """Save message records to file."""
    ensure_data_file()
    with open(DATA_FILE, 'w') as f:
        json.dump(records, f, indent=2)

def clean_old_records(records):
    """Remove records older than the cooldown period."""
    now = datetime.now()
    cleaned = {}
    for user_id, timestamp_str in records.items():
        timestamp = datetime.fromisoformat(timestamp_str)
        if now - timestamp < timedelta(hours=MESSAGE_COOLDOWN_HOURS):
            cleaned[user_id] = timestamp_str
    return cleaned

def can_user_send_message(user_id: int, records: dict) -> tuple[bool, timedelta | None]:
    """Check if user can send a message. Returns (can_send, time_remaining)."""
    user_id_str = str(user_id)
    if user_id_str not in records:
        return True, None
    
    last_message_time = datetime.fromisoformat(records[user_id_str])
    time_since_last = datetime.now() - last_message_time
    
    if time_since_last >= timedelta(hours=MESSAGE_COOLDOWN_HOURS):
        return True, None
    
    time_remaining = timedelta(hours=MESSAGE_COOLDOWN_HOURS) - time_since_last
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

async def is_admin(bot, chat_id: int, user_id: int) -> bool:
    """Check if user is an admin in the group. Uses caching to reduce API calls."""
    cache_key = f"{chat_id}"
    now = datetime.now()
    
    # Check if we have a valid cache
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
    
    # Skip bot messages and admins (optional - uncomment to enable)
    # chat_member = await context.bot.get_chat_member(chat_id, user_id)
    # if chat_member.status in ['administrator', 'creator']:
    #     return
    
    # Load and clean records
    records = load_records()
    records = clean_old_records(records)
    
    # Check if user can send a message
    can_send, time_remaining = can_user_send_message(user_id, records)
    
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
            
            # Send a warning (will be deleted after a few seconds)
            warning = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=TOPIC_ID,
                text=f"⚠️ @{username}, you can only send 1 message per {MESSAGE_COOLDOWN_HOURS} hours.\n"
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
• `/reset <user_id>` - Reset a user's cooldown
• `/help` - Show this message

**How it works:**
1. Users can send only 1 message per {MESSAGE_COOLDOWN_HOURS} hours in the topic
2. Additional messages are automatically deleted
3. A temporary warning is shown to the user

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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    # Start the bot
    logger.info("Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
