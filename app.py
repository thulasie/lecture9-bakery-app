"""Local bakery planner. Python standard library only."""
import csv
import html
import json
import re
import sqlite3
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'bakery.db'
START = '2026-11-23'

def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def quantity(text):
    m = re.search(r'(\d+)\s+(Dozen Cupcakes|Cake)', text, re.I)
    if not m:
        raise ValueError('Use a positive whole quantity of cakes or dozen cupcakes.')
    return ('cupcakes' if 'dozen' in m[2].lower() else 'cake', int(m[1]))

def initialize():
    with connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, customer TEXT, product TEXT, qty INTEGER, pickup TEXT, status TEXT, collected INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, received TEXT, customer TEXT, action TEXT, product TEXT, qty INTEGER, pickup TEXT, raw TEXT, source TEXT, status TEXT DEFAULT 'pending', reason TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS helpers(pickup TEXT PRIMARY KEY, booked TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, day TEXT, detail TEXT);''')
        if c.execute("SELECT 1 FROM settings WHERE key='day'").fetchone():
            return
        c.execute("INSERT INTO settings VALUES('day',?)", (START,))
        for row in csv.DictReader((ROOT/'existing_orders.csv').read_text().splitlines()):
            product, qty = quantity(row['item'])
            pickup = date.fromisoformat(START) + timedelta(days=int(re.search(r'\d+', row['pickup_day'])[0])-23)
            c.execute('INSERT INTO orders(id,customer,product,qty,pickup,status) VALUES(?,?,?,?,?,?)', (int(row['order'][1:]), row['customer'],product,qty,str(pickup),'accepted'))
        for offset, day in enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']):
            path = ROOT/f'InstagramOrders_{day}.html'
            for raw in re.findall(r'<p class="message">(.*?)</p>',path.read_text(), re.S):
                raw = html.unescape(raw)
                name, order, pickup = re.fullmatch(r'Name: (.*?)\nOrder: (.*?)\nPickup Date: (.*)',raw).groups()
                bits = pickup.split()
                pickup = date(2026, {'Nov':11,'Dec':12}[bits[1]],int(bits[2])).isoformat()
                action = 'cancel' if order == 'Cancel' else 'change' if order.startswith('Change') else 'new'
                product, qty = (None,0) if action == 'cancel' else quantity(order)
                c.execute('INSERT INTO requests(received,customer,action,product,qty,pickup,raw,source) VALUES(?,?,?,?,?,?,?,?)',(str(date.fromisoformat(START)+timedelta(days=offset)),name,action,product,qty,pickup,raw,path.name))

def current(c):
    return c.execute("SELECT value FROM settings WHERE key='day'").fetchone()[0]

def hours(product, qty):
    return qty * (0.5 if product == 'cupcakes' else 2)

def revenue(product, qty):
    return qty * (40 if product == 'cupcakes' else 120)

def used(c, pickup):
    return sum(hours(r['product'],r['qty']) for r in c.execute("SELECT * FROM orders WHERE pickup=? AND status='accepted'",(pickup,)))

def capacity(c, pickup):
    return 12 if c.execute('SELECT 1 FROM helpers WHERE pickup=?',(pickup,)).fetchone() else 8

def assess(c, r):
    if (date.fromisoformat(r['pickup'])-date.fromisoformat(r['received'])).days < 2:
        return False, 'Reject: fewer than 2 calendar days of notice.', None
    target = None
    if r['action'] != 'new':
        matches = c.execute("SELECT * FROM orders WHERE customer=? COLLATE NOCASE AND pickup=? AND status='accepted'",(r['customer'],r['pickup'])).fetchall()
        if len(matches) != 1:
            return False, 'Reject: no unique accepted order to change or cancel. Resolve the earlier request first.', None
        target = matches[0]
    load = used(c,r['pickup']) - (hours(target['product'],target['qty']) if target else 0)
    load += 0 if r['action']=='cancel' else hours(r['product'],r['qty'])
    if load > capacity(c,r['pickup']):
        helper = (date.fromisoformat(r['pickup'])-date.fromisoformat(current(c))).days >= 2 and capacity(c,r['pickup'])==8 and load<=12
        return False, f'Capacity exceeded: would use {load:g}h / {capacity(c,r["pickup"])}h.' + (' Book a $100 helper first to add 4h.' if helper else ' No eligible extra capacity available.'), target
    return True, f'Eligible: {load:g}h / {capacity(c,r["pickup"])}h after this {r["action"]} request.', target

def state(c):
    today=current(c)
    requests=[]
    for r in c.execute('SELECT * FROM requests WHERE received<=? ORDER BY received,id',(today,)):
        d=dict(r)
        d['eligible'],d['assessment'],_=assess(c,r)
        d['price']=revenue(r['product'],r['qty']) if r['product'] else 0
        requests.append(d)
    orders=[dict(r) for r in c.execute('SELECT * FROM orders ORDER BY pickup,id')]
    days=[]
    for n in range(9):
        day=str(date.fromisoformat(START)+timedelta(days=n))
        sales=sum(revenue(r['product'],r['qty']) for r in orders if r['pickup']==day and r['status']=='accepted')
        helper=capacity(c,day)==12
        days.append(dict(day=day,used=used(c,day),capacity=capacity(c,day),sales=sales,helper=helper,net=sales-(100 if helper else 0),bookable=not helper and (date.fromisoformat(day)-date.fromisoformat(today)).days>=2))
    return dict(today=today,requests=requests,orders=orders,days=days,audit=[dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC')])

def mutate(c,p):
    today=current(c)
    action=p['action']
    detail=''
    if action=='decide':
        r=c.execute('SELECT * FROM requests WHERE id=?',(int(p['id']),)).fetchone()
        if not r or r['status']!='pending' or r['received']>today: raise ValueError('This request is not pending.')
        # Process messages in arrival order, including dependencies.
        first=c.execute("SELECT id FROM requests WHERE status='pending' AND received<=? ORDER BY received,id LIMIT 1",(today,)).fetchone()
        if first[0]!=r['id']: raise ValueError('Decide the earliest pending message first.')
        ok,reason,target=assess(c,r)
        if p['decision'] not in ('accepted','rejected'): raise ValueError('Invalid decision.')
        if p['decision']=='accepted':
            if not ok: raise ValueError(reason)
            if r['action']=='new': c.execute("INSERT INTO orders(customer,product,qty,pickup,status) VALUES(?,?,?,?,'accepted')",(r['customer'],r['product'],r['qty'],r['pickup']))
            elif r['action']=='change': c.execute('UPDATE orders SET product=?,qty=? WHERE id=?',(r['product'],r['qty'],target['id']))
            else: c.execute("UPDATE orders SET status='cancelled' WHERE id=?",(target['id'],))
        detail=f"{r['customer']} • {r['action']} • {r['pickup']}: {p['decision']}. {reason}"
        c.execute('UPDATE requests SET status=?,reason=? WHERE id=?',(p['decision'],reason,r['id']))
    elif action=='helper':
        day=date.fromisoformat(p['day']).isoformat()
        if (date.fromisoformat(day)-date.fromisoformat(today)).days<2: raise ValueError('Helpers need 2 calendar days of notice.')
        if c.execute('SELECT 1 FROM helpers WHERE pickup=?',(day,)).fetchone(): raise ValueError('A helper is already booked.')
        c.execute('INSERT INTO helpers VALUES(?,?)',(day,today))
        detail=f'Helper booked for {day}: $100, +4 oven hours.'
    elif action=='advance':
        if c.execute("SELECT 1 FROM requests WHERE status='pending' AND received<=?",(today,)).fetchone(): raise ValueError('Accept or reject all pending messages before advancing.')
        if today>='2026-12-01': raise ValueError('End of supplied planning period.')
        day=str(date.fromisoformat(today)+timedelta(days=1))
        c.execute("UPDATE settings SET value=? WHERE key='day'",(day,))
        detail=f'Moved planning day to {day}.'
    elif action=='collect':
        r=c.execute("SELECT * FROM orders WHERE id=? AND status='accepted'",(int(p['id']),)).fetchone()
        if not r or r['pickup']>today: raise ValueError('Collection can only be recorded on or after pickup day.')
        c.execute('UPDATE orders SET collected=? WHERE id=?',(0 if r['collected'] else 1,r['id']))
        detail=f"Updated collection for order #{r['id']}."
    elif action=='add':
        name=str(p['customer']).strip()
        qty=int(p['qty']); product=p['product']; kind=p['kind']
        if not name or len(name)>100 or not 1<=qty<=1000 or product not in ('cake','cupcakes') or kind not in ('new','change','cancel'): raise ValueError('Check customer, product, quantity and request type.')
        pickup=date.fromisoformat(p['pickup']).isoformat()
        if not START<=pickup<='2026-12-01': raise ValueError('Use a pickup date within the planning period, Nov 23–Dec 1.')
        c.execute('INSERT INTO requests(received,customer,action,product,qty,pickup,raw,source) VALUES(?,?,?,?,?,?,?,?)',(today,name,kind,product,qty,pickup,f'{name}: {kind}, {qty} {product}, {pickup}','Manual entry'))
        detail=f'Manual {kind} request added for {name}.'
    else: raise ValueError('Unknown action.')
    c.execute('INSERT INTO audit(day,detail) VALUES(?,?)',(today,detail))

PAGE=r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Home Bakery • Order book</title>
<style>
:root{--ink:#302d28;--green:#355d4b;--cream:#f7f4ed}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:15px/1.5 system-ui,sans-serif}header{background:var(--green);color:white;padding:24px max(5vw,20px);display:flex;justify-content:space-between;align-items:center}header a{color:white}h1{font:36px Georgia;margin:0}h2{font:27px Georgia;margin:0 0 14px}h3{margin:0;font-size:17px}main{max-width:1350px;margin:auto;padding:28px 24px}button,input,select{font:inherit;border:1px solid #c5c5b8;border-radius:7px;padding:9px 12px}button{cursor:pointer;background:white;color:var(--green);font-weight:600}button.primary{background:var(--green);color:white}button:disabled{opacity:.45;cursor:not-allowed}button:hover:not(:disabled){filter:brightness(.94)}.topline{display:flex;align-items:center;justify-content:space-between;gap:15px}.muted{color:#66685d}.days{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0}.card,section{background:white;border:1px solid #dfdfd1;border-radius:12px;padding:20px}.card{padding:16px}.bar{height:8px;background:#ebece3;border-radius:8px;margin:12px 0;overflow:hidden}.bar span{display:block;background:#79977e;height:100%}.layout{display:grid;grid-template-columns:1.2fr 1fr;gap:22px}.request{border-top:1px solid #e2e2d9;padding:18px 0}.request:first-of-type{border-top:0}.badge{display:inline-block;border-radius:30px;background:#edf3ec;padding:3px 10px;font-size:12px;font-weight:700}.bad{background:#f9e5d9;color:#883c22}.actions{display:flex;gap:8px;margin-top:12px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;border-bottom:1px solid #eee;padding:9px 6px}th{font-weight:600;color:#626458}.scroll{overflow:auto}.form{display:flex;gap:12px;flex-wrap:wrap}.form label{display:flex;flex-direction:column;gap:4px}.notice{background:#ebeede;border-radius:8px;padding:14px;margin:16px 0}#error{color:#892e1e;font-weight:600}details{margin-top:18px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;font:inherit}.full{margin-top:22px}a{color:var(--green)}@media(max-width:850px){.layout{grid-template-columns:1fr}.days{grid-template-columns:repeat(2,1fr)}header{align-items:start}h1{font-size:29px}}@media(max-width:500px){.days{grid-template-columns:1fr}.topline{align-items:start;flex-direction:column}}
</style><header><div><small>HOME BAKERY / THE ORDER BOOK</small><h1>A little room for something sweet.</h1></div><a href="/README.html" target="_blank">How to use ↗</a></header><main><div class="topline"><div><h2 id="day"></h2><span class="muted">Planning day • Every pickup is at 5 PM</span></div><button onclick="advance()">Finish day →</button></div><div id="error" role="alert"></div><div class="notice">Cupcakes: $40 / dozen · 0.5h / dozen &nbsp; | &nbsp; Cakes: $120 each · 2h each<br>8 oven hours daily. Orders, changes, cancellations and helpers require 2 calendar days’ notice.</div><div id="days" class="days"></div><div class="layout"><section><h2>Messages & decisions</h2><p class="muted">Work from the earliest pending message. Capacity updates after every decision. An eligible request is your choice to accept or reject.</p><label>Show <select id="filter" onchange="render()"><option value="pending">Pending</option><option value="all">All received messages</option></select></label><div id="requests"></div></section><section><h2>Pickup book</h2><label>Find a customer <input id="search" placeholder="Customer name" oninput="renderOrders()"></label><div class="scroll"><table><thead><tr><th>Customer / order</th><th>Pickup</th><th>Order</th><th>Collection</th></tr></thead><tbody id="orders"></tbody></table></div><p class="muted">Cancelled orders stay in the book. Collection does not free oven capacity.</p><details><summary>Planning guidance</summary><p>Cakes tend to arrive a week ahead; cupcakes usually arrive in the last 3 days. Saturday is busiest. Leave room deliberately or book a helper by Thursday for Saturday.</p><p>A helper adds 4 hours for $100, once per day. Cupcakes earn $80 per oven hour; cakes earn $60. Compare additional sales against the $100 fee. Sales less helper fees excludes ingredients, labour and other costs, so it is not profit.</p></details></section></div><section class="full"><h2>Add a request</h2><p class="muted">For messages received on the current planning day. To change or cancel, enter the same customer name and original pickup date.</p><form id="entry" class="form"><label>Customer<input name="customer" required maxlength="100"></label><label>Request<select name="kind"><option value="new">New order</option><option value="change">Change quantity/product</option><option value="cancel">Cancellation</option></select></label><label>Product<select name="product"><option value="cupcakes">Cupcakes (dozens)</option><option value="cake">Cakes (each)</option></select></label><label>Quantity<input name="qty" type="number" min="1" max="1000" value="1" required></label><label>Pickup date<input name="pickup" type="date" min="2026-11-23" max="2026-12-01" required></label><button class="primary">Add to inbox</button></form><small>Quantity and product are ignored for cancellations.</small></section><details class="full"><summary>Decision history</summary><div id="audit"></div></details></main><script>
let s;const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmt=d=>new Date(d+'T12:00:00').toLocaleDateString('en-CA',{weekday:'short',month:'short',day:'numeric'});const item=r=>`${r.qty} ${r.product==='cupcakes'?'dozen cupcakes':'cake'+(r.qty>1?'s':'')}`;
async function load(){try{let r=await fetch('/api/state');if(!r.ok)throw Error('Could not load the order book.');s=await r.json();render()}catch(e){document.querySelector('#error').textContent=e.message}}
async function act(p){try{let r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});let j=await r.json();if(!r.ok)throw Error(j.error);document.querySelector('#error').textContent='';await load();return true}catch(e){document.querySelector('#error').textContent=e.message;return false}}
function advance(){if(confirm('Finish this planning day? You cannot go back. Make any helper bookings first.'))act({action:'advance'})}
function helper(day){if(confirm('Book a helper for '+fmt(day)+'? This commits $100 for 4 extra hours.'))act({action:'helper',day})}
function render(){document.querySelector('#day').textContent=fmt(s.today)+', 2026';document.querySelector('#days').innerHTML=s.days.map(d=>`<div class="card"><div class="topline"><h3>${fmt(d.day)}</h3>${d.helper?'<span class="badge">Helper booked</span>':''}</div><div class="bar"><span style="width:${100*d.used/d.capacity}%"></span></div><b>${d.used} / ${d.capacity}h</b> · ${d.capacity-d.used}h free<br><span class="muted">Sales $${d.sales} · After helper $${d.net}</span><br>${d.bookable?`<button style="margin-top:10px" onclick="helper('${d.day}')">Book helper · $100</button>`:''}</div>`).join('');let pending=s.requests.find(r=>r.status==='pending');let rs=s.requests.filter(r=>document.querySelector('#filter').value==='all'||r.status==='pending');document.querySelector('#requests').innerHTML=rs.map(r=>`<article class="request"><div class="topline"><h3>${esc(r.customer)} · ${esc(r.action)}</h3><span class="badge ${r.status==='rejected'?'bad':''}">${esc(r.status)}</span></div><p>${r.action==='cancel'?'Cancel order':esc(item(r))+' · $'+r.price}<br><b>Pickup ${fmt(r.pickup)} · 5 PM</b><br><small class="muted">Received ${fmt(r.received)} · ${esc(r.source)}</small></p>${r.status==='pending'?`<div class="badge ${r.eligible?'':'bad'}">${esc(r.assessment)}</div><div class="actions"><button class="primary" ${!r.eligible||r.id!==pending?.id?'disabled':''} onclick="act({action:'decide',id:${r.id},decision:'accepted'})">${r.action==='new'?'Accept order':'Approve '+r.action}</button><button ${r.id!==pending?.id?'disabled':''} onclick="act({action:'decide',id:${r.id},decision:'rejected'})">Reject</button></div>${r.id!==pending?.id?'<small class="muted">Decide earlier messages first.</small>':''}`:`<p class="muted">${esc(r.reason)}</p>`}<details><summary>Original message</summary><pre>${esc(r.raw)}</pre></details></article>`).join('')||'<p>No pending messages. Review helpers before finishing the day.</p>';renderOrders();document.querySelector('#audit').innerHTML=s.audit.map(a=>`<p><b>${fmt(a.day)}</b> · ${esc(a.detail)}</p>`).join('')||'<p>No decisions yet.</p>'}
function renderOrders(){let q=document.querySelector('#search').value.toLowerCase();document.querySelector('#orders').innerHTML=s.orders.filter(r=>r.customer.toLowerCase().includes(q)).map(r=>`<tr><td><b>${esc(r.customer)}</b><br>#${r.id}</td><td>${fmt(r.pickup)}<br>5 PM</td><td>${esc(item(r))}<br>$${r.qty*(r.product==='cake'?120:40)}${r.status==='cancelled'?'<br><span class="badge bad">Cancelled</span>':''}</td><td>${r.status==='accepted'?`<button ${r.pickup>s.today?'disabled':''} onclick="act({action:'collect',id:${r.id}})">${r.collected?'Collected ✓':'Mark collected'}</button>`:'—'}</td></tr>`).join('')}
document.querySelector('#entry').onsubmit=async e=>{e.preventDefault();let p=Object.fromEntries(new FormData(e.target));p.action='add';if(await act(p))e.target.reset()};load();
</script></html>'''


# The second dashboard shares the order book; the original page stays at /.
BLUSH_STYLE = r"""
:root{--ink:#57263e;--green:#ad365f;--cream:#fff5f8}body{font-size:16px;background:linear-gradient(135deg,#fff8fa,#fce5ee);color:var(--ink)}header{background:#fffafb;color:var(--ink);border-bottom:1px solid #edc4d3;padding:22px 5vw}header a{color:#a32d57}header small{letter-spacing:3px;font-size:14px}header h1{font-size:36px;margin-top:5px}h2{font-size:29px}main{max-width:1400px}button,input,select{border-color:#e6b8ca;border-radius:12px}button{color:#a32d57}button.primary{background:#ad365f}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #a32d57;outline-offset:3px}.card,section{border:1px solid #edccd9;border-radius:20px;box-shadow:0 5px 20px #873e6010}.muted{color:#795366}.notice{background:#f8deea}.badge{background:#fbe2ec;color:#87304f;font-size:14px}.bad{background:#ffe4df;color:#8b302b}.bar{background:#f5dfE8;height:10px}.bar span{background:#c74f7b}.days{grid-template-columns:repeat(3,1fr)}.daypick{display:block;border:0;background:none;padding:0;text-align:left;font:700 18px Georgia;color:#782742}.card.selected{border:2px solid #b33765;background:#fff1f6}.hero{display:grid;grid-template-columns:1fr 230px;background:#f6ccdf;border-radius:26px;padding:32px 36px;margin-bottom:24px;align-items:center;overflow:hidden}.hero h2{font-size:42px;margin:5px 0 12px}.hero p{max-width:550px;margin:0}.eyebrow{text-transform:uppercase;letter-spacing:2px;font-size:14px;font-weight:700}.pastries{font-size:86px;line-height:1.2;text-align:center;filter:drop-shadow(0 8px 6px #94324d22);transform:rotate(-8deg)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}.metric{background:#fff;border:1px solid #edccd9;border-radius:18px;padding:20px}.metric strong{display:block;font:32px Georgia;margin-top:6px}.today-section{margin-bottom:24px}.ordergrid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.sweetorder{border:1px solid #edccd9;background:#fff8fb;border-radius:16px;padding:20px}.sweetorder h3{font:700 25px Georgia}.orderline{display:flex;justify-content:space-between;gap:14px;align-items:center;margin:16px 0}.price{font:700 29px Georgia;color:#a72e59}.schedule{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}.schedule span{background:#f9e2ec;border-radius:9px;padding:6px 10px;font-size:14px}.checker{background:#fff1f6;border:1px solid #e9bbce;border-radius:18px;padding:20px;margin:20px 0}.checker h3{margin-bottom:12px}.checker label{display:inline-flex;flex-direction:column;gap:6px;margin:0 12px 10px 0}.result{font-weight:600;margin:8px 0 0}.sectiontitle{display:flex;justify-content:space-between;align-items:center;gap:12px}.sectiontitle a{font-size:16px}.layout{grid-template-columns:1.1fr 1fr}.request{border-color:#efd5e0}th{color:#795366}#error:not(:empty){padding:16px;background:#ffe4df;border-radius:12px;margin-bottom:16px}a{color:#a32d57}small{font-size:14px}#day{color:#9f2d59}.topline>div>h2{font-size:34px}@media(max-width:850px){.layout{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.hero{grid-template-columns:1fr 140px;padding:24px}.pastries{font-size:58px}.days{grid-template-columns:repeat(2,1fr)}}@media(max-width:550px){.hero{grid-template-columns:1fr}.pastries{display:none}.hero h2{font-size:34px}.ordergrid,.days{grid-template-columns:1fr}.metric{padding:14px}header{gap:20px}header h1{font-size:29px}main{padding:20px 14px}.sectiontitle{align-items:start;flex-direction:column}}
"""
BLUSH_SCRIPT = r"""
let selectedDay;
const baseRender=render;
const money=n=>new Intl.NumberFormat('en-CA',{style:'currency',currency:'CAD',maximumFractionDigits:0}).format(n);
const clock=h=>{const m=Math.round(h*60);return `${Math.floor(m/60)%12||12}:${String(m%60).padStart(2,'0')} ${m>=720?'PM':'AM'}`};
function chooseDay(d){selectedDay=d;render()}
render=function(){
 baseRender();selectedDay=selectedDay||s.today;
 const d=s.days.find(x=>x.day===selectedDay);
 const orders=s.orders.filter(x=>x.pickup===selectedDay&&x.status==='accepted');
 document.querySelector('#metrics').innerHTML=[['Orders to bake',orders.length],['Baking hours booked',d.used+'h'],['Hours still available',Math.max(0,d.capacity-d.used)+'h'],['Order sales',money(d.sales)]].map(([label,value])=>`<div class="metric"><span class="muted">${label}</span><strong>${value}</strong></div>`).join('');
 document.querySelector('#selected-date').textContent=(selectedDay===s.today?'Today’s orders · ':'Orders · ')+fmt(selectedDay);
 let elapsed=0;
 document.querySelector('#today-orders').innerHTML=orders.map(r=>{let duration=r.qty*(r.product==='cake'?2:.5);let start=9+elapsed/(d.helper?1.5:1);elapsed+=duration;let end=9+elapsed/(d.helper?1.5:1);return `<article class="sweetorder"><div class="topline"><h3>${esc(r.customer)}</h3><span class="badge">${r.collected?'Collected':'Accepted'}</span></div><div class="orderline"><div><span aria-hidden="true">${r.product==='cake'?'🍰':'🧁'}</span> ${esc(item(r))}<br><span class="muted">Order #${r.id}</span></div><strong class="price">${money(r.qty*(r.product==='cake'?120:40))}</strong></div><div class="schedule"><span>${duration} oven hours</span><span>Est. ready <b>${clock(end)}</b></span><span>Pickup 5 PM</span></div><p class="muted">Estimated baking slot ${clock(start)}–${clock(end)}</p><button ${r.pickup>s.today?'disabled':''} onclick="act({action:'collect',id:${r.id}})">${r.collected?'Undo collection':'Mark collected'}</button></article>`}).join('')||'<p class="muted">No accepted orders for this date. A little room for something sweet!</p>';
 document.querySelector('#days').innerHTML=s.days.map(x=>`<div class="card ${x.day===selectedDay?'selected':''}"><button class="daypick" aria-pressed="${x.day===selectedDay}" onclick="chooseDay('${x.day}')">${fmt(x.day)}${x.day===s.today?' · TODAY':''}</button><div class="bar"><span style="width:${Math.min(100,100*x.used/x.capacity)}%"></span></div><b>${x.used}h booked</b> · ${Math.max(0,x.capacity-x.used)}h left<br><span class="muted">${money(x.sales)} sales · ${x.capacity}h capacity</span>${x.helper?'<p class="badge">Helper booked · $100</p>':x.bookable?`<div class="actions"><button onclick="helper('${x.day}')">Add helper · $100</button></div>`:''}</div>`).join('');
 document.querySelector('#checkdate').value=selectedDay;checkCapacity();
};
function checkCapacity(){if(!s)return;let day=document.querySelector('#checkdate').value;let d=s.days.find(x=>x.day===day);let qty=Number(document.querySelector('#checkqty').value),product=document.querySelector('#checkproduct').value;let out=document.querySelector('#checkresult');if(!d||!Number.isInteger(qty)||qty<1||qty>1000){out.textContent='Choose a planning date and a whole quantity from 1 to 1,000.';return}let need=qty*(product==='cake'?2:.5),left=d.capacity-d.used;let notice=(Date.parse(day+'T12:00:00Z')-Date.parse(s.today+'T12:00:00Z'))/86400000>=2;out.textContent=`This order needs ${need}h. ${Math.max(0,left)}h available on ${fmt(day)}. `+(!notice?'Cannot accept: at least 2 days’ notice is required.':need>left?`Cannot accept: ${need-left}h over capacity.`:`Fits the rules: ${left-need}h would remain. Add it to the inbox below for review.`)}
"""
PAGE2=PAGE.replace('<title>Home Bakery • Order book</title>','<title>Blush &amp; Butter | Your daily bake book</title>')
PAGE2=PAGE2.replace('</style>','\n'+BLUSH_STYLE+'</style>',1)
start=PAGE2.index('<header>');end=PAGE2.index('<main>',start)
PAGE2=PAGE2[:start]+"""<header><div><small>YOUR DAILY BAKE BOOK</small><h1>Blush &amp; Butter ♡</h1></div><a href="/">Original order book</a></header>"""+PAGE2[end:]
PAGE2=PAGE2.replace('<main>',"""<main><div class="hero"><div><span class="eyebrow">A sprinkle of organisation</span><h2>Sweet plans. Happy baking.</h2><p>Your orders, oven hours, and a little breathing room.<br>Made for a beautifully organised baking day.</p></div><div class="pastries" aria-label="Cupcake and cake illustrations">🧁 🍰</div></div>""",1)
PAGE2=PAGE2.replace('<div id="days" class="days"></div>',"""<div id="metrics" class="metrics"></div><section class="today-section"><div class="sectiontitle"><h2 id="selected-date">Today’s orders</h2><button onclick="chooseDay(s.today)">Show today</button></div><p class="muted">Completion estimates assume a 9 AM start, in order-number sequence. A helper shares the work evenly. These are planning estimates; all pickups remain at 5 PM.</p><div id="today-orders" class="ordergrid"></div></section><h2>A peek at your baking days</h2><p class="muted">Select a date to see its orders. The highlighted TODAY is the current planning day.</p><div id="days" class="days"></div><div class="checker"><h3>Can I fit another order?</h3><label>Pickup date<input id="checkdate" type="date" min="2026-11-23" max="2026-12-01" onchange="checkCapacity()"></label><label>Sweet treat<select id="checkproduct" onchange="checkCapacity()"><option value="cupcakes">Cupcakes (dozens)</option><option value="cake">Cakes (each)</option></select></label><label>Quantity<input id="checkqty" type="number" min="1" max="1000" value="1" oninput="checkCapacity()"></label><p id="checkresult" class="result" role="status"></p></div>""")
PAGE2=PAGE2.replace('};load();','};'+BLUSH_SCRIPT+';load();')

class Handler(BaseHTTPRequestHandler):
    def send(self, code, data, content_type='application/json'):
        body=data.encode()
        self.send_response(code)
        self.send_header('Content-Type',content_type+'; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path=='/': self.send(200,PAGE,'text/html')
        elif self.path in ('/blush','/blush/'): self.send(200,PAGE2,'text/html')
        elif self.path=='/README.html': self.send(200,(ROOT/'README.html').read_text(),'text/html')
        elif self.path=='/api/state':
            with connect() as c: self.send(200,json.dumps(state(c)))
        else: self.send(404,json.dumps({'error':'Not found'}))
    def do_POST(self):
        if self.path!='/api/action': return self.send(404,'{}')
        if self.headers.get('Origin') not in (None,'http://127.0.0.1:8000','http://localhost:8000'):
            return self.send(403,json.dumps({'error':'Local requests only.'}))
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384: raise ValueError('Invalid request size.')
            p=json.loads(self.rfile.read(length))
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                mutate(c,p)
            self.send(200,'{"ok":true}')
        except (ValueError,KeyError,TypeError,sqlite3.IntegrityError) as e:
            self.send(400,json.dumps({'error':str(e)}))

if __name__=='__main__':
    initialize()
    print('Bakery ready: http://127.0.0.1:8000 (Ctrl+C to stop)',flush=True)
    HTTPServer(('127.0.0.1',8000),Handler).serve_forever()
