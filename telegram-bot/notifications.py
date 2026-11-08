"""ORM persistence for incident history and a durable Telegram digest."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3

from sqlalchemy import BigInteger, CheckConstraint, Float, Index, Integer, JSON, String, Text, create_engine, delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

LOG = logging.getLogger("telegram-bot")


class Base(DeclarativeBase):
    pass


class AlertState(Base):
    __tablename__ = "notification_state"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class DigestSchedule(Base):
    __tablename__ = "notification_schedule"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    next_digest: Mapped[float] = mapped_column(Float)


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    __table_args__ = (Index("pending_notification_events", "delivered", "id"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    identity: Mapped[str] = mapped_column(String(64), unique=True)
    event_key: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    created: Mapped[float] = mapped_column(Float)
    delivered: Mapped[float | None] = mapped_column(Float)


@dataclass
class EventGroup:
    initial: str
    message: str
    first: float
    last: float
    total: int = 1


def telegram_length(text):
    return len(text.encode("utf-16-le")) // 2


def truncate(text, limit):
    return text.encode("utf-16-le")[:limit * 2].decode("utf-16-le", errors="ignore")


def event_identity(key, message, now):
    return hashlib.sha256(json.dumps([key, message, now], ensure_ascii=False).encode()).hexdigest()


class NotificationStore:
    def __init__(self, database_url, now, interval=300, legacy_path=None):
        url = make_url(database_url) if isinstance(database_url, str) else database_url
        connect_args = {"timeout": 5}
        if url.get_backend_name() == "postgresql":
            options = url.query.get("options", "") + " -c statement_timeout=10000 -c lock_timeout=5000"
            connect_args = {"connect_timeout": 5, "options": options}
        self.engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True, hide_parameters=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.interval = interval
        try:
            Base.metadata.create_all(self.engine)
            with self.sessions.begin() as session:
                if legacy_path and session.get(AlertState, 1) is None:
                    self._import_sqlite(session, legacy_path)
                if session.get(DigestSchedule, 1) is None:
                    session.add(DigestSchedule(id=1, next_digest=now + interval))
        except Exception:
            self.close()
            raise

    def close(self):
        self.engine.dispose()

    @staticmethod
    def _import_sqlite(session, path):
        path = Path(path)
        if not path.exists():
            return
        # The old database is read-only; import history, state and schedule in one PG transaction.
        legacy = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        legacy.row_factory = sqlite3.Row
        try:
            state = legacy.execute("SELECT payload FROM state WHERE id = 1").fetchone()
            if state is None:
                raise ValueError("legacy SQLite has no alert state")
            session.add(AlertState(id=1, payload=json.loads(state[0])))
            schedule = legacy.execute("SELECT next_digest FROM schedule WHERE id = 1").fetchone()
            if schedule:
                session.merge(DigestSchedule(id=1, next_digest=schedule[0]))
            cursor = legacy.execute("SELECT id, event_key, message, created, delivered FROM events ORDER BY id")
            while rows := cursor.fetchmany(500):
                for row in rows:
                    session.add(NotificationEvent(identity=event_identity("legacy:" + str(row["id"]),
                                                                         row["message"], row["created"]),
                                                  event_key=row["event_key"], message=row["message"],
                                                  created=row["created"], delivered=row["delivered"]))
                session.flush()
        finally:
            legacy.close()
        LOG.info("Imported legacy SQLite notification history")

    def load(self):
        with self.sessions() as session:
            state = session.get(AlertState, 1)
            return state.payload if state else None

    def save(self, state, events=()):
        with self.sessions.begin() as session:
            stored = session.get(AlertState, 1)
            if stored is None:
                session.add(AlertState(id=1, payload=state))
            elif stored.payload != state:
                stored.payload = state
            # Stable identities make an uncertain commit safe to reconcile on the next save.
            pending = {event_identity(key, message, now): (key, message, now) for key, message, now in events}
            if pending:
                existing = set(session.scalars(select(NotificationEvent.identity)
                                               .where(NotificationEvent.identity.in_(pending))))
                session.add_all(NotificationEvent(identity=identity, event_key=key, message=message, created=now)
                                for identity, (key, message, now) in pending.items() if identity not in existing)

    def flush(self, send, now):
        with self.sessions.begin() as session:
            schedule = session.get(DigestSchedule, 1, with_for_update=True)
            if schedule is None:
                raise RuntimeError("Notification digest schedule is missing")
            if now < schedule.next_digest:
                return False
            # Reserve the next attempt before I/O: timeouts/restarts cannot cause a burst.
            schedule.next_digest = now + self.interval
            session.execute(delete(NotificationEvent).where(NotificationEvent.delivered < now - 30 * 86400))
        groups = {}
        maximum = None
        with self.sessions() as session:
            events = session.scalars(select(NotificationEvent).where(NotificationEvent.delivered.is_(None))
                                     .order_by(NotificationEvent.id)).yield_per(500)
            for event in events:
                maximum = event.id
                if event.event_key not in groups:
                    groups[event.event_key] = EventGroup(event.message, event.message, event.created, event.created)
                else:
                    group = groups[event.event_key]
                    group.message = event.message
                    group.first = min(group.first, event.created)
                    group.last = max(group.last, event.created)
                    group.total += 1
        if maximum is None:
            return False
        message, total, omitted = digest_message(groups.values())
        # Sessions/transactions are closed before contacting Telegram.
        LOG.info("Telegram digest sending events=%s groups=%s shortened=%s", total, len(groups), omitted)
        send(message)
        with self.sessions.begin() as session:
            session.execute(update(NotificationEvent).where(NotificationEvent.delivered.is_(None),
                                                            NotificationEvent.id <= maximum).values(delivered=now))
        LOG.info("Telegram digest delivered events=%s groups=%s", total, len(groups))
        return True


def digest_message(groups):
    groups = list(groups)
    total = sum(group.total for group in groups)
    message = f"📋 Зведення моніторингу\nПодій: {total}; груп: {len(groups)}. Час: UTC."
    omitted = 0
    for group in groups:
        first = datetime.fromtimestamp(group.first, timezone.utc).strftime("%d.%m %H:%M")
        last = datetime.fromtimestamp(group.last, timezone.utc).strftime("%d.%m %H:%M")
        block = f"\n\n{group.message}\n{first}–{last}; подій: {group.total}."
        if group.initial != group.message:
            block += "\nПочаток: " + truncate(group.initial.replace("\n", " | "), 220)
        if telegram_length(message + block) <= 3750:
            message += block
        elif len(groups) == 1:
            message += truncate(block, 3750 - telegram_length(message)) + "…"
            omitted += 1
        else:
            omitted += 1
    if omitted:
        message += f"\n\nСкорочено груп: {omitted}. Повна історія всіх подій збережена у БД."
    return message, total, omitted
