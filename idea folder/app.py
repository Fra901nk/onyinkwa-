import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, make_response, request, send_from_directory
from werkzeug.exceptions import HTTPException


ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "3000"))
PRODUCTION = (
    os.environ.get("APP_ENV") == "production"
    or os.environ.get("FLASK_ENV") == "production"
    or os.environ.get("NODE_ENV") == "production"
    or bool(os.environ.get("VERCEL"))
)
HOST = os.environ.get("HOST", "0.0.0.0" if PRODUCTION else "127.0.0.1")
SESSION_COOKIE = "jadhum_admin"
SESSION_DURATION = 8 * 60 * 60
MAX_REQUEST_BYTES = 16 * 1024
QUOTATION_STATUSES = {"pending", "priced", "ordered", "closed"}
ORDER_STATUSES = {"received", "processing", "completed", "cancelled"}

SEED_CATEGORIES = [
    ("rods", "Fishing Rods"),
    ("reels", "Fishing Reels"),
    ("line", "Fishing Line"),
    ("terminal-tackle", "Hooks & Terminal Tackle"),
    ("lures-bait", "Lures & Bait"),
    ("nets-gaffs", "Nets & Gaffs"),
    ("tackle-storage", "Tackle Storage"),
    ("floats-sinkers", "Floats & Sinkers"),
    ("apparel-waders", "Apparel & Waders"),
    ("electronics", "Fishing Electronics"),
    ("boating-safety", "Boating & Safety"),
    ("tools", "Fishing Tools"),
]
SEED_PRODUCTS = [
    ("rods", "Fishing Rods", "Spinning, casting, surf, and travel rods for different waters and techniques.", "rods.jpg", "rods"),
    ("reels", "Fishing Reels", "Spinning, baitcasting, and fly reels to pair with your fishing rod.", "reels.jpg", "reels"),
    ("line", "Fishing Line", "Monofilament, braid, and fluorocarbon lines for a range of fishing conditions.", "line.jpg", "line"),
    ("hooks", "Hooks & Terminal Tackle", "Hooks, swivels, leaders, sinkers, and rigs for your tackle box.", "hooks.jpg", "terminal-tackle"),
    ("lures", "Lures & Bait", "Hard baits, soft plastics, spoons, jigs, and baiting essentials.", "lures.jpg", "lures-bait"),
    ("nets", "Nets & Gaffs", "Landing nets and fish-handling gear for bringing in your catch.", "nets.jpg", "nets-gaffs"),
    ("storage", "Tackle Storage", "Tackle boxes, utility trays, bags, and organizers for your gear.", "storage.jpg", "tackle-storage"),
    ("floats", "Floats & Sinkers", "Bobbers, floats, weights, and split shot for balanced presentations.", "floats.jpg", "floats-sinkers"),
    ("apparel", "Apparel & Waders", "Weather-ready clothing, boots, gloves, and waders for days outdoors.", "apparel.jpg", "apparel-waders"),
    ("electronics", "Fishing Electronics", "Fish finders, depth finders, and useful navigation accessories.", "electronics.jpg", "electronics"),
    ("boating", "Boating & Safety", "Life jackets, boat accessories, dry bags, and on-water essentials.", "boating.jpg", "boating-safety"),
    ("tools", "Fishing Tools", "Pliers, line cutters, scales, hook removers, and handy accessories.", "tools.jpg", "tools"),
]
IMAGE_FILES = [product[3] for product in SEED_PRODUCTS]
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

configured_password = os.environ.get("ADMIN_PASSWORD", "Admin123456!")
if PRODUCTION and not os.environ.get("ADMIN_PASSWORD"):
    raise RuntimeError("ADMIN_PASSWORD must be set in the deployment environment.")
if len(configured_password) < 12 and HOST not in LOCAL_HOSTS:
    raise RuntimeError("ADMIN_PASSWORD must be at least 12 characters when the server is exposed beyond this computer.")
if not os.environ.get("ADMIN_PASSWORD"):
    logging.warning("ADMIN_PASSWORD is not set. Using the built-in fallback password for local development.")
if len(configured_password) < 12:
    logging.warning("Warning: this short admin password is allowed only on localhost. Use a strong password before deployment.")
PASSWORD_HASH = hashlib.sha256(configured_password.encode("utf-8")).digest()

configured_database = os.environ.get("DATABASE_PATH")
if configured_database:
    DATABASE_PATH = Path(configured_database)
elif os.environ.get("VERCEL"):
    DATABASE_PATH = Path(tempfile.gettempdir()) / "jadhum-quotations.sqlite"
else:
    DATABASE_PATH = ROOT / "data" / "quotations.sqlite"
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = ROOT / DATABASE_PATH

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
sessions = {}
login_attempts = {}
quotation_attempts = {}
state_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def initialize_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as database:
        database.execute("PRAGMA foreign_keys = ON")
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS quotations (
                reference TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp', 'sms')),
                status TEXT NOT NULL CHECK (status IN ('pending', 'priced', 'ordered', 'closed')),
                items_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS categories (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                image TEXT NOT NULL,
                price_cents INTEGER,
                category_id TEXT NOT NULL REFERENCES categories(id),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS customers_email_idx ON customers (email);
            CREATE INDEX IF NOT EXISTS customers_phone_idx ON customers (phone);
            CREATE TABLE IF NOT EXISTS orders (
                reference TEXT PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                quotation_reference TEXT UNIQUE REFERENCES quotations(reference),
                items_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('received', 'processing', 'completed', 'cancelled')),
                total_cents INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders (created_at DESC);
            CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status);
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                type TEXT NOT NULL,
                description TEXT NOT NULL,
                entity TEXT NOT NULL DEFAULT '',
                entity_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS quotations_created_at_idx ON quotations (created_at DESC);
            CREATE INDEX IF NOT EXISTS quotations_status_idx ON quotations (status);
            """
        )
        database.executemany(
            "INSERT OR IGNORE INTO categories (id, name) VALUES (?, ?)",
            SEED_CATEGORIES,
        )
        now = now_iso()
        database.executemany(
            """
            INSERT OR IGNORE INTO products
                (id, name, description, image, category_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(*product, now, now) for product in SEED_PRODUCTS],
        )


initialize_database()


def get_db():
    if "database" not in g:
        g.database = sqlite3.connect(DATABASE_PATH)
        g.database.row_factory = sqlite3.Row
        g.database.execute("PRAGMA foreign_keys = ON")
    return g.database


@app.after_request
def finish_request(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if "database" in g:
        if response.status_code < 400:
            g.database.commit()
        else:
            g.database.rollback()
    return response


@app.teardown_appcontext
def close_db(error=None):
    database = g.pop("database", None)
    if database is not None:
        if error is not None:
            database.rollback()
        database.close()


@app.errorhandler(ApiError)
def handle_api_error(error):
    return jsonify(error=str(error)), error.status


@app.errorhandler(413)
def handle_too_large(_error):
    return jsonify(error="Request body is too large."), 413


@app.errorhandler(HTTPException)
def handle_http_error(error):
    if request.path.startswith("/api/"):
        message = "Page or API route was not found." if error.code == 404 else error.description
        return jsonify(error=message), error.code
    return error


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    logging.exception("Request failed.", exc_info=error)
    if request.path.startswith("/api/"):
        return jsonify(error="The server could not complete the request."), 500
    return "The server could not complete the request.", 500


def body_json():
    try:
        body = request.get_json(force=True)
    except Exception as error:
        raise ApiError(400, "Request body must be valid JSON.") from error
    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be valid JSON.")
    return body


def normalize_text(value, field, max_length, optional=False):
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be text.")
    text = value.strip()
    if (not optional and not text) or len(text) > max_length:
        raise ApiError(400, f"{field} is required and must be at most {max_length} characters.")
    return text


def rate_limited(store, key, limit, window_seconds):
    now = time.time()
    with state_lock:
        entry = store.get(key)
        if not entry or entry["started"] + window_seconds <= now:
            store[key] = {"started": now, "count": 0}
            return False
        return entry["count"] >= limit


def record_attempt(store, key, window_seconds):
    now = time.time()
    with state_lock:
        entry = store.get(key)
        if not entry or entry["started"] + window_seconds <= now:
            store[key] = {"started": now, "count": 1}
        else:
            entry["count"] += 1


def record_activity(kind, description, entity="", entity_id=""):
    get_db().execute(
        "INSERT INTO activity (created_at, type, description, entity, entity_id) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), kind, description, entity, entity_id),
    )


def valid_password(candidate):
    return hmac.compare_digest(hashlib.sha256(candidate.encode("utf-8")).digest(), PASSWORD_HASH)


def get_session_token():
    return request.cookies.get(SESSION_COOKIE, "")


def is_authenticated():
    token = get_session_token()
    with state_lock:
        session = sessions.get(token)
        if not session:
            return False
        if session["expires"] <= time.time():
            del sessions[token]
            return False
        return True


def require_admin(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            raise ApiError(401, "Admin sign-in is required.")
        return function(*args, **kwargs)
    return wrapped


def validate_product(body, existing_id=""):
    product_id = existing_id or normalize_text(body.get("id"), "Product ID", 60)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", product_id):
        raise ApiError(400, "Product ID may use lowercase letters, numbers, and hyphens.")
    name = normalize_text(body.get("name"), "Product name", 120)
    description = normalize_text(body.get("description"), "Product description", 500)
    category_id = normalize_text(body.get("categoryId"), "Category", 60)
    database = get_db()
    if not database.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
        raise ApiError(400, "Choose an existing product category.")
    image = body.get("image")
    if not isinstance(image, str) or Path(image).name not in IMAGE_FILES:
        raise ApiError(400, "Choose an available product photo.")
    image = Path(image).name
    price = body.get("price")
    price_cents = None
    if price not in ("", None):
        try:
            amount = float(price)
        except (TypeError, ValueError):
            amount = float("nan")
        if not (0 <= amount <= 100000000 and amount != float("inf") and amount == amount):
            raise ApiError(400, "Price must be a non-negative KES amount.")
        price_cents = int(amount * 100 + 0.5)
    return {
        "id": product_id,
        "name": name,
        "description": description,
        "categoryId": category_id,
        "image": image,
        "priceCents": price_cents,
    }


def validate_quotation(body):
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ApiError(400, "Add at least one valid product to the quotation.")
    channel = body.get("channel")
    if not isinstance(channel, str) or channel not in {"email", "whatsapp", "sms"}:
        raise ApiError(400, "Choose a valid contact channel.")
    seen = set()
    result = []
    database = get_db()
    for item in items:
        quantity = item.get("quantity") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item["id"] in seen
            or isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not 1 <= quantity <= 99
            or isinstance(quantity, float) and not quantity.is_integer()
        ):
            raise ApiError(400, "The quotation contains an invalid product or quantity.")
        product = database.execute(
            "SELECT name, active FROM products WHERE id = ?", (item["id"],)
        ).fetchone()
        if not product or not product["active"]:
            raise ApiError(400, "One of the selected products is no longer available.")
        seen.add(item["id"])
        result.append({"id": item["id"], "name": product["name"], "quantity": int(quantity)})
    return channel, result


def make_reference(table, prefix="JAD", created_at=None):
    date_part = (created_at or now_iso())[:10].replace("-", "")
    database = get_db()
    for _attempt in range(5):
        reference = f"{prefix}-{date_part}-{secrets.token_bytes(4).hex().upper()}"
        if not database.execute(f"SELECT 1 FROM {table} WHERE reference = ?", (reference,)).fetchone():
            return reference
    raise RuntimeError("Could not generate a unique quotation reference.")


def format_product(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "image": f"/images/photos/{row['image']}",
        "price": None if row["price_cents"] is None else row["price_cents"] / 100,
        "categoryId": row["category_id"],
        "categoryName": row["category_name"],
        "active": bool(row["active"]),
    }


def format_quotation(row):
    return {
        "reference": row["reference"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "channel": row["channel"],
        "status": row["status"],
        "orderReference": row["order_reference"] or None,
        "items": json.loads(row["items_json"]),
    }


def format_order(row):
    return {
        "reference": row["reference"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "status": row["status"],
        "total": None if row["total_cents"] is None else row["total_cents"] / 100,
        "customer": {
            "id": row["customer_id"],
            "name": row["customer_name"],
            "email": row["email"],
            "phone": row["phone"],
        },
        "quotationReference": row["quotation_reference"],
        "items": json.loads(row["items_json"]),
    }


@app.get("/api/products")
def api_products():
    rows = get_db().execute(
        """
        SELECT p.id, p.name, p.description, p.image, p.price_cents, p.category_id, p.active,
            c.name AS category_name
        FROM products p JOIN categories c ON c.id = p.category_id
        WHERE p.active = 1 ORDER BY p.name COLLATE NOCASE
        """
    ).fetchall()
    return jsonify(products=[format_product(row) for row in rows])


@app.get("/api/categories")
def api_categories():
    rows = get_db().execute(
        "SELECT id, name FROM categories ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return jsonify(categories=[dict(row) for row in rows])


@app.post("/api/quotations")
def create_quotation():
    client_ip = request.remote_addr or "unknown"
    if rate_limited(quotation_attempts, client_ip, 30, 15 * 60):
        raise ApiError(429, "Too many quotation requests. Please try again later.")
    channel, items = validate_quotation(body_json())
    created_at = now_iso()
    reference = make_reference("quotations", created_at=created_at)
    database = get_db()
    database.execute(
        """
        INSERT INTO quotations (reference, created_at, updated_at, channel, status, items_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (reference, created_at, created_at, channel, "pending", json.dumps(items)),
    )
    record_attempt(quotation_attempts, client_ip, 15 * 60)
    record_activity("quotation.created", f"New price request received by {channel}.", "quotation", reference)
    return jsonify(reference=reference, createdAt=created_at, status="pending"), 201


@app.post("/api/admin/login")
def admin_login():
    client_ip = request.remote_addr or "unknown"
    if rate_limited(login_attempts, client_ip, 10, 15 * 60):
        raise ApiError(429, "Too many sign-in attempts. Try again in 15 minutes.")
    body = body_json()
    password = body.get("password")
    if not isinstance(password, str) or len(password) > 256 or not valid_password(password):
        record_attempt(login_attempts, client_ip, 15 * 60)
        raise ApiError(401, "Incorrect admin password.")
    with state_lock:
        login_attempts.pop(client_ip, None)
        token = secrets.token_hex(32)
        sessions[token] = {"expires": time.time() + SESSION_DURATION}
    response = jsonify(authenticated=True)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DURATION,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE") == "1" or request.is_secure,
        samesite="Strict",
        path="/",
    )
    return response


@app.get("/api/admin/session")
def admin_session():
    return jsonify(authenticated=is_authenticated())


@app.post("/api/admin/logout")
@require_admin
def admin_logout():
    with state_lock:
        sessions.pop(get_session_token(), None)
    response = jsonify(authenticated=False)
    response.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE") == "1" or request.is_secure,
        samesite="Strict",
        path="/",
    )
    return response


@app.get("/api/admin/overview")
@require_admin
def admin_overview():
    database = get_db()
    counts = database.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM products WHERE active = 1) AS products,
            (SELECT COUNT(*) FROM categories) AS categories,
            (SELECT COUNT(*) FROM customers) AS customers,
            (SELECT COUNT(*) FROM orders) AS orders,
            (SELECT COUNT(*) FROM orders WHERE status IN ('received', 'processing')) AS open_orders,
            (SELECT COUNT(*) FROM quotations WHERE status = 'pending') AS pending_quotations,
            (SELECT COALESCE(SUM(total_cents), 0) FROM orders WHERE status != 'cancelled') AS recorded_sales_cents
        """
    ).fetchone()
    activity = database.execute(
        "SELECT id, created_at, type, description, entity, entity_id FROM activity ORDER BY id DESC LIMIT 20"
    ).fetchall()
    return jsonify(
        stats={
            "products": counts["products"],
            "categories": counts["categories"],
            "customers": counts["customers"],
            "orders": counts["orders"],
            "openOrders": counts["open_orders"],
            "pendingQuotations": counts["pending_quotations"],
            "recordedSales": counts["recorded_sales_cents"] / 100,
        },
        activity=[dict(row) for row in activity],
    )


@app.get("/api/admin/categories")
@require_admin
def admin_categories():
    rows = get_db().execute(
        """
        SELECT c.id, c.name, COUNT(p.id) AS product_count
        FROM categories c LEFT JOIN products p ON p.category_id = c.id
        GROUP BY c.id ORDER BY c.name COLLATE NOCASE
        """
    ).fetchall()
    return jsonify(categories=[dict(row) for row in rows])


@app.get("/api/admin/options")
@require_admin
def admin_options():
    rows = get_db().execute(
        "SELECT id, name FROM categories ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return jsonify(categories=[dict(row) for row in rows], images=IMAGE_FILES)


@app.post("/api/admin/categories")
@require_admin
def create_category():
    body = body_json()
    name = normalize_text(body.get("name"), "Category name", 80)
    category_id = body.get("id")
    if isinstance(category_id, str) and category_id.strip():
        category_id = normalize_text(category_id, "Category ID", 60)
    else:
        category_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", category_id):
        raise ApiError(400, "Category ID may use lowercase letters, numbers, and hyphens.")
    database = get_db()
    if database.execute(
        "SELECT 1 FROM categories WHERE id = ? OR name = ? COLLATE NOCASE", (category_id, name)
    ).fetchone():
        raise ApiError(409, "A category with this ID or name already exists.")
    database.execute("INSERT INTO categories (id, name) VALUES (?, ?)", (category_id, name))
    record_activity("category.created", f"Added category {name}.", "category", category_id)
    return jsonify(id=category_id, name=name), 201


@app.route("/api/admin/categories/<category_id>", methods=["PATCH", "DELETE"])
@require_admin
def update_or_delete_category(category_id):
    database = get_db()
    if request.method == "PATCH":
        name = normalize_text(body_json().get("name"), "Category name", 80)
        if database.execute(
            "SELECT 1 FROM categories WHERE id != ? AND name = ? COLLATE NOCASE", (category_id, name)
        ).fetchone():
            raise ApiError(409, "A category with this name already exists.")
        result = database.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        if result.rowcount == 0:
            raise ApiError(404, "Category was not found.")
        record_activity("category.updated", f"Updated category {name}.", "category", category_id)
        return jsonify(id=category_id, name=name)
    category = database.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not category:
        raise ApiError(404, "Category was not found.")
    usage = database.execute(
        "SELECT COUNT(*) AS count FROM products WHERE category_id = ?", (category_id,)
    ).fetchone()
    if usage["count"] > 0:
        raise ApiError(409, "Move or remove this category's products before deleting it.")
    database.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    record_activity("category.deleted", f"Deleted category {category['name']}.", "category", category_id)
    return jsonify(id=category_id)


@app.get("/api/admin/products")
@require_admin
def admin_products():
    term = request.args.get("q", "").strip()[:100]
    pattern = f"%{term}%"
    rows = get_db().execute(
        """
        SELECT p.id, p.name, p.description, p.image, p.price_cents, p.category_id, p.active,
            c.name AS category_name
        FROM products p JOIN categories c ON c.id = p.category_id
        WHERE p.name LIKE ? OR p.id LIKE ? OR c.name LIKE ?
        ORDER BY p.active DESC, p.name COLLATE NOCASE LIMIT 500
        """,
        (pattern, pattern, pattern),
    ).fetchall()
    return jsonify(products=[format_product(row) for row in rows])


@app.post("/api/admin/products")
@require_admin
def create_product():
    product = validate_product(body_json())
    database = get_db()
    if database.execute("SELECT 1 FROM products WHERE id = ?", (product["id"],)).fetchone():
        raise ApiError(409, "A product with this ID already exists.")
    now = now_iso()
    database.execute(
        """
        INSERT INTO products
            (id, name, description, image, price_cents, category_id, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            product["id"], product["name"], product["description"], product["image"],
            product["priceCents"], product["categoryId"], now, now,
        ),
    )
    record_activity("product.created", f"Added product {product['name']}.", "product", product["id"])
    return jsonify(product=product), 201


@app.route("/api/admin/products/<product_id>", methods=["PATCH", "DELETE"])
@require_admin
def update_or_archive_product(product_id):
    database = get_db()
    previous = database.execute(
        "SELECT name FROM products WHERE id = ? AND active = 1", (product_id,)
    ).fetchone()
    if not previous:
        raise ApiError(404, "Active product was not found.")
    if request.method == "PATCH":
        product = validate_product(body_json(), product_id)
        now = now_iso()
        database.execute(
            """
            UPDATE products
            SET name = ?, description = ?, image = ?, price_cents = ?, category_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                product["name"], product["description"], product["image"],
                product["priceCents"], product["categoryId"], now, product_id,
            ),
        )
        record_activity("product.updated", f"Updated product {product['name']}.", "product", product_id)
        product["id"] = product_id
        return jsonify(product=product)
    database.execute(
        "UPDATE products SET active = 0, updated_at = ? WHERE id = ?", (now_iso(), product_id)
    )
    record_activity("product.archived", f"Archived product {previous['name']}.", "product", product_id)
    return jsonify(id=product_id, archived=True)


@app.get("/api/admin/quotations")
@require_admin
def admin_quotations():
    term = request.args.get("q", "").strip()[:100]
    status = request.args.get("status", "all")
    if status != "all" and status not in QUOTATION_STATUSES:
        raise ApiError(400, "Choose a valid quotation status.")
    pattern = f"%{term}%"
    base_query = """
        SELECT q.reference, q.created_at, q.updated_at, q.channel, q.status, q.items_json,
            (SELECT o.reference FROM orders o WHERE o.quotation_reference = q.reference) AS order_reference
        FROM quotations q
    """
    if status == "all":
        rows = get_db().execute(
            base_query + """
            WHERE q.reference LIKE ? OR q.items_json LIKE ?
            ORDER BY q.created_at DESC LIMIT 200
            """,
            (pattern, pattern),
        ).fetchall()
    else:
        rows = get_db().execute(
            base_query + """
            WHERE q.status = ? AND (q.reference LIKE ? OR q.items_json LIKE ?)
            ORDER BY q.created_at DESC LIMIT 200
            """,
            (status, pattern, pattern),
        ).fetchall()
    return jsonify(quotations=[format_quotation(row) for row in rows])


@app.get("/api/admin/customers")
@require_admin
def admin_customers():
    pattern = f"%{request.args.get('q', '').strip()[:100]}%"
    rows = get_db().execute(
        """
        SELECT c.id, c.name, c.email, c.phone, c.created_at,
            COUNT(o.reference) AS order_count, MAX(o.created_at) AS last_order_at
        FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
        WHERE c.name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?
        GROUP BY c.id ORDER BY c.created_at DESC LIMIT 500
        """,
        (pattern, pattern, pattern),
    ).fetchall()
    return jsonify(customers=[dict(row) for row in rows])


@app.get("/api/admin/orders")
@require_admin
def admin_orders():
    term = request.args.get("q", "").strip()[:100]
    status = request.args.get("status", "all")
    if status != "all" and status not in ORDER_STATUSES:
        raise ApiError(400, "Choose a valid order status.")
    pattern = f"%{term}%"
    base_query = """
        SELECT o.*, c.name AS customer_name, c.email, c.phone
        FROM orders o JOIN customers c ON c.id = o.customer_id
    """
    if status == "all":
        rows = get_db().execute(
            base_query + """
            WHERE o.reference LIKE ? OR c.name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?
            ORDER BY o.created_at DESC LIMIT 200
            """,
            (pattern, pattern, pattern, pattern),
        ).fetchall()
    else:
        rows = get_db().execute(
            base_query + """
            WHERE o.status = ? AND (o.reference LIKE ? OR c.name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?)
            ORDER BY o.created_at DESC LIMIT 200
            """,
            (status, pattern, pattern, pattern, pattern),
        ).fetchall()
    return jsonify(orders=[format_order(row) for row in rows])


@app.patch("/api/admin/orders/<order_reference>")
@require_admin
def update_order(order_reference):
    status = body_json().get("status")
    if not isinstance(status, str) or status not in ORDER_STATUSES:
        raise ApiError(400, "Choose a valid order status.")
    updated_at = now_iso()
    result = get_db().execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE reference = ?",
        (status, updated_at, order_reference),
    )
    if result.rowcount == 0:
        raise ApiError(404, "Order was not found.")
    record_activity("order.status", f"Order {order_reference} marked {status}.", "order", order_reference)
    return jsonify(reference=order_reference, status=status, updatedAt=updated_at)


@app.post("/api/admin/quotations/<quotation_reference>/order")
@require_admin
def create_order_from_quotation(quotation_reference):
    database = get_db()
    quotation = database.execute(
        "SELECT * FROM quotations WHERE reference = ?", (quotation_reference,)
    ).fetchone()
    if not quotation:
        raise ApiError(404, "Quotation was not found.")
    if database.execute(
        "SELECT 1 FROM orders WHERE quotation_reference = ?", (quotation_reference,)
    ).fetchone():
        raise ApiError(409, "An order has already been recorded for this quotation.")
    body = body_json()
    name = normalize_text(body.get("name"), "Customer name", 120)
    email = normalize_text(body.get("email") or "", "Customer email", 254, optional=True).lower()
    phone = normalize_text(body.get("phone") or "", "Customer phone", 40, optional=True)
    if not email and not phone:
        raise ApiError(400, "Enter at least a customer email address or phone number.")
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ApiError(400, "Enter a valid customer email address.")
    total = body.get("total")
    total_cents = None
    if total not in ("", None):
        try:
            amount = float(total)
        except (TypeError, ValueError):
            amount = float("nan")
        if not (0 <= amount <= 1000000000 and amount != float("inf") and amount == amount):
            raise ApiError(400, "Order total must be a non-negative KES amount.")
        total_cents = int(amount * 100 + 0.5)

    now = now_iso()
    customer = None
    if email:
        customer = database.execute("SELECT id FROM customers WHERE email = ?", (email,)).fetchone()
    if not customer and phone:
        customer = database.execute("SELECT id FROM customers WHERE phone = ?", (phone,)).fetchone()
    if customer:
        customer_id = customer["id"]
        database.execute(
            "UPDATE customers SET name = ?, email = ?, phone = ?, updated_at = ? WHERE id = ?",
            (name, email, phone, now, customer_id),
        )
    else:
        cursor = database.execute(
            "INSERT INTO customers (name, email, phone, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, email, phone, now, now),
        )
        customer_id = cursor.lastrowid
    order_reference = make_reference("orders", "ORD", now)
    database.execute(
        """
        INSERT INTO orders
            (reference, customer_id, quotation_reference, items_json, status, total_cents, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'received', ?, ?, ?)
        """,
        (order_reference, customer_id, quotation_reference, quotation["items_json"], total_cents, now, now),
    )
    database.execute(
        "UPDATE quotations SET status = 'ordered', updated_at = ? WHERE reference = ?",
        (now, quotation_reference),
    )
    record_activity("order.created", f"Recorded order {order_reference} for {name}.", "order", order_reference)
    record_activity(
        "quotation.status",
        f"Quotation {quotation_reference} converted to an order.",
        "quotation",
        quotation_reference,
    )
    return jsonify(reference=order_reference, quotationReference=quotation_reference, status="received"), 201


@app.patch("/api/admin/quotations/<quotation_reference>")
@require_admin
def update_quotation(quotation_reference):
    status = body_json().get("status")
    if not isinstance(status, str) or status not in QUOTATION_STATUSES:
        raise ApiError(400, "Choose a valid quotation status.")
    updated_at = now_iso()
    result = get_db().execute(
        "UPDATE quotations SET status = ?, updated_at = ? WHERE reference = ?",
        (status, updated_at, quotation_reference),
    )
    if result.rowcount == 0:
        raise ApiError(404, "Quotation was not found.")
    record_activity(
        "quotation.status",
        f"Quotation {quotation_reference} marked {status}.",
        "quotation",
        quotation_reference,
    )
    return jsonify(reference=quotation_reference, status=status, updatedAt=updated_at)


@app.route("/", defaults={"asset": ""}, methods=["GET"])
@app.route("/<path:asset>", methods=["GET"])
def serve_page(asset):
    pages = {
        "": ("index.html", "text/html; charset=utf-8"),
        "index.html": ("index.html", "text/html; charset=utf-8"),
        "style.css": ("style.css", "text/css; charset=utf-8"),
        "script.js": ("script.js", "text/javascript; charset=utf-8"),
        "admin": ("admin.html", "text/html; charset=utf-8"),
        "admin.html": ("admin.html", "text/html; charset=utf-8"),
        "admin.css": ("admin.css", "text/css; charset=utf-8"),
        "admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    }
    if asset in pages:
        filename, content_type = pages[asset]
        response = make_response(send_from_directory(ROOT, filename))
        response.headers["Content-Type"] = content_type
        response.headers["Cache-Control"] = "no-cache"
        return response
    if asset.startswith("images/photos/") and Path(asset).name in IMAGE_FILES:
        return send_from_directory(ROOT / "images" / "photos", Path(asset).name, mimetype="image/jpeg")
    abort(404)


if __name__ == "__main__":
    if not 1 <= PORT <= 65535:
        raise SystemExit("PORT must be a valid TCP port.")
    logging.basicConfig(level=logging.INFO)
    app.run(host=HOST, port=PORT)
