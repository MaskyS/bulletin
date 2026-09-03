"""Split unlabeled candidates into batch files for parallel labeling, and merge results back.

  uv run pipeline/batches.py make     write data/batches/batch-NNN.json (offers since March,
                                      asks inside the 14-day window), print the file list
  uv run pipeline/batches.py merge    fold data/batches/labels-NNN.json into data/labels.json

A labeler (person, model, or subagent) reads one batch and writes labels-NNN.json: an object
keyed by candidate id whose values match the label shape in classify.py, plus "by".
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from classify import KIND_IDS, RESPOND, clean_text, side_of

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BATCHES = DATA / "batches"
SIZE = 50
ASK_WINDOW_DAYS = 14


def make():
    cands = json.loads((DATA / "candidates.json").read_text())
    labels = json.loads((DATA / "labels.json").read_text())
    ask_since = (date.today() - timedelta(days=ASK_WINDOW_DAYS + 2)).isoformat()
    todo = []
    for c in cands.values():
        if c["id"] in labels:
            continue
        text = clean_text(c["text"])
        side = side_of(text)
        if side == "offer" or (side == "ask" and c["posted_at"] >= ask_since):
            todo.append({"id": c["id"], "handle": c["author"]["handle"], "posted_at": c["posted_at"][:10], "guess": side, "text": text})
    todo.sort(key=lambda c: c["posted_at"], reverse=True)
    BATCHES.mkdir(exist_ok=True)
    for old in BATCHES.glob("batch-*.json"):
        old.unlink()
    files = []
    for i in range(0, len(todo), SIZE):
        p = BATCHES / f"batch-{i // SIZE:03d}.json"
        p.write_text(json.dumps(todo[i:i + SIZE], ensure_ascii=False, indent=0))
        files.append(str(p))
    (BATCHES / "index.json").write_text(json.dumps(files))
    print(f"{len(todo)} candidates in {len(files)} batches -> {BATCHES}")


def merge():
    labels_path = DATA / "labels.json"
    labels = json.loads(labels_path.read_text())
    added, bad = 0, 0
    for p in sorted(BATCHES.glob("labels-*.json")):
        for cid, lab in json.loads(p.read_text()).items():
            if cid in labels:
                continue
            if lab.get("is_offer"):
                ok = lab.get("side") in ("offer", "ask") and lab.get("kind") in KIND_IDS and lab.get("respond") in RESPOND and lab.get("summary")
                if not ok:
                    bad += 1
                    continue
                lab["expires_at"] = lab.get("expires_at") or None
                lab["place"] = lab.get("place") or None
            lab.setdefault("by", "model")
            labels[cid] = lab
            added += 1
    labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=0) + "\n")
    print(f"merged {added} labels ({bad} rejected as malformed); labels.json now has {len(labels)} entries")


if __name__ == "__main__":
    {"make": make, "merge": merge}[sys.argv[1]]()
