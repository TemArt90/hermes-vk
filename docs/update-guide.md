# Что делать после `hermes update`

Плагин живёт в `~/.hermes/plugins/platforms/vk/`, но опирается на внутренности ядра Hermes
(`gateway.platforms.*`). Обновление ядра — единственное событие, которое может сломать плагин, не тронув
его код. Этот файл — чек-лист на такой случай.

## Чек-лист (5 минут)

```bash
# 1. Плагин на месте и включён
hermes plugins list | grep -i vk                     # ожидается: vk-platform … enabled

# 2. Плагин соответствует ожиданиям менеджера и проходит проверку безопасности
hermes plugins validate ~/.hermes/plugins/platforms/vk

# 3. Тесты (самый быстрый детектор сломанных внутренних API ядра)
cd ~/.hermes/hermes-agent && venv/bin/python -m pytest ~/.hermes/plugins/platforms/vk -q

# 4. Канал поднялся
hermes gateway restart
grep -i "VK: connected to community" ~/.hermes/logs/agent.log | tail -1

# 5. Живая проверка: сообщение, кнопка, файл
hermes send --to vk:<peer_id> "проверка после обновления"

# 6. Состояние транспорта
grep -iE "VK: (long poll|fallback|upload|reaction)" ~/.hermes/logs/agent.log | tail -20
```

Если шаг 3 упал — идите в раздел ниже. Если упал шаг 4 при зелёных тестах — смотрите `docs/troubleshooting.md`.

## Что именно может сломаться и где искать

Плагин держится за четыре точки ядра. Их и надо проверять в первую очередь:

| Что импортируем | Где искать в новой версии ядра |
|---|---|
| `gateway.platforms.base` → `BasePlatformAdapter`, `ExecApprovalPrompt`, `SendResult`, `PlatformAdapter` | `search_files("class BasePlatformAdapter", path="~/.hermes/hermes-agent/gateway/platforms")` |
| `gateway.platforms.event` → `MessageEvent`, `MessageType`, `ProcessingOutcome` | тот же каталог, `event.py` |
| `gateway.platforms.helpers` → `MessageDeduplicator`, `cancel_task`, `compile_mention_patterns` | `helpers.py` |
| `gateway.platforms._shared` | `_shared.py` — сюда ядро складывает общие адаптерные утилиты |

Практический приём: после `hermes update` достаточно прогнать тесты — они импортируют ровно эти имена, и
переименование в ядре проявится как `ImportError` с точным именем.

```bash
cd ~/.hermes/hermes-agent && venv/bin/python -m pytest ~/.hermes/plugins/platforms/vk -q 2>&1 | tail -20
```

## Что мы уже переживали

| Что менялось в ядре | Как проявилось | Как вылечено |
|---|---|---|
| Путь к YAML (`yaml` → `hermes_yaml`/`ruamel.yaml`) | CI на старом теге падал `No module named 'yaml'` | в рецепт CI добавлен `PyYAML`, версии запинованы |
| Разметка и её рендер (таблицы) | таблицы VK пришли сырыми `\|---\|---\|` | `render_tables` до рендера + общий `convert_table_to_bullets` из `helpers.py` |
| Хуки обработки (`on_processing_start` / `on_processing_complete`) | реакции не ставились | адаптер реализует оба хука сам, ядро зовёт их из зоны gateway |
| Ограничения отображения | в VK приезжали «рассуждения» агента | `display.platforms.vk.show_reasoning: false` в `config.yaml` |

## Матрица ревизий

CI плагина проверяет **две** ревизии ядра: текущий `main` и самую старую поддерживаемую
(`v2026.9.14` = 0.21.3). Это держит поле `requires_hermes` в заготовке каталога измеренным фактом, а не
предположением. Если после `hermes update` основная ревизия зелёная, а старая — нет, значит появилась
зависимость от нового внутреннего API: либо добавьте совместимость, либо (осознанно) поднимите нижнюю
планку в манифесте и заготовке каталога.

```bash
cd ~/.hermes/plugins/platforms/vk && git push origin main && sleep 25
gh run list --limit 5 --json headSha,status,conclusion --jq '.[] | "\(.headSha[0:7]) \(.status) \(.conclusion)"'
```

## Чего не делать

- **Не патчить ядро Hermes** ради плагина: правка потеряется при следующем `hermes update`.
  Если правка всё-таки нужна — она кладётся патчем в `~/.hermes/patches/` и переприменяется осознанно
  (так живёт патч email-канала), но для VK-плагина такой необходимости нет.
- **Не менять формат вывода на простой текст.** Разметка VK (`format_data`) — единственное, чем этот плагин
  отличается от альтернатив; обнуление `format_data` целым блоком уже один раз произошло из-за
  неподдерживаемого элемента.
- **Не подавать в каталог плагинов**, не спросив владельца (заготовка: `docs/catalog-submission.md`).

## Если нужно откатиться

```bash
cd ~/.hermes/plugins/platforms/vk && git log --oneline -10
git checkout v1.3.4      # или любой предыдущий тег
hermes gateway restart
```

Плагин не пишет своих файлов состояния (кроме кэша вложений агента) и не меняет `config.yaml` при
работе — откат сводится к возврату кода и перезапуску шлюза.
