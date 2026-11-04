"""The slowed-estimate policies' load model. Run with: cd bridge && uv run python -m unittest discover tests"""

import math
import random
import unittest
from datetime import datetime, timezone

from strategies import kaptain as k


class Quiet(k.Cluster):
    def loop(self):  # no API to poll here
        return


def pod(uid, node, task, millicpu, bound, started, finished):
    def stamp(seconds):
        return datetime.fromtimestamp(1_790_000_000 + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {"metadata": {"uid": uid, "labels": {k.WORKLOAD: task}},
            "spec": {"nodeName": node, "containers": [{"resources": {"limits": {"cpu": f"{millicpu}m"}}}]},
            "status": {"phase": "Succeeded",
                       "conditions": [{"type": "PodScheduled", "status": "True", "lastTransitionTime": stamp(bound)}],
                       "containerStatuses": [{"state": {"terminated": {"startedAt": stamp(started), "finishedAt": stamp(finished)}}}]}}


class FitSlowdown(unittest.TestCase):
    def test_recovers_alpha(self):
        rng = random.Random(1)
        for true in (0.0, 0.3, 1.0, 2.0):
            points = []
            for _ in range(200):
                task = rng.choice("abc")
                millicpu, occ = rng.randint(200, 1000), rng.random() * 1.5
                runtime = {"a": 30, "b": 60, "c": 120}[task] / (millicpu / 1000) * (1 + true * occ) * math.exp(rng.gauss(0, 0.1))
                points.append((task, occ, math.log(runtime * millicpu / 1000)))
            self.assertAlmostEqual(k.fit_slowdown(points), true, delta=0.1)

    def test_no_alpha_without_occupancy_spread(self):
        # Each task always at the same occupancy: every alpha fits as well, none is learned.
        points = [(task, occ, math.log(30 * (1 + occ))) for task, occ in [("a", 0.25), ("b", 0.75)] * 40]
        self.assertIsNone(k.fit_slowdown(points))


class Learning(unittest.TestCase):
    def cluster(self):
        cluster = Quiet(load=k.Load(learn=True))
        cluster.load.see([{"name": "w", "allocatable_millicpu": 2000, "requested_millicpu": 0}], set())
        return cluster

    def test_no_limit_does_not_stop_the_learning(self):
        cluster = self.cluster()
        p = pod("a", "w", "cpu", 0, 0, 0, 30)
        p["spec"]["containers"][0]["resources"] = {}
        cluster.read([p])
        self.assertIn(("cpu", "w"), cluster.estimates.known)
        self.assertEqual(len(cluster.load.points["w"]), 0)

    def test_occupancy_when_bound(self):
        # b is bound while a runs but starts after a finished: the snapshot that placed b counted
        # a, so b's occupancy does too. a was bound alone.
        cluster = self.cluster()
        cluster.read([pod("a", "w", "cpu", 500, 0, 0, 20), pod("b", "w", "memory", 500, 10, 25, 55)])
        occ = {task: o for task, _, o in cluster.load.history["w"]}
        self.assertAlmostEqual(occ["cpu"], 0.25)  # its own 500m over 2 cores
        self.assertAlmostEqual(occ["memory"], 0.5)  # and a's

    def test_estimates_follow_the_last_alpha(self):
        # Once alpha has moved, every runtime the estimates hold is divided by the last one, as if
        # it had been known from the start: none divided by the fixed alpha of the first 20 pods.
        rng, cluster, t = random.Random(2), self.cluster(), 0
        for i in range(80):
            others = rng.choice([0, 500, 1000, 1500])
            occ = (500 + others) / 2000
            runtime = round(30 * (1 + 0.3 * occ))
            if others:
                cluster.read([pod(f"o{i}", "w", "disk", others, t, t, t + runtime + 5)])
            cluster.read([pod(f"p{i}", "w", "cpu", 500, t, t, t + runtime)])
            t += runtime + 10
        alpha = cluster.load.alpha("w")
        self.assertNotEqual(alpha, k.FIXED_SLOWDOWN)
        fresh = k.Estimates()
        for task, runtime, occ in cluster.load.history["w"]:
            fresh.update(task, "w", runtime / (1 + alpha * occ))
        for key, (mu, v, n) in fresh.known.items():
            self.assertAlmostEqual(cluster.estimates.known[key][0], mu, places=9)
            self.assertEqual(cluster.estimates.known[key][2], n)

    def test_no_alpha_learned_without_occupancy_spread(self):
        cluster, t = self.cluster(), 0
        for i in range(40):
            cluster.read([pod(f"p{i}", "w", "cpu", 500, t, t, t + 20)])  # always alone: occupancy 0.25
            t += 30
        self.assertEqual(cluster.load.alpha("w"), k.FIXED_SLOWDOWN)

    def test_failed_pod_is_learned_later(self):
        # The learning raises once for a: the other pods are still learned, and a is the next time.
        cluster, record, calls = self.cluster(), k.Load.record, []

        def flaky(load, task, *rest):
            calls.append(task)
            if task == "cpu" and calls.count("cpu") == 1:
                raise RuntimeError("odd pod")
            return record(load, task, *rest)

        cluster.load.record = flaky.__get__(cluster.load)
        pods = [pod("a", "w", "cpu", 500, 0, 0, 20), pod("b", "w", "memory", 500, 0, 0, 30)]
        with self.assertLogs("kaptain.decider", "ERROR"):
            cluster.read(pods)
        self.assertNotIn(("cpu", "w"), cluster.estimates.known)
        self.assertIn(("memory", "w"), cluster.estimates.known)
        cluster.read(pods)
        self.assertIn(("cpu", "w"), cluster.estimates.known)
        self.assertEqual(len(cluster.load.history["w"]), 2)
        self.assertEqual(cluster.estimates.known[("memory", "w")][2], 1)


class SystemLoad(unittest.TestCase):
    def test_busy_snapshot_does_not_count_twice(self):
        # The snapshot of a busy node also counts the experiment pods: only idle ones give the
        # system's requests, and a pod's occupancy counts the others once, from the pod list.
        cluster = Quiet(load=k.Load(learn=True))
        cluster.read([pod("a", "w", "cpu", 500, 0, 0, 20)])
        busy = pod("b", "w", "memory", 500, 5, 5, 40)
        del busy["status"]["containerStatuses"][0]["state"]["terminated"]
        busy["status"]["phase"] = "Running"
        cluster.read([busy])
        self.assertEqual(cluster.busy, {"w"})
        cluster.load.see([{"name": "w", "allocatable_millicpu": 2000, "requested_millicpu": 700}], cluster.busy)
        self.assertNotIn("w", cluster.load.system)
        cluster.read([])
        cluster.load.see([{"name": "w", "allocatable_millicpu": 2000, "requested_millicpu": 200}], cluster.busy)
        cluster.load.see([{"name": "w", "allocatable_millicpu": 2000, "requested_millicpu": 700}], {"w"})
        self.assertEqual(cluster.load.system["w"], 200)
        occ, _ = cluster.load.record("disk", "w", 30, 500, 500)
        self.assertAlmostEqual(occ, (200 + 500 + 500) / 2000)


if __name__ == "__main__":
    unittest.main()
