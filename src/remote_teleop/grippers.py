"""Independent soft pickup for passive leader grippers, in metres."""
from .control import vector


class GripperTakeover:
    """Hold the follower opening until the handle reaches/crosses it, then map 1:1.

    A constant relative offset would translate the handle's whole stroke beyond
    one end of the follower's travel. Grippers must not inherit arm-joint offsets.
    """
    tolerance_m = 0.005

    def __init__(self, leader, follower, minimum_m=0.0, maximum_m=0.085):
        leader, follower = vector(leader), vector(follower)
        self.items = []
        for arm, i in (("left", 6), ("right", 13)):
            pickup = max(minimum_m, min(maximum_m, follower[i]))
            self.items.append({"arm": arm, "index": i, "target": follower[i],
                               # Only the pickup point is projected onto handle
                               # travel; absolute commands are never clipped here.
                               "pickup": pickup,
                               "armed": abs(leader[i] - pickup) > self.tolerance_m,
                               "previous": leader[i], "leader": leader[i],
                               "active": abs(leader[i] - follower[i]) <= self.tolerance_m})

    def apply(self, pose):
        pose = vector(pose)
        result = []
        for item in self.items:
            value, target, pickup = pose[item["index"]], item["target"], item["pickup"]
            if abs(value - pickup) > self.tolerance_m:
                item["armed"] = True
            if item["armed"] and (abs(value - pickup) <= self.tolerance_m
                    or (item["previous"] - pickup) * (value - pickup) <= 0):
                item["active"] = True
            item["previous"] = item["leader"] = value
            result.append(value if item["active"] else target)
        return result

    def status(self):
        return [{"arm": item["arm"], "state": "active" if item["active"] else "waiting_for_match",
                 "leader_m": item["leader"], "target_m": item["target"],
                 "pickup_m": item["pickup"],
                 "needs_reentry": not item["active"] and not item["armed"],
                 "tolerance_m": self.tolerance_m} for item in self.items]
