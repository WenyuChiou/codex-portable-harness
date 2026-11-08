"""Kernel leases for three executing jobs and one heavy I/O job. No daemon."""
from contextlib import contextmanager, ExitStack
import errno
import os
import time

from storage_lifecycle import Guard


@contextmanager
def lock(path):
    with path.open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                raise BlockingIOError("workload slot busy") from exc
            raise
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def work_slot(home, heavy=True, wait=300):
    guard = Guard(home)
    root = guard.check(home/"maintenance/workload", home, allow_missing=True)
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic()+wait
    with ExitStack() as stack:
        # Heavy waiters do not occupy the three general execution slots.
        if heavy:
            while True:
                try:
                    stack.enter_context(lock(guard.check(root/"heavy.lock", home, allow_missing=True)))
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("heavy workload slot wait expired")
                    time.sleep(.1)
        while True:
            for number in range(3):
                try:
                    stack.enter_context(lock(guard.check(root/f"job-{number}.lock", home, allow_missing=True)))
                except BlockingIOError:
                    continue
                yield
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("three executing jobs already hold slots")
            time.sleep(.1)
