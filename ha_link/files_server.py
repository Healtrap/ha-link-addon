#!/usr/bin/env python3
"""HA Link file service: web file manager + WebDAV over the existing FRP tunnel."""
import hmac
import json
import os
import time
from http import HTTPStatus
from urllib.parse import parse_qs, quote, unquote

from cheroot import wsgi
from wsgidav.wsgidav_app import WsgiDAVApp

TOKEN = os.environ.get("FILES_TOKEN", "")
DOMAIN = os.environ.get("FILES_DOMAIN", "files.virvinsky.ru")
PORT = int(os.environ.get("FILES_PORT", "8099"))
MAX_UPLOAD = int(os.environ.get("FILES_MAX_UPLOAD", str(1024 * 1024 * 1024)))
ROOTS = [r for r in os.environ.get("FILES_ROOTS", "/config:/share:/addons").split(":") if os.path.isdir(r)]
HEALTH_FILE = os.environ.get("FILES_HEALTH_FILE", "/data/hb")
HEALTH_MAX_AGE = int(os.environ.get("FILES_HEALTH_MAX_AGE", "60"))

TEXT_EXT = {
    ".yaml", ".yml", ".json", ".txt", ".md", ".log", ".conf", ".cfg", ".ini", ".toml",
    ".sh", ".ps1", ".bat", ".py", ".js", ".css", ".html", ".xml", ".env", ".csv",
}

_attempts = {}


def rate_ok(ip):
    now = time.time()
    cnt, ts = _attempts.get(ip, (0, now))
    if now - ts > 60:
        cnt, ts = 0, now
    cnt += 1
    _attempts[ip] = (cnt, ts)
    return cnt <= 20


def token_ok(value):
    return bool(TOKEN) and hmac.compare_digest(value or "", TOKEN)


def cookie_token(environ):
    for part in environ.get("HTTP_COOKIE", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "hatoken":
            return unquote(v)
    return ""


def is_authed(environ):
    if token_ok(environ.get("HTTP_X_TOKEN", "")):
        return True
    if token_ok(cookie_token(environ)):
        return True
    return False


def resolve(path):
    path = (path or "").replace("\\", "/")
    if not path.startswith("/"):
        raise PermissionError("path must be absolute")
    real = os.path.realpath(path)
    for root in ROOTS:
        if real == root or real.startswith(root + os.sep):
            return real
    raise PermissionError("outside allowed roots")


def entry_info(path):
    st = os.stat(path)
    name = os.path.basename(path.rstrip("/")) or path
    return {
        "name": name,
        "path": path,
        "dir": os.path.isdir(path),
        "size": 0 if os.path.isdir(path) else st.st_size,
        "mtime": int(st.st_mtime),
    }


def send_json(start_response, obj, status="200 OK"):
    body = json.dumps(obj, ensure_ascii=False).encode()
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return [body]


def send_html(start_response, html, status="200 OK"):
    body = html.encode()
    start_response(status, [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return [body]


def send_error(start_response, code, message):
    return send_json(start_response, {"ok": False, "error": message},
                     f"{code} {HTTPStatus(code).phrase}")


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HA Link files</title>
<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#1c1c1c;padding:24px;border-radius:12px;min-width:280px}
input{width:100%;padding:10px;margin:12px 0;border-radius:8px;border:1px solid #444;background:#0d0d0d;color:#eee}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#03a9f4;color:#fff;font-size:15px}
p{color:#f66;min-height:18px;margin:0}</style></head>
<body><form method="post" action="/login">
<h3>HA Link files</h3>
<input type="password" name="token" placeholder="Token" autofocus>
<input type="hidden" name="next" value="__NEXT__">
<button type="submit">Войти</button><p>__ERR__</p>
</form></body></html>"""

APP_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HA Link files</title>
<style>
:root{color-scheme:dark}
body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#e6e6e6}
header{display:flex;gap:8px;align-items:center;padding:10px 14px;background:#1b1b1b;position:sticky;top:0;z-index:5}
select,button,input[type=text]{background:#0d0d0d;color:#eee;border:1px solid #3a3a3a;border-radius:8px;padding:7px 10px}
button{cursor:pointer}button.primary{background:#0288d1;border-color:#0288d1}
main{padding:12px 14px 60px;max-width:1100px;margin:0 auto}
.crumbs{font-size:14px;color:#9ad;margin:8px 0;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:8px 6px;border-bottom:1px solid #262626;text-align:left;vertical-align:middle}
th{color:#888;font-weight:500}
a{color:#4fc3f7;text-decoration:none}
.actions a,.actions button{margin-right:10px;font-size:13px;background:none;border:0;color:#4fc3f7;padding:0;cursor:pointer}
.del{color:#ef5350 !important}
.small{color:#777;font-size:12px}
#ed{position:fixed;inset:0;background:#000c;display:none;align-items:center;justify-content:center;padding:20px;z-index:10}
#ed .box{background:#1a1a1a;border-radius:12px;width:100%;max-width:900px;height:80vh;display:flex;flex-direction:column;padding:14px}
#ed textarea{flex:1;width:100%;background:#0d0d0d;color:#ddd;border:1px solid #333;border-radius:8px;padding:10px;font-family:ui-monospace,monospace;font-size:13px;resize:none}
#ed .row{display:flex;gap:10px;align-items:center;margin-top:10px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#263238;color:#fff;padding:10px 16px;border-radius:8px;display:none;z-index:20}
</style></head><body>
<header>
<select id="rootSelect"></select>
<button class="primary" id="btnUpload">Загрузить</button>
<button id="btnMkdir">Папка</button>
<input type="file" id="fileInput" multiple style="display:none">
<button id="btnLogout">Выйти</button>
</header>
<main>
<div class="crumbs" id="crumbs"></div>
<table><thead><tr><th>Имя</th><th>Размер</th><th>Изменён</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
<p class="small" id="status"></p>
</main>
<div id="ed"><div class="box">
<div id="edTitle" class="crumbs"></div>
<textarea id="edText" spellcheck="false"></textarea>
<div class="row"><button class="primary" id="edSave">Сохранить</button><button id="edClose">Закрыть</button></div>
</div></div>
<div id="toast"></div>
<script>
let cur = localStorage.getItem('haRoot') || '/config';
let editing = null;
const $ = s => document.querySelector(s);
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2500)}
function fmtSize(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';if(n<1073741824)return (n/1048576).toFixed(1)+' MB';return (n/1073741824).toFixed(2)+' GB'}
function fmtDate(ts){return new Date(ts*1000).toLocaleString()}
function esc(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(url,opts){const r=await fetch(url,Object.assign({credentials:'same-origin'},opts||{}));
 if(r.status===401){location.href='/login';throw new Error('auth')}
 const j=await r.json().catch(()=>({ok:false,error:'bad response'}));if(!j.ok)throw new Error(j.error||'error');return j}
async function initRoots(){const j=await api('/api/roots');const sel=$('#rootSelect');sel.innerHTML='';
 for(const r of j.roots){const o=document.createElement('option');o.value=r;o.textContent=r;sel.appendChild(o)}
 if(!j.roots.includes(cur)){cur=j.roots[0];localStorage.setItem('haRoot',cur)}sel.value=cur}
async function load(){
 document.querySelectorAll('.crumb').length;
 const j=await api('/api/list?path='+encodeURIComponent(cur));
 renderCrumbs(j.path);const rows=$('#rows');rows.innerHTML='';
 if(j.parent)rows.appendChild(row('..','',0,0,'dir',j.parent));
 for(const e of j.entries){rows.appendChild(row(e.name,e.path,e.dir?0:e.size,e.mtime,e.dir?'dir':'file'))}
 $('#status').textContent=j.entries.length+' объектов'}
function renderCrumbs(p){const el=$('#crumbs');const parts=p.split('/').filter(Boolean);let html='';
 let acc='';for(let i=0;i<parts.length;i++){acc+='/'+parts[i];html+=`<a href="#" class="crumb" data-p="${esc(acc)}">${esc(parts[i])}</a> / `}
 el.innerHTML=html;$('#crumbs').querySelectorAll('.crumb').forEach(a=>a.onclick=e=>{e.preventDefault();cur=a.dataset.p;localStorage.setItem('haRoot',cur);load()})}
function row(name,path,size,mtime,type,parent){
 const tr=document.createElement('tr');const td1=document.createElement('td');
 const a=document.createElement('a');a.textContent=name;a.href='#';
 if(type==='dir'){a.onclick=e=>{e.preventDefault();cur=path;localStorage.setItem('haRoot',cur);load()}}
 else{a.href='/api/download?path='+encodeURIComponent(path)}
 td1.appendChild(a);tr.appendChild(td1);
 const td2=document.createElement('td');td2.textContent=type==='dir'?'':fmtSize(size);tr.appendChild(td2);
 const td3=document.createElement('td');td3.textContent=type==='dir'?'':fmtDate(mtime);tr.appendChild(td3);
 const td4=document.createElement('td');td4.className='actions';
 if(type==='file'){
  const ed=document.createElement('button');ed.textContent='✎';ed.onclick=()=>openEditor(path);td4.appendChild(ed);
  const del=document.createElement('button');del.textContent='✕';del.className='del';del.onclick=()=>removeFile(path,name);td4.appendChild(del)}
 tr.appendChild(td4);return tr}
async function openEditor(path){
 const j=await api('/api/read?path='+encodeURIComponent(path));
 editing=path;$('#edTitle').textContent=path;$('#edText').value=j.content;$('#ed').style.display='flex'}
async function removeFile(path,name){if(!confirm('Удалить '+name+'?'))return;
 await api('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});toast('Удалено');load()}
$('#rootSelect').onchange=e=>{cur=e.target.value;localStorage.setItem('haRoot',cur);load()};
$('#btnUpload').onclick=()=>$('#fileInput').click();
$('#fileInput').onchange=async e=>{
 for(const f of e.target.files){$('#status').textContent='Загрузка '+f.name+'...';
  const r=await fetch('/api/upload?path='+encodeURIComponent(cur+'/'+f.name),{method:'POST',credentials:'same-origin',body:f});
  if(!r.ok){toast('Ошибка загрузки '+f.name);break}}
 e.target.value='';toast('Готово');load()};
$('#btnMkdir').onclick=async()=>{const n=prompt('Имя папки');if(!n)return;
 await api('/api/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:cur+'/'+n})});load()};
$('#edClose').onclick=()=>{$('#ed').style.display='none';editing=null};
$('#edSave').onclick=async()=>{if(!editing)return;
 await api('/api/write',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:editing,content:$('#edText').value})});
 toast('Сохранено');$('#ed').style.display='none';editing=null;load()};
$('#btnLogout').onclick=async()=>{await fetch('/logout',{credentials:'same-origin'});location.href='/login'};
initRoots().then(load).catch(e=>toast(e.message));
</script></body></html>"""


def web_app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    ip = environ.get("REMOTE_ADDR", "-")

    # /health — без авторизации, только для watchdog Supervisor.
    # Отдаём минимум: живость контейнера, без токенов и путей.
    if path == "/health":
        return health_response(start_response)

    if path == "/login":
        if method == "POST":
            if not rate_ok(ip):
                return send_html(start_response, LOGIN_PAGE.replace("__NEXT__", "/").replace("__ERR__", "Слишком много попыток"), "429 Too Many Requests")
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            body = environ["wsgi.input"].read(min(length, 8192)).decode("utf-8", "replace")
            form = parse_qs(body)
            if token_ok((form.get("token") or [""])[0]):
                nxt = (form.get("next") or ["/"])[0]
                if not nxt.startswith("/"):
                    nxt = "/"
                cookie = f"hatoken={quote(TOKEN)}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax"
                start_response("303 See Other", [("Location", nxt), ("Set-Cookie", cookie)])
                return [b""]
            return send_html(start_response, LOGIN_PAGE.replace("__NEXT__", "/").replace("__ERR__", "Неверный токен"), "401 Unauthorized")
        return send_html(start_response, LOGIN_PAGE.replace("__NEXT__", "/").replace("__ERR__", ""))

    if path == "/logout":
        start_response("303 See Other", [("Location", "/login"), ("Set-Cookie", "hatoken=; Path=/; Max-Age=0; HttpOnly; Secure")])
        return [b""]

    if not is_authed(environ):
        if path.startswith("/api/"):
            return send_error(start_response, 401, "unauthorized")
        start_response("303 See Other", [("Location", "/login")])
        return [b""]

    query = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        if path == "/":
            return send_html(start_response, APP_PAGE)

        if path == "/api/roots":
            return send_json(start_response, {"ok": True, "roots": ROOTS})

        if path == "/api/list":
            target = resolve((query.get("path") or [ROOTS[0]])[0])
            if not os.path.isdir(target):
                return send_error(start_response, 404, "not a directory")
            entries = []
            for name in sorted(os.listdir(target), key=lambda s: (not os.path.isdir(os.path.join(target, s)), s.lower())):
                full = os.path.join(target, name)
                try:
                    entries.append(entry_info(full))
                except OSError:
                    pass
            parent = os.path.dirname(target.rstrip("/"))
            parent = parent if any(parent == r or parent.startswith(r + os.sep) for r in ROOTS) else ""
            return send_json(start_response, {"ok": True, "path": target, "parent": parent, "entries": entries})

        if path == "/api/download":
            target = resolve((query.get("path") or [""])[0])
            if not os.path.isfile(target):
                return send_error(start_response, 404, "not a file")
            size = os.path.getsize(target)
            start_response("200 OK", [
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(size)),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{quote(os.path.basename(target))}"),
            ])
            f = open(target, "rb")

            def pump():
                try:
                    while True:
                        chunk = f.read(1 << 16)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    f.close()
            return pump()

        if path == "/api/read":
            target = resolve((query.get("path") or [""])[0])
            if not os.path.isfile(target):
                return send_error(start_response, 404, "not a file")
            if os.path.getsize(target) > 4 * 1024 * 1024:
                return send_error(start_response, 413, "file too large to edit")
            with open(target, "rb") as fh:
                data = fh.read()
            return send_json(start_response, {"ok": True, "content": data.decode("utf-8", "replace")})

        if path == "/api/upload" and method == "POST":
            target = resolve((query.get("path") or [""])[0])
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if length > MAX_UPLOAD:
                return send_error(start_response, 413, "file too large")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".part"
            left = length
            with open(tmp, "wb") as out:
                while left > 0:
                    chunk = environ["wsgi.input"].read(min(1 << 16, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
            os.replace(tmp, target)
            return send_json(start_response, {"ok": True, "size": os.path.getsize(target)})

        if path in ("/api/mkdir", "/api/delete", "/api/rename", "/api/write") and method == "POST":
            length = int(environ.get("CONTENT_LENGTH") or 0)
            payload = json.loads(environ["wsgi.input"].read(min(length, 8 * 1024 * 1024)) or b"{}")
            target = resolve(payload.get("path", ""))
            if path == "/api/mkdir":
                os.makedirs(target, exist_ok=False)
            elif path == "/api/delete":
                if os.path.isdir(target) and not os.path.islink(target):
                    import shutil
                    shutil.rmtree(target)
                else:
                    os.remove(target)
            elif path == "/api/rename":
                new = resolve(payload.get("to", ""))
                os.replace(target, new)
            else:
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(payload.get("content", ""))
            return send_json(start_response, {"ok": True})

        return send_error(start_response, 404, "not found")
    except PermissionError as exc:
        return send_error(start_response, 403, str(exc))
    except FileNotFoundError:
        return send_error(start_response, 404, "not found")
    except FileExistsError:
        return send_error(start_response, 409, "already exists")
    except Exception as exc:  # noqa: BLE001
        print(f"[files] error: {exc!r}", flush=True)
        return send_error(start_response, 500, str(exc))


dav_config = {
    "provider_mapping": {f"/{os.path.basename(r)}": {"root": r, "readonly": False} for r in ROOTS},
    "simple_dc": {"user_mapping": {"*": {"ha": {"password": TOKEN}}}},
    "http_authenticator": {
        "domain_controller": None,
        "accept_basic": True,
        "accept_digest": False,
        "default_to_digest": False,
    },
    "verbose": 1,
    "logging": {"enable": False},
    "dir_browser": {"enable": False},
}

dav_app = WsgiDAVApp(dav_config)


def health_response(start_response):
    """200, пока контейнер жив (heartbeat свежий). Не зависит от связи с релеем."""
    age = None
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as fh:
            age = int(time.time() - float(fh.read().strip()))
    except Exception:
        age = None
    if age is not None and 0 <= age <= HEALTH_MAX_AGE:
        return send_json(start_response, {"ok": True, "age": age})
    return send_json(start_response, {"ok": False, "age": age}, "503 Service Unavailable")


def app(environ, start_response):
    if environ.get("PATH_INFO", "").startswith("/dav"):
        environ["PATH_INFO"] = environ["PATH_INFO"][4:] or "/"
        return dav_app(environ, start_response)
    return web_app(environ, start_response)


if __name__ == "__main__":
    if not TOKEN:
        print("[files] FILES_TOKEN is empty, file service disabled", flush=True)
        raise SystemExit(1)
    print(f"[files] roots={ROOTS} port={PORT} domain={DOMAIN}", flush=True)
    server = wsgi.Server(("0.0.0.0", PORT), app, numthreads=10, timeout=120)
    server.start()
