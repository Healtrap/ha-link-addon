#!/usr/bin/env python3
"""Юнит-тесты флагов watchdog/boot в HA Control.

Главное, что проверяем: тело запроса к `/addons/<slug>/options` имеет форму
`{"watchdog": true, "boot": "auto"}` и НЕ содержит опций «плоско».

Почему это критично: Supervisor принимает на этом эндпоинте только
`{"options": {...}, "watchdog": bool, "boot": ...}` (voluptuous PREVENT_EXTRA).
Старый код отправлял опции плоско (`{"relay_token": ..., "watchdog": true}`)
и получал 400 — флаг watchdog никогда не включался, а значит Supervisor
не перезапускал упавший туннель. Этот тест — регрессия на ту ошибку.

Запуск: python3 tools/test_watchdog_flag.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "ha_control"))
os.environ.setdefault("SUPERVISOR_TOKEN", "test")

import control_server as cs  # noqa: E402

results = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  %s" % (extra,)))
    results.append(bool(cond))


CALLS = []


def make_supervisor(watchdog_flag, installed=True, boot="auto", options=None):
    def fake(path, method="GET", payload=None, timeout=30, raw=False):
        CALLS.append((path, method, payload))
        if path.endswith("/info"):
            return 200, {"data": {"installed": installed,
                                  "watchdog": watchdog_flag,
                                  "boot": boot,
                                  "options": dict(options or {})}}
        if path.endswith("/options"):
            return 200, {"data": {}}
        return 404, {}
    return fake


opts = {"relay_token": "SECRET", "config_url": "https://example/agent.json",
        "check_interval": 300, "files_domain": "files.example", "ssh_remote_port": 0}

# 1. флаг выключен -> включаем, опции не отправляем вовсе
cs.supervisor = make_supervisor(False, options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
posts = [c for c in CALLS if c[1] == "POST"]
check("watchdog off -> one POST", len(posts) == 1, CALLS)
check("body is {'watchdog': True} only",
      posts and posts[0][2] == {"watchdog": True}, posts)
check("no flat options in body",
      posts and "relay_token" not in posts[0][2], posts)

# 2. флаг уже включён, boot auto -> ничего не делаем
cs.supervisor = make_supervisor(True, options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
check("watchdog on -> no POST", not [c for c in CALLS if c[1] == "POST"], CALLS)

# 3. boot: manual -> переводим в auto (иначе аддон не поднимется после ребута)
cs.supervisor = make_supervisor(True, boot="manual", options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
posts = [c for c in CALLS if c[1] == "POST"]
check("boot manual -> auto", posts and posts[0][2] == {"boot": "auto"}, posts)

# 4. boot: manual_only менять нельзя -> не отправляем boot
cs.supervisor = make_supervisor(True, boot="manual_only", options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
posts = [c for c in CALLS if c[1] == "POST"]
check("boot manual_only untouched", not posts, posts)

# 5. аддон не установлен -> ничего не делаем
cs.supervisor = make_supervisor(False, installed=False, options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
check("not installed -> no POST", not [c for c in CALLS if c[1] == "POST"], CALLS)

# 6. sibling_slugs выводит оба слага из своего
cs.supervisor = lambda path, **kw: (200, {"data": {"slug": "4f3d5ae8_ha_control"}})
check("sibling slugs", cs.sibling_slugs() == ["4f3d5ae8_ha_link", "4f3d5ae8_ha_control"],
      cs.sibling_slugs())

check("VERSION is 0.6.0", cs.VERSION == "0.6.0", cs.VERSION)

print("\n%d/%d passed" % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
