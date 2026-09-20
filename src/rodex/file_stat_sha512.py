"""Fingerprint file metadata using the user-supplied stat algorithm, not contents."""

import hashlib
import os
import struct


def file_stat_sha512(path: str) -> str:
    s = os.stat(path)

    # Metadata only — NO file contents are read.
    data = struct.pack(
        "!QQQQQQQQ",
        s.st_mode,  # permissions + file type
        s.st_ino,  # inode
        s.st_dev,  # device
        s.st_uid,  # owner
        s.st_gid,  # group
        s.st_size,  # size
        s.st_mtime_ns,  # modification time
        s.st_ctime_ns,  # metadata change time
    )

    return hashlib.sha512(data).hexdigest()
