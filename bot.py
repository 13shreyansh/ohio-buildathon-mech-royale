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
        self.explore_origin = None
        self.explore_until = 0.0
        self.explore_index = 0
        self.crate_id = None
        self.crate_deadline = 0.0
        self.skip_crate = {}

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
            self.ring_destination = (center[0] + dx * radius, center[1] + dy * radius)
            self.phase = phase
        safe_radius = max(0, zone["next_radius"] * 0.78 - me["stats"]["radius"])
        seeking = me["cubes"] == 0 or None in me["weapons"]
        if (not seeking or obs["t"] >= self.config["zone"]["phases"][0]["hold_s"]
                or distance(pos, center) > safe_radius + 3 or me["hp"] < 0.85 * me["max_hp"]
                or obs["drops"] or any(m["kind"] == "mech" for m in obs["mechs"])):
            self.anchor = self.ring_destination
            self.explore_until = 0.0
            return self.anchor
        if self.explore_origin is None:
            self.explore_origin = tuple(pos[:2])
        if obs["t"] >= self.explore_until:
            # A short triangle around the first safe tile exposes unseen floor
            # items while keeping every waypoint within the next game circle.
            angle = me["id"] * 2.4 + self.explore_index * math.tau / 3
            span = min(65, safe_radius * 0.9)
            x = self.explore_origin[0] + math.cos(angle) * span - center[0]
            y = self.explore_origin[1] + math.sin(angle) * span - center[1]
            scale = min(1.0, safe_radius / max(0.01, math.hypot(x, y)))
            self.anchor = (center[0] + x * scale, center[1] + y * scale)
            self.explore_index += 1
            self.explore_until = obs["t"] + 7.0
        return self.anchor

    def public_loot_reach(self, obs, item, base_reach):
        me, zone = obs["self"], obs["zone"]
        if obs["t"] >= zone["shrink_start"] or me["hp"] < me["max_hp"] * 0.82:
            return base_reach
        if me["cubes"] > 0 and None not in me["weapons"]:
            return base_reach
        pos, p = me["pos"], item["pos"]
        # Newly unlocked detours need a clear resource advantage. Preserve
        # baseline behavior for the nearer items it already considered.
        if distance(pos, p) < base_reach:
            return base_reach
        if any(m["kind"] == "mech" and min(distance(m["pos"], pos), distance(m["pos"], p)) < 75 for m in obs["mechs"]):
            return base_reach
        # A single nearby crate already promises a cube and may drop gear.
        # Do not walk past it for a distant loose item on an empty map.
        walking_budget = 7.0 * me["stats"]["walk_speed"]
        for crate in obs["crates"]:
            if abs(crate["pos"][2] - pos[2]) < 4:
                walking_budget = min(walking_budget, distance(pos, crate["pos"]) + 2.0 * me["stats"]["walk_speed"])
        return max(base_reach, walking_budget)

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
            if d < self.public_loot_reach(obs, cube, 32) and can_visit(p, d):
                choices.append((d, cube["id"], p))
        held = {w["kind"] for w in me["weapons"] if w}
        if None in me["weapons"]:
            for item in obs["pickups"]:
                p = item["pos"]
                d = distance(pos, p)
                if item["kind"] in ("laser", "lightning") and item["kind"] not in held and d < self.public_loot_reach(obs, item, 42) and can_visit(p, d):
                    choices.append((d - 9, item["id"], p))
        for drop in obs["drops"]:
            p = drop["pos"]
            d = distance(pos, p)
            if drop.get("holder") not in (None, me["id"]) or abs(pos[2] - p[2]) > 4:
                continue
            if distance(p, zone["next_center"]) + drop["radius"] >= zone["next_radius"]:
                continue
            travel = max(0, d - drop["radius"]) / speed
            # Announced drops can justify a longer trip before landing. Once a
            # drop has landed, limit the detour to a shorter collection window.
            if travel > (14 if drop["lands_in"] > 0 else 9):
                continue
            if drop["lands_in"] > 1.4 * travel + 4:
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

    def crate_goal(self, obs, goal):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        pos = me["pos"]
        if goal != self.anchor or not me["in_zone"] or me["hp"] < 0.82 * me["max_hp"]:
            self.crate_id = None
            return None
        to_ring = max(0, distance(pos, zone["next_center"]) - zone["next_radius"] + me["stats"]["radius"])
        if to_ring > 0 and to_ring * 1.5 / me["stats"]["walk_speed"] + 6 > zone["shrink_end"] - now:
            self.crate_id = None
            return None
        if any(m["kind"] == "mech" for m in obs["mechs"]):
            self.crate_id = None
            return None
        if self.crate_id is not None and now > self.crate_deadline:
            self.skip_crate[self.crate_id] = now + 9
            self.crate_id = None
        options = []
        for crate in obs["crates"]:
            p = crate["pos"]
            d = distance(pos, p)
            budget = 8 if me["cubes"] == 0 and now < zone["shrink_start"] else 4
            grid = self.navigator.grid
            floor_item = abs(p[2] - grid.floor[grid.index(p)]) <= 3
            same_level = abs(p[2] - pos[2]) <= 3
            if d > budget * me["stats"]["walk_speed"] or not (floor_item or same_level) or self.skip_crate.get(crate["id"], -1) > now:
                continue
            if distance(p, zone["center"]) > zone["radius"] - 12:
                continue
            outside = max(0, distance(p, zone["next_center"]) - zone["next_radius"] + 10)
            if outside and (d + outside) * 1.5 / me["stats"]["walk_speed"] + 7 >= zone["shrink_end"] - now:
                continue
            options.append((0 if crate["id"] == self.crate_id else 1, d, crate["id"], crate))
        if not options:
            self.crate_id = None
            return None
        _, d, ident, crate = min(options)
        if self.crate_id != ident:
            self.crate_id = ident
            self.crate_deadline = now + 4 + d / me["stats"]["walk_speed"]
        if any(e["type"] == "hit" and e.get("target") == ident for e in obs["events"]):
            self.crate_deadline = now + 4
        # The crate disappears from the game stream when opened. On the
        # following decision the original loose-cube policy takes over.
        return crate

    def collect_crate(self, obs, crate):
        me, p = obs["self"], crate["pos"]
        pos = me["pos"]
        d = distance(pos, p)
        dx, dy = unit(pos[0] - p[0], pos[1] - p[1])
        if not dx and not dy:
            dx = 1
        if 12 <= d <= 21:
            move = (0, 0)
        else:
            stance = (p[0] + dx * 16, p[1] + dy * 16)
            zone = obs["zone"]
            if distance(stance, zone["center"]) > zone["radius"] - 4:
                return None
            move = self.movement(obs, stance)
        action = self.actions(obs, move)
        yaw = math.atan2(p[1] - pos[1], p[0] - pos[0])
        pitch = math.atan2(p[2] + self.config["crates"]["height"] / 2 - pos[2] - me["stats"]["muzzle_height"], d)
        action["aim"] = (yaw, pitch)
        # The loose-cube collector searches 32 game units. Open the crate
        # inside that radius so the transition does not strand its reward.
        ready = d <= 30 and abs(math.remainder(yaw - me["aim"][0], math.tau)) < 0.12 and abs(pitch - me["aim"][1]) < 0.1
        action["fire"] = [bool(w and w["kind"] == "gun" and w["ammo"] > 0 and w["reload"] == 0 and ready) for w in me["weapons"]]
        action["reload"] = any(w and w["kind"] == "gun" and w["ammo"] == 0 for w in me["weapons"])
        return action

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

    def actions(self, obs, move):
        me = obs["self"]
        pos = me["pos"]
        action = {"tick": obs["tick"], "move": move, "boost": not me["in_zone"] and me["grounded"] and me["energy"] > 25}
        visible = [p for p in obs["mechs"] if p["kind"] == "mech"]
        if visible:
            target = min(visible, key=lambda item: distance(pos, item["pos"]))
            p = target["pos"]
            yaw = math.atan2(p[1] - pos[1], p[0] - pos[0])
            avatar_height = self.config["mech"]["height"] * (1 + self.config["cubes"]["size_per_cube"] * target.get("cubes", 0))
            pitch = math.atan2(p[2] + avatar_height / 2 - pos[2] - me["stats"]["muzzle_height"], distance(pos, p))
            action["aim"] = [yaw, pitch]
            action["fire"] = self.slots(obs, target, yaw, pitch)
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
        if self.rest is None:
            crate = self.crate_goal(obs, goal)
            if crate:
                gathering = self.collect_crate(obs, crate)
                if gathering is not None:
                    return gathering
        if tuple(goal[:2]) == tuple(obs["self"]["pos"][:2]):
            recovery = goal
            self.rest = None
        else:
            recovery = self.recovery_goal(obs, goal)
        action = self.actions(obs, self.movement(obs, recovery))
        if self.rest is not None:
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
