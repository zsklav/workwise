"""python -m workwise.messaging.publisher: lock one outbox row until confirmed and committed."""
from datetime import datetime, timezone
import json
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import pika

from workwise.db.session import engine
from workwise.db.models import OutboxEvent
from workwise.messaging.broker import BrokerSettings, QUEUE, connect, log, prepare, publish
from workwise.messaging.health import beat
from workwise.core.tracing import SpanKind, configure_tracing, span


def publish_one(session: Session, channel) -> bool:
    with session.begin():
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
                               .order_by(OutboxEvent.created_at, OutboxEvent.id)
                               .with_for_update(skip_locked=True).limit(1))
        if event is None:
            return False
        with span("outbox.publish", SpanKind.PRODUCER, event.trace_context or {}) as current:
            current.set_attribute("messaging.message.id", str(event.id))
            publish(channel, QUEUE, json.dumps(event.payload).encode(), message_id=str(event.id))
        event.published_at = datetime.now(timezone.utc)
    log("outbox_published", event_id=str(event.id))
    return True


def main():
    configure_tracing("workwise-publisher")
    settings = BrokerSettings()
    while True:
        connection = None
        try:
            connection = connect(settings)
            channel = prepare(connection)
            log("publisher_connected")
            while connection.is_open:
                with Session(engine, expire_on_commit=False) as session:
                    worked = publish_one(session, channel)
                beat()
                connection.sleep(0 if worked else 1)
        except (pika.exceptions.AMQPError, SQLAlchemyError, OSError) as error:
            log("publisher_retry", error_type=type(error).__name__)
        finally:
            if connection is not None and connection.is_open:
                connection.close()
        time.sleep(2)


if __name__ == "__main__":
    main()
