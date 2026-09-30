#!/usr/bin/env python3
"""
FORWARD BOT - Channel forward with filters, caption, buttons, status panel
Commands styled like popular forward bots: /forward /stop /settings /reset
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "0") or "0")
# Single or multiple targets from env:
# TARGET_CHAT_ID=-100111
# or TARGET_CHAT_IDS=-100111,-100222,-100333
_raw_targets = os.getenv("TARGET_CHAT_IDS") or os.getenv("TARGET_CHAT_ID", "0")
ENV_TARGET_IDS = []
for _p in str(_raw_targets).split(","):
    _p = _p.strip()
    if _p.lstrip("-").isdigit():
        ENV_TARGET_IDS.append(int(_p))
TARGET_CHAT_ID = ENV_TARGET_IDS[0] if ENV_TARGET_IDS else 0  # backward compat
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
    if ":" not in part:
        continue
    old, new = part.split(":", 1)
    old = old.strip().lower()
    if old:
        ENV_REPLACE[old] = new

ENV_BUTTONS = []
for part in os.getenv("BUTTONS", "").split("|"):
    part = part.strip()
    if "=" not in part:
        continue
    t, u = part.split("=", 1)
    t, u = t.strip(), u.strip()
    if t and u:
        ENV_BUTTONS.append({"text": t, "url": u})

# Default line under every caption (screenshot style)
# Override with CAPTION_FOOTER= or /captionfooter
DEFAULT_OWNER_LINE = os.getenv(
    "CAPTION_FOOTER",
    "Owner 👤 : @RicxyBhai",
).strip()

DATA_FILE = Path(__file__).parent / "filter_data.json"
CAP_LIMIT = 1024

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("forward-bot")

# Forward job state (FTM-style stats)
fwd_task = None
fwd_stop = asyncio.Event()
fwd = {
    "running": False,
    "from_id": 0,
    "to_id": 0,
    "current": 0,
    "skip_number": 0,
    "fetched": 0,
    "ok": 0,
    "duplicate": 0,
    "deleted": 0,
    "skipped": 0,
    "filtered": 0,
    "fail": 0,
    "status": "idle",  # idle|running|cancelled|completed
}

# simple duplicate memory (file_unique_id / text hash) for current session + saved
seen_ids = set()


def default_data():
    return {
        "remove": [],
        "replace": {},
        "caption": {
            "header": "",
            # Always under original caption after replace (user request)
            "footer": DEFAULT_OWNER_LINE or "Owner 👤 : @RicxyBhai",
            "replace_all": "",
            # Default template: full caption + owner line
            # {caption} = original text after word replace
            "template": "{caption}\n\nOwner 👤 : @RicxyBhai",
            "remove_original": False,
            "use_template_always": True,
        },
        "buttons": [],
        # Bot-managed target list. When targets_only=True, ENV TARGET_* is IGNORED.
        "targets": [],
        "targets_only": False,  # True after /addtarget /settarget /cleartargets
        "source_id": 0,  # 0 = use ENV SOURCE_CHAT_ID
        "settings": {
            "skip_number": 0,
            "last_from": 1,
            "last_to": 0,
            "last_current": 0,
            "delay": 0.35,
            # OFF by default — ON caused mid-range gaps when captions similar
            "skip_duplicates": False,
            "awaiting": "",  # add_target | set_target | add_source | ""
        },
    }


def load_data():
    data = default_data()
    if not DATA_FILE.exists():
        return data
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(raw.get("remove"), list):
            data["remove"] = [str(w).lower().strip() for w in raw["remove"] if str(w).strip()]
        if isinstance(raw.get("replace"), dict):
            data["replace"] = {
                str(k).lower().strip(): str(v)
                for k, v in raw["replace"].items()
                if str(k).strip()
            }
        if isinstance(raw.get("caption"), dict):
            c = raw["caption"]
            for k in ("header", "footer", "replace_all", "template"):
                if k in c:
                    data["caption"][k] = str(c.get(k, "") or "")
            data["caption"]["remove_original"] = bool(c.get("remove_original", False))
            if "use_template_always" in c:
                data["caption"]["use_template_always"] = bool(
                    c.get("use_template_always", True)
                )
            # If footer empty and never customized, keep default owner line
            if not data["caption"].get("footer") and not c.get("footer_cleared"):
                data["caption"]["footer"] = DEFAULT_OWNER_LINE or "Owner 👤 : @RicxyBhai"
        if isinstance(raw.get("buttons"), list):
            data["buttons"] = raw["buttons"]
        if isinstance(raw.get("targets"), list):
            data["targets"] = [
                int(x) for x in raw["targets"] if str(x).lstrip("-").isdigit()
            ]
        if "targets_only" in raw:
            data["targets_only"] = bool(raw.get("targets_only"))
        if raw.get("source_id"):
            try:
                data["source_id"] = int(raw["source_id"])
            except Exception:
                pass
        if isinstance(raw.get("settings"), dict):
            data["settings"].update(raw["settings"])
    except Exception as e:
        logger.error("load_data: %s", e)
    return data


def save_data(data):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_remove_words():
    data = load_data()
    words = list(data.get("remove", [])) + list(ENV_BLOCK)
    out, seen = [], set()
    for w in words:
        w = w.lower().strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def get_replace_map():
    data = load_data()
    m = dict(ENV_REPLACE)
    m.update(data.get("replace", {}))
    return m


def get_caption_settings():
    return (load_data().get("caption") or default_data()["caption"])


def get_settings():
    return (load_data().get("settings") or default_data()["settings"])


def get_source_id():
    data = load_data()
    sid = int(data.get("source_id") or 0)
    return sid if sid else SOURCE_CHAT_ID


def get_target_ids():
    """
    Active target channels.
    - If targets_only=True (user managed via bot): ONLY saved targets list.
      Railway TARGET_CHAT_ID is NOT used — so old env target stops getting posts.
    - Else: ENV targets + saved list (first-run / no bot config yet).
    """
    data = load_data()
    out = []
    seen = set()
    if data.get("targets_only"):
        pool = list(data.get("targets") or [])
    else:
        pool = list(ENV_TARGET_IDS) + list(data.get("targets") or [])
    for tid in pool:
        try:
            tid = int(tid)
        except Exception:
            continue
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _enable_targets_only(data, seed_from_env_if_empty=False):
    """Switch to bot-only target list so ENV old target is ignored."""
    data["targets_only"] = True
    data.setdefault("targets", [])
    if seed_from_env_if_empty and not data["targets"] and ENV_TARGET_IDS:
        # keep previous env targets once, user can remove later
        for t in ENV_TARGET_IDS:
            if t not in data["targets"]:
                data["targets"].append(int(t))


def add_target_id(tid, replace_all=False):
    """
    Add target. Always turns on targets_only so ENV alone cannot force old channel.
    replace_all=True → only this target (change target).
    """
    tid = int(tid)
    data = load_data()
    if replace_all:
        data["targets"] = [tid]
        data["targets_only"] = True
        save_data(data)
        return True
    _enable_targets_only(data, seed_from_env_if_empty=False)
    data.setdefault("targets", [])
    if tid in data["targets"]:
        save_data(data)  # still persist targets_only
        return False
    data["targets"].append(tid)
    save_data(data)
    return True


def set_target_ids(ids):
    """Replace entire target list (bot-only mode)."""
    data = load_data()
    out = []
    seen = set()
    for t in ids:
        t = int(t)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    data["targets"] = out
    data["targets_only"] = True
    save_data(data)
    return out


def remove_target_id(tid):
    tid = int(tid)
    data = load_data()
    # Switch to bot-only so removing also drops ENV-forced target
    if not data.get("targets_only"):
        # build current effective list then remove
        cur = list(get_target_ids())
        data["targets"] = [x for x in cur if int(x) != tid]
        data["targets_only"] = True
        save_data(data)
        return True
    before = list(data.get("targets") or [])
    data["targets"] = [x for x in before if int(x) != tid]
    data["targets_only"] = True
    save_data(data)
    return len(data["targets"]) != len(before)


def clear_all_targets():
    data = load_data()
    data["targets"] = []
    data["targets_only"] = True  # empty + only → forward nowhere (not back to ENV)
    save_data(data)


def parse_chat_id_from_text(text):
    """
    Parse channel id from:
    - plain -100123
    - https://t.me/c/1234567890/99  -> -1001234567890
    - https://t.me/username (returns username string, caller resolves)
    """
    if not text:
        return None
    text = text.strip()
    if text.lstrip("-").isdigit():
        return int(text)
    # t.me/c/PRIVATE_ID/msg
    m = re.search(r"t\.me/c/(\d+)(?:/\d+)?", text)
    if m:
        return int("-100" + m.group(1))
    # t.me/username or @username
    m = re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,})", text)
    if m:
        return "@" + m.group(1)
    return None


def chat_id_from_forward(msg):
    """If user forwarded a channel post, return that channel id."""
    if not msg:
        return None
    # new API
    if getattr(msg, "forward_origin", None) is not None:
        fo = msg.forward_origin
        ch = getattr(fo, "chat", None)
        if ch is not None and getattr(ch, "id", None):
            return int(ch.id)
    if getattr(msg, "forward_from_chat", None) is not None:
        return int(msg.forward_from_chat.id)
    return None


async def resolve_chat_ref(bot, ref):
    """ref: int id or @username -> (id, title)"""
    if ref is None:
        return None, None
    try:
        if isinstance(ref, str) and ref.startswith("@"):
            ch = await bot.get_chat(ref)
        else:
            ch = await bot.get_chat(int(ref))
        return int(ch.id), (ch.title or ch.username or str(ch.id))
    except Exception as e:
        return None, str(e)


def get_buttons_rows():
    rows = load_data().get("buttons") or []
    if rows:
        return rows
    if ENV_BUTTONS:
        return [[{"text": b["text"], "url": b["url"]}] for b in ENV_BUTTONS]
    return []


def build_keyboard():
    rows = get_buttons_rows()
    if not rows:
        return None
    kb = []
    for row in rows:
        line = []
        for b in row:
            t, u = str(b.get("text", "")).strip(), str(b.get("url", "")).strip()
            if t and u:
                line.append(InlineKeyboardButton(t, url=u))
        if line:
            kb.append(line)
    return InlineKeyboardMarkup(kb) if kb else None


def is_admin(uid):
    return uid is not None and uid in ADMIN_IDS


def main_menu_keyboard():
    """Bottom reply keyboard (quick access)."""
    return ReplyKeyboardMarkup(
        [
            ["Forward messages", "Forward status"],
            ["Add source", "Add target"],
            ["Targets", "Settings"],
            ["Filters", "Buttons"],
            ["Stop task", "Help"],
        ],
        resize_keyboard=True,
    )


def _ib(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def home_inline_kb():
    """Main /start inline menu — our features only (no FTM/plans names)."""
    return InlineKeyboardMarkup(
        [
            [_ib("👤 HELP", "m:help"), _ib("ℹ️ ABOUT", "m:about")],
            [_ib("⚙️ SETTINGS", "m:settings"), _ib("📤 FORWARD", "m:forward")],
            [_ib("🏷 CHANNELS", "m:channels"), _ib("🖋 CAPTION", "m:caption")],
            [_ib("🕵️ FILTERS", "m:filters"), _ib("⬜ BUTTONS", "m:buttons")],
            [_ib("🎓 HOW TO USE", "m:how"), _ib("📊 STATUS", "m:status")],
            [_ib("🛑 STOP", "m:stop"), _ib("🧪 TEST", "m:test")],
        ]
    )


def settings_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("🏷 CHANNELS", "m:channels"), _ib("🖋 CAPTION", "m:caption")],
            [_ib("🕵️ FILTERS", "m:filters"), _ib("⬜ BUTTONS", "m:buttons")],
            [_ib("⏭ SKIP", "m:skip_info"), _ib("⏱ DELAY", "m:delay_info")],
            [_ib("📑 DUPLICATES", "m:dup_info"), _ib("🔄 RESET", "m:reset_info")],
            [_ib("📤 FORWARD", "m:forward"), _ib("📊 STATUS", "m:status")],
            [_ib("◀️ BACK", "m:home")],
        ]
    )


def channels_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("📥 ADD SOURCE", "a:addsource"), _ib("📤 ADD TARGET", "a:addtarget")],
            [_ib("🎯 SET TARGET (only 1)", "a:settarget"), _ib("📋 LIST", "a:targets")],
            [_ib("🗑 REMOVE TARGET", "m:remove_info"), _ib("🧹 CLEAR TARGETS", "a:cleartargets")],
            [_ib("♻️ CLEAR SOURCE", "a:clearsource"), _ib("🧪 TEST POST", "a:test")],
            [_ib("◀️ BACK", "m:settings")],
        ]
    )


def caption_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("📋 SHOW CAPTION", "a:caption"), _ib("👤 OWNER / FOOTER", "m:footer_info")],
            [_ib("⬆️ HEADER", "m:header_info"), _ib("📝 TEMPLATE", "m:template_info")],
            [_ib("🧹 CLEAR CAPTION", "m:captionclear_info"), _ib("◀️ BACK", "m:settings")],
        ]
    )


def filters_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("📋 LIST FILTERS", "a:listfilters"), _ib("🔁 REPLACE", "m:replace_info")],
            [_ib("🚫 BLOCK WORD", "m:block_info"), _ib("♻️ UNEQUIFY", "a:unequify")],
            [_ib("◀️ BACK", "m:settings")],
        ]
    )


def buttons_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("📋 SHOW BUTTONS", "a:buttons"), _ib("🧹 CLEAR BUTTONS", "a:buttonclear")],
            [_ib("➕ HOW TO ADD", "m:button_info"), _ib("◀️ BACK", "m:settings")],
        ]
    )


def forward_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("📖 HOW FORWARD", "m:forward_help"), _ib("📊 STATUS", "a:status")],
            [_ib("🛑 STOP", "a:stop"), _ib("⏭ SKIP INFO", "m:skip_info")],
            [_ib("🧪 TEST", "a:test"), _ib("◀️ BACK", "m:home")],
        ]
    )


def help_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("📤 FORWARD", "m:forward"), _ib("🏷 CHANNELS", "m:channels")],
            [_ib("🖋 CAPTION", "m:caption"), _ib("🕵️ FILTERS", "m:filters")],
            [_ib("⬜ BUTTONS", "m:buttons"), _ib("⚙️ SETTINGS", "m:settings")],
            [_ib("🎓 HOW TO USE", "m:how"), _ib("◀️ BACK", "m:home")],
        ]
    )


HOME_TEXT = (
    "HELLO {name}\n\n"
    "I'M A POWERFULL AUTO FORWARD BOT\n\n"
    "I CAN FORWARD ALL MESSAGE FROM ONE CHANNEL "
    "TO ANOTHER CHANNEL ➜ WITH MORE FEATURES.\n\n"
    "CLICK HELP BUTTON TO KNOW MORE ABOUT ME"
)

ABOUT_TEXT = (
    "ABOUT THIS BOT\n\n"
    "Channel → Channel auto forward\n"
    "• Multiple TARGET channels\n"
    "• Source / Target add by LINK or FORWARD\n"
    "• Word REPLACE + BLOCK\n"
    "• Full caption + Owner footer\n"
    "• Inline URL buttons under posts\n"
    "• Bulk /forward with STATUS + STOP\n"
    "• Skip / Delay / Duplicates\n\n"
    "Admin only. 24/7 on Railway."
)

HOW_TEXT = (
    "HOW TO USE\n\n"
    "1) Bot ko SOURCE channel me Admin banao\n"
    "2) Bot ko har TARGET me Admin + Post ON\n"
    "3) /addsource  → link ya post forward\n"
    "4) /addtarget  → link ya post forward\n"
    "5) /button Contact Us | https://t.me/you\n"
    "6) /replace old new\n"
    "7) /captionfooter Owner 👤 : @you\n"
    "8) /test\n"
    "9) /forward 1 13365\n"
    "10) /status  /stop\n\n"
    "Resume after cancel:\n"
    "/forward LAST TO 0"
)

HELP_TEXT = (
    "HELP — COMMANDS\n\n"
    "CHANNELS\n"
    "/addsource  /addtarget\n"
    "/targets  /removetarget\n"
    "/clearsource\n\n"
    "FORWARD\n"
    "/forward FROM TO [SKIP] [DELAY]\n"
    "/status  /stop  /copy ID\n"
    "/skip N  /delay 0.3\n\n"
    "CAPTION\n"
    "/caption  /captionfooter\n"
    "/captionheader  /captiontemplate\n\n"
    "FILTERS\n"
    "/replace old new  /block word\n"
    "/listfilters  /unequify\n\n"
    "BUTTONS\n"
    "/button Text | https://url\n"
    "/buttons  /buttonclear\n\n"
    "OTHER\n"
    "/settings  /test  /reset  /cancel"
)


# ---- formatting ----
def entities_to_html(text, entities):
    if not text:
        return ""
    if not entities:
        return escape(text)

    def py_index(utf16_offset):
        units = 0
        idx = 0
        while idx < len(text) and units < utf16_offset:
            units += 1 if ord(text[idx]) <= 0xFFFF else 2
            idx += 1
        return idx

    def open_tag(e):
        t = e.type
        if t in (MessageEntity.BOLD, "bold"):
            return "<b>"
        if t in (MessageEntity.ITALIC, "italic"):
            return "<i>"
        if t in (MessageEntity.UNDERLINE, "underline"):
            return "<u>"
        if t in (MessageEntity.STRIKETHROUGH, "strikethrough"):
            return "<s>"
        if t in (MessageEntity.CODE, "code"):
            return "<code>"
        if t in (MessageEntity.PRE, "pre"):
            return "<pre>"
        if t in (MessageEntity.SPOILER, "spoiler"):
            return "<tg-spoiler>"
        if t in (MessageEntity.TEXT_LINK, "text_link"):
            return '<a href="%s">' % escape(e.url or "")
        if t in (MessageEntity.TEXT_MENTION, "text_mention") and e.user:
            return '<a href="tg://user?id=%s">' % e.user.id
        return ""

    def close_tag(e):
        t = e.type
        if t in (MessageEntity.BOLD, "bold"):
            return "</b>"
        if t in (MessageEntity.ITALIC, "italic"):
            return "</i>"
        if t in (MessageEntity.UNDERLINE, "underline"):
            return "</u>"
        if t in (MessageEntity.STRIKETHROUGH, "strikethrough"):
            return "</s>"
        if t in (MessageEntity.CODE, "code"):
            return "</code>"
        if t in (MessageEntity.PRE, "pre"):
            return "</pre>"
        if t in (MessageEntity.SPOILER, "spoiler"):
            return "</tg-spoiler>"
        if t in (
            MessageEntity.TEXT_LINK,
            "text_link",
            MessageEntity.TEXT_MENTION,
            "text_mention",
        ):
            return "</a>"
        return ""

    events = []
    for e in entities:
        s = py_index(e.offset)
        en = py_index(e.offset + e.length)
        events.append((s, 0, e))
        events.append((en, 1, e))
    events.sort(key=lambda x: (x[0], x[1]))

    out, stack, i, ei = [], [], 0, 0
    while i <= len(text):
        while ei < len(events) and events[ei][0] == i and events[ei][1] == 1:
            e = events[ei][2]
            tmp = []
            while stack and stack[-1] is not e:
                top = stack.pop()
                out.append(close_tag(top))
                tmp.append(top)
            if stack and stack[-1] is e:
                stack.pop()
                out.append(close_tag(e))
            for t in reversed(tmp):
                stack.append(t)
                out.append(open_tag(t))
            ei += 1
        while ei < len(events) and events[ei][0] == i and events[ei][1] == 0:
            e = events[ei][2]
            tag = open_tag(e)
            if tag:
                out.append(tag)
                stack.append(e)
            ei += 1
        if i < len(text):
            out.append(escape(text[i]))
        i += 1
    while stack:
        out.append(close_tag(stack.pop()))
    return "".join(out)


def apply_word_filters_plain(text):
    if not text:
        return text
    cleaned = text
    rep = get_replace_map()
    for old in sorted(rep.keys(), key=len, reverse=True):
        cleaned = re.compile(re.escape(old), re.IGNORECASE).sub(rep[old], cleaned)
    for w in get_remove_words():
        if w in rep:
            continue
        cleaned = re.compile(re.escape(w), re.IGNORECASE).sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def apply_word_filters_html(html):
    if not html:
        return html
    cleaned = html
    rep = get_replace_map()
    for old in sorted(rep.keys(), key=len, reverse=True):
        new = rep[old]
        cleaned = re.compile(re.escape(old), re.IGNORECASE).sub(
            new if "<" in new else escape(new), cleaned
        )
    for w in get_remove_words():
        if w in rep:
            continue
        cleaned = re.compile(re.escape(w), re.IGNORECASE).sub("", cleaned)
    return cleaned


def format_size(n):
    if not n:
        return ""
    x = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if x < 1024 or u == "GB":
            return ("%d %s" % (int(x), u)) if u == "B" else ("%.2f %s" % (x, u))
        x /= 1024.0
    return str(n)


def get_media_meta(msg):
    meta = {
        "filename": "",
        "size": "",
        "type": "PHOTO",
        "quality": "",
        "year": "",
        "language": "",
        "has_file_media": False,
        "dup_key": "",
    }
    if msg.document:
        meta["has_file_media"] = True
        meta["type"] = "DOCUMENT"
        meta["filename"] = msg.document.file_name or "document"
        meta["size"] = format_size(msg.document.file_size or 0)
        meta["dup_key"] = msg.document.file_unique_id or ""
        mt = (msg.document.mime_type or "").lower()
        if mt.startswith("video/"):
            meta["type"] = "VIDEO"
        elif mt.startswith("audio/"):
            meta["type"] = "AUDIO"
    elif msg.video:
        meta["has_file_media"] = True
        meta["type"] = "VIDEO"
        meta["filename"] = msg.video.file_name or "video.mp4"
        meta["size"] = format_size(msg.video.file_size or 0)
        meta["dup_key"] = msg.video.file_unique_id or ""
        h = msg.video.height or 0
        if h >= 1000:
            meta["quality"] = "1080p"
        elif h >= 700:
            meta["quality"] = "720p"
        elif h >= 400:
            meta["quality"] = "480p"
    elif msg.audio:
        meta["has_file_media"] = True
        meta["type"] = "AUDIO"
        meta["filename"] = msg.audio.file_name or "audio.mp3"
        meta["size"] = format_size(msg.audio.file_size or 0)
        meta["dup_key"] = msg.audio.file_unique_id or ""
    elif msg.photo:
        meta["type"] = "PHOTO"
        meta["filename"] = "photo.jpg"
        meta["dup_key"] = msg.photo[-1].file_unique_id if msg.photo else ""
    elif msg.animation:
        meta["has_file_media"] = True
        meta["type"] = "VIDEO"
        meta["filename"] = msg.animation.file_name or "anim.mp4"
        meta["dup_key"] = msg.animation.file_unique_id or ""
    name = meta["filename"]
    m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", name or "")
    meta["year"] = m.group(1) if m else ""
    for q in ("2160p", "1080p", "720p", "480p", "4k"):
        if q in (name or "").lower():
            meta["quality"] = meta["quality"] or q
            break
    n = (name or "").lower()
    langs = []
    for k, v in (("hindi", "Hindi"), ("english", "English"), ("dual audio", "Dual Audio")):
        if k in n:
            langs.append(v)
    meta["language"] = ", ".join(langs)
    # Prefer unique media id; for text-only include message id when present
    # so similar course captions are NOT treated as duplicates of each other.
    if not meta["dup_key"]:
        mid = getattr(msg, "message_id", None) or getattr(msg, "forward_from_message_id", None)
        body = (msg.caption or msg.text or "")[:120]
        if mid:
            meta["dup_key"] = "m:%s:%s" % (mid, hash(body) & 0xFFFFFFFF)
        elif body:
            meta["dup_key"] = "t:%s" % (hash(body) & 0xFFFFFFFF)
        else:
            meta["dup_key"] = ""
    return meta


def render_template(template, variables):
    out = template
    for k, v in variables.items():
        out = out.replace("{" + k + "}", escape(str(v)))
        out = out.replace("{" + k.upper() + "}", escape(str(v)))
    return out


def build_caption_from_message(msg):
    """
    Final caption layout (user request / screenshot):
      [optional header]
      [FULL original caption after word-replace, with formatting]
      [Owner 👤 : @RicxyBhai]   <- footer, always under caption
    Optional template can wrap {caption} etc.
    """
    original = msg.text or msg.caption or ""
    entities = msg.caption_entities or msg.entities or []

    # Keep full formatted caption when possible
    if original and entities:
        body_html = apply_word_filters_html(entities_to_html(original, entities))
    else:
        plain = apply_word_filters_plain(original)
        body_html = escape(plain) if plain else ""

    plain_filtered = apply_word_filters_plain(original)
    cap = get_caption_settings()
    meta = get_media_meta(msg)

    # Template: if set, use it for ALL media types when use_template_always
    # Default template is "{caption}\n\nOwner 👤 : @RicxyBhai"
    use_tpl = bool(cap.get("template"))
    always = cap.get("use_template_always", True)

    if use_tpl and (always or meta.get("has_file_media")):
        # plain_filtered goes into {caption}; footer usually inside template
        body = render_template(
            cap["template"],
            {
                "filename": meta["filename"],
                "size": meta["size"],
                "caption": plain_filtered,
                "year": meta["year"],
                "quality": meta["quality"],
                "type": meta["type"],
                "language": meta["language"],
            },
        )
        # If template already contains owner line, don't double footer
        # unless footer is different and template has no {caption} only - still allow header
        parts = []
        if cap.get("header"):
            h = cap["header"]
            parts.append(h if "<" in h else escape(h))
        if body:
            parts.append(body)
        # Only add footer if template does NOT already include footer text
        footer = (cap.get("footer") or "").strip()
        if footer and footer not in body and "Owner" not in body:
            parts.append(footer if "<" in footer else escape(footer))
        out = "\n\n".join(parts).strip()
    elif cap.get("replace_all"):
        r = cap["replace_all"]
        body = r if "<" in r else escape(r)
        parts = []
        if cap.get("header"):
            h = cap["header"]
            parts.append(h if "<" in h else escape(h))
        if body:
            parts.append(body)
        if cap.get("footer"):
            f = cap["footer"]
            parts.append(f if "<" in f else escape(f))
        out = "\n\n".join(parts).strip()
    elif cap.get("remove_original"):
        parts = []
        if cap.get("header"):
            h = cap["header"]
            parts.append(h if "<" in h else escape(h))
        if cap.get("footer"):
            f = cap["footer"]
            parts.append(f if "<" in f else escape(f))
        out = "\n\n".join(parts).strip()
    else:
        # DEFAULT PATH (most course posts / photos):
        # full replaced caption + owner footer underneath
        parts = []
        if cap.get("header"):
            h = cap["header"]
            parts.append(h if "<" in h else escape(h))
        if body_html:
            parts.append(body_html)
        footer = (cap.get("footer") or DEFAULT_OWNER_LINE or "").strip()
        if footer:
            parts.append(footer if "<" in footer else escape(footer))
        out = "\n\n".join(parts).strip()

    if len(out) > CAP_LIMIT:
        out = out[: CAP_LIMIT - 3] + "..."
    return out, ParseMode.HTML if out else None, meta


def status_text():
    s = fwd
    total = max(1, s["to_id"] - s["from_id"] + 1)
    done = max(0, s["current"] - s["from_id"] + 1) if s["to_id"] else 0
    pct = int(min(100, done * 100 / total)) if s["to_id"] else 0
    remaining = max(0, s["to_id"] - s["current"]) if s["to_id"] else 0
    return (
        "╔════ FORWARD STATUS ════╗\n"
        "║\n"
        "║  FETCHED MESSAGES: %s\n"
        "║  REMAINING MESSAGES: %s\n"
        "║  SUCCESSFULLY FORWARDED: %s\n"
        "║  DUPLICATE MESSAGES: %s\n"
        "║  DELETED MESSAGES: %s\n"
        "║  SKIPPED MESSAGES: %s\n"
        "║  FILTERED MESSAGES: %s\n"
        "║  FAILED: %s\n"
        "║  CURRENT STATUS: %s\n"
        "║  CURRENT ID: %s / %s\n"
        "║  PERCENTAGE: %s %%\n"
        "║\n"
        "╚════════════════════════╝"
        % (
            s["fetched"],
            remaining,
            s["ok"],
            s["duplicate"],
            s["deleted"],
            s["skipped"],
            s["filtered"],
            s["fail"],
            s["status"].upper(),
            s["current"],
            s["to_id"],
            pct,
        )
    )


async def reply(msg, text, **kwargs):
    await msg.reply_text(text, **kwargs)


async def send_to_one_chat(context, msg: Message, chat_id, cleaned, parse_mode, keyboard, html=True):
    """Send one prepared post to one target chat_id."""
    kw = {}
    if html and parse_mode and cleaned:
        kw["parse_mode"] = parse_mode
    if keyboard:
        kw["reply_markup"] = keyboard

    if not html:
        cap = re.sub(r"<[^>]+>", "", cleaned or "") or None
    else:
        cap = cleaned or None

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

    if not has_media:
        if not cap:
            return "filtered"
        await context.bot.send_message(chat_id=chat_id, text=cap, **kw)
        return "ok"

    if msg.photo:
        await context.bot.send_photo(
            chat_id, msg.photo[-1].file_id, caption=cap, **kw
        )
    elif msg.video:
        await context.bot.send_video(chat_id, msg.video.file_id, caption=cap, **kw)
    elif msg.document:
        await context.bot.send_document(
            chat_id, msg.document.file_id, caption=cap, **kw
        )
    elif msg.audio:
        await context.bot.send_audio(chat_id, msg.audio.file_id, caption=cap, **kw)
    elif msg.voice:
        await context.bot.send_voice(chat_id, msg.voice.file_id, caption=cap, **kw)
    elif msg.animation:
        await context.bot.send_animation(
            chat_id, msg.animation.file_id, caption=cap, **kw
        )
    elif msg.sticker:
        kw2 = {"reply_markup": keyboard} if keyboard else {}
        await context.bot.send_sticker(chat_id, msg.sticker.file_id, **kw2)
    elif msg.video_note:
        await context.bot.send_video_note(chat_id, msg.video_note.file_id)
        if keyboard or cleaned:
            await context.bot.send_message(
                chat_id,
                cleaned or "-",
                reply_markup=keyboard,
                parse_mode=parse_mode if html else None,
            )
    else:
        return "filtered"
    return "ok"


async def _send_with_retries(context, msg, tid, cleaned, parse_mode, keyboard, tries=4):
    """Send one chat with flood/timeout retries. Never silent-skip."""
    last_err = None
    for attempt in range(tries):
        try:
            st = await send_to_one_chat(
                context, msg, tid, cleaned, parse_mode, keyboard, True
            )
            return st
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 3) or 3) + 1
            logger.warning("flood wait %ss target=%s", wait, tid)
            await asyncio.sleep(wait)
            last_err = e
        except TelegramError as e:
            err = str(e).lower()
            last_err = e
            if "parse" in err or "entity" in err or "can't parse" in err:
                try:
                    st = await send_to_one_chat(
                        context, msg, tid, cleaned, parse_mode, keyboard, False
                    )
                    return st
                except RetryAfter as e2:
                    await asyncio.sleep(int(e2.retry_after) + 1)
                    last_err = e2
                    continue
                except Exception as e2:
                    last_err = e2
            if any(
                x in err
                for x in (
                    "timed out",
                    "timeout",
                    "flood",
                    "too many requests",
                    "retry",
                    "connection",
                    "temporary",
                    "bad gateway",
                    "network",
                )
            ):
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            # hard errors (chat not found, forbidden, etc.)
            logger.exception("send hard fail target %s: %s", tid, e)
            break
        except Exception as e:
            last_err = e
            logger.exception("send fail target %s", tid)
            await asyncio.sleep(1.0 * (attempt + 1))
    if last_err:
        logger.error("give up target %s: %s", tid, last_err)
    return "fail"


async def send_cleaned_to_target(context, msg: Message, check_dup=True):
    """Send to ALL target channels. Returns ok|duplicate|filtered|fail|no_target."""
    targets = get_target_ids()
    if not targets:
        return "no_target"

    cleaned, parse_mode, meta = build_caption_from_message(msg)
    keyboard = build_keyboard()
    settings = get_settings()

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
        return "filtered"

    # Duplicate check ONLY after we know content exists.
    # Mark seen ONLY after at least one successful send (so fail can retry later).
    dup_key = meta.get("dup_key") or ""
    if (
        check_dup
        and settings.get("skip_duplicates", False)
        and dup_key
        and dup_key in seen_ids
    ):
        return "duplicate"

    ok_n = 0
    fail_n = 0
    for tid in targets:
        st = await _send_with_retries(
            context, msg, tid, cleaned, parse_mode, keyboard
        )
        if st == "ok":
            ok_n += 1
        elif st == "filtered":
            # no content for this type — not a network fail
            return "filtered"
        else:
            fail_n += 1

    if ok_n > 0:
        if check_dup and settings.get("skip_duplicates", False) and dup_key:
            seen_ids.add(dup_key)
            # prevent unbounded growth
            if len(seen_ids) > 50000:
                # drop arbitrary half
                for _ in range(len(seen_ids) // 2):
                    try:
                        seen_ids.pop()
                    except KeyError:
                        break
        return "ok"
    return "fail"


async def channel_auto_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.id != get_source_id():
        return
    try:
        await send_cleaned_to_target(context, msg, check_dup=True)
    except Exception:
        logger.exception("auto")


async def manual_forward_handler(update, context):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type != "private":
        return
    if not is_admin(user.id):
        return

    text = (msg.text or "").strip()
    data = load_data()
    awaiting = (data.get("settings") or {}).get("awaiting") or ""

    # ---- waiting for target/source add (link or forward) ----
    if awaiting in ("add_target", "set_target", "add_source", "set_source"):
        ref = chat_id_from_forward(msg)
        if ref is None:
            ref = parse_chat_id_from_text(text or (msg.caption or ""))
        if ref is None:
            await reply(
                msg,
                "Samajh nahi aaya.\n\n"
                "1) Channel LINK bhejo\n"
                "   https://t.me/c/1234567890/1\n"
                "2) YA channel se koi post yahan FORWARD karo\n"
                "3) YA seedha ID: -1001234567890\n"
                "4) YA @channelusername\n\n"
                "/cancel se band",
            )
            return

        cid, title = await resolve_chat_ref(context.bot, ref)
        if cid is None:
            await reply(
                msg,
                "Chat resolve fail: %s\n\n"
                "Fix:\n"
                "• Bot ko us channel me Admin banao\n"
                "• Private channel me bot add + admin\n"
                "• Phir dubara link/forward bhejo"
                % title,
            )
            return

        data = load_data()
        data.setdefault("settings", {})["awaiting"] = ""
        save_data(data)

        if awaiting == "set_target":
            # ONLY this target — purane (ENV + list) band
            set_target_ids([cid])
            await reply(
                msg,
                "✅ TARGET CHANGED (sirf ye)\n"
                "Title: %s\n"
                "ID: %s\n\n"
                "Purane targets band.\n"
                "Ab forward SIRF yahan jayega.\n"
                "/targets — check\n"
                "/test"
                % (title, cid),
            )
            return

        if awaiting == "add_target":
            added = add_target_id(cid, replace_all=False)
            if added:
                await reply(
                    msg,
                    "✅ TARGET ADDED\n"
                    "Title: %s\n"
                    "ID: %s\n\n"
                    "Active targets: %s\n"
                    "(Railway purana TARGET_CHAT_ID ignore)\n"
                    "/targets\n"
                    "Sirf ek chahiye? /settarget"
                    % (title, cid, len(get_target_ids())),
                )
            else:
                await reply(
                    msg,
                    "Pehle se list me hai:\n%s\n%s\n\nActive: %s"
                    % (title, cid, get_target_ids()),
                )
            return

        # add_source / set_source
        data = load_data()
        data["source_id"] = cid
        data.setdefault("settings", {})["awaiting"] = ""
        save_data(data)
        await reply(
            msg,
            "✅ SOURCE SET\n"
            "Title: %s\n"
            "ID: %s\n\n"
            "Is channel se messages uthenge.\n"
            "Bot ko SOURCE me Admin banao.\n"
            "/targets"
            % (title, cid),
        )
        return

    # Reply keyboard actions
    if text and not text.startswith("/"):
        low = text.lower()
        if low in ("forward messages", "forward"):
            await cmd_forward_help(update, context)
            return
        if low in ("forward status", "status"):
            await cmd_status(update, context)
            return
        if low == "settings":
            # open inline settings panel like FTM
            await msg.reply_text(
                "SETTINGS\n\nchange your settings as your wish",
                reply_markup=settings_inline_kb(),
            )
            return
        if low in ("stop task", "stop"):
            await cmd_stop(update, context)
            return
        if low == "filters":
            await msg.reply_text(
                "FILTERS\n\nWord replace + block",
                reply_markup=filters_inline_kb(),
            )
            return
        if low == "buttons":
            await msg.reply_text(
                "BUTTONS\n\nURL buttons under posts",
                reply_markup=buttons_inline_kb(),
            )
            return
        if low in ("targets", "channels"):
            await msg.reply_text(
                "CHANNELS\n\nSource + Targets",
                reply_markup=channels_inline_kb(),
            )
            return
        if low in ("add source", "addsource", "source"):
            await cmd_addsource(update, context)
            return
        if low in ("add target", "addtarget"):
            await cmd_addtarget(update, context)
            return
        if low == "help":
            await msg.reply_text(HELP_TEXT, reply_markup=help_inline_kb())
            return
        if low == "reset":
            await cmd_reset(update, context)
            return
        if low in ("menu", "home", "main menu"):
            await cmd_start(update, context)
            return
        # manual content -> all targets
        if not get_target_ids():
            await reply(msg, "Pehle /addtarget se target add karo.")
            return
        st = await send_cleaned_to_target(context, msg)
        await reply(msg, "Posted: " + str(st))
        return

    if text.startswith("/"):
        return
    st = await send_cleaned_to_target(context, msg)
    await reply(msg, "Posted: " + str(st))


def _is_missing_msg_error(err: str) -> bool:
    err = (err or "").lower()
    return any(
        x in err
        for x in (
            "message to forward not found",
            "message not found",
            "msg_id_invalid",
            "message identifier is not specified",
            "bad request: message to copy not found",
            "message to copy not found",
        )
    )


def _is_retryable_error(err: str) -> bool:
    err = (err or "").lower()
    return any(
        x in err
        for x in (
            "timed out",
            "timeout",
            "flood",
            "too many requests",
            "retry",
            "connection",
            "temporary",
            "bad gateway",
            "network",
            "gateway",
            "503",
            "502",
            "429",
        )
    )


async def _fetch_source_message(context, source, hop, mid, tries=5):
    """
    Bots cannot read history directly — hop forward to get Message object.
    Returns (msg, status) status=ok|deleted|fail|protected
    """
    last_err = None
    for attempt in range(tries):
        try:
            fwd_msg = await context.bot.forward_message(
                chat_id=hop,
                from_chat_id=source,
                message_id=mid,
            )
            return fwd_msg, "ok"
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 3) or 3) + 1
            logger.warning("forward hop flood mid=%s wait=%s", mid, wait)
            await asyncio.sleep(wait)
            last_err = e
            continue
        except TelegramError as e:
            err = str(e).lower()
            last_err = e
            if _is_missing_msg_error(err):
                return None, "deleted"
            if "protected" in err or "can't be forwarded" in err:
                return None, "protected"
            if _is_retryable_error(err):
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            # unknown — retry a couple times then fail (NOT deleted)
            if attempt < tries - 1:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            logger.error("hop fail mid=%s: %s", mid, e)
            return None, "fail"
        except Exception as e:
            last_err = e
            logger.exception("hop exception mid=%s", mid)
            await asyncio.sleep(1.2 * (attempt + 1))
    logger.error("hop give up mid=%s last=%s", mid, last_err)
    return None, "fail"


async def _copy_raw_to_targets(context, source, mid, targets):
    """Fallback when content is protected — plain copy, no caption edit."""
    ok = 0
    kb = build_keyboard()
    for tid in targets:
        for attempt in range(4):
            try:
                await context.bot.copy_message(
                    tid, source, mid, reply_markup=kb
                )
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 1)
            except TelegramError as e:
                if _is_missing_msg_error(str(e)):
                    return "deleted"
                if _is_retryable_error(str(e)):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                logger.exception("copy fail %s mid=%s", tid, mid)
                break
            except Exception:
                await asyncio.sleep(1.0)
    return "ok" if ok else "fail"


async def forward_one(context, mid, check_dup=False):
    """
    Pull one source message then send cleaned version to all targets.
    Retries transient errors. Only marks deleted when Telegram says not found.
    """
    targets = get_target_ids()
    if not targets:
        return "no_target"
    source = get_source_id()
    if not source:
        return "fail"

    hop = targets[0]
    fwd_msg, st = await _fetch_source_message(context, source, hop, mid)
    if st == "deleted":
        return "deleted"
    if st == "protected":
        return await _copy_raw_to_targets(context, source, mid, targets)
    if st != "ok" or fwd_msg is None:
        # last chance: try raw copy without filter
        raw = await _copy_raw_to_targets(context, source, mid, targets)
        return raw if raw != "fail" else "fail"

    # Delete temp hop forward (we will re-post cleaned to ALL targets including hop)
    try:
        await context.bot.delete_message(hop, fwd_msg.message_id)
    except TelegramError:
        pass
    except Exception:
        pass

    try:
        # Bulk: check_dup=False so similar captions never drop mid-range posts
        result = await send_cleaned_to_target(context, fwd_msg, check_dup=check_dup)
        if result == "fail":
            # one more full retry after short pause
            await asyncio.sleep(2)
            result = await send_cleaned_to_target(
                context, fwd_msg, check_dup=check_dup
            )
        return result
    except RetryAfter as e:
        await asyncio.sleep(int(e.retry_after) + 1)
        try:
            return await send_cleaned_to_target(
                context, fwd_msg, check_dup=check_dup
            )
        except Exception:
            return "fail"
    except Exception:
        logger.exception("send_cleaned mid=%s", mid)
        return "fail"


async def run_forward(context, admin_id, from_id, to_id, skip_number, delay):
    global fwd
    fwd_stop.clear()
    # Start EXACTLY at from_id when skip=0 (top / upar se)
    skip_number = int(skip_number or 0)
    if skip_number < 0:
        skip_number = 0
    start_id = int(from_id) + skip_number
    if start_id < 1:
        start_id = 1
    if start_id > to_id:
        await context.bot.send_message(
            admin_id,
            "Skip too large.\nFROM=%s SKIP=%s → start=%s > TO=%s"
            % (from_id, skip_number, start_id, to_id),
        )
        return

    # Safer default delay — too fast = flood gaps
    try:
        delay = float(delay)
    except Exception:
        delay = 0.35
    if delay < 0.2:
        delay = 0.2

    # During bulk: do NOT use caption-duplicate skip (main cause of mid gaps)
    bulk_check_dup = False

    fwd.update(
        {
            "running": True,
            "from_id": from_id,
            "to_id": to_id,
            "current": start_id,
            "skip_number": skip_number,
            "fetched": 0,
            "ok": 0,
            "duplicate": 0,
            "deleted": 0,
            "skipped": skip_number,
            "filtered": 0,
            "fail": 0,
            "status": "running",
        }
    )
    data = load_data()
    data.setdefault("settings", {})
    data["settings"]["last_from"] = from_id
    data["settings"]["last_to"] = to_id
    data["settings"]["last_current"] = start_id
    data["settings"]["skip_number"] = skip_number
    data["settings"]["delay"] = delay
    save_data(data)

    targets = get_target_ids()
    await context.bot.send_message(
        admin_id,
        "FORWARD STARTED\n"
        "Range: %s -> %s\n"
        "Skip arg: %s\n"
        "▶ FIRST MESSAGE ID: %s\n"
        "Delay: %ss\n"
        "Targets: %s\n"
        "Bulk dup-skip: OFF\n\n"
        "Agar FIRST ID 1 nahi hai to tumne\n"
        "/forward 1 TO nahi likha — FROM change karo.\n"
        "Empty source ids = DELETED (normal gap).\n"
        "/status  /stop"
        % (from_id, to_id, skip_number, start_id, delay, len(targets)),
    )
    await context.bot.send_message(admin_id, status_text())

    last_report = 0
    consecutive_fail = 0
    try:
        for mid in range(start_id, to_id + 1):
            if fwd_stop.is_set():
                fwd["status"] = "cancelled"
                break
            fwd["current"] = mid
            fwd["fetched"] += 1

            result = await forward_one(context, mid, check_dup=bulk_check_dup)

            # One automatic re-try on fail (network blip)
            if result == "fail":
                await asyncio.sleep(max(delay, 1.0))
                result = await forward_one(context, mid, check_dup=bulk_check_dup)

            if result == "ok":
                fwd["ok"] += 1
                consecutive_fail = 0
            elif result == "duplicate":
                fwd["duplicate"] += 1
                consecutive_fail = 0
            elif result == "deleted":
                # Real missing id in source (Telegram gap) — not a bug
                fwd["deleted"] += 1
                consecutive_fail = 0
            elif result == "filtered":
                fwd["filtered"] += 1
                consecutive_fail = 0
            elif result in ("skip", "skipped"):
                fwd["skipped"] += 1
                consecutive_fail = 0
            else:
                fwd["fail"] += 1
                consecutive_fail += 1

            try:
                data = load_data()
                data.setdefault("settings", {})["last_current"] = mid
                save_data(data)
            except Exception:
                pass

            # Progress every 25 msgs (more frequent feedback)
            if fwd["fetched"] - last_report >= 25 or mid == to_id:
                last_report = fwd["fetched"]
                try:
                    await context.bot.send_message(admin_id, status_text())
                except Exception:
                    pass

            # If many fails in a row, slow down (flood / outage)
            if consecutive_fail >= 5:
                try:
                    await context.bot.send_message(
                        admin_id,
                        "⚠️ 5 fails in a row at id %s — waiting 10s then continue.\n"
                        "Check bot is Admin on source+target."
                        % mid,
                    )
                except Exception:
                    pass
                await asyncio.sleep(10)
                consecutive_fail = 0

            await asyncio.sleep(delay)
    except Exception:
        logger.exception("run_forward crash")
        fwd["status"] = "cancelled"
        try:
            await context.bot.send_message(
                admin_id, "FORWARD crashed — /status dekho, /forward se resume."
            )
        except Exception:
            pass
    finally:
        if fwd["status"] != "cancelled":
            fwd["status"] = "completed"
        fwd["running"] = False
        try:
            await context.bot.send_message(
                admin_id,
                status_text()
                + "\n\nFORWARD "
                + fwd["status"].upper()
                + "\n\nResume tip:\n/forward %s %s 0"
                % (fwd.get("current") or start_id, to_id),
            )
        except Exception:
            pass


# ---------- commands ----------
async def cmd_start(update, context):
    """Always answer — clears stuck add-mode so bot never feels 'dead'."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    try:
        # Unstick SOURCE ADD / TARGET ADD waiting mode
        try:
            data = load_data()
            if (data.get("settings") or {}).get("awaiting"):
                data.setdefault("settings", {})["awaiting"] = ""
                save_data(data)
        except Exception:
            logger.exception("start clear awaiting")

        name = (user.first_name if user else "USER") or "USER"
        name = str(name).upper()
        admin = bool(user and is_admin(user.id))
        text = HOME_TEXT.format(name=name)
        if not admin:
            text += (
                "\n\n⚠️ Admin only.\n"
                "Railway ADMIN_IDS me apna Telegram id daalo.\n"
                "Your id: %s" % (user.id if user else "?")
            )
        try:
            await msg.reply_text(
                text,
                reply_markup=home_inline_kb() if admin else None,
            )
        except Exception:
            await msg.reply_text(text)

        if admin:
            try:
                n = len(get_target_ids())
                src = get_source_id()
                await msg.reply_text(
                    "Bot ONLINE ✅\n"
                    "Source: %s\n"
                    "Targets: %s\n"
                    "Mode: %s\n\n"
                    "Bottom keys + blue buttons.\n"
                    "/settarget = sirf 1 naya target\n"
                    "/targets = list\n"
                    "/ping = alive check"
                    % (
                        src or "(not set)",
                        n,
                        "bot-list" if load_data().get("targets_only") else "env+list",
                    ),
                    reply_markup=main_menu_keyboard(),
                )
            except Exception:
                try:
                    await msg.reply_text("Bot ONLINE ✅", reply_markup=main_menu_keyboard())
                except Exception:
                    pass
    except Exception:
        logger.exception("cmd_start")
        try:
            await msg.reply_text("Bot alive. /ping")
        except Exception:
            pass


async def cmd_ping(update, context):
    msg = update.effective_message
    if not msg:
        return
    try:
        await msg.reply_text(
            "PONG ✅ Bot chal raha hai\n"
            "src=%s targets=%s"
            % (get_source_id(), get_target_ids())
        )
    except Exception as e:
        try:
            await msg.reply_text("PONG (err) " + str(e)[:200])
        except Exception:
            pass


async def _edit_or_reply(query, text, kb=None):
    """Edit callback message; fallback to new message."""
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except TelegramError:
        try:
            await query.message.reply_text(text, reply_markup=kb)
        except Exception:
            pass


async def menu_callback(update, context):
    """Inline blue-button menu (FTM-style layout, our feature names)."""
    query = update.callback_query
    if not query:
        return
    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.answer("Admin only", show_alert=True)
        return
    data = query.data or ""
    await query.answer()

    # ---- panels (m:) ----
    if data == "m:home":
        name = (user.first_name or "USER").upper()
        await _edit_or_reply(query, HOME_TEXT.format(name=name), home_inline_kb())
        return
    if data == "m:help":
        await _edit_or_reply(query, HELP_TEXT, help_inline_kb())
        return
    if data == "m:about":
        await _edit_or_reply(query, ABOUT_TEXT, InlineKeyboardMarkup([[_ib("◀️ BACK", "m:home")]]))
        return
    if data == "m:how":
        await _edit_or_reply(query, HOW_TEXT, InlineKeyboardMarkup([[_ib("◀️ BACK", "m:home")]]))
        return
    if data == "m:settings":
        st = get_settings()
        text = (
            "SETTINGS\n\n"
            "change your settings as your wish\n\n"
            "Source: %s\n"
            "Targets: %s\n"
            "Skip: %s | Delay: %ss\n"
            "Duplicates: %s"
            % (
                get_source_id(),
                len(get_target_ids()),
                st.get("skip_number", 0),
                st.get("delay", 0.25),
                st.get("skip_duplicates", True),
            )
        )
        await _edit_or_reply(query, text, settings_inline_kb())
        return
    if data == "m:channels":
        await _edit_or_reply(
            query,
            "CHANNELS\n\n"
            "Source + Target channels yahan manage karo.\n"
            "Link bhejo YA channel se post FORWARD karo.",
            channels_inline_kb(),
        )
        return
    if data == "m:caption":
        cap = get_caption_settings()
        await _edit_or_reply(
            query,
            "CAPTION\n\n"
            "Footer (Owner):\n%s\n\n"
            "Header: %s\n"
            "Template:\n%s"
            % (
                cap.get("footer") or "(empty)",
                cap.get("header") or "(empty)",
                (cap.get("template") or "(default)")[:200],
            ),
            caption_inline_kb(),
        )
        return
    if data == "m:filters":
        rm = get_remove_words()
        rp = get_replace_map()
        await _edit_or_reply(
            query,
            "FILTERS\n\n"
            "Block words: %s\n"
            "Replace pairs: %s\n\n"
            "Commands:\n"
            "/replace old new\n"
            "/block word\n"
            "/listfilters"
            % (len(rm), len(rp)),
            filters_inline_kb(),
        )
        return
    if data == "m:buttons":
        rows = get_buttons_rows()
        n = sum(len(r) for r in rows) if rows else 0
        await _edit_or_reply(
            query,
            "BUTTONS\n\n"
            "URL buttons under every post: %s\n\n"
            "Add:\n"
            "/button Contact Us | https://t.me/you"
            % n,
            buttons_inline_kb(),
        )
        return
    if data == "m:forward":
        await _edit_or_reply(
            query,
            "FORWARD\n\n"
            "Bulk forward source → all targets\n\n"
            "Usage:\n"
            "/forward 1 13365\n"
            "/forward 5077 13365 0\n"
            "/forward FROM TO SKIP DELAY\n\n"
            "/status  /stop  /copy ID",
            forward_inline_kb(),
        )
        return
    if data == "m:forward_help":
        await _edit_or_reply(
            query,
            "HOW FORWARD\n\n"
            "1) Channels set (/addsource /addtarget)\n"
            "2) /forward 1 100\n"
            "3) /status dekhte raho\n"
            "4) /stop se cancel\n"
            "5) Cancel ke baad resume:\n"
            "   /forward LAST TO 0\n\n"
            "Skip first N:\n"
            "/forward 1 1000 50\n"
            "Delay 0.5s:\n"
            "/forward 1 1000 0 0.5",
            forward_inline_kb(),
        )
        return
    if data == "m:status":
        # fall through to action
        data = "a:status"
    if data == "m:stop":
        data = "a:stop"
    if data == "m:test":
        data = "a:test"

    # info panels (tell user the command)
    info_map = {
        "m:skip_info": (
            "SKIP NUMBER\n\n"
            "Forward shuru karte time pehle N skip.\n\n"
            "Command:\n"
            "/skip 5\n"
            "/forward 1 1000 5\n\n"
            "Current skip: %s" % get_settings().get("skip_number", 0)
        ),
        "m:delay_info": (
            "DELAY\n\n"
            "Har message ke beech wait (seconds).\n\n"
            "Command:\n"
            "/delay 0.3\n"
            "/forward 1 100 0 0.5\n\n"
            "Current: %ss" % get_settings().get("delay", 0.25)
        ),
        "m:dup_info": (
            "DUPLICATES\n\n"
            "Same content dobara na bheje.\n\n"
            "Command:\n"
            "/duplicates on\n"
            "/duplicates off\n"
            "/unequify  (memory clear)\n\n"
            "Now: %s" % get_settings().get("skip_duplicates", True)
        ),
        "m:reset_info": (
            "RESET\n\n"
            "Settings wapas default.\n\n"
            "Command:\n"
            "/reset"
        ),
        "m:remove_info": (
            "REMOVE / CHANGE TARGET\n\n"
            "List dekho:\n"
            "/targets\n\n"
            "Ek hatao:\n"
            "/removetarget -100...\n\n"
            "SIRF naya target (purana band):\n"
            "/settarget\n"
            "  phir link ya forward\n\n"
            "Saare targets khali:\n"
            "/cleartargets\n\n"
            "Note: Railway TARGET_CHAT_ID\n"
            "bot manage mode me IGNORE hota hai."
        ),
        "m:footer_info": (
            "OWNER / FOOTER\n\n"
            "Har caption ke NEECHE ye line.\n\n"
            "Command:\n"
            "/captionfooter Owner 👤 : @RicxyBhai\n"
            "/captionfooter   (clear)"
        ),
        "m:header_info": (
            "HEADER\n\n"
            "Caption ke UPAR text.\n\n"
            "Command:\n"
            "/captionheader Your text"
        ),
        "m:template_info": (
            "TEMPLATE\n\n"
            "{caption} = full original after replace\n\n"
            "Command:\n"
            "/captiontemplate\n"
            "(reply to a template message)\n\n"
            "Default:\n"
            "{caption}\n\nOwner 👤 : @RicxyBhai"
        ),
        "m:captionclear_info": (
            "CLEAR CAPTION\n\n"
            "Header/footer/template default pe.\n\n"
            "Command:\n"
            "/captionclear"
        ),
        "m:replace_info": (
            "REPLACE\n\n"
            "Word badlo caption me.\n\n"
            "Command:\n"
            "/replace @old @new\n"
            "/unreplace @old\n"
            "/listfilters"
        ),
        "m:block_info": (
            "BLOCK WORD\n\n"
            "Word hatao, baaki forward.\n\n"
            "Command:\n"
            "/block spamword\n"
            "/unblock spamword"
        ),
        "m:button_info": (
            "ADD URL BUTTON\n\n"
            "Har post ke neeche button.\n\n"
            "Command:\n"
            "/button Contact Us | https://t.me/RicxyBhai\n"
            "/button2 Row2 | https://t.me/you\n"
            "/buttons\n"
            "/buttonclear"
        ),
    }
    if data in info_map:
        back = "m:settings"
        if data.startswith("m:footer") or data.startswith("m:header") or data.startswith("m:template") or data.startswith("m:caption"):
            back = "m:caption"
        elif data.startswith("m:replace") or data.startswith("m:block"):
            back = "m:filters"
        elif data.startswith("m:button"):
            back = "m:buttons"
        elif data.startswith("m:remove"):
            back = "m:channels"
        elif data in ("m:skip_info", "m:delay_info", "m:dup_info", "m:reset_info"):
            back = "m:settings"
        await _edit_or_reply(
            query,
            info_map[data],
            InlineKeyboardMarkup([[_ib("◀️ BACK", back)]]),
        )
        return

    # ---- direct actions (a:) run existing commands ----
    # Build a fake update pointing at the same message for cmd_* reuse
    if not data.startswith("a:"):
        return

    action = data[2:]
    # Fake context.args empty; some cmds need no args
    class _U:
        pass

    fake = update  # real update is fine for effective_user/message
    msg = query.message

    async def run_and_show(coro):
        try:
            await coro
        except Exception as e:
            try:
                await msg.reply_text("Error: " + str(e))
            except Exception:
                pass

    if action == "addsource":
        await run_and_show(cmd_addsource(update, context))
        return
    if action == "addtarget":
        await run_and_show(cmd_addtarget(update, context))
        return
    if action == "settarget":
        await run_and_show(cmd_settarget(update, context))
        return
    if action == "cleartargets":
        await run_and_show(cmd_cleartargets(update, context))
        return
    if action == "targets":
        await run_and_show(cmd_targets(update, context))
        return
    if action == "clearsource":
        await run_and_show(cmd_clearsource(update, context))
        return
    if action == "test":
        await run_and_show(cmd_test(update, context))
        return
    if action == "status":
        await run_and_show(cmd_status(update, context))
        return
    if action == "stop":
        await run_and_show(cmd_stop(update, context))
        return
    if action == "listfilters":
        await run_and_show(cmd_listfilters(update, context))
        return
    if action == "unequify":
        await run_and_show(cmd_unequify(update, context))
        return
    if action == "buttons":
        await run_and_show(cmd_buttons(update, context))
        return
    if action == "buttonclear":
        await run_and_show(cmd_buttonclear(update, context))
        return
    if action == "caption":
        await run_and_show(cmd_caption(update, context))
        return
    if action == "settings":
        await run_and_show(cmd_settings(update, context))
        return


async def cmd_targets(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    targets = get_target_ids()
    src = get_source_id()
    data = load_data()
    lines = ["CHANNELS\n", "—— SOURCE ——", "ID: %s" % src]
    try:
        ch = await context.bot.get_chat(src)
        lines.append("Name: %s" % (ch.title or ""))
    except Exception as e:
        lines.append("ERR: %s (bot ko source me Admin banao)" % e)
    if data.get("source_id"):
        lines.append("(bot se set)")
    elif SOURCE_CHAT_ID:
        lines.append("(Railway ENV)")
    else:
        lines.append("(not set — /addsource)")

    mode = "BOT LIST only (ENV ignore)" if data.get("targets_only") else "ENV + bot list"
    lines.append("\n—— TARGETS (%s) ——" % len(targets))
    lines.append("Mode: %s" % mode)
    if ENV_TARGET_IDS and data.get("targets_only"):
        lines.append(
            "Railway TARGET_CHAT_ID=%s  → IGNORE (bot mode)"
            % ",".join(str(x) for x in ENV_TARGET_IDS)
        )
    elif ENV_TARGET_IDS and not data.get("targets_only"):
        lines.append(
            "Railway still active: %s"
            % ",".join(str(x) for x in ENV_TARGET_IDS)
        )
    if not targets:
        lines.append("(empty) /settarget ya /addtarget")
    for i, tid in enumerate(targets, 1):
        try:
            ch = await context.bot.get_chat(tid)
            title = "%s" % (ch.title or tid)
        except Exception:
            title = "(bot not admin / not found)"
        tag = ""
        if not data.get("targets_only") and tid in ENV_TARGET_IDS:
            tag = " [from Railway ENV]"
        lines.append("%s. %s\n   %s%s" % (i, title, tid, tag))

    lines.append(
        "\nCommands:\n"
        "/settarget  → SIRF 1 naya (purana band)\n"
        "/addtarget  → list me aur add\n"
        "/removetarget -100...\n"
        "/cleartargets → saare hatao\n"
        "/addsource\n\n"
        "Problem: purane channel pe bhi ja raha?\n"
        "→ /settarget se naya set karo\n"
        "  YA /cleartargets phir /addtarget"
    )
    await reply(msg, "\n".join(lines))


async def cmd_addtarget(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        await reply(msg, "Admin only.")
        return

    if context.args:
        raw = " ".join(context.args)
        ref = parse_chat_id_from_text(raw)
        if ref is None:
            await reply(msg, "Invalid.\n/addtarget -100123\n/addtarget https://t.me/c/123/1")
            return
        cid, title = await resolve_chat_ref(context.bot, ref)
        if cid is None:
            await reply(msg, "Resolve fail: %s\nBot ko Admin banao." % title)
            return
        added = add_target_id(cid, replace_all=False)
        if added:
            await reply(
                msg,
                "✅ TARGET ADDED\n%s\nID: %s\n\nActive now (%s):\n%s\n\n"
                "Sirf ye chahiye? /settarget"
                % (title, cid, len(get_target_ids()), get_target_ids()),
            )
        else:
            await reply(msg, "Already in list:\n%s\n%s\nActive: %s" % (title, cid, get_target_ids()))
        return

    data = load_data()
    data.setdefault("settings", {})["awaiting"] = "add_target"
    save_data(data)
    await reply(
        msg,
        "TARGET ADD MODE\n\n"
        "Naya channel list me JUDEGA.\n"
        "(Purane ENV target band — bot list mode ON)\n\n"
        "Bhejo: LINK / FORWARD / ID\n"
        "Sirf EK target chahiye? /settarget use karo\n"
        "/cancel",
    )


async def cmd_settarget(update, context):
    """Replace ALL targets with one channel — fixes old ENV target leak."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        await reply(msg, "Admin only.")
        return

    if context.args:
        raw = " ".join(context.args)
        ref = parse_chat_id_from_text(raw)
        if ref is None:
            await reply(msg, "Invalid.\n/settarget -100123\n/settarget https://t.me/c/123/1")
            return
        cid, title = await resolve_chat_ref(context.bot, ref)
        if cid is None:
            await reply(msg, "Resolve fail: %s" % title)
            return
        set_target_ids([cid])
        await reply(
            msg,
            "✅ TARGET SET (only this)\n"
            "Name: %s\n"
            "ID: %s\n\n"
            "Purane saare targets + Railway TARGET_CHAT_ID band.\n"
            "Ab forward SIRF yahan.\n"
            "/targets  /test"
            % (title, cid),
        )
        return

    data = load_data()
    data.setdefault("settings", {})["awaiting"] = "set_target"
    save_data(data)
    await reply(
        msg,
        "SET TARGET MODE (replace all)\n\n"
        "Jo channel bhejoge wahi EK target rahega.\n"
        "Purana Ricxy / Railway target band ho jayega.\n\n"
        "1) LINK  https://t.me/c/..../1\n"
        "2) YA post FORWARD karo\n"
        "3) YA ID  -100....\n\n"
        "/cancel",
    )


async def cmd_removetarget(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args:
        await reply(
            msg,
            "Usage: /removetarget -1004306407226\n"
            "/targets se ID dekho\n"
            "Saare hataane: /cleartargets\n"
            "Naya single: /settarget",
        )
        return
    ref = parse_chat_id_from_text(" ".join(context.args))
    if ref is None or isinstance(ref, str):
        try:
            ref = int(context.args[0])
        except Exception:
            await reply(msg, "Numeric ID: /removetarget -100...")
            return
    tid = int(ref)
    if remove_target_id(tid):
        left = get_target_ids()
        await reply(
            msg,
            "Removed: %s\n\nActive ab (%s):\n%s\n\nMode: bot list only"
            % (tid, len(left), left or "(none)"),
        )
    else:
        await reply(msg, "List me nahi: %s\nActive: %s" % (tid, get_target_ids()))


async def cmd_cleartargets(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    clear_all_targets()
    await reply(
        msg,
        "Saare targets clear.\n"
        "Railway TARGET_CHAT_ID bhi ignore.\n"
        "Ab kahi forward nahi jayega jab tak:\n"
        "/settarget  YA  /addtarget",
    )


async def cmd_addsource(update, context):
    """Same easy flow as /addtarget — link, forward, or ID."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        await reply(msg, "Admin only.")
        return

    if context.args:
        raw = " ".join(context.args)
        ref = parse_chat_id_from_text(raw)
        if ref is None:
            await reply(
                msg,
                "Invalid.\n"
                "/addsource -1003415196836\n"
                "/addsource https://t.me/c/3415196836/1",
            )
            return
        cid, title = await resolve_chat_ref(context.bot, ref)
        if cid is None:
            await reply(
                msg,
                "Resolve fail: %s\n"
                "Bot ko SOURCE channel me Admin banao, phir try."
                % title,
            )
            return
        data = load_data()
        data["source_id"] = cid
        data.setdefault("settings", {})["awaiting"] = ""
        save_data(data)
        await reply(
            msg,
            "✅ SOURCE SET\n"
            "Name: %s\n"
            "ID: %s\n\n"
            "Is channel se forward hoga.\n"
            "/targets — check\n"
            "/clearsource — ENV pe wapas"
            % (title, cid),
        )
        return

    data = load_data()
    data.setdefault("settings", {})["awaiting"] = "add_source"
    save_data(data)
    await reply(
        msg,
        "SOURCE ADD MODE ON\n\n"
        "Ab bhejo (koi ek):\n\n"
        "1) Source channel LINK\n"
        "   https://t.me/c/3415196836/10\n\n"
        "2) Source channel se koi post\n"
        "   yahan FORWARD karo\n\n"
        "3) Seedha ID\n"
        "   -1003415196836\n\n"
        "Bot us channel me Admin hona chahiye.\n"
        "/cancel se band.",
    )


async def cmd_setsource(update, context):
    # alias of addsource
    await cmd_addsource(update, context)


async def cmd_clearsource(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data["source_id"] = 0
    data.setdefault("settings", {})["awaiting"] = ""
    save_data(data)
    await reply(
        msg,
        "Source cleared.\n"
        "Ab ENV SOURCE_CHAT_ID use hoga: %s"
        % (SOURCE_CHAT_ID or "(not set in Railway)"),
    )


async def cmd_cancel(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data.setdefault("settings", {})["awaiting"] = ""
    save_data(data)
    await reply(msg, "Cancelled. Awaiting mode off.")


async def cmd_forward_help(update, context):
    """/forward with no args = help; with args = start job."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        await reply(msg, "Admin only.")
        return

    if not context.args:
        st = get_settings()
        await reply(
            msg,
            "FORWARD MESSAGES\n\n"
            "UPAR SE start (message 1 se):\n"
            "/forward 1 13365\n\n"
            "Usage:\n"
            "/forward FROM TO\n"
            "/forward FROM TO SKIP DELAY\n\n"
            "Examples:\n"
            "/forward 1 13365\n"
            "  → bilkul id 1 se start\n"
            "/forward 1 13365 0 0.35\n"
            "  → same, skip=0\n"
            "/forward 5000 13365\n"
            "  → id 5000 se (resume)\n\n"
            "⚠️ SKIP sirf tab lagta hai jab\n"
            "tum 3rd number LIKHO.\n"
            "Saved /skip ab auto-apply NAHI.\n\n"
            "Last job: %s -> %s (at %s)\n"
            "Resume tip: /forward %s %s\n\n"
            "/status  /stop  /skip 0"
            % (
                st.get("last_from", 1),
                st.get("last_to", 0),
                st.get("last_current", 0),
                (st.get("last_current") or st.get("last_from") or 1),
                st.get("last_to") or "TO",
            ),
        )
        return

    global fwd_task
    if fwd["running"]:
        await reply(msg, "Forward already running.\n/status or /stop")
        return

    try:
        from_id = int(context.args[0])
        to_id = int(context.args[1]) if len(context.args) > 1 else from_id
        # IMPORTANT: default skip = 0 always.
        # Do NOT use saved skip_number — that made /forward 1 … start mid-way.
        if len(context.args) > 2:
            skip = int(context.args[2])
        else:
            skip = 0
        delay = float(context.args[3]) if len(context.args) > 3 else float(
            get_settings().get("delay", 0.35) or 0.35
        )
    except ValueError:
        await reply(msg, "Numbers only.\nExample: /forward 1 13365")
        return

    if from_id < 1 or to_id < from_id:
        await reply(msg, "Need FROM >= 1 and TO >= FROM")
        return
    if to_id - from_id > 200000:
        await reply(msg, "Range too large")
        return
    if skip < 0:
        skip = 0
    delay = min(max(delay, 0.2), 5.0)

    if not get_target_ids():
        await reply(msg, "No targets. Pehle /settarget ya /addtarget.")
        return
    if not get_source_id():
        await reply(msg, "No source. Pehle /addsource.")
        return

    real_start = from_id + skip
    if real_start > to_id:
        await reply(msg, "Skip too large — start id %s > TO %s" % (real_start, to_id))
        return

    # Persist skip=0 so old saved skip cannot surprise next time
    try:
        data = load_data()
        data.setdefault("settings", {})["skip_number"] = skip
        save_data(data)
    except Exception:
        pass

    fwd_task = asyncio.create_task(
        run_forward(context, user.id, from_id, to_id, skip, delay)
    )
    await reply(
        msg,
        "Forward job queued ✅\n\n"
        "FROM: %s\n"
        "TO: %s\n"
        "SKIP: %s\n"
        "▶ REAL START ID: %s\n"
        "DELAY: %ss\n\n"
        "Upar se chahiye? Start ID = 1 hona chahiye.\n"
        "/status  /stop"
        % (from_id, to_id, skip, real_start, delay),
    )


async def cmd_bulk(update, context):
    # alias - tell user about /forward but still work
    if update.effective_message and not context.args:
        await reply(
            update.effective_message,
            "Note: /bulk is now /forward\nSame usage: /forward FROM TO",
        )
    await cmd_forward_help(update, context)


async def cmd_stop(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not fwd["running"]:
        await reply(msg, "No ongoing forward task.")
        return
    fwd_stop.set()
    fwd["status"] = "cancelled"
    await reply(msg, "Stop signal sent...\n" + status_text())


async def cmd_status(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    src = "?"
    try:
        src = (await context.bot.get_chat(get_source_id())).title or str(get_source_id())
    except Exception as e:
        src = "ERR " + str(e)
    tlines = []
    for tid in get_target_ids()[:10]:
        try:
            t = await context.bot.get_chat(tid)
            tlines.append("- %s (%s)" % (t.title or tid, tid))
        except Exception as e:
            tlines.append("- %s ERR %s" % (tid, e))
    await reply(
        msg,
        "Source: %s\nTargets:\n%s\n\n%s"
        % (src, "\n".join(tlines) or "(none — /addtarget)", status_text()),
    )


async def cmd_settings(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    st = get_settings()
    targets = get_target_ids()
    await reply(
        msg,
        "SETTINGS\n\n"
        "SOURCE: %s\n"
        "TARGETS (%s): %s\n"
        "/targets  /addtarget  /removetarget\n\n"
        "Skip number: %s  → /skip 5\n"
        "Delay: %ss → /delay 0.35\n"
        "Skip duplicates: %s → /duplicates on|off\n"
        "  (bulk forward ALWAYS ignores dup — no mid gaps)\n\n"
        "Last forward: %s -> %s (at %s)\n"
        "Resume: /forward %s %s 0\n"
        % (
            get_source_id(),
            len(targets),
            ", ".join(str(t) for t in targets[:5]) + ("..." if len(targets) > 5 else ""),
            st.get("skip_number", 0),
            st.get("delay", 0.35),
            st.get("skip_duplicates", False),
            st.get("last_from", 1),
            st.get("last_to", 0),
            st.get("last_current", 0),
            (st.get("last_current") or st.get("last_from") or 1),
            st.get("last_to") or "TO",
        ),
    )


async def cmd_skip(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args:
        await reply(
            msg,
            "SKIP info\n\n"
            "Upar se forward (message 1 se):\n"
            "/forward 1 13365\n\n"
            "Saved /skip ab AUTO use nahi hota.\n"
            "Skip tabhi jab 3rd number likho:\n"
            "/forward 1 13365 100\n"
            "  → start = 101\n\n"
            "Resume better:\n"
            "/forward 5000 13365\n\n"
            "Saved skip value: %s"
            % get_settings().get("skip_number", 0),
        )
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await reply(msg, "Number only. /skip 0")
        return
    if n < 0:
        n = 0
    data = load_data()
    data.setdefault("settings", {})["skip_number"] = n
    save_data(data)
    await reply(
        msg,
        "Skip value saved: %s\n"
        "Lekin /forward 1 TO phir bhi 1 se start karega\n"
        "jab tak /forward 1 TO %s na likho."
        % (n, n),
    )


async def cmd_delay(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args:
        await reply(msg, "Usage: /delay 0.25\nCurrent: %s" % get_settings().get("delay"))
        return
    try:
        d = float(context.args[0])
    except ValueError:
        await reply(msg, "Number only")
        return
    d = min(max(d, 0.08), 5.0)
    data = load_data()
    data.setdefault("settings", {})["delay"] = d
    save_data(data)
    await reply(msg, "Delay set to %ss" % d)


async def cmd_duplicates(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args:
        await reply(msg, "Usage: /duplicates on|off")
        return
    on = context.args[0].lower() in ("on", "1", "true", "yes")
    data = load_data()
    data.setdefault("settings", {})["skip_duplicates"] = on
    save_data(data)
    await reply(msg, "Skip duplicates: %s" % on)


async def cmd_reset(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data["settings"] = default_data()["settings"]
    save_data(data)
    seen_ids.clear()
    await reply(msg, "Settings reset. Filters/buttons/caption kept.\n/status")


async def cmd_unequify(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    seen_ids.clear()
    await reply(
        msg,
        "Duplicate memory cleared.\n"
        "Bulk forward ignores caption-dup (no mid gaps).\n"
        "Live auto: /duplicates off  (recommended)",
    )


async def cmd_copy(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args or not context.args[0].isdigit():
        await reply(msg, "Usage: /copy 13365")
        return
    mid = int(context.args[0])
    r = await forward_one(context, mid)
    await reply(msg, "Forward result: %s (id %s)" % (r, mid))


async def cmd_test(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return
    targets = get_target_ids()
    if not targets:
        await reply(msg, "No targets. /addtarget pehle.")
        return
    try:
        kb = build_keyboard()
        text = (
            "<b>BATCH NAME : Test</b>\n"
            "====================\n"
            "Dm:- test"
        )
        text = apply_word_filters_html(text)
        cap = get_caption_settings()
        parts = []
        if cap.get("header"):
            parts.append(escape(cap["header"]))
        parts.append(text)
        if cap.get("footer"):
            parts.append(escape(cap["footer"]))
        body = "\n\n".join(parts)
        ids = []
        for tid in targets:
            sent = await context.bot.send_message(
                tid,
                body,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            ids.append("%s:%s" % (tid, sent.message_id))
        await reply(msg, "Test OK on %s targets\n%s" % (len(ids), "\n".join(ids)))
    except Exception as e:
        await reply(msg, "Fail: " + str(e))


async def cmd_block(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args:
        await reply(msg, "Usage: /block spam")
        return
    w = " ".join(context.args).strip().lower()
    data = load_data()
    if w not in data["remove"]:
        data["remove"].append(w)
        data.get("replace", {}).pop(w, None)
        save_data(data)
    await reply(msg, "Blocked (remove): " + w)


async def cmd_replace(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args or len(context.args) < 2:
        await reply(msg, "Usage: /replace @ZCYT_2026 @RicxyBhai")
        return
    old = context.args[0].strip().lower()
    new = " ".join(context.args[1:])
    data = load_data()
    data["remove"] = [x for x in data.get("remove", []) if x != old]
    data.setdefault("replace", {})[old] = new
    save_data(data)
    await reply(msg, "Replace: %s => %s" % (old, new))


async def cmd_unblock(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args:
        await reply(msg, "Usage: /unblock word")
        return
    w = " ".join(context.args).strip().lower()
    data = load_data()
    data["remove"] = [x for x in data.get("remove", []) if x != w]
    save_data(data)
    await reply(msg, "Unblocked: " + w)


async def cmd_unreplace(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args:
        await reply(msg, "Usage: /unreplace word")
        return
    w = context.args[0].strip().lower()
    data = load_data()
    data.get("replace", {}).pop(w, None)
    save_data(data)
    await reply(msg, "Unreplace: " + w)


async def cmd_listfilters(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    rem, rep = get_remove_words(), get_replace_map()
    cap = get_caption_settings()
    lines = ["FILTERS", "", "REMOVE:"] + (["- " + w for w in rem] or ["(none)"])
    lines += ["", "REPLACE:"]
    lines += ["%s => %s" % (k, v) for k, v in rep.items()] or ["(none)"]
    lines += [
        "",
        "CAPTION header: " + (cap.get("header") or "off"),
        "CAPTION footer: " + (cap.get("footer") or "off"),
        "TEMPLATE: " + ("ON" if cap.get("template") else "off"),
    ]
    await reply(msg, "\n".join(lines))


async def cmd_button(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    raw = " ".join(context.args) if context.args else ""
    if "|" not in raw:
        await reply(
            msg,
            "Add URL button under posts:\n"
            "/button Contact Us | https://t.me/RicxyBhai\n"
            "/button2 Join | https://t.me/channel  (same row)\n"
            "/buttons\n/buttonclear",
        )
        return
    text, url = raw.split("|", 1)
    text, url = text.strip(), url.strip()
    if not url.startswith(("http://", "https://", "tg://")):
        url = "https://" + url
    data = load_data()
    data.setdefault("buttons", []).append([{"text": text, "url": url}])
    save_data(data)
    await reply(msg, "Button added:\n%s\n%s" % (text, url))


async def cmd_button2(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    raw = " ".join(context.args) if context.args else ""
    if "|" not in raw:
        await reply(msg, "Usage: /button2 Text | https://url")
        return
    text, url = raw.split("|", 1)
    text, url = text.strip(), url.strip()
    if not url.startswith(("http://", "https://", "tg://")):
        url = "https://" + url
    data = load_data()
    data.setdefault("buttons", [])
    if not data["buttons"]:
        data["buttons"].append([])
    data["buttons"][-1].append({"text": text, "url": url})
    save_data(data)
    await reply(msg, "Button same row: %s" % text)


async def cmd_buttons(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    rows = get_buttons_rows()
    if not rows:
        await reply(msg, "No buttons. /button Contact Us | https://t.me/x")
        return
    lines = ["Buttons:"]
    for i, row in enumerate(rows):
        for b in row:
            lines.append("%s) %s => %s" % (i + 1, b.get("text"), b.get("url")))
    await reply(msg, "\n".join(lines))


async def cmd_buttonclear(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data["buttons"] = []
    save_data(data)
    await reply(msg, "Buttons cleared.")


async def cmd_caption(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    cap = get_caption_settings()
    demo_body = "BATCH NAME : 01 Excel Course\nOur Price: AFFORDABLE\nDm:- @ZCYT_2026"
    demo_body = apply_word_filters_plain(demo_body)
    footer = cap.get("footer") or DEFAULT_OWNER_LINE or "Owner 👤 : @RicxyBhai"
    demo = demo_body + "\n\n" + footer
    await reply(
        msg,
        "YOUR CAPTION LAYOUT\n\n"
        "{caption}  = full original (after replace)\n"
        "then Owner line under it\n\n"
        "header: %s\n"
        "footer/owner: %s\n"
        "template: %s\n\n"
        "DEMO OUT:\n%s\n\n"
        "Set owner line:\n"
        "/captionfooter Owner 👤 : @RicxyBhai\n"
        "Clear owner: /captionfooter\n"
        "Custom full template: reply msg + /captiontemplate\n"
        "Default template uses {caption} + owner"
        % (
            cap.get("header") or "off",
            footer or "off",
            "ON" if cap.get("template") else "off",
            demo,
        ),
    )


async def cmd_captionheader(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])
    if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
        data["caption"]["header"] = msg.reply_to_message.text or msg.reply_to_message.caption
    elif context.args:
        data["caption"]["header"] = " ".join(context.args)
    else:
        data["caption"]["header"] = ""
        save_data(data)
        await reply(msg, "Header cleared")
        return
    save_data(data)
    await reply(msg, "Header set")


async def cmd_captionfooter(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])
    if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
        data["caption"]["footer"] = msg.reply_to_message.text or msg.reply_to_message.caption
        data["caption"]["footer_cleared"] = False
    elif context.args:
        data["caption"]["footer"] = " ".join(context.args)
        data["caption"]["footer_cleared"] = False
    else:
        data["caption"]["footer"] = ""
        data["caption"]["footer_cleared"] = True
        save_data(data)
        await reply(msg, "Footer cleared (Owner line off)")
        return
    # Keep template in sync if it was default owner template
    tpl = data["caption"].get("template") or ""
    if "{caption}" in tpl and "Owner" in tpl:
        data["caption"]["template"] = (
            "{caption}\n\n" + data["caption"]["footer"]
        )
    save_data(data)
    await reply(
        msg,
        "Footer / Owner line set:\n%s\n\n"
        "Har post pe:\n"
        "1) Full original caption (replace ke baad)\n"
        "2) Niche ye line"
        % data["caption"]["footer"],
    )


async def cmd_captiontemplate(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data.setdefault("caption", default_data()["caption"])
    if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
        data["caption"]["template"] = msg.reply_to_message.text or msg.reply_to_message.caption
    elif context.args and context.args[0].lower() == "clear":
        data["caption"]["template"] = ""
        save_data(data)
        await reply(msg, "Template cleared")
        return
    elif context.args:
        data["caption"]["template"] = " ".join(context.args)
    else:
        await reply(msg, "Reply to template with /captiontemplate\nOr /captiontemplate clear")
        return
    save_data(data)
    await reply(msg, "Template saved")


async def cmd_captionclear(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    data["caption"] = default_data()["caption"]
    save_data(data)
    await reply(msg, "Caption cleared")


async def on_error(update, context):
    logger.exception("err %s", context.error)
    # Try notify admin so bot never looks fully dead
    try:
        err = str(context.error or "")[:300]
        chat = None
        if update is not None:
            if getattr(update, "effective_chat", None):
                chat = update.effective_chat
            elif getattr(update, "callback_query", None) and update.callback_query.message:
                chat = update.callback_query.message.chat
        if chat and chat.type == "private":
            await context.bot.send_message(
                chat.id,
                "Error (bot still running):\n%s\n\n/start /ping /cancel" % err,
            )
    except Exception:
        pass


async def unknown_cmd(update, context):
    """Reply on unknown /commands so user knows bot is alive."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        return
    txt = (msg.text or "").strip()
    await reply(
        msg,
        "Unknown command: %s\n\n"
        "Try:\n"
        "/start  /ping  /menu\n"
        "/settarget  /addtarget  /targets\n"
        "/forward 1 10  /status  /stop\n"
        "/cancel"
        % (txt.split()[0] if txt else "?"),
    )


async def cmd_menu(update, context):
    """Re-open main inline menu."""
    await cmd_start(update, context)


async def cmd_help_cmd(update, context):
    """/help shows full command list + inline buttons."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not user or not is_admin(user.id):
        await reply(msg, "Admin only.")
        return
    await msg.reply_text(HELP_TEXT, reply_markup=help_inline_kb())


async def post_init(app: Application):
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "open main menu"),
                BotCommand("ping", "bot alive check"),
                BotCommand("menu", "open main menu"),
                BotCommand("help", "all commands"),
                BotCommand("forward", "forward messages"),
                BotCommand("status", "forward status panel"),
                BotCommand("stop", "stop ongoing task"),
                BotCommand("targets", "list source + targets"),
                BotCommand("addsource", "add source (link/forward)"),
                BotCommand("addtarget", "add extra target"),
                BotCommand("settarget", "ONLY this target (replace)"),
                BotCommand("removetarget", "remove one target id"),
                BotCommand("cleartargets", "clear all targets"),
                BotCommand("clearsource", "clear bot source"),
                BotCommand("setsource", "same as addsource"),
                BotCommand("settings", "configure settings"),
                BotCommand("skip", "set skip number"),
                BotCommand("unequify", "reset duplicate filter"),
                BotCommand("reset", "reset settings"),
                BotCommand("copy", "forward one message id"),
                BotCommand("button", "add url button"),
                BotCommand("replace", "replace words"),
                BotCommand("listfilters", "show filters"),
                BotCommand("caption", "caption settings"),
                BotCommand("test", "test all targets"),
                BotCommand("cancel", "cancel add mode"),
            ]
        )
    except Exception:
        logger.exception("set_my_commands failed (ok to ignore)")
    logger.info(
        "post_init ok src=%s targets=%s only=%s",
        get_source_id(),
        get_target_ids(),
        load_data().get("targets_only"),
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Config missing: BOT_TOKEN — set Railway Variable BOT_TOKEN")
    try:
        if not DATA_FILE.exists():
            save_data(default_data())
        else:
            # touch-load so corrupt json is logged early
            load_data()
    except Exception:
        logger.exception("data file init")
        try:
            save_data(default_data())
        except Exception:
            pass
    if not SOURCE_CHAT_ID and not get_source_id():
        logger.warning("No SOURCE yet — use /addsource after start")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    app.add_error_handler(on_error)

    for name, fn in [
        ("start", cmd_start),
        ("ping", cmd_ping),
        ("menu", cmd_menu),
        ("help", cmd_help_cmd),
        ("forward", cmd_forward_help),
        ("bulk", cmd_bulk),
        ("status", cmd_status),
        ("stop", cmd_stop),
        ("bulkstop", cmd_stop),
        ("bulkstatus", cmd_status),
        ("targets", cmd_targets),
        ("addsource", cmd_addsource),
        ("addtarget", cmd_addtarget),
        ("settarget", cmd_settarget),
        ("removetarget", cmd_removetarget),
        ("cleartargets", cmd_cleartargets),
        ("clearsource", cmd_clearsource),
        ("setsource", cmd_setsource),
        ("cancel", cmd_cancel),
        ("settings", cmd_settings),
        ("skip", cmd_skip),
        ("delay", cmd_delay),
        ("duplicates", cmd_duplicates),
        ("reset", cmd_reset),
        ("unequify", cmd_unequify),
        ("copy", cmd_copy),
        ("test", cmd_test),
        ("block", cmd_block),
        ("replace", cmd_replace),
        ("unblock", cmd_unblock),
        ("unreplace", cmd_unreplace),
        ("listfilters", cmd_listfilters),
        ("listblocks", cmd_listfilters),
        ("button", cmd_button),
        ("button2", cmd_button2),
        ("buttons", cmd_buttons),
        ("buttonclear", cmd_buttonclear),
        ("caption", cmd_caption),
        ("captionheader", cmd_captionheader),
        ("captionfooter", cmd_captionfooter),
        ("captiontemplate", cmd_captiontemplate),
        ("captionclear", cmd_captionclear),
    ]:
        app.add_handler(CommandHandler(name, fn))

    # Inline menu callbacks (blue buttons)
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(m:|a:)"))

    # Dynamic source: listen all channel posts, filter inside handler
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & ~filters.COMMAND,
            channel_auto_handler,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            manual_forward_handler,
        ),
        group=2,
    )
    # Unknown /commands → still reply (bot alive)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.COMMAND,
            unknown_cmd,
        ),
        group=3,
    )

    logger.info(
        "FORWARD BOT start src=%s targets=%s only=%s",
        get_source_id(),
        get_target_ids(),
        load_data().get("targets_only"),
    )
    # drop_pending_updates=True avoids stuck queue after crash/redeploy
    app.run_polling(
        allowed_updates=[
            "message",
            "channel_post",
            "edited_channel_post",
            "edited_message",
            "callback_query",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
