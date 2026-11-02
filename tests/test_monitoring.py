import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))
import bot


def jwt(expires):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


class AuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.client = bot.Beszel("http://beszel", "user", "SECRET_PASSWORD")
        self.client.token = jwt(2000)
        self.client.token_expires_at = 2000

    def test_renewal_prevents_silent_empty_response_after_days_of_uptime(self):
        fresh = jwt(600000)
        def api(url, body=None, token=None, **kwargs):
            if body:
                return {"token": fresh}
            if token == self.client_old_token:
                return {"items": [], "totalPages": 0}  # PocketBase's expired-token behavior.
            return {"items": [{"id": "host1"}], "totalPages": 1}
        self.client_old_token = self.client.token
        with patch.object(bot.time, "time", return_value=500000), patch.object(bot, "http_json", side_effect=api) as call:
            self.assertEqual(list(self.client.systems()), [{"id": "host1"}])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.client.token, fresh)
        self.assertEqual(self.client.events, [])  # Scheduled renewal is routine, not an outage.

    def test_renewal_starts_before_expiry(self):
        with patch.object(bot.time, "time", return_value=1940), patch.object(bot, "http_json", side_effect=[
            {"token": jwt(5000)}, {"items": []},
        ]) as call:
            self.client.latest("host1")
        self.assertIn("auth-with-password", call.call_args_list[0].args[0])

    def test_valid_token_is_reused(self):
        with patch.object(bot.time, "time", return_value=1000), patch.object(bot, "http_json", return_value={"items": []}) as call:
            self.client.latest("host1")
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs["token"], jwt(2000))

    def test_empty_http_200_recovers_with_one_login(self):
        with patch.object(bot.time, "time", return_value=1000), patch.object(bot, "http_json", side_effect=[
            {"items": [], "totalPages": 0}, {"token": jwt(5000)},
            {"items": [{"id": "host1"}], "totalPages": 1},
        ]) as call:
            self.assertEqual(list(self.client.systems()), [{"id": "host1"}])
        self.assertEqual(call.call_count, 3)
        self.assertEqual(len(self.client.events), 1)

    def test_genuinely_empty_systems_do_not_create_login_loop(self):
        empty = {"items": [], "totalPages": 0}
        with patch.object(bot.time, "time", return_value=1000), patch.object(self.client, "authenticate") as auth, \
                patch.object(self.client, "get", return_value=empty):
            for _ in range(20):
                self.assertEqual(list(self.client.systems("missing")), [])
        auth.assert_called_once()
        with patch.object(bot.time, "time", return_value=1300), patch.object(self.client, "authenticate") as auth, \
                patch.object(self.client, "get", return_value=empty):
            self.assertEqual(list(self.client.systems("missing")), [])
        auth.assert_called_once()
        self.assertEqual(self.client.events, [])

    def test_login_failure_backoff_and_retry(self):
        self.client.token_expires_at = 1000
        with patch.object(bot.time, "time", return_value=1000), patch.object(bot, "http_json", side_effect=bot.APIError("Beszel", 400)) as call:
            for _ in range(3):
                with self.assertRaises(bot.APIError):
                    self.client.latest("host1")
        call.assert_called_once()
        with patch.object(bot.time, "time", return_value=1030), patch.object(bot, "http_json", side_effect=[
            {"token": jwt(5000)}, {"items": []},
        ]):
            self.assertIsNone(self.client.latest("host1"))

    def test_failed_empty_response_reauthentication_cannot_look_like_recovery(self):
        with patch.object(bot.time, "time", return_value=1000), patch.object(bot, "http_json", side_effect=[
            {"items": [], "totalPages": 0}, bot.APIError("Beszel", 400),
        ]):
            with self.assertRaises(bot.APIError):
                list(self.client.systems())
        with patch.object(bot.time, "time", return_value=1010), patch.object(bot, "http_json") as call:
            with self.assertRaises(bot.APIError):
                list(self.client.systems())
        call.assert_not_called()

    def test_rejected_authorization_retries_only_once(self):
        with patch.object(bot.time, "time", return_value=1000), patch.object(bot, "http_json", side_effect=[
            bot.APIError("Beszel", 403), {"token": jwt(5000)}, bot.APIError("Beszel", 403),
        ]) as call:
            with self.assertRaises(bot.APIError):
                self.client.latest("host1")
        self.assertEqual(call.call_count, 3)

    def test_invalid_auth_payload_is_sanitized_and_does_not_replace_token(self):
        for token in ("SECRET_TOKEN", jwt("SECRET"), jwt(True), jwt(float("nan"))):
            with self.subTest(token=token):
                self.client.auth_retry_at = 0
                with patch.object(bot.time, "time", return_value=1000), patch.object(bot, "http_json", return_value={"token": token}), \
                        self.assertLogs(bot.LOG, level="INFO") as logs:
                    with self.assertRaises(bot.APIError) as caught:
                        self.client.authenticate()
                self.assertNotIn("SECRET", str(caught.exception) + " ".join(logs.output))
                self.assertEqual(self.client.token, jwt(2000))


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "alerts.json"
        self.messages = []
        self.alerts = bot.Alerts(self.path, self.messages.append)
        self.diagnostics = bot.Diagnostics(self.alerts)


class IncidentTest(StateTestCase):
    def test_failure_repeat_recovery_and_restart(self):
        self.diagnostics.fail("api", "API failure", bot.APIError("Beszel", 503), 1000)
        self.diagnostics.flush(1000)
        self.diagnostics.fail("api", "API failure", bot.APIError("Beszel", 503), 1030)
        self.alerts.save()
        self.alerts = bot.Alerts(self.path, self.messages.append)
        self.diagnostics = bot.Diagnostics(self.alerts)
        self.diagnostics.flush(1030)
        self.assertEqual(len(self.messages), 1)
        self.diagnostics.flush(2800)
        self.assertEqual(len(self.messages), 2)
        self.diagnostics.recover("api", 2810)
        self.diagnostics.flush(2810)
        self.assertEqual(len(self.messages), 3)
        self.assertIn("Роботу відновлено", self.messages[-1])
        self.assertIn("невдалих спроб: 2", self.messages[-1])
        self.assertNotIn("diagnostic:api", self.alerts.state)

    def test_telegram_outage_survives_restart_and_reports_after_recovery(self):
        self.diagnostics.fail("api", "API failure", bot.APIError("Beszel", 503), 1000)
        self.alerts.send = Mock(side_effect=bot.APIError("Telegram", 503))
        self.diagnostics.flush(1000)
        self.assertNotIn("sent", self.alerts.state["diagnostic:api"])
        self.alerts.save()
        alerts = bot.Alerts(self.path, self.messages.append)
        diagnostics = bot.Diagnostics(alerts)
        diagnostics.recover("api", 1100)
        diagnostics.flush(1100)
        self.assertEqual(len(self.messages), 2)
        self.assertTrue(all("Роботу відновлено" in msg for msg in self.messages))
        self.assertEqual(alerts.state, {})

    def test_repaired_auth_event_deduplicates_across_restart(self):
        self.diagnostics.event("auth", "repaired", 1000)
        alerts = bot.Alerts(self.path, self.messages.append)
        bot.Diagnostics(alerts).event("auth", "repaired", 1030)
        self.assertEqual(self.messages, ["✅ repaired"])

    def test_exception_messages_and_locals_are_not_exposed(self):
        try:
            raise ValueError("SECRET_PASSWORD")
        except ValueError as exc:
            with self.assertLogs(bot.LOG, level="ERROR") as logs:
                self.diagnostics.fail("parser", "Parser error", exc, 1000)
        self.diagnostics.flush(1000)
        output = " ".join(logs.output + self.messages)
        self.assertNotIn("SECRET_PASSWORD", output)
        self.assertIn("ValueError", output)
        self.assertIn("test_monitoring.py", output)

    def test_corrupted_state_is_preserved_and_reported(self):
        self.path.write_text('{"SECRET":')
        alerts = bot.Alerts(self.path, self.messages.append)
        self.assertEqual(alerts.state, {})
        self.assertIsInstance(alerts.load_error, ValueError)
        self.assertEqual(len(list(self.path.parent.glob("alerts.json.corrupt-*"))), 1)
        telegram = Mock(retry_at=0)
        source = Mock(events=[])
        commands = Mock(load_error=None)
        monitor = bot.Monitor(source, telegram, alerts, commands)
        monitor.diagnostics.flush(bot.time.time())
        self.assertIn("Пошкоджений файл стану", self.messages[-1])
        self.assertNotIn("SECRET", self.messages[-1])

    def test_rate_limited_command_polling_defers_sends(self):
        telegram = bot.Telegram("SECRET", "123", Mock(return_value={
            "ok": False, "error_code": 429, "parameters": {"retry_after": 120},
        }))
        with patch.object(bot.time, "time", return_value=1000):
            with self.assertRaises(bot.APIError):
                telegram.updates(0)
            with self.assertRaises(bot.APIError):
                telegram.send("test")
        self.assertEqual(telegram.retry_at, 1120)
        self.assertEqual(telegram.call.call_count, 1)

    def test_network_error_details_do_not_contain_url_or_token(self):
        exc = URLError(ConnectionRefusedError(61, "SECRET"))
        with patch.object(bot.request, "urlopen", side_effect=exc), self.assertLogs(bot.LOG) as logs:
            with self.assertRaises(bot.APIError) as caught:
                bot.http_json("https://api.telegram.org/botSECRET/getUpdates", service="Telegram")
        output = " ".join(logs.output) + str(caught.exception)
        self.assertNotIn("SECRET", output)
        self.assertIn("getUpdates", output)
        self.assertIn("ConnectionRefusedError errno=61", output)


class PrometheusTest(StateTestCase):
    def response(self, items=None, health="ok"):
        return {"status": "success", "data": {"groups": [{"rules": [{
            "name": "ExporterDown", "health": health, "alerts": items or [],
        }]}]}}

    def item(self, instance="node-host:9100", state="firing"):
        return {"state": state, "labels": {"alertname": "ExporterDown", "instance": instance},
                "annotations": {"summary": "Exporter unavailable", "description": "No metrics"}}

    def poll(self, response, now=1000):
        with patch.object(bot, "http_json", return_value=response):
            bot.Prometheus("http://prometheus:9090").poll(self.alerts, now)

    def test_firing_pending_repeat_and_recovery(self):
        self.poll(self.response([self.item(state="pending")]))
        self.assertEqual(self.messages, [])
        firing = self.response([self.item()])
        self.poll(firing)
        self.poll(firing, 1030)
        self.assertEqual(len(self.messages), 1)
        self.poll(firing, 2800)
        self.assertEqual(len(self.messages), 2)
        self.poll(self.response(), 2830)
        self.assertEqual(len(self.messages), 3)
        self.assertIn("умова тривоги більше не активна", self.messages[-1])

    def test_distinct_targets_are_not_deduplicated_together(self):
        self.poll(self.response([self.item(), self.item("cadvisor:8080")]))
        self.assertEqual(len(self.messages), 2)

    def test_firing_state_survives_restart_and_preserves_recovery_context(self):
        self.poll(self.response([self.item()]))
        self.alerts.save()
        self.alerts = bot.Alerts(self.path, self.messages.append)
        self.poll(self.response([self.item()]), 1030)
        self.assertEqual(len(self.messages), 1)
        self.poll(self.response(), 1060)
        self.assertIn("Exporter unavailable", self.messages[-1])

    def test_invalid_or_unhealthy_response_never_resolves_active_alert(self):
        self.poll(self.response([self.item()]))
        for response in ({"status": "error"}, {"status": "success", "data": {"groups": []}},
                         self.response(health="err"), self.response([{"state": "unknown"}])):
            with self.subTest(response=response):
                with self.assertRaises((ValueError, bot.APIError)):
                    self.poll(response, 1030)
                self.assertEqual(len(self.messages), 1)
        with patch.object(bot, "http_json", side_effect=bot.APIError("Prometheus", 503)):
            with self.assertRaises(bot.APIError):
                bot.Prometheus("http://prometheus").poll(self.alerts, 1030)
        self.assertEqual(len(self.messages), 1)

    def test_disappearing_rule_is_a_monitoring_error_not_recovery(self):
        self.poll(self.response([self.item()]))
        changed = self.response()
        changed["data"]["groups"][0]["rules"][0]["name"] = "DifferentRule"
        with self.assertRaises(bot.APIError):
            self.poll(changed, 1030)
        self.assertEqual(len(self.messages), 1)


class MonitorTest(StateTestCase):
    def monitor(self):
        self.source = Mock(events=[])
        self.telegram = Mock(retry_at=0)
        self.commands = Mock(load_error=None)
        self.prometheus = Mock()
        return bot.Monitor(self.source, self.telegram, self.alerts, self.commands, self.prometheus)

    def test_beszel_failure_does_not_stop_prometheus_or_commands(self):
        monitor = self.monitor()
        with patch.object(bot, "poll", side_effect=bot.APIError("Beszel", 503)):
            monitor.tick(1000)
        self.prometheus.poll.assert_called_once()
        self.commands.poll.assert_called_once()
        self.assertIn("Помилка перевірки метрик Beszel", self.messages[0])
        self.assertTrue(self.path.exists())

    def test_delivery_failure_is_not_classified_as_beszel_failure(self):
        monitor = self.monitor()
        with patch.object(bot, "poll", side_effect=bot.APIError("Telegram", 503)):
            monitor.tick(1000)
        self.assertNotIn("diagnostic:beszel", self.alerts.state)
        self.assertTrue(any("доставити повідомлення Telegram" in m for m in self.messages))

    def test_command_conflict_is_reported_with_recovery(self):
        monitor = self.monitor()
        self.commands.poll.side_effect = bot.APIError("Telegram", 409, operation="getUpdates")
        with patch.object(bot, "poll"):
            monitor.tick(1000)
            monitor.tick(1002)
            self.assertEqual(self.commands.poll.call_count, 1)
            self.commands.poll.side_effect = None
            monitor.tick(1030)
        self.assertIn("HTTP 409", self.messages[0])
        self.assertIn("Роботу відновлено", self.messages[-1])

    def test_unexpected_error_is_logged_and_does_not_stop_other_checks(self):
        monitor = self.monitor()
        with patch.object(bot, "poll", side_effect=RuntimeError("SECRET")), self.assertLogs(bot.LOG) as logs:
            monitor.tick(1000)
        self.prometheus.poll.assert_called_once()
        self.assertNotIn("SECRET", " ".join(logs.output + self.messages))
        self.assertIn("RuntimeError", self.messages[0])

    def test_failed_event_delivery_is_retried(self):
        monitor = self.monitor()
        self.source.events.append("Authentication repaired")
        self.alerts.send = Mock(side_effect=bot.APIError("Telegram", 503))
        with patch.object(bot, "poll"):
            monitor.tick(1000)
            self.assertEqual(self.source.events, ["Authentication repaired"])
            self.alerts.send = self.messages.append
            monitor.tick(1002)
        self.assertEqual(self.source.events, [])
        self.assertIn("Authentication repaired", self.messages[0])

    def test_command_polling_waits_for_shared_telegram_rate_limit(self):
        monitor = self.monitor()
        self.telegram.retry_at = 1120
        with patch.object(bot, "poll"):
            monitor.tick(1000)
            self.commands.poll.assert_not_called()
            monitor.tick(1120)
        self.commands.poll.assert_called_once()

    def test_state_write_failure_is_reported_and_loop_recovers(self):
        monitor = self.monitor()
        with patch.object(bot, "poll"), patch.object(self.alerts, "save", side_effect=OSError(28, "SECRET")):
            monitor.tick(1000)
        self.assertTrue(self.alerts.state["diagnostic:storage"]["active"])
        with patch.object(bot, "poll"):
            monitor.tick(1002)
            monitor.tick(1004)
        self.assertTrue(any("errno=28" in m for m in self.messages))
        self.assertNotIn("diagnostic:storage", self.alerts.state)
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
