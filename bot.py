"""A game client for the mechfight/1 JSON-lines protocol."""
import json
import math
import os
import select
import sys

from navigation import Grid, Navigator


def unit(x, y):
    length = math.hypot(x, y)
    return (x / length, y / length) if length > 0.01 else (0.0, 0.0)


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Bot:
    name = "Ringwalker"

    def __init__(self, hello):
        self.config = hello["config"]
        self.navigator = Navigator(Grid.load(hello))
        self.anchor = None
        self.phase = None
        self.rest = None
        self.rest_phase = None
        self.retry_repair = 0.0
        self.repair_started = 0.0
        self.repair_hp = 0.0
        self.previous_ids = set()
        self.contact_until = {}
        self.next_probe = {}
        self.focus_id = None
        self.withdraw_until = -1.0
        self.escape = None
        self.escape_phase = None
        self.escape_refresh = -1.0
        match = self.config["match"]
        self.frame = match["decide_every"] / match["tick_hz"]

    def zone_goal(self, obs):
        me = obs["self"]
        pos = me["pos"]
        zone = obs["zone"]
        center = zone["next_center"]
        phase = (*center, zone["next_radius"])
        if phase != self.phase:
            # Stop inside the next ring on the spawn side. Crowding the center
            # early gives the bot little room to collect nearby upgrades.
            dx, dy = unit(pos[0] - center[0], pos[1] - center[1])
            radius = max(0, zone["next_radius"] * 0.78 - me["stats"]["radius"])
            self.anchor = (center[0] + dx * radius, center[1] + dy * radius)
            self.phase = phase
        return self.anchor

    def objective(self, obs):
        me = obs["self"]
        pos = me["pos"]
        zone = obs["zone"]
        goal = self.zone_goal(obs)
        if not me["in_zone"]:
            return goal
        inset = max(0, zone["next_radius"] - me["stats"]["radius"])
        to_safety = max(0, distance(pos, zone["next_center"]) - inset)
        time_left = zone["shrink_end"] - obs["t"]
        speed = me["stats"]["walk_speed"]
        if to_safety > 0 and to_safety / speed * 1.5 + 6 > time_left:
            return goal
        def can_visit(p, d):
            if abs(pos[2] - p[2]) > 4 or distance(p, zone["center"]) >= zone["radius"] - 3:
                return False
            return_distance = max(0, distance(p, zone["next_center"]) - inset)
            if return_distance == 0:
                return True
            return (d + return_distance) * 1.5 / speed + 4 < time_left

        choices = []
        for cube in obs["cubes"]:
            p = cube["pos"]
            d = distance(pos, p)
            if d < 32 and can_visit(p, d):
                choices.append((d, cube["id"], p))
        held = {w["kind"] for w in me["weapons"] if w}
        if None in me["weapons"]:
            for item in obs["pickups"]:
                p = item["pos"]
                d = distance(pos, p)
                if item["kind"] in ("laser", "lightning") and item["kind"] not in held and d < 42 and can_visit(p, d):
                    choices.append((d - 9, item["id"], p))
        for drop in obs["drops"]:
            p = drop["pos"]
            d = distance(pos, p)
            if drop.get("holder") not in (None, me["id"]) or abs(pos[2] - p[2]) > 4:
                continue
            if d > 55 or distance(p, zone["next_center"]) + drop["radius"] >= zone["next_radius"]:
                continue
            if drop["lands_in"] > max(0, d - drop["radius"]) / speed + 1:
                continue
            if any(m["kind"] == "mech" and distance(p, m["pos"]) < drop["radius"] + 15 for m in obs["mechs"]):
                continue
            value = 28 + 30 * (1 - me["hp"] / me["max_hp"])
            if d < drop["radius"] - 1:
                return pos[:2]
            choices.append((d - value, drop["id"], p))
        if choices:
            return min(choices)[2]
        return goal

    def recovery_goal(self, obs, goal):
        me = obs["self"]
        pos = me["pos"]
        zone = obs["zone"]
        margin = zone["next_radius"] - distance(pos, zone["next_center"])
        if me["hp"] >= me["max_hp"] * 0.85 or not me["in_zone"] or margin < 14:
            self.rest = None
            return goal
        if obs["t"] < self.retry_repair:
            return goal
        if self.rest is not None and obs["t"] - self.repair_started > me["stats"]["regen_delay"] + 5:
            if me["hp"] <= self.repair_hp:
                self.rest = None
                self.retry_repair = obs["t"] + 7
                return goal
            self.repair_started, self.repair_hp = obs["t"], me["hp"]
        if self.rest is None and me["hp"] > me["max_hp"] * 0.58:
            return goal
        grid = self.navigator.grid
        if self.rest is None or self.rest_phase != self.phase:
            # A repair stop is a game tile next to a solid, with enough
            # clearance to stand in. Only look for stops inside the next ring.
            i = grid.index(pos)
            sx, sy = i % grid.w, i // grid.w
            best = None
            for y in range(max(1, sy-8), min(grid.h-1, sy+9)):
                for x in range(max(1, sx-8), min(grid.w-1, sx+9)):
                    j = y * grid.w + x
                    if not grid.open(j, me["stats"]["radius"], me["stats"]["height"]):
                        continue
                    point = grid.xy(j)
                    if distance(point, zone["next_center"]) > zone["next_radius"] - 8:
                        continue
                    cost = distance(pos, point) + 7 * min(8, grid.clear[j])
                    if best is None or cost < best[0]:
                        best = cost, point
            self.rest = best[1] if best else pos[:2]
            self.rest_phase = self.phase
            self.repair_started, self.repair_hp = obs["t"], me["hp"]
        return self.rest

    def movement(self, obs, goal):
        pos = obs["self"]["pos"]
        stats = obs["self"]["stats"]
        goal = self.navigator.step(pos, goal, stats["radius"], stats["height"])
        if distance(pos, goal) < 1:
            return (0.0, 0.0)
        return unit(goal[0] - pos[0], goal[1] - pos[1])

    def encounter(self, obs):
        me, now = obs["self"], obs["t"]
        pos = me["pos"]
        players = [avatar for avatar in obs["mechs"] if avatar["kind"] == "mech"]
        ids = {a["id"] for a in players}
        # The server keeps publishing a mech for five seconds after it was
        # last seen. A newly published id and game hit events are stronger
        # evidence than continued presence in that list.
        for actor in ids - self.previous_ids:
            self.contact_until[actor] = now + 0.75
            self.next_probe[actor] = now + 1.5
        for event in obs["events"]:
            if event["type"] == "hit" and event.get("target") in ids:
                self.contact_until[event["target"]] = now + 0.6
        self.previous_ids = ids
        near = self.config["vision"]["near_range"] + me["stats"].get("near_range_bonus", 0)
        pressure = [a for a in players if distance(pos, a["pos"]) < 2.8 * near]
        if len(pressure) >= 2 or (pressure and me["hp"] < 0.60 * me["max_hp"]):
            self.withdraw_until = now + 1.6
        withdrawing = now < self.withdraw_until
        candidates = []
        for a in players:
            dx, dy = a["pos"][0] - pos[0], a["pos"][1] - pos[1]
            d = math.hypot(dx, dy)
            yaw = math.atan2(dy, dx)
            vision = self.config["vision"]
            if d > vision["range"] + me["stats"].get("vision_bonus", 0):
                continue
            if d > near and abs(math.remainder(yaw - me["aim"][0], math.tau)) > math.radians(vision["fov_deg"] / 2):
                continue
            candidates.append(a)
        # Keep the same avatar selected through small range-order changes.
        # Prefer contacts with recent server evidence when switching.
        viable = [a for a in candidates if now <= self.contact_until.get(a["id"], -1)]
        pool = viable or candidates
        target = next((a for a in pool if a["id"] == self.focus_id), None)
        if target is None and pool:
            target = min(pool, key=lambda a: (distance(pos, a["pos"]), a["id"]))
        self.focus_id = target["id"] if target else None
        return target, pressure, withdrawing

    def withdraw_goal(self, obs, pressure):
        me, zone = obs["self"], obs["zone"]
        pos, now = me["pos"], obs["t"]
        radius = me["stats"]["radius"]
        inset = max(0, zone["next_radius"] - radius)
        remaining = max(0, distance(pos, zone["next_center"]) - inset)
        urgent = remaining > 0 and remaining * 1.5 / me["stats"]["walk_speed"] + 6 > zone["shrink_end"] - now
        if not me["in_zone"] or urgent:
            self.escape = None
            return self.zone_goal(obs)
        if self.escape_phase == self.phase and self.escape is not None and now < self.escape_refresh:
            return self.escape
        if not pressure:
            return self.zone_goal(obs)
        grid = self.navigator.grid
        choices = []
        for i in range(16):
            angle = i * math.tau / 16
            point = (pos[0] + 44 * math.cos(angle), pos[1] + 44 * math.sin(angle))
            j = grid.index(point)
            point = grid.xy(j)
            if not grid.open(j, radius, me["stats"]["height"]):
                continue
            if distance(point, zone["center"]) > zone["radius"] - radius:
                continue
            if distance(point, zone["next_center"]) > inset + remaining:
                continue
            # Only consider withdrawal moves with a walkable approach. A goal
            # on the other side of a wall can send A* outside the game ring.
            steps = max(1, math.ceil(distance(pos, point) / 2))
            if any(not grid.open(grid.index((pos[0] + (point[0]-pos[0])*k/steps,
                                             pos[1] + (point[1]-pos[1])*k/steps)),
                                 radius, me["stats"]["height"]) for k in range(1, steps+1)):
                continue
            separation = min(distance(point, a["pos"]) for a in pressure)
            choices.append((separation - 0.12 * distance(point, self.anchor), point))
        self.escape = max(choices)[1] if choices else self.zone_goal(obs)
        self.escape_phase = self.phase
        self.escape_refresh = now + 1.6
        return self.escape

    def slots(self, obs, target, yaw, pitch):
        me = obs["self"]
        d = math.dist(target["pos"], me["pos"])
        facing = abs(math.remainder(yaw - me["aim"][0], math.tau))
        held = []
        for item in me["weapons"]:
            trigger = False
            if item and item["cooldown"] <= 0.1:
                stats = item["stats"]
                kind = item["kind"]
                if kind == "gun":
                    trigger = item["ammo"] > 0 and item["reload"] <= 0 and d < stats["range"] and facing < 0.24 and abs(pitch - me["aim"][1]) < 0.24
                elif kind == "laser":
                    # In this game True charges the slot, False releases it.
                    trigger = d < stats["range"] and item["charge"] < stats["charge"] and facing < 0.35
                elif kind == "lightning":
                    reserve = 20 if me["in_zone"] else 45
                    trigger = d < stats["range"] and facing < math.radians(stats["cone_deg"]) and me["energy"] >= reserve + stats["energy"]
            held.append(trigger)
        return held

    def actions(self, obs, move, target):
        me = obs["self"]
        pos = me["pos"]
        action = {"tick": obs["tick"], "move": move, "boost": not me["in_zone"] and me["grounded"] and me["energy"] > 25}
        if target:
            p = target["pos"]
            # For the default slot, use the avatar position one game decision
            # ahead. Instant slots use the current animation frame.
            if not any(w and w["kind"] in ("laser", "lightning") and w["cooldown"] <= self.frame for w in me["weapons"]):
                vel = target.get("vel", (0, 0, 0))
                p = [v + self.frame * dv for v, dv in zip(p, vel)]
            yaw = math.atan2(p[1] - pos[1], p[0] - pos[0])
            avatar_height = self.config["mech"]["height"] * (1 + self.config["cubes"]["size_per_cube"] * target.get("cubes", 0))
            pitch = math.atan2(p[2] + avatar_height / 2 - pos[2] - me["stats"]["muzzle_height"], distance(pos, p))
            action["aim"] = [yaw, pitch]
            fire = self.slots(obs, target, yaw, pitch)
            actor, now = target["id"], obs["t"]
            if now > self.contact_until.get(actor, -1):
                # A visible-list entry can be memory only. Permit a single
                # basic-slot check, then require a hit event to resume.
                probe = now >= self.next_probe.get(actor, 0)
                fire = [bool(f and probe and w and w["kind"] == "gun") for f, w in zip(fire, me["weapons"])]
                if any(fire):
                    self.next_probe[actor] = now + 1.6
            action["fire"] = fire
        else:
            action["aim"] = [obs["t"] * 0.9 % math.tau - math.pi, 0.0]
            action["reload"] = any(w and w["kind"] == "gun" and w["ammo"] < w["stats"]["mag"] * 0.8 for w in me["weapons"])
        if None in me["weapons"]:
            reach = self.config["loot"]["pickup_radius"]
            equipped = {w["kind"] for w in me["weapons"] if w}
            nearby = [p for p in obs["pickups"] if p["kind"] in ("laser", "lightning") and p["kind"] not in equipped and distance(pos, p["pos"]) <= reach and abs(p["pos"][2] - pos[2]) <= 4]
            if nearby:
                action["take"] = min(nearby, key=lambda p: (distance(pos, p["pos"]), p["id"]))["id"]
        if obs["offer"]:
            order = ["servos", "plating", "armor", "nanites", "reactor", "gun_rate"]
            choices = obs["offer"]["choices"]
            action["pick"] = min(range(len(choices)), key=lambda i: order.index(choices[i]["id"]) if choices[i]["id"] in order else len(order))
        return action

    def act(self, obs):
        goal = self.objective(obs)
        target, pressure, withdrawing = self.encounter(obs)
        if withdrawing:
            self.rest = None
            recovery = self.withdraw_goal(obs, pressure)
        elif tuple(goal[:2]) == tuple(obs["self"]["pos"][:2]):
            recovery = goal
            self.rest = None
        else:
            recovery = self.recovery_goal(obs, goal)
        action = self.actions(obs, self.movement(obs, recovery), target)
        if withdrawing:
            me = obs["self"]
            action["boost"] = me["grounded"] and me["energy"] > 25 and any(action["move"])
        if self.rest is not None or withdrawing:
            action["fire"] = [False, False]
        return action


def run(bot_class):
    fd = sys.stdin.fileno()
    pending = b""
    bot = None
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return
        pending += chunk
        while select.select([fd], [], [], 0)[0]:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        newest = None
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            if event["type"] == "hello":
                bot = bot_class(event)
                print(json.dumps({"type": "ready", "name": bot.name}), flush=True)
            elif event["type"] == "obs":
                newest = event
            elif event["type"] == "end":
                return
        if newest is not None:
            print(json.dumps(bot.act(newest), separators=(",", ":"), allow_nan=False), flush=True)


if __name__ == "__main__":
    run(Bot)
