"""Explicit operator replay of one valid DLQ event; invalid envelopes stay quarantined."""
import argparse
from uuid import UUID

from pydantic import ValidationError

from messaging.broker import BrokerSettings, DLQ, QUEUE, connect, log, prepare, publish
from messaging.events import JobCreated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", type=UUID, required=True)
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 1001), metavar="1..1000")
    args = parser.parse_args()
    connection = connect(BrokerSettings())
    try:
        channel = prepare(connection)
        count = channel.queue_declare(DLQ, passive=True).method.message_count
        for _ in range(min(count, args.limit)):
            method, properties, body = channel.basic_get(DLQ, auto_ack=False)
            if method is None:
                break
            try:
                event = JobCreated.model_validate_json(body)
                valid = UUID(properties.message_id) == event.event_id
            except (ValidationError, ValueError, TypeError, AttributeError):
                valid = False
            if valid and event.event_id == args.event_id:
                publish(channel, QUEUE, body, message_id=str(event.event_id), retries=0)
                channel.basic_ack(method.delivery_tag)
                log("notification_replayed", event_id=str(event.event_id))
                return
            # Keep other deliveries unacked until close, so they cannot hide later matches.
        raise SystemExit("Requested valid event not found within scan limit; other messages were retained.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
