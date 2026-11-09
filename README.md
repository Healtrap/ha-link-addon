# HA Link add-ons

Репозиторий аддонов Home Assistant для проекта HA Link.

## Установка

1. HA → **Настройки → Apps → ⋮ (справа сверху) → Repositories**
2. Добавить URL:
   ```
   https://github.com/Healtrap/ha-link-addon
   ```
3. **Найти HA Link → Install**.
4. В **Configuration** указать `relay_token` (токен FRP) и `files_token` (пароль файлового доступа).
5. **Start**.

## Обновление

После пуша новой версии в этот репозиторий:

**Apps → ⋮ → Check for updates → HA Link → Update**

## Состав

- `ha_link/` — аддон: динамический FRP-клиент + файловый сервис (web + WebDAV).
