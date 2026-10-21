"""Durable queues, mandatory publishing and publisher confirms."""
import json
import logging

import pika
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

QUEUE = "workwise.notifications.v1"
DLQ = "workwise.notifications.dead.v1"
MAX_RETRIES = 3


class BrokerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = Field(default=5672, ge=1, le=65535)
    rabbitmq_user: str = "workwise"
    rabbitmq_password: SecretStr


def connect(settings: BrokerSettings):
    # Library exception strings can include connection details. Our logs only emit types.
    logging.getLogger("pika").setLevel(logging.CRITICAL)
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=settings.rabbitmq_host, port=settings.rabbitmq_port,
        credentials=pika.PlainCredentials(settings.rabbitmq_user, settings.rabbitmq_password.get_secret_value()),
        heartbeat=30, blocked_connection_timeout=10, socket_timeout=5, stack_timeout=15,
        connection_attempts=1,
    ))


def prepare(connection):
    channel = connection.channel()
    for queue in (QUEUE, DLQ):
        channel.queue_declare(queue=queue, durable=True, arguments={"x-queue-type": "classic"})
    channel.basic_qos(prefetch_count=1)
    channel.confirm_delivery()
    return channel


def publish(channel, queue, body, *, message_id, retries=0, reason=None):
    headers = {"retries": retries}
    if reason:
        headers["failure_type"] = reason
    channel.basic_publish(
        exchange="", routing_key=queue, body=body,
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2,
                                        message_id=message_id, headers=headers),
        mandatory=True,
    )


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)
