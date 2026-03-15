"""
admin_ui.py  —  Single-file HTML admin dashboard
Mount into main.py with:  app.mount("/admin", admin_app)
Access: GET /admin/ui  (password protected via HTTP Basic)
"""
import os, time, json, base64, secrets, hashlib, hmac
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change_this")
SHARED_SECRET = bytes.fromhex(os.environ.get("SHARED_SECRET",
    "8cfaf7568ebd0d6f5557552efa46e43dfa57bb9618635753c224d3f38b3ac158"))

def _enc(data: dict) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    ct    = AESGCM(SHARED_SECRET).encrypt(nonce, json.dumps(data).encode(), None)
    return base64.b64encode(nonce + ct).decode()

DASHBOARD_HTML = r"""<!DOCTYPE html>
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
  .token-bar{background:#1c2128;border-bottom:1px solid #30363d;padding:10px 28px;
             display:flex;gap:10px;align-items:center}
  .token-bar input{background:#0d1117;border:1px solid #30363d;color:#e6edf3;
                   padding:6px 12px;border-radius:6px;font-size:13px;width:320px}
  .token-bar button{background:#238636;color:#fff;border:none;padding:6px 16px;
                    border-radius:6px;font-size:13px;cursor:pointer}
  .token-bar button:hover{background:#2ea043}
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
  #loading{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(13,17,23,.7);
           display:flex;align-items:center;justify-content:center;font-size:14px;
           color:#58a6ff;z-index:999}
</style>
</head>
<body>
<div id="loading">Loading…</div>
<div class="topbar">
  <h1>⚙ License Server Admin</h1>
  <span id="health-badge">checking…</span>
</div>
<div class="token-bar">
  <input type="password" id="token" placeholder="Admin token" />
  <button onclick="load()">Connect</button>
  <span id="conn-status" style="font-size:13px;color:#8b949e"></span>
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
    <table id="prod-table"><thead><tr>
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

  <!-- Sub-tab bar -->
  <div style="display:flex;gap:0;border-bottom:1px solid #30363d;margin-bottom:20px">
    <div class="tab active" id="dt-tab-lic"   onclick="dtSwitch('lic')"  style="padding:8px 16px">Licenses</div>
    <div class="tab"        id="dt-tab-pay"   onclick="dtSwitch('pay')"  style="padding:8px 16px">Payments</div>
    <div class="tab"        id="dt-tab-cust"  onclick="dtSwitch('cust')" style="padding:8px 16px">Customers</div>
  </div>

  <!-- Licenses sub-view -->
  <div id="dt-lic">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-lic-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('lic',-1)" id="dt-lic-prev" disabled>◀ Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('lic', 1)" id="dt-lic-next" disabled>Next ▶</button>
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

  <!-- Payments sub-view -->
  <div id="dt-pay" style="display:none">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-pay-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('pay',-1)" id="dt-pay-prev" disabled>◀ Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('pay', 1)" id="dt-pay-next" disabled>Next ▶</button>
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

  <!-- Customers sub-view -->
  <div id="dt-cust" style="display:none">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-cust-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('cust',-1)" id="dt-cust-prev" disabled>◀ Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('cust', 1)" id="dt-cust-next" disabled>Next ▶</button>
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
const base = window.location.origin;
let tok = '';

function showTab(t) {
  document.querySelectorAll('.tab').forEach((e,i)=>e.classList.toggle('active',e.textContent.toLowerCase().includes(t)));
  document.querySelectorAll('.page').forEach(e=>e.classList.toggle('active',e.id==='page-'+t));
}

async function apiCall(endpoint, body) {
  const r = await fetch(base+endpoint, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({data: await encrypt({...body, admin_token: tok,
                                                timestamp: Math.floor(Date.now()/1000)})})
  });
  const j = await r.json();
  return await decrypt(j.data);
}

// Client-side AES-GCM using SubtleCrypto
let _key = null;
async function getKey() {
  if (_key) return _key;
  // Primary: cookie set by server (works on HTTPS with secure=True, samesite=lax)
  let hexKey = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('sk='))?.split('=')[1] || '';
  // Fallback: URL hash e.g. /admin/ui#sk=<64hexchars> — hash is never sent to server
  if (!hexKey) {
    const m = window.location.hash.match(/sk=([0-9a-fA-F]{64})/);
    if (m) hexKey = m[1];
  }
  if (!hexKey) throw new Error('Encryption key not found. Refresh the page or use the #sk= URL method.');
  const raw = new Uint8Array(hexKey.match(/.{2}/g).map(b=>parseInt(b,16)));
  _key = await crypto.subtle.importKey('raw',raw,{name:'AES-GCM'},false,['encrypt','decrypt']);
  return _key;
}
async function encrypt(obj) {
  const key   = await getKey();
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const pt    = new TextEncoder().encode(JSON.stringify(obj));
  const ct    = new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},key,pt));
  const out   = new Uint8Array(12+ct.length);
  out.set(nonce); out.set(ct,12);
  return btoa(String.fromCharCode(...out));
}
async function decrypt(b64) {
  const key = await getKey();
  const raw = Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
  const pt  = await crypto.subtle.decrypt({name:'AES-GCM',iv:raw.slice(0,12)},key,raw.slice(12));
  return JSON.parse(new TextDecoder().decode(pt));
}

function fmt_ts(ts) {
  if (!ts) return '—';
  return new Date(ts*1000).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'});
}

async function load() {
  tok = document.getElementById('token').value;
  _key = null;  // clear cached key so fresh cookie is always re-read
  document.getElementById('conn-status').textContent = 'Connecting…';
  try {
    const s = await apiCall('/admin/stats', {});
    if (!s.ok) { document.getElementById('conn-status').textContent = '✗ Wrong token'; return; }
    document.getElementById('conn-status').textContent = '✓ Connected';
    renderStats(s);
    loadProducts();
    loadCoupons();
  } catch(e) {
    document.getElementById('conn-status').textContent = '✗ Error: '+e.message;
  }
}

function renderStats(s) {
  const g = document.getElementById('stats-grid');
  const items = [
    ['Total Customers', s.total_customers ?? 0, 'blue'],
    ['Active Licenses', s.active_licenses ?? 0, 'green'],
    ['Trial Licenses', s.trial_licenses ?? 0, 'amber'],
    ['Revenue INR', '₹'+(s.revenue_inr??0).toFixed(2), 'green'],
    ['Revenue USD', '$'+(s.revenue_usd??0).toFixed(2), 'blue'],
    ['Total Payments', s.total_payments ?? 0, ''],
    ['Refunds', s.refunds ?? 0, s.refunds?'red':''],
    ['Coupons Used', s.coupons_redeemed ?? 0, 'amber'],
    ['OTPs (24h)', s.otps_sent_last_24h ?? 0, ''],
  ];
  g.innerHTML = items.map(([l,v,c])=>`
    <div class="stat-card">
      <div class="val" style="${c==='green'?'color:#3fb950':c==='red'?'color:#f85149':c==='amber'?'color:#d29922':''}">${v}</div>
      <div class="lbl">${l}</div>
    </div>`).join('');
}

async function loadProducts() {
  const r = await apiCall('/admin/products', {include_inactive: true});
  if (!r.ok) return;
  document.getElementById('prod-body').innerHTML = r.products.map(p=>`
    <tr>
      <td><code>${p.product_id}</code></td>
      <td>${p.name}</td>
      <td>₹${p.price_inr||0}</td>
      <td>$${p.price_usd||0}</td>
      <td>${p.max_machines}</td>
      <td>${p.trial_days||0}d</td>
      <td><span class="badge ${p.is_active?'badge-green':'badge-red'}">${p.is_active?'Active':'Inactive'}</span></td>
      <td>
        <button class="btn btn-secondary" style="font-size:11px;padding:4px 10px"
          onclick="fillProduct(${JSON.stringify(p).replace(/"/g,'&quot;')})">Edit</button>
        <button class="btn btn-danger" style="font-size:11px;padding:4px 10px;margin-left:4px"
          onclick="delProduct('${p.product_id}')">Del</button>
      </td>
    </tr>`).join('');
}

function fillProduct(p) {
  document.getElementById('p-id').value        = p.product_id;
  document.getElementById('p-name').value      = p.name;
  document.getElementById('p-inr').value       = p.price_inr||0;
  document.getElementById('p-usd').value       = p.price_usd||0;
  document.getElementById('p-max').value       = p.max_machines||1;
  document.getElementById('p-trial').value     = p.trial_days||0;
  document.getElementById('p-rzp').value       = p.razorpay_link||'';
  document.getElementById('p-gum-id').value    = p.gumroad_product_id||'';
  document.getElementById('p-gum-link').value  = p.gumroad_link||'';
}

async function saveProduct() {
  const r = await apiCall('/admin/product', {
    product_id: document.getElementById('p-id').value.trim().toUpperCase(),
    name: document.getElementById('p-name').value.trim(),
    price_inr: +document.getElementById('p-inr').value,
    price_usd: +document.getElementById('p-usd').value,
    max_machines: +document.getElementById('p-max').value,
    trial_days: +document.getElementById('p-trial').value,
    razorpay_link: document.getElementById('p-rzp').value.trim()||null,
    gumroad_product_id: document.getElementById('p-gum-id').value.trim()||null,
    gumroad_link: document.getElementById('p-gum-link').value.trim()||null,
  });
  const m = document.getElementById('prod-msg');
  m.className='msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent = r.ok ? (r.created?'✓ Product created':'✓ Product updated') : '✗ '+r.reason;
  if (r.ok) loadProducts();
}

async function delProduct(id) {
  if (!confirm('Soft-delete '+id+'? Licenses are preserved.')) return;
  const r = await apiCall('/admin/product/delete', {product_id: id});
  if (r.ok) loadProducts();
}

async function loadCoupons() {
  const r = await apiCall('/admin/coupons', {});
  if (!r.ok) return;
  document.getElementById('coup-body').innerHTML = r.coupons.map(c=>`
    <tr>
      <td><code>${c.code}</code></td>
      <td>${c.product_id||'All'}</td>
      <td>${c.discount_pct?c.discount_pct+'%':''} ${c.discount_flat_inr?'₹'+c.discount_flat_inr:''} ${c.discount_flat_usd?'$'+c.discount_flat_usd:''}</td>
      <td>${c.plan_override||'—'}</td>
      <td>${c.uses}</td>
      <td>${c.max_uses}</td>
      <td>${fmt_ts(c.valid_until)}</td>
      <td><span class="badge ${c.is_active&&c.uses<c.max_uses?'badge-green':'badge-red'}">${c.is_active&&c.uses<c.max_uses?'Active':'Done'}</span></td>
    </tr>`).join('');
}

async function saveCoupon() {
  const r = await apiCall('/admin/coupon', {
    code: document.getElementById('c-code').value.trim(),
    product_id: document.getElementById('c-prod').value.trim()||null,
    discount_pct: +document.getElementById('c-pct').value,
    discount_flat_inr: +document.getElementById('c-inr').value,
    discount_flat_usd: +document.getElementById('c-usd').value,
    plan_override: document.getElementById('c-plan').value||null,
    max_uses: +document.getElementById('c-uses').value,
    valid_until: +document.getElementById('c-until').value||null,
  });
  const m = document.getElementById('coup-msg');
  m.className='msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent = r.ok ? '✓ Coupon '+document.getElementById('c-code').value.trim().toUpperCase()+' created' : '✗ '+r.reason;
  if (r.ok) loadCoupons();
}

async function revokeAction() {
  const email = document.getElementById('revoke-email').value.trim();
  if (!email) return;
  const r = await apiCall('/admin/revoke', {
    identity: email,
    identity_type: document.getElementById('revoke-itype').value,
    product_id: document.getElementById('revoke-prod').value.trim()||null,
    reason: document.getElementById('revoke-reason').value,
  });
  const m = document.getElementById('revoke-msg');
  m.className='msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent = r.ok ? `✓ Revoked ${r.revoked} license(s)` : '✗ '+r.reason;
}

async function lookupCustomer() {
  const identity = document.getElementById('cust-search').value.trim();
  const itype    = document.getElementById('cust-type').value;
  if (!identity) return;
  const r = await apiCall('/admin/customer', {identity, identity_type: itype});
  const el = document.getElementById('cust-result');
  if (!r.ok) { el.innerHTML=`<div class="msg msg-err">✗ ${r.reason}</div>`; return; }
  const c = r.customer;
  el.innerHTML = `
    <div class="card">
      <h2>${c.identity} — member since ${fmt_ts(c.member_since)}</h2>
      <p style="font-size:13px;color:#8b949e;margin:8px 0">${r.active_licenses} active / ${r.total_licenses} total licenses</p>
      <table><thead><tr><th>Product</th><th>Plan</th><th>Activated</th><th>Expires</th><th>Days Left</th><th>Status</th></tr></thead>
      <tbody>${r.licenses.map(l=>`<tr>
        <td>${l.product_name||l.product_id}</td>
        <td><span class="badge badge-blue">${l.plan}</span></td>
        <td>${fmt_ts(l.activated_at)}</td>
        <td>${fmt_ts(l.expires_at)}</td>
        <td>${l.days_left!=null?l.days_left+'d':'∞'}</td>
        <td><span class="badge ${l.is_active&&!l.is_expired?'badge-green':'badge-red'}">${l.is_active&&!l.is_expired?'Active':l.is_expired?'Expired':'Revoked'}</span></td>
      </tr>`).join('')}</tbody></table>
    </div>`;
}


// ── Data browser ──────────────────────────────────────────────────────────────
const DT_PAGE = 500;
const dtState = {
  lic:  {offset:0, total:0, loaded:false},
  pay:  {offset:0, total:0, loaded:false},
  cust: {offset:0, total:0, loaded:false},
};
const dtEndpoint = {lic:'licenses', pay:'payments', cust:'customers'};

function dtSwitch(view) {
  ['lic','pay','cust'].forEach(v => {
    document.getElementById('dt-'+v).style.display = v===view ? '' : 'none';
    document.getElementById('dt-tab-'+v).classList.toggle('active', v===view);
  });
  // Auto-load first time tab is visited
  if (!dtState[view].loaded) dtLoad(view, 0);
}

async function dtLoad(view, dir) {
  const st = dtState[view];
  // dir: 0=reset/reload, 1=next, -1=prev
  if (dir === 0) { st.offset = 0; }
  else if (dir === 1) { st.offset = Math.min(st.offset + DT_PAGE, Math.max(0, st.total - DT_PAGE)); }
  else if (dir === -1) { st.offset = Math.max(0, st.offset - DT_PAGE); }

  const infoEl  = document.getElementById('dt-'+view+'-info');
  const prevBtn = document.getElementById('dt-'+view+'-prev');
  const nextBtn = document.getElementById('dt-'+view+'-next');
  const tbody   = document.getElementById('dt-'+view+'-body');

  infoEl.textContent = 'Loading…';
  prevBtn.disabled = true;
  nextBtn.disabled = true;

  try {
    const r = await apiCall('/admin/browse/'+dtEndpoint[view], {
      offset: st.offset, limit: DT_PAGE
    });
    if (!r.ok) {
      infoEl.textContent = '✗ ' + (r.reason || 'Error');
      return;
    }
    st.total  = r.total;
    st.loaded = true;
    const from = st.offset + 1;
    const to   = Math.min(st.offset + r.rows.length, r.total);
    infoEl.textContent = r.total === 0
      ? 'No records found'
      : `Showing ${from}–${to} of ${r.total} (latest first)`;
    prevBtn.disabled = st.offset <= 0;
    nextBtn.disabled = !r.has_more;

    if (view === 'lic')  tbody.innerHTML = dtRenderLic(r.rows);
    if (view === 'pay')  tbody.innerHTML = dtRenderPay(r.rows);
    if (view === 'cust') tbody.innerHTML = dtRenderCust(r.rows);
  } catch(e) {
    infoEl.textContent = '✗ ' + e.message;
  }
}

function chBadge(t) {
  return t==='email' ? '<span style="color:#58a6ff;font-size:11px">✉</span>'
                     : '<span style="color:#3fb950;font-size:11px">📱</span>';
}

function dtRenderLic(rows) {
  if (!rows.length) return '<tr><td colspan="11" style="color:#8b949e;text-align:center;padding:24px">No licenses</td></tr>';
  return rows.map(r => {
    const status = !r.is_active ? '<span class="badge badge-red">Revoked</span>'
                 : r.expires_at && Date.now()/1000 > r.expires_at
                   ? '<span class="badge badge-amber">Expired</span>'
                   : '<span class="badge badge-green">Active</span>';
    const amt = r.currency==='INR' ? '₹'+(r.amount||0) : '$'+(r.amount||0);
    const machine = r.machine_label || (r.machine_id ? r.machine_id.slice(0,12)+'…' : '—');
    return `<tr>
      <td style="white-space:nowrap">${fmt_ts(r.activated_at)}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.identity}">${r.identity}</td>
      <td>${chBadge(r.identity_type)}</td>
      <td><code>${r.product_id}</code></td>
      <td><span class="badge badge-blue">${r.plan}</span></td>
      <td>${status}</td>
      <td style="font-size:11px;color:#8b949e">${r.source||'—'}</td>
      <td style="white-space:nowrap">${amt} ${r.currency||''}</td>
      <td style="text-align:center">${r.verify_count||0}</td>
      <td style="white-space:nowrap;color:#8b949e;font-size:12px">${fmt_ts(r.last_seen_at)}</td>
      <td style="font-size:11px;color:#8b949e;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.machine_label||r.machine_id||''}">${machine}</td>
    </tr>`;
  }).join('');
}

function dtRenderPay(rows) {
  if (!rows.length) return '<tr><td colspan="9" style="color:#8b949e;text-align:center;padding:24px">No payments</td></tr>';
  return rows.map(r => {
    const status = r.is_refunded
      ? '<span class="badge badge-red">Refunded</span>'
      : '<span class="badge badge-green">Paid</span>';
    const amt = r.currency==='INR' ? '₹'+r.amount : '$'+r.amount;
    return `<tr>
      <td style="white-space:nowrap">${fmt_ts(r.paid_at)}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.identity}">${r.identity}</td>
      <td>${chBadge(r.identity_type)}</td>
      <td><code>${r.product_id}</code></td>
      <td style="font-size:11px;color:#8b949e">${r.source}</td>
      <td style="white-space:nowrap">${amt}</td>
      <td><span class="badge badge-blue">${r.plan}</span></td>
      <td>${status}</td>
      <td style="font-size:11px;color:#8b949e;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.payment_ref}">${r.payment_ref}</td>
    </tr>`;
  }).join('');
}

function dtRenderCust(rows) {
  if (!rows.length) return '<tr><td colspan="5" style="color:#8b949e;text-align:center;padding:24px">No customers</td></tr>';
  return rows.map(r => {
    const active = r.active_licenses || 0;
    const total  = r.total_licenses  || 0;
    return `<tr>
      <td style="white-space:nowrap">${fmt_ts(r.created_at)}</td>
      <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.identity}">${r.identity}</td>
      <td>${chBadge(r.identity_type)}</td>
      <td style="text-align:center">
        <span class="badge ${active>0?'badge-green':'badge-red'}">${active}</span>
      </td>
      <td style="text-align:center;color:#8b949e">${total}</td>
    </tr>`;
  }).join('');
}

// Health check (unencrypted)
fetch(base+'/health').then(r=>r.json()).then(h=>{
  const el = document.getElementById('health-badge');
  el.textContent = h.status==='ok' ? '● Server OK' : '⚠ '+h.status;
  el.style.color = h.status==='ok' ? '#3fb950' : '#d29922';
  document.getElementById('loading').style.display='none';
}).catch(()=>{
  document.getElementById('health-badge').textContent='✗ Server unreachable';
  document.getElementById('loading').style.display='none';
});
</script>
</body>
</html>"""


admin_app = FastAPI()

@admin_app.get("/ui", response_class=HTMLResponse)
async def admin_ui(request: Request):
    # Set the shared secret as a short-lived cookie for JS SubtleCrypto
    # Cookie is httpOnly=False intentionally (JS needs it for AES)
    # This is safe because the admin dashboard is only exposed over HTTPS
    response = HTMLResponse(DASHBOARD_HTML)
    sk_hex   = os.environ.get("SHARED_SECRET",
        "8cfaf7568ebd0d6f5557552efa46e43dfa57bb9618635753c224d3f38b3ac158")
    response.set_cookie("sk", sk_hex, max_age=3600, samesite="lax", secure=True,
                        httponly=False)
    return response