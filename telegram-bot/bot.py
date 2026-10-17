"""Telegram notifications from fresh Beszel samples; Python standard library only."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time
from urllib import error, parse, request

LOG = logging.getLogger("telegram-bot")


class APIError(Exception):
    """Deliberately excludes URLs, response bodies, passwords and bot tokens."""

    def __init__(self, service, status=0, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"{service}: HTTP {status}" if status else f"{service}: request failed")


def http_json(url, body=None, token=None, service="Beszel"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=10) as response:
            return json.load(response)
    except error.HTTPError as exc:
        retry_after = 0
        if exc.code == 429:
            try:
                retry_after = int(json.load(exc).get("parameters", {}).get("retry_after", 0))
            except (ValueError, TypeError):
                pass
        raise APIError(service, exc.code, retry_after) from None
    except (OSError, ValueError):
        raise APIError(service) from None


class Beszel:
    def __init__(self, url, email, password):
        self.url = url.rstrip("/")
        self.email, self.password, self.token = email, password, None

    def get(self, path, **params):
        url = self.url + path + ("?" + parse.urlencode(params) if params else "")
        for attempt in range(2):
            if not self.token:
                auth = http_json(self.url + "/api/collections/users/auth-with-password",
                                 {"identity": self.email, "password": self.password})
                self.token = auth["token"]
            try:
                return http_json(url, token=self.token)
            except APIError as exc:
                if exc.status not in (401, 403) or attempt:
                    raise
                self.token = None

    def systems(self, system_id=""):
        page = 1
        while True:
            response = self.get("/api/collections/systems/records", page=page, perPage=100)
            for system in response["items"]:
                if not system_id or system["id"] == system_id:
                    yield system
            if page >= response["totalPages"]:
                return
            page += 1

    def latest(self, system_id):
        # The id originates from the authenticated API, but still validate filter input.
        if not system_id.isalnum():
            raise ValueError("invalid system id")
        result = self.get("/api/collections/system_stats/records", perPage=1,
                          sort="-created", filter=f'system="{system_id}" && type="1m"')
        return result["items"][0] if result["items"] else None


class Telegram:
    def __init__(self, token, chat_id, call=http_json):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id, self.call = chat_id, call
        self.retry_at = 0

    def send(self, message):
        if time.time() < self.retry_at:
            raise APIError("Telegram rate limit", 429)
        try:
            result = self.call(self.url, {"chat_id": self.chat_id, "text": message[:4000],
                                        "disable_notification": False}, service="Telegram")
        except APIError as exc:
            self.retry_at = time.time() + max(exc.retry_after, 30)
            raise
        if result.get("ok") is not True:
            self.retry_at = time.time() + 30
            raise APIError("Telegram", result.get("error_code", 0))


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    value: float
    limit: float
    low: bool = False


def percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 100 else None


def metrics_from(stats):
    if not isinstance(stats, dict):
        raise ValueError("invalid system statistics")
    metrics, missing = [], []
    for key, field, limit in [("CPU", "cpu", 95), ("RAM", "mp", 70)]:
        value = percent(stats.get(field))
        if value is None:
            missing.append(key)
        else:
            metrics.append(Metric(key.lower(), key, value, limit))
    gpus = stats.get("g") or {}
    if not isinstance(gpus, dict):
        raise ValueError("invalid GPU data")
    if not gpus:
        missing.extend(["GPU", "VRAM"])
    for device, gpu in gpus.items():
        if not isinstance(gpu, dict):
            raise ValueError("invalid GPU data")
        name = str(gpu.get("n") or device)[:80]
        usage = percent(gpu.get("u"))
        if usage is None:
            missing.append(f"GPU {name}")
        else:
            metrics.append(Metric(f"gpu:{device}", f"GPU {name}", usage, 95))
        total, used = gpu.get("mt"), gpu.get("mu", 0)
        if (isinstance(total, (int, float)) and not isinstance(total, bool)
                and isinstance(used, (int, float)) and not isinstance(used, bool)
                and math.isfinite(total) and math.isfinite(used) and total > 0
                and 0 <= used <= total):
            metrics.append(Metric(f"vram:{device}", f"VRAM {name}", 100 * used / total, 70))
        else:
            missing.append(f"VRAM {name}")
    # bat is Beszel's primary/system battery. Do not use peripheral batteries in bats.
    battery = stats.get("bat")
    charge = percent(battery[0]) if isinstance(battery, list) and len(battery) == 2 else None
    if charge is None:
        missing.append("батарея ноутбука")
    else:
        metrics.append(Metric("battery", "Батарея ноутбука", charge, 35, low=True))
    return metrics, missing


def sample_time(record):
    value = record["created"]
    if isinstance(value, (int, float)):
        return value / 1000  # Beszel 0.21.0 stores system_stats.created in milliseconds.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


class Alerts:
    def __init__(self, path, send, repeat=1800):
        self.path, self.send, self.repeat = Path(path), send, repeat
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False))
        tmp.replace(self.path)

    def notify(self, key, active, message, now):
        # Update deduplication state only after Telegram confirms delivery.
        self.send(message)
        self.state[key] = {"active": active, "sent": now}
        self.save()

    def metric(self, scope, name, metric, now):
        key = f"{scope}:{metric.key}"
        old = self.state.get(key, {})
        breached = metric.value < metric.limit if metric.low else metric.value > metric.limit
        cleared = metric.value >= metric.limit + 3 if metric.low else metric.value <= metric.limit - 3
        if old.get("active") and cleared:
            self.notify(key, False, f"✅ {name}\n{metric.name}: {metric.value:.1f}% — відновлено", now)
        elif breached and (not old.get("active") or now - old["sent"] >= self.repeat):
            relation = "нижче" if metric.low else "понад"
            prefix = "🔁" if old.get("active") else "🚨"
            self.notify(key, True, f"{prefix} {name}\n{metric.name}: {metric.value:.1f}%\n"
                        f"Критичний поріг: {relation} {metric.limit}%", now)

    def issue(self, key, problem, message, now, recovery="Моніторинг відновлено"):
        key = f"issue:{key}"
        old = self.state.get(key, {})
        if not problem:
            if old.get("active"):
                self.notify(key, False, f"✅ {recovery}", now)
                del self.state[key]
            elif old:
                del self.state[key]
            return
        if not old or (not old.get("active") and "since" not in old):
            self.state[key] = {"since": now}
        elif not old.get("active") and now - old["since"] >= 120:
            self.notify(key, True, f"⚠️ {message}", now)
        elif old.get("active") and now - old["sent"] >= self.repeat:
            self.notify(key, True, f"⚠️ {message}", now)


def poll(beszel, alerts, now, system_id=""):
    systems = list(beszel.systems(system_id))
    alerts.issue("systems", not systems, "Beszel: немає доступних систем для моніторингу", now,
                 "Системи Beszel доступні для моніторингу")
    for system in systems:
        sid, name = system["id"], str(system["name"])[:120]
        record = beszel.latest(sid) if system.get("status") == "up" else None
        stale = record is None or not 0 <= now - sample_time(record) <= 180
        alerts.issue(sid + ":stale", stale, f"{name}: немає свіжих метрик (система Down або дані старші 3 хв)", now,
                     f"{name}: свіжі метрики знову доступні")
        if stale:
            continue  # Neither alert nor resolve from stale values.
        metrics, missing = metrics_from(record["stats"])
        for metric in metrics:
            alerts.metric(sid, name, metric, now)
        alerts.issue(sid + ":missing", bool(missing),
                     f"{name}: недоступні метрики: {', '.join(missing)}. Перевір доступ агента до GPU/батареї", now,
                     f"{name}: CPU/GPU/RAM/VRAM і батарея доступні")


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name}")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    state_dir = Path(os.environ.get("STATE_DIR", "/data"))
    heartbeat = state_dir / "heartbeat"
    if args.health:
        return 0 if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 120 else 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
    repeat = int(os.environ.get("ALERT_REPEAT_SECONDS", "1800"))
    if not 5 <= interval <= 60 or repeat < 60:
        raise ValueError("Poll interval must be 5..60 seconds; repeat must be >=60 seconds")
    beszel = Beszel(os.environ.get("BESZEL_URL", "http://beszel:8090"),
                    required("BESZEL_EMAIL"), required("BESZEL_PASSWORD"))
    telegram = Telegram(required("TELEGRAM_BOT_TOKEN"), required("TELEGRAM_CHAT_ID"))
    alerts = Alerts(state_dir / "alerts.json", telegram.send, repeat)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    LOG.info("Monitoring started: CPU/GPU >95%%, RAM/VRAM >70%%, battery <35%%")
    while not stop.is_set():
        now = time.time()
        try:
            poll(beszel, alerts, now, os.environ.get("BESZEL_SYSTEM_ID", ""))
            alerts.issue("api", False, "Зʼєднання бота з Beszel", now)
        except APIError as exc:
            LOG.warning("%s", exc)
            # Telegram delivery failures must not masquerade as a Beszel outage.
            if str(exc).startswith("Beszel"):
                try:
                    alerts.issue("api", True, "Бот не може отримати дані з Beszel", now)
                except APIError:
                    LOG.warning("Telegram notification failed; will retry")
        except (ValueError, KeyError, TypeError):
            LOG.error("Invalid Beszel response; no alerts will be resolved from this data")
            try:
                alerts.issue("api", True, "Бот отримує некоректні дані Beszel; перевір моніторинг", now)
            except APIError:
                LOG.warning("Telegram notification failed; will retry")
        alerts.save()
        heartbeat.touch()
        stop.wait(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
