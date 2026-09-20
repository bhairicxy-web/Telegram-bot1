# Telegram Forward Bot + Word Block + Bulk

Source channel → (optional word filter) → Target channel

## Features
- Auto-forward **new** posts from source channel
- **Word block** on new posts + manual forwards
- **Bulk copy** thousands of old messages: `/bulk 1 12500`
- Admin commands in private chat

## Quick start (local)
```bash
cp .env.example .env
# edit .env with your values
pip install -r requirements.txt
python bot.py
```

## Railway deploy
See **RAILWAY_STEPS_HINDI.md** (simple click-by-click).

## Commands (admin)
| Command | Meaning |
|---------|---------|
| `/start` | Help |
| `/status` | Check source/target |
| `/test` | Post test to target |
| `/bulk 1 12500` | Copy old messages by ID range |
| `/bulkstatus` | Bulk progress |
| `/bulkstop` | Stop bulk |
| `/block word` | Add blocked word |
| `/unblock word` | Remove word |
| `/listblocks` | List words |
| `/copy 36` | Copy one message by ID |

## Important
- Bot must be **Admin** on source + target channels
- Bulk uses Telegram `copyMessage` (word filter does **not** apply on bulk)
- Word filter applies on **new** channel posts and when you forward a post to the bot
