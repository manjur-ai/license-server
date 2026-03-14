"""
admin.py — CLI admin tool for the license server.

Commands:
  python admin.py summary                                  revenue per product
  python admin.py list                                     all licenses
  python admin.py list TOOL1                               licenses for one product
  python admin.py info user@gmail.com                      all licenses for customer (email)
  python admin.py info +919876543210 sms                   all licenses for customer (phone)
  python admin.py info user@gmail.com email TOOL1          one product for customer
  python admin.py revoke user@gmail.com                    revoke all (email identity)
  python admin.py revoke +919876543210 sms                 revoke all (phone identity)
  python admin.py revoke user@gmail.com email TOOL1        revoke one product
  python admin.py activate user@gmail.com                  re-enable all (email)
  python admin.py activate +919876543210 sms               re-enable all (phone)
  python admin.py activate user@gmail.com email TOOL1      re-enable one product
  python admin.py refund pay_ABC123                        mark refunded + revoke
  python admin.py products                                 list all products
  python admin.py addproduct                               interactive: add new product
  python admin.py otps                                     recent OTP activity (all channels)
  python admin.py customers                                list all customers
  python admin.py search keyword                           search customers by email/phone/name
"""

import sqlite3, sys, time, os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "licenses.db")

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ts(t):
    if not t: return "never"
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")

def expiry_str(expires_at):
    if not expires_at: return "lifetime"
    now = time.time()
    if now > expires_at:
        return f"EXPIRED ({ts(expires_at)})"
    return f"{ts(expires_at)} ({int((expires_at-now)/86400)}d left)"

def status_str(is_active, expires_at):
    if not is_active: return "REVOKED"
    if expires_at and time.time() > expires_at: return "EXPIRED"
    return "ACTIVE"

def identity_label(identity, identity_type):
    """Display identity with channel badge."""
    badge = {"email": "✉", "sms": "📱", "google": "G"}.get(identity_type, "?")
    return f"{badge} {identity}"

def sep(char="-", width=96):
    print(char * width)

def normalize(identity, identity_type):
    identity = identity.strip()
    if identity_type == "email":
        return identity.lower()
    if identity_type == "sms":
        phone = identity.replace(" ", "").replace("-", "")
        if not phone.startswith("+"):
            phone = "+" + phone
        return phone
    return identity

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_summary():
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            pr.product_id, pr.name,
            COUNT(DISTINCT l.id)           AS total_licenses,
            COALESCE(SUM(l.is_active), 0)  AS active_licenses,
            COUNT(DISTINCT l.customer_id)  AS unique_customers,
            COALESCE(SUM(CASE WHEN p.currency='INR' AND p.is_refunded=0
                              THEN p.amount ELSE 0 END), 0) AS revenue_inr,
            COALESCE(SUM(CASE WHEN p.currency='USD' AND p.is_refunded=0
                              THEN p.amount ELSE 0 END), 0) AS revenue_usd,
            COALESCE(SUM(CASE WHEN p.is_refunded=1 THEN 1 ELSE 0 END), 0) AS refunds
        FROM products pr
        LEFT JOIN licenses l ON l.product_id = pr.product_id
        LEFT JOIN payments p ON p.id = l.payment_id
        GROUP BY pr.product_id ORDER BY pr.created_at
    """).fetchall()

    # Channel breakdown
    ch = conn.execute("""
        SELECT cu.identity_type,
               COUNT(DISTINCT l.id)          AS licenses,
               COUNT(DISTINCT l.customer_id) AS customers
        FROM licenses l
        JOIN customers cu ON cu.id = l.customer_id
        GROUP BY cu.identity_type
    """).fetchall()
    conn.close()

    print()
    print("  PRODUCT REVENUE SUMMARY")
    sep("=")
    print(f"  {'PRODUCT':<10} {'NAME':<25} {'ACTIVE':<8} {'CUSTOMERS':<11}"
          f" {'INR ₹':>10} {'USD $':>10} {'REFUNDS':>8}")
    sep()
    tinr, tusd = 0, 0
    for r in rows:
        print(f"  {r['product_id']:<10} {r['name']:<25} "
              f"{str(r['active_licenses']):<8} {str(r['unique_customers']):<11} "
              f"{r['revenue_inr']:>10.2f} {r['revenue_usd']:>10.2f} "
              f"{str(r['refunds']):>8}")
        tinr += r['revenue_inr']
        tusd += r['revenue_usd']
    sep()
    print(f"  {'TOTAL':<10} {'':<25} {'':<8} {'':<11} {tinr:>10.2f} {tusd:>10.2f}")

    if ch:
        print()
        print("  CHANNEL BREAKDOWN")
        sep()
        print(f"  {'CHANNEL':<12} {'LICENSES':<12} {'CUSTOMERS'}")
        sep()
        for r in ch:
            badge = {"email":"✉ email","sms":"📱 sms","google":"G google"}.get(
                        r['identity_type'], r['identity_type'])
            print(f"  {badge:<12} {r['licenses']:<12} {r['customers']}")
    print()


def cmd_list(product_id=None):
    conn  = get_conn()
    where = "WHERE l.product_id=?" if product_id else ""
    args  = (product_id,) if product_id else ()
    rows  = conn.execute(f"""
        SELECT l.product_id,
               cu.identity, cu.identity_type,
               p.source, p.amount, p.currency, p.plan,
               l.is_active, l.paid_at, l.expires_at,
               l.last_seen_at, l.verify_count, l.revoke_reason
        FROM licenses l
        JOIN customers cu ON cu.id = l.customer_id
        JOIN payments  p  ON p.id  = l.payment_id
        {where}
        ORDER BY l.paid_at DESC
    """, args).fetchall()
    conn.close()

    title = "ALL LICENSES" if not product_id else f"LICENSES — {product_id}"
    print()
    print(f"  {title}  ({len(rows)} rows)")
    sep("=")
    print(f"  {'PRODUCT':<8} {'CH':<3} {'IDENTITY':<28} {'STATUS':<9} {'SOURCE':<10} "
          f"{'PLAN':<10} {'AMT':>7} {'PAID':<17} {'LAST SEEN':<17} VFY")
    sep()
    for r in rows:
        amt   = f"₹{r['amount']:.0f}" if r['currency']=='INR' else f"${r['amount']:.2f}"
        stat  = status_str(r['is_active'], r['expires_at'])
        badge = {"email":"✉","sms":"📱","google":"G"}.get(r['identity_type'], "?")
        note  = f" [{r['revoke_reason']}]" if r['revoke_reason'] else ""
        ident = r['identity'][:26] + ".." if len(r['identity']) > 28 else r['identity']
        print(f"  {r['product_id']:<8} {badge:<3} {ident:<28} {stat:<9} "
              f"{r['source']:<10} {r['plan']:<10} {amt:>7} "
              f"{ts(r['paid_at']):<17} {ts(r['last_seen_at']):<17} "
              f"{r['verify_count']}{note}")
    sep()
    print(f"  Total: {len(rows)}\n")


def cmd_info(identity, identity_type="email", product_id=None):
    identity = normalize(identity, identity_type)
    conn = get_conn()

    cust = conn.execute(
        "SELECT * FROM customers WHERE identity=? AND identity_type=?",
        (identity, identity_type)
    ).fetchone()

    if not cust:
        # Try searching by email or phone across all types
        cust = conn.execute(
            "SELECT * FROM customers WHERE identity=?", (identity,)
        ).fetchone()
        if not cust:
            print(f"\n  No customer found: {identity_label(identity, identity_type)}\n")
            conn.close()
            return
        identity_type = cust["identity_type"]

    # OTP status
    otp_row = conn.execute(
        "SELECT otp, sent_at, attempts, verified, identity_type FROM identity_otps "
        "WHERE identity=? AND identity_type=?",
        (identity, identity_type)
    ).fetchone()

    extra = "AND l.product_id=?" if product_id else ""
    args  = (identity, identity_type, product_id) if product_id else (identity, identity_type)
    rows  = conn.execute(f"""
        SELECT l.product_id, p.source, p.payment_ref, p.amount, p.currency,
               p.plan, p.paid_at, p.is_refunded,
               l.machine_id, l.is_active, l.expires_at,
               l.activated_at, l.last_seen_at, l.last_seen_ip,
               l.verify_count, l.revoked_at, l.revoke_reason
        FROM licenses l
        JOIN payments  p  ON p.id  = l.payment_id
        JOIN customers cu ON cu.id = l.customer_id
        WHERE cu.identity=? AND cu.identity_type=? {extra}
        ORDER BY l.product_id, l.activated_at
    """, args).fetchall()
    conn.close()

    print()
    sep("=")
    print(f"  CUSTOMER: {identity_label(identity, identity_type)}")
    sep("=")
    print(f"  Identity type : {identity_type.upper()}")
    if cust['email'] and identity_type != "email":
        print(f"  Email         : {cust['email']}")
    if cust['phone'] and identity_type != "sms":
        print(f"  Phone         : {cust['phone']}")
    print(f"  Name          : {cust['full_name'] or '—'}")
    print(f"  Country       : {cust['country'] or '—'}")
    print(f"  First seen    : {ts(cust['created_at'])}")
    print(f"  Licenses      : {len(rows)}")

    if otp_row:
        otp_status = "VERIFIED ✓" if otp_row["verified"] else "not verified"
        ch_label   = otp_row["identity_type"].upper()
        print(f"  OTP ({ch_label:5}) : {otp_status}  "
              f"(sent {ts(otp_row['sent_at'])}, attempts: {otp_row['attempts']})")
    else:
        print(f"  OTP status    : no OTP record")
    print()

    for r in rows:
        amt = f"₹{r['amount']:.0f} INR" if r['currency']=='INR' else f"${r['amount']:.2f} USD"
        sep()
        print(f"  Product    : {r['product_id']}")
        print(f"  Status     : {status_str(r['is_active'], r['expires_at'])}")
        print(f"  Plan       : {r['plan']}  |  Expiry: {expiry_str(r['expires_at'])}")
        print(f"  Paid       : {ts(r['paid_at'])}  ({amt} via {r['source']})")
        print(f"  Payment ref: {r['payment_ref']}")
        print(f"  Refunded   : {'YES' if r['is_refunded'] else 'no'}")
        print(f"  Machine ID : {r['machine_id'][:32]}...")
        print(f"  Activated  : {ts(r['activated_at'])}")
        print(f"  Last seen  : {ts(r['last_seen_at'])}  from IP {r['last_seen_ip'] or '—'}")
        print(f"  Verifies   : {r['verify_count']} times")
        if r['revoke_reason']:
            print(f"  Revoked at : {ts(r['revoked_at'])}  reason: {r['revoke_reason']}")
    sep()
    print()


def cmd_revoke(identity, identity_type="email", product_id=None):
    from database import revoke_license
    n     = revoke_license(identity, identity_type, product_id=product_id, reason="manual")
    scope = f"[{product_id}]" if product_id else "[ALL PRODUCTS]"
    print(f"\n  Revoked {n} license(s) for "
          f"{identity_label(normalize(identity, identity_type), identity_type)} {scope}\n")


def cmd_activate(identity, identity_type="email", product_id=None):
    identity = normalize(identity, identity_type)
    conn = get_conn()
    if product_id:
        c = conn.execute("""
            UPDATE licenses SET is_active=1, revoked_at=NULL, revoke_reason=NULL
            WHERE customer_id=(
                SELECT id FROM customers WHERE identity=? AND identity_type=?
            ) AND product_id=? AND is_active=0
        """, (identity, identity_type, product_id))
    else:
        c = conn.execute("""
            UPDATE licenses SET is_active=1, revoked_at=NULL, revoke_reason=NULL
            WHERE customer_id=(
                SELECT id FROM customers WHERE identity=? AND identity_type=?
            ) AND is_active=0
        """, (identity, identity_type))
    conn.commit()
    n     = c.rowcount
    scope = f"[{product_id}]" if product_id else "[ALL PRODUCTS]"
    conn.close()
    print(f"\n  Re-activated {n} license(s) for "
          f"{identity_label(identity, identity_type)} {scope}\n")


def cmd_refund(payment_ref):
    from database import mark_refunded
    ok = mark_refunded(payment_ref)
    if ok:
        print(f"\n  Payment '{payment_ref}' marked as refunded. License revoked.\n")
    else:
        print(f"\n  Payment ref '{payment_ref}' not found.\n")


def cmd_products():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM products ORDER BY created_at").fetchall()
    conn.close()
    print()
    print("  REGISTERED PRODUCTS")
    sep("=")
    for r in rows:
        print(f"  ID        : {r['product_id']}")
        print(f"  Name      : {r['name']}")
        print(f"  Price     : ₹{r['price_inr']} INR  /  ${r['price_usd']} USD")
        print(f"  Max PCs   : {r['max_machines']}")
        print(f"  Razorpay  : {r['razorpay_link'] or '—'}")
        print(f"  Gumroad   : {r['gumroad_link'] or '—'}")
        print(f"  Status    : {'ACTIVE' if r['is_active'] else 'DISABLED'}")
        sep()
    print()


def cmd_addproduct():
    from database import add_product
    print("\n  ADD NEW PRODUCT")
    sep()
    pid   = input("  product_id (e.g. TOOL3): ").strip().upper()
    name  = input("  name: ").strip()
    pinr  = float(input("  price INR (e.g. 499): ").strip())
    pusd  = float(input("  price USD (e.g. 9.99): ").strip())
    rzp   = input("  razorpay link (Enter to skip): ").strip() or None
    gid   = input("  gumroad product_id (Enter to skip): ").strip() or None
    glink = input("  gumroad link (Enter to skip): ").strip() or None
    mmax  = int(input("  max machines [1]: ").strip() or "1")
    ok    = add_product(pid, name, pinr, pusd, rzp, gid, glink, mmax)
    print(f"\n  {'Product added!' if ok else 'Failed — ID may already exist.'}\n")


def cmd_otps():
    """Show recent OTP activity across ALL channels (email + sms)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT identity, identity_type, sent_at, attempts, verified
        FROM identity_otps
        ORDER BY sent_at DESC
        LIMIT 50
    """).fetchall()
    conn.close()
    print()
    print("  RECENT OTP ACTIVITY — ALL CHANNELS (latest 50)")
    sep("=")
    print(f"  {'CH':<3} {'IDENTITY':<32} {'SENT':<17} {'VERIFIED':<10} {'ATTEMPTS'}")
    sep()
    for r in rows:
        verified = "YES ✓" if r['verified'] else "no"
        badge    = {"email":"✉","sms":"📱","google":"G"}.get(r['identity_type'], "?")
        ident    = r['identity'][:30] + ".." if len(r['identity']) > 32 else r['identity']
        print(f"  {badge:<3} {ident:<32} {ts(r['sent_at']):<17} {verified:<10} {r['attempts']}")
    sep()
    print(f"  Total: {len(rows)}\n")


def cmd_customers():
    """List all customers with their identity type and license count."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT cu.identity, cu.identity_type, cu.email, cu.phone,
               cu.full_name, cu.country, cu.created_at,
               COUNT(l.id)          AS total_licenses,
               SUM(l.is_active)     AS active_licenses
        FROM customers cu
        LEFT JOIN licenses l ON l.customer_id = cu.id
        GROUP BY cu.id
        ORDER BY cu.created_at DESC
    """).fetchall()
    conn.close()
    print()
    print(f"  ALL CUSTOMERS ({len(rows)} total)")
    sep("=")
    print(f"  {'CH':<3} {'IDENTITY':<30} {'NAME':<20} {'COUNTRY':<8} "
          f"{'JOINED':<17} {'LIC(ACT/TOT)'}")
    sep()
    for r in rows:
        badge = {"email":"✉","sms":"📱","google":"G"}.get(r['identity_type'], "?")
        ident = r['identity'][:28] + ".." if len(r['identity']) > 30 else r['identity']
        name  = (r['full_name'] or '—')[:18]
        lic   = f"{int(r['active_licenses'] or 0)}/{int(r['total_licenses'] or 0)}"
        print(f"  {badge:<3} {ident:<30} {name:<20} {(r['country'] or '—'):<8} "
              f"{ts(r['created_at']):<17} {lic}")
    sep()
    print(f"  Total: {len(rows)}\n")


def cmd_search(keyword):
    """Search customers by email, phone, or name (partial match)."""
    conn = get_conn()
    kw   = f"%{keyword.lower()}%"
    rows = conn.execute("""
        SELECT cu.identity, cu.identity_type, cu.email, cu.phone,
               cu.full_name, cu.country, cu.created_at,
               COUNT(l.id)      AS total_licenses,
               SUM(l.is_active) AS active_licenses
        FROM customers cu
        LEFT JOIN licenses l ON l.customer_id = cu.id
        WHERE LOWER(cu.identity) LIKE ?
           OR LOWER(COALESCE(cu.full_name,'')) LIKE ?
           OR LOWER(COALESCE(cu.phone,'')) LIKE ?
        GROUP BY cu.id
        ORDER BY cu.created_at DESC
    """, (kw, kw, kw)).fetchall()
    conn.close()

    print()
    print(f"  SEARCH RESULTS for '{keyword}'  ({len(rows)} found)")
    sep("=")
    if not rows:
        print("  No matches found.")
    for r in rows:
        badge = {"email":"✉","sms":"📱","google":"G"}.get(r['identity_type'], "?")
        lic   = f"{int(r['active_licenses'] or 0)}/{int(r['total_licenses'] or 0)}"
        print(f"  {badge} {r['identity']}")
        if r['full_name']:  print(f"    Name   : {r['full_name']}")
        if r['country']:    print(f"    Country: {r['country']}")
        print(f"    Joined : {ts(r['created_at'])}   Licenses: {lic} (active/total)")
        print(f"    → python admin.py info {r['identity']} {r['identity_type']}")
        sep()
    print()


def cmd_backup(live=True, hist=True):
    """
    Trigger an immediate backup right now.
    Run this before every redeploy to ensure LIVE is current.

    Flags:
      --live-only  → only update licenses_LIVE.db
      --hist-only  → only create licenses_PREV_timestamp.db
      (default: both)
    """
    from database import backup_db, DATABASE_INFO, _PROVIDER
    print()
    print(f"  BACKUP  (provider={_PROVIDER.upper()}, live={live}, hist={hist})")
    sep()

    result = backup_db(upload_live=live, upload_hist=hist)

    if result.get("ok"):
        if result.get("live"):
            print(f"  ✓ LIVE  : {result['live']}")
        if result.get("hist"):
            print(f"  ✓ HIST  : {result['hist']}")
        folder = DATABASE_INFO.get("gdrive_folder_id", "root")
        print(f"  Folder  : {folder}")
        print(f"\n  Backup complete — safe to redeploy.\n")
    else:
        err = result.get("error", result.get("reason", "unknown"))
        print(f"  ✗ FAILED: {err}")
        print()
        if "backup_gdrive is false" in err:
            print("  To enable: set backup_gdrive=true in DATABASE_INFO")
            print("  and set GDRIVE_CREDENTIALS_JSON env var")
        elif "not yet implemented" in err:
            print(f"  Note: backup_db is not yet implemented for {_PROVIDER}")
            print(f"  For {_PROVIDER}, data persists automatically — no backup needed before redeploy.")
        print()


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
  python admin.py summary                                  revenue per product + channel breakdown
  python admin.py list                                     all licenses
  python admin.py list TOOL1                               licenses for TOOL1 only
  python admin.py customers                                list all customers
  python admin.py search keyword                           search by email/phone/name

  python admin.py info user@gmail.com                      customer info (email, default)
  python admin.py info +919876543210 sms                   customer info (phone)
  python admin.py info user@gmail.com email TOOL1          one product for customer

  python admin.py revoke user@gmail.com                    revoke all (email)
  python admin.py revoke +919876543210 sms                 revoke all (phone)
  python admin.py revoke user@gmail.com email TOOL1        revoke one product

  python admin.py activate user@gmail.com                  re-enable all (email)
  python admin.py activate +919876543210 sms               re-enable all (phone)
  python admin.py activate user@gmail.com email TOOL1      re-enable one product

  python admin.py refund pay_ABC123                        mark refunded + revoke
  python admin.py products                                 list all products
  python admin.py addproduct                               add a new product
  python admin.py otps                                     OTP activity (email + sms)

  python admin.py backup                                   backup now (LIVE + HIST)
  python admin.py backup --live-only                       backup LIVE only
  python admin.py backup --hist-only                       backup HIST only
"""

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(USAGE)
    elif args[0] == "summary":
        cmd_summary()
    elif args[0] == "list":
        cmd_list(args[1] if len(args) > 1 else None)
    elif args[0] == "customers":
        cmd_customers()
    elif args[0] == "search" and len(args) >= 2:
        cmd_search(args[1])
    elif args[0] == "info" and len(args) >= 2:
        # info identity [identity_type] [product_id]
        identity      = args[1]
        identity_type = args[2] if len(args) > 2 and args[2] in ("email","sms","google") else "email"
        product_id    = args[3] if len(args) > 3 else (
                        args[2] if len(args) > 2 and args[2] not in ("email","sms","google") else None)
        cmd_info(identity, identity_type, product_id)
    elif args[0] == "revoke" and len(args) >= 2:
        identity      = args[1]
        identity_type = args[2] if len(args) > 2 and args[2] in ("email","sms","google") else "email"
        product_id    = args[3] if len(args) > 3 else (
                        args[2] if len(args) > 2 and args[2] not in ("email","sms","google") else None)
        cmd_revoke(identity, identity_type, product_id)
    elif args[0] == "activate" and len(args) >= 2:
        identity      = args[1]
        identity_type = args[2] if len(args) > 2 and args[2] in ("email","sms","google") else "email"
        product_id    = args[3] if len(args) > 3 else (
                        args[2] if len(args) > 2 and args[2] not in ("email","sms","google") else None)
        cmd_activate(identity, identity_type, product_id)
    elif args[0] == "refund" and len(args) >= 2:
        cmd_refund(args[1])
    elif args[0] == "products":
        cmd_products()
    elif args[0] == "addproduct":
        cmd_addproduct()
    elif args[0] == "otps":
        cmd_otps()
    elif args[0] == "backup":
        flags     = set(args[1:])
        live_only = "--live-only" in flags
        hist_only = "--hist-only" in flags
        cmd_backup(
            live = not hist_only,
            hist = not live_only
        )
    else:
        print(USAGE)