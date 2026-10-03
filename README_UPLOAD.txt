=========================================================
  FORWARD BOT — README (02-10-2026, BUILD 2026-10-02-MENU-CLEAN-18)
=========================================================

GITHUB PE KYA UPLOAD KARNA HAI
  1) bot.py            -> MUST (purana file replace karo)
  2) mise.toml         -> MUST (build fix ke liye)
  3) filter_data.json  -> OPTIONAL (upload karo to purani settings
                          reset ho jayengi — target list bhi khaali.
                          Upload na karo to server ki settings waisi hi rahegi)

DELETE (zaroori nahi, par saaf rehta hai): runtime.txt, Procfile.txt,
RAILWAY_DEPLOY.md, RAILWAY_STEPS_HINDI.md

=========================================================
 ⚡ ABHI YE 6 STEP KARO (isi order me)
=========================================================
1) GitHub repo (bhairicxy-web/Telegram-bot1) me bot.py REPLACE karo
   (file upload se — phone se paste NAHI).
2) Railway -> Deploy (lazmi nayi deploy chalao).
3) Bot me: /ping
   ➜ BUILD line me likha aana chahiye:
     2026-10-02-MENU-CLEAN-18
   Agar purana build dikhe to upload/deploy dobara karo.
4) /check
   ➜ Sabse upar "LAST SEND ERROR" dikhega (agar koi send fail hui ho).
   ➜ Purane build me /check ke baad "Unknown command: /check" aata tha.
     Naye build me wo NAHI aayega.
5) /test
   ➜ Har target channel me ek TEST post jayegi (delete NAHI hogi).
6) /forward 45 13365
   ⚠️ /forward 1 ... NAHI — source ke id 1-23 khaali hain
      (isliye bot unhe DELETED ginता hai). 45 se shuru karo.

=========================================================
 1) HOP POORI TARAH REMOVE (naya — 16:37 ka demand)
=========================================================
Ab bot me HOP naam ki cheez hi nahi hai. Poora code hata diya.

KYA HATA:
  • /hop aur /sethop commands — GAYAB (ab ye chal hi nahi sakte)
  • HOP_CHAT_ID env / settings me hop_chat_id — GAYAB
  • "set_hop" mode — GAYAB
  • temp copy banana — GAYAB
  • DM me copy bhejna — GAYAB
  • koi bhi delete — GAYAB (bot ab kisi bhi chat me kuch delete
    nahi karta — code me delete ka function hi nahi hai)

AB POST KAISE JATI HAI (sirf EK tarika):
  1. Source post ka asli copy SEEDHA target channel me jata hai
     (copy_message — photo/video/document + formatting sab intact)
  2. Usi bheje hue message ka caption EDIT hota hai:
        word replace -> quote (purple bar + collapse) -> Owner line -> buttons
  3. Caption 1024 se bada ho to bache hue hisse agle messages me chale jate hain
  4. Target channel me sirf FINAL post dikhti hai — koi flash, koi delete nahi

TEST PROOF: _test_nohop.py me 2 target pe copy, 0 DM copy, 0 delete,
2 caption edit — ALL_OK.

==============================================================================================
 1b) SAAF LOOK — /start pe sirf MENU
=========================================================
  /start  ->  EK hi message:
       👋 HELLO <naam>
       📥 Source : -1003415196836
       🎯 Targets: 1 channel ✅
       🔴 Auto   : OFF
       (neeche 12 blue buttons)
  • Command list wala purana text HATA diya
  • Telegram ke "/" menu me sirf 2 command: /start aur /help
  • Bottom keyboard bhi hata diya (sirf clean inline menu)
  • Unknown command ka jawab chhota: "Ye command nahi hai... /start"

====================
 2) AUTO FORWARD BAND
=========================================================
Live auto forward OFF hai (channel me nayi post aaye to bot khud
nahi bhejta). /auto se on/off. Bulk ke liye /forward FROM TO.

=========================================================
 3) CAPTION FIX (asli bug)
=========================================================
Caption ka poora body jata hai + formatting (bold, links) + header/footer.
/footer se Owner line (default: Owner 👤 : @RicxyBhai).
/caption -> settings, /captiondump -> .txt file, /preview -> DM me preview
(preview 15 min me khud band ho jata hai).

=========================================================
 4) QUOTE / PURPLE BAR (collapse wala)
=========================================================
1) Source post me quote ho -> WAHI quote jata hai (purple bar + "…")
2) Quote na ho par list ho (ek hi emoji wali 3+ lagatar lines, jaise 📌)
   -> bot KHUD quote bana deta hai
3) /quote            -> status + on/off
4) /quote lines 3    -> kitni lines se quote bane
5) /inspect 13365    -> ek post ka poora report (+ .txt file)
6) /copy 13365       -> ek purani post ko naye caption ke saath dobara bhejo
   (Telegram bheji hui post ko baad me edit nahi karta — isliye /copy)

=========================================================
 5) "POST KARTA HAI AUR DELETE BHI KARTA HAI" — SOLVED
=========================================================
Purana tarika: bot target channel me temp copy bhejta tha, padhta tha,
phir delete karta tha -> subscriber ko "post aayi, delete ho gayi" dikhta tha.
Ab temp copy hi nahi hai (hop removed) -> target channel 100% saaf.
Sabse pehle "🚨 KUCH BHI CHANNEL ME NAHI GAYA" warning aati hai
jab ek bhi post na jaye — uske saath asli error + 4-step fix likha hota hai.

=========================================================
 6) "DM me aa raha hai, channel me kuch nahi" — SOLVED
=========================================================
Ab DM copy ka concept hi nahi (hop removed).
Sirf 2 wajah bachti hain:
  A) Target list me positive id (DM id) hai -> bot usko channel samajhkar
     tumhe DM karta tha. Ab /panic aur /targets aisi id AUTO hata dete hain
     aur [PRIVATE] likh kar batate hain.
  B) Bot us target me admin nahi / "Post Messages" OFF -> post nahi jaati.
FIX: /panic -> har target ka bot status + post permission + verdict.

=========================================================
 7) /check — EK COMMAND ME SAB
=========================================================
/check ke naye output me:
  • LAST SEND ERROR (sabse upar) — aakhri fail ka ASLI Telegram error + fix hint
  • SOURCE  -> title, type, bot admin?, post permission?
  • TARGETS -> har ek ka title/type/bot status/post permission
  • SETTINGS -> auto, quote, temp copy: REMOVED
  • LAST JOB -> kitne ok / fail / deleted
⚠️ /check visible test post NAHI bhejta (channel saaf rehta hai).
   Visible test ke liye: /test

=========================================================
 8) /panic (alias /fix) — AUTO FIX
=========================================================
Ek command me: stuck mode band + DM id cleanup + har target ka
title/type/bot admin/post permission + seedha VERDICT (problem + fix).

=========================================================
 9) BUILD FAIL FIX (Railway)
=========================================================
Error: "mise python@3.12.6 failed: No GitHub artifact attestations found"
FIX (koi ek): mise.toml upload karo | Builder = NIXPACKS |
MISE_PYTHON_GITHUB_ATTESTATIONS=false | Dockerfile.

=========================================================
 10) COMMANDS (naya list)
=========================================================
  /ping /version /help /menu
  /forward FROM TO      bulk (FROM se hi shuru hota hai)
  /stop /status         chalu job band / status panel
  /auto on|off          live auto (abhi OFF)
  /targets /settarget /addtarget /removetarget /cleartargets
  /addsource /setsource /clearsource
  /check     /test     /panic (/fix)
  /quote /quote lines N /inspect ID /copy ID
  /caption /captionheader /captionfooter /captiontemplate /captionclear /captiondump
  /preview /block /remove /replace /button /buttons /buttonclear
  /listfilters /skip /delay /duplicates /unequify /settings /reset /cancel

  ❌ GAYAB: /hop, /sethop (hop poora remove)

=========================================================
 11) EK POST MANUAL FORWARD (test)
=========================================================
  /copy 13365      -> us id ko targets me bhejta hai (caption set ke saath)
  /inspect 13365   -> us id ka report + post target me
  /forward 45 13365 -> bulk 45 se 13365 tak

=========================================================
 12) DHYAN RAKHO
=========================================================
  • Bot har target channel me ADMIN ho + "Post Messages" permission ON
  • Target id channel ki ho (-100... se shuru) — DM id nahi
  • Source aur target same channel na ho (/check warning deta hai)
  • Ek time pe ek hi jagah polling chale (Railway ya Arena) — warna
    getUpdates Conflict
