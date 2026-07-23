"""
revenue_sync.py

Скрипт логинится в личный кабинет papasushi.goulash.tech (Yii2-приложение),
забирает JSON с внутреннего эндпоинта /dashboad/api/header?departmentId=...
(тот же запрос, который дёргает сама страница дашборда каждые ~10 сек / раз в минуту),
достаёт из него выручку (поле total_summ_clear) и отправляет её на crmpapa.ru.

Заодно (если настроено) достаёт из того же ответа время ожидания — блок
department_info.messages с записью {"name": "Обещаем гостю", "values": [...]} —
и отправляет его отдельным запросом на webhook времени ожидания того же филиала
(см. GOULASH_WAITTIME_SYNC_MAP). Один HTTP-запрос к goulash обслуживает оба вебхука.

Поддерживает несколько филиалов за один запуск: один логин на аккаунт goulash,
затем по кругу — каждый department_id со своим webhook-URL на crmpapa.ru
(см. GOULASH_SYNC_MAP).

Дополнительно (если настроено — см. GOULASH_ORDERS_QUERY_TEMPLATE) тем же логином
забирает CSV-выгрузку «Отчёт по заказам» (страница receipts/receipts/index — та же,
что раньше скачивали вручную и загружали в CRM) и пересылает её как есть на отдельный
вебхук заказов; разбор файла целиком на стороне crmpapa.ru (см. sync_orders()).

ВАЖНО:
- Логин/пароль и URL вебхуков не хранятся в коде — задаются через переменные окружения.
  Формат тела запроса на crmpapa.ru уже согласован (см. send_to_crmpapa()
  и send_waittime_to_crmpapa()).
- Если сайт обновит вёрстку логина или поменяет механизм CSRF, функции get_csrf_token()
  и login() может понадобиться поправить — см. комментарии внутри.
- Точный формат messages[].values при непустом значении не был подтверждён на реальных
  данных на момент написания — _extract_message_value() разбирает несколько вероятных
  вариантов и пишет предупреждение в лог, если формат окажется другим, вместо того чтобы
  падать. Если в логах видно предупреждение "не удалось разобрать" — пришлите пример JSON,
  поправим разбор под реальный формат.
- Заказы: страница receipts/receipts/index при обычном GET отдаёт HTML саму страницу
  отчёта, а не файл — реальный файл отдаётся только с доп. параметром `is_excel=true`
  (найдено разбором JS кнопки «Выгрузить в Excel» на самой странице; проверено на
  реальном запросе — content-disposition и содержимое совпадают с тем, что выгружает
  кнопка вручную). fetch_orders_csv() всё равно проверяет ответ (_looks_like_orders_csv)
  и пишет понятную ошибку в лог вместо падения, если сайт когда-нибудь поменяет этот
  механизм — на случай, если вёрстка/JS страницы изменятся.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Конфигурация (через переменные окружения, см. .env.example)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("GOULASH_BASE_URL", "https://papasushi.goulash.tech")
USERNAME = os.environ.get("GOULASH_USERNAME")
PASSWORD = os.environ.get("GOULASH_PASSWORD")

# Несколько филиалов за один запуск: одна пара "department_id=webhook_url" на филиал,
# разделённые ";". Пример для 8 филиалов:
#   GOULASH_SYNC_MAP=162=https://crmpapa.ru/api/revenue-webhook/tok1;165=https://crmpapa.ru/api/revenue-webhook/tok2
SYNC_MAP_RAW = os.environ.get("GOULASH_SYNC_MAP", "")

# Время ожидания («Обещаем гостю») — тот же формат "department_id=webhook_url;...",
# но со своими токенами (Настройки → API → «Синхронизация времени ожидания» на crmpapa.ru).
# Необязательно: если не задано, время ожидания просто не отправляется, выручка продолжает работать как раньше.
WAITTIME_SYNC_MAP_RAW = os.environ.get("GOULASH_WAITTIME_SYNC_MAP", "")

# Обратная совместимость: одиночный режим (один филиал), если GOULASH_SYNC_MAP не задан
DEPARTMENT_ID = os.environ.get("GOULASH_DEPARTMENT_ID", "")
CRMPAPA_WEBHOOK_URL = os.environ.get("CRMPAPA_WEBHOOK_URL", "")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))  # по умолчанию 10 минут

# Таймаут на все запросы к goulash (в секундах). Без него зависший/недоступный сайт мог
# заблокировать процесс навсегда без единой строки в логе — ни ошибки, ни следующей попытки.
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30"))

LOGIN_PATH = "/site/login"
HEADER_API_PATH = "/dashboad/api/header"

# Названия сообщений в department_info["messages"], откуда берём время ожидания
PROMISE_MESSAGE_NAME = "Обещаем гостю"
ESTIMATED_MESSAGE_NAME = "Расчетное время"

# ---------------------------------------------------------------------------
# Заказы («Отчёт по заказам» — та же CSV-выгрузка, что раньше скачивали и
# загружали в CRM вручную, см. README, раздел «Заказы»)
# ---------------------------------------------------------------------------

RECEIPTS_PATH = "/receipts/receipts/index"

# Полная строка запроса страницы отчёта — всё, что после "?" в её адресе в браузере,
# скопированное как есть (агенты/филиалы, статус заказа, набор колонок и т.д.),
# но с {date_from}/{date_to} вместо конкретных чисел в датах — их подставляет сам
# скрипт перед каждым запросом. См. .env.example и README.
ORDERS_QUERY_TEMPLATE = os.environ.get("GOULASH_ORDERS_QUERY_TEMPLATE", "")

# Один вебхук сразу на все филиалы — Настройки → API → «Синхронизация заказов»
# на crmpapa.ru. В отличие от выручки/времени ожидания разбор по филиалам не нужен
# на этом уровне: каждый заказ сам несёт своё подразделение (колонка в выгрузке).
ORDERS_WEBHOOK_URL = os.environ.get("CRMPAPA_ORDERS_WEBHOOK_URL", "")

# За сколько последних дней запрашивать заказы при каждом запуске. Специально шире,
# чем интервал опроса, — чтобы подхватывать заказы, которые "дозрели" (сменили сумму
# или статус) уже после первой загрузки. Это безопасно: повторная загрузка уже
# известного заказа не создаёт дубль на crmpapa.ru, а обновляет его строку.
ORDERS_LOOKBACK_DAYS = int(os.environ.get("ORDERS_LOOKBACK_DAYS", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("revenue_sync")


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

def get_csrf_token(session: requests.Session) -> Optional[str]:
    """Забирает страницу логина и пытается найти CSRF-токен (Yii2)."""
    resp = session.get(f"{BASE_URL}{LOGIN_PATH}", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]

    # Yii2 advanced-шаблон обычно называет поле "_csrf-frontend",
    # базовый шаблон — просто "_csrf". Проверяем оба варианта.
    for field_name in ("_csrf-frontend", "_csrf"):
        hidden = soup.find("input", attrs={"name": field_name})
        if hidden and hidden.get("value"):
            return hidden["value"]

    log.warning("CSRF-токен не найден на странице логина — возможно, защита отключена")
    return None


def login(session: requests.Session) -> None:
    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "Не заданы GOULASH_USERNAME / GOULASH_PASSWORD в переменных окружения"
        )

    csrf_token = get_csrf_token(session)

    payload = {
        "LoginForm[username]": USERNAME,
        "LoginForm[password]": PASSWORD,
        "yt0": "Войти",
    }
    if csrf_token:
        # Отправляем под обоими возможными именами поля — лишнее поле сервер проигнорирует
        payload["_csrf-frontend"] = csrf_token
        payload["_csrf"] = csrf_token

    resp = session.post(f"{BASE_URL}{LOGIN_PATH}", data=payload, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    if "LoginForm[username]" in resp.text and "Авторизация" in resp.text:
        raise RuntimeError(
            "Логин не удался: сервер снова вернул форму входа. "
            "Проверьте логин/пароль или структуру формы (могла поменяться разметка/CSRF)."
        )

    log.info("Успешно авторизовались как %s", USERNAME)


# ---------------------------------------------------------------------------
# Получение выручки и времени ожидания
# ---------------------------------------------------------------------------

def _extract_message_value(messages: list, name: str) -> Optional[float]:
    """Достаёт числовое значение из department_info["messages"] по имени записи
    (например, "Обещаем гостю"). Точный формат values[] на реальных данных с
    непустым значением не был подтверждён заранее, поэтому разбираем несколько
    вероятных вариантов и не падаем, если формат окажется другим — просто
    возвращаем None и пишем предупреждение в лог."""
    for msg in messages or []:
        if msg.get("name") != name:
            continue
        values = msg.get("values") or []
        if not values:
            return None
        first = values[0]
        if isinstance(first, (int, float)):
            return float(first)
        if isinstance(first, dict):
            for key in ("value", "minutes", "min", "time"):
                if key in first:
                    try:
                        return float(first[key])
                    except (TypeError, ValueError):
                        continue
            log.warning("Не удалось разобрать values для %r: %r", name, first)
            return None
        try:
            return float(first)
        except (TypeError, ValueError):
            log.warning("Не удалось разобрать values для %r: %r", name, first)
            return None
    return None


def fetch_department_data(session: requests.Session, department_id: str) -> dict:
    resp = session.get(
        f"{BASE_URL}{HEADER_API_PATH}",
        params={"departmentId": department_id},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            "Ответ не в формате JSON — скорее всего сессия истекла "
            "и сервер вернул HTML-страницу логина вместо данных."
        ) from exc

    if not data.get("success"):
        raise RuntimeError(f"API вернул success=false: {data}")

    dept_info = data["department_info"]
    messages = dept_info.get("messages") or []

    return {
        "revenue": dept_info.get("total_summ_clear"),
        "orders_count": dept_info.get("count_orders"),
        "department_id": dept_info.get("id"),
        "department_title": dept_info.get("title"),
        "promised_minutes": _extract_message_value(messages, PROMISE_MESSAGE_NAME),
        "estimated_minutes": _extract_message_value(messages, ESTIMATED_MESSAGE_NAME),
    }


# ---------------------------------------------------------------------------
# Заказы («Отчёт по заказам»)
# ---------------------------------------------------------------------------

def _looks_like_orders_csv(content: bytes) -> bool:
    """Грубая проверка, что goulash вернул именно CSV-выгрузку «Отчёт по заказам»,
    а не HTML (например форму логина, если сессия истекла, или саму страницу отчёта
    как есть — вдруг для CSV нужен отдельный запрос/ссылка «Экспорт», а не этот же
    URL). Ищем характерный заголовок колонки — он есть в выгрузке в любой кодировке
    из тех, что понимает разбор на стороне crmpapa.ru (см. _parse_orders_csv)."""
    text = None
    for enc in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            text = content[:4000].decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return False
    return "Номер заказа" in text


def fetch_orders_csv(session: requests.Session, date_from: str, date_to: str, current_date: bool = False) -> bytes:
    """date_from/date_to — строки в формате ДД.ММ.ГГГГ (как в фильтре страницы).

    Обычный GET на receipts/receipts/index с этими же параметрами отдаёт HTML-страницу
    отчёта (саму таблицу), а не файл — нужный CSV отдаётся только с дополнительным
    параметром is_excel=true. Найдено разбором JS на самой странице: кнопка «Выгрузить
    в Excel» (id="excel") берёт текущий фильтр, добавляет в него is_excel=true и делает
    обычный переход по тому же URL с этим параметром — так же делаем и мы.

    current_date=True — переключает ReceiptsAll[isCurrentDate] с 0 на 1. Обнаружено
    эмпирически: обычный запрос (isCurrentDate=0) для сегодняшнего, ещё не закрытого
    дня возвращает 0 строк вообще, независимо от date_s/date_do — гуляш, похоже,
    считает isCurrentDate=0 запросом только по «закрытым» дням. isCurrentDate=1 —
    единственный способ увидеть сегодняшние заказы, включая ещё не завершённые
    (см. sync_orders — вызывается отдельным запросом только за сегодня)."""
    if not ORDERS_QUERY_TEMPLATE:
        raise RuntimeError("Не задан GOULASH_ORDERS_QUERY_TEMPLATE")
    query = ORDERS_QUERY_TEMPLATE.format(date_from=date_from, date_to=date_to)
    if current_date:
        query = query.replace("ReceiptsAll%5BisCurrentDate%5D=0", "ReceiptsAll%5BisCurrentDate%5D=1")
    url = f"{BASE_URL}{RECEIPTS_PATH}?{query}&is_excel=true"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    if not _looks_like_orders_csv(resp.content):
        raise RuntimeError(
            "Ответ не похож на CSV-выгрузку «Отчёт по заказам» (нет колонки "
            "«Номер заказа» в начале ответа) — либо истекла сессия и сайт вернул "
            "страницу логина, либо этот URL при таких параметрах отдаёт HTML-страницу "
            "отчёта, а не файл, и нужна отдельная ссылка на экспорт/скачивание. "
            f"Начало ответа: {resp.content[:300]!r}"
        )
    return resp.content


# ---------------------------------------------------------------------------
# Отправка на crmpapa.ru
# ---------------------------------------------------------------------------

def send_to_crmpapa(payload: dict, webhook_url: str) -> None:
    body = {
        "revenue": payload["revenue"],
        "orders_count": payload["orders_count"],
        "department_id": payload["department_id"],
        "department_title": payload["department_title"],
        "source": "papasushi.goulash.tech",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not webhook_url:
        log.warning(
            "Webhook URL не задан для филиала %s — данные не отправлены, просто печатаю:\n%s",
            payload.get("department_id"),
            json.dumps(body, ensure_ascii=False, indent=2),
        )
        return

    resp = requests.post(webhook_url, json=body, timeout=15)
    resp.raise_for_status()

    log.info("Отправлено на crmpapa.ru (филиал %s): %s", payload.get("department_id"), body)


def send_waittime_to_crmpapa(payload: dict, webhook_url: str) -> None:
    if payload.get("promised_minutes") is None and payload.get("estimated_minutes") is None:
        log.info(
            "Нет данных о времени ожидания для филиала %s — пропускаю отправку",
            payload.get("department_id"),
        )
        return

    body = {
        "promised_minutes": payload.get("promised_minutes"),
        "estimated_minutes": payload.get("estimated_minutes"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not webhook_url:
        log.warning(
            "Waittime webhook URL не задан для филиала %s — данные не отправлены, просто печатаю:\n%s",
            payload.get("department_id"),
            json.dumps(body, ensure_ascii=False, indent=2),
        )
        return

    resp = requests.post(webhook_url, json=body, timeout=15)
    resp.raise_for_status()

    log.info("Время ожидания отправлено на crmpapa.ru (филиал %s): %s", payload.get("department_id"), body)


def send_orders_to_crmpapa(csv_bytes: bytes, webhook_url: str) -> dict:
    """Пересылает CSV как есть (тем же байтами, что получены от goulash) — разбор
    формата целиком на стороне crmpapa.ru, тот же самый, что и при ручной загрузке
    файла в «Отчёт по заказам» (см. _parse_orders_csv в app.py)."""
    resp = requests.post(
        webhook_url, data=csv_bytes,
        headers={"Content-Type": "text/csv; charset=utf-8"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def sync_orders(session: requests.Session) -> None:
    if not ORDERS_QUERY_TEMPLATE or not ORDERS_WEBHOOK_URL:
        return  # заказы не настроены — молча пропускаем, остальная синхронизация не страдает

    # Локальная дата (TZ=Asia/Novosibirsk задан в контейнере), а не UTC: date_s/date_do
    # на гуляше — это календарный день ресторана. С UTC начало суток в Новосибирске
    # (UTC+7) на несколько часов "отставало" бы от локальной даты и не захватывало
    # свежий день примерно 7 часов в сутки (с полуночи по 7 утра по Новосибирску).
    today = datetime.now().date()
    date_from = (today - timedelta(days=ORDERS_LOOKBACK_DAYS)).strftime("%d.%m.%Y")
    date_to = today.strftime("%d.%m.%Y")

    csv_bytes = fetch_orders_csv(session, date_from, date_to)
    result = send_orders_to_crmpapa(csv_bytes, ORDERS_WEBHOOK_URL)

    if result.get("ok"):
        log.info(
            "Заказы %s–%s: импортировано %s, обновлено %s (всего строк %s)",
            date_from, date_to, result.get("imported"), result.get("updated"), result.get("rows"),
        )
    else:
        log.error("Заказы %s–%s: вебхук вернул ошибку: %s", date_from, date_to, result)

    # Сегодняшний (ещё не закрытый) день обычный запрос выше не захватывает вообще
    # (см. комментарий в fetch_orders_csv) — забираем его отдельно с isCurrentDate=1,
    # это единственный способ увидеть заказы, которые прямо сейчас в работе.
    today_str = today.strftime("%d.%m.%Y")
    csv_bytes_today = fetch_orders_csv(session, today_str, today_str, current_date=True)
    result_today = send_orders_to_crmpapa(csv_bytes_today, ORDERS_WEBHOOK_URL)

    if result_today.get("ok"):
        log.info(
            "Заказы (сегодня) %s: импортировано %s, обновлено %s (всего строк %s)",
            today_str, result_today.get("imported"), result_today.get("updated"), result_today.get("rows"),
        )
    else:
        log.error("Заказы (сегодня) %s: вебхук вернул ошибку: %s", today_str, result_today)


# ---------------------------------------------------------------------------
# Карта "department_id -> webhook_url" для нескольких филиалов
# ---------------------------------------------------------------------------

def parse_sync_map(raw: str) -> dict:
    """Разбирает GOULASH_SYNC_MAP вида 'dep1=url1;dep2=url2' в {dep_id: url}."""
    mapping: dict = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            log.warning("Пропускаю некорректную пару в GOULASH_SYNC_MAP: %r", pair)
            continue
        dept_id, url = pair.split("=", 1)
        dept_id = dept_id.strip()
        url = url.strip()
        if dept_id and url:
            mapping[dept_id] = url
    return mapping


def get_sync_targets() -> dict:
    """Возвращает {department_id: webhook_url} для всех филиалов, которые нужно синхронизировать."""
    mapping = parse_sync_map(SYNC_MAP_RAW)
    if mapping:
        return mapping
    # Обратная совместимость: старый режим с одним филиалом
    if DEPARTMENT_ID and CRMPAPA_WEBHOOK_URL:
        return {DEPARTMENT_ID: CRMPAPA_WEBHOOK_URL}
    return {}


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def run_once() -> None:
    targets = get_sync_targets()
    orders_enabled = bool(ORDERS_QUERY_TEMPLATE and ORDERS_WEBHOOK_URL)

    if not targets and not orders_enabled:
        log.error(
            "Не задано ни одной синхронизации. Укажите GOULASH_SYNC_MAP "
            "(department_id=webhook_url;...) для выручки, либо GOULASH_ORDERS_QUERY_TEMPLATE "
            "+ CRMPAPA_ORDERS_WEBHOOK_URL для заказов (см. README, раздел «Заказы»)."
        )
        return

    waittime_targets = parse_sync_map(WAITTIME_SYNC_MAP_RAW)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (revenue-sync-script)"})
    login(session)

    for department_id, webhook_url in targets.items():
        try:
            payload = fetch_department_data(session, department_id)
            send_to_crmpapa(payload, webhook_url)
            waittime_webhook_url = waittime_targets.get(department_id)
            if waittime_webhook_url:
                send_waittime_to_crmpapa(payload, waittime_webhook_url)
        except Exception as exc:
            log.error("Ошибка синхронизации филиала %s: %s", department_id, exc)

    if orders_enabled:
        try:
            sync_orders(session)
        except Exception as exc:
            log.error("Ошибка синхронизации заказов: %s", exc)


def main() -> None:
    run_forever = "--once" not in sys.argv

    if not run_forever:
        run_once()
        return

    log.info("Запуск в режиме периодического опроса каждые %s сек.", POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001 - логируем и продолжаем цикл
            log.error("Ошибка на итерации: %s", exc)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
