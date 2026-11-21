"""Ground navigation for the organizer's virtual arena, in game coordinates."""
from array import array
from collections import deque
import heapq
import json
import math
from pathlib import Path
import sys

CELL = 4.0
STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))


def row_intervals(rings, y):
    intersections = []
    for ring in rings:
        ax, ay = ring[-2:]
        for bx, by in zip(ring[::2], ring[1::2]):
            if (by > y) != (ay > y):
                intersections.append(ax + (y - ay) * (bx - ax) / (by - ay))
            ax, ay = bx, by
    intersections.sort()
    return zip(intersections[::2], intersections[1::2])


class Grid:
    def __init__(self, world, ground, stats):
        b = world["bounds"]
        self.x, self.y = b["minX"], b["minY"]
        self.w = int((b["maxX"] - self.x) / CELL) + 1
        self.h = int((b["maxY"] - self.y) / CELL) + 1
        size = self.w * self.h
        self.clear = bytearray([255]) * size
        self.floor = array("f", (ground(*self.xy(i)) for i in range(size)))
        self.overhead = array("f", [float("inf")]) * size
        self.slope = math.tan(math.radians(stats["max_slope_deg"]))
        for prism in world["prisms"]:
            rings = [prism["ring"]] + prism.get("holes", [])
            low = max(0, math.floor((min(rings[0][1::2]) - self.y) / CELL))
            high = min(self.h - 1, math.ceil((max(rings[0][1::2]) - self.y) / CELL))
            for j in range(low, high + 1):
                # Sample the strip at both edges and the center so a thin
                # diagonal prism does not disappear between rows.
                for offset in (-0.45, 0, 0.45):
                    for left, right in row_intervals(rings, self.y + (j + offset) * CELL):
                        start = max(0, math.ceil((left - self.x) / CELL - 0.49))
                        end = min(self.w - 1, math.floor((right - self.x) / CELL + 0.49))
                        for x in range(start, end + 1):
                            i = j * self.w + x
                            if prism["top"] <= self.floor[i] + stats["step_up"]:
                                continue
                            gap = prism["base"] - self.floor[i]
                            if gap <= stats["height"]:
                                self.clear[i] = 0
                            else:
                                self.overhead[i] = min(self.overhead[i], gap)
        self.overhead_limits = [(i, limit) for i, limit in enumerate(self.overhead) if math.isfinite(limit)]
        self.cached_footprint = None
        self.cached_walkable = None
        queue = deque()
        for i in range(size):
            x, y = i % self.w, i // self.w
            if x in (0, self.w - 1) or y in (0, self.h - 1):
                self.clear[i] = 0
            if not self.clear[i]:
                queue.append(i)
        # Chebyshev clearance is conservative near diagonal corners.
        while queue:
            i = queue.popleft()
            level = self.clear[i] + 1
            if level > 12:
                continue
            x, y = i % self.w, i // self.w
            for dx, dy in STEPS:
                if 0 <= x + dx < self.w and 0 <= y + dy < self.h:
                    nxt = i + dx + dy * self.w
                    if self.clear[nxt] > level:
                        self.clear[nxt] = level
                        queue.append(nxt)

    @classmethod
    def load(cls, hello):
        root = Path(hello["map"]["dir"]) / "v2"
        world = json.loads((root / "world.json").read_bytes())
        terrain = world["terrain"]
        heights = array("h")
        heights.frombytes((root / terrain["file"]).read_bytes())
        if sys.byteorder != "little":
            heights.byteswap()

        def ground(x, y):
            u = (x - terrain["minX"]) / terrain["step"]
            v = (y - terrain["minY"]) / terrain["step"]
            a = min(terrain["nx"] - 2, max(0, int(u)))
            b = min(terrain["ny"] - 2, max(0, int(v)))
            u, v = u - a, v - b
            i = b * terrain["nx"] + a
            j = i + terrain["nx"]
            return terrain["scale"] * ((1-v)*((1-u)*heights[i]+u*heights[i+1]) + v*((1-u)*heights[j]+u*heights[j+1]))

        return cls(world, ground, hello["config"]["mech"])

    def xy(self, index):
        return self.x + index % self.w * CELL, self.y + index // self.w * CELL

    def index(self, pos):
        x = max(0, min(self.w - 1, round((pos[0] - self.x) / CELL)))
        y = max(0, min(self.h - 1, round((pos[1] - self.y) / CELL)))
        return y * self.w + x

    def open(self, i, radius, height):
        return self.clear[i] * CELL >= radius + CELL / 2 and self.overhead[i] > height

    def walkable_mask(self, radius, height):
        clearance = math.ceil((radius + CELL / 2) / CELL)
        key = clearance, height
        if key != self.cached_footprint:
            eligible = bytes(int(i >= clearance) for i in range(256))
            mask = self.clear.translate(eligible)
            for i, limit in self.overhead_limits:
                if limit <= height:
                    mask[i] = 0
            self.cached_walkable = mask
            self.cached_footprint = key
        return self.cached_walkable

    def nearest(self, pos, radius, height):
        i = self.index(pos)
        if self.open(i, radius, height):
            return i
        x, y = i % self.w, i // self.w
        for r in range(1, 20):
            options = []
            for dx in range(-r, r + 1):
                for dy in (range(-r, r + 1) if abs(dx) == r else (-r, r)):
                    if 0 <= x + dx < self.w and 0 <= y + dy < self.h:
                        j = i + dx + dy * self.w
                        if self.open(j, radius, height):
                            options.append(j)
            if options:
                return min(options, key=lambda j: math.dist(pos[:2], self.xy(j)))
        return None


class Navigator:
    def __init__(self, grid):
        self.grid = grid
        self.key = None
        self.path = deque()
        self.frontier = []
        self.start = None
        self.best = None

    def step(self, pos, goal, radius, height):
        g = self.grid
        target = g.nearest(goal, radius, height)
        start = g.nearest(pos, radius, height)
        if start is None or target is None:
            return goal
        key = target, math.ceil((radius + CELL / 2) / CELL), round(height, 2)
        if self.key != key or (self.path and math.dist(pos[:2], g.xy(self.path[0])) > CELL * 4):
            self.key = key
            self.start = start
            self.parents = {start: None}
            self.cost = {start: 0}
            self.frontier = [(0, 0, start)]
            self.path.clear()
            self.best = (math.dist(g.xy(start), g.xy(target)), start)
        tx, ty = target % g.w, target // g.w
        walkable = g.walkable_mask(radius, height)
        for _ in range(350):
            if not self.frontier:
                break
            _, cost, i = heapq.heappop(self.frontier)
            if self.cost.get(i) != cost:
                continue
            estimate_to_goal = math.hypot(i % g.w - tx, i // g.w - ty) * CELL
            if estimate_to_goal < self.best[0]:
                self.best = estimate_to_goal, i
            if i == target:
                self.frontier.clear()
                break
            for dx, dy in STEPS:
                j = i + dx + dy * g.w
                if not walkable[j]:
                    continue
                if dx and dy and (not walkable[i + dx] or not walkable[i + dy * g.w]):
                    continue
                length = 1.4142 if dx and dy else 1
                if abs(g.floor[j] - g.floor[i]) > g.slope * CELL * length:
                    continue
                new_cost = cost + length
                if new_cost < self.cost.get(j, math.inf):
                    self.cost[j] = new_cost
                    self.parents[j] = i
                    x, y = abs(j % g.w - tx), abs(j // g.w - ty)
                    estimate = max(x, y) + 0.4142 * min(x, y)
                    heapq.heappush(self.frontier, (new_cost + estimate * 1.14, new_cost, j))
        if not self.frontier and self.best is not None:
            # An enclosed goal may be unreachable. Stop on the closest
            # reachable side instead of walking repeatedly into its wall.
            self.endpoint = self.best[1]
            i = self.endpoint
            while i != self.start:
                self.path.appendleft(i)
                i = self.parents[i]
            self.best = None
        while self.path and math.dist(pos[:2], g.xy(self.path[0])) < CELL * 0.64:
            self.path.popleft()
        if self.path:
            return g.xy(self.path[0])
        if self.frontier:
            return pos[:2]
        if self.endpoint != target or g.index(goal) != target:
            return g.xy(self.endpoint)
        return goal
