"""
test_client.py — Full test suite for the license server.

Works in two modes:

  TEST MODE (TEST_MODE=true on server):
    - OTPs returned in API response (test_otp field)
    - source=test payments accepted
    - Fully automated, no user interaction
    Run locally:
      python test_client.py
    Run against Railway in test mode:
      SERVER=https://your-app.up.railway.app python test_client.py

  PRODUCTION MODE (TEST_MODE=false on server):
    - Real OTPs sent to email/phone — script pauses and prompts you to enter them
    - Needs a real payment ID to test register/verify
    - Security + admin tests still run automatically
    Run:
      SERVER=https://your-app.up.railway.app \
      EMAIL=you@gmail.com PHONE=+91XXXXXXXXXX \
      ADMIN_TOKEN=your_token \
      python test_client.py

    With real payment (to test register/verify):
      RAZORPAY_ID=pay_xxx    python test_client.py   (Razorpay)
      GUMROAD_KEY=xxxx-xxxx  python test_client.py   (Gumroad)

    Skip register/verify tests entirely:
      SKIP_PAYMENT=true      python test_client.py

Environment variables:
  SERVER         URL of the server              default: http://localhost:8000
  SHARED_SECRET  64-char hex key                default: dev key
  ADMIN_TOKEN    admin password                 default: test_admin_123
  EMAIL          test email address             default: testuser@gmail.com
  PHONE          test phone number              default: +919876543210
  PRODUCT        product_id to test             default: TOOL1
  SKIP_PAYMENT   true = skip register tests     default: false
  RAZORPAY_ID    real Razorpay payment_id       enables register tests in prod
  GUMROAD_KEY    real Gumroad license_key        enables register tests in prod
"""

import requests, json, base64, secrets, hashlib, time, os, sys
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── Config ──────────────────────────────────────────────────────────────────────

SERVER       = os.environ.get("SERVER",       "https://web-production-b851a.up.railway.app")
SECRET_HEX   = os.environ.get("SHARED_SECRET",
               "fc0e3b19df3e631af37bc862707adce87ad8a571872224ad88dee54e3e958b9c")
ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN",  "siddhatown")
EMAIL        = os.environ.get("EMAIL",        "whoisaaban@gmail.com")
PHONE        = os.environ.get("PHONE",        "+919331210187")
PRODUCT      = os.environ.get("PRODUCT",      "TOOL1")
SKIP_PAYMENT = os.environ.get("SKIP_PAYMENT", "false").lower() == "true"
RAZORPAY_ID  = os.environ.get("RAZORPAY_ID",  "")
GUMROAD_KEY  = os.environ.get("GUMROAD_KEY",  "")

SECRET_BYTES = bytes.fromhex(SECRET_HEX)

# ── Crypto ───────────────────────────────────────────────────────────────────────

def encrypt(data):
    nonce = secrets.token_bytes(12)
    ct    = AESGCM(SECRET_BYTES).encrypt(nonce, json.dumps(data).encode(), None)
    return base64.b64encode(nonce + ct).decode()

def decrypt(b64):
    raw = base64.b64decode(b64)
    pt  = AESGCM(SECRET_BYTES).decrypt(raw[:12], raw[12:], None)
    return json.loads(pt)

def post(endpoint, data):
    r = requests.post(f"{SERVER}{endpoint}",
                      json={"data": encrypt(data)}, timeout=20)
    return decrypt(r.json()["data"])

def ts():
    return int(time.time())

def machine():
    return hashlib.sha256(secrets.token_bytes(16)).hexdigest()

# ── OTP helper ───────────────────────────────────────────────────────────────────

def get_otp(send_result, channel, identity):
    """
    TEST_MODE: read test_otp from response (automatic).
    PROD MODE: prompt user to enter the OTP they received.
    Returns "" if not available (downstream tests will be skipped).
    """
    otp = send_result.get("test_otp", "")
    if otp:
        print(f"         [TEST_MODE] {channel.upper()} OTP: {otp}")
        return otp

    # Production: OTP sent to real email/phone
    print(f"\n  ┌──────────────────────────────────────────────────────────────────┐")
    print(f"  │  OTP sent to: {identity}")
    print(f"  │  Check your {channel} inbox and type the 6-digit code.")
    print(f"  └──────────────────────────────────────────────────────────────────┘")
    try:
        entered = input("  Enter OTP (or press Enter to skip): ").strip()
    except EOFError:
        entered = ""
    if not entered:
        print("  ⚠  Skipped — OTP-dependent tests will be marked [SKIP]")
    return entered

# ── Test runner ──────────────────────────────────────────────────────────────────

passed = failed = skipped = 0

def check(label, result, expect_ok, expect_reason=None):
    global passed, failed
    ok_match     = result.get("ok") == expect_ok
    reason_match = (expect_reason is None) or (result.get("reason") == expect_reason)
    if ok_match and reason_match:
        passed += 1
        print(f"  [PASS] {label:60} -> {result}")
    else:
        failed += 1
        exp = f"ok={expect_ok}" + (f", reason={expect_reason}" if expect_reason else "")
        print(f"  [FAIL] {label:60} -> got={result}  expected={exp}")

def skip(label, reason="n/a"):
    global skipped
    skipped += 1
    print(f"  [SKIP] {label:60} ({reason})")

# ── Health check ─────────────────────────────────────────────────────────────────

print(f"\nServer : {SERVER}")
try:
    h = requests.get(f"{SERVER}/health", timeout=10).json()
except Exception as e:
    print(f"Cannot reach server: {e}"); sys.exit(1)

print(f"Health : {h}")
if h.get("status") != "ok":
    print("Server not healthy — abort"); sys.exit(1)

TEST_MODE_ON = bool(h.get("test_mode"))
print(f"Mode   : {'TEST (automated)' if TEST_MODE_ON else 'PRODUCTION (real OTPs)'}")
if not TEST_MODE_ON:
    if SKIP_PAYMENT:
        print("         SKIP_PAYMENT=true — register/verify tests will be skipped")
    elif not RAZORPAY_ID and not GUMROAD_KEY:
        print("         No payment ID set — register tests will be skipped")
        print("         Set RAZORPAY_ID=pay_xxx or GUMROAD_KEY=xxx to enable them")

print(f"\nProduct: {PRODUCT}")
print(f"Email  : {EMAIL}")
print(f"Phone  : {PHONE}")
print("=" * 75)

MACHINE_E = machine()
MACHINE_S = machine()
MACHINE_X = machine()

email_verified   = False
sms_verified     = False
email_registered = False
sms_registered   = False

# ══════════════════════════════════════════════════════════════════════════════
# BLOCK A — EMAIL FLOW
# ══════════════════════════════════════════════════════════════════════════════
print("\n── BLOCK A: EMAIL FLOW ──\n")

r = post("/send-otp", {"identity": EMAIL, "identity_type": "email",
                        "product_id": PRODUCT, "timestamp": ts()})
check("A1. Send OTP (email)", r, True, "sent")

# A2 doesn't need a real OTP
check("A2. Wrong OTP rejected",
      post("/verify-otp", {"identity": EMAIL, "identity_type": "email",
                            "otp": "000000", "timestamp": ts()}),
      False, "invalid_otp")

email_otp = get_otp(r, "email", EMAIL)

if email_otp:
    res = post("/verify-otp", {"identity": EMAIL, "identity_type": "email",
                                "otp": email_otp, "timestamp": ts()})
    check("A3. Correct OTP verified", res, True, "verified")
    email_verified = res.get("ok") and res.get("reason") in ("verified", "already_verified")

    check("A4. Same OTP again -> already_verified",
          post("/verify-otp", {"identity": EMAIL, "identity_type": "email",
                                "otp": email_otp, "timestamp": ts()}),
          True, "already_verified")
else:
    skip("A3. Correct OTP verified", "no OTP")
    skip("A4. Same OTP again -> already_verified", "no OTP")

# A5 always works — uses a fresh unverified identity
check("A5. Register without OTP -> identity_not_verified",
      post("/register", {"product_id": PRODUCT,
                          "identity": "nootp@gmail.com", "identity_type": "email",
                          "machine_id": MACHINE_E, "source": "test",
                          "payment_id": f"test_nootp_{ts()}", "timestamp": ts()}),
      False, "identity_not_verified")

# A6-A8 need OTP + payment
if not email_verified:
    skip("A6. Register after email OTP", "OTP not verified")
    skip("A7. Register same machine -> max_machines_reached", "OTP not verified")
    skip("A8. Verify valid email license", "OTP not verified")
elif SKIP_PAYMENT:
    skip("A6. Register after email OTP", "SKIP_PAYMENT=true")
    skip("A7. Register same machine -> max_machines_reached", "SKIP_PAYMENT=true")
    skip("A8. Verify valid email license", "SKIP_PAYMENT=true")
else:
    if TEST_MODE_ON:
        src, ref, amt, cur = "test", f"test_email_{ts()}", 499, "INR"
    elif RAZORPAY_ID:
        src, ref, amt, cur = "razorpay", RAZORPAY_ID, 499, "INR"
    elif GUMROAD_KEY:
        src, ref, amt, cur = "gumroad", GUMROAD_KEY, 9.99, "USD"
    else:
        src = None

    if src:
        res = post("/register", {"product_id": PRODUCT,
                                  "identity": EMAIL, "identity_type": "email",
                                  "machine_id": MACHINE_E, "source": src,
                                  "payment_id": ref, "amount": amt,
                                  "currency": cur, "timestamp": ts()})
        check("A6. Register after email OTP", res, True, "registered")
        email_registered = res.get("ok")

        check("A7. Register same machine -> max_machines_reached",
              post("/register", {"product_id": PRODUCT,
                                  "identity": EMAIL, "identity_type": "email",
                                  "machine_id": MACHINE_E, "source": src,
                                  "payment_id": ref + "_dup", "amount": amt,
                                  "currency": cur, "timestamp": ts()}),
              False)

        check("A8. Verify valid email license",
              post("/verify", {"product_id": PRODUCT, "identity": EMAIL,
                                "identity_type": "email", "machine_id": MACHINE_E,
                                "timestamp": ts()}),
              True, "ok")
    else:
        skip("A6. Register after email OTP", "no payment ID")
        skip("A7. Register same machine -> max_machines_reached", "no payment ID")
        skip("A8. Verify valid email license", "no payment ID")

# ══════════════════════════════════════════════════════════════════════════════
# BLOCK B — SMS FLOW
# ══════════════════════════════════════════════════════════════════════════════
print("\n── BLOCK B: SMS FLOW ──\n")

r = post("/send-otp", {"identity": PHONE, "identity_type": "sms",
                        "product_id": PRODUCT, "timestamp": ts()})
check("B1. Send OTP (sms)", r, True, "sent")

check("B2. Wrong SMS OTP rejected",
      post("/verify-otp", {"identity": PHONE, "identity_type": "sms",
                            "otp": "000000", "timestamp": ts()}),
      False, "invalid_otp")

sms_otp = get_otp(r, "SMS", PHONE)

if sms_otp:
    res = post("/verify-otp", {"identity": PHONE, "identity_type": "sms",
                                "otp": sms_otp, "timestamp": ts()})
    check("B3. Correct SMS OTP verified", res, True, "verified")
    sms_verified = res.get("ok") and res.get("reason") in ("verified", "already_verified")
else:
    skip("B3. Correct SMS OTP verified", "no OTP")

if not sms_verified:
    skip("B4. Register after SMS OTP", "OTP not verified")
    skip("B5. Verify valid SMS license", "OTP not verified")
elif SKIP_PAYMENT:
    skip("B4. Register after SMS OTP", "SKIP_PAYMENT=true")
    skip("B5. Verify valid SMS license", "SKIP_PAYMENT=true")
else:
    if TEST_MODE_ON:
        src2, ref2, amt2, cur2 = "test", f"test_sms_{ts()}", 499, "INR"
    elif RAZORPAY_ID:
        src2, ref2, amt2, cur2 = "razorpay", RAZORPAY_ID + "_b", 499, "INR"
    else:
        src2 = None

    if src2:
        res = post("/register", {"product_id": PRODUCT,
                                  "identity": PHONE, "identity_type": "sms",
                                  "machine_id": MACHINE_S, "source": src2,
                                  "payment_id": ref2, "amount": amt2,
                                  "currency": cur2, "timestamp": ts()})
        check("B4. Register after SMS OTP", res, True, "registered")
        sms_registered = res.get("ok")

        check("B5. Verify valid SMS license",
              post("/verify", {"product_id": PRODUCT, "identity": PHONE,
                                "identity_type": "sms", "machine_id": MACHINE_S,
                                "timestamp": ts()}),
              True, "ok")
    else:
        skip("B4. Register after SMS OTP", "no payment ID")
        skip("B5. Verify valid SMS license", "no payment ID")

# ══════════════════════════════════════════════════════════════════════════════
# BLOCK C — SECURITY  (all automatic, no registration needed)
# ══════════════════════════════════════════════════════════════════════════════
print("\n── BLOCK C: SECURITY ──\n")

check("C1. Replay attack (old timestamp) rejected",
      post("/verify", {"product_id": PRODUCT, "identity": EMAIL,
                        "identity_type": "email", "machine_id": MACHINE_E,
                        "timestamp": ts() - 999}),
      False, "expired")

check("C2. Wrong product_id rejected",
      post("/verify", {"product_id": "WRONG", "identity": EMAIL,
                        "identity_type": "email", "machine_id": MACHINE_E,
                        "timestamp": ts()}),
      False, "not_found")

check("C3. Different machine rejected",
      post("/verify", {"product_id": PRODUCT, "identity": EMAIL,
                        "identity_type": "email", "machine_id": MACHINE_X,
                        "timestamp": ts()}),
      False, "not_found")

check("C4. Different identity rejected",
      post("/verify", {"product_id": PRODUCT, "identity": "other@gmail.com",
                        "identity_type": "email", "machine_id": MACHINE_E,
                        "timestamp": ts()}),
      False, "not_found")

check("C5. Unsupported identity_type rejected",
      post("/send-otp", {"identity": "user@x.com", "identity_type": "twitter",
                          "product_id": PRODUCT, "timestamp": ts()}),
      False, "unsupported_identity_type")

# ══════════════════════════════════════════════════════════════════════════════
# BLOCK D — CROSS-CHANNEL ISOLATION
# ══════════════════════════════════════════════════════════════════════════════
print("\n── BLOCK D: CROSS-CHANNEL ISOLATION ──\n")

check("D1. Email machine + sms identity_type -> not_found",
      post("/verify", {"product_id": PRODUCT, "identity": EMAIL,
                        "identity_type": "sms", "machine_id": MACHINE_E,
                        "timestamp": ts()}),
      False, "not_found")

check("D2. SMS machine + email identity_type -> not_found",
      post("/verify", {"product_id": PRODUCT, "identity": PHONE,
                        "identity_type": "email", "machine_id": MACHINE_S,
                        "timestamp": ts()}),
      False, "not_found")

# ══════════════════════════════════════════════════════════════════════════════
# BLOCK E — ADMIN
# ══════════════════════════════════════════════════════════════════════════════
print("\n── BLOCK E: ADMIN ──\n")

if email_registered:
    check("E1. Admin revoke email license",
          post("/admin/revoke", {"admin_token": ADMIN_TOKEN,
                                  "identity": EMAIL, "identity_type": "email",
                                  "product_id": PRODUCT, "reason": "manual",
                                  "timestamp": ts()}),
          True)
    check("E2. Verify after revoke -> revoked",
          post("/verify", {"product_id": PRODUCT, "identity": EMAIL,
                            "identity_type": "email", "machine_id": MACHINE_E,
                            "timestamp": ts()}),
          False, "revoked")
else:
    skip("E1. Admin revoke email license", "email license not registered this run")
    skip("E2. Verify after revoke -> revoked", "email license not registered this run")

if sms_registered:
    check("E3. Admin revoke SMS license",
          post("/admin/revoke", {"admin_token": ADMIN_TOKEN,
                                  "identity": PHONE, "identity_type": "sms",
                                  "product_id": PRODUCT, "reason": "manual",
                                  "timestamp": ts()}),
          True)
    check("E4. Verify SMS after revoke -> revoked",
          post("/verify", {"product_id": PRODUCT, "identity": PHONE,
                            "identity_type": "sms", "machine_id": MACHINE_S,
                            "timestamp": ts()}),
          False, "revoked")
else:
    skip("E3. Admin revoke SMS license", "SMS license not registered this run")
    skip("E4. Verify SMS after revoke -> revoked", "SMS license not registered this run")

check("E5. Wrong admin token -> unauthorized",
      post("/admin/revoke", {"admin_token": "wrong_token",
                              "identity": EMAIL, "identity_type": "email",
                              "timestamp": ts()}),
      False, "unauthorized")

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
total = passed + failed + skipped
print(f"\n{'=' * 75}")
print(f"Results: {passed}/{total} passed  |  {failed} failed  |  {skipped} skipped")

if failed == 0 and skipped == 0:
    print("All PASS — server is working correctly.")
elif failed == 0:
    print(f"All testable checks PASS. {skipped} test(s) skipped (production mode — normal).")
    if not TEST_MODE_ON:
        print()
        print("  To run the full suite against this production server:")
        print("    RAZORPAY_ID=pay_xxx python test_client.py   # use a real Razorpay ID")
        print("    GUMROAD_KEY=xxx     python test_client.py   # use a real Gumroad key")
else:
    print(f"ATTENTION: {failed} test(s) failed — check output above.")

print(f"{'=' * 75}\n")
sys.exit(0 if failed == 0 else 1)