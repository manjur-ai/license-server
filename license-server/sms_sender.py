"""
sms_sender.py — Multi-method SMS OTP sending with automatic fallback.

Same pattern as email_sender.py — tries each method in order, first success wins.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 METHOD     FREE/COST        RAILWAY  DLT INDIA  NOTES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FAST2SMS   Rs50 free credit  YES      built-in   Best for India
 MSG91      No free tier      YES      built-in   Reliable India
 TWILIO     $15 trial credit  YES      manual     Global, easy API
 2FACTOR    No free tier      YES      built-in   India only
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 DLT = TRAI registration required for India SMS delivery.
 Fast2SMS and MSG91 handle DLT registration for you.
 Twilio requires you to handle DLT separately.

Parameters per method:
  FAST2SMS : api_key
             (phone number passed at send time)
  MSG91    : api_key, sender_id, template_id
             sender_id: 6-char approved sender e.g. TOOLFY
             template_id: DLT approved template ID
  TWILIO   : account_sid, auth_token, from_number
             from_number: your Twilio number e.g. +12345678901
  2FACTOR  : api_key

Set Railway environment variable SMS_SEND_METHODS (JSON array):

SMS_SEND_METHODS = [
  {"method":"FAST2SMS", "api_key":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
  {"method":"MSG91",    "api_key":"xxxxxxxxxx", "sender_id":"TOOLFY", "template_id":"123456789"},
  {"method":"TWILIO",   "account_sid":"ACxxxxxxxx", "auth_token":"xxxxxxxx", "from_number":"+12345678901"},
  {"method":"2FACTOR",  "api_key":"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
]
"""

import os, json, time
import requests as http_req

# ── Load method list from env ──────────────────────────────────────────────────

def _load_sms_methods() -> list:
    raw = os.environ.get("SMS_SEND_METHODS", "[]")
    try:
        methods = json.loads(raw)
        if not isinstance(methods, list):
            print("SMS_SEND_METHODS must be a JSON array — SMS disabled")
            return []
        if methods:
            print(f"  SMS methods loaded: {[m.get('method','?') for m in methods]}")
        return methods
    except json.JSONDecodeError as e:
        print(f"SMS_SEND_METHODS JSON parse error: {e} — SMS disabled")
        return []

SMS_SEND_METHODS: list = _load_sms_methods()

# ── Per-method senders ─────────────────────────────────────────────────────────

def _try_fast2sms(to: str, otp: str, p: dict) -> bool:
    """
    Fast2SMS — Best for India. Rs50 free credits on signup.
    DLT registration handled by Fast2SMS.
    Cost: ~Rs0.15 per SMS after free credits.
    Setup: fast2sms.com > signup > Dashboard > Dev API > copy API key
    Phone format: 10-digit Indian number (without +91)
    """
    api_key = p.get("api_key", "")
    if not api_key:
        print("  [FAST2SMS] Missing api_key — skip"); return False

    # Fast2SMS expects 10-digit number without country code
    phone = to.replace("+91", "").replace("+", "").strip()
    if len(phone) != 10:
        print(f"  [FAST2SMS] Invalid Indian phone: {to} — skip"); return False

    try:
        r = http_req.get(
            "https://www.fast2sms.com/dev/bulkV2",
            params={
                "authorization": api_key,
                "variables_values": otp,
                "route": "otp",
                "numbers": phone
            },
            timeout=12
        )
        data = r.json()
        if data.get("return") is True:
            print(f"  [FAST2SMS] + Sent OTP to {to}")
            return True
        print(f"  [FAST2SMS] x {data.get('message', r.text[:200])}")
        return False
    except Exception as e:
        print(f"  [FAST2SMS] x {e}"); return False


def _try_msg91(to: str, otp: str, p: dict) -> bool:
    """
    MSG91 — Reliable India SMS. No free tier but cheap (~Rs0.18/SMS).
    DLT registration handled by MSG91.
    Setup:
      1. msg91.com > signup > get API key
      2. Create DLT approved template containing {otp} variable
      3. Note the template_id and your 6-char sender_id
    """
    api_key     = p.get("api_key", "")
    sender_id   = p.get("sender_id", "TOOLFY")
    template_id = p.get("template_id", "")
    if not api_key or not template_id:
        print("  [MSG91] Missing api_key/template_id — skip"); return False

    # MSG91 expects number with country code, no +
    phone = to.replace("+", "").strip()

    try:
        r = http_req.post(
            "https://api.msg91.com/api/v5/otp",
            headers={"Content-Type": "application/json"},
            json={
                "authkey":     api_key,
                "mobile":      phone,
                "otp":         otp,
                "sender":      sender_id,
                "template_id": template_id
            },
            timeout=12
        )
        data = r.json()
        if data.get("type") == "success":
            print(f"  [MSG91] + Sent OTP to {to}")
            return True
        print(f"  [MSG91] x {data.get('message', r.text[:200])}")
        return False
    except Exception as e:
        print(f"  [MSG91] x {e}"); return False


def _try_twilio(to: str, otp: str, p: dict) -> bool:
    """
    Twilio — Global SMS. $15 free trial credit.
    Works everywhere. For India: DLT registration must be done separately.
    Setup:
      1. twilio.com > signup > get Account SID + Auth Token
      2. Buy a phone number (from_number)
      3. For India SMS: register DLT sender ID separately
    """
    account_sid  = p.get("account_sid", "")
    auth_token   = p.get("auth_token", "")
    from_number  = p.get("from_number", "")
    if not account_sid or not auth_token or not from_number:
        print("  [TWILIO] Missing account_sid/auth_token/from_number — skip"); return False

    try:
        r = http_req.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            auth=(account_sid, auth_token),
            data={
                "From": from_number,
                "To":   to,
                "Body": f"Your verification code is: {otp}\nValid for 10 minutes. Do not share."
            },
            timeout=12
        )
        data = r.json()
        if r.status_code in (200, 201) and data.get("sid"):
            print(f"  [TWILIO] + Sent OTP to {to}")
            return True
        if r.status_code == 429:
            print(f"  [TWILIO] x Rate limit — next"); return False
        print(f"  [TWILIO] x {r.status_code}: {data.get('message', r.text[:200])}")
        return False
    except Exception as e:
        print(f"  [TWILIO] x {e}"); return False


def _try_2factor(to: str, otp: str, p: dict) -> bool:
    """
    2Factor — India SMS. No free tier (~Rs0.25/SMS).
    DLT registration handled by 2Factor.
    Setup: 2factor.in > signup > API Keys > copy key
    """
    api_key = p.get("api_key", "")
    if not api_key:
        print("  [2FACTOR] Missing api_key — skip"); return False

    # 2Factor expects 10-digit number
    phone = to.replace("+91", "").replace("+", "").strip()

    try:
        r = http_req.get(
            f"https://2factor.in/API/V1/{api_key}/SMS/{phone}/{otp}/OTP1",
            timeout=12
        )
        data = r.json()
        if data.get("Status") == "Success":
            print(f"  [2FACTOR] + Sent OTP to {to}")
            return True
        print(f"  [2FACTOR] x {data.get('Details', r.text[:200])}")
        return False
    except Exception as e:
        print(f"  [2FACTOR] x {e}"); return False


# ── Dispatch table ─────────────────────────────────────────────────────────────

_SMS_SENDERS = {
    "FAST2SMS": _try_fast2sms,
    "MSG91":    _try_msg91,
    "TWILIO":   _try_twilio,
    "2FACTOR":  _try_2factor,
}

# ── Public API ─────────────────────────────────────────────────────────────────

def send_sms_otp(to: str, otp: str, test_mode: bool = False) -> bool:
    """
    Send OTP via SMS. Tries each method in SMS_SEND_METHODS in order.
    Returns True on first success. Returns False only if ALL methods fail.
    In test_mode: prints to log instead of sending real SMS.
    """
    if test_mode:
        sep = "-" * 55
        print(f"\n{sep}")
        print(f"  [TEST SMS]  To: {to}  OTP: {otp}")
        print(f"{sep}\n")
        return True

    if not SMS_SEND_METHODS:
        print("WARNING: SMS_SEND_METHODS not configured — SMS skipped")
        return False

    n = len(SMS_SEND_METHODS)
    print(f"\n  Sending SMS OTP -> {to}")
    for i, entry in enumerate(SMS_SEND_METHODS):
        method = entry.get("method", "").upper()
        fn     = _SMS_SENDERS.get(method)
        if not fn:
            print(f"  [{i+1}/{n}] Unknown SMS method '{method}' — skip")
            continue
        print(f"  [{i+1}/{n}] Trying {method}...")
        try:
            if fn(to, otp, entry):
                return True
        except Exception as e:
            print(f"  [{method}] Unexpected error: {e}")
        time.sleep(0.3)

    print(f"  x All {n} SMS methods failed -> {to}")
    return False