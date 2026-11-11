#!/usr/bin/env python3
"""Тесты самовосстановления (self-heal) в HA Control.

Проверяется поведение, от которого зависит «поднимется ли туннель сам»:

  * работающий аддон -> снимок опций сохраняется, ничего не перезапускаем;
  * остановленный -> POST /start;
  * опции вне диапазона (авария 2026-11-11: ssh_port: 0) -> чинятся;
  * Supervisor отказал в старте -> восстанавливаем последний рабочий снимок;
  * заведомо битая версия + есть обновление -> обновляем и запускаем;
  * не установлен -> не трогаем;
  * ограничитель частоты не даёт устроить цикл перезапусков.

Запуск: python3 tools/test_selfheal.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "ha_control"))
os.environ.setdefault("SUPERVISOR_TOKEN", "test")

import control_server as cs  # noqa: E402

results = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  %s" % (extra,)))
    results.append(bool(cond))


class FakeSup:
    """Минимальный Supervisor API: info / options / start / update."""

    def __init__(self, addons):
        self.addons = addons
        self.calls = []
        self.start_fail = {}
        self.always_fail_start = set()
        self.options_fail = False

    def __call__(self, path, method="GET", payload=None, timeout=30, raw=False):
        self.calls.append((path, method, payload))
        m = re.match(r"^/addons/([^/]+)/(info|options|start|update)$", path)
        if not m:
            return 404, {"message": "no route"}
        slug, action = m.group(1), m.group(2)
        addon = self.addons.get(slug)
        if addon is None:
            return 404, {"message": "unknown addon"}

        if action == "info":
            return 200, {"data": dict(addon)}

        if action == "options":
            if self.options_fail:
                return 400, {"message": "Invalid options"}
            if payload and "options" in payload:
                addon["options"] = dict(payload["options"])
            if payload and "watchdog" in payload:
                addon["watchdog"] = payload["watchdog"]
            if payload and "boot" in payload:
                addon["boot"] = payload["boot"]
            return 200, {"data": {}}

        if action == "start":
            if slug in self.always_fail_start:
                return 400, {"message": "cannot start"}
            queue = self.start_fail.get(slug) or []
            if queue:
                return 400, {"message": queue.pop(0)}
            addon["state"] = "started"
            return 200, {"data": {}}

        if action == "update":
            addon["version"] = addon.get("version_latest", addon["version"])
            addon["update_available"] = False
            return 200, {"data": {}}

        return 404, {"message": "no route"}


def addon(state="started", version="0.4.0", **kw):
    data = {
        "installed": True,
        "state": state,
        "version": version,
        "version_latest": version,
        "update_available": False,
        "watchdog": True,
        "boot": "auto",
        "options": {"relay_token": "SECRET", "config_url": "https://x/agent.json",
                    "check_interval": 300, "files_token": "", "files_domain": "f.example",
                    "ssh_remote_port": 0},
    }
    data.update(kw)
    return data


TMP = tempfile.mkdtemp(prefix="selfheal-")
cs.SELFHEAL_DIR = TMP


def reset(addons, **kw):
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    cs._heal_state.clear()
    cs.SELFHEAL_MIN_GAP = 120
    cs.SELFHEAL_MAX_PER_HOUR = 5
    fake = FakeSup(addons)
    for key, value in kw.items():
        setattr(fake, key, value)
    cs.supervisor = fake
    return fake


SLUG = "4f3d5ae8_ha_link"

# 1. не установлен -> ничего не делаем
fake = reset({SLUG: addon(installed=False)})
rep = cs.heal_once(SLUG)
check("not installed -> skip", rep.get("state") == "not_installed", rep)
check("not installed -> no start", not [c for c in fake.calls if c[0].endswith("/start")], fake.calls)

# 2. работает -> снимок сохранён, старт не вызывается
fake = reset({SLUG: addon()})
rep = cs.heal_once(SLUG)
snap = os.path.join(TMP, SLUG + ".options.json")
check("started -> health running", rep.get("health") == "running", rep)
check("started -> snapshot written", os.path.isfile(snap), snap)
check("started -> no start call", not [c for c in fake.calls if c[0].endswith("/start")], fake.calls)
if os.path.isfile(snap):
    saved = json.load(open(snap, encoding="utf-8"))
    check("snapshot keeps relay_token", saved["options"].get("relay_token") == "SECRET", saved)

# 3. остановлен с валидными опциями -> просто стартуем
fake = reset({SLUG: addon(state="stopped")})
rep = cs.heal_once(SLUG)
check("stopped -> started", rep.get("started") is True, rep)
check("stopped -> start called once",
      len([c for c in fake.calls if c[0].endswith("/start")]) == 1, fake.calls)

# 4. АВАРИЯ 2026-11-11: опция вне диапазона -> чиним и запускаем
bad = addon(state="stopped")
bad["options"]["ssh_remote_port"] = 70000
fake = reset({SLUG: bad})
rep = cs.heal_once(SLUG)
posts = [c for c in fake.calls if c[0].endswith("/options") and c[1] == "POST"]
check("out-of-range -> options repaired",
      posts and posts[0][2].get("options", {}).get("ssh_remote_port") == 65535, posts)
check("out-of-range -> started", rep.get("started") is True, rep)

# 5. нечисловое значение -> ключ убираем (Supervisor подставит default)
bad = addon(state="stopped")
bad["options"]["ssh_remote_port"] = "abc"
fake = reset({SLUG: bad})
rep = cs.heal_once(SLUG)
posts = [c for c in fake.calls if c[0].endswith("/options") and c[1] == "POST"]
check("bad type -> key dropped",
      posts and "ssh_remote_port" not in posts[0][2].get("options", {}), posts)

# 6. старт отказан -> восстанавливаем снимок и стартуем снова
fake = reset({SLUG: addon(state="started")})
cs.heal_once(SLUG)  # создаёт снимок рабочих опций
fake.addons[SLUG]["state"] = "stopped"
fake.addons[SLUG]["options"]["ssh_remote_port"] = 70000  # «испорченные» опции
fake.start_fail[SLUG] = ["Invalid options"]
rep = cs.heal_once(SLUG)
check("failed start -> snapshot restored", rep.get("restored") is True, rep)
check("failed start -> eventually started", rep.get("started") is True, rep)
check("restored options are the good ones",
      fake.addons[SLUG]["options"].get("ssh_remote_port") == 0, fake.addons[SLUG]["options"])

# 7. битая версия + обновление -> update, затем start
fake = reset({SLUG: addon(state="stopped", version="0.3.1", version_latest="0.4.0",
                          update_available=True)})
rep = cs.heal_once(SLUG)
check("broken version -> update called", rep.get("update") is True, rep)
check("broken version -> started after update", rep.get("started") is True, rep)
check("broken version -> version bumped", fake.addons[SLUG]["version"] == "0.4.0",
      fake.addons[SLUG]["version"])

# 8. ограничитель частоты: не больше 5 попыток в час
fake = reset({SLUG: addon(state="stopped")})
fake.always_fail_start.add(SLUG)
cs.SELFHEAL_MIN_GAP = 0
cs.SELFHEAL_MAX_PER_HOUR = 5
for _ in range(5):
    cs.heal_once(SLUG)
rep = cs.heal_once(SLUG)
check("rate limit -> skipped", rep.get("skipped") == "rate limited", rep)
check("rate limit -> exactly 5 starts",
      len([c for c in fake.calls if c[0].endswith("/start")]) == 5, fake.calls)

# 9. repair_options/known_problems — юнит-уровень
check("known_problems empty for valid", cs.known_problems({"ssh_remote_port": 0}) == [])
check("known_problems finds 70000",
      bool(cs.known_problems({"ssh_remote_port": 70000})))
fixed, changed = cs.repair_options({"ssh_remote_port": -5, "check_interval": 999999})
check("repair clamps both",
      fixed == {"ssh_remote_port": 0, "check_interval": 86400} and len(changed) == 2, (fixed, changed))
fixed, _ = cs.repair_options({"relay_token": "x", "unknown_key": 1})
check("repair never drops unknown keys", fixed == {"relay_token": "x", "unknown_key": 1}, fixed)

# 10. снимок не пишется, если опции плохие и починить не удалось
fake = reset({SLUG: addon(state="started")})
fake.addons[SLUG]["options"]["ssh_remote_port"] = 70000
fake.options_fail = True
cs.heal_once(SLUG)
check("bad options + failed repair -> no snapshot",
      not os.path.isfile(os.path.join(TMP, SLUG + ".options.json")))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d/%d passed" % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
