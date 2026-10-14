"""
STANDOFF 2 SETTINGS BOT
=======================
Telegram bot: Python 3.11, aiogram 3, aiohttp webhook.
Ma'lumotlar: SQLite (DB_PATH) yoki PostgreSQL (DATABASE_URL) - db.py.

Ishga tushirish:  python bot.py   (muhit o'zgaruvchilari uchun README.md ga qarang)
"""
import asyncio
import hashlib
import html
import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from db import (
    BRANDS,
    GYRO_MODES,
    PHONE_FIELDS,
    SUPPORT_USERNAME,
    TARIFF_ORDER,
    TARIFFS,
    BotError,
    Database,
    PromoNotFound,
)

try:  # bir foydalanuvchining parallel update'lari FSM holatini buzmasligi uchun
    from aiogram.fsm.storage.memory import SimpleEventIsolation
except ImportError:  # pragma: no cover
    SimpleEventIsolation = None

log = logging.getLogger("bot")


# ============================================================================
# SOZLAMALAR (muhit o'zgaruvchilari)
# ============================================================================
@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset
    webhook_base: str
    webhook_path: str
    webhook_secret: str
    database_url: str
    db_path: str
    port: int
    player_bonus_on_start: bool


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
        sys.exit("XATO: BOT_TOKEN berilmagan yoki noto'g'ri formatda.")

    try:
        admin_ids = frozenset(int(x) for x in re.split(r"[,\s;]+", os.getenv("ADMIN_IDS", "")) if x)
    except ValueError:
        sys.exit("XATO: ADMIN_IDS faqat raqamlardan iborat bo'lishi kerak (vergul bilan ajrating).")
    if not admin_ids:
        sys.exit("XATO: ADMIN_IDS bo'sh. Kamida bitta admin Telegram ID kiriting.")

    base = (os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not base.startswith("https://"):
        sys.exit("XATO: WEBHOOK_BASE berilmagan yoki https:// bilan boshlanmaydi "
                 "(Telegram webhook uchun faqat HTTPS qabul qiladi).")

    digest = hashlib.sha256(token.encode()).hexdigest()
    secret = os.getenv("WEBHOOK_SECRET", "").strip() or digest[:48]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        sys.exit("XATO: WEBHOOK_SECRET faqat A-Z a-z 0-9 _ - belgilaridan iborat bo'lishi kerak.")

    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError:
        sys.exit("XATO: PORT raqam bo'lishi kerak.")

    return Settings(
        bot_token=token,
        admin_ids=admin_ids,
        webhook_base=base,
        webhook_path=f"/webhook/{digest[-24:]}",
        webhook_secret=secret,
        database_url=(os.getenv("DATABASE_URL") or "").strip(),
        db_path=(os.getenv("DB_PATH") or "data/bot.db").strip(),
        port=port,
        player_bonus_on_start=os.getenv("PLAYER_BONUS_ON_START", "").strip().lower() in {"1", "true", "yes", "on"},
    )


# ============================================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================================
PER_PAGE_MODELS = 8
PER_PAGE_TX = 10
PROMO_RE = re.compile(r"[A-Z0-9_-]{3,32}")
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def parse_int(text, lo: int, hi: int):
    t = (text or "").strip().replace(" ", "")
    if not re.fullmatch(r"\d{1,18}", t, re.ASCII):
        return None
    v = int(t)
    return v if lo <= v <= hi else None


def parse_ref(arg):
    """/start USER_ID  (yoki ref_USER_ID) -> int | None"""
    a = (arg or "").strip()
    if a.lower().startswith("ref_"):
        a = a[4:]
    return int(a) if re.fullmatch(r"\d{1,16}", a, re.ASCII) else None


def tariff_info(name: str) -> dict:
    return TARIFFS.get(name, TARIFFS["PLAYER"])


def btn(text: str, data: str = None, url: str = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def ikb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def grid(buttons: list, per_row: int = 2) -> list:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def pager(prefix: str, page: int, total: int, per_page: int) -> list:
    pages = max(1, -(-total // per_page))
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}:{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(btn("▶️", f"{prefix}:{page + 1}"))
    return row


def cb_parts(cb: CallbackQuery, n: int):
    p = (cb.data or "").split(":")
    return p if len(p) == n else None


def to_int(s: str):
    return int(s) if re.fullmatch(r"\d{1,18}", s or "", re.ASCII) else None


async def show(event, text: str, kb: InlineKeyboardMarkup = None) -> None:
    """Callback bo'lsa xabarni tahrirlaydi, oddiy xabar bo'lsa yangi xabar yuboradi."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        if isinstance(msg, Message):
            try:
                await msg.edit_text(text, reply_markup=kb)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
                log.debug("edit_text bajarilmadi (%s), yangi xabar yuboriladi", exc)
        await event.bot.send_message(event.from_user.id, text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


async def safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except TelegramAPIError as exc:
        log.info("Xabar yuborib bo'lmadi (%s): %s", chat_id, exc)
        return False


class FailLimiter:
    """Xotirada: oynada ko'p noto'g'ri urinish bo'lsa (promokod terib ko'rish) vaqtincha to'xtatadi."""

    def __init__(self, limit: int = 5, window: int = 600):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)

    def blocked_for(self, key) -> int:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return int(self.window - (now - q[0])) + 1
        return 0

    def add(self, key) -> None:
        if len(self.hits) > 2000:  # xotira o'smasligi uchun eskirgan yozuvlarni tozalaymiz
            now = time.monotonic()
            for k in [k for k, q in self.hits.items() if not q or now - q[-1] > self.window]:
                del self.hits[k]
        self.hits[key].append(time.monotonic())


PROMO_LIMITER = FailLimiter()


# ============================================================================
# MATNLAR VA MENYU
# ============================================================================
BTN_SETTINGS = "📱 NASTROYKA"
BTN_PROFILE = "👤 PROFIL"
BTN_COINS = "🪙 TANGALAR"
BTN_REFERRAL = "👥 REFERAL"
BTN_TARIFF = "💎 TARIF OLISH"
BTN_PROMO = "🎟 PROMOKOD"
BTN_PC = "🖥 PC NASTROYKALARI"
BTN_HELP = "🆘 YORDAM"
MENU_TEXTS = frozenset({BTN_SETTINGS, BTN_PROFILE, BTN_COINS, BTN_REFERRAL, BTN_TARIFF, BTN_PROMO, BTN_PC, BTN_HELP})

TX_LABELS = {
    "referral": "👥 Referal",
    "bonus": "🎁 Tarif bonusi",
    "promo": "🎟 Promokod",
    "admin_add": "➕ Admin qo'shdi",
    "admin_sub": "➖ Admin ayirdi",
}

WELCOME = (
    "👋 Assalomu alaykum, <b>{name}</b>!\n\n"
    "🎮 <b>STANDOFF 2 SETTINGS BOT</b> ga xush kelibsiz.\n\n"
    "Bu yerda iPhone, Android va PC uchun sozlamalarni topasiz. "
    "Kerakli bo'limni quyidagi menyudan tanlang 👇"
)

HELP_TEXT = (
    "🆘 <b>YORDAM</b>\n\n"
    "<b>Qanday foydalaniladi?</b>\n"
    "1️⃣ <b>📱 NASTROYKA</b> - qurilmangizni (iPhone / Android / PC) va modelingizni tanlang.\n"
    "2️⃣ <b>👥 REFERAL</b> - havolangizni do'stlaringizga yuboring; ular botga birinchi marta kirganda tanga olasiz.\n"
    "3️⃣ <b>🎟 PROMOKOD</b> - promokodni kiriting (kuniga bitta promokod, har bir kod faqat bir marta).\n"
    "4️⃣ <b>💎 TARIF OLISH</b> - PREMIUM yoki KT CREATOR olish tartibi.\n"
    "5️⃣ <b>🖥 PC NASTROYKALARI</b> - faqat PREMIUM va KT CREATOR uchun.\n\n"
    "Tarif to'lovi botda avtomatik emas: admin to'lovni qo'lda tasdiqlaydi.\n\n"
    f"❓ Savol yoki muammo bo'lsa: @{SUPPORT_USERNAME}"
)

PC_LOCKED = (
    "🔒 <b>PC sozlamalari yopiq</b>\n\n"
    "PC sozlamalari barcha tariflar uchun bepul.\n\n"
    f"Tarif olish uchun @{SUPPORT_USERNAME} bilan bog'laning."
)


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_SETTINGS, BTN_PROFILE],
        [BTN_COINS, BTN_REFERRAL],
        [BTN_TARIFF, BTN_PROMO],
        [BTN_PC, BTN_HELP],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
    )


def kb_contact() -> InlineKeyboardMarkup:
    return ikb([btn(f"📩 @{SUPPORT_USERNAME} bilan bog'lanish", url=SUPPORT_URL)])


def render_settings(model: dict, show_kt: bool, admin: bool = False) -> str:
    """Telefon sozlamalari matni. KT CREATOR maydonlari faqat KT CREATOR (yoki admin) uchun."""
    lines = [f"📱 <b>{esc(model['name'])}</b>"]
    if admin:
        lines[0] += f"  <i>({esc(BRANDS[model['brand']])})</i>"
    lines.append("")
    for col, label, kt_only in PHONE_FIELDS:
        if kt_only and not show_kt:
            continue
        value = model.get(col)
        tag = " (KT)" if (admin and kt_only) else ""
        lines.append(f"<b>{label}{tag}:</b> {esc(value) if value else '—'}")
    return "\n".join(lines)


def models_kb(rows, total, page, item_prefix, pager_prefix, footer_rows) -> InlineKeyboardMarkup:
    items = [btn(r["name"], f"{item_prefix}:{r['id']}") for r in rows]
    return ikb(*grid(items, 2), pager(pager_prefix, page, total, PER_PAGE_MODELS), *footer_rows)


# ============================================================================
# FSM HOLATLARI VA FILTRLAR
# ============================================================================
class UserStates(StatesGroup):
    promo = State()


class AdminStates(StatesGroup):
    user_id = State()
    coin_amount = State()
    model_name = State()
    model_rename = State()
    field_value = State()
    pc_value = State()
    promo_code = State()
    promo_coins = State()
    promo_max = State()
    broadcast_msg = State()


class PlainText(BaseFilter):
    """Oddiy matn: buyruq ('/...') ham, menyu tugmasi matni ham emas."""

    async def __call__(self, message: Message) -> bool:
        t = message.text
        return bool(t) and not t.startswith("/") and t not in MENU_TEXTS


class AnyContent(BaseFilter):
    """Xabar yuborish uchun: istalgan turdagi xabar (buyruq va menyu tugmalaridan tashqari)."""

    async def __call__(self, message: Message) -> bool:
        t = message.text or ""
        return not t.startswith("/") and t not in MENU_TEXTS


class IsAdmin(BaseFilter):
    """Faqat ADMIN_IDS dagi foydalanuvchilar va faqat shaxsiy chatda."""

    async def __call__(self, event: TelegramObject, settings: Settings) -> bool:
        user = getattr(event, "from_user", None)
        if user is None or user.id not in settings.admin_ids:
            return False
        msg = event if isinstance(event, Message) else getattr(event, "message", None)
        return msg is None or msg.chat.type == "private"


class TouchUser(BaseMiddleware):
    """Har bir update'da foydalanuvchini bazada yaratadi/yangilaydi va handlerga `user`, `is_new` beradi."""

    async def __call__(self, handler, event, data):
        tg = data.get("event_from_user")
        if tg is None or tg.is_bot:
            return await handler(event, data)
        user, created = await data["db"].touch_user(tg.id, tg.username, tg.first_name)
        data["user"], data["is_new"] = user, created
        return await handler(event, data)


user_router = Router(name="user")
user_router.message.middleware(TouchUser())
user_router.callback_query.middleware(TouchUser())

admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# ============================================================================
# FOYDALANUVCHI QISMI
# ============================================================================
@user_router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext,
                    db: Database, bot: Bot, user: dict, is_new: bool):
    await state.clear()
    referrer_id = parse_ref(command.args)
    # Referal faqat YANGI foydalanuvchi uchun (is_new). Mavjud foydalanuvchi havolani bossa mukofot yo'q.
    if is_new and referrer_id:
        res = await db.apply_referral(user["tg_id"], referrer_id)
        if res:
            await safe_send(
                bot, res["referrer_id"],
                f"🎉 Havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
                f"🪙 +{res['reward']} tanga. Balans: <b>{res['balance']}</b>",
            )
    name = esc(message.from_user.first_name or "do'st")
    await message.answer(WELCOME.format(name=name), reply_markup=main_menu())


@user_router.message(Command("menu"))
@user_router.message(Command("cancel"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("📋 Asosiy menyu", reply_markup=main_menu())


@user_router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ---------- NASTROYKA ----------
def kb_settings_root() -> InlineKeyboardMarkup:
    return ikb([btn("🍎 iPhone", "st:b:iphone:0"), btn("🤖 Android", "st:android")], [btn("🖥 PC", "st:pc")])


SETTINGS_ROOT_TEXT = "📱 <b>NASTROYKA</b>\n\nQurilmangiz turini tanlang:"


@user_router.message(F.text == BTN_SETTINGS)
async def menu_settings(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(SETTINGS_ROOT_TEXT, reply_markup=kb_settings_root())


@user_router.callback_query(F.data == "st:root")
async def cb_settings_root(cb: CallbackQuery):
    await show(cb, SETTINGS_ROOT_TEXT, kb_settings_root())
    await cb.answer()


@user_router.callback_query(F.data == "st:android")
async def cb_android(cb: CallbackQuery):
    brands = [btn(BRANDS[k], f"st:b:{k}:0") for k in ("redmi", "samsung", "honor", "xiaomi", "poco", "realme", "infinix", "tecno", "oneplus")]
    await show(cb, "🤖 <b>Android</b>\n\nBrendni tanlang:", ikb(brands, [btn("⬅️ Orqaga", "st:root")]))
    await cb.answer()


@user_router.callback_query(F.data.startswith("st:b:"))
async def cb_brand_models(cb: CallbackQuery, db: Database):
    p = cb_parts(cb, 4)
    page = to_int(p[3]) if p else None
    if not p or p[2] not in BRANDS or page is None:
        return await cb.answer("Tugma eskirgan. /start ni bosing.")
    brand = p[2]
    rows, total, page = await db.list_models(brand, page, PER_PAGE_MODELS)
    back = "st:root" if brand == "iphone" else "st:android"
    title = f"📱 <b>{esc(BRANDS[brand])}</b>\n\nModelingizni tanlang:" if rows else \
        f"📱 <b>{esc(BRANDS[brand])}</b>\n\nHozircha modellar qo'shilmagan."
    await show(cb, title, models_kb(rows, total, page, "st:m", f"st:b:{brand}", [[btn("⬅️ Orqaga", back)]]))
    await cb.answer()


@user_router.callback_query(F.data.startswith("st:m:"))
async def cb_model(cb: CallbackQuery, db: Database, user: dict):
    p = cb_parts(cb, 3)
    model_id = to_int(p[2]) if p else None
    model = await db.get_model(model_id) if model_id is not None else None
    if model is None:
        return await cb.answer("Model topilmadi (o'chirilgan bo'lishi mumkin).", show_alert=True)
    # PLAYER tarifidagi Android model sozlamasini ko'rish 10 tanga turadi.
    # PREMIUM va KT CREATOR tariflari bepul; balans yetmasa sozlama ochilmaydi.
    if user["tariff"] == "PLAYER":
        try:
            await db.adjust_coins(user["tg_id"], -10, cb.from_user.id)
        except BotError:
            fresh = await db.get_user(cb.from_user.id)
            balance = fresh["balance"] if fresh else 0
            return await cb.answer(
                f"Tanga yetarli emas! Kerak: 10 🪙. Balansingiz: {balance} 🪙",
                show_alert=True,
            )
    text = render_settings(model, show_kt=tariff_info(user["tariff"])["kt"])
    if user["tariff"] == "PLAYER":
        text += "\n\n🪙 Sozlama narxi: 10 tanga"
    await show(cb, text, ikb([btn("⬅️ Modellar", f"st:b:{model['brand']}:0")]))
    await cb.answer()


# ---------- PC ----------
async def send_pc_menu(event, db: Database, user: dict, with_back: bool, page: int = 0) -> None:
    if not tariff_info(user["tariff"])["pc"]:
        await show(event, PC_LOCKED, kb_contact())
        return
    sections = await db.list_pc_sections()
    page_size = 10
    page_count = max(1, (len(sections) + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    visible = sections[page * page_size:(page + 1) * page_size]
    rows = [[btn(f"{s['num']}. {s['title']}", f"st:pcs:{s['num']}")] for s in visible]
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Oldingi", f"st:pcpage:{page - 1}"))
    if page < page_count - 1:
        nav.append(btn("Keyingi ➡️", f"st:pcpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn(f"📄 {page + 1}/{page_count}-qism", "st:pcpage:noop")])
    if with_back:
        rows.append([btn("⬅️ Orqaga", "st:root")])
    await show(event, f"🖥 <b>PC NASTROYKALARI</b>\n\n{page + 1}-qism — {len(visible)} ta profil. Bo'limni tanlang:", ikb(*rows))


@user_router.message(F.text == BTN_PC)
async def menu_pc(message: Message, state: FSMContext, db: Database, user: dict):
    await state.clear()
    await send_pc_menu(message, db, user, with_back=False)


@user_router.callback_query(F.data == "st:pc")
async def cb_pc(cb: CallbackQuery, db: Database, user: dict):
    await send_pc_menu(cb, db, user, with_back=True)
    await cb.answer()


@user_router.callback_query(F.data == "st:pclist")
async def cb_pc_list(cb: CallbackQuery, db: Database, user: dict):
    await send_pc_menu(cb, db, user, with_back=False)
    await cb.answer()

@user_router.callback_query(F.data.startswith("st:pcpage:"))
async def cb_pc_page(cb: CallbackQuery, db: Database, user: dict):
    p = cb_parts(cb, 3)
    if p and p[2] == "noop":
        return await cb.answer()
    page = to_int(p[2]) if p else 0
    await send_pc_menu(cb, db, user, with_back=False, page=page or 0)
    await cb.answer()


@user_router.callback_query(F.data.startswith("st:pcs:"))
async def cb_pc_section(cb: CallbackQuery, db: Database, user: dict):
    if not tariff_info(user["tariff"])["pc"]:  # tarif tushirilgan bo'lsa eski tugma ishlamasin
        await show(cb, PC_LOCKED, kb_contact())
        return await cb.answer()
    p = cb_parts(cb, 3)
    num = to_int(p[2]) if p else None
    sec = await db.get_pc_section(num) if num is not None else None
    if sec is None:
        return await cb.answer("Bo'lim topilmadi.", show_alert=True)
    body = esc(sec["content"]) if sec["content"] else "<i>Hozircha ma'lumot kiritilmagan.</i>"
    await show(cb, f"🖥 <b>{sec['num']}. {esc(sec['title'])}</b>\n\n{body}",
               ikb([btn("⬅️ Bo'limlar", "st:pclist")]))
    await cb.answer()


# ---------- PROFIL ----------
@user_router.message(F.text == BTN_PROFILE)
async def menu_profile(message: Message, state: FSMContext, db: Database, user: dict):
    await state.clear()
    count, _earned = await db.referral_stats(user["tg_id"])
    uname = f"@{esc(user['username'])}" if user["username"] else "—"
    await message.answer(
        "👤 <b>PROFIL</b>\n\n"
        f"🆔 Telegram ID: <code>{user['tg_id']}</code>\n"
        f"👤 Username: {uname}\n"
        f"💎 Tarif: <b>{esc(user['tariff'])}</b>\n"
        f"🪙 Balans: <b>{user['balance']}</b>\n"
        f"👥 Referallar soni: <b>{count}</b>"
    )


# ---------- TANGALAR ----------
async def send_coins(event, db: Database, user: dict, page: int) -> None:
    rows, total, page = await db.list_transactions(user["tg_id"], page, PER_PAGE_TX)
    lines = ["🪙 <b>TANGALAR</b>", "", f"Balans: <b>{user['balance']}</b> 🪙", "", "📜 <b>Tranzaksiyalar tarixi</b>"]
    if not rows:
        lines.append("<i>Hozircha tranzaksiya yo'q.</i>")
    for r in rows:
        sign = "+" if r["amount"] > 0 else ""
        label = TX_LABELS.get(r["kind"], esc(r["kind"]))
        lines.append(f"<b>{sign}{r['amount']}</b> 🪙 · {label} · <i>{esc(r['created_at'][:16])}</i>")
    kb = ikb(pager("tx", page, total, PER_PAGE_TX))
    await show(event, "\n".join(lines), kb if kb.inline_keyboard else None)


@user_router.message(F.text == BTN_COINS)
async def menu_coins(message: Message, state: FSMContext, db: Database, user: dict):
    await state.clear()
    await send_coins(message, db, user, 0)


@user_router.callback_query(F.data.startswith("tx:"))
async def cb_coins_page(cb: CallbackQuery, db: Database, user: dict):
    p = cb_parts(cb, 2)
    page = to_int(p[1]) if p else None
    await send_coins(cb, db, user, page or 0)
    await cb.answer()


# ---------- REFERAL ----------
@user_router.message(F.text == BTN_REFERRAL)
async def menu_referral(message: Message, state: FSMContext, db: Database, user: dict, bot_username: str):
    await state.clear()
    count, earned = await db.referral_stats(user["tg_id"])
    link = f"https://t.me/{bot_username}?start={user['tg_id']}"
    reward = tariff_info(user["tariff"])["referral"]
    share = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote("Standoff 2 sozlamalari boti 🎮", safe="")
    await message.answer(
        "👥 <b>REFERAL</b>\n\n"
        "Sizning havolangiz:\n"
        f"<code>{link}</code>\n\n"
        f"💎 Tarifingiz: <b>{esc(user['tariff'])}</b> - har bir referal uchun <b>{reward} 🪙</b>\n"
        f"👥 Taklif qilinganlar: <b>{count}</b>\n"
        f"🪙 Referaldan topilgan: <b>{earned}</b>\n\n"
        "<b>Qoidalar:</b>\n"
        "• Mukofot faqat botga <b>birinchi marta</b> kirgan yangi foydalanuvchi uchun beriladi;\n"
        "• o'zingizni taklif qilib bo'lmaydi;\n"
        "• bitta foydalanuvchi uchun mukofot faqat bir marta beriladi.",
        reply_markup=ikb([btn("📤 Do'stlarga ulashish", url=share)]),
    )


# ---------- TARIF OLISH ----------
@user_router.message(F.text == BTN_TARIFF)
async def menu_tariff(message: Message, state: FSMContext, user: dict):
    await state.clear()
    await message.answer(
        "💎 <b>TARIFLAR</b>\n\n"
        "🔹 <b>PLAYER</b>\n"
        "• 📱 Telefon sozlamalari\n"
        "• 👥 Har bir referal uchun 1 🪙\n"
        "• 🎁 Faollashtirishda bir martalik 10 🪙 bonus\n\n"
        "🔸 <b>PREMIUM</b>\n"
        "• PLAYER imkoniyatlari + 🖥 PC sozlamalari\n"
        "• 👥 Har bir referal uchun 2 🪙\n"
        "• 🎁 Faollashtirishda bir martalik 100 🪙 bonus\n\n"
        "👑 <b>KT CREATOR</b>\n"
        "• PREMIUM imkoniyatlari\n"
        "• 📱 Qo'shimcha telefon sozlamalari (ACCELERATOR, KICHIK USHLAGICH)\n"
        "• 👥 Har bir referal uchun 2 🪙\n\n"
        f"Joriy tarifingiz: <b>{esc(user['tariff'])}</b>\n\n"
        f"📩 Tarif olish uchun @{SUPPORT_USERNAME} ga yozing va ID raqamingizni yuboring: <code>{user['tg_id']}</code>\n\n"
        "⚠️ <b>To'lov botda avtomatik amalga oshmaydi.</b> Admin to'lovni qo'lda tekshiradi va "
        "tasdiqlagach tarifingizni faollashtiradi.",
        reply_markup=kb_contact(),
    )


# ---------- PROMOKOD ----------
def kb_cancel_promo() -> InlineKeyboardMarkup:
    return ikb([btn("❌ Bekor qilish", "pr:cancel")])


@user_router.message(F.text == BTN_PROMO)
async def menu_promo(message: Message, state: FSMContext):
    await state.set_state(UserStates.promo)
    await message.answer(
        "🎟 <b>PROMOKOD</b>\n\nPromokodni yuboring.\n\n"
        "<i>Kuniga faqat bitta promokod ishlatish mumkin; har bir kod bir foydalanuvchi uchun bir martalik.</i>",
        reply_markup=kb_cancel_promo(),
    )


@user_router.callback_query(F.data == "pr:cancel")
async def cb_promo_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "Bekor qilindi.")
    await cb.answer()


@user_router.message(UserStates.promo, PlainText())
async def promo_submit(message: Message, state: FSMContext, db: Database, user: dict):
    uid = user["tg_id"]
    wait = PROMO_LIMITER.blocked_for(uid)
    if wait:
        return await message.answer(f"⏳ Juda ko'p noto'g'ri urinish. {wait // 60 + 1} daqiqadan keyin qayta urinib ko'ring.")
    code = message.text.strip().upper()
    if not PROMO_RE.fullmatch(code):
        PROMO_LIMITER.add(uid)
        return await message.answer("❌ Promokod formati noto'g'ri. Qayta yuboring yoki menyudan boshqa bo'limni tanlang.",
                                    reply_markup=kb_cancel_promo())
    try:
        res = await db.redeem_promo(uid, code)
    except PromoNotFound:
        PROMO_LIMITER.add(uid)
        return await message.answer("❌ Bunday promokod mavjud emas. Qayta yuboring yoki menyudan boshqa bo'limni tanlang.",
                                    reply_markup=kb_cancel_promo())
    except BotError as exc:
        return await message.answer(f"⚠️ {esc(exc)}", reply_markup=kb_cancel_promo())
    await state.clear()
    await message.answer(f"✅ Promokod qabul qilindi!\n🪙 +{res['coins']} tanga. Balans: <b>{res['balance']}</b>")


# ---------- YORDAM ----------
@user_router.message(F.text == BTN_HELP)
async def menu_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=kb_contact())


# ---------- noma'lum xabarlar (eng oxirida!) ----------
@user_router.message()
async def fallback_message(message: Message):
    await message.answer("Kerakli bo'limni quyidagi menyudan tanlang 👇", reply_markup=main_menu())


@user_router.callback_query()
async def fallback_callback(cb: CallbackQuery):
    await cb.answer("Bu tugma eskirgan. /start ni bosing.")


# ============================================================================
# ADMIN QISMI
# ============================================================================
ADMIN_HOME_TEXT = "🛠 <b>ADMIN PANEL</b>\n\nBo'limni tanlang:"
BACK_ADMIN = btn("⬅️ Admin menyu", "ad:home")


def kb_admin_home() -> InlineKeyboardMarkup:
    items = [
        btn("📊 Statistika", "ad:st"), btn("🔍 Foydalanuvchi", "ad:user"),
        btn("📱 Modellar", "ad:mod"), btn("🖥 PC sozlamalari", "ad:pc"),
        btn("🎟 Promokodlar", "ad:pr"), btn("📢 Xabar yuborish", "ad:bc"),
    ]
    return ikb(*grid(items, 2))


async def ask(event, state: FSMContext, new_state, text: str, back: str = "ad:home", **data) -> None:
    """Admindan matn kiritishni so'raydi (FSM)."""
    await state.set_state(new_state)
    if data:
        await state.update_data(**data)
    await show(event, text, ikb([btn("❌ Bekor qilish", back)]))


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(ADMIN_HOME_TEXT, reply_markup=kb_admin_home())


@admin_router.callback_query(F.data == "ad:home")
async def cb_admin_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, ADMIN_HOME_TEXT, kb_admin_home())
    await cb.answer()


# ---------- statistika ----------
@admin_router.callback_query(F.data == "ad:st")
async def cb_admin_stats(cb: CallbackQuery, db: Database):
    s = await db.stats()
    tariffs = "\n".join(f"   • {esc(t)}: <b>{s['by_tariff'].get(t, 0)}</b>" for t in TARIFF_ORDER)
    await show(
        cb,
        "📊 <b>STATISTIKA</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{s['total']}</b>\n"
        f"🆕 Bugun: <b>{s['today']}</b> · 7 kunda: <b>{s['week']}</b>\n"
        f"💎 Tariflar:\n{tariffs}\n"
        f"🪙 Jami balanslar: <b>{s['coins']}</b>\n"
        f"👥 Referallar: <b>{s['referrals']}</b>\n"
        f"🎟 Promokodlar: <b>{s['promos']}</b>\n"
        f"📱 Modellar: <b>{s['models']}</b>",
        ikb([BACK_ADMIN]),
    )
    await cb.answer()


# ---------- foydalanuvchini boshqarish ----------
async def show_user_card(event, db: Database, uid: int) -> None:
    user = await db.get_user(uid)
    if user is None:
        return await show(event, "❌ Foydalanuvchi topilmadi.", ikb([BACK_ADMIN]))
    count, earned = await db.referral_stats(uid)
    uname = f"@{esc(user['username'])}" if user["username"] else "—"
    text = (
        "👤 <b>Foydalanuvchi</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👤 Username: {uname}\n"
        f"📛 Ism: {esc(user['first_name'] or '—')}\n"
        f"💎 Tarif: <b>{esc(user['tariff'])}</b>\n"
        f"🪙 Balans: <b>{user['balance']}</b>\n"
        f"👥 Referallar: <b>{count}</b> (topgan: {earned} 🪙)\n"
        f"🔗 Kim taklif qilgan: {user['referrer_id'] or '—'}\n"
        f"📅 Ro'yxatdan o'tgan: {esc(user['created_at'])}"
    )
    kb = ikb(
        [btn("➕ Tanga qo'shish", f"ad:coin:add:{uid}"), btn("➖ Tanga ayirish", f"ad:coin:sub:{uid}")],
        [btn("🎫 Tarifni o'zgartirish", f"ad:tar:{uid}")],
        [BACK_ADMIN],
    )
    await show(event, text, kb)


@admin_router.callback_query(F.data == "ad:user")
async def cb_admin_user(cb: CallbackQuery, state: FSMContext):
    await ask(cb, state, AdminStates.user_id, "🔍 Foydalanuvchining Telegram ID raqamini yuboring:")
    await cb.answer()


@admin_router.message(AdminStates.user_id, PlainText())
async def admin_user_id(message: Message, state: FSMContext, db: Database):
    uid = parse_int(message.text, 1, 10 ** 16)
    if uid is None:
        return await message.answer("❗ Faqat raqam (Telegram ID) yuboring.")
    if await db.get_user(uid) is None:
        return await message.answer("❌ Bunday ID bazada yo'q (foydalanuvchi botga hali kirmagan). Boshqa ID yuboring.")
    await state.clear()
    await show_user_card(message, db, uid)


@admin_router.callback_query(F.data.startswith("ad:u:"))
async def cb_admin_user_card(cb: CallbackQuery, state: FSMContext, db: Database):
    p = cb_parts(cb, 3)
    uid = to_int(p[2]) if p else None
    if uid is None:
        return await cb.answer("Eskirgan tugma.")
    await state.clear()
    await show_user_card(cb, db, uid)
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:coin:"))
async def cb_admin_coin(cb: CallbackQuery, state: FSMContext):
    p = cb_parts(cb, 4)
    uid = to_int(p[3]) if p else None
    if uid is None or p[2] not in ("add", "sub"):
        return await cb.answer("Eskirgan tugma.")
    word = "qo'shish" if p[2] == "add" else "ayirish"
    await ask(cb, state, AdminStates.coin_amount,
              f"🪙 Foydalanuvchi <code>{uid}</code> balansidan <b>{word}</b> uchun tanga miqdorini yuboring (butun son):",
              back=f"ad:u:{uid}", uid=uid, mode=p[2])
    await cb.answer()


@admin_router.message(AdminStates.coin_amount, PlainText())
async def admin_coin_amount(message: Message, state: FSMContext, db: Database, bot: Bot):
    amount = parse_int(message.text, 1, 1_000_000)
    if amount is None:
        return await message.answer("❗ 1 dan 1 000 000 gacha butun son yuboring.")
    data = await state.get_data()
    uid, mode = data.get("uid"), data.get("mode")
    if uid is None or mode not in ("add", "sub"):
        await state.clear()
        return await message.answer("Eskirgan so'rov. /admin ni bosing.")
    signed = amount if mode == "add" else -amount
    try:
        balance = await db.adjust_coins(uid, signed, message.from_user.id)
    except BotError as exc:
        return await message.answer(f"❌ {esc(exc)}\nBoshqa miqdor yuboring yoki bekor qiling.")
    await state.clear()
    await db.audit(message.from_user.id, "coins", f"user={uid} amount={signed}")
    verb = "qo'shdi" if signed > 0 else "ayirdi"
    await safe_send(bot, uid, f"🪙 Admin balansingizga {abs(signed)} tanga {verb}. Yangi balans: <b>{balance}</b>")
    await message.answer(f"✅ Bajarildi. Yangi balans: <b>{balance}</b>")
    await show_user_card(message, db, uid)


@admin_router.callback_query(F.data.startswith("ad:tar:"))
async def cb_admin_tariff(cb: CallbackQuery, db: Database):
    p = cb_parts(cb, 3)
    uid = to_int(p[2]) if p else None
    user = await db.get_user(uid) if uid is not None else None
    if user is None:
        return await cb.answer("Foydalanuvchi topilmadi.", show_alert=True)
    rows = [[btn(("✅ " if t == user["tariff"] else "") + t, f"ad:tpick:{uid}:{i}")] for i, t in enumerate(TARIFF_ORDER)]
    rows.append([btn("⬅️ Orqaga", f"ad:u:{uid}")])
    await show(cb, f"🎫 <code>{uid}</code> uchun yangi tarifni tanlang (hozir: <b>{esc(user['tariff'])}</b>):", ikb(*rows))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:tpick:"))
async def cb_admin_tariff_pick(cb: CallbackQuery, db: Database):
    p = cb_parts(cb, 4)
    uid, idx = (to_int(p[2]), to_int(p[3])) if p else (None, None)
    if uid is None or idx is None or idx >= len(TARIFF_ORDER):
        return await cb.answer("Eskirgan tugma.")
    tariff = TARIFF_ORDER[idx]
    user = await db.get_user(uid)
    if user is None:
        return await cb.answer("Foydalanuvchi topilmadi.", show_alert=True)
    info = await db.bonus_info(uid, tariff)
    bonus_line = {
        "available": f"🎁 Bir martalik bonus: <b>+{TARIFFS[tariff]['bonus']} 🪙</b> (hali berilmagan, hozir beriladi)",
        "claimed": "🎁 Bu tarif bonusi avval berilgan - qayta berilmaydi.",
        "none": "🎁 Bu tarifda faollashtirish bonusi yo'q.",
    }[info]
    await show(
        cb,
        f"❓ <b>Tasdiqlang</b>\n\nFoydalanuvchi: <code>{uid}</code>\n"
        f"Tarif: <b>{esc(user['tariff'])}</b> → <b>{esc(tariff)}</b>\n{bonus_line}",
        ikb([btn("✅ Tasdiqlash", f"ad:tok:{uid}:{idx}"), btn("❌ Bekor", f"ad:u:{uid}")]),
    )
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:tok:"))
async def cb_admin_tariff_confirm(cb: CallbackQuery, db: Database, bot: Bot):
    p = cb_parts(cb, 4)
    uid, idx = (to_int(p[2]), to_int(p[3])) if p else (None, None)
    if uid is None or idx is None or idx >= len(TARIFF_ORDER):
        return await cb.answer("Eskirgan tugma.")
    try:
        res = await db.set_tariff(uid, TARIFF_ORDER[idx])
    except BotError as exc:
        return await cb.answer(str(exc), show_alert=True)
    await db.audit(cb.from_user.id, "tariff", f"user={uid} {res['old']}->{res['new']} bonus={res['bonus']}")
    note = f"\n🎁 Bonus: +{res['bonus']} 🪙 (balans: {res['balance']})" if res["bonus"] else ""
    await safe_send(bot, uid, f"💎 Tarifingiz <b>{esc(res['new'])}</b> ga o'zgartirildi.{note}")
    await cb.answer("✅ Tarif o'zgartirildi")
    await show_user_card(cb, db, uid)


# ---------- modellar va telefon sozlamalari ----------
def admin_model_kb(model_id: int, brand: str) -> InlineKeyboardMarkup:
    fields = [btn("✏️ " + label, f"ad:mf:{model_id}:{i}") for i, (_c, label, _kt) in enumerate(PHONE_FIELDS)]
    return ikb(
        *grid(fields, 2),
        [btn("🏷 Nomini o'zgartirish", f"ad:mr:{model_id}"), btn("🗑 O'chirish", f"ad:md:{model_id}")],
        [btn("⬅️ Modellar", f"ad:ml:{brand}:0")],
    )


async def show_admin_model(event, db: Database, model_id: int) -> None:
    model = await db.get_model(model_id)
    if model is None:
        return await show(event, "❌ Model topilmadi.", ikb([btn("⬅️ Modellar", "ad:mod")]))
    await show(event, render_settings(model, show_kt=True, admin=True) +
               "\n\n<i>Tahrirlash uchun maydonni tanlang.</i>", admin_model_kb(model_id, model["brand"]))


@admin_router.callback_query(F.data == "ad:mod")
async def cb_admin_models(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    items = [btn(label, f"ad:ml:{k}:0") for k, label in BRANDS.items()]
    await show(cb, "📱 <b>Modellar</b>\n\nBrendni tanlang:", ikb(*grid(items, 2), [BACK_ADMIN]))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:ml:"))
async def cb_admin_model_list(cb: CallbackQuery, state: FSMContext, db: Database):
    p = cb_parts(cb, 4)
    page = to_int(p[3]) if p else None
    if not p or p[2] not in BRANDS or page is None:
        return await cb.answer("Eskirgan tugma.")
    await state.clear()
    brand = p[2]
    rows, total, page = await db.list_models(brand, page, PER_PAGE_MODELS)
    footer = [[btn("➕ Model qo'shish", f"ad:maddb:{brand}")], [btn("⬅️ Brendlar", "ad:mod")]]
    await show(cb, f"📱 <b>{esc(BRANDS[brand])}</b> - modellar: {total}",
               models_kb(rows, total, page, "ad:m", f"ad:ml:{brand}", footer))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:maddb:"))
async def cb_admin_model_add(cb: CallbackQuery, state: FSMContext):
    p = cb_parts(cb, 3)
    if not p or p[2] not in BRANDS:
        return await cb.answer("Eskirgan tugma.")
    await ask(cb, state, AdminStates.model_name,
              f"➕ <b>{esc(BRANDS[p[2]])}</b> uchun yangi model nomini yuboring (2-40 belgi):",
              back=f"ad:ml:{p[2]}:0", brand=p[2])
    await cb.answer()


@admin_router.message(AdminStates.model_name, PlainText())
async def admin_model_name(message: Message, state: FSMContext, db: Database):
    name = " ".join(message.text.split())
    if not 2 <= len(name) <= 40:
        return await message.answer("❗ Nom 2 dan 40 gacha belgidan iborat bo'lsin.")
    brand = (await state.get_data()).get("brand")
    try:
        model_id = await db.add_model(brand, name)
    except BotError as exc:
        return await message.answer(f"❌ {esc(exc)} Boshqa nom yuboring.")
    await state.clear()
    await db.audit(message.from_user.id, "model_add", f"{brand}:{name}")
    await message.answer("✅ Model qo'shildi.")
    await show_admin_model(message, db, model_id)


@admin_router.callback_query(F.data.startswith("ad:m:"))
async def cb_admin_model(cb: CallbackQuery, state: FSMContext, db: Database):
    p = cb_parts(cb, 3)
    model_id = to_int(p[2]) if p else None
    if model_id is None:
        return await cb.answer("Eskirgan tugma.")
    await state.clear()
    await show_admin_model(cb, db, model_id)
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:mf:"))
async def cb_admin_field(cb: CallbackQuery, state: FSMContext):
    p = cb_parts(cb, 4)
    model_id, idx = (to_int(p[2]), to_int(p[3])) if p else (None, None)
    if model_id is None or idx is None or idx >= len(PHONE_FIELDS):
        return await cb.answer("Eskirgan tugma.")
    col, label, _kt = PHONE_FIELDS[idx]
    if col == "gyro_mode":  # faqat 100Hz / 60Hz / 30Hz dan bittasi
        modes = [btn(m, f"ad:gm:{model_id}:{i}") for i, m in enumerate(GYRO_MODES)]
        await show(cb, "🌀 <b>GYRO MODE</b> ni tanlang:",
                   ikb(modes, [btn("🧹 Tozalash", f"ad:gm:{model_id}:x")], [btn("⬅️ Orqaga", f"ad:m:{model_id}")]))
    else:
        await ask(cb, state, AdminStates.field_value,
                  f"✏️ <b>{label}</b> uchun yangi qiymatni yuboring (maks. 200 belgi).\n"
                  "Qiymatni o'chirish uchun <code>-</code> yuboring.",
                  back=f"ad:m:{model_id}", model_id=model_id, idx=idx)
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:gm:"))
async def cb_admin_gyro(cb: CallbackQuery, db: Database):
    p = cb_parts(cb, 4)
    model_id = to_int(p[2]) if p else None
    if model_id is None:
        return await cb.answer("Eskirgan tugma.")
    if p[3] == "x":
        value = None
    else:
        i = to_int(p[3])
        if i is None or i >= len(GYRO_MODES):
            return await cb.answer("Eskirgan tugma.")
        value = GYRO_MODES[i]
    try:
        await db.update_setting(model_id, "gyro_mode", value)
    except BotError as exc:
        return await cb.answer(str(exc), show_alert=True)
    await db.audit(cb.from_user.id, "setting", f"model={model_id} gyro_mode={value}")
    await cb.answer("✅ Saqlandi")
    await show_admin_model(cb, db, model_id)


@admin_router.message(AdminStates.field_value, PlainText())
async def admin_field_value(message: Message, state: FSMContext, db: Database):
    text = message.text.strip()
    if len(text) > 200:
        return await message.answer("❗ Juda uzun (maks. 200 belgi). Qisqaroq yuboring.")
    data = await state.get_data()
    model_id, idx = data.get("model_id"), data.get("idx")
    if model_id is None or idx is None or idx >= len(PHONE_FIELDS):
        await state.clear()
        return await message.answer("Eskirgan so'rov. /admin ni bosing.")
    value = None if text == "-" else text
    col = PHONE_FIELDS[idx][0]
    try:
        await db.update_setting(model_id, col, value)
    except BotError as exc:
        await state.clear()
        return await message.answer(f"❌ {esc(exc)}")
    await state.clear()
    await db.audit(message.from_user.id, "setting", f"model={model_id} {col}={value}")
    await message.answer("✅ Saqlandi.")
    await show_admin_model(message, db, model_id)


@admin_router.callback_query(F.data.startswith("ad:mr:"))
async def cb_admin_rename(cb: CallbackQuery, state: FSMContext):
    p = cb_parts(cb, 3)
    model_id = to_int(p[2]) if p else None
    if model_id is None:
        return await cb.answer("Eskirgan tugma.")
    await ask(cb, state, AdminStates.model_rename, "🏷 Modelning yangi nomini yuboring (2-40 belgi):",
              back=f"ad:m:{model_id}", model_id=model_id)
    await cb.answer()


@admin_router.message(AdminStates.model_rename, PlainText())
async def admin_rename_value(message: Message, state: FSMContext, db: Database):
    name = " ".join(message.text.split())
    if not 2 <= len(name) <= 40:
        return await message.answer("❗ Nom 2 dan 40 gacha belgidan iborat bo'lsin.")
    model_id = (await state.get_data()).get("model_id")
    try:
        await db.rename_model(model_id, name)
    except BotError as exc:
        return await message.answer(f"❌ {esc(exc)} Boshqa nom yuboring.")
    await state.clear()
    await db.audit(message.from_user.id, "model_rename", f"model={model_id} name={name}")
    await message.answer("✅ Nom o'zgartirildi.")
    await show_admin_model(message, db, model_id)


@admin_router.callback_query(F.data.startswith("ad:md:"))
async def cb_admin_delete_ask(cb: CallbackQuery, db: Database):
    p = cb_parts(cb, 3)
    model_id = to_int(p[2]) if p else None
    model = await db.get_model(model_id) if model_id is not None else None
    if model is None:
        return await cb.answer("Model topilmadi.", show_alert=True)
    await show(cb, f"🗑 <b>{esc(model['name'])}</b> modeli va uning sozlamalari butunlay o'chiriladi. Davom etasizmi?",
               ikb([btn("✅ Ha, o'chirish", f"ad:mdy:{model_id}"), btn("❌ Yo'q", f"ad:m:{model_id}")]))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:mdy:"))
async def cb_admin_delete(cb: CallbackQuery, db: Database):
    p = cb_parts(cb, 3)
    model_id = to_int(p[2]) if p else None
    model = await db.get_model(model_id) if model_id is not None else None
    if model is None:
        return await cb.answer("Model topilmadi.", show_alert=True)
    await db.delete_model(model_id)
    await db.audit(cb.from_user.id, "model_delete", f"{model['brand']}:{model['name']}")
    await cb.answer("🗑 O'chirildi")
    await show(cb, f"🗑 <b>{esc(model['name'])}</b> o'chirildi.", ikb([btn("⬅️ Modellar", f"ad:ml:{model['brand']}:0")]))


# ---------- PC bo'limlari (aynan 10 ta; faqat tahrirlanadi) ----------
@admin_router.callback_query(F.data == "ad:pc")
async def cb_admin_pc(cb: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    rows = [[btn(f"{s['num']}. {s['title']}", f"ad:pcs:{s['num']}")] for s in await db.list_pc_sections()]
    await show(cb, "🖥 <b>PC sozlamalari</b> (10 ta bo'lim)\n\nTahrirlash uchun bo'limni tanlang:", ikb(*rows, [BACK_ADMIN]))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:pcs:"))
async def cb_admin_pc_section(cb: CallbackQuery, state: FSMContext, db: Database):
    p = cb_parts(cb, 3)
    num = to_int(p[2]) if p else None
    sec = await db.get_pc_section(num) if num is not None else None
    if sec is None:
        return await cb.answer("Bo'lim topilmadi.", show_alert=True)
    await state.clear()
    content = sec["content"]
    preview = esc(content[:600]) + ("…" if len(content) > 600 else "") if content else "<i>(bo'sh)</i>"
    await show(
        cb, f"🖥 <b>{sec['num']}. {esc(sec['title'])}</b>\n\n{preview}",
        ikb([btn("✏️ Sarlavha", f"ad:pce:{num}:t"), btn("📝 Matn", f"ad:pce:{num}:c")],
            [btn("⬅️ Bo'limlar", "ad:pc")]),
    )
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:pce:"))
async def cb_admin_pc_edit(cb: CallbackQuery, state: FSMContext):
    p = cb_parts(cb, 4)
    num = to_int(p[2]) if p else None
    if num is None or p[3] not in ("t", "c"):
        return await cb.answer("Eskirgan tugma.")
    if p[3] == "t":
        prompt = f"✏️ {num}-bo'lim uchun yangi <b>sarlavha</b> yuboring (1-40 belgi):"
    else:
        prompt = f"📝 {num}-bo'lim uchun yangi <b>matn</b> yuboring (maks. 3500 belgi).\nTozalash uchun <code>-</code> yuboring."
    await ask(cb, state, AdminStates.pc_value, prompt, back=f"ad:pcs:{num}", num=num, part=p[3])
    await cb.answer()


@admin_router.message(AdminStates.pc_value, PlainText())
async def admin_pc_value(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    num, part = data.get("num"), data.get("part")
    if num is None or part not in ("t", "c"):
        await state.clear()
        return await message.answer("Eskirgan so'rov. /admin ni bosing.")
    text = message.text.strip()
    if part == "t":
        title = " ".join(text.split())
        if not 1 <= len(title) <= 40:
            return await message.answer("❗ Sarlavha 1 dan 40 gacha belgidan iborat bo'lsin.")
        await db.update_pc_section(num, title=title)
    else:
        if len(text) > 3500:
            return await message.answer(f"❗ Matn juda uzun ({len(text)}). Maksimum 3500 belgi.")
        await db.update_pc_section(num, content="" if text == "-" else text)
    await state.clear()
    await db.audit(message.from_user.id, "pc_section", f"num={num} part={part}")
    await message.answer("✅ Saqlandi.")
    sec = await db.get_pc_section(num)
    await show(message, f"🖥 <b>{sec['num']}. {esc(sec['title'])}</b>",
               ikb([btn("⬅️ Bo'limlar", "ad:pc")]))


# ---------- promokodlar ----------
async def show_promos(event, db: Database) -> None:
    promos = await db.list_promos()
    lines = ["🎟 <b>Promokodlar</b>", ""]
    if not promos:
        lines.append("<i>Hozircha promokod yo'q.</i>")
    for pr in promos:
        limit = pr["max_uses"] if pr["max_uses"] else "∞"
        lines.append(f"<code>{esc(pr['code'])}</code> - {pr['coins']} 🪙 · ishlatilgan: {pr['used_count']}/{limit}")
    rows = [[btn("➕ Yaratish", "ad:prn")]]
    rows += grid([btn(f"🗑 {pr['code']}", f"ad:prd:{pr['code']}") for pr in promos], 2)
    rows.append([BACK_ADMIN])
    await show(event, "\n".join(lines), ikb(*rows))


@admin_router.callback_query(F.data == "ad:pr")
async def cb_admin_promos(cb: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    await show_promos(cb, db)
    await cb.answer()


@admin_router.callback_query(F.data == "ad:prn")
async def cb_admin_promo_new(cb: CallbackQuery, state: FSMContext):
    await ask(cb, state, AdminStates.promo_code,
              "🎟 Yangi promokod matnini yuboring (3-32 belgi: A-Z, 0-9, _ yoki -):", back="ad:pr")
    await cb.answer()


@admin_router.message(AdminStates.promo_code, PlainText())
async def admin_promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    if not PROMO_RE.fullmatch(code):
        return await message.answer("❗ Faqat A-Z, 0-9, _ va - ; uzunligi 3-32 belgi.")
    await ask(message, state, AdminStates.promo_coins,
              f"🪙 <code>{esc(code)}</code> necha tanga bersin? (1 - 1 000 000)", back="ad:pr", code=code)


@admin_router.message(AdminStates.promo_coins, PlainText())
async def admin_promo_coins(message: Message, state: FSMContext):
    coins = parse_int(message.text, 1, 1_000_000)
    if coins is None:
        return await message.answer("❗ 1 dan 1 000 000 gacha butun son yuboring.")
    await ask(message, state, AdminStates.promo_max,
              "👥 Jami necha foydalanuvchi ishlata oladi? (0 = cheklovsiz)", back="ad:pr", coins=coins)


@admin_router.message(AdminStates.promo_max, PlainText())
async def admin_promo_max(message: Message, state: FSMContext, db: Database):
    max_uses = parse_int(message.text, 0, 1_000_000)
    if max_uses is None:
        return await message.answer("❗ 0 dan 1 000 000 gacha butun son yuboring.")
    data = await state.get_data()
    code, coins = data.get("code"), data.get("coins")
    if not code or coins is None:
        await state.clear()
        return await message.answer("Eskirgan so'rov. /admin ni bosing.")
    try:
        await db.create_promo(code, coins, max_uses, message.from_user.id)
    except BotError as exc:
        await state.clear()
        return await message.answer(f"❌ {esc(exc)}")
    await state.clear()
    await db.audit(message.from_user.id, "promo_create", f"{code} coins={coins} max={max_uses}")
    await message.answer("✅ Promokod yaratildi.")
    await show_promos(message, db)


@admin_router.callback_query(F.data.startswith("ad:prd:"))
async def cb_admin_promo_del_ask(cb: CallbackQuery):
    p = cb_parts(cb, 3)
    if not p or not PROMO_RE.fullmatch(p[2]):
        return await cb.answer("Eskirgan tugma.")
    await show(cb, f"🗑 <code>{esc(p[2])}</code> promokodi o'chirilsinmi?",
               ikb([btn("✅ Ha", f"ad:prdy:{p[2]}"), btn("❌ Yo'q", "ad:pr")]))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("ad:prdy:"))
async def cb_admin_promo_del(cb: CallbackQuery, db: Database):
    p = cb_parts(cb, 3)
    if not p or not PROMO_RE.fullmatch(p[2]):
        return await cb.answer("Eskirgan tugma.")
    try:
        await db.delete_promo(p[2])
    except BotError as exc:
        await cb.answer(str(exc), show_alert=True)
    else:
        await db.audit(cb.from_user.id, "promo_delete", p[2])
        await cb.answer("🗑 O'chirildi")
    await show_promos(cb, db)


# ---------- xabar yuborish ----------
BROADCAST = {"running": False}
BACKGROUND_TASKS = set()


async def run_broadcast(bot: Bot, db: Database, admin_id: int, src_chat: int, src_msg: int) -> None:
    sent = blocked = failed = 0
    try:
        ids = await db.all_user_ids()
        for uid in ids:
            for _attempt in range(2):
                try:
                    await bot.copy_message(chat_id=uid, from_chat_id=src_chat, message_id=src_msg)
                    sent += 1
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                    continue
                except TelegramForbiddenError:
                    blocked += 1
                except TelegramAPIError as exc:
                    failed += 1
                    log.warning("Broadcast: %s ga yuborilmadi: %s", uid, exc)
                break
            else:
                failed += 1
            await asyncio.sleep(0.05)  # ~20 xabar/soniya (Telegram limiti ~30/s)
        await db.audit(admin_id, "broadcast", f"sent={sent} blocked={blocked} failed={failed}")
        await safe_send(bot, admin_id,
                        f"📢 <b>Xabar yuborish yakunlandi</b>\n\n✅ Yuborildi: {sent}\n🚫 Bloklagan: {blocked}\n⚠️ Xato: {failed}")
    except asyncio.CancelledError:
        log.warning("Broadcast to'xtatildi (server o'chirilmoqda): sent=%s", sent)
        raise
    except Exception:
        log.exception("Broadcast xatosi")
        await safe_send(bot, admin_id, f"⚠️ Xabar yuborish xato bilan to'xtadi. Yuborilgan: {sent}")
    finally:
        BROADCAST["running"] = False


@admin_router.callback_query(F.data == "ad:bc")
async def cb_admin_broadcast(cb: CallbackQuery, state: FSMContext):
    if BROADCAST["running"]:
        return await cb.answer("Hozir boshqa xabar yuborilmoqda. Tugashini kuting.", show_alert=True)
    await ask(cb, state, AdminStates.broadcast_msg,
              "📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring (matn, rasm, video... istalgani).")
    await cb.answer()


@admin_router.message(AdminStates.broadcast_msg, AnyContent())
async def admin_broadcast_msg(message: Message, state: FSMContext, db: Database):
    total = len(await db.all_user_ids())
    await state.update_data(bc_chat=message.chat.id, bc_msg=message.message_id)
    await message.answer(
        f"❓ Yuqoridagi xabar <b>{total}</b> ta foydalanuvchiga yuborilsinmi?",
        reply_markup=ikb([btn("✅ Yuborish", "ad:bcy"), btn("❌ Bekor", "ad:home")]),
    )


@admin_router.callback_query(F.data == "ad:bcy")
async def cb_admin_broadcast_go(cb: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    data = await state.get_data()
    chat_id, msg_id = data.get("bc_chat"), data.get("bc_msg")
    await state.clear()
    if chat_id is None or msg_id is None:
        return await cb.answer("Eskirgan so'rov. Qaytadan boshlang.", show_alert=True)
    if BROADCAST["running"]:
        return await cb.answer("Hozir boshqa xabar yuborilmoqda.", show_alert=True)
    BROADCAST["running"] = True
    task = asyncio.create_task(run_broadcast(bot, db, cb.from_user.id, chat_id, msg_id))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    await show(cb, "📤 Yuborish boshlandi. Tugagach hisobot keladi.", ikb([BACK_ADMIN]))
    await cb.answer()


# ============================================================================
# XATOLARNI USHLASH
# ============================================================================
async def on_error(event: ErrorEvent) -> bool:
    log.error("Update'ni qayta ishlashda xato: %r", event.exception, exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer("⚠️ Xatolik yuz berdi. Keyinroq urinib ko'ring.", show_alert=True)
        elif update.message:
            await update.message.answer("⚠️ Xatolik yuz berdi. Keyinroq urinib ko'ring.")
    except Exception as exc:  # foydalanuvchiga xabar berib bo'lmasa ham bot ishlashda davom etadi
        log.debug("Xatolik haqida xabar berib bo'lmadi: %s", exc)
    return True


# ============================================================================
# DISPATCHER VA WEB-ILOVA
# ============================================================================
def build_dispatcher(settings: Settings, db: Database) -> Dispatcher:
    kwargs = {"storage": MemoryStorage()}  # FSM holati xotirada (qayta ishga tushganda tozalanadi)
    if SimpleEventIsolation is not None:
        kwargs["events_isolation"] = SimpleEventIsolation()
    dp = Dispatcher(**kwargs)
    dp["settings"] = settings
    dp["db"] = db
    dp.errors.register(on_error)
    dp.include_router(admin_router)  # tartib muhim: admin handlerlari birinchi
    dp.include_router(user_router)
    return dp


async def configure_webhook(bot: Bot, dp: Dispatcher, settings: Settings) -> None:
    url = settings.webhook_base + settings.webhook_path
    last_exc = None
    for attempt in range(1, 6):
        try:
            await bot.set_webhook(
                url=url,
                secret_token=settings.webhook_secret,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=False,
                max_connections=10,  # kichik (bepul) server uchun parallellikni cheklaydi
            )
            info = await bot.get_webhook_info()
            log.info("Webhook o'rnatildi: %s/webhook/*** (kutilayotgan update'lar: %s)",
                     settings.webhook_base, info.pending_update_count)
            if info.last_error_message:
                log.warning("Telegram oxirgi webhook xatosi: %s", info.last_error_message)
            return
        except TelegramUnauthorizedError:
            raise
        except TelegramAPIError as exc:
            last_exc = exc
            log.warning("set_webhook %s/5 muvaffaqiyatsiz: %s", attempt, exc)
            await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"Webhook o'rnatib bo'lmadi: {last_exc}")


def create_app(settings: Settings) -> web.Application:
    db = Database(settings.database_url or None, settings.db_path, settings.player_bonus_on_start)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = build_dispatcher(settings, db)

    async def health(_request: web.Request) -> web.Response:
        try:
            await db.fetchval("SELECT 1")
        except Exception:
            log.exception("/health: baza javob bermadi")
            return web.json_response({"status": "error", "db": "down"}, status=503)
        return web.json_response({"status": "ok", "db": db.backend})

    async def ping(_request: web.Request) -> web.Response:
        return web.Response(text="ok")  # bazaga tegmaydi: tez-tez so'raladigan monitoring/uyg'otish uchun

    async def lifecycle(_app: web.Application):
        await db.connect()
        try:
            me = await bot.get_me()  # tokenni darhol tekshiradi
            dp["bot_username"] = me.username
            log.info("Bot: @%s | baza: %s | adminlar: %d ta", me.username, db.backend, len(settings.admin_ids))
            if db.backend == "sqlite":
                log.warning("SQLite ishlatilmoqda: %s. Hosting fayl tizimi vaqtincha bo'lsa (bepul rejalar), "
                            "qayta deploy/uyqudan keyin BARCHA MA'LUMOT YO'QOLADI. Doimiy saqlash uchun "
                            "DATABASE_URL (tashqi PostgreSQL) bering.", os.path.abspath(settings.db_path))
            await configure_webhook(bot, dp, settings)
            yield
        finally:
            # Webhook ATAYIN o'chirilmaydi: yangi nusxa allaqachon o'rnatgan bo'lishi mumkin.
            for task in list(BACKGROUND_TASKS):
                task.cancel()
            if BACKGROUND_TASKS:
                await asyncio.gather(*BACKGROUND_TASKS, return_exceptions=True)
            await bot.session.close()  # HTTP sessiyani yopish
            await db.close()
            log.info("To'xtatildi: bot sessiyasi va baza yopildi")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/ping", ping)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.webhook_secret).register(
        app, path=settings.webhook_path)
    setup_application(app, dp, bot=bot)
    app.cleanup_ctx.append(lifecycle)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    app = create_app(settings)
    web.run_app(app, host="0.0.0.0", port=settings.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
