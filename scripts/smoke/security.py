"""Verify auth revocation/throttles and real AMQP permission denials on a disposable stack."""
import json
import secrets
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

COMPOSE=["docker","compose","--env-file",".env.docker"]


def request(path,payload=None,*,token=None,expected=200,form=False,method="POST"):
    headers={"Authorization":"Bearer "+token} if token else {}
    body=None
    if payload is not None:
        body=(urlencode(payload) if form else json.dumps(payload)).encode()
        headers["Content-Type"]="application/x-www-form-urlencoded" if form else "application/json"
    try:
        response=urlopen(Request("http://127.0.0.1:8001"+path,data=body,headers=headers,method=method),timeout=10)
    except HTTPError as error:
        response=error
    with response:
        assert response.status==expected,f"{path}: expected {expected}, received {response.status}"
        data=response.read()
        return json.loads(data) if data else None,response.headers


def permission_checks(service,code):
    prefix=["run","--rm","--no-deps","--entrypoint","python","replay","-c"] if service=="replay" else ["exec","-T",service,"python","-c"]
    subprocess.run([*COMPOSE,*prefix,code],check=True)


def main():
    username,password="security_"+uuid4().hex,secrets.token_urlsafe(32)
    request("/auth/signup",{"username":username,"password":password},expected=201)
    def login():
        return request("/auth/login",{"grant_type":"password","username":username,"password":password},form=True)[0]
    pair=login()
    newer,headers=request("/auth/refresh",{"refresh_token":pair["refresh_token"]})
    assert newer["refresh_token"]!=pair["refresh_token"] and headers["Cache-Control"]=="no-store"
    request("/auth/logout",token=newer["access_token"],expected=204)
    request("/auth/me",method="GET",token=pair["access_token"],expected=401)
    request("/auth/refresh",{"refresh_token":newer["refresh_token"]},expected=401)
    pair=login()
    successor,_=request("/auth/refresh",{"refresh_token":pair["refresh_token"]})
    request("/auth/refresh",{"refresh_token":pair["refresh_token"]},expected=401)
    request("/auth/me",method="GET",token=successor["access_token"],expected=401)
    request("/auth/refresh",{"refresh_token":successor["refresh_token"]},expected=401)
    assert login()["access_token"]
    print("Verified: HTTP refresh rotation, account-wide logout, reuse containment and login recovery",flush=True)
    unknown="limit_"+uuid4().hex
    payload={"grant_type":"password","username":unknown,"password":password}
    for _ in range(10): request("/auth/login",payload,form=True,expected=401)
    _,headers=request("/auth/login",payload,form=True,expected=429)
    assert 1<=int(headers["Retry-After"])<=60
    print("Verified: failed HTTP logins consume the shared account throttle and return Retry-After",flush=True)
    helper='''
from workwise.messaging.broker import *
import httpx
def denied(operation):
    connection=connect(BrokerSettings())
    try:
        channel=prepare(connection)
        try:
            operation(channel)
        except pika.exceptions.ChannelClosedByBroker as error:
            assert error.reply_code==403
        else:
            raise AssertionError("Operation unexpectedly permitted")
    finally:
        if connection.is_open: connection.close()
s=BrokerSettings()
assert httpx.get("http://rabbitmq:15672/api/overview",auth=(s.rabbitmq_user,s.rabbitmq_password.get_secret_value()),timeout=5,trust_env=False).status_code in (401,403)
denied(lambda ch: ch.queue_declare(queue="workwise.security.forbidden",durable=True))
'''
    permission_checks("publisher",helper+'''
denied(lambda ch: ch.basic_get(QUEUE,auto_ack=False))
denied(lambda ch: publish(ch,DLQ,b"forbidden",message_id="forbidden"))
denied(lambda ch: ch.basic_publish(exchange="",routing_key=QUEUE,body=b"forbidden",mandatory=True))
print("Verified: publisher cannot consume, configure topology, publish dead letters or use the default exchange")
''')
    permission_checks("notification-worker",helper+'''
denied(lambda ch: ch.basic_get(DLQ,auto_ack=False))
print("Verified: worker cannot consume dead letters, configure topology or use management API")
''')
    permission_checks("replay",helper+'''
denied(lambda ch: ch.basic_get(QUEUE,auto_ack=False))
denied(lambda ch: publish(ch,DLQ,b"forbidden",message_id="forbidden"))
print("Verified: replay cannot consume the main queue, publish dead letters or configure topology")
''')
    subprocess.run([*COMPOSE,"exec","-T","api","python","-c",
        "from workwise.db.session import engine; from sqlalchemy import text; c=engine.connect(); "
        "assert c.scalar(text(\"SELECT has_column_privilege(current_user,'users','token_version','UPDATE')\")); "
        "assert not c.scalar(text(\"SELECT has_column_privilege(current_user,'users','role','UPDATE')\")); "
        "assert not c.scalar(text(\"SELECT has_table_privilege(current_user,'auth_refresh_tokens','DELETE')\")); c.close()"],check=True)
    print("Security hardening smoke test passed.",flush=True)


if __name__ == "__main__":
    main()
