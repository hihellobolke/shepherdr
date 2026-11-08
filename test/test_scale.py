"""50 fake workers writing status at once on the current Store: lock wait (p50, p95, max) and
queue behaviour (SHMD-10, OpenSpec flow proposal T09, section 9.3).

Each worker is a real separate process (not a thread), so there is no shared Python interpreter or
GIL between them: this measures the flock queue exactly as 50 real herdr-task workers writing
status to one folder's list would hit it. Every worker owns its own task on the one list, so all
50 contend on the single store lock file. The write path is the Store's own: open the locked Store,
set the task's `progress`, mark it dirty, and on exit write the whole list back atomically while
still holding the lock.

What the measured wait is, and is not:
- It is the time from asking an already-built Store to open to having it open (the flock queue plus
  the small list read that runs under the lock). The Store object is built before timing starts, so
  the pre-lock slug/collision read in store_path is not counted.
- It is a Store-level number. The real `herdr-task status` command (cmd_status) holds this same
  lock across live_agents()/view(), which shell out to hc, so production waits are LARGER than the
  numbers here. The headline store.py/commands.py follow-up, if these waits are too high, is to
  stop holding the store lock across those hc calls: read under the lock, compute the view after
  releasing it.

Quick by default (few writes per worker) so it stays in the normal suite; set HERDR_SCALE_FULL=1
for the full run (more writes per worker for a steadier distribution).
"""
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mods", "tasks", "bin")
sys.path.insert(0, BIN)

import herdr_task.store as store_mod  # noqa: E402

WORKERS = 50
QUICK_WRITES = 3  # writes per worker in the normal suite (50 * 3 = 150 locked status writes)
FULL_WRITES = 20  # writes per worker when HERDR_SCALE_FULL=1 (50 * 20 = 1000 locked status writes)
FULL = os.environ.get("HERDR_SCALE_FULL") == "1"
# The proposal's budget: a status write should not wait on the store lock longer than this.
WAIT_BUDGET_MS = 200
# A worker waiting this long has not hit the lock queue; it has hit a deadlock or starvation.
STUCK_MS = 30000


def percentile(values, pct):
    """The pct-th percentile (0..100) of `values`, nearest-rank on the sorted samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _worker(store_dir, loc, task_id, writes, barrier, result_q):
    """One fake worker, in its own process: write status `writes` times on its own task, timing how
    long each open of the locked Store takes. Reports (task_id, [wait_ms...], error_or_None)."""
    # Point this process's Store at the throwaway root, however it was started (fork inherits the
    # parent's real STORE; spawn re-imports from the real env). This keeps every worker off the
    # real ~/.herdr/tasks, with no HOME change.
    import herdr_task.base as base
    base.STORE = store_dir
    store_mod.STORE = store_dir
    from herdr_task.store import Store
    from herdr_task.base import now

    waits = []
    try:
        barrier.wait(timeout=STUCK_MS / 1000.0)  # all 50 start writing at once
        for k in range(writes):
            store = Store(loc)  # built before timing: the pre-lock slug read is not the lock wait
            t0 = time.perf_counter()
            store.__enter__()  # the wait: flock queue, then the small list read under the lock
            waits.append((time.perf_counter() - t0) * 1000.0)
            try:
                task = store.find(task_id)
                # A distinct value per worker per iteration, so a lost or clobbered write shows up
                # in the final list (identical text would hide a dropped write).
                task["progress"] = {"text": f"w{task_id} iter {k}", "at": now(), "set": now(),
                                    "done": k}
                store.dirty = True
            finally:
                store.__exit__(None, None, None)  # writes the list back, then releases the lock
        result_q.put((task_id, waits, None))
    except Exception as exc:  # noqa: BLE001 - a worker's failure is a test failure
        try:
            barrier.abort()  # don't leave siblings blocked at the barrier
        except Exception:  # noqa: BLE001
            pass
        result_q.put((task_id, waits, repr(exc)))


class StoreLockScale(unittest.TestCase):
    """Many worker processes writing status at once on one Store, in a throwaway root (no HOME
    change, no hc, no gh: the Store's status write path shells out to neither)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_dir = os.path.join(self.tmp, "tasks")
        os.makedirs(self.store_dir)
        # The parent uses store_path to find the list to pre-create; point it at the throwaway root.
        p = mock.patch.object(store_mod, "STORE", self.store_dir)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._cleanup_tmp)

        self.folder = os.path.join(self.tmp, "repo")
        os.makedirs(self.folder)
        self.loc = {"folder": self.folder, "kind": "folder", "name": "repo", "common": None}

        from herdr_task.base import now
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
        """Fire WORKERS processes that all start writing at once; return (wait_ms samples, wall_ms,
        errors). Processes that get stuck are terminated so a locking regression fails the test
        instead of hanging the suite."""
        # fork keeps it fast and predictable (Linux CI's default); it is available on macOS too.
        # This test process is single-threaded here, so fork is safe.
        ctx = mp.get_context("fork")
        barrier = ctx.Barrier(WORKERS)
        result_q = ctx.Queue()
        procs = [ctx.Process(target=_worker,
                             args=(self.store_dir, self.loc, i + 1, writes_per_worker, barrier,
                                   result_q))
                 for i in range(WORKERS)]

        start = time.perf_counter()
        for pr in procs:
            pr.start()

        # Drain results first (a full Queue can wedge a process at join); bound the wait so a
        # deadlock surfaces as a failure, not a hung suite.
        deadline = start + (STUCK_MS / 1000.0) + 30.0
        results = {}
        errors = []
        while len(results) < WORKERS and time.perf_counter() < deadline:
            try:
                task_id, waits, err = result_q.get(timeout=1.0)
            except Exception:  # noqa: BLE001 - empty queue tick
                continue
            results[task_id] = waits
            if err:
                errors.append(f"worker {task_id}: {err}")
        wall_ms = (time.perf_counter() - start) * 1000.0

        if len(results) < WORKERS:
            errors.append(f"only {len(results)}/{WORKERS} workers reported: likely a deadlock or "
                          "starvation on the store lock")
        for pr in procs:
            pr.join(timeout=5.0)
            if pr.is_alive():
                pr.terminate()
                pr.join(timeout=5.0)

        samples = [w for waits in results.values() for w in waits]
        return samples, wall_ms, errors

    def test_fifty_workers_write_status_at_once(self):
        writes = FULL_WRITES if FULL else QUICK_WRITES
        waits, wall_ms, errors = self._run_contention(writes)

        self.assertEqual(errors, [], "a worker failed or got stuck while writing status")
        self.assertEqual(len(waits), WORKERS * writes, "not every worker finished its writes")

        # The list is still valid JSON, and every worker's FINAL write survived on its own task:
        # no write was lost or clobbered by another worker's whole-list rewrite under the lock.
        with open(self.path) as f:
            after = json.load(f)
        self.assertEqual(len(after["tasks"]), WORKERS)
        for i in range(WORKERS):
            task = next(t for t in after["tasks"] if t["id"] == i + 1)
            prog = task.get("progress") or {}
            self.assertEqual(prog.get("text"), f"w{i + 1} iter {writes - 1}",
                             f"task {i + 1} lost its final status write under contention")
            self.assertEqual(prog.get("done"), writes - 1)
            self.assertIn("set", prog)

        p50 = percentile(waits, 50)
        p95 = percentile(waits, 95)
        worst = max(waits)
        over = " OVER the 200 ms budget" if worst > WAIT_BUDGET_MS else " within the 200 ms budget"
        # Printed so the numbers show up in a plain run and in the PR; unittest hides stdout unless
        # a test fails or -v is passed, so emit on stderr.
        summary = (f"[SHMD-10 store lock scale] {WORKERS} worker processes x {writes} writes = "
                   f"{len(waits)} locked status writes on one list; "
                   f"store-lock wait p50={p50:.1f}ms p95={p95:.1f}ms max={worst:.1f}ms{over}; "
                   f"wall={wall_ms:.0f}ms "
                   f"({'full run' if FULL else 'quick run, set HERDR_SCALE_FULL=1 for the full run'}).")
        print(summary, file=sys.stderr)

        # No absolute-timing assert: real waits are machine- and load-dependent, so a hard 200 ms
        # gate here would be flaky. The budget is reported above and the PR calls out a breach. We
        # assert only that the queue behaved: every worker got the lock, nothing was lost, waits
        # were measured, and no single wait ran away into a deadlock or starvation.
        self.assertGreater(len(waits), 0)
        self.assertTrue(all(w >= 0 for w in waits))
        self.assertLess(worst, STUCK_MS, "a status write waited over 30s: a deadlock or starvation")


if __name__ == "__main__":
    unittest.main()
