"""Operator-only topology and least-privilege service identities; no secret output."""
import re
import time

import httpx
import pika
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from workwise.messaging.broker import BrokerSettings, DLQ, EXCHANGES, QUEUE, connect, declare_topology


class Settings(BaseSettings):
    model_config=SettingsConfigDict(extra="ignore",hide_input_in_errors=True)
    rabbitmq_publisher_password: SecretStr
    rabbitmq_worker_password: SecretStr
    rabbitmq_replay_password: SecretStr


def exact(*names):
    return "^("+"|".join(re.escape(name) for name in names)+")$"


def configure(settings,admin):
    connection=connect(admin)
    try:
        declare_topology(connection.channel())
    finally:
        connection.close()
    roles=(
        ("publisher",settings.rabbitmq_publisher_password,exact(EXCHANGES[QUEUE]),"^$"),
        ("worker",settings.rabbitmq_worker_password,exact(*EXCHANGES.values()),exact(QUEUE)),
        ("replay",settings.rabbitmq_replay_password,exact(EXCHANGES[QUEUE]),exact(DLQ)),
    )
    with httpx.Client(base_url=f"http://{admin.rabbitmq_host}:15672",auth=(admin.rabbitmq_user,admin.rabbitmq_password.get_secret_value()),timeout=10,trust_env=False) as client:
        for role,password,write,read in roles:
            username="workwise_"+role
            client.put("/api/users/"+username,json={"password":password.get_secret_value(),"tags":""}).raise_for_status()
            client.put("/api/permissions/%2F/"+username,json={"configure":"^$","write":write,"read":read}).raise_for_status()


def main():
    settings=Settings()
    admin=BrokerSettings(rabbitmq_user="workwise")
    deadline=time.monotonic()+90
    while True:
        try:
            configure(settings,admin)
            break
        except (httpx.HTTPError,pika.exceptions.AMQPError,OSError):
            # Erlang ping can become healthy before AMQP/management listeners.
            if time.monotonic()>=deadline:
                raise SystemExit("Broker setup unavailable; inspect operator credentials and broker readiness.") from None
            time.sleep(2)
    print("Broker topology and separate publisher/worker/replay permissions configured.")


if __name__ == "__main__":
    main()
