# Синхронизация выручки: papasushi.goulash.tech → crmpapa.ru

Что делает скрипт `revenue_sync.py`:

1. Логинится в личный кабинет `papasushi.goulash.tech` (та же форма, что и обычный вход в браузере).
2. Дёргает внутренний JSON-эндпоинт `GET /dashboad/api/header?departmentId=...` — это тот же запрос,
   который сама страница дашборда выполняет каждую минуту для обновления шапки (найден через вкладку
   Network в DevTools).
3. Берёт из ответа поле `total_summ_clear` — это и есть выручка — плюс `count_orders` (число заказов).
4. Отправляет их JSON-POST-запросом на `CRMPAPA_WEBHOOK_URL`.

## 1. Открыть в VS Code

Откройте папку с этими файлами в VS Code (File → Open Folder).
Файлы:

- `revenue_sync.py` — сам скрипт
- `requirements.txt` — зависимости
- `.env.example` — шаблон переменных окружения

## 2. Установка

В терминале VS Code:

```bash
python3 -m venv venv
source venv/bin/activate        # на Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Настройка

```bash
cp .env.example .env
```

Откройте `.env` и заполните:

- `GOULASH_USERNAME` / `GOULASH_PASSWORD` — ваш логин и пароль от papasushi.goulash.tech
- `GOULASH_DEPARTMENT_ID` — id подразделения (162 — уже подставлен, видно в скриншотах Network)
- `CRMPAPA_WEBHOOK_URL` — **пока пусто**. Когда на crmpapa.ru появится эндпоинт, принимающий выручку,
  впишите сюда его URL. Пока URL не задан, скрипт просто печатает данные в консоль вместо отправки —
  можно проверить, что всё работает, ещё до готовности приёмной стороны.

Скрипт сейчас читает `.env` не автоматически — либо экспортируйте переменные в шелл, либо добавьте
в начало `revenue_sync.py` пакет `python-dotenv` (`pip install python-dotenv` и
`from dotenv import load_dotenv; load_dotenv()`), если хотите подхватывать `.env` из файла.

Быстрый вариант без dotenv — экспортировать переменные прямо в терминале:

```bash
export GOULASH_USERNAME=ваш_логин
export GOULASH_PASSWORD=ваш_пароль
export GOULASH_DEPARTMENT_ID=162
export CRMPAPA_WEBHOOK_URL=https://crmpapa.ru/ваш-эндпоинт
```

## 4. Запуск

Один раз (проверить, что всё работает):

```bash
python revenue_sync.py --once
```

Постоянно, с опросом каждые 10 минут (значение берётся из `POLL_INTERVAL_SECONDS`):

```bash
python revenue_sync.py
```

Либо, если хотите не держать процесс запущенным вечно, а гонять по cron — используйте
`--once` в cron-задаче:

```
*/10 * * * * cd /path/to/project && venv/bin/python revenue_sync.py --once >> sync.log 2>&1
```

## 6. Постоянная работа 24/7 на Railway (рекомендуется)

Чтобы синхронизация не зависела от того, включён ли ваш компьютер, скрипт можно запустить
как отдельный сервис на Railway — рядом с самой CRM, в том же проекте. Файл `Dockerfile`
в этой папке уже готов для этого (ставит нужный часовой пояс и запускает
`python3 revenue_sync.py` в режиме постоянного опроса).

Шаги в панели Railway (railway.app):

1. Откройте ваш проект на Railway (тот же, где крутится сама CRM).
2. **+ New** → **GitHub Repo** → выберите тот же репозиторий ещё раз (создастся второй,
   независимый сервис в этом же проекте).
3. У нового сервиса откройте **Settings**:
   - **Root Directory** — укажите `Синх` (Railway будет собирать и запускать только эту папку).
   - Уберите/не трогайте настройки домена — этому сервису публичный URL не нужен, он
     ничего не принимает, только сам ходит на goulash и на crmpapa.ru.
4. Там же, в **Variables**, добавьте переменные окружения (те же, что в `.env`):
   - `GOULASH_USERNAME`
   - `GOULASH_PASSWORD`
   - `GOULASH_DEPARTMENT_ID` = `162`
   - `CRMPAPA_WEBHOOK_URL` — URL с токеном из Настройки → Интеграции на crmpapa.ru
   - `POLL_INTERVAL_SECONDS` = `600` (или чаще, например `120` — раз в 2 минуты)
5. Нажмите **Deploy**. Railway соберёт образ по `Dockerfile` и запустит скрипт — он будет
   работать постоянно и сам перезапускаться, если упадёт (по умолчанию у Railway включён
   restart-on-failure).
6. Проверить, что данные доходят — смотрите логи этого сервиса в Railway (там будут строки
   "Успешно авторизовались" и "Отправлено на crmpapa.ru"), либо раздел «Лог входящих
   запросов» в Настройки → Интеграции на самом crmpapa.ru.

Секреты (логин/пароль от goulash, URL вебхука) хранятся только в переменных окружения
Railway — они не попадают в git и не видны в коде.

## 5. Когда появится реальный формат для crmpapa.ru

В `revenue_sync.py`, функция `send_to_crmpapa()`, тело запроса сейчас выглядит так:

```python
body = {
    "revenue": payload["revenue"],
    "orders_count": payload["orders_count"],
    "department_id": payload["department_id"],
    "department_title": payload["department_title"],
    "source": "papasushi.goulash.tech",
    "timestamp": datetime.now(timezone.utc).isoformat(),
}
resp = requests.post(CRMPAPA_WEBHOOK_URL, json=body, timeout=15)
```

Поправьте имена полей / способ авторизации (например добавить заголовок с API-ключом) под то,
что реально ожидает эндпоинт на crmpapa.ru.

## Возможные проблемы

- **Логин не проходит** — сайт на Yii2, скрипт пытается найти CSRF-токен автоматически
  (`_csrf-frontend` / `_csrf` / мета-тег). Если разметка формы изменится, поправьте
  `get_csrf_token()`.
- **Ответ не JSON / сессия истекла** — `fetch_revenue()` кидает понятную ошибку в этом случае;
  скрипт в режиме постоянного опроса просто залогинится заново на следующей итерации.
- **Двухфакторная авторизация или вход по отпечатку/штрихкоду** — на странице логина видна
  фраза "Можно авторизоваться по отпечатку пальца или по штрих коду". Если на вашем аккаунте
  включена такая защита, обычный логин/пароль через форму может не сработать — тогда нужен
  отдельный технический пользователь с простой авторизацией, если система это поддерживает.
