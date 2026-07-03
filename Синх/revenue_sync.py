"""
revenue_sync.py

Скрипт логинится в личный кабинет papasushi.goulash.tech (Yii2-приложение),
забирает JSON с внутреннего эндпоинта /dashboad/api/header?departmentId=...
(тот же запрос, который дёргает сама страница дашборда каждые ~10 сек / раз в минуту),
достаёт из него выручку (поле total_summ_clear) и отправляет её на crmpapa.ru.

Поддерживает несколько филиалов за один запуск: один логин на аккаунт goulash,
затем по кругу — каждый department_id со своим webhook-URL на crmpapa.ru
(см. GOULASH_SYNC_MAP).

ВАЖНО:
- Логин/пароль и URL вебхуков не хранятся в коде — задаются через переменные окружения.
  Формат тела запроса на crmpapa.ru уже согласован (см. send_to_crmpapa()).
- Если сайт обновит вёрстку логина или поменяет механизм CSRF, функции get_csrf_token()
  и login() может понадобиться поправить — см. комментарии внутри.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
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

# Обратная совместимость: одиночный режим (один филиал), если GOULASH_SYNC_MAP не задан
DEPARTMENT_ID = os.environ.get("GOULASH_DEPARTMENT_ID", "")
CRMPAPA_WEBHOOK_URL = os.environ.get("CRMPAPA_WEBHOOK_URL", "")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))  # по умолчанию 10 минут

LOGIN_PATH = "/site/login"
HEADER_API_PATH = "/dashboad/api/header"

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
    resp = session.get(f"{BASE_URL}{LOGIN_PATH}")
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

    resp = session.post(f"{BASE_URL}{LOGIN_PATH}", data=payload, allow_redirects=True)
    resp.raise_for_status()

    if "LoginForm[username]" in resp.text and "Авторизация" in resp.text:
        raise RuntimeError(
            "Логин не удался: сервер снова вернул форму входа. "
            "Проверьте логин/пароль или структуру формы (могла поменяться разметка/CSRF)."
        )

    log.info("Успешно авторизовались как %s", USERNAME)


# ---------------------------------------------------------------------------
# Получение выручки
# ---------------------------------------------------------------------------

def fetch_revenue(session: requests.Session, department_id: str) -> dict:
    resp = session.get(
        f"{BASE_URL}{HEADER_API_PATH}",
        params={"departmentId": department_id},
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

    return {
        "revenue": dept_info.get("total_summ_clear"),
        "orders_count": dept_info.get("count_orders"),
        "department_id": dept_info.get("id"),
        "department_title": dept_info.get("title"),
    }


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
    if not targets:
        log.error(
            "Не заданы филиалы для синхронизации. Укажите GOULASH_SYNC_MAP "
            "(department_id=webhook_url;...) либо GOULASH_DEPARTMENT_ID + CRMPAPA_WEBHOOK_URL "
            "для одного филиала."
        )
        return

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (revenue-sync-script)"})
    login(session)

    for department_id, webhook_url in targets.items():
        try:
            payload = fetch_revenue(session, department_id)
            send_to_crmpapa(payload, webhook_url)
        except Exception as exc:
            log.error("Ошибка синхронизации филиала %s: %s", department_id, exc)


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
