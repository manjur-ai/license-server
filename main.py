"""
main.py — License Server
Full flow:
  Step 1. POST /send-otp    → generates OTP, sends to email OR phone
  Step 2. POST /verify-otp  → user submits OTP → identity marked verified
  Step 3. POST /register    → payment verified + identity verified → license created
  Step 4. POST /verify      → silent check on every EXE launch (no OTP needed)

identity_type values:
  "email"  → identity is an email address  e.g. "user@gmail.com"
  "sms"    → identity is a phone number    e.g. "+919876543210"
  "google" → identity is "google:<uid>"    (future — no OTP, uses Google token)

Environment variables (set on Railway):
  SHARED_SECRET         32-byte hex — must match C++ client
  ADMIN_TOKEN           your admin password
  RAZORPAY_KEY_ID       rzp_live_xxxx
  RAZORPAY_KEY_SECRET   razorpay secret
  SUPPORT_EMAIL         email shown in email footers
  TEST_MODE             true (local testing only — NEVER set on Railway prod)
  EMAIL_SEND_METHODS    JSON array — see email_sender.py
  SMS_SEND_METHODS      JSON array — see sms_sender.py
"""

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
import os, time, json, base64, secrets, hashlib, hmac
import requests as http_requests
from email_sender import send_email as _send_email, EMAIL_SEND_METHODS
from sms_sender   import send_sms_otp, SMS_SEND_METHODS
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from database import (
    init_db, get_product, normalize_identity,
    generate_otp, store_otp, is_valid_otp, is_identity_verified,
    register_license, verify_license,
    revoke_license, mark_refunded, backup_db,
    list_machines, unlink_machines,
    check_otp_rate, upsert_product, list_products, delete_product,
    create_coupon, validate_coupon, redeem_coupon, list_coupons,
    get_customer_profile, get_stats, VALID_PLANS,
)

from admin_ui import admin_app
app = FastAPI()
app.mount("/admin", admin_app)

# ── DB startup ────────────────────────────────────────────────────────────────
_DB_INIT_ERROR: str = ""
try:
    init_db()
except Exception as _e:
    _DB_INIT_ERROR = str(_e)
    print(f"FATAL: database init failed — {_DB_INIT_ERROR}")

# ── Config ────────────────────────────────────────────────────────────────────
SHARED_SECRET       = bytes.fromhex(os.environ.get("SHARED_SECRET",
    "8cfaf7568ebd0d6f5557552efa46e43dfa57bb9618635753c224d3f38b3ac158"))
ADMIN_TOKEN         = os.environ.get("ADMIN_TOKEN",         "change_this")
RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID",     "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
SUPPORT_EMAIL       = os.environ.get("SUPPORT_EMAIL",       "support@toolfy.com")
TEST_MODE           = os.environ.get("TEST_MODE", "false").lower() == "true"
MAX_TS_DRIFT        = 30

# ── Crypto ────────────────────────────────────────────────────────────────────

def aes_decrypt(b64: str) -> dict:
    try:
        raw = base64.b64decode(b64)
        pt  = AESGCM(SHARED_SECRET).decrypt(raw[:12], raw[12:], None)
        return json.loads(pt)
    except:
        return {}

def aes_encrypt(data: dict) -> str:
    nonce = secrets.token_bytes(12)
    ct    = AESGCM(SHARED_SECRET).encrypt(nonce, json.dumps(data).encode(), None)
    return base64.b64encode(nonce + ct).decode()

def valid_ts(ts) -> bool:
    try:    return abs(time.time() - float(ts)) <= MAX_TS_DRIFT
    except: return False

def make_unique_key(product_id: str, identity: str, identity_type: str, machine_id: str) -> str:
    identity = normalize_identity(identity, identity_type)
    data = f"{product_id}:{identity_type}:{identity}:{machine_id}".encode()
    return hmac.new(SHARED_SECRET, data, hashlib.sha256).hexdigest()

# ── Notification dispatch ─────────────────────────────────────────────────────
# Routes to email or SMS based on identity_type

def send_email(to: str, subject: str, html_body: str) -> dict:
    """Returns SendResult dict: {ok, reason, provider}"""
    ok = _send_email(to, subject, html_body, test_mode=TEST_MODE)
    return {
        "ok":       ok,
        "reason":   "sent" if ok else ("not_configured" if not EMAIL_SEND_METHODS else "all_methods_failed"),
        "provider": "email",
    }

def notify_otp(identity: str, identity_type: str, otp: str, product_name: str) -> dict:
    """
    Send OTP via email or SMS depending on identity_type.
    Returns a SendResult dict: {ok, reason, provider}

    Possible reasons:
      "sent"               — OTP delivered successfully
      "not_configured"     — email/SMS provider not set up in env vars
      "all_methods_failed" — every configured provider failed to deliver
    """
    if identity_type == "sms":
        ok = send_sms_otp(identity, otp, test_mode=TEST_MODE)
        return {
            "ok":       ok,
            "reason":   "sent" if ok else "all_methods_failed",
            "provider": "sms",
        }
    else:
        return send_email(
            to        = identity,
            subject   = f"Your verification code: {otp}",
            html_body = f"""
            <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
                <h2 style="color:#1d4ed8">Verification Code</h2>
                <p>You requested to activate <b>{product_name}</b>.</p>
                <p>Enter this code in the application:</p>
                <div style="font-size:40px;font-weight:bold;letter-spacing:10px;
                            color:#1d4ed8;background:#eff6ff;padding:24px;
                            border-radius:10px;text-align:center;margin:16px 0">
                    {otp}
                </div>
                <p style="color:#6b7280;font-size:13px">
                    This code expires in <b>10 minutes</b>.<br>
                    If you didn't request this, ignore this message.<br><br>
                    Support: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
                </p>
            </div>
            """
        )

def notify_activated(identity: str, identity_type: str, product_name: str, plan: str):
    """Send activation confirmation — email only (SMS too long)."""
    if identity_type != "email":
        return   # Skip SMS for activation — too long for SMS
    send_email(
        to        = identity,
        subject   = f"✅ Your {product_name} license is activated",
        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
            <h2 style="color:#16a34a">License Activated!</h2>
            <p>Your <b>{product_name}</b> license is now active.</p>
            <table style="border-collapse:collapse;width:100%;margin:16px 0">
                <tr style="background:#f0fdf4">
                    <td style="padding:10px;border:1px solid #bbf7d0"><b>Product</b></td>
                    <td style="padding:10px;border:1px solid #bbf7d0">{product_name}</td>
                </tr>
                <tr>
                    <td style="padding:10px;border:1px solid #e5e7eb"><b>Plan</b></td>
                    <td style="padding:10px;border:1px solid #e5e7eb">{plan.title()}</td>
                </tr>
                <tr style="background:#f9fafb">
                    <td style="padding:10px;border:1px solid #e5e7eb"><b>Note</b></td>
                    <td style="padding:10px;border:1px solid #e5e7eb">
                        This license is tied to your current machine.
                    </td>
                </tr>
            </table>
            <p style="color:#6b7280;font-size:13px">
                Changing your PC? Contact us to transfer.<br>
                Support: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
            </p>
        </div>
        """
    )

def notify_revoked(identity: str, identity_type: str, product_name: str, reason: str):
    if identity_type != "email":
        return
    reason_map = {
        "refund": "your payment was refunded",
        "abuse":  "a violation of our terms was detected",
        "manual": "an administrative action was taken",
    }
    send_email(
        to        = identity,
        subject   = f"⚠️ Your {product_name} license has been deactivated",
        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
            <h2 style="color:#dc2626">License Deactivated</h2>
            <p>Your <b>{product_name}</b> license was deactivated because
               {reason_map.get(reason, "an administrative action")}.</p>
            <p>If this is a mistake, contact us immediately.</p>
            <p style="color:#6b7280;font-size:13px">
                Support: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
            </p>
        </div>
        """
    )

def notify_refund(identity: str, identity_type: str, product_name: str):
    if identity_type != "email":
        return
    send_email(
        to        = identity,
        subject   = f"💰 Refund processed — {product_name}",
        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
            <h2 style="color:#7c3aed">Refund Processed</h2>
            <p>Your refund for <b>{product_name}</b> has been processed
               and your license has been deactivated.</p>
            <p>Amount will appear in your account within 5–7 business days.</p>
            <p style="color:#6b7280;font-size:13px">
                Questions? <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
            </p>
        </div>
        """
    )

# ── Payment verification ───────────────────────────────────────────────────────

def verify_razorpay_payment(payment_id: str, min_paise: int) -> bool:
    if not RAZORPAY_KEY_ID: return False
    try:
        r = http_requests.get(
            f"https://api.razorpay.com/v1/payments/{payment_id}",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=10
        )
        d = r.json()
        return d.get("status") == "captured" and d.get("amount", 0) >= min_paise
    except: return False

def verify_gumroad_key(license_key: str, identity: str, identity_type: str,
                       gumroad_product_id: str) -> bool:
    if not gumroad_product_id: return False
    try:
        r = http_requests.post(
            "https://api.gumroad.com/v2/licenses/verify",
            data={"product_id": gumroad_product_id,
                  "license_key": license_key.strip(),
                  "increment_uses_count": "false"}, timeout=10
        )
        d = r.json()
        if not d.get("success"): return False
        if d.get("purchase", {}).get("refunded"): return False
        # Email match only when identity_type is email
        if identity_type == "email":
            purchase_email = d.get("purchase", {}).get("email", "").lower()
            if purchase_email and purchase_email != identity.lower(): return False
        return True
    except: return False

# ── Request model ──────────────────────────────────────────────────────────────

class Payload(BaseModel):
    data: str   # AES-GCM encrypted JSON, base64 encoded

# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 1 — SEND OTP
# ═════════════════════════════════════════════════════════════════════════════

# ── DB guard: called at the start of every endpoint ──────────────────────────
def _db_ok():
    """Returns (True, "") or (False, reason_string) based on DB init status."""
    if _DB_INIT_ERROR:
        return False, "database_not_configured"
    return True, ""


@app.post("/send-otp")
async def send_otp(payload: Payload, request: Request):
    """
    Step 1 — EXE calls this first with the user's identity.

    EXE sends (encrypted):
        {
          identity:       "user@gmail.com"  OR  "+919876543210",
          identity_type:  "email" | "sms",
          product_id:     "TOOL1",
          timestamp:      1234567890
        }

    Returns (encrypted):
        { ok, reason }
        reason: sent | unknown_product | missing_fields | expired
        In TEST_MODE also returns: test_otp
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    product_id    = req.get("product_id", "")

    if not identity or not product_id:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    if identity_type not in ("email", "sms"):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unsupported_identity_type"})})

    ok_db, db_reason = _db_ok()
    if not ok_db:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": db_reason})})

    prod = get_product(product_id)
    if not prod:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_product"})})

    # ── OTP rate limiting ───────────────────────────────────────────
    ip = request.client.host
    rate = check_otp_rate(identity, identity_type, ip)
    if not rate["ok"]:
        return JSONResponse({"data": aes_encrypt({
            "ok": False,
            "reason": rate["reason"],
            "wait_seconds": rate.get("wait_seconds", 0),
        })})

    otp = generate_otp()
    store_otp(identity, identity_type, otp)
    delivery = notify_otp(identity, identity_type, otp, prod["name"])

    # ── Delivery failed — surface the real reason to the client ──────────
    if not delivery["ok"]:
        # Map internal reason → user-facing reason codes
        reason_map = {
            "not_configured":     "delivery_not_configured",
            "all_methods_failed": "delivery_failed",
        }
        reason = reason_map.get(delivery["reason"], "delivery_failed")
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": reason})})

    trial_days = prod.get("trial_days", 0)
    if TEST_MODE:
        return JSONResponse({"data": aes_encrypt({
            "ok": True, "reason": "sent",
            "trial_days": trial_days,
            "test_otp": otp,
        })})
    return JSONResponse({"data": aes_encrypt({
        "ok": True, "reason": "sent",
        "trial_days": trial_days,
    })})


# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 2 — VERIFY OTP
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/verify-otp")
async def verify_otp(payload: Payload, request: Request):
    """
    Step 2 — User types the OTP they received.

    EXE sends (encrypted):
        {
          identity:       "user@gmail.com"  OR  "+919876543210",
          identity_type:  "email" | "sms",
          otp:            "123456",
          timestamp:      1234567890
        }

    Returns (encrypted):
        { ok, reason }
        reason: verified | already_verified | otp_not_found |
                otp_expired | invalid_otp | max_attempts_exceeded
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    otp           = req.get("otp", "").strip()

    if not identity or not otp:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    result = is_valid_otp(identity, identity_type, otp)
    return JSONResponse({"data": aes_encrypt(result)})


# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 3 — REGISTER
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/register")
async def register(payload: Payload, request: Request):
    """
    Step 3 — Called after OTP verified + payment done.

    EXE sends (encrypted):
        {
          product_id:     "TOOL1",
          identity:       "user@gmail.com"  OR  "+919876543210",
          identity_type:  "email" | "sms",
          machine_id:     "sha256hash",
          timestamp:      1234567890,
          source:         "razorpay" | "gumroad" | "test",

          -- if source == "razorpay":
          payment_id:     "pay_xxx"

          -- if source == "gumroad":
          license_key:    "XXXX-XXXX-XXXX-XXXX"
        }

    Returns (encrypted):
        { ok, reason }
        reason: registered | already_registered | identity_not_verified |
                payment_invalid | unknown_product | max_machines_reached |
                payment_already_used
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    product_id    = req.get("product_id", "")
    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    machine_id    = req.get("machine_id", "")
    source        = req.get("source", "")
    computer_name = req.get("computer_name", "").strip()
    os_username   = req.get("os_username",   "").strip()
    plan          = req.get("plan", "lifetime").strip().lower()
    coupon_code   = req.get("coupon_code", "").strip()
    ip            = request.client.host

    if plan not in VALID_PLANS:
        plan = "lifetime"   # silently normalise unknown plans

    # Build human-readable machine label: "ComputerName / username"
    parts = [p for p in [computer_name, os_username] if p]
    machine_label = " / ".join(parts) if parts else None

    if not all([product_id, identity, machine_id, source]):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    # ── Identity must be OTP-verified ────────────────────────────
    ok_db, db_reason = _db_ok()
    if not ok_db:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": db_reason})})

    if not is_identity_verified(identity, identity_type):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "identity_not_verified"})})

    # ── Product check ────────────────────────────────────────────
    prod = get_product(product_id)
    if not prod:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_product"})})

    # ── Payment verification ─────────────────────────────────────
    payment_ref, amount, currency = "", 0.0, "INR"

    # ── Coupon validation (before payment) ──────────────────────────
    coupon_result = None
    if coupon_code:
        coupon_result = validate_coupon(coupon_code, product_id)
        if not coupon_result["ok"]:
            return JSONResponse({"data": aes_encrypt({
                "ok": False, "reason": coupon_result["reason"]})})
        # Coupon can override plan (e.g. coupon that upgrades trial→lifetime)
        if coupon_result.get("plan_override"):
            plan = coupon_result["plan_override"]

    if source == "trial":
        # Free trial — no payment needed, but product must allow trials
        trial_days = prod.get("trial_days", 0)
        if not trial_days:
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "trial_not_available"})})
        payment_ref      = f"trial_{int(time.time())}"
        amount, currency = 0.0, "INR"
        plan             = "trial"

    elif source == "razorpay":
        payment_ref      = req.get("payment_id", "")
        amount, currency = prod["price_inr"], "INR"
        # Apply coupon discount
        if coupon_result:
            pct_off = coupon_result.get("discount_pct", 0)
            flat    = coupon_result.get("discount_inr", 0)
            amount  = max(0, amount * (1 - pct_off/100) - flat)
        min_paise = int(amount * 100 * 0.9)  # 10% tolerance
        if not verify_razorpay_payment(payment_ref, min_paise):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})

    elif source == "gumroad":
        payment_ref      = req.get("license_key", "")
        amount, currency = prod["price_usd"], "USD"
        if coupon_result:
            pct_off = coupon_result.get("discount_pct", 0)
            flat    = coupon_result.get("discount_usd", 0)
            amount  = max(0, amount * (1 - pct_off/100) - flat)
        if not verify_gumroad_key(payment_ref, identity, identity_type,
                                  prod.get("gumroad_product_id", "")):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})

    elif source == "coupon_only":
        # 100% discount via coupon — no payment gateway
        if not coupon_result or coupon_result.get("discount_pct", 0) < 100:
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})
        payment_ref      = f"coupon_{coupon_code}_{int(time.time())}"
        amount, currency = 0.0, "INR"

    elif source == "test":
        if not TEST_MODE:
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "test_mode_disabled"})})
        payment_ref      = req.get("payment_id", f"test_{int(time.time())}")
        amount, currency = 0.0, "INR"

    else:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_source"})})

    # ── Register in DB ───────────────────────────────────────────
    unique_key = make_unique_key(product_id, identity, identity_type, machine_id)
    result = register_license(
        product_id, identity, identity_type, machine_id, unique_key,
        source, payment_ref, amount, currency,
        plan=plan, ip_address=ip, machine_label=machine_label
    )

    # ── Post-registration ────────────────────────────────────────
    if result.get("ok") and result["reason"] == "registered":
        notify_activated(identity, identity_type, prod["name"], plan)
        if coupon_code and coupon_result and coupon_result["ok"]:
            redeem_coupon(coupon_code)

    return JSONResponse({"data": aes_encrypt(result)})


# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 4 — VERIFY (silent check on every EXE launch)
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/verify")
async def verify(payload: Payload, request: Request):
    """
    Step 4 — Called silently every time the EXE starts.

    EXE sends (encrypted):
        {
          product_id:     "TOOL1",
          identity:       "user@gmail.com"  OR  "+919876543210",
          identity_type:  "email" | "sms",
          machine_id:     "sha256hash",
          timestamp:      1234567890
        }

    Returns (encrypted):
        { ok, reason }
        reason: ok | not_found | revoked | expired | wrong_product
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    product_id    = req.get("product_id", "")
    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    machine_id    = req.get("machine_id", "")
    ip            = request.client.host

    if not all([product_id, identity, machine_id]):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    ok_db, db_reason = _db_ok()
    if not ok_db:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": db_reason})})

    unique_key = make_unique_key(product_id, identity, identity_type, machine_id)
    result     = verify_license(product_id, unique_key, ip)
    return JSONResponse({"data": aes_encrypt(result)})




# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 5 — LIST MACHINES
#  Returns all active machines for this identity+product.
#  Requires identity to be OTP-verified in this session.
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/list-machines")
async def ep_list_machines(payload: Payload, request: Request):
    """
    Called when max_machines_reached is returned by /register.
    Client shows the list so the user can choose which machine to unlink.

    EXE sends (encrypted):
        {
          product_id:     "TOOL1",
          identity:       "user@gmail.com",
          identity_type:  "email",
          timestamp:      1234567890
        }

    Returns (encrypted):
        {
          ok:       true,
          machines: [
            {
              unique_key:    "abc123...",
              machine_label: "DESKTOP-ABC / john",
              activated_at:  1700000000.0,
              last_seen_at:  1700100000.0
            },
            ...
          ]
        }

    Failure reasons:
        expired | missing_fields | identity_not_verified | database_not_configured
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    identity      = req.get("identity",      "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    product_id    = req.get("product_id",    "")

    if not all([identity, product_id]):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    ok_db, db_reason = _db_ok()
    if not ok_db:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": db_reason})})

    # Must have completed OTP verification before listing machines
    if not is_identity_verified(identity, identity_type):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "identity_not_verified"})})

    machines = list_machines(identity, identity_type, product_id)
    return JSONResponse({"data": aes_encrypt({"ok": True, "machines": machines})})


# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 6 — UNLINK MACHINES
#  Deactivates selected machines. After this, client retries /register.
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/unlink")
async def ep_unlink(payload: Payload, request: Request):
    """
    User selects machines to unlink from the list returned by /list-machines.
    After successful unlink, the EXE retries /register for the current machine.

    EXE sends (encrypted):
        {
          product_id:     "TOOL1",
          identity:       "user@gmail.com",
          identity_type:  "email",
          unique_keys:    ["abc123...", "def456..."],   ← machines to remove
          timestamp:      1234567890
        }

    Returns (encrypted):
        {
          ok:        true,
          unlinked:  1,     ← number of machines successfully unlinked
          not_found: 0      ← keys that didn't match (already gone, or not owned)
        }

    Failure reasons:
        expired | missing_fields | identity_not_verified |
        no_keys_provided | no_owned_keys_found | database_not_configured
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    identity      = req.get("identity",      "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    product_id    = req.get("product_id",    "")
    unique_keys   = req.get("unique_keys",   [])

    if not all([identity, product_id]) or not isinstance(unique_keys, list):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    if not unique_keys:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "no_keys_provided"})})

    # Hard cap: can't unlink more than 10 at once (sanity guard)
    unique_keys = unique_keys[:10]

    ok_db, db_reason = _db_ok()
    if not ok_db:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": db_reason})})

    # Must have completed OTP verification before unlinking
    if not is_identity_verified(identity, identity_type):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "identity_not_verified"})})

    result = unlink_machines(identity, identity_type, product_id, unique_keys)
    return JSONResponse({"data": aes_encrypt(result)})


# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT — VALIDATE COUPON  (call before payment to show discount)
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/validate-coupon")
async def ep_validate_coupon(payload: Payload):
    """
    EXE sends: { product_id, coupon_code, timestamp }
    Returns:   { ok, discount_pct, discount_inr, discount_usd,
                 plan_override, reason }
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})
    ok_db, db_reason = _db_ok()
    if not ok_db:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": db_reason})})

    product_id  = req.get("product_id", "")
    coupon_code = req.get("coupon_code", "").strip()
    if not coupon_code:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "no_code"})})

    result = validate_coupon(coupon_code, product_id)
    return JSONResponse({"data": aes_encrypt(result)})


# ═════════════════════════════════════════════════════════════════════════════
#  ENDPOINT — /me  (customer self-service — OTP verified)
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/me")
async def ep_me(payload: Payload):
    """
    Returns all licenses, payment history and machine list for the identity.
    Requires OTP verification first.

    EXE sends: { identity, identity_type, timestamp }
    Returns:   { ok, customer, licenses, payments, total_licenses, active_licenses }
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()

    if not identity:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    ok_db, db_reason = _db_ok()
    if not ok_db:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": db_reason})})

    if not is_identity_verified(identity, identity_type):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "identity_not_verified"})})

    result = get_customer_profile(identity, identity_type)
    return JSONResponse({"data": aes_encrypt(result)})


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN — PRODUCT CRUD
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/admin/product")
async def admin_product_upsert(payload: Payload):
    """
    Create or update a product.
    Sends: { admin_token, product_id, name, price_inr, price_usd,
              razorpay_link?, gumroad_product_id?, gumroad_link?,
              max_machines?, trial_days?, is_active? }
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    product_id = req.get("product_id", "").strip().upper()
    name       = req.get("name", "").strip()
    if not product_id or not name:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    result = upsert_product(
        product_id         = product_id,
        name               = name,
        price_inr          = float(req.get("price_inr", 0)),
        price_usd          = float(req.get("price_usd", 0)),
        razorpay_link      = req.get("razorpay_link"),
        gumroad_product_id = req.get("gumroad_product_id"),
        gumroad_link       = req.get("gumroad_link"),
        max_machines       = int(req.get("max_machines", 1)),
        trial_days         = int(req.get("trial_days", 0)),
        is_active          = int(req.get("is_active", 1)),
    )
    return JSONResponse({"data": aes_encrypt(result)})


@app.post("/admin/product/delete")
async def admin_product_delete(payload: Payload):
    """Soft-delete a product. Sends: { admin_token, product_id }"""
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})
    result = delete_product(req.get("product_id", "").strip().upper())
    return JSONResponse({"data": aes_encrypt(result)})


@app.post("/admin/products")
async def admin_list_products(payload: Payload):
    """List all products. Sends: { admin_token, include_inactive? }"""
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})
    products = list_products(include_inactive=req.get("include_inactive", False))
    return JSONResponse({"data": aes_encrypt({"ok": True, "products": products})})


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN — COUPON MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/admin/coupon")
async def admin_create_coupon(payload: Payload):
    """
    Create a coupon code.
    Sends: { admin_token, code, product_id?, discount_pct?,
              discount_flat_inr?, discount_flat_usd?,
              plan_override?, max_uses?, valid_from?, valid_until? }
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    code = req.get("code", "").strip()
    if not code:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_code"})})

    result = create_coupon(
        code               = code,
        product_id         = req.get("product_id"),
        discount_pct       = float(req.get("discount_pct", 0)),
        discount_flat_inr  = float(req.get("discount_flat_inr", 0)),
        discount_flat_usd  = float(req.get("discount_flat_usd", 0)),
        plan_override      = req.get("plan_override"),
        max_uses           = int(req.get("max_uses", 1)),
        valid_from         = req.get("valid_from"),
        valid_until        = req.get("valid_until"),
    )
    return JSONResponse({"data": aes_encrypt(result)})


@app.post("/admin/coupons")
async def admin_list_coupons(payload: Payload):
    """List all coupons. Sends: { admin_token }"""
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})
    return JSONResponse({"data": aes_encrypt({"ok": True, "coupons": list_coupons()})})


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN — STATS
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/admin/stats")
async def admin_stats(payload: Payload):
    """Returns dashboard numbers. Sends: { admin_token }"""
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})
    return JSONResponse({"data": aes_encrypt({"ok": True, **get_stats()})})

@app.post("/admin/customer")
async def admin_customer(payload: Payload):
    """Look up full customer profile. Sends: { admin_token, identity, identity_type }"""
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})
    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    from database import get_customer_profile
    result = get_customer_profile(identity, identity_type)
    return JSONResponse({"data": aes_encrypt(result)})


@app.post("/admin/revoke")
async def admin_revoke(payload: Payload):
    """
    Admin — revoke a license.

    Sends (encrypted):
        {
          admin_token:    "xxx",
          identity:       "user@gmail.com" OR "+919876543210",
          identity_type:  "email" | "sms",   (default: "email")
          product_id:     "TOOL1",            (optional — revokes all if omitted)
          reason:         "manual" | "abuse" | "refund"
        }
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    product_id    = req.get("product_id", None)
    reason        = req.get("reason", "manual")

    prod_name = "Your Tool"
    if product_id:
        prod = get_product(product_id)
        if prod: prod_name = prod["name"]

    n = revoke_license(identity, identity_type, product_id=product_id, reason=reason)
    if n > 0:
        notify_revoked(identity, identity_type, prod_name, reason)

    return JSONResponse({"data": aes_encrypt({"ok": True, "revoked": n})})


@app.post("/admin/refund")
async def admin_refund(payload: Payload):
    """
    Admin — mark payment refunded, revoke license, notify customer.

    Sends (encrypted):
        {
          admin_token:    "xxx",
          payment_ref:    "pay_xxx",
          identity:       "user@gmail.com",
          identity_type:  "email",
          product_name:   "Image Converter Pro"
        }
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    payment_ref   = req.get("payment_ref", "")
    identity      = req.get("identity", "").strip()
    identity_type = req.get("identity_type", "email").lower().strip()
    product_name  = req.get("product_name", "Your Tool")

    ok = mark_refunded(payment_ref)
    if ok and identity:
        notify_refund(identity, identity_type, product_name)

    return JSONResponse({"data": aes_encrypt({"ok": ok, "refunded": ok})})


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN — BACKUP DB
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/admin/backup_db")
async def admin_backup_db(payload: Payload):
    """
    Admin — trigger an immediate full backup right now.
    Run this BEFORE every redeploy to ensure LIVE is up to date.

    Sends (encrypted):
        {
          admin_token:  "xxx",
          live:         true,   ← overwrite licenses_LIVE.db    (default true)
          hist:         true    ← create   licenses_PREV_*.db   (default true)
        }

    Returns (encrypted):
        { ok: true,  live: "licenses_LIVE.db", hist: "licenses_PREV_....db" }
        { ok: false, error: "reason" }

    Provider support:
        sqlite     → uploads to Google Drive (requires backup_gdrive=true)
        turso      → not needed (Turso is persistent, survives redeploys)
        postgresql → planned (pg_dump to Drive)
        mysql      → planned (mysqldump to Drive)
        mongodb    → planned (mongodump to Drive)
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    upload_live = req.get("live", True)
    upload_hist = req.get("hist", True)

    result = backup_db(upload_live=upload_live, upload_hist=upload_hist)
    return JSONResponse({"data": aes_encrypt(result)})




# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN — READ-ONLY DATA BROWSER  (paginated, latest-first, 500 rows/page)
# ═════════════════════════════════════════════════════════════════════════════

import sqlite3 as _sqlite3

def _sqlite_browse(sql_data: str, sql_count: str, params_data, params_count, offset: int, limit: int):
    """Generic SQLite paginated fetch helper."""
    try:
        db_path = os.environ.get("DB_PATH", "licenses.db")
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        total = conn.execute(sql_count, params_count).fetchone()[0]
        rows  = [dict(r) for r in conn.execute(sql_data, params_data).fetchall()]
        conn.close()
        return {"ok": True, "rows": rows, "total": total,
                "offset": offset, "limit": limit,
                "has_more": (offset + limit) < total}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.post("/admin/browse/licenses")
async def admin_browse_licenses(payload: Payload):
    """
    Read-only paginated license list. Latest activated first.
    Sends: { admin_token, offset?, limit? }
    Returns: { ok, rows, total, offset, limit, has_more }
    Each row: product_id, identity, identity_type, plan, is_active,
              activated_at, expires_at, last_seen_at, verify_count,
              revoke_reason, source, amount, currency, machine_id
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    offset = max(0, int(req.get("offset", 0)))
    limit  = min(500, max(1, int(req.get("limit", 500))))

    sql_data = """
        SELECT l.product_id,
               cu.identity, cu.identity_type,
               l.plan, l.is_active, l.activated_at, l.expires_at,
               l.last_seen_at, l.verify_count, l.revoke_reason,
               l.machine_id, l.machine_label,
               p.source, p.amount, p.currency, p.payment_ref
        FROM licenses l
        JOIN customers cu ON cu.id = l.customer_id
        JOIN payments  p  ON p.id  = l.payment_id
        ORDER BY l.activated_at DESC
        LIMIT ? OFFSET ?
    """
    sql_count = "SELECT COUNT(*) FROM licenses"
    result = _sqlite_browse(sql_data, sql_count, (limit, offset), (), offset, limit)
    return JSONResponse({"data": aes_encrypt(result)})


@app.post("/admin/browse/payments")
async def admin_browse_payments(payload: Payload):
    """
    Read-only paginated payment list. Latest paid first.
    Sends: { admin_token, offset?, limit? }
    Returns: { ok, rows, total, offset, limit, has_more }
    Each row: payment_ref, source, amount, currency, plan,
              paid_at, is_refunded, identity, identity_type, product_id
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    offset = max(0, int(req.get("offset", 0)))
    limit  = min(500, max(1, int(req.get("limit", 500))))

    sql_data = """
        SELECT p.payment_ref, p.source, p.amount, p.currency, p.plan,
               p.paid_at, p.is_refunded, p.refunded_at,
               cu.identity, cu.identity_type, p.product_id
        FROM payments p
        JOIN customers cu ON cu.id = p.customer_id
        ORDER BY p.paid_at DESC
        LIMIT ? OFFSET ?
    """
    sql_count = "SELECT COUNT(*) FROM payments"
    result = _sqlite_browse(sql_data, sql_count, (limit, offset), (), offset, limit)
    return JSONResponse({"data": aes_encrypt(result)})


@app.post("/admin/browse/customers")
async def admin_browse_customers(payload: Payload):
    """
    Read-only paginated customer list. Newest first.
    Sends: { admin_token, offset?, limit? }
    Returns: { ok, rows, total, offset, limit, has_more }
    Each row: identity, identity_type, created_at,
              total_licenses, active_licenses
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    offset = max(0, int(req.get("offset", 0)))
    limit  = min(500, max(1, int(req.get("limit", 500))))

    sql_data = """
        SELECT cu.identity, cu.identity_type, cu.created_at,
               COUNT(l.id)      AS total_licenses,
               SUM(l.is_active) AS active_licenses
        FROM customers cu
        LEFT JOIN licenses l ON l.customer_id = cu.id
        GROUP BY cu.id
        ORDER BY cu.created_at DESC
        LIMIT ? OFFSET ?
    """
    sql_count = "SELECT COUNT(*) FROM customers"
    result = _sqlite_browse(sql_data, sql_count, (limit, offset), (), offset, limit)
    return JSONResponse({"data": aes_encrypt(result)})


# ═════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    email_status = f"{len(EMAIL_SEND_METHODS)} method(s) configured" if EMAIL_SEND_METHODS else "not_configured"
    sms_status   = f"{len(SMS_SEND_METHODS)} method(s) configured"   if SMS_SEND_METHODS   else "not_configured"
    db_status    = "ok" if not _DB_INIT_ERROR else f"error: {_DB_INIT_ERROR}"
    overall      = "ok" if not _DB_INIT_ERROR else "degraded"
    return {
        "status":    overall,
        "time":      time.time(),
        "test_mode": TEST_MODE,
        "database":  db_status,
        "email":     email_status,
        "sms":       sms_status,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)