import html
import os
import sqlite3
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template_string, request, send_file, session, url_for

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STORE_NAME = os.getenv("STORE_NAME", "LORD STORE")
DB_PATH = os.getenv("DB_PATH", "lord_store.db")
ADMIN_WEB_PASSWORD = os.getenv("ADMIN_WEB_PASSWORD", "change-this-password")
ADMIN_WEB_SECRET = os.getenv("ADMIN_WEB_SECRET", "")
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000") or 8000)

app = Flask(__name__)
app.secret_key = ADMIN_WEB_SECRET or os.urandom(32)


def now():
    return datetime.utcnow().isoformat()


def esc(v):
    return html.escape(str(v or ""), quote=False)


def selected(current, value):
    return "selected" if str(current) == str(value) else ""


def checked(value):
    return "checked" if int(value or 0) else ""


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
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS stock_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                sold INTEGER DEFAULT 0,
                sold_to INTEGER,
                order_id INTEGER,
                created_at TEXT,
                sold_at TEXT
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

            CREATE INDEX IF NOT EXISTS idx_stock_unsold ON stock_items(product_id, sold, id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, product_id, id);
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
        if not c.execute("SELECT COUNT(*) n FROM categories").fetchone()["n"]:
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
        general = c.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()
        if general:
            c.execute("UPDATE products SET category_id=? WHERE category_id IS NULL", (general["id"],))


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def tg_send_message(user_id: int, text: str):
    if not BOT_TOKEN or BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": str(user_id), "text": text, "parse_mode": "HTML"}).encode()
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def sync_stock(product_id: int):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        product = c.execute("SELECT delivery_mode,stock FROM products WHERE id=?", (product_id,)).fetchone()
        if product and (product["delivery_mode"] or "manual") == "auto":
            count = c.execute(
                "SELECT COUNT(*) n FROM stock_items WHERE product_id=? AND sold=0",
                (product_id,),
            ).fetchone()["n"]
            c.execute("UPDATE products SET stock=? WHERE id=?", (count, product_id))
        else:
            count = int(product["stock"]) if product else 0
        c.execute("COMMIT")
    return count


def fulfill_waiting_orders_sync(product_id: int):
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
                "UPDATE orders SET status='delivered',delivery_content=?,updated_at=? WHERE id=?",
                (item["content"], now(), order["id"]),
            )
            count = c.execute("SELECT COUNT(*) n FROM stock_items WHERE product_id=? AND sold=0", (product_id,)).fetchone()["n"]
            c.execute("UPDATE products SET stock=? WHERE id=?", (count, product_id))
            c.execute("COMMIT")
            user_id = int(order["user_id"])
            order_id = int(order["id"])
            content = item["content"]
        tg_send_message(user_id, f"✅ تم توفير المخزون وتسليم طلبك رقم #{order_id}\n\n<code>{html.escape(str(content))}</code>")
        delivered += 1


def stats():
    with db_conn() as c:
        return {
            "users": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
            "categories": c.execute("SELECT COUNT(*) n FROM categories").fetchone()["n"],
            "products": c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"],
            "stock": c.execute("SELECT COALESCE(SUM(stock),0) n FROM products WHERE active=1").fetchone()["n"],
            "pending_topups": c.execute("SELECT COUNT(*) n FROM topups WHERE status='pending'").fetchone()["n"],
            "processing_orders": c.execute("SELECT COUNT(*) n FROM orders WHERE status='processing'").fetchone()["n"],
            "waiting_stock": c.execute("SELECT COUNT(*) n FROM orders WHERE status='waiting_stock'").fetchone()["n"],
            "delivered": c.execute("SELECT COUNT(*) n FROM orders WHERE status='delivered'").fetchone()["n"],
        }


BASE = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} - LORD STORE</title>
<style>
:root{--bg:#080808;--panel:#151515;--gold:#d8aa3a;--text:#fff8e7;--muted:#b9ae90;--red:#e24a4a;--green:#34c76b;--blue:#2a8ed8}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#302713,#080808 45%);font-family:Tahoma,Arial,sans-serif;color:var(--text)}
a{color:var(--gold);text-decoration:none}.layout{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
.side{background:#0b0b0b;border-left:1px solid #3c2c0d;padding:22px;position:sticky;top:0;height:100vh}.brand{font-size:28px;font-weight:900;color:var(--gold)}.sub{color:var(--muted);font-size:13px;margin:6px 0 24px}
.nav a{display:block;background:#151515;color:#fff;border:1px solid #303030;border-radius:13px;padding:12px;margin:8px 0}.nav a:hover{border-color:var(--gold);color:var(--gold)}
.main{padding:24px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}h1{margin:0;color:var(--gold)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(160px,1fr));gap:14px}.card{background:linear-gradient(180deg,#1b1b1b,#101010);border:1px solid #3b2c10;border-radius:18px;padding:18px;box-shadow:0 0 25px rgba(216,170,58,.08)}.num{font-size:28px;font-weight:900;color:var(--gold)}.label{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;background:#111;border:1px solid #3b2c10;border-radius:15px;overflow:hidden}th,td{padding:12px;border-bottom:1px solid #242424;text-align:right;vertical-align:top}th{background:#20190d;color:var(--gold)}
input,textarea,select{width:100%;padding:11px;border-radius:10px;border:1px solid #3b2c10;background:#0c0c0c;color:#fff}textarea{min-height:115px;direction:ltr;text-align:left}.form-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.full{grid-column:1/-1}
button,.btn{display:inline-block;border:0;background:var(--gold);color:#080808;font-weight:800;border-radius:10px;padding:9px 12px;cursor:pointer}.btn2{background:#282828;color:#fff;border:1px solid #444}.danger{background:var(--red);color:#fff}.ok{background:var(--green);color:#06220d}.muted{color:var(--muted)}.flash{padding:12px;border:1px solid #554013;border-radius:12px;color:var(--gold);background:#191919;margin-bottom:12px}.ltr{direction:ltr;text-align:left;white-space:pre-wrap}.pill{padding:4px 8px;border-radius:20px;background:#2b230f;color:var(--gold);font-size:12px}
@media(max-width:900px){.layout{grid-template-columns:1fr}.side{position:relative;height:auto}.grid,.form-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="layout">
<aside class="side">
<div class="brand">LORD STORE</div><div class="sub">Market Style Dashboard</div>
<nav class="nav">
<a href="{{ url_for('index') }}">🏠 الرئيسية</a>
<a href="{{ url_for('categories') }}">🧩 الأقسام</a>
<a href="{{ url_for('products') }}">🛒 المنتجات والمخزون</a>
<a href="{{ url_for('topups') }}">💳 طلبات الشحن</a>
<a href="{{ url_for('orders') }}">📦 الطلبات</a>
<a href="{{ url_for('users') }}">👤 العملاء</a>
<a href="{{ url_for('backup') }}">⬇️ Backup</a>
<a href="{{ url_for('logout') }}">🚪 خروج</a>
</nav>
</aside>
<main class="main">
<div class="top"><h1>{{ title }}</h1><span class="pill">Local Dashboard</span></div>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{{ content|safe }}
</main>
</div>
</body></html>
"""


def page(title, content):
    return render_template_string(BASE, title=title, content=content)


@app.route("/")
def root():
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_WEB_PASSWORD and ADMIN_WEB_PASSWORD != "change-this-password":
            session["admin_logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("كلمة المرور غير صحيحة أو لم تغيّر ADMIN_WEB_PASSWORD من .env")
    return page("Login", """
    <div class="card" style="max-width:430px;margin:auto">
    <h2 style="color:var(--gold)">تسجيل دخول الأدمن</h2>
    <form method="post"><label>Password</label><input type="password" name="password" autofocus><br><br><button>دخول</button></form>
    </div>
    """)


@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin")
@require_login
def index():
    s = stats()
    content = f"""
    <div class="grid">
    <div class="card"><div class="num">{s['users']}</div><div class="label">العملاء</div></div>
    <div class="card"><div class="num">{s['categories']}</div><div class="label">الأقسام</div></div>
    <div class="card"><div class="num">{s['products']}</div><div class="label">المنتجات</div></div>
    <div class="card"><div class="num">{s['stock']}</div><div class="label">إجمالي المخزون الظاهر</div></div>
    <div class="card"><div class="num">{s['pending_topups']}</div><div class="label">شحن معلق</div></div>
    <div class="card"><div class="num">{s['processing_orders']}</div><div class="label">طلبات تجهيز</div></div>
    <div class="card"><div class="num">{s['waiting_stock']}</div><div class="label">حجوزات بانتظار ستوك</div></div>
    <div class="card"><div class="num">{s['delivered']}</div><div class="label">تم تسليمها</div></div>
    </div>
    <br>
    <div class="card">
    <a class="btn" href="{url_for('products')}">إضافة منتج / مخزون</a>
    <a class="btn btn2" href="{url_for('orders')}">متابعة الطلبات</a>
    <a class="btn btn2" href="{url_for('topups')}">مراجعة الشحن</a>
    </div>
    """
    return page("الرئيسية", content)


@app.route("/admin/categories", methods=["GET", "POST"])
@require_login
def categories():
    if request.method == "POST":
        name_ar = request.form.get("name_ar", "").strip()
        name_en = request.form.get("name_en", "").strip() or name_ar
        emoji = request.form.get("emoji", "🛍").strip() or "🛍"
        sort_order = int(request.form.get("sort_order", "0") or 0)
        with db_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO categories(name_ar,name_en,emoji,sort_order,active,created_at) VALUES(?,?,?,?,1,?)",
                (name_ar, name_en, emoji, sort_order, now()),
            )
            c.execute("COMMIT")
        flash("تم إضافة القسم ✅")
        return redirect(url_for("categories"))
    with db_conn() as c:
        rows = c.execute("SELECT * FROM categories ORDER BY sort_order,id").fetchall()
    content = """
    <div class="card"><h2>إضافة قسم</h2>
    <form method="post" class="form-grid">
    <div><label>Emoji</label><input name="emoji" value="🛍"></div>
    <div><label>ترتيب</label><input name="sort_order" type="number" value="0"></div>
    <div><label>اسم عربي</label><input name="name_ar" required></div>
    <div><label>اسم إنجليزي</label><input name="name_en"></div>
    <div class="full"><button>إضافة</button></div>
    </form></div><br>
    <table><tr><th>ID</th><th>القسم</th><th>ترتيب</th><th>حالة</th></tr>
    """
    for r in rows:
        content += f"<tr><td>#{r['id']}</td><td>{esc(r['emoji'])} {esc(r['name_ar'])} / {esc(r['name_en'])}</td><td>{r['sort_order']}</td><td>{'نشط' if r['active'] else 'متوقف'}</td></tr>"
    content += "</table>"
    return page("الأقسام", content)


@app.route("/admin/products", methods=["GET"])
@require_login
def products():
    with db_conn() as c:
        cats = c.execute("SELECT * FROM categories WHERE active=1 ORDER BY sort_order,id").fetchall()
        rows = c.execute(
            """
            SELECT p.*, c.emoji, c.name_ar category_name,
                   (SELECT COUNT(*) FROM orders o WHERE o.product_id=p.id AND o.status='waiting_stock') AS waiting_count
            FROM products p LEFT JOIN categories c ON c.id=p.category_id
            ORDER BY c.sort_order,p.id DESC
            """
        ).fetchall()
    cat_options = "".join([f"<option value='{x['id']}'>{esc(x['emoji'])} {esc(x['name_ar'])}</option>" for x in cats])
    content = f"""
    <div class="card">
    <h2>إضافة منتج</h2>
    <form method="post" action="/admin/products/add" class="form-grid">
    <div><label>القسم</label><select name="category_id">{cat_options}</select></div>
    <div><label>نوع التسليم</label><select name="delivery_mode"><option value="auto">Auto من المخزون</option><option value="manual">Manual من الأدمن</option></select></div>
    <div><label>اسم عربي</label><input name="title_ar" required></div>
    <div><label>اسم إنجليزي</label><input name="title_en"></div>
    <div><label>السعر EGP</label><input name="price" type="number" step="0.01" required></div>
    <div><label>مخزون يدوي فقط</label><input name="stock" type="number" value="0"></div>
    <div><label>المدة</label><input name="duration" value="شهر"></div>
    <div><label>الضمان</label><input name="warranty" value="25 يوم"></div>
    <div><label>طريقة التسليم</label><input name="delivery_method" value="Auto ⚡ Instant delivery"></div>
    <div><label>السماح بالحجز لو مفيش ستوك؟</label><select name="allow_preorder"><option value="1">نعم - Pay & Reserve</option><option value="0">لا</option></select></div>
    <div class="full"><label>الوصف عربي</label><textarea name="desc_ar" required></textarea></div>
    <div class="full"><label>الوصف إنجليزي</label><textarea name="desc_en"></textarea></div>
    <div class="full"><label>رسالة التسليم اليدوي</label><textarea name="delivery_text"></textarea></div>
    <div class="full"><button>إضافة المنتج</button></div>
    </form></div><br>
    <table><tr><th>ID</th><th>القسم</th><th>المنتج</th><th>السعر</th><th>المخزون</th><th>حجوزات</th><th>Mode</th><th>إجراءات</th></tr>
    """
    for r in rows:
        content += f"""
        <tr>
        <td>#{r['id']}</td><td>{esc(r['emoji'])} {esc(r['category_name'])}</td>
        <td><b>{esc(r['title_ar'])}</b><br><span class="muted">{esc(r['desc_ar'])}</span></td>
        <td>{float(r['price']):.2f} EGP</td><td>{r['stock']}</td><td>{r['waiting_count']}</td><td>{esc(r['delivery_mode'])}</td>
        <td>
        <a class="btn" href="/admin/products/{r['id']}/edit">تعديل</a>
        <a class="btn btn2" href="/admin/products/{r['id']}/stock">مخزون</a>
        <form method="post" action="/admin/products/{r['id']}/toggle" style="display:inline"><button class="btn2">تفعيل/تعطيل</button></form>
        </td>
        </tr>
        """
    content += "</table>"
    return page("المنتجات والمخزون", content)


@app.post("/admin/products/add")
@require_login
def add_product():
    category_id = int(request.form.get("category_id", "1") or 1)
    title_ar = request.form.get("title_ar", "").strip()
    title_en = request.form.get("title_en", "").strip() or title_ar
    desc_ar = request.form.get("desc_ar", "").strip()
    desc_en = request.form.get("desc_en", "").strip() or desc_ar
    price = float(request.form.get("price", "0") or 0)
    delivery_mode = request.form.get("delivery_mode", "auto")
    stock = int(request.form.get("stock", "0") or 0)
    duration = request.form.get("duration", "")
    warranty = request.form.get("warranty", "")
    delivery_method = request.form.get("delivery_method", "")
    allow_preorder = int(request.form.get("allow_preorder", "1") or 1)
    delivery_text = request.form.get("delivery_text", "").strip() or "سيتم التواصل معك من الدعم للتسليم."
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """
            INSERT INTO products(category_id,title_ar,title_en,desc_ar,desc_en,price,duration,warranty,delivery_method,stock,delivery_text,delivery_mode,allow_preorder,active,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
            """,
            (category_id, title_ar, title_en, desc_ar, desc_en, price, duration, warranty, delivery_method, 0 if delivery_mode == "auto" else stock, delivery_text, delivery_mode, allow_preorder, now()),
        )
        c.execute("COMMIT")
    flash("تم إضافة المنتج ✅")
    return redirect(url_for("products"))


@app.post("/admin/products/<int:product_id>/toggle")
@require_login
def toggle_product(product_id):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT active FROM products WHERE id=?", (product_id,)).fetchone()
        if r:
            c.execute("UPDATE products SET active=? WHERE id=?", (0 if r["active"] else 1, product_id))
        c.execute("COMMIT")
    flash("تم تحديث حالة المنتج")
    return redirect(url_for("products"))



@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@require_login
def edit_product(product_id):
    with db_conn() as c:
        product = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        cats = c.execute("SELECT * FROM categories WHERE active=1 ORDER BY sort_order,id").fetchall()
    if not product:
        flash("المنتج غير موجود")
        return redirect(url_for("products"))

    if request.method == "POST":
        action = request.form.get("action", "save")

        if action == "save":
            category_id = int(request.form.get("category_id", product["category_id"] or 1) or 1)
            title_ar = request.form.get("title_ar", "").strip()
            title_en = request.form.get("title_en", "").strip() or title_ar
            desc_ar = request.form.get("desc_ar", "").strip()
            desc_en = request.form.get("desc_en", "").strip() or desc_ar
            price = float(request.form.get("price", "0") or 0)
            duration = request.form.get("duration", "").strip()
            warranty = request.form.get("warranty", "").strip()
            delivery_method = request.form.get("delivery_method", "").strip()
            delivery_text = request.form.get("delivery_text", "").strip()
            delivery_mode = request.form.get("delivery_mode", "manual")
            allow_preorder = int(request.form.get("allow_preorder", "0") or 0)
            active = int(request.form.get("active", "0") or 0)
            manual_stock = int(request.form.get("manual_stock", "0") or 0)

            with db_conn() as c:
                c.execute("BEGIN IMMEDIATE")
                old = c.execute("SELECT delivery_mode FROM products WHERE id=?", (product_id,)).fetchone()
                stock_value = manual_stock
                if delivery_mode == "auto":
                    stock_value = c.execute(
                        "SELECT COUNT(*) n FROM stock_items WHERE product_id=? AND sold=0",
                        (product_id,),
                    ).fetchone()["n"]
                c.execute(
                    """
                    UPDATE products SET
                        category_id=?,
                        title_ar=?,
                        title_en=?,
                        desc_ar=?,
                        desc_en=?,
                        price=?,
                        duration=?,
                        warranty=?,
                        delivery_method=?,
                        delivery_text=?,
                        delivery_mode=?,
                        allow_preorder=?,
                        active=?,
                        stock=?
                    WHERE id=?
                    """,
                    (
                        category_id, title_ar, title_en, desc_ar, desc_en, price,
                        duration, warranty, delivery_method, delivery_text,
                        delivery_mode, allow_preorder, active, stock_value, product_id
                    ),
                )
                c.execute("COMMIT")
            flash("تم حفظ تعديلات المنتج ✅")
            return redirect(url_for("edit_product", product_id=product_id))

        if action == "add_stock":
            lines = [x.strip() for x in request.form.get("stock_lines", "").splitlines() if x.strip()]
            if not lines:
                flash("لم يتم إضافة مخزون لأن الخانة فارغة")
                return redirect(url_for("edit_product", product_id=product_id))
            with db_conn() as c:
                c.execute("BEGIN IMMEDIATE")
                c.executemany(
                    "INSERT INTO stock_items(product_id,content,sold,created_at) VALUES(?,?,0,?)",
                    [(product_id, line, now()) for line in lines],
                )
                c.execute("UPDATE products SET delivery_mode='auto' WHERE id=?", (product_id,))
                c.execute("COMMIT")
            total = sync_stock(product_id)
            delivered = fulfill_waiting_orders_sync(product_id)
            flash(f"تم إضافة {len(lines)} عنصر للمخزون ✅ المخزون الحالي: {total} | تم تسليم {delivered} طلب محجوز")
            return redirect(url_for("edit_product", product_id=product_id))

        if action == "replace_unsold_stock":
            lines = [x.strip() for x in request.form.get("replace_stock_lines", "").splitlines() if x.strip()]
            with db_conn() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute("DELETE FROM stock_items WHERE product_id=? AND sold=0", (product_id,))
                if lines:
                    c.executemany(
                        "INSERT INTO stock_items(product_id,content,sold,created_at) VALUES(?,?,0,?)",
                        [(product_id, line, now()) for line in lines],
                    )
                c.execute("UPDATE products SET delivery_mode='auto' WHERE id=?", (product_id,))
                c.execute("COMMIT")
            total = sync_stock(product_id)
            delivered = fulfill_waiting_orders_sync(product_id)
            flash(f"تم استبدال المخزون غير المباع ✅ المخزون الحالي: {total} | تم تسليم {delivered} طلب محجوز")
            return redirect(url_for("edit_product", product_id=product_id))

        if action == "clear_unsold_stock":
            with db_conn() as c:
                c.execute("BEGIN IMMEDIATE")
                deleted = c.execute("DELETE FROM stock_items WHERE product_id=? AND sold=0", (product_id,)).rowcount
                c.execute("COMMIT")
            total = sync_stock(product_id)
            flash(f"تم حذف {deleted} عنصر غير مباع من المخزون. المخزون الحالي: {total}")
            return redirect(url_for("edit_product", product_id=product_id))

    with db_conn() as c:
        product = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        counts = c.execute(
            """
            SELECT
              SUM(CASE WHEN sold=0 THEN 1 ELSE 0 END) unsold,
              SUM(CASE WHEN sold=1 THEN 1 ELSE 0 END) sold
            FROM stock_items WHERE product_id=?
            """,
            (product_id,),
        ).fetchone()
        waiting = c.execute("SELECT COUNT(*) n FROM orders WHERE product_id=? AND status='waiting_stock'", (product_id,)).fetchone()["n"]
        unsold_items = c.execute(
            "SELECT id,content,created_at FROM stock_items WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 50",
            (product_id,),
        ).fetchall()

    cat_options = "".join([
        f"<option value='{cat['id']}' {selected(product['category_id'], cat['id'])}>{esc(cat['emoji'])} {esc(cat['name_ar'])}</option>"
        for cat in cats
    ])
    auto_sel = selected(product["delivery_mode"], "auto")
    manual_sel = selected(product["delivery_mode"], "manual")
    preorder_yes = selected(product["allow_preorder"], 1)
    preorder_no = selected(product["allow_preorder"], 0)
    active_yes = selected(product["active"], 1)
    active_no = selected(product["active"], 0)

    unsold_text = "\n".join([str(x["content"]) for x in unsold_items])

    content = f"""
    <div class="card">
      <h2>تعديل المنتج #{product_id}</h2>
      <form method="post" class="form-grid">
        <input type="hidden" name="action" value="save">

        <div>
          <label>القسم</label>
          <select name="category_id">{cat_options}</select>
        </div>

        <div>
          <label>حالة المنتج</label>
          <select name="active">
            <option value="1" {active_yes}>نشط</option>
            <option value="0" {active_no}>متوقف</option>
          </select>
        </div>

        <div>
          <label>اسم المنتج عربي</label>
          <input name="title_ar" value="{esc(product['title_ar'])}" required>
        </div>

        <div>
          <label>اسم المنتج إنجليزي</label>
          <input name="title_en" value="{esc(product['title_en'])}">
        </div>

        <div>
          <label>السعر بالجنيه EGP</label>
          <input name="price" type="number" step="0.01" value="{float(product['price']):.2f}" required>
        </div>

        <div>
          <label>نوع التسليم</label>
          <select name="delivery_mode">
            <option value="auto" {auto_sel}>Auto من المخزون</option>
            <option value="manual" {manual_sel}>Manual من الأدمن</option>
          </select>
        </div>

        <div>
          <label>المدة</label>
          <input name="duration" value="{esc(product['duration'])}">
        </div>

        <div>
          <label>الضمان</label>
          <input name="warranty" value="{esc(product['warranty'])}">
        </div>

        <div>
          <label>طريقة التسليم</label>
          <input name="delivery_method" value="{esc(product['delivery_method'])}">
        </div>

        <div>
          <label>السماح بالحجز لو مفيش ستوك؟</label>
          <select name="allow_preorder">
            <option value="1" {preorder_yes}>نعم - Pay & Reserve</option>
            <option value="0" {preorder_no}>لا</option>
          </select>
        </div>

        <div>
          <label>المخزون اليدوي Manual Stock</label>
          <input name="manual_stock" type="number" value="{int(product['stock'] or 0)}">
          <small class="muted">يستخدم فقط لو نوع التسليم Manual. لو Auto المخزون بيتحسب من العناصر غير المباعة.</small>
        </div>

        <div>
          <label>إحصائيات Auto Stock</label>
          <input readonly value="غير مباع: {counts['unsold'] or 0} | مباع: {counts['sold'] or 0} | حجوزات: {waiting}">
        </div>

        <div class="full">
          <label>الوصف عربي</label>
          <textarea name="desc_ar" style="direction:rtl;text-align:right">{esc(product['desc_ar'])}</textarea>
        </div>

        <div class="full">
          <label>الوصف إنجليزي</label>
          <textarea name="desc_en">{esc(product['desc_en'])}</textarea>
        </div>

        <div class="full">
          <label>رسالة التسليم اليدوي</label>
          <textarea name="delivery_text" style="direction:rtl;text-align:right">{esc(product['delivery_text'])}</textarea>
        </div>

        <div class="full">
          <button type="submit">حفظ التعديلات ✅</button>
          <a class="btn btn2" href="{url_for('products')}">رجوع للمنتجات</a>
        </div>
      </form>
    </div>

    <br>

    <div class="card">
      <h2>إضافة مخزون Auto</h2>
      <p class="muted">كل عنصر في سطر منفصل. بعد الإضافة سيتم تسليم أي طلبات محجوزة تلقائيًا حسب الأقدمية.</p>
      <form method="post">
        <input type="hidden" name="action" value="add_stock">
        <textarea name="stock_lines" placeholder="CODE-111&#10;email@example.com|password&#10;download link"></textarea>
        <br><br>
        <button type="submit">إضافة للمخزون + تسليم الحجوزات</button>
      </form>
    </div>

    <br>

    <div class="card">
      <h2>تعديل / استبدال المخزون غير المباع</h2>
      <p class="muted">هنا يظهر أول 50 عنصر غير مباع فقط. عند الحفظ سيتم حذف المخزون غير المباع القديم واستبداله بالنص الموجود هنا. العناصر المباعة لن يتم لمسها.</p>
      <form method="post">
        <input type="hidden" name="action" value="replace_unsold_stock">
        <textarea name="replace_stock_lines">{esc(unsold_text)}</textarea>
        <br><br>
        <button type="submit">استبدال المخزون غير المباع</button>
      </form>
      <br>
      <form method="post" onsubmit="return confirm('هل أنت متأكد من حذف كل المخزون غير المباع لهذا المنتج؟')">
        <input type="hidden" name="action" value="clear_unsold_stock">
        <button class="danger" type="submit">حذف كل المخزون غير المباع</button>
      </form>
    </div>

    <br>

    <div class="card">
      <h2>أول 50 عنصر غير مباع</h2>
      <table><tr><th>ID</th><th>المحتوى</th><th>تاريخ الإضافة</th></tr>
    """
    for item in unsold_items:
        content += f"<tr><td>#{item['id']}</td><td class='ltr'>{esc(item['content'])}</td><td>{esc(item['created_at'])}</td></tr>"
    content += "</table></div>"
    return page("تعديل المنتج", content)


@app.route("/admin/products/<int:product_id>/stock", methods=["GET", "POST"])
@require_login
def product_stock(product_id):
    with db_conn() as c:
        product = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        flash("المنتج غير موجود")
        return redirect(url_for("products"))

    if request.method == "POST":
        if request.form.get("manual_stock") is not None:
            stock = int(request.form.get("manual_stock", "0") or 0)
            with db_conn() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE products SET stock=? WHERE id=?", (stock, product_id))
                c.execute("COMMIT")
            flash("تم تحديث المخزون اليدوي")
            return redirect(url_for("product_stock", product_id=product_id))

        lines = [x.strip() for x in request.form.get("stock_lines", "").splitlines() if x.strip()]
        if not lines:
            flash("المخزون فارغ")
            return redirect(url_for("product_stock", product_id=product_id))
        with db_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.executemany(
                "INSERT INTO stock_items(product_id,content,sold,created_at) VALUES(?,?,0,?)",
                [(product_id, line, now()) for line in lines],
            )
            c.execute("UPDATE products SET delivery_mode='auto' WHERE id=?", (product_id,))
            c.execute("COMMIT")
        total = sync_stock(product_id)
        delivered = fulfill_waiting_orders_sync(product_id)
        flash(f"تم إضافة {len(lines)} عنصر. المخزون الحالي: {total}. تم تسليم {delivered} طلب محجوز تلقائيًا.")
        return redirect(url_for("product_stock", product_id=product_id))

    with db_conn() as c:
        counts = c.execute(
            "SELECT SUM(CASE WHEN sold=0 THEN 1 ELSE 0 END) unsold, SUM(CASE WHEN sold=1 THEN 1 ELSE 0 END) sold FROM stock_items WHERE product_id=?",
            (product_id,),
        ).fetchone()
        waiting = c.execute("SELECT COUNT(*) n FROM orders WHERE product_id=? AND status='waiting_stock'", (product_id,)).fetchone()["n"]
        sample = c.execute("SELECT * FROM stock_items WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 15", (product_id,)).fetchall()

    content = f"""
    <div class="card"><h2>{esc(product['title_ar'])}</h2>
    <p>Auto stock غير مباع: <b>{counts['unsold'] or 0}</b> | مباع: <b>{counts['sold'] or 0}</b> | حجوزات بانتظار ستوك: <b>{waiting}</b></p>
    <form method="post">
    <label>إضافة ستوك تلقائي، كل عنصر في سطر منفصل</label>
    <textarea name="stock_lines" placeholder="CODE-111&#10;email@example.com|password&#10;download link"></textarea><br><br>
    <button>إضافة Auto Stock + تسليم الحجوزات</button>
    </form></div><br>
    <div class="card"><h3>مخزون يدوي</h3>
    <form method="post" class="form-grid"><div><input name="manual_stock" type="number" value="{product['stock']}"></div><div><button>تحديث المخزون اليدوي</button></div></form>
    </div><br>
    <div class="card"><h3>أول 15 عنصر غير مباع</h3><table><tr><th>ID</th><th>المحتوى</th><th>التاريخ</th></tr>
    """
    for s in sample:
        content += f"<tr><td>#{s['id']}</td><td class='ltr'>{esc(s['content'])}</td><td>{esc(s['created_at'])}</td></tr>"
    content += "</table></div>"
    return page("مخزون المنتج", content)


@app.route("/admin/topups")
@require_login
def topups():
    with db_conn() as c:
        rows = c.execute("SELECT * FROM topups ORDER BY id DESC LIMIT 150").fetchall()
    content = "<table><tr><th>ID</th><th>User</th><th>Method</th><th>Amount</th><th>Proof</th><th>Status</th><th>Action</th></tr>"
    for r in rows:
        actions = ""
        if r["status"] == "pending":
            actions = f"""
            <form method="post" action="/admin/topups/{r['id']}/approve" style="display:inline"><button class="ok">Approve</button></form>
            <form method="post" action="/admin/topups/{r['id']}/reject" style="display:inline"><button class="danger">Reject</button></form>
            """
        content += f"<tr><td>#{r['id']}</td><td>{r['user_id']}</td><td>{esc(r['method'])}</td><td>{float(r['amount_input']):.2f} {esc(r['currency'])}<br>{float(r['amount_egp']):.2f} EGP</td><td class='ltr'>{esc(r['proof_text'])}</td><td>{esc(r['status'])}</td><td>{actions}</td></tr>"
    content += "</table>"
    return page("طلبات الشحن", content)


@app.post("/admin/topups/<int:topup_id>/approve")
@require_login
def topup_approve(topup_id):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM topups WHERE id=?", (topup_id,)).fetchone()
        if not r or r["status"] != "pending":
            c.execute("ROLLBACK"); flash("تم التعامل مع الطلب قبل كده"); return redirect(url_for("topups"))
        c.execute("UPDATE topups SET status='approved', updated_at=? WHERE id=?", (now(), topup_id))
        c.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(r["amount_egp"]), r["user_id"]))
        c.execute("COMMIT")
    tg_send_message(r["user_id"], f"تمت إضافة رصيد إلى حسابك ✅\nالمبلغ: {float(r['amount_egp']):.2f} EGP")
    flash("تمت الموافقة وإضافة الرصيد ✅")
    return redirect(url_for("topups"))


@app.post("/admin/topups/<int:topup_id>/reject")
@require_login
def topup_reject(topup_id):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM topups WHERE id=?", (topup_id,)).fetchone()
        if not r or r["status"] != "pending":
            c.execute("ROLLBACK"); flash("تم التعامل مع الطلب قبل كده"); return redirect(url_for("topups"))
        c.execute("UPDATE topups SET status='rejected', updated_at=? WHERE id=?", (now(), topup_id))
        c.execute("COMMIT")
    tg_send_message(r["user_id"], "تم رفض طلب الشحن الخاص بك. تواصل مع الدعم.")
    flash("تم رفض الطلب")
    return redirect(url_for("topups"))


@app.route("/admin/orders")
@require_login
def orders():
    status = request.args.get("status", "")
    with db_conn() as c:
        if status:
            rows = c.execute("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 200", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    content = """
    <div class="card">
    <a class="btn btn2" href="/admin/orders">كل الطلبات</a>
    <a class="btn btn2" href="/admin/orders?status=processing">قيد التجهيز</a>
    <a class="btn btn2" href="/admin/orders?status=waiting_stock">بانتظار ستوك</a>
    <a class="btn btn2" href="/admin/orders?status=delivered">تم التسليم</a>
    </div><br>
    <table><tr><th>ID</th><th>User</th><th>Product</th><th>Price</th><th>Status</th><th>Delivery</th><th>Action</th></tr>
    """
    for r in rows:
        actions = ""
        if r["status"] in ("processing", "waiting_stock"):
            actions = f"""
            <form method="post" action="/admin/orders/{r['id']}/deliver">
            <textarea name="delivery_text" placeholder="رسالة التسليم"></textarea>
            <button class="ok">Deliver</button>
            </form>
            <form method="post" action="/admin/orders/{r['id']}/refund"><button class="danger">Cancel + Refund</button></form>
            """
        content += f"<tr><td>#{r['id']}</td><td>{r['user_id']}</td><td>{esc(r['title'])}</td><td>{float(r['price']):.2f} EGP</td><td>{esc(r['status'])}</td><td class='ltr'>{esc(r['delivery_content'])}</td><td>{actions}</td></tr>"
    content += "</table>"
    return page("الطلبات", content)


@app.post("/admin/orders/<int:order_id>/deliver")
@require_login
def order_deliver(order_id):
    delivery_text = request.form.get("delivery_text", "").strip() or "تم تجهيز طلبك، تواصل مع الدعم لو احتجت مساعدة."
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not r or r["status"] not in ("processing", "waiting_stock"):
            c.execute("ROLLBACK"); flash("الطلب غير صالح"); return redirect(url_for("orders"))
        c.execute("UPDATE orders SET status='delivered',delivery_content=?,updated_at=? WHERE id=?", (delivery_text, now(), order_id))
        c.execute("COMMIT")
    tg_send_message(r["user_id"], f"تم تسليم طلبك رقم #{order_id} ✅\n\n<code>{html.escape(delivery_text)}</code>")
    flash("تم التسليم ✅")
    return redirect(url_for("orders"))


@app.post("/admin/orders/<int:order_id>/refund")
@require_login
def order_refund(order_id):
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not r or r["status"] not in ("processing", "waiting_stock"):
            c.execute("ROLLBACK"); flash("الطلب غير صالح"); return redirect(url_for("orders"))
        c.execute("UPDATE orders SET status='rejected',updated_at=? WHERE id=?", (now(), order_id))
        c.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(r["price"]), r["user_id"]))
        c.execute("COMMIT")
    tg_send_message(r["user_id"], f"تم إلغاء طلبك رقم #{order_id} ورد الرصيد لحسابك.")
    flash("تم الإلغاء ورد الرصيد ✅")
    return redirect(url_for("orders"))


@app.route("/admin/users")
@require_login
def users():
    with db_conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 300").fetchall()
    content = "<table><tr><th>User ID</th><th>Balance</th><th>Currency</th><th>Lang</th><th>Created</th></tr>"
    for r in rows:
        content += f"<tr><td>{r['id']}</td><td>{float(r['balance']):.2f} EGP</td><td>{esc(r['currency'])}</td><td>{esc(r['lang'])}</td><td>{esc(r['created_at'])}</td></tr>"
    content += "</table>"
    return page("العملاء", content)


@app.route("/admin/backup")
@require_login
def backup():
    path = os.path.abspath(DB_PATH)
    if not os.path.exists(path):
        flash("قاعدة البيانات غير موجودة")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True, download_name=f"lord_store_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")


if __name__ == "__main__":
    init_db()
    print(f"Dashboard running: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/admin")
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)
