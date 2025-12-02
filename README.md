# Topic Message Limiter Bot

A Telegram bot that limits users to **1 message per 24 hours** in a specific topic/thread.

## Features

- ✅ **Rate Limiting**: Users can only send 1 message per 24 hours in the monitored topic
- ✅ **Auto-Delete**: Extra messages are automatically deleted
- ✅ **Duplicate Detection**: Check for users who might have bypassed the limit
- ✅ **Persistent Storage**: Message records are saved to a JSON file
- ✅ **Admin Commands**: Status, reset, and duplicate check commands

## Setup

### 1. Create a Bot

1. Go to [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the instructions
3. Copy the bot token

### 2. Add Bot to Group

1. Add the bot to your group
2. **Make the bot an admin** with these permissions:
   - Delete messages
   - (Optional) Restrict members

### 3. Configure the Bot

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and add your bot token:
   ```
   BOT_TOKEN=your_bot_token_here
   ```

3. The topic ID is already configured for `https://t.me/PasarGuardGP/1362` (Topic ID: 1362)

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Run the Bot

```bash
python bot.py
```

### Running with Docker (docker run)

Build the image locally:

```bash
docker build -t topic-limiter:latest .
```

Run the container (recommended - using a data directory):

```bash
# Create data directory first
mkdir -p ./data

# Make sure you have a `.env` with `BOT_TOKEN` in the project root
docker run -d \
   --name topic-limiter \
   --restart unless-stopped \
   --env-file .env \
   -e DATA_DIR=/app/data \
   -v "$(pwd)/data:/app/data" \
   topic-limiter:latest
```

Notes:
- The `--env-file .env` flag reads environment variables (including `BOT_TOKEN`) from your local `.env` file.
- The `-v` mount persists the data directory on the host so message records survive container restarts.
- **Important**: Always mount a **directory**, not a file. Docker will create a directory if the mount target doesn't exist, which causes errors.

Alternative (pass token directly):

```bash
mkdir -p ./data
docker run -d --name topic-limiter \
   --restart unless-stopped \
   -e BOT_TOKEN=your_bot_token_here \
   -e DATA_DIR=/app/data \
   -v "$(pwd)/data:/app/data" \
   topic-limiter:latest
```

View logs:

```bash
docker logs -f topic-limiter
```

Stop and remove container:

```bash
docker stop topic-limiter
docker rm topic-limiter
```


## Commands (Admin Only)

| Command | Description |
|---------|-------------|
| `/status` | View current message records (last 24h) |
| `/check_duplicates` | Check for duplicate user messages today |
| `/reset <user_id>` | Reset a specific user's cooldown |
| `/help` | Show help message |

## How It Works

1. When a user sends a message in the monitored topic, the bot records their user ID and timestamp
2. If the same user tries to send another message within 24 hours:
   - The message is deleted
   - A temporary warning is shown (auto-deletes after 10 seconds)
3. After 24 hours, the user can send a new message
4. Old records are automatically cleaned up

## File Structure

```
topic-limiter/
├── bot.py                 # Main bot code
├── requirements.txt       # Python dependencies
├── .env                   # Bot token (create from .env.example)
├── .env.example           # Example environment file
├── .gitignore             # Git ignore file
├── message_records.json   # Persistent message records (auto-created)
└── README.md              # This file
```

## Troubleshooting

- **Bot not deleting messages**: Make sure the bot is an admin with "Delete messages" permission
- **Bot not responding**: Check that the bot token is correct in `.env`
- **Wrong topic**: Update `TOPIC_ID` in `bot.py` to match your topic

## License

MIT License
