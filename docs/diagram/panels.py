"""Add the side panels to the generated request-flow diagram.

    python3 panels.py architecture-core.excalidraw architecture.excalidraw

Reads the core diagram written by the Excalidraw plugin's generate.mjs and
tidies it in place: every text in the plain font (fontFamily 2), a few boxes
moved (LAYOUT) with every arrow redrawn between its boxes, each arrow label
placed clear of other arrows (LABELS; render.mjs adds a halo), the auth-to-store
arrow drawn as an elbow around the membership box, and every element's
random fields set to stable values. Then it writes the full board: the core
plus the panels below. Both files are therefore byte-identical across rebuilds of
unchanged sources. Each panel title starts with the key
of the README section that justifies it (T, E, D1 to D9, L, S, G).
Keep every line here true of the code; the README carries the reasons.
"""

import copy
import json
import sys
import zlib
from pathlib import Path

# (title, stroke colour, fill colour, body lines)
# T is the same list, word for word, as the README's "T. Targets" and DESIGN.md's non-functional requirements.
LEFT = [
    (
        "T  Targets",
        "#1971c2",
        "#e7f5ff",
        [
            "About 20 teams of up to 50 people (D4)",
            "Up to 10,000 notes per team, about 2 KB each (D7)",
            "A list page or a search in under 200 ms (D7)",
            "About 5 writes/s: 100 editors renewing every 20 s (D5)",
            "Viewers hear of a save, lock or unlock within 1 s (D4)",
            "A waiting viewer holds no thread or connection (D4, D6)",
            "Outsiders learn nothing about a team (D1)",
            "No edit lost silently (D2, D3)",
            "A crashed editor blocks a note at most 60 s (D2)",
            "Membership and edits read fresh; lists may lag (G)",
            "One command to run, one to test",
        ],
    ),
    (
        "E  Entities",
        "#e8590c",
        "#fff4e6",
        [
            "User, Session (only the token's hash is stored)",
            "Team, Membership (team, user, admin | member)",
            "Note (team, author, title, body, version)",
            "Edit lock: columns on Note, holder, expiry, activity",
            "Tag: version + live holder; viewers wait on it",
        ],
    ),
    (
        "L  Limits",
        "#495057",
        "#f1f3f5",
        [
            "Title 200 chars, body 100,000 chars, request 1 MB",
            "200 members and 10,000 notes per team",
            "Page 1 to 100 (cursor paging), q up to 200",
            "Lock lease 60 s, renew every 20 s, idle 10 min",
            "Long poll up to 25 s, 10 held per user",
            "Token lasts 7 days",
            "Write waits 5 s for a lock, then 503 busy",
        ],
    ),
]
RIGHT = [
    (
        "D1 to D9  Decisions",
        "#7048e8",
        "#f3f0ff",
        [
            "D1 Isolation: one membership check, 404 outside",
            "D2 Edit lock: 60 s lease, 10 min idle, If-Match",
            "D3 Each guarded write: one conditional statement",
            "D4 Long poll woken by an in-memory notifier",
            "D5 One store: SQLite, or Postgres as its own service",
            "D6 40 connections = 40 threads, one per hop",
            "D7 Cursor-paged lists; search scans one team",
            "D8 Sync handlers on 40 threads, one async route",
            "D9 Opaque bearer tokens, argon2 passwords",
        ],
    ),
    (
        "S  Scope cuts",
        "#e03131",
        "#fff5f5",
        [
            "Concurrent editing: one editor at a time instead",
            "Content push to viewers, presence, cursors",
            "Rich text, attachments, trash, history, tags",
            "Private notes, per-note sharing, invitations",
            "Role changes, renaming or deleting teams",
            "Logout, password reset, SSO, rate limiting",
            "Idempotency keys, migrations, metrics",
        ],
    ),
    (
        "G  Growth path (not built)",
        "#2f9e44",
        "#ebfbee",
        [
            "1. Several processes: LISTEN/NOTIFY to each notifier",
            "2. Search too slow: FTS5 or tsvector, same database",
            "3. Reads outgrow a machine: replicas for lists",
            "4. A measured hot read: cache, never membership",
            "5. Data outgrows a database: shard by team_id",
        ],
    ),
]
# Box positions (top-left x, y) that override generate.mjs's layout, so the notifier sits under the
# changes route and the store under the routes that call it, with fewer crossing arrows.
LAYOUT = {
    "rect-store": (525, 964),
    "rect-notifier": (1000, 976),
    "rect-sqlite_store": (400, 1200),
    "rect-pg_store": (695, 1200),
    "rect-sqlite_db": (405, 1424),
    "rect-pg_db": (690, 1412),
}
# The rows below the routes moved down by this much (room for the labels), so the process group grows too.
GROUP_EXTRA_HEIGHT = 100
# Where an arrow meets its target's top edge, as an offset from the target's left side, for arrows that
# would otherwise share the centre point (default: the centre).
END_X = {
    "arrow-team_routes-notifier": 20,
    "arrow-note_routes-notifier": 100,
}
# Arrow labels: (fraction along the arrow, dx, dy) for the label's centre: beside its line where there is
# room, otherwise on a stretch of its line that no other arrow crosses. render.mjs draws every label with
# a halo of the background colour, so the line under it (its own) is hidden rather than striking it.
LABELS = {
    "arrow-client-auth_routes": (0.45, -78, 0),
    "arrow-client-authn": (0.7, 70, -4),
    "arrow-authn-authz": (0.5, 85, -10),
    "arrow-authn-team_routes": (0.75, -45, 0),
    "arrow-changes-notifier": (0.5, 80, 0),
    "arrow-note_routes-notifier": (0.25, 0, 0),
    "arrow-team_routes-notifier": (0.822, 0, 0),
}
# The session lookup: drawn by hand around the membership box (generate.mjs would draw it straight
# through it). x of its vertical run, in the gap between the team and note routes.
AUTH_STORE_X = 580
TITLE = "Team Notes: architecture. Keys (T, E, D1 to D9, L, S, G) are explained in the README."
WIDTH = 520
GAP = 60


def stable(element: dict) -> dict:
    """Replace the fields Excalidraw randomises or timestamps with values derived from the element's id.

    `seed` drives the hand-drawn jitter, `versionNonce` and `updated` only track edits. generate.mjs sets all three
    afresh on every run, which made every rebuild a large diff with no visible change.
    """
    digest = zlib.crc32(element["id"].encode())
    element.update(seed=digest % 2**31 or 1, versionNonce=(digest >> 1) % 2**31 or 1, updated=1)
    return element


def write(path: str, board: dict) -> None:
    """Write `board` as indented JSON with a final newline, every element made stable first."""
    for element in board["elements"]:
        stable(element)
    Path(path).write_text(json.dumps(board, indent=1) + "\n")


def tidy(elements: list[dict]) -> None:
    """Apply LAYOUT, redraw every arrow between its boxes, place LABELS, add the auth-to-store elbow."""
    by_id = {e["id"]: e for e in elements}
    for rect_id, (x, y) in LAYOUT.items():
        rect = by_id[rect_id]
        dx, dy = x - rect["x"], y - rect["y"]
        rect.update(x=x, y=y)
        for bound in rect["boundElements"]:
            if bound["type"] == "text":
                by_id[bound["id"]]["x"] += dx
                by_id[bound["id"]]["y"] += dy
    by_id["groupbox-app"]["height"] += GROUP_EXTRA_HEIGHT
    # As generate.mjs draws them (TB): from the source's bottom centre to the target's top centre.
    for arrow in [e for e in elements if e["type"] == "arrow"]:
        src, dst = by_id[arrow["startBinding"]["elementId"]], by_id[arrow["endBinding"]["elementId"]]
        x0, y0 = src["x"] + src["width"] / 2, src["y"] + src["height"]
        x1, y1 = dst["x"] + END_X.get(arrow["id"], dst["width"] / 2), dst["y"]
        arrow.update(x=x0, y=y0, points=[[0, 0], [x1 - x0, y1 - y0]], width=abs(x1 - x0), height=abs(y1 - y0))

    labels = []
    for arrow_id, (t, dx, dy) in LABELS.items():
        arrow, label = by_id[arrow_id], by_id[f"text-{arrow_id}"]
        (_, _), (px, py) = arrow["points"]
        cx, cy = arrow["x"] + px * t + dx, arrow["y"] + py * t + dy
        width, height = len(label["text"]) * label["fontSize"] * 0.56, label["fontSize"] * 1.25
        arrow["boundElements"] = []
        label.update(containerId=None, x=cx - width / 2, y=cy - height / 2, width=width, height=height)
        labels.append(label)
    authn, store = by_id["rect-authn"], by_id["rect-store"]
    elbow = copy.deepcopy(by_id["arrow-auth_routes-store"])
    x0, y0 = authn["x"], authn["y"] + authn["height"] / 2
    elbow.update(
        id="arrow-authn-store",
        x=x0,
        y=y0,
        points=[[0, 0], [AUTH_STORE_X - x0, 0], [AUTH_STORE_X - x0, store["y"] - y0]],
        width=x0 - AUTH_STORE_X,
        height=store["y"] - y0,
        startBinding={"elementId": authn["id"], "focus": 0, "gap": 1},
        endBinding={"elementId": store["id"], "focus": 0, "gap": 1},
        boundElements=[],
    )
    elbow_label = copy.deepcopy(labels[0])
    text = "session lookup"
    width = len(text) * elbow_label["fontSize"] * 0.56
    elbow_label.update(
        id="text-arrow-authn-store",
        text=text,
        originalText=text,
        x=AUTH_STORE_X - 10 - width,
        y=y0 + 110,
        width=width,
    )
    # Labels last (export.mjs draws all rectangles, then arrows, then texts, each in this order).
    for label in labels:
        elements.remove(label)
    elements.extend([elbow, *labels, elbow_label])


def main(core_path: str, full_path: str) -> None:
    board = json.loads(Path(core_path).read_text())
    elements = board["elements"]
    for element in elements:
        if element["type"] == "text":
            element["fontFamily"] = 2  # plain Helvetica-like font; render.mjs matches it in the SVG
            # Larger than generate.mjs's 16/14 px so the flow stays readable at README width.
            # The boxes were sized for 10 px per character, which leaves room for this.
            element["fontSize"] = 19 if element["containerId"] and element["containerId"].startswith("rect-") else 16
    tidy(elements)
    write(core_path, board)

    text_template = next(e for e in elements if e["type"] == "text" and e["containerId"] is None)
    rect_template = next(e for e in elements if e["type"] == "rectangle")

    def text(id_: str, x: float, y: float, s: str, size: int, color: str) -> dict:
        e = copy.deepcopy(text_template)
        lines = s.split("\n")
        e.update(
            id=id_,
            x=x,
            y=y,
            text=s,
            originalText=s,
            fontSize=size,
            strokeColor=color,
            fontFamily=2,
            width=max(len(line) for line in lines) * size * 0.56,
            height=len(lines) * size * 1.25,
            boundElements=[],
            groupIds=[],
        )
        return e

    def rect(id_: str, x: float, y: float, w: float, h: float, stroke: str, bg: str) -> dict:
        e = copy.deepcopy(rect_template)
        e.update(
            id=id_,
            x=x,
            y=y,
            width=w,
            height=h,
            strokeColor=stroke,
            backgroundColor=bg,
            boundElements=[],
            groupIds=[],
            strokeStyle="solid",
        )
        return e

    min_x = min(e["x"] for e in elements)
    max_x = max(e["x"] + e.get("width", 0) for e in elements)
    top = min(e["y"] for e in elements)
    new = [text("board-title", min_x, top - 70, TITLE, 22, "#1e1e1e")]
    for x, prefix, panels in ((min_x - GAP - WIDTH, "panel-l", LEFT), (max_x + GAP, "panel-r", RIGHT)):
        y = top
        for i, (title, stroke, bg, lines) in enumerate(panels):
            body = "\n".join(lines)
            height = 52 + len(body.split("\n")) * 22 + 16
            new.append(rect(f"{prefix}{i}", x, y, WIDTH, height, stroke, bg))
            new.append(text(f"{prefix}-title-{i}", x + 18, y + 14, title, 20, stroke))
            new.append(text(f"{prefix}-body-{i}", x + 18, y + 52, body, 17, "#1e1e1e"))
            y += height + 28
    board["elements"] = elements + new
    write(full_path, board)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
