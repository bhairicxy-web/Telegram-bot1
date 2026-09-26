#!/usr/bin/env python3
"""
Telegram Forward Bot + Word Block + BULK (with word filter)
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

# Railway-friendly extra words: BLOCKED_WORDS=spam,scam,badword
ENV_WORDS = [
    w.lower().strip()
    for w in os.getenv("BLOCKED_WORDS", "").split(",")
    if w.strip()
]

WORDS_FILE = Path(__file__).parent / "blocked_words.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("forward-bot")

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
    "filtered": 0,
}


def load_words() -> list[str]:
    words: list[str] = []
    if WORDS_FILE.exists():
        try:
            data = json.loads(WORDS_FILE.read_text(encoding="utf-8"))
            words.extend(
                w.lower().strip() for w in data.get("words", []) if w.strip()
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.error("words read error: %s", e)
    words.extend(ENV_WORDS)
    # unique keep order
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


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


def text_was_filtered(original: str, cleaned: str) -> bool:
    return (original or "") != (cleaned or "")


async def reply(msg: Message, text: str) -> None:
    await msg.reply_text(text)


async def send_cleaned_to_target(context: ContextTypes.DEFAULT_TYPE, msg: Message) -> str:
    """Send msg content to target with word filter. Returns ok|skip_empty."""
    words = load_words()
    original = msg.text or msg.caption or ""
    cleaned = remove_blocked_words(original, words)
    has_media = bool(
        msg.photo
        or msg.video
        or msg.document
        or msg.audio
        or msg.voice
        or msg.animation
        or msg.sticker
        or msg.video_note
    )
    if not cleaned and not has_media:
        return "skip_empty"

    if msg.photo:
        await context.bot.send_photo(
            chat_id=TARGET_CHAT_ID,
            photo=msg.photo[-1].file_id,
            caption=cleaned or None,
        )
    elif msg.video:
        await context.bot.send_video(
            chat_id=TARGET_CHAT_ID,
            video=msg.video.file_id,
            caption=cleaned or None,
        )
    elif msg.document:
        await context.bot.send_document(
            chat_id=TARGET_CHAT_ID,
            document=msg.document.file_id,
            caption=cleaned or None,
        )
    elif msg.audio:
        await context.bot.send_audio(
            chat_id=TARGET_CHAT_ID,
            audio=msg.audio.file_id,
            caption=cleaned or None,
        )
    elif msg.voice:
        await context.bot.send_voice(
            chat_id=TARGET_CHAT_ID,
            voice=msg.voice.file_id,
            caption=cleaned or None,
        )
    elif msg.animation:
        await context.bot.send_animation(
            chat_id=TARGET_CHAT_ID,
            animation=msg.animation.file_id,
            caption=cleaned or None,
        )
    elif msg.sticker:
        await context.bot.send_sticker(
            chat_id=TARGET_CHAT_ID, sticker=msg.sticker.file_id
        )
    elif msg.video_note:
        await context.bot.send_video_note(
            chat_id=TARGET_CHAT_ID, video_note=msg.video_note.file_id
        )
    elif cleaned:
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=cleaned)
    else:
        return "skip_empty"

    if text_was_filtered(original, cleaned):
        return "ok_filtered"
    return "ok"


async def channel_auto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.id != SOURCE_CHAT_ID:
        return
    try:
        status = await send_cleaned_to_target(context, msg)
        if status.startswith("ok"):
            logger.info("Auto-forwarded msg %s (%s)", msg.message_id, status)
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
        if status.startswith("ok"):
            extra = " (words filtered)" if status == "ok_filtered" else ""
            await reply(msg, f"Done! Hmm pe chala gaya.{extra}")
        else:
            await reply(msg, "Empty after filter / no media — skip.")
    except Exception as e:
        await reply(msg, f"Fail: {e}")


async def bulk_one_message(context: ContextTypes.DEFAULT_TYPE, mid: int) -> str:
    """
    One message bulk with WORD FILTER:
    1) forward source→target (API returns full Message with text/media)
    2) delete that forward (so 'Forwarded from' header na rahe)
    3) re-send cleaned content
    Returns: ok | ok_filtered | skip | fail
    """
    try:
        # forwardMessage returns full Message — isse text/caption milta hai
        fwd = await context.bot.forward_message(
            chat_id=TARGET_CHAT_ID,
            from_chat_id=SOURCE_CHAT_ID,
            message_id=mid,
        )
    except RetryAfter as e:
        await asyncio.sleep(int(e.retry_after) + 1)
        try:
            fwd = await context.bot.forward_message(
                chat_id=TARGET_CHAT_ID,
                from_chat_id=SOURCE_CHAT_ID,
                message_id=mid,
            )
        except TelegramError as e2:
            err = str(e2).lower()
            if "not found" in err or "message" in err and "not found" in err:
                return "skip"
            return "fail"
    except TelegramError as e:
        err = str(e).lower()
        if "not found" in err or "message to forward not found" in err:
            return "skip"
        if "protected" in err:
            # forward forbidden — try plain copy (no filter)
            try:
                await context.bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=mid,
                )
                return "ok"
            except TelegramError:
                return "fail"
        return "fail"
    except Exception:
        return "fail"

    # Delete the raw forward (header wala)
    try:
        await context.bot.delete_message(
            chat_id=TARGET_CHAT_ID, message_id=fwd.message_id
        )
    except TelegramError:
        # delete fail ho to bhi cleaned dubara bhej denge (duplicate risk)
        logger.warning("Could not delete temp forward id=%s", fwd.message_id)

    # Re-send with word filter
    try:
        status = await send_cleaned_to_target(context, fwd)
        if status == "skip_empty":
            return "skip"
        if status == "ok_filtered":
            return "ok_filtered"
        return "ok"
    except RetryAfter as e:
        await asyncio.sleep(int(e.retry_after) + 1)
        try:
            status = await send_cleaned_to_target(context, fwd)
            if status == "skip_empty":
                return "skip"
            return "ok_filtered" if status == "ok_filtered" else "ok"
        except Exception:
            return "fail"
    except Exception:
        logger.exception("bulk resend fail id=%s", mid)
        return "fail"


async def run_bulk(
    context: ContextTypes.DEFAULT_TYPE,
    admin_chat_id: int,
    from_id: int,
    to_id: int,
    delay: float,
) -> None:
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
            "filtered": 0,
        }
    )
    total = to_id - from_id + 1
    words = load_words()
    logger.info(
        "BULK+FILTER start %s→%s total~%s delay=%s words=%s",
        from_id,
        to_id,
        total,
        delay,
        len(words),
    )

    await context.bot.send_message(
        admin_chat_id,
        f"BULK START (with WORD BLOCK)\n"
        f"Range: {from_id} → {to_id} (~{total} ids)\n"
        f"Blocked words: {len(words)}\n"
        f"{', '.join(words[:20]) or '(none — /block se add karo)'}\n"
        f"Delay: {delay}s\n"
        f"ETA rough: {int(total * delay / 60)}+ min\n\n"
        f"/bulkstatus  |  /bulkstop\n\n"
        f"Har message: filter → Hmm pe clean post\n"
        f"Missing IDs skip.",
    )

    last_progress = 0
    try:
        for mid in range(from_id, to_id + 1):
            if bulk_stop.is_set():
                break

            bulk_status["current"] = mid
            result = await bulk_one_message(context, mid)

            if result == "ok":
                bulk_status["ok"] += 1
            elif result == "ok_filtered":
                bulk_status["ok"] += 1
                bulk_status["filtered"] += 1
            elif result == "skip":
                bulk_status["skip"] += 1
            else:
                bulk_status["fail"] += 1

            done = (
                bulk_status["ok"]
                + bulk_status["skip"]
                + bulk_status["fail"]
            )
            if done - last_progress >= 50 or mid == to_id:
                last_progress = done
                pct = int((mid - from_id + 1) / total * 100)
                try:
                    await context.bot.send_message(
                        admin_chat_id,
                        f"Progress {pct}%\n"
                        f"ID: {mid}/{to_id}\n"
                        f"OK: {bulk_status['ok']} "
                        f"(filtered {bulk_status['filtered']})\n"
                        f"Skip: {bulk_status['skip']} | Fail: {bulk_status['fail']}",
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
            f"OK: {bulk_status['ok']}\n"
            f"Word-filtered: {bulk_status['filtered']}\n"
            f"Skip: {bulk_status['skip']}\n"
            f"Fail: {bulk_status['fail']}\n"
            f"Last ID: {bulk_status['current']}",
        )
        logger.info("BULK done %s", bulk_status)


async def cmd_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            "BULK + WORD BLOCK\n\n"
            "Usage:\n"
            "/bulk FROM_ID TO_ID\n"
            "/bulk FROM_ID TO_ID DELAY\n\n"
            "Example:\n"
            "/bulk 1 100\n"
            "/bulk 1 12500 0.25\n\n"
            "Message ID: post link t.me/c/xxxx/ID\n\n"
            "Words: /listblocks  |  /block word\n"
            "Railway pe permanent words ke liye variable:\n"
            "BLOCKED_WORDS=spam,scam,fraud\n\n"
            "/bulkstatus  /bulkstop\n"
            "Default delay 0.2s (filter mode thoda slower)",
        )
        return

    try:
        from_id = int(context.args[0])
        to_id = int(context.args[1])
        delay = float(context.args[2]) if len(context.args) >= 3 else 0.2
    except ValueError:
        await reply(msg, "IDs numbers hone chahiye. Example: /bulk 1 12500")
        return

    if from_id < 1 or to_id < from_id:
        await reply(msg, "FROM >= 1 aur TO >= FROM hona chahiye.")
        return
    if to_id - from_id > 200000:
        await reply(msg, "Range bahut badi (max 200000).")
        return
    delay = min(max(delay, 0.08), 5.0)

    bulk_stop.clear()
    bulk_task = asyncio.create_task(
        run_bulk(context, user.id, from_id, to_id, delay)
    )
    await reply(
        msg,
        f"Bulk+Filter start: {from_id} → {to_id}\n"
        f"Words loaded: {len(load_words())}\n"
        f"/bulkstatus se dekho.",
    )


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
    await reply(msg, "Stop signal — thodi der me rukegi.")


async def cmd_bulkstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        if msg:
            await reply(msg, "Sirf admin.")
        return
    s = bulk_status
    if not s["running"] and s["ok"] == 0 and s["current"] == 0:
        await reply(msg, "Abhi koi bulk job nahi.")
        return
    total = max(1, s["to_id"] - s["from_id"] + 1)
    pct = (
        int((s["current"] - s["from_id"] + 1) / total * 100) if s["to_id"] else 0
    )
    await reply(
        msg,
        f"Bulk {'RUNNING' if s['running'] else 'IDLE'}\n"
        f"Range: {s['from_id']} → {s['to_id']}\n"
        f"Current: {s['current']} ({pct}%)\n"
        f"OK: {s['ok']} (word-filtered: {s.get('filtered', 0)})\n"
        f"Skip: {s['skip']} | Fail: {s['fail']}",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    await reply(
        msg,
        "Forward Bot + Word Block + BULK\n\n"
        "=== BULK (purane + word block) ===\n"
        "/bulk 1 12500\n"
        "/bulkstatus | /bulkstop\n\n"
        "=== Words ===\n"
        "/block word | /unblock word\n"
        "/listblocks\n\n"
        "=== Naye posts === auto + word block\n"
        "=== Ek post === Forward → is bot pe\n\n"
        f"/status /test\n"
        f"Admin: {'YES' if user and is_admin(user.id) else 'NO'}\n"
        f"Words now: {len(load_words())}",
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
    """Single ID with word filter (same as bulk one)."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args or not context.args[0].isdigit():
        await reply(msg, "Usage: /copy 36  (word filter ke saath)")
        return
    mid = int(context.args[0])
    result = await bulk_one_message(context, mid)
    if result in ("ok", "ok_filtered"):
        await reply(
            msg,
            f"Copied {mid}"
            + (" (filtered)" if result == "ok_filtered" else ""),
        )
    elif result == "skip":
        await reply(msg, f"Skip {mid} (not found / empty after filter)")
    else:
        await reply(msg, f"Fail {mid}")


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
    # save only file words (not env); merge display via load_words
    file_words: list[str] = []
    if WORDS_FILE.exists():
        try:
            file_words = [
                w.lower().strip()
                for w in json.loads(
                    WORDS_FILE.read_text(encoding="utf-8")
                ).get("words", [])
                if w.strip()
            ]
        except (json.JSONDecodeError, OSError):
            file_words = []
    if word in file_words or word in ENV_WORDS:
        await reply(msg, "Pehle se blocked.")
        return
    file_words.append(word)
    save_words(file_words)
    await reply(
        msg,
        f"Blocked: {word}\n"
        f"Total now: {len(load_words())}\n"
        f"Note: Railway pe /block restart pe reset ho sakta hai.\n"
        f"Permanent: Variables me BLOCKED_WORDS=word1,word2",
    )


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
    if word in ENV_WORDS:
        await reply(
            msg,
            f"'{word}' ENV (BLOCKED_WORDS) se aaya hai — "
            f"Railway Variables se hatao.",
        )
        return
    file_words: list[str] = []
    if WORDS_FILE.exists():
        try:
            file_words = [
                w.lower().strip()
                for w in json.loads(
                    WORDS_FILE.read_text(encoding="utf-8")
                ).get("words", [])
                if w.strip()
            ]
        except (json.JSONDecodeError, OSError):
            file_words = []
    if word not in file_words:
        await reply(msg, "List me nahi.")
        return
    save_words([w for w in file_words if w != word])
    await reply(msg, f"Unblocked: {word}")


async def cmd_listblocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    words = load_words()
    await reply(
        msg,
        "Blocked words:\n"
        + ("\n".join(f"- {w}" for w in words) if words else "(empty)"),
    )


async def cmd_clearblocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    save_words([])
    extra = (
        f"\nENV words still active: {', '.join(ENV_WORDS)}"
        if ENV_WORDS
        else ""
    )
    await reply(msg, "File words cleared." + extra)


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
        f"/bulk 1 LAST_ID  (word block ON)",
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

    logger.info(
        "Bot starting bulk+filter source=%s target=%s words=%s",
        SOURCE_CHAT_ID,
        TARGET_CHAT_ID,
        len(load_words()),
    )
    app.run_polling(
        allowed_updates=[
            "message",
            "channel_post",
            "edited_channel_post",
            "edited_message",
        ],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
