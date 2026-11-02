"""Beszel alerts and Telegram /status reports; Python standard library only."""

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time
import traceback
from urllib import error, parse, request

LOG = logging.getLogger("telegram-bot")


class ConfigError(ValueError):
    """Messages contain only known setting names, never their values."""


class APIError(Exception):
    """Deliberately excludes URLs, response bodies, passwords and bot tokens."""

    def __init__(self, service, status=0, retry_after=0, operation="", reason=""):
        self.service, self.operation, self.reason = service, operation, reason
        self.status = status
        self.retry_after = retry_after
        message = f"{service}: HTTP {status}" if status else f"{service}: request failed"
        super().__init__(message + (f" ({operation})" if operation else "")
                         + (f" [{reason}]" if reason else ""))


def error_detail(exc):
    # Exception messages/bodies/locals may contain credentials; never log them.
    if isinstance(exc, ConfigError):
        return str(exc)
    if isinstance(exc, APIError):
        detail = str(exc) + (f"; retry_after={exc.retry_after}s" if exc.retry_after else "")
        if exc.service.startswith("Telegram"):
            hint = {401: "перевір TELEGRAM_BOT_TOKEN", 403: "перевір права бота в чаті",
                    409: "конфлікт getUpdates: інший екземпляр бота або активний webhook",
                    429: "Telegram обмежив частоту запитів"}.get(exc.status)
        else:
            hint = "перевір облікові дані та права користувача" if exc.service == "Beszel" and exc.status in (400, 401, 403) else None
        return detail + (f"; {hint}" if hint else "")
    frames = traceback.extract_tb(exc.__traceback__)
    location = " > ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
    code = f" errno={exc.errno}" if isinstance(exc, OSError) and exc.errno is not None else ""
    return type(exc).__name__ + code + (f" at {location}" if location else "")


def load_state(path, validate):
    if not path.exists():
        return {}, None
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or not validate(state):
            raise ValueError("invalid state structure")
        return state, None
    except (ValueError, TypeError, KeyError) as exc:
        backup = path.with_name(path.name + f".corrupt-{time.time_ns()}")
        path.replace(backup)
        LOG.error("Invalid state file=%s; preserved as=%s; error=%s",
                  path.name, backup.name, error_detail(exc))
        return {}, exc


def valid_alert_state(state):
    for key, item in state.items():
        if not isinstance(item, dict):
            return False
        if key.startswith("diagnostic:"):
            if not {"since", "count", "message", "error", "active"} <= item.keys():
                return False
        if item.get("active") and not key.startswith("diagnostic:") and "sent" not in item:
            return False
        for field in ("since", "sent", "count", "ended", "last"):
            if field in item and (isinstance(item[field], bool) or
                                 not isinstance(item[field], (int, float)) or not math.isfinite(item[field])):
                return False
    return True


def http_json(url, body=None, token=None, service="Beszel"):
    path = parse.urlsplit(url).path
    operation = ("POST " if body is not None else "GET ") + (
        path.rsplit("/", 1)[-1] if service == "Telegram" else path)
    started = time.monotonic()
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
            except (ValueError, TypeError, AttributeError):
                pass
        LOG.warning("HTTP failed service=%s operation=%s status=%s retry_after=%ss elapsed=%.2fs",
                    service, operation, exc.code, retry_after, time.monotonic() - started)
        raise APIError(service, exc.code, retry_after, operation) from None
    except (OSError, ValueError) as exc:
        cause = exc.reason if isinstance(exc, error.URLError) else exc
        reason = "invalid JSON" if isinstance(exc, ValueError) else type(cause).__name__
        if isinstance(cause, OSError) and cause.errno is not None:
            reason += f" errno={cause.errno}"
        LOG.warning("HTTP failed service=%s operation=%s reason=%s elapsed=%.2fs",
                    service, operation, reason, time.monotonic() - started)
        raise APIError(service, operation=operation, reason=reason) from None


class Beszel:
    def __init__(self, url, email, password):
        self.url = url.rstrip("/")
        self.email, self.password, self.token = email, password, None
        self.token_expires_at = 0
        self.auth_retry_at = self.empty_retry_at = 0
        self.auth_error = None
        self.events = []

    def authenticate(self, reason="initial login"):
        if time.time() < self.auth_retry_at:
            raise self.auth_error
        LOG.info("Beszel authentication started reason=%s", reason)
        try:
            auth = http_json(self.url + "/api/collections/users/auth-with-password",
                             {"identity": self.email, "password": self.password})
            token = auth["token"]
            # Read exp only to schedule renewal. The server still verifies the JWT.
            payload = token.split(".")[1]
            expires = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
            if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                    or not math.isfinite(expires) or expires <= time.time()):
                raise ValueError("invalid token expiry")
        except (APIError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            self.auth_error = (exc if isinstance(exc, APIError) else
                               APIError("Beszel", operation="authentication", reason="invalid token response"))
            self.auth_retry_at = time.time() + max(30, self.auth_error.retry_after)
            LOG.error("Beszel authentication failed reason=%s error=%s retry_in=%ss",
                      reason, error_detail(self.auth_error), max(30, self.auth_error.retry_after))
            raise self.auth_error from None
        self.token, self.token_expires_at = token, expires
        self.auth_retry_at, self.auth_error = 0, None
        LOG.info("Beszel authentication succeeded reason=%s expires_at=%s", reason,
                 datetime.fromtimestamp(expires, timezone.utc).isoformat())

    def get(self, path, **params):
        url = self.url + path + ("?" + parse.urlencode(params) if params else "")
        for attempt in range(2):
            if self.auth_error or not self.token or time.time() >= self.token_expires_at - 60:
                self.authenticate("token renewal" if self.token else "initial login")
            try:
                return http_json(url, token=self.token)
            except APIError as exc:
                if exc.status not in (401, 403) or attempt:
                    raise
                LOG.warning("Beszel authorization rejected status=%s; authenticating and retrying once", exc.status)
                self.authenticate("HTTP 401/403")
                message = "Beszel: авторизацію автоматично оновлено після HTTP 401/403."
                if message not in self.events:
                    self.events.append(message)

    def systems(self, system_id=""):
        systems = self._systems(system_id)
        if not systems and time.time() >= self.empty_retry_at:
            self.empty_retry_at = time.time() + 300
            LOG.warning("Beszel returned no matching systems system_id=%s; checking fresh authentication",
                        system_id or "all")
            self.authenticate("empty systems response (HTTP 200)")
            systems = self._systems(system_id)
            if systems:
                LOG.info("Beszel systems recovered after authentication count=%s", len(systems))
                message = "Beszel: доступ до систем автоматично відновлено після повторного входу."
                if message not in self.events:
                    self.events.append(message)
            else:
                LOG.error("Beszel systems still empty after login system_id=%s; check membership and configured ID",
                          system_id or "all")
        yield from systems

    def _systems(self, system_id):
        page = 1
        systems = []
        while True:
            response = self.get("/api/collections/systems/records", page=page, perPage=100)
            for system in response["items"]:
                if not system_id or system["id"] == system_id:
                    systems.append(system)
            if page >= response["totalPages"]:
                return systems
            page += 1

    def latest(self, system_id):
        # The id originates from the authenticated API, but still validate filter input.
        if not system_id.isalnum():
            raise ValueError("invalid system id")
        result = self.get("/api/collections/system_stats/records", perPage=1,
                          sort="-created", filter=f'system="{system_id}" && type="1m"')
        return result["items"][0] if result["items"] else None

    def history(self, system_id, start, end):
        if not system_id.isalnum():
            raise ValueError("invalid system id")
        page = 1
        while True:
            result = self.get(
                "/api/collections/system_stats/records", page=page, perPage=200,
                sort="created", filter=(f'system="{system_id}" && type="1m"'
                                        f' && created>={int(start * 1000)}'
                                        f' && created<={int(end * 1000)}'))
            yield from result["items"]
            if page >= result["totalPages"]:
                return
            page += 1


class Telegram:
    def __init__(self, token, chat_id, call=http_json):
        self.api_url = f"https://api.telegram.org/bot{token}/"
        self.url = self.api_url + "sendMessage"
        self.chat_id, self.call = chat_id, call
        self.retry_at = 0
        self.username = None

    def invoke(self, method, body=None):
        try:
            result = self.call(self.api_url + method, body, service="Telegram")
            if result.get("ok") is not True:
                raise APIError("Telegram", result.get("error_code", 0),
                               result.get("parameters", {}).get("retry_after", 0), operation=method)
            return result
        except APIError as exc:
            if exc.status == 429:
                self.retry_at = max(self.retry_at, time.time() + max(exc.retry_after, 30))
            raise

    def send(self, message):
        # Telegram limits message length. Split reports instead of dropping metrics.
        while message:
            end = len(message) if len(message) <= 4000 else message.rfind("\n", 0, 4001)
            if end <= 0:
                end = 4000
            self._send(message[:end])
            message = message[end:].lstrip("\n")

    def _send(self, message):
        if time.time() < self.retry_at:
            raise APIError("Telegram rate limit", 429)
        try:
            self.invoke("sendMessage", {"chat_id": self.chat_id, "text": message,
                                        "disable_notification": False})
        except APIError as exc:
            self.retry_at = time.time() + max(exc.retry_after, 30)
            raise

    def updates(self, offset):
        result = self.invoke("getUpdates", {
            "offset": offset, "limit": 20, "timeout": 0,
            "allowed_updates": ["message", "channel_post"],
        })
        return result["result"]

    def addressed_to_me(self, command):
        if "@" not in command:
            return True
        if self.username is None:
            result = self.invoke("getMe")
            self.username = result["result"]["username"]
        return command.split("@", 1)[1].casefold() == self.username.casefold()


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


def status_report(beszel, system, now):
    """Arithmetic means of available minute samples, never substituted zeroes."""
    latest = beszel.latest(system["id"])
    windows = {300: {}, 3600: {}}
    names = {"cpu": "CPU", "ram": "RAM"}
    seen = set()
    for record in beszel.history(system["id"], now - 3600, now):
        timestamp = sample_time(record)
        if timestamp in seen or not now - 3600 < timestamp <= now:
            continue
        seen.add(timestamp)
        metrics, _ = metrics_from(record["stats"])
        for metric in metrics:
            names[metric.key] = metric.name
            for seconds, values in windows.items():
                if timestamp > now - seconds:
                    values.setdefault(metric.key, []).append(metric.value)
    current = {}
    if latest:
        timestamp = sample_time(latest)
        fresh = system.get("status") == "up" and 0 <= now - timestamp <= 180
        label = "свіжий" if fresh else "застарілий / сервер недоступний"
        updated = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        for metric in metrics_from(latest["stats"])[0]:
            names[metric.key] = metric.name
            current[metric.key] = metric.value
        stamp = f"Останній вимір: {updated} ({label})"
    else:
        stamp = "Останній вимір: немає даних"
    lines = [f"📊 {str(system['name'])[:120]}", stamp,
             "Метрика: останнє | середнє 5 хв | середнє 1 год"]
    for key, name in names.items():
        cells = [f"{current[key]:.1f}%" if key in current else "н/д"]
        for values in windows.values():
            samples = values.get(key, [])
            cells.append(f"{sum(samples) / len(samples):.1f}% ({len(samples)})" if samples else "н/д")
        lines.append(f"{name}: " + " | ".join(cells))
    lines.append("Середні — за доступними хвилинними вимірами; у дужках їх кількість. "
                 "Повні вікна: приблизно 5 та 60. н/д — немає даних.")
    return "\n".join(lines)


class Commands:
    def __init__(self, path, telegram, beszel, system_id=""):
        self.path, self.telegram, self.beszel = Path(path), telegram, beszel
        self.system_id = system_id
        state, self.load_error = load_state(self.path, lambda s: isinstance(s.get("offset"), int)
                                           and not isinstance(s["offset"], bool) and s["offset"] >= 0)
        self.offset = state.get("offset", 0)

    def poll(self, now):
        for update in self.telegram.updates(self.offset):
            if update["update_id"] < self.offset:
                continue
            message = update.get("message") or update.get("channel_post", {})
            words = message.get("text", "").split()
            command = words[0] if words else ""
            # Only the configured chat may request server data. Skip old queued commands.
            if (str(message.get("chat", {}).get("id")) == str(self.telegram.chat_id)
                    and -30 <= now - message.get("date", 0) <= 300
                    and command.split("@", 1)[0] == "/status"
                    and self.telegram.addressed_to_me(command)):
                self.status(now)
            # A delivery failure leaves this update unacknowledged for retry.
            self.offset = update["update_id"] + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"offset": self.offset}))
            tmp.replace(self.path)

    def status(self, now):
        try:
            systems = list(self.beszel.systems(self.system_id))
            if not systems:
                LOG.warning("Status command: no matching Beszel systems system_id=%s", self.system_id or "all")
                self.telegram.send("Beszel: немає доступних систем для звіту.")
            for system in systems:
                self.telegram.send(status_report(self.beszel, system, now))
        except APIError as exc:
            LOG.warning("Status command failed error=%s", error_detail(exc))
            if not str(exc).startswith("Beszel"):
                raise
            self.telegram.send(f"Не вдалося отримати метрики з Beszel: {error_detail(exc)}. Спробуй /status пізніше.")
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            LOG.error("Status command invalid data error=%s", error_detail(exc))
            self.telegram.send("Beszel повернув некоректні дані. Спробуй /status пізніше.")


class Alerts:
    def __init__(self, path, send, repeat=1800):
        self.path, self.send, self.repeat = Path(path), send, repeat
        self.state, self.load_error = load_state(self.path, valid_alert_state)
        self.saved_state = None

    def save(self):
        serialized = json.dumps(self.state, ensure_ascii=False)
        if serialized == self.saved_state:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(serialized)
        tmp.replace(self.path)
        self.saved_state = serialized

    def notify(self, key, active, message, now):
        LOG.log(logging.WARNING if active else logging.INFO,
                "Alert delivery key=%s active=%s message=%s", key, active, message.replace("\n", " | "))
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

    def issue(self, key, problem, message, now, recovery="Моніторинг відновлено", grace=120):
        key = f"issue:{key}"
        old = self.state.get(key, {})
        if not problem:
            if old:
                LOG.info("Issue recovered key=%s message=%s", key, recovery)
            if old.get("active"):
                self.notify(key, False, f"✅ {recovery}", now)
                del self.state[key]
            elif old:
                del self.state[key]
            return
        if not old or (not old.get("active") and "since" not in old):
            self.state[key] = {"since": now}
            old = self.state[key]
            LOG.warning("Issue detected key=%s grace=%ss message=%s", key, grace, message)
        if not old.get("active") and now - old["since"] >= grace:
            self.notify(key, True, f"⚠️ {message}", now)
        elif old.get("active") and now - old["sent"] >= self.repeat:
            self.notify(key, True, f"⚠️ {message}", now)


class Diagnostics:
    """Persist incidents even when Telegram itself is unavailable; no recursive sends."""

    def __init__(self, alerts):
        self.alerts = alerts

    def fail(self, key, message, exc, now):
        detail = error_detail(exc)
        LOG.error("Operation failed key=%s error=%s", key, detail)
        key = "diagnostic:" + key
        old = self.alerts.state.get(key, {})
        if not old.get("active"):
            # Preserve a pending recovery if the incident recurs before delivery.
            old = {"since": old.get("since", now), "count": old.get("count", 0),
                   **({"sent": old["sent"]} if "sent" in old else {})}
        old.update(active=True, message=message, error=detail, last=now,
                   count=old.get("count", 0) + 1)
        self.alerts.state[key] = old

    def recover(self, key, now):
        item = self.alerts.state.get("diagnostic:" + key)
        if item and item.get("active"):
            LOG.info("Operation recovered key=%s failures=%s duration=%.0fs",
                     key, item["count"], now - item["since"])
            item.update(active=False, ended=now)

    def event(self, key, message, now):
        key = "event:" + key
        old = self.alerts.state.get(key, {})
        if not old or now - old.get("sent", 0) >= self.alerts.repeat:
            self.alerts.notify(key, False, "✅ " + message, now)

    def flush(self, now):
        for key, item in list(self.alerts.state.items()):
            if not key.startswith("diagnostic:"):
                continue
            if item["active"] and "sent" in item and now - item["sent"] < self.alerts.repeat:
                continue
            # Successfully delivering this message itself proves delivery recovery.
            active = item["active"] and key != "diagnostic:telegram-delivery"
            duration = int(item.get("ended", now) - item["since"])
            prefix = "⚠️" if active else "✅"
            status = "Збій триває" if active else "Роботу відновлено"
            message = (f"{prefix} {item['message']}\n{status}; тривалість: {duration} с; "
                       f"невдалих спроб: {item['count']}.\nПричина: {item['error']}")
            try:
                self.alerts.send(message)
            except APIError as exc:
                self.fail("telegram-delivery", "Не вдалося доставити повідомлення Telegram", exc, now)
                return
            if active:
                item["sent"] = now
            else:
                del self.alerts.state[key]
            self.recover("telegram-delivery", now)
            LOG.info("Diagnostic delivered key=%s active=%s failures=%s", key, active, item["count"])
            self.alerts.save()


class Prometheus:
    """Forward existing alert rules; do not resolve incidents from failed evaluations."""

    def __init__(self, url):
        self.url = url.rstrip("/")

    def poll(self, alerts, now):
        response = http_json(self.url + "/api/v1/rules?type=alert", service="Prometheus")
        if response["status"] != "success":
            raise APIError("Prometheus", operation="rules", reason="API reported an error")
        groups = response["data"]["groups"]
        if not isinstance(groups, list) or not groups:
            raise ValueError("missing Prometheus rule groups")
        firing, unhealthy, names = {}, [], set()
        # Parse the complete response before sending/resolving any alerts.
        for group in groups:
            for rule in group["rules"]:
                name = rule["name"]
                names.add(name)
                if rule["health"] != "ok":
                    unhealthy.append(name)
                for item in rule["alerts"]:
                    if item["state"] not in ("pending", "firing"):
                        raise ValueError("invalid Prometheus alert state")
                    if item["state"] != "firing":
                        continue
                    labels, annotations = item["labels"], item.get("annotations", {})
                    identity = hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:24]
                    key = "prometheus:" + identity
                    summary = annotations.get("summary", name)
                    description = annotations.get("description", "")
                    context = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "alertname")
                    firing[key] = (f"Prometheus: {summary}\n{description}\n{context}"[:3500], name)
        if not names:
            raise ValueError("no Prometheus alert rules loaded")
        missing = {item["rule"] for key, item in alerts.state.items()
                   if key.startswith("issue:prometheus:") and "rule" in item and item["rule"] not in names}
        if missing:
            LOG.error("Previously alerting Prometheus rules disappeared rules=%s", ",".join(sorted(missing)))
            raise APIError("Prometheus", operation="rules", reason="previously alerting rules are missing")
        for key, (message, name) in firing.items():
            alerts.issue(key, True, message, now, grace=0)
            alerts.state["issue:" + key].update(message=message, rule=name)
        if unhealthy:
            LOG.error("Prometheus rule evaluation failed rules=%s", ",".join(sorted(unhealthy)))
            raise APIError("Prometheus", operation="rule evaluation", reason="one or more rules unhealthy")
        for key, item in list(alerts.state.items()):
            if key.startswith("issue:prometheus:") and key[6:] not in firing:
                alerts.issue(key[6:], False, "", now,
                             "Prometheus: умова тривоги більше не активна\n" + item.get("message", key[6:]))


def poll(beszel, alerts, now, system_id=""):
    systems = list(beszel.systems(system_id))
    alerts.issue("systems", not systems, "Beszel: немає доступних систем після перевірки авторизації. "
                 "Перевір доступ користувача та TELEGRAM_BESZEL_SYSTEM_ID.", now,
                 "Системи Beszel доступні для моніторингу")
    for system in systems:
        sid, name = system["id"], str(system["name"])[:120]
        record = beszel.latest(sid) if system.get("status") == "up" else None
        stale = record is None or not 0 <= now - sample_time(record) <= 180
        alerts.issue(sid + ":stale", stale, f"{name}: немає свіжих метрик (система Down або дані старші 3 хв)", now,
                     f"{name}: свіжі метрики знову доступні")
        if stale:
            continue  # Neither alert nor resolve from stale values.
        metrics, _ = metrics_from(record["stats"])
        for metric in metrics:
            alerts.metric(sid, name, metric, now)


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Set {name}")
    return value


class Monitor:
    def __init__(self, beszel, telegram, alerts, commands, prometheus=None, interval=30, system_id=""):
        self.beszel, self.telegram, self.alerts, self.commands = beszel, telegram, alerts, commands
        self.prometheus, self.interval, self.system_id = prometheus, interval, system_id
        self.diagnostics = Diagnostics(alerts)
        self.next_metrics = self.next_commands = self.next_prometheus = 0
        for name, source in (("alerts", alerts), ("commands", commands)):
            if isinstance(source.load_error, Exception):
                now = time.time()
                self.diagnostics.fail("state-" + name,
                                      f"Пошкоджений файл стану {name}; збережено копію та створено новий стан",
                                      source.load_error, now)
                self.diagnostics.recover("state-" + name, now)

    def attempt(self, key, message, action, now):
        try:
            action()
            self.diagnostics.recover(key, now)
            return None
        except Exception as exc:
            # Keep other checks alive, including for unexpected parser/storage errors.
            if isinstance(exc, APIError) and exc.service.startswith("Telegram"):
                key = "telegram-delivery" if key != "commands" else key
                if key == "telegram-delivery":
                    message = "Не вдалося доставити повідомлення Telegram"
            self.diagnostics.fail(key, message, exc, now)
            return exc

    def tick(self, now):
        if now >= self.next_metrics:
            self.next_metrics = now + self.interval
            self.attempt("beszel", "Помилка перевірки метрик Beszel", lambda: self.metrics(now), now)
        if self.prometheus and now >= self.next_prometheus:
            self.next_prometheus = now + self.interval
            self.attempt("prometheus", "Помилка перевірки правил Prometheus",
                         lambda: self.prometheus.poll(self.alerts, now), now)
        if now >= max(self.next_commands, self.telegram.retry_at):
            exc = self.attempt("commands", "Помилка обробки команд Telegram",
                               lambda: self.commands.poll(now), now)
            self.next_commands = max(now + (max(30, getattr(exc, "retry_after", 0)) if exc else 2),
                                     self.telegram.retry_at)
        if now >= self.telegram.retry_at:
            # Events are removed only after confirmed delivery (or deduplication).
            while self.beszel.events:
                message = self.beszel.events[0]
                exc = self.attempt("auth-event", "Помилка доставки звіту про авторизацію",
                                   lambda: self.diagnostics.event("beszel-auth", message, now), now)
                if exc:
                    break
                self.beszel.events.pop(0)
            if now >= self.telegram.retry_at:
                self.attempt("diagnostics", "Помилка доставки діагностики",
                             lambda: self.diagnostics.flush(now), now)
        self.attempt("storage", "Не вдалося зберегти стан бота",
                     self.alerts.save, now)

    def metrics(self, now):
        poll(self.beszel, self.alerts, now, self.system_id)
        self.alerts.issue("api", False, "", now, "Зʼєднання бота з Beszel відновлено")
        self.alerts.issue("startup", False, "", now, "Бот успішно запущено після помилки старту")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    state_dir = Path(os.environ.get("STATE_DIR", "/data"))
    heartbeat = state_dir / "heartbeat"
    if args.health:
        return 0 if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 120 else 1
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC %(levelname)s %(message)s")
    try:
        interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
        repeat = int(os.environ.get("ALERT_REPEAT_SECONDS", "1800"))
    except ValueError:
        raise ConfigError("POLL_INTERVAL_SECONDS and ALERT_REPEAT_SECONDS must be integers") from None
    if not 5 <= interval <= 60 or repeat < 60:
        raise ConfigError("POLL_INTERVAL_SECONDS must be 5..60; ALERT_REPEAT_SECONDS must be >=60")
    beszel = Beszel(os.environ.get("BESZEL_URL", "http://beszel:8090"),
                    required("BESZEL_EMAIL"), required("BESZEL_PASSWORD"))
    telegram = Telegram(required("TELEGRAM_BOT_TOKEN"), required("TELEGRAM_CHAT_ID"))
    alerts = Alerts(state_dir / "alerts.json", telegram.send, repeat)
    commands = Commands(state_dir / "commands.json", telegram, beszel,
                        os.environ.get("BESZEL_SYSTEM_ID", ""))
    prometheus_url = os.environ.get("PROMETHEUS_URL", "").strip()
    monitor = Monitor(beszel, telegram, alerts, commands,
                      Prometheus(prometheus_url) if prometheus_url else None, interval,
                      os.environ.get("BESZEL_SYSTEM_ID", ""))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    LOG.info("Monitoring started: CPU/GPU >95%%, RAM/VRAM >70%%, battery <35%%; "
             "poll=%ss repeat=%ss prometheus=%s", interval, repeat, bool(prometheus_url))
    while not stop.is_set():
        now = time.time()
        monitor.tick(now)
        monitor.attempt("heartbeat", "Не вдалося записати heartbeat бота", heartbeat.touch, now)
        stop.wait(2)
    LOG.info("Monitoring stopped by signal")
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        LOG.critical("Bot startup/runtime failure error=%s", error_detail(exc))
        try:
            telegram = Telegram(required("TELEGRAM_BOT_TOKEN"), required("TELEGRAM_CHAT_ID"))
            alerts = Alerts(Path(os.environ.get("STATE_DIR", "/data")) / "alerts.json", telegram.send)
            alerts.issue("startup", True, "Бот не зміг запуститися або аварійно зупинився: "
                         + error_detail(exc), time.time(), grace=0)
        except Exception as delivery_error:
            LOG.critical("Cannot report fatal failure error=%s", error_detail(delivery_error))
        result = 1
    raise SystemExit(result)
