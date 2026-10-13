# SO2 SETTINGS BOT — Render Web Service

Telegram bot for Standoff 2 device settings. It runs as a Render Web Service with a Telegram webhook and SQLite database.

## Included
- Main menu: Nastroyka, Profil, Tangalar, Referal, Tarif olish, Promokod, PC Nastroykalari, Yordam
- iPhone and Android model menus (Redmi, Samsung, Honor) with seed models
- Model-specific settings editable by admins
- PC section with exactly 10 settings; blocked for PLAYER users, available to PREMIUM / KT CREATOR and admins
- PLAYER / PREMIUM / KT CREATOR tariffs
- Referral links and coin rewards (PLAYER referrer: 1 coin; PREMIUM/KT CREATOR referrer: 2 coins)
- Tariff activation bonus via admin tariff change (PLAYER +10, PREMIUM +100)
- Promo codes, broadcast, statistics, user tariff and coin administration
- Persistent SQLite database at `/data/so2_settings.db`

## Deploy to Render
1. Create a Telegram bot with `@BotFather` and copy the token.
2. Push these files to a GitHub repository.
3. In Render choose **New + → Blueprint** and select that repository, or create a Web Service:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot.py`
   - Health check path: `/health`
4. Set environment variables:
   - `BOT_TOKEN` = token from BotFather
   - `ADMIN_IDS` = your Telegram numeric user ID (multiple IDs separated by commas)
   - `WEBHOOK_BASE` = deployed HTTPS URL, e.g. `https://your-service.onrender.com` (no trailing slash)
   - `DB_PATH` = `/data/so2_settings.db`
5. Add a persistent disk mounted at `/data` if creating the service manually. The Blueprint defines one.
6. Deploy and open `https://your-service.onrender.com/health`; expected JSON: `{"status":"ok"}`.
7. Send `/start` to the Telegram bot. Admins send `/admin`.

## Admin panel usage
- **Model qidirish / nastroyka**: search a model by its name, then press Edit. Enter seven values separated by `|`:
  `SENS | SCOPE SENS | GYRO(100Hz/60Hz/30Hz) | SENS2 | SCOPE2 | ACCELERATOR | KICHIK USHLAGICH`
  Example: `150 | 80 | 60Hz | 140 | 70 | 1.0 | 0.5`
- **Model qo‘shish**: `platform | brand | model name`
  - iPhone: `iphone | | iPhone 18`
  - Android: `android | Redmi | Redmi Note 10S Pro`
- **PC 10 nastroyka**: `number | title | text`, number 1–10.
- **ID orqali tarif**: `user_id | PLAYER` or `user_id | PREMIUM` or `user_id | KT CREATOR`
- **ID orqali tanga**: `user_id | amount` (negative amounts subtract)
- **Promokod yaratish**: `CODE | coins | use_count`
- **Xabar yuborish**: send message text after selecting broadcast.
- **Statistika**: total users, premium, creators, models and settings claims.

## Important behavior
- All device setting values start as placeholders (`***`) until the admin fills them in.
- The bot cannot automatically detect a player's device or modify in-game settings. Users choose a model manually.
- PC settings are not model-based and are not accessible to PLAYER tariff users.
- For a tariff purchase, contact `@lwox_org`. Payments are not automatically processed in this version; an admin activates the tariff from the admin panel.
- Telegram Bot API requires the bot token to be kept secret.
