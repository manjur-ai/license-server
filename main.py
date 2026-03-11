"""
main.py — License Server
Full flow:
  Step 1. POST /send-otp    → generates OTP, emails it to user
  Step 2. POST /verify-otp  → user submits OTP from email → marks email verified
  Step 3. POST /register    → payment verified + email verified → license created
  Step 4. POST /verify      → silent check on every EXE launch (no OTP needed)

Environment variables (set on Railway):
  SHARED_SECRET         32-byte hex — must match C++ client
  ADMIN_TOKEN           your admin password
  RAZORPAY_KEY_ID       rzp_live_xxxx
  RAZORPAY_KEY_SECRET   razorpay secret
  GMAIL_USER            yourgmail@gmail.com
  GMAIL_PASSWORD        16-char Gmail App Password (from Google App Passwords)
  SUPPORT_EMAIL         support@yourdomain.com  (shown in emails to customers)
  TEST_MODE             true  (local testing only — never set on Railway)
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import os, time, json, base64, secrets, hashlib, hmac
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests as http_requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from database import (
    init_db, get_product,
    generate_otp, store_otp, is_valid_otp, is_email_verified,
    register_license, verify_license,
    revoke_license, mark_refunded
)

app = FastAPI()
init_db()

# ── Config ──────────────────────────────────────────────────────────────────────
SHARED_SECRET       = bytes.fromhex(os.environ.get("SHARED_SECRET",
    "8cfaf7568ebd0d6f5557552efa46e43dfa57bb9618635753c224d3f38b3ac158"))
ADMIN_TOKEN         = os.environ.get("ADMIN_TOKEN",         "change_this")
RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID",     "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
GMAIL_USER          = os.environ.get("GMAIL_USER",          "")
GMAIL_PASSWORD      = os.environ.get("GMAIL_PASSWORD",      "")
SUPPORT_EMAIL       = os.environ.get("SUPPORT_EMAIL",       GMAIL_USER)
TEST_MODE           = os.environ.get("TEST_MODE", "false").lower() == "true"
MAX_TS_DRIFT        = 30   # seconds — reject requests older than this

# ── Crypto ──────────────────────────────────────────────────────────────────────

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

def make_unique_key(product_id: str, email: str, machine_id: str) -> str:
    data = f"{product_id}:{email.lower().strip()}:{machine_id}".encode()
    return hmac.new(SHARED_SECRET, data, hashlib.sha256).hexdigest()

# ── Email sender ────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, html_body: str) -> bool:
    """
    Send email via Gmail SMTP SSL (port 465).
    In TEST_MODE: prints to server log instead of actually sending.
    """
    if TEST_MODE:
        print(f"\n{'─'*55}")
        print(f"  [TEST EMAIL]  To: {to}")
        print(f"  Subject: {subject}")
        print(f"{'─'*55}\n")
        return True

    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("WARNING: GMAIL_USER or GMAIL_PASSWORD not set — email skipped")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"License Server <{GMAIL_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASSWORD)
            smtp.send_message(msg)
        print(f"  Email sent → {to} | {subject}")
        return True
    except Exception as e:
        print(f"  Email ERROR: {e}")
        return False

# ── Email templates ─────────────────────────────────────────────────────────────

def email_send_otp(to: str, otp: str, product_name: str):
    """OTP verification email — Step 1."""
    send_email(
        to       = to,
        subject  = f"Your verification code: {otp}",
        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
            <h2 style="color:#1d4ed8">Email Verification</h2>
            <p>You requested to activate <b>{product_name}</b>.</p>
            <p>Enter this code in the application:</p>
            <div style="font-size:40px;font-weight:bold;letter-spacing:10px;
                        color:#1d4ed8;background:#eff6ff;padding:24px;
                        border-radius:10px;text-align:center;margin:16px 0">
                {otp}
            </div>
            <p style="color:#6b7280;font-size:13px">
                ⏱ This code expires in <b>10 minutes</b>.<br>
                If you didn't request this, ignore this email.<br><br>
                Support: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
            </p>
        </div>
        """
    )

def email_activated(to: str, product_name: str, plan: str):
    """Sent after successful license registration."""
    send_email(
        to       = to,
        subject  = f"✅ Your {product_name} license is activated",
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
                ⚠️ Changing your PC? Contact us to transfer your license.<br>
                Support: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
            </p>
        </div>
        """
    )

def email_revoked(to: str, product_name: str, reason: str):
    """Sent when admin revokes a license."""
    reason_map = {
        "refund": "your payment was refunded",
        "abuse":  "a violation of our terms was detected",
        "manual": "an administrative action was taken",
    }
    send_email(
        to       = to,
        subject  = f"⚠️ Your {product_name} license has been deactivated",
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

def email_refund_complete(to: str, product_name: str):
    """Sent when refund is processed."""
    send_email(
        to       = to,
        subject  = f"💰 Refund processed — {product_name}",
        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
            <h2 style="color:#7c3aed">Refund Processed</h2>
            <p>Your refund for <b>{product_name}</b> has been processed and your
               license has been deactivated.</p>
            <p>The amount will appear in your account within 5–7 business days.</p>
            <p style="color:#6b7280;font-size:13px">
                Questions? <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
            </p>
        </div>
        """
    )

# ── Payment verification ────────────────────────────────────────────────────────

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

def verify_gumroad_key(license_key: str, email: str, gumroad_product_id: str) -> bool:
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
        purchase_email = d.get("purchase", {}).get("email", "").lower()
        if email and purchase_email and purchase_email != email.lower(): return False
        return True
    except: return False

# ── Request model ───────────────────────────────────────────────────────────────

class Payload(BaseModel):
    data: str   # AES-GCM encrypted JSON, base64 encoded

# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 1 — SEND OTP
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/send-otp")
async def send_otp(payload: Payload, request: Request):
    """
    Step 1 — EXE calls this first with the user's email.
    Server generates a 6-digit OTP and emails it to the user.

    EXE sends (encrypted):
        { email, product_id, timestamp }

    Returns (encrypted):
        { ok, reason }
        reason: sent | resent | unknown_product | missing_fields | expired
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    email      = req.get("email", "").lower().strip()
    product_id = req.get("product_id", "")

    if not email or not product_id:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    # Validate product exists
    prod = get_product(product_id)
    if not prod:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_product"})})

    # Generate OTP and save to DB
    otp = generate_otp()
    store_otp(email, otp)

    # Send OTP email
    email_send_otp(email, otp, prod["name"])

    # In TEST_MODE: include OTP in response so remote test clients can read it
    # In production (TEST_MODE=false): OTP is NEVER exposed in the response
    if TEST_MODE:
        return JSONResponse({"data": aes_encrypt({"ok": True, "reason": "sent", "test_otp": otp})})

    return JSONResponse({"data": aes_encrypt({"ok": True, "reason": "sent"})})


# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 2 — VERIFY OTP
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/verify-otp")
async def verify_otp(payload: Payload, request: Request):
    """
    Step 2 — User types the OTP they received in email.
    EXE sends it here. If correct, email is marked verified.
    Only after this can /register be called.

    EXE sends (encrypted):
        { email, otp, timestamp }

    Returns (encrypted):
        { ok, reason }
        reason:
          verified           — correct OTP ✓  (proceed to /register)
          already_verified   — OTP already used (also ok, proceed)
          otp_not_found      — /send-otp was never called for this email
          otp_expired        — OTP older than 10 minutes (call /send-otp again)
          invalid_otp        — wrong code (attempts_left also returned)
          max_attempts_exceeded — locked after 5 wrong tries
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    email = req.get("email", "").lower().strip()
    otp   = req.get("otp", "").strip()

    if not email or not otp:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    result = is_valid_otp(email, otp)
    return JSONResponse({"data": aes_encrypt(result)})


# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 3 — REGISTER
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/register")
async def register(payload: Payload, request: Request):
    """
    Step 3 — Called ONLY after OTP is verified + payment is done.
    Server checks:
      (a) email is OTP-verified (within last 30 min)
      (b) payment is valid (Razorpay or Gumroad)
    Then creates the license tied to this machine fingerprint.

    EXE sends (encrypted):
        {
          product_id, email, machine_id, timestamp,
          source: "razorpay" | "gumroad" | "test",

          -- if source == "razorpay":
          payment_id: "pay_xxx"

          -- if source == "gumroad":
          license_key: "XXXX-XXXX-XXXX-XXXX"
        }

    Returns (encrypted):
        { ok, reason }
        reason:
          registered          — success, license created ✓
          already_registered  — same machine already has a license (ok)
          email_not_verified  — /verify-otp not called first
          payment_invalid     — payment check failed
          unknown_product     — wrong product_id
          max_machines_reached — customer already used all machine slots
          payment_already_used — same payment_ref used before
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    product_id = req.get("product_id", "")
    email      = req.get("email", "").lower().strip()
    machine_id = req.get("machine_id", "")
    source     = req.get("source", "")
    ip         = request.client.host

    if not all([product_id, email, machine_id, source]):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    # ── (a) EMAIL MUST BE OTP-VERIFIED ────────────────────────────
    # Even in test mode, email must be OTP-verified.
    # (test_client seeds OTP verification before calling /register)
    if not is_email_verified(email):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "email_not_verified"})})

    # ── Product check ─────────────────────────────────────────────
    prod = get_product(product_id)
    if not prod:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_product"})})

    # ── (b) PAYMENT VERIFICATION ──────────────────────────────────
    payment_ref, amount, currency = "", 0.0, "INR"

    if source == "razorpay":
        payment_ref     = req.get("payment_id", "")
        amount, currency = prod["price_inr"], "INR"
        min_paise       = int(prod["price_inr"] * 100 * 0.9)   # 10% tolerance
        if not verify_razorpay_payment(payment_ref, min_paise):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})

    elif source == "gumroad":
        payment_ref     = req.get("license_key", "")
        amount, currency = prod["price_usd"], "USD"
        if not verify_gumroad_key(payment_ref, email, prod.get("gumroad_product_id", "")):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})

    elif source == "test":
        # Only allowed when TEST_MODE=true (local dev only)
        if not TEST_MODE:
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "test_mode_disabled"})})
        payment_ref     = req.get("payment_id", f"test_{int(time.time())}")
        amount, currency = prod["price_inr"], "INR"

    else:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_source"})})

    # ── Register in DB ────────────────────────────────────────────
    unique_key = make_unique_key(product_id, email, machine_id)
    result = register_license(
        product_id, email, machine_id, unique_key,
        source, payment_ref, amount, currency,
        ip_address=ip
    )

    # ── Send confirmation email ───────────────────────────────────
    if result.get("ok"):
        if result["reason"] == "registered":
            email_activated(email, prod["name"], "lifetime")
        # Note: no email for "already_registered" to avoid spam

    return JSONResponse({"data": aes_encrypt(result)})


# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 4 — VERIFY (silent check on every EXE launch)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/verify")
async def verify(payload: Payload, request: Request):
    """
    Step 4 — Called silently every time the EXE starts.
    No OTP needed here — OTP was only needed during first registration.

    EXE sends (encrypted):
        { product_id, email, machine_id, timestamp }

    Returns (encrypted):
        { ok, reason }
        reason: ok | not_found | revoked | expired | wrong_product
    """
    req = aes_decrypt(payload.data)
    if not req or not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    product_id = req.get("product_id", "")
    email      = req.get("email", "").lower().strip()
    machine_id = req.get("machine_id", "")
    ip         = request.client.host

    if not all([product_id, email, machine_id]):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    unique_key = make_unique_key(product_id, email, machine_id)
    result     = verify_license(product_id, unique_key, ip)
    return JSONResponse({"data": aes_encrypt(result)})


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/admin/revoke")
async def admin_revoke(payload: Payload):
    """
    Admin — revoke a license and notify customer by email.

    Sends (encrypted):
        { admin_token, email, product_id (optional), reason (optional) }
        reason: "manual" | "abuse" | "refund"

    Returns (encrypted):
        { ok, revoked: N }
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    email      = req.get("email", "").lower().strip()
    product_id = req.get("product_id", None)
    reason     = req.get("reason", "manual")

    prod_name = "Your Tool"
    if product_id:
        prod = get_product(product_id)
        if prod: prod_name = prod["name"]

    n = revoke_license(email, product_id=product_id, reason=reason)

    if n > 0:
        email_revoked(email, prod_name, reason)

    return JSONResponse({"data": aes_encrypt({"ok": True, "revoked": n})})


@app.post("/admin/refund")
async def admin_refund(payload: Payload):
    """
    Admin — mark payment refunded, revoke license, email customer.

    Sends (encrypted):
        { admin_token, payment_ref, email, product_name (optional) }

    Returns (encrypted):
        { ok, refunded }
    """
    req = aes_decrypt(payload.data)
    if not req or req.get("admin_token") != ADMIN_TOKEN:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unauthorized"})})

    payment_ref  = req.get("payment_ref", "")
    email        = req.get("email", "").lower().strip()
    product_name = req.get("product_name", "Your Tool")

    ok = mark_refunded(payment_ref)
    if ok and email:
        email_refund_complete(email, product_name)

    return JSONResponse({"data": aes_encrypt({"ok": ok, "refunded": ok})})


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    """Railway health check. Also shows server config status."""
    return {
        "status":    "ok",
        "time":      time.time(),
        "test_mode": TEST_MODE,
        "email":     "configured" if GMAIL_USER else "not_configured"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)