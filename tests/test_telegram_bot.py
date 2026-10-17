import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))
import bot


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.messages = []
        self.path = Path(self.tmp.name) / "alerts.json"
        self.alerts = bot.Alerts(self.path, self.messages.append, repeat=1800)

    def metric(self, value, name="CPU", limit=95, low=False, now=1000):
        self.alerts.metric("host1", "Laptop", bot.Metric(name, name, value, limit, low), now)

    def test_strict_thresholds_for_all_five_metrics(self):
        for name, limit, low in [("CPU", 95, False), ("GPU", 95, False),
                                 ("RAM", 70, False), ("VRAM", 70, False), ("Battery", 35, True)]:
            with self.subTest(name=name):
                count = len(self.messages)
                self.metric(limit, name, limit, low)
                self.assertEqual(len(self.messages), count)
                self.metric(limit - .1 if low else limit + .1, name, limit, low)
                self.assertEqual(len(self.messages), count + 1)

    def test_deduplication_reminders_hysteresis_and_restart(self):
        self.metric(96)
        self.metric(99, now=1030)
        self.assertEqual(len(self.messages), 1)
        self.alerts = bot.Alerts(self.path, self.messages.append)
        self.metric(99, now=1060)
        self.assertEqual(len(self.messages), 1)
        self.metric(94, now=2800)  # Hysteresis band: neither repeat nor recovery.
        self.assertEqual(len(self.messages), 1)
        self.metric(99, now=2830)
        self.assertTrue(self.messages[-1].startswith("🔁"))
        self.metric(92, now=2860)
        self.assertTrue(self.messages[-1].startswith("✅"))
        self.metric(97, now=2890)
        self.assertTrue(self.messages[-1].startswith("🚨"))

    def test_battery_recovery_at_38(self):
        self.metric(34, "battery", 35, True)
        self.metric(36, "battery", 35, True, now=1030)
        self.assertEqual(len(self.messages), 1)
        self.metric(38, "battery", 35, True, now=1060)
        self.assertTrue(self.messages[-1].startswith("✅"))

    def test_failed_delivery_is_retried_not_marked_sent(self):
        self.alerts.send = lambda _: (_ for _ in ()).throw(bot.APIError("Telegram", 503))
        with self.assertRaises(bot.APIError):
            self.metric(99)
        self.assertFalse(self.alerts.state.get("host1:CPU", {}).get("active"))
        self.alerts.send = self.messages.append
        self.metric(99, now=1030)
        self.assertEqual(len(self.messages), 1)

    def test_missing_data_warning_grace_recovery_and_recurrence(self):
        for now in (1000, 1030, 1119):
            self.alerts.issue("gpu", True, "GPU unavailable", now)
        self.assertEqual(self.messages, [])
        self.alerts.issue("gpu", True, "GPU unavailable", 1120)
        self.assertEqual(len(self.messages), 1)
        self.alerts.issue("gpu", False, "GPU", 1130, "GPU available")
        self.assertEqual(self.messages[-1], "✅ GPU available")
        self.alerts.issue("gpu", True, "GPU unavailable again", 1140)
        self.alerts.issue("gpu", True, "GPU unavailable again", 1260)
        self.assertEqual(len(self.messages), 3)

    def test_stale_or_down_samples_do_not_alert_or_resolve(self):
        class FakeBeszel:
            status = "up"
            created = 1000 * 1000
            def systems(self, _):
                return [{"id": "host1", "name": "Laptop", "status": self.status}]
            def latest(self, _):
                return {"created": self.created, "stats": {"cpu": 10, "mp": 20}}
        source = FakeBeszel()
        self.alerts.metric("host1", "Laptop", bot.Metric("cpu", "CPU", 99, 95), 1000)
        bot.poll(source, self.alerts, 1200)
        source.created = 1201 * 1000
        source.status = "down"
        bot.poll(source, self.alerts, 1201)
        self.assertEqual(len(self.messages), 1)

    def test_stale_critical_sample_is_not_replayed(self):
        class FakeBeszel:
            def systems(self, _):
                return [{"id": "host1", "name": "Laptop", "status": "up"}]
            def latest(self, _):
                return {"created": 1000 * 1000, "stats": {"cpu": 99, "mp": 90}}
        bot.poll(FakeBeszel(), self.alerts, 1200)
        self.assertEqual(self.messages, [])


class ParsingTest(unittest.TestCase):
    def test_multiple_gpu_vram_ratios_and_primary_laptop_battery(self):
        metrics, missing = bot.metrics_from({
            "cpu": 96, "mp": 71, "bat": [34, 4], "bats": {"Mouse": 5},
            "g": {"gpu0": {"n": "GPU A", "u": 97, "mu": 8000, "mt": 10000},
                  "gpu1": {"n": "GPU B", "u": 1, "mt": 1000}}})
        values = {m.key: m.value for m in metrics}
        self.assertEqual(values["vram:gpu0"], 80)
        self.assertEqual(values["vram:gpu1"], 0)
        self.assertEqual(values["battery"], 34)
        self.assertEqual(missing, [])

    def test_missing_invalid_and_zero_metrics_are_distinct(self):
        metrics, missing = bot.metrics_from({"cpu": float("nan"), "mp": True,
                                            "g": {"0": {"u": 0, "mt": 0}}})
        self.assertEqual([m.key for m in metrics], ["gpu:0"])
        self.assertIn("CPU", missing)
        self.assertIn("RAM", missing)
        self.assertIn("батарея ноутбука", missing)
        metrics, _ = bot.metrics_from({"cpu": 0, "mp": 0, "bat": [0, 1]})
        self.assertEqual(next(m.value for m in metrics if m.key == "battery"), 0)

    def test_timestamp_milliseconds_and_offsets(self):
        self.assertEqual(bot.sample_time({"created": 1735689600000}), 1735689600)
        self.assertEqual(bot.sample_time({"created": "2025-01-01T02:00:00+02:00"}), 1735689600)


class APITest(unittest.TestCase):
    def test_beszel_renews_expired_auth_and_filters_raw_samples(self):
        calls = []
        def fake(url, body=None, token=None, **_):
            calls.append((url, body, token))
            if body:
                return {"token": "new"}
            if token == "expired":
                raise bot.APIError("Beszel", 401)
            return {"items": [{"stats": {"cpu": 96}}]}
        client = bot.Beszel("http://beszel:8090", "bot@example.com", "test-password")
        client.token = "expired"
        with patch.object(bot, "http_json", side_effect=fake):
            self.assertEqual(client.latest("abc123")["stats"]["cpu"], 96)
        params = parse_qs(urlsplit(calls[-1][0]).query)
        self.assertEqual(params["filter"], ['system="abc123" && type="1m"'])
        self.assertEqual(params["sort"], ["-created"])
        self.assertEqual(client.token, "new")

    def test_pagination_and_system_filter(self):
        client = bot.Beszel("http://beszel", "email", "password")
        def pages(_, page, **kwargs):
            return {"items": [{"id": str(page)}], "totalPages": 2}
        with patch.object(client, "get", side_effect=pages):
            self.assertEqual([s["id"] for s in client.systems("2")], ["2"])

    def test_telegram_push_and_errors_do_not_leak_token(self):
        calls = []
        sender = bot.Telegram("secret-token", "-12345", lambda *a, **k: calls.append((a, k)) or {"ok": True})
        sender.send("🚨 CPU 99%")
        self.assertFalse(calls[0][0][1]["disable_notification"])
        self.assertEqual(calls[0][0][1]["chat_id"], "-12345")
        sender.call = lambda *a, **k: {"ok": False, "error_code": 403}
        with self.assertRaises(bot.APIError) as caught:
            sender.send("test")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_rate_limit_retry_after_and_sanitized_http_error(self):
        exc = HTTPError("https://api.telegram.org/botSECRET/sendMessage", 429, "rate limit", {},
                        io.BytesIO(json.dumps({"parameters": {"retry_after": 120}}).encode()))
        with patch.object(bot.request, "urlopen", side_effect=exc):
            with self.assertRaises(bot.APIError) as caught:
                bot.http_json(exc.url, {}, service="Telegram")
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertNotIn("SECRET", str(caught.exception))
        sender = bot.Telegram("secret", "123", lambda *a, **k: (_ for _ in ()).throw(caught.exception))
        with patch.object(bot.time, "time", return_value=1000):
            with self.assertRaises(bot.APIError):
                sender.send("test")
        self.assertEqual(sender.retry_at, 1120)


if __name__ == "__main__":
    unittest.main()
