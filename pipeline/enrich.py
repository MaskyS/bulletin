# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1"]
# ///
"""Add the community to data/offers.json: who is a member, who follows whom among the
authors, how long each author has been around, and who took each notice up.

  uv run pipeline/enrich.py            refresh follow lists older than 7 days
  uv run pipeline/enrich.py --force    refresh everything

Corpus-wide facts (tenure, replies, quotes) come from the Parquet dump in one query each.
Membership and follow lists are not in the dump, so they come from the REST API, cached in
data/enrich.json. The board reads only offers.json.

Follow lists are intersected with the member set and stored as indices into `members`, so a
visitor's relationship to every author is a set lookup in the browser with no network call.
"""
import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dump import connect, ensure
from retrieve import CA_HEADERS, CA_URL, _with_retries

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FRESH_DAYS = 7
CHUNK = 150  # member ids per in.() filter; keeps the URL well under limits


def get(path, **params):
    url = f"{CA_URL}/rest/v1/{path}?{urllib.parse.urlencode(params)}"
    def call():
        req = urllib.request.Request(url, headers=CA_HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    out = _with_retries(call)
    time.sleep(0.15)
    return out


def paged(path, **params):
    rows, offset = [], 0
    while True:
        page = get(path, limit=1000, offset=offset, **params)
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += 1000


def fresh(entry):
    return entry and (datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])).days < FRESH_DAYS


def stamp(value):
    return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **value}


def fetch_members():
    rows = paged("user_directory", select="account_id,username,account_display_name,has_archive,is_opted_in,joined_at",
                 **{"or": "(has_archive.is.true,is_opted_in.is.true)", "order": "account_id"})
    # a few directory rows have no account_id or username; they cannot take part in relationships
    return [[r["account_id"], r["username"], r.get("account_display_name") or r["username"]]
            for r in rows if r.get("account_id") and r.get("username")]


def fetch_follows(account_id, member_ids):
    followers, following = set(), set()
    for i in range(0, len(member_ids), CHUNK):
        chunk = ",".join(member_ids[i:i + CHUNK])
        followers |= {r["follower_account_id"] for r in get("followers", select="follower_account_id",
                      account_id=f"eq.{account_id}", follower_account_id=f"in.({chunk})")}
        following |= {r["following_account_id"] for r in get("following", select="following_account_id",
                      account_id=f"eq.{account_id}", following_account_id=f"in.({chunk})")}
    return {"followers": sorted(followers), "following": sorted(following)}


def from_dump(notice_ids, author_ids):
    """Tenure per author and public uptake per notice, from the dump."""
    ensure()
    con = connect()
    tenure = dict(con.execute(
        "SELECT account_id, CAST(year(min(created_at)) AS VARCHAR) FROM tweets WHERE account_id IN (SELECT unnest(?)) GROUP BY 1",
        [author_ids]).fetchall())
    replies = {k: v for k, v in con.execute(
        "SELECT CAST(reply_to_tweet_id AS VARCHAR), list(DISTINCT account_id) FROM tweets "
        "WHERE CAST(reply_to_tweet_id AS VARCHAR) IN (SELECT unnest(?)) GROUP BY 1", [notice_ids]).fetchall()}
    quotes = dict(con.execute(
        "SELECT CAST(quoted_tweet_id AS VARCHAR), count(*) FROM tweets "
        "WHERE CAST(quoted_tweet_id AS VARCHAR) IN (SELECT unnest(?)) GROUP BY 1", [notice_ids]).fetchall())
    return tenure, replies, quotes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cached-only", action="store_true", help="do not fetch follow lists; authors without a cache entry get none")
    args = ap.parse_args()

    data = json.loads((DATA / "offers.json").read_text())
    cache_path = DATA / "enrich.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {"members": None, "follows": {}}
    cache.setdefault("follows", {})
    save = lambda: cache_path.write_text(json.dumps(cache, ensure_ascii=False))

    if args.force or not fresh(cache["members"]):
        cache["members"] = stamp({"rows": fetch_members()})
        save()
    members = cache["members"]["rows"]
    idx = {m[0]: i for i, m in enumerate(members)}
    by_handle = {m[1].lower(): m[0] for m in members}
    member_ids = [m[0] for m in members]
    print(f"{len(members)} members")

    handles = sorted({n["author"]["handle"].lower() for n in data["notices"]})
    author_ids = [by_handle[h] for h in handles if h in by_handle]
    for h in handles:
        aid = by_handle.get(h)
        if not aid:
            print(f"  {h}: not in the member directory")
            continue
        if args.cached_only and aid not in cache["follows"]:
            continue
        if args.force or not fresh(cache["follows"].get(aid)):
            cache["follows"][aid] = stamp(fetch_follows(aid, member_ids))
            save()
            f = cache["follows"][aid]
            print(f"  @{h}: {len(f['followers'])} member followers, follows {len(f['following'])} members")

    notice_ids = [n["id"].split(":", 1)[1] for n in data["notices"]]
    tenure, replies, quotes = from_dump(notice_ids, author_ids)
    print(f"dump: tenure for {len(tenure)} authors, replies on {len(replies)} notices, quotes on {len(quotes)}")

    data["members"] = members
    data["authors"] = {}
    for h in handles:
        aid = by_handle.get(h)
        f = cache["follows"].get(aid) if aid else None
        if aid:
            data["authors"][h] = {
                "tenure": tenure.get(aid),
                "followers": [idx[i] for i in (f or {}).get("followers", []) if i in idx],
                "following": [idx[i] for i in (f or {}).get("following", []) if i in idx],
            }
    outside = 0
    for n in data["notices"]:
        # the boundary: only members (uploaded or opted in) are on the board
        n["member"] = n["author"]["handle"].lower() in by_handle
        outside += not n["member"]
        tid = n["id"].split(":", 1)[1]
        r = replies.get(tid, [])
        n["uptake"] = {"replies": len(r), "quotes": int(quotes.get(tid, 0)), "repliers": sorted(idx[a] for a in r if a in idx)}
    (DATA / "offers.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"offers.json enriched; {outside} notices are by non-members and will not be shown")


if __name__ == "__main__":
    main()
