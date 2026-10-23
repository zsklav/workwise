"""Destructive boundaries and corrupt archives must fail before touching PostgreSQL."""
from contextlib import nullcontext
import json
from unittest.mock import Mock

import pytest

from scripts.database.backup import backup, drill, file_hash, fresh_database, load_manifest, validate_target


def archive_set(directory):
    archive = directory / "jobs.dump"
    archive.write_bytes(b"PGDMP-test-archive")
    manifest = {"format": 1, "databases": {"jobs": {
        "archive": archive.name, "bytes": archive.stat().st_size, "sha256": file_hash(archive)}}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize("name", ["workwise", "workwise_test", "notifications", "postgres",
                                  "ww_restore_abc_jobs", 'ww_restore_";DROP DATABASE workwise;--',
                                  "ww_restore_" + "a" * 32 + "_notifications"])
def test_restore_rejects_existing_or_unbounded_target_names(name):
    with pytest.raises(ValueError):
        validate_target(name, "jobs")


@pytest.mark.parametrize("corruption", [b"corrupted-backup", b"PGDMP-test-archivX"])
def test_archive_corruption_is_rejected_before_database_creation(tmp_path, corruption):
    archive_set(tmp_path)
    (tmp_path / "jobs.dump").write_bytes(corruption)
    transport = Mock(mode="docker")
    with pytest.raises(ValueError, match="checksum/size"):
        drill(transport, tmp_path)
    transport.sql.assert_not_called()
    transport.run.assert_not_called()


def test_archive_path_traversal_is_rejected(tmp_path):
    manifest = archive_set(tmp_path)
    manifest["databases"]["jobs"]["archive"] = "../jobs.dump"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="archive path"):
        load_manifest(tmp_path)


def test_unknown_database_is_rejected(tmp_path):
    manifest = archive_set(tmp_path)
    manifest["databases"]["postgres"] = manifest["databases"].pop("jobs")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest"):
        load_manifest(tmp_path)


def test_restore_failure_only_cleans_up_database_created_by_this_drill():
    transport = Mock()
    with pytest.raises(RuntimeError, match="simulated restore failure"):
        with fresh_database(transport, "jobs") as name:
            validate_target(name, "jobs")
            raise RuntimeError("simulated restore failure")
    calls = transport.sql.call_args_list
    assert len(calls) == 2
    assert calls[0].args[1] == f'CREATE DATABASE "{name}" OWNER "workwise_migrator" TEMPLATE template0'
    assert calls[1].args[1] == f'DROP DATABASE "{name}"'
    assert all(call.kwargs == {"database": "postgres"} for call in calls)


def test_database_name_collision_never_drops_an_existing_database():
    transport = Mock()
    transport.sql.side_effect = RuntimeError("already exists")
    with pytest.raises(RuntimeError):
        with fresh_database(transport, "jobs"):
            pytest.fail("An existing database must not be reused")
    assert transport.sql.call_count == 1


def test_failed_backup_has_no_completed_manifest_and_existing_backup_is_preserved(tmp_path, monkeypatch):
    destination = tmp_path / "backup"
    transport = Mock(mode="docker")
    snapshot = Mock(identity="00000003-0000001B-1")
    snapshot.inventory.return_value = {"tables": {}, "schema": {}, "version": "0010"}
    monkeypatch.setattr("scripts.database.backup.Snapshot", lambda *args: nullcontext(snapshot))
    transport.run.side_effect = RuntimeError("simulated pg_dump failure")
    with pytest.raises(RuntimeError, match="pg_dump failure"):
        backup(transport, destination, ("jobs",))
    assert not (destination / "manifest.json").exists()
    sentinel = destination / "preserved.dump"
    sentinel.write_bytes(b"existing backup")
    with pytest.raises(FileExistsError):
        backup(transport, destination, ("jobs",))
    assert sentinel.read_bytes() == b"existing backup"
