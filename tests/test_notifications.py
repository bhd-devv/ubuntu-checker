from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))
import bot
from notifications import telegram_length
from storage_support import events, open_alerts


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "alerts.json"
        self.messages = []
        self.alerts = self.open_alerts()

    def open_alerts(self, now=1000):
        return open_alerts(self, self.path, self.messages.append, now)

    def test_empty_windows_are_silent_and_later_event_waits_for_next_window(self):
        self.assertFalse(self.alerts.flush(1300))
        self.assertFalse(self.alerts.flush(1600))
        self.alerts.notify("cpu", True, "CPU critical", 1601)
        self.assertFalse(self.alerts.flush(1899))
        self.assertTrue(self.alerts.flush(1900))
        self.assertFalse(self.alerts.flush(2200))
        self.assertEqual(len(self.messages), 1)

    def test_multiple_sources_and_repeated_failures_use_one_send_message(self):
        call = Mock(return_value={"ok": True})
        telegram = bot.Telegram("SECRET", "123", call)
        self.alerts.send = telegram.send
        diagnostics = bot.Diagnostics(self.alerts)
        self.alerts.metric("host", "Host", bot.Metric("cpu", "CPU", 99, 95), 1000)
        self.alerts.issue("exporter", True, "Exporter unavailable", 1030, grace=0)
        for now in (1000, 1030, 1060):
            diagnostics.fail("commands", "Commands unavailable", bot.APIError("Telegram", reason="TimeoutError"), now)
        self.assertFalse(self.alerts.flush(1299))
        call.assert_not_called()
        self.alerts.flush(1300)
        call.assert_called_once()
        self.assertTrue(call.call_args.args[0].endswith("/sendMessage"))
        message = call.call_args.args[1]["text"]
        for expected in ("CPU", "Exporter unavailable", "TimeoutError", "невдалих спроб: 3", "Подій: 5; груп: 3"):
            self.assertIn(expected, message)

    def test_short_incident_includes_original_problem_and_recovery(self):
        self.alerts.metric("host", "Host", bot.Metric("cpu", "CPU", 99, 95), 1000)
        self.alerts.metric("host", "Host", bot.Metric("cpu", "CPU", 20, 95), 1030)
        self.alerts.flush(1300)
        self.assertEqual(len(self.messages), 1)
        for expected in ("відновлено", "CPU: 99.0%", "CPU: 20.0%", "подій: 2"):
            self.assertIn(expected, self.messages[0])

    def test_failed_digest_and_deadline_survive_restart(self):
        self.alerts.notify("cpu", True, "CPU critical", 1000)
        self.alerts.send = Mock(side_effect=bot.APIError("Telegram", reason="TimeoutError"))
        with self.assertRaises(bot.APIError):
            self.alerts.flush(1300)
        restarted = self.open_alerts(now=1310)
        self.assertFalse(restarted.flush(1310))
        self.assertFalse(restarted.flush(1599))
        self.assertTrue(restarted.flush(1600))
        self.assertEqual(len(self.messages), 1)
        self.assertFalse(restarted.flush(1900))

    def test_large_digest_is_one_message_and_preserves_full_history(self):
        with self.assertLogs(bot.LOG, level="WARNING"):
            for index in range(40):
                self.alerts.notify(str(index), True, f"{index}: " + "🚨" * 300, 1000 + index)
        self.alerts.flush(1300)
        self.assertEqual(len(self.messages), 1)
        self.assertLessEqual(telegram_length(self.messages[0]), 4000)
        self.assertIn("Скорочено груп:", self.messages[0])
        self.assertEqual(sum(event.delivered is not None for event in events(self.alerts)), 40)
        self.assertIn("🚨" * 300, events(self.alerts)[0].message)

    def test_single_large_event_is_truncated_without_splitting_surrogates(self):
        with self.assertLogs(bot.LOG, level="WARNING"):
            self.alerts.notify("large", True, "🚨" * 5000, 1000)
        self.alerts.flush(1300)
        self.assertEqual(len(self.messages), 1)
        self.assertLessEqual(telegram_length(self.messages[0]), 4000)
        self.assertIn("Повна історія", self.messages[0])

    def test_acknowledgement_does_not_consume_events_recorded_during_delivery(self):
        self.alerts.notify("cpu", True, "CPU critical", 1000)
        def send(message):
            self.assertEqual(self.alerts.store.engine.pool.checkedout(), 0)
            self.messages.append(message)
            self.alerts.notify("ram", True, "RAM critical", 1301)
        self.alerts.send = send
        self.alerts.flush(1300)
        pending = [event.message for event in events(self.alerts) if event.delivered is None]
        self.assertEqual(pending, ["RAM critical"])
        self.alerts.send = self.messages.append
        self.alerts.flush(1600)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("RAM critical", self.messages[1])

    def test_state_and_event_commit_atomically_and_can_retry_storage_failure(self):
        with self.assertRaises(IntegrityError):
            self.alerts.store.save({"cpu": {"active": True, "sent": 1000}}, [(None, "CPU critical", 1000)])
        self.assertEqual(self.alerts.store.load(), {})
        self.assertEqual(events(self.alerts), [])
        self.alerts.notify("cpu", True, "CPU critical", 1000)
        restarted = self.open_alerts()
        self.assertTrue(restarted.state["cpu"]["active"])
        restarted.flush(1300)
        self.assertIn("CPU critical", self.messages[0])

    def test_retention_removes_only_delivered_events(self):
        self.alerts.notify("old", True, "Old event", 1000)
        self.alerts.flush(1300)
        self.alerts.notify("pending", True, "Undelivered event", 1400)
        self.alerts.send = Mock(side_effect=bot.APIError("Telegram", 503))
        with self.assertRaises(bot.APIError):
            self.alerts.flush(1301 + 30 * 86400)
        self.assertEqual([(event.message, event.delivered) for event in events(self.alerts)],
                         [("Undelivered event", None)])

    def test_legacy_json_import_preserves_deduplication_and_pending_diagnostics(self):
        self.path = self.path.parent / "legacy.json"
        legacy = {
            "host:cpu": {"active": True, "sent": 1000},
            "diagnostic:commands": {"active": False, "since": 900, "ended": 950, "count": 2,
                                    "message": "Commands failed", "error": "TimeoutError"},
        }
        self.path.write_text(json.dumps(legacy))
        alerts = self.open_alerts()
        alerts.metric("host", "Host", bot.Metric("cpu", "CPU", 99, 95), 1030)
        self.assertNotIn("diagnostic:commands", alerts.state)
        self.assertEqual(json.loads(self.path.read_text()), legacy)
        alerts.flush(1300)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Роботу відновлено", self.messages[0])
        self.assertNotIn("CPU", self.messages[0])
        self.path.write_text("broken old backup")
        restarted = self.open_alerts(now=1400)
        self.assertIsNone(restarted.load_error)
        self.assertFalse(restarted.flush(1600))

    def test_healthy_monitor_creates_no_event_history(self):
        source = Mock(events=[])
        source.systems.return_value = [{"id": "host", "name": "Host", "status": "up"}]
        commands = Mock(load_error=None)
        monitor = bot.Monitor(source, Mock(retry_at=0), self.alerts, commands)
        for now in range(1000, 1631, 30):
            source.latest.return_value = {"created": now * 1000, "stats": {"cpu": 10, "mp": 20}}
            monitor.tick(now)
        self.assertEqual(events(self.alerts), [])
        self.assertEqual(self.messages, [])

    def test_monitor_respects_rate_limit_and_records_while_telegram_is_unavailable(self):
        source = Mock(events=[])
        telegram = Mock(retry_at=1700)
        monitor = bot.Monitor(source, telegram, self.alerts, Mock(load_error=None))
        with patch.object(bot, "poll", side_effect=bot.APIError("Beszel", 503)):
            monitor.tick(1000)
            monitor.tick(1300)
            self.assertEqual(self.messages, [])
            self.assertEqual(len(events(self.alerts)), 2)
            monitor.tick(1700)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("невдалих спроб: 3", self.messages[0])

    def test_command_polling_uses_thirty_seconds_between_successful_calls(self):
        commands = Mock(load_error=None)
        monitor = bot.Monitor(Mock(events=[]), Mock(retry_at=0), self.alerts, commands)
        with patch.object(bot, "poll"):
            for now in (1000, 1002, 1010, 1029):
                monitor.tick(now)
            commands.poll.assert_called_once_with(1000)
            monitor.tick(1030)
        self.assertEqual(commands.poll.call_count, 2)

    def legacy_database(self, invalid=False):
        self.path = self.path.parent / "sqlite-legacy.json"
        path = self.path.with_suffix(".sqlite3")
        with sqlite3.connect(path) as legacy:
            legacy.executescript("""
                CREATE TABLE state (id INTEGER PRIMARY KEY, payload TEXT);
                CREATE TABLE schedule (id INTEGER PRIMARY KEY, next_digest REAL);
                CREATE TABLE events (id INTEGER PRIMARY KEY, event_key TEXT, message TEXT, created REAL, delivered REAL);
            """)
            legacy.execute("INSERT INTO state VALUES (1, ?)",
                           (json.dumps({"host:cpu": {"active": True, "sent": 1000}}),))
            legacy.execute("INSERT INTO schedule VALUES (1, 1500)")
            legacy.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", [
                (1, "old", "Delivered history", 900, 950),
                (2, None if invalid else "pending", "Pending history", 1000, None),
            ])
        return path

    def test_sqlite_import_preserves_history_queue_deadline_and_source(self):
        path = self.legacy_database()
        original = path.read_bytes()
        alerts = self.open_alerts(now=1100)
        self.assertTrue(alerts.state["host:cpu"]["active"])
        self.assertEqual([(event.message, event.delivered) for event in events(alerts)],
                         [("Delivered history", 950), ("Pending history", None)])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(alerts.flush(1499))
        self.assertTrue(alerts.flush(1500))
        self.assertIn("Pending history", self.messages[0])
        self.assertNotIn("Delivered history", self.messages[0])
        # Once committed, the target database is authoritative, even if the old backup changes.
        path.write_bytes(b"broken old backup")
        restarted = self.open_alerts(now=1510)
        self.assertEqual(len(events(restarted)), 2)
        self.assertFalse(restarted.flush(1800))

    def test_failed_sqlite_import_rolls_back_and_retries_without_duplicate_history(self):
        path = self.legacy_database(invalid=True)
        with self.assertRaises(IntegrityError):
            self.open_alerts()
        with sqlite3.connect(path) as legacy:
            legacy.execute("UPDATE events SET event_key = 'pending' WHERE id = 2")
        alerts = self.open_alerts()
        self.assertEqual(len(events(alerts)), 2)
        self.assertTrue(alerts.state["host:cpu"]["active"])
        alerts.flush(1500)
        self.assertEqual(len(self.messages), 1)

    def test_postgresql_lock_allows_only_one_digest_claim_per_window(self):
        if self.alerts.store.engine.dialect.name != "postgresql":
            self.skipTest("requires PostgreSQL row locks")
        self.alerts.notify("cpu", True, "CPU critical", 1000)
        other = self.open_alerts()
        barrier = Barrier(2)
        def flush(alerts):
            barrier.wait(timeout=5)
            return alerts.flush(1300)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(flush, alerts) for alerts in (self.alerts, other)]
            self.assertEqual(sorted(future.result(timeout=10) for future in futures), [False, True])
        self.assertEqual(len(self.messages), 1)

    def test_persistence_reconciles_an_already_committed_batch_without_duplicates(self):
        batch = [("cpu", "CPU critical", 1000)]
        state = {"cpu": {"active": True, "sent": 1000}}
        self.alerts.store.save(state, batch)
        # Reconcile the same batch if a caller did not receive its commit acknowledgement.
        self.alerts.store.save(state, batch)
        self.assertEqual(len(events(self.alerts)), 1)
        self.assertEqual(self.alerts.store.load(), state)


class DatabaseConfigurationTest(unittest.TestCase):
    def test_password_is_preserved_without_manual_url_escaping(self):
        secret = "test:@/%?# space пароль"
        with patch.dict(os.environ, {"PGHOST": "postgres", "PGUSER": "monitoring",
                                     "PGDATABASE": "monitoring", "PGPASSWORD": secret}):
            url = bot.notification_database_url()
        self.assertEqual(url.password, secret)
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertNotIn(secret, str(url))

    def test_runtime_requires_postgresql_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(bot.ConfigError):
                bot.notification_database_url()


if __name__ == "__main__":
    unittest.main()
