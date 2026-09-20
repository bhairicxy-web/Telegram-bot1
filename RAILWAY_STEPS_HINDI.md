# Railway pe deploy — final simple steps

Tumhare values (Variables me yahi paste karna):

```
BOT_TOKEN=<<BotFather wala token>>
SOURCE_CHAT_ID=-1003415196836
TARGET_CHAT_ID=-1004389329782
ADMIN_IDS=8467972004,6312515331
```

---

## PART A — GitHub pe code

### 1. GitHub account
- https://github.com → Sign up / Login

### 2. Naya repo
1. Right top **+** → **New repository**
2. Name: `telegram-forward-bot`
3. Public
4. **Create repository** (README mat add karo)

### 3. Files upload
1. Repo page pe **uploading an existing file** / **Add file → Upload files**
2. Apne PC se ZIP extract karke ye files upload karo:

```
bot.py
requirements.txt
blocked_words.json
Procfile
runtime.txt
railway.toml
.gitignore
README.md
.env.example
RAILWAY_STEPS_HINDI.md
```

3. **`.env` mat upload karna** (token public ho jayega)
4. **Commit changes**

---

## PART B — Railway

### 1. Login
- https://railway.app (ya railway.com)
- **Login with GitHub** → Authorize

### 2. Project
1. **New Project**
2. **Deploy from GitHub repo**
3. Pehli baar: Configure GitHub App → repo allow karo
4. `telegram-forward-bot` select

### 3. Variables (SABSE ZAROORI)
Service pe click → **Variables** → Add:

| Name | Value |
|------|--------|
| BOT_TOKEN | (BotFather token) |
| SOURCE_CHAT_ID | -1003415196836 |
| TARGET_CHAT_ID | -1004389329782 |
| ADMIN_IDS | 8467972004,6312515331 |

Save → auto redeploy.

### 4. Start command (agar need ho)
**Settings** → Start Command:
```
python bot.py
```
(Procfile / railway.toml me pehle se hai)

### 5. Logs
**Deployments → Logs** me dikhna chahiye:
```
Bot starting bulk-enabled source=... target=...
Application started
```

### 6. Test
Telegram → @Forwardbyrbot:
```
/start
/status
/test
```

Bulk (example):
```
/bulk 1 100
/bulkstatus
```
Full:
```
/bulk 1 12500
```

---

## Agar error aaye

| Error | Fix |
|-------|-----|
| BOT_TOKEN missing | Variables me naam exact `BOT_TOKEN` |
| chat not found | Bot dono channels me Admin hai? |
| Conflict getUpdates | Kahin aur (PC/Arena) same bot band karo |
| Build fail | requirements.txt repo me hai? |
| Credits / pay | Railway trial/hobby — billing check |

---

## Deploy ke baad Arena/PC band

Railway pe chal raha bot **24/7** rahega.  
Arena chat band ho to bhi chalega.

**Important:** Same bot token ek time pe **ek hi jagah** chalao  
(Arena + Railway dono = Conflict error).
