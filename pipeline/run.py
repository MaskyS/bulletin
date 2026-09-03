# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=0.60", "duckdb>=1.1"]
# ///
"""Build data/offers.json: retrieve -> prefilter -> label -> write.

  uv run pipeline/run.py                     keyword retrieval (offers and asks), labels from cache
  uv run pipeline/run.py --queries asks      only the ask queries
  uv run pipeline/run.py --since 2026-08-01  keyword retrieval looks at recent tweets only
  uv run pipeline/run.py --llm               also ask Claude to label new candidates
  uv run pipeline/run.py --retriever embed   semantic retrieval (needs CA_EMBED_URL)
  uv run pipeline/run.py --offline           skip retrieval, rebuild from the candidate cache

offers.json is regenerated in full every run from data/candidates.json + data/labels.json,
so it is always reproducible and a hand edit to labels.json shows up on the next run.
Run pipeline/enrich.py afterwards to add members, follow relationships, tenure, and uptake.
"""
import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from classify import KINDS, Labels, clean_text, llm_label, side_of
from retrieve import ASK_QUERIES, FTS_QUERIES, RETRIEVERS, FtsRetriever

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ASK_TTL_DAYS = 14    # an ask with no end date comes down after two weeks unless renewed
OFFER_TTL_DAYS = 60  # a one-off offer with no date fades after two months; standing offers stay
MIN_CONFIDENCE = 0.6  # a labeler's borderline calls stay in labels.json but off the board


def load_json(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retriever", choices=list(RETRIEVERS), default="dump")
    ap.add_argument("--queries", choices=["offers", "asks", "all"], default="all", help="which keyword set (fts only)")
    ap.add_argument("--since", help="YYYY-MM-DD (dump default: 2026-03-01)")
    ap.add_argument("--llm", action="store_true", help="label unlabeled candidates with Claude")
    ap.add_argument("--offline", action="store_true", help="do not retrieve; use data/candidates.json")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    cache = load_json(DATA / "candidates.json", {})
    if not args.offline:
        if args.retriever == "fts":
            queries = {"offers": FTS_QUERIES, "asks": ASK_QUERIES, "all": FTS_QUERIES + ASK_QUERIES}[args.queries]
            retriever = FtsRetriever(queries=queries, since=args.since)
        elif args.retriever == "dump":
            retriever = RETRIEVERS["dump"](since=args.since)
        else:
            retriever = RETRIEVERS[args.retriever]()
        print(f"retrieving with {args.retriever} ({args.queries})")
        for c in retriever.candidates():
            prev = cache.get(c["id"])
            if prev:
                c["signals"] = sorted(set(prev["signals"]) | set(c["signals"]))
            cache[c["id"]] = c
        (DATA / "candidates.json").write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"{len(cache)} candidates in cache")

    labels = Labels(DATA / "labels.json")
    prev = load_json(DATA / "offers.json", {})
    prev_members = {m[1].lower() for m in prev.get("members", [])}
    notices, unlabeled, refused, lowconf = [], 0, 0, 0
    for cid, cand in cache.items():
        text = clean_text(cand["text"])
        side = side_of(text)
        if side is None:
            continue
        label = labels.get(cid)
        if label is None:
            if not args.llm:
                unlabeled += 1
                continue
            label = llm_label(cand)
            if label is None:
                refused += 1
                continue
            labels.put(cid, label)
        if not label.get("is_offer"):
            continue
        if label.get("confidence", 1.0) < MIN_CONFIDENCE:
            lowconf += 1
            continue
        side = label.get("side") or side
        summary = (label.get("summary") or "").strip()
        if side == "ask":  # "Wants a chess app" -> "A chess app"; the column already says Wanted
            m = re.match(r"^(wants|wanted|looking for|seeking|needs)[:\s]+(.*)$", summary, re.I)
            if m:
                summary = m.group(2)[:1].upper() + m.group(2)[1:]
        label = {**label, "summary": summary}
        expires = label.get("expires_at")
        if not expires and not label.get("standing"):
            ttl = ASK_TTL_DAYS if side == "ask" else OFFER_TTL_DAYS
            expires = (datetime.fromisoformat(cand["posted_at"]) + timedelta(days=ttl)).date().isoformat()
        notices.append({
            "id": cid,
            "url": cand["url"],
            "author": cand["author"],
            "posted_at": cand["posted_at"],
            "text": text,
            "likes": cand.get("likes", 0),
            "signals": cand.get("signals", []),
            "side": side,
            "expires_at": expires,
            # membership is settled by enrich.py; carry the last known answer meanwhile
            "member": (cand["author"]["handle"].lower() in prev_members) if prev_members else None,
            **{k: label.get(k) for k in ("kind", "summary", "topics", "respond", "standing", "place", "featured", "confidence")},
        })

    notices.sort(key=lambda n: n["posted_at"] or "", reverse=True)
    notices.sort(key=lambda n: not n.get("featured"))
    out = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "corpus": "Community Archive",
            "retrievers": sorted({s.split(":")[0] for n in notices for s in n["signals"]}),
            "labels_repo": "TheExGenesis/community-archive",
            "labels_path": "prototypes/offering-board/data/labels.json",
        },
        "review": "hand" if all((labels.get(n["id"]) or {}).get("by", "hand") == "hand" for n in notices) else "model",
        "kinds": KINDS,
        "ask_ttl_days": ASK_TTL_DAYS,
        "offer_ttl_days": OFFER_TTL_DAYS,
        # enrich.py fills these; carry the previous run's values so a rebuild never blanks them
        "members": prev.get("members", []),
        "authors": prev.get("authors", {}),
        "notices": notices,
    }
    prev_uptake = {n["id"]: n.get("uptake") for n in prev.get("notices", []) if n.get("uptake")}
    for n in notices:
        if n["id"] in prev_uptake:
            n["uptake"] = prev_uptake[n["id"]]
    (DATA / "offers.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    sides = {s: sum(1 for n in notices if n["side"] == s) for s in ("offer", "ask")}
    print(f"{len(notices)} notices written ({sides['offer']} offers, {sides['ask']} asks); "
          f"{unlabeled} candidates await a label; {lowconf} below confidence {MIN_CONFIDENCE}; {refused} refused")


if __name__ == "__main__":
    main()
