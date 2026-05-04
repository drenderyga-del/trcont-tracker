# trcont-tracker

Небольшой сервис для отслеживания контейнеров на
[isales.trcont.com/tracking](https://isales.trcont.com/tracking?lang=ru).
Раз в заданный интервал открывает страницу трекинга через Playwright/Chromium,
забирает последнюю дислокацию и отправляет изменения в Telegram.

## Что умеет

- отслеживает один или несколько номеров оборудования;
- шлет уведомление только когда событие изменилось;
- пишет локальный state, чтобы не дублировать сообщения после перезапуска;
- считает среднюю скорость по последним точкам истории;
- умеет отправлять heartbeat и алерт при нескольких неуспешных циклах подряд.

## Подготовка Telegram-бота

1. Откройте в Telegram `@BotFather`.
2. Выполните `/newbot`, задайте имя и username.
3. Скопируйте токен бота в `TELEGRAM_BOT_TOKEN`.
4. Напишите боту любое сообщение или добавьте его в нужную группу.
5. Узнайте `chat_id`:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates"
```

В ответе найдите `message.chat.id`. Для групп id обычно начинается с `-100`.
Если получателей несколько, укажите их через запятую.

## Настройка `.env`

Скопируйте пример и заполните свои значения:

```bash
cp .env.example .env
```

Минимальный набор:

```env
TELEGRAM_BOT_TOKEN=123456:your_bot_token
TELEGRAM_CHAT_ID=123456789
EQUIPMENT_NUMBERS=TKRU4553164,ABCD1234567
```

Полезные опции:

```env
# Интервал между циклами опроса, секунд.
POLL_INTERVAL_SECONDS=3600

# Пауза между разными контейнерами внутри одного цикла.
PER_EQUIPMENT_DELAY_SECONDS=3

# Папка для state-файлов. В Docker переопределяется на /app/state.
STATE_DIR=state

# 0 отключает heartbeat.
HEARTBEAT_INTERVAL_SECONDS=86400

# 0 отключает алерт о полностью провальных циклах.
ALERT_AFTER_FULL_FAIL_CYCLES=3

# Сообщение в Telegram при старте сервиса.
SEND_STARTUP_MESSAGE=false

# Окно для расчета средней скорости, часов.
SPEED_WINDOW_HOURS=6

# Сохранять сырые JSON-ответы в STATE_DIR/raw_dislocation/.
SAVE_RAW_DISLOCATION=true
```

Технические настройки Playwright/SmartCaptcha обычно можно не трогать:
`HEADLESS`, `NAV_TIMEOUT_MS`, `SEARCH_TIMEOUT_MS`, `FETCH_ATTEMPTS`,
`CAPTCHA_RETRY_DELAY_S`, `CAPTCHA_BACKOFF_S`, `PRE_CLICK_DELAY_MS`.

## Запуск в Docker

```bash
docker compose build
docker compose up -d
docker compose logs -f tracker
```

Локальное состояние хранится в `./state/` и не должно попадать в Git.

## Локальный запуск без Docker

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python -m app.main
```

## Обновление списка контейнеров

Измените `EQUIPMENT_NUMBERS` в `.env` и перезапустите сервис:

```bash
docker compose restart tracker
```

## Служебные команды

Обрезать локальную историю до последней точки по каждому контейнеру:

```bash
docker compose run --rm tracker python -m app.reset_history
```

## Перед публикацией

В репозиторий не должны попадать:

- `.env` с токеном Telegram;
- папка `state/`;
- логи, дампы браузера и временные файлы.

Для публичного GitHub достаточно публиковать код, `README.md`,
`.env.example`, Docker-файлы и `requirements.txt`.
