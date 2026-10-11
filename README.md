# MARTOV SMS Bot

Telegram-бот для продажи виртуальных номеров под SMS-активации (поставщик номеров — GetSMS):
покупка и аренда номеров, почта для активаций, пополнение баланса криптовалютой,
админ-панель прямо в Telegram. Интерфейс на русском, английском и немецком.

## Возможности

- Покупка номеров по странам и сервисам, все ценовые варианты поставщика, Топ-10 дешёвых стран, избранное, поиск.
- Аренда номеров и почта для активаций.
- Автоматическая выдача SMS-кода; если SMS не пришло — деньги автоматически возвращаются на баланс.
- Баланс и цены в $ или € (пользователь выбирает в профиле), курс ЦБ обновляется каждый час.
- Пополнение: CryptoBot, xRocket (минимум — копейки), NOWPayments; зачисление автоматически и строго один раз.
- Живые (анимированные) эмодзи в текстах и на кнопках, кнопки снизу экрана, команды /start, /buy, /fill.
- Защита от абуза: проверка цены на стороне сервера, лимиты покупок, защита от спама, блокировка пользователей.

## Установка

Нужен Python 3.11 или новее.

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt          # Windows: venv\Scripts\pip install -r requirements.txt
cp .env.example .env                               # Windows: copy .env.example .env
```

Заполните `.env`:

| Переменная | Что это |
|---|---|
| `BOT_TOKEN` | токен бота от @BotFather |
| `GETSMS_API_KEY` | API-ключ из кабинета GetSMS (с этого аккаунта оплачиваются номера — держите на нём баланс) |
| `ADMIN_IDS` | Telegram ID администраторов через запятую |
| `BOT_USERNAME` | username бота без @ (для реферальных ссылок и соглашения) |
| `SUPPORT_LINK`, `CHANNEL_LINK`, `SUPPORT_EMAIL` | ссылки, которые видят пользователи |
| `SETTINGS_SECRET` | любая длинная случайная строка; шифрует ключи платёжек. **Задайте один раз и не меняйте** |
| `DATABASE_URL` | по умолчанию SQLite-файл `bot.db` рядом с ботом |
| `NOWPAYMENTS_*`, `BANNER_*` | необязательно |

## Запуск

```bash
venv/bin/python -m bot.main
```

База данных создаётся и обновляется автоматически при запуске.
Не запускайте две копии бота с одним токеном одновременно.

### На сервере (systemd)

```ini
# /etc/systemd/system/martov-sms-bot.service
[Unit]
Description=MARTOV SMS Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/opt/martov-sms-bot
ExecStart=/opt/martov-sms-bot/venv/bin/python -m bot.main
Environment=PYTHONIOENCODING=utf-8
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now martov-sms-bot
journalctl -u martov-sms-bot -f        # логи
```

Делайте резервные копии файла `bot.db` — в нём балансы пользователей.

## Админ-панель

Команда `/admin` (только для `ADMIN_IDS`):

- 👥 Пользователи, 📊 Статистика, 🧾 Последние покупки, 🔍 Найти пользователя (с кнопкой блокировки)
- 💰 Начислить / ➖ Списать баланс, 📨 Рассылка
- 💳 Способы оплаты — ввод ключей, включение/выключение, название кнопки, минимальная сумма
- 💹 Наценка на номера (×1.5, ×2, ×3 или своя)
- 💱 Курсы валют — автоматически по ЦБ или вручную
- 🎨 Живые эмодзи — перешлите сообщение с премиум-эмодзи, бот запомнит их

Команды: `/addbal <id> <сумма>`, `/rembal <id> <сумма>`, `/userinfo <id>`.

Живые эмодзи работают, если у владельца бота (в @BotFather) есть Telegram Premium; иначе бот сам покажет обычные.

## Тесты

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```

Тесты не обращаются к реальным API. Проверка с настоящими API (только чтение): `pytest --live`.
