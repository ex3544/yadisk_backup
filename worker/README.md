# Cloudflare Worker: отчёты и мониторинг

Принимает диагностические ZIP-отчёты от клиента и пересылает их в Telegram,
принимает heartbeat-сводки и алерты.

## Маршруты

Все, кроме `/feedback`, требуют заголовок `X-Report-Key`.

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | проверка бота и чата |
| POST | `/report` | multipart, поле `file` (ZIP до 45 МБ) → `sendDocument` |
| POST | `/message` | `{"text": "..."}` — тестовое сообщение |
| POST | `/feedback` | **без ключа**: сообщение клиента (имя, почта, телефон, текст) + необязательный ZIP → Telegram |
| POST | `/heartbeat` | JSON-сводка; `alerts[]` сразу уходят в Telegram |
| GET | `/summary` | сводка по установкам (нужна привязка KV) |

`/feedback` публичный намеренно: форма «Написать разработчику» должна работать
у клиента без настройки ключа. Ограничения — имя до 80, почта до 120, телефон
до 40, текст до 4000 символов, ZIP до 45 МБ; скрытое поле `website` служит
ловушкой для ботов.

## Секреты

```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler secret put REPORT_KEY
```

## KV для heartbeat (необязательно)

Без KV `/heartbeat` принимается и алерты доставляются, но `/summary` отвечает
`501 kv_not_bound`. Чтобы включить:

```bash
npx wrangler kv namespace create HEARTBEATS
```

Полученный `id` вписать в `wrangler.toml` (блок `[[kv_namespaces]]` там
закомментирован) и задеплоить:

```bash
npx wrangler deploy
```

Записи живут 30 дней (`expirationTtl`), сводка отдаётся по последнему heartbeat
каждой установки.
