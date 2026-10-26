import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
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

    def test_missing_metrics_stay_silent_across_polls_and_restart(self):
        source = Mock()
        source.systems.return_value = [{"id": "host1", "name": "Laptop", "status": "up"}]
        for now in (1000, 1120, 2800, 4600):
            source.latest.return_value = {"created": now * 1000, "stats": {"cpu": 10, "mp": 20}}
            bot.poll(source, self.alerts, now)
            self.alerts.save()
            self.alerts = bot.Alerts(self.path, self.messages.append)
        self.assertEqual(self.messages, [])

    def test_legacy_missing_warning_stays_silent_and_thresholds_still_work(self):
        self.alerts.state["issue:host1:missing"] = {"active": True, "sent": 1000}
        self.alerts.save()
        self.alerts = bot.Alerts(self.path, self.messages.append)
        source = Mock()
        source.systems.return_value = [{"id": "host1", "name": "Laptop", "status": "up"}]
        source.latest.return_value = {"created": 2800 * 1000, "stats": {"cpu": 99, "mp": 20}}
        bot.poll(source, self.alerts, 2800)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("CPU: 99.0%", self.messages[0])

        source.latest.return_value = {"created": 2830 * 1000, "stats": {
            "cpu": 90, "mp": 20, "bat": [60, 1],
            "g": {"0": {"u": 10, "mu": 20, "mt": 100}},
        }}
        bot.poll(source, self.alerts, 2830)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("CPU: 90.0%", self.messages[1])
        self.assertIn("відновлено", self.messages[1])


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
    def test_history_uses_millisecond_bounds_and_paginates_raw_samples(self):
        client = bot.Beszel("http://beszel", "email", "password")
        with patch.object(client, "get", side_effect=[
            {"items": [{"id": "a"}], "totalPages": 2},
            {"items": [{"id": "b"}], "totalPages": 2},
        ]) as get:
            self.assertEqual(list(client.history("host1", 1000, 4600)), [{"id": "a"}, {"id": "b"}])
        self.assertEqual(get.call_args.kwargs["page"], 2)
        self.assertEqual(get.call_args.kwargs["filter"],
                         'system="host1" && type="1m" && created>=1000000 && created<=4600000')
        with self.assertRaises(ValueError):
            list(client.history('bad"id', 0, 1))

    def test_beszel_renews_expired_auth_and_filters_raw_samples(self):
        calls = []
        new_token = "header." + base64.urlsafe_b64encode(json.dumps({"exp": 9999999999}).encode()).decode().rstrip("=") + ".signature"
        def fake(url, body=None, token=None, **_):
            calls.append((url, body, token))
            if body:
                return {"token": new_token}
            if token == "expired":
                raise bot.APIError("Beszel", 401)
            return {"items": [{"stats": {"cpu": 96}}]}
        client = bot.Beszel("http://beszel:8090", "bot@example.com", "test-password")
        client.token = "expired"
        client.token_expires_at = 9999999999  # Revoked token before local expiry: exercise HTTP 401.
        with patch.object(bot, "http_json", side_effect=fake):
            self.assertEqual(client.latest("abc123")["stats"]["cpu"], 96)
        params = parse_qs(urlsplit(calls[-1][0]).query)
        self.assertEqual(params["filter"], ['system="abc123" && type="1m"'])
        self.assertEqual(params["sort"], ["-created"])
        self.assertEqual(client.token, new_token)

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

    def test_telegram_updates_include_messages_and_channel_posts(self):
        call = Mock(return_value={"ok": True, "result": [{"update_id": 4}]})
        sender = bot.Telegram("secret", "123", call)
        self.assertEqual(sender.updates(3), [{"update_id": 4}])
        self.assertEqual(call.call_args.args[1], {
            "offset": 3, "limit": 20, "timeout": 0,
            "allowed_updates": ["message", "channel_post"]})

    def test_long_reports_are_split_without_truncation(self):
        call = Mock(return_value={"ok": True})
        sender = bot.Telegram("secret", "123", call)
        message = "CPU: 12%\n" * 1000
        sender.send(message)
        chunks = [c.args[1]["text"] for c in call.call_args_list]
        self.assertTrue(all(len(c) <= 4000 for c in chunks))
        self.assertEqual("".join(c.replace("\n", "") for c in chunks), message.replace("\n", ""))


class StatusTest(unittest.TestCase):
    now = 10000

    def source(self, records, latest=None, systems=None):
        source = Mock()
        source.history.return_value = records
        source.latest.return_value = latest if latest is not None else (records[-1] if records else None)
        source.systems.return_value = systems or [{"id": "host1", "name": "VPS", "status": "up"}]
        return source

    def record(self, age, **stats):
        return {"created": (self.now - age) * 1000, "stats": stats}

    def report(self, source, status="up"):
        return bot.status_report(source, {"id": "host1", "name": "VPS", "status": status}, self.now)

    def test_current_five_minute_and_hour_means_are_distinct(self):
        records = [self.record(i * 60 + 30, cpu=i, mp=20) for i in range(60)]
        source = self.source(records, latest=records[0])
        report = self.report(source)
        self.assertIn("CPU: 0.0% | 2.0% (5) | 29.5% (60)", report)
        self.assertIn("RAM: 20.0% | 20.0% (5) | 20.0% (60)", report)
        self.assertIn("UTC (свіжий)", report)
        source.history.assert_called_once_with("host1", 6400, 10000)

    def test_partial_history_missing_values_and_zeroes(self):
        records = [self.record(100, cpu=0), self.record(50, cpu=20, mp=50)]
        report = self.report(self.source(records))
        self.assertIn("CPU: 20.0% | 10.0% (2) | 10.0% (2)", report)
        self.assertIn("RAM: 50.0% | 50.0% (1) | 50.0% (1)", report)
        self.assertIn("кількість", report)

    def test_window_boundaries_future_and_duplicate_records(self):
        record = self.record(10, cpu=10)
        records = [record, record, self.record(300, cpu=30),
                   self.record(3600, cpu=100), self.record(-1, cpu=100)]
        report = self.report(self.source(records, latest=record))
        self.assertIn("CPU: 10.0% | 10.0% (1) | 20.0% (2)", report)

    def test_stale_down_and_absent_samples_are_explicit(self):
        source = self.source([self.record(600, cpu=50)])
        self.assertIn("застарілий", self.report(source))
        self.assertIn("CPU: 50.0% | н/д | 50.0% (1)", self.report(source))
        self.assertIn("сервер недоступний", self.report(self.source([self.record(10, cpu=50)]), "down"))
        empty = self.report(self.source([]))
        self.assertIn("Останній вимір: немає даних", empty)
        self.assertIn("CPU: н/д | н/д | н/д", empty)

    def test_gpu_devices_have_separate_averages(self):
        record = self.record(30, cpu=1, g={
            "0": {"n": "GPU A", "u": 10, "mu": 20, "mt": 100},
            "1": {"n": "GPU B", "u": 70, "mu": 80, "mt": 100}}, bat=[60, 1])
        report = self.report(self.source([record]))
        self.assertIn("GPU GPU A: 10.0% | 10.0% (1)", report)
        self.assertIn("VRAM GPU B: 80.0% | 80.0% (1)", report)
        self.assertIn("Батарея ноутбука: 60.0%", report)


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "commands.json"
        self.telegram = Mock(chat_id="123")
        self.telegram.addressed_to_me.return_value = True
        self.source = Mock()
        self.commands = bot.Commands(self.path, self.telegram, self.source)

    def update(self, update_id=5, text="/status", chat=123, date=1000, kind="message"):
        return {"update_id": update_id, kind: {"text": text, "chat": {"id": chat}, "date": date}}

    def test_only_configured_chat_and_fresh_status_are_processed(self):
        self.telegram.updates.return_value = [
            self.update(1, chat=999), self.update(2, date=0),
            self.update(3, text="/other"), self.update(4, text="/status@mybot"),
        ]
        with patch.object(self.commands, "status") as status:
            self.commands.poll(1000)
        status.assert_called_once_with(1000)
        self.assertEqual(self.commands.offset, 5)
        self.assertEqual(bot.Commands(self.path, self.telegram, self.source).offset, 5)

    def test_commands_for_another_bot_and_edited_messages_are_ignored(self):
        self.telegram.addressed_to_me.return_value = False
        self.telegram.updates.return_value = [self.update(text="/status@otherbot"),
                                             {"update_id": 6, "edited_message": self.update()["message"]}]
        with patch.object(self.commands, "status") as status:
            self.commands.poll(1000)
        status.assert_not_called()

    def test_channel_status_posts_send_reports_to_configured_channel(self):
        self.telegram.chat_id = "-1001234567890"
        system = {"id": "host1", "name": "Laptop", "status": "up"}
        self.source.systems.return_value = [system]
        self.telegram.updates.return_value = [
            self.update(1, chat=-1001234567890, kind="channel_post"),
            self.update(2, text="/status@mybot", chat=-1001234567890, kind="channel_post"),
        ]
        with patch.object(bot, "status_report", return_value="Server report") as report:
            self.commands.poll(1000)
        self.assertEqual(report.call_count, 2)
        report.assert_called_with(self.source, system, 1000)
        self.assertEqual(self.telegram.send.call_args_list,
                         [unittest.mock.call("Server report"), unittest.mock.call("Server report")])
        self.assertEqual(bot.Commands(self.path, self.telegram, self.source).offset, 3)

    def test_channel_posts_from_other_chats_old_posts_and_noncommands_are_ignored(self):
        self.telegram.updates.return_value = [
            self.update(1, chat=999, kind="channel_post"),
            self.update(2, date=0, kind="channel_post"),
            self.update(3, text="Server report", kind="channel_post"),
            self.update(4, kind="edited_channel_post"),
        ]
        with patch.object(self.commands, "status") as status:
            self.commands.poll(1000)
        status.assert_not_called()
        self.assertEqual(self.commands.offset, 5)

    def test_channel_command_for_another_bot_is_ignored(self):
        self.telegram.addressed_to_me.return_value = False
        self.telegram.updates.return_value = [
            self.update(text="/status@otherbot", kind="channel_post"),
        ]
        with patch.object(self.commands, "status") as status:
            self.commands.poll(1000)
        status.assert_not_called()

    def test_telegram_username_is_checked_and_cached(self):
        call = Mock(return_value={"ok": True, "result": {"username": "MyBot"}})
        telegram = bot.Telegram("secret", "123", call)
        self.assertTrue(telegram.addressed_to_me("/status@mybot"))
        self.assertFalse(telegram.addressed_to_me("/status@otherbot"))
        call.assert_called_once()

    def test_delivery_failure_does_not_acknowledge_command(self):
        for kind in ("message", "channel_post"):
            with self.subTest(kind=kind):
                self.telegram.updates.return_value = [self.update(kind=kind)]
                with patch.object(self.commands, "status", side_effect=bot.APIError("Telegram", 503)):
                    with self.assertRaises(bot.APIError):
                        self.commands.poll(1000)
                self.assertEqual(self.commands.offset, 0)
                self.assertFalse(self.path.exists())

    def test_status_reports_each_server_and_honors_system_filter(self):
        self.commands.system_id = "host2"
        self.source.systems.return_value = [{"id": "host1"}, {"id": "host2"}]
        with patch.object(bot, "status_report", side_effect=["Server 1", "Server 2"]):
            self.commands.status(1000)
        self.source.systems.assert_called_once_with("host2")
        self.assertEqual([c.args[0] for c in self.telegram.send.call_args_list], ["Server 1", "Server 2"])

    def test_beszel_outage_sends_clear_reply_without_secrets(self):
        self.source.systems.side_effect = bot.APIError("Beszel", 503)
        self.commands.status(1000)
        self.assertIn("Не вдалося отримати метрики", self.telegram.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
