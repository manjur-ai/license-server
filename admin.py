"""
admin_ui.py  —  Single-file HTML admin dashboard
Mount into main.py with:  app.mount("/admin", admin_app)
Access: GET /admin/ui

Security model:
  - SHARED_SECRET never appears in HTML source, cookies, or any browser storage.
  - Login is two-factor: ADMIN_TOKEN password + OTP sent to ADMIN_EMAILS.
  - On login, browser calls GET /admin/wrapped-secret which returns
    SHARED_SECRET wrapped with AES-GCM using a key derived via
    PBKDF2-SHA256(ADMIN_TOKEN, random_salt, 100k iterations).
  - Browser unwraps client-side with WebCrypto — secret held only in JS memory.
  - All API calls use the unwrapped key for AES-GCM encryption.
  - Session token expires in 30 minutes. Page shows countdown.
  - Add ADMIN_EMAILS env var (comma-separated). Falls back to SUPPORT_EMAIL.
"""
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>License Server — Admin</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
  .topbar{background:#161b22;border-bottom:1px solid #30363d;padding:14px 28px;
          display:flex;align-items:center;gap:16px}
  .topbar h1{font-size:16px;font-weight:600;color:#58a6ff}
  .topbar span{font-size:12px;color:#8b949e}
  .tabs{display:flex;gap:0;background:#161b22;border-bottom:1px solid #30363d;padding:0 28px}
  .tab{padding:10px 18px;font-size:13px;cursor:pointer;border-bottom:2px solid transparent;
       color:#8b949e;transition:.15s}
  .tab:hover{color:#e6edf3}
  .tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
  .page{display:none;padding:28px;max-width:1200px}
  .page.active{display:block}
  .stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-bottom:28px}
  .stat-card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px}
  .stat-card .val{font-size:28px;font-weight:700;color:#58a6ff}
  .stat-card .lbl{font-size:12px;color:#8b949e;margin-top:4px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px;margin-bottom:20px}
  .card h2{font-size:14px;font-weight:600;margin-bottom:14px;color:#e6edf3}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:8px 12px;color:#8b949e;font-weight:500;
     border-bottom:1px solid #30363d;font-size:12px}
  td{padding:8px 12px;border-bottom:1px solid #21262d;vertical-align:middle}
  tr:hover td{background:#1c2128}
  .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
  .badge-green{background:#033a16;color:#3fb950}
  .badge-red{background:#3d1212;color:#f85149}
  .badge-amber{background:#2d1f00;color:#d29922}
  .badge-blue{background:#0c2d6b;color:#58a6ff}
  .form-row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px}
  .form-row label{font-size:12px;color:#8b949e;display:block;margin-bottom:4px}
  .form-row input,.form-row select{background:#0d1117;border:1px solid #30363d;
    color:#e6edf3;padding:7px 11px;border-radius:6px;font-size:13px;min-width:140px}
  .btn{padding:7px 16px;border-radius:6px;border:none;font-size:13px;cursor:pointer;font-weight:500}
  .btn-primary{background:#238636;color:#fff}.btn-primary:hover{background:#2ea043}
  .btn-danger{background:#b91c1c;color:#fff}.btn-danger:hover{background:#dc2626}
  .btn-secondary{background:#21262d;color:#e6edf3;border:1px solid #30363d}
  .btn-secondary:hover{background:#30363d}
  .msg{padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:14px}
  .msg-ok{background:#033a16;color:#3fb950;border:1px solid #1a5c2a}
  .msg-err{background:#3d1212;color:#f85149;border:1px solid #8b1a1a}
  #login-overlay{position:fixed;inset:0;background:#0d1117;z-index:1000;
    display:flex;align-items:center;justify-content:center}
  .login-box{background:#161b22;border:1px solid #30363d;border-radius:12px;
    padding:40px;width:100%;max-width:400px}
  .login-box h2{color:#58a6ff;font-size:20px;font-weight:600;margin-bottom:8px}
  .login-box p.sub{color:#8b949e;font-size:13px;margin-bottom:28px}
  .login-box input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;
    padding:10px 14px;border-radius:6px;font-size:14px;margin-bottom:12px;display:block}
  .login-box input:focus{outline:none;border-color:#58a6ff}
  .login-box button{width:100%;padding:10px;border-radius:6px;border:none;
    background:#238636;color:#fff;font-size:14px;font-weight:500;cursor:pointer;margin-top:4px}
  .login-box button:hover:not(:disabled){background:#2ea043}
  .login-box button:disabled{background:#21262d;color:#8b949e;cursor:default}
  #login-err{color:#f85149;font-size:13px;margin-bottom:12px;min-height:18px}
  #login-step2{display:none}
  .session-bar{font-size:12px;color:#8b949e;margin-left:auto;cursor:pointer}
  .session-bar:hover{color:#e6edf3}
  #loading{position:fixed;inset:0;background:rgba(13,17,23,.75);
           display:none;align-items:center;justify-content:center;
           font-size:14px;color:#58a6ff;z-index:500}
</style>
</head>
<body>

<!-- ── Login overlay ─────────────────────────────────────────────────────── -->
<div id="login-overlay">
  <div class="login-box">
    <h2>⚙ Admin Login</h2>
    <p class="sub">License Server Admin Dashboard</p>
    <div id="login-err"></div>

    <div id="login-step1">
      <input type="password" id="l-pass" placeholder="Admin token / password"
             onkeydown="if(event.key==='Enter')loginStep1()"/>
      <button id="l-btn1" onclick="loginStep1()">Continue →</button>
    </div>

    <div id="login-step2">
      <p id="l-otp-hint" style="color:#8b949e;font-size:13px;margin-bottom:16px"></p>
      <input type="text" id="l-otp" placeholder="6-digit code"
             maxlength="6" style="letter-spacing:8px;font-size:20px;text-align:center"
             oninput="this.value=this.value.replace(/\\D/g,'')"
             onkeydown="if(event.key==='Enter')loginStep2()"/>
      <button id="l-btn2" onclick="loginStep2()">Verify &amp; Connect</button>
      <p style="margin-top:14px;font-size:12px;color:#8b949e;text-align:center">
        <span style="cursor:pointer;color:#58a6ff" onclick="loginReset()">← Back</span>
        &nbsp;·&nbsp;
        <span style="cursor:pointer;color:#58a6ff" onclick="loginStep1()">Resend OTP</span>
      </p>
    </div>
  </div>
</div>

<div id="loading">Loading…</div>

<div class="topbar">
  <h1>⚙ License Server Admin</h1>
  <span id="health-badge">checking…</span>
  <span class="session-bar" id="session-bar" onclick="doLogout()" title="Click to log out"></span>
</div>
<div class="tabs">
  <div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
  <div class="tab" onclick="showTab('products')">Products</div>
  <div class="tab" onclick="showTab('coupons')">Coupons</div>
  <div class="tab" onclick="showTab('customers')">Customers</div>
  <div class="tab" onclick="showTab('data')">📊 Data</div>
</div>

<!-- DASHBOARD -->
<div class="page active" id="page-dashboard">
  <div class="stat-grid" id="stats-grid"></div>
  <div class="card"><h2>Quick Actions</h2>
    <div class="form-row">
      <div><label>Revoke license by email or phone</label>
        <input type="text" id="revoke-email" placeholder="user@example.com or +91..." style="width:280px"/>
      </div>
      <div><label>Identity type</label>
        <select id="revoke-itype">
          <option value="email">email</option>
          <option value="sms">sms (phone)</option>
        </select>
      </div>
      <div><label>Product ID (blank=all)</label>
        <input type="text" id="revoke-prod" placeholder="optional" style="width:140px"/>
      </div>
      <div><label>Reason</label>
        <select id="revoke-reason">
          <option value="manual">Manual</option>
          <option value="abuse">Abuse</option>
          <option value="refund">Refund</option>
        </select>
      </div>
      <button class="btn btn-danger" onclick="revokeAction()">Revoke</button>
    </div>
    <div id="revoke-msg"></div>
  </div>
</div>

<!-- PRODUCTS -->
<div class="page" id="page-products">
  <div class="card"><h2>Products</h2>
    <div id="prod-msg"></div>
    <div class="form-row">
      <div><label>Product ID*</label><input id="p-id" placeholder="TOOL1"/></div>
      <div><label>Name*</label><input id="p-name" placeholder="My Tool Pro" style="width:200px"/></div>
      <div><label>Price INR</label><input id="p-inr" type="number" placeholder="499" style="width:100px"/></div>
      <div><label>Price USD</label><input id="p-usd" type="number" placeholder="9.99" style="width:100px"/></div>
      <div><label>Max Machines</label><input id="p-max" type="number" value="1" style="width:80px"/></div>
      <div><label>Trial Days</label><input id="p-trial" type="number" value="0" style="width:80px"/></div>
    </div>
    <div class="form-row">
      <div><label>Razorpay Link</label><input id="p-rzp" placeholder="https://rzp.io/l/..." style="width:260px"/></div>
      <div><label>Gumroad Product ID</label><input id="p-gum-id" placeholder="abc123" style="width:140px"/></div>
      <div><label>Gumroad Link</label><input id="p-gum-link" placeholder="https://..." style="width:220px"/></div>
      <button class="btn btn-primary" onclick="saveProduct()">Save Product</button>
    </div>
    <table><thead><tr>
      <th>ID</th><th>Name</th><th>INR</th><th>USD</th><th>Machines</th>
      <th>Trial</th><th>Status</th><th>Actions</th>
    </tr></thead><tbody id="prod-body"></tbody></table>
  </div>
</div>

<!-- COUPONS -->
<div class="page" id="page-coupons">
  <div class="card"><h2>Coupons</h2>
    <div id="coup-msg"></div>
    <div class="form-row">
      <div><label>Code*</label><input id="c-code" placeholder="LAUNCH20" style="text-transform:uppercase"/></div>
      <div><label>Product ID (blank=all)</label><input id="c-prod" placeholder="optional"/></div>
      <div><label>Discount %</label><input id="c-pct" type="number" value="0" style="width:80px"/></div>
      <div><label>Flat INR off</label><input id="c-inr" type="number" value="0" style="width:90px"/></div>
      <div><label>Flat USD off</label><input id="c-usd" type="number" value="0" style="width:90px"/></div>
      <div><label>Plan Override</label>
        <select id="c-plan">
          <option value="">None</option>
          <option value="trial">trial</option>
          <option value="monthly">monthly</option>
          <option value="annual">annual</option>
          <option value="lifetime">lifetime</option>
        </select>
      </div>
      <div><label>Max Uses</label><input id="c-uses" type="number" value="1" style="width:80px"/></div>
      <div><label>Expires (unix ts)</label><input id="c-until" type="number" placeholder="optional" style="width:130px"/></div>
      <button class="btn btn-primary" onclick="saveCoupon()">Create Coupon</button>
    </div>
    <table><thead><tr>
      <th>Code</th><th>Product</th><th>Discount</th><th>Plan</th>
      <th>Uses</th><th>Max</th><th>Expires</th><th>Status</th>
    </tr></thead><tbody id="coup-body"></tbody></table>
  </div>
</div>

<!-- CUSTOMERS -->
<div class="page" id="page-customers">
  <div class="card"><h2>Customer Lookup</h2>
    <div class="form-row">
      <div><label>Email or Phone</label>
        <input id="cust-search" placeholder="user@example.com" style="width:300px"/>
      </div>
      <select id="cust-type"><option value="email">email</option><option value="sms">sms</option></select>
      <button class="btn btn-secondary" onclick="lookupCustomer()">Look up</button>
    </div>
    <div id="cust-result" style="margin-top:16px"></div>
  </div>
</div>

<!-- DATA BROWSER -->
<div class="page" id="page-data">
  <div style="display:flex;gap:0;border-bottom:1px solid #30363d;margin-bottom:20px">
    <div class="tab active" id="dt-tab-lic"  onclick="dtSwitch('lic')"  style="padding:8px 16px">Licenses</div>
    <div class="tab"        id="dt-tab-pay"  onclick="dtSwitch('pay')"  style="padding:8px 16px">Payments</div>
    <div class="tab"        id="dt-tab-cust" onclick="dtSwitch('cust')" style="padding:8px 16px">Customers</div>
  </div>
  <div id="dt-lic">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-lic-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('lic',-1)" id="dt-lic-prev" disabled>&#9664; Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('lic', 1)" id="dt-lic-next" disabled>Next &#9654;</button>
      <button class="btn btn-primary"   onclick="dtLoad('lic', 0)">Load / Refresh</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto">
      <table><thead><tr>
        <th>Activated</th><th>Identity</th><th>Ch</th><th>Product</th>
        <th>Plan</th><th>Status</th><th>Source</th><th>Amount</th>
        <th>Verifies</th><th>Last seen</th><th>Machine</th>
      </tr></thead><tbody id="dt-lic-body">
        <tr><td colspan="11" style="color:#8b949e;text-align:center;padding:32px">Click Load to fetch licenses</td></tr>
      </tbody></table>
    </div>
  </div>
  <div id="dt-pay" style="display:none">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-pay-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('pay',-1)" id="dt-pay-prev" disabled>&#9664; Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('pay', 1)" id="dt-pay-next" disabled>Next &#9654;</button>
      <button class="btn btn-primary"   onclick="dtLoad('pay', 0)">Load / Refresh</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto">
      <table><thead><tr>
        <th>Paid at</th><th>Identity</th><th>Ch</th><th>Product</th>
        <th>Source</th><th>Amount</th><th>Plan</th><th>Status</th><th>Payment ref</th>
      </tr></thead><tbody id="dt-pay-body">
        <tr><td colspan="9" style="color:#8b949e;text-align:center;padding:32px">Click Load to fetch payments</td></tr>
      </tbody></table>
    </div>
  </div>
  <div id="dt-cust" style="display:none">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-cust-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('cust',-1)" id="dt-cust-prev" disabled>&#9664; Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('cust', 1)" id="dt-cust-next" disabled>Next &#9654;</button>
      <button class="btn btn-primary"   onclick="dtLoad('cust', 0)">Load / Refresh</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto">
      <table><thead><tr>
        <th>Joined</th><th>Identity</th><th>Ch</th>
        <th>Active licenses</th><th>Total licenses</th>
      </tr></thead><tbody id="dt-cust-body">
        <tr><td colspan="5" style="color:#8b949e;text-align:center;padding:32px">Click Load to fetch customers</td></tr>
      </tbody></table>
    </div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
//  SECURITY MODEL
//  _sk   : CryptoKey (AES-GCM) — unwrapped from server using PBKDF2(password)
//          Lives only in JS heap. Never in DOM, localStorage, or cookies.
//  _tok  : session_token — HMAC-SHA256 signed by server, 30-min TTL.
//  _pass : admin token — cleared from memory immediately after key unwrap.
//
//  SHARED_SECRET never appears in HTML source or any network response in
//  plaintext. The server wraps it with a fresh random salt on every request
//  to /admin/wrapped-secret. An attacker who intercepts the wrapped blob
//  cannot use it without also knowing ADMIN_TOKEN.
// ═══════════════════════════════════════════════════════════════════════════
const base = window.location.origin;
let _sk            = null;
let _tok           = '';
let _pass          = '';
let _sessionExpiry = 0;

// ── AES-GCM encrypt/decrypt ──────────────────────────────────────────────────
async function aesEncrypt(obj) {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const pt    = new TextEncoder().encode(JSON.stringify(obj));
  const ct    = new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},_sk,pt));
  const out   = new Uint8Array(12 + ct.length);
  out.set(nonce); out.set(ct, 12);
  let bin = '';
  for (let i = 0; i < out.length; i++) bin += String.fromCharCode(out[i]);
  return btoa(bin);
}

async function aesDecrypt(b64) {
  const bin = atob(b64);
  const raw = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) raw[i] = bin.charCodeAt(i);
  const pt = await crypto.subtle.decrypt({name:'AES-GCM',iv:raw.slice(0,12)},_sk,raw.slice(12));
  return JSON.parse(new TextDecoder().decode(pt));
}

// ── Authenticated API call ────────────────────────────────────────────────────
async function apiCall(endpoint, body) {
  const payload = {...body, session_token: _tok, timestamp: Math.floor(Date.now()/1000)};
  const r = await fetch(base + endpoint, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({data: await aesEncrypt(payload)})
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => '');
    throw new Error('HTTP ' + r.status + (txt ? ' — ' + txt.slice(0,120) : ''));
  }
  let j;
  try { j = await r.json(); } catch(e) {
    throw new Error('Server returned non-JSON (HTTP ' + r.status + ')');
  }
  if (!j || !j.data) throw new Error('Server response missing data field');
  return await aesDecrypt(j.data);
}

// ── PBKDF2 key derivation (pure WebCrypto, no libraries) ─────────────────────
async function pbkdf2Unwrap(password, saltHex, nonceHex, wrappedHex, iterations) {
  const enc     = new TextEncoder();
  const toArr   = hex => new Uint8Array(hex.match(/.{2}/g).map(b => parseInt(b,16)));
  const baseKey = await crypto.subtle.importKey('raw', enc.encode(password),
                    {name:'PBKDF2'}, false, ['deriveKey']);
  const wrapKey = await crypto.subtle.deriveKey(
    {name:'PBKDF2', salt:toArr(saltHex), iterations, hash:'SHA-256'},
    baseKey, {name:'AES-GCM', length:256}, false, ['decrypt']
  );
  const skRaw = await crypto.subtle.decrypt(
    {name:'AES-GCM', iv:toArr(nonceHex)}, wrapKey, toArr(wrappedHex)
  );
  return crypto.subtle.importKey('raw', skRaw, {name:'AES-GCM'}, false, ['encrypt','decrypt']);
}

// ── LOGIN STEP 1 — password + key unwrap + OTP request ───────────────────────
function loginReset() {
  _sk = null; _tok = ''; _pass = ''; _sessionExpiry = 0;
  document.getElementById('login-step1').style.display = '';
  document.getElementById('login-step2').style.display = 'none';
  document.getElementById('login-err').textContent     = '';
  document.getElementById('l-pass').value = '';
  document.getElementById('l-otp').value  = '';
  btnState('l-btn1', false); btnState('l-btn2', false);
}

function btnState(id, busy) {
  const b = document.getElementById(id);
  b.disabled    = busy;
  b.textContent = busy
    ? (id==='l-btn1' ? 'Sending OTP…' : 'Verifying…')
    : (id==='l-btn1' ? 'Continue \u2192'   : 'Verify & Connect');
}

async function loginStep1() {
  const pass = document.getElementById('l-pass').value.trim();
  if (!pass) { document.getElementById('login-err').textContent = 'Enter your admin token'; return; }
  _pass = pass;
  document.getElementById('login-err').textContent = '';
  btnState('l-btn1', true);
  try {
    // 1. Fetch wrapped secret (unauthenticated — safe, useless without password)
    const wr = await fetch(base + '/auth/admin/wrapped-secret').then(r => {
      if (!r.ok) throw new Error('Server unreachable (HTTP ' + r.status + ')');
      return r.json();
    });

    // 2. Unwrap SHARED_SECRET using PBKDF2(password, salt)
    try {
      _sk = await pbkdf2Unwrap(_pass, wr.salt, wr.nonce, wr.wrapped, wr.iterations);
    } catch(e) {
      throw new Error('Wrong admin token — password incorrect');
    }

    // 3. Request OTP (encrypted with just-unwrapped key)
    const otpR = await apiCall('/auth/admin/request-otp', {admin_token: _pass});
    if (!otpR.ok) {
      _sk = null; _pass = '';
      throw new Error(
        otpR.reason === 'unauthorized'              ? 'Wrong admin token'
      : otpR.reason === 'no_admin_email_configured' ? 'ADMIN_EMAILS not configured on server — add it to Railway env vars'
      : otpR.reason === 'delivery_failed'           ? 'OTP email delivery failed — check EMAIL_SEND_METHODS'
      : otpR.reason || 'OTP request failed'
      );
    }

    // 4. Advance to OTP step
    document.getElementById('l-otp-hint').textContent =
      'OTP sent to: ' + (otpR.sent_to || []).join(', ');
    document.getElementById('login-step1').style.display = 'none';
    document.getElementById('login-step2').style.display = '';
    setTimeout(() => document.getElementById('l-otp').focus(), 50);
    btnState('l-btn1', false);

  } catch(e) {
    document.getElementById('login-err').textContent = e.message;
    btnState('l-btn1', false);
    _sk = null; _pass = '';
  }
}

// ── LOGIN STEP 2 — OTP verify + receive session token ────────────────────────
async function loginStep2() {
  const otp = document.getElementById('l-otp').value.trim();
  if (otp.length !== 6) { document.getElementById('login-err').textContent = 'Enter the 6-digit code'; return; }
  document.getElementById('login-err').textContent = '';
  btnState('l-btn2', true);
  try {
    const r = await apiCall('/auth/admin/verify-otp', {admin_token: _pass, otp});
    if (!r.ok) {
      throw new Error(
        r.reason === 'invalid_otp'           ? 'Wrong code — check the email and try again'
      : r.reason === 'otp_expired'           ? 'Code expired — click Resend OTP'
      : r.reason === 'max_attempts_exceeded' ? 'Too many wrong attempts — click Resend OTP'
      : r.reason === 'otp_not_requested'     ? 'No OTP was requested — go back and start over'
      : r.reason || 'Verification failed'
      );
    }
    _tok           = r.session_token;
    _pass          = '';   // no longer needed — clear immediately
    _sessionExpiry = Date.now() + r.expires_in * 1000;
    document.getElementById('login-overlay').style.display = 'none';
    startSessionTimer();
    afterLogin();
  } catch(e) {
    document.getElementById('login-err').textContent = e.message;
    btnState('l-btn2', false);
  }
}

// ── Session countdown in topbar ───────────────────────────────────────────────
function startSessionTimer() {
  const el   = document.getElementById('session-bar');
  const tick = () => {
    const left = Math.max(0, Math.round((_sessionExpiry - Date.now()) / 1000));
    if (left === 0) { doLogout(); return; }
    const m = Math.floor(left/60), s = (left%60).toString().padStart(2,'0');
    el.textContent = 'Session: ' + m + ':' + s + '  \u00b7  Log out';
    setTimeout(tick, 1000);
  };
  tick();
}

function doLogout() {
  _sk = null; _tok = ''; _pass = ''; _sessionExpiry = 0;
  document.getElementById('login-overlay').style.display = 'flex';
  loginReset();
}

// ── After login — load initial data ──────────────────────────────────────────
async function afterLogin() {
  document.getElementById('loading').style.display = 'flex';
  try {
    const s = await apiCall('/admin/stats', {});
    if (s.ok) renderStats(s);
    await Promise.all([loadProducts(), loadCoupons()]);
  } catch(e) { console.error('afterLogin:', e); }
  finally { document.getElementById('loading').style.display = 'none'; }
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(t) {
  document.querySelectorAll('.tabs > .tab').forEach(e => {
    const name = e.textContent.trim().toLowerCase().replace(/^📊 /,'');
    e.classList.toggle('active', name === t);
  });
  document.querySelectorAll('.page').forEach(e =>
    e.classList.toggle('active', e.id === 'page-' + t));
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function renderStats(s) {
  document.getElementById('stats-grid').innerHTML = [
    ['Total Customers',  s.total_customers   ?? 0, 'blue'],
    ['Active Licenses',  s.active_licenses   ?? 0, 'green'],
    ['Trial Licenses',   s.trial_licenses    ?? 0, 'amber'],
    ['Revenue INR',     '&#8377;'+(s.revenue_inr ??0).toFixed(2), 'green'],
    ['Revenue USD',     '$'+(s.revenue_usd  ??0).toFixed(2), 'blue'],
    ['Total Payments',   s.total_payments    ?? 0, ''],
    ['Refunds',          s.refunds           ?? 0, s.refunds?'red':''],
    ['Coupons Used',     s.coupons_redeemed  ?? 0, 'amber'],
    ['OTPs (24h)',       s.otps_sent_last_24h?? 0, ''],
  ].map(([l,v,c]) => `<div class="stat-card">
    <div class="val" style="${c==='green'?'color:#3fb950':c==='red'?'color:#f85149':c==='amber'?'color:#d29922':''}">${v}</div>
    <div class="lbl">${l}</div></div>`).join('');
}

// ── Products ──────────────────────────────────────────────────────────────────
async function loadProducts() {
  const r = await apiCall('/admin/products', {include_inactive:true});
  if (!r.ok) return;
  document.getElementById('prod-body').innerHTML = r.products.map(p => `<tr>
    <td><code>${p.product_id}</code></td><td>${p.name}</td>
    <td>&#8377;${p.price_inr||0}</td><td>$${p.price_usd||0}</td>
    <td>${p.max_machines}</td><td>${p.trial_days||0}d</td>
    <td><span class="badge ${p.is_active?'badge-green':'badge-red'}">${p.is_active?'Active':'Inactive'}</span></td>
    <td>
      <button class="btn btn-secondary" style="font-size:11px;padding:4px 10px"
        onclick="fillProduct(${JSON.stringify(p).replace(/"/g,'&quot;')})">Edit</button>
      <button class="btn btn-danger" style="font-size:11px;padding:4px 10px;margin-left:4px"
        onclick="delProduct('${p.product_id}')">Del</button>
    </td></tr>`).join('');
}
function fillProduct(p) {
  ['p-id','p-name','p-inr','p-usd','p-max','p-trial','p-rzp','p-gum-id','p-gum-link'].forEach(id => {
    const key = {
      'p-id':'product_id','p-name':'name','p-inr':'price_inr','p-usd':'price_usd',
      'p-max':'max_machines','p-trial':'trial_days','p-rzp':'razorpay_link',
      'p-gum-id':'gumroad_product_id','p-gum-link':'gumroad_link'
    }[id];
    document.getElementById(id).value = p[key] || (typeof p[key]==='number'?p[key]:'');
  });
  showTab('products');
}
async function saveProduct() {
  const r = await apiCall('/admin/product', {
    product_id: document.getElementById('p-id').value.trim().toUpperCase(),
    name:       document.getElementById('p-name').value.trim(),
    price_inr: +document.getElementById('p-inr').value,
    price_usd: +document.getElementById('p-usd').value,
    max_machines: +document.getElementById('p-max').value,
    trial_days:   +document.getElementById('p-trial').value,
    razorpay_link:      document.getElementById('p-rzp').value.trim()||null,
    gumroad_product_id: document.getElementById('p-gum-id').value.trim()||null,
    gumroad_link:       document.getElementById('p-gum-link').value.trim()||null,
  });
  const m = document.getElementById('prod-msg');
  m.className = 'msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent = r.ok ? (r.created?'&#10003; Product created':'&#10003; Product updated') : '&#10007; '+r.reason;
  if (r.ok) loadProducts();
}
async function delProduct(id) {
  if (!confirm('Soft-delete '+id+'? Licenses are preserved.')) return;
  const r = await apiCall('/admin/product/delete', {product_id:id});
  if (r.ok) loadProducts();
}

// ── Coupons ───────────────────────────────────────────────────────────────────
async function loadCoupons() {
  const r = await apiCall('/admin/coupons', {});
  if (!r.ok) return;
  document.getElementById('coup-body').innerHTML = r.coupons.map(c => `<tr>
    <td><code>${c.code}</code></td>
    <td>${c.product_id||'All'}</td>
    <td>${c.discount_pct?c.discount_pct+'%':''} ${c.discount_flat_inr?'&#8377;'+c.discount_flat_inr:''} ${c.discount_flat_usd?'$'+c.discount_flat_usd:''}</td>
    <td>${c.plan_override||'&#8212;'}</td>
    <td>${c.uses}</td><td>${c.max_uses}</td>
    <td>${fmt_ts(c.valid_until)}</td>
    <td><span class="badge ${c.is_active&&c.uses<c.max_uses?'badge-green':'badge-red'}">${c.is_active&&c.uses<c.max_uses?'Active':'Done'}</span></td>
    </tr>`).join('');
}
async function saveCoupon() {
  const r = await apiCall('/admin/coupon', {
    code:              document.getElementById('c-code').value.trim(),
    product_id:        document.getElementById('c-prod').value.trim()||null,
    discount_pct:     +document.getElementById('c-pct').value,
    discount_flat_inr:+document.getElementById('c-inr').value,
    discount_flat_usd:+document.getElementById('c-usd').value,
    plan_override:     document.getElementById('c-plan').value||null,
    max_uses:         +document.getElementById('c-uses').value,
    valid_until:      +document.getElementById('c-until').value||null,
  });
  const m = document.getElementById('coup-msg');
  m.className = 'msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent = r.ok
    ? '&#10003; Coupon '+document.getElementById('c-code').value.trim().toUpperCase()+' created'
    : '&#10007; '+r.reason;
  if (r.ok) loadCoupons();
}

// ── Revoke ────────────────────────────────────────────────────────────────────
async function revokeAction() {
  const identity = document.getElementById('revoke-email').value.trim();
  if (!identity) return;
  const r = await apiCall('/admin/revoke', {
    identity,
    identity_type: document.getElementById('revoke-itype').value,
    product_id:    document.getElementById('revoke-prod').value.trim()||null,
    reason:        document.getElementById('revoke-reason').value,
  });
  const m = document.getElementById('revoke-msg');
  m.className = 'msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent = r.ok ? '&#10003; Revoked '+r.revoked+' license(s)' : '&#10007; '+r.reason;
}

// ── Customer lookup ───────────────────────────────────────────────────────────
async function lookupCustomer() {
  const identity = document.getElementById('cust-search').value.trim();
  if (!identity) return;
  const r  = await apiCall('/admin/customer', {
    identity, identity_type: document.getElementById('cust-type').value
  });
  const el = document.getElementById('cust-result');
  if (!r.ok) { el.innerHTML = '<div class="msg msg-err">&#10007; '+r.reason+'</div>'; return; }
  const c = r.customer;
  el.innerHTML = `<div class="card">
    <h2>${c.identity} &#8212; member since ${fmt_ts(c.member_since)}</h2>
    <p style="font-size:13px;color:#8b949e;margin:8px 0">${r.active_licenses} active / ${r.total_licenses} total</p>
    <table><thead><tr><th>Product</th><th>Plan</th><th>Activated</th><th>Expires</th><th>Days Left</th><th>Status</th></tr></thead>
    <tbody>${r.licenses.map(l=>`<tr>
      <td>${l.product_name||l.product_id}</td>
      <td><span class="badge badge-blue">${l.plan}</span></td>
      <td>${fmt_ts(l.activated_at)}</td><td>${fmt_ts(l.expires_at)}</td>
      <td>${l.days_left!=null?l.days_left+'d':'&#8734;'}</td>
      <td><span class="badge ${l.is_active&&!l.is_expired?'badge-green':'badge-red'}">${l.is_active&&!l.is_expired?'Active':l.is_expired?'Expired':'Revoked'}</span></td>
    </tr>`).join('')}</tbody></table></div>`;
}

// ── Data browser ──────────────────────────────────────────────────────────────
const DT_PAGE = 500;
const dtState = {
  lic: {offset:0,total:0,loaded:false},
  pay: {offset:0,total:0,loaded:false},
  cust:{offset:0,total:0,loaded:false}
};
const dtEp = {lic:'licenses',pay:'payments',cust:'customers'};

function dtSwitch(v) {
  ['lic','pay','cust'].forEach(x => {
    document.getElementById('dt-'+x).style.display = x===v?'':'none';
    document.getElementById('dt-tab-'+x).classList.toggle('active', x===v);
  });
  if (!dtState[v].loaded) dtLoad(v, 0);
}

async function dtLoad(v, dir) {
  const st = dtState[v];
  if (dir===0) st.offset=0;
  else if (dir===1)  st.offset = Math.min(st.offset+DT_PAGE, Math.max(0,st.total-DT_PAGE));
  else if (dir===-1) st.offset = Math.max(0, st.offset-DT_PAGE);
  const info = document.getElementById('dt-'+v+'-info');
  const prev = document.getElementById('dt-'+v+'-prev');
  const next = document.getElementById('dt-'+v+'-next');
  const body = document.getElementById('dt-'+v+'-body');
  info.textContent='Loading…'; prev.disabled=true; next.disabled=true;
  try {
    const r = await apiCall('/admin/browse/'+dtEp[v], {offset:st.offset, limit:DT_PAGE});
    if (!r.ok) { info.textContent='Error: '+(r.reason||'unknown'); return; }
    st.total=r.total; st.loaded=true;
    const from=st.offset+1, to=Math.min(st.offset+r.rows.length,r.total);
    info.textContent = r.total===0 ? 'No records'
      : 'Showing '+from+'&#8211;'+to+' of '+r.total+' (latest first)';
    prev.disabled = st.offset<=0;
    next.disabled = !r.has_more;
    if (v==='lic')  body.innerHTML = dtLic(r.rows);
    if (v==='pay')  body.innerHTML = dtPay(r.rows);
    if (v==='cust') body.innerHTML = dtCust(r.rows);
  } catch(e) { info.textContent='Error: '+e.message; }
}

function chB(t){ return t==='email'?'<span style="color:#58a6ff;font-size:11px">&#9993;</span>':'<span style="color:#3fb950;font-size:11px">&#128241;</span>'; }

function dtLic(rows) {
  if (!rows.length) return '<tr><td colspan="11" style="color:#8b949e;text-align:center;padding:24px">No licenses</td></tr>';
  return rows.map(r=>{
    const st = !r.is_active?'<span class="badge badge-red">Revoked</span>'
      :r.expires_at&&Date.now()/1000>r.expires_at?'<span class="badge badge-amber">Expired</span>'
      :'<span class="badge badge-green">Active</span>';
    const amt=r.currency==='INR'?'&#8377;'+(r.amount||0):'$'+(r.amount||0);
    const mc=r.machine_label||(r.machine_id?r.machine_id.slice(0,12)+'&#8230;':'&#8212;');
    return `<tr>
      <td style="white-space:nowrap">${fmt_ts(r.activated_at)}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.identity}">${r.identity}</td>
      <td>${chB(r.identity_type)}</td><td><code>${r.product_id}</code></td>
      <td><span class="badge badge-blue">${r.plan}</span></td><td>${st}</td>
      <td style="font-size:11px;color:#8b949e">${r.source||'&#8212;'}</td>
      <td style="white-space:nowrap">${amt} ${r.currency||''}</td>
      <td style="text-align:center">${r.verify_count||0}</td>
      <td style="white-space:nowrap;color:#8b949e;font-size:12px">${fmt_ts(r.last_seen_at)}</td>
      <td style="font-size:11px;color:#8b949e;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${r.machine_label||r.machine_id||''}">${mc}</td></tr>`;
  }).join('');
}
function dtPay(rows) {
  if (!rows.length) return '<tr><td colspan="9" style="color:#8b949e;text-align:center;padding:24px">No payments</td></tr>';
  return rows.map(r=>{
    const st=r.is_refunded?'<span class="badge badge-red">Refunded</span>':'<span class="badge badge-green">Paid</span>';
    const amt=r.currency==='INR'?'&#8377;'+r.amount:'$'+r.amount;
    return `<tr>
      <td style="white-space:nowrap">${fmt_ts(r.paid_at)}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.identity}">${r.identity}</td>
      <td>${chB(r.identity_type)}</td><td><code>${r.product_id}</code></td>
      <td style="font-size:11px;color:#8b949e">${r.source}</td>
      <td style="white-space:nowrap">${amt}</td>
      <td><span class="badge badge-blue">${r.plan}</span></td><td>${st}</td>
      <td style="font-size:11px;color:#8b949e;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${r.payment_ref}">${r.payment_ref}</td></tr>`;
  }).join('');
}
function dtCust(rows) {
  if (!rows.length) return '<tr><td colspan="5" style="color:#8b949e;text-align:center;padding:24px">No customers</td></tr>';
  return rows.map(r=>{
    const a=r.active_licenses||0, t=r.total_licenses||0;
    return `<tr>
      <td style="white-space:nowrap">${fmt_ts(r.created_at)}</td>
      <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.identity}">${r.identity}</td>
      <td>${chB(r.identity_type)}</td>
      <td style="text-align:center"><span class="badge ${a>0?'badge-green':'badge-red'}">${a}</span></td>
      <td style="text-align:center;color:#8b949e">${t}</td></tr>`;
  }).join('');
}

// ── Utility ───────────────────────────────────────────────────────────────────
function fmt_ts(ts) {
  if (!ts) return '&#8212;';
  return new Date(ts*1000).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'});
}

// ── Health check on page load (no auth needed) ────────────────────────────────
fetch(base+'/health').then(r=>r.json()).then(h=>{
  const el = document.getElementById('health-badge');
  el.textContent = h.status==='ok' ? '\u25cf Server OK' : '\u26a0 '+h.status;
  el.style.color  = h.status==='ok' ? '#3fb950' : '#d29922';
}).catch(()=>{
  document.getElementById('health-badge').textContent = '\u2716 Server unreachable';
});
</script>
</body>
</html>"""


admin_app = FastAPI()

@admin_app.get("/ui", response_class=HTMLResponse)
async def admin_ui(request: Request):
    """
    Serves the admin dashboard HTML.
    No secrets are injected into the page — zero.
    The browser obtains the AES key by:
      1. Calling GET /admin/wrapped-secret (returns PBKDF2-wrapped key)
      2. Deriving the unwrap key from the admin password via WebCrypto PBKDF2
      3. Decrypting the wrapper to get SHARED_SECRET as a CryptoKey in memory
    """
    return HTMLResponse(DASHBOARD_HTML)