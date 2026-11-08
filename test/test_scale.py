"""50 fake workers writing status at once on the current Store: lock wait (p50, p95, max) and
queue behaviour (SHMD-10, OpenSpec flow proposal T09, section 9.3).

Each worker opens its own Store on one throwaway task list (one shared lock file), so all 50
contend on the same flock, exactly as real herdr-task workers writing status to one folder's list
do.
The write path is the same as `herdr-task status`: open the locked Store, set the task's
`progress`, mark it dirty, and on exit write the list back atomically while still holding the lock.

The lock wait we measure is the time from asking for the Store to having it (the flock queue plus
the tiny list read); the whole write-back runs inside that lock, so a worker's wait is the sum of
the critical sections queued ahead of it.

Quick by default (small per-worker write count) so it stays in the normal suite; set
HERDR_SCALE_FULL=1 for the full run with more writes per worker for a steadier distribution.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

from herdr_task.base import now  # noqa: E402
import herdr_task.store as store_mod  # noqa: E402
from herdr_task.store import Store  # noqa: E402

WORKERS = 50
QUICK_WRITES = 2  # writes per worker in the normal suite (50 * 2 = 100 locked writes)
FULL_WRITES = 20  # writes per worker when HERDR_SCALE_FULL=1 (50 * 20 = 1000 locked writes)
FULL = os.environ.get("HERDR_SCALE_FULL") == "1"
# The proposal's budget: a status write should not wait on the lock longer than this.
WAIT_BUDGET_MS = 200


def percentile(values, pct):
    """The pct-th percentile (0..100) of `values`, nearest-rank on the sorted samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


class StoreLockScale(unittest.TestCase):
    """Many workers writing status at once on one Store, in a throwaway root (no HOME change, no
    hc, no gh): just the locked Store on real files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_dir = os.path.join(self.tmp, "tasks")
        os.makedirs(self.store_dir)
        # Point the Store's STORE at the throwaway root (store_path and store_files read it).
        p = mock.patch.object(store_mod, "STORE", self.store_dir)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._cleanup_tmp)

        self.folder = os.path.join(self.tmp, "repo")
        os.makedirs(self.folder)
        self.loc = {"folder": self.folder, "kind": "folder", "name": "repo", "common": None}

        slug, self.path = store_mod.store_path(self.folder)
        # One running task per worker, so each worker writes its own task's status (all on the one
        # list, so all on the one lock). The prefix is preset so opening never mints one or retags.
        tasks = [{"id": i + 1, "title": f"t{i + 1}", "prompt": "", "mode": "here",
                  "state": "running", "created": now(), "agents": []} for i in range(WORKERS)]
        data = {"version": 1, "folder": self.folder, "kind": "folder", "name": "repo",
                "slug": slug, "prefix": "SCALE", "next": WORKERS + 1, "tasks": tasks}
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    def _cleanup_tmp(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_contention(self, writes_per_worker):
        """Fire WORKERS threads that all start writing at once; return (wait_ms samples, wall_ms,
        errors). Each worker opens its own Store per write, so each write is a real lock acquire."""
        barrier = threading.Barrier(WORKERS)
        samples = [[] for _ in range(WORKERS)]
        errors = []

        def worker(idx):
            task_id = idx + 1
            try:
                barrier.wait()
                for _ in range(writes_per_worker):
                    t0 = time.perf_counter()
                    with Store(self.loc) as s:
                        waited_ms = (time.perf_counter() - t0) * 1000.0
                        task = s.find(task_id)
                        task["progress"] = {"text": "Working on it", "at": now(),
                                            "set": now(), "done": 50}
                        s.dirty = True
                    samples[idx].append(waited_ms)
            except Exception as exc:  # noqa: BLE001 - a worker's failure is a test failure
                errors.append(f"worker {idx}: {exc!r}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
        start = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        wall_ms = (time.perf_counter() - start) * 1000.0
        flat = [w for per in samples for w in per]
        return flat, wall_ms, errors

    def test_fifty_workers_write_status_at_once(self):
        writes = FULL_WRITES if FULL else QUICK_WRITES
        waits, wall_ms, errors = self._run_contention(writes)

        self.assertEqual(errors, [], "a worker failed while writing status under contention")
        self.assertEqual(len(waits), WORKERS * writes, "not every worker finished its writes")

        # The list is still valid JSON and holds every worker's write (no lost or corrupt data).
        with open(self.path) as f:
            after = json.load(f)
        self.assertEqual(len(after["tasks"]), WORKERS)
        self.assertTrue(all(t.get("progress", {}).get("text") == "Working on it"
                            for t in after["tasks"]),
                        "some task never got its status under contention")

        p50 = percentile(waits, 50)
        p95 = percentile(waits, 95)
        worst = max(waits)
        over = " OVER the 200 ms budget" if worst > WAIT_BUDGET_MS else " within the 200 ms budget"
        # Printed so the numbers show up in the PR description and a plain run; unittest hides it
        # unless the test fails or -v is passed, so also emit on stderr.
        summary = (f"[SHMD-10 store lock scale] {WORKERS} workers x {writes} writes = "
                   f"{len(waits)} locked status writes on one list; "
                   f"lock wait p50={p50:.1f}ms p95={p95:.1f}ms max={worst:.1f}ms{over}; "
                   f"wall={wall_ms:.0f}ms "
                   f"({'full run' if FULL else 'quick run, set HERDR_SCALE_FULL=1 for the full run'}).")
        print(summary, file=sys.stderr)

        # No timing assert: absolute waits are machine- and load-dependent, so a hard budget here
        # would be flaky. The budget is reported above and the PR calls out a breach. We only
        # assert the queue behaved: every worker got the lock, nothing was lost, waits were
        # measured, and no single wait ran away to an absurd degree (deadlock or starvation).
        self.assertGreater(len(waits), 0)
        self.assertTrue(all(w >= 0 for w in waits))
        self.assertLess(worst, 30000, "a status write waited over 30s: likely a deadlock or starvation")


if __name__ == "__main__":
    unittest.main()
