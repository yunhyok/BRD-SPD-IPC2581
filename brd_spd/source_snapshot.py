"""Keep a reviewed SPD revision stable during generation or upload."""
from contextlib import contextmanager
from concurrent.futures import CancelledError
from pathlib import Path
from tempfile import TemporaryDirectory
import os

from .selection_ui import catalog_signature


@contextmanager
def source_snapshot(source, expected, directory, cancelled=lambda: False, progress=None):
    source = Path(source).resolve()
    if catalog_signature(source) != expected:
        raise ValueError("SPD 파일이 변경되었습니다. 다시 불러오세요.")
    with TemporaryDirectory(prefix="brd-spd-input-", dir=directory) as temporary:
        snapshot = Path(temporary) / source.name
        with source.open("rb") as incoming, snapshot.open("wb") as outgoing:
            stat = os.fstat(incoming.fileno())
            if (str(source), stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino) != expected:
                raise ValueError("SPD 파일이 변경되었습니다. 다시 불러오세요.")
            # Report at most every 64 MiB or 5%, so a large copy still shows progress.
            total = stat.st_size
            step = min(64 * 1024 * 1024, max(total // 20, 1))
            copied = reported = 0
            while chunk := incoming.read(4 * 1024 * 1024):
                if cancelled():
                    raise CancelledError("SPD 입력 준비가 취소되었습니다.")
                outgoing.write(chunk)
                copied += len(chunk)
                if progress is not None and (copied - reported >= step or copied >= total):
                    progress(copied, total)
                    reported = copied
            stat = os.fstat(incoming.fileno())
            if (str(source), stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino) != expected:
                raise ValueError("읽는 동안 SPD 파일이 변경되었습니다. 다시 불러오세요.")
        if catalog_signature(source) != expected:
            raise ValueError("읽는 동안 SPD 파일이 변경되었습니다. 다시 불러오세요.")
        if cancelled():
            raise CancelledError("SPD 입력 준비가 취소되었습니다.")
        yield snapshot
