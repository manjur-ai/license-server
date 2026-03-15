"""
admin_ui.py  —  Single-file HTML admin dashboard
Mount into main.py with:  app.mount("/admin", admin_app)
Access: GET /admin/ui
"""
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

# NOTE: login-step1 has NO display:none — it is visible by default.
# login-step2 has display:none — hidden until OTP is sent.
# Both steps use white background with black text — no dark-on-dark issues.

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>License Server Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
.topbar{background:#161b22;border-bottom:1px solid #30363d;padding:14px 28px;display:flex;align-items:center;gap:16px}
.topbar h1{font-size:16px;font-weight:600;color:#58a6ff}
.topbar span{font-size:12px;color:#8b949e}
.tabs{display:flex;background:#161b22;border-bottom:1px solid #30363d;padding:0 28px}
.tab{padding:10px 18px;font-size:13px;cursor:pointer;border-bottom:2px solid transparent;color:#8b949e;transition:.15s}
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
th{text-align:left;padding:8px 12px;color:#8b949e;font-weight:500;border-bottom:1px solid #30363d;font-size:12px}
td{padding:8px 12px;border-bottom:1px solid #21262d;vertical-align:middle}
tr:hover td{background:#1c2128}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge-green{background:#033a16;color:#3fb950}
.badge-red{background:#3d1212;color:#f85149}
.badge-amber{background:#2d1f00;color:#d29922}
.badge-blue{background:#0c2d6b;color:#58a6ff}
.form-row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px}
.form-row label{font-size:12px;color:#8b949e;display:block;margin-bottom:4px}
.form-row input,.form-row select{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 11px;border-radius:6px;font-size:13px;min-width:140px}
.btn{padding:7px 16px;border-radius:6px;border:none;font-size:13px;cursor:pointer;font-weight:500}
.btn-primary{background:#238636;color:#fff}.btn-primary:hover{background:#2ea043}
.btn-danger{background:#b91c1c;color:#fff}.btn-danger:hover{background:#dc2626}
.btn-secondary{background:#21262d;color:#e6edf3;border:1px solid #30363d}
.btn-secondary:hover{background:#30363d}
.msg{padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:14px}
.msg-ok{background:#033a16;color:#3fb950;border:1px solid #1a5c2a}
.msg-err{background:#3d1212;color:#f85149;border:1px solid #8b1a1a}
.session-bar{font-size:12px;color:#8b949e;margin-left:auto;cursor:pointer}
.session-bar:hover{color:#e6edf3}
#overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:#0d1117;z-index:1000;display:flex;align-items:center;justify-content:center;overflow-y:auto}
#loading{position:fixed;inset:0;background:rgba(13,17,23,.75);display:none;align-items:center;justify-content:center;font-size:14px;color:#58a6ff;z-index:500}
</style>
</head>
<body>

<div id="overlay">
  <div style="background:#fff;border-radius:12px;padding:40px;width:380px;box-shadow:0 8px 40px rgba(0,0,0,.6)">
    <h2 style="color:#1d4ed8;font-size:22px;margin:0 0 6px;font-family:Arial">&#9881; Admin Login</h2>
    <p style="color:#6b7280;font-size:14px;margin:0 0 24px;font-family:Arial">License Server Admin Dashboard</p>
    <div id="err" style="color:#dc2626;font-size:13px;min-height:20px;margin-bottom:10px;font-family:Arial;font-weight:600"></div>

    <div id="step1">
      <input id="l-pass" type="password" placeholder="Admin token / password"
             onkeydown="if(event.key==='Enter')go1()"
             style="display:block;width:100%;padding:12px;border:2px solid #cbd5e1;border-radius:8px;
                    font-size:15px;color:#111;background:#f8fafc;box-sizing:border-box;
                    margin-bottom:14px;font-family:Arial;outline:none">
      <button id="btn1" onclick="go1()"
              style="display:block;width:100%;padding:13px;border:none;border-radius:8px;
                     background:#16a34a;color:#fff;font-size:15px;font-weight:700;
                     cursor:pointer;font-family:Arial;box-sizing:border-box">
        Continue &#8594;
      </button>
    </div>

    <div id="step2" style="display:none">
      <p id="hint" style="color:#374151;font-size:13px;margin:0 0 14px;font-family:Arial"></p>
      <input id="l-otp" type="text" placeholder="6-digit code" maxlength="6"
             oninput="this.value=this.value.replace(/\\D/g,'')"
             onkeydown="if(event.key==='Enter')go2()"
             style="display:block;width:100%;padding:12px;border:2px solid #cbd5e1;border-radius:8px;
                    font-size:26px;letter-spacing:14px;text-align:center;color:#111;
                    background:#f8fafc;box-sizing:border-box;margin-bottom:14px;
                    font-family:Arial;outline:none">
      <button id="btn2" onclick="go2()"
              style="display:block;width:100%;padding:13px;border:none;border-radius:8px;
                     background:#16a34a;color:#fff;font-size:15px;font-weight:700;
                     cursor:pointer;font-family:Arial;box-sizing:border-box">
        Verify &amp; Connect
      </button>
      <p style="margin-top:14px;font-size:13px;color:#6b7280;text-align:center;font-family:Arial">
        <span style="cursor:pointer;color:#1d4ed8" onclick="reset()">&#8592; Back</span>
        &nbsp;&nbsp;&#183;&nbsp;&nbsp;
        <span style="cursor:pointer;color:#1d4ed8" onclick="go1()">Resend OTP</span>
      </p>
    </div>
  </div>
</div>

<div id="loading">Loading&#8230;</div>

<div class="topbar">
  <h1>&#9881; License Server Admin</h1>
  <span id="hbadge">checking&#8230;</span>
  <span class="session-bar" id="sbar" onclick="logout()" title="Click to log out"></span>
</div>
<div class="tabs">
  <div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
  <div class="tab" onclick="showTab('products')">Products</div>
  <div class="tab" onclick="showTab('coupons')">Coupons</div>
  <div class="tab" onclick="showTab('customers')">Customers</div>
  <div class="tab" onclick="showTab('data')">&#128202; Data</div>
</div>

<div class="page active" id="page-dashboard">
  <div class="stat-grid" id="stats-grid"></div>
  <div class="card"><h2>Quick Actions</h2>
    <div class="form-row">
      <div><label>Email or phone</label><input type="text" id="revoke-email" placeholder="user@example.com or +91..." style="width:280px"></div>
      <div><label>Identity type</label><select id="revoke-itype"><option value="email">email</option><option value="sms">sms (phone)</option></select></div>
      <div><label>Product ID (blank=all)</label><input type="text" id="revoke-prod" placeholder="optional" style="width:140px"></div>
      <div><label>Reason</label><select id="revoke-reason"><option value="manual">Manual</option><option value="abuse">Abuse</option><option value="refund">Refund</option></select></div>
      <button class="btn btn-danger" onclick="revokeAction()">Revoke</button>
    </div>
    <div id="revoke-msg"></div>
  </div>
</div>

<div class="page" id="page-products">
  <div class="card"><h2>Products</h2>
    <div id="prod-msg"></div>
    <div class="form-row">
      <div><label>Product ID*</label><input id="p-id" placeholder="TOOL1"></div>
      <div><label>Name*</label><input id="p-name" placeholder="My Tool Pro" style="width:200px"></div>
      <div><label>Price INR</label><input id="p-inr" type="number" placeholder="499" style="width:100px"></div>
      <div><label>Price USD</label><input id="p-usd" type="number" placeholder="9.99" style="width:100px"></div>
      <div><label>Max Machines</label><input id="p-max" type="number" value="1" style="width:80px"></div>
      <div><label>Trial Days</label><input id="p-trial" type="number" value="0" style="width:80px"></div>
    </div>
    <div class="form-row">
      <div><label>Razorpay Link</label><input id="p-rzp" placeholder="https://rzp.io/l/..." style="width:260px"></div>
      <div><label>Gumroad Product ID</label><input id="p-gum-id" placeholder="abc123" style="width:140px"></div>
      <div><label>Gumroad Link</label><input id="p-gum-link" placeholder="https://..." style="width:220px"></div>
      <button class="btn btn-primary" onclick="saveProduct()">Save Product</button>
    </div>
    <table><thead><tr><th>ID</th><th>Name</th><th>INR</th><th>USD</th><th>Machines</th><th>Trial</th><th>Status</th><th>Actions</th></tr></thead><tbody id="prod-body"></tbody></table>
  </div>
</div>

<div class="page" id="page-coupons">
  <div class="card"><h2>Coupons</h2>
    <div id="coup-msg"></div>
    <div class="form-row">
      <div><label>Code*</label><input id="c-code" placeholder="LAUNCH20" style="text-transform:uppercase"></div>
      <div><label>Product ID (blank=all)</label><input id="c-prod" placeholder="optional"></div>
      <div><label>Discount %</label><input id="c-pct" type="number" value="0" style="width:80px"></div>
      <div><label>Flat INR off</label><input id="c-inr" type="number" value="0" style="width:90px"></div>
      <div><label>Flat USD off</label><input id="c-usd" type="number" value="0" style="width:90px"></div>
      <div><label>Plan Override</label><select id="c-plan"><option value="">None</option><option value="trial">trial</option><option value="monthly">monthly</option><option value="annual">annual</option><option value="lifetime">lifetime</option></select></div>
      <div><label>Max Uses</label><input id="c-uses" type="number" value="1" style="width:80px"></div>
      <div><label>Expires (unix ts)</label><input id="c-until" type="number" placeholder="optional" style="width:130px"></div>
      <button class="btn btn-primary" onclick="saveCoupon()">Create Coupon</button>
    </div>
    <table><thead><tr><th>Code</th><th>Product</th><th>Discount</th><th>Plan</th><th>Uses</th><th>Max</th><th>Expires</th><th>Status</th></tr></thead><tbody id="coup-body"></tbody></table>
  </div>
</div>

<div class="page" id="page-customers">
  <div class="card"><h2>Customer Lookup</h2>
    <div class="form-row">
      <div><label>Email or Phone</label><input id="cust-search" placeholder="user@example.com" style="width:300px"></div>
      <select id="cust-type"><option value="email">email</option><option value="sms">sms</option></select>
      <button class="btn btn-secondary" onclick="lookupCustomer()">Look up</button>
    </div>
    <div id="cust-result" style="margin-top:16px"></div>
  </div>
</div>

<div class="page" id="page-data">
  <div style="display:flex;border-bottom:1px solid #30363d;margin-bottom:20px">
    <div class="tab active" id="dt-tab-lic" onclick="dtSwitch('lic')" style="padding:8px 16px">Licenses</div>
    <div class="tab" id="dt-tab-pay" onclick="dtSwitch('pay')" style="padding:8px 16px">Payments</div>
    <div class="tab" id="dt-tab-cust" onclick="dtSwitch('cust')" style="padding:8px 16px">Customers</div>
  </div>
  <div id="dt-lic">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-lic-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('lic',-1)" id="dt-lic-prev" disabled>&#9664; Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('lic',1)" id="dt-lic-next" disabled>Next &#9654;</button>
      <button class="btn btn-primary" onclick="dtLoad('lic',0)">Load / Refresh</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto">
      <table><thead><tr><th>Activated</th><th>Identity</th><th>Ch</th><th>Product</th><th>Plan</th><th>Status</th><th>Source</th><th>Amount</th><th>Verifies</th><th>Last seen</th><th>Machine</th></tr></thead>
      <tbody id="dt-lic-body"><tr><td colspan="11" style="color:#8b949e;text-align:center;padding:32px">Click Load to fetch licenses</td></tr></tbody></table>
    </div>
  </div>
  <div id="dt-pay" style="display:none">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-pay-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('pay',-1)" id="dt-pay-prev" disabled>&#9664; Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('pay',1)" id="dt-pay-next" disabled>Next &#9654;</button>
      <button class="btn btn-primary" onclick="dtLoad('pay',0)">Load / Refresh</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto">
      <table><thead><tr><th>Paid at</th><th>Identity</th><th>Ch</th><th>Product</th><th>Source</th><th>Amount</th><th>Plan</th><th>Status</th><th>Payment ref</th></tr></thead>
      <tbody id="dt-pay-body"><tr><td colspan="9" style="color:#8b949e;text-align:center;padding:32px">Click Load to fetch payments</td></tr></tbody></table>
    </div>
  </div>
  <div id="dt-cust" style="display:none">
    <div class="card" style="padding:14px 20px;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span id="dt-cust-info" style="font-size:13px;color:#8b949e;flex:1">Click Load to fetch data</span>
      <button class="btn btn-secondary" onclick="dtLoad('cust',-1)" id="dt-cust-prev" disabled>&#9664; Prev</button>
      <button class="btn btn-secondary" onclick="dtLoad('cust',1)" id="dt-cust-next" disabled>Next &#9654;</button>
      <button class="btn btn-primary" onclick="dtLoad('cust',0)">Load / Refresh</button>
    </div>
    <div class="card" style="padding:0;overflow-x:auto">
      <table><thead><tr><th>Joined</th><th>Identity</th><th>Ch</th><th>Active licenses</th><th>Total licenses</th></tr></thead>
      <tbody id="dt-cust-body"><tr><td colspan="5" style="color:#8b949e;text-align:center;padding:32px">Click Load to fetch customers</td></tr></tbody></table>
    </div>
  </div>
</div>

<script>
const B = window.location.origin;
let _sk=null, _tok='', _pass='', _exp=0;

// ── AES-GCM ──────────────────────────────────────────────────────────────────
async function enc(obj){
  const n=crypto.getRandomValues(new Uint8Array(12));
  const pt=new TextEncoder().encode(JSON.stringify(obj));
  const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:n},_sk,pt));
  const o=new Uint8Array(12+ct.length); o.set(n); o.set(ct,12);
  let s=''; for(let i=0;i<o.length;i++) s+=String.fromCharCode(o[i]);
  return btoa(s);
}
async function dec(b64){
  const b=atob(b64), r=new Uint8Array(b.length);
  for(let i=0;i<b.length;i++) r[i]=b.charCodeAt(i);
  const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:r.slice(0,12)},_sk,r.slice(12));
  return JSON.parse(new TextDecoder().decode(pt));
}

// ── API call ──────────────────────────────────────────────────────────────────
async function api(ep, body){
  const r=await fetch(B+ep,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({data:await enc({...body,session_token:_tok,timestamp:Math.floor(Date.now()/1000)})})});
  if(!r.ok){const t=await r.text().catch(()=>''); throw new Error('HTTP '+r.status+(t?' — '+t.slice(0,100):'')); }
  const j=await r.json().catch(()=>{throw new Error('Non-JSON response');});
  if(!j||!j.data) throw new Error('Missing data field');
  return dec(j.data);
}

// ── PBKDF2 unwrap ─────────────────────────────────────────────────────────────
async function unwrap(pw,saltH,nonceH,wrappedH,itr){
  const toA=h=>new Uint8Array(h.match(/.{2}/g).map(b=>parseInt(b,16)));
  const bk=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),{name:'PBKDF2'},false,['deriveKey']);
  const wk=await crypto.subtle.deriveKey({name:'PBKDF2',salt:toA(saltH),iterations:itr,hash:'SHA-256'},bk,{name:'AES-GCM',length:256},false,['decrypt']);
  const raw=await crypto.subtle.decrypt({name:'AES-GCM',iv:toA(nonceH)},wk,toA(wrappedH));
  return crypto.subtle.importKey('raw',raw,{name:'AES-GCM'},false,['encrypt','decrypt']);
}

// ── Login helpers ─────────────────────────────────────────────────────────────
function setErr(m){ document.getElementById('err').textContent=m||''; }
function busy(id,b){
  const el=document.getElementById(id);
  el.disabled=b;
  el.style.background=b?'#9ca3af':'#16a34a';
  el.style.cursor=b?'default':'pointer';
  el.textContent=b?(id==='btn1'?'Sending OTP...':'Verifying...'):(id==='btn1'?'Continue \u2192':'Verify & Connect');
}
function reset(){
  _sk=null;_tok='';_pass='';_exp=0;
  setErr('');
  document.getElementById('step1').style.display='block';
  document.getElementById('step2').style.display='none';
  document.getElementById('l-pass').value='';
  document.getElementById('l-otp').value='';
  busy('btn1',false); busy('btn2',false);
}

async function go1(){
  const pw=document.getElementById('l-pass').value.trim();
  if(!pw){setErr('Enter your admin token');return;}
  _pass=pw; setErr(''); busy('btn1',true);
  try{
    const wr=await fetch(B+'/auth/admin/wrapped-secret').then(r=>{
      if(!r.ok) throw new Error('Server error HTTP '+r.status); return r.json();});
    try{ _sk=await unwrap(_pass,wr.salt,wr.nonce,wr.wrapped,wr.iterations); }
    catch(e){ throw new Error('Wrong admin token'); }
    const r=await api('/auth/admin/request-otp',{admin_token:_pass});
    if(!r.ok){_sk=null;_pass='';
      throw new Error(r.reason==='unauthorized'?'Wrong admin token':
        r.reason==='no_admin_email_configured'?'Add ADMIN_EMAILS env var on Railway':
        r.reason==='delivery_failed'?'OTP email failed — check EMAIL_SEND_METHODS':r.reason||'OTP request failed');}
    document.getElementById('hint').textContent='OTP sent to: '+(r.sent_to||[]).join(', ');
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
    setTimeout(()=>document.getElementById('l-otp').focus(),50);
    busy('btn1',false);
  }catch(e){setErr(e.message);busy('btn1',false);_sk=null;_pass='';}
}

async function go2(){
  const otp=document.getElementById('l-otp').value.trim();
  if(otp.length!==6){setErr('Enter the 6-digit code');return;}
  setErr(''); busy('btn2',true);
  try{
    const r=await api('/auth/admin/verify-otp',{admin_token:_pass,otp});
    if(!r.ok) throw new Error(
      r.reason==='invalid_otp'?'Wrong code':
      r.reason==='otp_expired'?'Code expired — click Resend OTP':
      r.reason==='max_attempts_exceeded'?'Too many attempts — click Resend OTP':r.reason||'Verification failed');
    _tok=r.session_token; _pass=''; _exp=Date.now()+r.expires_in*1000;
    document.getElementById('overlay').style.display='none';
    startTimer(); afterLogin();
  }catch(e){setErr(e.message);busy('btn2',false);}
}

// ── Session timer ─────────────────────────────────────────────────────────────
function startTimer(){
  const el=document.getElementById('sbar');
  const t=()=>{
    const s=Math.max(0,Math.round((_exp-Date.now())/1000));
    if(s===0){logout();return;}
    const m=Math.floor(s/60);
    el.textContent='Session: '+m+':'+(s%60).toString().padStart(2,'0')+'  ·  Log out';
    setTimeout(t,1000);
  }; t();
}
function logout(){_sk=null;_tok='';_pass='';_exp=0;document.getElementById('overlay').style.display='flex';reset();}

// ── After login ───────────────────────────────────────────────────────────────
async function afterLogin(){
  document.getElementById('loading').style.display='flex';
  try{
    const s=await api('/admin/stats',{});
    if(s.ok) renderStats(s);
    await Promise.all([loadProducts(),loadCoupons()]);
  }catch(e){console.error(e);}
  finally{document.getElementById('loading').style.display='none';}
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function showTab(t){
  document.querySelectorAll('.tabs>.tab').forEach(e=>{
    const n=e.textContent.trim().toLowerCase().replace(/^\S+ /,'');
    e.classList.toggle('active',n===t||e.textContent.trim().toLowerCase()===t);
  });
  document.querySelectorAll('.page').forEach(e=>e.classList.toggle('active',e.id==='page-'+t));
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function renderStats(s){
  document.getElementById('stats-grid').innerHTML=[
    ['Total Customers',s.total_customers??0,'blue'],
    ['Active Licenses',s.active_licenses??0,'green'],
    ['Trial Licenses',s.trial_licenses??0,'amber'],
    ['Revenue INR','&#8377;'+(s.revenue_inr??0).toFixed(2),'green'],
    ['Revenue USD','$'+(s.revenue_usd??0).toFixed(2),'blue'],
    ['Total Payments',s.total_payments??0,''],
    ['Refunds',s.refunds??0,s.refunds?'red':''],
    ['Coupons Used',s.coupons_redeemed??0,'amber'],
    ['OTPs (24h)',s.otps_sent_last_24h??0,''],
  ].map(([l,v,c])=>`<div class="stat-card"><div class="val" style="${c==='green'?'color:#3fb950':c==='red'?'color:#f85149':c==='amber'?'color:#d29922':''}">${v}</div><div class="lbl">${l}</div></div>`).join('');
}

// ── Products ──────────────────────────────────────────────────────────────────
async function loadProducts(){
  const r=await api('/admin/products',{include_inactive:true}); if(!r.ok) return;
  document.getElementById('prod-body').innerHTML=r.products.map(p=>`<tr>
    <td><code>${p.product_id}</code></td><td>${p.name}</td>
    <td>&#8377;${p.price_inr||0}</td><td>$${p.price_usd||0}</td>
    <td>${p.max_machines}</td><td>${p.trial_days||0}d</td>
    <td><span class="badge ${p.is_active?'badge-green':'badge-red'}">${p.is_active?'Active':'Inactive'}</span></td>
    <td>
      <button class="btn btn-secondary" style="font-size:11px;padding:4px 10px" onclick="fillP(${JSON.stringify(p).replace(/"/g,'&quot;')})">Edit</button>
      <button class="btn btn-danger" style="font-size:11px;padding:4px 10px;margin-left:4px" onclick="delP('${p.product_id}')">Del</button>
    </td></tr>`).join('');
}
function fillP(p){
  const m={'p-id':'product_id','p-name':'name','p-inr':'price_inr','p-usd':'price_usd','p-max':'max_machines','p-trial':'trial_days','p-rzp':'razorpay_link','p-gum-id':'gumroad_product_id','p-gum-link':'gumroad_link'};
  Object.entries(m).forEach(([id,k])=>document.getElementById(id).value=p[k]||(typeof p[k]==='number'?p[k]:''));
  showTab('products');
}
async function saveProduct(){
  const r=await api('/admin/product',{
    product_id:document.getElementById('p-id').value.trim().toUpperCase(),
    name:document.getElementById('p-name').value.trim(),
    price_inr:+document.getElementById('p-inr').value,
    price_usd:+document.getElementById('p-usd').value,
    max_machines:+document.getElementById('p-max').value,
    trial_days:+document.getElementById('p-trial').value,
    razorpay_link:document.getElementById('p-rzp').value.trim()||null,
    gumroad_product_id:document.getElementById('p-gum-id').value.trim()||null,
    gumroad_link:document.getElementById('p-gum-link').value.trim()||null,
  });
  const m=document.getElementById('prod-msg');
  m.className='msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent=r.ok?(r.created?'&#10003; Created':'&#10003; Updated'):'&#10007; '+r.reason;
  if(r.ok) loadProducts();
}
async function delP(id){
  if(!confirm('Soft-delete '+id+'?')) return;
  const r=await api('/admin/product/delete',{product_id:id});
  if(r.ok) loadProducts();
}

// ── Coupons ───────────────────────────────────────────────────────────────────
async function loadCoupons(){
  const r=await api('/admin/coupons',{}); if(!r.ok) return;
  document.getElementById('coup-body').innerHTML=r.coupons.map(c=>`<tr>
    <td><code>${c.code}</code></td><td>${c.product_id||'All'}</td>
    <td>${c.discount_pct?c.discount_pct+'%':''} ${c.discount_flat_inr?'&#8377;'+c.discount_flat_inr:''} ${c.discount_flat_usd?'$'+c.discount_flat_usd:''}</td>
    <td>${c.plan_override||'&#8212;'}</td><td>${c.uses}</td><td>${c.max_uses}</td>
    <td>${fmt(c.valid_until)}</td>
    <td><span class="badge ${c.is_active&&c.uses<c.max_uses?'badge-green':'badge-red'}">${c.is_active&&c.uses<c.max_uses?'Active':'Done'}</span></td>
    </tr>`).join('');
}
async function saveCoupon(){
  const r=await api('/admin/coupon',{
    code:document.getElementById('c-code').value.trim(),
    product_id:document.getElementById('c-prod').value.trim()||null,
    discount_pct:+document.getElementById('c-pct').value,
    discount_flat_inr:+document.getElementById('c-inr').value,
    discount_flat_usd:+document.getElementById('c-usd').value,
    plan_override:document.getElementById('c-plan').value||null,
    max_uses:+document.getElementById('c-uses').value,
    valid_until:+document.getElementById('c-until').value||null,
  });
  const m=document.getElementById('coup-msg');
  m.className='msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent=r.ok?'&#10003; Coupon '+document.getElementById('c-code').value.trim().toUpperCase()+' created':'&#10007; '+r.reason;
  if(r.ok) loadCoupons();
}

// ── Revoke ────────────────────────────────────────────────────────────────────
async function revokeAction(){
  const id=document.getElementById('revoke-email').value.trim(); if(!id) return;
  const r=await api('/admin/revoke',{identity:id,identity_type:document.getElementById('revoke-itype').value,product_id:document.getElementById('revoke-prod').value.trim()||null,reason:document.getElementById('revoke-reason').value});
  const m=document.getElementById('revoke-msg');
  m.className='msg '+(r.ok?'msg-ok':'msg-err');
  m.textContent=r.ok?'&#10003; Revoked '+r.revoked+' license(s)':'&#10007; '+r.reason;
}

// ── Customer lookup ───────────────────────────────────────────────────────────
async function lookupCustomer(){
  const id=document.getElementById('cust-search').value.trim(); if(!id) return;
  const r=await api('/admin/customer',{identity:id,identity_type:document.getElementById('cust-type').value});
  const el=document.getElementById('cust-result');
  if(!r.ok){el.innerHTML='<div class="msg msg-err">&#10007; '+r.reason+'</div>';return;}
  const c=r.customer;
  el.innerHTML=`<div class="card"><h2>${c.identity} &#8212; since ${fmt(c.member_since)}</h2>
    <p style="font-size:13px;color:#8b949e;margin:8px 0">${r.active_licenses} active / ${r.total_licenses} total</p>
    <table><thead><tr><th>Product</th><th>Plan</th><th>Activated</th><th>Expires</th><th>Days Left</th><th>Status</th></tr></thead>
    <tbody>${r.licenses.map(l=>`<tr><td>${l.product_name||l.product_id}</td>
      <td><span class="badge badge-blue">${l.plan}</span></td>
      <td>${fmt(l.activated_at)}</td><td>${fmt(l.expires_at)}</td>
      <td>${l.days_left!=null?l.days_left+'d':'&#8734;'}</td>
      <td><span class="badge ${l.is_active&&!l.is_expired?'badge-green':'badge-red'}">${l.is_active&&!l.is_expired?'Active':l.is_expired?'Expired':'Revoked'}</span></td>
    </tr>`).join('')}</tbody></table></div>`;
}

// ── Data browser ──────────────────────────────────────────────────────────────
const PG=500;
const DST={lic:{o:0,t:0,l:false},pay:{o:0,t:0,l:false},cust:{o:0,t:0,l:false}};
const DEP={lic:'licenses',pay:'payments',cust:'customers'};
function dtSwitch(v){
  ['lic','pay','cust'].forEach(x=>{
    document.getElementById('dt-'+x).style.display=x===v?'':'none';
    document.getElementById('dt-tab-'+x).classList.toggle('active',x===v);
  });
  if(!DST[v].l) dtLoad(v,0);
}
async function dtLoad(v,d){
  const s=DST[v];
  if(d===0) s.o=0;
  else if(d===1) s.o=Math.min(s.o+PG,Math.max(0,s.t-PG));
  else if(d===-1) s.o=Math.max(0,s.o-PG);
  const info=document.getElementById('dt-'+v+'-info');
  const prev=document.getElementById('dt-'+v+'-prev');
  const next=document.getElementById('dt-'+v+'-next');
  const body=document.getElementById('dt-'+v+'-body');
  info.textContent='Loading...'; prev.disabled=true; next.disabled=true;
  try{
    const r=await api('/admin/browse/'+DEP[v],{offset:s.o,limit:PG});
    if(!r.ok){info.textContent='Error: '+(r.reason||'unknown');return;}
    s.t=r.total; s.l=true;
    const fr=s.o+1, to=Math.min(s.o+r.rows.length,r.total);
    info.textContent=r.total===0?'No records':'Showing '+fr+'&#8211;'+to+' of '+r.total+' (newest first)';
    prev.disabled=s.o<=0; next.disabled=!r.has_more;
    if(v==='lic') body.innerHTML=rLic(r.rows);
    if(v==='pay') body.innerHTML=rPay(r.rows);
    if(v==='cust') body.innerHTML=rCust(r.rows);
  }catch(e){info.textContent='Error: '+e.message;}
}
function chB(t){return t==='email'?'<span style="color:#58a6ff;font-size:11px">&#9993;</span>':'<span style="color:#3fb950;font-size:11px">&#128241;</span>';}
function rLic(rows){
  if(!rows.length) return '<tr><td colspan="11" style="color:#8b949e;text-align:center;padding:24px">No licenses</td></tr>';
  return rows.map(r=>{
    const st=!r.is_active?'<span class="badge badge-red">Revoked</span>':r.expires_at&&Date.now()/1000>r.expires_at?'<span class="badge badge-amber">Expired</span>':'<span class="badge badge-green">Active</span>';
    const a=r.currency==='INR'?'&#8377;'+(r.amount||0):'$'+(r.amount||0);
    const mc=r.machine_label||(r.machine_id?r.machine_id.slice(0,12)+'...':'&#8212;');
    return `<tr><td style="white-space:nowrap">${fmt(r.activated_at)}</td><td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.identity}</td><td>${chB(r.identity_type)}</td><td><code>${r.product_id}</code></td><td><span class="badge badge-blue">${r.plan}</span></td><td>${st}</td><td style="font-size:11px;color:#8b949e">${r.source||'&#8212;'}</td><td>${a}</td><td style="text-align:center">${r.verify_count||0}</td><td style="white-space:nowrap;color:#8b949e;font-size:11px">${fmt(r.last_seen_at)}</td><td style="font-size:11px;color:#8b949e">${mc}</td></tr>`;
  }).join('');
}
function rPay(rows){
  if(!rows.length) return '<tr><td colspan="9" style="color:#8b949e;text-align:center;padding:24px">No payments</td></tr>';
  return rows.map(r=>{
    const st=r.is_refunded?'<span class="badge badge-red">Refunded</span>':'<span class="badge badge-green">Paid</span>';
    const a=r.currency==='INR'?'&#8377;'+r.amount:'$'+r.amount;
    return `<tr><td style="white-space:nowrap">${fmt(r.paid_at)}</td><td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.identity}</td><td>${chB(r.identity_type)}</td><td><code>${r.product_id}</code></td><td style="font-size:11px;color:#8b949e">${r.source}</td><td>${a}</td><td><span class="badge badge-blue">${r.plan}</span></td><td>${st}</td><td style="font-size:11px;color:#8b949e;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.payment_ref}</td></tr>`;
  }).join('');
}
function rCust(rows){
  if(!rows.length) return '<tr><td colspan="5" style="color:#8b949e;text-align:center;padding:24px">No customers</td></tr>';
  return rows.map(r=>`<tr><td style="white-space:nowrap">${fmt(r.created_at)}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.identity}</td><td>${chB(r.identity_type)}</td><td style="text-align:center"><span class="badge ${(r.active_licenses||0)>0?'badge-green':'badge-red'}">${r.active_licenses||0}</span></td><td style="text-align:center;color:#8b949e">${r.total_licenses||0}</td></tr>`).join('');
}
function fmt(ts){if(!ts) return '&#8212;'; return new Date(ts*1000).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'});}

// ── Health check ──────────────────────────────────────────────────────────────
fetch(B+'/health').then(r=>r.json()).then(h=>{
  const el=document.getElementById('hbadge');
  el.textContent=h.status==='ok'?'&#9679; Server OK':'&#9888; '+h.status;
  el.style.color=h.status==='ok'?'#3fb950':'#d29922';
}).catch(()=>document.getElementById('hbadge').textContent='&#10006; Unreachable');
</script>
</body></html>"""

admin_app = FastAPI()

@admin_app.get("/ui", response_class=HTMLResponse)
async def admin_ui(request: Request):
    return HTMLResponse(DASHBOARD_HTML)