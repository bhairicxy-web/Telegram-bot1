=========================================================
 FORWARD BOT — UPDATE (AUTO OFF + CAPTION FIX + PREVIEW)
=========================================================

GITHUB PE KYA UPLOAD KARNA HAI
-----------------------------
1) bot.py              -> MUST (replace old file)
2) filter_data.json    -> OPTIONAL (settings reset ho jayenge,
                          target dobara /settarget se add karna padega)
   requirements.txt / Procfile / railway.toml / blocked_words.json
   = purane hi rehne do, change nahi hua.

UPLOAD KE BAAD: Railway -> Deploy (redeploy) -> Logs me
"FORWARD BOT start ..." line dikhe -> phir Telegram me /ping


1) AUTO FORWARD BAND (naya)
---------------------------
Pehle: source me post karte hi target me chala jata tha.
Ab:   DEFAULT OFF hai.

   /auto off   -> live auto forward band (post aayegi, forward nahi hogi)
   /auto on    -> live auto forward chalu
   /auto       -> status

Note: source me post aane par (jab auto OFF ho) bot admin ko
ek chhota notice bhejta hai: "AUTO FORWARD: OFF". Spam nahi —
max 10 minute me ek baar.

Inline menu: SETTINGS -> "LIVE / AUTO" button se turant toggle.

Bulk /forward FROM TO pe koi asar nahi — wo hamesha chalega.


2) CAPTION FIX (asli bug tha)
-----------------------------
a) Purana template "{caption}\n\nOwner ..." caption ko chhota kar
   deta tha. Ab template OFF — ORIGINAL CAPTION POORI (jaisi hai
   waisi, bold/italic/link formatting ke saath) aati hai,
   aur uske NEECHE owner line:
        Owner 👤 : @RicxyBhai

b) Caption 1024 se lamba ho to pehle "..." se KAT jati thi.
   Ab nahi — pehla hissa media ke saath, baaki text message me,
   buttons sabse aakhri message par.

c) Word replace/block ab sirf TEXT par lagta hai.
   Pehle HTML tags (bold/link) ke andar bhi lag jata tha jisse
   formatting toot jati thi. Wo fix ho gaya.


3) PREVIEW (naya) — caption ka test
-----------------------------------
   /preview   -> mode ON
   SOURCE channel se koi post bot ke DM me FORWARD karo
   -> bot wahi post DM me bhejta hai (caption + owner + buttons),
      bilkul jaise target me jayegi. Koi target me kuch NAHI jata.
   Info bhi milti hai: type, size, caption chars, buttons YES/NO.
   Band: /cancel  (ya /preview dobara)


CHECKLIST (Telegram me)
-----------------------
/ping
/auto            -> OFF dikhna chahiye
/targets         -> source + target sahi?
/addtarget       -> link bhejo ya post forward karo
/caption         -> footer: Owner 👤 : @RicxyBhai
/preview         -> source post forward karke caption dekho
/forward 1 13365 -> bulk (id 1 se shuru hoga)
/status /stop

4) CAPTION DUMP (naya) — .txt file
----------------------------------
   /preview  -> post forward karo
   -> preview message ke saath hi "caption_dump.txt" file bhi aati hai
      (ya kabhi bhi /captiondump)

   Us file me hota hai:
     - source caption (jaisa channel me tha)
     - source formatting (bold/italic/link list)
     - FINAL caption (jo target me jata hai) HTML + plain
     - length check (1024/4096 limit)
     - settings: owner line, header, template, replace/block, buttons
     - neeche free-text jagah: "# yahan likho kya badalna hai"

   Isi .txt file ko Arena chat me upload kar do -> exact fix mil jayega.
   (Arena chat video support nahi karta — image/jpg/png, text, pdf chalte hain.
    Screenshot bhi bilkul kaam karta hai.)

CHECKLIST UPDATE
----------------
/preview -> post forward -> preview + caption_dump.txt
- Agar caption kharab lage: caption_dump.txt upload + "kya badalna hai" likho

5) QUOTE / PURPLE BAR FIX (zaroori)
-----------------------------------
Problem (screenshots me dikha):  source post me list ek QUOTE (purple bar,
collapse/expand wala) ke andar thi — 3 line dikhte the, phir "..." aur
expand. Hamare bot ke output me wo quote FLAT ho gaya tha: purple bar gayab,
poori list khuli hui, collapse khatam.

Wajah: source ki "blockquote" / "expandable_blockquote" entity hamare
caption builder me handle hi nahi thi — wo skip ho jati thi.

Fix:
  - <blockquote> aur <blockquote expandable> properly bheje jaate hain
  - Quote ke andar bhi word replace (@ZCYT_2026 -> @RicxyBhai) lagta hai
  - Quote ke BAHAR ka text bahar hi rehta hai (jaise "Included in VIP ✅")
  - 1024+ caption split hone par bhi quote theek band/khol hota hai
  - Nested quote (Telegram allow nahi karta) auto hata diya jata hai
  - Agar Telegram 'expandable' reject kare -> normal quote bhejta hai,
    phir bhi fail ho to plain text (text loss kabhi nahi)

Note: "Included in VIP ✅" line hamare bot ne nahi jodi — wo SOURCE POST ka
text hai (code me aisa kahin nahi hai). Wo ab quote ke bahar, sahi jagah par
aayega jaisa original me tha.

Ye fix maujooda source posts par bhi kaam karega (koi config change nahi).

6) QUOTE AUTO — "purple bar sirf ek post par aaya" ka asli fix
-------------------------------------------------------------
Jo dikha: source post me quote (purple bar + "…" collapse) hai,
par bot ke output me list FLAT aa gayi. Kabhi aa jata hai, kabhi nahi.

Kyun: quote Telegram me ek "entity" (blockquote) hoti hai. Wo entity
source se bot tak sabhi paths me reliably nahi pahunchti (forward/copy/
different update types). Bot ke paas quote na aaye to wo list flat
bhej deta tha.

AB FIX (bulletproof — dono taraf se):
  1) Source me quote entity ho -> WAHI quote bheja jata hai
  2) Entity na ho, par caption me list ho (ek hi marker wali
     3+ lagatar lines, jaise 📌) -> bot KHUD <blockquote expandable>
     bana deta hai. 100% cases me purple bar aayega.

Commands:
  /quote            -> status
  /quote on|off     -> auto-quote band/chalu
  /quote lines 3    -> kam se kam kitni lines ho to quote bane
  /inspect ID       -> "Pattern se banega: YES (lines x-y)" dikhata hai
  Menu: CAPTION -> "🔲 QUOTE AUTO" button se turant toggle

NOTE: "Included in VIP ✅" jaise bina-marker wale lines quote ke BAHAR
rehte hain — jaisa original me tha.

UPLOAD
------
Sirf bot.py (aur chaho to filter_data.json). Purani flat posts apne aap
theek nahi hongi — unhe /copy <id> se dobara bhejo.
