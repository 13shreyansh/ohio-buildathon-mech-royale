# Ringwalker

Ringwalker is an original Python entry for the Builders Mech Royale contest. It walks toward a point inside the next ring and uses the mounted game map to route around solids. Nearby cubes, eligible pickups and uncontested supply markers can change its destination. The bot budgets travel time back into the ring. At low health it can pause to regenerate, then move on if its health stays flat.

Build the root Dockerfile with `docker build --platform linux/amd64 -t ringwalker .`. Run it with standard input attached and the organizer's arena mounted read-only at `/arena`. It reads mechfight/1 JSON lines and writes one action for the newest queued observation. For a native run, use `python3 -B -u bot.py` and pass the arena path in the hello message as documented by the organizer.

The client and strategy were written for this event with Codex assistance. The runtime uses the Python standard library and the official Python container image pinned in the Dockerfile. The organizer's protocol and checker informed development. Map files and recordings are supplied separately by the organizer.

The entry code is released under the MIT license in LICENSE.
