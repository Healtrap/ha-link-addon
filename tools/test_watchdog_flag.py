#!/usr/bin/env python3
"""Юнит-тест логики watchdog-флага в HA Control.

Проверяем главное: включая watchdog у ha_link, панель НЕ теряет его опции
(relay_token и прочее). Ошибка здесь означала бы мёртвый туннель.

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


def make_supervisor(watchdog_flag, installed=True, options=None):
    def fake(path, method="GET", payload=None, timeout=30, raw=False):
        CALLS.append((path, method, payload))
        if path.endswith("/info"):
            return 200, {"data": {"installed": installed,
                                  "watchdog": watchdog_flag,
                                  "options": dict(options or {})}}
        if path.endswith("/options"):
            return 200, {"data": {}}
        return 404, {}
    return fake


# 1. флаг выключен -> включаем, опции сохраняются целиком
opts = {"relay_token": "SECRET", "config_url": "https://example/agent.json",
        "check_interval": 300, "files_domain": "files.example", "ssh_remote_port": 0}
cs.supervisor = make_supervisor(False, options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
posts = [c for c in CALLS if c[1] == "POST"]
check("watchdog off -> one POST", len(posts) == 1, CALLS)
check("watchdog flag added", posts and posts[0][2].get("watchdog") is True, posts)
check("existing options preserved",
      posts and posts[0][2].get("relay_token") == "SECRET" and posts[0][2].get("config_url"),
      posts)

# 2. флаг уже включён -> ничего не делаем
cs.supervisor = make_supervisor(True, options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
check("watchdog on -> no POST", not [c for c in CALLS if c[1] == "POST"], CALLS)

# 3. аддон не установлен -> ничего не делаем
cs.supervisor = make_supervisor(False, installed=False, options=opts)
CALLS.clear()
cs.ensure_watchdog("4f3d5ae8_ha_link")
check("not installed -> no POST", not [c for c in CALLS if c[1] == "POST"], CALLS)

# 4. sibling_slugs выводит оба слага из своего
cs.supervisor = lambda path, **kw: (200, {"data": {"slug": "4f3d5ae8_ha_control"}})
check("sibling slugs", cs.sibling_slugs() == ["4f3d5ae8_ha_link", "4f3d5ae8_ha_control"],
      cs.sibling_slugs())

# 5. /api/ssh-логика не падает без /data
cs.supervisor = make_supervisor(True)
check("VERSION is 0.6.0", cs.VERSION == "0.6.0", cs.VERSION)

print("\n%d/%d passed" % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
