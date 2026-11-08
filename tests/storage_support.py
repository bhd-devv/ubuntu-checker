"""Run storage behavior tests on SQLite or an isolated real PostgreSQL schema."""

import hashlib
import os

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

import bot
from notifications import NotificationEvent


def open_alerts(test, path, send, now=1000):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url:
        base_url = make_url(database_url)
        schema = "test_" + hashlib.sha256(str(path).encode()).hexdigest()[:24]
        schemas = getattr(test, "storage_schemas", set())
        if schema not in schemas:
            engine = create_engine(base_url, hide_parameters=True)
            with engine.begin() as connection:
                connection.execute(CreateSchema(schema))
            def cleanup():
                try:
                    with engine.begin() as connection:
                        connection.execute(DropSchema(schema, cascade=True))
                finally:
                    engine.dispose()
            test.addCleanup(cleanup)
            schemas.add(schema)
            test.storage_schemas = schemas
        options = base_url.query.get("options", "") + " -csearch_path=" + schema
        database_url = base_url.update_query_dict({"options": options})
    alerts = bot.Alerts(path, send, now=now, database_url=database_url)
    test.addCleanup(alerts.store.close)
    return alerts


def events(alerts):
    with alerts.store.sessions() as session:
        return list(session.scalars(select(NotificationEvent).order_by(NotificationEvent.id)))
