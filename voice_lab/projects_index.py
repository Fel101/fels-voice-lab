"""Read-only workspace index for native scenes, dubs and books."""

import json

from voice_lab import book_export
from voice_lab import export_panel
from voice_lab.book_store import Books
from voice_lab.scene_store import subtitle_group


def recent_projects(sc):
    """The five most recently saved or rendered projects, with imports kept in reading order."""
    with sc._connect() as db:
        has_books = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'books' AND type = 'table'").fetchone()
        book_rows = db.execute("SELECT id,document,updated FROM books").fetchall() if has_books else ()
        scene_rows = db.execute(
            """SELECT scenes.id, scenes.name, scenes.created_at, scenes.updated_at, scenes.manifest_json,
                      (SELECT MAX(jobs.updated_at) FROM jobs WHERE jobs.scene_id = scenes.id) AS last_batch
               FROM scenes"""
        ).fetchall()
    scene_times = {row["id"]: max(row["updated_at"], row["last_batch"] or "") for row in scene_rows}
    items = []
    chapters = set()
    for row in book_rows:
        book = json.loads(row["document"])
        chapters.update(book["chapters"])
        stamp = max(row["updated"], *(scene_times.get(scene_id, "") for scene_id in book["chapters"]))
        items.append({"id": row["id"], "title": book["metadata"]["title"], "kind": "book", "updated_at": stamp})
    groups = {}
    for row in scene_rows:
        scene = json.loads(row["manifest_json"])
        if row["id"] in chapters or scene.get("book_id"):
            continue
        timeline = scene.get("timeline", {})
        key = subtitle_group(row, timeline)
        project = {
            "id": row["id"],
            "title": row["name"],
            "kind": "dub" if timeline else "scene",
            "updated_at": scene_times[row["id"]],
        }
        groups.setdefault(key, []).append((timeline.get("source_offset_ms", 0), project))
    for sections in groups.values():
        _position, first = min(sections, key=lambda section: section[0])
        items.append({**first, "updated_at": max(project["updated_at"] for _position, project in sections)})
    items.sort(key=lambda project: project["title"].casefold())
    items.sort(key=lambda project: project["updated_at"], reverse=True)
    return items[:5]


def projects(sc, kind="", query=""):
    if kind not in {"", "scene", "dub", "book"}:
        raise ValueError("Select scene, dub, book, or all projects.")
    query = query.strip().casefold()
    if len(query) > 120:
        raise ValueError("Project searches support up to 120 characters.")
    native = sc.list_scenes(group_subtitles=True)
    by_id = {row["scene_id"]: row for row in native}
    books = Books(sc)
    candidates = []
    if kind in {"", "book"}:
        for row in books.list():
            book = books.get(row["id"])
            parts = [by_id[chapter] for chapter in book["chapters"] if chapter in by_id]
            candidates.append((row["id"], row["title"], "book", parts))
    for row in native:
        item_kind = "dub" if row.get("timeline") else "scene"
        if not row.get("book_id") and kind in {"", item_kind}:
            candidates.append((row["scene_id"], row["name"], item_kind, [row]))
    result = []
    scene_exports = None
    for key, title, item_kind, parts in candidates:
        lines = [line for part in parts for line in part["lines"]]
        if query and not (query in title.casefold() or any(query in line["text"].casefold() for line in lines)):
            continue
        voices = sorted({line["identity_id"] for line in lines})
        selected = sorted(
            {
                take
                for line in lines
                for take in ([line["selected_take_id"]] if line.get("selected_take_id") else [])
                + list((line.get("selected_takes") or {}).values())
            }
        )
        links = []
        if item_kind == "book":
            output = book_export.latest(books, key)
            if output:
                links.append(f"/api/books/{key}/exports/{output['id']}/manifest.json")
        else:
            if scene_exports is None:
                scene_exports = export_panel.list_exports(sc.root)
            for row in scene_exports:
                if row.get("scene_id") == key and row["status"] == "complete":
                    links.append("/out/exports/" + row["name"] + "/manifest.json")
                    break

        latest_job = None
        if parts:
            marks = ",".join("?" for _ in parts)
            with sc._connect() as db:
                latest_job = db.execute(
                    f"SELECT id,scene_id,status FROM jobs WHERE scene_id IN ({marks}) ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    [part["scene_id"] for part in parts],
                ).fetchone()
        result.append(
            {
                "id": key,
                "title": title,
                "kind": item_kind,
                "passages": len(lines),
                "voices": voices,
                "selected_takes": len(selected),
                "selected_take_ids": selected,
                "exports": links,
                "transcript": f"/api/scenes/{key}/export.json" if item_kind == "dub" else None,
                "latest_job_status": latest_job["status"] if latest_job else None,
                "latest_job": (f"/api/scenes/jobs/{latest_job['id']}" if latest_job else None),
                "scene_ids": [part["scene_id"] for part in parts],
            }
        )
        if len(result) >= 100:
            break
    return result
