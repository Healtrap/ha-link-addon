#!/usr/bin/env python3
"""HA Control — веб-панель управления Home Assistant.

Работает только через ingress Supervisor (отдельного порта наружу нет).
Права: hassio_api (role=manager) — список аддонов, старт/стоп/рестарт,
логи, бэкапы, сведения о хосте. Никаких docker-операций в этой версии.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "0.1.0"
SUPERVISOR = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
PORT = int(os.environ.get("CONTROL_PORT", "8099"))
ACTIONS = {"start", "stop", "restart"}
SLUG_RE = re.compile(r"^[a-z0-9_]{1,64}$")


# --- Supervisor API --------------------------------------------------------

def supervisor(path, method="GET", payload=None, timeout=60):
    """Возвращает (status, data). data — dict/list для JSON, иначе str."""
    headers = {"Authorization": "Bearer %s" % TOKEN}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(SUPERVISOR + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(raw or b"{}")
                except ValueError:
                    return resp.status, {"raw": raw.decode("utf-8", "replace")}
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}
    except Exception as exc:  # сеть, таймаут, DNS
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def meminfo():
    vals = {}
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                vals[key.strip()] = int(rest.strip().split()[0])
    except Exception:
        return {}
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    stotal = vals.get("SwapTotal", 0)
    sfree = vals.get("SwapFree", 0)
    return {
        "total_mb": round(total / 1024),
        "used_mb": round((total - avail) / 1024),
        "free_mb": round(avail / 1024),
        "swap_total_mb": round(stotal / 1024),
        "swap_used_mb": round((stotal - sfree) / 1024),
    }


def collect_state():
    state = {"version": VERSION, "errors": []}

    status, data = supervisor("/host/info")
    if status == 200 and isinstance(data, dict):
        state["host"] = data.get("data", {})
    else:
        state["host"] = {}
        state["errors"].append("host/info: %s" % status)

    status, data = supervisor("/supervisor/info")
    if status == 200 and isinstance(data, dict):
        state["supervisor"] = data.get("data", {})
    else:
        state["supervisor"] = {}
        state["errors"].append("supervisor/info: %s" % status)

    status, data = supervisor("/core/info")
    if status == 200 and isinstance(data, dict):
        state["core"] = data.get("data", {})
    else:
        state["core"] = {}
        state["errors"].append("core/info: %s" % status)

    addons = []
    status, data = supervisor("/addons")
    if status == 200 and isinstance(data, dict):
        for item in (data.get("data", {}) or {}).get("addons", []):
            if not item.get("version"):
                continue  # не установлен
            addons.append({
                "slug": item.get("slug", ""),
                "name": item.get("name", item.get("slug", "")),
                "version": item.get("version"),
                "version_latest": item.get("version_latest"),
                "state": item.get("state", "unknown"),
                "boot": item.get("boot", ""),
                "update_available": bool(item.get("update_available")),
                "description": item.get("description", ""),
                "icon": bool(item.get("icon")),
            })
    else:
        state["errors"].append("addons: %s" % status)
    addons.sort(key=lambda a: a["name"].lower())
    state["addons"] = addons

    backups = []
    status, data = supervisor("/backups")
    if status == 200 and isinstance(data, dict):
        for item in (data.get("data", {}) or {}).get("backups", []):
            backups.append({
                "slug": item.get("slug", ""),
                "name": item.get("name", ""),
                "date": item.get("date", ""),
                "type": item.get("type", ""),
                "size_mb": round((item.get("size_bytes") or 0) / 1048576, 1),
            })
    else:
        state["errors"].append("backups: %s" % status)
    backups.sort(key=lambda b: b["date"], reverse=True)
    state["backups"] = backups[:12]

    state["mem"] = meminfo()
    try:
        state["loadavg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        state["loadavg"] = []
    return state


# --- HTTP ------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HA Control</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#111318; color:#e6e8ee;
         font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; }
  header { padding:18px 20px 10px; }
  h1 { margin:0; font-size:20px; font-weight:600; letter-spacing:.2px; }
  .sub { color:#8b93a7; font-size:12px; margin-top:4px; }
  nav { display:flex; gap:6px; padding:0 20px; border-bottom:1px solid #232838; }
  nav button { background:none; border:0; color:#8b93a7; padding:10px 14px; cursor:pointer;
               font-size:13px; border-bottom:2px solid transparent; }
  nav button.active { color:#e6e8ee; border-bottom-color:#3d8bfd; }
  main { padding:18px 20px 40px; max-width:1100px; }
  .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); }
  .card { background:#171b23; border:1px solid #232838; border-radius:14px; padding:14px 16px; }
  .card h3 { margin:0 0 4px; font-size:14px; font-weight:600; }
  .muted { color:#8b93a7; font-size:12px; }
  .row { display:flex; align-items:center; justify-content:space-between; gap:10px; }
  .badge { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid transparent; }
  .started { background:#12351f; color:#5fd68a; border-color:#1d5c33; }
  .stopped { background:#2a2f3a; color:#a9b1c4; border-color:#3a4150; }
  .error   { background:#3a1a1d; color:#ff8b93; border-color:#5c252a; }
  .unknown { background:#2a2f3a; color:#a9b1c4; border-color:#3a4150; }
  .actions { display:flex; gap:6px; margin-top:12px; flex-wrap:wrap; }
  button.btn { background:#232838; color:#e6e8ee; border:1px solid #2f3648; border-radius:9px;
               padding:6px 11px; font-size:12px; cursor:pointer; }
  button.btn:hover { background:#2b3142; }
  button.btn.primary { background:#1f4fd8; border-color:#2f63f0; }
  button.btn.primary:hover { background:#2a5be6; }
  button.btn.danger { color:#ff9aa2; }
  button.btn:disabled { opacity:.45; cursor:default; }
  .bar { height:8px; background:#232838; border-radius:99px; overflow:hidden; margin-top:8px; }
  .bar > i { display:block; height:100%; background:#3d8bfd; }
  .bar.warn > i { background:#e0a13a; }
  .bar.crit > i { background:#e2565f; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:9px 8px; border-bottom:1px solid #232838; font-size:13px; }
  th { color:#8b93a7; font-weight:500; font-size:12px; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.65); display:none;
           align-items:center; justify-content:center; padding:20px; z-index:50; }
  .modal.open { display:flex; }
  .modal .box { background:#12151c; border:1px solid #232838; border-radius:14px;
                width:100%; max-width:900px; max-height:85vh; display:flex; flex-direction:column; }
  .modal .head { display:flex; justify-content:space-between; align-items:center;
                 padding:12px 16px; border-bottom:1px solid #232838; }
  pre { margin:0; padding:14px 16px; overflow:auto; font:12px/1.5 ui-monospace,Consolas,monospace;
        color:#c8cede; white-space:pre-wrap; word-break:break-word; }
  .toast { position:fixed; right:18px; bottom:18px; background:#232838; color:#e6e8ee;
           border:1px solid #2f3648; border-radius:10px; padding:10px 14px; font-size:13px;
           opacity:0; transition:opacity .2s; pointer-events:none; max-width:380px; }
  .toast.show { opacity:1; }
  .toast.err { background:#3a1a1d; border-color:#5c252a; color:#ffb3b9; }
  .hint { color:#8b93a7; font-size:12px; margin:0 0 14px; }
</style>
</head>
<body>
<header>
  <h1>HA Control</h1>
  <div class="sub" id="sub">загрузка…</div>
</header>
<nav>
  <button data-tab="apps" class="active">Аддоны</button>
  <button data-tab="system">Система</button>
  <button data-tab="backups">Бэкапы</button>
</nav>
<main>
  <section id="tab-apps">
    <p class="hint">Управление установленными аддонами. «Стоп» и «Рестарт» применяются сразу.</p>
    <div class="grid" id="apps"></div>
  </section>
  <section id="tab-system" style="display:none">
    <div class="grid" id="system"></div>
  </section>
  <section id="tab-backups" style="display:none">
    <div class="row" style="margin-bottom:12px">
      <p class="hint" style="margin:0">Полный бэкап создаётся на малине и занимает 1–3 минуты.</p>
      <button class="btn primary" id="mkbackup">Создать полный бэкап</button>
    </div>
    <div class="card"><table id="backups"><tbody></tbody></table></div>
  </section>
</main>
<div class="modal" id="modal">
  <div class="box">
    <div class="head"><b id="modal-title">Логи</b>
      <button class="btn" onclick="closeModal()">Закрыть</button></div>
    <pre id="modal-body">…</pre>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const BASE = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
let STATE = null;

function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toast(msg, isErr){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(() => t.className = 'toast', 4000);
}
function badge(state){
  const s = ['started','stopped','error'].includes(state) ? state : 'unknown';
  const label = {started:'работает', stopped:'остановлен', error:'ошибка', unknown:'неизвестно'}[s];
  return '<span class="badge ' + s + '">' + label + '</span>';
}
function bar(pct, cls){
  const c = pct >= 90 ? 'crit' : (pct >= 75 ? 'warn' : '');
  return '<div class="bar ' + c + '"><i style="width:' + Math.min(100, pct) + '%"></i></div>';
}
function fmtDate(s){ try { return new Date(s).toLocaleString('ru-RU'); } catch(e){ return s; } }

function renderApps(){
  const box = document.getElementById('apps');
  box.innerHTML = STATE.addons.map(a => {
    const upd = a.update_available && a.version_latest
      ? '<div class="muted" style="color:#e0a13a;margin-top:6px">Доступно обновление: ' + esc(a.version_latest) + '</div>' : '';
    return '<div class="card">' +
      '<div class="row"><h3>' + esc(a.name) + '</h3>' + badge(a.state) + '</div>' +
      '<div class="muted">' + esc(a.slug) + ' · v' + esc(a.version) + '</div>' +
      upd +
      '<div class="actions">' +
        (a.state === 'started'
          ? '<button class="btn" onclick="act(\'' + a.slug + '\',\'stop\')">Стоп</button>' +
            '<button class="btn" onclick="act(\'' + a.slug + '\',\'restart\')">Рестарт</button>'
          : '<button class="btn primary" onclick="act(\'' + a.slug + '\',\'start\')">Запустить</button>') +
        '<button class="btn" onclick="logs(\'' + a.slug + '\')">Логи</button>' +
      '</div></div>';
  }).join('') || '<div class="card muted">Нет установленных аддонов</div>';
}

function renderSystem(){
  const h = STATE.host || {}, s = STATE.supervisor || {}, c = STATE.core || {}, m = STATE.mem || {};
  const memPct = m.total_mb ? Math.round(100 * m.used_mb / m.total_mb) : 0;
  const diskPct = h.disk_total ? Math.round(100 * h.disk_used / h.disk_total) : 0;
  const swapPct = m.swap_total_mb ? Math.round(100 * m.swap_used_mb / m.swap_total_mb) : 0;
  document.getElementById('system').innerHTML =
    '<div class="card"><h3>Хост</h3>' +
      '<div class="muted">' + esc(h.operating_system || '—') + '</div>' +
      '<div class="muted">' + esc(h.hostname || '') + ' · ' + esc(h.kernel || '') + '</div>' +
      '<div class="muted">' + esc(h.timezone || '') + '</div>' +
    '</div>' +
    '<div class="card"><h3>Память</h3>' +
      '<div class="muted">' + (m.used_mb||0) + ' / ' + (m.total_mb||0) + ' МБ (' + memPct + '%)</div>' +
      bar(memPct) +
      '<div class="muted" style="margin-top:8px">swap: ' + (m.swap_used_mb||0) + ' / ' + (m.swap_total_mb||0) + ' МБ (' + swapPct + '%)</div>' +
      bar(swapPct) +
    '</div>' +
    '<div class="card"><h3>Диск</h3>' +
      '<div class="muted">' + (h.disk_used||0) + ' / ' + (h.disk_total||0) + ' ГБ (' + diskPct + '%)</div>' +
      bar(diskPct) +
      '<div class="muted" style="margin-top:8px">свободно: ' + (h.disk_free||0) + ' ГБ</div>' +
    '</div>' +
    '<div class="card"><h3>Supervisor</h3>' +
      '<div class="muted">v' + esc(s.version || '—') + ' · ' + esc(s.channel || '') + ' · ' + esc(s.arch || '') + '</div>' +
      '<div class="muted">healthy: ' + (s.healthy ? 'да' : 'нет') + ' · supported: ' + (s.supported ? 'да' : 'нет') + '</div>' +
    '</div>' +
    '<div class="card"><h3>Home Assistant</h3>' +
      '<div class="muted">v' + esc(c.version || '—') + ' · ' + esc(c.machine || '') + '</div>' +
      (c.update_available ? '<div class="muted" style="color:#e0a13a">доступно обновление</div>' : '') +
    '</div>' +
    '<div class="card"><h3>Нагрузка</h3>' +
      '<div class="muted">' + (STATE.loadavg && STATE.loadavg.length ? STATE.loadavg.join('  ') : '—') + '</div>' +
    '</div>';
}

function renderBackups(){
  const tb = document.querySelector('#backups tbody');
  tb.innerHTML = '<tr><th>Имя</th><th>Дата</th><th>Тип</th><th>Размер</th></tr>' +
    (STATE.backups.map(b =>
      '<tr><td>' + esc(b.name) + '</td><td>' + esc(fmtDate(b.date)) + '</td>' +
      '<td>' + esc(b.type) + '</td><td>' + esc(b.size_mb) + ' МБ</td></tr>').join('') ||
      '<tr><td colspan="4" class="muted">Бэкапов нет</td></tr>');
}

function render(){
  document.getElementById('sub').textContent =
    'Supervisor ' + ((STATE.supervisor||{}).version || '?') +
    ' · HA ' + ((STATE.core||{}).version || '?') +
    ' · ' + ((STATE.host||{}).operating_system || '') +
    (STATE.errors && STATE.errors.length ? ' · ошибки: ' + STATE.errors.join(', ') : '');
  renderApps(); renderSystem(); renderBackups();
}

async function load(){
  try {
    const r = await fetch(BASE + 'api/state', {cache:'no-store'});
    STATE = await r.json();
    render();
  } catch(e) { toast('Не удалось получить состояние: ' + e, true); }
}

async function act(slug, action){
  if (action !== 'start' && !confirm('Выполнить «' + action + '» для ' + slug + '?')) return;
  toast('Выполняю ' + action + '…');
  try {
    const r = await fetch(BASE + 'api/apps/' + encodeURIComponent(slug) + '/' + action, {method:'POST'});
    const j = await r.json();
    if (!r.ok) throw new Error(j.message || j.error || r.status);
    toast('Готово: ' + action);
  } catch(e) { toast('Ошибка: ' + e.message, true); }
  setTimeout(load, 1200);
}

async function logs(slug, name){
  document.getElementById('modal-title').textContent = 'Логи: ' + name;
  document.getElementById('modal-body').textContent = 'загрузка…';
  document.getElementById('modal').classList.add('open');
  try {
    const r = await fetch(BASE + 'api/apps/' + encodeURIComponent(slug) + '/logs');
    const t = await r.text();
    document.getElementById('modal-body').textContent = t || '(пусто)';
  } catch(e) {
    document.getElementById('modal-body').textContent = 'Ошибка: ' + e;
  }
}
function closeModal(){ document.getElementById('modal').classList.remove('open'); }

document.getElementById('mkbackup').onclick = async () => {
  if (!confirm('Создать полный бэкап? Это займёт 1–3 минуты.')) return;
  toast('Создаю бэкап…');
  try {
    const r = await fetch(BASE + 'api/backups', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: 'manual_' + new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.message || j.error || r.status);
    toast('Бэкап создан');
  } catch(e) { toast('Ошибка: ' + e.message, true); }
  load();
};

document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  ['apps','system','backups'].forEach(t => {
    document.getElementById('tab-' + t).style.display = (t === b.dataset.tab) ? '' : 'none';
  });
});
document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

load();
setInterval(load, 15000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "HAControl/" + VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _path(self):
        """Путь запроса без префикса ingress (/api/hassio_ingress/<token>)."""
        path = urlparse(self.path).path or "/"
        m = re.match(r"^/api/hassio_ingress/[^/]+(/.*)?$", path)
        if m:
            path = m.group(1) or "/"
        return path

    def do_GET(self):
        path = self._path()
        if not path.startswith("/api/"):
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if path == "/api/health":
            return self._json(200, {"ok": True, "version": VERSION})

        if path == "/api/state":
            return self._json(200, collect_state())

        m = re.match(r"^/api/apps/([a-z0-9_]+)/logs$", path)
        if m:
            slug = m.group(1)
            qs = parse_qs(urlparse(self.path).query)
            lines = qs.get("lines", ["200"])[0]
            status, data = supervisor("/addons/%s/logs" % slug, timeout=30)
            if status != 200:
                return self._json(status or 502, {"error": "logs: %s" % status, "detail": data})
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            if isinstance(data, dict) and isinstance(data.get("data"), str):
                text = data["data"]
            try:
                n = max(10, min(2000, int(lines)))
            except ValueError:
                n = 200
            return self._send(200, "\n".join(text.splitlines()[-n:]),
                              "text/plain; charset=utf-8")

        return self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid json"})

        m = re.match(r"^/api/apps/([a-z0-9_]+)/(start|stop|restart)$", path)
        if m:
            slug, action = m.group(1), m.group(2)
            if not SLUG_RE.match(slug) or action not in ACTIONS:
                return self._json(400, {"error": "bad request"})
            status, data = supervisor("/addons/%s/%s" % (slug, action), method="POST", timeout=180)
            if status != 200:
                return self._json(status or 502, {"error": "action failed",
                                                  "message": _msg(data), "detail": data})
            return self._json(200, {"ok": True, "slug": slug, "action": action})

        if path == "/api/backups":
            name = str(body.get("name") or "").strip()[:64] or "manual"
            status, data = supervisor("/backups", method="POST",
                                      payload={"name": name}, timeout=1800)
            if status != 200:
                return self._json(status or 502, {"error": "backup failed",
                                                  "message": _msg(data), "detail": data})
            return self._json(200, {"ok": True, "name": name})

        return self._json(404, {"error": "not found", "path": path})


def _msg(data):
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or "")[:300]
    return str(data)[:300]


def main():
    if not TOKEN:
        print("FATAL: SUPERVISOR_TOKEN is not set (add-on must have hassio_api: true)", flush=True)
        sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("HA Control %s listening on 0.0.0.0:%d" % (VERSION, PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
