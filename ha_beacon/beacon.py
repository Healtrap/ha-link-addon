#!/usr/bin/env python3
"""HA Beacon — публикует компактный статус дома на домен (FTP), мимо VPS.

Работает внутри аддона Home Assistant, использует SUPERVISOR_TOKEN для доступа
к API ядра. Раз в interval_minutes пишет beacon.json на хостинг и обновляет
локальный heartbeat. Отдаёт /health для watchdog Supervisor.

Дополнительно (с 0.2.0) следит за файлом command.json в том же каталоге на
хостинге: если он появился — применяет режимы отопления и удаляет файл, чтобы
команда сработала ровно один раз. Результат пишется в command_result.json.
"""

import ftplib
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
COMMAND_FILE = "command.json"
RESULT_FILE = "command_result.json"
LAST_COMMAND_PATH = "/data/last_command.json"

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

# Зоны отопления и их режимы. Ключи режимов фиксированы: day / night / prog.
ZONES = {
    "house": {
        "day": "automation.otoplenie_doma_vkl_1_etazh",
        "night": "automation.avariinyi_rezhim_nochnoi_kotel_bez_datchikov",
        "prog": "automation.otoplenie_doma_vkl_postoiannyi_progrev",
    },
    "boiler": {
        "day": "automation.otoplenie_doma_vkl_kotelnaia",
        "night": "automation.kotelnaia_postoiannyi_nochnoi_progrev_bez_datchika",
        "prog": "automation.kotelnaia_vkl_postoiannyi_progrev",
    },
    "bath1": {
        "day": "automation.otoplenie_bani_vykl_1_etazh",
        "night": "automation.bania_1_i_etazh_postoiannyi_nochnoi_progrev_bez_datchika",
        "prog": "automation.bania_1_i_etazh_vkl_postoiannyi_progrev",
    },
    "bath2": {
        "day": "automation.otoplenie_bani_vkl_podval",
        "night": "automation.bania_podval_postoiannyi_nochnoi_progrev_bez_datchika",
        "prog": "automation.bania_podval_vkl_postoiannyi_progrev",
    },
}
ZONE_MODES = ("day", "night", "prog", "off")

started_at = time.time()
state_lock = threading.Lock()
state = {"last_ok": None, "last_error": None, "payload": None}
wake = threading.Event()


def log(message):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def service_call(service, entity_ids):
    """POST /core/api/services/<service> для одной или нескольких сущностей."""
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    body = json.dumps({"entity_id": entity_ids}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://supervisor/core/api/services/{service}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


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


def zone_modes(st):
    """Текущий режим каждой зоны: day / night / prog / off."""
    result = {}
    for zone, modes in ZONES.items():
        current = "off"
        for mode, entity_id in modes.items():
            if st(entity_id) == "on":
                current = mode
                break
        result[zone] = current
    return result


def build_payload():
    states = api_get("/core/api/states")
    by_id = {item.get("entity_id"): item for item in states}

    def st(entity_id):
        return (by_id.get(entity_id) or {}).get("state")

    payload = {
        "schema": 1,
        "ts": now_iso(),
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
    payload["z"] = zone_modes(st)

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


def ftp_connect(options):
    host = options.get("ftp_host") or "vh330.timeweb.ru"
    user = options.get("ftp_user") or ""
    password = options.get("ftp_pass") or ""
    path = (options.get("ftp_path") or "public_html/ha").strip("/")

    if not user or not password:
        raise RuntimeError("ftp_user/ftp_pass не заданы в настройках аддона")

    ftp = FTP(host, timeout=25)
    ftp.login(user, password)
    ftp.cwd(path)
    return ftp


def ftp_close(ftp):
    try:
        ftp.quit()
    except Exception:  # noqa: BLE001
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass


def ftp_upload(ftp, name, body):
    ftp.storbinary(f"STOR {name}", io.BytesIO(body))


def ftp_read(ftp, name):
    """Читает файл; None, если файла нет."""
    buffer = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {name}", buffer.write)
    except ftplib.error_perm as error:
        if str(error).startswith("550"):
            return None
        raise
    return buffer.getvalue()


def ftp_delete(ftp, name):
    try:
        ftp.delete(name)
        return True
    except ftplib.error_perm as error:
        if str(error).startswith("550"):
            return False
        raise


def publish(payload, options):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    ftp = ftp_connect(options)
    try:
        ftp_upload(ftp, "beacon.json", body)
    finally:
        ftp_close(ftp)
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


def read_last_command():
    try:
        with open(LAST_COMMAND_PATH, encoding="utf-8") as fh:
            return str(json.load(fh).get("id") or "")
    except Exception:  # noqa: BLE001
        return ""


def write_last_command(command_id):
    try:
        with open(LAST_COMMAND_PATH, "w", encoding="utf-8") as fh:
            json.dump({"id": command_id, "ts": now_iso()}, fh)
    except Exception as error:  # noqa: BLE001
        log(f"last command write failed: {error}")


def apply_command(command):
    """Применяет команду. Возвращает (applied, errors)."""
    applied = {}
    errors = []

    if not isinstance(command, dict):
        return applied, ["команда должна быть объектом"]

    spec = command.get("set")
    if not isinstance(spec, dict) or not spec:
        return applied, ["в команде нет блока set"]

    for zone, raw_mode in spec.items():
        modes = ZONES.get(zone)
        if not modes:
            errors.append(f"неизвестная зона: {zone}")
            continue
        mode = str(raw_mode).strip().lower()
        if mode not in ZONE_MODES:
            errors.append(f"{zone}: неизвестный режим «{raw_mode}»")
            continue
        off_ids = [entity for name, entity in modes.items() if name != mode]
        try:
            if off_ids:
                service_call("automation/turn_off", off_ids)
            if mode != "off":
                service_call("automation/turn_on", modes[mode])
            applied[zone] = mode
        except Exception as error:  # noqa: BLE001
            errors.append(f"{zone}: {error}")

    return applied, errors


def upload_result(ftp, result):
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    ftp_upload(ftp, RESULT_FILE, body)


def process_commands(options):
    ftp = ftp_connect(options)
    try:
        raw = ftp_read(ftp, COMMAND_FILE)
        if raw is None:
            return False

        result = {
            "schema": 1,
            "ts": now_iso(),
            "id": "",
            "ok": False,
            "applied": {},
            "errors": [],
        }
        try:
            command = json.loads(raw.decode("utf-8"))
        except Exception as error:  # noqa: BLE001
            result["errors"] = [f"не разобрать JSON: {error}"]
            upload_result(ftp, result)
            ftp_delete(ftp, COMMAND_FILE)
            log(f"command rejected: {result['errors']}")
            return True

        command_id = str(command.get("id") or command.get("ts") or "")
        result["id"] = command_id

        if command_id and command_id == read_last_command():
            # Уже применяли ранее, а файл остался (например, не удалился) —
            # повторно не применяем, просто убираем.
            ftp_delete(ftp, COMMAND_FILE)
            log(f"command {command_id} already applied, file removed")
            return False

        applied, errors = apply_command(command)
        result["applied"] = applied
        result["errors"] = errors
        result["ok"] = bool(applied) and not errors

        if command_id:
            write_last_command(command_id)
        upload_result(ftp, result)
        ftp_delete(ftp, COMMAND_FILE)

        log(f"command {command_id}: applied={applied} errors={errors}")
        if applied:
            # Дать HA пару секунд обновить состояния и сразу опубликовать отчёт.
            time.sleep(3)
            wake.set()
        return True
    finally:
        ftp_close(ftp)


def command_loop(options):
    interval = max(20, min(600, int(options.get("command_poll_seconds") or 60)))
    log(f"command polling every {interval}s")
    while True:
        try:
            process_commands(options)
        except Exception as error:  # noqa: BLE001
            log(f"command check failed: {error}")
        time.sleep(interval)


def loop(options):
    interval = max(1, min(60, int(options.get("interval_minutes") or 5))) * 60
    while True:
        ok = publish_once(options)
        if ok:
            wake.wait(interval)
            wake.clear()
        else:
            wake.wait(60)
            wake.clear()


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
    threading.Thread(target=command_loop, args=(options,), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    log(f"health server on :{HEALTH_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
