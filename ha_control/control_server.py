#!/usr/bin/env python3
"""HA Control — веб-панель управления Home Assistant (v0.2.0).

Работает только через ingress Supervisor (отдельного порта наружу нет).
Права: hassio_api + hassio_role=admin — аддоны (установка/обновление/удаление),
магазин, логи, бэкапы (создание/скачивание/удаление), рестарт HA и ребут хоста,
файлы /config и /share. docker_api не используется.
"""

import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

VERSION = "0.4.2"
SUPERVISOR = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
PORT = int(os.environ.get("CONTROL_PORT", "8099"))
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

APP_ACTIONS = {"start", "stop", "restart", "update", "uninstall", "install"}
CORE_ACTIONS = {"start", "stop", "restart", "update"}
HOST_ACTIONS = {"reboot", "shutdown"}
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
FILE_ROOTS = tuple(x for x in os.environ.get(
    "CONTROL_FILE_ROOTS",
    "/config|/share|/media|/ssl|/backup|/app_configs|/addon_configs|/local_apps").split("|") if x)
MAX_WRITE = 4 * 1024 * 1024
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
CHUNK = 64 * 1024


# --- Supervisor API --------------------------------------------------------

def supervisor(path, method="GET", payload=None, timeout=60, raw=False):
    """Возвращает (status, data). data — dict/list для JSON, bytes для raw, иначе str."""
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
    """Достаёт список из ответа Supervisor, понимая обе формы: list и {key: list}."""
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
    if items is None:  # старые версии: /store
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
    state["docker"] = docker_available()
    return state


class _FixedReader:
    """Тело с Content-Length."""

    def __init__(self, rfile, length):
        self.rfile = rfile
        self.left = max(0, int(length or 0))

    def read(self, n):
        if self.left <= 0:
            return b""
        data = self.rfile.read(min(n, self.left))
        self.left -= len(data)
        return data


class _ChunkedReader:
    """Тело в Transfer-Encoding: chunked (так HA ingress передаёт POST при ingress_stream)."""

    def __init__(self, rfile):
        self.rfile = rfile
        self.left = 0
        self.done = False

    def _next_chunk(self):
        while True:
            line = self.rfile.readline(128)
            if not line:
                self.done = True
                return False
            line = line.strip()
            if not line:
                continue
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                self.done = True
                return False
            if size == 0:
                self.rfile.readline()
                self.done = True
                return False
            self.left = size
            return True

    def read(self, n):
        out = bytearray()
        while len(out) < n and not self.done:
            if self.left <= 0 and not self._next_chunk():
                break
            take = min(n - len(out), self.left)
            data = self.rfile.read(take)
            if not data:
                self.done = True
                break
            out += data
            self.left -= len(data)
            if self.left == 0:
                self.rfile.read(2)  # CRLF после чанка
        return bytes(out)


# --- Docker (консоль) ------------------------------------------------------

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=120):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


def docker_available():
    return os.path.exists(DOCKER_SOCK)


def docker_call(method, path, payload=None, timeout=120):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Host": "localhost"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = _UnixConnection(DOCKER_SOCK, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if not raw:
            return resp.status, {}
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw.decode("utf-8", "replace")
    finally:
        conn.close()


def docker_containers():
    status, data = docker_call("GET", "/containers/json?all=1", timeout=30)
    if status != 200 or not isinstance(data, list):
        return None, _msg(data)
    out = []
    for item in data:
        names = item.get("Names") or [item.get("Id", "")[:12]]
        out.append({"name": names[0].lstrip("/"), "image": item.get("Image", ""),
                    "state": item.get("State", ""), "status": item.get("Status", "")})
    out.sort(key=lambda c: c["name"])
    return out, None


def docker_exec(container, cmd, timeout=120, limit=200 * 1024):
    """Выполняет команду в контейнере, возвращает (output, exit_code, err)."""
    status, data = docker_call("POST", "/containers/%s/exec" % quote(container, safe=""),
                               {"AttachStdout": True, "AttachStderr": True,
                                "Tty": False, "Cmd": ["sh", "-lc", cmd]}, timeout=30)
    if status not in (200, 201) or not isinstance(data, dict):
        return None, None, "exec create: %s %s" % (status, _msg(data))
    exec_id = data.get("Id")
    if not exec_id:
        return None, None, "exec create: нет Id"

    conn = _UnixConnection(DOCKER_SOCK, timeout=timeout)
    try:
        conn.request("POST", "/exec/%s/start" % exec_id,
                     body=json.dumps({"Detach": False, "Tty": False}).encode("utf-8"),
                     headers={"Content-Type": "application/json", "Host": "localhost"})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()

    # docker мультиплексирует потоки: 8-байтный заголовок + payload
    chunks = []
    i = 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        if size <= 0 or i + 8 + size > len(raw):
            break
        chunks.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if not chunks and raw:
        chunks.append(raw)

    text = b"".join(chunks).decode("utf-8", "replace")
    if len(text) > limit:
        text = text[:limit] + "\n… (обрезано, показано %d КБ)" % (limit // 1024)

    exit_code = None
    for _ in range(10):
        st, info = docker_call("GET", "/exec/%s/json" % exec_id, timeout=15)
        if st == 200 and isinstance(info, dict) and info.get("ExitCode") is not None:
            exit_code = info.get("ExitCode")
            break
        time.sleep(0.2)
    return text, exit_code, None


def _retag(image, tag):
    """ghcr.io/x/y:1.0 -> ghcr.io/x/y:<tag> (учитывая порт в registry)."""
    if "@" in image:
        image = image.split("@", 1)[0]
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        return image[:colon] + ":" + tag
    return image + ":" + tag


def _split_image(image):
    """Возвращает (repo, tag)."""
    if "@" in image:
        return image.split("@", 1)[0], None
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        return image[:colon], image[colon + 1:]
    return image, None


def app_prepull(slug):
    """Скачивает образ новой версии заранее и проверяет, что он запускаемый."""
    status, info = supervisor("/addons/%s/info" % slug, timeout=30)
    if status != 200 or not isinstance(info, dict):
        return None, "не удалось получить данные аддона (%s)" % status
    data = info.get("data", {}) or {}
    version = data.get("version_latest")
    if not version:
        return None, "нет версии для обновления"

    status, items = docker_call("GET", "/containers/json?all=1", timeout=30)
    if status != 200 or not isinstance(items, list):
        return None, "docker недоступен (%s)" % status
    target = "app_%s" % slug
    image = None
    for item in items:
        names = [n.lstrip("/") for n in (item.get("Names") or [])]
        if target in names:
            image = item.get("Image")
            break
    if not image:
        return None, "контейнер %s не найден" % target

    repo, cur_tag = _split_image(image)
    if cur_tag == version:
        return {"image": image, "pulled": False, "reason": "версия уже локально"}, None
    new_image = _retag(image, version)

    status, resp = docker_call(
        "POST", "/images/create?fromImage=%s&tag=%s" % (quote(repo, safe=""), quote(version)),
        timeout=1200)
    if status != 200:
        return None, "не удалось скачать %s: %s %s" % (new_image, status, _msg(resp))

    # проверка: у образа должна быть команда запуска
    status, insp = docker_call("GET", "/images/%s/json" % quote(new_image, safe=""), timeout=60)
    if status != 200 or not isinstance(insp, dict):
        return None, "образ скачан, но не читается (%s)" % status
    cfg = insp.get("Config") or {}
    if not (cfg.get("Cmd") or cfg.get("Entrypoint")):
        return None, "образ без Cmd/Entrypoint — обновление отменено"
    return {"image": new_image, "pulled": True, "version": version,
            "size_mb": round((insp.get("Size") or 0) / 1048576, 1)}, None


def check_tunnel(url, timeout=12):
    import ssl
    ctx = ssl._create_unverified_context()
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return {"url": url, "code": resp.status, "ok": 200 <= resp.status < 400,
                    "seconds": round(time.time() - t0, 2)}
    except urllib.error.HTTPError as exc:
        return {"url": url, "code": exc.code, "ok": 200 <= exc.code < 400,
                "seconds": round(time.time() - t0, 2)}
    except Exception as exc:
        return {"url": url, "code": 0, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "seconds": round(time.time() - t0, 2)}


# --- files -----------------------------------------------------------------

def safe_path(raw):
    """Путь внутри разрешённых корней. Возвращает (real, err)."""
    if not raw:
        return None, "пустой путь"
    full = os.path.realpath(raw)
    if not any(full == root or full.startswith(root + os.sep) for root in FILE_ROOTS):
        return None, "путь вне разрешённых каталогов"
    return full, None


def file_listing(path):
    real, err = safe_path(path)
    if err:
        return None, err
    if not os.path.isdir(real):
        return None, "не каталог"
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
        return None, "нет доступа"
    parent = os.path.dirname(real)
    if not any(parent == r or parent.startswith(r + os.sep) for r in FILE_ROOTS):
        parent = None
    return {"path": real, "parent": parent, "items": items}, None


def file_read(path, limit=512 * 1024):
    real, err = safe_path(path)
    if err:
        return None, err
    if not os.path.isfile(real):
        return None, "не файл"
    size = os.path.getsize(real)
    with open(real, "rb") as fh:
        chunk = fh.read(limit)
    if b"\x00" in chunk[:4096]:
        return None, "бинарный файл (%d байт)" % size
    return {"path": real, "size": size, "content": chunk.decode("utf-8", "replace")}, None


def file_write(path, content):
    real, err = safe_path(path)
    if err:
        return err
    if os.path.isdir(real):
        return "это каталог"
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
        return "нельзя удалить корень"
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
        return "некорректное имя"
    dest = os.path.join(os.path.dirname(real), name)
    dest_real, err = safe_path(dest)
    if err:
        return err
    if os.path.exists(dest_real):
        return "такое имя уже существует"
    os.rename(real, dest_real)
    return None


def file_upload(dir_path, name, reader, max_size=MAX_UPLOAD):
    """Пишет поток в файл потоково (Content-Length или chunked), без буферизации в память."""
    real, err = safe_path(dir_path)
    if err:
        return None, err
    if not os.path.isdir(real):
        return None, "не каталог"
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None, "некорректное имя файла"
    dest, err = safe_path(os.path.join(real, name))
    if err:
        return None, err
    if os.path.isdir(dest):
        return None, "это каталог"
    if os.path.exists(dest):
        shutil.copy2(dest, dest + ".bak")
    total = 0
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as fh:
            while True:
                chunk = reader.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise ValueError("файл больше %d МБ" % (max_size // 1048576))
                fh.write(chunk)
        if total == 0:
            os.remove(tmp)
            return None, "пустой файл"
        os.replace(tmp, dest)
    except Exception as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None, "%s" % (exc if isinstance(exc, ValueError) else "%s: %s" % (type(exc).__name__, exc))
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
  <div><h1>HA Control</h1><div class="sub" id="sub">загрузка…</div></div>
  <div style="display:flex;gap:6px;flex-wrap:wrap">
    <button class="btn" onclick="load()">Обновить</button>
    <button class="btn warn" onclick="coreAct('restart')">Рестарт HA</button>
  </div>
</header>
<nav>
  <button data-tab="apps" class="active">Аддоны</button>
  <button data-tab="store">Магазин</button>
  <button data-tab="files">Файлы</button>
  <button data-tab="logs">Логи</button>
  <button data-tab="backups">Бэкапы</button>
  <button data-tab="system">Система</button>
  <button data-tab="console">Консоль</button>
</nav>
<main>
  <section id="tab-apps">
    <p class="hint">Установленные аддоны. «Стоп» и «Рестарт» применяются сразу, удаление — с подтверждением.</p>
    <div class="grid" id="apps"></div>
  </section>

  <section id="tab-store" style="display:none">
    <div class="card" style="margin-bottom:12px">
      <div class="row"><h3>Репозитории</h3><button class="btn" onclick="storeReload()">Перезагрузить магазин</button></div>
      <div id="repos" style="margin-top:10px"></div>
      <div class="row" style="margin-top:12px">
        <input type="text" id="repo-url" placeholder="https://github.com/user/repo">
        <button class="btn primary" onclick="repoAdd()">Добавить</button>
      </div>
      <div class="muted" style="margin-top:8px">После добавления нажми «Перезагрузить магазин». Установка — на вкладке «Аддоны» (кнопка «Установить»).</div>
    </div>
    <div class="card"><table id="store-apps"><tbody></tbody></table></div>
  </section>

  <section id="tab-files" style="display:none">
    <p class="hint">Все каталоги HA: <b>/config</b>, <b>/share</b>, <b>/media</b>, <b>/ssl</b>,
      <b>/backup</b>, <b>/addon_configs</b>, <b>/local_apps</b>. При перезаписи создаётся копия <code>.bak</code>.</p>
    <div class="files">
      <div class="card">
        <div class="row" style="margin-bottom:8px;flex-wrap:wrap;gap:6px">
          <b id="fpath" style="font-size:12px;word-break:break-all">/config</b>
          <span style="display:flex;gap:6px">
            <button class="btn" onclick="fup()">Вверх</button>
            <button class="btn primary" onclick="document.getElementById('fupload').click()">Загрузить</button>
            <input type="file" id="fupload" multiple style="display:none">
          </span>
        </div>
        <div class="roots" id="froots"></div>
        <div class="flist" id="flist"></div>
        <div class="row" style="margin-top:10px;gap:6px">
          <input type="text" id="fnew" placeholder="имя_файла.yaml">
          <button class="btn" onclick="fnewFile()">+ файл</button>
          <button class="btn" onclick="fnewDir()">+ папка</button>
        </div>
      </div>
      <div class="card">
        <div class="row" style="margin-bottom:8px;flex-wrap:wrap;gap:6px">
          <b id="fname" style="font-size:12px;word-break:break-all">файл не выбран</b>
          <span style="display:flex;gap:6px;flex-wrap:wrap">
            <a class="btn" id="fdl" style="display:none;text-decoration:none">Скачать</a>
            <button class="btn" onclick="frename()">Переименовать</button>
            <button class="btn primary" onclick="fsave()">Сохранить</button>
          </span>
        </div>
        <textarea id="fcontent" spellcheck="false" placeholder="Выбери файл слева"></textarea>
        <div class="row" style="margin-top:10px">
          <span class="muted" id="fmeta"></span>
          <button class="btn danger" onclick="fdel()">Удалить</button>
        </div>
      </div>
    </div>
  </section>

  <section id="tab-logs" style="display:none">
    <div class="row" style="margin-bottom:12px;flex-wrap:wrap">
      <select id="logsrc" style="max-width:320px"></select>
      <span style="display:flex;gap:6px">
        <button class="btn primary" onclick="showLogs()">Показать</button>
        <button class="btn" onclick="toggleFollow()" id="followbtn">Автообновление: выкл</button>
      </span>
    </div>
    <div class="card"><pre id="logbody" style="max-height:70vh">Выбери источник и нажми «Показать».</pre></div>
  </section>

  <section id="tab-backups" style="display:none">
    <div class="row" style="margin-bottom:12px;flex-wrap:wrap">
      <p class="hint" style="margin:0">Полный бэкап создаётся на малине 1–3 минуты.</p>
      <button class="btn primary" onclick="mkBackup()">Создать полный бэкап</button>
    </div>
    <div class="card"><table id="backups"><tbody></tbody></table></div>
  </section>

  <section id="tab-console" style="display:none">
    <div class="card danger-zone">
      <h3>Консоль (docker exec)</h3>
      <div class="muted">Команда выполняется внутри выбранного контейнера. Это доступ уровня root
        к системе: ошибка может уронить HA (туннель <code>ha_link</code> — отдельный контейнер).</div>
      <div class="row" style="margin-top:10px;gap:6px;flex-wrap:wrap">
        <select id="cshell" style="max-width:360px"></select>
        <button class="btn" onclick="loadContainers()">Обновить список</button>
      </div>
      <div class="row" style="margin-top:10px;gap:6px">
        <input type="text" id="ccmd" placeholder="например: ha core info"
               onkeydown="if(event.key==='Enter')crun()">
        <button class="btn primary" onclick="crun()">Выполнить</button>
      </div>
      <div class="actions">
        <button class="btn" onclick="cquick('ha core info')">ha core info</button>
        <button class="btn" onclick="cquick('ha addons')">ha addons</button>
        <button class="btn" onclick="cquick('df -h')">df -h</button>
        <button class="btn" onclick="cquick('free -m')">free -m</button>
        <button class="btn" onclick="cquick('docker ps')">docker ps</button>
      </div>
      <pre id="cout" style="margin-top:12px;max-height:50vh">—</pre>
    </div>
  </section>

  <section id="tab-system" style="display:none">
    <div class="grid" id="system"></div>
    <div class="card" style="margin-top:12px">
      <h3>Связь</h3>
      <div class="muted">Проверка публичного туннеля <code>ha_link</code> изнутри панели.</div>
      <div class="actions"><button class="btn" onclick="checkTunnel()">Проверить туннель</button></div>
      <pre id="tunnelout" style="margin-top:8px">—</pre>
    </div>
    <div class="card danger-zone" style="margin-top:12px">
      <h3>Опасная зона</h3>
      <div class="muted">Действия выполняются на хосте. Туннель <code>ha_link</code> при ребуте поднимется сам (boot: auto).</div>
      <div class="actions">
        <button class="btn warn" onclick="hostAct('reboot')">Перезагрузить хост</button>
        <button class="btn danger" onclick="hostAct('shutdown')">Выключить хост</button>
      </div>
    </div>
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
let STATE = null, CURDIR = '/config', CURFILE = null, FOLLOW = null;

const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg, isErr){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(() => t.className = 'toast', 4500);
}
function badge(state){
  const s = ['started','stopped','error'].includes(state) ? state : 'unknown';
  const label = {started:'работает', stopped:'остановлен', error:'ошибка', unknown:'неизвестно'}[s];
  return '<span class="badge ' + s + '">' + label + '</span>';
}
function bar(pct){
  const c = pct >= 90 ? 'crit' : (pct >= 75 ? 'warn' : '');
  return '<div class="bar ' + c + '"><i style="width:' + Math.min(100, pct) + '%"></i></div>';
}
function fmtDate(s){ try { return new Date(s).toLocaleString('ru-RU'); } catch(e){ return s; } }
function fmtSize(b){ return b > 1048576 ? (b/1048576).toFixed(1)+' МБ' : Math.max(1, Math.round(b/1024))+' КБ'; }

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
      ? '<div class="muted" style="color:#e0a13a;margin-top:6px">Доступно обновление: ' + esc(a.version_latest) + '</div>' : '';
    const btns = [];
    if (a.state === 'started') {
      btns.push('<button class="btn" onclick="appAct(\'' + a.slug + '\',\'stop\')">Стоп</button>');
      btns.push('<button class="btn" onclick="appAct(\'' + a.slug + '\',\'restart\')">Рестарт</button>');
    } else {
      btns.push('<button class="btn primary" onclick="appAct(\'' + a.slug + '\',\'start\')">Запустить</button>');
    }
    if (a.update_available) btns.push('<button class="btn warn" onclick="appAct(\'' + a.slug + '\',\'update\')">Обновить</button>');
    btns.push('<button class="btn" onclick="openLogs(\'' + a.slug + '\')">Логи</button>');
    btns.push('<button class="btn danger" onclick="appAct(\'' + a.slug + '\',\'uninstall\')">Удалить</button>');
    return '<div class="card">' +
      '<div class="row"><h3>' + esc(a.name) + '</h3>' + badge(a.state) + '</div>' +
      '<div class="muted">' + esc(a.slug) + ' · v' + esc(a.version) + ' · boot: ' + esc(a.boot) + '</div>' +
      upd +
      '<div class="actions">' + btns.join('') + '</div></div>';
  }).join('') || '<div class="card muted">Нет установленных аддонов</div>';
}

function renderSystem(){
  const h = STATE.host||{}, s = STATE.supervisor||{}, c = STATE.core||{}, m = STATE.mem||{};
  const memPct = m.total_mb ? Math.round(100*m.used_mb/m.total_mb) : 0;
  const diskPct = h.disk_total ? Math.round(100*h.disk_used/h.disk_total) : 0;
  const swapPct = m.swap_total_mb ? Math.round(100*m.swap_used_mb/m.swap_total_mb) : 0;
  const temp = STATE.temp_c != null ? STATE.temp_c + ' °C' : '—';
  document.getElementById('system').innerHTML =
    '<div class="card"><h3>Хост</h3><div class="muted">' + esc(h.operating_system||'—') + '</div>' +
      '<div class="muted">' + esc(h.hostname||'') + ' · ' + esc(h.kernel||'') + '</div>' +
      '<div class="muted">температура CPU: ' + temp + '</div>' +
      '<div class="muted">' + esc(h.timezone||'') + ' · uptime: ' + (h.startup_time!=null?Math.round(h.startup_time)+' с':'—') + '</div></div>' +
    '<div class="card"><h3>Память</h3><div class="muted">' + (m.used_mb||0) + ' / ' + (m.total_mb||0) + ' МБ (' + memPct + '%)</div>' + bar(memPct) +
      '<div class="muted" style="margin-top:8px">swap: ' + (m.swap_used_mb||0) + ' / ' + (m.swap_total_mb||0) + ' МБ (' + swapPct + '%)</div>' + bar(swapPct) + '</div>' +
    '<div class="card"><h3>Диск</h3><div class="muted">' + (h.disk_used||0) + ' / ' + (h.disk_total||0) + ' ГБ (' + diskPct + '%)</div>' + bar(diskPct) +
      '<div class="muted" style="margin-top:8px">свободно: ' + (h.disk_free||0) + ' ГБ</div></div>' +
    '<div class="card"><h3>Supervisor</h3><div class="muted">v' + esc(s.version||'—') + ' · ' + esc(s.channel||'') + ' · ' + esc(s.arch||'') + '</div>' +
      '<div class="muted">healthy: ' + (s.healthy?'да':'нет') + ' · supported: ' + (s.supported?'да':'нет') + '</div></div>' +
    '<div class="card"><h3>Home Assistant</h3><div class="muted">v' + esc(c.version||'—') + ' · ' + esc(c.machine||'') + '</div>' +
      (c.update_available?'<div class="muted" style="color:#e0a13a">доступно обновление</div>':'') +
      '<div class="actions"><button class="btn warn" onclick="coreAct(\'update\')">Обновить HA</button></div></div>' +
    '<div class="card"><h3>Нагрузка</h3><div class="muted">' + ((STATE.loadavg||[]).join('  ') || '—') + '</div></div>';
}

function renderBackups(){
  const tb = document.querySelector('#backups tbody');
  tb.innerHTML = '<tr><th>Имя</th><th>Дата</th><th>Тип</th><th>Размер</th><th></th></tr>' +
    ((STATE.backups||[]).map(b =>
      '<tr><td>' + esc(b.name) + '</td><td>' + esc(fmtDate(b.date)) + '</td><td>' + esc(b.type) + '</td><td>' + esc(b.size_mb) + ' МБ</td>' +
      '<td style="white-space:nowrap"><a class="btn" style="text-decoration:none" href="' + BASE + 'api/backups/' + encodeURIComponent(b.slug) + '/download">Скачать</a> ' +
      '<button class="btn danger bdel" data-slug="' + esc(b.slug) + '" data-name="' + esc(b.name) + '">Удалить</button></td></tr>'
    ).join('') || '<tr><td colspan="5" class="muted">Бэкапов нет</td></tr>');
  tb.querySelectorAll('.bdel').forEach(el => {
    el.onclick = () => delBackup(el.dataset.slug, el.dataset.name);
  });
}

function renderRepos(){
  document.getElementById('repos').innerHTML = (STATE.repositories||[]).map(r =>
    '<div class="row" style="padding:6px 0;border-bottom:1px solid #232838">' +
    '<div><b>' + esc(r.name||r.slug) + '</b><div class="muted">' + esc(r.url||r.slug) + '</div></div>' +
    '<button class="btn danger" onclick="repoDel(\'' + esc(r.slug) + '\')">Удалить</button></div>'
  ).join('') || '<div class="muted">Нет репозиториев</div>';

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
    'HA ' + (c.version||'?') + ' · Supervisor ' + (s.version||'?') + ' · ' + (h.operating_system||'') +
    ' · панель v' + (STATE.version||'?') +
    ((STATE.errors&&STATE.errors.length) ? ' · ошибки: ' + STATE.errors.join(', ') : '');
  renderApps(); renderSystem(); renderBackups(); renderRepos(); renderRoots();
}

async function load(){
  try { STATE = await api('api/state'); render(); }
  catch(e){ toast('Не удалось получить состояние: ' + e.message, true); }
}

async function appAct(slug, action){
  const names = {stop:'остановить', restart:'перезапустить', start:'запустить', update:'обновить', uninstall:'УДАЛИТЬ'};
  if (action !== 'start' && !confirm('Точно ' + names[action] + ' ' + slug + '?')) return;
  if (action === 'uninstall' && !confirm('Удаление необратимо. Продолжить?')) return;
  if (action === 'update') {
    toast('Скачиваю образ заранее (простой будет короче)…');
    try {
      const p = await api('api/apps/' + encodeURIComponent(slug) + '/prepull', {method:'POST'});
      if (p.pulled) toast('Образ ' + p.version + ' скачан (' + p.size_mb + ' МБ), обновляю…');
      else toast('Образ уже локально, обновляю…');
    } catch(e) {
      toast('Предзагрузка не удалась: ' + e.message + ' — обновляю обычным путём', true);
    }
  }
  toast('Выполняю: ' + names[action] + '…');
  try { await api('api/apps/' + encodeURIComponent(slug) + '/' + action, {method:'POST'}); toast('Готово: ' + names[action]); }
  catch(e){ toast('Ошибка: ' + e.message, true); }
  setTimeout(load, 1500);
}

async function coreAct(action){
  const label = {restart:'перезапустить Home Assistant', start:'запустить', stop:'ОСТАНОВИТЬ', update:'обновить Home Assistant'}[action];
  if (!confirm('Точно ' + label + '?')) return;
  toast('Выполняю…');
  try { await api('api/system/core/' + action, {method:'POST'}); toast('Команда принята'); }
  catch(e){ toast('Ошибка: ' + e.message, true); }
  setTimeout(load, 3000);
}

async function checkTunnel(){
  const el = document.getElementById('tunnelout');
  el.textContent = 'проверяю…';
  try {
    const d = await api('api/check/tunnel');
    el.textContent = d.checks.map(c =>
      c.url + ' -> ' + (c.code || 'ERR') + ' (' + c.seconds + 's)' + (c.error ? '  ' + c.error : '')
    ).join('\n') + '\n\n' + (d.ok ? 'связь в порядке' : 'ЕСТЬ ПРОБЛЕМА');
  } catch(e){ el.textContent = 'Ошибка: ' + e.message; }
}

async function hostAct(action){  const label = action === 'reboot' ? 'ПЕРЕЗАГРУЗИТЬ' : 'ВЫКЛЮЧИТЬ';
  if (!confirm('Реально ' + label + ' хост?')) return;
  if (!confirm('Последнее подтверждение: ' + label + '?')) return;
  toast('Отправляю команду…');
  try { await api('api/system/host/' + action, {method:'POST'}); toast('Команда отправлена, связь прервётся'); }
  catch(e){ toast('Ошибка: ' + e.message, true); }
}

async function storeReload(){
  toast('Перезагружаю магазин (до минуты)…');
  try { await api('api/store/reload', {method:'POST', setTimeout:0}); toast('Магазин обновлён'); }
  catch(e){ toast('Ошибка: ' + e.message, true); }
  load();
}
async function repoAdd(){
  const url = document.getElementById('repo-url').value.trim();
  if (!url) return;
  try { await api('api/store/repos', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})}); toast('Репозиторий добавлен'); document.getElementById('repo-url').value=''; }
  catch(e){ toast('Ошибка: ' + e.message, true); }
  load();
}
async function repoDel(slug){
  if (!confirm('Удалить репозиторий ' + slug + '? Аддоны из него останутся установленными.')) return;
  try { await api('api/store/repos/' + encodeURIComponent(slug), {method:'DELETE'}); toast('Удалён'); }
  catch(e){ toast('Ошибка: ' + e.message, true); }
  load();
}

async function mkBackup(){
  if (!confirm('Создать полный бэкап? 1–3 минуты.')) return;
  toast('Создаю бэкап…');
  try { await api('api/backups', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name:'manual_' + new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')})}); toast('Бэкап создан'); }
  catch(e){ toast('Ошибка: ' + e.message, true); }
  load();
}
async function delBackup(slug, name){
  if (!confirm('Удалить бэкап «' + name + '»?')) return;
  try { await api('api/backups/' + encodeURIComponent(slug), {method:'DELETE'}); toast('Удалён'); }
  catch(e){ toast('Ошибка: ' + e.message, true); }
  load();
}

/* --- логи --- */
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
    body.textContent = t || '(пусто)';
    body.scrollTop = body.scrollHeight;
  } catch(e){ body.textContent = 'Ошибка: ' + e.message; }
}
function toggleFollow(){
  const b = document.getElementById('followbtn');
  if (FOLLOW) { clearInterval(FOLLOW); FOLLOW = null; b.textContent = 'Автообновление: выкл'; }
  else { FOLLOW = setInterval(showLogs, 5000); b.textContent = 'Автообновление: вкл'; }
}

/* --- файлы --- */
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
      '<span>' + (it.dir ? '📁 ' : '📄 ') + esc(it.name) + '</span>' +
      '<span class="muted">' + (it.dir ? '' : fmtSize(it.size)) + '</span></div>'
    ).join('') || '<div class="muted">пусто</div>';
    document.querySelectorAll('#flist .fitem').forEach(el => {
      el.onclick = () => el.dataset.dir === '1' ? fload(el.dataset.path) : fopen(el.dataset.path);
    });
    window._fparent = d.parent;
  } catch(e){ toast('Ошибка: ' + e.message, true); }
}
function fup(){ if (window._fparent) fload(window._fparent); }
async function fopen(path){
  try {
    const d = await api('api/files/read?path=' + encodeURIComponent(path));
    CURFILE = d.path;
    document.getElementById('fname').textContent = d.path;
    document.getElementById('fcontent').value = d.content;
    document.getElementById('fmeta').textContent = fmtSize(d.size) + (d.size > 512*1024 ? ' (показаны первые 512 КБ)' : '');
    const dl = document.getElementById('fdl');
    dl.href = BASE + 'api/files/download?path=' + encodeURIComponent(d.path);
    dl.style.display = '';
  } catch(e){ toast('Ошибка: ' + e.message, true); }
}
async function fsave(){
  if (!CURFILE) return toast('Файл не выбран', true);
  try {
    await api('api/files/write', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: CURFILE, content: document.getElementById('fcontent').value})});
    toast('Сохранено (старая версия — .bak)');
  } catch(e){ toast('Ошибка: ' + e.message, true); }
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
  } catch(e){ toast('Ошибка: ' + e.message, true); }
}
async function fnewDir(){
  const name = prompt('Имя новой папки:');
  if (!name) return;
  try {
    await api('api/files/mkdir', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: CURDIR.replace(/\/$/,'') + '/' + name})});
    toast('Папка создана'); fload(CURDIR);
  } catch(e){ toast('Ошибка: ' + e.message, true); }
}
async function frename(){
  if (!CURFILE) return toast('Файл не выбран', true);
  const name = prompt('Новое имя:', CURFILE.split('/').pop());
  if (!name) return;
  try {
    await api('api/files/rename', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: CURFILE, name: name})});
    toast('Переименовано'); fload(CURDIR); fopen(CURDIR.replace(/\/$/,'') + '/' + name);
  } catch(e){ toast('Ошибка: ' + e.message, true); }
}
async function fdel(){
  if (!CURFILE) return toast('Файл не выбран', true);
  if (!confirm('Удалить ' + CURFILE + '?')) return;
  try {
    await api('api/files?path=' + encodeURIComponent(CURFILE), {method:'DELETE'});
    toast('Удалён'); CURFILE = null;
    document.getElementById('fname').textContent = 'файл не выбран';
    document.getElementById('fcontent').value = '';
    document.getElementById('fmeta').textContent = '';
    document.getElementById('fdl').style.display = 'none';
    fload(CURDIR);
  } catch(e){ toast('Ошибка: ' + e.message, true); }
}
async function fuploadFiles(files, dir){
  for (const f of files) {
    toast('Загружаю ' + f.name + ' (' + fmtSize(f.size) + ')…');
    try {
      const r = await fetch(BASE + 'api/files/upload?path=' + encodeURIComponent(dir) +
                            '&name=' + encodeURIComponent(f.name),
                            {method:'POST', body:f, headers:{'Content-Type':'application/octet-stream'}});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      toast('Загружен: ' + f.name);
    } catch(e) { toast('Ошибка ' + f.name + ': ' + e.message, true); break; }
  }
  fload(dir);
}

/* --- консоль --- */
async function loadContainers(){
  const sel = document.getElementById('cshell');
  try {
    const d = await api('api/console/containers');
    const prev = sel.value;
    sel.innerHTML = d.containers.map(c =>
      '<option value="' + esc(c.name) + '">' + esc(c.name) + ' · ' + esc(c.state) + '</option>').join('');
    if (prev) sel.value = prev;
    if (!sel.value) {
      const cli = d.containers.find(c => c.name === 'hassio_cli');
      if (cli) sel.value = cli.name;
    }
  } catch(e){ toast('Консоль недоступна: ' + e.message, true); }
}
async function crun(){
  const sel = document.getElementById('cshell');
  const cmd = document.getElementById('ccmd').value;
  const out = document.getElementById('cout');
  if (!sel.value || !cmd.trim()) return;
  out.textContent = 'выполняю…';
  try {
    const d = await api('api/console/exec', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({container: sel.value, cmd: cmd})});
    out.textContent = (d.output || '(пусто)') + '\n\n[exit code: ' + d.exit_code + ']';
  } catch(e){ out.textContent = 'Ошибка: ' + e.message; }
}
function cquick(cmd){
  document.getElementById('ccmd').value = cmd;
  crun();
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
  ['apps','store','files','logs','backups','system','console'].forEach(t =>
    document.getElementById('tab-' + t).style.display = (t === b.dataset.tab) ? '' : 'none');
  if (b.dataset.tab === 'files') fload(CURDIR);
  if (b.dataset.tab === 'logs') showLogs();
  if (b.dataset.tab === 'console' && !document.getElementById('cshell').options.length) loadContainers();
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

    def _body_stream(self):
        """Тело запроса: Content-Length или chunked (HA ingress при ingress_stream)."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return _ChunkedReader(self.rfile), None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return _FixedReader(self.rfile, length), length

    def _body(self):
        reader, length = self._body_stream()
        if length == 0:
            return {}
        raw = reader.read(MAX_WRITE + 1)
        if len(raw) > MAX_WRITE:
            raise ValueError("too large")
        return json.loads(raw) if raw else {}

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

        if path == "/api/check/tunnel":
            res = [
                check_tunnel("https://ha.virvinsky.ru:8443/"),
                check_tunnel("https://files.virvinsky.ru:8443/login"),
            ]
            return self._json(200, {"checks": res, "ok": all(r["ok"] for r in res)})

        if path == "/api/console/containers":
            if not docker_available():
                return self._json(503, {"error": "docker socket недоступен (docker_api/protection)"})
            items, err = docker_containers()
            if err:
                return self._json(502, {"error": err})
            return self._json(200, {"docker": True, "containers": items})

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
                return self._json(400, {"error": err or "не файл"})
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

        # Загрузка файла: тело запроса — сырые байты, поэтому обрабатываем до _body()
        if path == "/api/files/upload":
            qs = parse_qs(urlparse(self.path).query)
            reader, length = self._body_stream()
            if length is not None and length <= 0:
                return self._json(400, {"error": "пустой файл"})
            if length is not None and length > MAX_UPLOAD:
                return self._json(413, {"error": "файл больше %d МБ" % (MAX_UPLOAD // 1048576)})
            dest, err = file_upload(qs.get("path", [""])[0],
                                    qs.get("name", [""])[0], reader)
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"ok": True, "path": dest,
                                    "size": os.path.getsize(dest) if os.path.exists(dest) else None})

        try:
            body = self._body()
        except ValueError:
            return self._json(400, {"error": "invalid json"})

        m = re.match(r"^/api/apps/([A-Za-z0-9_.-]+)/prepull$", path)
        if m:
            res, err = app_prepull(m.group(1))
            if err:
                return self._json(400, {"error": err})
            return self._json(200, dict(res, ok=True))

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

        if path == "/api/console/exec":
            if not docker_available():
                return self._json(503, {"error": "docker socket недоступен (docker_api/protection)"})
            container = str(body.get("container") or "").strip()
            cmd = str(body.get("cmd") or "")
            if not container:
                return self._json(400, {"error": "контейнер не выбран"})
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", container):
                return self._json(400, {"error": "некорректное имя контейнера",
                                        "container": container[:80]})
            if not cmd.strip() or len(cmd) > 4000:
                return self._json(400, {"error": "пустая или слишком длинная команда"})
            out, code, err = docker_exec(container, cmd, timeout=180)
            if err:
                return self._json(502, {"error": err})
            return self._json(200, {"output": out, "exit_code": code, "container": container})

        if path == "/api/store/reload":
            return self._supervisor_action("/store/reload", "reload", {}, timeout=600)

        if path == "/api/store/repos":
            url = str(body.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return self._json(400, {"error": "нужен http(s)-URL"})
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
    """Включает панель в сайдбаре и watchdog: оба флага живут в apps.json, по умолчанию off."""
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
        print("включено: %s (панель в меню / watchdog)" % ", ".join(sorted(body)), flush=True)
    else:
        print("ensure_settings: %s -> %s" % (body, _msg(data)), flush=True)


def ensure_docker():
    """Если в манифесте запрошен docker_api, а сокета нет — снимает защиту и перезапускается."""
    if docker_available():
        return
    status, data = supervisor("/addons/self/info", timeout=20)
    if status != 200 or not isinstance(data, dict):
        return
    info = data.get("data", {}) or {}
    if not info.get("docker_api"):
        return
    if info.get("protected"):
        st, _ = supervisor("/addons/self/security", method="POST",
                           payload={"protected": False}, timeout=30)
        if st != 200:
            print("ensure_docker: не удалось снять protection mode (%s)" % st, flush=True)
            return
        print("ensure_docker: protection mode снят", flush=True)
    guard = "/data/.docker_restart_at"
    try:
        if os.path.exists(guard) and time.time() - os.path.getmtime(guard) < 600:
            return
    except OSError:
        pass
    try:
        with open(guard, "w") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass
    print("ensure_docker: перезапуск, чтобы примонтировать docker.sock", flush=True)
    supervisor("/addons/self/restart", method="POST", timeout=30)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not TOKEN:
        print("FATAL: SUPERVISOR_TOKEN не задан (нужен hassio_api: true)", flush=True)
        sys.exit(1)
    try:
        ensure_settings()
    except Exception as exc:
        print("ensure_settings: %s" % exc, flush=True)
    try:
        ensure_docker()
    except Exception as exc:
        print("ensure_docker: %s" % exc, flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("HA Control %s listening on 0.0.0.0:%d" % (VERSION, PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
