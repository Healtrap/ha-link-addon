#!/usr/bin/env python3
"""Тест /health и авторизации файлового сервиса ha_link.

Запускает files_server.py как отдельный процесс (как в контейнере) и проверяет:
  * /health -> 200, пока heartbeat свежий (watchdog считает аддон живым);
  * /health -> 503, когда heartbeat протух (watchdog перезапустит аддон);
  * /health доступен без токена (Supervisor его не передаёт);
  * /api/* без токена -> 401, с X-Token -> 200.

Запуск: python3 tools/test_files_health.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "ha_link", "files_server.py")
TOKEN = "test-token-123"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(300)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(300)


def main():
    root = tempfile.mkdtemp(prefix="hafiles-")
    hb = os.path.join(root, "hb")
    port = free_port()
    env = dict(os.environ)
    env.update({
        "FILES_TOKEN": TOKEN,
        "FILES_PORT": str(port),
        "FILES_ROOTS": root,
        "FILES_HEALTH_FILE": hb,
        "PYTHONIOENCODING": "utf-8",
    })
    with open(hb, "w", encoding="utf-8") as fh:
        fh.write(str(int(time.time())))

    proc = subprocess.Popen([sys.executable, SERVER], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    results = []
    try:
        # ждём подъёма
        up = False
        for _ in range(60):
            try:
                get(base + "/health")
                up = True
                break
            except Exception:
                time.sleep(0.25)
        if not up:
            print("FAIL server did not start")
            print((proc.stdout.read() or b"").decode("utf-8", "replace")[:800])
            return 1

        code, body = get(base + "/health")
        ok = code == 200 and json.loads(body)["ok"] is True
        print(("PASS" if ok else "FAIL") + " /health fresh -> %s %s" % (code, body[:60]))
        results.append(ok)

        # протухший heartbeat
        with open(hb, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time()) - 600))
        code, body = get(base + "/health")
        ok = code == 503
        print(("PASS" if ok else "FAIL") + " /health stale -> %s" % code)
        results.append(ok)

        # нет файла heartbeat вовсе
        os.unlink(hb)
        code, body = get(base + "/health")
        ok = code == 503
        print(("PASS" if ok else "FAIL") + " /health missing heartbeat -> %s" % code)
        results.append(ok)

        # API без токена
        code, _ = get(base + "/api/roots")
        ok = code == 401
        print(("PASS" if ok else "FAIL") + " /api/roots without token -> %s" % code)
        results.append(ok)

        # API с токеном
        code, body = get(base + "/api/roots", {"X-Token": TOKEN})
        ok = code == 200 and b"roots" in body
        print(("PASS" if ok else "FAIL") + " /api/roots with token -> %s %s" % (code, body[:60]))
        results.append(ok)

        # /health не требует токена — уже проверено выше, но фиксируем явно
        with open(hb, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
        code, _ = get(base + "/health", {"X-Token": ""})
        ok = code == 200
        print(("PASS" if ok else "FAIL") + " /health without token -> %s" % code)
        results.append(ok)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
