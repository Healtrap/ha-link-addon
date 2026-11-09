# HA Link — add-on v0.2.0

Замена FRP-клиента **плюс** удалённый доступ к файлам HA.

## Что делает

- Читает `agent.json` при старте и далее с интервалом `check_interval`
  (по умолчанию 300 с, `0` — только при старте).
- Держит туннель к VPS: `ha.virvinsky.ru:8443` → HA (`127.0.0.1:8123`).
- Если `files_token` задан — поднимает файловый сервис:
  - веб-менеджер: `https://files.virvinsky.ru:8443/`
  - WebDAV: `https://files.virvinsky.ru:8443/dav/`
  - доступ к `/config`, `/share`, `/addons`.

## Установка

1. Скопируй папку `ha_link` в `/addons/ha_link/` на HA
   (Samba-шара `addons` или File Editor).
2. HA → **Настройки → Apps → Install app → ⋮ → Check for updates**.
3. **Local apps → HA Link → Install** (пересоберёт образ, нужен интернет).
4. **Configuration**:
   - `relay_token`: токен FRP (`auth.token` из `/etc/frp/frps.toml` на VPS)
   - `files_token`: придумай длинный пароль для файлов (пусто — выключено)
   - `files_domain`: `files.virvinsky.ru`
5. **Start**.

## Файловый сервис

Веб: открыть `https://files.virvinsky.ru:8443/`, ввести `files_token`.
WebDAV в Windows: `\\files.virvinsky.ru@SSL@8443\dav\config`
(логин `ha`, пароль = `files_token`).

Безопасность:
- только HTTPS, токен в cookie (HttpOnly, Secure) для веба и Basic для WebDAV;
- песочница: доступны только `/config`, `/share`, `/addons`, выйти за них нельзя;
- ограничение размера загрузки (по умолчанию 1 ГБ);
- все операции пишутся в лог аддона.

## Важно

- Не держи одновременно старый FRP Client: конфликт имени прокси `homeassistant`.
- Сертификат и nginx на VPS настраиваются отдельно (`RECOVERY.md`).
