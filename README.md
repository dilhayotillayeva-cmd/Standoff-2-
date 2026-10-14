# 🎮 STANDOFF 2 SETTINGS BOT

Telegram bot (faqat bot, sayt yo'q): **Python 3.11 · aiogram 3 · aiohttp webhook**.
Ma'lumotlar bazasi: **SQLite** (`DB_PATH`) yoki tashqi **PostgreSQL** (`DATABASE_URL`). Til: o'zbekcha.

## Fayllar

| Fayl | Vazifasi |
|---|---|
| `bot.py` | Sozlamalar, foydalanuvchi va admin handlerlari, aiohttp webhook, `/health` |
| `db.py` | Baza qatlami (SQLite + PostgreSQL) va butun biznes-mantiq (tanga, referal, bonus, promokod) |
| `tests/test_db.py` | Baza mantiqi testlari (faqat standart kutubxona) |
| `requirements.txt`, `.python-version` | Bog'liqliklar, Python 3.11 |
| `render.yaml`, `Procfile`, `Dockerfile` | Hosting konfiguratsiyalari |
| `.env.example` | Muhit o'zgaruvchilari namunasi |

## Muhit o'zgaruvchilari

| O'zgaruvchi | Majburiy | Izoh |
|---|---|---|
| `BOT_TOKEN` | ✅ | @BotFather tokeni |
| `ADMIN_IDS` | ✅ | Admin Telegram ID lari, vergul bilan: `111,222` |
| `WEBHOOK_BASE` | ✅ | Ochiq **HTTPS** manzil, oxirida `/`siz: `https://bot.example.com`. Render'da bo'sh qoldirilsa `RENDER_EXTERNAL_URL` olinadi |
| `DATABASE_URL` | yoki | PostgreSQL: `postgresql://user:pass@host:5432/db?sslmode=require` |
| `DB_PATH` | yoki | SQLite fayl yo'li (standart `data/bot.db`). `DATABASE_URL` berilsa u ishlatiladi |
| `PORT` | – | Standart `8080` (hostlar o'zi beradi) |
| `WEBHOOK_SECRET` | – | Bo'sh bo'lsa `BOT_TOKEN` dan avtomatik hosil qilinadi |
| `PLAYER_BONUS_ON_START` | – | `1` bo'lsa yangi foydalanuvchiga ro'yxatdan o'tishda 10 tanga PLAYER bonusi beriladi (standart: o'chiq) |
| `LOG_LEVEL` | – | `INFO` (standart) / `DEBUG` |

Xato yoki yetishmayotgan o'zgaruvchida bot aniq xabar bilan darhol to'xtaydi.

## Ma'lumotlar qayta deploy'da yo'qolmasligi

Konteyner/hostning o'z fayl tizimi **vaqtinchalik**. Ikki yo'l bor:

1. **PostgreSQL (tavsiya)** - `DATABASE_URL` ni kiriting (Neon, Supabase, Railway Postgres va h.k.). Bepul rejalarda ham ishlaydi.
2. **SQLite + doimiy disk** - diskni ulab, `DB_PATH` ni shu disk ichiga yo'naltiring (masalan `/data/bot.db`).
   Render'da disk faqat pullik rejada bor (`render.yaml` shunga yozilgan); Railway'da Volume ulang.

Bot ishga tushganda qaysi baza ishlatilayotganini logga yozadi. Sxema avtomatik yaratiladi, namunaviy modellar faqat **birinchi** ishga tushirishda qo'shiladi (admin o'chirgan model qayta tiklanmaydi).

## Deploy

**Render:** repozitoriyni ulang → *Blueprint* (`render.yaml`) → `BOT_TOKEN`, `ADMIN_IDS` ni kiriting. Health check: `/health`.
Bepul rejada: `render.yaml` dan `disk:` va `DB_PATH` ni olib tashlab, `DATABASE_URL` bering.

**Railway / Heroku-simon hostlar:** `Procfile` ishlatiladi (`web: python bot.py`); o'zgaruvchilarni panelda kiriting, `WEBHOOK_BASE` ga xizmatning ochiq HTTPS manzilini bering.

**Docker / VPS:** `docker build -t s2bot .` va `docker run -p 8080:8080 -v s2data:/data --env-file .env s2bot`. HTTPS ni reverse-proxy (nginx/Caddy) bilan ta'minlang.

**Lokal sinov:** `pip install -r requirements.txt`, `.env` dagi qiymatlarni eksport qiling, `cloudflared`/`ngrok` bilan HTTPS tunnel oching va uni `WEBHOOK_BASE` ga qo'ying, so'ng `python bot.py`.

## Webhook va xavfsizlik

- Webhook yo'li tokendan hosil qilingan tasodifiy segment bilan: `/webhook/<hash>`; Telegram so'rovlari `X-Telegram-Bot-Api-Secret-Token` bilan tekshiriladi (noto'g'ri bo'lsa 401).
- `set_webhook` ishga tushganda 5 martagacha qayta uriniladi. Bot to'xtaganda webhook **o'chirilmaydi** (rolling deploy'da yangi nusxa o'rnatgan webhookni eskisi o'chirib yubormasligi uchun).
- To'xtatishda (SIGTERM/SIGINT): fon vazifalari (xabar yuborish) bekor qilinadi, `bot.session` (HTTP sessiya) va baza ulanishlari yopiladi.
- `/health` - bazaga `SELECT 1` yuboradi; `200 {"status":"ok"}` yoki baza ishlamasa `503`.
- Admin: `ADMIN_IDS` + faqat shaxsiy chat. Admin handlerlari router darajasida filtrlangan (buyruq, tugma va matn kiritish holatlari), begona foydalanuvchi uchun admin tugmalari ishlamaydi. Admin amallari (tanga, tarif, model, promokod, xabar) `admin_log` jadvaliga yoziladi.
- Foydalanuvchiga ko'rinadigan barcha dinamik matn HTML-escape qilinadi. Tanga amallari atomik tranzaksiyada, balans manfiy bo'lmaydi (`CHECK`).
- Promokodni terib ko'rishga qarshi: 10 daqiqada 5 ta noto'g'ri urinishdan keyin vaqtincha to'xtatiladi (xotirada; qayta ishga tushganda tozalanadi).

## Funksiyalar va qoidalar

**Tariflar** (yangi foydalanuvchi - `PLAYER`)

| Tarif | Referal | Faollashtirish bonusi (bir martalik) | Imkoniyat |
|---|---|---|---|
| PLAYER | 1 🪙 | 10 🪙 | Telefon sozlamalari |
| PREMIUM | 2 🪙 | 100 🪙 | + PC sozlamalari |
| KT CREATOR | 2 🪙 | – | PREMIUM + telefonda ACCELERATOR va KICHIK USHLAGICH |

- Bonus `(foydalanuvchi, tarif)` juftligi bo'yicha **bir marta** beriladi (`bonus_claims`). Tarif PLAYER → PREMIUM → PLAYER → PREMIUM bo'lsa ham takroran berilmaydi.
- **Referal:** havola `https://t.me/<bot>?start=<USER_ID>`. Mukofot faqat botga **birinchi marta** kirgan yangi foydalanuvchi uchun, referrerning **o'sha paytdagi** tarifiga qarab beriladi. O'zini taklif qilish, mavjud foydalanuvchi uchun va takroriy mukofot bloklangan (`referrals.referred_id` - PRIMARY KEY).
- **Promokod:** bitta foydalanuvchi **kuniga bitta** promokod (kun - Toshkent vaqti, UTC+5) va **har bir kodni faqat bir marta** ishlatadi. Admin yaratganda ixtiyoriy umumiy limit (0 = cheksiz) belgilanadi.
- **TARIF OLISH:** to'lov avtomatik emas; foydalanuvchi @lwox_org ga yozadi, admin tasdiqlab tarifni qo'lda o'zgartiradi (bot buni aniq yozib qo'yadi).
- **PC NASTROYKALARI:** aynan 10 ta bo'lim (bazada `CHECK (num BETWEEN 1 AND 10)`); admin sarlavha va matnni tahrirlaydi, qo'sha/o'chira olmaydi. Faqat PREMIUM va KT CREATOR ko'radi (eski tugmalar orqali ham kirib bo'lmaydi).

**Telefon sozlamasi maydonlari** (har bir model uchun): `DEVICE`, `SENS`, `SCOPE SENS`, `GYRO MODE` (faqat 100Hz / 60Hz / 30Hz), `SENS (GYRO)`, `SCOPE SENS (GYRO)`, `OLINGAN VAQT`, `PROFIL ID`; KT CREATOR uchun qo'shimcha: `ACCELERATOR`, `KICHIK USHLAGICH`.

**Admin (`/admin`):** statistika · foydalanuvchini ID bilan qidirish (tanga qo'shish/ayirish, tarifni o'zgartirish - bonus oldindan ko'rsatiladi va tasdiqlanadi) · modellar (qo'shish, nomini o'zgartirish, o'chirish, har bir maydonni tahrirlash) · 10 ta PC bo'lim · promokod yaratish/o'chirish · barcha foydalanuvchilarga xabar yuborish (istalgan turdagi xabar, tasdiqdan keyin, ~20 xabar/s).

## Qabul qilingan farazlar (so'rovda aniq aytilmagan joylar)

Quyidagilarni o'zingizga moslab o'zgartirishingiz mumkin:

1. **PREMIUM va KT CREATOR ham telefon sozlamalarini ko'radi** (yuqori tarif quyidagi imkoniyatlarni yo'qotmaydi).
2. **PLAYER 10 tanga bonusi** admin tarifni faollashtirganda beriladi. Yangi ro'yxatdan o'tganda avtomatik berilmaydi (tanga farm qilishdan saqlanish uchun); xohlasangiz `PLAYER_BONUS_ON_START=1`.
3. So'rovdagi ikkinchi `SENS` / `SCOPE SENS` juftligi **gyro** sezgirligi deb qabul qilindi va `SENS (GYRO)` / `SCOPE SENS (GYRO)` deb nomlandi (`db.py` dagi `PHONE_FIELDS` da nomini o'zgartirish mumkin).
4. `OLINGAN VAQT` - admin qo'lda tahrirlaydi; maydon bo'sh bo'lsa, modelga **birinchi qiymat** kiritilganda avtomatik hozirgi vaqt qo'yiladi.
5. **Namunaviy ma'lumotlar:** iPhone 11-17 (7 ta, Pro/Max variantlarisiz) va Redmi/Samsung/Honor dan bir nechta namunaviy model qo'shiladi; **barcha sozlama qiymatlari bo'sh** (bot hech qanday sezgirlik qiymatini o'zi o'ylab topmaydi) - admin to'ldiradi. 10 ta PC bo'limning sarlavhalari ham namuna (`PC_DEFAULT_TITLES`), matnlari bo'sh.
6. Tangalar nimaga sarflanishi so'ralmagan, shuning uchun bot faqat balans, tarix va admin tomonidan boshqaruvni beradi.
7. Tarif narxlari so'ralmagan va botda ko'rsatilmaydi (narx uchun @lwox_org).

## Cheklovlar

- FSM holati (admin matn kiritayotgan paytdagi bosqich) **xotirada**: qayta ishga tushsa tozalanadi, admin `/admin` dan qayta boshlaydi. Baza ma'lumotlari saqlanib qoladi.
- Xabar yuborish jarayonida server qayta ishga tushsa, yuborish to'xtaydi (hisobot o'rniga log yoziladi).
- Ko'p akkaunt ochib referal/bonus yig'ishdan to'liq himoya yo'q (Telegram'da buni aniqlash qiyin).
- Foydalanuvchini bloklash (ban) funksiyasi so'ralmagani uchun yo'q.

## Nima tekshirildi, nima tekshirilmadi

Haqiqatan bajarilgan:
- `python -m unittest discover -s tests -v` - **20 ta test o'tdi** (SQLite): bonusning bir martaligi, referal himoyalari (o'zini taklif, takroriy, parallel so'rovlar), promokod qoidalari (kuniga bitta, takroriy, limit, parallel), tranzaksiya rollback, balans manfiy bo'lmasligi, 10 ta PC bo'lim cheklovi, qayta ulanganda ma'lumot saqlanishi.
- `bot.py` va `db.py` uchun statik tekshiruv: sintaksis, aniqlanmagan nomlar, ishlatilmagan importlar, Python 3.11 bilan mos f-string qoidalari.
- `bot.py` handlerlari aiogram'ning **yengil soxta (stub) nusxasi** bilan simulyatsiya qilindi: foydalanuvchi va admin oqimlari, FSM, admin himoyasi, 105 ta tugma `callback_data` sining to'g'ri handlerga yetib borishi va 64 bayt chegarasi (82 ta tekshiruv).

**Tekshirilmagan** (muhitda tarmoq va kutubxonalar yo'q edi):
- `bot.py` **haqiqiy aiogram 3 bilan ishga tushirilmadi** va haqiqiy Telegram'ga ulanmadi (webhook o'rnatish, `SimpleRequestHandler`, `copy_message`, Telegram javoblari). Aiogram API ishlatilishi xotiraga va hujjatlarga asoslangan; birinchi deploy'da loglarni kuzating.
- **PostgreSQL (`asyncpg`) yo'li ishga tushirilmadi.** Faqat `?` → `$n` almashtirish testlangan; SQL SQLite va PostgreSQL uchun umumiy sintaksisda yozilgan, lekin PostgreSQL'da real sinov o'tkazilmagan.
- `Dockerfile`, `render.yaml`, `Procfile` build/deploy qilib ko'rilmadi.

Birinchi ishga tushirishda tavsiya: `/start`, `/admin`, bitta modelga sozlama kiritish, o'zingizning ikkinchi akkauntingiz bilan referal va promokodni sinab ko'ring.
