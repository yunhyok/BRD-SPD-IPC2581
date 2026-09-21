from concurrent.futures import CancelledError

import pytest

from brd_spd.selection_ui import catalog_signature
from brd_spd.source_snapshot import source_snapshot


def _temporary_snapshots(directory):
    return list(directory.glob("brd-spd-input-*"))


def test_rejects_source_changed_before_copy(tmp_path):
    source = tmp_path / "board.spd"
    source.write_bytes(b"reviewed")
    expected = catalog_signature(source)
    source.write_bytes(b"changed-before-copy")

    with pytest.raises(ValueError, match="SPD 파일이 변경"):
        with source_snapshot(source, expected, tmp_path):
            raise AssertionError("changed source must not yield a snapshot")
    assert _temporary_snapshots(tmp_path) == []


def test_rejects_source_changed_during_copy_and_cleans_partial_snapshot(tmp_path):
    source = tmp_path / "large.spd"
    source.write_bytes(b"a" * (9 * 1024 * 1024))
    expected = catalog_signature(source)
    callbacks = 0

    def mutate_after_first_chunk():
        nonlocal callbacks
        callbacks += 1
        if callbacks == 1:
            with source.open("ab") as stream:
                stream.write(b"changed")
        return False

    with pytest.raises(ValueError, match="읽는 동안 SPD 파일이 변경"):
        with source_snapshot(
                source, expected, tmp_path, cancelled=mutate_after_first_chunk):
            raise AssertionError("mutated copy must not be exposed")
    assert callbacks >= 1
    assert _temporary_snapshots(tmp_path) == []


def test_snapshot_remains_stable_if_original_changes_after_preparation(tmp_path):
    source = tmp_path / "board.spd"
    source.write_bytes(b"reviewed-revision")
    expected = catalog_signature(source)
    snapshot_path = None

    with source_snapshot(source, expected, tmp_path) as snapshot:
        snapshot_path = snapshot
        assert snapshot.read_bytes() == b"reviewed-revision"
        source.write_bytes(b"later-original-revision")
        assert snapshot.read_bytes() == b"reviewed-revision"
        assert snapshot.resolve() != source.resolve()

    assert snapshot_path is not None and not snapshot_path.exists()
    assert _temporary_snapshots(tmp_path) == []


def test_snapshot_cleanup_after_consumer_error(tmp_path):
    source = tmp_path / "board.spd"
    source.write_bytes(b"reviewed")

    with pytest.raises(RuntimeError, match="consumer failed"):
        with source_snapshot(source, catalog_signature(source), tmp_path) as snapshot:
            assert snapshot.is_file()
            raise RuntimeError("consumer failed")
    assert _temporary_snapshots(tmp_path) == []


def test_snapshot_copy_honors_cancellation_and_cleans_up(tmp_path):
    source = tmp_path / "large.spd"
    source.write_bytes(b"a" * (5 * 1024 * 1024))
    checks = 0

    def cancel_during_copy():
        nonlocal checks
        checks += 1
        return checks >= 2

    with pytest.raises(CancelledError, match="취소"):
        with source_snapshot(
                source, catalog_signature(source), tmp_path,
                cancelled=cancel_during_copy):
            raise AssertionError("cancelled copy must not be exposed")
    assert checks >= 2
    assert _temporary_snapshots(tmp_path) == []
