"""
admin.py — CLI admin tool
Commands:
  python admin.py summary                        revenue per product
  python admin.py list                           all licenses
  python admin.py list TOOL1                     licenses for one product
  python admin.py info email@x.com               all licenses for customer
  python admin.py info email@x.com TOOL1         one product for customer
  python admin.py revoke email@x.com             revoke all products
  python admin.py revoke email@x.com TOOL1       revoke one product
  python admin.py activate email@x.com           re-enable all
  python admin.py activate email@x.com TOOL1     re-enable one product
  python admin.py refund pay_ABC123              mark refunded + revoke
  python admin.py products                       list all products
  python admin.py addproduct                     interactive: add new product
  python admin.py otps                           list recent OTP activity
"""

import sqlite3, sys, time
from datetime import datetime

DB_PATH = "licenses.db"

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

def sep(char="-", width=92):
    print(char * width)

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_summary():
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            pr.product_id, pr.name,
            COUNT(DISTINCT l.id)          AS total_licenses,
            COALESCE(SUM(l.is_active), 0) AS active_licenses,
            COUNT(DISTINCT l.customer_id) AS unique_customers,
            COALESCE(SUM(CASE WHEN p.currency='INR' AND p.is_refunded=0
                              THEN p.amount ELSE 0 END), 0) AS revenue_inr,
            COALESCE(SUM(CASE WHEN p.currency='USD' AND p.is_refunded=0
                              THEN p.amount ELSE 0 END), 0) AS revenue_usd,
            COALESCE(SUM(CASE WHEN p.is_refunded=1 THEN 1 ELSE 0 END), 0) AS refunds
        FROM products pr
        LEFT JOIN licenses l ON l.product_id  = pr.product_id
        LEFT JOIN payments p ON p.id          = l.payment_id
        GROUP BY pr.product_id ORDER BY pr.created_at
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
    print()


def cmd_list(product_id=None):
    conn  = get_conn()
    where = "WHERE l.product_id=?" if product_id else ""
    args  = (product_id,) if product_id else ()
    rows  = conn.execute(f"""
        SELECT l.product_id, cu.email, cu.country,
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
    print(f"  {'PRODUCT':<8} {'EMAIL':<28} {'STATUS':<9} {'SOURCE':<10} "
          f"{'PLAN':<10} {'AMT':>7} {'PAID':<17} {'LAST SEEN':<17} VFY")
    sep()
    for r in rows:
        amt  = f"₹{r['amount']:.0f}" if r['currency']=='INR' else f"${r['amount']:.2f}"
        stat = status_str(r['is_active'], r['expires_at'])
        note = f" [{r['revoke_reason']}]" if r['revoke_reason'] else ""
        print(f"  {r['product_id']:<8} {r['email']:<28} {stat:<9} "
              f"{r['source']:<10} {r['plan']:<10} {amt:>7} "
              f"{ts(r['paid_at']):<17} {ts(r['last_seen_at']):<17} "
              f"{r['verify_count']}{note}")
    sep()
    print(f"  Total: {len(rows)}\n")


def cmd_info(email, product_id=None):
    conn  = get_conn()
    cust  = conn.execute("SELECT * FROM customers WHERE email=?",
                         (email.lower(),)).fetchone()
    if not cust:
        print(f"\n  No customer found: {email}\n")
        conn.close()
        return

    # OTP status for this email
    otp_row = conn.execute("SELECT otp, sent_at, attempts, verified FROM email_otps WHERE email=?",
                           (email.lower(),)).fetchone()

    extra = "AND l.product_id=?" if product_id else ""
    args  = (email.lower(), product_id) if product_id else (email.lower(),)
    rows  = conn.execute(f"""
        SELECT l.product_id, p.source, p.payment_ref, p.amount, p.currency,
               p.plan, p.paid_at, p.is_refunded,
               l.machine_id, l.is_active, l.expires_at,
               l.activated_at, l.last_seen_at, l.last_seen_ip,
               l.verify_count, l.revoked_at, l.revoke_reason
        FROM licenses l
        JOIN payments  p  ON p.id  = l.payment_id
        JOIN customers cu ON cu.id = l.customer_id
        WHERE cu.email=? {extra}
        ORDER BY l.product_id, l.activated_at
    """, args).fetchall()
    conn.close()

    print()
    sep("=")
    print(f"  CUSTOMER: {cust['email']}")
    sep("=")
    print(f"  Name       : {cust['full_name'] or '—'}")
    print(f"  Country    : {cust['country'] or '—'}")
    print(f"  First seen : {ts(cust['created_at'])}")
    print(f"  Licenses   : {len(rows)}")

    if otp_row:
        otp_status = "VERIFIED ✓" if otp_row["verified"] else "not verified"
        print(f"  OTP status : {otp_status}  (sent {ts(otp_row['sent_at'])}, "
              f"attempts: {otp_row['attempts']})")
    else:
        print(f"  OTP status : no OTP record")
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


def cmd_revoke(email, product_id=None):
    from database import revoke_license
    n     = revoke_license(email.lower(), product_id=product_id, reason="manual")
    scope = f"[{product_id}]" if product_id else "[ALL PRODUCTS]"
    print(f"\n  Revoked {n} license(s) for {email} {scope}\n")


def cmd_activate(email, product_id=None):
    conn = get_conn()
    if product_id:
        c = conn.execute("""
            UPDATE licenses SET is_active=1, revoked_at=NULL, revoke_reason=NULL
            WHERE customer_id=(SELECT id FROM customers WHERE email=?)
              AND product_id=? AND is_active=0
        """, (email.lower(), product_id))
    else:
        c = conn.execute("""
            UPDATE licenses SET is_active=1, revoked_at=NULL, revoke_reason=NULL
            WHERE customer_id=(SELECT id FROM customers WHERE email=?)
              AND is_active=0
        """, (email.lower(),))
    conn.commit()
    n     = c.rowcount
    scope = f"[{product_id}]" if product_id else "[ALL PRODUCTS]"
    conn.close()
    print(f"\n  Re-activated {n} license(s) for {email} {scope}\n")


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
    """Show recent OTP activity — useful for debugging."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT email, sent_at, attempts, verified
        FROM email_otps
        ORDER BY sent_at DESC
        LIMIT 50
    """).fetchall()
    conn.close()
    print()
    print("  RECENT OTP ACTIVITY (latest 50)")
    sep("=")
    print(f"  {'EMAIL':<32} {'SENT':<17} {'VERIFIED':<10} {'ATTEMPTS'}")
    sep()
    for r in rows:
        verified = "YES ✓" if r['verified'] else "no"
        print(f"  {r['email']:<32} {ts(r['sent_at']):<17} {verified:<10} {r['attempts']}")
    sep()
    print(f"  Total: {len(rows)}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
  python admin.py summary                        revenue per product
  python admin.py list                           all licenses
  python admin.py list TOOL1                     licenses for TOOL1 only
  python admin.py info email@x.com               all licenses for customer
  python admin.py info email@x.com TOOL1         one product for customer
  python admin.py revoke email@x.com             revoke all products
  python admin.py revoke email@x.com TOOL1       revoke one product
  python admin.py activate email@x.com           re-enable all
  python admin.py activate email@x.com TOOL1     re-enable one product
  python admin.py refund pay_ABC123              mark refunded + revoke
  python admin.py products                       list all products
  python admin.py addproduct                     add a new product
  python admin.py otps                           recent OTP activity
"""

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(USAGE)
    elif args[0] == "summary":
        cmd_summary()
    elif args[0] == "list":
        cmd_list(args[1] if len(args) > 1 else None)
    elif args[0] == "info" and len(args) >= 2:
        cmd_info(args[1], args[2] if len(args) > 2 else None)
    elif args[0] == "revoke" and len(args) >= 2:
        cmd_revoke(args[1], args[2] if len(args) > 2 else None)
    elif args[0] == "activate" and len(args) >= 2:
        cmd_activate(args[1], args[2] if len(args) > 2 else None)
    elif args[0] == "refund" and len(args) >= 2:
        cmd_refund(args[1])
    elif args[0] == "products":
        cmd_products()
    elif args[0] == "addproduct":
        cmd_addproduct()
    elif args[0] == "otps":
        cmd_otps()
    else:
        print(USAGE)