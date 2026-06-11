"""
Standalone Telegram outbox worker.

Run this process on a server where Telegram Bot API is reachable. The LK backend
only stores notifications in PostgreSQL; this worker claims queued messages via
the LK HTTPS API, sends them to Telegram, and reports sent/failed status back.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as datetime_time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


def _get_env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _int_env(name: str, default: int, min_value: int = 1, max_value: Optional[int] = None) -> int:
    try:
        value = int(_get_env(name, str(default)))
    except ValueError:
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _time_env(name: str, default: str) -> datetime_time:
    value = _get_env(name, default)
    try:
        hour_text, minute_text = value.split(":", 1)
        return datetime_time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError):
        raise RuntimeError(f"Invalid {name}: expected HH:MM, got {value!r}")


def _api_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


def is_within_work_hours(
    *,
    timezone: ZoneInfo,
    start: datetime_time,
    end: datetime_time,
) -> bool:
    if start == end:
        return True

    now = datetime.now(timezone).time()
    if start < end:
        return start <= now < end
    return now >= start or now < end


def setup_logging() -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = TimedRotatingFileHandler(
        log_dir / "telegram_worker.log",
        when="midnight",
        interval=1,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[console_handler, file_handler],
        force=True,
    )


def claim_notifications(
    *,
    lk_api_base: str,
    token: str,
    worker_id: str,
    batch_size: int,
    timeout: int,
) -> list[Dict[str, Any]]:
    response = requests.post(
        _api_url(lk_api_base, "/internal/telegram-notifications/claim"),
        headers={"Authorization": f"Bearer {token}"},
        json={"workerId": worker_id, "limit": batch_size},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items") or []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def send_telegram_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: Optional[str],
    timeout: int,
    attempts: int,
    retry_delay: int,
) -> tuple[bool, Optional[str], Optional[str]]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    last_error: Optional[str] = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < attempts:
                logging.warning("Telegram send attempt failed: attempt=%s/%s error=%s", attempt, attempts, last_error)
                time.sleep(retry_delay)
                continue
            return False, None, last_error

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code == 200 and data.get("ok") is True:
            message_id = data.get("result", {}).get("message_id")
            return True, (str(message_id) if message_id is not None else None), None

        description = data.get("description") if isinstance(data, dict) else None
        error = description or f"Telegram HTTP {response.status_code}: {response.text[:500]}"
        last_error = error
        retry_after = None
        if isinstance(data, dict):
            retry_after = data.get("parameters", {}).get("retry_after")
        is_retryable = response.status_code == 429 or response.status_code >= 500
        if is_retryable and attempt < attempts:
            delay = retry_after if isinstance(retry_after, int) and retry_after > 0 else retry_delay
            logging.warning(
                "Telegram send attempt failed: attempt=%s/%s status=%s delay=%s error=%s",
                attempt,
                attempts,
                response.status_code,
                delay,
                error,
            )
            time.sleep(delay)
            continue

        return False, None, error

    return False, None, last_error or "send_failed"


def report_result(
    *,
    lk_api_base: str,
    token: str,
    notification_id: int,
    status: str,
    timeout: int,
    telegram_message_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    payload: Dict[str, Any] = {"status": status}
    if telegram_message_id:
        payload["telegramMessageId"] = telegram_message_id
    if error:
        payload["error"] = error[:2000]

    last_exc: Optional[Exception] = None
    for _attempt in range(3):
        try:
            response = requests.post(
                _api_url(lk_api_base, f"/internal/telegram-notifications/{notification_id}/result"),
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2)
    if last_exc:
        raise last_exc


def run() -> None:
    load_dotenv()
    setup_logging()

    lk_api_base = _get_env("LK_API_BASE")
    worker_token = _get_env("TELEGRAM_WORKER_API_TOKEN")
    bot_token = _get_env("TELEGRAM_BOT_TOKEN")
    worker_id = _get_env("WORKER_ID", "nl-worker-1")
    poll_seconds = _int_env("POLL_SECONDS", 10, min_value=1)
    batch_size = _int_env("BATCH_SIZE", 50, min_value=1, max_value=100)
    timeout = _int_env("REQUEST_TIMEOUT_SECONDS", 20, min_value=1)
    telegram_send_attempts = _int_env("TELEGRAM_SEND_ATTEMPTS", 3, min_value=1, max_value=10)
    telegram_retry_delay = _int_env("TELEGRAM_RETRY_DELAY_SECONDS", 2, min_value=1, max_value=300)
    work_hours_timezone_name = _get_env("WORK_HOURS_TIMEZONE", "Europe/Moscow")
    work_hours_start = _time_env("WORK_HOURS_START", "08:00")
    work_hours_end = _time_env("WORK_HOURS_END", "22:00")

    try:
        work_hours_timezone = ZoneInfo(work_hours_timezone_name)
    except Exception:
        raise RuntimeError(f"Invalid WORK_HOURS_TIMEZONE: {work_hours_timezone_name!r}")

    missing = [
        name
        for name, value in (
            ("LK_API_BASE", lk_api_base),
            ("TELEGRAM_WORKER_API_TOKEN", worker_token),
            ("TELEGRAM_BOT_TOKEN", bot_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required env values: {', '.join(missing)}")

    logging.info(
        "Telegram worker started: worker_id=%s batch_size=%s poll_seconds=%s telegram_send_attempts=%s work_hours=%s-%s timezone=%s",
        worker_id,
        batch_size,
        poll_seconds,
        telegram_send_attempts,
        work_hours_start.strftime("%H:%M"),
        work_hours_end.strftime("%H:%M"),
        work_hours_timezone_name,
    )

    outside_work_hours_logged = False
    while True:
        try:
            if not is_within_work_hours(
                timezone=work_hours_timezone,
                start=work_hours_start,
                end=work_hours_end,
            ):
                if not outside_work_hours_logged:
                    logging.info(
                        "Telegram worker paused outside work hours: work_hours=%s-%s timezone=%s",
                        work_hours_start.strftime("%H:%M"),
                        work_hours_end.strftime("%H:%M"),
                        work_hours_timezone_name,
                    )
                    outside_work_hours_logged = True
                time.sleep(poll_seconds)
                continue

            if outside_work_hours_logged:
                logging.info("Telegram worker resumed inside work hours")
                outside_work_hours_logged = False

            items = claim_notifications(
                lk_api_base=lk_api_base,
                token=worker_token,
                worker_id=worker_id,
                batch_size=batch_size,
                timeout=timeout,
            )
            if items:
                logging.info("Claimed Telegram notifications: count=%s", len(items))

            for item in items:
                notification_id = int(item["id"])
                ok, message_id, error = send_telegram_message(
                    bot_token=bot_token,
                    chat_id=str(item.get("chatId") or ""),
                    text=str(item.get("text") or ""),
                    parse_mode=item.get("parseMode") or "HTML",
                    timeout=timeout,
                    attempts=telegram_send_attempts,
                    retry_delay=telegram_retry_delay,
                )
                if ok:
                    report_result(
                        lk_api_base=lk_api_base,
                        token=worker_token,
                        notification_id=notification_id,
                        status="sent",
                        telegram_message_id=message_id,
                        timeout=timeout,
                    )
                    logging.info("Telegram notification sent: id=%s message_id=%s", notification_id, message_id)
                else:
                    report_result(
                        lk_api_base=lk_api_base,
                        token=worker_token,
                        notification_id=notification_id,
                        status="failed",
                        error=error or "send_failed",
                        timeout=timeout,
                    )
                    logging.warning("Telegram notification failed: id=%s error=%s", notification_id, error)
        except Exception:
            logging.warning("Telegram worker cycle failed", exc_info=True)

        time.sleep(poll_seconds)


if __name__ == "__main__":
    run()
