"""
Database Schema & Manager — Multi-Product License Server
One server, one DB, N products. Each tool has its own product_id.
"""

import sqlite3, time, os

DB_PATH = "licenses.db"

SCHEMA = """

-- TABLE 0: products — one row per tool you sell
CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          TEXT    NOT NULL UNIQUE,    -- short code: "TOOL1", "PDF_PRO"
    name                TEXT    NOT NULL,           -- "Image Converter Pro"
    price_inr           REAL,                       -- Rs 499
    price_usd           REAL,                       -- $ 9.99
    razorpay_link       TEXT,                       -- https://rzp.io/l/xxx
    gumroad_product_id  TEXT,                       -- gumroad product_id for API verify
    gumroad_link        TEXT,                       -- https://you.gumroad.com/l/xxx
    max_machines        INTEGER NOT NULL DEFAULT 1, -- PCs allowed per license
    is_active           INTEGER NOT NULL DEFAULT 1, -- 0 = stop selling
    created_at          REAL    NOT NULL
);

-- TABLE 1: customers — one row per buyer email (shared across products)
-- Same person buying 3 tools = 1 customer, 3 payments
CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE,
    full_name       TEXT,
    country         TEXT,                           -- 'IN' | 'US' | 'GB' etc.
    created_at      REAL    NOT NULL,
    updated_at      REAL
);

-- TABLE 2: payments — one row per transaction
-- product_id tells you which tool was bought
CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT    NOT NULL REFERENCES products(product_id),
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    source          TEXT    NOT NULL,               -- 'razorpay' | 'gumroad'
    payment_ref     TEXT    NOT NULL UNIQUE,        -- pay_xxx (razorpay) or license key (gumroad)
    amount          REAL    NOT NULL,
    currency        TEXT    NOT NULL,               -- 'INR' | 'USD'
    plan            TEXT    NOT NULL DEFAULT 'lifetime', -- 'lifetime'|'annual'|'monthly'
    paid_at         REAL    NOT NULL,
    is_refunded     INTEGER NOT NULL DEFAULT 0,
    refunded_at     REAL
);

-- TABLE 3: licenses — one row per activated machine per product
-- unique_key = HMAC(product_id + email + machine_id)
-- Same person, same machine, different tool = different unique_key = different row
CREATE TABLE IF NOT EXISTS licenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT    NOT NULL REFERENCES products(product_id),
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    payment_id      INTEGER NOT NULL REFERENCES payments(id),
    unique_key      TEXT    NOT NULL UNIQUE,
    machine_id      TEXT    NOT NULL,
    machine_label   TEXT,                           -- optional "Home PC"
    plan            TEXT    NOT NULL DEFAULT 'lifetime',
    is_active       INTEGER NOT NULL DEFAULT 1,
    paid_at         REAL    NOT NULL,
    expires_at      REAL,                           -- NULL = lifetime
    activated_at    REAL    NOT NULL,
    last_seen_at    REAL,
    last_seen_ip    TEXT,
    verify_count    INTEGER NOT NULL DEFAULT 0,
    revoked_at      REAL,
    revoke_reason   TEXT                            -- 'refund'|'abuse'|'manual'
);

-- TABLE 4: verify_log — audit trail, auto-purged after 90 days
CREATE TABLE IF NOT EXISTS verify_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT,
    license_id      INTEGER REFERENCES licenses(id),
    email           TEXT,
    ip_address      TEXT,
    result          TEXT    NOT NULL,               -- 'ok'|'invalid'|'expired'|'revoked'|'wrong_product'
    called_at       REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lic_unique_key   ON licenses(unique_key);
CREATE INDEX IF NOT EXISTS idx_lic_product      ON licenses(product_id);
CREATE INDEX IF NOT EXISTS idx_lic_customer     ON licenses(customer_id);
CREATE INDEX IF NOT EXISTS idx_lic_expires      ON licenses(expires_at);
CREATE INDEX IF NOT EXISTS idx_pay_product      ON payments(product_id);
CREATE INDEX IF NOT EXISTS idx_pay_customer     ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_vlog_called      ON verify_log(called_at);
CREATE INDEX IF NOT EXISTS idx_vlog_product     ON verify_log(product_id);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

# ── Products ──────────────────────────────────────────────────────────────────

def add_product(product_id, name, price_inr, price_usd,
                razorpay_link=None, gumroad_product_id=None,
                gumroad_link=None, max_machines=1):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO products
            (product_id,name,price_inr,price_usd,razorpay_link,
             gumroad_product_id,gumroad_link,max_machines,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (product_id,name,price_inr,price_usd,razorpay_link,
              gumroad_product_id,gumroad_link,max_machines,time.time()))
        conn.commit()
        return True
    except Exception as e:
        print(f"add_product error: {e}")
        return False
    finally:
        conn.close()

def get_product(product_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM products WHERE product_id=? AND is_active=1",
                       (product_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

# ── Registration ──────────────────────────────────────────────────────────────

def register_license(product_id, email, machine_id, unique_key,
                     source, payment_ref, amount, currency,
                     plan="lifetime", full_name=None, country=None, ip_address=None):
    conn = get_conn()
    now = time.time()
    try:
        c = conn.cursor()

        # Validate product
        c.execute("SELECT max_machines FROM products WHERE product_id=? AND is_active=1", (product_id,))
        prod = c.fetchone()
        if not prod:
            return {"ok": False, "reason": "unknown_product"}

        # Upsert customer
        c.execute("SELECT id FROM customers WHERE email=?", (email,))
        row = c.fetchone()
        if row:
            customer_id = row["id"]
            c.execute("UPDATE customers SET updated_at=? WHERE id=?", (now, customer_id))
        else:
            c.execute("INSERT INTO customers (email,full_name,country,created_at,updated_at) VALUES (?,?,?,?,?)",
                      (email, full_name, country, now, now))
            customer_id = c.lastrowid

        # Reject reused payment_ref
        c.execute("SELECT id FROM payments WHERE payment_ref=?", (payment_ref,))
        if c.fetchone():
            return {"ok": False, "reason": "payment_already_used"}

        # Check machine count for this customer + product
        c.execute("SELECT COUNT(*) AS cnt FROM licenses WHERE customer_id=? AND product_id=? AND is_active=1",
                  (customer_id, product_id))
        if c.fetchone()["cnt"] >= prod["max_machines"]:
            return {"ok": False, "reason": f"max_machines_reached ({prod['max_machines']})"}

        # Insert payment
        c.execute("""INSERT INTO payments
                     (product_id,customer_id,source,payment_ref,amount,currency,plan,paid_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (product_id, customer_id, source, payment_ref, amount, currency, plan, now))
        payment_db_id = c.lastrowid

        # Check if license already exists
        c.execute("SELECT id, is_active FROM licenses WHERE unique_key=?", (unique_key,))
        existing = c.fetchone()
        if existing:
            return {"ok": True,  "reason": "already_registered"} if existing["is_active"] \
                   else {"ok": False, "reason": "license_revoked"}

        # Insert license
        c.execute("""INSERT INTO licenses
                     (product_id,customer_id,payment_id,unique_key,machine_id,
                      plan,paid_at,expires_at,activated_at,last_seen_at,last_seen_ip,verify_count)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                  (product_id, customer_id, payment_db_id, unique_key, machine_id,
                   plan, now, plan_expiry(plan, now), now, now, ip_address))
        conn.commit()
        return {"ok": True, "reason": "registered"}

    except Exception as e:
        conn.rollback()
        return {"ok": False, "reason": f"db_error: {e}"}
    finally:
        conn.close()

# ── Verification ──────────────────────────────────────────────────────────────

def verify_license(product_id, unique_key, ip_address=None):
    """
    product_id is sent by the EXE hardcoded.
    Prevents reusing Tool1 key to unlock Tool2.
    """
    conn = get_conn()
    now = time.time()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT l.id, l.is_active, l.expires_at, l.product_id, cu.email
            FROM licenses l JOIN customers cu ON cu.id=l.customer_id
            WHERE l.unique_key=?
        """, (unique_key,))
        row = c.fetchone()

        if not row:
            _log(conn, product_id, None, None, ip_address, "invalid")
            return {"ok": False, "reason": "not_found"}

        if row["product_id"] != product_id:
            _log(conn, product_id, None, row["email"], ip_address, "wrong_product")
            return {"ok": False, "reason": "wrong_product"}

        if not row["is_active"]:
            _log(conn, product_id, row["id"], row["email"], ip_address, "revoked")
            return {"ok": False, "reason": "revoked"}

        if row["expires_at"] and now > row["expires_at"]:
            _log(conn, product_id, row["id"], row["email"], ip_address, "expired")
            return {"ok": False, "reason": "expired"}

        c.execute("UPDATE licenses SET last_seen_at=?,last_seen_ip=?,verify_count=verify_count+1 WHERE id=?",
                  (now, ip_address, row["id"]))
        conn.commit()
        _log(conn, product_id, row["id"], row["email"], ip_address, "ok")
        return {"ok": True, "reason": "ok"}
    finally:
        conn.close()

# ── Admin ─────────────────────────────────────────────────────────────────────

def revoke_license(email, product_id=None, reason="manual"):
    conn = get_conn()
    now = time.time()
    c = conn.cursor()
    if product_id:
        c.execute("""UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason=?
                     WHERE customer_id=(SELECT id FROM customers WHERE email=?)
                       AND product_id=? AND is_active=1""", (now, reason, email, product_id))
    else:
        c.execute("""UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason=?
                     WHERE customer_id=(SELECT id FROM customers WHERE email=?)
                       AND is_active=1""", (now, reason, email))
    conn.commit()
    n = c.rowcount
    conn.close()
    return n

def mark_refunded(payment_ref):
    conn = get_conn()
    now = time.time()
    c = conn.cursor()
    c.execute("UPDATE payments SET is_refunded=1,refunded_at=? WHERE payment_ref=?", (now, payment_ref))
    if c.rowcount == 0:
        conn.close()
        return False
    c.execute("""UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason='refund'
                 WHERE payment_id=(SELECT id FROM payments WHERE payment_ref=?)""", (now, payment_ref))
    conn.commit()
    conn.close()
    return True

def admin_list(product_id=None):
    conn = get_conn()
    where = "WHERE l.product_id=?" if product_id else ""
    args  = (product_id,) if product_id else ()
    rows  = conn.execute(f"""
        SELECT l.product_id, cu.email, cu.country,
               p.source, p.amount, p.currency, p.plan,
               l.machine_id, l.is_active,
               l.paid_at, l.expires_at, l.last_seen_at,
               l.verify_count, l.revoke_reason
        FROM licenses l
        JOIN customers cu ON cu.id=l.customer_id
        JOIN payments  p  ON p.id =l.payment_id
        {where}
        ORDER BY l.paid_at DESC
    """, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def admin_product_summary():
    """Revenue + active licenses per product at a glance."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            pr.product_id, pr.name,
            COUNT(DISTINCT l.id)  AS total_licenses,
            SUM(l.is_active)      AS active_licenses,
            SUM(CASE WHEN p.currency='INR' AND p.is_refunded=0 THEN p.amount ELSE 0 END) AS revenue_inr,
            SUM(CASE WHEN p.currency='USD' AND p.is_refunded=0 THEN p.amount ELSE 0 END) AS revenue_usd
        FROM products pr
        LEFT JOIN licenses l ON l.product_id=pr.product_id
        LEFT JOIN payments p ON p.id=l.payment_id
        GROUP BY pr.product_id
        ORDER BY pr.created_at
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Helpers ───────────────────────────────────────────────────────────────────

def plan_expiry(plan, paid_at):
    if plan == "lifetime": return None
    if plan == "annual":   return paid_at + 365*86400
    if plan == "monthly":  return paid_at + 30*86400
    return None

def _log(conn, product_id, license_id, email, ip, result):
    try:
        conn.execute("INSERT INTO verify_log (product_id,license_id,email,ip_address,result,called_at) VALUES (?,?,?,?,?,?)",
                     (product_id, license_id, email, ip, result, time.time()))
        conn.commit()
        conn.execute("DELETE FROM verify_log WHERE called_at < ?", (time.time()-90*86400,))
        conn.commit()
    except: pass

# ── Seed / init ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    # Run once to add your products:
    add_product("TOOL1","Image Converter Pro",499,9.99,
                "https://rzp.io/l/tool1","gumroad_id_1","https://you.gumroad.com/l/tool1", max_machines=1)
    add_product("TOOL2","PDF Merger Pro",699,14.99,
                "https://rzp.io/l/tool2","gumroad_id_2","https://you.gumroad.com/l/tool2", max_machines=2)
    print("\nProducts registered:")
    conn = get_conn()
    for r in conn.execute("SELECT product_id,name,price_inr,price_usd,max_machines FROM products").fetchall():
        print(f"  [{r['product_id']}] {r['name']:25} ₹{r['price_inr']} / ${r['price_usd']}  max {r['max_machines']} PC")
    conn.close()
