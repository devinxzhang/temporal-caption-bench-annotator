#!/usr/bin/env python
"""Temporal-Caption Bench annotation tool - server side (redesigned).

A FastAPI server modeled on highlight_annotator/fyp_server.py:
  * Serves the single-page UI (tcb_review.html).
  * Streams clip files (with HTTP Range support) from clips/ — relative paths
    in manifest.json are resolved from this repo root.
  * Login is by annotator (zx / whc / lbb). Each annotator only sees their
    assigned groups (assignments.json). Data comes from manifest.json.
  * Holds review state in memory + persists every edit immediately to
    annotations/<annotator>.json. Format is a dict {gid: record} — identical to
    the original serve.py so 06_aggregate stays compatible. The original input
    files (manifest/assignments) are never touched.
  * Single-step undo per annotator.

Usage:
    python tcb_server.py --port 8000
Then open http://localhost:8000/ and log in as zx / whc / lbb.
"""

import argparse
import copy
import json
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
MANIFEST = REPO_ROOT / "manifest.json"
ASSIGNMENTS = REPO_ROOT / "assignments.json"
ANN_DIR = REPO_ROOT / "annotations"


# ---------------------------------------------------------------------------
# Helpers (range streaming, atomic write) — same approach as fyp_server.py
# ---------------------------------------------------------------------------
def guess_mime(path: str) -> str:
    ctype, _ = mimetypes.guess_type(path)
    return ctype or "application/octet-stream"


def _parse_range_header(header: str, size: int):
    if not header.startswith("bytes="):
        return None, None
    spec = header[6:].split(",", 1)[0].strip()
    if "-" not in spec:
        return None, None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0:
                return None, None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        if start < 0 or end >= size or start > end:
            return None, None
    except ValueError:
        return None, None
    return start, end


def file_iter(path: str, start: int, end: int, chunk_size: int = 256 * 1024):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            buf = f.read(min(chunk_size, remaining))
            if not buf:
                break
            try:
                yield buf
            except (BrokenPipeError, ConnectionResetError, GeneratorExit):
                return
            remaining -= len(buf)


def atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _resolve_repo_relative_path(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str((REPO_ROOT / p).resolve())


# ---------------------------------------------------------------------------
# Default annotation record (one per group) — identical shape to serve.py
# ---------------------------------------------------------------------------
def _default_seg_rec(gemini_query_occurs: Any) -> Dict[str, Any]:
    return {
        "query_occurs": gemini_query_occurs,
        "facts_ok": None,
        "specific_ok": None,
        "negatives_ok": None,
        "bad_facts": [],
        "bad_negs": [],
    }


def _default_group_rec() -> Dict[str, Any]:
    return {"seg": {}, "segments_distinct": None, "decision": None, "note": "", "done": False}


def _group_complete(rec: Dict[str, Any], group: Dict[str, Any]) -> bool:
    """Mirror of the client-side complete() check."""
    for s in group["segments"]:
        r = rec["seg"].get(str(s["index"])) or rec["seg"].get(s["index"])
        if not r:
            return False
        if r.get("query_occurs") is None:
            return False
        if not r.get("facts_ok") or not r.get("specific_ok") or not r.get("negatives_ok"):
            return False
    return rec.get("segments_distinct") is not None and bool(rec.get("decision"))


def _group_status(rec: Optional[Dict[str, Any]]) -> str:
    """pending | done-keep | done-reject | started."""
    if not rec:
        return "pending"
    if rec.get("done"):
        return "reject" if rec.get("decision") == "reject" else "done"
    # any field touched?
    touched = bool(rec.get("seg")) or rec.get("segments_distinct") is not None \
        or rec.get("decision") or rec.get("note")
    return "started" if touched else "pending"


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------
class Store:
    """In-memory + on-disk review state for all annotators."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        os.makedirs(ANN_DIR, exist_ok=True)
        self.manifest = json.load(open(MANIFEST, encoding="utf-8"))
        self.assignments = json.load(open(ASSIGNMENTS, encoding="utf-8"))
        self.groups: Dict[int, Dict[str, Any]] = {g["gid"]: g for g in self.manifest["groups"]}
        self.per_annotator: Dict[str, List[int]] = self.assignments["per_annotator"]
        # annotator -> {gid(str): record}
        self.state: Dict[str, Dict[str, Any]] = {}
        # annotator -> last pre-edit snapshot {gid, prev_rec_or_None}
        self.snapshots: Dict[str, Optional[Dict[str, Any]]] = {}
        for ann in self.per_annotator:
            self.state[ann] = self._load_annotations(ann)
            self.snapshots[ann] = None

    # -- persistence -----------------------------------------------------
    @staticmethod
    def _ann_path(annotator: str) -> str:
        return str(ANN_DIR / f"{annotator}.json")

    def _load_annotations(self, annotator: str) -> Dict[str, Any]:
        p = self._ann_path(annotator)
        if not os.path.exists(p):
            return {}
        try:
            data = json.load(open(p, encoding="utf-8"))
            # keys may be ints or strs depending on writer; normalize to str
            return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _persist_locked(self, annotator: str) -> None:
        atomic_write(self._ann_path(annotator),
                     json.dumps(self.state[annotator], ensure_ascii=False, indent=1))

    # -- validation ------------------------------------------------------
    def valid_annotator(self, annotator: str) -> bool:
        return annotator in self.per_annotator

    # -- reads -----------------------------------------------------------
    def summary(self, annotator: str) -> Dict[str, Any]:
        with self.lock:
            gids = self.per_annotator.get(annotator, [])
            st = self.state.get(annotator, {})
            items, counts = [], {"pending": 0, "started": 0, "done": 0, "reject": 0}
            for idx, gid in enumerate(gids):
                g = self.groups[gid]
                rec = st.get(str(gid))
                status = _group_status(rec)
                counts[status] = counts.get(status, 0) + 1
                items.append({
                    "gid": gid,
                    "index": idx,
                    "query": g["query"],
                    "dataset": g["dataset"],
                    "n_segments": g["n_segments"],
                    "duration": g.get("duration"),
                    "auto_flag": g.get("auto_flag", False),
                    "status": status,
                    "done": bool(rec and rec.get("done")),
                })
            n_done = counts["done"] + counts["reject"]
            return {
                "annotator": annotator,
                "total": len(gids),
                "n_done": n_done,
                "counts": counts,
                "groups": items,
            }

    def get_group(self, annotator: str, gid: int) -> Dict[str, Any]:
        with self.lock:
            if gid not in set(self.per_annotator.get(annotator, [])):
                raise KeyError(f"{annotator} not assigned gid {gid}")
            g = self.groups[gid]
            rec = self.state[annotator].get(str(gid))
            if rec is None:
                rec = _default_group_rec()
            return {"group": copy.deepcopy(g), "ann": copy.deepcopy(rec),
                    "index": self.per_annotator[annotator].index(gid)}

    # -- writes ----------------------------------------------------------
    def save_group(self, annotator: str, gid: int, rec: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if gid not in set(self.per_annotator.get(annotator, [])):
                raise KeyError(f"{annotator} not assigned gid {gid}")
            g = self.groups[gid]
            # snapshot previous state for single-step undo
            prev = self.state[annotator].get(str(gid))
            self.snapshots[annotator] = {"gid": gid, "prev": copy.deepcopy(prev)}
            # recompute done strictly (never trust client's done over the check)
            rec = copy.deepcopy(rec)
            rec["done"] = bool(rec.get("done")) and _group_complete(rec, g)
            self.state[annotator][str(gid)] = rec
            self._persist_locked(annotator)
            return {"status": _group_status(rec), "done": rec["done"]}

    def mark_done(self, annotator: str, gid: int, rec: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if gid not in set(self.per_annotator.get(annotator, [])):
                raise KeyError(f"{annotator} not assigned gid {gid}")
            g = self.groups[gid]
            if not _group_complete(rec, g):
                return {"ok": False, "reason": "incomplete"}
            prev = self.state[annotator].get(str(gid))
            self.snapshots[annotator] = {"gid": gid, "prev": copy.deepcopy(prev)}
            rec = copy.deepcopy(rec)
            rec["done"] = True
            self.state[annotator][str(gid)] = rec
            self._persist_locked(annotator)
            return {"ok": True, "status": _group_status(rec)}

    def undo(self, annotator: str) -> Optional[int]:
        with self.lock:
            snap = self.snapshots.get(annotator)
            if not snap:
                return None
            gid = snap["gid"]
            if snap["prev"] is None:
                self.state[annotator].pop(str(gid), None)
            else:
                self.state[annotator][str(gid)] = snap["prev"]
            self.snapshots[annotator] = None
            self._persist_locked(annotator)
            return gid

    def undo_status(self, annotator: str) -> Dict[str, Any]:
        with self.lock:
            snap = self.snapshots.get(annotator)
            return {"can_undo": snap is not None, "gid": snap["gid"] if snap else None}


STORE = Store()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
def build_app(html_path: str):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from pydantic import BaseModel

    class SaveReq(BaseModel):
        annotator: str
        gid: int
        ann: Dict[str, Any]

    class DoneReq(BaseModel):
        annotator: str
        gid: int
        ann: Dict[str, Any]

    class AnnReq(BaseModel):
        annotator: str

    app = FastAPI(title="Temporal-Caption Bench Annotation Tool")

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # -- clip streaming with HTTP Range -----------------------------------
    @app.get("/api/clip")
    def clip_endpoint(request: Request, path: str):
        resolved = _resolve_repo_relative_path(path)
        # confine to repo root
        if not resolved.startswith(str(REPO_ROOT)):
            raise HTTPException(403, "forbidden")
        if not os.path.exists(resolved):
            raise HTTPException(404, f"clip not found: {path}")
        size = os.path.getsize(resolved)
        ctype = guess_mime(resolved)
        range_h = request.headers.get("range") or request.headers.get("Range")
        if range_h:
            start, end = _parse_range_header(range_h.strip(), size)
            if start is None:
                return JSONResponse({"detail": "Range Not Satisfiable"}, status_code=416,
                                    headers={"Content-Range": f"bytes */{size}"})
            return StreamingResponse(
                file_iter(resolved, start, end), status_code=206,
                headers={"Content-Type": ctype, "Accept-Ranges": "bytes",
                         "Content-Range": f"bytes {start}-{end}/{size}",
                         "Content-Length": str(end - start + 1)})
        return StreamingResponse(
            file_iter(resolved, 0, size - 1), status_code=200,
            headers={"Content-Type": ctype, "Accept-Ranges": "bytes",
                     "Content-Length": str(size)})

    # -- reads ------------------------------------------------------------
    @app.get("/api/annotators")
    def annotators():
        return {"annotators": list(STORE.per_annotator.keys())}

    @app.get("/api/summary")
    def summary(annotator: str):
        if not STORE.valid_annotator(annotator):
            raise HTTPException(404, f"unknown annotator: {annotator}")
        return STORE.summary(annotator)

    @app.get("/api/group")
    def group(annotator: str, gid: int):
        if not STORE.valid_annotator(annotator):
            raise HTTPException(404, f"unknown annotator: {annotator}")
        try:
            return STORE.get_group(annotator, gid)
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/undo_status")
    def undo_status(annotator: str):
        if not STORE.valid_annotator(annotator):
            raise HTTPException(404, f"unknown annotator: {annotator}")
        return STORE.undo_status(annotator)

    # -- writes -----------------------------------------------------------
    @app.post("/api/save_group")
    def save_group(req: SaveReq):
        if not STORE.valid_annotator(req.annotator):
            raise HTTPException(404, f"unknown annotator: {req.annotator}")
        try:
            return {"ok": True, **STORE.save_group(req.annotator, req.gid, req.ann)}
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/mark_done")
    def mark_done(req: DoneReq):
        if not STORE.valid_annotator(req.annotator):
            raise HTTPException(404, f"unknown annotator: {req.annotator}")
        try:
            return STORE.mark_done(req.annotator, req.gid, req.ann)
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/undo")
    def undo(req: AnnReq):
        if not STORE.valid_annotator(req.annotator):
            raise HTTPException(404, f"unknown annotator: {req.annotator}")
        gid = STORE.undo(req.annotator)
        return {"ok": gid is not None, "gid": gid}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--html", default=str(REPO_ROOT / "tcb_review.html"))
    args = parser.parse_args()
    if not os.path.exists(args.html):
        raise SystemExit(f"HTML not found at {args.html}")
    app = build_app(args.html)
    print(f"[TCB] Temporal-Caption Bench annotation server")
    print(f"[TCB] open http://localhost:{args.port}/   annotators: {list(STORE.per_annotator.keys())}")
    print(f"[TCB] saves -> {ANN_DIR}/<annotator>.json")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
