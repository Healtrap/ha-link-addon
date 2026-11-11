#!/usr/bin/env python3
"""Статическая проверка config.yaml аддонов Home Assistant.

Ловит ошибки, которые Supervisor обнаруживает только в момент запуска аддона
(и которые в худшем случае оставляют систему без удалённого доступа).

Проверяется:
  * обязательные ключи, формат version/slug/arch/boot/startup/hassio_role;
  * `image:` без тега (тег Supervisor подставляет сам из version);
  * каждый ключ `options` присутствует в `schema` и наоборот;
  * значение по умолчанию соответствует типу схемы (в т.ч. port = 1..65535);
  * `watchdog`/`webui` — строка с шаблоном URL (в новом Supervisor watchdog
    уже не bool, а URL);
  * `map` — только известные типы.

Запуск:  python3 tools/validate_manifest.py ha_link/config.yaml [ещё файлы]
Код возврата 1, если есть ошибки.
"""
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("нужен PyYAML: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

ARCH_OK = {"aarch64", "amd64", "armv7", "armhf", "i386"}
BOOT_OK = {"auto", "manual", "manual_only"}
STARTUP_OK = {"initialize", "system", "services", "application", "once"}
ROLE_OK = {"default", "homeassistant", "backup", "manager", "admin"}
MAP_OK = {
    "homeassistant_config", "app_config", "all_app_configs", "local_apps",
    "ssl", "share", "media", "backup", "data",
    # устаревшие, но принимаемые
    "config", "addons", "all_addon_configs", "addon_config",
}
REQUIRED = ("name", "version", "slug", "description", "arch")

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
IMAGE_RE = re.compile(r"^[a-z0-9.\-]+(:\d+)?/[a-z0-9.\-_/{}\[\]]+$")
URL_TEMPLATE_RE = re.compile(r"^(https?|tcp)://(\[HOST\]|[a-z0-9.\-]+)(:(\[PORT:\d+\]|\d+))?(/\S*)?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"^(https?|ftp)://\S+$")


def parse_type(spec):
    """'port' -> ('port', None, None, False); 'int(0,65535)' -> ('int', 0, 65535, False)."""
    if not isinstance(spec, str):
        return None
    text = spec.strip()
    optional = text.endswith("?")
    if optional:
        text = text[:-1].strip()
    m = re.match(r"^([a-z_]+)(?:\((.*)\))?$", text)
    if not m:
        return None
    name, args = m.group(1), m.group(2)
    lo = hi = None
    if args is not None and name in ("int", "float", "str", "password"):
        parts = args.split(",")
        if len(parts) != 2:
            return None
        lo = parts[0].strip()
        hi = parts[1].strip()
        lo = None if lo == "" else (float(lo) if name == "float" else int(lo))
        hi = None if hi == "" else (float(hi) if name == "float" else int(hi))
    return (name, lo, hi, optional)


def check_value(value, spec, where, errors):
    parsed = parse_type(spec)
    if parsed is None:
        # list(a|b|c) и прочие составные типы
        if isinstance(spec, str) and spec.strip().startswith("list(") and spec.strip().endswith(")"):
            allowed = [x.strip() for x in spec.strip()[5:-1].split("|")]
            if value not in allowed:
                errors.append("%s: %r не входит в list(%s)" % (where, value, "|".join(allowed)))
            return
        if isinstance(spec, str) and spec.strip().startswith("match("):
            pattern = spec.strip()[6:-1]
            if not isinstance(value, str) or not re.search(pattern, value):
                errors.append("%s: %r не соответствует match(%s)" % (where, value, pattern))
            return
        if isinstance(spec, dict):
            check_mapping(value, spec, where, errors)
            return
        if isinstance(spec, list):
            if not isinstance(value, list):
                errors.append("%s: ожидался список, получено %r" % (where, value))
                return
            item_spec = spec[0] if spec else "str"
            for i, item in enumerate(value):
                check_value(item, item_spec, "%s[%d]" % (where, i), errors)
            return
        errors.append("%s: неизвестный тип схемы %r" % (where, spec))
        return

    name, lo, hi, _optional = parsed
    if name in ("str", "password"):
        if not isinstance(value, str):
            errors.append("%s: ожидалась строка, получено %r" % (where, value))
            return
        if lo is not None and len(value) < lo:
            errors.append("%s: строка короче %s" % (where, lo))
        if hi is not None and len(value) > hi:
            errors.append("%s: строка длиннее %s" % (where, hi))
    elif name == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append("%s: ожидалось целое, получено %r" % (where, value))
            return
        if lo is not None and value < lo:
            errors.append("%s: %s меньше минимума %s" % (where, value, lo))
        if hi is not None and value > hi:
            errors.append("%s: %s больше максимума %s" % (where, value, hi))
    elif name == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append("%s: ожидалось число, получено %r" % (where, value))
            return
        if lo is not None and value < lo:
            errors.append("%s: %s меньше минимума %s" % (where, value, lo))
        if hi is not None and value > hi:
            errors.append("%s: %s больше максимума %s" % (where, value, hi))
    elif name == "bool":
        if not isinstance(value, bool):
            errors.append("%s: ожидался bool, получено %r" % (where, value))
    elif name == "port":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append("%s: порт должен быть целым, получено %r" % (where, value))
            return
        if not 1 <= value <= 65535:
            errors.append(
                "%s: порт %s вне диапазона 1..65535 — Supervisor откажется запускать аддон"
                % (where, value))
    elif name == "email":
        if not isinstance(value, str) or not EMAIL_RE.match(value):
            errors.append("%s: не похоже на email: %r" % (where, value))
    elif name == "url":
        if not isinstance(value, str) or not URL_RE.match(value):
            errors.append("%s: не похоже на URL: %r" % (where, value))
    elif name == "device":
        if not isinstance(value, str) or not value.startswith("/"):
            errors.append("%s: ожидался путь устройства, получено %r" % (where, value))
    else:
        errors.append("%s: неизвестный тип схемы %r" % (where, name))


def check_mapping(value, spec, where, errors):
    if not isinstance(value, dict):
        errors.append("%s: ожидался словарь, получено %r" % (where, value))
        return
    for key, sub in spec.items():
        if key not in value:
            parsed = parse_type(sub)
            if parsed is None or not parsed[3]:
                errors.append("%s.%s: обязательный ключ отсутствует" % (where, key))
            continue
        check_value(value[key], sub, "%s.%s" % (where, key), errors)


def validate(path, errors, warnings):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        errors.append("файл не является YAML-словарём")
        return

    for key in REQUIRED:
        if key not in cfg or cfg[key] in (None, ""):
            errors.append("нет обязательного ключа %r" % key)
    if errors:
        return

    if not isinstance(cfg["version"], str) or not VERSION_RE.match(cfg["version"]):
        errors.append("version должна быть строкой вида 1.2.3 в кавычках, получено %r" % cfg["version"])
    if not SLUG_RE.match(str(cfg["slug"])):
        errors.append("slug %r содержит недопустимые символы" % cfg["slug"])

    arch = cfg.get("arch")
    if not isinstance(arch, list) or not arch:
        errors.append("arch должен быть непустым списком")
    else:
        for a in arch:
            if a not in ARCH_OK:
                errors.append("arch: неизвестная архитектура %r" % a)

    for key, allowed in (("boot", BOOT_OK), ("startup", STARTUP_OK), ("hassio_role", ROLE_OK)):
        if key in cfg and cfg[key] not in allowed:
            errors.append("%s: недопустимое значение %r" % (key, cfg[key]))

    image = cfg.get("image")
    if image is not None:
        if not isinstance(image, str) or not image.strip():
            errors.append("image должен быть непустой строкой")
        elif "@" in image:
            errors.append("image не должен содержать digest (@sha256:...): %r" % image)
        elif ":" in image.split("/")[-1]:
            errors.append("image не должен содержать тег: %r (Supervisor подставит version)" % image)
        elif not IMAGE_RE.match(image):
            errors.append("image должен быть вида ghcr.io/user/repo/{arch}, получено %r" % image)

    for key in ("watchdog", "webui"):
        if key in cfg:
            val = cfg[key]
            if not isinstance(val, str):
                errors.append(
                    "%s должен быть строкой-URL (например \"http://[HOST]:[PORT:8099]/health\"), "
                    "получено %r" % (key, val))
            elif not URL_TEMPLATE_RE.match(val):
                errors.append("%s: не разобрать шаблон URL %r" % (key, val))

    if "ingress_port" in cfg:
        port = cfg["ingress_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            errors.append("ingress_port должен быть целым 0..65535, получено %r" % port)

    for entry in cfg.get("map") or []:
        if isinstance(entry, str):
            kind = entry.split(":")[0]
        elif isinstance(entry, dict):
            kind = entry.get("type")
        else:
            errors.append("map: непонятная запись %r" % (entry,))
            continue
        if kind not in MAP_OK:
            errors.append("map: неизвестный тип %r" % kind)

    options = cfg.get("options") or {}
    schema = cfg.get("schema")
    if schema is False:
        warnings.append("schema: false — валидация опций отключена (осознанно?)")
        return
    schema = schema or {}
    if not isinstance(options, dict) or not isinstance(schema, dict):
        errors.append("options и schema должны быть словарями")
        return

    for key, spec in schema.items():
        if key not in options:
            parsed = parse_type(spec)
            if parsed is None or not parsed[3]:
                warnings.append("schema.%s: нет значения по умолчанию — пользователь обязан заполнить" % key)
            continue
        check_value(options[key], spec, "options.%s" % key, errors)

    for key in options:
        if key not in schema:
            warnings.append("options.%s: ключа нет в schema — Supervisor будет ругаться при каждом старте" % key)


def main(argv):
    paths = argv[1:]
    if not paths:
        print("usage: validate_manifest.py <config.yaml> [...]", file=sys.stderr)
        return 2
    failed = 0
    for path in paths:
        errors, warnings = [], []
        try:
            validate(path, errors, warnings)
        except Exception as exc:  # noqa: BLE001 - отчёт важнее трейсбека
            errors.append("не удалось разобрать файл: %s" % exc)
        print("== %s" % path)
        for w in warnings:
            print("   warn: %s" % w)
        for e in errors:
            print("   ERROR: %s" % e)
        if errors:
            failed += 1
        else:
            print("   ok")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
