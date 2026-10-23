"""Logical backups and restore verification in newly created, isolated databases."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from workwise.core.paths import ROOT_DIR


DATABASES = {
    "jobs": {"service": "db", "database": "workwise", "owner": "workwise_migrator",
             "tables": {"alembic_version": "version_num", "users": "id", "companies": "id",
                        "jobs": "id", "outbox_events": "id", "auth_refresh_tokens": "id", "auth_rate_limits": "bucket_hash"}},
    "notifications": {"service": "notification-db", "database": "notifications", "owner": "postgres",
                      "tables": {"notification_schema_version": "version", "notification_receipts": "event_id", "notifications": "event_id"}},
}
COMPOSE = ["docker", "compose", "--env-file", str(ROOT_DIR / ".env.docker")]
TARGET_PATTERN = re.compile(r"ww_restore_[0-9a-f]{32}_(jobs|notifications)\Z")

# Catalog data deliberately excludes database names, OIDs and row values.
SCHEMA_SQL = """SELECT jsonb_build_object(
 'columns', (SELECT jsonb_agg(to_jsonb(c) ORDER BY c.table_name,c.ordinal_position) FROM
   (SELECT table_name,column_name,ordinal_position,data_type,udt_name,is_nullable,column_default,is_identity,identity_generation
    FROM information_schema.columns WHERE table_schema='public') c),
 'constraints', (SELECT jsonb_agg(jsonb_build_array(r.relname,c.conname,pg_get_constraintdef(c.oid)) ORDER BY r.relname,c.conname)
    FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public'),
 'indexes', (SELECT jsonb_agg(jsonb_build_array(tablename,indexname,indexdef) ORDER BY tablename,indexname)
    FROM pg_indexes WHERE schemaname='public'),
 'owners', (SELECT jsonb_agg(jsonb_build_array(c.relname,pg_get_userbyid(c.relowner)) ORDER BY c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','S')),
 'table_grants', (SELECT jsonb_agg(to_jsonb(g) ORDER BY g.table_name,g.grantee,g.privilege_type) FROM
    (SELECT grantor,grantee,table_name,privilege_type,is_grantable FROM information_schema.table_privileges WHERE table_schema='public') g),
 'column_grants', (SELECT jsonb_agg(to_jsonb(g) ORDER BY g.table_name,g.column_name,g.grantee,g.privilege_type) FROM
    (SELECT grantor,grantee,table_name,column_name,privilege_type,is_grantable FROM information_schema.column_privileges WHERE table_schema='public') g)
)"""


def private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Transport:
    def __init__(self, mode="docker", pg_bin=None):
        self.mode, self.pg_bin = mode, pg_bin
        self.env = os.environ.copy()
        # Do not let ambient libpq variables redirect an operator command.
        for key in list(self.env):
            if key.startswith("PG"):
                self.env.pop(key)
        self.env["PGCONNECT_TIMEOUT"] = "5"
        self.env["PGCLIENTENCODING"] = "UTF8"
        self.env["PGOPTIONS"] = "-c statement_timeout=120000 -c lock_timeout=5000"
        if mode == "native":
            from workwise.core.config import MigrationSettings
            self.settings = MigrationSettings()
            self.env["PGPASSWORD"] = self.settings.migration_db_password.get_secret_value()

    def command(self, kind, tool, database=None):
        spec = DATABASES[kind]
        if self.mode == "docker":
            return [*COMPOSE, "exec", "-T", "-e", "PGCONNECT_TIMEOUT=5", "-e",
                    "PGCLIENTENCODING=UTF8", "-e", "PGOPTIONS=-c statement_timeout=120000 -c lock_timeout=5000", spec["service"],
                    tool, "--username=postgres", "--no-password", "--dbname=" + (database or spec["database"])]
        if kind != "jobs":
            raise ValueError("Native mode backs up the configured jobs database only; use Docker for both databases.")
        executable = str(Path(self.pg_bin) / tool) if self.pg_bin else shutil.which(tool)
        if not executable and os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "PostgreSQL/17/bin" / (tool + ".exe")
            executable = str(candidate) if candidate.is_file() else None
        if not executable:
            raise ValueError("PostgreSQL 17 client tools required; pass --pg-bin pointing to their bin directory.")
        s = self.settings
        return [executable, "--host=" + s.db_host, "--port=" + str(s.db_port),
                "--username=" + s.migration_db_user, "--no-password", "--dbname=" + (database or s.db_name)]

    def run(self, kind, tool, *args, database=None, **kwargs):
        # Capture stderr: database diagnostics can contain personal data.
        result = subprocess.run([*self.command(kind, tool, database), *args], env=self.env,
                                stderr=subprocess.PIPE, timeout=180, **kwargs)
        if result.returncode:
            raise RuntimeError(f"{tool} failed for {kind}; details suppressed to protect backup data.")
        return result

    def sql(self, kind, statement, database=None):
        return self.run(kind, "psql", "-XqAt", "--set=ON_ERROR_STOP=1", "--command=" + statement,
                        database=database, stdout=subprocess.PIPE).stdout.decode().strip()


class Snapshot:
    """Keep the exported snapshot alive while pg_dump and fingerprints use it."""
    def __init__(self, transport, kind, database=None):
        self.transport, self.kind, self.database = transport, kind, database

    def __enter__(self):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen([*self.transport.command(self.kind, "psql", self.database),
                                         "-XqAt", "--set=ON_ERROR_STOP=1"],
                                        env=self.transport.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors)
        try:
            self.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TIME ZONE 'UTC'")
            self.identity = self.query("SELECT pg_export_snapshot()")[0].decode().strip()
            if not re.fullmatch(r"[0-9A-Fa-f-]+", self.identity):
                raise RuntimeError("Unexpected snapshot identifier")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def lines(self, statement):
        marker = ("ww_end_" + uuid4().hex).encode()
        self.process.stdin.write((statement + ";\n\\echo " + marker.decode() + "\n").encode())
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Snapshot query failed; database details suppressed.")
            # Normalize psql's Windows stdout line endings before hashing.
            line = line.rstrip(b"\r\n") + b"\n"
            if line.rstrip(b"\n") == marker:
                break
            yield line

    def query(self, statement):
        return list(self.lines(statement))

    def inventory(self):
        expected = DATABASES[self.kind]["tables"]
        names = [r.decode().strip() for r in self.query("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
        if sorted(names) != sorted(expected):
            raise RuntimeError("Public table inventory changed; review backup coverage before proceeding.")
        data = {}
        for table, key in expected.items():
            digest, count = hashlib.sha256(), 0
            for row in self.lines(f'COPY (SELECT to_jsonb(t)::text FROM public."{table}" t ORDER BY "{key}") TO STDOUT'):
                digest.update(row)
                count += 1
            data[table] = {"rows": count, "sha256": digest.hexdigest()}
        version_query = "SELECT version_num FROM alembic_version" if self.kind == "jobs" else "SELECT version FROM notification_schema_version"
        return {"tables": data, "schema": json.loads(self.query(SCHEMA_SQL)[0]),
                "version": self.query(version_query)[0].decode().strip()}

    def __exit__(self, *unused):
        if self.process.poll() is None:
            # Closing the connection rolls back the read-only transaction.
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.stdout.close()
        self.errors.close()


def backup(transport, destination, kinds):
    destination = Path(destination)
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "mode": transport.mode, "databases": {}}
    for kind in kinds:
        started = time.monotonic()
        archive = destination / (kind + ".dump")
        with Snapshot(transport, kind) as snapshot:
            metadata = snapshot.inventory()
            with private_file(archive) as output:
                transport.run(kind, "pg_dump", "--format=custom", "--snapshot=" + snapshot.identity,
                              "--lock-wait-timeout=5s", stdout=output)
        manifest["databases"][kind] = {**metadata, "archive": archive.name,
            "sha256": file_hash(archive), "bytes": archive.stat().st_size,
            "backup_seconds": round(time.monotonic() - started, 3)}
    # Written last: an interrupted backup never has a completed manifest.
    with private_file(destination / "manifest.json") as output:
        output.write((json.dumps(manifest, indent=2) + "\n").encode())
    return manifest


def load_manifest(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not manifest.get("databases") or set(manifest["databases"]) - DATABASES.keys():
        raise ValueError("Unsupported backup manifest")
    for kind, item in manifest["databases"].items():
        if item["archive"] != kind + ".dump":
            raise ValueError("Unexpected archive path")
        archive = directory / item["archive"]
        if archive.is_symlink() or archive.stat().st_size != item["bytes"] or file_hash(archive) != item["sha256"]:
            raise ValueError("Backup checksum/size verification failed; no database was created.")
    return manifest


def validate_target(name, kind):
    if not TARGET_PATTERN.fullmatch(name) or not name.endswith("_" + kind):
        raise ValueError("Restore targets must be fresh, generated drill database names.")
    return name


@contextmanager
def fresh_database(transport, kind):
    name = validate_target("ww_restore_" + uuid4().hex + "_" + kind, kind)
    owner = DATABASES[kind]["owner"]
    # Never accept an operator-supplied target, --clean, --create, or FORCE drop.
    transport.sql(kind, f'CREATE DATABASE "{name}" OWNER "{owner}" TEMPLATE template0', database="postgres")
    try:
        yield name
    finally:
        transport.sql(kind, f'DROP DATABASE "{validate_target(name, kind)}"', database="postgres")


def drill(transport, directory):
    if transport.mode != "docker":
        raise ValueError("Restore drills use Docker's isolated operator connections.")
    manifest = load_manifest(directory)  # Verify every archive before creating any database.
    results = {}
    for kind, item in manifest["databases"].items():
        started = time.monotonic()
        with fresh_database(transport, kind) as target:
            with (Path(directory) / item["archive"]).open("rb") as archive:
                transport.run(kind, "pg_restore", "--single-transaction", "--exit-on-error", "--no-owner",
                              "--role=" + DATABASES[kind]["owner"], database=target, stdin=archive, stdout=subprocess.PIPE)
            with Snapshot(transport, kind, target) as restored:
                actual = restored.inventory()
            if actual != {key: item[key] for key in ("tables", "schema", "version")}:
                raise RuntimeError(f"Restored {kind} data/schema/permissions differ from the captured snapshot.")
            verify_writes(transport, kind, target)
            results[kind] = {"verified": True, "schema_version": item["version"],
                             "rows": {t: v["rows"] for t, v in item["tables"].items()},
                             "restore_verify_seconds": round(time.monotonic() - started, 3)}
    return {"format": 1, "verified_at": datetime.now(timezone.utc).isoformat(), "databases": results}


def verify_writes(transport, kind, target):
    validate_target(target, kind)
    if kind == "jobs":
        statement = """BEGIN; SET LOCAL ROLE workwise_app;
        INSERT INTO users(username,password_hash) VALUES ('drill_'||substr(md5(random()::text),1,24),'restore-write-probe');
        INSERT INTO companies(name) VALUES ('Restore probe '||gen_random_uuid());
        INSERT INTO jobs(title,company_id,location) SELECT 'Restored sequence probe',max(id),'Remote' FROM companies;
        DO $$BEGIN
          IF has_column_privilege(current_user,'users','role','UPDATE') OR
             has_table_privilege(current_user,'auth_refresh_tokens','DELETE') OR
             has_schema_privilege(current_user,'public','CREATE') THEN RAISE EXCEPTION 'Excess runtime privilege'; END IF;
          IF NOT has_column_privilege(current_user,'users','token_version','UPDATE') THEN RAISE EXCEPTION 'Missing epoch privilege'; END IF;
        END$$; ROLLBACK"""
    else:
        event = str(uuid4())
        statement = f"""BEGIN; SET LOCAL ROLE notification_worker;
        INSERT INTO notification_receipts(event_id,payload) VALUES ('{event}','{{}}');
        INSERT INTO notifications(event_id,recipient_id,job_id,company_id,message,occurred_at)
          VALUES ('{event}',1,1,1,'Restored inbox write probe',now());
        SET LOCAL ROLE notification_api; UPDATE notifications SET read_at=now() WHERE event_id='{event}';
        DO $$BEGIN
          IF has_table_privilege(current_user,'notifications','INSERT') OR
             has_column_privilege(current_user,'notifications','message','UPDATE') OR
             has_schema_privilege(current_user,'public','CREATE') THEN RAISE EXCEPTION 'Excess inbox privilege'; END IF;
        END$$; ROLLBACK"""
    transport.sql(kind, statement, database=target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("docker", "native"), default="docker")
    parser.add_argument("--pg-bin", help="Native PostgreSQL client binary directory")
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--output", type=Path, default=ROOT_DIR / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]))
    restore = commands.add_parser("drill")
    restore.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        transport = Transport(args.mode, args.pg_bin)
        if args.action == "backup":
            backup(transport, args.output, ("jobs",) if args.mode == "native" else tuple(DATABASES))
            print("Backup completed: " + str(args.output.resolve()))
        else:
            report = drill(transport, args.directory)
            print(json.dumps(report, indent=2))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Backup operation failed ({type(error).__name__}); inspect configuration and trusted archives. No existing database is replaced.") from None


if __name__ == "__main__":
    main()
