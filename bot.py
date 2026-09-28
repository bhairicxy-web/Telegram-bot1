#!/usr/bin/env python3
"""
Telegram Forward Bot
- Auto forward + bulk
- Word REMOVE / REPLACE
- Caption header/footer + FTM-style TEMPLATE
  {filename} {size} {caption} {year} {quality} {type} {language}
- HTML formatting in template
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
from telegram.constants import ParseMode
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

ENV_BLOCK = [
    w.lower().strip()
    for w in os.getenv("BLOCKED_WORDS", "").split(",")
    if w.strip()
]

ENV_REPLACE = {}
for part in os.getenv("REPLACE_WORDS", "").split("|"):
    part = part.strip()
    if not part or ":" not in part:
        continue
    old, new = part.split(":", 1)
    old = old.lower().strip()
    if old:
        ENV_REPLACE[old] = new

ENV_CAP_HEADER = os.getenv("CAPTION_HEADER", "")
ENV_CAP_FOOTER = os.getenv("CAPTION_FOOTER", "")
ENV_CAP_REPLACE = os.getenv("CAPTION_REPLACE", "")
ENV_CAP_TEMPLATE = os.getenv("CAPTION_TEMPLATE", "")
ENV_CAP_REMOVE = os.getenv("CAPTION_REMOVE_ORIGINAL", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

DATA_FILE = Path(__file__).parent / "filter_data.json"
OLD_WORDS_FILE = Path(__file__).parent / "blocked_words.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("forward-bot")

bulk_task = None
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

# Telegram caption limit
CAP_LIMIT = 1024


def default_data():
    return {
        "remove": ["spam", "scam", "fraud"],
        "replace": {},
        "caption": {
            "header": "",
            "footer": "",
            "replace_all": "",
            "template": "",
            "remove_original": False,
            "html": True,
        },
    }


def load_data():
    data = default_data()
    if DATA_FILE.exists():
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(raw.get("remove"), list):
                data["remove"] = [
                    str(w).lower().strip() for w in raw["remove"] if str(w).strip()
                ]
            if isinstance(raw.get("replace"), dict):
                data["replace"] = {
                    str(k).lower().strip(): str(v)
                    for k, v in raw["replace"].items()
                    if str(k).strip()
                }
            if isinstance(raw.get("caption"), dict):
                c = raw["caption"]
                for key in (
                    "header",
                    "footer",
                    "replace_all",
                    "template",
                ):
                    data["caption"][key] = str(c.get(key, "") or "")
                data["caption"]["remove_original"] = bool(
                    c.get("remove_original", False)
                )
                if "html" in c:
                    data["caption"]["html"] = bool(c.get("html", True))
        except Exception as e:
            logger.error("filter_data read error: %s", e)
    elif OLD_WORDS_FILE.exists():
        try:
            raw = json.loads(OLD_WORDS_FILE.read_text(encoding="utf-8"))
            data["remove"] = [
                str(w).lower().strip()
                for w in raw.get("words", [])
                if str(w).strip()
            ]
        except Exception:
            pass
    return data


def save_data(data):
    cap = data.get("caption") or {}
    clean = {
        "remove": sorted(
            {w.lower().strip() for w in data.get("remove", []) if w.strip()}
        ),
        "replace": {
            str(k).lower().strip(): str(v)
            for k, v in data.get("replace", {}).items()
            if str(k).strip()
        },
        "caption": {
            "header": str(cap.get("header", "") or ""),
            "footer": str(cap.get("footer", "") or ""),
            "replace_all": str(cap.get("replace_all", "") or ""),
            "template": str(cap.get("template", "") or ""),
            "remove_original": bool(cap.get("remove_original", False)),
            "html": bool(cap.get("html", True)),
        },
    }
    DATA_FILE.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_remove_words():
    data = load_data()
    words = list(data.get("remove", [])) + list(ENV_BLOCK)
    seen = set()
    out = []
    for w in words:
        w = w.lower().strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def get_replace_map():
    data = load_data()
    merged = dict(ENV_REPLACE)
    merged.update(data.get("replace", {}))
    return merged


def get_caption_settings():
    data = load_data()
    c = data.get("caption") or {}
    return {
        "header": c.get("header") or ENV_CAP_HEADER or "",
        "footer": c.get("footer") or ENV_CAP_FOOTER or "",
        "replace_all": c.get("replace_all") or ENV_CAP_REPLACE or "",
        "template": c.get("template") or ENV_CAP_TEMPLATE or "",
        "remove_original": bool(c.get("remove_original")) or ENV_CAP_REMOVE,
        "html": bool(c.get("html", True)),
    }


def is_admin(user_id):
    return user_id is not None and user_id in ADMIN_IDS


def apply_word_filters(text):
    if not text:
        return text, False
    original = text
    cleaned = text
    rep = get_replace_map()
    for old in sorted(rep.keys(), key=len, reverse=True):
        cleaned = re.compile(re.escape(old), re.IGNORECASE).sub(rep[old], cleaned)
    for word in get_remove_words():
        if word in rep:
            continue
        cleaned = re.compile(re.escape(word), re.IGNORECASE).sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    return cleaned, cleaned != (original or "").strip()


def format_size(num_bytes):
    if not num_bytes or num_bytes < 0:
        return ""
    num = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            if unit == "B":
                return "%d %s" % (int(num), unit)
            return "%.2f %s" % (num, unit)
        num /= 1024.0
    return str(num_bytes)


def detect_quality(name):
    if not name:
        return ""
    n = name.lower()
    for q in (
        "2160p",
        "1440p",
        "1080p",
        "720p",
        "480p",
        "360p",
        "240p",
        "4k",
        "2k",
        "hd",
        "sd",
    ):
        if q in n:
            return q.upper() if q == "hd" or q == "sd" or q == "4k" or q == "2k" else q
    return ""


def detect_year(name):
    if not name:
        return ""
    # prefer (2019) or .2019. or _2019_
    m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", name)
    if m:
        return m.group(1)
    return ""


def detect_language(name):
    if not name:
        return ""
    n = name.lower()
    langs = [
        ("hindi", "Hindi"),
        ("english", "English"),
        ("tamil", "Tamil"),
        ("telugu", "Telugu"),
        ("malayalam", "Malayalam"),
        ("kannada", "Kannada"),
        ("bengali", "Bengali"),
        ("marathi", "Marathi"),
        ("dual audio", "Dual Audio"),
        ("multi audio", "Multi Audio"),
        ("hin", "Hindi"),
        ("eng", "English"),
    ]
    found = []
    for key, label in langs:
        if key in n and label not in found:
            found.append(label)
    return ", ".join(found)


def get_media_meta(msg: Message):
    """Extract filename, size, type, quality, year, language from message."""
    meta = {
        "filename": "",
        "size": "",
        "size_bytes": 0,
        "type": "PHOTO",
        "quality": "",
        "year": "",
        "language": "",
        "has_file_media": False,
    }

    if msg.document:
        meta["has_file_media"] = True
        meta["type"] = "DOCUMENT"
        meta["filename"] = msg.document.file_name or "document"
        meta["size_bytes"] = msg.document.file_size or 0
        # mime hint
        mt = (msg.document.mime_type or "").lower()
        if mt.startswith("video/"):
            meta["type"] = "VIDEO"
        elif mt.startswith("audio/"):
            meta["type"] = "AUDIO"
    elif msg.video:
        meta["has_file_media"] = True
        meta["type"] = "VIDEO"
        meta["filename"] = msg.video.file_name or "video.mp4"
        meta["size_bytes"] = msg.video.file_size or 0
        if msg.video.height:
            h = msg.video.height
            if h >= 2100:
                meta["quality"] = "2160p"
            elif h >= 1400:
                meta["quality"] = "1440p"
            elif h >= 1000:
                meta["quality"] = "1080p"
            elif h >= 700:
                meta["quality"] = "720p"
            elif h >= 450:
                meta["quality"] = "480p"
            else:
                meta["quality"] = "%dp" % h
    elif msg.audio:
        meta["has_file_media"] = True
        meta["type"] = "AUDIO"
        meta["filename"] = msg.audio.file_name or (
            (msg.audio.title or "audio") + ".mp3"
        )
        meta["size_bytes"] = msg.audio.file_size or 0
    elif msg.animation:
        meta["has_file_media"] = True
        meta["type"] = "VIDEO"
        meta["filename"] = msg.animation.file_name or "animation.mp4"
        meta["size_bytes"] = msg.animation.file_size or 0
    elif msg.voice:
        meta["has_file_media"] = True
        meta["type"] = "AUDIO"
        meta["filename"] = "voice.ogg"
        meta["size_bytes"] = msg.voice.file_size or 0
    elif msg.video_note:
        meta["has_file_media"] = True
        meta["type"] = "VIDEO"
        meta["filename"] = "video_note.mp4"
        meta["size_bytes"] = msg.video_note.file_size or 0
    elif msg.photo:
        meta["type"] = "PHOTO"
        meta["filename"] = "photo.jpg"
        meta["size_bytes"] = msg.photo[-1].file_size or 0
        meta["has_file_media"] = False  # FTM: template vars mainly for file media
    elif msg.sticker:
        meta["type"] = "STICKER"
        meta["filename"] = "sticker.webp"

    meta["size"] = format_size(meta["size_bytes"])
    name = meta["filename"]
    if not meta["quality"]:
        meta["quality"] = detect_quality(name)
    meta["year"] = detect_year(name)
    meta["language"] = detect_language(name)
    return meta


def html_escape(text):
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_template(template, variables, use_html):
    """
    Replace {filename} {size} {caption} {year} {quality} {type} {language}
    User may include HTML tags in template.
    Variable values are escaped so they don't break HTML.
    """
    out = template
    # support both {var} and {VAR}
    for key, val in variables.items():
        safe = html_escape(str(val)) if use_html else str(val)
        out = out.replace("{" + key + "}", safe)
        out = out.replace("{" + key.upper() + "}", safe)
        out = out.replace("{" + key.lower() + "}", safe)
    # leftover unknown {x} leave as-is or blank
    return out


def build_final_caption(msg: Message, original_text: str):
    """
    Full caption pipeline:
    1) word filter on original caption/text
    2) if template set AND media is video/audio/document -> use template
       (photos keep filtered original unless replace_all/header/footer)
    3) replace_all / remove_original
    4) header + footer
    Returns (caption_str, changed_bool, parse_mode_or_None)
    """
    cap = get_caption_settings()
    filtered, changed_words = apply_word_filters(original_text or "")
    meta = get_media_meta(msg)

    body = filtered
    used_template = False
    use_html = bool(cap.get("html", True))

    # FTM-like: template works on videos, audio, documents
    if cap["template"] and meta["has_file_media"]:
        variables = {
            "filename": meta["filename"],
            "size": meta["size"],
            "caption": filtered,
            "year": meta["year"],
            "quality": meta["quality"],
            "type": meta["type"],
            "language": meta["language"],
        }
        body = render_template(cap["template"], variables, use_html)
        used_template = True
        changed_words = True
    elif cap["replace_all"]:
        body = cap["replace_all"]
        changed_words = True
    elif cap["remove_original"]:
        body = ""
        changed_words = True

    parts = []
    if cap["header"]:
        parts.append(cap["header"])
    if body:
        parts.append(body)
    if cap["footer"]:
        parts.append(cap["footer"])

    out = "\n\n".join(parts).strip()
    if len(out) > CAP_LIMIT:
        out = out[: CAP_LIMIT - 3] + "..."

    parse_mode = None
    # enable HTML if template used or header/footer/replace contain tags
    joined_check = (cap["header"] or "") + (cap["footer"] or "") + (
        cap["replace_all"] or ""
    ) + (cap["template"] or "")
    if use_html and (
        used_template
        or "<b>" in joined_check
        or "<i>" in joined_check
        or "<a " in joined_check
        or "<code>" in joined_check
        or "<u>" in joined_check
        or "<s>" in joined_check
        or "<spoiler>" in joined_check
    ):
        parse_mode = ParseMode.HTML

    changed = changed_words or (out != (original_text or "").strip())
    return out, changed, parse_mode


async def reply(msg, text):
    await msg.reply_text(text)


async def send_cleaned_to_target(context, msg):
    original = msg.text or msg.caption or ""
    cleaned, changed, parse_mode = build_final_caption(msg, original)

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

    kwargs = {"parse_mode": parse_mode} if parse_mode else {}

    try:
        if not has_media:
            if cleaned:
                await context.bot.send_message(
                    chat_id=TARGET_CHAT_ID, text=cleaned, **kwargs
                )
                return "ok_filtered" if changed else "ok"
            return "skip_empty"

        caption = cleaned or None
        if msg.photo:
            # FTM note: photos keep original-style caption (our filters still apply)
            await context.bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=msg.photo[-1].file_id,
                caption=caption,
                **kwargs,
            )
        elif msg.video:
            await context.bot.send_video(
                chat_id=TARGET_CHAT_ID,
                video=msg.video.file_id,
                caption=caption,
                **kwargs,
            )
        elif msg.document:
            await context.bot.send_document(
                chat_id=TARGET_CHAT_ID,
                document=msg.document.file_id,
                caption=caption,
                **kwargs,
            )
        elif msg.audio:
            await context.bot.send_audio(
                chat_id=TARGET_CHAT_ID,
                audio=msg.audio.file_id,
                caption=caption,
                **kwargs,
            )
        elif msg.voice:
            await context.bot.send_voice(
                chat_id=TARGET_CHAT_ID,
                voice=msg.voice.file_id,
                caption=caption,
                **kwargs,
            )
        elif msg.animation:
            await context.bot.send_animation(
                chat_id=TARGET_CHAT_ID,
                animation=msg.animation.file_id,
                caption=caption,
                **kwargs,
            )
        elif msg.sticker:
            await context.bot.send_sticker(
                chat_id=TARGET_CHAT_ID,
                sticker=msg.sticker.file_id,
            )
        elif msg.video_note:
            await context.bot.send_video_note(
                chat_id=TARGET_CHAT_ID,
                video_note=msg.video_note.file_id,
            )
        else:
            return "skip_empty"
    except TelegramError as e:
        # if HTML broken, retry plain
        if parse_mode and "parse" in str(e).lower():
            logger.warning("HTML parse fail, retry plain: %s", e)
            return await send_cleaned_plain(context, msg, cleaned, changed)
        raise

    return "ok_filtered" if changed else "ok"


async def send_cleaned_plain(context, msg, cleaned, changed):
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
    caption = cleaned or None
    if not has_media:
        if cleaned:
            await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=cleaned)
            return "ok_filtered" if changed else "ok"
        return "skip_empty"
    if msg.photo:
        await context.bot.send_photo(
            chat_id=TARGET_CHAT_ID, photo=msg.photo[-1].file_id, caption=caption
        )
    elif msg.video:
        await context.bot.send_video(
            chat_id=TARGET_CHAT_ID, video=msg.video.file_id, caption=caption
        )
    elif msg.document:
        await context.bot.send_document(
            chat_id=TARGET_CHAT_ID, document=msg.document.file_id, caption=caption
        )
    elif msg.audio:
        await context.bot.send_audio(
            chat_id=TARGET_CHAT_ID, audio=msg.audio.file_id, caption=caption
        )
    elif msg.voice:
        await context.bot.send_voice(
            chat_id=TARGET_CHAT_ID, voice=msg.voice.file_id, caption=caption
        )
    elif msg.animation:
        await context.bot.send_animation(
            chat_id=TARGET_CHAT_ID, animation=msg.animation.file_id, caption=caption
        )
    elif msg.sticker:
        await context.bot.send_sticker(
            chat_id=TARGET_CHAT_ID, sticker=msg.sticker.file_id
        )
    elif msg.video_note:
        await context.bot.send_video_note(
            chat_id=TARGET_CHAT_ID, video_note=msg.video_note.file_id
        )
    return "ok_filtered" if changed else "ok"


async def channel_auto_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.id != SOURCE_CHAT_ID:
        return
    try:
        status = await send_cleaned_to_target(context, msg)
        if status.startswith("ok"):
            logger.info("Auto-forwarded %s %s", msg.message_id, status)
    except Exception:
        logger.exception("Auto forward failed")


async def manual_forward_handler(update, context):
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
            extra = " (filtered)" if status == "ok_filtered" else ""
            await reply(msg, "Done! Target pe chala gaya." + extra)
        else:
            await reply(msg, "Empty after filter / no media.")
    except Exception as e:
        await reply(msg, "Fail: " + str(e))


async def bulk_one_message(context, mid):
    try:
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
        except TelegramError:
            return "skip"
    except TelegramError as e:
        err = str(e).lower()
        if "not found" in err:
            return "skip"
        if "protected" in err:
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

    try:
        await context.bot.delete_message(
            chat_id=TARGET_CHAT_ID, message_id=fwd.message_id
        )
    except TelegramError:
        pass

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
            if status == "ok_filtered":
                return "ok_filtered"
            return "ok"
        except Exception:
            return "fail"
    except Exception:
        return "fail"


async def run_bulk(context, admin_chat_id, from_id, to_id, delay):
    global bulk_status
    bulk_stop.clear()
    bulk_status = {
        "running": True,
        "from_id": from_id,
        "to_id": to_id,
        "current": from_id,
        "ok": 0,
        "fail": 0,
        "skip": 0,
        "filtered": 0,
    }
    total = to_id - from_id + 1
    await context.bot.send_message(
        admin_chat_id,
        "BULK START\nRange: %s -> %s (~%s)\nDelay: %ss\n/bulkstatus | /bulkstop"
        % (from_id, to_id, total, delay),
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
            done = bulk_status["ok"] + bulk_status["skip"] + bulk_status["fail"]
            if done - last_progress >= 50 or mid == to_id:
                last_progress = done
                pct = int((mid - from_id + 1) / total * 100)
                try:
                    await context.bot.send_message(
                        admin_chat_id,
                        "Progress %s%% | ID %s/%s\nOK %s filtered %s | Skip %s Fail %s"
                        % (
                            pct,
                            mid,
                            to_id,
                            bulk_status["ok"],
                            bulk_status["filtered"],
                            bulk_status["skip"],
                            bulk_status["fail"],
                        ),
                    )
                except Exception:
                    pass
            await asyncio.sleep(delay)
    finally:
        bulk_status["running"] = False
        title = "STOPPED" if bulk_stop.is_set() else "FINISHED"
        await context.bot.send_message(
            admin_chat_id,
            "BULK %s\nOK %s | Filtered %s\nSkip %s | Fail %s\nLast %s"
            % (
                title,
                bulk_status["ok"],
                bulk_status["filtered"],
                bulk_status["skip"],
                bulk_status["fail"],
                bulk_status["current"],
            ),
        )


async def cmd_bulk(update, context):
    global bulk_task
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        await reply(msg, "Sirf admin.")
        return
    if bulk_status["running"]:
        await reply(msg, "Bulk running. /bulkstatus /bulkstop")
        return
    if not context.args or len(context.args) < 2:
        await reply(msg, "Usage: /bulk FROM TO\n/bulk 1 13365\n/copy 13365")
        return
    try:
        from_id = int(context.args[0])
        to_id = int(context.args[1])
        delay = float(context.args[2]) if len(context.args) >= 3 else 0.2
    except ValueError:
        await reply(msg, "Numbers only.")
        return
    if from_id < 1 or to_id < from_id or to_id - from_id > 200000:
        await reply(msg, "Invalid range.")
        return
    delay = min(max(delay, 0.08), 5.0)
    bulk_stop.clear()
    bulk_task = asyncio.create_task(
        run_bulk(context, user.id, from_id, to_id, delay)
    )
    await reply(msg, "Bulk start %s -> %s" % (from_id, to_id))


async def cmd_bulkstop(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        if msg:
            await reply(msg, "Sirf admin.")
        return
    if not bulk_status["running"]:
        await reply(msg, "No bulk.")
        return
    bulk_stop.set()
    await reply(msg, "Stop signal sent.")


async def cmd_bulkstatus(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        if msg:
            await reply(msg, "Sirf admin.")
        return
    s = bulk_status
    if not s["running"] and s["ok"] == 0 and s["current"] == 0:
        await reply(msg, "No bulk job.")
        return
    total = max(1, s["to_id"] - s["from_id"] + 1)
    pct = int((s["current"] - s["from_id"] + 1) / total * 100) if s["to_id"] else 0
    await reply(
        msg,
        "Bulk %s\n%s->%s at %s (%s%%)\nOK %s filtered %s skip %s fail %s"
        % (
            "RUNNING" if s["running"] else "IDLE",
            s["from_id"],
            s["to_id"],
            s["current"],
            pct,
            s["ok"],
            s.get("filtered", 0),
            s["skip"],
            s["fail"],
        ),
    )


async def cmd_start(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    admin = "YES" if user and is_admin(user.id) else "NO"
    await reply(
        msg,
        "Forward Bot + Caption Template\n\n"
        "=== CAPTION TEMPLATE (FTM style) ===\n"
        "/caption                 show settings\n"
        "/captiontemplate         set multi-line template\n"
        "  (reply to a message with template text)\n"
        "  OR /captiontemplate <b>{filename}</b>\n"
        "/captionheader TEXT\n"
        "/captionfooter TEXT\n"
        "/captionreplace TEXT\n"
        "/captionremove on|off\n"
        "/captionclear\n\n"
        "Variables (video/audio/document):\n"
        "{filename} {size} {caption}\n"
        "{year} {quality} {type} {language}\n\n"
        "HTML: <b> <i> <u> <s> <code> <spoiler>\n"
        "<a href='url'>text</a>\n\n"
        "=== WORDS ===\n"
        "/block /replace /listfilters\n\n"
        "=== BULK ===\n"
        "/bulk 1 13365 | /copy 13365\n\n"
        "/status /test\nAdmin: %s" % admin,
    )


async def cmd_help(update, context):
    await cmd_start(update, context)


async def cmd_test(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Sirf admin.")
        return
    try:
        # fake meta demo via process on plain text
        sample = "Original spam caption"
        filtered, _ = apply_word_filters(sample)
        cap = get_caption_settings()
        demo = filtered
        if cap["template"]:
            demo = render_template(
                cap["template"],
                {
                    "filename": "Movie.2024.1080p.Hindi.mp4",
                    "size": "1.25 GB",
                    "caption": filtered,
                    "year": "2024",
                    "quality": "1080p",
                    "type": "VIDEO",
                    "language": "Hindi",
                },
                True,
            )
        parts = []
        if cap["header"]:
            parts.append(cap["header"])
        if cap["replace_all"]:
            parts.append(cap["replace_all"])
        elif demo:
            parts.append(demo)
        if cap["footer"]:
            parts.append(cap["footer"])
        out = "\n\n".join(parts) or "(empty)"
        sent = await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="Bot test OK\n\nDEMO OUT:\n" + out,
            parse_mode=ParseMode.HTML if "<" in out else None,
        )
        await reply(msg, "Target OK id=%s" % sent.message_id)
    except Exception as e:
        # retry plain
        try:
            sent = await context.bot.send_message(
                chat_id=TARGET_CHAT_ID, text="Bot test OK (plain)."
            )
            await reply(msg, "Target OK id=%s (html fail: %s)" % (sent.message_id, e))
        except Exception as e2:
            await reply(msg, "Fail: " + str(e2))


async def cmd_copy(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args or not context.args[0].isdigit():
        await reply(msg, "Usage: /copy 13365")
        return
    mid = int(context.args[0])
    result = await bulk_one_message(context, mid)
    if result in ("ok", "ok_filtered"):
        await reply(
            msg,
            "Copied %s%s" % (mid, " (filtered)" if result == "ok_filtered" else ""),
        )
    elif result == "skip":
        await reply(msg, "Skip %s" % mid)
    else:
        await reply(msg, "Fail %s" % mid)


async def cmd_block(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args:
        await reply(msg, "Usage: /block spam")
        return
    word = " ".join(context.args).strip().lower()
    data = load_data()
    if word in ENV_BLOCK or word in data["remove"]:
        await reply(msg, "Already blocked.")
        return
    data["replace"].pop(word, None)
    data["remove"].append(word)
    save_data(data)
    await reply(msg, "REMOVE: " + word)


async def cmd_replace(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args or len(context.args) < 2:
        await reply(msg, "Usage: /replace spam ***")
        return
    old = context.args[0].strip().lower()
    new = " ".join(context.args[1:])
    data = load_data()
    data["remove"] = [w for w in data.get("remove", []) if w != old]
    data.setdefault("replace", {})[old] = new
    save_data(data)
    await reply(msg, "REPLACE: %s => %s" % (old, new))


async def cmd_unreplace(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args:
        await reply(msg, "Usage: /unreplace spam")
        return
    word = context.args[0].strip().lower()
    data = load_data()
    if word not in data.get("replace", {}):
        await reply(msg, "Not found.")
        return
    data["replace"].pop(word, None)
    save_data(data)
    await reply(msg, "Removed replace: " + word)


async def cmd_unblock(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    if not context.args:
        await reply(msg, "Usage: /unblock spam")
        return
    word = " ".join(context.args).strip().lower()
    data = load_data()
    if word not in data.get("remove", []):
        await reply(msg, "Not found.")
        return
    data["remove"] = [w for w in data["remove"] if w != word]
    save_data(data)
    await reply(msg, "Unblocked: " + word)


async def cmd_listfilters(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    rem = get_remove_words()
    rep = get_replace_map()
    cap = get_caption_settings()
    lines = ["=== REMOVE ==="]
    lines += ["- " + w for w in rem] if rem else ["(none)"]
    lines += ["", "=== REPLACE ==="]
    if rep:
        for k, v in rep.items():
            lines.append("%s => %s" % (k, v))
    else:
        lines.append("(none)")
    lines += ["", "=== CAPTION ==="]
    lines.append("header: " + (cap["header"] or "off"))
    lines.append("footer: " + (cap["footer"] or "off"))
    lines.append("replace_all: " + (cap["replace_all"] or "off"))
    lines.append("template: " + ("ON" if cap["template"] else "off"))
    if cap["template"]:
        lines.append("--- template ---")
        lines.append(cap["template"][:500])
        lines.append("--- end ---")
    lines.append("remove_original: %s" % cap["remove_original"])
    await reply(msg, "\n".join(lines))


async def cmd_listblocks(update, context):
    await cmd_listfilters(update, context)


async def cmd_clearblocks(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    data = load_data()
    data["remove"] = []
    save_data(data)
    await reply(msg, "Remove list cleared.")


async def cmd_caption(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    cap = get_caption_settings()
    demo = render_template(
        cap["template"]
        or "<b>{filename}</b>\n📦 {size}\n🎬 {quality}\n🗓 {year}\n🗣 {language}\n\n{caption}",
        {
            "filename": "Movie.2024.1080p.Hindi.mp4",
            "size": "1.25 GB",
            "caption": "Original caption here",
            "year": "2024",
            "quality": "1080p",
            "type": "VIDEO",
            "language": "Hindi",
        },
        True,
    )
    await reply(
        msg,
        "CUSTOM CAPTION SETTINGS\n\n"
        "header: %s\nfooter: %s\nreplace_all: %s\n"
        "remove_original: %s\n\n"
        "template:\n%s\n\n"
        "VARIABLES (video/audio/document):\n"
        "{filename} {size} {caption}\n"
        "{year} {quality} {type} {language}\n\n"
        "HTML: <b>bold</b> <i>i</i> <u>u</u> <s>s</s>\n"
        "<code>code</code> <spoiler>x</spoiler>\n"
        "<a href='https://t.me/x'>link</a>\n\n"
        "DEMO:\n%s\n\n"
        "Set template:\n"
        "1) Write template as a message\n"
        "2) Reply that message with /captiontemplate\n"
        "OR one line: /captiontemplate <b>{filename}</b>\n\n"
        "/captionheader /captionfooter /captionclear"
        % (
            cap["header"] or "off",
            cap["footer"] or "off",
            cap["replace_all"] or "off",
            cap["remove_original"],
            cap["template"] or "(not set - using demo default above)",
            demo[:800],
        ),
    )


async def cmd_captiontemplate(update, context):
    """
    Set FTM-style template.
    - Reply to a message: uses that message text as template (multi-line OK)
    - Or args: one-line template
    """
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])

    template = ""
    if msg.reply_to_message and (
        msg.reply_to_message.text or msg.reply_to_message.caption
    ):
        template = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    elif context.args:
        template = " ".join(context.args)
    else:
        await reply(
            msg,
            "Template kaise set karein:\n\n"
            "METHOD 1 (best multi-line):\n"
            "1) Apna template message likho, example:\n"
            "<b>{filename}</b>\n"
            "📦 size: {size}\n"
            "🎬 quality: {quality}\n"
            "🗓 year: {year}\n"
            "🗣 lang: {language}\n\n"
            "{caption}\n\n"
            "2) Us message pe REPLY karke bhejo:\n"
            "/captiontemplate\n\n"
            "METHOD 2 one line:\n"
            "/captiontemplate <b>{filename}</b> | {size}\n\n"
            "Clear: /captiontemplate clear",
        )
        return

    if template.strip().lower() == "clear":
        data["caption"]["template"] = ""
        save_data(data)
        await reply(msg, "Template cleared.")
        return

    data["caption"]["template"] = template
    save_data(data)
    await reply(
        msg,
        "Template saved!\n\n---\n%s\n---\n\n"
        "Test: /caption  or forward a video/document\n"
        "Works on VIDEO / AUDIO / DOCUMENT\n"
        "Photos: normal caption filters only"
        % template[:900],
    )


async def cmd_captionheader(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])
    if not context.args:
        # reply support
        if msg.reply_to_message and msg.reply_to_message.text:
            data["caption"]["header"] = msg.reply_to_message.text
        else:
            data["caption"]["header"] = ""
            save_data(data)
            await reply(msg, "Header cleared.")
            return
    else:
        data["caption"]["header"] = " ".join(context.args)
    save_data(data)
    await reply(msg, "Header set:\n" + data["caption"]["header"])


async def cmd_captionfooter(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])
    if not context.args:
        if msg.reply_to_message and msg.reply_to_message.text:
            data["caption"]["footer"] = msg.reply_to_message.text
        else:
            data["caption"]["footer"] = ""
            save_data(data)
            await reply(msg, "Footer cleared.")
            return
    else:
        data["caption"]["footer"] = " ".join(context.args)
    save_data(data)
    await reply(msg, "Footer set:\n" + data["caption"]["footer"])


async def cmd_captionreplace(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])
    if not context.args:
        if msg.reply_to_message and msg.reply_to_message.text:
            data["caption"]["replace_all"] = msg.reply_to_message.text
        else:
            data["caption"]["replace_all"] = ""
            save_data(data)
            await reply(msg, "replace_all cleared.")
            return
    else:
        data["caption"]["replace_all"] = " ".join(context.args)
    save_data(data)
    await reply(msg, "replace_all set (template off recommended):\n" + data["caption"]["replace_all"])


async def cmd_captionremove(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])
    if not context.args:
        await reply(msg, "Usage: /captionremove on|off")
        return
    on = context.args[0].lower() in ("on", "1", "true", "yes")
    data["caption"]["remove_original"] = on
    save_data(data)
    await reply(msg, "remove_original = %s" % on)


async def cmd_captionclear(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Sirf admin.")
        return
    data = load_data()
    data["caption"] = default_data()["caption"]
    save_data(data)
    await reply(msg, "All caption settings cleared.")


async def cmd_status(update, context):
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
        src = "ERR " + str(e)
    try:
        tgt = (await context.bot.get_chat(TARGET_CHAT_ID)).title or "?"
    except Exception as e:
        tgt = "ERR " + str(e)
    cap = get_caption_settings()
    await reply(
        msg,
        "Source: %s\nTarget: %s\nRemove: %s Replace: %s\n"
        "Template: %s | Header: %s | Footer: %s\nBulk: %s"
        % (
            src,
            tgt,
            len(get_remove_words()),
            len(get_replace_map()),
            "ON" if cap["template"] else "off",
            "ON" if cap["header"] else "off",
            "ON" if cap["footer"] else "off",
            bulk_status["running"],
        ),
    )


async def on_error(update, context):
    logger.exception("Handler error: %s", context.error)


def main():
    if not BOT_TOKEN or not SOURCE_CHAT_ID or not TARGET_CHAT_ID:
        raise SystemExit("Config missing")
    if not DATA_FILE.exists():
        save_data(default_data())

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    for name, fn in [
        ("start", cmd_start),
        ("help", cmd_help),
        ("test", cmd_test),
        ("copy", cmd_copy),
        ("bulk", cmd_bulk),
        ("bulkstop", cmd_bulkstop),
        ("bulkstatus", cmd_bulkstatus),
        ("block", cmd_block),
        ("replace", cmd_replace),
        ("unreplace", cmd_unreplace),
        ("unblock", cmd_unblock),
        ("listfilters", cmd_listfilters),
        ("listblocks", cmd_listblocks),
        ("clearblocks", cmd_clearblocks),
        ("caption", cmd_caption),
        ("captiontemplate", cmd_captiontemplate),
        ("captionheader", cmd_captionheader),
        ("captionfooter", cmd_captionfooter),
        ("captionreplace", cmd_captionreplace),
        ("captionremove", cmd_captionremove),
        ("captionclear", cmd_captionclear),
        ("status", cmd_status),
    ]:
        app.add_handler(CommandHandler(name, fn))

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

    logger.info("Bot start source=%s target=%s", SOURCE_CHAT_ID, TARGET_CHAT_ID)
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
