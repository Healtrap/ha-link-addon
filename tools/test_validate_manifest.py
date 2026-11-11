#!/usr/bin/env python3
"""Регрессионные тесты валидатора манифестов.

Главный кейс — реальная авария 2026-11-11: `ssh_port: 0` при схеме `port`
(Supervisor требует 1..65535) → аддон не стартует, туннель не поднимается.
Валидатор обязан ловить это до сборки образа.

Запуск: python3 tools/test_validate_manifest.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import validate_manifest as vm  # noqa: E402

GOOD = """\
name: HA Link
version: "0.4.0"
slug: ha_link
description: test
url: https://example.org
arch:
  - aarch64
  - amd64
startup: application
boot: auto
host_network: true
image: ghcr.io/healtrap/ha-link-addon/ha_link-{arch}
watchdog: http://[HOST]:[PORT:8099]/health
map:
  - config:rw
options:
  config_url: https://example.org/agent.json
  relay_token: ""
  check_interval: 300
  ssh_remote_port: 0
schema:
  config_url: str
  relay_token: password
  check_interval: int(0,86400)
  ssh_remote_port: int(0,65535)
"""


def run(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    errors, warnings = [], []
    try:
        vm.validate(path, errors, warnings)
    finally:
        os.unlink(path)
    return errors, warnings


def check(name, text, expect_errors, contains=None):
    errors, warnings = run(text)
    ok = bool(errors) == expect_errors
    if ok and contains:
        ok = any(contains in e for e in errors)
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print("   errors:", errors)
        print("   warnings:", warnings)
    return ok


def main():
    results = []

    # 1. боевые манифесты должны быть валидны
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("ha_link/config.yaml", "ha_control/config.yaml"):
        path = os.path.join(here, rel)
        errors, warnings = [], []
        vm.validate(path, errors, warnings)
        ok = not errors
        print(("PASS " if ok else "FAIL ") + "manifest %s" % rel)
        if not ok:
            print("   errors:", errors)
        if warnings:
            print("   warnings:", warnings)
        results.append(ok)

    # 2. синтетически корректный манифест
    results.append(check("good manifest", GOOD, False))

    # 3. АВАРИЯ 2026-11-11: port = 0
    bad = GOOD.replace("ssh_remote_port: 0", "ssh_remote_port: 0").replace(
        "ssh_remote_port: int(0,65535)", "ssh_remote_port: port")
    results.append(check("port: 0 rejected (incident 2026-11-11)", bad, True, "1..65535"))

    # 4. watchdog как bool (устаревшая схема)
    results.append(check("watchdog: true rejected",
                         GOOD.replace("watchdog: http://[HOST]:[PORT:8099]/health", "watchdog: true"),
                         True, "должен быть строкой"))

    # 5. image с тегом
    results.append(check("image with tag rejected",
                         GOOD.replace("ha_link-{arch}", "ha_link-{arch}:0.4.0"),
                         True, "не должен содержать тег"))

    # 6. опция вне схемы — только предупреждение
    errors, warnings = run(GOOD.replace("  ssh_remote_port: 0\n", "  ssh_remote_port: 0\n  legacy_key: 1\n"))
    ok = not errors and any("legacy_key" in w for w in warnings)
    print(("PASS " if ok else "FAIL ") + "option outside schema -> warning")
    if not ok:
        print("   errors:", errors, "warnings:", warnings)
    results.append(ok)

    # 7. неверный тип значения
    results.append(check("string default for int schema rejected",
                         GOOD.replace("check_interval: 300", 'check_interval: "300"'),
                         True))

    # 8. ключ схемы без значения по умолчанию — аддон не стартует после обновления
    results.append(check("schema key without default rejected",
                         GOOD.replace("  ssh_remote_port: 0\n", ""),
                         True, "нет значения по умолчанию"))

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
