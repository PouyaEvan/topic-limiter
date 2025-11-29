import os
import json
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from dotenv import load_dotenv

load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
TOPIC_ID = 1362  # From the link https://t.me/PasarGuardGP/1362

# Data file to persist message records (use /app/data in Docker)
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "message_records.json")

def load_records():
    """Load message records from file."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_records(records):
    """Save message records to file."""
    with open(DATA_FILE, 'w') as f:
        json.dump(records, f, indent=2)

def clean_old_records(records):
    """Remove records older than 24 hours."""
    now = datetime.now()
    cleaned = {}
    for user_id, timestamp_str in records.items():
        timestamp = datetime.fromisoformat(timestamp_str)
        if now - timestamp < timedelta(hours=24):
            cleaned[user_id] = timestamp_str
    return cleaned

def can_user_send_message(user_id: int, records: dict) -> tuple[bool, timedelta | None]:
    """Check if user can send a message. Returns (can_send, time_remaining)."""
    user_id_str = str(user_id)
    if user_id_str not in records:
        return True, None
    
    last_message_time = datetime.fromisoformat(records[user_id_str])
    time_since_last = datetime.now() - last_message_time
    
    if time_since_last >= timedelta(hours=24):
        return True, None
    
    time_remaining = timedelta(hours=24) - time_since_last
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
ADMIN_CACHE_TTL = 300  # 5 minutes

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
        print(f"Error fetching admins: {e}")
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages in the topic."""
    message = update.message
    
    if not message:
        return
    
    # Check if it's from the correct group and topic
    chat_id = message.chat_id
    message_thread_id = message.message_thread_id
    
    # Debug logging (can be removed in production)
    print(f"Message from chat: {chat_id}, thread: {message_thread_id}, user: {message.from_user.id}")
    
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
        # Delete the message
        try:
            await message.delete()
            
            # Calculate remaining time
            hours = int(time_remaining.total_seconds() // 3600)
            minutes = int((time_remaining.total_seconds() % 3600) // 60)
            
            # Send a warning (will be deleted after a few seconds)
            warning = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=TOPIC_ID,
                text=f"⚠️ @{username}, you can only send 1 message per 24 hours.\n"
                     f"Please wait {hours}h {minutes}m before sending another message.",
            )
            
            # Delete warning after 10 seconds
            await asyncio.sleep(10)
            await warning.delete()
            
        except Exception as e:
            print(f"Error handling message: {e}")
        
        return
    
    # Record this message
    records[str(user_id)] = datetime.now().isoformat()
    save_records(records)
    
    print(f"✅ Message from {username} (ID: {user_id}) recorded")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status and current records."""
    message = update.message
    
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
    
    # Check if user is admin in the group
    if not await is_admin(context.bot, message.chat_id, message.from_user.id):
        await message.reply_text("❌ This command is for group admins only.")
        return
    
    help_text = """
🤖 **Topic Message Limiter Bot**

This bot limits users to 1 message per 24 hours in the monitored topic.

**Commands (Admin Only):**
• `/status` - View current message records
• `/check_duplicates` - Check for duplicate users today
• `/reset <user_id>` - Reset a user's cooldown
• `/help` - Show this message

**How it works:**
1. Users can send only 1 message per 24 hours in the topic
2. Additional messages are automatically deleted
3. A temporary warning is shown to the user
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

def main():
    """Start the bot."""
    if not BOT_TOKEN:
        print("❌ Error: BOT_TOKEN not found in environment variables!")
        print("Please create a .env file with BOT_TOKEN=your_token_here")
        return
    
    print("🚀 Starting Topic Message Limiter Bot...")
    print(f"📍 Monitoring Topic ID: {TOPIC_ID}")
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("check_duplicates", check_duplicates_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    # Start the bot
    print("✅ Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
