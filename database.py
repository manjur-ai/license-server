"""
database.py — Multi-provider database layer for the License Server.

Set DATABASE_INFO environment variable (JSON) to choose your provider:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SQLITE (local file — with optional Google Drive backup + auto-restore):
  DATABASE_INFO = {
    "provider":        "sqlite",
    "path":            "licenses.db",
    "backup_gdrive":   true,
    "backup_every_n":  10,
    "gdrive_folder_id": "your_google_drive_folder_id"
  }
  Also set: GDRIVE_CREDENTIALS_JSON = service account JSON contents

  How backup works:
    Every N-th register() call writes 2 files to Google Drive:
      licenses_LIVE.db                  ← always overwritten (used for restore)
      licenses_PREV_YYYYMMDD_HHMMSS.db  ← new timestamped file every cycle (full history)
    Runs in background thread — never blocks requests.
    Drive grows by one PREV_* file per backup cycle.

  How restore works (on server startup):
    - licenses.db exists AND has real licenses → use it, skip restore.
    - licenses.db missing, empty, or corrupt  → download licenses_LIVE.db from Drive.
    - LIVE missing or corrupt                 → CRITICAL warning, start fresh.
    - After restore, schema migration runs (CREATE IF NOT EXISTS — safe, never wipes data).

TURSO (hosted SQLite — survives redeploys, free tier):
  DATABASE_INFO = {"provider": "turso",
                   "url": "libsql://your-db.turso.io",
                   "token": "your-turso-auth-token"}

RAILWAY / SUPABASE PostgreSQL:
  DATABASE_INFO = {"provider": "postgresql",
                   "url": "postgresql://user:pass@host:5432/dbname"}

PLANETSCALE (MySQL):
  DATABASE_INFO = {"provider": "mysql",
                   "host": "aws.connect.psdb.cloud",
                   "port": 3306,
                   "user": "your-user",
                   "password": "your-password",
                   "database": "your-db",
                   "ssl": true}

MONGODB (Atlas or self-hosted):
  DATABASE_INFO = {"provider": "mongodb",
                   "url": "mongodb+srv://user:pass@cluster.mongodb.net/dbname",
                   "database": "licenses"}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

All providers implement the same public API:
  init_db()
  generate_otp() / store_otp() / is_valid_otp() / is_identity_verified()
  add_product() / get_product()
  register_license() / verify_license()
  revoke_license() / mark_refunded()
  normalize_identity()
"""

import os, json, time, random, string, threading

# ── Load DATABASE_INFO from env ───────────────────────────────────────────────

def _load_db_info() -> dict:
    raw = os.environ.get("DATABASE_INFO", "")
    if not raw:
        # Fallback: legacy DB_PATH env var → sqlite
        return {"provider": "sqlite", "path": os.environ.get("DB_PATH", "licenses.db")}
    try:
        info = json.loads(raw)
        provider = info.get("provider", "sqlite").lower()
        print(f"  [DB] Provider: {provider.upper()}")
        return info
    except json.JSONDecodeError as e:
        print(f"  [DB] DATABASE_INFO parse error: {e} — falling back to SQLite")
        return {"provider": "sqlite", "path": "licenses.db"}

DATABASE_INFO = _load_db_info()
_PROVIDER     = DATABASE_INFO.get("provider", "sqlite").lower()

OTP_EXPIRY_SECONDS = 10 * 60
OTP_MAX_ATTEMPTS   = 5

# ─────────────────────────────────────────────────────────────────
#  IDENTITY HELPERS  (shared across all providers)
# ─────────────────────────────────────────────────────────────────

def normalize_identity(identity: str, identity_type: str) -> str:
    identity = identity.strip()
    if identity_type == "email":
        return identity.lower()
    if identity_type == "sms":
        phone = identity.replace(" ", "").replace("-", "")
        if not phone.startswith("+"):
            phone = "+" + phone
        return phone
    return identity

def generate_otp() -> str:
    return ''.join(random.choices(string.digits, k=6))

def plan_expiry(plan, paid_at):
    if plan == "lifetime": return None
    if plan == "annual":   return paid_at + 365 * 86400
    if plan == "monthly":  return paid_at + 30  * 86400
    return None

# ═════════════════════════════════════════════════════════════════
#  PROVIDER: SQLITE  (local file + Google Drive backup/restore)
# ═════════════════════════════════════════════════════════════════
#
#  Every backup cycle writes 2 files to Google Drive:
#    licenses_LIVE.db                  ← always overwritten (fast restore target)
#    licenses_PREV_YYYYMMDD_HHMMSS.db  ← new timestamped file (full history)
#
#  Restore sequence on startup:
#    1. licenses.db exists AND has licenses → use as-is (normal restart / Railway restart)
#    2. licenses.db missing, empty, corrupt → download licenses_LIVE.db from Drive
#    3. LIVE missing or corrupt             → CRITICAL warning, start fresh
#

_GDRIVE_LIVE_NAME = "licenses_LIVE.db"
# PREV files are named licenses_PREV_YYYYMMDD_HHMMSS.db — see _gdrive_upload()

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, price_inr REAL, price_usd REAL,
    razorpay_link TEXT, gumroad_product_id TEXT, gumroad_link TEXT,
    max_machines INTEGER NOT NULL DEFAULT 1, is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_otps (
    identity TEXT NOT NULL, identity_type TEXT NOT NULL DEFAULT 'email',
    otp TEXT NOT NULL, sent_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity, identity_type)
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity TEXT NOT NULL, identity_type TEXT NOT NULL DEFAULT 'email',
    email TEXT, phone TEXT, full_name TEXT, country TEXT,
    created_at REAL NOT NULL, updated_at REAL,
    UNIQUE(identity, identity_type)
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
    source TEXT NOT NULL, payment_ref TEXT NOT NULL UNIQUE,
    amount REAL NOT NULL, currency TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'lifetime',
    paid_at REAL NOT NULL, is_refunded INTEGER NOT NULL DEFAULT 0, refunded_at REAL
);
CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
    payment_id INTEGER NOT NULL, unique_key TEXT NOT NULL UNIQUE,
    machine_id TEXT NOT NULL, machine_label TEXT,
    plan TEXT NOT NULL DEFAULT 'lifetime', is_active INTEGER NOT NULL DEFAULT 1,
    paid_at REAL NOT NULL, expires_at REAL, activated_at REAL NOT NULL,
    last_seen_at REAL, last_seen_ip TEXT,
    verify_count INTEGER NOT NULL DEFAULT 0, revoked_at REAL, revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS verify_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT,
    license_id INTEGER, identity TEXT, ip_address TEXT,
    result TEXT NOT NULL, called_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lic_ukey  ON licenses(unique_key);
CREATE INDEX IF NOT EXISTS idx_lic_prod  ON licenses(product_id);
CREATE INDEX IF NOT EXISTS idx_lic_cust  ON licenses(customer_id);
CREATE INDEX IF NOT EXISTS idx_otp_ident ON identity_otps(identity, identity_type);
CREATE INDEX IF NOT EXISTS idx_vlog_time ON verify_log(called_at);
"""

# ── Google Drive service builder ───────────────────────────────────────────────

def _gdrive_service():
    """
    Build and return an authenticated Google Drive service object.
    Raises ImportError if google libs not installed.
    Raises ValueError if GDRIVE_CREDENTIALS_JSON not set.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    creds_json = os.environ.get("GDRIVE_CREDENTIALS_JSON", "")
    if not creds_json:
        raise ValueError("GDRIVE_CREDENTIALS_JSON not set")
    creds = Credentials.from_service_account_info(
                json.loads(creds_json),
                scopes=["https://www.googleapis.com/auth/drive.file"])
    return build("drive", "v3", credentials=creds)

def _gdrive_find_file(service, name: str, folder_id: str) -> str:
    """Return Drive file ID for given filename in folder, or None if not found."""
    q = f"name='{name}' and trashed=false"
    if folder_id:
        q += f" and '{folder_id}' in parents"
    res = service.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None

# ── Startup restore ────────────────────────────────────────────────────────────

def _sqlite_needs_restore(path: str) -> bool:
    """
    Return True if the DB file should be replaced by a Drive backup.

    Decision logic:
      - File missing                    → True  (redeploy wiped it)
      - File unreadable / corrupt       → True
      - licenses table missing          → True  (blank schema)
      - licenses table exists, count>0  → False (real data, keep it)
      - licenses table exists, count=0  → True  (no customers ever registered)
        NOTE: we use licenses (not customers/products) as the signal because:
          • products are seeded at every deploy via add_product() — count>0 always
          • customers row is created at register time, same as licenses
          • licenses is the single source of truth that real users exist
    """
    if not os.path.exists(path):
        return True
    if os.path.getsize(path) == 0:
        return True
    try:
        import sqlite3
        conn  = sqlite3.connect(path)
        # Check the licenses table exists
        has_table = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='licenses'"
        ).fetchone()[0]
        if not has_table:
            conn.close(); return True
        count = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        conn.close()
        return count == 0   # no licenses = no real customers = restore from Drive
    except Exception:
        return True         # corrupt file → restore

def _sqlite_restore_from_gdrive(path: str) -> bool:
    """
    Download licenses_LIVE.db from Google Drive and restore it.
    Returns True if restore succeeded, False otherwise.

    Only LIVE is used for restore — PREV_* files are history only.
    If LIVE is missing or corrupt → logs CRITICAL, returns False → fresh start.
    """
    backup_enabled = DATABASE_INFO.get("backup_gdrive", False)
    folder_id      = DATABASE_INFO.get("gdrive_folder_id", "")

    if not backup_enabled:
        print("  [RESTORE] backup_gdrive=false — skipping restore attempt")
        return False

    try:
        from googleapiclient.http import MediaIoBaseDownload
        service = _gdrive_service()
    except ImportError:
        print("  [RESTORE] google-api-python-client not installed — cannot restore")
        return False
    except ValueError as e:
        print(f"  [RESTORE] {e} — cannot restore")
        return False

    print(f"  [RESTORE] Looking for {_GDRIVE_LIVE_NAME} in Google Drive ...")
    try:
        file_id = _gdrive_find_file(service, _GDRIVE_LIVE_NAME, folder_id)
        if not file_id:
            print(f"  [RESTORE] {_GDRIVE_LIVE_NAME} not found — no backup exists yet")
            print(f"  [RESTORE] Starting fresh (first ever deploy or Drive not configured)")
            return False

        # Download to temp file first, verify, then move into place
        tmp_path = path + ".restore_tmp"
        request  = service.files().get_media(fileId=file_id)
        with open(tmp_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        # Verify downloaded file is a valid SQLite DB with real license data
        import sqlite3 as _sq3
        try:
            _conn  = _sq3.connect(tmp_path)
            count  = _conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
            ccount = _conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            _conn.close()
            if count == 0:
                print(f"  [RESTORE] {_GDRIVE_LIVE_NAME} has 0 licenses — "
                      f"backup exists but no customers yet, starting fresh")
                os.remove(tmp_path)
                return False
        except Exception as e:
            print(f"  [RESTORE] CRITICAL: {_GDRIVE_LIVE_NAME} is corrupt ({e})")
            print(f"  [RESTORE] Recover manually from a licenses_PREV_*.db file in Drive")
            if os.path.exists(tmp_path): os.remove(tmp_path)
            return False

        # Good — move into place (keep old file as emergency backup)
        if os.path.exists(path):
            os.replace(path, path + ".pre_restore_bak")
        os.replace(tmp_path, path)
        print(f"  [RESTORE] ✓ Restored {_GDRIVE_LIVE_NAME} — "
              f"{count} licenses, {ccount} customers recovered")
        return True

    except Exception as e:
        print(f"  [RESTORE] CRITICAL: Download failed: {e}")
        print(f"  [RESTORE] Check GDRIVE_CREDENTIALS_JSON and gdrive_folder_id")
        return False

# ── SQLite init (entry point) ──────────────────────────────────────────────────

def _sqlite_init():
    import sqlite3
    global _sqlite_path
    _sqlite_path = DATABASE_INFO.get("path", os.environ.get("DB_PATH", "licenses.db"))

    # ── STEP 1: Restore from Drive if DB is blank/missing ──────────────────
    if _sqlite_needs_restore(_sqlite_path):
        print(f"  [DB] licenses.db is blank or missing — attempting restore ...")
        restored = _sqlite_restore_from_gdrive(_sqlite_path)
        if not restored:
            print(f"  [DB] Starting fresh (no backup available or backup_gdrive=false)")
    else:
        print(f"  [DB] licenses.db exists with data — using as-is")

    # ── STEP 2: Apply schema (CREATE IF NOT EXISTS — never wipes data) ──────
    conn = sqlite3.connect(_sqlite_path)
    conn.executescript(_SQLITE_SCHEMA)
    conn.commit()
    conn.close()
    print(f"  [DB] SQLite ready: {_sqlite_path}")

def _sqlite_conn():
    import sqlite3
    conn = sqlite3.connect(_sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

# ── Two independent backup counters ───────────────────────────────────────────
#
#  backup_live_every_n  (default 3)
#    → overwrite licenses_LIVE.db every N registrations
#    → keeps LIVE fresh for fast restore
#
#  backup_hist_every_n  (default 10)
#    → create licenses_PREV_timestamp.db every N registrations
#    → permanent history, never overwritten
#
#  Both counters are independent. Example with live=3, hist=10:
#    reg #1  → nothing
#    reg #2  → nothing
#    reg #3  → LIVE updated
#    reg #6  → LIVE updated
#    reg #9  → LIVE updated
#    reg #10 → LIVE updated + PREV_timestamp created
#    reg #12 → LIVE updated
#    reg #20 → LIVE updated + PREV_timestamp created
#

_live_counter = 0
_hist_counter = 0
_backup_lock  = threading.Lock()

def _sqlite_maybe_backup():
    """
    Called after every successful register().
    Checks both counters and triggers appropriate background uploads.
    No-op if backup_gdrive=false or provider is not sqlite.
    """
    if _PROVIDER != "sqlite": return
    if not DATABASE_INFO.get("backup_gdrive", False): return

    live_every = int(DATABASE_INFO.get("backup_live_every_n",
                     DATABASE_INFO.get("backup_every_n", 3)))   # legacy fallback
    hist_every = int(DATABASE_INFO.get("backup_hist_every_n", 10))

    global _live_counter, _hist_counter
    do_live = do_hist = False

    with _backup_lock:
        _live_counter += 1
        _hist_counter += 1
        if _live_counter >= live_every:
            _live_counter = 0
            do_live = True
        if _hist_counter >= hist_every:
            _hist_counter = 0
            do_hist = True

    if do_live or do_hist:
        threading.Thread(
            target=_gdrive_upload,
            args=(do_live, do_hist),
            daemon=True
        ).start()

# ── Google Drive upload ────────────────────────────────────────────────────────

def _gdrive_upload(upload_live: bool = True, upload_hist: bool = True):
    """
    Upload current licenses.db snapshot to Google Drive.

    upload_live=True  → overwrite licenses_LIVE.db  (fast restore target)
    upload_hist=True  → create  licenses_PREV_YYYYMMDD_HHMMSS.db (history)

    Called automatically by _sqlite_maybe_backup() in a background thread.
    Also called directly by backup_db() for a manual / pre-redeploy backup
    (both flags True by default in that case).

    Snapshot is taken via SQLite's online backup API — atomic, consistent,
    never blocks active write transactions.
    """
    if not upload_live and not upload_hist:
        return {"ok": True, "msg": "nothing to do"}

    folder_id = DATABASE_INFO.get("gdrive_folder_id", "")
    tmp_snap  = _sqlite_path + ".snap"
    snap_conn = None
    results   = {}

    try:
        from googleapiclient.http import MediaFileUpload
        service = _gdrive_service()
    except ImportError:
        msg = ("google-api-python-client not installed — "
               "run: pip install google-api-python-client google-auth --break-system-packages")
        print(f"  [BACKUP] {msg}")
        return {"ok": False, "error": msg}
    except ValueError as e:
        print(f"  [BACKUP] {e}")
        return {"ok": False, "error": str(e)}

    try:
        # ── Consistent snapshot ──────────────────────────────────────────────
        import sqlite3
        src       = sqlite3.connect(_sqlite_path)
        dst       = sqlite3.connect(":memory:")
        src.backup(dst)
        src.close()
        snap_conn = sqlite3.connect(tmp_snap)
        dst.backup(snap_conn)
        dst.close()
        snap_conn.close()
        snap_conn = None

        # ── LIVE: overwrite licenses_LIVE.db ────────────────────────────────
        if upload_live:
            live_media = MediaFileUpload(tmp_snap,
                                         mimetype="application/octet-stream",
                                         resumable=False)
            live_id = _gdrive_find_file(service, _GDRIVE_LIVE_NAME, folder_id)
            if live_id:
                service.files().update(fileId=live_id,
                                       media_body=live_media).execute()
            else:
                meta = {"name": _GDRIVE_LIVE_NAME}
                if folder_id: meta["parents"] = [folder_id]
                service.files().create(body=meta,
                                       media_body=live_media,
                                       fields="id").execute()
            print(f"  [BACKUP] ✓ {_GDRIVE_LIVE_NAME} updated")
            results["live"] = _GDRIVE_LIVE_NAME

        # ── HIST: create licenses_PREV_YYYYMMDD_HHMMSS.db ───────────────────
        if upload_hist:
            ts_str    = time.strftime("%Y%m%d_%H%M%S")
            prev_name = f"licenses_PREV_{ts_str}.db"
            prev_media = MediaFileUpload(tmp_snap,
                                         mimetype="application/octet-stream",
                                         resumable=False)
            meta = {"name": prev_name}
            if folder_id: meta["parents"] = [folder_id]
            service.files().create(body=meta,
                                   media_body=prev_media,
                                   fields="id").execute()
            print(f"  [BACKUP] ✓ {prev_name} created  (folder: {folder_id or 'root'})")
            results["hist"] = prev_name

        return {"ok": True, **results}

    except Exception as e:
        print(f"  [BACKUP] Google Drive upload failed: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if snap_conn:
            try: snap_conn.close()
            except: pass
        try: os.remove(tmp_snap)
        except: pass

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — backup_db  (manual / pre-redeploy backup)
# ═════════════════════════════════════════════════════════════════

def backup_db(upload_live: bool = True, upload_hist: bool = True) -> dict:
    """
    Trigger an immediate full backup right now.
    Runs synchronously (blocks until upload is done) — call before redeploy.

    Returns dict:
      { ok: True,  live: "licenses_LIVE.db", hist: "licenses_PREV_....db" }
      { ok: False, error: "reason" }

    upload_live: overwrite licenses_LIVE.db  (default True)
    upload_hist: create   licenses_PREV_timestamp.db  (default True)

    Currently implemented for: sqlite (with backup_gdrive=true)
    Future providers (postgresql, mysql, mongodb) will export a dump here.
    """
    if _PROVIDER == "sqlite":
        if not DATABASE_INFO.get("backup_gdrive", False):
            return {"ok": False, "error": "backup_gdrive is false — enable it in DATABASE_INFO"}
        # Run synchronously (not in background thread) so caller gets result
        return _gdrive_upload(upload_live=upload_live, upload_hist=upload_hist)

    # Future providers:
    # elif _PROVIDER == "postgresql":
    #     return _pg_dump_to_gdrive()
    # elif _PROVIDER == "mysql":
    #     return _mysql_dump_to_gdrive()
    # elif _PROVIDER == "mongodb":
    #     return _mongo_export_to_gdrive()

    return {"ok": False, "error": f"backup_db not yet implemented for provider: {_PROVIDER}"}

# ═════════════════════════════════════════════════════════════════
#  PROVIDER: TURSO  (hosted SQLite via libsql)
# ═════════════════════════════════════════════════════════════════

def _turso_init():
    """Turso uses the same SQLite schema but connects via libsql protocol."""
    global _turso_url, _turso_token
    _turso_url   = DATABASE_INFO.get("url", "")
    _turso_token = DATABASE_INFO.get("token", "")
    if not _turso_url or not _turso_token:
        raise ValueError("Turso requires 'url' and 'token' in DATABASE_INFO")

    # Turso supports libsql:// and https:// URLs
    # We use the HTTP API for simplicity (works without native driver issues)
    print(f"  [DB] Turso ready: {_turso_url}")
    _turso_execute_batch(_SQLITE_SCHEMA_DDL)

def _turso_execute(sql: str, params: list = None) -> list:
    """Execute SQL on Turso via HTTP API. Returns list of row dicts."""
    import requests as req
    url     = _turso_url.replace("libsql://", "https://")
    headers = {"Authorization": f"Bearer {_turso_token}",
               "Content-Type": "application/json"}
    stmt    = {"type": "execute", "stmt": {"sql": sql}}
    if params:
        stmt["stmt"]["args"] = [_turso_val(p) for p in params]
    body = {"requests": [stmt, {"type": "close"}]}
    r    = req.post(f"{url}/v2/pipeline", headers=headers, json=body, timeout=15)
    r.raise_for_status()
    result = r.json()["results"][0]
    if result.get("type") == "error":
        raise Exception(f"Turso error: {result['error']}")
    rows_data = result.get("response", {}).get("result", {})
    cols      = [c["name"] for c in rows_data.get("cols", [])]
    rows      = []
    for row in rows_data.get("rows", []):
        rows.append({cols[i]: _turso_from_val(row[i]) for i in range(len(cols))})
    return rows

def _turso_execute_batch(sql: str):
    """Execute multiple DDL statements on Turso."""
    stmts = [s.strip() for s in sql.split(";") if s.strip()]
    import requests as req
    url     = _turso_url.replace("libsql://", "https://")
    headers = {"Authorization": f"Bearer {_turso_token}",
               "Content-Type": "application/json"}
    requests_body = [{"type": "execute", "stmt": {"sql": s}} for s in stmts]
    requests_body.append({"type": "close"})
    req.post(f"{url}/v2/pipeline", headers=headers,
             json={"requests": requests_body}, timeout=30)

def _turso_val(v):
    if v is None:                         return {"type": "null"}
    if isinstance(v, bool):               return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):                return {"type": "integer", "value": str(v)}
    if isinstance(v, float):              return {"type": "float",   "value": v}
    return                                       {"type": "text",    "value": str(v)}

def _turso_from_val(v):
    if v is None or (isinstance(v, dict) and v.get("type") == "null"): return None
    if isinstance(v, dict):
        t = v.get("type", "text")
        val = v.get("value")
        if t == "integer": return int(val) if val is not None else None
        if t == "float":   return float(val) if val is not None else None
        return val
    return v

# Shared DDL for both SQLite and Turso
_SQLITE_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY, product_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, price_inr REAL, price_usd REAL,
    razorpay_link TEXT, gumroad_product_id TEXT, gumroad_link TEXT,
    max_machines INTEGER NOT NULL DEFAULT 1, is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_otps (
    identity TEXT NOT NULL, identity_type TEXT NOT NULL DEFAULT 'email',
    otp TEXT NOT NULL, sent_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity, identity_type)
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY, identity TEXT NOT NULL,
    identity_type TEXT NOT NULL DEFAULT 'email',
    email TEXT, phone TEXT, full_name TEXT, country TEXT,
    created_at REAL NOT NULL, updated_at REAL,
    UNIQUE(identity, identity_type)
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY, product_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
    source TEXT NOT NULL, payment_ref TEXT NOT NULL UNIQUE,
    amount REAL NOT NULL, currency TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'lifetime',
    paid_at REAL NOT NULL, is_refunded INTEGER NOT NULL DEFAULT 0, refunded_at REAL
);
CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY, product_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
    payment_id INTEGER NOT NULL, unique_key TEXT NOT NULL UNIQUE,
    machine_id TEXT NOT NULL, machine_label TEXT,
    plan TEXT NOT NULL DEFAULT 'lifetime', is_active INTEGER NOT NULL DEFAULT 1,
    paid_at REAL NOT NULL, expires_at REAL, activated_at REAL NOT NULL,
    last_seen_at REAL, last_seen_ip TEXT,
    verify_count INTEGER NOT NULL DEFAULT 0, revoked_at REAL, revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS verify_log (
    id INTEGER PRIMARY KEY, product_id TEXT, license_id INTEGER,
    identity TEXT, ip_address TEXT, result TEXT NOT NULL, called_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lic_ukey  ON licenses(unique_key);
CREATE INDEX IF NOT EXISTS idx_lic_prod  ON licenses(product_id);
CREATE INDEX IF NOT EXISTS idx_otp_ident ON identity_otps(identity, identity_type);
CREATE INDEX IF NOT EXISTS idx_vlog_time ON verify_log(called_at)
"""

# ═════════════════════════════════════════════════════════════════
#  PROVIDER: POSTGRESQL  (Railway, Supabase, or any Postgres)
# ═════════════════════════════════════════════════════════════════

def _pg_init():
    global _pg_url
    _pg_url = DATABASE_INFO.get("url", os.environ.get("DATABASE_URL", ""))
    if not _pg_url:
        raise ValueError("PostgreSQL requires 'url' in DATABASE_INFO")

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY, product_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, price_inr REAL, price_usd REAL,
        razorpay_link TEXT, gumroad_product_id TEXT, gumroad_link TEXT,
        max_machines INTEGER NOT NULL DEFAULT 1, is_active INTEGER NOT NULL DEFAULT 1,
        created_at DOUBLE PRECISION NOT NULL
    );
    CREATE TABLE IF NOT EXISTS identity_otps (
        identity TEXT NOT NULL, identity_type TEXT NOT NULL DEFAULT 'email',
        otp TEXT NOT NULL, sent_at DOUBLE PRECISION NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (identity, identity_type)
    );
    CREATE TABLE IF NOT EXISTS customers (
        id SERIAL PRIMARY KEY, identity TEXT NOT NULL,
        identity_type TEXT NOT NULL DEFAULT 'email',
        email TEXT, phone TEXT, full_name TEXT, country TEXT,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION,
        UNIQUE(identity, identity_type)
    );
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY, product_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
        source TEXT NOT NULL, payment_ref TEXT NOT NULL UNIQUE,
        amount REAL NOT NULL, currency TEXT NOT NULL,
        plan TEXT NOT NULL DEFAULT 'lifetime',
        paid_at DOUBLE PRECISION NOT NULL,
        is_refunded INTEGER NOT NULL DEFAULT 0, refunded_at DOUBLE PRECISION
    );
    CREATE TABLE IF NOT EXISTS licenses (
        id SERIAL PRIMARY KEY, product_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
        payment_id INTEGER NOT NULL, unique_key TEXT NOT NULL UNIQUE,
        machine_id TEXT NOT NULL, machine_label TEXT,
        plan TEXT NOT NULL DEFAULT 'lifetime', is_active INTEGER NOT NULL DEFAULT 1,
        paid_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION,
        activated_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION, last_seen_ip TEXT,
        verify_count INTEGER NOT NULL DEFAULT 0,
        revoked_at DOUBLE PRECISION, revoke_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS verify_log (
        id SERIAL PRIMARY KEY, product_id TEXT, license_id INTEGER,
        identity TEXT, ip_address TEXT, result TEXT NOT NULL,
        called_at DOUBLE PRECISION NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_lic_ukey  ON licenses(unique_key);
    CREATE INDEX IF NOT EXISTS idx_lic_prod  ON licenses(product_id);
    CREATE INDEX IF NOT EXISTS idx_otp_ident ON identity_otps(identity, identity_type);
    CREATE INDEX IF NOT EXISTS idx_vlog_time ON verify_log(called_at);
    """
    conn = _pg_conn()
    cur  = conn.cursor()
    cur.execute(SCHEMA)
    conn.commit()
    conn.close()
    print(f"  [DB] PostgreSQL ready")

def _pg_conn():
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(_pg_url)
    conn.autocommit = False
    return conn

def _pg_rows(cur) -> list:
    """Convert psycopg2 cursor result to list of dicts."""
    if cur.description is None:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

# ═════════════════════════════════════════════════════════════════
#  PROVIDER: MYSQL  (PlanetScale or any MySQL)
# ═════════════════════════════════════════════════════════════════

def _mysql_init():
    global _mysql_cfg
    _mysql_cfg = {
        "host":     DATABASE_INFO.get("host", ""),
        "port":     int(DATABASE_INFO.get("port", 3306)),
        "user":     DATABASE_INFO.get("user", ""),
        "password": DATABASE_INFO.get("password", ""),
        "database": DATABASE_INFO.get("database", ""),
        "ssl_disabled": not DATABASE_INFO.get("ssl", True),
    }
    if not _mysql_cfg["host"] or not _mysql_cfg["user"]:
        raise ValueError("MySQL requires host, user, password, database in DATABASE_INFO")

    # PlanetScale requires SSL
    if DATABASE_INFO.get("ssl", True):
        _mysql_cfg["ssl_ca"] = "/etc/ssl/certs/ca-certificates.crt"
        del _mysql_cfg["ssl_disabled"]

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS products (
        id INT AUTO_INCREMENT PRIMARY KEY, product_id VARCHAR(64) NOT NULL UNIQUE,
        name VARCHAR(255) NOT NULL, price_inr FLOAT, price_usd FLOAT,
        razorpay_link TEXT, gumroad_product_id VARCHAR(128), gumroad_link TEXT,
        max_machines INT NOT NULL DEFAULT 1, is_active TINYINT NOT NULL DEFAULT 1,
        created_at DOUBLE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS identity_otps (
        identity VARCHAR(255) NOT NULL, identity_type VARCHAR(16) NOT NULL DEFAULT 'email',
        otp VARCHAR(6) NOT NULL, sent_at DOUBLE NOT NULL,
        attempts INT NOT NULL DEFAULT 0, verified TINYINT NOT NULL DEFAULT 0,
        PRIMARY KEY (identity, identity_type)
    );
    CREATE TABLE IF NOT EXISTS customers (
        id INT AUTO_INCREMENT PRIMARY KEY, identity VARCHAR(255) NOT NULL,
        identity_type VARCHAR(16) NOT NULL DEFAULT 'email',
        email VARCHAR(255), phone VARCHAR(32), full_name VARCHAR(255), country VARCHAR(64),
        created_at DOUBLE NOT NULL, updated_at DOUBLE,
        UNIQUE KEY uq_identity (identity, identity_type)
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INT AUTO_INCREMENT PRIMARY KEY, product_id VARCHAR(64) NOT NULL,
        customer_id INT NOT NULL, source VARCHAR(32) NOT NULL,
        payment_ref VARCHAR(255) NOT NULL UNIQUE,
        amount FLOAT NOT NULL, currency VARCHAR(8) NOT NULL,
        plan VARCHAR(32) NOT NULL DEFAULT 'lifetime',
        paid_at DOUBLE NOT NULL, is_refunded TINYINT NOT NULL DEFAULT 0, refunded_at DOUBLE
    );
    CREATE TABLE IF NOT EXISTS licenses (
        id INT AUTO_INCREMENT PRIMARY KEY, product_id VARCHAR(64) NOT NULL,
        customer_id INT NOT NULL, payment_id INT NOT NULL,
        unique_key VARCHAR(128) NOT NULL UNIQUE, machine_id VARCHAR(128) NOT NULL,
        machine_label VARCHAR(255), plan VARCHAR(32) NOT NULL DEFAULT 'lifetime',
        is_active TINYINT NOT NULL DEFAULT 1,
        paid_at DOUBLE NOT NULL, expires_at DOUBLE, activated_at DOUBLE NOT NULL,
        last_seen_at DOUBLE, last_seen_ip VARCHAR(64),
        verify_count INT NOT NULL DEFAULT 0, revoked_at DOUBLE, revoke_reason VARCHAR(64)
    );
    CREATE TABLE IF NOT EXISTS verify_log (
        id INT AUTO_INCREMENT PRIMARY KEY, product_id VARCHAR(64),
        license_id INT, identity VARCHAR(255), ip_address VARCHAR(64),
        result VARCHAR(32) NOT NULL, called_at DOUBLE NOT NULL,
        INDEX idx_vlog_time (called_at)
    );
    """
    conn = _mysql_conn()
    cur  = conn.cursor()
    # MySQL doesn't support multi-statement in one execute — split by semicolon
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                cur.execute(stmt)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    print(f"  [DB/MySQL] DDL warning: {e}")
    conn.commit()
    conn.close()
    print(f"  [DB] MySQL/PlanetScale ready: {_mysql_cfg['host']}")

def _mysql_conn():
    import mysql.connector
    return mysql.connector.connect(**_mysql_cfg)

def _mysql_rows(cur) -> list:
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    return [dict(zip(cols, row)) for row in rows]

# MySQL uses %s placeholders instead of ?
def _msql(sql: str) -> str:
    return sql.replace("?", "%s")

# MySQL uses LAST_INSERT_ID() instead of lastrowid
# MySQL uses ON DUPLICATE KEY UPDATE instead of ON CONFLICT

# ═════════════════════════════════════════════════════════════════
#  PROVIDER: MONGODB
# ═════════════════════════════════════════════════════════════════

def _mongo_init():
    global _mongo_db
    from pymongo import MongoClient, ASCENDING
    url      = DATABASE_INFO.get("url", "")
    db_name  = DATABASE_INFO.get("database", "licenses")
    if not url:
        raise ValueError("MongoDB requires 'url' in DATABASE_INFO")
    client   = MongoClient(url, serverSelectionTimeoutMS=10000)
    _mongo_db = client[db_name]

    # Create indexes
    _mongo_db.products.create_index      ([("product_id", ASCENDING)],   unique=True)
    _mongo_db.identity_otps.create_index ([("identity",   ASCENDING),
                                           ("identity_type", ASCENDING)], unique=True)
    _mongo_db.customers.create_index     ([("identity",   ASCENDING),
                                           ("identity_type", ASCENDING)], unique=True)
    _mongo_db.payments.create_index      ([("payment_ref", ASCENDING)],   unique=True)
    _mongo_db.licenses.create_index      ([("unique_key", ASCENDING)],    unique=True)
    _mongo_db.verify_log.create_index    ([("called_at",  ASCENDING)])
    print(f"  [DB] MongoDB ready: db={db_name}")

# ═════════════════════════════════════════════════════════════════
#  INIT ROUTER
# ═════════════════════════════════════════════════════════════════

def init_db():
    if   _PROVIDER == "sqlite":     _sqlite_init()
    elif _PROVIDER == "turso":      _turso_init()
    elif _PROVIDER == "postgresql": _pg_init()
    elif _PROVIDER == "mysql":      _mysql_init()
    elif _PROVIDER == "mongodb":    _mongo_init()
    else:
        raise ValueError(f"Unknown provider: '{_PROVIDER}'. "
                         f"Use: sqlite, turso, postgresql, mysql, mongodb")

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — store_otp
# ═════════════════════════════════════════════════════════════════

def store_otp(identity: str, identity_type: str, otp: str) -> bool:
    identity = normalize_identity(identity, identity_type)
    now      = time.time()

    if _PROVIDER == "sqlite":
        conn = _sqlite_conn()
        try:
            conn.execute("""
                INSERT INTO identity_otps (identity,identity_type,otp,sent_at,attempts,verified)
                VALUES (?,?,?,?,0,0)
                ON CONFLICT(identity,identity_type) DO UPDATE SET
                    otp=excluded.otp, sent_at=excluded.sent_at, attempts=0, verified=0
            """, (identity, identity_type, otp, now))
            conn.commit(); return True
        finally: conn.close()

    elif _PROVIDER == "turso":
        _turso_execute("""
            INSERT INTO identity_otps (identity,identity_type,otp,sent_at,attempts,verified)
            VALUES (?,?,?,?,0,0)
            ON CONFLICT(identity,identity_type) DO UPDATE SET
                otp=excluded.otp, sent_at=excluded.sent_at, attempts=0, verified=0
        """, [identity, identity_type, otp, now])
        return True

    elif _PROVIDER == "postgresql":
        conn = _pg_conn()
        try:
            conn.cursor().execute("""
                INSERT INTO identity_otps (identity,identity_type,otp,sent_at,attempts,verified)
                VALUES (%s,%s,%s,%s,0,0)
                ON CONFLICT(identity,identity_type) DO UPDATE SET
                    otp=EXCLUDED.otp, sent_at=EXCLUDED.sent_at, attempts=0, verified=0
            """, (identity, identity_type, otp, now))
            conn.commit(); return True
        finally: conn.close()

    elif _PROVIDER == "mysql":
        conn = _mysql_conn()
        try:
            conn.cursor().execute("""
                INSERT INTO identity_otps (identity,identity_type,otp,sent_at,attempts,verified)
                VALUES (%s,%s,%s,%s,0,0)
                ON DUPLICATE KEY UPDATE otp=VALUES(otp), sent_at=VALUES(sent_at),
                    attempts=0, verified=0
            """, (identity, identity_type, otp, now))
            conn.commit(); return True
        finally: conn.close()

    elif _PROVIDER == "mongodb":
        _mongo_db.identity_otps.update_one(
            {"identity": identity, "identity_type": identity_type},
            {"$set": {"otp": otp, "sent_at": now, "attempts": 0, "verified": 0}},
            upsert=True)
        return True

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — is_valid_otp
# ═════════════════════════════════════════════════════════════════

def is_valid_otp(identity: str, identity_type: str, otp: str) -> dict:
    identity = normalize_identity(identity, identity_type)
    now      = time.time()

    def _check(row):
        """Shared validation logic once row is fetched as dict."""
        if not row:
            return {"ok": False, "reason": "otp_not_found"}
        if row.get("verified"):
            return {"ok": True, "reason": "already_verified"}
        if now - row["sent_at"] > OTP_EXPIRY_SECONDS:
            return {"ok": False, "reason": "otp_expired"}
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            return {"ok": False, "reason": "max_attempts_exceeded"}
        if row["otp"] != otp.strip():
            return {"ok": False, "reason": "invalid_otp",
                    "attempts_left": OTP_MAX_ATTEMPTS - row["attempts"] - 1,
                    "_inc_attempts": True}
        return {"ok": True, "reason": "verified", "_mark_verified": True}

    if _PROVIDER == "sqlite":
        conn = _sqlite_conn()
        try:
            row = conn.execute(
                "SELECT otp,sent_at,attempts,verified FROM identity_otps "
                "WHERE identity=? AND identity_type=?", (identity, identity_type)
            ).fetchone()
            row  = dict(row) if row else None
            res  = _check(row)
            if res.get("_inc_attempts"):
                conn.execute("UPDATE identity_otps SET attempts=attempts+1 "
                             "WHERE identity=? AND identity_type=?",
                             (identity, identity_type))
                conn.commit()
            if res.get("_mark_verified"):
                conn.execute("UPDATE identity_otps SET verified=1 "
                             "WHERE identity=? AND identity_type=?",
                             (identity, identity_type))
                conn.commit()
            return {k: v for k, v in res.items() if not k.startswith("_")}
        finally: conn.close()

    elif _PROVIDER == "turso":
        rows = _turso_execute(
            "SELECT otp,sent_at,attempts,verified FROM identity_otps "
            "WHERE identity=? AND identity_type=?", [identity, identity_type])
        row = rows[0] if rows else None
        res = _check(row)
        if res.get("_inc_attempts"):
            _turso_execute("UPDATE identity_otps SET attempts=attempts+1 "
                           "WHERE identity=? AND identity_type=?",
                           [identity, identity_type])
        if res.get("_mark_verified"):
            _turso_execute("UPDATE identity_otps SET verified=1 "
                           "WHERE identity=? AND identity_type=?",
                           [identity, identity_type])
        return {k: v for k, v in res.items() if not k.startswith("_")}

    elif _PROVIDER == "postgresql":
        conn = _pg_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT otp,sent_at,attempts,verified FROM identity_otps "
                        "WHERE identity=%s AND identity_type=%s", (identity, identity_type))
            rows = _pg_rows(cur)
            row  = rows[0] if rows else None
            res  = _check(row)
            if res.get("_inc_attempts"):
                cur.execute("UPDATE identity_otps SET attempts=attempts+1 "
                            "WHERE identity=%s AND identity_type=%s",
                            (identity, identity_type))
                conn.commit()
            if res.get("_mark_verified"):
                cur.execute("UPDATE identity_otps SET verified=1 "
                            "WHERE identity=%s AND identity_type=%s",
                            (identity, identity_type))
                conn.commit()
            return {k: v for k, v in res.items() if not k.startswith("_")}
        finally: conn.close()

    elif _PROVIDER == "mysql":
        conn = _mysql_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT otp,sent_at,attempts,verified FROM identity_otps "
                        "WHERE identity=%s AND identity_type=%s", (identity, identity_type))
            rows = _mysql_rows(cur)
            row  = rows[0] if rows else None
            res  = _check(row)
            if res.get("_inc_attempts"):
                cur.execute("UPDATE identity_otps SET attempts=attempts+1 "
                            "WHERE identity=%s AND identity_type=%s",
                            (identity, identity_type))
                conn.commit()
            if res.get("_mark_verified"):
                cur.execute("UPDATE identity_otps SET verified=1 "
                            "WHERE identity=%s AND identity_type=%s",
                            (identity, identity_type))
                conn.commit()
            return {k: v for k, v in res.items() if not k.startswith("_")}
        finally: conn.close()

    elif _PROVIDER == "mongodb":
        doc = _mongo_db.identity_otps.find_one(
            {"identity": identity, "identity_type": identity_type})
        res = _check(doc)
        if res.get("_inc_attempts"):
            _mongo_db.identity_otps.update_one(
                {"identity": identity, "identity_type": identity_type},
                {"$inc": {"attempts": 1}})
        if res.get("_mark_verified"):
            _mongo_db.identity_otps.update_one(
                {"identity": identity, "identity_type": identity_type},
                {"$set": {"verified": 1}})
        return {k: v for k, v in res.items() if not k.startswith("_")}

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — is_identity_verified
# ═════════════════════════════════════════════════════════════════

def is_identity_verified(identity: str, identity_type: str) -> bool:
    if identity_type == "google": return True
    identity = normalize_identity(identity, identity_type)
    now      = time.time()

    def _check(row):
        if not row: return False
        if not row.get("verified"): return False
        if now - row["sent_at"] > 30 * 60: return False
        return True

    if _PROVIDER == "sqlite":
        conn = _sqlite_conn()
        row  = conn.execute("SELECT verified,sent_at FROM identity_otps "
                            "WHERE identity=? AND identity_type=?",
                            (identity, identity_type)).fetchone()
        conn.close()
        return _check(dict(row) if row else None)

    elif _PROVIDER == "turso":
        rows = _turso_execute("SELECT verified,sent_at FROM identity_otps "
                              "WHERE identity=? AND identity_type=?",
                              [identity, identity_type])
        return _check(rows[0] if rows else None)

    elif _PROVIDER == "postgresql":
        conn = _pg_conn()
        cur  = conn.cursor()
        cur.execute("SELECT verified,sent_at FROM identity_otps "
                    "WHERE identity=%s AND identity_type=%s", (identity, identity_type))
        rows = _pg_rows(cur); conn.close()
        return _check(rows[0] if rows else None)

    elif _PROVIDER == "mysql":
        conn = _mysql_conn()
        cur  = conn.cursor()
        cur.execute("SELECT verified,sent_at FROM identity_otps "
                    "WHERE identity=%s AND identity_type=%s", (identity, identity_type))
        rows = _mysql_rows(cur); conn.close()
        return _check(rows[0] if rows else None)

    elif _PROVIDER == "mongodb":
        doc = _mongo_db.identity_otps.find_one(
            {"identity": identity, "identity_type": identity_type},
            {"verified": 1, "sent_at": 1})
        return _check(doc)

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — add_product / get_product
# ═════════════════════════════════════════════════════════════════

def add_product(product_id, name, price_inr, price_usd,
                razorpay_link=None, gumroad_product_id=None,
                gumroad_link=None, max_machines=1):
    now = time.time()

    if _PROVIDER in ("sqlite", "turso"):
        sql = """INSERT OR IGNORE INTO products
                 (product_id,name,price_inr,price_usd,razorpay_link,
                  gumroad_product_id,gumroad_link,max_machines,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?)"""
        args = (product_id, name, price_inr, price_usd, razorpay_link,
                gumroad_product_id, gumroad_link, max_machines, now)
        if _PROVIDER == "sqlite":
            conn = _sqlite_conn()
            conn.execute(sql, args); conn.commit(); conn.close()
        else:
            _turso_execute(sql, list(args))

    elif _PROVIDER == "postgresql":
        conn = _pg_conn()
        conn.cursor().execute("""
            INSERT INTO products
            (product_id,name,price_inr,price_usd,razorpay_link,
             gumroad_product_id,gumroad_link,max_machines,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(product_id) DO NOTHING
        """, (product_id, name, price_inr, price_usd, razorpay_link,
              gumroad_product_id, gumroad_link, max_machines, now))
        conn.commit(); conn.close()

    elif _PROVIDER == "mysql":
        conn = _mysql_conn(); cur = conn.cursor()
        cur.execute("""INSERT IGNORE INTO products
            (product_id,name,price_inr,price_usd,razorpay_link,
             gumroad_product_id,gumroad_link,max_machines,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (product_id, name, price_inr, price_usd, razorpay_link,
              gumroad_product_id, gumroad_link, max_machines, now))
        conn.commit(); conn.close()

    elif _PROVIDER == "mongodb":
        _mongo_db.products.update_one(
            {"product_id": product_id},
            {"$setOnInsert": {"product_id": product_id, "name": name,
                "price_inr": price_inr, "price_usd": price_usd,
                "razorpay_link": razorpay_link, "gumroad_product_id": gumroad_product_id,
                "gumroad_link": gumroad_link, "max_machines": max_machines,
                "is_active": 1, "created_at": now}},
            upsert=True)
    return True

def get_product(product_id) -> dict:
    if _PROVIDER == "sqlite":
        conn = _sqlite_conn()
        row  = conn.execute("SELECT * FROM products WHERE product_id=? AND is_active=1",
                            (product_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    elif _PROVIDER == "turso":
        rows = _turso_execute("SELECT * FROM products WHERE product_id=? AND is_active=1",
                              [product_id])
        return rows[0] if rows else None

    elif _PROVIDER == "postgresql":
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE product_id=%s AND is_active=1",
                    (product_id,))
        rows = _pg_rows(cur); conn.close()
        return rows[0] if rows else None

    elif _PROVIDER == "mysql":
        conn = _mysql_conn(); cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE product_id=%s AND is_active=1",
                    (product_id,))
        rows = _mysql_rows(cur); conn.close()
        return rows[0] if rows else None

    elif _PROVIDER == "mongodb":
        doc = _mongo_db.products.find_one(
            {"product_id": product_id, "is_active": 1}, {"_id": 0})
        return doc

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — register_license
# ═════════════════════════════════════════════════════════════════

def register_license(product_id, identity, identity_type, machine_id, unique_key,
                     source, payment_ref, amount, currency,
                     plan="lifetime", full_name=None, country=None, ip_address=None):
    identity = normalize_identity(identity, identity_type)
    now      = time.time()

    if _PROVIDER == "mongodb":
        return _mongo_register(product_id, identity, identity_type, machine_id, unique_key,
                               source, payment_ref, amount, currency, plan,
                               full_name, country, ip_address, now)

    # SQL providers share the same logic with provider-specific syntax
    return _sql_register(product_id, identity, identity_type, machine_id, unique_key,
                         source, payment_ref, amount, currency, plan,
                         full_name, country, ip_address, now)

def _sql_register(product_id, identity, identity_type, machine_id, unique_key,
                  source, payment_ref, amount, currency, plan,
                  full_name, country, ip_address, now):
    """Shared registration logic for all SQL providers."""

    pg   = _PROVIDER == "postgresql"
    my   = _PROVIDER == "mysql"
    ph   = "%s" if (pg or my) else "?"   # placeholder

    def conn_and_cur():
        if   _PROVIDER == "sqlite":     c = _sqlite_conn();  return c, c.cursor()
        elif _PROVIDER == "turso":      return None, None    # handled separately
        elif _PROVIDER == "postgresql": c = _pg_conn();      return c, c.cursor()
        elif _PROVIDER == "mysql":      c = _mysql_conn();   return c, c.cursor()

    if _PROVIDER == "turso":
        return _turso_register(product_id, identity, identity_type, machine_id, unique_key,
                               source, payment_ref, amount, currency, plan,
                               full_name, country, ip_address, now)

    conn, cur = conn_and_cur()
    try:
        def q(row_dict_or_none):
            if row_dict_or_none is None: return None
            return row_dict_or_none

        def fetch_one(sql, args):
            cur.execute(sql.replace("?", ph), args)
            if _PROVIDER == "sqlite":
                r = cur.fetchone()
                return dict(r) if r else None
            elif pg:  rows = _pg_rows(cur);    return rows[0] if rows else None
            elif my:  rows = _mysql_rows(cur); return rows[0] if rows else None

        # Validate product
        prod = fetch_one("SELECT max_machines FROM products WHERE product_id=? AND is_active=1",
                         (product_id,))
        if not prod: return {"ok": False, "reason": "unknown_product"}

        # Upsert customer
        cust = fetch_one("SELECT id FROM customers WHERE identity=? AND identity_type=?",
                         (identity, identity_type))
        if cust:
            customer_id = cust["id"]
            if identity_type == "email":
                cur.execute(f"UPDATE customers SET email={ph}, updated_at={ph} WHERE id={ph}",
                            (identity, now, customer_id))
            elif identity_type == "sms":
                cur.execute(f"UPDATE customers SET phone={ph}, updated_at={ph} WHERE id={ph}",
                            (identity, now, customer_id))
            else:
                cur.execute(f"UPDATE customers SET updated_at={ph} WHERE id={ph}",
                            (now, customer_id))
        else:
            email_val = identity if identity_type == "email" else None
            phone_val = identity if identity_type == "sms"   else None
            cur.execute(
                f"INSERT INTO customers "
                f"(identity,identity_type,email,phone,full_name,country,created_at,updated_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (identity, identity_type, email_val, phone_val, full_name, country, now, now))
            if pg or my:
                customer_id = cur.lastrowid if my else None
                if pg:
                    cur.execute("SELECT lastval()")
                    customer_id = cur.fetchone()[0]
            else:
                customer_id = cur.lastrowid

        # Reject duplicate payment
        dup = fetch_one("SELECT id FROM payments WHERE payment_ref=?", (payment_ref,))
        if dup: return {"ok": False, "reason": "payment_already_used"}

        # Check machine count
        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM licenses "
            f"WHERE customer_id={ph} AND product_id={ph} AND is_active=1",
            (customer_id, product_id))
        if _PROVIDER == "sqlite":
            cnt_row = dict(cur.fetchone())
        elif pg:
            cnt_row = _pg_rows(cur)[0]
        else:
            cnt_row = _mysql_rows(cur)[0]
        if cnt_row["cnt"] >= prod["max_machines"]:
            return {"ok": False, "reason": f"max_machines_reached ({prod['max_machines']})"}

        # Insert payment
        cur.execute(
            f"INSERT INTO payments "
            f"(product_id,customer_id,source,payment_ref,amount,currency,plan,paid_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (product_id, customer_id, source, payment_ref, amount, currency, plan, now))
        if pg:
            cur.execute("SELECT lastval()"); payment_db_id = cur.fetchone()[0]
        else:
            payment_db_id = cur.lastrowid

        # Check existing license
        existing = fetch_one("SELECT id,is_active FROM licenses WHERE unique_key=?",
                             (unique_key,))
        if existing:
            return {"ok": True,  "reason": "already_registered"} if existing["is_active"] \
                   else {"ok": False, "reason": "license_revoked"}

        # Insert license
        cur.execute(
            f"INSERT INTO licenses "
            f"(product_id,customer_id,payment_id,unique_key,machine_id,plan,"
            f" paid_at,expires_at,activated_at,last_seen_at,last_seen_ip,verify_count) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},0)",
            (product_id, customer_id, payment_db_id, unique_key, machine_id, plan,
             now, plan_expiry(plan, now), now, now, ip_address))

        conn.commit()
        _sqlite_maybe_backup()   # no-op for non-sqlite providers
        return {"ok": True, "reason": "registered"}

    except Exception as e:
        if conn: conn.rollback()
        return {"ok": False, "reason": f"db_error: {e}"}
    finally:
        if conn: conn.close()

def _turso_register(product_id, identity, identity_type, machine_id, unique_key,
                    source, payment_ref, amount, currency, plan,
                    full_name, country, ip_address, now):
    """Turso uses HTTP API — each statement is a separate call."""
    try:
        rows = _turso_execute("SELECT max_machines FROM products WHERE product_id=? AND is_active=1",
                              [product_id])
        if not rows: return {"ok": False, "reason": "unknown_product"}
        max_machines = rows[0]["max_machines"]

        rows = _turso_execute("SELECT id FROM customers WHERE identity=? AND identity_type=?",
                              [identity, identity_type])
        if rows:
            customer_id = rows[0]["id"]
        else:
            email_val = identity if identity_type == "email" else None
            phone_val = identity if identity_type == "sms"   else None
            _turso_execute(
                "INSERT INTO customers "
                "(identity,identity_type,email,phone,full_name,country,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [identity, identity_type, email_val, phone_val, full_name, country, now, now])
            rows = _turso_execute(
                "SELECT id FROM customers WHERE identity=? AND identity_type=?",
                [identity, identity_type])
            customer_id = rows[0]["id"]

        rows = _turso_execute("SELECT id FROM payments WHERE payment_ref=?", [payment_ref])
        if rows: return {"ok": False, "reason": "payment_already_used"}

        rows = _turso_execute(
            "SELECT COUNT(*) AS cnt FROM licenses "
            "WHERE customer_id=? AND product_id=? AND is_active=1",
            [customer_id, product_id])
        if rows[0]["cnt"] >= max_machines:
            return {"ok": False, "reason": f"max_machines_reached ({max_machines})"}

        _turso_execute(
            "INSERT INTO payments "
            "(product_id,customer_id,source,payment_ref,amount,currency,plan,paid_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [product_id, customer_id, source, payment_ref, amount, currency, plan, now])
        rows = _turso_execute(
            "SELECT id FROM payments WHERE payment_ref=?", [payment_ref])
        payment_db_id = rows[0]["id"]

        existing = _turso_execute(
            "SELECT id,is_active FROM licenses WHERE unique_key=?", [unique_key])
        if existing:
            return {"ok": True, "reason": "already_registered"} if existing[0]["is_active"] \
                   else {"ok": False, "reason": "license_revoked"}

        _turso_execute(
            "INSERT INTO licenses "
            "(product_id,customer_id,payment_id,unique_key,machine_id,plan,"
            " paid_at,expires_at,activated_at,last_seen_at,last_seen_ip,verify_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            [product_id, customer_id, payment_db_id, unique_key, machine_id, plan,
             now, plan_expiry(plan, now), now, now, ip_address])
        return {"ok": True, "reason": "registered"}
    except Exception as e:
        return {"ok": False, "reason": f"db_error: {e}"}

def _mongo_register(product_id, identity, identity_type, machine_id, unique_key,
                    source, payment_ref, amount, currency, plan,
                    full_name, country, ip_address, now):
    from pymongo import ReturnDocument
    try:
        prod = _mongo_db.products.find_one({"product_id": product_id, "is_active": 1})
        if not prod: return {"ok": False, "reason": "unknown_product"}

        cust = _mongo_db.customers.find_one_and_update(
            {"identity": identity, "identity_type": identity_type},
            {"$setOnInsert": {
                "identity": identity, "identity_type": identity_type,
                "email":  identity if identity_type == "email" else None,
                "phone":  identity if identity_type == "sms"   else None,
                "full_name": full_name, "country": country,
                "created_at": now, "updated_at": now}},
            upsert=True, return_document=ReturnDocument.AFTER)
        customer_id = str(cust["_id"])

        if _mongo_db.payments.find_one({"payment_ref": payment_ref}):
            return {"ok": False, "reason": "payment_already_used"}

        active_count = _mongo_db.licenses.count_documents(
            {"customer_id": customer_id, "product_id": product_id, "is_active": 1})
        if active_count >= prod["max_machines"]:
            return {"ok": False, "reason": f"max_machines_reached ({prod['max_machines']})"}

        existing = _mongo_db.licenses.find_one({"unique_key": unique_key})
        if existing:
            return {"ok": True,  "reason": "already_registered"} if existing["is_active"] \
                   else {"ok": False, "reason": "license_revoked"}

        pay_res = _mongo_db.payments.insert_one({
            "product_id": product_id, "customer_id": customer_id,
            "source": source, "payment_ref": payment_ref,
            "amount": amount, "currency": currency,
            "plan": plan, "paid_at": now,
            "is_refunded": 0, "refunded_at": None})

        _mongo_db.licenses.insert_one({
            "product_id": product_id, "customer_id": customer_id,
            "payment_id": str(pay_res.inserted_id), "unique_key": unique_key,
            "machine_id": machine_id, "plan": plan, "is_active": 1,
            "paid_at": now, "expires_at": plan_expiry(plan, now),
            "activated_at": now, "last_seen_at": now,
            "last_seen_ip": ip_address, "verify_count": 0,
            "revoked_at": None, "revoke_reason": None})
        return {"ok": True, "reason": "registered"}
    except Exception as e:
        return {"ok": False, "reason": f"db_error: {e}"}

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — verify_license
# ═════════════════════════════════════════════════════════════════

def verify_license(product_id, unique_key, ip_address=None):
    now = time.time()

    def _eval(row):
        if not row:
            return {"ok": False, "reason": "not_found", "_log": "invalid"}
        if row["product_id"] != product_id:
            return {"ok": False, "reason": "wrong_product", "_log": "wrong_product",
                    "_lid": row.get("id"), "_identity": row.get("identity")}
        if not row["is_active"]:
            return {"ok": False, "reason": "revoked", "_log": "revoked",
                    "_lid": row.get("id"), "_identity": row.get("identity")}
        if row.get("expires_at") and now > row["expires_at"]:
            return {"ok": False, "reason": "expired", "_log": "expired",
                    "_lid": row.get("id"), "_identity": row.get("identity")}
        return {"ok": True, "reason": "ok", "_update": True,
                "_lid": row.get("id"), "_identity": row.get("identity")}

    if _PROVIDER == "sqlite":
        conn = _sqlite_conn()
        try:
            row = conn.execute("""
                SELECT l.id, l.is_active, l.expires_at, l.product_id, cu.identity
                FROM licenses l JOIN customers cu ON cu.id=l.customer_id
                WHERE l.unique_key=?""", (unique_key,)).fetchone()
            row = dict(row) if row else None
            res = _eval(row)
            if res.get("_update"):
                conn.execute("UPDATE licenses SET last_seen_at=?,last_seen_ip=?,"
                             "verify_count=verify_count+1 WHERE id=?",
                             (now, ip_address, res["_lid"]))
                conn.commit()
            _sql_log(conn, product_id, res.get("_lid"), res.get("_identity"),
                     ip_address, res["_log"] if not res["ok"] else "ok")
            return {"ok": res["ok"], "reason": res["reason"]}
        finally: conn.close()

    elif _PROVIDER == "turso":
        rows = _turso_execute("""
            SELECT l.id, l.is_active, l.expires_at, l.product_id, cu.identity
            FROM licenses l JOIN customers cu ON cu.id=l.customer_id
            WHERE l.unique_key=?""", [unique_key])
        row = rows[0] if rows else None
        res = _eval(row)
        if res.get("_update"):
            _turso_execute("UPDATE licenses SET last_seen_at=?,last_seen_ip=?,"
                           "verify_count=verify_count+1 WHERE id=?",
                           [now, ip_address, res["_lid"]])
        return {"ok": res["ok"], "reason": res["reason"]}

    elif _PROVIDER == "postgresql":
        conn = _pg_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT l.id, l.is_active, l.expires_at, l.product_id, cu.identity
                FROM licenses l JOIN customers cu ON cu.id=l.customer_id
                WHERE l.unique_key=%s""", (unique_key,))
            rows = _pg_rows(cur); row = rows[0] if rows else None
            res  = _eval(row)
            if res.get("_update"):
                cur.execute("UPDATE licenses SET last_seen_at=%s,last_seen_ip=%s,"
                            "verify_count=verify_count+1 WHERE id=%s",
                            (now, ip_address, res["_lid"]))
                conn.commit()
            return {"ok": res["ok"], "reason": res["reason"]}
        finally: conn.close()

    elif _PROVIDER == "mysql":
        conn = _mysql_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT l.id, l.is_active, l.expires_at, l.product_id, cu.identity
                FROM licenses l JOIN customers cu ON cu.id=l.customer_id
                WHERE l.unique_key=%s""", (unique_key,))
            rows = _mysql_rows(cur); row = rows[0] if rows else None
            res  = _eval(row)
            if res.get("_update"):
                cur.execute("UPDATE licenses SET last_seen_at=%s,last_seen_ip=%s,"
                            "verify_count=verify_count+1 WHERE id=%s",
                            (now, ip_address, res["_lid"]))
                conn.commit()
            return {"ok": res["ok"], "reason": res["reason"]}
        finally: conn.close()

    elif _PROVIDER == "mongodb":
        doc = _mongo_db.licenses.find_one({"unique_key": unique_key})
        if not doc:
            return {"ok": False, "reason": "not_found"}
        cust = _mongo_db.customers.find_one({"_id": doc.get("customer_id")},
                                            {"identity": 1}) or {}
        doc["identity"] = cust.get("identity", "")
        res = _eval(doc)
        if res.get("_update"):
            _mongo_db.licenses.update_one(
                {"unique_key": unique_key},
                {"$set": {"last_seen_at": now, "last_seen_ip": ip_address},
                 "$inc": {"verify_count": 1}})
        return {"ok": res["ok"], "reason": res["reason"]}

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — revoke_license
# ═════════════════════════════════════════════════════════════════

def revoke_license(identity, identity_type="email", product_id=None, reason="manual"):
    identity = normalize_identity(identity, identity_type)
    now      = time.time()

    if _PROVIDER == "sqlite":
        conn = _sqlite_conn(); c = conn.cursor()
        if product_id:
            c.execute("""UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason=?
                WHERE customer_id=(SELECT id FROM customers WHERE identity=? AND identity_type=?)
                AND product_id=? AND is_active=1""",
                      (now, reason, identity, identity_type, product_id))
        else:
            c.execute("""UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason=?
                WHERE customer_id=(SELECT id FROM customers WHERE identity=? AND identity_type=?)
                AND is_active=1""", (now, reason, identity, identity_type))
        conn.commit(); n = c.rowcount; conn.close(); return n

    elif _PROVIDER == "turso":
        if product_id:
            rows = _turso_execute("SELECT id FROM customers WHERE identity=? AND identity_type=?",
                                  [identity, identity_type])
            if not rows: return 0
            cid = rows[0]["id"]
            _turso_execute("UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason=? "
                           "WHERE customer_id=? AND product_id=? AND is_active=1",
                           [now, reason, cid, product_id])
        else:
            rows = _turso_execute("SELECT id FROM customers WHERE identity=? AND identity_type=?",
                                  [identity, identity_type])
            if not rows: return 0
            cid = rows[0]["id"]
            _turso_execute("UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason=? "
                           "WHERE customer_id=? AND is_active=1", [now, reason, cid])
        return 1  # Turso doesn't return rowcount easily

    elif _PROVIDER == "postgresql":
        conn = _pg_conn(); cur = conn.cursor()
        if product_id:
            cur.execute("""UPDATE licenses SET is_active=0,revoked_at=%s,revoke_reason=%s
                WHERE customer_id=(SELECT id FROM customers WHERE identity=%s AND identity_type=%s)
                AND product_id=%s AND is_active=1""",
                        (now, reason, identity, identity_type, product_id))
        else:
            cur.execute("""UPDATE licenses SET is_active=0,revoked_at=%s,revoke_reason=%s
                WHERE customer_id=(SELECT id FROM customers WHERE identity=%s AND identity_type=%s)
                AND is_active=1""", (now, reason, identity, identity_type))
        conn.commit(); n = cur.rowcount; conn.close(); return n

    elif _PROVIDER == "mysql":
        conn = _mysql_conn(); cur = conn.cursor()
        if product_id:
            cur.execute("""UPDATE licenses SET is_active=0,revoked_at=%s,revoke_reason=%s
                WHERE customer_id=(SELECT id FROM customers WHERE identity=%s AND identity_type=%s)
                AND product_id=%s AND is_active=1""",
                        (now, reason, identity, identity_type, product_id))
        else:
            cur.execute("""UPDATE licenses SET is_active=0,revoked_at=%s,revoke_reason=%s
                WHERE customer_id=(SELECT id FROM customers WHERE identity=%s AND identity_type=%s)
                AND is_active=1""", (now, reason, identity, identity_type))
        conn.commit(); n = cur.rowcount; conn.close(); return n

    elif _PROVIDER == "mongodb":
        cust = _mongo_db.customers.find_one(
            {"identity": identity, "identity_type": identity_type}, {"_id": 1})
        if not cust: return 0
        filt = {"customer_id": str(cust["_id"]), "is_active": 1}
        if product_id: filt["product_id"] = product_id
        res = _mongo_db.licenses.update_many(
            filt, {"$set": {"is_active": 0, "revoked_at": now, "revoke_reason": reason}})
        return res.modified_count

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — mark_refunded
# ═════════════════════════════════════════════════════════════════

def mark_refunded(payment_ref) -> bool:
    now = time.time()

    if _PROVIDER == "sqlite":
        conn = _sqlite_conn(); c = conn.cursor()
        c.execute("UPDATE payments SET is_refunded=1,refunded_at=? WHERE payment_ref=?",
                  (now, payment_ref))
        if c.rowcount == 0: conn.close(); return False
        c.execute("""UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason='refund'
                     WHERE payment_id=(SELECT id FROM payments WHERE payment_ref=?)""",
                  (now, payment_ref))
        conn.commit(); conn.close(); return True

    elif _PROVIDER == "turso":
        rows = _turso_execute("SELECT id FROM payments WHERE payment_ref=?", [payment_ref])
        if not rows: return False
        pay_id = rows[0]["id"]
        _turso_execute("UPDATE payments SET is_refunded=1,refunded_at=? WHERE payment_ref=?",
                       [now, payment_ref])
        _turso_execute("UPDATE licenses SET is_active=0,revoked_at=?,revoke_reason='refund' "
                       "WHERE payment_id=?", [now, pay_id])
        return True

    elif _PROVIDER == "postgresql":
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("UPDATE payments SET is_refunded=1,refunded_at=%s WHERE payment_ref=%s",
                    (now, payment_ref))
        if cur.rowcount == 0: conn.close(); return False
        cur.execute("""UPDATE licenses SET is_active=0,revoked_at=%s,revoke_reason='refund'
                       WHERE payment_id=(SELECT id FROM payments WHERE payment_ref=%s)""",
                    (now, payment_ref))
        conn.commit(); conn.close(); return True

    elif _PROVIDER == "mysql":
        conn = _mysql_conn(); cur = conn.cursor()
        cur.execute("UPDATE payments SET is_refunded=1,refunded_at=%s WHERE payment_ref=%s",
                    (now, payment_ref))
        if cur.rowcount == 0: conn.close(); return False
        cur.execute("""UPDATE licenses SET is_active=0,revoked_at=%s,revoke_reason='refund'
                       WHERE payment_id=(SELECT id FROM payments WHERE payment_ref=%s)""",
                    (now, payment_ref))
        conn.commit(); conn.close(); return True

    elif _PROVIDER == "mongodb":
        pay = _mongo_db.payments.find_one_and_update(
            {"payment_ref": payment_ref},
            {"$set": {"is_refunded": 1, "refunded_at": now}})
        if not pay: return False
        _mongo_db.licenses.update_many(
            {"payment_id": str(pay["_id"])},
            {"$set": {"is_active": 0, "revoked_at": now, "revoke_reason": "refund"}})
        return True

# ═════════════════════════════════════════════════════════════════
#  INTERNAL LOG HELPER
# ═════════════════════════════════════════════════════════════════

def _sql_log(conn, product_id, license_id, identity, ip, result):
    try:
        if _PROVIDER == "sqlite":
            conn.execute("INSERT INTO verify_log (product_id,license_id,identity,"
                         "ip_address,result,called_at) VALUES (?,?,?,?,?,?)",
                         (product_id, license_id, identity, ip, result, time.time()))
            conn.commit()
            conn.execute("DELETE FROM verify_log WHERE called_at < ?",
                         (time.time() - 90 * 86400,))
            conn.commit()
    except: pass

# ═════════════════════════════════════════════════════════════════
#  SEED  (run once: python database.py)
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    add_product("TOOL1", "Image Converter Pro", 499, 9.99,
                "https://rzp.io/l/tool1", "gumroad_id_1",
                "https://you.gumroad.com/l/tool1", max_machines=1)
    add_product("TOOL2", "PDF Merger Pro", 699, 14.99,
                "https://rzp.io/l/tool2", "gumroad_id_2",
                "https://you.gumroad.com/l/tool2", max_machines=2)
    print("\nDatabase initialized. Products registered:")
    p1 = get_product("TOOL1")
    p2 = get_product("TOOL2")
    for p in [p1, p2]:
        if p:
            print(f"  [{p['product_id']}] {p['name']:25} "
                  f"₹{p['price_inr']} / ${p['price_usd']}  "
                  f"max {p['max_machines']} PC")