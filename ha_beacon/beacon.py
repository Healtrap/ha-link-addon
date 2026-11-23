#!/usr/bin/env python3
"""HA Beacon — публикует компактный статус дома на домен (FTP), мимо VPS.

Работает внутри аддона Home Assistant, использует SUPERVISOR_TOKEN для доступа
к API ядра. Раз в interval_minutes пишет beacon.json на хостинг и обновляет
локальный heartbeat. Отдаёт /health для watchdog Supervisor.
"""

import io
import json
import os
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from ftplib import FTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPTIONS_PATH = "/data/options.json"
HEALTH_PORT = 8097
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
RELAY_URL = os.environ.get("RELAY_URL", "https://ha.virvinsky.ru:8443/api/")

TEMPS = {
    "d01": "sensor.sensor_d01_temperatura",
    "g01": "sensor.sensor_g01_temperatura",
    "b01": "sensor.sensor_b01_temperatura",
    "b02": "sensor.sensor_b02_temperatura",
    "b03": "sensor.sensor_b03_temperatura",
}
SWITCHES = {
    "r05": "switch.socket_r05",
    "roz": "switch.rozetka_01",
    "b1": "light.relay_b01",
    "b2": "light.relay_b02",
    "b3": "light.relay_b03",
    "r06": "switch.socket_r06",
}
MODES = {
    "d": "automation.otoplenie_doma_vkl_1_etazh",
    "n": "automation.avariinyi_rezhim_nochnoi_kotel_bez_datchikov",
    "p": "automation.otoplenie_doma_vkl_postoiannyi_progrev",
    "k": "automation.kontrol_nagrevatelia_v_kotelnoy",
    "b1": "automation.otoplenie_bani_vykl_1_etazh",
    "bp": "automation.otoplenie_bani_vkl_podval",
}
BATTERIES = {
    "b01": "sensor.sensor_b01_batareia",
    "b02": "sensor.sensor_b02_batareia",
    "b03": "sensor.sensor_b03_batareia",
}
POWER = {"r05": "sensor.socket_r05_moshchnost"}
ADDONS = {"link": "ha_link", "control": "ha_control"}

started_at = time.time()
state_lock = threading.Lock()
state = {"last_ok": None, "last_error": None, "payload": None}


def log(message):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def load_options():
    if not os.path.exists(OPTIONS_PATH):
        return {}
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as error:  # noqa: BLE001
        log(f"options read failed: {error}")
        return {}


def api_get(path):
    url = f"http://supervisor{path}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def num(value, digits=1):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _relay_once(ctx):
    req = urllib.request.Request(RELAY_URL, headers={"User-Agent": "ha-beacon"})
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            return resp.status in (200, 401, 403)
    except urllib.error.HTTPError as error:
        return error.code in (200, 401, 403)
    except Exception:  # noqa: BLE001
        return False


def relay_online(attempts=3, delay=3):
    """Проверяет, живой ли релей (VPS/туннель) — независимо от FTP и HA.

    Туннель иногда переподключается на 1-2 секунды, поэтому одной пробы мало:
    делаем до трёх попыток и считаем релей живым, если ответила хоть одна.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(attempts):
        if _relay_once(ctx):
            return True
        if attempt < attempts - 1:
            time.sleep(delay)
    return False


def build_payload():
    states = api_get("/core/api/states")
    by_id = {item.get("entity_id"): item for item in states}

    def st(entity_id):
        return (by_id.get(entity_id) or {}).get("state")

    payload = {
        "schema": 1,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "up": int(time.time() - started_at),
    }

    try:
        config = api_get("/core/api/config")
        payload["ha"] = config.get("version")
    except Exception:  # noqa: BLE001
        pass

    temps = {}
    critical = []
    for key, entity_id in TEMPS.items():
        value = num(st(entity_id))
        if value is None:
            critical.append(f"t{key}:нет данных")
        else:
            temps[key] = value
    payload["t"] = temps

    payload["on"] = {k: 1 if st(e) == "on" else 0 for k, e in SWITCHES.items()}
    payload["m"] = {k: 1 if st(e) == "on" else 0 for k, e in MODES.items()}

    batteries = {}
    for key, entity_id in BATTERIES.items():
        value = num(st(entity_id), 0)
        if value is not None:
            batteries[key] = int(value)
            if value < 20:
                critical.append(f"b{key}:{int(value)}%")
    payload["b"] = batteries

    payload["p"] = {k: int(num(st(e), 0) or 0) for k, e in POWER.items()}
    if payload["on"].get("r05") and payload["p"].get("r05", 0) < 65:
        critical.append("r05:нет розжига")

    try:
        addons = api_get("/addons")
        items = addons.get("addons") if isinstance(addons, dict) else addons
        if isinstance(items, list):
            by_slug = {a.get("slug"): a for a in items if isinstance(a, dict)}
            payload["a"] = {
                key: (by_slug.get(slug) or {}).get("state", "?")
                for key, slug in ADDONS.items()
            }
    except Exception:  # noqa: BLE001
        pass

    payload["r"] = 1 if relay_online() else 0

    if critical:
        payload["c"] = critical
    return payload


def publish(payload, options):
    host = options.get("ftp_host") or "vh330.timeweb.ru"
    user = options.get("ftp_user") or ""
    password = options.get("ftp_pass") or ""
    path = (options.get("ftp_path") or "public_html/ha").strip("/")

    if not user or not password:
        raise RuntimeError("ftp_user/ftp_pass не заданы в настройках аддона")

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    ftp = FTP(host, timeout=25)
    try:
        ftp.login(user, password)
        ftp.cwd(path)
        ftp.storbinary("STOR beacon.json", io.BytesIO(body))
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()
    return len(body)


def write_heartbeat(payload):
    tmp = "/data/beacon.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, "/data/beacon.json")
    with open("/data/hb", "w", encoding="utf-8") as fh:
        fh.write(str(int(time.time())))


def publish_once(options):
    try:
        payload = build_payload()
        size = publish(payload, options)
        write_heartbeat(payload)
        with state_lock:
            state["last_ok"] = time.time()
            state["last_error"] = None
            state["payload"] = payload
        log(f"published {size} bytes, temps={payload.get('t')}")
        return True
    except Exception as error:  # noqa: BLE001
        with state_lock:
            state["last_error"] = str(error)
        log(f"publish failed: {error}")
        return False


def loop(options):
    interval = max(1, min(60, int(options.get("interval_minutes") or 5))) * 60
    while True:
        ok = publish_once(options)
        time.sleep(interval if ok else 60)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            with state_lock:
                last_ok = state["last_ok"]
                error = state["last_error"]
                payload = state["payload"]
            interval = 300
            try:
                interval = (
                    max(1, int(load_options().get("interval_minutes") or 5)) * 60
                )
            except Exception:  # noqa: BLE001
                pass
            age = None if last_ok is None else int(time.time() - last_ok)
            # Не дёргаем перезапуск из-за пары неудачных попыток: считаем
            # маячок сломанным, только если публикации нет дольше
            # max(3 интервала, 30 минут).
            stale_after = max(interval * 3, 1800)
            fresh = last_ok is not None and age is not None and age < stale_after
            grace = time.time() - started_at < stale_after
            code = 200 if (fresh or grace) else 503
            body = json.dumps(
                {
                    "ok": code == 200,
                    "age": age,
                    "error": error,
                    "ts": (payload or {}).get("ts"),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        with state_lock:
            payload = state["payload"]
        body = json.dumps(payload or {"ok": False}, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A003
        return


def main():
    options = load_options()
    threading.Thread(target=loop, args=(options,), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    log(f"health server on :{HEALTH_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
