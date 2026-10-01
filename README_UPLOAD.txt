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
