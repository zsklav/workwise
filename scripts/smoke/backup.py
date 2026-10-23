"""Exercise paired database backups on a disposable Docker stack; pauses writers."""
import argparse
import json
import subprocess
from uuid import uuid4

from scripts.database.backup import COMPOSE, ROOT_DIR, Snapshot, Transport, backup, drill, private_file


WRITERS = ("api", "publisher", "notification-worker", "notification-api")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-service-interruption", action="store_true", required=True)
    parser.parse_args()
    transport = Transport()
    marker = uuid4().hex
    event, read_event = str(uuid4()), str(uuid4())
    destination = ROOT_DIR / "backups" / ("ci_" + marker)
    # Keep the sample outbox producer/consumer states stable during the paired capture.
    try:
        subprocess.run([*COMPOSE, "stop", *WRITERS], check=True, timeout=90)
        transport.sql("jobs", f"""BEGIN;
            INSERT INTO outbox_events(id,payload,created_at) VALUES
              ('{event}',jsonb_build_object('restore_probe','{marker}'),now());
            COMMIT""")
        transport.sql("notifications", f"""BEGIN;
            INSERT INTO notification_receipts(event_id,payload) VALUES
              ('{event}',jsonb_build_object('restore_probe','{marker}')),
              ('{read_event}',jsonb_build_object('restore_probe','{marker}'));
            INSERT INTO notifications(event_id,recipient_id,job_id,company_id,message,occurred_at,read_at) VALUES
              ('{event}',1,1,1,'Restore probe: unread ✓',now(),NULL),
              ('{read_event}',1,1,1,'Restore probe: read ✓',now(),now());
            COMMIT""")
        manifest = backup(transport, destination, ("jobs", "notifications"))
        report = drill(transport, destination)
        # Paused source databases must still match their original captured snapshots.
        for kind, item in manifest["databases"].items():
            with Snapshot(transport, kind) as source:
                assert source.inventory() == {key: item[key] for key in ("tables", "schema", "version")}
        report["source_databases_unchanged"] = True
        report["pending_outbox_and_read_unread_inbox_preserved"] = True
        output = ROOT_DIR / "backup-reports"
        output.mkdir(exist_ok=True)
        with private_file(output / ("restore_" + marker + ".json")) as stream:
            stream.write((json.dumps(report, indent=2) + "\n").encode())
        print("Verified: both archives restored with identical rows, schema, owners and runtime grants", flush=True)
        print("Verified: identity-generated writes and inbox read markers work on restored copies", flush=True)
        print("Verified: pending outbox, receipts and read/unread inbox preserved; source data unchanged", flush=True)
    finally:
        # Only this smoke test's unique fixtures are removed, before any consumers resume.
        try:
            transport.sql("jobs", f"DELETE FROM outbox_events WHERE id='{event}' AND payload->>'restore_probe'='{marker}'")
            transport.sql("notifications", f"""BEGIN;
                DELETE FROM notifications WHERE event_id IN ('{event}','{read_event}');
                DELETE FROM notification_receipts WHERE event_id IN ('{event}','{read_event}') AND payload->>'restore_probe'='{marker}';
                COMMIT""")
        finally:
            subprocess.run([*COMPOSE, "start", *WRITERS], check=True, timeout=90)
    print("Backup and restore smoke test passed.", flush=True)


if __name__ == "__main__":
    main()
