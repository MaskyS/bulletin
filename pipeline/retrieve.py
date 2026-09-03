"""Candidate retrieval. One interface, two implementations.

Every retriever returns a list of candidate dicts in the same shape, so the rest of
the pipeline does not care where a tweet came from:

    {"id": "x:<tweet_id>", "url", "author": {"handle","name","avatar"},
     "posted_at", "text", "likes", "signals": ["why it was pulled", ...]}

FtsRetriever   keyword search over the archive's full-text index. Works today.
EmbedRetriever semantic probes against the CA_Embed service (POST /embeddings/search).
               Needs CA_EMBED_URL. Results are tweet ids, hydrated from the archive.
"""
import json
import os
import time
import urllib.parse
import urllib.request

CA_URL = "https://fabxmporizzqflnftavs.supabase.co"
# Public anon key, published in the archive's own docs.
CA_ANON = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZhYnhtcG9y"
    "aXp6cWZsbmZ0YXZzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MjIyNDQ5MTIsImV4cCI6MjAzNzgyMDkxMn0."
    "UIEJiUNkLsW28tBHmG-RQDW-I5JNlJLt62CSk9D_qG8"
)
CA_HEADERS = {"apikey": CA_ANON, "Authorization": f"Bearer {CA_ANON}"}

# Keyword anchors for the archive's to_tsquery('english', ...) search. They cast a
# wide net on purpose; classify.py does the precision work.
FTS_QUERIES = [
    "standing & offer",
    "offering",
    "office & hours",
    "happy & (help | assist | pair | review)",
    "dm & me",
    "free & (help | consult | advice | session | call | coaching)",
    "glad & (help | assist)",
    "pro & bono",
    "reach & out",
]

# Asks: the other side of the exchange. Stopwords (does, do, who, i) drop out of the
# english config, so anchors are content words only.
ASK_QUERIES = [
    "looking & for & (someone | anyone | recs | recommendations | intro)",
    "anyone & (recommend | recs | recommendations | know | knows)",
    "need & (help | advice | intro | recommendation) & (anyone | someone)",
    "(wanted | seeking) & (help | someone | anyone | collaborator | cofounder)",
]

# Natural-language probes for the embeddings retriever. Each one describes an offer
# the way a person would write it, so the nearest neighbours are offers too.
EMBED_PROBES = [
    "standing offer to my mutuals: if you need help with anything, dm me",
    "happy to help anyone who wants to talk this through, my dms are open",
    "I'm offering a series of free 1:1 sessions, reply if you'd like one",
    "reply or dm me to be added to the group chat",
    "we host office hours every week, come by",
    "I know someone who can help with that, dm me and I'll make the intro",
    "we're hiring, dm me if you're interested",
]


def _request(url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _with_retries(fn, tries=3, wait=3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - the archive RPC drops connections under load
            if i == tries - 1:
                raise
            print(f"    retry {i + 1}: {e}")
            time.sleep(wait * (i + 1))


def _candidate(row, signals):
    handle = row.get("username") or ""
    return {
        "id": f"x:{row['tweet_id']}",
        "url": f"https://x.com/{handle}/status/{row['tweet_id']}",
        "author": {
            "handle": handle,
            "name": row.get("account_display_name") or handle,
            "avatar": row.get("avatar_media_url"),
        },
        "posted_at": row.get("created_at"),
        "text": row.get("full_text") or "",
        "likes": row.get("favorite_count") or 0,
        "signals": list(signals),
    }


class FtsRetriever:
    def __init__(self, queries=FTS_QUERIES, since=None, limit=250):
        self.queries, self.since, self.limit = queries, since, limit

    def candidates(self):
        found = {}
        for q in self.queries:
            body = {"search_query": q, "limit_": self.limit}
            if self.since:
                body["since_date"] = self.since
            try:
                rows = _with_retries(lambda: _request(f"{CA_URL}/rest/v1/rpc/search_tweets", body, CA_HEADERS), tries=5, wait=6)
            except Exception as e:  # noqa: BLE001 - one bad query must not lose the others
                print(f"  fts [{q}] -> gave up: {e}")
                continue
            for row in rows or []:
                c = found.setdefault(row["tweet_id"], _candidate(row, []))
                c["signals"].append(f"fts:{q}")
            print(f"  fts [{q}] -> {len(rows or [])} rows, {len(found)} unique so far")
            time.sleep(1)  # the RPC is happier when it is not hammered
        return list(found.values())


class EmbedRetriever:
    def __init__(self, base_url=None, probes=EMBED_PROBES, k=100, threshold=0.6):
        self.base_url = (base_url or os.environ.get("CA_EMBED_URL") or "").rstrip("/")
        if not self.base_url:
            raise SystemExit("EmbedRetriever needs CA_EMBED_URL (base URL of the CA_Embed service)")
        self.probes, self.k, self.threshold = probes, k, threshold

    def candidates(self):
        hits = {}  # tweet_id -> signals
        for probe in self.probes:
            body = {"searchTerm": probe, "k": self.k, "threshold": self.threshold, "with_payload": False}
            res = _with_retries(lambda: _request(f"{self.base_url}/embeddings/search", body))
            for r in res.get("results", []):
                hits.setdefault(str(r["key"]), []).append(f"embed:{probe[:40]} ({r.get('distance', 0):.2f})")
            print(f"  embed [{probe[:50]}] -> {res.get('count', 0)} hits, {len(hits)} unique so far")
        return hydrate(hits)


def hydrate(hits, chunk=100):
    """Turn {tweet_id: signals} into full candidates using the archive's enriched view."""
    ids = list(hits)
    out = []
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        params = urllib.parse.urlencode({
            "select": "tweet_id,username,account_display_name,avatar_media_url,created_at,full_text,favorite_count",
            "tweet_id": f"in.({','.join(batch)})",
        })
        rows = _with_retries(lambda: _request(f"{CA_URL}/rest/v1/enriched_tweets?{params}", headers=CA_HEADERS))
        for row in rows:
            if not (row.get("full_text") or "").startswith("RT @"):
                out.append(_candidate(row, hits[row["tweet_id"]]))
    return out


class DumpRetriever:
    """Regex over the nightly Parquet dump with DuckDB. Seconds, no API calls, and only
    members' tweets by construction. This is the default."""

    def __init__(self, since=None, limit=200000):
        self.since = since or "2026-03-01"
        self.limit = limit

    def candidates(self):
        from classify import ASK, CONVENTION, OFFER
        from dump import connect, ensure
        print(f"  dump export {ensure()}")
        con = connect()
        pats = [OFFER.pattern, ASK.pattern, CONVENTION.pattern]
        rows = con.execute(
            """
            SELECT t.tweet_id, strftime(t.created_at, '%Y-%m-%dT%H:%M:%S') || '+00:00', t.full_text, t.favorite_count,
                   p.username, p.display_name, p.avatar_media_url
            FROM tweets t JOIN profiles p USING (account_id)
            WHERE t.retweeted_tweet_id IS NULL AND NOT starts_with(t.full_text, 'RT @')
              AND t.reply_to_tweet_id IS NULL
              AND t.created_at >= CAST(? AS TIMESTAMP)
              AND (regexp_matches(t.full_text, ?, 'i') OR regexp_matches(t.full_text, ?, 'i') OR regexp_matches(t.full_text, ?, 'i'))
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            [self.since, *pats, self.limit],
        ).fetchall()
        out = []
        for tweet_id, created_at, text, likes, handle, name, avatar in rows:
            signals = [f"dump:{k}" for k, p in (("offer", OFFER), ("ask", ASK), ("convention", CONVENTION)) if p.search(text)]
            out.append(_candidate({
                "tweet_id": str(tweet_id), "created_at": created_at, "full_text": text,
                "favorite_count": likes, "username": handle, "account_display_name": name, "avatar_media_url": avatar,
            }, signals))
        print(f"  dump -> {len(out)} candidates since {self.since}")
        return out


RETRIEVERS = {"dump": DumpRetriever, "fts": FtsRetriever, "embed": EmbedRetriever}
