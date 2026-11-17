"""Read-only workspace index for native scenes, dubs and books."""

import book_export
import export_panel
from book_store import Books


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
