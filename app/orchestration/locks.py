"""
Phase 4A Part L: per-lecture concurrency control.

WHY ADVISORY LOCKS AND NOT A LOCK TABLE
---------------------------------------
A lock row in a table outlives the process that wrote it. If a scheduler is
killed mid-cycle - OOM, deploy, power - the row stays, and every later cycle
skips that lecture until somebody notices and deletes it by hand. The failure
mode of a crash becomes a silently stuck lecture, which is precisely the
failure an unattended system must not have.

A PostgreSQL session-level advisory lock is released by the SERVER when the
connection ends, for any reason at all, including a process that never got to
run a line of cleanup code. Recovery after a crash is not a procedure; it is
the absence of one.

WHY NOT REDIS
-------------
Nothing in this platform needs Redis today, and a lock that lives in a second
datastore can disagree with the database it is protecting. The lock and the
rows it guards should fail together or not at all.

SCOPE
-----
One lock per lecture, never one per day. Two cycles covering overlapping
lookback windows must be able to make progress on different lectures at the
same time; only the same lecture is serialised. `pg_try_advisory_lock` is
non-blocking on purpose - a cycle that finds a lecture busy records LOCKED and
moves on, because a scheduler that waits on a lock is a scheduler that can be
made to wait forever by one slow lecture.
"""
import hashlib
import logging
from contextlib import contextmanager

from app.common.errors import DATABASE_ERROR, PlatformError


# Namespace for the two-key form of the advisory lock functions. Sharing the
# 64-bit space with anything else would let an unrelated lock elsewhere in the
# database silently block a lecture, so the namespace is explicit and fixed.
LOCK_NAMESPACE = 0x4B42_4C31  # "KBL1"

LOCK_SCOPE = "LECTURE"


class LectureBusy(RuntimeError):
    """Another process holds this lecture. Not an error - a reason to skip."""

    def __init__(self, lecture_id):
        super().__init__(f"lecture {lecture_id} is being processed elsewhere")
        self.lecture_id = str(lecture_id)


def lock_key(lecture_id) -> int:
    """
    A stable signed 32-bit key for one lecture id.

    Derived from the id itself rather than from a counter, so two processes
    that have never spoken agree on the key without coordination.
    """
    digest = hashlib.sha256(str(lecture_id).encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big", signed=False)
    # pg_advisory_lock(int, int) takes signed 32-bit integers.
    return value - 0x1_0000_0000 if value >= 0x8000_0000 else value


class LectureLockManager:
    """
    Session-level advisory locks, one per lecture.

    Session-level rather than transaction-level (`pg_try_advisory_xact_lock`)
    because a lecture's processing spans several service calls with their own
    transaction boundaries, and a lock released by the first commit would
    protect almost nothing.
    """

    def __init__(self, *, namespace: int = LOCK_NAMESPACE):
        self.namespace = namespace
        self.log = logging.getLogger(__name__)

    def try_acquire(self, connection, lecture_id) -> bool:
        try:
            row = connection.execute("SELECT pg_try_advisory_lock(%s, %s)",
                                     (self.namespace, lock_key(lecture_id))).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture lock acquire failed") from exc
        return bool(row[0])

    def release(self, connection, lecture_id) -> bool:
        try:
            row = connection.execute("SELECT pg_advisory_unlock(%s, %s)",
                                     (self.namespace, lock_key(lecture_id))).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture lock release failed") from exc
        return bool(row[0])

    def holders(self, connection) -> list[int]:
        """Which lecture keys this database currently has locked, for diagnostics."""
        try:
            rows = connection.execute(
                "SELECT objid FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = %s AND granted", (self.namespace,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture lock read failed") from exc
        return [row[0] for row in rows]

    @contextmanager
    def hold(self, connection, lecture_id):
        """
        Hold the lock for the duration of the block, releasing it whatever
        happens inside - including an exception, which is the case that
        matters. A failure that leaves a lecture locked would turn one bad
        lecture into a permanently stuck one.
        """
        if not self.try_acquire(connection, lecture_id):
            raise LectureBusy(lecture_id)
        try:
            yield
        finally:
            try:
                self.release(connection, lecture_id)
            except PlatformError:
                # The connection is already broken, and the server releases
                # session locks when it closes. Nothing to repair, and raising
                # here would mask the real failure.
                self.log.warning("advisory lock release failed for %s; the "
                                 "server releases it when the session ends",
                                 lecture_id)


# Phase 4B. A SECOND, coarser scope. Per-lecture locks stop two cycles working
# the same lecture; they do nothing about two cycles both running day-wide
# discovery, both sweeping Graph for the same transcripts, and both deciding
# independently that a lecture needs a generation. The cheapest fix is to allow
# only one cycle at a time - the work inside a cycle is already parallel-safe,
# and a second concurrent cycle adds cost without adding throughput.
#
# A distinct key in the same namespace, so a cycle lock can never collide with
# a lecture lock: lecture keys are derived from uuids, and this one is fixed.
CYCLE_LOCK_KEY = 0x0C1C1E
CYCLE_SCOPE = "SCHEDULER_CYCLE"


class SchedulerCycleBusy(RuntimeError):
    """Another scheduler cycle is running. Not an error - a reason to stand down."""


class SchedulerCycleLock:
    """
    One advisory lock for the whole cycle, held for its duration.

    Non-blocking, like the lecture locks, and for the same reason: a cycle that
    queues behind another cycle is a cycle that can pile up. If the lock is
    held, the correct behaviour is to skip this firing entirely and let the
    running cycle finish - the next scheduled tick will find whatever is left.

    Session-level again, so a crashed scheduler releases it when its connection
    dies rather than blocking every future cycle until somebody intervenes.
    """

    def __init__(self, *, namespace: int = LOCK_NAMESPACE,
                 key: int = CYCLE_LOCK_KEY):
        self.namespace = namespace
        self.key = key
        self.log = logging.getLogger(__name__)

    def try_acquire(self, connection) -> bool:
        try:
            row = connection.execute("SELECT pg_try_advisory_lock(%s, %s)",
                                     (self.namespace, self.key)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "scheduler cycle lock failed") from exc
        return bool(row[0])

    def release(self, connection) -> bool:
        try:
            row = connection.execute("SELECT pg_advisory_unlock(%s, %s)",
                                     (self.namespace, self.key)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "scheduler cycle unlock failed") from exc
        return bool(row[0])

    @contextmanager
    def hold(self, connection):
        if not self.try_acquire(connection):
            raise SchedulerCycleBusy(
                "another scheduler cycle is already running")
        try:
            yield
        finally:
            try:
                self.release(connection)
            except PlatformError:
                self.log.warning("scheduler cycle lock release failed; the "
                                 "server releases it when the session ends")


class NullCycleLock:
    """No cycle locking. For dry runs, which change nothing worth serialising."""

    def try_acquire(self, connection) -> bool:
        return True

    def release(self, connection) -> bool:
        return True

    @contextmanager
    def hold(self, connection):
        yield


class NullLockManager:
    """
    No locking at all, for callers that are single-process by construction -
    a dry run, or a unit test with no database behind it.

    It refuses nothing, which is safe ONLY because a dry run performs no write.
    """

    def try_acquire(self, connection, lecture_id) -> bool:
        return True

    def release(self, connection, lecture_id) -> bool:
        return True

    def holders(self, connection) -> list[int]:
        return []

    @contextmanager
    def hold(self, connection, lecture_id):
        yield
