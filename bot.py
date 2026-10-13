import os
import asyncio
import logging
import sqlite3
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("so2-settings")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").strip().rstrip("/")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).split(",") if x.strip().isdigit()}
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = "/webhook"
DB_PATH = os.getenv("DB_PATH", "/data/so2_settings.db")
if not os.path.isdir(os.path.dirname(DB_PATH) or "."):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

if not BOT_TOKEN:
    log.warning("BOT_TOKEN environment variable is not set. Set it in Render Environment.")

dp = Dispatcher()
bot: Optional[Bot] = None

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 NASTROYKA"), KeyboardButton(text="👤 PROFIL")],
        [KeyboardButton(text="🪙 TANGALAR"), KeyboardButton(text="👥 REFERAL")],
        [KeyboardButton(text="💎 TARIF OLISH"), KeyboardButton(text="🎟 PROMOKOD")],
        [KeyboardButton(text="🖥 PC NASTROYKALARI"), KeyboardButton(text="🆘 YORDAM")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Bo‘limni tanlang"
)

def now_text():
    return datetime.now().strftime("%d.%m.%Y %H:%M")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            coins INTEGER NOT NULL DEFAULT 0, tariff TEXT NOT NULL DEFAULT 'PLAYER',
            referred_by INTEGER, joined_at TEXT NOT NULL, last_promo_date TEXT
        );
        CREATE TABLE IF NOT EXISTS models(
            id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
            brand TEXT NOT NULL DEFAULT '', model_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, UNIQUE(platform, brand, model_name)
        );
        CREATE TABLE IF NOT EXISTS settings(
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER NOT NULL,
            sens TEXT DEFAULT '***', scope_sens TEXT DEFAULT '***',
            gyro_mode TEXT DEFAULT '60Hz', sens2 TEXT DEFAULT '***',
            scope_sens2 TEXT DEFAULT '***', accelerator TEXT DEFAULT '',
            small_grip TEXT DEFAULT '', updated_at TEXT,
            FOREIGN KEY(model_id) REFERENCES models(id) ON DELETE CASCADE,
            UNIQUE(model_id)
        );
        CREATE TABLE IF NOT EXISTS pc_settings(
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            model_id INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(model_id) REFERENCES models(id)
        );
        CREATE TABLE IF NOT EXISTS promos(
            code TEXT PRIMARY KEY, reward INTEGER NOT NULL DEFAULT 0,
            uses_left INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS promo_uses(
            user_id INTEGER NOT NULL, code TEXT NOT NULL, used_at TEXT NOT NULL,
            PRIMARY KEY(user_id, code)
        );
        CREATE TABLE IF NOT EXISTS referrals(
            inviter_id INTEGER NOT NULL, invited_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Seed common models; admins can add any missing model from the admin panel.
        seed = [
            ("iphone", "", "iPhone 11"), ("iphone", "", "iPhone 11 Pro"), ("iphone", "", "iPhone 11 Pro Max"),
            ("iphone", "", "iPhone 12"), ("iphone", "", "iPhone 12 Pro"), ("iphone", "", "iPhone 12 Pro Max"),
            ("iphone", "", "iPhone 13"), ("iphone", "", "iPhone 13 Pro"), ("iphone", "", "iPhone 13 Pro Max"),
            ("iphone", "", "iPhone 14"), ("iphone", "", "iPhone 14 Pro"), ("iphone", "", "iPhone 14 Pro Max"),
            ("iphone", "", "iPhone 15"), ("iphone", "", "iPhone 15 Pro"), ("iphone", "", "iPhone 15 Pro Max"),
            ("iphone", "", "iPhone 16"), ("iphone", "", "iPhone 16 Pro"), ("iphone", "", "iPhone 16 Pro Max"),
            ("iphone", "", "iPhone 17"), ("iphone", "", "iPhone 17 Pro"), ("iphone", "", "iPhone 17 Pro Max"),
            ("android", "Redmi", "Redmi Note 10S"), ("android", "Redmi", "Redmi Note 10"), 
            ("android", "Redmi", "Redmi Note 11"), ("android", "Redmi", "Redmi Note 12"),
            ("android", "Redmi", "Redmi Note 13"), ("android", "Redmi", "Redmi Note 14"),
            ("android", "Redmi", "Redmi 12"), ("android", "Redmi", "Redmi 13"),
            ("android", "Samsung", "Samsung Galaxy A15"), ("android", "Samsung", "Samsung Galaxy A25"),
            ("android", "Samsung", "Samsung Galaxy A35"), ("android", "Samsung", "Samsung Galaxy A55"),
            ("android", "Samsung", "Samsung Galaxy S23"), ("android", "Samsung", "Samsung Galaxy S24"),
            ("android", "Samsung", "Samsung Galaxy S25"), ("android", "Honor", "Honor X7"),
            ("android", "Honor", "Honor X8"), ("android", "Honor", "Honor X9"),
            ("android", "Honor", "Honor X9b"), ("android", "Honor", "Honor 90"),
            ("android", "Honor", "Honor 200"), ("android", "Honor", "Honor Magic6 Pro"),
        ]
        for platform, brand, name in seed:
            c.execute("INSERT OR IGNORE INTO models(platform,brand,model_name) VALUES(?,?,?)", (platform, brand, name))
        for row in c.execute("SELECT id FROM models").fetchall():
            c.execute("INSERT OR IGNORE INTO settings(model_id,updated_at) VALUES(?,?)", (row["id"], now_text()))
        for i in range(1, 11):
            c.execute("INSERT OR IGNORE INTO pc_settings(id,title,content) VALUES(?,?,?)",
                      (i, f"PC Nastroyka #{i}", "Bu nastroyka admin tomonidan to‘ldirilmagan. Admin paneldan qiymat kiriting."))
        c.execute("INSERT OR IGNORE INTO settings_meta(key,value) VALUES('premium_contact','@lwox_org')")

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def ensure_user(user):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,username,full_name,joined_at) VALUES(?,?,?,?)",
                  (user.id, user.username or "", user.full_name or "", now_text()))
        c.execute("UPDATE users SET username=?,full_name=? WHERE user_id=?",
                  (user.username or "", user.full_name or "", user.id))

def get_user(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def set_tariff(uid, tariff):
    with db() as c:
        c.execute("UPDATE users SET tariff=? WHERE user_id=?", (tariff, uid))

def add_coins(uid, amount):
    with db() as c:
        c.execute("UPDATE users SET coins=MAX(0, coins + ?) WHERE user_id=?", (amount, uid))

def main_kb():
    return MAIN_MENU

def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def model_keyboard(rows, prefix="model", back="platform"):
    buttons = [[InlineKeyboardButton(text=r["model_name"], callback_data=f"{prefix}:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back)])
    return inline(buttons)

async def send_main(message: Message, text="🎮 SO2 SETTINGS BOT\nKerakli bo‘limni tanlang."):
    await message.answer(text, reply_markup=main_kb())

@dp.message(CommandStart())
async def start(message: Message):
    ensure_user(message.from_user)
    # Referral deep link: /start ref_123456
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            inviter = int(parts[1][4:])
            if inviter != message.from_user.id:
                with db() as c:
                    exists = c.execute("SELECT 1 FROM referrals WHERE invited_id=?", (message.from_user.id,)).fetchone()
                    inviter_exists = c.execute("SELECT 1 FROM users WHERE user_id=?", (inviter,)).fetchone()
                    if not exists and inviter_exists:
                        c.execute("INSERT INTO referrals(inviter_id,invited_id,created_at) VALUES(?,?,?)",
                                  (inviter, message.from_user.id, now_text()))
                        inviter_row = c.execute("SELECT tariff FROM users WHERE user_id=?", (inviter,)).fetchone()
                        reward = 2 if inviter_row and inviter_row["tariff"] in ("PREMIUM", "KT CREATOR") else 1
                        c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (reward, inviter))
        except ValueError:
            pass
    await send_main(message, f"🎮 Assalomu alaykum, {message.from_user.full_name}!\n\n📱 Standoff 2 uchun qurilmangizga mos nastroykani tanlang.")

@dp.message(F.text == "📱 NASTROYKA")
async def settings_menu(message: Message):
    await message.answer("📱 Qurilma turini tanlang:", reply_markup=inline([
        [InlineKeyboardButton(text="🍎 iPhone", callback_data="platform:iphone")],
        [InlineKeyboardButton(text="🤖 Android", callback_data="platform:android")],
    ]))

@dp.callback_query(F.data == "platform:iphone")
async def iphone_list(call: CallbackQuery):
    with db() as c:
        rows = c.execute("SELECT * FROM models WHERE platform='iphone' AND active=1 ORDER BY model_name").fetchall()
    await call.message.edit_text("🍎 iPhone modelini tanlang:", reply_markup=model_keyboard(rows, back="settings:home"))
    await call.answer()

@dp.callback_query(F.data == "platform:android")
async def android_brands(call: CallbackQuery):
    await call.message.edit_text("🤖 Android brendini tanlang:", reply_markup=inline([
        [InlineKeyboardButton(text="🔴 Redmi", callback_data="brand:Redmi")],
        [InlineKeyboardButton(text="🔵 Samsung", callback_data="brand:Samsung")],
        [InlineKeyboardButton(text="🟢 Honor", callback_data="brand:Honor")],
        [InlineKeyboardButton(text="🔎 Boshqa / barcha Android", callback_data="brand:all")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="settings:home")],
    ]))
    await call.answer()

@dp.callback_query(F.data.startswith("brand:"))
async def brand_models(call: CallbackQuery):
    brand = call.data.split(":",1)[1]
    with db() as c:
        if brand == "all":
            rows = c.execute("SELECT * FROM models WHERE platform='android' AND active=1 ORDER BY brand,model_name").fetchall()
        else:
            rows = c.execute("SELECT * FROM models WHERE platform='android' AND brand=? AND active=1 ORDER BY model_name", (brand,)).fetchall()
    if not rows:
        await call.answer("Bu brend uchun model hali qo‘shilmagan.", show_alert=True)
        return
    await call.message.edit_text(f"📱 {brand} modelini tanlang:", reply_markup=model_keyboard(rows, back="platform:android"))
    await call.answer()

@dp.callback_query(F.data.startswith("model:"))
async def deliver_settings(call: CallbackQuery):
    model_id = int(call.data.split(":",1)[1])
    user = get_user(call.from_user.id)
    with db() as c:
        row = c.execute("""SELECT m.model_name,s.* FROM models m JOIN settings s ON s.model_id=m.id
                           WHERE m.id=? AND m.active=1""", (model_id,)).fetchone()
    if not row:
        await call.answer("Nastroyka topilmadi.", show_alert=True)
        return
    if user and user["tariff"] == "PLAYER" and not is_admin(call.from_user.id):
        # Player can receive phone settings; PC settings are gated separately.
        pass
    with db() as c:
        c.execute("INSERT INTO claims(user_id,model_id,created_at) VALUES(?,?,?)",
                  (call.from_user.id, model_id, now_text()))
    gyro = row["gyro_mode"] if row["gyro_mode"] in ("100Hz", "60Hz", "30Hz") else "60Hz"
    lines = [
        f"DEVICE: {row['model_name']}", "",
        f"SENS: {row['sens'] or '***'}",
        f"SCOPE SENS: {row['scope_sens'] or '***'}",
        f"GYRO MODE: {gyro}",
        f"SENS: {row['sens2'] or '***'}",
        f"SCOPE SENS: {row['scope_sens2'] or '***'}",
        f"OLINGAN VAQT: {now_text()}",
        f"PROFIL ID: {call.from_user.id}",
    ]
    if user and user["tariff"] in ("PREMIUM", "KT CREATOR"):
        if row["accelerator"]:
            lines.insert(1, f"ACCELERATOR: {row['accelerator']}")
        if row["small_grip"]:
            lines.insert(2, f"KICHIK USHLAGICH: {row['small_grip']}")
    await call.message.answer("🎯 STANDOFF 2 NASTROYKA\n\n" + "\n".join(lines))
    await call.answer("Nastroyka yuborildi!")

@dp.callback_query(F.data == "settings:home")
async def settings_home(call: CallbackQuery):
    await call.message.edit_text("📱 Qurilma turini tanlang:", reply_markup=inline([
        [InlineKeyboardButton(text="🍎 iPhone", callback_data="platform:iphone")],
        [InlineKeyboardButton(text="🤖 Android", callback_data="platform:android")],
    ]))
    await call.answer()

@dp.message(F.text == "🖥 PC NASTROYKALARI")
async def pc_menu(message: Message):
    user = get_user(message.from_user.id)
    if not user or user["tariff"] not in ("PREMIUM", "KT CREATOR") and not is_admin(message.from_user.id):
        await message.answer("🔒 PC nastroykalari oddiy PLAYER tarifida mavjud emas. Tarif olish uchun @lwox_org ga murojaat qiling.")
        return
    with db() as c:
        rows = c.execute("SELECT id,title FROM pc_settings WHERE active=1 ORDER BY id").fetchall()
    await message.answer("🖥 PC uchun 10 ta nastroykadan birini tanlang:", reply_markup=inline(
        [[InlineKeyboardButton(text=r["title"], callback_data=f"pc:{r['id']}")] for r in rows]
    ))

@dp.callback_query(F.data.startswith("pc:"))
async def pc_deliver(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user or user["tariff"] not in ("PREMIUM", "KT CREATOR") and not is_admin(call.from_user.id):
        await call.answer("PC nastroykalari faqat PREMIUM yoki KT CREATOR tarifida!", show_alert=True)
        return
    pc_id = int(call.data.split(":",1)[1])
    with db() as c:
        row = c.execute("SELECT * FROM pc_settings WHERE id=? AND active=1", (pc_id,)).fetchone()
    if not row:
        await call.answer("Nastroyka topilmadi.", show_alert=True)
        return
    await call.message.answer(f"🖥 STANDOFF 2 — {row['title']}\n\n{row['content']}\n\nOLINGAN VAQT: {now_text()}\nPROFIL ID: {call.from_user.id}")
    await call.answer()

@dp.message(F.text == "👤 PROFIL")
async def profile(message: Message):
    u = get_user(message.from_user.id)
    with db() as c:
        claims = c.execute("SELECT COUNT(*) n FROM claims WHERE user_id=?", (message.from_user.id,)).fetchone()["n"]
        refs = c.execute("SELECT COUNT(*) n FROM referrals WHERE inviter_id=?", (message.from_user.id,)).fetchone()["n"]
    await message.answer(
        f"👤 PROFIL\n\n🆔 ID: {message.from_user.id}\n🎖 Tarif: {u['tariff']}\n🪙 Tangalar: {u['coins']}\n👥 Referallar: {refs}\n🎯 Olingan nastroykalar: {claims}",
        reply_markup=inline([[InlineKeyboardButton(text="💎 Tarif olish", callback_data="tariff:info")]])
    )

@dp.message(F.text == "🪙 TANGALAR")
async def coins(message: Message):
    u = get_user(message.from_user.id)
    await message.answer(f"🪙 Balansingiz: {u['coins']} tanga\n\nTangalarni referal orqali yig‘ishingiz mumkin.")

@dp.message(F.text == "👥 REFERAL")
async def referral(message: Message):
    me = await bot.get_me()
    u = get_user(message.from_user.id)
    reward = 2 if u["tariff"] in ("PREMIUM", "KT CREATOR") else 1
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM referrals WHERE inviter_id=?", (message.from_user.id,)).fetchone()["n"]
    await message.answer(f"👥 REFERAL TIZIMI\n\nSizning havolangiz:\n{link}\n\nTaklif qilinganlar: {n}\nHar bir yangi referal uchun: {reward} tanga.\nReferal birinchi marta botga kirganda hisoblanadi.")

@dp.message(F.text == "💎 TARIF OLISH")
async def tariff_info(message: Message):
    await message.answer("💎 TARIFLAR\n\n🟢 PLAYER — bepul, telefon nastroykalari.\n💎 PREMIUM — PC nastroykalari va qo‘shimcha sozlamalar.\n👑 KT CREATOR — admin tasdiqlaydigan kontentmeyker tarifi.\n\nTarif sotib olish uchun: @lwox_org",
                         reply_markup=inline([[InlineKeyboardButton(text="📩 @lwox_org ga murojaat", url="https://t.me/lwox_org")]]))

@dp.callback_query(F.data == "tariff:info")
async def tariff_info_cb(call: CallbackQuery):
    await call.message.answer("💎 Tarif sotib olish uchun @lwox_org ga murojaat qiling.")
    await call.answer()

@dp.message(F.text == "🎟 PROMOKOD")
async def promo_start(message: Message):
    await message.answer("🎟 Promokodni kiriting. Promokoddan kuniga 1 marta foydalanish mumkin.")
    # use per-user FSM-free marker in DB meta
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings_meta(key,value) VALUES(?,?)", (f"await_promo_{message.from_user.id}", "1"))

@dp.message(F.text == "🆘 YORDAM")
async def help_message(message: Message):
    await message.answer("🆘 Yordam kerakmi? Tarif va sozlamalar bo‘yicha @lwox_org ga yozing.")

@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 ADMIN PANEL", reply_markup=inline([
        [InlineKeyboardButton(text="🔎 Model qidirish / nastroyka", callback_data="adm:search")],
        [InlineKeyboardButton(text="➕ Model qo‘shish", callback_data="adm:addmodel")],
        [InlineKeyboardButton(text="🖥 PC 10 nastroyka", callback_data="adm:pc")],
        [InlineKeyboardButton(text="👤 ID orqali tarif", callback_data="adm:tariff")],
        [InlineKeyboardButton(text="🪙 ID orqali tanga", callback_data="adm:coins")],
        [InlineKeyboardButton(text="🎟 Promokod yaratish", callback_data="adm:promo")],
        [InlineKeyboardButton(text="📣 Xabar yuborish", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats")],
    ]))

# Admin text-input workflows are stored in settings_meta, so they survive restarts.
def set_admin_state(uid, state):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings_meta(key,value) VALUES(?,?)", (f"admin_state_{uid}", state))
def get_admin_state(uid):
    with db() as c:
        r = c.execute("SELECT value FROM settings_meta WHERE key=?", (f"admin_state_{uid}",)).fetchone()
        return r["value"] if r else ""
def clear_admin_state(uid):
    with db() as c:
        c.execute("DELETE FROM settings_meta WHERE key=?", (f"admin_state_{uid}",))

@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Adminlar uchun.", show_alert=True)
        return
    action = call.data.split(":",1)[1]
    if action.startswith("edit:"):
        model_id = int(action.split(":", 1)[1])
        with db() as c:
            row = c.execute("SELECT model_name FROM models WHERE id=?", (model_id,)).fetchone()
        if not row:
            await call.answer("Model topilmadi.", show_alert=True)
            return
        set_admin_state(call.from_user.id, f"editsettings:{model_id}")
        await call.message.answer(
            f"✏️ {row['model_name']} nastroykasini quyidagi formatda yuboring:\n"
            "SENS | SCOPE SENS | GYRO(100Hz/60Hz/30Hz) | SENS2 | SCOPE2 | ACCELERATOR | KICHIK USHLAGICH\n"
            "Misol: 150 | 80 | 60Hz | 140 | 70 | 1.0 | 0.5"
        )
        await call.answer()
        return
    prompts = {
        "search": "Model nomini yozing (masalan: Redmi Note 10S).",
        "addmodel": "Modelni shu formatda yuboring: platforma | brend | model nomi\nMisol: android | Redmi | Redmi Note 10S Pro\niPhone uchun: iphone | | iPhone 18",
        "tariff": "Foydalanuvchi ID va tarifni yuboring:\nID | PLAYER yoki PREMIUM yoki KT CREATOR",
        "coins": "Foydalanuvchi ID va tanga miqdorini yuboring:\nID | miqdor (manfiy son ham mumkin)",
        "promo": "Promokod yaratish: KOD | tanga | foydalanish_soni",
        "broadcast": "Yuboriladigan xabar matnini yuboring.",
        "pc": "PC nastroykasini o‘zgartirish uchun: raqam | sarlavha | matn\nMisol: 1 | PC Nastroyka #1 | Sens: ...",
    }
    if action == "stats":
        with db() as c:
            users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            prem = c.execute("SELECT COUNT(*) n FROM users WHERE tariff='PREMIUM'").fetchone()["n"]
            creators = c.execute("SELECT COUNT(*) n FROM users WHERE tariff='KT CREATOR'").fetchone()["n"]
            models = c.execute("SELECT COUNT(*) n FROM models WHERE active=1").fetchone()["n"]
            claims = c.execute("SELECT COUNT(*) n FROM claims").fetchone()["n"]
        await call.message.answer(f"📊 STATISTIKA\nFoydalanuvchilar: {users}\nPremium: {prem}\nKT Creator: {creators}\nModellar: {models}\nOlingan nastroykalar: {claims}")
    else:
        set_admin_state(call.from_user.id, action)
        await call.message.answer(prompts.get(action, "Ma'lumot yuboring."))
    await call.answer()

@dp.message()
async def catch_all(message: Message):
    uid = message.from_user.id
    # Promo code input
    with db() as c:
        waiting = c.execute("SELECT value FROM settings_meta WHERE key=?", (f"await_promo_{uid}",)).fetchone()
    if waiting:
        code = (message.text or "").strip().upper()
        with db() as c:
            u = c.execute("SELECT last_promo_date FROM users WHERE user_id=?", (uid,)).fetchone()
            today = datetime.now().strftime("%Y-%m-%d")
            if u and u["last_promo_date"] == today:
                c.execute("DELETE FROM settings_meta WHERE key=?", (f"await_promo_{uid}",))
                await message.answer("⏳ Promokod bugun ishlatildi. Ertaga qayta urinib ko‘ring.")
                return
            p = c.execute("SELECT * FROM promos WHERE code=? AND active=1 AND uses_left>0", (code,)).fetchone()
            used = c.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code)).fetchone()
            if p and not used:
                c.execute("UPDATE users SET coins=coins+?,last_promo_date=? WHERE user_id=?", (p["reward"], today, uid))
                c.execute("UPDATE promos SET uses_left=uses_left-1 WHERE code=?", (code,))
                c.execute("INSERT INTO promo_uses(user_id,code,used_at) VALUES(?,?,?)", (uid, code, now_text()))
                c.execute("DELETE FROM settings_meta WHERE key=?", (f"await_promo_{uid}",))
                await message.answer(f"✅ Promokod qabul qilindi! +{p['reward']} tanga.")
            else:
                await message.answer("❌ Promokod noto‘g‘ri, tugagan yoki avval ishlatilgan. Qayta kiriting yoki /start bosing.")
        return
    state = get_admin_state(uid) if is_admin(uid) else ""
    if state:
        text = (message.text or "").strip()
        try:
            if state.startswith("editsettings:"):
                model_id = int(state.split(":",1)[1])
                parts = [p.strip() for p in text.split("|")]
                if len(parts) != 7:
                    await message.answer("7 ta qiymat kerak: SENS | SCOPE SENS | GYRO(100Hz/60Hz/30Hz) | SENS2 | SCOPE2 | ACCELERATOR | KICHIK USHLAGICH")
                    return
                sens, scope, gyro, sens2, scope2, accel, grip = parts
                if gyro not in ("100Hz", "60Hz", "30Hz"):
                    await message.answer("GYRO qiymati 100Hz, 60Hz yoki 30Hz bo‘lishi kerak.")
                    return
                with db() as c:
                    c.execute("""UPDATE settings SET sens=?,scope_sens=?,gyro_mode=?,sens2=?,scope_sens2=?,
                                 accelerator=?,small_grip=?,updated_at=? WHERE model_id=?""",
                              (sens, scope, gyro, sens2, scope2, accel, grip, now_text(), model_id))
                clear_admin_state(uid)
                await message.answer("✅ Nastroyka saqlandi.")
            elif state == "search":
                with db() as c:
                    rows = c.execute("SELECT * FROM models WHERE model_name LIKE ? ORDER BY model_name LIMIT 30", (f"%{text}%",)).fetchall()
                if not rows:
                    await message.answer("Model topilmadi. Uni ➕ Model qo‘shish orqali kiriting.")
                else:
                    for r in rows:
                        await message.answer(f"🔎 {r['model_name']} (ID: {r['id']})", reply_markup=inline([
                            [InlineKeyboardButton(text="✏️ Nastroykani tahrirlash", callback_data=f"adm:edit:{r['id']}")]
                        ]))
                clear_admin_state(uid)
            elif state == "addmodel":
                parts = [p.strip() for p in text.split("|")]
                if len(parts) != 3 or parts[0].lower() not in ("iphone", "android"):
                    await message.answer("Format xato. Misol: android | Redmi | Redmi Note 10S Pro")
                    return
                platform, brand, name = parts
                with db() as c:
                    c.execute("INSERT OR IGNORE INTO models(platform,brand,model_name) VALUES(?,?,?)", (platform.lower(), brand, name))
                    r = c.execute("SELECT id FROM models WHERE platform=? AND brand=? AND model_name=?", (platform.lower(), brand, name)).fetchone()
                    c.execute("INSERT OR IGNORE INTO settings(model_id,updated_at) VALUES(?,?)", (r["id"], now_text()))
                clear_admin_state(uid)
                await message.answer(f"✅ Model saqlandi: {name}")
            elif state == "tariff":
                parts = [p.strip() for p in text.split("|")]
                if len(parts) != 2 or parts[1].upper() not in ("PLAYER", "PREMIUM", "KT CREATOR"):
                    await message.answer("Format: ID | PLAYER yoki PREMIUM yoki KT CREATOR")
                    return
                target, tariff = int(parts[0]), parts[1].upper()
                with db() as c:
                    existing = c.execute("SELECT tariff FROM users WHERE user_id=?", (target,)).fetchone()
                    if not existing:
                        await message.answer("Bu ID botdan hali foydalanmagan.")
                        return
                    old_tariff = existing["tariff"]
                    c.execute("UPDATE users SET tariff=? WHERE user_id=?", (tariff, target))
                clear_admin_state(uid)
                await message.answer(f"✅ {target} tarifi: {tariff}")
                try:
                    await bot.send_message(target, f"🎉 Sizning tarifingiz admin tomonidan {tariff} ga o‘zgartirildi.")
                    if old_tariff != tariff and tariff == "PREMIUM":
                        add_coins(target, 100)
                        await bot.send_message(target, "🪙 Premium tarifi faollashdi: +100 tanga!")
                    elif old_tariff != tariff and tariff == "PLAYER":
                        add_coins(target, 10)
                        await bot.send_message(target, "🪙 Player tarifi faollashdi: +10 tanga!")
                except Exception:
                    pass
            elif state == "coins":
                parts = [p.strip() for p in text.split("|")]
                if len(parts) != 2:
                    await message.answer("Format: ID | miqdor")
                    return
                target, amount = int(parts[0]), int(parts[1])
                with db() as c:
                    c.execute("UPDATE users SET coins=MAX(0,coins+?) WHERE user_id=?", (amount, target))
                    exists = c.execute("SELECT changes() n").fetchone()["n"]
                clear_admin_state(uid)
                await message.answer(f"✅ Tanga balansi yangilandi. (O‘zgarish: {exists})")
                try:
                    await bot.send_message(target, f"🪙 Admin hisobingizni {amount:+d} tanga ga o‘zgartirdi.")
                except Exception:
                    pass
            elif state == "promo":
                parts = [p.strip() for p in text.split("|")]
                if len(parts) != 3:
                    await message.answer("Format: KOD | tanga | foydalanish_soni")
                    return
                code, reward, uses = parts[0].upper(), int(parts[1]), int(parts[2])
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO promos(code,reward,uses_left,active) VALUES(?,?,?,1)", (code, reward, uses))
                clear_admin_state(uid)
                await message.answer(f"✅ Promokod yaratildi: {code} (+{reward} tanga, {uses} foydalanish)")
            elif state == "broadcast":
                clear_admin_state(uid)
                with db() as c:
                    ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]
                ok = 0
                for target in ids:
                    try:
                        await bot.send_message(target, text)
                        ok += 1
                    except Exception:
                        pass
                await message.answer(f"📣 Xabar yuborildi: {ok}/{len(ids)} foydalanuvchi.")
            elif state == "pc":
                parts = [p.strip() for p in text.split("|", 2)]
                if len(parts) != 3:
                    await message.answer("Format: raqam | sarlavha | matn")
                    return
                pcid, title, content = int(parts[0]), parts[1], parts[2]
                if pcid < 1 or pcid > 10:
                    await message.answer("Raqam 1 dan 10 gacha bo‘lishi kerak.")
                    return
                with db() as c:
                    c.execute("UPDATE pc_settings SET title=?,content=? WHERE id=?", (title, content, pcid))
                clear_admin_state(uid)
                await message.answer(f"✅ PC nastroyka #{pcid} yangilandi.")
        except Exception as e:
            log.exception("Admin input error")
            await message.answer(f"❌ Xatolik: {e}")
        return
    # Avoid responding to every random text.
    await message.answer("Menyudan bo‘limni tanlang yoki /start bosing.", reply_markup=MAIN_MENU)

@dp.callback_query(F.data.startswith("adm:edit:"))
async def admin_edit_settings(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Adminlar uchun.", show_alert=True)
        return
    model_id = int(call.data.rsplit(":",1)[1])
    with db() as c:
        row = c.execute("SELECT m.model_name,s.* FROM models m JOIN settings s ON s.model_id=m.id WHERE m.id=?", (model_id,)).fetchone()
    if not row:
        await call.answer("Model topilmadi.", show_alert=True)
        return
    set_admin_state(call.from_user.id, f"editsettings:{model_id}")
    await call.message.answer(
        f"✏️ {row['model_name']} nastroykasini quyidagi formatda yuboring:\n"
        "SENS | SCOPE SENS | GYRO(100Hz/60Hz/30Hz) | SENS2 | SCOPE2 | ACCELERATOR | KICHIK USHLAGICH\n"
        "Misol: 150 | 80 | 60Hz | 140 | 70 | 1.0 | 0.5"
    )
    await call.answer()

# Process model setting edit as a dedicated state before generic admin states.
@dp.message(F.text)
async def admin_edit_settings_text(message: Message):
    if not is_admin(message.from_user.id):
        return
    state = get_admin_state(message.from_user.id)
    if not state.startswith("editsettings:"):
        return
    try:
        model_id = int(state.split(":",1)[1])
        parts = [p.strip() for p in (message.text or "").split("|")]
        if len(parts) != 7:
            await message.answer("7 ta qiymat kerak, ularni | bilan ajrating.")
            return
        sens, scope, gyro, sens2, scope2, accel, grip = parts
        if gyro not in ("100Hz", "60Hz", "30Hz"):
            await message.answer("GYRO qiymati 100Hz, 60Hz yoki 30Hz bo‘lishi kerak.")
            return
        with db() as c:
            c.execute("""UPDATE settings SET sens=?,scope_sens=?,gyro_mode=?,sens2=?,scope_sens2=?,
                         accelerator=?,small_grip=?,updated_at=? WHERE model_id=?""",
                      (sens, scope, gyro, sens2, scope2, accel, grip, now_text(), model_id))
        clear_admin_state(message.from_user.id)
        await message.answer("✅ Nastroyka saqlandi.")
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

async def on_startup(app):
    if bot is None:
        return
    init_db()
    await bot.set_my_commands([BotCommand(command="start", description="Botni boshlash"),
                               BotCommand(command="admin", description="Admin panel")])
    if WEBHOOK_BASE:
        await bot.set_webhook(f"{WEBHOOK_BASE}{WEBHOOK_PATH}", drop_pending_updates=False)
        log.info("Webhook set: %s%s", WEBHOOK_BASE, WEBHOOK_PATH)
    else:
        log.warning("WEBHOOK_BASE not set; webhook was not registered.")

async def on_shutdown(app):
    if bot:
        await bot.session.close()

def create_app():
    global bot
    if not BOT_TOKEN:
        # Still provide health endpoint; configure BOT_TOKEN in Render to activate bot.
        application = web.Application()
        async def health(_request):
            return web.json_response({"status": "ok", "bot": "not_configured"})
        application.router.add_get("/", health)
        application.router.add_get("/health", health)
        return application
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    init_db()
    app = web.Application()
    app.router.add_get("/", lambda request: web.json_response({"status":"ok","service":"SO2 SETTINGS BOT"}))
    app.router.add_get("/health", lambda request: web.json_response({"status":"ok"}))
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)
    return app

app = create_app()

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
