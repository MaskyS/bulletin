# Bulletin

A notice board for members of the Community Archive: what they have offered each other and
asked each other for, collected from their own tweets. Enter a handle and notices from people
you follow, or who follow you, sort to the top. Every notice is a real tweet with one action
link to X. Design plan and reasoning: the plan artifact (Ostrom's commons principles mapped to
board mechanisms).

## Run it

```bash
uv run pipeline/dump.py            # fetch the nightly Parquet dump (~900 MB, once per export)
uv run pipeline/run.py             # candidates from the dump, notices from data/labels.json
uv run pipeline/enrich.py          # members, follow relationships (REST), tenure + uptake (dump)
uv run board/build.py              # writes board.html from data/offers.json
```

To label new candidates at scale:

```bash
uv run pipeline/batches.py make    # unlabeled candidates -> data/batches/batch-NNN.json
# label each batch (a person, a model, or a fleet of subagents) into labels-NNN.json
uv run pipeline/batches.py merge   # fold them into data/labels.json, then re-run run.py
```

`run.py --llm` labels inline with Claude instead (spends API credit). `--retriever fts` uses
the archive's search RPC for the last day or two the dump does not cover yet;
`--retriever embed` uses the CA_Embed service once `CA_EMBED_URL` is known.

## How the data flows

```
dump.py       latest.json -> data/dump/tweets.parquet + profiles.parquet
retrieve.py   DumpRetriever (regex in DuckDB) | FtsRetriever | EmbedRetriever -> data/candidates.json
classify.py   side_of prefilter, label shape, optional llm_label                data/labels.json
batches.py    make / merge: parallel labeling in files                          data/batches/
run.py        candidates x labels -> notices, with default lifetimes            -> data/offers.json
enrich.py     members + follows from REST, tenure + uptake from the dump        -> data/offers.json
build.py      injects offers.json into board/template.html                      -> board.html
```

Why the dump: the archive's own guidance sends corpus-wide work to the Parquet export and
filtered reads to the REST API. Regex over 10M tweets takes seconds in DuckDB and makes no
API calls; the search RPC times out on the same job. The dump also holds only eligible
members' tweets (uploaded or opted in, opt-outs removed), so the community boundary is
enforced by construction. It lags about two days, so uptake on the newest notices shows late.

`labels.json` is the reviewed layer: one entry per candidate id, positive or negative, with
`"by"` recording who decided. The pipeline never re-labels an id that has an entry, so a hand
correction sticks. Negatives permanently suppress a false positive. The board's "not right?"
link opens a GitHub issue against this file.

## The offer record (offers.json, version 2)

```json
{
  "kinds": [{"id": "help", "label": "Help & skills"}, ...],
  "ask_ttl_days": 14, "offer_ttl_days": 60,
  "members": [["<account_id>", "<handle>", "<display name>"], ...],
  "authors": {"<handle>": {"tenure": "2018", "followers": [12, 40], "following": [12, 77]}},
  "notices": [{
    "id": "x:2095532958920679610", "url": "...", "author": {"handle", "name", "avatar"},
    "posted_at": "...", "text": "verbatim", "likes": 0, "signals": ["dump:offer"],
    "side": "offer", "kind": "help", "summary": "one sentence", "topics": ["ux"],
    "respond": "dm", "standing": true, "expires_at": null, "place": null,
    "member": true, "confidence": 1.0,
    "uptake": {"replies": 11, "quotes": 0, "repliers": [3, 9]}
  }]
}
```

- `side` is offer or ask; `kind` is the shape of the exchange, a closed list shipped in
  `kinds`; `topics` is open, ready for embeddings-derived clusters.
- Lifetimes: `standing` offers stay up. An ask with no date lives 14 days, a one-off offer
  60; after that the board files it under past. Quoting your own notice renews it.
- `authors.*.followers` / `following` are indices into `members`, intersected with the member
  set, from the author's own archive upload. In the browser a visitor's relationship to every
  author is a set lookup with no network call. Authors who opted in without uploading have
  no lists; the visitor's own lists fill the gap when the visitor is an author too.
- `uptake` counts public replies and quotes by members. Likes are not used: they arrive only
  with an upload and lag by months.

## What the board does not do

It has no write path. Nothing is posted, answered, or marked from the page; every action is a
link to X. It shows nothing from DMs and infers nothing about them. The record line for a
handle is computed in the browser and shown only to whoever typed that handle.

## Where embeddings plug in

Retrieval (`EmbedRetriever`, natural-language probes to `POST /embeddings/search`), matching
(asks and offers as vectors), and topics (cluster the notice vectors, name the clusters). The
service is `TheExGenesis/CA_embeddings_infra`; set `CA_EMBED_URL` once its base URL is known.
