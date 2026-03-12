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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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
    revoke_license, mark_refunded, backup_db
)

app = FastAPI()
init_db()

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

def send_email(to: str, subject: str, html_body: str) -> bool:
    return _send_email(to, subject, html_body, test_mode=TEST_MODE)

def notify_otp(identity: str, identity_type: str, otp: str, product_name: str):
    """Send OTP via email or SMS depending on identity_type."""
    if identity_type == "sms":
        send_sms_otp(identity, otp, test_mode=TEST_MODE)
    else:
        send_email(
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

    prod = get_product(product_id)
    if not prod:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_product"})})

    otp = generate_otp()
    store_otp(identity, identity_type, otp)
    notify_otp(identity, identity_type, otp, prod["name"])

    if TEST_MODE:
        return JSONResponse({"data": aes_encrypt({"ok": True, "reason": "sent", "test_otp": otp})})
    return JSONResponse({"data": aes_encrypt({"ok": True, "reason": "sent"})})


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
    ip            = request.client.host

    if not all([product_id, identity, machine_id, source]):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    # ── Identity must be OTP-verified ────────────────────────────
    if not is_identity_verified(identity, identity_type):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "identity_not_verified"})})

    # ── Product check ────────────────────────────────────────────
    prod = get_product(product_id)
    if not prod:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_product"})})

    # ── Payment verification ─────────────────────────────────────
    payment_ref, amount, currency = "", 0.0, "INR"

    if source == "razorpay":
        payment_ref      = req.get("payment_id", "")
        amount, currency = prod["price_inr"], "INR"
        min_paise        = int(prod["price_inr"] * 100 * 0.9)
        if not verify_razorpay_payment(payment_ref, min_paise):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})

    elif source == "gumroad":
        payment_ref      = req.get("license_key", "")
        amount, currency = prod["price_usd"], "USD"
        if not verify_gumroad_key(payment_ref, identity, identity_type,
                                  prod.get("gumroad_product_id", "")):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})

    elif source == "test":
        if not TEST_MODE:
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "test_mode_disabled"})})
        payment_ref      = req.get("payment_id", f"test_{int(time.time())}")
        amount, currency = prod["price_inr"], "INR"

    else:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_source"})})

    # ── Register in DB ───────────────────────────────────────────
    unique_key = make_unique_key(product_id, identity, identity_type, machine_id)
    result = register_license(
        product_id, identity, identity_type, machine_id, unique_key,
        source, payment_ref, amount, currency, ip_address=ip
    )

    # ── Send confirmation ────────────────────────────────────────
    if result.get("ok") and result["reason"] == "registered":
        notify_activated(identity, identity_type, prod["name"], "lifetime")

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

    unique_key = make_unique_key(product_id, identity, identity_type, machine_id)
    result     = verify_license(product_id, unique_key, ip)
    return JSONResponse({"data": aes_encrypt(result)})


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

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
#  HEALTH
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    email_status = f"{len(EMAIL_SEND_METHODS)} method(s)" if EMAIL_SEND_METHODS else "not_configured"
    sms_status   = f"{len(SMS_SEND_METHODS)} method(s)"   if SMS_SEND_METHODS   else "not_configured"
    return {
        "status":    "ok",
        "time":      time.time(),
        "test_mode": TEST_MODE,
        "email":     email_status,
        "sms":       sms_status
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)