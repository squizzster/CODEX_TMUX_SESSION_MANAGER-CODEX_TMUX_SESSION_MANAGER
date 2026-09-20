import hashlib
import struct
from types import SimpleNamespace

import rodex.file_stat_sha512 as stat_library


def test_fingerprint_is_exactly_the_supplied_eight_metadata_fields(monkeypatch):
    fields = (33188, 100, 200, 1000, 1000, 42, 1700000000000000000, 1700000000000000001)
    metadata = SimpleNamespace(
        **dict(
            zip(
                ("st_mode", "st_ino", "st_dev", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"),
                fields,
                strict=True,
            )
        )
    )
    seen = []
    monkeypatch.setattr(stat_library, "os", SimpleNamespace(stat=lambda path: seen.append(path) or metadata))
    assert stat_library.file_stat_sha512("rules.yaml") == hashlib.sha512(struct.pack("!QQQQQQQQ", *fields)).hexdigest()
    assert seen == ["rules.yaml"]
