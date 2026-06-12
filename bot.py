import asyncio
import html
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
STORE_NAME = os.getenv("STORE_NAME", "LORD STORE")
BOT_USERNAME = os.getenv("BOT_USERNAME", "LORDSTOREAIBOT")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@YourSupportUsername")
VODAFONE = os.getenv("VODAFONE_CASH_NUMBER", "01070417791")
VODAFONE_NAME = os.getenv("VODAFONE_CASH_NAME", "محمد ع")
BINANCE = os.getenv("BINANCE_PAY_ID", "")
USD_TO_EGP = float(os.getenv("USD_TO_EGP", "50") or 50)
VND_TO_EGP = float(os.getenv("VND_TO_EGP", "0.002") or 0.002)
DB_PATH = os.getenv("DB_PATH", "lord_store.db")

router = Router()


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def now() -> str:
    return datetime.utcnow().isoformat()


def h(value) -> str:
    return html.escape(str(value or ""), quote=False)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def fmt_money(amount_egp: float, currency: str) -> str:
    amount_egp = float(amount_egp)
    if currency == "USD":
        return f"${amount_egp / USD_TO_EGP:.2f}"
    if currency == "VND":
        return f"{amount_egp / VND_TO_EGP:.0f} VND"
    return f"{amount_egp:.2f} EGP"


def to_egp(amount: float, currency: str) -> float:
    if currency == "USD":
        return round(float(amount) * USD_TO_EGP, 2)
    if currency == "VND":
        return round(float(amount) * VND_TO_EGP, 2)
    return round(float(amount), 2)


def init_db():
    with db_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'ar',
                currency TEXT DEFAULT 'EGP',
                balance REAL DEFAULT 0,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_ar TEXT NOT NULL,
                name_en TEXT NOT NULL,
                emoji TEXT DEFAULT '🛍',
                sort_order INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                title_ar TEXT NOT NULL,
                title_en TEXT NOT NULL,
                desc_ar TEXT NOT NULL,
                desc_en TEXT NOT NULL,
                price REAL NOT NULL,
                duration TEXT DEFAULT '',
                warranty TEXT DEFAULT '',
                delivery_method TEXT DEFAULT '',
                stock INTEGER DEFAULT 0,
                delivery_text TEXT DEFAULT '',
                delivery_mode TEXT DEFAULT 'manual',
                allow_preorder INTEGER DEFAULT 1,
                active INTEGER DEFAULT 1,
                created_at TEXT,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS stock_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                sold INTEGER DEFAULT 0,
                sold_to INTEGER,
                order_id INTEGER,
                created_at TEXT,
                sold_at TEXT,
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS topups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                amount_input REAL NOT NULL,
                amount_egp REAL NOT NULL,
                currency TEXT NOT NULL,
                proof_text TEXT,
                proof_file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                price REAL NOT NULL,
                status TEXT DEFAULT 'processing',
                delivery_content TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id, active, id);
            CREATE INDEX IF NOT EXISTS idx_stock_unsold ON stock_items(product_id, sold, id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, product_id, id);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_topups_status ON topups(status, id);
            """
        )
        for sql in [
            "ALTER TABLE products ADD COLUMN category_id INTEGER",
            "ALTER TABLE products ADD COLUMN allow_preorder INTEGER DEFAULT 1",
            "ALTER TABLE products ADD COLUMN delivery_mode TEXT DEFAULT 'manual'",
            "ALTER TABLE orders ADD COLUMN delivery_content TEXT",
            "ALTER TABLE orders ADD COLUMN updated_at TEXT",
            "ALTER TABLE topups ADD COLUMN updated_at TEXT",
        ]:
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                pass
        seed_if_empty(c)
        general_id = get_or_create_category_sql(c, "General", "General", "🛍")
        c.execute("UPDATE products SET category_id=? WHERE category_id IS NULL", (general_id,))


def get_or_create_category_sql(c, name_ar: str, name_en: str, emoji: str = "🛍") -> int:
    row = c.execute("SELECT id FROM categories WHERE name_ar=? OR name_en=?", (name_ar, name_en)).fetchone()
    if row:
        return int(row["id"])
    cur = c.execute(
        "INSERT INTO categories(name_ar,name_en,emoji,sort_order,active,created_at) VALUES(?,?,?,?,1,?)",
        (name_ar, name_en, emoji, 0, now()),
    )
    return int(cur.lastrowid)


def seed_if_empty(c):
    cat_count = c.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"]
    if not cat_count:
        defaults = [
            ("ChatGPT", "ChatGPT", "🤖", 10),
            ("Gemini", "Gemini", "1️⃣", 20),
            ("Claude", "Claude", "🌟", 30),
            ("Outlook", "Outlook", "📧", 40),
            ("Adobe", "Adobe", "🎨", 50),
            ("YouTube", "YouTube", "▶️", 60),
            ("Canva", "Canva", "🖼", 70),
            ("VPN", "VPN", "🛡", 80),
        ]
        c.executemany(
            "INSERT INTO categories(name_ar,name_en,emoji,sort_order,active,created_at) VALUES(?,?,?,?,1,?)",
            [(a, e, em, so, now()) for a, e, em, so in defaults],
        )
    product_count = c.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    if product_count:
        return
    chatgpt = get_or_create_category_sql(c, "ChatGPT", "ChatGPT", "🤖")
    gemini = get_or_create_category_sql(c, "Gemini", "Gemini", "1️⃣")
    outlook = get_or_create_category_sql(c, "Outlook", "Outlook", "📧")
    seed = [
        (chatgpt, "ChatGPT Plus 1 Month", "ChatGPT Plus 1 Month", "📌 تفاصيل الاشتراك:\n📦 خدمة اشتراك رقمية يتم تسليمها فورًا عند توفر المخزون.", "Digital subscription service with instant delivery when stock is available.", 355, "شهر", "25 يوم", "Auto ⚡ Instant delivery", 0, "auto"),
        (gemini, "Gemini Advanced 18 Months", "Gemini Advanced 18 Months", "Link / activation service.", "Link / activation service.", 455, "18 شهر", "كامل المدة", "Auto / Manual", 0, "auto"),
        (outlook, "Outlook Ready Account", "Outlook Ready Account", "خدمة بريد إلكتروني رقمية.", "Digital email service.", 4, "مدى الحياة حسب الخدمة", "بدون", "Auto ⚡ Instant delivery", 0, "auto"),
    ]
    c.executemany(
        """
        INSERT INTO products(category_id,title_ar,title_en,desc_ar,desc_en,price,duration,warranty,delivery_method,stock,delivery_text,delivery_mode,allow_preorder,active,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'Auto delivery from stock',?,1,1,?)
        """,
        [(cid, ar, en, dar, den, price, dur, war, method, stock, mode, now()) for cid, ar, en, dar, den, price, dur, war, method, stock, mode in seed],
    )


def ensure_user(user_id: int):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            c.execute("INSERT INTO users(id,created_at) VALUES(?,?)", (user_id, now()))
        c.execute("COMMIT")


def get_user(user_id: int):
    ensure_user(user_id)
    with db_conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def set_user_field(user_id: int, field: str, value: str):
    if field not in {"lang", "currency"}:
        raise ValueError("invalid user field")
    ensure_user(user_id)
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, user_id))
        c.execute("COMMIT")


def get_categories(active_only=True):
    query = """
        SELECT c.*,
               COUNT(p.id) AS product_count,
               COALESCE(SUM(CASE WHEN p.active=1 THEN p.stock ELSE 0 END),0) AS stock_total
        FROM categories c
        LEFT JOIN products p ON p.category_id=c.id AND p.active=1
    """
    params = []
    if active_only:
        query += " WHERE c.active=1"
    query += " GROUP BY c.id ORDER BY c.sort_order ASC, c.id ASC"
    with db_conn() as c:
        return c.execute(query, params).fetchall()


def get_products_by_category(category_id: int):
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM products WHERE active=1 AND category_id=? ORDER BY id ASC",
            (category_id,),
        ).fetchall()


def get_product(product_id: int):
    with db_conn() as c:
        return c.execute(
            """
            SELECT p.*, c.name_ar AS category_ar, c.name_en AS category_en, c.emoji AS category_emoji
            FROM products p
            LEFT JOIN categories c ON c.id=p.category_id
            WHERE p.id=?
            """,
            (product_id,),
        ).fetchone()


def add_balance(user_id: int, amount_egp: float):
    ensure_user(user_id)
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(amount_egp), user_id))
        c.execute("COMMIT")


def sync_stock(product_id: int) -> int:
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        product = c.execute("SELECT delivery_mode FROM products WHERE id=?", (product_id,)).fetchone()
        if product and (product["delivery_mode"] or "manual") == "auto":
            count = c.execute(
                "SELECT COUNT(*) AS n FROM stock_items WHERE product_id=? AND sold=0",
                (product_id,),
            ).fetchone()["n"]
            c.execute("UPDATE products SET stock=? WHERE id=?", (count, product_id))
        else:
            row = c.execute("SELECT stock FROM products WHERE id=?", (product_id,)).fetchone()
            count = int(row["stock"]) if row else 0
        c.execute("COMMIT")
        return count


def add_category(name_ar: str, name_en: str, emoji: str) -> int:
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cid = get_or_create_category_sql(c, name_ar, name_en, emoji)
        c.execute("COMMIT")
        return cid


def add_auto_product(category_id: int, title: str, price: float, duration: str, warranty: str, delivery_method: str, desc: str, allow_preorder: int = 1) -> int:
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            """
            INSERT INTO products(category_id,title_ar,title_en,desc_ar,desc_en,price,duration,warranty,delivery_method,stock,delivery_text,delivery_mode,allow_preorder,active,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,0,?,'auto',?,1,?)
            """,
            (category_id, title, title, desc, desc, float(price), duration, warranty, delivery_method, "Auto delivery from stock", int(allow_preorder), now()),
        )
        product_id = int(cur.lastrowid)
        c.execute("COMMIT")
        return product_id


def add_stock_lines(product_id: int, lines: list[str]) -> int:
    clean = [line.strip() for line in lines if line.strip()]
    if not clean:
        return 0
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        product = c.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            c.execute("ROLLBACK")
            raise ValueError("Product not found")
        c.executemany(
            "INSERT INTO stock_items(product_id,content,sold,created_at) VALUES(?,?,0,?)",
            [(product_id, line, now()) for line in clean],
        )
        count = c.execute(
            "SELECT COUNT(*) AS n FROM stock_items WHERE product_id=? AND sold=0",
            (product_id,),
        ).fetchone()["n"]
        c.execute("UPDATE products SET delivery_mode='auto', stock=? WHERE id=?", (count, product_id))
        c.execute("COMMIT")
    return len(clean)


def purchase_product(user_id: int, product_id: int, preorder: bool = False):
    ensure_user(user_id)
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        user = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        product = c.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not product:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        if float(user["balance"]) < float(product["price"]):
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "no_balance", "need": float(product["price"]) - float(user["balance"])}
        mode = product["delivery_mode"] or "manual"
        allow_wait = bool(product["allow_preorder"]) or preorder
        delivery_content = None
        stock_item_id = None
        status = "processing"

        if mode == "auto":
            item = c.execute(
                "SELECT * FROM stock_items WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 1",
                (product_id,),
            ).fetchone()
            if item:
                delivery_content = item["content"]
                stock_item_id = item["id"]
                status = "delivered"
            elif allow_wait:
                status = "waiting_stock"
            else:
                c.execute("ROLLBACK")
                return {"ok": False, "reason": "out_stock"}
        else:
            if int(product["stock"]) > 0:
                c.execute("UPDATE products SET stock=MAX(stock-1,0) WHERE id=?", (product_id,))
                status = "processing"
            elif allow_wait:
                status = "waiting_stock"
            else:
                c.execute("ROLLBACK")
                return {"ok": False, "reason": "out_stock"}

        c.execute("UPDATE users SET balance=balance-? WHERE id=?", (float(product["price"]), user_id))
        cur = c.execute(
            """
            INSERT INTO orders(user_id,product_id,title,price,status,delivery_content,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (user_id, product_id, product["title_ar"], product["price"], status, delivery_content, now(), now()),
        )
        order_id = int(cur.lastrowid)

        if mode == "auto" and stock_item_id:
            c.execute(
                "UPDATE stock_items SET sold=1,sold_to=?,order_id=?,sold_at=? WHERE id=?",
                (user_id, order_id, now(), stock_item_id),
            )
            count = c.execute(
                "SELECT COUNT(*) AS n FROM stock_items WHERE product_id=? AND sold=0",
                (product_id,),
            ).fetchone()["n"]
            c.execute("UPDATE products SET stock=? WHERE id=?", (count, product_id))

        c.execute("COMMIT")
        return {
            "ok": True,
            "order_id": order_id,
            "mode": mode,
            "status": status,
            "product": dict(product),
            "delivery_content": delivery_content,
        }


async def fulfill_waiting_orders(bot: Bot, product_id: int):
    delivered = 0
    while True:
        with db_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            product = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if not product or (product["delivery_mode"] or "manual") != "auto":
                c.execute("ROLLBACK")
                return delivered
            order = c.execute(
                "SELECT * FROM orders WHERE product_id=? AND status='waiting_stock' ORDER BY id ASC LIMIT 1",
                (product_id,),
            ).fetchone()
            item = c.execute(
                "SELECT * FROM stock_items WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 1",
                (product_id,),
            ).fetchone()
            if not order or not item:
                c.execute("ROLLBACK")
                return delivered
            c.execute(
                "UPDATE stock_items SET sold=1,sold_to=?,order_id=?,sold_at=? WHERE id=?",
                (order["user_id"], order["id"], now(), item["id"]),
            )
            c.execute(
                "UPDATE orders SET status='delivered', delivery_content=?, updated_at=? WHERE id=?",
                (item["content"], now(), order["id"]),
            )
            count = c.execute(
                "SELECT COUNT(*) AS n FROM stock_items WHERE product_id=? AND sold=0",
                (product_id,),
            ).fetchone()["n"]
            c.execute("UPDATE products SET stock=? WHERE id=?", (count, product_id))
            c.execute("COMMIT")
            user_id = int(order["user_id"])
            order_id = int(order["id"])
            content = str(item["content"])
        try:
            await bot.send_message(
                user_id,
                f"✅ تم توفير المخزون وتسليم طلبك رقم #{order_id}\n\n<code>{h(content)}</code>",
            )
        except Exception:
            pass
        delivered += 1


def create_topup(user_id: int, method: str, amount_input: float, amount_egp: float, currency: str, proof_text: str, proof_file_id: Optional[str]) -> int:
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            """
            INSERT INTO topups(user_id,method,amount_input,amount_egp,currency,proof_text,proof_file_id,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'pending',?,?)
            """,
            (user_id, method, amount_input, amount_egp, currency, proof_text, proof_file_id, now(), now()),
        )
        topup_id = int(cur.lastrowid)
        c.execute("COMMIT")
        return topup_id


def approve_topup(topup_id: int):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM topups WHERE id=?", (topup_id,)).fetchone()
        if not row or row["status"] != "pending":
            c.execute("ROLLBACK")
            return None
        c.execute("UPDATE topups SET status='approved', updated_at=? WHERE id=?", (now(), topup_id))
        c.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(row["amount_egp"]), row["user_id"]))
        c.execute("COMMIT")
        return dict(row)


def reject_topup(topup_id: int):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM topups WHERE id=?", (topup_id,)).fetchone()
        if not row or row["status"] != "pending":
            c.execute("ROLLBACK")
            return None
        c.execute("UPDATE topups SET status='rejected', updated_at=? WHERE id=?", (now(), topup_id))
        c.execute("COMMIT")
        return dict(row)


def deliver_order(order_id: int, delivery_text: Optional[str] = None):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        order = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] not in {"processing", "waiting_stock"}:
            c.execute("ROLLBACK")
            return None
        product = c.execute("SELECT * FROM products WHERE id=?", (order["product_id"],)).fetchone()
        content = delivery_text or (product["delivery_text"] if product else "تواصل مع الدعم للتسليم.")
        c.execute(
            "UPDATE orders SET status='delivered', delivery_content=?, updated_at=? WHERE id=?",
            (content, now(), order_id),
        )
        c.execute("COMMIT")
        data = dict(order)
        data["delivery_content"] = content
        return data


def refund_order(order_id: int):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        order = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] not in {"processing", "waiting_stock"}:
            c.execute("ROLLBACK")
            return None
        c.execute("UPDATE orders SET status='rejected', updated_at=? WHERE id=?", (now(), order_id))
        c.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(order["price"]), order["user_id"]))
        if order["status"] == "processing":
            c.execute("UPDATE products SET stock=stock+1 WHERE id=?", (order["product_id"],))
        c.execute("COMMIT")
        return dict(order)


class TopupState(StatesGroup):
    amount = State()
    proof = State()


class AddStockState(StatesGroup):
    content = State()


def main_menu(lang="ar"):
    if lang == "en":
        rows = [
            ["🛒 Products", "💳 Wallet"],
            ["👤 Profile", "📦 My Orders"],
            ["👨‍💻 Technical support", "🌐 English/Arabic"],
            ["💱 Currency", "🛡 Terms of Use"],
        ]
    else:
        rows = [
            ["🛒 المنتجات", "💳 المحفظة"],
            ["👤 الحساب", "📦 طلباتي"],
            ["👨‍💻 الدعم الفني", "🌐 English/Arabic"],
            ["💱 العملة", "🛡 شروط الاستخدام"],
        ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=x) for x in row] for row in rows],
        resize_keyboard=True,
    )


def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_label(status: str, lang="ar") -> str:
    ar = {
        "processing": "قيد التجهيز",
        "waiting_stock": "بانتظار المخزون",
        "delivered": "تم التسليم",
        "rejected": "ملغي / مردود",
        "pending": "معلق",
        "approved": "مقبول",
    }
    en = {
        "processing": "Processing",
        "waiting_stock": "Waiting for stock",
        "delivered": "Delivered",
        "rejected": "Cancelled / Refunded",
        "pending": "Pending",
        "approved": "Approved",
    }
    return (en if lang == "en" else ar).get(status, status)


async def notify_admins(bot: Bot, text: str, markup=None, photo_file_id: Optional[str] = None):
    for admin_id in ADMIN_IDS:
        try:
            if photo_file_id:
                await bot.send_photo(admin_id, photo_file_id, caption=text, reply_markup=markup)
            else:
                await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as exc:
            logging.warning("Failed to notify admin %s: %s", admin_id, exc)


def product_buttons(product_id: int, stock: int, allow_preorder: int):
    rows = []
    if stock > 0:
        rows.append([InlineKeyboardButton(text="✅ Buy now", callback_data=f"buy:{product_id}")])
    else:
        if allow_preorder:
            rows.append([InlineKeyboardButton(text="💳 Pay & reserve until restock", callback_data=f"pre:{product_id}")])
        rows.append([InlineKeyboardButton(text="💳 Deposit Now", callback_data="deposit_now")])
    rows.append([InlineKeyboardButton(text="◀️ Reference", callback_data="services")])
    return inline(rows)


@router.message(CommandStart())
async def start(message: Message):
    user = get_user(message.from_user.id)
    username = f"@{message.from_user.username}" if message.from_user.username else "No username"
    text = (
        "👋 <b>Welcome to the professional store!</b>\n\n"
        f"🆔 Hands: <code>{message.from_user.id}</code>\n"
        f"👤 Name: <b>{h(message.from_user.full_name)}</b>\n"
        f"🔎 User: {h(username)}\n"
        f"💵 Balance: <b>{h(fmt_money(user['balance'], user['currency']))}</b>"
    )
    await message.answer(text, reply_markup=main_menu(user["lang"]))


@router.message(F.text.in_({"🌐 English/Arabic"}))
@router.message(Command("lang"))
async def language_menu(message: Message):
    await message.answer(
        "اختر اللغة / Choose language",
        reply_markup=inline(
            [[InlineKeyboardButton(text="🇪🇬 العربية", callback_data="lang:ar"), InlineKeyboardButton(text="🇺🇸 English", callback_data="lang:en")]]
        ),
    )


@router.callback_query(F.data.startswith("lang:"))
async def set_language(call: CallbackQuery):
    lang = call.data.split(":", 1)[1]
    set_user_field(call.from_user.id, "lang", lang)
    await call.message.answer("تم تغيير اللغة ✅" if lang == "ar" else "Language changed ✅", reply_markup=main_menu(lang))
    await call.answer()


@router.message(F.text.in_({"🛒 المنتجات", "🛒 Products", "الخدمات 🛍", "Services 🛍"}))
@router.callback_query(F.data == "services")
async def services(event):
    user_id = event.from_user.id
    user = get_user(user_id)
    cats = [c for c in get_categories(active_only=True) if c["product_count"] > 0]
    rows = []
    line = []
    for cat in cats:
        name = cat["name_en"] if user["lang"] == "en" else cat["name_ar"]
        line.append(InlineKeyboardButton(text=f"{cat['emoji']} {name} ({cat['product_count']})", callback_data=f"cat:{cat['id']}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([InlineKeyboardButton(text="🔄 to update", callback_data="services")])
    text = "🛒 <b>Available Products:</b>" if user["lang"] == "en" else "🛒 <b>المنتجات المتاحة:</b>"
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=inline(rows))
        await event.answer()
    else:
        await event.answer(text, reply_markup=inline(rows))


@router.callback_query(F.data.startswith("cat:"))
async def category_products(call: CallbackQuery):
    user = get_user(call.from_user.id)
    category_id = int(call.data.split(":", 1)[1])
    with db_conn() as c:
        cat = c.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not cat:
        await call.answer("Category not found", show_alert=True)
        return
    products = get_products_by_category(category_id)
    name = cat["name_en"] if user["lang"] == "en" else cat["name_ar"]
    lines = [f"{cat['emoji']} <b>{h(name)}</b>", "━━━━━━━━━━━━━━", "📦 <b>Available Products:</b>", "Tap on any product for more details\n"]
    rows = []
    for i, product in enumerate(products, 1):
        title = product["title_en"] if user["lang"] == "en" else product["title_ar"]
        stock = int(product["stock"])
        warn = "✅ In stock" if stock > 0 else ("⏳ Pre-order available" if product["allow_preorder"] else "❌ Out of stock")
        lines.append(f"<b>{i}</b> {cat['emoji']} {h(title)}\n{warn}")
        btn_prefix = "✅" if stock > 0 else ("⏳" if product["allow_preorder"] else "❌")
        rows.append([InlineKeyboardButton(text=f"{btn_prefix} {title} ({stock})", callback_data=f"product:{product['id']}")])
    rows.append([InlineKeyboardButton(text="◀️ Services", callback_data="services")])
    await call.message.answer("\n\n".join(lines), reply_markup=inline(rows))
    await call.answer()


@router.callback_query(F.data.startswith("product:"))
async def product_details(call: CallbackQuery):
    user = get_user(call.from_user.id)
    product_id = int(call.data.split(":", 1)[1])
    product = get_product(product_id)
    if not product or not product["active"]:
        await call.answer("الخدمة غير متاحة", show_alert=True)
        return
    title = product["title_en"] if user["lang"] == "en" else product["title_ar"]
    desc = product["desc_en"] if user["lang"] == "en" else product["desc_ar"]
    stock = int(product["stock"])
    delivery_type = "Auto ⚡ (Instant delivery)" if (product["delivery_mode"] or "manual") == "auto" else "Manual by admin"
    if stock <= 0 and product["allow_preorder"]:
        stock_line = "0 pcs — Pay & reserve enabled"
    else:
        stock_line = f"{stock} pcs"
    text = (
        f"{product['category_emoji'] or '🛍'} <b>{h(title)}</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"📝 {h(desc)}\n\n"
        f"🚚 Delivery: <b>{h(delivery_type)}</b>\n"
        f"💰 Price: <b>{h(fmt_money(product['price'], user['currency']))}</b>\n"
        f"📊 Stock: <b>{h(stock_line)}</b>\n"
        f"🕠 Duration: <b>{h(product['duration'])}</b>\n"
        f"♻️ Warranty: <b>{h(product['warranty'])}</b>"
    )
    await call.message.answer(
        text,
        reply_markup=product_buttons(product_id, stock, int(product["allow_preorder"])),
    )
    await call.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_confirm(call: CallbackQuery):
    user = get_user(call.from_user.id)
    product_id = int(call.data.split(":", 1)[1])
    product = get_product(product_id)
    if not product:
        await call.answer("Not found", show_alert=True)
        return
    text = (
        f"📦 <b>{h(product['title_ar'])}</b>\n\n"
        "━━━━━━━━━━━━━━\n"
        "🔢 Qty: 1\n"
        f"💵 Price: <b>{h(fmt_money(product['price'], user['currency']))}</b>/unit\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 Total: <b>{h(fmt_money(product['price'], user['currency']))}</b>\n\n"
        "Confirm purchase?"
    )
    await call.message.answer(
        text,
        reply_markup=inline(
            [
                [
                    InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm:{product_id}:buy"),
                    InlineKeyboardButton(text="❌ Cancel", callback_data="cancel"),
                ]
            ]
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pre:"))
async def preorder_confirm(call: CallbackQuery):
    user = get_user(call.from_user.id)
    product_id = int(call.data.split(":", 1)[1])
    product = get_product(product_id)
    if not product:
        await call.answer("Not found", show_alert=True)
        return
    text = (
        f"⏳ <b>Reserve order</b>\n\n"
        f"📦 Product: <b>{h(product['title_ar'])}</b>\n"
        f"💰 Total: <b>{h(fmt_money(product['price'], user['currency']))}</b>\n\n"
        "المنتج غير متوفر حاليًا، تقدر تدفع وتحجز الطلب، وأول ما نضيف ستوك البوت هيسلّمه تلقائيًا."
    )
    await call.message.answer(
        text,
        reply_markup=inline(
            [
                [
                    InlineKeyboardButton(text="✅ Pay & Reserve", callback_data=f"confirm:{product_id}:pre"),
                    InlineKeyboardButton(text="❌ Cancel", callback_data="cancel"),
                ]
            ]
        ),
    )
    await call.answer()


@router.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery):
    await call.message.answer("تم الإلغاء.")
    await call.answer()


@router.callback_query(F.data == "deposit_now")
async def deposit_now(call: CallbackQuery):
    rows = [[InlineKeyboardButton(text="فودافون كاش 💵", callback_data="topup:vodafone")]]
    if BINANCE:
        rows.append([InlineKeyboardButton(text="Binance Pay 💰", callback_data="topup:binance")])
    await call.message.answer("اختر طريقة الشحن:", reply_markup=inline(rows))
    await call.answer()


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_purchase(call: CallbackQuery, bot: Bot):
    _, pid_text, mode = call.data.split(":")
    product_id = int(pid_text)
    preorder = mode == "pre"
    result = purchase_product(call.from_user.id, product_id, preorder=preorder)
    user = get_user(call.from_user.id)
    if not result["ok"]:
        if result["reason"] == "no_balance":
            await call.message.answer(
                "❌ <b>Insufficient balance!</b>\n\nDeposit and try again.",
                reply_markup=inline([[InlineKeyboardButton(text="💳 Deposit Now", callback_data="deposit_now")]]),
            )
        elif result["reason"] == "out_stock":
            await call.message.answer("❌ المنتج غير متوفر حاليًا.", reply_markup=main_menu(user["lang"]))
        else:
            await call.message.answer("حدث خطأ أثناء تنفيذ الطلب.", reply_markup=main_menu(user["lang"]))
        await call.answer()
        return

    order_id = result["order_id"]
    product = result["product"]
    if result["status"] == "delivered":
        await call.message.answer(
            f"✅ تم تنفيذ طلبك رقم #{order_id} وتسليمه تلقائيًا:\n\n<code>{h(result['delivery_content'])}</code>",
            reply_markup=main_menu(user["lang"]),
        )
        await notify_admins(
            bot,
            f"✅ Auto delivered order\nOrder #{order_id}\nUser: {call.from_user.id}\nProduct: {product['title_ar']}\nPrice: {product['price']:.2f} EGP",
        )
    elif result["status"] == "waiting_stock":
        await call.message.answer(
            f"⏳ تم حجز طلبك رقم #{order_id} بنجاح.\nتم خصم الرصيد، وأول ما نضيف ستوك للمنتج هيتسلم تلقائيًا.",
            reply_markup=main_menu(user["lang"]),
        )
        await notify_admins(
            bot,
            f"⏳ Pre-order waiting for stock\nOrder #{order_id}\nUser: {call.from_user.id}\nProduct: {product['title_ar']}\nPrice: {product['price']:.2f} EGP",
        )
    else:
        await call.message.answer(
            f"⏳ Processing your order #{order_id}...\nسيتم التسليم من الإدارة قريبًا.",
            reply_markup=main_menu(user["lang"]),
        )
        await notify_admins(
            bot,
            f"📦 طلب جديد ينتظر التسليم\nOrder #{order_id}\nUser: {call.from_user.id}\nProduct: {product['title_ar']}\nPrice: {product['price']:.2f} EGP",
            inline([[InlineKeyboardButton(text="✅ Deliver", callback_data=f"order:deliver:{order_id}"), InlineKeyboardButton(text="❌ Cancel/Refund", callback_data=f"order:refund:{order_id}")]]),
        )
    await call.answer()


@router.message(F.text.in_({"💳 المحفظة", "💳 Wallet", "إضافة رصيد 💳", "Add balance 💳"}))
async def wallet(message: Message):
    user = get_user(message.from_user.id)
    rows = [[InlineKeyboardButton(text="فودافون كاش 💵", callback_data="topup:vodafone")]]
    if BINANCE:
        rows.append([InlineKeyboardButton(text="Binance Pay 💰", callback_data="topup:binance")])
    text = (
        f"💳 <b>Wallet</b>\n\n"
        f"💵 Balance: <b>{h(fmt_money(user['balance'], user['currency']))}</b>\n\n"
        "اختر طريقة الشحن:"
    )
    await message.answer(text, reply_markup=inline(rows))


@router.callback_query(F.data.startswith("topup:"))
async def topup_start(call: CallbackQuery, state: FSMContext):
    method = call.data.split(":", 1)[1]
    await state.clear()
    await state.update_data(method=method)
    await state.set_state(TopupState.amount)
    if method == "vodafone":
        await call.message.answer(
            f"💰 أرسل المبلغ الذي تريد تحويله إلى المحفظة:\n<code>{h(VODAFONE)}</code>\nالاسم: <code>{h(VODAFONE_NAME)}</code>\n\nاكتب المبلغ الذي حولته الآن:"
        )
    else:
        await call.message.answer(
            f"حوّل المبلغ إلى Binance Pay ID ثم اكتب مبلغ USDT وبعده أرسل رقم العملية/Order ID.\n\nBinance ID: <code>{h(BINANCE)}</code>"
        )
    await call.answer()


@router.message(TopupState.amount)
async def topup_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", ".").strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer("اكتب رقم صحيح.")
        return
    await state.update_data(amount=amount)
    await state.set_state(TopupState.proof)
    await message.answer("أرسل إثبات الدفع الآن: صورة التحويل أو رقم العملية / Order ID.")


@router.message(TopupState.proof)
async def topup_proof(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    method = data["method"]
    amount = float(data["amount"])
    currency = "EGP" if method == "vodafone" else "USD"
    amount_egp = amount if method == "vodafone" else to_egp(amount, "USD")
    file_id = None
    proof = message.text or message.caption or ""
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    topup_id = create_topup(message.from_user.id, method, amount, amount_egp, currency, proof, file_id)
    user = get_user(message.from_user.id)
    await state.clear()
    await message.answer("✅ تم إرسال طلب الشحن للإدارة.\nسيتم إضافة الرصيد بعد المراجعة.", reply_markup=main_menu(user["lang"]))
    admin_text = (
        f"💳 طلب شحن جديد\nTopup #{topup_id}\nUser: {message.from_user.id}\nMethod: {method}\nAmount: {amount:.2f} {currency}\nEGP: {amount_egp:.2f}\nProof: {proof}"
    )
    await notify_admins(
        bot,
        admin_text,
        inline([[InlineKeyboardButton(text="✅ Approve", callback_data=f"topup:approve:{topup_id}"), InlineKeyboardButton(text="❌ Reject", callback_data=f"topup:reject:{topup_id}")]]),
        photo_file_id=file_id,
    )


@router.message(F.text.in_({"👤 الحساب", "👤 Profile", "حسابي 👤", "My account 👤"}))
async def account(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(
        f"👋 <b>Welcome to the professional store!</b>\n\n"
        f"🆔 Hands: <code>{message.from_user.id}</code>\n"
        f"👤 Name: <b>{h(message.from_user.full_name)}</b>\n"
        f"💵 Balance: <b>{h(fmt_money(user['balance'], user['currency']))}</b>",
        reply_markup=main_menu(user["lang"]),
    )


@router.message(F.text.in_({"👨‍💻 الدعم الفني", "👨‍💻 Technical support", "الدعم 💬", "Support 💬"}))
async def support(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(f"💬 للدعم الفني تواصل معنا:\n{h(SUPPORT_USERNAME)}", reply_markup=main_menu(user["lang"]))


@router.message(F.text.in_({"👥 الإحالات", "👥 Referrals"}))
async def referrals(message: Message):
    user = get_user(message.from_user.id)
    await message.answer("👥 نظام الإحالات غير مفعل حاليًا، ويمكن إضافته لاحقًا.", reply_markup=main_menu(user["lang"]))


@router.message(F.text.in_({"🛡 شروط الاستخدام", "🛡 Terms of Use"}))
async def terms(message: Message):
    user = get_user(message.from_user.id)
    await message.answer("🛡 Terms of Use\n\nاستخدم الخدمات طبقًا لشروط المنصات، ولا تشارك بيانات الطلب مع أي شخص.", reply_markup=main_menu(user["lang"]))


@router.message(F.text.in_({"الرصيد 💰", "Balance 💰"}))
async def balance(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(f"رصيدك الحالي هو {fmt_money(user['balance'], user['currency'])}", reply_markup=main_menu(user["lang"]))


@router.message(F.text.in_({"طلباتي 📦", "My orders 📦", "📦 طلباتي", "📦 My Orders"}))
@router.message(Command("orders"))
async def my_orders(message: Message):
    user = get_user(message.from_user.id)
    with db_conn() as c:
        rows = c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("لا توجد طلبات حتى الآن.")
        return
    lines = ["📦 <b>My Orders</b>\n"]
    for r in rows:
        lines.append(f"#{r['id']} | {h(r['title'])}\n💰 {h(fmt_money(r['price'], user['currency']))} | {h(status_label(r['status'], user['lang']))}")
        if r["status"] == "delivered" and r["delivery_content"]:
            lines.append(f"<code>{h(r['delivery_content'])}</code>")
        lines.append("")
    await message.answer("\n".join(lines), reply_markup=main_menu(user["lang"]))


@router.message(F.text.in_({"💱 العملة", "💱 Currency"}))
@router.message(Command("currency"))
async def currency_cmd(message: Message):
    await message.answer(
        "اختر العملة:",
        reply_markup=inline(
            [
                [InlineKeyboardButton(text="ج.م EGP", callback_data="cur:EGP"), InlineKeyboardButton(text="$ USDT", callback_data="cur:USD")],
                [InlineKeyboardButton(text="VND", callback_data="cur:VND")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cur:"))
async def set_currency(call: CallbackQuery):
    currency = call.data.split(":", 1)[1]
    set_user_field(call.from_user.id, "currency", currency)
    user = get_user(call.from_user.id)
    await call.message.answer(f"تم تغيير العملة إلى {currency} ✅", reply_markup=main_menu(user["lang"]))
    await call.answer()


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("هذا الأمر للإدارة فقط.")
        return
    text = (
        "⚙️ <b>لوحة الإدارة</b>\n\n"
        "أوامر المنتجات:\n"
        "/products_admin\n"
        "/addcategory emoji|name_ar|name_en\n"
        "/addproduct_auto category_id|الاسم|السعر|المدة|الضمان|طريقة التسليم|الوصف|preorder yes/no\n"
        "/addstock PRODUCT_ID\n"
        "/deliver ORDER_ID رسالة التسليم"
    )
    await message.answer(
        text,
        reply_markup=inline(
            [
                [InlineKeyboardButton(text="🛒 المنتجات", callback_data="admin:products")],
                [InlineKeyboardButton(text="💳 طلبات الشحن", callback_data="admin:topups")],
                [InlineKeyboardButton(text="📦 طلبات تنتظر التسليم", callback_data="admin:orders")],
                [InlineKeyboardButton(text="⏳ طلبات بانتظار المخزون", callback_data="admin:waiting")],
            ]
        ),
    )


@router.message(Command("addcategory"))
async def addcategory_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("هذا الأمر للإدارة فقط.")
        return
    raw = (message.text or "").replace("/addcategory", "", 1).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) < 3:
        await message.answer("استخدم:\n/addcategory emoji|name_ar|name_en\nمثال:\n/addcategory 🤖|ChatGPT|ChatGPT")
        return
    emoji, name_ar, name_en = parts[:3]
    cid = add_category(name_ar, name_en, emoji)
    await message.answer(f"تم إضافة/تحديث القسم ✅\nCategory ID: {cid}")


@router.message(Command("products_admin"))
async def products_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("هذا الأمر للإدارة فقط.")
        return
    rows = []
    with db_conn() as c:
        rows = c.execute(
            """
            SELECT p.*, c.emoji, c.name_ar category_name,
                   (SELECT COUNT(*) FROM orders o WHERE o.product_id=p.id AND o.status='waiting_stock') AS waiting_count
            FROM products p LEFT JOIN categories c ON c.id=p.category_id
            ORDER BY c.sort_order, p.id
            """
        ).fetchall()
    if not rows:
        await message.answer("لا توجد منتجات.")
        return
    text = ["📦 <b>المنتجات:</b>"]
    for p in rows:
        text.append(
            f"#{p['id']} | {p['emoji'] or '🛍'} {h(p['title_ar'])} | {p['price']:.2f} EGP | stock: {p['stock']} | waiting: {p['waiting_count']} | {p['delivery_mode']}"
        )
    await message.answer("\n".join(text))


@router.callback_query(F.data == "admin:products")
async def admin_products_button(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admins only", show_alert=True)
        return
    with db_conn() as c:
        rows = c.execute(
            """
            SELECT p.*, c.emoji, c.name_ar category_name,
                   (SELECT COUNT(*) FROM orders o WHERE o.product_id=p.id AND o.status='waiting_stock') AS waiting_count
            FROM products p LEFT JOIN categories c ON c.id=p.category_id
            ORDER BY c.sort_order, p.id
            """
        ).fetchall()
    if not rows:
        await call.message.answer("لا توجد منتجات.")
    else:
        text = ["📦 <b>المنتجات:</b>"]
        for p in rows:
            text.append(f"#{p['id']} | {p['emoji'] or '🛍'} {h(p['title_ar'])} | {p['price']:.2f} EGP | stock: {p['stock']} | waiting: {p['waiting_count']} | {p['delivery_mode']}")
        await call.message.answer("\n".join(text))
    await call.answer()


@router.message(Command("addproduct_auto"))
async def addproduct_auto(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("هذا الأمر للإدارة فقط.")
        return
    raw = (message.text or "").replace("/addproduct_auto", "", 1).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) < 7:
        await message.answer("استخدم:\n/addproduct_auto category_id|الاسم|السعر|المدة|الضمان|طريقة التسليم|الوصف|preorder yes/no")
        return
    category_id_text, title, price, duration, warranty, delivery_method, desc = parts[:7]
    allow = 1
    if len(parts) >= 8 and parts[7].lower() in {"no", "0", "false", "لا"}:
        allow = 0
    try:
        product_id = add_auto_product(int(category_id_text), title, float(price), duration, warranty, delivery_method, desc, allow)
    except Exception as exc:
        await message.answer(f"خطأ في إضافة المنتج: {h(exc)}")
        return
    await message.answer(f"تم إضافة المنتج الأوتوماتيك ✅\nProduct ID: {product_id}\n\nالآن أضف المخزون:\n/addstock {product_id}")


@router.message(Command("addstock"))
async def addstock_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("هذا الأمر للإدارة فقط.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("استخدم:\n/addstock PRODUCT_ID\nمثال:\n/addstock 3")
        return
    product_id = int(parts[1])
    if not get_product(product_id):
        await message.answer("Product ID غير موجود.")
        return
    await state.set_state(AddStockState.content)
    await state.update_data(product_id=product_id)
    await message.answer("أرسل المخزون الآن.\nكل عنصر في سطر منفصل.\nأول ما تضيف ستوك، أي طلبات محجوزة هتتسلم تلقائيًا.")


@router.message(AddStockState.content)
async def addstock_receive(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("هذا الأمر للإدارة فقط.")
        return
    data = await state.get_data()
    product_id = int(data["product_id"])
    if message.document:
        await message.answer("انسخ محتوى ملف TXT والصقه هنا كسطور حاليًا.")
        return
    lines = (message.text or "").splitlines()
    try:
        count = add_stock_lines(product_id, lines)
        total = sync_stock(product_id)
        delivered = await fulfill_waiting_orders(bot, product_id)
    except Exception as exc:
        await message.answer(f"خطأ أثناء إضافة المخزون: {h(exc)}")
        return
    await state.clear()
    await message.answer(f"تم إضافة {count} عنصر للمخزون ✅\nالمخزون الحالي: {total}\nطلبات تم تسليمها تلقائيًا من الحجوزات: {delivered}")


@router.message(Command("deliver"))
async def deliver_command(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("هذا الأمر للإدارة فقط.")
        return
    raw = (message.text or "").replace("/deliver", "", 1).strip()
    if " " not in raw:
        await message.answer("استخدم:\n/deliver ORDER_ID رسالة التسليم")
        return
    order_id_text, delivery_text = raw.split(" ", 1)
    if not order_id_text.isdigit() or not delivery_text.strip():
        await message.answer("استخدم:\n/deliver ORDER_ID رسالة التسليم")
        return
    order = deliver_order(int(order_id_text), delivery_text.strip())
    if not order:
        await message.answer("الطلب غير موجود أو ليس قيد المراجعة.")
        return
    await bot.send_message(order["user_id"], f"تم تسليم طلبك رقم #{order['id']} ✅\n\n<code>{h(delivery_text.strip())}</code>")
    await message.answer("تم التسليم ✅")


@router.callback_query(F.data == "admin:topups")
async def admin_topups(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admins only", show_alert=True)
        return
    with db_conn() as c:
        rows = c.execute("SELECT * FROM topups WHERE status='pending' ORDER BY id ASC").fetchall()
    if not rows:
        await call.message.answer("لا توجد طلبات شحن معلقة.")
        await call.answer()
        return
    for row in rows:
        text = f"💳 Topup #{row['id']}\nUser: {row['user_id']}\nMethod: {row['method']}\nAmount: {row['amount_input']:.2f} {row['currency']}\nEGP: {row['amount_egp']:.2f}\nProof: {h(row['proof_text'])}"
        await call.message.answer(text, reply_markup=inline([[InlineKeyboardButton(text="✅ Approve", callback_data=f"topup:approve:{row['id']}"), InlineKeyboardButton(text="❌ Reject", callback_data=f"topup:reject:{row['id']}")]]))
    await call.answer()


@router.callback_query(F.data.startswith("topup:approve:"))
async def topup_approve(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Admins only", show_alert=True)
        return
    topup_id = int(call.data.split(":")[-1])
    row = approve_topup(topup_id)
    if not row:
        await call.answer("Invalid or already handled", show_alert=True)
        return
    await bot.send_message(row["user_id"], f"تمت إضافة رصيد إلى حسابك ✅\nالمبلغ: {row['amount_egp']:.2f} EGP")
    await call.message.answer("تمت الموافقة ✅")
    await call.answer()


@router.callback_query(F.data.startswith("topup:reject:"))
async def topup_reject(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Admins only", show_alert=True)
        return
    topup_id = int(call.data.split(":")[-1])
    row = reject_topup(topup_id)
    if not row:
        await call.answer("Invalid or already handled", show_alert=True)
        return
    await bot.send_message(row["user_id"], "تم رفض طلب الشحن الخاص بك. تواصل مع الدعم.")
    await call.message.answer("تم الرفض ❌")
    await call.answer()


async def show_orders_by_status(call: CallbackQuery, status: str):
    if not is_admin(call.from_user.id):
        await call.answer("Admins only", show_alert=True)
        return
    with db_conn() as c:
        rows = c.execute("SELECT * FROM orders WHERE status=? ORDER BY id ASC", (status,)).fetchall()
    if not rows:
        await call.message.answer("لا توجد طلبات.")
        await call.answer()
        return
    for row in rows:
        await call.message.answer(
            f"📦 Order #{row['id']}\nUser: {row['user_id']}\nProduct: {h(row['title'])}\nPrice: {row['price']:.2f} EGP\nStatus: {h(status_label(row['status']))}",
            reply_markup=inline([[InlineKeyboardButton(text="✅ Deliver default", callback_data=f"order:deliver:{row['id']}"), InlineKeyboardButton(text="❌ Cancel/Refund", callback_data=f"order:refund:{row['id']}")]]),
        )
    await call.answer()


@router.callback_query(F.data == "admin:orders")
async def admin_orders(call: CallbackQuery):
    await show_orders_by_status(call, "processing")


@router.callback_query(F.data == "admin:waiting")
async def admin_waiting(call: CallbackQuery):
    await show_orders_by_status(call, "waiting_stock")


@router.callback_query(F.data.startswith("order:deliver:"))
async def order_deliver_button(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Admins only", show_alert=True)
        return
    order_id = int(call.data.split(":")[-1])
    order = deliver_order(order_id)
    if not order:
        await call.answer("Invalid order", show_alert=True)
        return
    await bot.send_message(order["user_id"], f"تم تسليم طلبك رقم #{order_id} ✅\n\n<code>{h(order['delivery_content'])}</code>")
    await call.message.answer("تم التسليم ✅")
    await call.answer()


@router.callback_query(F.data.startswith("order:refund:"))
async def order_refund_button(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Admins only", show_alert=True)
        return
    order_id = int(call.data.split(":")[-1])
    order = refund_order(order_id)
    if not order:
        await call.answer("Invalid order", show_alert=True)
        return
    await bot.send_message(order["user_id"], f"تم إلغاء طلبك رقم #{order_id} ورد الرصيد لحسابك.")
    await call.message.answer("تم الإلغاء ورد الرصيد ✅")
    await call.answer()


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
        raise RuntimeError("ضع BOT_TOKEN في ملف .env")
    if not ADMIN_IDS:
        raise RuntimeError("ضع ADMIN_IDS في ملف .env")
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
