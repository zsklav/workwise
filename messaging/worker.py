"""Manual ACK only after committing a receipt or confirming a retry/DLQ copy."""
import time
from uuid import UUID

import pika
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from messaging.broker import (BrokerSettings, DLQ, MAX_RETRIES, QUEUE, connect, log, prepare, publish)
from messaging.events import JobCreated
from messaging.notification_store import EventConflict, NotificationSettings, record_notification, store_engine


def handle(connection, channel, method, properties, body, engine):
    try:
        event = JobCreated.model_validate_json(body)
        if UUID(properties.message_id) != event.event_id:
            raise ValueError("Message ID does not match envelope")
    except (ValidationError, ValueError, TypeError, AttributeError):
        publish(channel, DLQ, body, message_id=properties.message_id, reason="invalid_event")
        channel.basic_ack(method.delivery_tag)
        log("notification_dead_lettered", reason="invalid_event")
        return

    retries = (properties.headers or {}).get("retries", 0)
    if type(retries) is not int or not 0 <= retries <= MAX_RETRIES:
        retries = MAX_RETRIES
    try:
        created = record_notification(engine, event)
    except (SQLAlchemyError, EventConflict) as error:
        exhausted = retries >= MAX_RETRIES or isinstance(error, EventConflict)
        if not exhausted:
            connection.sleep(min(2 ** retries, 4))
        publish(channel, DLQ if exhausted else QUEUE, body, message_id=str(event.event_id),
                retries=retries if exhausted else retries + 1, reason=type(error).__name__)
        channel.basic_ack(method.delivery_tag)
        log("notification_dead_lettered" if exhausted else "notification_retry",
            event_id=str(event.event_id), retries=retries, error_type=type(error).__name__)
        return
    channel.basic_ack(method.delivery_tag)
    log("notification_recorded" if created else "notification_duplicate", event_id=str(event.event_id))


def main():
    settings = BrokerSettings()
    engine = store_engine(NotificationSettings())
    while True:
        connection = None
        try:
            connection = connect(settings)
            channel = prepare(connection)
            channel.basic_consume(QUEUE, lambda ch, method, props, body: handle(connection, ch, method, props, body, engine),
                                  auto_ack=False)
            log("worker_connected")
            channel.start_consuming()
        except (pika.exceptions.AMQPError, OSError) as error:
            log("worker_reconnect", error_type=type(error).__name__)
        finally:
            if connection is not None and connection.is_open:
                connection.close()
        time.sleep(2)


if __name__ == "__main__":
    main()
