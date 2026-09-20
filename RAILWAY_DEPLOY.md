# Railway pe Telegram Forward Bot deploy (click-by-click)

Is bot ko public website ki zarurat nahi — sirf ek hamesha-chalti Python process chahiye.

> **Sachchai pehle:** Railway ab “hamesha free” nahi hai.  
> Naye account pe **trial credits** milte hain (~$5). Bot bahut halka hai, isliye kai din/hafte chal sakta hai.  
> Credits khatam hone ke baad **Hobby plan ~$5/month** (usage ke hisaab se).  
> Bilkul ₹0 long-term chahiye ho to neeche **Option B / C** dekho.

---

## Pehle ye 4 values ready rakho

| Variable | Kahan se |
|----------|----------|
| `BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `SOURCE_CHAT_ID` | Source channel/group ID (`-100...`) |
| `TARGET_CHAT_ID` | Target channel/group ID (`-100...`) |
| `ADMIN_IDS` | @userinfobot se apna numeric ID |

Bot **source + target dono** me Admin hona chahiye.  
Group ho to BotFather → `/setprivacy` → **Disable**.

---

# OPTION A — Railway (sabse simple)

## Part 1: Code GitHub pe daalo

### 1.1 GitHub account
1. Browser: [https://github.com](https://github.com)  
2. Sign up / Login (free)

### 1.2 Naya repository
1. Right-top **+** → **New repository**
2. Repository name: `telegram-forward-bot` (kuch bhi)
3. **Public** select karo (Private bhi chalega)
4. **Add a README** mat tick karo (optional)
5. **Create repository**

### 1.3 Files upload (bina Git ke — easiest)
1. Repo page pe **uploading an existing file** / **Add file → Upload files**
2. Apne PC se `telegram_forward_bot` folder ki ye files drag-drop karo:

```
bot.py
requirements.txt
blocked_words.json
Procfile
runtime.txt
railway.toml
.gitignore
README.md
```

3. **`.env` MAT upload karna** (token leak ho jayega)
4. Commit message: `first upload`
5. **Commit changes**

> Agar Git aata ho:
> ```bash
> cd telegram_forward_bot
> git init
> git add .
> git commit -m "forward bot"
> git branch -M main
> git remote add origin https://github.com/USERNAME/telegram-forward-bot.git
> git push -u origin main
> ```

---

## Part 2: Railway project

### 2.1 Login
1. [https://railway.app](https://railway.app) (ya [https://railway.com](https://railway.com))
2. **Login** → **Login with GitHub**
3. GitHub authorize karo

### 2.2 Naya project
1. Dashboard pe **New Project**
2. **Deploy from GitHub repo** choose karo  
   (pehli baar GitHub access mangega → **Configure** → apna repo allow karo → Save)
3. List se `telegram-forward-bot` select karo
4. Railway auto-detect karke deploy start kar dega

### 2.3 Environment variables (SABSE ZAROORI)
Deploy fail / bot crash ho sakta hai jab tak ye na daalo:

1. Apne service (box) pe click karo  
2. Upar tabs me **Variables**  
3. **+ New Variable** / **RAW Editor** se ye add karo:

```
BOT_TOKEN=7123456789:AAH....tumhara_token
SOURCE_CHAT_ID=-1001234567890
TARGET_CHAT_ID=-1009876543210
ADMIN_IDS=123456789
```

4. Save / Add  
5. Variables save hote hi Railway **re-deploy** karega (automatic)

### 2.4 Start command check (agar bot start na ho)
1. Service → **Settings**
2. **Start Command** me ho:
   ```
   python bot.py
   ```
3. (Procfile / railway.toml me pehle se set hai — usually auto chal jata hai)

### 2.5 Logs se confirm
1. Service → **Deployments** → latest deploy → **View Logs**
2. Success dikhega aise:
   ```
   Bot starting | source=-100... target=-100... blocked=3
   Application started
   ```
3. Error aaye to common fixes neeche dekho

### 2.6 Telegram pe test
1. Bot ko DM: `/start`
2. `/status` — source/target IDs sahi hain?
3. Source channel me test post: `this is a scam test`
4. Target pe `scam` hata hua message aana chahiye
5. `/block hello` se naya word add karo

**Ho gaya — bot cloud pe chal raha hai.** PC band ho to bhi kaam karega.

---

## Railway common errors

| Log / problem | Fix |
|---------------|-----|
| `BOT_TOKEN .env me set karo` | Variables tab me `BOT_TOKEN` missing / galat naam |
| `SOURCE_CHAT_ID aur TARGET...` | Dono IDs Variables me number ke roop me (`-100...`) |
| `Conflict: terminated by other getUpdates` | Kahin aur (PC) bhi same bot chal raha hai — PC pe `Ctrl+C` se band karo |
| `Chat not found` / can't send | Bot target channel me **Admin** nahi |
| Group se message nahi aata | `/setprivacy` → Disable, bot hata ke wapas add |
| Build fail `No start command` | Settings → Start Command: `python bot.py` |
| Credits / payment | Trial khatam → Hobby plan ya dusra host |

---

## Blocked words Railway pe

`blocked_words.json` repo me hai — default words deploy ke saath aate hain.  
`/block` se add kiye words **container restart** pe reset ho sakte hain (disk ephemeral).  

**Fix:** Jo words hamesha chahiye, unhe pehle se `blocked_words.json` me likh ke GitHub pe push karo — redeploy ke baad bhi rahenge.

---

# OPTION B — Render (alternative)

1. Code GitHub pe same tarah  
2. [https://render.com](https://render.com) → GitHub login  
3. **New +** →  
   - Pehle **Background Worker** try karo (kabhi paid hota hai)  
   - Free chahiye to **Web Service** (sleep ho sakta hai; pure 24/7 nahi)  
4. Build: `pip install -r requirements.txt`  
5. Start: `python bot.py`  
6. Environment: wahi 4 variables  

> Render free web service idle pe so jata hai — forward bot ke liye Railway/VPS better.

---

# OPTION C — Bilkul free-ish long run

| Option | Cost | Notes |
|--------|------|--------|
| Purana Android + **Termux** | ₹0 | Phone charge pe rakho, `python bot.py` |
| Ghar ka PC 24/7 | bijli | `python bot.py` + sleep disable |
| Cheap VPS (Contabo/Hetzner/Indian VPS) | ~₹300–500/mo | sabse stable |
| Oracle Cloud free VM | ₹0 (limits) | setup thoda mushkil |

---

# Checklist (print / tick)

- [ ] @BotFather se bot + token  
- [ ] Source me bot Admin  
- [ ] Target me bot Admin  
- [ ] Group ho to privacy Disable  
- [ ] 4 values ready (token, 2 chat ids, admin id)  
- [ ] GitHub pe files (bina `.env`)  
- [ ] Railway → Deploy from GitHub  
- [ ] Variables add  
- [ ] Logs me “Bot starting”  
- [ ] `/start` + source pe test message  

---

## Help chahiye to

Deploy ke baad agar error aaye to Railway **Logs** ki last 20 lines copy karke bhej dena — us hisaab se exact fix bata dunga.
