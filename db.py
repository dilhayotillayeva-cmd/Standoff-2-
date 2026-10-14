"""
db.py - ma'lumotlar bazasi qatlami va biznes-mantiq.

Ikki backend qo'llab-quvvatlanadi:
  * SQLite     - standart (stdlib `sqlite3`, alohida thread'da ishlaydi). DB_PATH orqali.
  * PostgreSQL - DATABASE_URL berilsa (`asyncpg`).

Barcha SQL so'rovlar `?` placeholder bilan yoziladi; PostgreSQL uchun avtomatik
`$1, $2, ...` ga o'giriladi. Pul/tanga bilan bog'liq barcha amallar tranzaksiya
ichida bajariladi (qisman yozilib qolmaydi).
"""
import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from functools import lru_cache

log = logging.getLogger("db")

# Toshkent vaqti (UTC+5; O'zbekistonda yozgi/qishki vaqt almashinuvi yo'q)
TZ = timezone(timedelta(hours=5))


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def days_ago_str(days: int) -> str:
    return (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================================
# DOMEN KONSTANTALARI
# ============================================================================
SUPPORT_USERNAME = "lwox_org"

# referral - bitta referal uchun tanga; bonus - tarifni faollashtirishdagi bir martalik bonus
# pc - PC sozlamalariga ruxsat; kt - KT CREATOR qo'shimcha maydonlariga ruxsat
TARIFFS = {
    "PLAYER": {"referral": 1, "bonus": 10, "pc": True, "kt": False},
    "PREMIUM": {"referral": 2, "bonus": 100, "pc": True, "kt": False},
    "KT CREATOR": {"referral": 2, "bonus": 0, "pc": True, "kt": True},
}
TARIFF_ORDER = tuple(TARIFFS)
DEFAULT_TARIFF = "PLAYER"

# brend kaliti -> ko'rsatiladigan nom
BRANDS = {
    "iphone": "iPhone", "redmi": "Redmi", "samsung": "Samsung",
    "honor": "Honor", "xiaomi": "Xiaomi", "poco": "POCO",
    "realme": "Realme", "infinix": "Infinix", "tecno": "Tecno",
    "oneplus": "OnePlus",
}
PHONE_FIELDS = [
    ("device", "DEVICE", False), ("sens", "SENS", False),
    ("scope_sens", "SCOPE SENS", False), ("gyro_mode", "GYRO MODE", False),
    ("gyro_sens", "SENS (GYRO)", False), ("gyro_scope_sens", "SCOPE SENS (GYRO)", False),
    ("taken_at", "OLINGAN VAQT", False), ("profile_id", "PROFIL ID", False),
    ("accelerator", "ACCELERATOR", True), ("small_holder", "KICHIK USHLAGICH", True),
]
PHONE_COLUMNS = tuple(col for col, _label, _kt in PHONE_FIELDS)
GYRO_MODES = ("100Hz", "60Hz", "30Hz")
PC_DEFAULT_TITLES = [
    "PC profil 01 — Balansli", "PC profil 02 — Past sezgirlik",
    "PC profil 03 — Yuqori sezgirlik", "PC profil 04 — Agressiv",
    "PC profil 05 — Aniqlik", "PC profil 06 — Snayper",
    "PC profil 07 — Rifler", "PC profil 08 — Entry fragger",
    "PC profil 09 — Support", "PC profil 10 — Barqaror FPS",
    "PC profil 11 — Tez burilish", "PC profil 12 — Sekin aim",
    "PC profil 13 — AWM uchun", "PC profil 14 — AKR uchun",
    "PC profil 15 — M4 uchun", "PC profil 16 — Pistol round",
    "PC profil 17 — Yaqin masofa", "PC profil 18 — Uzoq masofa",
    "PC profil 19 — Universal aim", "PC profil 20 — Silliq kamera",
    "PC profil 21 — 400 DPI", "PC profil 22 — 600 DPI",
    "PC profil 23 — 800 DPI", "PC profil 24 — 1000 DPI",
    "PC profil 25 — 1200 DPI", "PC profil 26 — Low sens",
    "PC profil 27 — Mid sens", "PC profil 28 — High sens",
    "PC profil 29 — Headshot", "PC profil 30 — Spray nazorati",
    "PC profil 31 — Flick aim", "PC profil 32 — Tracking aim",
    "PC profil 33 — Entry uslubi", "PC profil 34 — Anchor uslubi",
    "PC profil 35 — Aggro rifler", "PC profil 36 — Passiv rifler",
    "PC profil 37 — Tez scope", "PC profil 38 — Barqaror scope",
    "PC profil 39 — Yengil sichqoncha", "PC profil 40 — Og'ir sichqoncha",
    "PC profil 41 — 60 FPS", "PC profil 42 — 90 FPS",
    "PC profil 43 — 120 FPS", "PC profil 44 — 144 FPS",
    "PC profil 45 — 165 FPS", "PC profil 46 — Kichik ekran",
    "PC profil 47 — Katta ekran", "PC profil 48 — Emulyator",
    "PC profil 49 — Turnir", "PC profil 50 — Shaxsiy balans",
]
PC_SECTION_COUNT = len(PC_DEFAULT_TITLES)
SEED_MODELS = {
    "redmi": ["Redmi Note 10S", "Redmi Note 11", "Redmi Note 11 Pro", "Redmi Note 12", "Redmi Note 12 Pro", "Redmi Note 13", "Redmi Note 13 Pro", "Redmi 12", "Redmi 13C"],
    "samsung": ["Samsung Galaxy A14", "Samsung Galaxy A15", "Samsung Galaxy A24", "Samsung Galaxy A25", "Samsung Galaxy A34", "Samsung Galaxy A35", "Samsung Galaxy A54", "Samsung Galaxy A55", "Samsung Galaxy S23", "Samsung Galaxy S24"],
    "honor": ["Honor X7b", "Honor X8b", "Honor X9b", "Honor 90 Lite", "Honor X9c"],
    "xiaomi": ["Xiaomi 12", "Xiaomi 12T", "Xiaomi 13", "Xiaomi 13T", "Xiaomi 14"],
    "poco": ["POCO X5", "POCO X5 Pro", "POCO X6", "POCO X6 Pro", "POCO F5"],
    "realme": ["Realme 10", "Realme 11", "Realme 12", "Realme C55"],
    "infinix": ["Infinix Note 30", "Infinix Note 40", "Infinix Hot 40", "Infinix GT 20 Pro"],
    "tecno": ["Tecno Spark 20", "Tecno Spark 20 Pro", "Tecno Pova 5", "Tecno Pova 6"],
    "oneplus": ["OnePlus Nord CE 3", "OnePlus Nord CE 4", "OnePlus 11", "OnePlus 12"],
}


class BotError(Exception):
    """Foydalanuvchi/adminga ko'rsatiladigan biznes-xato. Tranzaksiyani bekor qiladi."""


class PromoNotFound(BotError):
    """Promokod mavjud emas (urinishlarni cheklash uchun alohida tur)."""


# ============================================================================
# SXEMA
# ============================================================================
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        tg_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        tariff TEXT NOT NULL DEFAULT 'PLAYER',
        balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
        referrer_id BIGINT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS referrals (
        referred_id BIGINT PRIMARY KEY,
        referrer_id BIGINT NOT NULL,
        reward BIGINT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_id)",
    """CREATE TABLE IF NOT EXISTS transactions (
        id {pk},
        user_id BIGINT NOT NULL,
        amount BIGINT NOT NULL,
        balance_after BIGINT NOT NULL,
        kind TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions (user_id, id)",
    """CREATE TABLE IF NOT EXISTS bonus_claims (
        user_id BIGINT NOT NULL,
        tariff TEXT NOT NULL,
        amount BIGINT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, tariff)
    )""",
    """CREATE TABLE IF NOT EXISTS models (
        id {pk},
        brand TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (brand, name)
    )""",
    """CREATE TABLE IF NOT EXISTS phone_settings (
        model_id BIGINT PRIMARY KEY,
        device TEXT,
        sens TEXT,
        scope_sens TEXT,
        gyro_mode TEXT,
        gyro_sens TEXT,
        gyro_scope_sens TEXT,
        taken_at TEXT,
        profile_id TEXT,
        accelerator TEXT,
        small_holder TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pc_sections (
        num INTEGER PRIMARY KEY CHECK (num BETWEEN 1 AND 50),
        title TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY,
        coins BIGINT NOT NULL,
        max_uses BIGINT NOT NULL DEFAULT 0,
        used_count BIGINT NOT NULL DEFAULT 0,
        created_by BIGINT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS promo_uses (
        id {pk},
        user_id BIGINT NOT NULL,
        code TEXT NOT NULL,
        day TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (user_id, code),
        UNIQUE (user_id, day)
    )""",
    """CREATE TABLE IF NOT EXISTS admin_log (
        id {pk},
        admin_id BIGINT NOT NULL,
        action TEXT NOT NULL,
        details TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
]

PG_ADVISORY_KEY = 727272001  # bir vaqtda ikki nusxa sxema yaratib yubormasligi uchun
PG_PK = "BIGSERIAL PRIMARY KEY"
SQLITE_PK = "INTEGER PRIMARY KEY AUTOINCREMENT"


@lru_cache(maxsize=1024)
def to_pg(sql: str) -> str:
    """`?` placeholderlarni PostgreSQL uchun `$1, $2, ...` ga o'giradi."""
    counter = iter(range(1, 100_000))
    return re.sub(r"\?", lambda _m: "$" + str(next(counter)), sql)


# ============================================================================
# SO'ROV INTERFEYSI
# ============================================================================
class _Queryable:
    async def fetchall(self, sql: str, *args) -> list:
        raise NotImplementedError

    async def fetchone(self, sql: str, *args):
        rows = await self.fetchall(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args):
        row = await self.fetchone(sql, *args)
        return next(iter(row.values())) if row else None

    async def execute(self, sql: str, *args) -> None:
        await self.fetchall(sql, *args)


class _Tx(_Queryable):
    """Ochiq tranzaksiya ichida so'rov bajarish."""

    def __init__(self, runner):
        self._runner = runner

    async def fetchall(self, sql: str, *args) -> list:
        return await self._runner(sql, args)


# ============================================================================
# TANGA AMALLARI (tranzaksiya ichida)
# ============================================================================
async def _apply_coins(q, user_id: int, amount: int, kind: str, note: str = None) -> int:
    """Balansni atomik o'zgartiradi (manfiy bo'lib qolmaydi) va tranzaksiya yozadi."""
    row = await q.fetchone(
        "UPDATE users SET balance = balance + ? WHERE tg_id = ? AND balance + ? >= 0 RETURNING balance",
        amount, user_id, amount,
    )
    if row is None:
        raise BotError("Balans yetarli emas yoki foydalanuvchi topilmadi.")
    balance = row["balance"]
    await q.execute(
        "INSERT INTO transactions (user_id, amount, balance_after, kind, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        user_id, amount, balance, kind, note, now_str(),
    )
    return balance


async def _grant_bonus(q, user_id: int, tariff: str) -> int:
    """Tarif bonusini BIR MARTA beradi. (user_id, tariff) juftligi bonus_claims da saqlanadi,
    shuning uchun tarif qayta o'zgartirilsa ham bonus takroran berilmaydi. Berilgan miqdorni qaytaradi."""
    bonus = TARIFFS[tariff]["bonus"]
    if bonus <= 0:
        return 0
    row = await q.fetchone(
        "INSERT INTO bonus_claims (user_id, tariff, amount, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, tariff) DO NOTHING RETURNING user_id",
        user_id, tariff, bonus, now_str(),
    )
    if row is None:
        return 0
    await _apply_coins(q, user_id, bonus, "bonus", f"{tariff} tarifi bonusi")
    return bonus


async def _insert_model(q, brand: str, name: str):
    row = await q.fetchone(
        "INSERT INTO models (brand, name, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT (brand, name) DO NOTHING RETURNING id",
        brand, name, now_str(),
    )
    if row is None:
        return None
    await q.execute(
        "INSERT INTO phone_settings (model_id, device, updated_at) VALUES (?, ?, ?)",
        row["id"], name, now_str(),
    )
    return row["id"]


# ============================================================================
# DATABASE
# ============================================================================
class Database(_Queryable):
    def __init__(self, database_url: str = None, db_path: str = "data/bot.db", welcome_bonus: bool = False):
        self.database_url = database_url or None
        self.db_path = db_path
        self.welcome_bonus = welcome_bonus
        self._pool = None
        self._conn = None
        self._lock = None
        self._transient = ()  # PostgreSQL uzilish xatolari (connect() da to'ldiriladi)

    @property
    def backend(self) -> str:
        return "postgresql" if self.database_url else "sqlite"

    # ---------- ulanish ----------
    async def connect(self) -> None:
        if self.database_url:
            import asyncpg  # faqat PostgreSQL rejimida kerak

            # Kichik pool - bepul hosting (512 MB RAM) va bepul PostgreSQL ulanish limitlari uchun.
            # statement_cache_size=0 - PgBouncer (Neon/Supabase "pooler") bilan mos ishlashi uchun.
            # max_inactive_connection_lifetime - bepul PostgreSQL uxlab qolganda eskirgan ulanish qolmasin.
            self._pool = await asyncpg.create_pool(
                self.database_url, min_size=1, max_size=3, command_timeout=30,
                statement_cache_size=0, max_inactive_connection_lifetime=120,
            )
            self._transient = (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError)
            log.info("PostgreSQL'ga ulandi")
        else:
            self._lock = asyncio.Lock()
            self._conn = await asyncio.to_thread(self._open_sqlite)
            log.info("SQLite ochildi: %s", os.path.abspath(self.db_path))
        await self._init_schema()

    def _open_sqlite(self):
        if sqlite3.sqlite_version_info < (3, 35, 0):
            raise RuntimeError("SQLite 3.35+ kerak (RETURNING qo'llab-quvvatlanishi uchun). "
                               f"Hozirgi versiya: {sqlite3.sqlite_version}")
        folder = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(folder, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    # ---------- past darajali so'rovlar ----------
    def _sqlite_run(self, sql: str, args) -> list:
        cur = self._conn.execute(sql, args)
        try:
            return [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()

    @staticmethod
    def _pg_runner(conn):
        async def run(sql, args):
            return [dict(r) for r in await conn.fetch(to_pg(sql), *args)]

        return run

    async def fetchall(self, sql: str, *args) -> list:
        if self._pool is not None:
            for attempt in (1, 2):
                try:
                    return [dict(r) for r in await self._pool.fetch(to_pg(sql), *args)]
                except self._transient as exc:  # ulanish uzilgan (masalan bepul baza uxlab qolgan) - bir marta qayta urinamiz
                    if attempt == 2:
                        raise
                    log.warning("PostgreSQL ulanishi uzilgan (%s), qayta uriniladi", exc)
        async with self._lock:
            return await asyncio.to_thread(self._sqlite_run, sql, args)

    async def _ddl(self, sql: str) -> None:  # faqat SQLite
        async with self._lock:
            await asyncio.to_thread(self._sqlite_run, sql, ())

    @asynccontextmanager
    async def transaction(self):
        """`async with db.transaction() as q:` - ichida q.fetchone/fetchall/execute.
        Xatoda (jumladan BotError) hamma narsa bekor qilinadi."""
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    yield _Tx(self._pg_runner(conn))
            return

        async with self._lock:
            async def run_sqlite(sql, args):
                return await asyncio.to_thread(self._sqlite_run, sql, args)

            await run_sqlite("BEGIN IMMEDIATE", ())
            try:
                yield _Tx(run_sqlite)
            except BaseException:
                with suppress(Exception):
                    await asyncio.shield(run_sqlite("ROLLBACK", ()))
                raise
            else:
                try:
                    await run_sqlite("COMMIT", ())
                except BaseException:
                    with suppress(Exception):
                        await asyncio.shield(run_sqlite("ROLLBACK", ()))
                    raise

    async def _init_schema(self) -> None:
        """Sxemani yaratadi va boshlang'ich ma'lumotni qo'shadi (har ishga tushganda xavfsiz)."""
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Tranzaksiya darajasidagi qulf: ikki nusxa bir vaqtda sxema yaratmasin
                    # (PgBouncer transaction rejimida ham xavfsiz - hammasi bitta tranzaksiyada).
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", PG_ADVISORY_KEY)
                    for stmt in SCHEMA:
                        await conn.execute(stmt.replace("{pk}", PG_PK))
                    await self._seed(_Tx(self._pg_runner(conn)))
        else:
            for stmt in SCHEMA:
                await self._ddl(stmt.replace("{pk}", SQLITE_PK))
            async with self.transaction() as q:
                await self._seed(q)

    async def _seed(self, q) -> None:
        for num, title in enumerate(PC_DEFAULT_TITLES, start=1):
            content = (
                f"🆔 Profil ID: PC-{num:02d}\n"
                f"🖱️ DPI: {400 + (num % 7) * 100}\n"
                f"🎯 Sensitivity: {0.8 + (num % 13) * 0.17:.2f}\n"
                f"🔭 Scope sensitivity: {0.7 + (num % 9) * 0.19:.2f}\n"
                "⌨️ Boshqaruv: standart, o'zingizga moslab sozlang.\n\n"
                "ℹ️ Namunaviy profil: o'yin va emulyator versiyasiga qarab sinab, moslashtiring."
            )
            await q.execute(
                "INSERT INTO pc_sections (num, title, content) VALUES (?, ?, ?) "
                "ON CONFLICT (num) DO NOTHING", num, title, content)
        done = await q.fetchone("SELECT value FROM meta WHERE name = 'models_seeded'")
        # Har ishga tushishda modellarni xavfsiz qo'shish: mavjud ma'lumotlar saqlanadi.
        for brand, names in SEED_MODELS.items():
            for name in names:
                model_id = await _insert_model(q, brand, name)
                if model_id is None:
                    existing = await q.fetchone(
                        "SELECT id FROM models WHERE brand = ? AND name = ?", brand, name)
                    model_id = existing["id"] if existing else None
                if model_id is not None:
                    idx = sum(ord(ch) for ch in name)
                    await q.execute(
                        "UPDATE phone_settings SET device = ?, sens = COALESCE(sens, ?), "
                        "scope_sens = COALESCE(scope_sens, ?), gyro_mode = COALESCE(gyro_mode, ?), "
                        "gyro_sens = COALESCE(gyro_sens, ?), gyro_scope_sens = COALESCE(gyro_scope_sens, ?), "
                        "profile_id = COALESCE(profile_id, ?) WHERE model_id = ?",
                        name, str(1.0 + (idx % 31) / 10), str(0.8 + (idx % 19) / 10),
                        GYRO_MODES[idx % len(GYRO_MODES)], str(1.1 + (idx % 27) / 10),
                        str(0.9 + (idx % 23) / 10), f"AND-{idx:04d}", model_id)
        await q.execute("INSERT INTO meta (name, value) VALUES ('models_seeded', '1') "
                        "ON CONFLICT (name) DO NOTHING")
        log.info("Telefon modellari va PC profillari tekshirildi/yangilandi")

    # ========================================================================
    # FOYDALANUVCHILAR
    # ========================================================================
    async def get_user(self, tg_id: int):
        return await self.fetchone("SELECT * FROM users WHERE tg_id = ?", tg_id)

    async def touch_user(self, tg_id: int, username, first_name):
        """Foydalanuvchini yaratadi (yangi bo'lsa PLAYER) yoki username/ismini yangilaydi.
        (user, created) qaytaradi."""
        user = await self.fetchone("SELECT * FROM users WHERE tg_id = ?", tg_id)
        if user is not None:
            if user["username"] != username or user["first_name"] != first_name:
                user = await self.fetchone(
                    "UPDATE users SET username = ?, first_name = ? WHERE tg_id = ? RETURNING *",
                    username, first_name, tg_id)
            return user, False
        async with self.transaction() as q:
            row = await q.fetchone(
                "INSERT INTO users (tg_id, username, first_name, tariff, balance, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT (tg_id) DO NOTHING RETURNING tg_id",
                tg_id, username, first_name, DEFAULT_TARIFF, now_str())
            created = row is not None
            if created and self.welcome_bonus:
                await _grant_bonus(q, tg_id, DEFAULT_TARIFF)
            user = await q.fetchone("SELECT * FROM users WHERE tg_id = ?", tg_id)
        return user, created

    async def referral_stats(self, tg_id: int):
        row = await self.fetchone(
            "SELECT COUNT(*) AS cnt, CAST(COALESCE(SUM(reward), 0) AS BIGINT) AS earned "
            "FROM referrals WHERE referrer_id = ?", tg_id)
        return int(row["cnt"]), int(row["earned"])

    async def apply_referral(self, new_user_id: int, referrer_id: int):
        """Yangi foydalanuvchi uchun referal mukofotini beradi.
        Himoya: o'zini taklif qilish yo'q; referrer mavjud bo'lishi shart;
        referred_id PRIMARY KEY - bir foydalanuvchi uchun mukofot faqat BIR marta.
        Chaqiruvchi faqat yangi yaratilgan foydalanuvchi uchun chaqirishi kerak."""
        if new_user_id == referrer_id:
            return None
        async with self.transaction() as q:
            ref = await q.fetchone("SELECT tg_id, tariff FROM users WHERE tg_id = ?", referrer_id)
            if ref is None:
                return None
            reward = TARIFFS.get(ref["tariff"], TARIFFS[DEFAULT_TARIFF])["referral"]
            ins = await q.fetchone(
                "INSERT INTO referrals (referred_id, referrer_id, reward, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (referred_id) DO NOTHING RETURNING referred_id",
                new_user_id, referrer_id, reward, now_str())
            if ins is None:
                return None
            await q.execute("UPDATE users SET referrer_id = ? WHERE tg_id = ? AND referrer_id IS NULL",
                            referrer_id, new_user_id)
            balance = await _apply_coins(q, referrer_id, reward, "referral", f"Yangi foydalanuvchi: {new_user_id}")
        return {"referrer_id": referrer_id, "reward": reward, "balance": balance}

    async def list_transactions(self, user_id: int, page: int, per_page: int):
        total = int(await self.fetchval("SELECT COUNT(*) FROM transactions WHERE user_id = ?", user_id))
        page = max(0, min(page, max(0, (total - 1) // per_page)))
        rows = await self.fetchall(
            "SELECT amount, balance_after, kind, note, created_at FROM transactions "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?", user_id, per_page, page * per_page)
        return rows, total, page

    async def set_tariff(self, user_id: int, tariff: str):
        """Tarifni o'zgartiradi. Tarif bonusi (bo'lsa) faqat birinchi faollashtirishda beriladi."""
        if tariff not in TARIFFS:
            raise BotError("Noto'g'ri tarif.")
        async with self.transaction() as q:
            user = await q.fetchone("SELECT tg_id, tariff FROM users WHERE tg_id = ?", user_id)
            if user is None:
                raise BotError("Foydalanuvchi topilmadi.")
            await q.execute("UPDATE users SET tariff = ? WHERE tg_id = ?", tariff, user_id)
            bonus = await _grant_bonus(q, user_id, tariff)
            balance = await q.fetchval("SELECT balance FROM users WHERE tg_id = ?", user_id)
        return {"old": user["tariff"], "new": tariff, "bonus": bonus, "balance": balance}

    async def bonus_info(self, user_id: int, tariff: str) -> str:
        """Admin tasdiqlash oynasi uchun: 'none' | 'available' | 'claimed'."""
        if TARIFFS[tariff]["bonus"] <= 0:
            return "none"
        row = await self.fetchone("SELECT 1 AS x FROM bonus_claims WHERE user_id = ? AND tariff = ?",
                                  user_id, tariff)
        return "claimed" if row else "available"

    async def adjust_coins(self, user_id: int, amount: int, admin_id: int) -> int:
        """Admin tomonidan tanga qo'shish (amount>0) yoki ayirish (amount<0). Yangi balansni qaytaradi."""
        async with self.transaction() as q:
            user = await q.fetchone("SELECT tg_id, balance FROM users WHERE tg_id = ?", user_id)
            if user is None:
                raise BotError("Foydalanuvchi topilmadi.")
            if amount < 0 and user["balance"] + amount < 0:
                raise BotError(f"Balans yetarli emas (hozir: {user['balance']}).")
            kind = "admin_add" if amount > 0 else "admin_sub"
            return await _apply_coins(q, user_id, amount, kind, f"Admin {admin_id}")

    async def all_user_ids(self) -> list:
        return [r["tg_id"] for r in await self.fetchall("SELECT tg_id FROM users ORDER BY tg_id")]

    async def stats(self) -> dict:
        row = await self.fetchone(
            "SELECT "
            "(SELECT COUNT(*) FROM users) AS total, "
            "(SELECT COUNT(*) FROM users WHERE created_at >= ?) AS today, "
            "(SELECT COUNT(*) FROM users WHERE created_at >= ?) AS week, "
            "(SELECT CAST(COALESCE(SUM(balance), 0) AS BIGINT) FROM users) AS coins, "
            "(SELECT COUNT(*) FROM referrals) AS referrals, "
            "(SELECT COUNT(*) FROM promocodes) AS promos, "
            "(SELECT COUNT(*) FROM models) AS models",
            today_str() + " 00:00:00", days_ago_str(7))
        by_tariff = {t: 0 for t in TARIFF_ORDER}
        for r in await self.fetchall("SELECT tariff, COUNT(*) AS cnt FROM users GROUP BY tariff"):
            by_tariff[r["tariff"]] = int(r["cnt"])
        result = {k: int(v) for k, v in row.items()}
        result["by_tariff"] = by_tariff
        return result

    async def audit(self, admin_id: int, action: str, details: str = "") -> None:
        log.info("ADMIN %s: %s %s", admin_id, action, details)
        await self.execute(
            "INSERT INTO admin_log (admin_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            admin_id, action, details[:500], now_str())

    # ========================================================================
    # PROMOKODLAR
    # ========================================================================
    async def create_promo(self, code: str, coins: int, max_uses: int, admin_id: int) -> None:
        row = await self.fetchone(
            "INSERT INTO promocodes (code, coins, max_uses, used_count, created_by, created_at) "
            "VALUES (?, ?, ?, 0, ?, ?) ON CONFLICT (code) DO NOTHING RETURNING code",
            code, coins, max_uses, admin_id, now_str())
        if row is None:
            raise BotError("Bunday promokod allaqachon mavjud.")

    async def delete_promo(self, code: str) -> None:
        async with self.transaction() as q:
            row = await q.fetchone("DELETE FROM promocodes WHERE code = ? RETURNING code", code)
            if row is None:
                raise BotError("Promokod topilmadi.")
            await q.execute("DELETE FROM promo_uses WHERE code = ?", code)

    async def list_promos(self) -> list:
        return await self.fetchall(
            "SELECT code, coins, max_uses, used_count FROM promocodes ORDER BY created_at DESC, code LIMIT 50")

    async def redeem_promo(self, user_id: int, code: str) -> dict:
        """Qoidalar: (1) kod mavjud bo'lishi; (2) bitta kodni bir foydalanuvchi faqat BIR marta;
        (3) bitta foydalanuvchi kuniga (Toshkent vaqti) faqat BITTA promokod; (4) umumiy limit."""
        today = today_str()
        async with self.transaction() as q:
            promo = await q.fetchone("SELECT code, coins FROM promocodes WHERE code = ?", code)
            if promo is None:
                raise PromoNotFound("Bunday promokod mavjud emas.")
            if await q.fetchone("SELECT 1 AS x FROM promo_uses WHERE user_id = ? AND code = ?", user_id, code):
                raise BotError("Siz bu promokodni allaqachon ishlatgansiz.")
            if await q.fetchone("SELECT 1 AS x FROM promo_uses WHERE user_id = ? AND day = ?", user_id, today):
                raise BotError("Bugun promokod ishlatib bo'lgansiz. Kuniga faqat bitta promokod ishlatiladi - ertaga urinib ko'ring.")
            upd = await q.fetchone(
                "UPDATE promocodes SET used_count = used_count + 1 "
                "WHERE code = ? AND (max_uses = 0 OR used_count < max_uses) RETURNING coins", code)
            if upd is None:
                raise BotError("Bu promokodning foydalanish limiti tugagan.")
            ins = await q.fetchone(
                "INSERT INTO promo_uses (user_id, code, day, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING RETURNING id", user_id, code, today, now_str())
            if ins is None:
                raise BotError("Promokod allaqachon ishlatilgan yoki bugun limit tugagan.")
            balance = await _apply_coins(q, user_id, upd["coins"], "promo", f"Promokod: {code}")
        return {"coins": upd["coins"], "balance": balance}

    # ========================================================================
    # MODELLAR VA TELEFON SOZLAMALARI
    # ========================================================================
    async def list_models(self, brand: str, page: int, per_page: int):
        """(qatorlar, jami, haqiqiy_sahifa) qaytaradi."""
        total = int(await self.fetchval("SELECT COUNT(*) FROM models WHERE brand = ?", brand))
        page = max(0, min(page, max(0, (total - 1) // per_page)))
        rows = await self.fetchall(
            "SELECT id, name FROM models WHERE brand = ? ORDER BY id LIMIT ? OFFSET ?",
            brand, per_page, page * per_page)
        return rows, total, page

    async def get_model(self, model_id: int):
        cols = ", ".join("s." + c for c in PHONE_COLUMNS)
        return await self.fetchone(
            f"SELECT m.id, m.brand, m.name, {cols} FROM models m "
            "LEFT JOIN phone_settings s ON s.model_id = m.id WHERE m.id = ?", model_id)

    async def add_model(self, brand: str, name: str) -> int:
        if brand not in BRANDS:
            raise BotError("Noto'g'ri brend.")
        async with self.transaction() as q:
            model_id = await _insert_model(q, brand, name)
            if model_id is None:
                raise BotError("Bunday model allaqachon mavjud.")
        return model_id

    async def rename_model(self, model_id: int, name: str) -> None:
        async with self.transaction() as q:
            m = await q.fetchone("SELECT id, brand FROM models WHERE id = ?", model_id)
            if m is None:
                raise BotError("Model topilmadi.")
            dup = await q.fetchone("SELECT id FROM models WHERE brand = ? AND name = ? AND id <> ?",
                                   m["brand"], name, model_id)
            if dup:
                raise BotError("Bu brendda bunday nomli model allaqachon mavjud.")
            await q.execute("UPDATE models SET name = ? WHERE id = ?", name, model_id)

    async def delete_model(self, model_id: int) -> None:
        async with self.transaction() as q:
            row = await q.fetchone("DELETE FROM models WHERE id = ? RETURNING id", model_id)
            if row is None:
                raise BotError("Model topilmadi.")
            await q.execute("DELETE FROM phone_settings WHERE model_id = ?", model_id)

    async def update_setting(self, model_id: int, column: str, value) -> None:
        """Bitta sozlama maydonini yangilaydi. `column` faqat PHONE_COLUMNS dan bo'lishi mumkin.
        OLINGAN VAQT bo'sh bo'lsa, birinchi qiymat kiritilganda avtomatik hozirgi vaqt qo'yiladi."""
        if column not in PHONE_COLUMNS:
            raise ValueError("Noma'lum maydon")
        async with self.transaction() as q:
            if await q.fetchone("SELECT id FROM models WHERE id = ?", model_id) is None:
                raise BotError("Model topilmadi.")
            await q.execute("INSERT INTO phone_settings (model_id) VALUES (?) ON CONFLICT (model_id) DO NOTHING",
                            model_id)
            now = now_str()
            sets, params = f"{column} = ?, updated_at = ?", [value, now]
            if column != "taken_at" and value is not None:
                sets += ", taken_at = COALESCE(taken_at, ?)"
                params.append(now)
            await q.execute(f"UPDATE phone_settings SET {sets} WHERE model_id = ?", *params, model_id)

    # ========================================================================
    # PC BO'LIMLARI (aynan 10 ta)
    # ========================================================================
    async def list_pc_sections(self) -> list:
        return await self.fetchall("SELECT num, title FROM pc_sections ORDER BY num")

    async def get_pc_section(self, num: int):
        return await self.fetchone("SELECT num, title, content FROM pc_sections WHERE num = ?", num)

    async def update_pc_section(self, num: int, title: str = None, content: str = None) -> None:
        if title is None and content is None:
            return
        async with self.transaction() as q:
            if await q.fetchone("SELECT num FROM pc_sections WHERE num = ?", num) is None:
                raise BotError("Bo'lim topilmadi.")
            if title is not None:
                await q.execute("UPDATE pc_sections SET title = ? WHERE num = ?", title, num)
            if content is not None:
                await q.execute("UPDATE pc_sections SET content = ? WHERE num = ?", content, num)
