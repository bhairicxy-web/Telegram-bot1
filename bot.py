#!/usr/bin/env python3
"""
FORWARD BOT - Channel forward with filters, caption, buttons, status panel
Commands styled like popular forward bots: /forward /stop /settings /reset
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
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

# Ye tag /version aur /ping me dikhta hai — pata chalta hai ki
# Railway pe NAYA code chal raha hai ya purana.
BUILD_TAG = "2026-10-04-PREVIEW-FIX-20 (preview quote fix)"

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

# Last target-send error (bulk me admin ko batane ke liye)
last_err_info = {"tid": 0, "err": "", "when": 0.0}

# Last caption jispe kaam hua — /captiondump isko file me nikalta hai
last_cap = {
    "when": 0.0,
    "msg_id": 0,
    "qspan": None,
    "has_quote_entity": False,
    "src_text": "",
    "src_entities": "",
    "final": "",
    "type": "",
    "footer": "",
    "template": "",
}


def remember_caption(msg, final_html, meta):
    """build_caption_from_message har baar isse update karta hai (memory me)."""
    try:
        last_cap["when"] = time.time()
        last_cap["msg_id"] = getattr(msg, "message_id", 0) or 0
        last_cap["src_text"] = (msg.text or msg.caption or "") or ""
        ents = msg.caption_entities or msg.entities or []
        lines = []
        for e in ents:
            lines.append(
                "  - %s  offset=%s len=%s%s"
                % (
                    getattr(e, "type", "?"),
                    getattr(e, "offset", "?"),
                    getattr(e, "length", "?"),
                    "  url=%s" % e.url if getattr(e, "url", None) else "",
                )
            )
        last_cap["src_entities"] = "\n".join(lines) or "  (koi formatting nahi)"
        last_cap["final"] = final_html or ""
        last_cap["type"] = str(meta.get("type") or "")
        cap = get_caption_settings()
        last_cap["footer"] = cap.get("footer") or ""
        last_cap["template"] = cap.get("template") or ""
    except Exception:
        logger.exception("remember_caption")


def default_data():
    return {
        "remove": [],
        "replace": {},
        "caption": {
            "header": "",
            # Always under original caption after replace (user request)
            "footer": DEFAULT_OWNER_LINE or "Owner 👤 : @RicxyBhai",
            "replace_all": "",
            # Template OFF by default: original caption (formatting kept) + owner line.
            # Ye {caption} template caption ko chhota/truncate kar sakta tha,
            # isliye default khaali rakha gaya. /captiontemplate se ON kar sakte ho.
            "template": "",
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
            # Quote auto: source me quote entity na ho, par list ho (3+ lines
            # same emoji jaise 📌) to bot khud <blockquote expandable> bana de.
            "quote_auto": True,
            "quote_min_lines": 3,
            # LIVE auto-forward (channel me naya post -> turant target).
            # DEFAULT OFF (user request): source me post aayegi par target me
            # nahi jayegi jab tak /auto on na karo.
            "auto_forward": False,
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
            # Legacy template ka exact purana default -> khaali karo.
            # (Wo caption ko {caption} me daal ke truncate kar deta tha)
            _tpl = (data["caption"].get("template") or "").strip()
            if _tpl in (
                "{caption}\n\nOwner 👤 : @RicxyBhai",
                "{caption}",
            ):
                data["caption"]["template"] = ""
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
        # always a real bool
        data["settings"]["auto_forward"] = bool(
            data["settings"].get("auto_forward", False)
        )
        data["settings"]["quote_auto"] = bool(
            data["settings"].get("quote_auto", True)
        )
        try:
            qml = int(data["settings"].get("quote_min_lines", 3) or 3)
        except Exception:
            qml = 3
        data["settings"]["quote_min_lines"] = min(max(qml, 2), 20)
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


MODE_LABELS = {
    "preview": "PREVIEW MODE (post DM me dikhti hai, TARGET me NAHI jati)",
    "add_target": "ADD TARGET MODE (agli post target ban jayegi)",
    "set_target": "SET TARGET MODE (agli post target ban jayegi)",
    "add_source": "ADD SOURCE MODE (agli post source ban jayegi)",
    "set_source": "ADD SOURCE MODE (agli post source ban jayegi)",
}


def get_mode():
    """Abhi koi 'awaiting' mode on hai? ('' = normal, sab kaam karega)"""
    try:
        m = (load_data().get("settings") or {}).get("awaiting") or ""
    except Exception:
        m = ""
    return m


def mode_warning():
    """Agar koi mode on hai to bada warning, warna ''."""
    m = get_mode()
    if not m:
        return ""
    return (
        "\n⚠️⚠️ MODE ON: %s\n"
        "   Is mode me jawab aayega, forwarding NAHI hoga!\n"
        "   Band karne ke liye: /cancel\n"
        % MODE_LABELS.get(m, m)
    )


def get_settings():
    return (load_data().get("settings") or default_data()["settings"])


def get_source_id():
    data = load_data()
    sid = int(data.get("source_id") or 0)
    return sid if sid else SOURCE_CHAT_ID


# Ye ids target list me thi par PRIVATE chat (DM) hain -> auto hata di gayi
PRIVATE_TARGETS_FOUND = []


def is_private_chat_id(tid):
    """Channel/supergroup id negative hoti hai (-100...).
    Positive id = user (DM) ya basic group -> target nahi ban sakta."""
    try:
        return int(tid) > 0
    except Exception:
        return False


async def target_chat_check(bot, cid):
    """Target channel ke liye validate. Returns (ok, info_text, kind)."""
    if is_private_chat_id(cid):
        return (
            False,
            "❌ Ye PRIVATE CHAT (DM / user id) hai: %s" % cid,
            "private",
        )
    try:
        ch = await bot.get_chat(cid)
        kind = str(getattr(ch, "type", "?"))
        title = ch.title or ch.username or str(cid)
        if kind == "private":
            return False, "❌ Ye PRIVATE CHAT hai: %s" % cid, kind
        return True, "%s | %s" % (kind, title), kind
    except Exception as e:
        return None, "(type check fail: %s)" % e, "?"


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
    bad = []
    for tid in pool:
        try:
            tid = int(tid)
        except Exception:
            continue
        # ---- PRIVATE (DM) id? -> target nahi ----
        if is_private_chat_id(tid):
            bad.append(tid)
            if tid not in PRIVATE_TARGETS_FOUND:
                PRIVATE_TARGETS_FOUND.append(tid)
            continue
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    # saved list me padhi private ids ko AUTO saaf kar do (ek hi baar)
    if bad and isinstance(data.get("targets"), list):
        kept = [t for t in data["targets"] if not is_private_chat_id(t)]
        if kept != data["targets"]:
            data["targets"] = kept
            save_data(data)
            logger.warning("private (DM) ids target list se hataye: %s", bad)
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
    PRIVATE (DM/user) id add NAHI hoti — warna post channel ki jagah DM me jati hai.
    """
    tid = int(tid)
    if is_private_chat_id(tid):
        logger.warning("private id target add karne ki koshish: %s", tid)
        return False
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
    """Replace entire target list (bot-only mode). Private ids skip."""
    data = load_data()
    out = []
    seen = set()
    for t in ids:
        t = int(t)
        if is_private_chat_id(t):
            logger.warning("private id set_target me skip: %s", t)
            continue
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


def _ib(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def home_inline_kb():
    """Main /start inline menu — saaf, sirf hamare naam."""
    return InlineKeyboardMarkup(
        [
            [_ib("📤 FORWARD", "m:forward"), _ib("📊 STATUS", "m:status")],
            [_ib("🏷 CHANNELS", "m:channels"), _ib("🖋 CAPTION", "m:caption")],
            [_ib("🕵️ FILTERS", "m:filters"), _ib("⬜ BUTTONS", "m:buttons")],
            [_ib("✅ CHECK", "a:check"), _ib("🛑 STOP", "a:stop")],
            [_ib("⚙️ SETTINGS", "m:settings"), _ib("🎓 HOW TO USE", "m:how")],
            [_ib("👤 HELP", "m:help"), _ib("ℹ️ ABOUT", "m:about")],
        ]
    )


def settings_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("🏷 CHANNELS", "m:channels"), _ib("🖋 CAPTION", "m:caption")],
            [_ib("🕵️ FILTERS", "m:filters"), _ib("⬜ BUTTONS", "m:buttons")],
            [_ib("🔴 LIVE / AUTO", "a:autotoggle")],
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
            [_ib("🔎 FULL CHECK", "a:check")],
            [_ib("🆘 PANIC (auto fix)", "a:panic")],
            [_ib("◀️ BACK", "m:settings")],
        ]
    )


def caption_inline_kb():
    return InlineKeyboardMarkup(
        [
            [_ib("📋 SHOW CAPTION", "a:caption"), _ib("👤 OWNER / FOOTER", "m:footer_info")],
            [_ib("⬆️ HEADER", "m:header_info"), _ib("📝 TEMPLATE", "m:template_info")],
            [_ib("🔲 QUOTE AUTO", "a:quotetoggle"), _ib("❓ QUOTE INFO", "m:quote_info")],
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


def home_text(name="ADMIN"):
    """Saaf home screen: greeting + status + menu ka ishara. Koi command list nahi."""
    try:
        src = get_source_id()
        tg = get_target_ids()
        auto = "ON" if get_settings().get("auto_forward", False) else "OFF"
    except Exception:
        src, tg, auto = 0, [], "OFF"
    src_line = str(src) if src else "❌ set nahi (menu → CHANNELS)"
    tg_line = ("%s channel ✅" % len(tg)) if tg else "❌ koi nahi (menu → CHANNELS)"
    return (
        "👋 HELLO %s\n\n"
        "📤 CHANNEL ➜ CHANNEL FORWARD\n"
        "━━━━━━━━━━━━━━━━━\n"
        "📥 Source : %s\n"
        "🎯 Targets: %s\n"
        "🔴 Auto   : %s\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        "Neeche ke buttons se sab kuch chalao 👇"
        % (name, src_line, tg_line, auto)
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
    "/auto on | off  (live post on/off)\n"
    "/preview  (caption ka test DM me)\n"
    "/captiondump  (caption .txt file)\n"
    "/inspect ID  (quote/formatting check)\n"
    "/quote on|off  (purple bar auto)\n"
    "/check  (sab test + error)\n"
    "/panic  (ek command me fix)\n"
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
BLOCKQUOTE_TYPES = {"blockquote", "expandable_blockquote"}
try:  # PTB 21.x
    BLOCKQUOTE_TYPES.add(str(MessageEntity.BLOCKQUOTE))
    BLOCKQUOTE_TYPES.add(str(MessageEntity.EXPANDABLE_BLOCKQUOTE))
except Exception:
    pass
EXPANDABLE_TYPES = {"expandable_blockquote"}
try:
    EXPANDABLE_TYPES.add(str(MessageEntity.EXPANDABLE_BLOCKQUOTE))
except Exception:
    pass


def _is_blockquote(t):
    return str(t) in BLOCKQUOTE_TYPES


def _is_expandable_quote(t):
    return str(t) in EXPANDABLE_TYPES


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
        # ---- QUOTE (source ki purple bar + collapse) ----
        # Ye pehle missing tha -> quote flat ho jata tha
        if _is_expandable_quote(t):
            return "<blockquote expandable>"
        if _is_blockquote(t):
            return "<blockquote>"
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
        if _is_blockquote(t):
            return "</blockquote>"
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


def _filter_segment(seg):
    """Ek HTML text-segment pe replace/block lagao (tags ko chhua nahi)."""
    if not seg:
        return seg
    out = seg
    rmap = get_replace_map()
    for old in sorted(rmap.keys(), key=len, reverse=True):
        new = rmap[old]
        out = re.compile(re.escape(old), re.IGNORECASE).sub(
            (new if "<" in new else escape(new)), out
        )
    for w in get_remove_words():
        if w in rmap:
            continue
        out = re.compile(re.escape(w), re.IGNORECASE).sub("", out)
    # thoda saaf: extra space / newline (sirf text me, entity break nahi)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    return out


_BULLET_MARKERS = ("-", "•", "*", "➤", ">", "»", "–", "—")


def _line_marker(line):
    """Line ka leading emoji/bullet marker (jaise 📌). Warna ''."""
    s = (line or "").strip()
    if not s:
        return ""
    m = re.match(r"^([^\w\s]{1,4})\s*", s)
    if not m:
        return ""
    tok = m.group(1)
    if not tok:
        return ""
    if any(ord(c) > 0x2000 for c in tok) or tok in _BULLET_MARKERS:
        return tok
    return ""


def detect_quote_span(text, min_lines=3):
    """Caption me ek hi marker wali lagatar lines (>=min_lines) dhundo.
    Returns (start_char, end_char, marker) ya None."""
    if not text or "\n" not in text:
        return None
    lines = text.split("\n")
    starts = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    groups = []
    i = 0
    while i < len(lines):
        mk = _line_marker(lines[i])
        if not mk:
            i += 1
            continue
        j = i
        while j + 1 < len(lines) and _line_marker(lines[j + 1]) == mk:
            j += 1
        if (j - i + 1) >= min_lines:
            groups.append((i, j, mk))
        i = j + 1
    if not groups:
        return None
    gi, gj, gmk = max(groups, key=lambda g: g[1] - g[0])
    start = starts[gi]
    end = starts[gj] + len(lines[gj])
    return start, end, gmk


def _u16_to_py(text, u):
    units = 0
    i = 0
    while i < len(text) and units < u:
        units += 1 if ord(text[i]) <= 0xFFFF else 2
        i += 1
    return i


def _py_to_u16(text, idx):
    return sum(1 if ord(c) <= 0xFFFF else 2 for c in text[:idx])


def entities_to_html_range(text, entities, a=0, b=None):
    """text[a:b] ka HTML — entities ko is range me clip karke (offset relative)."""
    if b is None:
        b = len(text)
    sub = text[a:b]
    if not entities:
        return escape(sub)
    clipped = []
    for e in entities:
        try:
            cs = _u16_to_py(text, getattr(e, "offset", 0))
            ce = _u16_to_py(
                text,
                getattr(e, "offset", 0) + getattr(e, "length", 0),
            )
        except Exception:
            continue
        s, t = max(cs, a), min(ce, b)
        if s >= t:
            continue
        off = _py_to_u16(sub, s - a)
        ln = _py_to_u16(sub, t - a) - off
        if ln <= 0:
            continue
        try:
            clipped.append(
                MessageEntity(
                    type=e.type,
                    offset=off,
                    length=ln,
                    url=getattr(e, "url", None),
                    user=getattr(e, "user", None),
                )
            )
        except Exception:
            continue
    return entities_to_html(sub, clipped)


def apply_word_filters_html(html):
    """Filter sirf TEXT pe — pehle wala version <b>/<a href> ke andar bhi
    replace kar deta tha aur formatting toot jati thi."""
    if not html:
        return html
    tag_re = re.compile(r"<[^>]+>")
    out = []
    pos = 0
    for m in tag_re.finditer(html):
        out.append(_filter_segment(html[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_filter_segment(html[pos:]))
    return "".join(out)


def _close_for(tag):
    if tag.lower().startswith("<a "):
        return "</a>"
    name = re.match(r"<([a-zA-Z0-9\-]+)", tag)
    return "</%s>" % name.group(1) if name else ""


def _u16len(s):
    """Telegram limit UTF-16 units me ginta hai (emoji = 2)."""
    return sum(1 if ord(c) <= 0xFFFF else 2 for c in s)


def split_html(html, limit=CAP_LIMIT):
    """HTML ko limit ke andar chunks me todo, tags close/re-open karke.
    Caption 1024 (media) ya 4096 (text) se bada ho to '...' se cut karne ki
    jagah poora content bhejta hai — pehla hissa media ke saath, baaki next msg."""
    if not html:
        return [""]
    if _u16len(html) <= limit:
        return [html]

    tag_re = re.compile(r"</?[a-zA-Z][^>]*>")
    void_tags = ("<br>", "<br/>", "<br />")
    chunks = []
    stack = []  # [(open_tag, close_tag)]
    cur = []
    cur_len = 0

    def open_tail():
        return "".join(c for _, c in stack)

    def head_tags():
        return "".join(o for o, _ in stack)

    def budget():
        return limit - cur_len - _u16len(open_tail())

    def flush():
        nonlocal cur, cur_len
        chunks.append("".join(cur) + open_tail())
        head = head_tags()
        cur = [head] if head else []
        cur_len = _u16len(head)

    def add_raw(tag):
        nonlocal cur_len
        if cur_len + len(tag) > limit:
            flush()
        cur.append(tag)
        cur_len += len(tag)

    def take_units(text, budget_units):
        units = 0
        i = 0
        while i < len(text):
            u = 1 if ord(text[i]) <= 0xFFFF else 2
            if units + u > budget_units:
                break
            units += u
            i += 1
        return i

    def add_text(text):
        nonlocal cur_len
        while text:
            b = budget()
            if b <= 0:
                flush()
                continue
            i = take_units(text, b)
            if i <= 0:
                flush()
                continue
            piece, text = text[:i], text[i:]
            if text:
                # entity beech me na kato (jaise &amp;)
                amp = piece.rfind("&")
                if amp != -1 and ";" not in piece[amp:]:
                    text = piece[amp:] + text
                    piece = piece[:amp]
            if not piece:
                flush()
                continue
            cur.append(piece)
            cur_len += _u16len(piece)

    pos = 0
    for m in tag_re.finditer(html):
        add_text(html[pos:m.start()])
        tag = m.group(0)
        if tag.startswith("</"):
            low = tag.lower()
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][1].lower() == low:
                    stack.pop(i)
                    break
            add_raw(tag)
        else:
            add_raw(tag)
            if tag.lower() not in void_tags and not tag.endswith("/>"):
                cl = _close_for(tag)
                if cl:
                    stack.append((tag, cl))
        pos = m.end()
    add_text(html[pos:])
    if cur:
        chunks.append("".join(cur))
    return [c for c in chunks if c]


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

    # ---- QUOTE AUTO ----
    # Source me quote entity ho -> wahi use karo.
    # Na ho, par list (📌/🔹 jaise 3+ lagatar lines) ho -> bot khud quote banaye.
    _st = get_settings()
    has_quote_entity = any(
        _is_blockquote(getattr(e, "type", "")) for e in entities
    )
    qspan = None
    if not has_quote_entity and _st.get("quote_auto", True):
        try:
            qspan = detect_quote_span(
                original, int(_st.get("quote_min_lines", 3) or 3)
            )
        except Exception:
            logger.exception("detect_quote_span")
            qspan = None
    last_cap["qspan"] = qspan
    last_cap["has_quote_entity"] = has_quote_entity

    if qspan:
        a, b = qspan[0], qspan[1]

        def _part(x, y):
            seg = original[x:y]
            if not seg:
                return ""
            if entities:
                return apply_word_filters_html(
                    entities_to_html_range(original, entities, x, y)
                )
            return escape(apply_word_filters_plain(seg))

        before = _part(0, a)
        inner = _part(a, b)
        after = _part(b, len(original))
        # quote se pehle newline ensure
        if before and not before.endswith("\n"):
            before += "\n"
        body_html = (
            before
            + "<blockquote expandable>"
            + inner
            + "</blockquote>"
            + after
        )
    # Keep full formatted caption when possible
    elif original and entities:
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

    # NOTE: pehle yahan caption 1024 pe "..." se cut hoti thi.
    # Ab nahi karte — send_to_one_chat split_html() se hisson me bhejta hai.
    out = sanitize_blockquotes(out)
    remember_caption(msg, out, meta)
    return out, ParseMode.HTML if out else None, meta


def err_line():
    """Status panel me last send error (silent fail ka asli reason)."""
    try:
        e = last_err_info.get("err") or ""
        if not e:
            return ""
        if time.time() - (last_err_info.get("when") or 0) > 3600:
            return ""
        return (
            "\n🚨 LAST SEND ERROR (target %s):\n   %s\n"
            % (last_err_info.get("tid"), e[:220])
        )
    except Exception:
        return ""


def status_text():
    s = fwd
    warn = ""
    try:
        sent_any = (s.get("ok") or 0) > 0
        tried = (s.get("fail") or 0) + (s.get("skipped") or 0)
        if s.get("running") and not sent_any and tried > 0:
            warn = (
                "\n⚠️ KUCH BHI SEND NAHI HUA — /check chalao\n"
                "   (target permission / bot admin check)\n"
            )
    except Exception:
        warn = ""
    try:
        warn = warn + mode_warning()
    except Exception:
        pass
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
        + warn
        + err_line()
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
        parts = split_html(cap, 4000)
        for i, part in enumerate(parts):
            k = dict(kw)
            if i == len(parts) - 1 and keyboard:
                k["reply_markup"] = keyboard
            elif i != len(parts) - 1:
                k.pop("reply_markup", None)
            await context.bot.send_message(chat_id=chat_id, text=part, **k)
        return "ok"

    # caption 1024 se bada -> pehla hissa media ke saath, baaki text message me
    parts = split_html(cap, CAP_LIMIT) if cap else [""]
    cap = parts[0] or None
    extra = parts[1:]
    if extra:
        kw.pop("reply_markup", None)

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

    # long caption ke bache hue hisse + buttons last message pe
    if extra:
        for i, part in enumerate(extra):
            k = {}
            if html and parse_mode:
                k["parse_mode"] = parse_mode
            if keyboard and i == len(extra) - 1:
                k["reply_markup"] = keyboard
            await context.bot.send_message(chat_id=chat_id, text=part, **k)
    return "ok"


async def _apply_caption_to_message(context, tid, sent, cleaned, parse_mode, keyboard):
    """Copied message ka caption/text EDIT karo (naya message nahi bhejte).
    Return True agar caption set ho gaya (post har haal me target me hai)."""
    has_media = bool(
        sent.photo
        or sent.video
        or sent.document
        or sent.audio
        or sent.voice
        or sent.animation
    )
    limit = CAP_LIMIT if has_media else 4000
    parts = split_html(cleaned or "", limit) if cleaned else [""]
    head = parts[0] if parts else ""
    extra = parts[1:]

    async def _edit(cap_text, mode):
        if has_media:
            await context.bot.edit_message_caption(
                chat_id=tid,
                message_id=sent.message_id,
                caption=(cap_text or None),
                parse_mode=mode,
                reply_markup=keyboard if not extra else None,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=tid,
                message_id=sent.message_id,
                text=(cap_text or "-"),
                parse_mode=mode,
                reply_markup=keyboard if not extra else None,
            )

    ok = False
    try:
        await _edit(head, parse_mode if head else None)
        ok = True
    except TelegramError as e:
        err = str(e).lower()
        logger.warning("caption edit fail %s: %s", tid, e)
        if "not modified" in err:
            ok = True  # caption pehle se wahi hai
        else:
            # plain text se ek aur koshish (parse/entity problem)
            try:
                plain = re.sub(r"<[^>]+>", "", head or "")
                await _edit(plain, None)
                ok = True
            except TelegramError as e2:
                if "not modified" in str(e2).lower():
                    ok = True
                else:
                    logger.warning("caption edit plain bhi fail %s: %s", tid, e2)

    # lamba caption -> bache hue hisse alag message me (post to ja chuki hai)
    for i, part in enumerate(extra):
        try:
            await context.bot.send_message(
                chat_id=tid,
                text=part,
                parse_mode=parse_mode,
                reply_markup=keyboard if i == len(extra) - 1 else None,
            )
        except TelegramError:
            try:
                await context.bot.send_message(
                    chat_id=tid, text=re.sub(r"<[^>]+>", "", part)
                )
            except TelegramError:
                logger.exception("extra part fail %s", tid)
    return ok


async def _copy_edit_to_target(context, source, tid, mid, cleaned, parse_mode, keyboard):
    """Ek target: copy + caption edit (no temp copy, no delete)."""
    sent = await context.bot.copy_message(
        chat_id=tid, from_chat_id=source, message_id=mid
    )
    ok = await _apply_caption_to_message(
        context, tid, sent, cleaned, parse_mode, keyboard
    )
    return sent, ok


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
                # 1) quote ko normal banao (formatting bachi rahe)
                if "<blockquote expandable>" in (cleaned or ""):
                    try:
                        st = await send_to_one_chat(
                            context, msg, tid, _degrade_quotes(cleaned),
                            parse_mode, keyboard, True
                        )
                        return st
                    except TelegramError as e3:
                        logger.warning("quote degrade fail: %s", e3)
                # 2) phir sab tags hata ke plain bhejo
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
        try:
            last_err_info["tid"] = tid
            last_err_info["err"] = str(last_err)
            last_err_info["when"] = time.time()
        except Exception:
            pass
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


# LIVE mode OFF hone par admin ko max 1 baar (per minute) batane ke liye
_live_notice_last = 0.0
LIVE_NOTICE_GAP_S = 600.0


async def _live_off_notice(context, chat):
    """AUTO off hone par naya source post aaya -> admin ko chhota notice."""
    global _live_notice_last
    now = time.time()
    if now - _live_notice_last < LIVE_NOTICE_GAP_S:
        return
    _live_notice_last = now
    admin = None
    for a in sorted(ADMIN_IDS):
        admin = a
        break
    if not admin:
        return
    try:
        await context.bot.send_message(
            admin,
            "⏸ AUTO FORWARD: OFF\n\n"
            "Source me naya post aaya (%s)\n"
            "par forward NAHI kiya.\n\n"
            "Chalu karne ke liye: /auto on"
            % (chat.title or chat.id),
        )
    except Exception:
        logger.exception("live notice")


async def channel_auto_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.id != get_source_id():
        return
    # ---- LIVE switch ----
    if not get_settings().get("auto_forward", False):
        await _live_off_notice(context, chat)
        return
    try:
        await send_cleaned_to_target(context, msg, check_dup=True)
    except Exception:
        logger.exception("auto")


async def preview_one(context, msg, chat_id):
    """Post jaisa dikhega waisa hi DM me bhejo (kisi target me NAHI).

    Tarika 1 (best): post SOURCE channel se forward hui ho to us ASLI
    message ki COPY bhejo + caption EDIT karo — bilkul jaise TARGET me
    jaati hai. Isse quote (purple bar), bold, links sab 100% aate hain.

    Tarika 2 (fallback): purana send tarika.
    """
    src = get_source_id()
    o_chat = None
    o_mid = None
    try:
        fo = getattr(msg, "forward_origin", None)
        oc = getattr(fo, "chat", None) if fo is not None else None
        if oc is not None and getattr(oc, "id", None):
            o_chat = int(oc.id)
            o_mid = getattr(fo, "message_id", None)
    except Exception:
        o_chat = None
    if o_chat is None:
        fc = getattr(msg, "forward_from_chat", None)
        if fc is not None and getattr(fc, "id", None):
            o_chat = int(fc.id)
            o_mid = getattr(msg, "forward_from_message_id", None)
    if src and o_chat and o_mid and int(o_chat) == int(src):
        try:
            sent = await context.bot.copy_message(
                chat_id=chat_id, from_chat_id=src, message_id=int(o_mid)
            )
            cleaned, parse_mode, meta = build_caption_from_message(sent)
            kb = build_keyboard()
            ok = await _apply_caption_to_message(
                context, chat_id, sent, cleaned, parse_mode, kb
            )
            meta["preview_via"] = "copy+edit" if ok else "copy (edit fail)"
            meta["quote_in_output"] = "<blockquote" in (cleaned or "")
            return sent, cleaned, meta
        except Exception:
            logger.exception("preview copy path fail -> send path")

    cleaned, parse_mode, meta = build_caption_from_message(msg)
    kb = build_keyboard()
    try:
        st = await send_to_one_chat(
            context, msg, chat_id, cleaned, parse_mode, kb, True
        )
    except TelegramError as e:
        err = str(e).lower()
        logger.warning("preview send (html) fail: %s", e)
        if "parse" in err or "entity" in err:
            if "<blockquote expandable>" in (cleaned or ""):
                try:
                    st = await send_to_one_chat(
                        context, msg, chat_id, _degrade_quotes(cleaned),
                        parse_mode, kb, True
                    )
                    meta["preview_via"] = "send (normal quote)"
                    meta["quote_in_output"] = True
                    return st, cleaned, meta
                except TelegramError as e2:
                    logger.warning("preview degrade fail: %s", e2)
            st = await send_to_one_chat(
                context, msg, chat_id, cleaned, parse_mode, kb, False
            )
            meta["preview_via"] = "send (PLAIN — Telegram ne HTML reject kiya)"
            meta["quote_in_output"] = False
        else:
            raise
    return st, cleaned, meta


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

    # ---- PREVIEW mode: post forward karo -> DM me preview, target me kuch nahi ----
    if awaiting == "preview":
        # 15 min baad khud band (bhoolne par bot chupchap DM me bhejta rehta tha)
        try:
            until = float((data.get("settings") or {}).get("preview_until") or 0)
        except Exception:
            until = 0
        if until and time.time() > until:
            data.setdefault("settings", {})["awaiting"] = ""
            data["settings"]["preview_until"] = 0
            save_data(data)
            await reply(
                msg,
                "⌛ PREVIEW MODE auto-OFF (15 min ho gaye).\n\n"
                "Ab ye post NORMAL tarike se forward hogi.\n"
                "Dobara preview chahiye: /preview",
            )
            awaiting = ""
        else:
            if text.startswith("/"):
                return
            try:
                st, cleaned, meta = await preview_one(context, msg, chat.id)
            except Exception as e:
                await reply(msg, "Preview fail: " + str(e))
                return
            kb = build_keyboard()
            rep_info = _entities_report(msg)
            info = [
                "⚠️ PREVIEW MODE ON — YE POST KISI TARGET CHANNEL ME NAHI GAYI",
                "Sirf tumhe dikhane ke liye DM me bheji hai.",
                "Forward chahiye? → /cancel karo, phir post bhejo.",
                "",
                "Result: " + str(st),
                "Type: " + str(meta.get("type")),
                "Bhejne ka tarika: %s" % (meta.get("preview_via") or "send"),
                "Source QUOTE: %s%s"
                % (
                    "YES" if rep_info["has_quote"] else "NO",
                    " (expandable)" if rep_info["expandable"] else "",
                ),
                "DM me PURPLE BAR: %s"
                % ("YES ✅" if meta.get("quote_in_output", True) else "NAHI ❌ (neeche dekho)"),
            ]
            if meta.get("filename"):
                info.append("File: %s (%s)" % (meta["filename"], meta.get("size")))
            info.append("Caption chars: %s" % len(cleaned or ""))
            info.append("URL buttons: %s" % ("YES" if kb else "NO"))
            footer = (get_caption_settings().get("footer") or "").strip()
            info.append("Owner line: %s" % ("YES — " + footer if footer else "NO"))
            if meta.get("has_file_media"):
                tpl = (get_caption_settings().get("template") or "").strip()
                info.append(
                    "Template: %s" % ("ON" if tpl else "OFF (full caption use hua)")
                )
            if not meta.get("quote_in_output", True):
                info.append("")
                info.append(
                    "⚠️ Is post me quote (purple bar) NAHI bana. Wajah: Telegram"
                )
                info.append(
                    "ne caption me quote reject kiya, isliye plain bheja gaya."
                )
                info.append(
                    "Post me 3+ ek jaise emoji wali lines (📌...) honi chahiye."
                )
            info.append("")
            info.append("Band karne ke liye: /cancel")
            await reply(msg, "\n".join(info))
            # exact caption ki .txt file (Arena me bhejne ke liye)
            try:
                buf = io.BytesIO(caption_dump_text().encode("utf-8"))
                buf.name = "caption_dump.txt"
                await context.bot.send_document(
                    chat_id=msg.chat_id,
                    document=buf,
                    filename="caption_dump.txt",
                    caption="caption_dump.txt — caption ki exact copy (yahan upload kar do)",
                )
            except Exception:
                logger.exception("preview dump")
            return

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

        ok_t, info_t, kind_t = await target_chat_check(context.bot, cid)
        if ok_t is False:
            await reply(
                msg,
                "%s\n\n"
                "TARGET sirf CHANNEL (ya supergroup) ho sakta hai.\n"
                "Channel ki id negative hoti hai: -100...\n\n"
                "Sahi tareeka:\n"
                "1) Us channel se koi post yahan FORWARD karo\n"
                "2) Ya channel LINK bhejo: https://t.me/c/1234567890/1\n"
                "3) Ya seedha: /settarget -1001234567890" % info_t,
            )
            return

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

    Koi temp copy NAHI:
      copy_message -> target  (asli post)
      usi message ka caption EDIT (filters + quote + owner + buttons)
    """

    targets = get_target_ids()
    if not targets:
        return "no_target"
    source = get_source_id()
    if not source:
        return "fail"

    # ---------- SIRF EK TARIKA: copy to target + caption edit ----------
    # (temp copy system poora hata diya gaya)
    first = targets[0]
    try:
        sent_first = await context.bot.copy_message(
            chat_id=first, from_chat_id=source, message_id=mid
        )
    except TelegramError as e:
        err = str(e).lower()
        if _is_missing_msg_error(err):
            return "deleted"
        if "protected" in err or "can't be copied" in err:
            return await _copy_raw_to_targets(context, source, mid, targets)
        if _is_retryable_error(err):
            await asyncio.sleep(2)
            try:
                sent_first = await context.bot.copy_message(
                    chat_id=first, from_chat_id=source, message_id=mid
                )
            except TelegramError as e2:
                if _is_missing_msg_error(str(e2)):
                    return "deleted"
                logger.error("copy fail (retry) target=%s mid=%s: %s", first, mid, e2)
                return "fail"
        else:
            logger.error("copy fail target=%s mid=%s: %s", first, mid, e)
            return "fail"

    # source ki formatting read ho gayi (entities copy me aati hain)
    cleaned, parse_mode, meta = build_caption_from_message(sent_first)
    keyboard = build_keyboard()
    ok_first = await _apply_caption_to_message(
        context, first, sent_first, cleaned, parse_mode, keyboard
    )
    ok_n = 1 if ok_first else 0
    fail_n = 0 if ok_first else 1

    # baaki targets
    for tid in targets[1:]:
        got = None
        for attempt in range(3):
            try:
                _s, okk = await _copy_edit_to_target(
                    context, source, tid, mid, cleaned, parse_mode, keyboard
                )
                got = okk
                break
            except TelegramError as e:
                err = str(e).lower()
                if _is_missing_msg_error(err):
                    return "deleted"
                if _is_retryable_error(err) or "flood" in err:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                logger.error("copy+edit fail target=%s: %s", tid, e)
                got = False
                break
            except Exception:
                await asyncio.sleep(1.0 * (attempt + 1))
        if got:
            ok_n += 1
        else:
            fail_n += 1

    # duplicate memory sirf success ke baad
    if ok_n > 0:
        if (
            check_dup
            and get_settings().get("skip_duplicates", False)
            and meta.get("dup_key")
        ):
            seen_ids.add(meta["dup_key"])
        return "ok"
    return "fail"


async def run_forward(
    context, admin_id, from_id, to_id, skip_number, delay
):
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
        "Bulk dup-skip: OFF\n"
        "Temp copy: REMOVED (koi DM copy nahi, koi delete nahi)\n"
        "%s\n"
        "Agar FIRST ID 1 nahi hai to tumne\n"
        "/forward 1 TO nahi likha — FROM change karo.\n"
        "Empty source ids = DELETED (normal gap).\n"
        "/status  /stop"
        % (
            from_id,
            to_id,
            skip_number,
            start_id,
            delay,
            len(targets),
            (
                "\n🚨 WARNING: target list me DM/private id thi — hata di:\n   %s\n"
                "   Post channel me jayegi, DM me nahi.\n"
                "   Sahi channel: /settarget"
                % ", ".join(str(x) for x in PRIVATE_TARGETS_FOUND)
                if PRIVATE_TARGETS_FOUND
                else ""
            ),
        ),
    )
    await context.bot.send_message(admin_id, status_text())

    last_report = 0
    consecutive_fail = 0
    err_reported = False
    last_err_info["tid"] = 0
    last_err_info["err"] = ""
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
            elif result == "fail" and not err_reported and last_err_info["err"]:
                # Silent fail band — exact Telegram error admin ko bhejo
                err_reported = True
                try:
                    await context.bot.send_message(
                        admin_id,
                        "⚠️ TARGET SEND FAIL ho raha hai (chupchap)\n\n"
                        "TARGET: %s\n"
                        "ERROR: %s\n\n"
                        "Isliye channel me kuch nahi ja raha.\n"
                        "Fix dekhne ke liye: /check\n\n"
                        "Common fix:\n"
                        "• Bot ko us channel me Admin + 'Post Messages' ON\n"
                        "• Target ID galat ho to: /settarget se naya set karo"
                        % (last_err_info["tid"], last_err_info["err"]),
                    )
                except Exception:
                    logger.exception("err report")
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
            msg_end = (
                status_text()
                + "\n\nFORWARD "
                + fwd["status"].upper()
                + "\n\nResume tip:\n/forward %s %s 0"
                % (fwd.get("current") or start_id, to_id)
            )
            if fwd.get("ok", 0) == 0 and fwd.get("fail", 0) > 0:
                msg_end += (
                    "\n\n🚨 KUCH BHI CHANNEL ME NAHI GAYA (ok=0, fail=%s)\n"
                    % fwd.get("fail")
                )
                if last_err_info.get("err"):
                    msg_end += "ERROR: %s\n" % last_err_info["err"][:300]
                msg_end += (
                    "\nFIX:\n"
                    "1) Target channel me bot ko ADMIN banao\n"
                    "2) Permissions me 'Post Messages' ON karo\n"
                    "3) Phir: /check  → test send ✅ aana chahiye\n"
                    "4) /forward <FROM> <TO>"
                )
            await context.bot.send_message(admin_id, msg_end)
        except Exception:
            pass


# ---------- commands ----------
async def cmd_start(update, context):
    """Ek hi saaf message: greeting + status + MENU. Koi command list nahi."""
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

        admin = bool(user and is_admin(user.id))
        if not admin:
            await reply(
                msg,
                "⛔ Admin only.\n\nYour id: %s\n"
                "(Railway ADMIN_IDS me ye id daalo)" % (user.id if user else "?"),
            )
            return

        name = (user.first_name if user else "ADMIN") or "ADMIN"
        await msg.reply_text(
            home_text(str(name).upper()),
            reply_markup=home_inline_kb(),
        )
    except Exception:
        logger.exception("cmd_start")


async def cmd_ping(update, context):
    """/ping — bot zinda hai? BUILD + source/targets + mode."""
    msg = update.effective_message
    if not msg:
        return
    st = get_settings()
    data = load_data()
    lines = [
        "PONG ✅ Bot chal raha hai",
        "",
        "BUILD: %s" % BUILD_TAG,
        "SOURCE: %s" % get_source_id(),
        "TARGETS (%s): %s" % (len(get_target_ids()), get_target_ids()),
        "TARGET MODE: %s"
        % (
            "BOT LIST only (ENV ignore)"
            if data.get("targets_only")
            else "ENV + bot list"
        ),
        "TEMP COPY: HATA diya gaya ✅ (koi DM copy nahi, koi delete nahi)",
        "AUTO (live): %s | QUOTE AUTO: %s"
        % (
            "ON" if st.get("auto_forward", False) else "OFF",
            "ON" if st.get("quote_auto", True) else "OFF",
        ),
        "MODE: %s" % (get_mode() or "(normal — forwarding chalega)"),
    ]
    warn = mode_warning()
    if warn:
        lines.append(warn)
        lines.append("👉 Pehle /cancel chalao, phir /forward karo.")
    try:
        await msg.reply_text("\n".join(lines))
    except Exception as e:
        try:
            await msg.reply_text("PONG (err) " + str(e)[:200])
        except Exception:
            pass


async def cmd_version(update, context):
    """/version — Railway pe kaunsa code chal raha hai."""
    msg = update.effective_message
    if not msg:
        return
    await reply(
        msg,
        "BUILD: %s\n\n"
        "Isme ye sab fix hain:\n"
        "• TEMP COPY system POORA REMOVE — uske commands bhi gayaab,\n"
        "  koi DM copy nahi, koi delete nahi\n"
        "• Post seedha TARGET me copy hoti hai + wahi caption EDIT hota hai\n"
        "• Target channel saaf rehta hai (koi temp post/delete nahi)\n"
        "• DM target block (positive id)\n"
        "• Quote auto (purple bar + collapse)\n"
        "• Mode guard (/forward se pehle stuck mode clear)\n"
        "• /check me LAST SEND ERROR\n" % BUILD_TAG,
    )


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
        await _edit_or_reply(query, home_text(name), home_inline_kb())
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
        "m:quote_info": (
            "QUOTE (purple bar)\n\n"
            "1) Source me quote ho -> wahi bhejte hain\n"
            "2) Quote na ho par list ho (3+ lines same emoji 📌) ->\n"
            "   bot khud quote bana deta hai\n\n"
            "Commands:\n"
            "/quote on | off\n"
            "/quote lines 3\n\n"
            "Ab: %s (min %s lines)"
            % (get_settings().get("quote_auto", True), get_settings().get("quote_min_lines", 3))
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
    if action == "check":
        await run_and_show(cmd_check(update, context))
        return
    if action == "panic":
        await run_and_show(cmd_panic(update, context))
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
    if action == "quotetoggle":
        data_ok = load_data()
        st = data_ok.setdefault("settings", {})
        new_val = not bool(st.get("quote_auto", True))
        st["quote_auto"] = new_val
        save_data(data_ok)
        await msg.reply_text(
            "QUOTE AUTO: %s\n\n%s"
            % ("ON" if new_val else "OFF", quote_status_text())
        )
        return
    if action == "autotoggle":
        data_ok = load_data()
        st = data_ok.setdefault("settings", {})
        new_val = not bool(st.get("auto_forward", False))
        st["auto_forward"] = new_val
        save_data(data_ok)
        await msg.reply_text(auto_status_text())
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
    if PRIVATE_TARGETS_FOUND:
        lines.append(
            "\n🚨 PEHLE YE PROBLEM THI: target me DM/private id thi —\n"
            "   %s\n"
            "   (hata di gayi hai — warna post DM me jati)\n"
            "   Sahi channel set karo: /settarget"
            % ", ".join(str(x) for x in PRIVATE_TARGETS_FOUND)
        )
    for i, tid in enumerate(targets, 1):
        try:
            ch = await context.bot.get_chat(tid)
            title = "%s [%s]" % (ch.title or tid, getattr(ch, "type", "?"))
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
        ok_t, info_t, _k = await target_chat_check(context.bot, cid)
        if ok_t is False:
            await reply(
                msg,
                "%s\n\n"
                "Ye TARGET nahi ban sakta.\n"
                "Channel ki id negative hoti hai (-100...), user/DM ki positive.\n\n"
                "Sahi ID ke liye:\n"
                "1) Channel se koi post yahan FORWARD karo\n"
                "2) Ya channel LINK bhejo\n"
                "3) Ya /targets se check karo" % info_t,
            )
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
        ok_t, info_t, _k = await target_chat_check(context.bot, cid)
        if ok_t is False:
            await reply(
                msg,
                "%s\n\n"
                "TARGET sirf CHANNEL (ya supergroup) ho sakta hai.\n"
                "Channel id negative hoti hai (-100...).\n\n"
                "Sahi tareeka:\n"
                "• Us channel se post yahan FORWARD karo, ya\n"
                "• /settarget -1001234567890" % info_t,
            )
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


def _degrade_quotes(html):
    """Agar Telegram 'expandable' quote reject kare to normal quote bana do."""
    return (html or "").replace("<blockquote expandable>", "<blockquote>")


def sanitize_blockquotes(html):
    """Telegram nested blockquote allow nahi karta (aur expandable quote ke
    andar doosra quote bhi nahi). Aise case me andar wale quote ke TAGS
    hata do — text rehne do."""
    if not html or "<blockquote" not in html:
        return html
    T_EXQ = "\x00EXQ\x00"
    T_BQ = "\x00BQ\x00"
    T_BQE = "\x00BQE\x00"
    toks = (
        html.replace("<blockquote expandable>", T_EXQ)
        .replace("<blockquote>", T_BQ)
        .replace("</blockquote>", T_BQE)
    )
    res = []
    stack = []  # True = ye quote bheja gaya, False = nested tha -> tags hata diye
    i = 0
    while i < len(toks):
        if toks.startswith(T_EXQ, i):
            keep = not any(stack)  # koi outer quote zinda? -> nested, hata do
            stack.append(keep)
            if keep:
                res.append("<blockquote expandable>")
            i += len(T_EXQ)
        elif toks.startswith(T_BQ, i):
            keep = not any(stack)
            stack.append(keep)
            if keep:
                res.append("<blockquote>")
            i += len(T_BQ)
        elif toks.startswith(T_BQE, i):
            keep = stack.pop() if stack else False
            if keep:
                res.append("</blockquote>")
            i += len(T_BQE)
        else:
            res.append(toks[i])
            i += 1
    return "".join(res)


def _plain_from_html(html):
    return re.sub(r"<[^>]+>", "", html or "")


def caption_dump_text(note=""):
    cur = get_caption_settings()
    st = get_settings()
    rep_map = get_replace_map()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "================= CAPTION DUMP =================",
        "Time (bot): %s" % now,
        "Post message_id: %s   Type: %s" % (last_cap["msg_id"], last_cap["type"]),
        "",
        "----- 1) SOURCE CAPTION (jaisa channel me tha) -----",
        last_cap["src_text"] or "(khaali — pehle /preview karo, phir post forward karo)",
        "",
        "----- SOURCE FORMATTING (entities) -----",
        last_cap["src_entities"] or "  (koi formatting nahi)",
        "",
        "----- 2) FINAL CAPTION (jo target me jata hai, HTML) -----",
        last_cap["final"] or "(khaali)",
        "",
        "----- 2b) FINAL CAPTION (plain text, tags hate hue) -----",
        _plain_from_html(last_cap["final"]) or "(khaali)",
        "",
        "----- LENGTHS (Telegram limit: media=1024, text=4096) -----",
        "source chars : %s" % len(last_cap["src_text"] or ""),
        "final  chars : %s" % len(last_cap["final"] or ""),
        "final  utf16 : %s" % _u16len(last_cap["final"] or ""),
        "hissa (parts): %s" % len(split_html(last_cap["final"] or "", CAP_LIMIT)),
        "",
        "----- QUOTE (purple bar) -----",
        "source me quote entity : %s"
        % ("YES" if last_cap.get("has_quote_entity") else "NO"),
        "pattern se detect     : %s"
        % (
            "YES (lines %s-%s, marker %r)"
            % (
                last_cap["qspan"][0],
                last_cap["qspan"][1],
                last_cap["qspan"][2],
            )
            if last_cap.get("qspan")
            else "NO"
        ),
        "final me quote tag    : %s"
        % ("YES" if "<blockquote" in (last_cap.get("final") or "") else "NO"),
        "quote_auto            : %s (min %s lines)"
        % (st.get("quote_auto", True), st.get("quote_min_lines", 3)),
        "",
        "----- 3) MERI SETTINGS -----",
        "owner/footer : %r" % (cur.get("footer") or ""),
        "header       : %r" % (cur.get("header") or ""),
        "template     : %r" % (cur.get("template") or ""),
        "replace_all  : %r" % (cur.get("replace_all") or ""),
        "remove_orig  : %s" % cur.get("remove_original"),
        "auto_forward : %s" % st.get("auto_forward", False),
        "delay        : %s" % st.get("delay"),
        "skip_number  : %s" % st.get("skip_number"),
        "buttons      :",
    ]
    rows = get_buttons_rows()
    if rows:
        for r in rows:
            for b in r:
                lines.append("  [%s] -> %s" % (b.get("text"), b.get("url")))
    else:
        lines.append("  (koi button nahi)")
    lines.append("replace words:")
    if rep_map:
        for k, v in rep_map.items():
            lines.append("  %r => %r" % (k, v))
    else:
        lines.append("  (koi nahi)")
    lines.append("block words:")
    rm = get_remove_words()
    lines.append("  %s" % (", ".join(rm) if rm else "(koi nahi)"))
    lines.append("")
    lines.append("----- 4) MUJHE YAHAN LIKHO KYA BADALNA HAI (free text) -----")
    lines.append(note or "# jaise: owner line sabse neeche chahiye / ye line hatao /")
    lines.append("#       pehli line bold chahiye / extra line add karo ...")
    lines.append("================= END =================")
    return "\n".join(lines)


async def cmd_captiondump(update, context):
    """/captiondump — caption ki exact copy .txt file me (Arena me bhejne ke liye)."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return
    text = caption_dump_text()
    if not last_cap["final"]:
        await reply(
            msg,
            "Abhi koi caption yaad nahi hai.\n\n"
            "Pehle: /preview  → phir source channel se post yahan forward karo.\n"
            "Dump file apne aap bhi chali jayegi.",
        )
        return
    buf = io.BytesIO(text.encode("utf-8"))
    buf.name = "caption_dump.txt"
    await context.bot.send_document(
        chat_id=msg.chat_id,
        document=buf,
        filename="caption_dump.txt",
        caption="Caption dump (.txt) — is file ko Arena chat me upload kar do.",
    )
    await reply(msg, "Dump ready ↑\nYe .txt file yahan upload kar do, main exact fix kar dunga.")


async def cmd_preview(update, context):
    """/preview — caption/owner/buttons ka live preview DM me (kuch publish nahi hota)."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    if (data.get("settings") or {}).get("awaiting") == "preview":
        data.setdefault("settings", {})["awaiting"] = ""
        save_data(data)
        await reply(msg, "PREVIEW mode OFF.")
        return
    data.setdefault("settings", {})["awaiting"] = "preview"
    data["settings"]["preview_until"] = time.time() + 900  # 15 min auto-off
    save_data(data)
    await reply(
        msg,
        "PREVIEW MODE ON (15 min — phir khud band)\n\n"
        "Ab SOURCE channel se koi bhi post yahan (is DM me) FORWARD karo.\n"
        "Bot wahi post yahin bhejega — caption + owner line + buttons ke saath —\n"
        "jaise bilkul TARGET channel me jayegi.\n\n"
        "• Kisi target channel me KUCH nahi jayega\n"
        "• Image/video ka preview colour dots ke saath nahi — wo TERA sawal tha\n"
        "• Neeche info bhi milega: type, size, buttons YES/NO, owner YES/NO\n\n"
        "‼️ IS MODE ME FORWARD NAHI HOGA.\n"
        "Forward chahiye to: /cancel  (ya /preview dobara)",
    )


async def cmd_cancel(update, context):
    msg = update.effective_message
    if not msg:
        return
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    st = data.setdefault("settings", {})
    old = st.get("awaiting") or ""
    st["awaiting"] = ""
    st["preview_until"] = 0
    save_data(data)
    if old:
        await reply(
            msg,
            "✅ CANCEL — mode band ho gaya\n"
            "Purana mode: %s\n\n"
            "Ab forwarding NORMAL chalega.\n"
            "/ping se confirm karo → MODE: (normal)"
            % MODE_LABELS.get(old, old),
        )
    else:
        await reply(
            msg,
            "✅ CANCEL — koi mode on nahi tha (normal hi tha).\n"
            "Ab bhi normal hai: /ping se check karo",
        )


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
            "AUTO (live): %s → /auto off se band\n\n"
            "Last job: %s -> %s (at %s)\n"
            "Resume tip: /forward %s %s\n\n"
            "/status  /stop  /skip 0"
            % (
                "ON" if st.get("auto_forward", False) else "OFF",
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

    # ---- STUCK MODE hataya (yahi wajah thi: preview/add_target mode me
    # ---- bot DM me jawab deta tha aur forward NAHI hota tha) ----
    _d = load_data()
    _old_mode = (_d.get("settings") or {}).get("awaiting") or ""
    if _old_mode:
        _d.setdefault("settings", {})["awaiting"] = ""
        _d["settings"]["preview_until"] = 0
        save_data(_d)
        await reply(
            msg,
            "ℹ️ Pehle MODE ON tha: %s\n"
            "Us mode me bot forward nahi karta (DM me jawab deta hai).\n"
            "Maine wo mode BAND kar diya — ab forward chalega.\n"
            "Ho sake to /cancel bhi dabao."
            % MODE_LABELS.get(_old_mode, _old_mode),
        )

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


def auto_status_text():
    """/auto ka status text + toggle hint."""
    on = get_settings().get("auto_forward", False)
    if on:
        return (
            "🟢 AUTO FORWARD: ON\n\n"
            "Source channel me naya post aate hi\n"
            "turant target me chala jayega.\n\n"
            "Band karne ke liye: /auto off"
        )
    return (
        "🔴 AUTO FORWARD: OFF\n\n"
        "Ab source me post aayegi par forward NAHI hogi.\n"
        "Bulk /forward ... is se alag hai — wo chalta rehta hai.\n\n"
        "Chalu karne ke liye: /auto on"
    )


def _entities_report(msg):
    """Message ke entities ka readable report (quote detect ke saath)."""
    ents = list(msg.caption_entities or msg.entities or [])
    lines = []
    has_q = False
    has_eq = False
    for e in ents:
        t = str(getattr(e, "type", "?"))
        if _is_expandable_quote(t):
            has_q = True
            has_eq = True
        elif _is_blockquote(t):
            has_q = True
        extra = ""
        if getattr(e, "url", None):
            extra = "  url=%s" % e.url
        lines.append(
            "  - %-24s offset=%-5s len=%-5s%s"
            % (t, getattr(e, "offset", "?"), getattr(e, "length", "?"), extra)
        )
    return {
        "count": len(ents),
        "has_quote": has_q,
        "expandable": has_eq,
        "text": "\n".join(lines) if lines else "  (koi entity nahi — plain text)",
    }


async def cmd_inspect(update, context):
    """/inspect <id> — bot ko us source post me kya milta hai (quote check)."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return
    if not context.args or not context.args[0].isdigit():
        await reply(msg, "Usage: /inspect 13365\n(post id daalo)")
        return
    mid = int(context.args[0])
    targets = get_target_ids()
    source = get_source_id()
    if not targets:
        await reply(msg, "No targets. /addtarget pehle.")
        return
    if not source:
        await reply(msg, "No source. /addsource pehle.")
        return

    first = targets[0]
    await reply(
        msg,
        "Id %s check kar raha hoon... (target me copy karke padhunga, "
        "caption set ho jayega)" % mid,
    )
    try:
        sent = await context.bot.copy_message(
            chat_id=first, from_chat_id=source, message_id=mid
        )
    except TelegramError as e:
        err = str(e).lower()
        if _is_missing_msg_error(err):
            await reply(msg, "Id %s: DELETED — us id pe source me kuch nahi hai." % mid)
        else:
            await reply(msg, "Id %s fail: %s" % (mid, e))
        return

    rep_info = _entities_report(sent)
    src_text = (sent.text or sent.caption or "") or ""
    qspan = None
    if not rep_info["has_quote"]:
        qspan = detect_quote_span(
            src_text, int(get_settings().get("quote_min_lines", 3) or 3)
        )
    final, pm, meta = build_caption_from_message(sent)
    keyboard = build_keyboard()
    ok_edit = await _apply_caption_to_message(
        context, first, sent, final, pm, keyboard
    )
    final_has_q = "<blockquote" in (final or "")
    body = [
        "INSPECT id %s" % mid,
        "Source caption text (%s chars): %s"
        % (len(src_text), "YES" if src_text else "NO"),
        "Entities: %s" % rep_info["count"],
        "QUOTE (blockquote): %s" % ("YES" if rep_info["has_quote"] else "NO"),
        "Expandable quote: %s" % ("YES" if rep_info["expandable"] else "NO"),
        "Pattern se banega: %s"
        % ("YES (lines %s-%s)" % (qspan[0], qspan[1]) if qspan else "NO"),
        "Final output me quote: %s" % ("YES" if final_has_q else "NO"),
        "Caption set (edit): %s" % ("OK" if ok_edit else "ORIGINAL reh gaya"),
        "",
        "Entity list:",
        rep_info["text"],
        "",
        "Type: %s" % meta.get("type"),
        "",
        "ℹ️ Ye post TARGET me bhej di gayi hai (caption ke saath).",
        "   Koi temp copy nahi bani, koi delete nahi hua.",
    ]
    await reply(msg, "\n".join(body))
    try:
        dump = [
            "============ INSPECT id %s ============" % mid,
            "",
            "----- SOURCE CAPTION TEXT -----",
            src_text or "(khaali)",
            "",
            "----- ENTITIES -----",
            rep_info["text"],
            "",
            "QUOTE: %s | EXPANDABLE: %s | FINAL me quote: %s"
            % (rep_info["has_quote"], rep_info["expandable"], final_has_q),
            "",
            "----- FINAL CAPTION (HTML) -----",
            final or "(khaali)",
            "",
        ]
        buf = io.BytesIO("\n".join(dump).encode("utf-8"))
        buf.name = "inspect_%s.txt" % mid
        await context.bot.send_document(
            chat_id=msg.chat_id,
            document=buf,
            filename="inspect_%s.txt" % mid,
            caption="inspect_%s.txt — ye file Arena chat me upload kar do" % mid,
        )
    except Exception:
        logger.exception("inspect dump")


def quote_status_text():
    st = get_settings()
    on = st.get("quote_auto", True)
    return (
        "QUOTE (purple bar / collapse)\n\n"
        "1) Source post me quote ho -> WAHI quote bhejte hain (purple bar + \"…\")\n"
        "2) Source me quote NA ho, par list ho (ek hi emoji wali %s+ lagatar\n"
        "   lines jaise 📌) -> bot KHUD quote bana deta hai\n\n"
        "Ab: %s  (min %s lines)\n\n"
        "Commands:\n"
        "/quote on | off\n"
        "/quote lines 3"
        % (
            st.get("quote_min_lines", 3),
            "ON" if on else "OFF",
            st.get("quote_min_lines", 3),
        )
    )


async def _chat_line(context, context_bot, cid):
    """Us chat ka status report."""
    lines = []
    title = ""
    try:
        ch = await context_bot.get_chat(cid)
        title = ch.title or ch.full_name or getattr(ch, "username", "") or ""
        kind = str(getattr(ch, "type", "?"))
        lines.append("   type: %s" % kind)
        if kind == "private":
            lines.append(
                "   🚨 PRIVATE CHAT hai — isme post JAYEGI, channel me nahi!\n"
                "   /settarget se channel set karo."
            )
    except Exception as e:
        lines.append("   name: (resolve fail: %s)" % e)
        title = ""
    if title:
        lines.append("   name: %s" % title)
    # bot ka membership/admin status
    try:
        me = await context_bot.get_me()
        mem = await context_bot.get_chat_member(cid, me.id)
        st = str(getattr(mem, "status", "?"))
        can_post = getattr(mem, "can_post_messages", None)
        extra = ""
        if can_post is not None:
            extra = " | post messages: %s" % ("YES" if can_post else "NO")
        lines.append("   bot status: %s%s" % (st, extra))
    except Exception as e:
        lines.append("   bot status: (check fail: %s)" % e)
    if can_post is False:
        lines.append(
            "   ⚠️ bot ko posting permission NAHI (fix: Admin + Post Messages ON)"
        )
    else:
        lines.append(
            "   ℹ️ yahan koi temp/test post nahi bheji jaati (channel saaf rehta hai)\n"
            "   asli test: /test"
        )
    return lines


async def cmd_panic(update, context):
    """/panic — ek command me: mode band + target audit + seedha verdict."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return

    out = ["🆘 PANIC — AUTO FIX + CHECK", ""]
    problems = []

    # ---------- 1) MODE band ----------
    data = load_data()
    st = data.setdefault("settings", {})
    old_mode = st.get("awaiting") or ""
    st["awaiting"] = ""
    st["preview_until"] = 0
    save_data(data)
    if old_mode:
        out.append(
            "1) MODE: BAND kiya — %s"
            % MODE_LABELS.get(old_mode, old_mode)
        )
        out.append(
            "   (Is mode me bot DM me jawab deta tha, forward nahi karta tha)"
        )
    else:
        out.append("1) MODE: pehle se normal ✅ (koi stuck mode nahi)")
    out.append("")

    # ---------- 2) TARGETS ----------
    PRIVATE_TARGETS_FOUND.clear()
    targets = get_target_ids()
    bad_private = list(PRIVATE_TARGETS_FOUND)  # sirf abhi mili hui

    out.append("2) TARGETS: %s" % (", ".join(str(t) for t in targets) if targets else "❌ KHALI"))
    if bad_private:
        out.append(
            "   🚨 DM/private id (positive) hata di: %s"
            % ", ".join(str(x) for x in bad_private)
        )
        out.append(
            "   Yahi wajah thi: bot usko 'channel' samajh ke TUMHE DM kar raha tha."
        )
        problems.append("dm_target")
    if not targets:
        out.append("   ❌ Koi target channel nahi — /settarget karo")
        problems.append("no_target")

    no_perm = []
    not_admin = []
    for tid in targets:
        try:
            ch = await context.bot.get_chat(tid)
            title = ch.title or str(tid)
            kind = str(getattr(ch, "type", "?"))
        except Exception as e:
            out.append("   %s → ❌ resolve fail: %s" % (tid, e))
            problems.append("resolve")
            continue
        line = "   %s → %s [%s]" % (tid, title, kind)
        if kind == "private":
            line += "  🚨 PRIVATE (DM) — /settarget se channel do"
            problems.append("dm_target")
        try:
            me = await context.bot.get_me()
            mem = await context.bot.get_chat_member(tid, me.id)
            mst = str(getattr(mem, "status", "?"))
            can_post = getattr(mem, "can_post_messages", None)
            line += "\n      bot: %s" % mst
            if can_post is not None:
                line += " | post: %s" % ("YES" if can_post else "NO")
            if mst not in ("administrator", "creator"):
                not_admin.append(tid)
                problems.append("not_admin")
            elif can_post is False:
                no_perm.append(tid)
                problems.append("no_perm")
        except Exception as e:
            line += "\n      bot check fail: %s" % e
            problems.append("member_fail")
        out.append(line)
    out.append("")

    # ---------- 3) SOURCE ----------
    source = get_source_id()
    out.append("3) SOURCE: %s" % source)
    if not source:
        out.append("   ❌ set nahi — /addsource")
        problems.append("no_source")
    else:
        try:
            ch = await context.bot.get_chat(source)
            out.append("   %s [%s]" % (ch.title or source, getattr(ch, "type", "?")))
        except Exception as e:
            out.append("   ⚠️ get_chat fail: %s (bot ko source me admin banao)" % e)
    out.append("")

    # ---------- 4) VERDICT ----------
    out.append("──────── VERDICT ────────")
    if "no_target" in problems:
        out.append("🎯 PROBLEM: target channel set nahi hai.")
        out.append("   FIX: /cleartargets  →  /settarget  →  phir apne")
        out.append("        CHANNEL se koi post yahan forward karo")
    elif "not_admin" in problems:
        out.append("🎯 PROBLEM: bot us target me ADMIN nahi hai → post nahi jayegi.")
        out.append("   FIX: Channel → Manage → Administrators → bot add +")
        out.append("        'Post Messages' ON")
    elif "no_perm" in problems:
        out.append("🎯 PROBLEM: bot ADMIN hai par 'Post Messages' permission OFF.")
        out.append("   FIX: Channel → Manage → Administrators → bot → Post Messages ON")
    elif "resolve" in problems or "member_fail" in problems:
        out.append("🎯 PROBLEM: target resolve/member check fail. Bot ko us channel")
        out.append("   me add karo, phir /settarget se freshen karo.")
    elif "dm_target" in problems:
        out.append("🎯 PROBLEM: target list me DM/private id thi — bot usko channel")
        out.append("   samajh ke TUMHE DM kar raha tha (hata di).")
        out.append("   FIX: /settarget se apna CHANNEL set karo")
    else:
        out.append("✅ Sab theek lag raha hai!")
        out.append("   Ab: /test   → target channel me TEST POST aani chahiye")
        out.append("       /forward 45 13365  (ids 1-23 source me khaali hain)")
    out.append("")
    out.append("BUILD: %s" % BUILD_TAG)

    await reply(msg, "\n".join(out))


async def cmd_check(update, context):
    """/check — source + targets ka status + last send error."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return

    data = load_data()
    source = get_source_id()
    targets = get_target_ids()

    out = ["🔎 BOT CHECK", ""]

    # ---- source ----
    out.append("SOURCE: %s" % source)
    if not source:
        out.append("   ❌ set nahi hai — /addsource")
    else:
        out.extend(await _chat_line(context, context.bot, source))
    out.append("")

    # ---- targets ----
    out.append("TARGETS: %s" % len(targets))
    for _t in targets:
        if _t == source:
            out.append(
                "   ⚠️ WARNING: TARGET = SOURCE (dono same channel!)\n"
                "   Isse post usi channel me wapas jayegi. Alag channel do."
            )
    if not targets:
        out.append("   ❌ koi target nahi! — /addtarget ya /settarget")
    for i, tid in enumerate(targets, 1):
        out.append("%s) %s" % (i, tid))
        out.extend(await _chat_line(context, context.bot, tid))
    out.append("")

    # ---- LAST SEND ERROR (automatic — sabse important) ----
    if last_err_info["err"]:
        out.append(
            "🚨 LAST SEND ERROR (bot ne khud pakda):\n"
            "   target: %s\n"
            "   error : %s\n"
            % (last_err_info["tid"], last_err_info["err"])
        )
        e = last_err_info["err"].lower()
        if "not enough rights" in e or "chat_write_forbidden" in e:
            out.append(
                "   👉 FIX: Channel → Manage → Administrators → bot →\n"
                "      'Post Messages' ON karo (bot ko Admin banao)"
            )
        elif "chat not found" in e or "not a member" in e or "peer id invalid" in e:
            out.append(
                "   👉 FIX: Bot ko us channel me add karo, phir\n"
                "      /settarget se naya ID do (channel link forward karo)"
            )
        out.append("")

    # ---- mode + last job ----
    mode = get_mode()
    out.append("MODE: %s" % (mode or "(normal — forwarding chalega)"))
    if mode:
        out.append(
            "   ⚠️⚠️ IS MODE ME FORWARD NAHI HOGA! Band karo: /cancel"
        )
    out.append(
        "LAST JOB: from=%s to=%s current=%s | ok=%s fail=%s deleted=%s status=%s"
        % (
            fwd.get("from_id"),
            fwd.get("to_id"),
            fwd.get("current"),
            fwd.get("ok"),
            fwd.get("fail"),
            fwd.get("deleted"),
            fwd.get("status"),
        )
    )
    out.append("")

    # ---- settings ----
    st = get_settings()
    out.append("SETTINGS")
    out.append("   auto (live): %s | quote_auto: %s | temp copy: REMOVED"
               % (st.get("auto_forward", False), st.get("quote_auto", True)))
    out.append("   targets mode: %s"
               % ("BOT LIST only" if data.get("targets_only") else "ENV + bot list"))
    out.append("   build: %s" % BUILD_TAG)
    if ENV_TARGET_IDS and data.get("targets_only"):
        out.append("   Railway TARGET_CHAT_ID IGNORE ho raha hai: %s"
                   % ",".join(str(x) for x in ENV_TARGET_IDS))

    await reply(msg, "\n".join(out))


async def cmd_quote(update, context):
    """/quote on|off|lines N — purple bar quote ka control."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return
    data = load_data()
    st = data.setdefault("settings", {})
    args = [a.strip().lower() for a in (context.args or [])]

    if args and args[0] in ("on", "1", "true", "yes"):
        st["quote_auto"] = True
        save_data(data)
        await reply(msg, "🟢 QUOTE AUTO: ON\n" + quote_status_text())
        return
    if args and args[0] in ("off", "0", "false", "no"):
        st["quote_auto"] = False
        save_data(data)
        await reply(
            msg,
            "🔴 QUOTE AUTO: OFF\n\n"
            "Ab sirf source ka quote use hoga (agar source me ho).\n"
            "Bot khud quote nahi banayega.",
        )
        return
    if len(args) >= 2 and args[0] in ("lines", "min", "minlines"):
        try:
            v = int(args[1])
        except ValueError:
            await reply(msg, "Number do: /quote lines 3")
            return
        st["quote_min_lines"] = min(max(v, 2), 20)
        save_data(data)
        await reply(
            msg, "Min lines set: %s" % st["quote_min_lines"] + "\n\n" + quote_status_text()
        )
        return
    await reply(msg, quote_status_text())


async def cmd_auto(update, context):
    """/auto on|off  (alias /autoforward, /live) — LIVE channel-post forwarding switch."""
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not is_admin(user.id if user else None):
        await reply(msg, "Admin only.")
        return

    arg = (context.args[0].strip().lower() if context.args else "")
    data = load_data()
    st = data.setdefault("settings", {})

    if arg in ("on", "1", "true", "yes", "start", "chalu"):
        st["auto_forward"] = True
        save_data(data)
        await reply(
            msg,
            "🟢 AUTO FORWARD: ON\n\n"
            "Source me naya post → turant target me.\n\n"
            "Band: /auto off\n"
            "Bulk: /forward 1 13365",
        )
        return

    if arg in ("off", "0", "false", "no", "stop", "band"):
        st["auto_forward"] = False
        save_data(data)
        await reply(
            msg,
            "🔴 AUTO FORWARD: OFF\n\n"
            "Ab source me post aayegi par target me NAHI jayegi.\n"
            "Bulk /forward (manual range) pe koi asar nahi.\n\n"
            "Chalu: /auto on",
        )
        return

    if arg in ("", "status", "info"):
        await reply(
            msg,
            auto_status_text()
            + "\n\nUsage: /auto on   |   /auto off",
        )
        return

    await reply(msg, "Usage: /auto on  |  /auto off  |  /auto")


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
        "AUTO (live post) forward: %s → /auto on|off\n"
        "Temp copy: REMOVED (koi DM copy nahi)\n"
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
            "ON" if st.get("auto_forward", False) else "OFF",
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
        await reply(
            msg,
            "Test OK on %s targets\n%s\n\nAUTO (live): %s  → /auto on|off"
            % (
                len(ids),
                "\n".join(ids),
                "ON" if get_settings().get("auto_forward", False) else "OFF",
            ),
        )
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
    cmd = (txt.split()[0].lstrip("/").split("@")[0].lower()) if txt else ""
    # Asli registered commands ko "Unknown" NA bole (pehle /targets ka bhi
    # doosra reply aata tha — "/targets" ke saath "Unknown command")
    known = set()
    try:
        for grp in context.application.handlers.values():
            for h in grp:
                if isinstance(h, CommandHandler):
                    known.update(c.lower() for c in h.commands)
    except Exception:
        known = set()
    if cmd in known:
        return
    await reply(
        msg,
        "❓ Ye command nahi hai: %s\n\n"
        "👉 Menu kholne ke liye /start bhejo\n"
        "👉 Poora guide: /help"
        % (txt.split()[0] if txt else "?")
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
                BotCommand("start", "menu kholo"),
                BotCommand("help", "help / guide"),
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
        ("version", cmd_version),
        ("menu", cmd_menu),
        ("help", cmd_help_cmd),
        ("forward", cmd_forward_help),
        ("auto", cmd_auto),
        ("preview", cmd_preview),
        ("captiondump", cmd_captiondump),
        ("inspect", cmd_inspect),
        ("quote", cmd_quote),
        ("check", cmd_check),
        ("panic", cmd_panic),
        ("fix", cmd_panic),
        ("autoforward", cmd_auto),
        ("live", cmd_auto),
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

    logger.info("BUILD %s", BUILD_TAG)
    logger.info(
        "FORWARD BOT start src=%s targets=%s only=%s auto=%s",
        get_source_id(),
        get_target_ids(),
        load_data().get("targets_only"),
        get_settings().get("auto_forward", False),
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
