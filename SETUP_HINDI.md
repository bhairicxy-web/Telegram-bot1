# Telegram Bot kaise use karein (bilkul simple)

## Pehle samjho: deploy kab chahiye?

| Situation | Kya karna hai |
|-----------|----------------|
| Sirf test / seekhna | Apne PC pe `python bot.py` chalao — **deploy nahi chahiye** |
| Jab PC band ho tab bhi bot chale | Cloud pe deploy (Railway / Render) — **free** options hain |
| Phone pe hi chalana ho | Termux (Android) se bhi chal sakta hai, lekin battery zyada use hogi |

Bot **polling** mode me hai — matlab koi website/URL nahi chahiye.  
Bas kahi pe process continuously chalti rehni chahiye.

---

## STEP 1 — Bot banao (2 minute)

1. Phone pe Telegram kholo
2. Search: **@BotFather**
3. `/newbot` bhejo
4. Bot ka naam do (jaise: `My Forward Filter`)
5. Username do (jaise: `my_forward_filter_bot`) — last me `bot` hona chahiye
6. BotFather **TOKEN** dega, jaise:
   ```
   7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
7. Ise copy karke safe rakh lo (kisi ko mat do)

---

## STEP 2 — Apna User ID lo

1. Telegram pe **@userinfobot** kholo
2. `/start` dabao
3. Jo number aaye (jaise `987654321`) — yeh **ADMIN_IDS** hai

---

## STEP 3 — Source & Target channel/group

### A) Channels use kar rahe ho
1. Do channels banao (ya existing use karo):
   - **Source** = jahan original posts aati hain
   - **Target** = jahan clean forward chahiye
2. Dono channels me bot ko **Admin** banao:
   - Channel info → Administrators → Add Admin → apna bot select
   - Permissions: **Post Messages** ON rakho

### B) Groups use kar rahe ho
1. Bot ko group me add karo + admin banao
2. **Zaroori:** @BotFather pe jao → apna bot select → `/setprivacy` → **Disable**  
   (warna group messages bot ko nahi dikhenge)

### C) Chat ID kaise nikaalein (sabse easy tareeka)

**Method 1 — @getidsbot / @RawDataBot**
1. Source channel/group se koi message **forward** karo @RawDataBot pe
2. Reply me `chat: { id: -100.... }` dikhega — wahi SOURCE_CHAT_ID
3. Target ke liye same karo

**Method 2 — temporary**
1. Bot ko channel/group me add karo
2. Channel me koi post karo / group me bot ko mention karke message bhejo
3. Browser me kholo (TOKEN replace karke):
   ```
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```
4. JSON me `"chat":{"id":-100...}` milega

IDs usually aise dikhte hain: `-1001234567890`

---

## STEP 4 — PC pe pehli baar chalao (TEST)

### Windows
1. [Python](https://www.python.org/downloads/) install karo (install pe “Add to PATH” tick karo)
2. `telegram_forward_bot` folder download/copy karo
3. Folder ke andar `.env.example` ko copy karke naam rakho **`.env`**
4. `.env` Notepad se kholo aur bharo:

```env
BOT_TOKEN=yahan_botfather_ka_token
SOURCE_CHAT_ID=-100xxxxxxxxxx
TARGET_CHAT_ID=-100yyyyyyyyyy
ADMIN_IDS=aapka_user_id
```

5. Folder me address bar pe `cmd` likh ke Enter
6. Commands:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

### Mac / Linux
```bash
cd telegram_forward_bot
cp .env.example .env
# .env edit karo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### Success dikhega aise:
```
Bot starting | source=-100... target=-100... blocked=3
```

Ab:
1. Telegram pe apne bot ko `/start` bhejo
2. Source channel/group me test message daalo jisme word ho jaise `scam`
3. Target me cleaned message aana chahiye
4. Bot ko DM karke: `/block hello` → phir source me `hello world` → target pe sirf `world`

**PC band / terminal band = bot band.** Isliye 24/7 ke liye neeche deploy karo.

---

## STEP 5 — 24/7 FREE deploy (Railway — recommended)

Railway free tier pe chhota bot chal jata hai.

1. GitHub pe free account banao
2. Naya repo banao, `telegram_forward_bot` ke saare files upload karo  
   (**`.env` mat upload karna** — secrets alag se)
3. [railway.app](https://railway.app) → Login with GitHub
4. **New Project** → **Deploy from GitHub repo** → apna repo select
5. Project me **Variables** add karo (same as .env):
   - `BOT_TOKEN`
   - `SOURCE_CHAT_ID`
   - `TARGET_CHAT_ID`
   - `ADMIN_IDS`
6. Start command (agar auto na chale): `python bot.py`
7. Deploy hone do — logs me “Bot starting…” dikhe to ready

### Render.com alternative
1. [render.com](https://render.com) → New → **Background Worker**
2. GitHub repo connect
3. Build: `pip install -r requirements.txt`
4. Start: `python bot.py`
5. Environment variables same 4 cheezein add karo

> Web Service mat banana — is bot ko public URL ki zaroorat nahi.  
> **Worker / Background process** type choose karo.

---

## STEP 6 — Daily use

Bot chal raha ho to:

| Aap kya karoge | Result |
|----------------|--------|
| Source pe normal post | Target pe same (words clean) |
| `/block badword` bot ko DM | Ab se wo word hatega |
| `/listblocks` | Saari list |
| `/unblock badword` | List se hatao |
| `/status` | Source/target check |

Blocked words file `blocked_words.json` me save hoti hain.  
**Cloud pe** agar container restart ho aur disk ephemeral ho to list reset ho sakti hai — isliye important words pehle se `blocked_words.json` me daal do, ya baad me DB add karwa lena.

---

## Common problems

| Problem | Fix |
|---------|-----|
| Bot message nahi uthata (group) | BotFather → `/setprivacy` → **Disable**, bot ko group se hata ke wapas add karo |
| Channel se nahi aata | Bot source channel me **Admin** hai? |
| Target pe nahi jaata | Bot target me Admin + Post permission? |
| `BOT_TOKEN` error | `.env` me extra space/quotes to nahi? |
| Chat id galat | Positive number mat daalna; channel IDs `-100` se start hoti hain |
| Conflict: terminated by other getUpdates | Kahin aur bhi same bot chal raha hai — dusri jagah band karo |

---

## Short summary

```
1. @BotFather → token
2. Bot ko source + target pe admin banao
3. Chat IDs + apna user ID lo
4. .env bharo
5. Test:  python bot.py   (PC pe)
6. 24/7:  Railway/Render pe same code + variables
```

Deploy ke bina bhi poora kaam karta hai — bas jab terminal chalu ho.
