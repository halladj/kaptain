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


class Learning(unittest.TestCase):
    def cluster(self):
        cluster = Quiet(load=k.Load(learn=True))
        cluster.load.see([{"name": "w", "allocatable_millicpu": 2000, "requested_millicpu": 0}])
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


if __name__ == "__main__":
    unittest.main()
