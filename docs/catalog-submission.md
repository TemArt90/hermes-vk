# Заявка в каталог плагинов Hermes — подготовлена, НЕ подана

**Статус:** подготовлено 03.10.2026, **не отправлено**. Решение владельца: «в каталог плагинов пока наш
не выкладываем». Публичного следа нет: PR в `NousResearch/hermes-agent` не открывался (`gh pr list
--author TemArt90` → 0). Заготовка ждёт в форке `TemArt90/hermes-agent`, ветка `catalog/hermes-vk`; файл
для их репозитория — `plugin-catalog/hermes-vk.yaml`.

Каталог работает только через PR владельца репозитория: записи кладутся в `plugin-catalog/<имя>.yaml`
и проверяются схемой и CI их репозитория. Без PR ничего не происходит — поэтому решение «не подаём»
означает ровно то, что ничего не опубликовано.

## Что учесть, когда решишь подавать (≈5 минут)

1. **Пин — SHA коммита, а не объекта тега:** `git rev-parse 'vX.Y.Z^{commit}'`. SHA аннотированного тега
   в записи не сойдётся с их проверкой.
2. **Версия в записи = версия в `plugin.yaml`** на том же коммите. Каталог сверяет именно это: в
   1.3.1–1.3.3 номера разъезжались, и выравнивание стало отдельным релизом 1.3.4.
3. **`capabilities` должны совпадать с реальностью:** `hermes plugins validate <каталог плагина>`
   прогоняет тот же сканер и пробу регистрации, что и CI каталога.
4. **Обновить пин**, если между подготовкой и подачей вышли новые релизы: поля `sha`, `version`, `docs_url`.
5. **Соперник в каталоге уже есть** — `vk-platform` от `web3blind` (`hermes-vk-platform`), и он шире по
   функциям (project lanes, видео и прочее). Поэтому в описании PR стоит честное сравнение «чем мы
   отличаемся», а не заявка на замену.

## Готовая запись (`plugin-catalog/hermes-vk.yaml`)

```yaml
name: hermes-vk
title: VK (ВКонтакте) channel
repo: https://github.com/TemArt90/hermes-vk
sha: aff07d8c5aace4d2b1c3d2e0d9d47caf96d43c2a
version: "1.10.0"
requires_hermes: ">=0.21.3"
description: >-
  VK (ВКонтакте) community channel for Hermes Agent: inbound over the Bots Long Poll API (no public URL,
  no domain, no webhook, no proxy), outbound over messages.send. Markdown is rendered into VK's
  format_data (bold, italic, links) with an allowlist of item types — VK silently voids the whole object
  when any type is unsupported — pipe tables become headings plus bullets and quotes become italics,
  photos/documents/voice are exchanged through VK's own upload servers, and clarify, exec-approval and
  slash-confirm prompts arrive as inline callback keyboards using the shared Hermes button convention,
  each carrying its own prompt id so a press resolves exactly once. An answer can be rewritten in
  place (messages.edit) instead of arriving as a second message, and reaction acks are available
  (VK_REACTIONS_ENABLED: a progress reaction while the agent works, then 👍 or 👎). Group chats can require a mention
  before the community answers (VK_REQUIRE_MENTION, with per-chat overrides and custom patterns), and
  inbound attachment download can be switched off or capped (VK_DOWNLOAD_ATTACHMENTS,
  VK_MAX_ATTACHMENT_BYTES). The command keyboard can be enabled per chat
  (VK_COMMAND_KEYBOARD_BY_PEER), and an opt-in fallback sweep polls the newest conversations when Long
  Poll has been silent for a whole interval (VK_FALLBACK_POLL_ENABLED, _INTERVAL_SECONDS, _BATCH_SIZE),
  feeding the same handler through the same deduplicator. Videos are attempted as native VK video and
  fall back to a document. Operator documentation ships in docs/ (development, troubleshooting,
  update-guide, vk-api-notes). An optional persistent command keyboard (VK_COMMAND_KEYBOARD=true) puts
  /help, /status, /new and /stop under the input field, since VK has no command-list API for community
  bots. Disclosure — the community access token is read from the Hermes profile .env (VK_TOKEN) and sent
  only to api.vk.com; network egress is limited to VK hosts (api.vk.com for API and Long Poll, the upload
  servers VK returns for media, and media CDN hosts from inbound attachments, which the host caches under
  the Hermes media cache); the adapter keeps no state file of its own — last inbound id per chat, dedupe
  ids and pending button ids live in memory only; no telemetry, no self-update, no third-party services.
  Access control is Hermes' own allowlist and pairing (VK_ALLOWED_USERS / VK_ALLOW_ALL_USERS).
maintainer: TemArt90
tier: community
category: platform
docs_url: https://github.com/TemArt90/hermes-vk/blob/aff07d8c5aace4d2b1c3d2e0d9d47caf96d43c2a/README.md
platforms: []
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env:
    - VK_TOKEN
```

## Готовое описание PR

## What

Adds `plugin-catalog/hermes-vk.yaml` for **[hermes-vk](https://github.com/TemArt90/hermes-vk)** — a VK (ВКонтакте) community channel for Hermes Agent. I own that repository (`TemArt90`), per the owner-submitted rule.

Pinned to the `v1.10.0` **release commit**: `aff07d8c5aace4d2b1c3d2e0d9d47caf96d43c2a` (annotated tag `v1.10.0`; the pin is the commit it points at, not the tag object, and it carries the same version string as the plugin manifest).

## Why a second VK entry (there is already `vk-platform`)

A different implementation with a different trade-off — not a replacement:

- **Token-only setup.** The community id is resolved from the token via `groups.getById`, so a user needs one env var (`VK_TOKEN`) instead of a token plus a group id.
- **Rendering fidelity.** Markdown becomes VK's `format_data` (bold, italic, links) under an allowlist of item types — VK silently voids the *entire* object when any item type is unsupported, so the renderer never emits one. Pipe tables become headings plus bullets; `>` quotes become italics; fenced code is left alone.
- **Buttons.** clarify / exec-approval / slash-confirm prompts use the shared Hermes button convention as inline callback keyboards; each card carries its own prompt id, a press resolves exactly once, and an index the prompt never offered is refused rather than turned into an answer the user never gave.
- **In-place editing** (`messages.edit`): long answers and progress rewrite the message already in the chat instead of appending a second one. Two VK limits are handled: the edit endpoint takes no `format_data` (so the text is flattened) and cannot split (over-long content is handed back as `success=False` so the caller sends it anew).
- **Reaction acks** (`VK_REACTIONS_ENABLED`, off by default): a progress reaction while the agent works, then 👍 or 👎. Driven by the core's own `on_processing_start` / `on_processing_complete` hooks, with configurable numbers because VK numbers reactions per community. Reactions are addressed by `cmid`, not by the message id.
- **Mention gate for group chats** (`VK_REQUIRE_MENTION`, with `VK_REQUIRE_MENTION_BY_PEER` overrides and `VK_MENTION_PATTERNS`): without it, a community added to a group chat answers every message in it. The gate runs *before* attachments are fetched, and the mention is stripped from the text the agent receives. Off by default.
- **Attachment policy** (`VK_DOWNLOAD_ATTACHMENTS`, `VK_MAX_ATTACHMENT_BYTES`): inbound media can be declined entirely or capped per install.
- **Optional persistent command keyboard** (`VK_COMMAND_KEYBOARD=true`): `/help`, `/status`, `/new`, `/stop` as one-tap buttons under the input field. VK has no command-list API for community bots (no `setMyCommands` equivalent), so a `text` keyboard is the platform's substitute.
- **68 tests + CI** on a clean GitHub runner, including a mock-transport live loop that needs no token and no network.

## Verification

- `hermes plugins validate <plugin>`: every check passes — `security scan — safe`, `capability probe — register() ran in isolation`, declared tools/hooks/middleware match registrations.
- CI on the pinned commit: **two jobs, both green** — Hermes `main` and `v2026.9.14` (= 0.21.3), `68 passed` each.
- The CI matrix earned its keep immediately: the 0.21.3 job first failed with `No module named 'yaml'`, because that revision's runtime imports PyYAML directly while current main routes YAML through `hermes_yaml` — a missing CI dependency, not a plugin incompatibility. Fixed in the recipe rather than papered over by dropping the older job.
- Live, against a real VK community: inbound → agent → reply; markup verified on the wire through `messages.getHistory` (`format_data` offsets); `messages.edit` verified by reading the edited text back; `messages.sendReaction` / `deleteReaction` verified from the raw responses (`[{"reaction_id":4,"count":1,"user_ids":[-<id сообщества>]}]`, replaced by a second id, then removed); documents, photos and voice messages verified by reading the attachment type back (`doc`, `photo`, `audio_message`); cron delivery; a command-keyboard press identified by the button `payload` in the inbound event.
- **Two defects and one hardening were found before this pin** — stated because they are the reason to trust the rest:
  1. **Approval buttons never rendered.** The approval / clarify / slash-confirm cards built their keyboard with a nested list in the second row, so the builder raised and the adapter silently fell back to plain text: a dangerous command arrived *without* Approve/Deny buttons and *without* the `/approve` steps the core only adds for adapters that have no button support at all.
  2. **Every document and voice message failed.** VK rejects `peer_id=0` for `docs.getMessagesUploadServer` ("peer_id is invalid") while `photos.…` accepts it. Found in the gateway log; then confirmed live for four `peer_id` values (0 → 100, `-<community>` → 901, omitted → "peer_id is required", the conversation peer → accepted).
  3. **Hardening, not a repair:** the ffmpeg lookup for voice transcoding now falls back to the copy Hermes bundles under `tools/` when `PATH` has none. An earlier claim in this description that voice was broken because the service had no ffmpeg was **wrong** and is corrected here: `/usr/bin/ffmpeg` is on the service PATH and encodes Opus — measured afterwards, by encoding with both binaries. The change stays because a host without a system ffmpeg would otherwise send voice notes as files.
  Defects 1 and 2 carry tests that fail when they return (checked by mutation, not by assertion).
- **`requires_hermes: ">=0.21.3"`** — declared because it is measured rather than assumed: CI runs the suite against that revision and against `main`.

## Disclosure

- **Credential.** The community access token is read from the Hermes profile `.env` (`VK_TOKEN`) and sent only to `api.vk.com`.
- **Network egress is limited to VK:** `api.vk.com` (API and Long Poll), the upload servers VK returns for media, and media CDN hosts from inbound attachments — those downloads are cached by the host under the Hermes media cache.
- **No state file of its own.** Last inbound id per chat, dedupe ids (900 s TTL), the chat+message → `cmid` pairs used for reactions, and pending button ids live in memory only (each capped). No telemetry, no self-updating code, no third-party services.
- **Access control** is Hermes' own allowlist and pairing (`VK_ALLOWED_USERS` / `VK_ALLOW_ALL_USERS`); the adapter ships no allowlist logic of its own.

## Checklist

- [x] Owner-submitted (the repository is mine)
- [x] Public repository
- [x] Released (`v1.3.4`, CI badge in the README)
- [x] Exact 40-char commit SHA pin (the release commit)
- [x] No self-updating code
- [x] Declared capabilities match reality (platform plugin: no tools/hooks/middleware; `VK_TOKEN` required)
- [x] `requires_hermes` verified against the declared floor
