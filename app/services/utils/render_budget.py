"""Shared native-render slots for threads and services using the same storage."""

import errno
import os
import time
from contextlib import contextmanager

from app.utils import utils

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def native_render_slot(slot_count):
    directory = utils.storage_dir("render-locks")
    os.makedirs(directory, exist_ok=True)
    acquired = None
    try:
        while acquired is None:
            for index in range(max(1, slot_count)):
                path = os.path.join(directory, f"slot-{index}.lock")
                try:
                    handle = open(path, "a+b")
                except PermissionError:
                    if os.name == "nt":
                        raise
                    # A container may create the shared lock file with a UID
                    # that the host user cannot write. flock also works on a
                    # read-only descriptor on Unix; keep the existing inode.
                    handle = open(path, "rb")
                try:
                    if os.name == "nt":
                        if os.fstat(handle.fileno()).st_size == 0:
                            handle.write(b"\0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    handle.close()
                    # Only contention should retry; disk/permission errors fail.
                    if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
                        raise
                    continue
                except BaseException:
                    handle.close()
                    raise
                acquired = handle
                break
            if acquired is None:
                time.sleep(0.05)
        yield
    finally:
        if acquired is not None:
            # Closing releases OS locks, including after exceptions. Keep the
            # file inode stable so waiting services always lock the same file.
            acquired.close()
