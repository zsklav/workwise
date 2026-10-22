"""Run a bounded benchmark on a dedicated native test DB or an acknowledged disposable Docker stack."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time

import httpx
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL

from scripts.performance.fixture import cleanup,seed
from scripts.performance.http_load import run_suite
from workwise.core.paths import ROOT_DIR


def write_report(directory,report):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/"http.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    lines=["# WorkWise HTTP measurement","",f"Recorded: {report['created_at']}","",
           "Closed-loop bounded clients; warmed connections; no think time. These measurements are not a capacity guarantee.","",
           "| Scenario | Clients | Repeat | Requests/s | p50 ms | p95 ms | p99 ms | Failures |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in report["rounds"]:
        lines.append(f"| {r['scenario']} | {r['clients']} | {r['repeat']} | {r['requests_per_second']:.2f} | {r['latency_ms']['p50']:.2f} | {r['latency_ms']['p95']:.2f} | {r['latency_ms']['p99']:.2f} | {r['failures']} |")
    (directory/"http.md").write_text("\n".join(lines)+"\n",encoding="utf-8")


def observe_delivery(base, token, expected, *, timeout=30):
    started=time.monotonic()
    received=0
    with httpx.Client(base_url=base,headers={"Authorization":"Bearer "+token},timeout=5,trust_env=False) as client:
        while True:
            try:
                events=[]
                for offset in range(0,expected+100,100):
                    result=client.get("/notifications",params={"limit":100,"offset":offset})
                    result.raise_for_status()
                    page=result.json()
                    events.extend(item["event_id"] for item in page)
                    if len(page)<100:
                        break
                received=len(set(events))
                if received == expected and len(events) == expected:
                    return {"mode":"docker_inbox","passed":True,"received":received,
                            "drain_seconds_after_http":time.monotonic()-started}
            except (httpx.HTTPError,ValueError,KeyError,TypeError):
                pass
            if time.monotonic()-started >= timeout:
                return {"mode":"docker_inbox","passed":False,"received":received,
                        "drain_seconds_after_http":time.monotonic()-started}
            time.sleep(.5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("native","docker"),default="native")
    parser.add_argument("--allow-disposable-stack",action="store_true")
    parser.add_argument("--rows",type=int,default=20000)
    parser.add_argument("--companies",type=int,default=100)
    parser.add_argument("--requests",type=int,default=100)
    parser.add_argument("--clients",type=int,nargs="+",default=[1,8])
    parser.add_argument("--repeats",type=int,default=2)
    parser.add_argument("--warmup",type=int,default=10)
    parser.add_argument("--output",type=Path,default=ROOT_DIR/"performance-results")
    args=parser.parse_args()
    if args.mode == "docker" and not args.allow_disposable_stack:
        parser.error("Docker measurement requires --allow-disposable-stack")
    if (not 1000 <= args.rows <= 100000 or not 2 <= args.companies <= min(1000,args.rows//20)
        or not 1 <= args.requests <= 10000 or not 1 <= args.repeats <= 5 or not 0 <= args.warmup <= 100
        or not 1 <= len(args.clients) <= 4 or len(set(args.clients)) != len(args.clients)
        or any(not 1 <= c <= 32 for c in args.clients)
        or (args.requests+args.warmup)*len(args.clients)*args.repeats*6 > 50000):
        parser.error("Fixture or load settings exceed documented bounds")
    if args.mode == "native":
        from workwise.db.session import database_url
        url=database_url.set(database="workwise_test")
    else:
        from workwise.core.config import MigrationSettings
        s=MigrationSettings()
        url=URL.create("postgresql+psycopg",host=s.db_host,port=s.db_port,database=s.db_name,
                       username=s.migration_db_user,password=s.migration_db_password.get_secret_value())
    database=create_engine(url,connect_args={"connect_timeout":5})
    fixture,process,log=None,None,None
    args.output.mkdir(parents=True,exist_ok=True)
    try:
        with database.begin() as connection:
            actual=connection.scalar(text("SELECT current_database()"))
            if args.mode == "native" and actual != "workwise_test":
                raise RuntimeError("Native benchmark is restricted to workwise_test")
            fixture=seed(connection,rows=args.rows,companies=args.companies)
            revision=connection.scalar(text("SELECT version_num FROM alembic_version"))
            postgres=connection.scalar(text("SELECT version()"))
        if args.mode == "native":
            with socket.socket() as probe:
                probe.bind(("127.0.0.1",0))
                port=probe.getsockname()[1]
            base=f"http://127.0.0.1:{port}"
            environment=os.environ.copy()
            environment["DB_NAME"]="workwise_test"
            for key in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT","NOTIFICATION_SERVICE_URL"):
                environment.pop(key,None)
            log=(args.output/"native-server.log").open("w",encoding="utf-8")
            process=subprocess.Popen([sys.executable,"-m","uvicorn","workwise.api.main:app","--host","127.0.0.1","--port",str(port),"--no-access-log"],
                                     cwd=ROOT_DIR,env=environment,stdout=log,stderr=log,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        else:
            base="http://api:8000"
        with httpx.Client(base_url=base,timeout=5,trust_env=False) as client:
            deadline=time.monotonic()+30
            while True:
                try:
                    if client.get("/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic()>deadline or process is not None and process.poll() is not None:
                    raise RuntimeError("Benchmark API did not become ready; inspect the local server log")
                time.sleep(.25)
            response=client.post("/auth/login",data={"grant_type":"password","username":fixture["username"],"password":fixture["password"]})
            response.raise_for_status()
            token=response.json()["access_token"]
        print(f"Measuring {args.mode} API: {args.rows} fixture jobs, {args.companies} companies; setup/login excluded.",flush=True)
        rounds=asyncio.run(run_suite(base,fixture,token,requests=args.requests,clients=args.clients,repeats=args.repeats,warmup=args.warmup))
        with database.connect() as connection:
            created=connection.scalar(text("SELECT count(*) FROM outbox_events WHERE payload->>'recipient_id'=:recipient"),{"recipient":str(fixture["user_id"])})
        expected=(args.requests+args.warmup)*len(args.clients)*args.repeats
        delivery=observe_delivery(base,token,expected) if args.mode == "docker" else {"mode":"not_measured_native","passed":None}
        report={"created_at":datetime.now(timezone.utc).isoformat(),"mode":args.mode,
                "revision":os.getenv("PERF_REVISION","local-working-tree"),"schema_revision":revision,
                "client":{"python":platform.python_version(),"platform":platform.platform(),"cpu_count":os.cpu_count()},
                "postgresql":postgres,"fixture":{"jobs":args.rows,"companies":args.companies,"description_bytes":120},
                "configuration":{"requests_per_round":args.requests,"clients":args.clients,"repeats":args.repeats,"warmup_per_round":args.warmup,
                                 "api_workers":1,"tracing":"disabled in native; Compose setting in Docker","request_timeout_seconds":10},
                "rounds":rounds,"created_outbox_events":created,"expected_create_calls_including_warmup":expected,
                "notification_delivery":delivery}
        write_report(args.output,report)
        if any(row["failures"] for row in rounds) or created != expected or delivery["passed"] is False:
            raise RuntimeError("Benchmark recorded failures or unexpected outbox counts; inspect the report")
        print("HTTP benchmark passed: zero response/transport failures and each create produced an outbox event.",flush=True)
        if args.mode == "docker":
            print(f"Verified: all {expected} create events produced distinct inbox items; final drain {delivery['drain_seconds_after_http']:.3f}s.",flush=True)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log is not None:
            log.close()
        try:
            if fixture is not None:
                with database.begin() as connection:
                    cleanup(connection,fixture)
                print("Removed only this benchmark's jobs, companies, outbox events and account.",flush=True)
        finally:
            database.dispose()


if __name__ == "__main__":
    main()
