# HA Link — add-on v0.3.0

Замена FRP-клиента **плюс** удалённый доступ к файлам HA **плюс** аварийный SSH.

## Что делает

- Читает `agent.json` при старте и далее с интервалом `check_interval`
  (по умолчанию 300 с, `0` — только при старте).
- Держит туннель к VPS: `ha.virvinsky.ru:8443` → HA (`127.0.0.1:8123`).
- Если `files_token` задан — поднимает файловый сервис:
  - веб-менеджер: `https://files.virvinsky.ru:8443/`
  - WebDAV: `https://files.virvinsky.ru:8443/dav/`
  - доступ к `/config`, `/share`, `/addons`.

## Установка

Аддон ставится **из репозитория** `https://github.com/Healtrap/ha-link-addon`
(образ из GHCR). Малина ничего не собирает.

1. HA → **Настройки → Apps → ⋮ → Repositories → добавить**
   `https://github.com/Healtrap/ha-link-addon`
2. **Check for updates → HA Link → Install**.
3. **Configuration**:
   - `relay_token`: токен FRP (`auth.token` из `/etc/frp/frps.toml` на VPS)
   - `files_token`: придумай длинный пароль для файлов (пусто — выключено)
   - `files_domain`: `files.virvinsky.ru`
   - `ssh_port`: `0` (выключено) или порт на VPS, например `2222`
   - `ssh_local_port`: `22`
4. **Start**.

## Аварийный SSH

1. Задать `ssh_port` (например `2222`), `ssh_local_port` = `22`.
2. На VPS открыть порт: `ufw allow 2222/tcp comment "ha_link ssh"`.
3. Подключиться:

```bash
ssh -p 2222 root@frp.virvinsky.ru
```

- Порт `22` **на VPS** — это SSH самого сервера, для туннеля его не использовать.
- В Advanced SSH включи вход по ключу и отключи пароль.
- SSH идёт через тот же туннель: умрёт `ha_link` — умрёт и SSH.

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
