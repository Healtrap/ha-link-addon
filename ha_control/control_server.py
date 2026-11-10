#!/usr/bin/env python3
"""HA Control â€” Ð²ÐµÐ±-Ð¿Ð°Ð½ÐµÐ»ÑŒ ÑƒÐ¿Ñ€Ð°Ð²Ð»ÐµÐ½Ð¸Ñ Home Assistant (v0.2.0).

Ð Ð°Ð±Ð¾Ñ‚Ð°ÐµÑ‚ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ñ‡ÐµÑ€ÐµÐ· ingress Supervisor (Ð¾Ñ‚Ð´ÐµÐ»ÑŒÐ½Ð¾Ð³Ð¾ Ð¿Ð¾Ñ€Ñ‚Ð° Ð½Ð°Ñ€ÑƒÐ¶Ñƒ Ð½ÐµÑ‚).
ÐŸÑ€Ð°Ð²Ð°: hassio_api + hassio_role=admin â€” Ð°Ð´Ð´Ð¾Ð½Ñ‹ (ÑƒÑÑ‚Ð°Ð½Ð¾Ð²ÐºÐ°/Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ/ÑƒÐ´Ð°Ð»ÐµÐ½Ð¸Ðµ),
Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½, Ð»Ð¾Ð³Ð¸, Ð±ÑÐºÐ°Ð¿Ñ‹ (ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ðµ/ÑÐºÐ°Ñ‡Ð¸Ð²Ð°Ð½Ð¸Ðµ/ÑƒÐ´Ð°Ð»ÐµÐ½Ð¸Ðµ), Ñ€ÐµÑÑ‚Ð°Ñ€Ñ‚ HA Ð¸ Ñ€ÐµÐ±ÑƒÑ‚ Ñ…Ð¾ÑÑ‚Ð°,
Ñ„Ð°Ð¹Ð»Ñ‹ /config Ð¸ /share. docker_api Ð½Ðµ Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ÑÑ.
"""

import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

VERSION = "0.3.1"
SUPERVISOR = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
PORT = int(os.environ.get("CONTROL_PORT", "8099"))

APP_ACTIONS = {"start", "stop", "restart", "update", "uninstall", "install"}
CORE_ACTIONS = {"start", "stop", "restart", "update"}
HOST_ACTIONS = {"reboot", "shutdown"}
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
FILE_ROOTS = tuple(x for x in os.environ.get(
    "CONTROL_FILE_ROOTS",
    "/config|/share|/media|/ssl|/backup|/app_configs|/local_apps|/addon_configs").split("|") if x)
MAX_WRITE = 4 * 1024 * 1024
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
CHUNK = 64 * 1024


# --- Supervisor API --------------------------------------------------------

def supervisor(path, method="GET", payload=None, timeout=60, raw=False):
    """Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ (status, data). data â€” dict/list Ð´Ð»Ñ JSON, bytes Ð´Ð»Ñ raw, Ð¸Ð½Ð°Ñ‡Ðµ str."""
    headers = {"Authorization": "Bearer %s" % TOKEN}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(SUPERVISOR + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if raw:
                return resp.status, body
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(body or b"{}")
                except ValueError:
                    return resp.status, {"raw": body.decode("utf-8", "replace")}
            return resp.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if raw:
            return exc.code, body
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body.decode("utf-8", "replace")}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def _msg(data):
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data.get("raw") or "")[:300]
    return str(data)[:300]


def meminfo():
    vals = {}
    try:
        with open("/proc/meminfo") as fh:
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


def temperature():
    for path in ("/sys/class/thermal/thermal_zone0/temp", "/sys/devices/virtual/thermal/thermal_zone0/temp"):
        try:
            with open(path) as fh:
                return round(int(fh.read().strip()) / 1000.0, 1)
        except Exception:
            continue
    return None


def _items(status, data, key):
    """Ð”Ð¾ÑÑ‚Ð°Ñ‘Ñ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð¸Ð· Ð¾Ñ‚Ð²ÐµÑ‚Ð° Supervisor, Ð¿Ð¾Ð½Ð¸Ð¼Ð°Ñ Ð¾Ð±Ðµ Ñ„Ð¾Ñ€Ð¼Ñ‹: list Ð¸ {key: list}."""
    if status != 200 or not isinstance(data, dict):
        return None
    payload = data.get("data")
    if isinstance(payload, dict):
        payload = payload.get(key, [])
    if not isinstance(payload, list):
        return None
    return payload


def collect_state():
    state = {"version": VERSION, "errors": []}

    for key, path in (("host", "/host/info"), ("supervisor", "/supervisor/info"), ("core", "/core/info")):
        status, data = supervisor(path, timeout=20)
        if status == 200 and isinstance(data, dict):
            state[key] = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        else:
            state[key] = {}
            state["errors"].append("%s: %s" % (path, status))

    addons = []
    status, data = supervisor("/addons", timeout=30)
    items = _items(status, data, "addons")
    if items is None:
        state["errors"].append("/addons: %s" % status)
    for item in items or []:
        if not isinstance(item, dict) or not item.get("version"):
            continue
        addons.append({
            "slug": item.get("slug", ""),
            "name": item.get("name", item.get("slug", "")),
            "version": item.get("version"),
            "version_latest": item.get("version_latest"),
            "state": item.get("state", "unknown"),
            "boot": item.get("boot", ""),
            "update_available": bool(item.get("update_available")),
            "description": item.get("description", ""),
        })
    addons.sort(key=lambda a: a["name"].lower())
    state["addons"] = addons

    backups = []
    status, data = supervisor("/backups", timeout=30)
    items = _items(status, data, "backups")
    if items is None:
        state["errors"].append("/backups: %s" % status)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        backups.append({
            "slug": item.get("slug", ""),
            "name": item.get("name", ""),
            "date": item.get("date", ""),
            "type": item.get("type", ""),
            "size_mb": round((item.get("size_bytes") or 0) / 1048576, 1),
        })
    backups.sort(key=lambda b: b["date"], reverse=True)
    state["backups"] = backups[:15]

    repos = []
    status, data = supervisor("/store/repositories", timeout=20)
    items = _items(status, data, "repositories")
    if items is None:  # ÑÑ‚Ð°Ñ€Ñ‹Ðµ Ð²ÐµÑ€ÑÐ¸Ð¸: /store
        status2, data2 = supervisor("/store", timeout=30)
        items = _items(status2, data2, "repositories")
    if items is None:
        state["errors"].append("/store/repositories: %s" % status)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        repos.append({"slug": item.get("slug", ""), "name": item.get("name", ""),
                      "url": item.get("url") or item.get("source", "")})
    state["repositories"] = repos

    try:
        state["mem"] = meminfo()
    except Exception:
        state["mem"] = {}
    try:
        state["temp_c"] = temperature()
    except Exception:
        state["temp_c"] = None
    try:
        state["loadavg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        state["loadavg"] = []
    state["file_roots"] = [r for r in FILE_ROOTS if os.path.isdir(r)] or list(FILE_ROOTS)
    return state


# --- files -----------------------------------------------------------------

def safe_path(raw):
    """ÐŸÑƒÑ‚ÑŒ Ð²Ð½ÑƒÑ‚Ñ€Ð¸ Ñ€Ð°Ð·Ñ€ÐµÑˆÑ‘Ð½Ð½Ñ‹Ñ… ÐºÐ¾Ñ€Ð½ÐµÐ¹. Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ (real, err)."""
    if not raw:
        return None, "Ð¿ÑƒÑÑ‚Ð¾Ð¹ Ð¿ÑƒÑ‚ÑŒ"
    full = os.path.realpath(raw)
    if not any(full == root or full.startswith(root + os.sep) for root in FILE_ROOTS):
        return None, "Ð¿ÑƒÑ‚ÑŒ Ð²Ð½Ðµ Ñ€Ð°Ð·Ñ€ÐµÑˆÑ‘Ð½Ð½Ñ‹Ñ… ÐºÐ°Ñ‚Ð°Ð»Ð¾Ð³Ð¾Ð²"
    return full, None


def file_listing(path):
    real, err = safe_path(path)
    if err:
        return None, err
    if not os.path.isdir(real):
        return None, "Ð½Ðµ ÐºÐ°Ñ‚Ð°Ð»Ð¾Ð³"
    items = []
    try:
        for name in sorted(os.listdir(real), key=lambda s: s.lower()):
            full = os.path.join(real, name)
            try:
                st = os.stat(full)
                isdir = os.path.isdir(full)
                items.append({"name": name, "dir": isdir, "size": 0 if isdir else st.st_size,
                              "mtime": int(st.st_mtime)})
            except OSError:
                continue
    except PermissionError:
        return None, "Ð½ÐµÑ‚ Ð´Ð¾ÑÑ‚ÑƒÐ¿Ð°"
    parent = os.path.dirname(real)
    if not any(parent == r or parent.startswith(r + os.sep) for r in FILE_ROOTS):
        parent = None
    return {"path": real, "parent": parent, "items": items}, None


def file_read(path, limit=512 * 1024):
    real, err = safe_path(path)
    if err:
        return None, err
    if not os.path.isfile(real):
        return None, "Ð½Ðµ Ñ„Ð°Ð¹Ð»"
    size = os.path.getsize(real)
    with open(real, "rb") as fh:
        chunk = fh.read(limit)
    if b"\x00" in chunk[:4096]:
        return None, "Ð±Ð¸Ð½Ð°Ñ€Ð½Ñ‹Ð¹ Ñ„Ð°Ð¹Ð» (%d Ð±Ð°Ð¹Ñ‚)" % size
    return {"path": real, "size": size, "content": chunk.decode("utf-8", "replace")}, None


def file_write(path, content):
    real, err = safe_path(path)
    if err:
        return err
    if os.path.isdir(real):
        return "ÑÑ‚Ð¾ ÐºÐ°Ñ‚Ð°Ð»Ð¾Ð³"
    os.makedirs(os.path.dirname(real) or "/", exist_ok=True)
    if os.path.exists(real):
        shutil.copy2(real, real + ".bak")
    with open(real, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return None


def file_delete(path):
    real, err = safe_path(path)
    if err:
        return err
    if real in FILE_ROOTS:
        return "Ð½ÐµÐ»ÑŒÐ·Ñ ÑƒÐ´Ð°Ð»Ð¸Ñ‚ÑŒ ÐºÐ¾Ñ€ÐµÐ½ÑŒ"
    if os.path.isdir(real):
        shutil.rmtree(real)
    else:
        os.remove(real)
    return None


def file_mkdir(path):
    real, err = safe_path(path)
    if err:
        return err
    os.makedirs(real, exist_ok=True)
    return None


def file_rename(path, name):
    real, err = safe_path(path)
    if err:
        return err
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return "Ð½ÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ð¾Ðµ Ð¸Ð¼Ñ"
    dest = os.path.join(os.path.dirname(real), name)
    dest_real, err = safe_path(dest)
    if err:
        return err
    if os.path.exists(dest_real):
        return "Ñ‚Ð°ÐºÐ¾Ðµ Ð¸Ð¼Ñ ÑƒÐ¶Ðµ ÑÑƒÑ‰ÐµÑÑ‚Ð²ÑƒÐµÑ‚"
    os.rename(real, dest_real)
    return None


def file_upload(dir_path, name, length, stream):
    """ÐŸÐ¸ÑˆÐµÑ‚ Ð¿Ð¾Ñ‚Ð¾Ðº Ð² Ñ„Ð°Ð¹Ð» Ð¿Ð¾Ñ‚Ð¾ÐºÐ¾Ð²Ð¾, Ð±ÐµÐ· Ð·Ð°Ð³Ñ€ÑƒÐ·ÐºÐ¸ Ð² Ð¿Ð°Ð¼ÑÑ‚ÑŒ."""
    real, err = safe_path(dir_path)
    if err:
        return None, err
    if not os.path.isdir(real):
        return None, "Ð½Ðµ ÐºÐ°Ñ‚Ð°Ð»Ð¾Ð³"
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None, "Ð½ÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ð¾Ðµ Ð¸Ð¼Ñ Ñ„Ð°Ð¹Ð»Ð°"
    dest, err = safe_path(os.path.join(real, name))
    if err:
        return None, err
    if os.path.isdir(dest):
        return None, "ÑÑ‚Ð¾ ÐºÐ°Ñ‚Ð°Ð»Ð¾Ð³"
    if os.path.exists(dest):
        shutil.copy2(dest, dest + ".bak")
    remaining = length
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as fh:
            while remaining > 0:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            os.remove(tmp)
            return None, "Ð¾Ð±Ð¾Ñ€Ð²Ð°Ð½Ð½Ð°Ñ Ð·Ð°Ð³Ñ€ÑƒÐ·ÐºÐ° (Ð½Ðµ Ñ…Ð²Ð°Ñ‚Ð¸Ð»Ð¾ %d Ð±Ð°Ð¹Ñ‚)" % remaining
        os.replace(tmp, dest)
    except Exception as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None, "%s: %s" % (type(exc).__name__, exc)
    return dest, None


# --- UI --------------------------------------------------------------------

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
  header { padding:18px 20px 10px; display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; }
  h1 { margin:0; font-size:20px; font-weight:600; letter-spacing:.2px; }
  .sub { color:#8b93a7; font-size:12px; margin-top:4px; }
  nav { display:flex; gap:4px; padding:0 20px; border-bottom:1px solid #232838; overflow-x:auto; }
  nav button { background:none; border:0; color:#8b93a7; padding:10px 12px; cursor:pointer;
               font-size:13px; border-bottom:2px solid transparent; white-space:nowrap; }
  nav button.active { color:#e6e8ee; border-bottom-color:#3d8bfd; }
  main { padding:18px 20px 60px; max-width:1180px; }
  .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); }
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
  button.btn.warn { color:#ffc46b; }
  button.btn.danger { color:#ff9aa2; }
  button.btn:disabled { opacity:.45; cursor:default; }
  .bar { height:8px; background:#232838; border-radius:99px; overflow:hidden; margin-top:8px; }
  .bar > i { display:block; height:100%; background:#3d8bfd; }
  .bar.warn > i { background:#e0a13a; }
  .bar.crit > i { background:#e2565f; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:9px 8px; border-bottom:1px solid #232838; font-size:13px; vertical-align:top; }
  th { color:#8b93a7; font-weight:500; font-size:12px; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.7); display:none;
           align-items:center; justify-content:center; padding:16px; z-index:50; }
  .modal.open { display:flex; }
  .modal .box { background:#12151c; border:1px solid #232838; border-radius:14px;
                width:100%; max-width:1000px; max-height:88vh; display:flex; flex-direction:column; }
  .modal .head { display:flex; justify-content:space-between; align-items:center; gap:10px;
                 padding:12px 16px; border-bottom:1px solid #232838; }
  pre { margin:0; padding:14px 16px; overflow:auto; font:12px/1.5 ui-monospace,Consolas,monospace;
        color:#c8cede; white-space:pre-wrap; word-break:break-word; }
  textarea { width:100%; min-height:220px; background:#0d1015; color:#c8cede; border:1px solid #232838;
             border-radius:10px; padding:12px; font:12px/1.5 ui-monospace,Consolas,monospace; resize:vertical; }
  input[type=text], select { background:#0d1015; color:#e6e8ee; border:1px solid #2f3648;
             border-radius:9px; padding:7px 10px; font-size:13px; width:100%; }
  .toast { position:fixed; right:18px; bottom:18px; background:#232838; color:#e6e8ee;
           border:1px solid #2f3648; border-radius:10px; padding:10px 14px; font-size:13px;
           opacity:0; transition:opacity .2s; pointer-events:none; max-width:420px; z-index:60; }
  .toast.show { opacity:1; }
  .toast.err { background:#3a1a1d; border-color:#5c252a; color:#ffb3b9; }
  .hint { color:#8b93a7; font-size:12px; margin:0 0 12px; }
  .files { display:grid; grid-template-columns:minmax(260px,1fr) minmax(320px,1.6fr); gap:12px; }
  @media (max-width:820px){ .files { grid-template-columns:1fr; } }
  .flist { max-height:60vh; overflow:auto; }
  .flist div { padding:6px 8px; border-radius:8px; cursor:pointer; display:flex; justify-content:space-between; gap:8px; }
  .flist div:hover { background:#1d2230; }
  .roots { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
  .chip { background:#1b2130; color:#9fb0cc; border:1px solid #2a3142; border-radius:999px;
          padding:4px 10px; font-size:11px; cursor:pointer; }
  .chip:hover { background:#243049; color:#e6e8ee; }
  .danger-zone { border-color:#5c252a; }
</style>
</head>
<body>
<header>
  <div><h1>HA Control</h1><div class="sub" id="sub">Ð·Ð°Ð³Ñ€ÑƒÐ·ÐºÐ°â€¦</div></div>
  <div style="display:flex;gap:6px;flex-wrap:wrap">
    <button class="btn" onclick="load()">ÐžÐ±Ð½Ð¾Ð²Ð¸Ñ‚ÑŒ</button>
    <button class="btn warn" onclick="coreAct('restart')">Ð ÐµÑÑ‚Ð°Ñ€Ñ‚ HA</button>
  </div>
</header>
<nav>
  <button data-tab="apps" class="active">ÐÐ´Ð´Ð¾Ð½Ñ‹</button>
  <button data-tab="store">ÐœÐ°Ð³Ð°Ð·Ð¸Ð½</button>
  <button data-tab="files">Ð¤Ð°Ð¹Ð»Ñ‹</button>
  <button data-tab="logs">Ð›Ð¾Ð³Ð¸</button>
  <button data-tab="backups">Ð‘ÑÐºÐ°Ð¿Ñ‹</button>
  <button data-tab="system">Ð¡Ð¸ÑÑ‚ÐµÐ¼Ð°</button>
</nav>
<main>
  <section id="tab-apps">
    <p class="hint">Ð£ÑÑ‚Ð°Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ‹Ðµ Ð°Ð´Ð´Ð¾Ð½Ñ‹. Â«Ð¡Ñ‚Ð¾Ð¿Â» Ð¸ Â«Ð ÐµÑÑ‚Ð°Ñ€Ñ‚Â» Ð¿Ñ€Ð¸Ð¼ÐµÐ½ÑÑŽÑ‚ÑÑ ÑÑ€Ð°Ð·Ñƒ, ÑƒÐ´Ð°Ð»ÐµÐ½Ð¸Ðµ â€” Ñ Ð¿Ð¾Ð´Ñ‚Ð²ÐµÑ€Ð¶Ð´ÐµÐ½Ð¸ÐµÐ¼.</p>
    <div class="grid" id="apps"></div>
  </section>

  <section id="tab-store" style="display:none">
    <div class="card" style="margin-bottom:12px">
      <div class="row"><h3>Ð ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ð¸Ð¸</h3><button class="btn" onclick="storeReload()">ÐŸÐµÑ€ÐµÐ·Ð°Ð³Ñ€ÑƒÐ·Ð¸Ñ‚ÑŒ Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½</button></div>
      <div id="repos" style="margin-top:10px"></div>
      <div class="row" style="margin-top:12px">
        <input type="text" id="repo-url" placeholder="https://github.com/user/repo">
        <button class="btn primary" onclick="repoAdd()">Ð”Ð¾Ð±Ð°Ð²Ð¸Ñ‚ÑŒ</button>
      </div>
      <div class="muted" style="margin-top:8px">ÐŸÐ¾ÑÐ»Ðµ Ð´Ð¾Ð±Ð°Ð²Ð»ÐµÐ½Ð¸Ñ Ð½Ð°Ð¶Ð¼Ð¸ Â«ÐŸÐµÑ€ÐµÐ·Ð°Ð³Ñ€ÑƒÐ·Ð¸Ñ‚ÑŒ Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½Â». Ð£ÑÑ‚Ð°Ð½Ð¾Ð²ÐºÐ° â€” Ð½Ð° Ð²ÐºÐ»Ð°Ð´ÐºÐµ Â«ÐÐ´Ð´Ð¾Ð½Ñ‹Â» (ÐºÐ½Ð¾Ð¿ÐºÐ° Â«Ð£ÑÑ‚Ð°Ð½Ð¾Ð²Ð¸Ñ‚ÑŒÂ»).</div>
    </div>
    <div class="card"><table id="store-apps"><tbody></tbody></table></div>
  </section>

  <section id="tab-files" style="display:none">
    <p class="hint">Ð’ÑÐµ ÐºÐ°Ñ‚Ð°Ð»Ð¾Ð³Ð¸ HA: <b>/config</b>, <b>/share</b>, <b>/media</b>, <b>/ssl</b>,
      <b>/backup</b>, <b>/addon_configs</b>, <b>/local_apps</b>. ÐŸÑ€Ð¸ Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿Ð¸ÑÐ¸ ÑÐ¾Ð·Ð´Ð°Ñ‘Ñ‚ÑÑ ÐºÐ¾Ð¿Ð¸Ñ <code>.bak</code>.</p>
    <div class="files">
      <div class="card">
        <div class="row" style="margin-bottom:8px;flex-wrap:wrap;gap:6px">
          <b id="fpath" style="font-size:12px;word-break:break-all">/config</b>
          <span style="display:flex;gap:6px">
            <button class="btn" onclick="fup()">Ð’Ð²ÐµÑ€Ñ…</button>
            <button class="btn primary" onclick="document.getElementById('fupload').click()">Ð—Ð°Ð³Ñ€ÑƒÐ·Ð¸Ñ‚ÑŒ</button>
            <input type="file" id="fupload" multiple style="display:none">
          </span>
        </div>
        <div class="roots" id="froots"></div>
        <div class="flist" id="flist"></div>
        <div class="row" style="margin-top:10px;gap:6px">
          <input type="text" id="fnew" placeholder="Ð¸Ð¼Ñ_Ñ„Ð°Ð¹Ð»Ð°.yaml">
          <button class="btn" onclick="fnewFile()">+ Ñ„Ð°Ð¹Ð»</button>
          <button class="btn" onclick="fnewDir()">+ Ð¿Ð°Ð¿ÐºÐ°</button>
        </div>
      </div>
      <div class="card">
        <div class="row" style="margin-bottom:8px;flex-wrap:wrap;gap:6px">
          <b id="fname" style="font-size:12px;word-break:break-all">Ñ„Ð°Ð¹Ð» Ð½Ðµ Ð²Ñ‹Ð±Ñ€Ð°Ð½</b>
          <span style="display:flex;gap:6px;flex-wrap:wrap">
            <a class="btn" id="fdl" style="display:none;text-decoration:none">Ð¡ÐºÐ°Ñ‡Ð°Ñ‚ÑŒ</a>
            <button class="btn" onclick="frename()">ÐŸÐµÑ€ÐµÐ¸Ð¼ÐµÐ½Ð¾Ð²Ð°Ñ‚ÑŒ</button>
            <button class="btn primary" onclick="fsave()">Ð¡Ð¾Ñ…Ñ€Ð°Ð½Ð¸Ñ‚ÑŒ</button>
          </span>
        </div>
        <textarea id="fcontent" spellcheck="false" placeholder="Ð’Ñ‹Ð±ÐµÑ€Ð¸ Ñ„Ð°Ð¹Ð» ÑÐ»ÐµÐ²Ð°"></textarea>
        <div class="row" style="margin-top:10px">
          <span class="muted" id="fmeta"></span>
          <button class="btn danger" onclick="fdel()">Ð£Ð´Ð°Ð»Ð¸Ñ‚ÑŒ</button>
        </div>
      </div>
    </div>
  </section>

  <section id="tab-logs" style="display:none">
    <div class="row" style="margin-bottom:12px;flex-wrap:wrap">
      <select id="logsrc" style="max-width:320px"></select>
      <span style="display:flex;gap:6px">
        <button class="btn primary" onclick="showLogs()">ÐŸÐ¾ÐºÐ°Ð·Ð°Ñ‚ÑŒ</button>
        <button class="btn" onclick="toggleFollow()" id="followbtn">ÐÐ²Ñ‚Ð¾Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ: Ð²Ñ‹ÐºÐ»</button>
      </span>
    </div>
    <div class="card"><pre id="logbody" style="max-height:70vh">Ð’Ñ‹Ð±ÐµÑ€Ð¸ Ð¸ÑÑ‚Ð¾Ñ‡Ð½Ð¸Ðº Ð¸ Ð½Ð°Ð¶Ð¼Ð¸ Â«ÐŸÐ¾ÐºÐ°Ð·Ð°Ñ‚ÑŒÂ».</pre></div>
  </section>

  <section id="tab-backups" style="display:none">
    <div class="row" style="margin-bottom:12px;flex-wrap:wrap">
      <p class="hint" style="margin:0">ÐŸÐ¾Ð»Ð½Ñ‹Ð¹ Ð±ÑÐºÐ°Ð¿ ÑÐ¾Ð·Ð´Ð°Ñ‘Ñ‚ÑÑ Ð½Ð° Ð¼Ð°Ð»Ð¸Ð½Ðµ 1â€“3 Ð¼Ð¸Ð½ÑƒÑ‚Ñ‹.</p>
      <button class="btn primary" onclick="mkBackup()">Ð¡Ð¾Ð·Ð´Ð°Ñ‚ÑŒ Ð¿Ð¾Ð»Ð½Ñ‹Ð¹ Ð±ÑÐºÐ°Ð¿</button>
    </div>
    <div class="card"><table id="backups"><tbody></tbody></table></div>
  </section>

  <section id="tab-system" style="display:none">
    <div class="grid" id="system"></div>
    <div class="card danger-zone" style="margin-top:12px">
      <h3>ÐžÐ¿Ð°ÑÐ½Ð°Ñ Ð·Ð¾Ð½Ð°</h3>
      <div class="muted">Ð”ÐµÐ¹ÑÑ‚Ð²Ð¸Ñ Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÑÑŽÑ‚ÑÑ Ð½Ð° Ñ…Ð¾ÑÑ‚Ðµ. Ð¢ÑƒÐ½Ð½ÐµÐ»ÑŒ <code>ha_link</code> Ð¿Ñ€Ð¸ Ñ€ÐµÐ±ÑƒÑ‚Ðµ Ð¿Ð¾Ð´Ð½Ð¸Ð¼ÐµÑ‚ÑÑ ÑÐ°Ð¼ (boot: auto).</div>
      <div class="actions">
        <button class="btn warn" onclick="hostAct('reboot')">ÐŸÐµÑ€ÐµÐ·Ð°Ð³Ñ€ÑƒÐ·Ð¸Ñ‚ÑŒ Ñ…Ð¾ÑÑ‚</button>
        <button class="btn danger" onclick="hostAct('shutdown')">Ð’Ñ‹ÐºÐ»ÑŽÑ‡Ð¸Ñ‚ÑŒ Ñ…Ð¾ÑÑ‚</button>
      </div>
    </div>
  </section>
</main>

<div class="modal" id="modal">
  <div class="box">
    <div class="head"><b id="modal-title">Ð›Ð¾Ð³Ð¸</b>
      <button class="btn" onclick="closeModal()">Ð—Ð°ÐºÑ€Ñ‹Ñ‚ÑŒ</button></div>
    <pre id="modal-body">â€¦</pre>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const BASE = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
let STATE = null, CURDIR = '/config', CURFILE = null, FOLLOW = null;

const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg, isErr){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(() => t.className = 'toast', 4500);
}
function badge(state){
  const s = ['started','stopped','error'].includes(state) ? state : 'unknown';
  const label = {started:'Ñ€Ð°Ð±Ð¾Ñ‚Ð°ÐµÑ‚', stopped:'Ð¾ÑÑ‚Ð°Ð½Ð¾Ð²Ð»ÐµÐ½', error:'Ð¾ÑˆÐ¸Ð±ÐºÐ°', unknown:'Ð½ÐµÐ¸Ð·Ð²ÐµÑÑ‚Ð½Ð¾'}[s];
  return '<span class="badge ' + s + '">' + label + '</span>';
}
function bar(pct){
  const c = pct >= 90 ? 'crit' : (pct >= 75 ? 'warn' : '');
  return '<div class="bar ' + c + '"><i style="width:' + Math.min(100, pct) + '%"></i></div>';
}
function fmtDate(s){ try { return new Date(s).toLocaleString('ru-RU'); } catch(e){ return s; } }
function fmtSize(b){ return b > 1048576 ? (b/1048576).toFixed(1)+' ÐœÐ‘' : Math.max(1, Math.round(b/1024))+' ÐšÐ‘'; }

async function api(path, opts){
  const r = await fetch(BASE + path, Object.assign({cache:'no-store'}, opts || {}));
  const ct = r.headers.get('content-type') || '';
  const body = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && (body.message || body.error)) || ('HTTP ' + r.status));
  return body;
}

function renderApps(){
  const box = document.getElementById('apps');
  box.innerHTML = (STATE.addons||[]).map(a => {
    const upd = a.update_available && a.version_latest
      ? '<div class="muted" style="color:#e0a13a;margin-top:6px">Ð”Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¾ Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ: ' + esc(a.version_latest) + '</div>' : '';
    const btns = [];
    if (a.state === 'started') {
      btns.push('<button class="btn" onclick="appAct(\'' + a.slug + '\',\'stop\')">Ð¡Ñ‚Ð¾Ð¿</button>');
      btns.push('<button class="btn" onclick="appAct(\'' + a.slug + '\',\'restart\')">Ð ÐµÑÑ‚Ð°Ñ€Ñ‚</button>');
    } else {
      btns.push('<button class="btn primary" onclick="appAct(\'' + a.slug + '\',\'start\')">Ð—Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚ÑŒ</button>');
    }
    if (a.update_available) btns.push('<button class="btn warn" onclick="appAct(\'' + a.slug + '\',\'update\')">ÐžÐ±Ð½Ð¾Ð²Ð¸Ñ‚ÑŒ</button>');
    btns.push('<button class="btn" onclick="openLogs(\'' + a.slug + '\')">Ð›Ð¾Ð³Ð¸</button>');
    btns.push('<button class="btn danger" onclick="appAct(\'' + a.slug + '\',\'uninstall\')">Ð£Ð´Ð°Ð»Ð¸Ñ‚ÑŒ</button>');
    return '<div class="card">' +
      '<div class="row"><h3>' + esc(a.name) + '</h3>' + badge(a.state) + '</div>' +
      '<div class="muted">' + esc(a.slug) + ' Â· v' + esc(a.version) + ' Â· boot: ' + esc(a.boot) + '</div>' +
      upd +
      '<div class="actions">' + btns.join('') + '</div></div>';
  }).join('') || '<div class="card muted">ÐÐµÑ‚ ÑƒÑÑ‚Ð°Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ‹Ñ… Ð°Ð´Ð´Ð¾Ð½Ð¾Ð²</div>';
}

function renderSystem(){
  const h = STATE.host||{}, s = STATE.supervisor||{}, c = STATE.core||{}, m = STATE.mem||{};
  const memPct = m.total_mb ? Math.round(100*m.used_mb/m.total_mb) : 0;
  const diskPct = h.disk_total ? Math.round(100*h.disk_used/h.disk_total) : 0;
  const swapPct = m.swap_total_mb ? Math.round(100*m.swap_used_mb/m.swap_total_mb) : 0;
  const temp = STATE.temp_c != null ? STATE.temp_c + ' Â°C' : 'â€”';
  document.getElementById('system').innerHTML =
    '<div class="card"><h3>Ð¥Ð¾ÑÑ‚</h3><div class="muted">' + esc(h.operating_system||'â€”') + '</div>' +
      '<div class="muted">' + esc(h.hostname||'') + ' Â· ' + esc(h.kernel||'') + '</div>' +
      '<div class="muted">Ñ‚ÐµÐ¼Ð¿ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð° CPU: ' + temp + '</div>' +
      '<div class="muted">' + esc(h.timezone||'') + ' Â· uptime: ' + (h.startup_time!=null?Math.round(h.startup_time)+' Ñ':'â€”') + '</div></div>' +
    '<div class="card"><h3>ÐŸÐ°Ð¼ÑÑ‚ÑŒ</h3><div class="muted">' + (m.used_mb||0) + ' / ' + (m.total_mb||0) + ' ÐœÐ‘ (' + memPct + '%)</div>' + bar(memPct) +
      '<div class="muted" style="margin-top:8px">swap: ' + (m.swap_used_mb||0) + ' / ' + (m.swap_total_mb||0) + ' ÐœÐ‘ (' + swapPct + '%)</div>' + bar(swapPct) + '</div>' +
    '<div class="card"><h3>Ð”Ð¸ÑÐº</h3><div class="muted">' + (h.disk_used||0) + ' / ' + (h.disk_total||0) + ' Ð“Ð‘ (' + diskPct + '%)</div>' + bar(diskPct) +
      '<div class="muted" style="margin-top:8px">ÑÐ²Ð¾Ð±Ð¾Ð´Ð½Ð¾: ' + (h.disk_free||0) + ' Ð“Ð‘</div></div>' +
    '<div class="card"><h3>Supervisor</h3><div class="muted">v' + esc(s.version||'â€”') + ' Â· ' + esc(s.channel||'') + ' Â· ' + esc(s.arch||'') + '</div>' +
      '<div class="muted">healthy: ' + (s.healthy?'Ð´Ð°':'Ð½ÐµÑ‚') + ' Â· supported: ' + (s.supported?'Ð´Ð°':'Ð½ÐµÑ‚') + '</div></div>' +
    '<div class="card"><h3>Home Assistant</h3><div class="muted">v' + esc(c.version||'â€”') + ' Â· ' + esc(c.machine||'') + '</div>' +
      (c.update_available?'<div class="muted" style="color:#e0a13a">Ð´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¾ Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ</div>':'') +
      '<div class="actions"><button class="btn warn" onclick="coreAct(\'update\')">ÐžÐ±Ð½Ð¾Ð²Ð¸Ñ‚ÑŒ HA</button></div></div>' +
    '<div class="card"><h3>ÐÐ°Ð³Ñ€ÑƒÐ·ÐºÐ°</h3><div class="muted">' + ((STATE.loadavg||[]).join('  ') || 'â€”') + '</div></div>';
}

function renderBackups(){
  const tb = document.querySelector('#backups tbody');
  tb.innerHTML = '<tr><th>Ð˜Ð¼Ñ</th><th>Ð”Ð°Ñ‚Ð°</th><th>Ð¢Ð¸Ð¿</th><th>Ð Ð°Ð·Ð¼ÐµÑ€</th><th></th></tr>' +
    ((STATE.backups||[]).map(b =>
      '<tr><td>' + esc(b.name) + '</td><td>' + esc(fmtDate(b.date)) + '</td><td>' + esc(b.type) + '</td><td>' + esc(b.size_mb) + ' ÐœÐ‘</td>' +
      '<td style="white-space:nowrap"><a class="btn" style="text-decoration:none" href="' + BASE + 'api/backups/' + encodeURIComponent(b.slug) + '/download">Ð¡ÐºÐ°Ñ‡Ð°Ñ‚ÑŒ</a> ' +
      '<button class="btn danger bdel" data-slug="' + esc(b.slug) + '" data-name="' + esc(b.name) + '">Ð£Ð´Ð°Ð»Ð¸Ñ‚ÑŒ</button></td></tr>'
    ).join('') || '<tr><td colspan="5" class="muted">Ð‘ÑÐºÐ°Ð¿Ð¾Ð² Ð½ÐµÑ‚</td></tr>');
  tb.querySelectorAll('.bdel').forEach(el => {
    el.onclick = () => delBackup(el.dataset.slug, el.dataset.name);
  });
}

function renderRepos(){
  document.getElementById('repos').innerHTML = (STATE.repositories||[]).map(r =>
    '<div class="row" style="padding:6px 0;border-bottom:1px solid #232838">' +
    '<div><b>' + esc(r.name||r.slug) + '</b><div class="muted">' + esc(r.url||r.slug) + '</div></div>' +
    '<button class="btn danger" onclick="repoDel(\'' + esc(r.slug) + '\')">Ð£Ð´Ð°Ð»Ð¸Ñ‚ÑŒ</button></div>'
  ).join('') || '<div class="muted">ÐÐµÑ‚ Ñ€ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ð¸ÐµÐ²</div>';

  const sel = document.getElementById('logsrc');
  const opts = ['<option value="core">HA Core</option>', '<option value="supervisor">Supervisor</option>']
    .concat((STATE.addons||[]).map(a => '<option value="' + a.slug + '">' + esc(a.name) + '</option>'));
  const prev = sel.value;
  sel.innerHTML = opts.join('');
  if (prev) sel.value = prev;
}

function render(){
  const s = STATE.supervisor||{}, c = STATE.core||{}, h = STATE.host||{};
  document.getElementById('sub').textContent =
    'HA ' + (c.version||'?') + ' Â· Supervisor ' + (s.version||'?') + ' Â· ' + (h.operating_system||'') +
    ' Â· Ð¿Ð°Ð½ÐµÐ»ÑŒ v' + (STATE.version||'?') +
    ((STATE.errors&&STATE.errors.length) ? ' Â· Ð¾ÑˆÐ¸Ð±ÐºÐ¸: ' + STATE.errors.join(', ') : '');
  renderApps(); renderSystem(); renderBackups(); renderRepos(); renderRoots();
}

async function load(){
  try { STATE = await api('api/state'); render(); }
  catch(e){ toast('ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¿Ð¾Ð»ÑƒÑ‡Ð¸Ñ‚ÑŒ ÑÐ¾ÑÑ‚Ð¾ÑÐ½Ð¸Ðµ: ' + e.message, true); }
}

async function appAct(slug, action){
  const names = {stop:'Ð¾ÑÑ‚Ð°Ð½Ð¾Ð²Ð¸Ñ‚ÑŒ', restart:'Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚ÑŒ', start:'Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚ÑŒ', update:'Ð¾Ð±Ð½Ð¾Ð²Ð¸Ñ‚ÑŒ', uninstall:'Ð£Ð”ÐÐ›Ð˜Ð¢Ð¬'};
  if (action !== 'start' && !confirm('Ð¢Ð¾Ñ‡Ð½Ð¾ ' + names[action] + ' ' + slug + '?')) return;
  if (action === 'uninstall' && !confirm('Ð£Ð´Ð°Ð»ÐµÐ½Ð¸Ðµ Ð½ÐµÐ¾Ð±Ñ€Ð°Ñ‚Ð¸Ð¼Ð¾. ÐŸÑ€Ð¾Ð´Ð¾Ð»Ð¶Ð¸Ñ‚ÑŒ?')) return;
  toast('Ð’Ñ‹Ð¿Ð¾Ð»Ð½ÑÑŽ: ' + names[action] + 'â€¦');
  try { await api('api/apps/' + encodeURIComponent(slug) + '/' + action, {method:'POST'}); toast('Ð“Ð¾Ñ‚Ð¾Ð²Ð¾: ' + names[action]); }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
  setTimeout(load, 1500);
}

async function coreAct(action){
  const label = {restart:'Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚ÑŒ Home Assistant', start:'Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚ÑŒ', stop:'ÐžÐ¡Ð¢ÐÐÐžÐ’Ð˜Ð¢Ð¬', update:'Ð¾Ð±Ð½Ð¾Ð²Ð¸Ñ‚ÑŒ Home Assistant'}[action];
  if (!confirm('Ð¢Ð¾Ñ‡Ð½Ð¾ ' + label + '?')) return;
  toast('Ð’Ñ‹Ð¿Ð¾Ð»Ð½ÑÑŽâ€¦');
  try { await api('api/system/core/' + action, {method:'POST'}); toast('ÐšÐ¾Ð¼Ð°Ð½Ð´Ð° Ð¿Ñ€Ð¸Ð½ÑÑ‚Ð°'); }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
  setTimeout(load, 3000);
}

async function hostAct(action){
  const label = action === 'reboot' ? 'ÐŸÐ•Ð Ð•Ð—ÐÐ“Ð Ð£Ð—Ð˜Ð¢Ð¬' : 'Ð’Ð«ÐšÐ›Ð®Ð§Ð˜Ð¢Ð¬';
  if (!confirm('Ð ÐµÐ°Ð»ÑŒÐ½Ð¾ ' + label + ' Ñ…Ð¾ÑÑ‚?')) return;
  if (!confirm('ÐŸÐ¾ÑÐ»ÐµÐ´Ð½ÐµÐµ Ð¿Ð¾Ð´Ñ‚Ð²ÐµÑ€Ð¶Ð´ÐµÐ½Ð¸Ðµ: ' + label + '?')) return;
  toast('ÐžÑ‚Ð¿Ñ€Ð°Ð²Ð»ÑÑŽ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñƒâ€¦');
  try { await api('api/system/host/' + action, {method:'POST'}); toast('ÐšÐ¾Ð¼Ð°Ð½Ð´Ð° Ð¾Ñ‚Ð¿Ñ€Ð°Ð²Ð»ÐµÐ½Ð°, ÑÐ²ÑÐ·ÑŒ Ð¿Ñ€ÐµÑ€Ð²Ñ‘Ñ‚ÑÑ'); }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}

async function storeReload(){
  toast('ÐŸÐµÑ€ÐµÐ·Ð°Ð³Ñ€ÑƒÐ¶Ð°ÑŽ Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½ (Ð´Ð¾ Ð¼Ð¸Ð½ÑƒÑ‚Ñ‹)â€¦');
  try { await api('api/store/reload', {method:'POST', setTimeout:0}); toast('ÐœÐ°Ð³Ð°Ð·Ð¸Ð½ Ð¾Ð±Ð½Ð¾Ð²Ð»Ñ‘Ð½'); }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
  load();
}
async function repoAdd(){
  const url = document.getElementById('repo-url').value.trim();
  if (!url) return;
  try { await api('api/store/repos', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})}); toast('Ð ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ð¸Ð¹ Ð´Ð¾Ð±Ð°Ð²Ð»ÐµÐ½'); document.getElementById('repo-url').value=''; }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
  load();
}
async function repoDel(slug){
  if (!confirm('Ð£Ð´Ð°Ð»Ð¸Ñ‚ÑŒ Ñ€ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ð¸Ð¹ ' + slug + '? ÐÐ´Ð´Ð¾Ð½Ñ‹ Ð¸Ð· Ð½ÐµÐ³Ð¾ Ð¾ÑÑ‚Ð°Ð½ÑƒÑ‚ÑÑ ÑƒÑÑ‚Ð°Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ‹Ð¼Ð¸.')) return;
  try { await api('api/store/repos/' + encodeURIComponent(slug), {method:'DELETE'}); toast('Ð£Ð´Ð°Ð»Ñ‘Ð½'); }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
  load();
}

async function mkBackup(){
  if (!confirm('Ð¡Ð¾Ð·Ð´Ð°Ñ‚ÑŒ Ð¿Ð¾Ð»Ð½Ñ‹Ð¹ Ð±ÑÐºÐ°Ð¿? 1â€“3 Ð¼Ð¸Ð½ÑƒÑ‚Ñ‹.')) return;
  toast('Ð¡Ð¾Ð·Ð´Ð°ÑŽ Ð±ÑÐºÐ°Ð¿â€¦');
  try { await api('api/backups', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name:'manual_' + new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')})}); toast('Ð‘ÑÐºÐ°Ð¿ ÑÐ¾Ð·Ð´Ð°Ð½'); }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
  load();
}
async function delBackup(slug, name){
  if (!confirm('Ð£Ð´Ð°Ð»Ð¸Ñ‚ÑŒ Ð±ÑÐºÐ°Ð¿ Â«' + name + 'Â»?')) return;
  try { await api('api/backups/' + encodeURIComponent(slug), {method:'DELETE'}); toast('Ð£Ð´Ð°Ð»Ñ‘Ð½'); }
  catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
  load();
}

/* --- Ð»Ð¾Ð³Ð¸ --- */
async function openLogs(slug){
  document.getElementById('logsrc').value = slug;
  document.querySelector('nav button[data-tab=logs]').click();
  showLogs();
}
async function showLogs(){
  const src = document.getElementById('logsrc').value;
  const body = document.getElementById('logbody');
  try {
    const t = await api('api/logs?source=' + encodeURIComponent(src) + '&lines=300');
    body.textContent = t || '(Ð¿ÑƒÑÑ‚Ð¾)';
    body.scrollTop = body.scrollHeight;
  } catch(e){ body.textContent = 'ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message; }
}
function toggleFollow(){
  const b = document.getElementById('followbtn');
  if (FOLLOW) { clearInterval(FOLLOW); FOLLOW = null; b.textContent = 'ÐÐ²Ñ‚Ð¾Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ: Ð²Ñ‹ÐºÐ»'; }
  else { FOLLOW = setInterval(showLogs, 5000); b.textContent = 'ÐÐ²Ñ‚Ð¾Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ: Ð²ÐºÐ»'; }
}

/* --- Ñ„Ð°Ð¹Ð»Ñ‹ --- */
function renderRoots(){
  const box = document.getElementById('froots');
  box.innerHTML = (STATE.file_roots||[]).map(r =>
    '<button class="chip" data-root="' + esc(r) + '">' + esc(r) + '</button>').join('');
  box.querySelectorAll('.chip').forEach(b => b.onclick = () => fload(b.dataset.root));
}
async function fload(path){
  try {
    const d = await api('api/files?path=' + encodeURIComponent(path));
    CURDIR = d.path;
    document.getElementById('fpath').textContent = d.path;
    document.getElementById('flist').innerHTML = d.items.map(it =>
      '<div class="fitem" data-path="' + esc(d.path + '/' + it.name) + '" data-dir="' + (it.dir ? '1' : '0') + '">' +
      '<span>' + (it.dir ? 'ðŸ“ ' : 'ðŸ“„ ') + esc(it.name) + '</span>' +
      '<span class="muted">' + (it.dir ? '' : fmtSize(it.size)) + '</span></div>'
    ).join('') || '<div class="muted">Ð¿ÑƒÑÑ‚Ð¾</div>';
    document.querySelectorAll('#flist .fitem').forEach(el => {
      el.onclick = () => el.dataset.dir === '1' ? fload(el.dataset.path) : fopen(el.dataset.path);
    });
    window._fparent = d.parent;
  } catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}
function fup(){ if (window._fparent) fload(window._fparent); }
async function fopen(path){
  try {
    const d = await api('api/files/read?path=' + encodeURIComponent(path));
    CURFILE = d.path;
    document.getElementById('fname').textContent = d.path;
    document.getElementById('fcontent').value = d.content;
    document.getElementById('fmeta').textContent = fmtSize(d.size) + (d.size > 512*1024 ? ' (Ð¿Ð¾ÐºÐ°Ð·Ð°Ð½Ñ‹ Ð¿ÐµÑ€Ð²Ñ‹Ðµ 512 ÐšÐ‘)' : '');
    const dl = document.getElementById('fdl');
    dl.href = BASE + 'api/files/download?path=' + encodeURIComponent(d.path);
    dl.style.display = '';
  } catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}
async function fsave(){
  if (!CURFILE) return toast('Ð¤Ð°Ð¹Ð» Ð½Ðµ Ð²Ñ‹Ð±Ñ€Ð°Ð½', true);
  try {
    await api('api/files/write', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: CURFILE, content: document.getElementById('fcontent').value})});
    toast('Ð¡Ð¾Ñ…Ñ€Ð°Ð½ÐµÐ½Ð¾ (ÑÑ‚Ð°Ñ€Ð°Ñ Ð²ÐµÑ€ÑÐ¸Ñ â€” .bak)');
  } catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}
async function fnewFile(){
  const name = document.getElementById('fnew').value.trim();
  if (!name) return;
  const p = CURDIR.replace(/\/$/,'') + '/' + name;
  try {
    await api('api/files/write', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: p, content: ''})});
    document.getElementById('fnew').value = '';
    fload(CURDIR); fopen(p);
  } catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}
async function fnewDir(){
  const name = prompt('Ð˜Ð¼Ñ Ð½Ð¾Ð²Ð¾Ð¹ Ð¿Ð°Ð¿ÐºÐ¸:');
  if (!name) return;
  try {
    await api('api/files/mkdir', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: CURDIR.replace(/\/$/,'') + '/' + name})});
    toast('ÐŸÐ°Ð¿ÐºÐ° ÑÐ¾Ð·Ð´Ð°Ð½Ð°'); fload(CURDIR);
  } catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}
async function frename(){
  if (!CURFILE) return toast('Ð¤Ð°Ð¹Ð» Ð½Ðµ Ð²Ñ‹Ð±Ñ€Ð°Ð½', true);
  const name = prompt('ÐÐ¾Ð²Ð¾Ðµ Ð¸Ð¼Ñ:', CURFILE.split('/').pop());
  if (!name) return;
  try {
    await api('api/files/rename', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: CURFILE, name: name})});
    toast('ÐŸÐµÑ€ÐµÐ¸Ð¼ÐµÐ½Ð¾Ð²Ð°Ð½Ð¾'); fload(CURDIR); fopen(CURDIR.replace(/\/$/,'') + '/' + name);
  } catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}
async function fdel(){
  if (!CURFILE) return toast('Ð¤Ð°Ð¹Ð» Ð½Ðµ Ð²Ñ‹Ð±Ñ€Ð°Ð½', true);
  if (!confirm('Ð£Ð´Ð°Ð»Ð¸Ñ‚ÑŒ ' + CURFILE + '?')) return;
  try {
    await api('api/files?path=' + encodeURIComponent(CURFILE), {method:'DELETE'});
    toast('Ð£Ð´Ð°Ð»Ñ‘Ð½'); CURFILE = null;
    document.getElementById('fname').textContent = 'Ñ„Ð°Ð¹Ð» Ð½Ðµ Ð²Ñ‹Ð±Ñ€Ð°Ð½';
    document.getElementById('fcontent').value = '';
    document.getElementById('fmeta').textContent = '';
    document.getElementById('fdl').style.display = 'none';
    fload(CURDIR);
  } catch(e){ toast('ÐžÑˆÐ¸Ð±ÐºÐ°: ' + e.message, true); }
}
async function fuploadFiles(files, dir){
  for (const f of files) {
    toast('Ð—Ð°Ð³Ñ€ÑƒÐ¶Ð°ÑŽ ' + f.name + ' (' + fmtSize(f.size) + ')â€¦');
    try {
      const r = await fetch(BASE + 'api/files/upload?path=' + encodeURIComponent(dir) +
                            '&name=' + encodeURIComponent(f.name),
                            {method:'POST', body:f, headers:{'Content-Type':'application/octet-stream'}});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      toast('Ð—Ð°Ð³Ñ€ÑƒÐ¶ÐµÐ½: ' + f.name);
    } catch(e) { toast('ÐžÑˆÐ¸Ð±ÐºÐ° ' + f.name + ': ' + e.message, true); break; }
  }
  fload(dir);
}

function closeModal(){ document.getElementById('modal').classList.remove('open'); }
document.getElementById('fupload').onchange = e => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (files.length) fuploadFiles(files, CURDIR);
};
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  ['apps','store','files','logs','backups','system'].forEach(t =>
    document.getElementById('tab-' + t).style.display = (t === b.dataset.tab) ? '' : 'none');
  if (b.dataset.tab === 'files') fload(CURDIR);
  if (b.dataset.tab === 'logs') showLogs();
});
document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
load();
setInterval(load, 15000);
</script>
</body>
</html>
"""


# --- HTTP handler ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HAControl/" + VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _path(self):
        path = urlparse(self.path).path or "/"
        m = re.match(r"^/api/hassio_ingress/[^/]+(/.*)?$", path)
        if m:
            path = m.group(1) or "/"
        return path

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

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if length > MAX_WRITE:
            raise ValueError("too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw)

    def _supervisor_action(self, path, action, ok_obj, timeout=300):
        status, data = supervisor(path, method="POST", timeout=timeout)
        if status != 200:
            return self._json(status or 502, {"error": action + " failed",
                                              "message": _msg(data), "detail": data})
        obj = {"ok": True}
        obj.update(ok_obj or {})
        return self._json(200, obj)

    # --- GET ---
    def do_GET(self):
        path = self._path()
        if not path.startswith("/api/"):
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if path == "/api/health":
            return self._json(200, {"ok": True, "version": VERSION})

        if path == "/api/state":
            try:
                return self._json(200, collect_state())
            except Exception as exc:
                return self._json(200, {"version": VERSION, "errors": ["state: %s" % exc],
                                        "host": {}, "supervisor": {}, "core": {},
                                        "addons": [], "backups": [], "repositories": [],
                                        "mem": {}, "temp_c": None, "loadavg": []})

        if path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)
            source = qs.get("source", ["core"])[0]
            try:
                lines = max(10, min(3000, int(qs.get("lines", ["200"])[0])))
            except ValueError:
                lines = 200
            if source == "core":
                sp = "/core/logs"
            elif source == "supervisor":
                sp = "/supervisor/logs"
            elif SLUG_RE.match(source):
                sp = "/addons/%s/logs" % source
            else:
                return self._json(400, {"error": "bad source"})
            status, data = supervisor(sp, timeout=30)
            if status != 200:
                return self._json(status or 502, {"error": "logs failed", "message": _msg(data)})
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            if isinstance(data, dict) and isinstance(data.get("data"), str):
                text = data["data"]
            return self._send(200, "\n".join(text.splitlines()[-lines:]),
                              "text/plain; charset=utf-8")

        m = re.match(r"^/api/backups/([A-Za-z0-9_.-]+)/download$", path)
        if m:
            slug = m.group(1)
            status, blob = supervisor("/backups/%s/download" % slug, timeout=600, raw=True)
            if status != 200:
                return self._json(status or 502, {"error": "download failed", "message": _msg(blob)})
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Content-Disposition", 'attachment; filename="%s.tar"' % slug)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            try:
                self.wfile.write(blob)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path == "/api/files":
            qs = parse_qs(urlparse(self.path).query)
            res, err = file_listing(qs.get("path", ["/config"])[0])
            if err:
                return self._json(400, {"error": err})
            return self._json(200, res)

        if path == "/api/files/read":
            qs = parse_qs(urlparse(self.path).query)
            res, err = file_read(qs.get("path", [""])[0])
            if err:
                return self._json(400, {"error": err})
            return self._json(200, res)

        if path == "/api/files/download":
            qs = parse_qs(urlparse(self.path).query)
            real, err = safe_path(qs.get("path", [""])[0])
            if err or not os.path.isfile(real):
                return self._json(400, {"error": err or "Ð½Ðµ Ñ„Ð°Ð¹Ð»"})
            size = os.path.getsize(real)
            name = os.path.basename(real)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                             % (quote(name, safe=""), quote(name)))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                with open(real, "rb") as fh:
                    while True:
                        chunk = fh.read(CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        return self._json(404, {"error": "not found", "path": path})

    # --- POST ---
    def do_POST(self):
        path = self._path()

        # Ð—Ð°Ð³Ñ€ÑƒÐ·ÐºÐ° Ñ„Ð°Ð¹Ð»Ð°: Ñ‚ÐµÐ»Ð¾ Ð·Ð°Ð¿Ñ€Ð¾ÑÐ° â€” ÑÑ‹Ñ€Ñ‹Ðµ Ð±Ð°Ð¹Ñ‚Ñ‹, Ð¿Ð¾ÑÑ‚Ð¾Ð¼Ñƒ Ð¾Ð±Ñ€Ð°Ð±Ð°Ñ‚Ñ‹Ð²Ð°ÐµÐ¼ Ð´Ð¾ _body()
        if path == "/api/files/upload":
            qs = parse_qs(urlparse(self.path).query)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return self._json(400, {"error": "Ð¿ÑƒÑÑ‚Ð¾Ð¹ Ñ„Ð°Ð¹Ð»"})
            if length > MAX_UPLOAD:
                return self._json(413, {"error": "Ñ„Ð°Ð¹Ð» Ð±Ð¾Ð»ÑŒÑˆÐµ %d ÐœÐ‘" % (MAX_UPLOAD // 1048576)})
            dest, err = file_upload(qs.get("path", [""])[0],
                                    qs.get("name", [""])[0], length, self.rfile)
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"ok": True, "path": dest, "size": length})

        try:
            body = self._body()
        except ValueError:
            return self._json(400, {"error": "invalid json"})

        m = re.match(r"^/api/apps/([A-Za-z0-9_.-]+)/([a-z]+)$", path)
        if m:
            slug, action = m.group(1), m.group(2)
            if action not in APP_ACTIONS:
                return self._json(400, {"error": "bad action"})
            return self._supervisor_action("/addons/%s/%s" % (slug, action), action,
                                           {"slug": slug, "action": action}, timeout=900)

        m = re.match(r"^/api/system/core/([a-z]+)$", path)
        if m and m.group(1) in CORE_ACTIONS:
            return self._supervisor_action("/core/%s" % m.group(1), m.group(1), {}, timeout=300)

        m = re.match(r"^/api/system/host/([a-z]+)$", path)
        if m and m.group(1) in HOST_ACTIONS:
            return self._supervisor_action("/host/%s" % m.group(1), m.group(1), {},
                                           timeout=120)

        if path == "/api/store/reload":
            return self._supervisor_action("/store/reload", "reload", {}, timeout=600)

        if path == "/api/store/repos":
            url = str(body.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return self._json(400, {"error": "Ð½ÑƒÐ¶ÐµÐ½ http(s)-URL"})
            status, data = supervisor("/store/repositories", method="POST",
                                      payload={"repository": url}, timeout=600)
            if status != 200:
                return self._json(status or 502, {"error": "add_repo failed",
                                                  "message": _msg(data)})
            return self._json(200, {"ok": True, "url": url})
        if path == "/api/backups":
            name = str(body.get("name") or "").strip()[:64] or "manual"
            status, data = supervisor("/backups", method="POST", payload={"name": name}, timeout=3000)
            if status != 200:
                return self._json(status or 502, {"error": "backup failed", "message": _msg(data)})
            return self._json(200, {"ok": True, "name": name})

        if path == "/api/files/write":
            err = file_write(str(body.get("path") or ""), str(body.get("content") or ""))
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"ok": True})

        if path == "/api/files/mkdir":
            err = file_mkdir(str(body.get("path") or ""))
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"ok": True})

        if path == "/api/files/rename":
            err = file_rename(str(body.get("path") or ""), str(body.get("name") or ""))
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found", "path": path})

    # --- DELETE ---
    def do_DELETE(self):
        path = self._path()

        m = re.match(r"^/api/backups/([A-Za-z0-9_.-]+)$", path)
        if m:
            slug = m.group(1)
            status, data = supervisor("/backups/%s" % slug, method="DELETE", timeout=120)
            if status != 200:
                return self._json(status or 502, {"error": "delete failed", "message": _msg(data)})
            return self._json(200, {"ok": True, "slug": slug})

        m = re.match(r"^/api/store/repos/([A-Za-z0-9_.-]+)$", path)
        if m:
            slug = m.group(1)
            status, data = supervisor("/store/repositories/%s" % slug, method="DELETE", timeout=120)
            if status != 200:
                return self._json(status or 502, {"error": "delete failed", "message": _msg(data)})
            return self._json(200, {"ok": True, "slug": slug})

        if path == "/api/files":
            qs = parse_qs(urlparse(self.path).query)
            err = file_delete(qs.get("path", [""])[0])
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found", "path": path})


# --- startup ---------------------------------------------------------------

def ensure_settings():
    """Ð’ÐºÐ»ÑŽÑ‡Ð°ÐµÑ‚ Ð¿Ð°Ð½ÐµÐ»ÑŒ Ð² ÑÐ°Ð¹Ð´Ð±Ð°Ñ€Ðµ Ð¸ watchdog: Ð¾Ð±Ð° Ñ„Ð»Ð°Ð³Ð° Ð¶Ð¸Ð²ÑƒÑ‚ Ð² apps.json, Ð¿Ð¾ ÑƒÐ¼Ð¾Ð»Ñ‡Ð°Ð½Ð¸ÑŽ off."""
    status, data = supervisor("/addons/self/info", timeout=20)
    if status != 200 or not isinstance(data, dict):
        return
    info = data.get("data", {}) or {}
    body = {}
    if info.get("ingress") and not info.get("ingress_panel"):
        body["ingress_panel"] = True
    if not info.get("watchdog"):
        body["watchdog"] = True
    if not body:
        return
    status, data = supervisor("/addons/self/options", method="POST", payload=body, timeout=60)
    if status == 200:
        print("Ð²ÐºÐ»ÑŽÑ‡ÐµÐ½Ð¾: %s (Ð¿Ð°Ð½ÐµÐ»ÑŒ Ð² Ð¼ÐµÐ½ÑŽ / watchdog)" % ", ".join(sorted(body)), flush=True)
    else:
        print("ensure_settings: %s -> %s" % (body, _msg(data)), flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not TOKEN:
        print("FATAL: SUPERVISOR_TOKEN Ð½Ðµ Ð·Ð°Ð´Ð°Ð½ (Ð½ÑƒÐ¶ÐµÐ½ hassio_api: true)", flush=True)
        sys.exit(1)
    try:
        ensure_settings()
    except Exception as exc:
        print("ensure_settings: %s" % exc, flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("HA Control %s listening on 0.0.0.0:%d" % (VERSION, PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
