# Личный токен VK: потоки авторизации и что из них работает

Этот документ отвечает на вопрос «как получить `VK_USER_TOKEN`, если в интерфейсе dev.vk.com не
получается», и отделяет **замеренное** от **предполагаемого**.

## Зачем он вообще нужен

`video.get` (входящее видео) и `video.save` (исходящее) — методы **пользовательского** уровня. Ключ
сообщества VK на них отвечает ошибкой 5 «User authorization failed» (замерено 03.10.2026 на нашем
собственном ключе: `hermes send ... MEDIA:/tmp/vk-probe.mp4` → `VK video.save failed (5)`). Всё
остальное в канале работает без личного токена: видео приходит пометкой, файл уходит документом.

## Три потока и их требования

| Поток | Кто выдаёт | Что нужно | Секрет приложения | Срок жизни ключа |
|---|---|---|---|---|
| Implicit (`response_type=token`) | `oauth.vk.com/authorize` | приложение типа «Внешнее» (Standalone); ключ отдаётся прямо в адресной строке | нет | долгий (по опыту сообщества — годы) |
| Authorization Code (`response_type=code`) | `oauth.vk.com/authorize` → обмен на `oauth.vk.com/access_token` | приложение с «Защищённым ключом» + `redirect_uri` | да | зависит от приложения; для старых внешних приложений — долгий |
| VK ID (Authorization Code Flow + PKCE) | `id.vk.com/authorize` → обмен на `id.vk.com/oauth2/auth` | приложение VK ID; `code`, `device_id` от SDK, `code_verifier` (PKCE) | да | **короткий: `expires_in` обычно 3600 с**, выдаётся `refresh_token` |

### Что замерено на живых endpoint'ах (без ключей, воспроизводимо)

Проверка делается самим инструментом, значения не печатаются:

```bash
python scripts/vk-user-token.py exchange --app-id 1 --code FICTITIOUS --dry-run            # состав запроса
VK_APP_SECRET=x python scripts/vk-user-token.py exchange --app-id 1 --code FICTITIOUS      # ответ VK
```

Фактические ответы VK (04.10.2026):

| Запрос | Ответ VK | Что это доказывает |
|---|---|---|
| `POST https://oauth.vk.com/access_token` (без верного секрета) | `{"error":"invalid_client","error_description":"client_secret is incorrect"}` | легаси-обмен кода **жив** и требует `client_id` + `client_secret` + `redirect_uri` + `code` |
| `POST https://id.vk.com/oauth2/auth` | `{"error":"invalid_request","error_description":"device id is missing"}` | VK ID **дополнительно** требует `device_id`; без SDK-потока ручной обмен неполон |
| `GET https://oauth.vk.com/authorize?...&response_type=token&scope=video&v=5.199` | HTTP 200, страница авторизации | implicit-поток на стороне API не закрыт |

Официальную документацию на этих страницах прочитать не удалось: `dev.vk.com` и `id.vk.com` отдают 403
ботам, страницы — SPA, а браузер в текущей конфигурации Hermes заблокирован (`browser.use_real_profile`
включён, браузер по умолчанию не Chromium). Поэтому срок жизни VK ID-токена ниже приведён по
**фрагменту официальной справки VK ID**, попавшему в поисковую выдачу (`expires_in: 3600`, поля
`access_token`, `refresh_token`, `id_token`, `scope`), а не по прочитанной странице — это помечено как
непроверенное напрямую.

## Порядок действий для владельца

```bash
cd <plugin-dir>
P=<hermes-install>/venv/bin/python

# 1. Секрет приложения («Защищённый ключ» из настроек приложения) — в файл, не в чат и не в аргументы
install -m 600 /dev/stdin ~/.hermes/vk-app-secret     # вставить ключ, Ctrl-D

# 2. Ссылка согласия. Для «Внешнего» приложения самый простой путь — implicit:
$P scripts/vk-user-token.py authorize --app-id <ID> --flow implicit

# 3. Открыть ссылку, подтвердить доступ «Видео», затем:
$P scripts/vk-user-token.py store --token <access_token из адресной строки>     # implicit
$P scripts/vk-user-token.py exchange --app-id <ID> --code <code>               # code flow

# 4. Проверить ключ (принят? есть ли право «Видео»?):
$P scripts/vk-user-token.py status

# 5. Подхватить ключ живым каналом:
hermes gateway restart
```

Если VK вернул `refresh_token`, инструмент сохранил и его, а также `VK_APP_ID` и `VK_AUTH_FLOW`, поэтому
обновление ключа после истечения — одна команда:

```bash
$P scripts/vk-user-token.py refresh --app-id <ID>
```

## Чего не делать

- Не передавать в переписку ни секрет приложения, ни токен, ни код авторизации. Инструмент специально
  читает секрет из файла и никогда не печатает значения — пишет прямо в `.env` (права 600).
- Не вставлять секрет в командную строку: он попадёт в историю шелла и в `ps`. По той же причине
  `exchange` и `refresh` не принимают секрет аргументом.
- Не рассчитывать на VK ID без `device_id` и PKCE-верификатора: обмен вернёт «device id is missing».
- Не хранить короткоживущий ключ как «настроил один раз и забыл»: без `refresh` он перестанет работать
  через час, а канал этого не заметит до первой попытки видео.

## Итог по надёжности

- **Implicit-поток** (если приложение типа «Внешнее» создаётся) — самый простой и долгоживущий: секрет не
  нужен, обмена нет, `store` + рестарт.
- **Code-поток на легаси-endpoint** — рабочий компромисс: секрет нужен, но ключ долгий.
- **VK ID** — самый «правильный» по современным стандартам и самый неудобный здесь: рассчитан на SDK на
  веб-странице (`device_id`, PKCE), короткий ключ и обязательный `refresh`. Для канала, где видео —
  единственная причина личного токена, это самая дорогая из трёх дорог; выбирать её стоит только если
  первые две недоступны.
