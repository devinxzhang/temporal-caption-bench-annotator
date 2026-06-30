# Temporal-Caption Bench — Annotation Tool

Local browser tool for verifying the **Temporal-Caption Bench** (phase-1, 150
same-query groups / 416 segment clips). Code lives here on GitHub; data is pulled
from HuggingFace ([`XinNUS/Temporal_Caption_Bench`](https://huggingface.co/datasets/XinNUS/Temporal_Caption_Bench)).
Each annotator runs the server on their own machine and reviews only their
assigned 50 groups.

## What you're annotating

Each **group** = one video + one shared grounding **query** that occurs in K
segments. For each segment you see a clip plus Gemini pre-labels (`facts`, split
into ★specific / ·shared, and `negatives`). You verify them:

- **Task ①** per segment: does the query actually occur (`query_occurs`); for the
  group: are the segments mutually **distinct**; then **keep / reject** the group.
- **Task ②** per segment: are the facts / specific-tags / negatives OK; click any
  bad fact or negative to flag it.

## Setup (each collaborator, once)

```bash
git clone https://github.com/devinxzhang/temporal-caption-bench-annotator.git
cd temporal-caption-bench-annotator
pip install -r requirements.txt

# log in to HuggingFace if the dataset is gated/private (skip if public):
#   huggingface-cli login

python prepare_data.py        # downloads clips + rebuilds manifest/assignments (~120 MB)
```

## Run

```bash
python tcb_server.py --port 8000
# open http://localhost:8000/  and log in as your name (zx / whc / lbb)
```

Your edits autosave to `annotations/<your-name>.json` (atomic, single-step undo).
You only see your own 50 groups.

## Sending results back

`annotations/<your-name>.json` is **git-ignored** — don't commit it. When done,
send that one file back to the maintainer (or push it to a `results/` branch if
asked). Re-running `prepare_data.py` never touches your annotations.

## Files

| file | role |
|---|---|
| `tcb_server.py` | FastAPI server: serves UI, streams clips (HTTP Range), persists annotations |
| `tcb_review.html` | single-page annotation UI |
| `prepare_data.py` | downloads HF dataset → rebuilds `manifest.json` + `assignments.json` |
| `requirements.txt` | fastapi / uvicorn / huggingface_hub |
