"""
database.py — Multi-Product License Server
Tables:
  0. products    — one row per tool you sell
  1. email_otps  — one row per email (overwritten each OTP send)
  2. customers   — one row per verified buyer
  3. payments    — one row per transaction
  4. licenses    — one row per activated machine per product
  5. verify_log  — audit trail (auto-purged 90 days)
"""

import sqlite3, time, os, random, string

DB_PATH = os.environ.get("DB_PATH", "licenses.db")

SCHEMA = """

-- TABLE 0: products
CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          TEXT    NOT NULL UNIQUE,
    name                TEXT    NOT NULL,
    price_inr           REAL,
    price_usd           REAL,
    razorpay_link       TEXT,
    gumroad_product_id  TEXT,
    gumroad_link        TEXT,
    max_machines        INTEGER NOT NULL DEFAULT 1,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          REAL    NOT NULL
);

-- TABLE 1: email_otps
-- One row per email. Overwritten every time a new OTP is sent.
-- No history kept — only the latest OTP matters.
CREATE TABLE IF NOT EXISTS email_otps (
    email           TEXT    PRIMARY KEY,            -- email is the key
    otp             TEXT    NOT NULL,               -- 6-digit OTP
    sent_at         REAL    NOT NULL,               -- unix timestamp when sent
    attempts        INTEGER NOT NULL DEFAULT 0,     -- wrong attempts (max 5)
    verified        INTEGER NOT NULL DEFAULT 0      -- 1 = OTP was verified successfully
);

-- TABLE 2: customers
CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE,
    full_name       TEXT,
    country         TEXT,
    created_at      REAL    NOT NULL,
    updated_at      REAL
);

-- TABLE 3: payments
CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT    NOT NULL REFERENCES products(product_id),
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    source          TEXT    NOT NULL,
    payment_ref     TEXT    NOT NULL UNIQUE,
    amount          REAL    NOT NULL,
    currency        TEXT    NOT NULL,
    plan            TEXT    NOT NULL DEFAULT 'lifetime',
    paid_at         REAL    NOT NULL,
    is_refunded     INTEGER NOT NULL DEFAULT 0,
    refunded_at     REAL
);

-- TABLE 4: licenses
CREATE TABLE IF NOT EXISTS licenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT    NOT NULL REFERENCES products(product_id),
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    payment_id      INTEGER NOT NULL REFERENCES payments(id),
    unique_key      TEXT    NOT NULL UNIQUE,
    machine_id      TEXT    NOT NULL,
    machine_label   TEXT,
    plan            TEXT    NOT NULL DEFAULT 'lifetime',
    is_active       INTEGER NOT NULL DEFAULT 1,
    paid_at         REAL    NOT NULL,
    expires_at      REAL,
    activated_at    REAL    NOT NULL,
    last_seen_at    REAL,
    last_seen_ip    TEXT,
    verify_count    INTEGER NOT NULL DEFAULT 0,
    revoked_at      REAL,
    revoke_reason   TEXT
);

-- TABLE 5: verify_log
CREATE TABLE IF NOT EXISTS verify_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT,
    license_id      INTEGER REFERENCES licenses(id),
    email           TEXT,
    ip_address      TEXT,
    result          TEXT    NOT NULL,
    called_at       REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lic_unique_key   ON licenses(unique_key);
CREATE INDEX IF NOT EXISTS idx_lic_product      ON licenses(product_id);
CREATE INDEX IF NOT EXISTS idx_lic_customer     ON licenses(customer_id);
CREATE INDEX IF NOT EXISTS idx_lic_expires      ON licenses(expires_at);
CREATE INDEX IF NOT EXISTS idx_pay_product      ON payments(product_id);
CREATE INDEX IF NOT EXISTS idx_pay_customer     ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_vlog_called      ON verify_log(called_at);
"""

OTP_EXPIRY_SECONDS = 10 * 60   # 10 minutes
OTP_MAX_ATTEMPTS   = 5         # lock after 5 wrong tries

# ─────────────────────────────────────────────────────────────────
#  CONNECTION
# ─────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────
#  OTP
# ─────────────────────────────────────────────────────────────────

def generate_otp() -> str:
    """Generate a secure 6-digit numeric OTP."""
    return ''.join(random.choices(string.digits, k=6))

def store_otp(email: str, otp: str) -> bool:
    """
    Save OTP for email. Overwrites any existing row.
    Resets attempts and verified flag.
    Called by: send_otp endpoint
    """
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO email_otps (email, otp, sent_at, attempts, verified)
            VALUES (?, ?, ?, 0, 0)
            ON CONFLICT(email) DO UPDATE SET
                otp      = excluded.otp,
                sent_at  = excluded.sent_at,
                attempts = 0,
                verified = 0
        """, (email.lower().strip(), otp, time.time()))
        conn.commit()
        return True
    except Exception as e:
        print(f"store_otp error: {e}")
        return False
    finally:
        conn.close()

def is_valid_otp(email: str, otp: str) -> dict:
    """
    Validate OTP for email.
    Returns: { ok, reason }
      reasons: not_found | expired | max_attempts | invalid | verified
    Increments attempt counter on wrong OTP.
    Marks verified=1 on success.
    """
    email = email.lower().strip()
    conn  = get_conn()
    try:
        row = conn.execute(
            "SELECT otp, sent_at, attempts, verified FROM email_otps WHERE email=?",
            (email,)
        ).fetchone()

        if not row:
            return {"ok": False, "reason": "otp_not_found"}

        # Already verified — allow through (idempotent)
        if row["verified"]:
            return {"ok": True, "reason": "already_verified"}

        # Expired?
        if time.time() - row["sent_at"] > OTP_EXPIRY_SECONDS:
            return {"ok": False, "reason": "otp_expired"}

        # Too many wrong attempts?
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            return {"ok": False, "reason": "max_attempts_exceeded"}

        # Wrong OTP — increment counter
        if row["otp"] != otp.strip():
            conn.execute(
                "UPDATE email_otps SET attempts = attempts + 1 WHERE email=?",
                (email,)
            )
            conn.commit()
            attempts_left = OTP_MAX_ATTEMPTS - row["attempts"] - 1
            return {"ok": False, "reason": "invalid_otp", "attempts_left": attempts_left}

        # Correct OTP — mark verified
        conn.execute(
            "UPDATE email_otps SET verified=1 WHERE email=?",
            (email,)
        )
        conn.commit()
        return {"ok": True, "reason": "verified"}

    finally:
        conn.close()

def is_email_verified(email: str) -> bool:
    """
    Quick check: has this email passed OTP verification recently?
    Used before allowing registration.
    """
    email = email.lower().strip()
    conn  = get_conn()
    row   = conn.execute(
        "SELECT verified, sent_at FROM email_otps WHERE email=?", (email,)
    ).fetchone()
    conn.close()
    if not row:
        return False
    if not row["verified"]:
        return False
    # Verification must be recent (within 30 minutes of OTP send)
    if time.time() - row["sent_at"] > 30 * 60:
        return False
    return True

# ─────────────────────────────────────────────────────────────────
#  PRODUCTS
# ─────────────────────────────────────────────────────────────────

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
        """, (product_id, name, price_inr, price_usd, razorpay_link,
              gumroad_product_id, gumroad_link, max_machines, time.time()))
        conn.commit()
        return True
    except Exception as e:
        print(f"add_product error: {e}")
        return False
    finally:
        conn.close()

def get_product(product_id):
    conn = get_conn()
    row  = conn.execute(
        "SELECT * FROM products WHERE product_id=? AND is_active=1", (product_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

# ─────────────────────────────────────────────────────────────────
#  REGISTRATION
# ─────────────────────────────────────────────────────────────────

def register_license(product_id, email, machine_id, unique_key,
                     source, payment_ref, amount, currency,
                     plan="lifetime", full_name=None, country=None,
                     ip_address=None):
    conn = get_conn()
    now  = time.time()
    try:
        c = conn.cursor()

        # Validate product
        c.execute("SELECT max_machines FROM products WHERE product_id=? AND is_active=1",
                  (product_id,))
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
            c.execute("""INSERT INTO customers
                         (email,full_name,country,created_at,updated_at)
                         VALUES (?,?,?,?,?)""",
                      (email, full_name, country, now, now))
            customer_id = c.lastrowid

        # Reject duplicate payment_ref
        c.execute("SELECT id FROM payments WHERE payment_ref=?", (payment_ref,))
        if c.fetchone():
            return {"ok": False, "reason": "payment_already_used"}

        # Check machine count
        c.execute("""SELECT COUNT(*) AS cnt FROM licenses
                     WHERE customer_id=? AND product_id=? AND is_active=1""",
                  (customer_id, product_id))
        if c.fetchone()["cnt"] >= prod["max_machines"]:
            return {"ok": False, "reason": f"max_machines_reached ({prod['max_machines']})"}

        # Insert payment
        c.execute("""INSERT INTO payments
                     (product_id,customer_id,source,payment_ref,
                      amount,currency,plan,paid_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (product_id, customer_id, source, payment_ref,
                   amount, currency, plan, now))
        payment_db_id = c.lastrowid

        # Check existing license
        c.execute("SELECT id, is_active FROM licenses WHERE unique_key=?", (unique_key,))
        existing = c.fetchone()
        if existing:
            return {"ok": True,  "reason": "already_registered"} if existing["is_active"] \
                   else {"ok": False, "reason": "license_revoked"}

        # Insert license
        c.execute("""INSERT INTO licenses
                     (product_id,customer_id,payment_id,unique_key,machine_id,
                      plan,paid_at,expires_at,activated_at,last_seen_at,
                      last_seen_ip,verify_count)
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

# ─────────────────────────────────────────────────────────────────
#  VERIFICATION (EXE launch check)
# ─────────────────────────────────────────────────────────────────

def verify_license(product_id, unique_key, ip_address=None):
    conn = get_conn()
    now  = time.time()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT l.id, l.is_active, l.expires_at, l.product_id, cu.email
            FROM licenses l
            JOIN customers cu ON cu.id = l.customer_id
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

        c.execute("""UPDATE licenses
                     SET last_seen_at=?, last_seen_ip=?, verify_count=verify_count+1
                     WHERE id=?""",
                  (now, ip_address, row["id"]))
        conn.commit()
        _log(conn, product_id, row["id"], row["email"], ip_address, "ok")
        return {"ok": True, "reason": "ok"}
    finally:
        conn.close()

# ─────────────────────────────────────────────────────────────────
#  ADMIN
# ─────────────────────────────────────────────────────────────────

def revoke_license(email, product_id=None, reason="manual"):
    conn = get_conn()
    now  = time.time()
    c    = conn.cursor()
    if product_id:
        c.execute("""UPDATE licenses SET is_active=0, revoked_at=?, revoke_reason=?
                     WHERE customer_id=(SELECT id FROM customers WHERE email=?)
                       AND product_id=? AND is_active=1""",
                  (now, reason, email, product_id))
    else:
        c.execute("""UPDATE licenses SET is_active=0, revoked_at=?, revoke_reason=?
                     WHERE customer_id=(SELECT id FROM customers WHERE email=?)
                       AND is_active=1""",
                  (now, reason, email))
    conn.commit()
    n = c.rowcount
    conn.close()
    return n

def mark_refunded(payment_ref):
    conn = get_conn()
    now  = time.time()
    c    = conn.cursor()
    c.execute("UPDATE payments SET is_refunded=1, refunded_at=? WHERE payment_ref=?",
              (now, payment_ref))
    if c.rowcount == 0:
        conn.close()
        return False
    c.execute("""UPDATE licenses SET is_active=0, revoked_at=?, revoke_reason='refund'
                 WHERE payment_id=(SELECT id FROM payments WHERE payment_ref=?)""",
              (now, payment_ref))
    conn.commit()
    conn.close()
    return True

# ─────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────

def plan_expiry(plan, paid_at):
    if plan == "lifetime": return None
    if plan == "annual":   return paid_at + 365 * 86400
    if plan == "monthly":  return paid_at + 30  * 86400
    return None

def _log(conn, product_id, license_id, email, ip, result):
    try:
        conn.execute("""INSERT INTO verify_log
                        (product_id,license_id,email,ip_address,result,called_at)
                        VALUES (?,?,?,?,?,?)""",
                     (product_id, license_id, email, ip, result, time.time()))
        conn.commit()
        # Auto-purge logs older than 90 days
        conn.execute("DELETE FROM verify_log WHERE called_at < ?",
                     (time.time() - 90 * 86400,))
        conn.commit()
    except:
        pass

# ─────────────────────────────────────────────────────────────────
#  SEED (run once)
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    add_product("TOOL1", "Image Converter Pro", 499, 9.99,
                "https://rzp.io/l/tool1", "gumroad_id_1",
                "https://you.gumroad.com/l/tool1", max_machines=1)
    add_product("TOOL2", "PDF Merger Pro", 699, 14.99,
                "https://rzp.io/l/tool2", "gumroad_id_2",
                "https://you.gumroad.com/l/tool2", max_machines=2)
    print("\nDatabase initialized. Products registered:")
    conn = get_conn()
    for r in conn.execute(
        "SELECT product_id,name,price_inr,price_usd,max_machines FROM products"
    ).fetchall():
        print(f"  [{r['product_id']}] {r['name']:25} "
              f"₹{r['price_inr']} / ${r['price_usd']}  max {r['max_machines']} PC")
    conn.close()