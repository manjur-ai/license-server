"""
email_sender.py — Multi-method email with automatic fallback.

How it works:
  - EMAIL_SEND_METHODS is a JSON list of methods to try in order.
  - Each method is tried. On any failure (network error, limit exceeded,
    bad credentials), the next method is tried automatically.
  - First success stops the chain. All failures are logged.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 METHOD       FREE LIMIT     RAILWAY   GMAIL SENDER   NOTES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 GSMTP        500/day        BLOCKED   YES            Local dev only
 BREVO        300/day        YES       YES*           Best free option
 GWORK        ~2000/day      YES       YES            Paid Rs125/mo
 SES          0 free**       YES       YES*           Cheapest at scale
 MAILGUN      100/day        YES       NO             Needs own domain
 MAILJET      200/day        YES       YES*           EU/GDPR friendly
 MAILERSEND   3000/month     YES       NO             Best free volume
 RESEND       3000/month     YES       NO             Simple API
 SENDGRID     100/day        YES       NO             Industry standard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 * Supports Gmail sender after verifying your email in their dashboard
 ** SES free only on AWS EC2. On Railway: $0.10/1000 emails (cheapest paid)

Parameters per method:
  GSMTP      : user, password, from_email, from_name
  BREVO      : api_key, from_email, from_name
  GWORK      : user, password, from_email, from_name
  SES        : user, password, from_email, from_name, region
               region: ap-south-1 (Mumbai), us-east-1, ap-southeast-1
  MAILGUN    : api_key, domain, from_email, from_name
               domain: your mailgun domain e.g. mg.toolfy.com
               base_url (optional): https://api.eu.mailgun.net for EU
  MAILJET    : api_key, api_secret, from_email, from_name
  MAILERSEND : api_key, from_email, from_name
  RESEND     : api_key, from_email, from_name
  SENDGRID   : api_key, from_email, from_name

Set this Railway environment variable (JSON array):

EMAIL_SEND_METHODS = [
  {"method":"GSMTP",      "user":"me@gmail.com",           "password":"xxxx xxxx xxxx xxxx",    "from_email":"me@gmail.com",          "from_name":"Toolfy"},
  {"method":"BREVO",      "api_key":"xkeysib-AAA",         "from_email":"support@gmail.com",     "from_name":"Toolfy Support"},
  {"method":"BREVO",      "api_key":"xkeysib-BBB",         "from_email":"support@gmail.com",     "from_name":"Toolfy Support"},
  {"method":"MAILJET",    "api_key":"abc123",              "api_secret":"xyz789",               "from_email":"support@gmail.com",     "from_name":"Toolfy Support"},
  {"method":"SES",        "user":"AKIAXXXXXXXXXXXXXXXX",   "password":"xxxxxxxxxxxxxxxxxxxxxxxx","from_email":"support@gmail.com",     "from_name":"Toolfy", "region":"ap-south-1"},
  {"method":"MAILGUN",    "api_key":"key-xxx",             "domain":"mg.toolfy.com",            "from_email":"noreply@toolfy.com",    "from_name":"Toolfy"},
  {"method":"MAILERSEND", "api_key":"mlsn.xxx",            "from_email":"noreply@toolfy.com",    "from_name":"Toolfy"},
  {"method":"RESEND",     "api_key":"re_xxx",              "from_email":"noreply@toolfy.com",    "from_name":"Toolfy"},
  {"method":"SENDGRID",   "api_key":"SG.xxx",              "from_email":"noreply@toolfy.com",    "from_name":"Toolfy"},
  {"method":"GWORK",      "user":"support@toolfy.com",     "password":"xxxxx",                  "from_email":"support@toolfy.com",    "from_name":"Toolfy"}
]
"""

import os, json, smtplib, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests as http_req

# ── Load method list from env ──────────────────────────────────────────────────

def _load_methods() -> list:
    raw = os.environ.get("EMAIL_SEND_METHODS", "[]")
    try:
        methods = json.loads(raw)
        if not isinstance(methods, list):
            print("EMAIL_SEND_METHODS must be a JSON array — email disabled")
            return []
        print(f"  Email methods loaded: {[m.get('method','?') for m in methods]}")
        return methods
    except json.JSONDecodeError as e:
        print(f"EMAIL_SEND_METHODS JSON parse error: {e} — email disabled")
        return []

EMAIL_SEND_METHODS: list = _load_methods()

# ── Shared SMTP helper ─────────────────────────────────────────────────────────

def _send_smtp(label, host, port, user, pwd, from_addr, to, subject, html) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(host, port, timeout=12) as s:
            s.ehlo(); s.starttls(); s.login(user, pwd)
            s.send_message(msg)
        print(f"  [{label}] + Sent via {from_addr}")
        return True
    except smtplib.SMTPAuthenticationError:
        print(f"  [{label}] x Auth failed — check credentials")
        return False
    except OSError as e:
        print(f"  [{label}] x Network error: {e}")
        return False
    except Exception as e:
        print(f"  [{label}] x {e}")
        return False

# ── Per-method senders ─────────────────────────────────────────────────────────

def _try_gsmtp(to, subject, html, p) -> bool:
    """
    Gmail personal SMTP (TLS port 587).
    Needs Gmail App Password (not your real password).
    Works locally. BLOCKED on Railway — auto-skips to next method.
    Setup: Google Account > Security > 2-Step Verification > App Passwords
    """
    user, pwd = p.get("user",""), p.get("password","")
    feml, fname = p.get("from_email", user), p.get("from_name","")
    if not user or not pwd:
        print("  [GSMTP] Missing user/password — skip"); return False
    addr = f"{fname} <{feml}>" if fname else feml
    return _send_smtp("GSMTP", "smtp.gmail.com", 587, user, pwd, addr, to, subject, html)


def _try_brevo(to, subject, html, p) -> bool:
    """
    Brevo HTTPS API. Free 300/day. Supports Gmail sender after verification.
    Setup: brevo.com > Senders & IPs > verify Gmail > API Keys > copy key
    Add multiple BREVO entries (different accounts) for higher daily volume.
    """
    api_key, feml = p.get("api_key",""), p.get("from_email","")
    fname = p.get("from_name","Support")
    if not api_key or not feml:
        print("  [BREVO] Missing api_key/from_email — skip"); return False
    try:
        r = http_req.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"sender":{"name":fname,"email":feml},"to":[{"email":to}],
                  "subject":subject,"htmlContent":html},
            timeout=12
        )
        if r.status_code in (200,201):
            print(f"  [BREVO] + Sent via {feml}"); return True
        body = r.text.lower()
        if r.status_code == 400 and any(w in body for w in ("daily","limit","quota")):
            print(f"  [BREVO] x Daily limit reached ({feml}) — next"); return False
        if r.status_code == 401:
            print(f"  [BREVO] x Invalid API key — skip"); return False
        print(f"  [BREVO] x {r.status_code}: {r.text[:200]}"); return False
    except Exception as e:
        print(f"  [BREVO] x {e}"); return False


def _try_gwork(to, subject, html, p) -> bool:
    """
    Google Workspace SMTP (TLS port 587). Paid ~Rs125/mo per user.
    Works on Railway. ~2000 emails/day. Send from @yourdomain.com.
    Setup: workspace.google.com > buy plan > create user > use as SMTP
    """
    user, pwd = p.get("user",""), p.get("password","")
    feml, fname = p.get("from_email", user), p.get("from_name","")
    if not user or not pwd:
        print("  [GWORK] Missing user/password — skip"); return False
    addr = f"{fname} <{feml}>" if fname else feml
    return _send_smtp("GWORK", "smtp.gmail.com", 587, user, pwd, addr, to, subject, html)


def _try_ses(to, subject, html, p) -> bool:
    """
    Amazon SES SMTP (TLS port 587).
    No free tier on Railway. $0.10 per 1000 emails — cheapest at scale.
    Free 62,000/month only on AWS EC2.
    Regions: ap-south-1 (Mumbai, best for India), us-east-1, ap-southeast-1
    Setup:
      1. AWS Console > SES > Verified Identities > verify your sender email
      2. Request Production Access (sandbox->production, ~24hr approval)
      3. SES > SMTP Settings > Create SMTP Credentials > copy user+password
    """
    user, pwd = p.get("user",""), p.get("password","")
    feml, fname = p.get("from_email",""), p.get("from_name","")
    region = p.get("region","ap-south-1")
    if not user or not pwd or not feml:
        print("  [SES] Missing user/password/from_email — skip"); return False
    host = f"email-smtp.{region}.amazonaws.com"
    addr = f"{fname} <{feml}>" if fname else feml
    return _send_smtp("SES", host, 587, user, pwd, addr, to, subject, html)


def _try_mailgun(to, subject, html, p) -> bool:
    """
    Mailgun HTTPS API. Free 100/day (Flex plan).
    Needs a sending domain (e.g. mg.toolfy.com). Cannot use Gmail directly.
    EU accounts: set base_url to https://api.eu.mailgun.net
    Setup:
      1. mailgun.com > Sending > Domains > Add domain > add DNS records
      2. API Keys > create private key
    """
    api_key, domain = p.get("api_key",""), p.get("domain","")
    feml, fname = p.get("from_email",""), p.get("from_name","Support")
    base_url = p.get("base_url","https://api.mailgun.net")
    if not api_key or not domain or not feml:
        print("  [MAILGUN] Missing api_key/domain/from_email — skip"); return False
    addr = f"{fname} <{feml}>" if fname else feml
    try:
        r = http_req.post(
            f"{base_url}/v3/{domain}/messages",
            auth=("api", api_key),
            data={"from":addr, "to":[to], "subject":subject, "html":html},
            timeout=12
        )
        if r.status_code in (200,202):
            print(f"  [MAILGUN] + Sent via {feml}"); return True
        if r.status_code == 429:
            print(f"  [MAILGUN] x Rate limit — next"); return False
        if r.status_code == 401:
            print(f"  [MAILGUN] x Invalid API key — skip"); return False
        print(f"  [MAILGUN] x {r.status_code}: {r.text[:200]}"); return False
    except Exception as e:
        print(f"  [MAILGUN] x {e}"); return False


def _try_mailjet(to, subject, html, p) -> bool:
    """
    Mailjet HTTPS API. Free 200/day, 6000/month. EU/GDPR compliant.
    Supports Gmail sender after verification. Needs BOTH api_key + api_secret.
    Setup:
      1. mailjet.com > free account
      2. Senders & Domains > Add sender > verify your Gmail
      3. API Keys > copy API Key AND Secret Key (need both)
    """
    api_key, api_secret = p.get("api_key",""), p.get("api_secret","")
    feml, fname = p.get("from_email",""), p.get("from_name","Support")
    if not api_key or not api_secret or not feml:
        print("  [MAILJET] Missing api_key/api_secret/from_email — skip"); return False
    try:
        r = http_req.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(api_key, api_secret),
            json={"Messages":[{
                "From":{"Email":feml,"Name":fname},
                "To":[{"Email":to}],
                "Subject":subject,
                "HTMLPart":html
            }]},
            timeout=12
        )
        if r.status_code in (200,201):
            status = r.json().get("Messages",[{}])[0].get("Status","")
            if status == "success":
                print(f"  [MAILJET] + Sent via {feml}"); return True
            print(f"  [MAILJET] x Status={status}: {r.text[:200]}"); return False
        if r.status_code == 429:
            print(f"  [MAILJET] x Rate limit — next"); return False
        if r.status_code == 401:
            print(f"  [MAILJET] x Invalid credentials — skip"); return False
        print(f"  [MAILJET] x {r.status_code}: {r.text[:200]}"); return False
    except Exception as e:
        print(f"  [MAILJET] x {e}"); return False


def _try_mailersend(to, subject, html, p) -> bool:
    """
    MailerSend HTTPS API. Free 3000/month (most generous free tier).
    Needs own domain. Cannot use Gmail directly.
    Setup:
      1. mailersend.com > free account
      2. Domains > Add domain > verify DNS records
      3. API Tokens > Generate token
    """
    api_key, feml = p.get("api_key",""), p.get("from_email","")
    fname = p.get("from_name","Support")
    if not api_key or not feml:
        print("  [MAILERSEND] Missing api_key/from_email — skip"); return False
    try:
        r = http_req.post(
            "https://api.mailersend.com/v1/email",
            headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},
            json={"from":{"email":feml,"name":fname},"to":[{"email":to}],
                  "subject":subject,"html":html},
            timeout=12
        )
        if r.status_code in (200,202):
            print(f"  [MAILERSEND] + Sent via {feml}"); return True
        if r.status_code == 429:
            print(f"  [MAILERSEND] x Rate limit — next"); return False
        if r.status_code == 401:
            print(f"  [MAILERSEND] x Invalid token — skip"); return False
        print(f"  [MAILERSEND] x {r.status_code}: {r.text[:200]}"); return False
    except Exception as e:
        print(f"  [MAILERSEND] x {e}"); return False


def _try_resend(to, subject, html, p) -> bool:
    """
    Resend HTTPS API. Free 3000/month. Needs own domain.
    Setup: resend.com > add domain > verify DNS > API Keys > create key
    """
    api_key, feml = p.get("api_key",""), p.get("from_email","")
    fname = p.get("from_name","Support")
    if not api_key or not feml:
        print("  [RESEND] Missing api_key/from_email — skip"); return False
    try:
        sender = f"{fname} <{feml}>" if fname else feml
        r = http_req.post(
            "https://api.resend.com/emails",
            headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},
            json={"from":sender,"to":[to],"subject":subject,"html":html},
            timeout=12
        )
        if r.status_code in (200,201):
            print(f"  [RESEND] + Sent via {feml}"); return True
        if r.status_code == 429:
            print(f"  [RESEND] x Rate limit — next"); return False
        if r.status_code == 403:
            print(f"  [RESEND] x Invalid API key — skip"); return False
        print(f"  [RESEND] x {r.status_code}: {r.text[:200]}"); return False
    except Exception as e:
        print(f"  [RESEND] x {e}"); return False


def _try_sendgrid(to, subject, html, p) -> bool:
    """
    SendGrid HTTPS API. Free 100/day. Needs own domain.
    Setup: sendgrid.com > Settings > Sender Authentication > verify domain
           > API Keys > create key with Mail Send permission
    """
    api_key, feml = p.get("api_key",""), p.get("from_email","")
    fname = p.get("from_name","Support")
    if not api_key or not feml:
        print("  [SENDGRID] Missing api_key/from_email — skip"); return False
    try:
        r = http_req.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},
            json={
                "personalizations":[{"to":[{"email":to}]}],
                "from":{"email":feml,"name":fname},
                "subject":subject,
                "content":[{"type":"text/html","value":html}]
            },
            timeout=12
        )
        if r.status_code in (200,202):
            print(f"  [SENDGRID] + Sent via {feml}"); return True
        if r.status_code == 429:
            print(f"  [SENDGRID] x Daily limit — next"); return False
        if r.status_code == 401:
            print(f"  [SENDGRID] x Invalid API key — skip"); return False
        print(f"  [SENDGRID] x {r.status_code}: {r.text[:200]}"); return False
    except Exception as e:
        print(f"  [SENDGRID] x {e}"); return False


# ── Dispatch table ─────────────────────────────────────────────────────────────

_SENDERS = {
    "GSMTP":      _try_gsmtp,
    "BREVO":      _try_brevo,
    "GWORK":      _try_gwork,
    "SES":        _try_ses,
    "MAILGUN":    _try_mailgun,
    "MAILJET":    _try_mailjet,
    "MAILERSEND": _try_mailersend,
    "RESEND":     _try_resend,
    "SENDGRID":   _try_sendgrid,
}

# ── Public API ─────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, html_body: str, test_mode: bool = False) -> bool:
    """
    Try each method in EMAIL_SEND_METHODS in order.
    Returns True on first success.
    Returns False only if ALL methods fail.
    In test_mode: prints to log instead of sending real emails.
    """
    if test_mode:
        sep = "-" * 55
        print(f"\n{sep}")
        print(f"  [TEST EMAIL]  To: {to}")
        print(f"  Subject: {subject}")
        print(f"{sep}\n")
        return True

    if not EMAIL_SEND_METHODS:
        print("WARNING: EMAIL_SEND_METHODS not configured — email skipped")
        return False

    n = len(EMAIL_SEND_METHODS)
    print(f"\n  Sending email -> {to} | {subject}")
    for i, entry in enumerate(EMAIL_SEND_METHODS):
        method = entry.get("method", "").upper()
        fn     = _SENDERS.get(method)
        if not fn:
            print(f"  [{i+1}/{n}] Unknown method '{method}' — skip")
            continue
        print(f"  [{i+1}/{n}] Trying {method}...")
        try:
            if fn(to, subject, html_body, entry):
                return True   # success — stop chain
        except Exception as e:
            print(f"  [{method}] Unexpected error: {e}")
        time.sleep(0.3)

    print(f"  x All {n} email methods failed -> {to}")
    return False