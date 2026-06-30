#!/usr/bin/env python
"""Fetch the Temporal-Caption Bench data from HuggingFace and rebuild the local
files the annotation server expects.

The HF dataset (videofolder) ships `clips/*.mp4` + `metadata.jsonl` (one row per
segment-clip, with full group context incl. the per-segment `facts`/`negatives`
and the `annotator` it was dispatched to). This script:

  1. downloads the dataset snapshot into ./data/
  2. reconstructs ./manifest.json     (groups, the shape tcb_server.py reads)
  3. reconstructs ./assignments.json  (per-annotator gid lists)

Run once after cloning, before starting the server:

    python prepare_data.py
    python tcb_server.py --port 8000
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = "XinNUS/Temporal_Caption_Bench"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default=DEFAULT_REPO)
    ap.add_argument("--data-dir", default=str(ROOT / "data"),
                    help="where to download clips + metadata")
    args = ap.parse_args()

    from huggingface_hub import snapshot_download

    data_dir = Path(args.data_dir)
    print(f"[prepare] downloading {args.repo_id} -> {data_dir} ...")
    snapshot_download(repo_id=args.repo_id, repo_type="dataset",
                      local_dir=str(data_dir))

    meta_path = data_dir / "metadata.jsonl"
    if not meta_path.exists():
        raise SystemExit(f"metadata.jsonl not found at {meta_path}")
    rows = [json.loads(l) for l in meta_path.read_text().splitlines() if l.strip()]
    print(f"[prepare] {len(rows)} clip rows")

    # data-dir relative to repo root, so manifest clip paths resolve under it
    rel = data_dir.relative_to(ROOT) if data_dir.is_relative_to(ROOT) else data_dir

    # --- rebuild groups (manifest.json) ---------------------------------
    groups = {}
    for r in rows:
        gid = r["gid"]
        g = groups.setdefault(gid, {
            "gid": gid,
            "dataset": r["dataset"],
            "video": r["source_video"],
            "duration": r["duration"],
            "query": r["query"],
            "n_segments": r["n_segments"],
            "gemini_segments_distinct": r["group_segments_distinct"],
            "segments": [],
            "auto_flag": r["group_auto_flag"],
        })
        g["segments"].append({
            "index": r["seg_index"],
            "span": [r["span_start"], r["span_end"]],
            "clip": str(rel / r["file_name"]),   # e.g. data/clips/<gid>_<seg>.mp4
            "clip_exists": (data_dir / r["file_name"]).exists(),
            "gemini_query_occurs": r["gemini_query_occurs"],
            "facts": r["facts"],
            "negatives": r["negatives"],
        })
    for g in groups.values():
        g["segments"].sort(key=lambda s: s["index"])
    ordered = [groups[k] for k in sorted(groups)]
    (ROOT / "manifest.json").write_text(
        json.dumps({"n_groups": len(ordered), "groups": ordered},
                   ensure_ascii=False, indent=1))

    # --- rebuild assignments.json ---------------------------------------
    per_ann = defaultdict(set)
    for r in rows:
        per_ann[r["annotator"]].add(r["gid"])
    per_ann = {a: sorted(v) for a, v in sorted(per_ann.items())}
    (ROOT / "assignments.json").write_text(
        json.dumps({"phase": 1, "annotators": list(per_ann.keys()),
                    "per_annotator": per_ann}, ensure_ascii=False, indent=1))

    missing = sum(1 for g in ordered for s in g["segments"] if not s["clip_exists"])
    print(f"[prepare] manifest.json: {len(ordered)} groups | "
          f"assignments.json: {{ {', '.join(f'{a}:{len(v)}' for a,v in per_ann.items())} }}")
    if missing:
        print(f"[prepare] WARNING: {missing} clips missing on disk")
    print("[prepare] done. now run:  python tcb_server.py --port 8000")


if __name__ == "__main__":
    main()
