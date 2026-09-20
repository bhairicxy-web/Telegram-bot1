#!/usr/bin/env python3
"""
Telegram Forward Bot + Word Block + BULK copy
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "0") or "0")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0") or "0")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

WORDS_FILE = Path(__file__).parent / "blocked_words.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("forward-bot")

# Bulk job state (single job at a time)
bulk_task: asyncio.Task | None = None
bulk_stop = asyncio.Event()
bulk_status = {
    "running": False,
    "from_id": 0,
    "to_id": 0,
    "current": 0,
    "ok": 0,
    "fail": 0,
    "skip": 0,
}


def load_words() -> list[str]:
    if not WORDS_FILE.exists():
        return []
    try:
        data = json.loads(WORDS_FILE.read_text(encoding="utf-8"))
        return [w.lower().strip() for w in data.get("words", []) if w.strip()]
    except (json.JSONDecodeError, OSError) as e:
        logger.error("words read error: %s", e)
        return []


def save_words(words: list[str]) -> None:
    unique = sorted({w.lower().strip() for w in words if w.strip()})
    WORDS_FILE.write_text(
        json.dumps({"words": unique}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def remove_blocked_words(text: str, words: list[str]) -> str:
    if not text or not words:
        return text
    cleaned = text
    for word in words:
        cleaned = re.compile(re.escape(word), re.IGNORECASE).sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


async def reply(msg: Message, text: str) -> None:
    await msg.reply_text(text)


async def send_cleaned_to_target(context: ContextTypes.DEFAULT_TYPE, msg: Message) -> str:
    words = load_words()
    text = msg.text or msg.caption or ""
    cleaned = remove_blocked_words(text, words)
    has_media = bool(
        msg.photo or msg.video or msg.document or msg.audio
        or msg.voice or msg.animation or msg.sticker or msg.video_note
    )
    if not cleaned and not has_media:
        return "skip_empty"

    if msg.photo:
        await context.bot.send_photo(
            chat_id=TARGET_CHAT_ID, photo=msg.photo[-1].file_id, caption=cleaned or None
        )
    elif msg.video:
        await context.bot.send_video(
            chat_id=TARGET_CHAT_ID, video=msg.video.file_id, caption=cleaned or None
        )
    elif msg.document:
        await context.bot.send_document(
            chat_id=TARGET_CHAT_ID, document=msg.document.file_id, caption=cleaned or None
        )
    elif msg.audio:
        await context.bot.send_audio(
            chat_id=TARGET_CHAT_ID, audio=msg.audio.file_id, caption=cleaned or None
        )
    elif msg.voice:
        await context.bot.send_voice(
            chat_id=TARGET_CHAT_ID, voice=msg.voice.file_id, caption=cleaned or None
        )
    elif msg.animation:
        await context.bot.send_animation(
            chat_id=TARGET_CHAT_ID, animation=msg.animation.file_id, caption=cleaned or None
        )
    elif msg.sticker:
        await context.bot.send_sticker(chat_id=TARGET_CHAT_ID, sticker=msg.sticker.file_id)
    elif msg.video_note:
        await context.bot.send_video_note(
            chat_id=TARGET_CHAT_ID, video_note=msg.video_note.file_id
        )
    elif cleaned:
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=cleaned)
    else:
        return "skip_empty"
    return "ok"


async def channel_auto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.id != SOURCE_CHAT_ID:
        return
    try:
        status = await send_cleaned_to_target(context, msg)
        if status == "ok":
            logger.info("Auto-forwarded msg %s", msg.message_id)
    except Exception:
        logger.exception("Auto forward failed")


async def manual_forward_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type != "private":
        return
    if msg.text and msg.text.startswith("/"):
        return
    if not is_admin(user.id):
        await reply(msg, "Sirf admin.")
        return
    try:
        status = await send_cleaned_to_target(context, msg)
        if status == "ok":
            await reply(msg, "Done! Hmm pe chala gaya.")
        else:
            await reply(msg, "Empty after filter / no media.")
    except Exception as e:
        await reply(msg, f"Fail: {e}")


# ── BULK COPY (12500+ messages) ─────────────────────────────────────────────
async def run_bulk(
    context: ContextTypes.DEFAULT_TYPE,
    admin_chat_id: int,
    from_id: int,
    to_id: int,
    delay: float,
) -> None:
    """Copy message IDs from_id..to_id from SOURCE → TARGET."""
    global bulk_status
    bulk_stop.clear()
    bulk_status.update(
        {
            "running": True,
            "from_id": from_id,
            "to_id": to_id,
            "current": from_id,
            "ok": 0,
            "fail": 0,
            "skip": 0,
        }
    )
    total = to_id - from_id + 1
    logger.info("BULK start %s→%s total~%s delay=%s", from_id, to_id, total, delay)

    await context.bot.send_message(
        admin_chat_id,
        f"BULK START\n"
        f"Range: {from_id} → {to_id} (~{total} ids)\n"
        f"Delay: {delay}s per msg\n"
        f"ETA rough: {int(total * delay / 60)}+ min\n\n"
        f"Progress: /bulkstatus\n"
        f"Stop: /bulkstop\n\n"
        f"Note: deleted/missing IDs skip ho jayenge.\n"
        f"Word-filter bulk copy pe nahi lagta (Telegram limit).\n"
        f"Sirf copy ho raha hai source → target.",
    )

    last_progress = 0
    try:
        for mid in range(from_id, to_id + 1):
            if bulk_stop.is_set():
                break

            bulk_status["current"] = mid
            try:
                await context.bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=mid,
                )
                bulk_status["ok"] += 1
            except RetryAfter as e:
                wait = int(e.retry_after) + 1
                logger.warning("Flood wait %ss at id %s", wait, mid)
                await asyncio.sleep(wait)
                try:
                    await context.bot.copy_message(
                        chat_id=TARGET_CHAT_ID,
                        from_chat_id=SOURCE_CHAT_ID,
                        message_id=mid,
                    )
                    bulk_status["ok"] += 1
                except TelegramError:
                    bulk_status["skip"] += 1
            except TelegramError as e:
                # message not found / deleted / not accessible
                err = str(e).lower()
                if "not found" in err or "message to copy not found" in err:
                    bulk_status["skip"] += 1
                else:
                    bulk_status["fail"] += 1
                    if bulk_status["fail"] <= 5:
                        logger.warning("bulk id %s: %s", mid, e)
            except Exception as e:
                bulk_status["fail"] += 1
                logger.warning("bulk id %s unexpected: %s", mid, e)

            # progress every 100 ok+skip+fail or every 100 ids
            done = bulk_status["ok"] + bulk_status["skip"] + bulk_status["fail"]
            if done - last_progress >= 100 or mid == to_id:
                last_progress = done
                pct = int((mid - from_id + 1) / total * 100)
                try:
                    await context.bot.send_message(
                        admin_chat_id,
                        f"Progress {pct}%\n"
                        f"ID: {mid}/{to_id}\n"
                        f"OK: {bulk_status['ok']} | Skip: {bulk_status['skip']} | Fail: {bulk_status['fail']}",
                    )
                except Exception:
                    pass

            await asyncio.sleep(delay)

    finally:
        bulk_status["running"] = False
        stopped = bulk_stop.is_set()
        await context.bot.send_message(
            admin_chat_id,
            f"BULK {'STOPPED' if stopped else 'FINISHED'}\n\n"
            f"OK copied: {bulk_status['ok']}\n"
            f"Skip (missing): {bulk_status['skip']}\n"
            f"Fail: {bulk_status['fail']}\n"
            f"Last ID: {bulk_status['current']}",
        )
        logger.info("BULK done status=%s", bulk_status)


async def cmd_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /bulk 1 12500
    /bulk 1 12500 0.15
    """
    global bulk_task
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        await reply(msg, "Sirf admin.")
        return

    if bulk_status["running"]:
        await reply(
            msg,
            f"Bulk pehle se chal raha hai (id {bulk_status['current']}).\n"
            f"/bulkstatus ya /bulkstop",
        )
        return

    if not context.args or len(context.args) < 2:
        await reply(
            msg,
            "12500 messages bulk copy:\n\n"
            "Usage:\n"
            "/bulk FROM_ID TO_ID\n"
            "/bulk FROM_ID TO_ID DELAY\n\n"
            "Example:\n"
            "/bulk 1 13361\n"
            "/bulk 1 13361 0.2\n\n"
            "Message ID kaise pata karein?\n"
            "Source post open karo → Share link\n"
            "Link: t.me/c/XXXX/13361 → ID = 13361\n"
            "Pehla post ID usually 1+ se start\n\n"
            "DELAY default 0.12 sec (flood se bachne ke liye)\n"
            "12500 x 0.12s ≈ 25 minutes+\n\n"
            "/bulkstatus - progress\n"
            "/bulkstop - band karo\n\n"
            "IMPORTANT:\n"
            "- Bulk = seedha COPY (word block nahi)\n"
            "- Word block sirf naye posts + manual forward pe\n"
            "- Beech ke deleted IDs skip",
        )
        return

    try:
        from_id = int(context.args[0])
        to_id = int(context.args[1])
        delay = float(context.args[2]) if len(context.args) >= 3 else 0.12
    except ValueError:
        await reply(msg, "IDs numbers hone chahiye. Example: /bulk 1 12500")
        return

    if from_id < 1 or to_id < from_id:
        await reply(msg, "FROM >= 1 aur TO >= FROM hona chahiye.")
        return
    if to_id - from_id > 200000:
        await reply(msg, "Range bahut badi hai (max 200000 ids ek job me).")
        return
    if delay < 0.05:
        delay = 0.05
    if delay > 5:
        delay = 5

    bulk_stop.clear()
    bulk_task = asyncio.create_task(
        run_bulk(context, user.id, from_id, to_id, delay)
    )
    await reply(msg, f"Bulk job start: {from_id} → {to_id}. /bulkstatus se dekho.")


async def cmd_bulkstop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        if msg:
            await reply(msg, "Sirf admin.")
        return
    if not bulk_status["running"]:
        await reply(msg, "Koi bulk job nahi chal rahi.")
        return
    bulk_stop.set()
    await reply(msg, "Stop signal bhej diya — thodi der me ruk jayegi.")


async def cmd_bulkstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        if msg:
            await reply(msg, "Sirf admin.")
        return
    s = bulk_status
    if not s["running"] and s["ok"] == 0 and s["current"] == 0:
        await reply(msg, "Abhi koi bulk job nahi / pehle nahi chali.")
        return
    total = max(1, s["to_id"] - s["from_id"] + 1)
    pct = int((s["current"] - s["from_id"] + 1) / total * 100) if s["to_id"] else 0
    await reply(
        msg,
        f"Bulk {'RUNNING' if s['running'] else 'IDLE'}\n"
        f"Range: {s['from_id']} → {s['to_id']}\n"
        f"Current ID: {s['current']} ({pct}%)\n"
        f"OK: {s['ok']} | Skip: {s['skip']} | Fail: {s['fail']}",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    await reply(
        msg,
        "Forward Bot + Word Block + BULK\n\n"
        "=== 12500 PURANE MESSAGES ===\n"
        "/bulk 1 13361\n"
        "(apne last message ID ke hisaab se TO_ID badlo)\n\n"
        "/bulkstatus - progress\n"
        "/bulkstop - stop\n\n"
        "Message ID: post ka link t.me/c/xxxx/ID\n\n"
        "=== Naye posts ===\n"
        "Source pe auto forward + word block\n\n"
        "=== Ek purana post + word block ===\n"
        "Post → Forward → @Forwardbyrbot\n\n"
        "Other:\n"
        "/status /test /block /listblocks /copy ID\n\n"
        f"Admin: {'YES' if user and is_admin(user.id) else 'NO'}",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Sirf admin.")
        return
    try:
        sent = await context.bot.send_message(
            chat_id=TARGET_CHAT_ID, text="Bot test OK."
        )
        await reply(msg, f"Target OK id={sent.message_id}")
    except Exception as e:
        await reply(msg, f"Fail: {e}")


async def cmd_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args or not context.args[0].isdigit():
        await reply(msg, "Usage: /copy 36")
        return
    mid = int(context.args[0])
    try:
        await context.bot.copy_message(
            chat_id=TARGET_CHAT_ID, from_chat_id=SOURCE_CHAT_ID, message_id=mid
        )
        await reply(msg, f"Copied {mid}")
    except Exception as e:
        await reply(msg, f"Fail: {e}")


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args:
        await reply(msg, "Usage: /block word")
        return
    word = " ".join(context.args).strip().lower()
    words = load_words()
    if word in words:
        await reply(msg, "Pehle se blocked.")
        return
    words.append(word)
    save_words(words)
    await reply(msg, f"Blocked: {word}")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args:
        await reply(msg, "Usage: /unblock word")
        return
    word = " ".join(context.args).strip().lower()
    words = load_words()
    if word not in words:
        await reply(msg, "List me nahi.")
        return
    save_words([w for w in words if w != word])
    await reply(msg, f"Unblocked: {word}")


async def cmd_listblocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    words = load_words()
    await reply(msg, "Blocked:\n" + ("\n".join(f"- {w}" for w in words) if words else "(empty)"))


async def cmd_clearblocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    save_words([])
    await reply(msg, "Cleared.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    src = tgt = "?"
    try:
        src = (await context.bot.get_chat(SOURCE_CHAT_ID)).title or "?"
    except Exception as e:
        src = f"ERR {e}"
    try:
        tgt = (await context.bot.get_chat(TARGET_CHAT_ID)).title or "?"
    except Exception as e:
        tgt = f"ERR {e}"
    await reply(
        msg,
        f"Source: {src}\nTarget: {tgt}\n"
        f"Blocked words: {len(load_words())}\n"
        f"Bulk running: {bulk_status['running']}\n"
        f"12500 ke liye: /bulk 1 LAST_ID",
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Handler error: %s", context.error)


def main() -> None:
    if not BOT_TOKEN or not SOURCE_CHAT_ID or not TARGET_CHAT_ID:
        raise SystemExit("Config missing")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    for cmd, fn in [
        ("start", cmd_start),
        ("help", cmd_help),
        ("test", cmd_test),
        ("copy", cmd_copy),
        ("bulk", cmd_bulk),
        ("bulkstop", cmd_bulkstop),
        ("bulkstatus", cmd_bulkstatus),
        ("block", cmd_block),
        ("unblock", cmd_unblock),
        ("listblocks", cmd_listblocks),
        ("clearblocks", cmd_clearblocks),
        ("status", cmd_status),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=SOURCE_CHAT_ID) & ~filters.COMMAND,
            channel_auto_handler,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & ~filters.COMMAND,
            channel_auto_handler,
        ),
        group=1,
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            manual_forward_handler,
        ),
        group=2,
    )

    logger.info("Bot starting bulk-enabled source=%s target=%s", SOURCE_CHAT_ID, TARGET_CHAT_ID)
    app.run_polling(
        allowed_updates=["message", "channel_post", "edited_channel_post", "edited_message"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
