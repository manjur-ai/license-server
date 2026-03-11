"""
License Validation Server — India (Razorpay) + International (Gumroad)
Uses database.py for all DB operations.
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import os, time, json, base64, secrets, hashlib, hmac, requests as http_requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from database import register_license, verify_license, get_product, init_db

app = FastAPI()
init_db()

SHARED_SECRET       = bytes.fromhex(os.environ.get("SHARED_SECRET",
    "8cfaf7568ebd0d6f5557552efa46e43dfa57bb9618635753c224d3f38b3ac158"))
ADMIN_TOKEN         = os.environ.get("ADMIN_TOKEN", "change_this")
RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
# Set TEST_MODE=true locally for testing. Never set this on Railway/production.
TEST_MODE           = os.environ.get("TEST_MODE", "false").lower() == "true"
MAX_TS_DRIFT        = 30   # seconds

# ── Crypto ────────────────────────────────────────────────────────
def aes_decrypt(b64: str) -> dict:
    try:
        raw = base64.b64decode(b64)
        pt = AESGCM(SHARED_SECRET).decrypt(raw[:12], raw[12:], None)
        return json.loads(pt)
    except:
        raise HTTPException(400, "bad payload")

def aes_encrypt(data: dict) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(SHARED_SECRET).encrypt(nonce, json.dumps(data).encode(), None)
    return base64.b64encode(nonce + ct).decode()

def valid_ts(ts):
    return abs(time.time() - float(ts)) <= MAX_TS_DRIFT

def make_unique_key(product_id, email, machine_id):
    data = f"{product_id}:{email.lower().strip()}:{machine_id}".encode()
    return hmac.new(SHARED_SECRET, data, hashlib.sha256).hexdigest()

# ── Payment verification ──────────────────────────────────────────
def verify_razorpay_payment(payment_id: str, min_amount_paise: int = 49900) -> bool:
    if not RAZORPAY_KEY_ID: return False
    try:
        r = http_requests.get(f"https://api.razorpay.com/v1/payments/{payment_id}",
                              auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=10)
        d = r.json()
        return d.get("status") == "captured" and d.get("amount", 0) >= min_amount_paise
    except: return False

def verify_gumroad(license_key: str, email: str, product_gumroad_id: str) -> bool:
    if not product_gumroad_id: return False
    try:
        r = http_requests.post("https://api.gumroad.com/v2/licenses/verify",
            data={"product_id": product_gumroad_id, "license_key": license_key.strip(),
                  "increment_uses_count": "false"}, timeout=10)
        d = r.json()
        if not d.get("success"): return False
        if d.get("purchase", {}).get("refunded"): return False
        purchase_email = d.get("purchase", {}).get("email", "").lower()
        if email and purchase_email and purchase_email != email.lower(): return False
        return True
    except: return False

# ── Endpoints ─────────────────────────────────────────────────────
class Payload(BaseModel):
    data: str

@app.post("/register")
async def register(payload: Payload, request: Request):
    try: req = aes_decrypt(payload.data)
    except: return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "bad_request"})})

    if not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "expired"})})

    product_id = req.get("product_id", "")
    email      = req.get("email", "").lower().strip()
    machine_id = req.get("machine_id", "")
    source     = req.get("source", "")
    ip         = request.client.host

    if not all([product_id, email, machine_id, source]):
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "missing_fields"})})

    prod = get_product(product_id)
    if not prod:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_product"})})

    # Verify payment
    payment_ref, amount, currency = "", 0.0, "INR"
    if source == "razorpay":
        payment_ref = req.get("payment_id", "")
        amount, currency = prod["price_inr"], "INR"
        min_paise = int(prod["price_inr"] * 100 * 0.9)  # allow 10% tolerance
        if not verify_razorpay_payment(payment_ref, min_paise):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})
    elif source == "gumroad":
        payment_ref = req.get("license_key", "")
        amount, currency = prod["price_usd"], "USD"
        if not verify_gumroad(payment_ref, email, prod.get("gumroad_product_id", "")):
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "payment_invalid"})})
    elif source == "test":
        # Only works when TEST_MODE=true (set locally, never on production server)
        if not TEST_MODE:
            return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "test_mode_disabled"})})
        payment_ref = req.get("payment_id", f"test_{int(time.time())}")
        amount, currency = prod["price_inr"], "INR"
    else:
        return JSONResponse({"data": aes_encrypt({"ok": False, "reason": "unknown_source"})})

    unique_key = make_unique_key(product_id, email, machine_id)
    result = register_license(product_id, email, machine_id, unique_key, source,
                               payment_ref, amount, currency, ip_address=ip)
    return JSONResponse({"data": aes_encrypt(result)})


@app.post("/verify")
async def verify(payload: Payload, request: Request):
    try: req = aes_decrypt(payload.data)
    except: return JSONResponse({"data": aes_encrypt({"ok": False})})

    if not valid_ts(req.get("timestamp", 0)):
        return JSONResponse({"data": aes_encrypt({"ok": False})})

    product_id = req.get("product_id", "")
    email      = req.get("email", "").lower().strip()
    machine_id = req.get("machine_id", "")
    ip         = request.client.host

    if not all([product_id, email, machine_id]):
        return JSONResponse({"data": aes_encrypt({"ok": False})})

    unique_key = make_unique_key(product_id, email, machine_id)
    result = verify_license(product_id, unique_key, ip)
    return JSONResponse({"data": aes_encrypt(result)})


@app.get("/health")
async def health():
    return {"status": "ok", "time": time.time()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)