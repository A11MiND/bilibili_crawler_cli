from __future__ import annotations

import codecs
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bili_comments.storage import (
    CSV_COLUMNS,
    CheckpointError,
    CheckpointStore,
    CsvRow,
    CsvStorageError,
    CsvStore,
    StorageError,
    backup_for_restart,
)


BVID = "BV0000000000"
OTHER_BVID = "BV0000000001"
SYNTHETIC_AID = 10**30


def make_row(
    comment_id: str,
    *,
    sequence: int = 1,
    relation: str = "一级评论",
    root_id: str | None = None,
    parent_id: str | None = None,
    content: str = "普通评论",
    ip_location: str | None = "IP属地：测试地区",
) -> CsvRow:
    resolved_root_id = root_id or comment_id
    if relation == "二级评论" and parent_id is None:
        parent_id = resolved_root_id
    return CsvRow(
        root_sequence=sequence,
        relation=relation,
        comment_id=comment_id,
        root_id=resolved_root_id,
        parent_id=parent_id,
        replied_to_name="视频UP主" if relation == "一级评论" else "根作者",
        replied_to_id="synthetic-video-owner",
        author_name=f"作者{comment_id}",
        author_id=f"synthetic-author-{comment_id}",
        content=content,
        published_at="2026-07-27 16:00:00+08:00",
        like_count=3,
        ip_location=ip_location,
    )


class CsvStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.csv_path = Path(self.temporary.name) / "output" / f"{BVID}.csv"

    def read_rows(self) -> list[dict[str, str]]:
        with self.csv_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as source:
            return list(csv.DictReader(source))

    def test_writes_bom_contract_header_and_round_trips_special_text(self) -> None:
        store = CsvStore(self.csv_path)
        content = '第一行, 有逗号和"引号"\n第二行'
        written = store.append_rows(
            [
                make_row("root-1", content=content),
                make_row(
                    "child-1",
                    relation="二级评论",
                    root_id="root-1",
                    parent_id="root-1",
                    content="回复\n跨行",
                    ip_location=None,
                ),
            ]
        )

        self.assertEqual(written, 2)
        self.assertTrue(self.csv_path.read_bytes().startswith(codecs.BOM_UTF8))
        rows = self.read_rows()
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual(rows[0]["评论内容"], content)
        self.assertEqual(rows[1]["评论内容"], "回复\n跨行")
        self.assertEqual(rows[1]["IP属地"], "")
        self.assertEqual(rows[1]["根评论ID"], "root-1")
        self.assertEqual(rows[1]["父评论ID"], "root-1")

    def test_deduplicates_within_batch_and_against_existing_file(self) -> None:
        store = CsvStore(self.csv_path)
        self.assertEqual(
            store.append_rows(
                [
                    make_row("root-1", sequence=1),
                    make_row("root-1", sequence=1),
                    make_row("root-2", sequence=2),
                ]
            ),
            2,
        )
        committed = store.committed_bytes

        reopened = CsvStore(self.csv_path, committed_bytes=committed)
        self.assertEqual(reopened.seen_ids, {"root-1", "root-2"})
        self.assertEqual(reopened.root_sequences, {"root-1": 1, "root-2": 2})
        self.assertEqual(
            reopened.authors["root-1"],
            ("synthetic-author-root-1", "作者root-1"),
        )
        self.assertEqual(
            reopened.append_rows(
                [
                    make_row("root-1", sequence=1),
                    make_row("root-3", sequence=3),
                ]
            ),
            1,
        )
        ids = [row["评论ID"] for row in self.read_rows()]
        self.assertEqual(ids, ["root-1", "root-2", "root-3"])

    def test_create_and_append_flush_and_fsync(self) -> None:
        with (
            mock.patch("bili_comments.storage.os.fsync") as fsync,
            mock.patch(
                "bili_comments.storage._fsync_directory"
            ) as fsync_directory,
        ):
            store = CsvStore(self.csv_path)
            store.append_rows([make_row("root-1")])
        self.assertGreaterEqual(fsync.call_count, 2)
        fsync_directory.assert_called_once_with(self.csv_path.parent)

    def test_recovery_truncates_uncommitted_partial_record(self) -> None:
        store = CsvStore(self.csv_path)
        store.append_rows(
            [make_row("root-1", content='已提交,"完整"\n内容')]
        )
        committed = store.committed_bytes
        with self.csv_path.open("ab") as output:
            output.write(b'2,first-level,"unterminated')
            output.flush()
            os.fsync(output.fileno())
        self.assertGreater(self.csv_path.stat().st_size, committed)

        recovered = CsvStore(self.csv_path, committed_bytes=committed)
        self.assertEqual(recovered.committed_bytes, committed)
        self.assertEqual(self.csv_path.stat().st_size, committed)
        self.assertEqual(recovered.seen_ids, {"root-1"})
        self.assertEqual(self.read_rows()[0]["评论内容"], '已提交,"完整"\n内容')

    def test_recovery_refuses_csv_shorter_than_checkpoint(self) -> None:
        store = CsvStore(self.csv_path)
        store.append_rows([make_row("root-1")])
        committed = store.committed_bytes
        with self.csv_path.open("r+b") as output:
            output.truncate(committed - 1)

        with self.assertRaisesRegex(
            CsvStorageError,
            "shorter than checkpoint",
        ):
            CsvStore(self.csv_path, committed_bytes=committed)

    def test_existing_file_requires_bom_and_exact_header(self) -> None:
        self.csv_path.parent.mkdir(parents=True)
        self.csv_path.write_text("wrong,header\n", encoding="utf-8")
        with self.assertRaisesRegex(CsvStorageError, "UTF-8 BOM"):
            CsvStore(self.csv_path)

    def test_conflicting_root_sequence_is_rejected_before_write(self) -> None:
        store = CsvStore(self.csv_path)
        committed = store.committed_bytes
        with self.assertRaisesRegex(CsvStorageError, "repeated in the batch"):
            store.append_rows(
                [
                    make_row("root-1", sequence=1),
                    make_row("root-2", sequence=1),
                ]
            )
        self.assertEqual(self.csv_path.stat().st_size, committed)

    def test_existing_csv_symlink_is_rejected_without_touching_target(self) -> None:
        target = Path(self.temporary.name) / "synthetic-target.csv"
        target_store = CsvStore(target)
        target_store.append_rows([make_row("root-1")])
        target_bytes = target.read_bytes()
        self.csv_path.parent.mkdir(parents=True)
        self.csv_path.symlink_to(target)

        with self.assertRaises(CsvStorageError):
            CsvStore(self.csv_path)

        self.assertEqual(target.read_bytes(), target_bytes)

    def test_read_only_open_does_not_create_missing_csv(self) -> None:
        with self.assertRaisesRegex(CsvStorageError, "CSV is missing"):
            CsvStore(self.csv_path, create=False)

        self.assertFalse(self.csv_path.exists())

    def test_csv_and_checkpoint_fifo_are_rejected_without_blocking(self) -> None:
        self.csv_path.parent.mkdir(parents=True)
        os.mkfifo(self.csv_path, 0o600)
        with self.assertRaises(CsvStorageError):
            CsvStore(self.csv_path, create=False)

        checkpoint_path = (
            Path(self.temporary.name) / "state" / f"{BVID}.json"
        )
        checkpoint_path.parent.mkdir(parents=True)
        os.mkfifo(checkpoint_path, 0o600)
        with self.assertRaises(CheckpointError):
            CheckpointStore(checkpoint_path.parent, BVID).load()

    def test_append_rejects_replaced_csv_symlink(self) -> None:
        store = CsvStore(self.csv_path)
        store.append_rows([make_row("root-1")])
        target = Path(self.temporary.name) / "synthetic-target.csv"
        target.write_bytes(self.csv_path.read_bytes())
        target_bytes = target.read_bytes()
        self.csv_path.unlink()
        self.csv_path.symlink_to(target)

        with self.assertRaises(CsvStorageError):
            store.append_rows([make_row("root-2", sequence=2)])

        self.assertEqual(target.read_bytes(), target_bytes)

    def test_encoding_failure_marks_store_unhealthy_until_reopened(self) -> None:
        store = CsvStore(self.csv_path)
        committed = store.committed_bytes
        with self.assertRaises(CsvStorageError):
            store.append_rows(
                [make_row("root-1", content="invalid surrogate \ud800")]
            )
        with self.assertRaisesRegex(CsvStorageError, "not writable"):
            store.append_rows([make_row("root-2")])

        recovered = CsvStore(
            self.csv_path,
            committed_bytes=committed,
            expected_rows=0,
            create=False,
        )
        self.assertEqual(recovered.append_rows([make_row("root-1")]), 1)

    def test_recovery_rejects_csv_symlink_without_truncating_target(self) -> None:
        store = CsvStore(self.csv_path)
        committed = store.committed_bytes
        target = Path(self.temporary.name) / "synthetic-target.csv"
        target_store = CsvStore(target)
        target_store.append_rows([make_row("root-1")])
        target_bytes = target.read_bytes()
        self.csv_path.unlink()
        self.csv_path.symlink_to(target)

        with self.assertRaises(CsvStorageError):
            CsvStore(
                self.csv_path,
                committed_bytes=committed,
                expected_rows=0,
            )

        self.assertEqual(target.read_bytes(), target_bytes)


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.store = CheckpointStore(self.state_dir, BVID)

    def test_create_save_and_load_round_trip(self) -> None:
        with mock.patch(
            "bili_comments.storage._fsync_directory"
        ) as fsync_directory:
            checkpoint = self.store.create(
                aid=SYNTHETIC_AID,
                committed_bytes=456,
            )
        fsync_directory.assert_called_once_with(self.state_dir)
        self.assertEqual(self.store.path.name, f"{BVID}.json")
        self.assertEqual(checkpoint.committed_bytes, 456)

        checkpoint.main_cursor = {"pagination_reply": {"next_offset": "abc"}}
        checkpoint.completed_root_ids_in_page.append("root-1")
        checkpoint.next_root_sequence = 2
        checkpoint.rows_written = 7
        self.store.save(checkpoint)

        loaded = self.store.load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.bvid, BVID)
        self.assertEqual(loaded.main_cursor, checkpoint.main_cursor)
        self.assertEqual(loaded.completed_root_ids_in_page, ["root-1"])
        self.assertEqual(loaded.rows_written, 7)
        self.assertTrue(loaded.updated_at)
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["committed_bytes"], 456)
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["auth_mode"], "anonymous")
        self.assertEqual(payload["child_strategy"], "page")

    def test_loads_legacy_v2_checkpoint_with_page_strategy(self) -> None:
        checkpoint = self.store.create(
            aid=SYNTHETIC_AID,
            committed_bytes=456,
        )
        payload = checkpoint.to_dict()
        payload["schema_version"] = 2
        payload.pop("child_strategy")
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = self.store.load()

        assert loaded is not None
        self.assertEqual(loaded.schema_version, 2)
        self.assertEqual(loaded.child_strategy, "page")

    def test_atomic_replace_failure_keeps_old_checkpoint(self) -> None:
        checkpoint = self.store.create(
            aid=SYNTHETIC_AID,
            committed_bytes=456,
        )
        old_bytes = self.store.path.read_bytes()
        checkpoint.status = "complete"
        checkpoint.phase = "complete"

        with mock.patch(
            "bili_comments.storage.os.replace",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaises(CheckpointError):
                self.store.save(checkpoint)

        self.assertEqual(self.store.path.read_bytes(), old_bytes)
        self.assertFalse(
            self.store.path.with_suffix(".json.tmp").exists(),
        )

    def test_save_does_not_follow_precreated_fixed_temp_symlink(self) -> None:
        self.state_dir.mkdir(parents=True)
        target = Path(self.temporary.name) / "synthetic-target.txt"
        target.write_text("unchanged\n", encoding="utf-8")
        legacy_temporary = self.store.path.with_suffix(".json.tmp")
        legacy_temporary.symlink_to(target)

        checkpoint = self.store.create(
            aid=SYNTHETIC_AID,
            committed_bytes=456,
        )

        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")
        self.assertTrue(legacy_temporary.is_symlink())
        self.assertEqual(self.store.load(), checkpoint)

    def test_load_rejects_corrupt_or_mismatched_checkpoint(self) -> None:
        checkpoint = self.store.create(
            aid=SYNTHETIC_AID,
            committed_bytes=456,
        )
        value = checkpoint.to_dict()
        value["bvid"] = OTHER_BVID
        self.store.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(CheckpointError, "BVID mismatch"):
            self.store.load()

        self.store.path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(CheckpointError, "failed to read"):
            self.store.load()


class RestartBackupTests(unittest.TestCase):
    def test_backs_up_existing_csv_and_checkpoint_with_same_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / f"{BVID}.csv"
            checkpoint_path = root / f"{BVID}.json"
            csv_path.write_bytes(b"csv")
            checkpoint_path.write_bytes(b"json")

            with mock.patch(
                "bili_comments.storage._fsync_directory"
            ) as fsync_directory:
                backups = backup_for_restart(
                    csv_path,
                    checkpoint_path,
                    timestamp="20260727T160000",
                )

            self.assertEqual(
                backups,
                (
                    Path(f"{csv_path}.bak.20260727T160000"),
                    Path(f"{checkpoint_path}.bak.20260727T160000"),
                ),
            )
            self.assertFalse(csv_path.exists())
            self.assertFalse(checkpoint_path.exists())
            self.assertEqual(backups[0].read_bytes(), b"csv")
            self.assertEqual(backups[1].read_bytes(), b"json")
            fsync_directory.assert_called_once_with(root)

    def test_backup_syncs_each_directory_and_syncs_after_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_directory = root / "output"
            second_directory = root / "state"
            first_directory.mkdir()
            second_directory.mkdir()
            csv_path = first_directory / f"{BVID}.csv"
            checkpoint_path = second_directory / f"{BVID}.json"
            csv_path.write_bytes(b"csv")
            checkpoint_path.write_bytes(b"json")

            with mock.patch(
                "bili_comments.storage._fsync_directory"
            ) as fsync_directory:
                backup_for_restart(
                    csv_path,
                    checkpoint_path,
                    timestamp="success",
                )
            self.assertEqual(
                {call.args[0] for call in fsync_directory.call_args_list},
                {first_directory, second_directory},
            )

            csv_backup = Path(f"{csv_path}.bak.success")
            checkpoint_backup = Path(f"{checkpoint_path}.bak.success")
            os.replace(csv_backup, csv_path)
            os.replace(checkpoint_backup, checkpoint_path)
            real_replace = os.replace
            replace_calls = 0

            def fail_second_replace(source: object, destination: object) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("injected second rename failure")
                real_replace(source, destination)

            with (
                mock.patch(
                    "bili_comments.storage.os.replace",
                    side_effect=fail_second_replace,
                ),
                mock.patch(
                    "bili_comments.storage._fsync_directory"
                ) as rollback_sync,
            ):
                with self.assertRaisesRegex(StorageError, "failed to back up"):
                    backup_for_restart(
                        csv_path,
                        checkpoint_path,
                        timestamp="rollback",
                    )

            self.assertTrue(csv_path.exists())
            self.assertTrue(checkpoint_path.exists())
            self.assertEqual(
                {call.args[0] for call in rollback_sync.call_args_list},
                {first_directory, second_directory},
            )

    def test_does_not_overwrite_existing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / f"{BVID}.csv"
            path.write_bytes(b"current")
            destination = Path(f"{path}.bak.fixed")
            destination.write_bytes(b"old backup")

            with self.assertRaisesRegex(StorageError, "already exists"):
                backup_for_restart(path, timestamp="fixed")
            self.assertEqual(path.read_bytes(), b"current")
            self.assertEqual(destination.read_bytes(), b"old backup")


if __name__ == "__main__":
    unittest.main()
