"""
revenue_sync.py

Скрипт логинится в личный кабинет papasushi.goulash.tech (Yii2-приложение),
забирает JSON с внутреннего эндпоинта /dashboad/api/header?departmentId=...
(тот же запрос, который дёргает сама страница дашборда каждые ~10 сек / раз в минуту),
достаёт из него выручку (поле total_summ_clear) и отправляет её на crmpapa.ru.

ВАЖНО:
- Логин/пароль и URL вебхука не хранятся в коде — задаются через переменные окружения.
- URL вебхука на crmpapa.ru — ЗАГЛУШКА (см. CRMPAPA_WEBHOOK_URL). Формат тела запроса
  в функции send_to_crmpapa() тоже нужно будет подогнать под реальный API crmpapa.ru,
  когда он будет известен.
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
DEPARTMENT_ID = os.environ.get("GOULASH_DEPARTMENT_ID", "162")

# TODO: подставить реальный URL эндпоинта на crmpapa.ru, когда он будет готов
CRMPAPA_WEBHOOK_URL = os.environ.get("CRMPAPA_WEBHOOK_URL")

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

def send_to_crmpapa(payload: dict) -> None:
    body = {
        "revenue": payload["revenue"],
        "orders_count": payload["orders_count"],
        "department_id": payload["department_id"],
        "department_title": payload["department_title"],
        "source": "papasushi.goulash.tech",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not CRMPAPA_WEBHOOK_URL:
        log.warning(
            "CRMPAPA_WEBHOOK_URL не задан — данные не отправлены, просто печатаю:\n%s",
            json.dumps(body, ensure_ascii=False, indent=2),
        )
        return

    # TODO: если crmpapa.ru ожидает другой формат (не JSON, другие имена полей,
    # заголовок с API-ключом и т.п.) — поправить этот запрос под реальный контракт.
    resp = requests.post(CRMPAPA_WEBHOOK_URL, json=body, timeout=15)
    resp.raise_for_status()

    log.info("Отправлено на crmpapa.ru: %s", body)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def run_once() -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (revenue-sync-script)"})

    login(session)
    payload = fetch_revenue(session, DEPARTMENT_ID)
    send_to_crmpapa(payload)


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
