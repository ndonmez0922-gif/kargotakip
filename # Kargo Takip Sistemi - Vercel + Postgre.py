# Kargo Takip Sistemi - Vercel + PostgreSQL (Neon) sürümü
import os
import secrets
from decimal import Decimal, InvalidOperation
from functools import wraps

import psycopg
import requests
from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from jinja2 import DictLoader
from psycopg.rows import dict_row
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY ortam değişkeni tanımlı değil.")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")))

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
CARRIERS = ["Yurtiçi Kargo", "Aras Kargo", "MNG Kargo", "PTT Kargo", "Sürat Kargo"]
STATUSES = ["Hazırlanıyor", "Kargoya Verildi", "Dağıtımda", "Teslim Edildi", "İade"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  full_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','seller')),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS customers (
  id BIGSERIAL PRIMARY KEY,
  seller_id BIGINT NOT NULL REFERENCES users(id),
  name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (seller_id, phone));
CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  seller_id BIGINT NOT NULL REFERENCES users(id),
  customer_id BIGINT NOT NULL REFERENCES customers(id),
  description TEXT NOT NULL,
  total_amount NUMERIC(12,2) NOT NULL CHECK (total_amount >= 0),
  carrier TEXT NOT NULL, tracking_number TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Kargoya Verildi',
  tracking_token TEXT NOT NULL UNIQUE,
  sms_sent BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_orders_seller ON orders(seller_id);
CREATE INDEX IF NOT EXISTS idx_orders_token ON orders(tracking_token);
"""

TEMPLATES = {
"base.html": """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Kargo Takip</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1f2937;color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
main{max-width:960px;margin:16px auto;padding:0 12px}
.card{background:#fff;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px #0002}
.stats{display:flex;gap:12px;flex-wrap:wrap}.stats .card{flex:1;min-width:140px}.big{font-size:1.6rem;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{padding:8px;border-bottom:1px solid #eee;text-align:left}
.tbl{overflow-x:auto}
input,select,textarea,button{font:inherit;padding:10px;width:100%;box-sizing:border-box;margin:4px 0 12px;border:1px solid #ccc;border-radius:6px}
button,.btn{background:#2563eb;color:#fff;border:0;cursor:pointer;text-decoration:none;display:inline-block;padding:10px 14px;border-radius:6px;width:auto}
.flash{background:#fef3c7;padding:10px;border-radius:6px;margin-bottom:12px}
label.chk{display:flex;gap:8px;align-items:center}label.chk input{width:auto;margin:0}
</style></head><body>
<header><b>Kargo Takip</b>
{% if session.user_id %}<form method="post" action="{{ url_for('logout') }}" style="margin:0">
<input type="hidden" name="_csrf" value="{{ csrf_token() }}">{{ session.name }} <button style="background:#374151">Çıkış</button></form>{% endif %}
</header><main>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{% block content %}{% endblock %}</main></body></html>""",

"login.html": """{% extends "base.html" %}{% block content %}
<div class="card" style="max-width:380px;margin:40px auto"><h2>Giriş</h2>
<form method="post"><input type="hidden" name="_csrf" value="{{ csrf_token() }}">
<label>Kullanıcı adı</label><input name="username" required autofocus>
<label>Şifre</label><input name="password" type="password" required>
<button>Giriş yap</button></form></div>{% endblock %}""",

"seller.html": """{% extends "base.html" %}{% block content %}
<div class="stats">
<div class="card"><div>Toplam Ciro</div><div class="big">{{ "%.2f"|format(stats.revenue) }} ₺</div></div>
<div class="card"><div>Sipariş Adedi</div><div class="big">{{ stats.order_count }}</div></div>
<div class="card"><div>Müşteri</div><div class="big">{{ customers|length }}</div></div></div>
<a class="btn" href="{{ url_for('order_new') }}">+ Yeni Sipariş / Kargo</a>
<div class="card tbl" style="margin-top:16px"><h3>Siparişlerim</h3>
<table><tr><th>Tarih</th><th>Müşteri</th><th>Tutar</th><th>Kargo</th><th>Durum</th><th>Link</th></tr>
{% for o in orders %}<tr><td>{{ (o.created_at|string)[:10] }}</td><td>{{ o.customer_name }}<br><small>{{ o.phone }}</small></td>
<td>{{ "%.2f"|format(o.total_amount) }} ₺</td><td>{{ o.carrier }}<br><small>{{ o.tracking_number }}</small></td>
<td><form method="post" action="{{ url_for('order_status', oid=o.id) }}" style="margin:0">
<input type="hidden" name="_csrf" value="{{ csrf_token() }}">
<select name="status" onchange="this.form.submit()" style="margin:0">{% for s in statuses %}<option {{ 'selected' if s==o.status }}>{{ s }}</option>{% endfor %}</select></form></td>
<td><a href="{{ url_for('track', token=o.tracking_token) }}" target="_blank">Aç</a></td></tr>{% endfor %}</table></div>
<div class="card tbl"><h3>Müşterilerim</h3><table><tr><th>Ad</th><th>Telefon</th><th>Adres</th></tr>
{% for c in customers %}<tr><td>{{ c.name }}</td><td>{{ c.phone }}</td><td>{{ c.address }}</td></tr>{% endfor %}</table></div>
{% endblock %}""",

"admin.html": """{% extends "base.html" %}{% block content %}
<div class="stats">
<div class="card"><div>Şirket Cirosu</div><div class="big">{{ "%.2f"|format(total.revenue) }} ₺</div></div>
<div class="card"><div>Toplam Sipariş</div><div class="big">{{ total.order_count }}</div></div>
<div class="card"><div>Toplam Müşteri</div><div class="big">{{ customers|length }}</div></div></div>
<div class="card tbl"><h3>Satıcı Performansı</h3><table><tr><th>Satıcı</th><th>Sipariş</th><th>Müşteri</th><th>Ciro</th><th>Durum</th></tr>
{% for s in per_seller %}<tr><td>{{ s.full_name }}<br><small>{{ s.username }}</small></td><td>{{ s.order_count }}</td><td>{{ s.customer_count }}</td><td>{{ "%.2f"|format(s.revenue) }} ₺</td>
<td><form method="post" action="{{ url_for('seller_toggle', uid=s.id) }}" style="margin:0"><input type="hidden" name="_csrf" value="{{ csrf_token() }}">
<button style="margin:0;background:{{ '#16a34a' if s.is_active else '#dc2626' }}">{{ 'Aktif' if s.is_active else 'Pasif' }}</button></form></td></tr>{% endfor %}</table></div>
<div class="card"><h3>Yeni Satıcı Ekle</h3>
<form method="post" action="{{ url_for('seller_create') }}"><input type="hidden" name="_csrf" value="{{ csrf_token() }}">
<label>Ad Soyad</label><input name="full_name" required>
<label>Kullanıcı adı</label><input name="username" required>
<label>Şifre (en az 8 karakter)</label><input name="password" type="password" minlength="8" required>
<button>Satıcı Oluştur</button></form></div>
<div class="card tbl"><h3>Tüm Müşteriler</h3><table><tr><th>Ad</th><th>Telefon</th><th>Satıcı</th></tr>
{% for c in customers %}<tr><td>{{ c.name }}</td><td>{{ c.phone }}</td><td>{{ c.seller_name }}</td></tr>{% endfor %}</table></div>
{% endblock %}""",

"order_form.html": """{% extends "base.html" %}{% block content %}
<div class="card"><h3>Yeni Sipariş / Kargo</h3>
<form method="post"><input type="hidden" name="_csrf" value="{{ csrf_token() }}">
<label>Müşteri Adı</label><input name="name" value="{{ f.get('name','') }}" required>
<label>Telefon (5XXXXXXXXX)</label><input name="phone" type="tel" value="{{ f.get('phone','') }}" required>
<label>Teslimat Adresi</label><textarea name="address" rows="3" required>{{ f.get('address','') }}</textarea>
<label>Ürün / Satış Açıklaması</label><textarea name="description" rows="2" required>{{ f.get('description','') }}</textarea>
<label>Toplam Tutar (₺)</label><input name="total_amount" inputmode="decimal" value="{{ f.get('total_amount','') }}" required>
<label>Kargo Firması</label><select name="carrier">{% for c in carriers %}<option>{{ c }}</option>{% endfor %}</select>
<label>Kargo Takip No</label><input name="tracking_number" value="{{ f.get('tracking_number','') }}" required>
<label class="chk"><input type="checkbox" name="send_sms" value="1" checked> Müşteriye Kargo Takip SMS'i Gitsin</label><br>
<button>Kaydet</button></form></div>{% endblock %}""",

"track.html": """{% extends "base.html" %}{% block content %}
<div class="card" style="max-width:480px;margin:24px auto"><h2>Kargo Takibi</h2>
<p>Merhaba {{ masked }},</p><p><b>Durum:</b> {{ o.status }}</p>
<p><b>Kargo Firması:</b> {{ o.carrier }}<br><b>Takip No:</b> {{ o.tracking_number }}</p>
<p><b>Sipariş:</b> {{ o.description }}<br><small>{{ (o.created_at|string)[:10] }}</small></p></div>{% endblock %}""",
}
app.jinja_loader = DictLoader(TEMPLATES)


# ---------- DB ----------
def db():
    if "db" not in g:
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db

@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()

_schema_ready = False

def ensure_schema():
    """Tablolar yoksa oluşturur; ilk admini ortam değişkenlerinden açar."""
    global _schema_ready
    if _schema_ready:
        return
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ortam değişkeni tanımlı değil.")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(727401)")
        conn.execute(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        au = (os.environ.get("ADMIN_USERNAME") or "").strip().lower()
        ap = os.environ.get("ADMIN_PASSWORD")
        if n == 0 and au and ap:
            conn.execute(
                "INSERT INTO users (username,password_hash,full_name,role) "
                "VALUES (%s,%s,%s,'admin') ON CONFLICT (username) DO NOTHING",
                (au, generate_password_hash(ap), "Yönetici"))
    _schema_ready = True

@app.before_request
def before():
    ensure_schema()
    if request.method == "POST" and request.form.get("_csrf") != session.get("_csrf"):
        abort(400)


# ---------- Auth ----------
def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]
app.jinja_env.globals["csrf_token"] = csrf_token

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=%s AND is_active",
                         (request.form["username"].strip().lower(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear()
            session.update(user_id=u["id"], role=u["role"], name=u["full_name"])
            return redirect(url_for("dashboard"))
        flash("Kullanıcı adı veya şifre hatalı.")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Panel ----------
def revenue_stats(seller_id=None):
    where, args = ("WHERE seller_id=%s", (seller_id,)) if seller_id else ("", ())
    return db().execute(
        f"SELECT COUNT(*) AS order_count, COALESCE(SUM(total_amount),0) AS revenue "
        f"FROM orders {where}", args).fetchone()

@app.route("/")
@login_required()
def dashboard():
    if session["role"] == "admin":
        total = revenue_stats()
        per_seller = db().execute("""
            SELECT u.id, u.username, u.full_name, u.is_active,
                   COUNT(o.id) AS order_count,
                   COALESCE(SUM(o.total_amount),0) AS revenue,
                   COUNT(DISTINCT o.customer_id) AS customer_count
            FROM users u LEFT JOIN orders o ON o.seller_id=u.id
            WHERE u.role='seller' GROUP BY u.id ORDER BY revenue DESC""").fetchall()
        customers = db().execute("""
            SELECT c.*, u.full_name AS seller_name FROM customers c
            JOIN users u ON u.id=c.seller_id ORDER BY c.created_at DESC""").fetchall()
        return render_template("admin.html", total=total, per_seller=per_seller,
                               customers=customers)
    sid = session["user_id"]          # satıcı: her sorguda seller_id filtresi
    stats = revenue_stats(sid)
    orders = db().execute("""
        SELECT o.*, c.name AS customer_name, c.phone FROM orders o
        JOIN customers c ON c.id=o.customer_id
        WHERE o.seller_id=%s ORDER BY o.created_at DESC""", (sid,)).fetchall()
    customers = db().execute(
        "SELECT * FROM customers WHERE seller_id=%s ORDER BY name", (sid,)).fetchall()
    return render_template("seller.html", stats=stats, orders=orders,
                           customers=customers, statuses=STATUSES)

@app.route("/admin/sellers", methods=["POST"])
@login_required("admin")
def seller_create():
    f = request.form
    username, full, pwd = f["username"].strip().lower(), f["full_name"].strip(), f["password"]
    if not username or not full or len(pwd) < 8:
        flash("Ad soyad, kullanıcı adı ve en az 8 karakterlik şifre gerekli.")
        return redirect(url_for("dashboard"))
    cur = db().execute(
        "INSERT INTO users (username,password_hash,full_name,role) "
        "VALUES (%s,%s,%s,'seller') ON CONFLICT (username) DO NOTHING",
        (username, generate_password_hash(pwd), full))
    db().commit()
    flash("Satıcı eklendi." if cur.rowcount else "Bu kullanıcı adı zaten var.")
    return redirect(url_for("dashboard"))

@app.route("/admin/sellers/<int:uid>/toggle", methods=["POST"])
@login_required("admin")
def seller_toggle(uid):
    db().execute("UPDATE users SET is_active = NOT is_active WHERE id=%s AND role='seller'",
                 (uid,))
    db().commit()
    return redirect(url_for("dashboard"))


# ---------- Sipariş ----------
def normalize_phone(raw):
    d = "".join(ch for ch in raw if ch.isdigit())
    if d.startswith("90") and len(d) == 12: d = d[2:]
    if d.startswith("0") and len(d) == 11: d = d[1:]
    return d if len(d) == 10 and d.startswith("5") else None

def new_token():
    return secrets.token_urlsafe(24)   # tahmin edilemez takip anahtarı

def send_sms(phone10, message):
    """Netgsm XML API. Başka sağlayıcı kullanacaksan burayı değiştir."""
    user, pwd, header = (os.environ.get(k) for k in
                         ("NETGSM_USER", "NETGSM_PASS", "NETGSM_HEADER"))
    if not (user and pwd and header):
        app.logger.warning("SMS ayarları yok, gönderilmedi: %s", phone10)
        return False
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<mainbody><header><company dil="TR">Netgsm</company>
<usercode>{user}</usercode><password>{pwd}</password>
<type>1:n</type><msgheader>{header}</msgheader></header>
<body><msg><![CDATA[{message}]]></msg><no>{phone10}</no></body></mainbody>"""
    try:
        r = requests.post("https://api.netgsm.com.tr/sms/send/xml",
                          data=xml.encode("utf-8"),
                          headers={"Content-Type": "application/xml"}, timeout=8)
        return r.text.strip().startswith("00")
    except requests.RequestException:
        app.logger.exception("SMS hatası")
        return False

@app.route("/orders/new", methods=["GET", "POST"])
@login_required("seller")
def order_new():
    if request.method == "POST":
        f = request.form
        phone = normalize_phone(f["phone"])
        try:
            amount = Decimal(f["total_amount"].replace(",", ".")).quantize(Decimal("0.01"))
            if amount < 0 or amount >= Decimal("10000000000"):
                raise InvalidOperation
        except InvalidOperation:
            amount = None
        if not phone or amount is None or f["carrier"] not in CARRIERS:
            flash("Telefon (5XXXXXXXXX), tutar veya kargo firması geçersiz.")
            return render_template("order_form.html", carriers=CARRIERS, f=f)
        conn, sid = db(), session["user_id"]
        cust = conn.execute("SELECT id FROM customers WHERE seller_id=%s AND phone=%s",
                            (sid, phone)).fetchone()
        if cust:
            cid = cust["id"]
            conn.execute("UPDATE customers SET name=%s, address=%s WHERE id=%s",
                         (f["name"].strip(), f["address"].strip(), cid))
        else:
            cid = conn.execute(
                "INSERT INTO customers (seller_id,name,phone,address) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (sid, f["name"].strip(), phone, f["address"].strip())).fetchone()["id"]
        token = new_token()
        conn.execute("""INSERT INTO orders (seller_id,customer_id,description,total_amount,
                        carrier,tracking_number,tracking_token)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                     (sid, cid, f["description"].strip(), amount, f["carrier"],
                      f["tracking_number"].strip(), token))
        conn.commit()      # ciro anında yansır
        if f.get("send_sms"):
            base = os.environ.get("BASE_URL") or request.url_root.rstrip("/")
            ok = send_sms(phone, f"Siparişiniz kargoya verildi. Takip: {base}/kargo-takip/{token}")
            conn.execute("UPDATE orders SET sms_sent=%s WHERE tracking_token=%s", (ok, token))
            conn.commit()
            flash("Sipariş kaydedildi, SMS gönderildi." if ok
                  else "Sipariş kaydedildi ama SMS gönderilemedi.")
        else:
            flash("Sipariş kaydedildi.")
        return redirect(url_for("dashboard"))
    return render_template("order_form.html", carriers=CARRIERS, f={})

@app.route("/orders/<int:oid>/status", methods=["POST"])
@login_required("seller")
def order_status(oid):
    st = request.form["status"]
    if st not in STATUSES: abort(400)
    cur = db().execute("UPDATE orders SET status=%s WHERE id=%s AND seller_id=%s",
                       (st, oid, session["user_id"]))
    db().commit()
    if cur.rowcount == 0: abort(404)
    return redirect(url_for("dashboard"))


# ---------- Public takip (girişsiz) ----------
@app.route("/kargo-takip/<token>")
def track(token):
    o = db().execute("""
        SELECT o.carrier, o.tracking_number, o.status, o.description, o.created_at, c.name
        FROM orders o JOIN customers c ON c.id=o.customer_id
        WHERE o.tracking_token=%s""", (token,)).fetchone()
    if not o: abort(404)
    masked = o["name"][0] + "*" * (len(o["name"]) - 1) if o["name"] else ""
    resp = app.make_response(render_template("track.html", o=o, masked=masked))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp
    Flask>=3.0
requests>=2.31
psycopg[binary]>=3.2
{
  "builds": [{ "src": "app.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "app.py" }]
}