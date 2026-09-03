"""The nightly Parquet dump, queried locally with DuckDB.

The archive's own guidance: filtered questions go to the REST API, corpus-wide questions
go to the dump. Candidate retrieval, uptake counts, and tenure are corpus-wide, so they
live here. Membership and follow lists are not in the dump and stay on the REST API.

  uv run pipeline/dump.py            download or refresh data/dump/*.parquet from latest.json

The dump holds only tweets by eligible members (uploaded or opted in, minus opt-outs), so
anything read from it is inside the community boundary by construction.
"""
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "data" / "dump"
LATEST = "https://fabxmporizzqflnftavs.supabase.co/storage/v1/object/public/community-archive-public-export/latest.json"


def manifest():
    with urllib.request.urlopen(LATEST, timeout=30) as r:
        return json.load(urllib.request.urlopen(json.load(r)["manifest_url"], timeout=30))


def ensure(force=False):
    """Download tweets.parquet and profiles.parquet if missing or superseded."""
    DUMP.mkdir(parents=True, exist_ok=True)
    (DUMP / ".gitignore").write_text("*.parquet\nexport_id\n")
    m = manifest()
    have = (DUMP / "export_id").read_text().strip() if (DUMP / "export_id").exists() else None
    tweets = DUMP / "tweets.parquet"
    if not force and tweets.exists() and (have == m["export_id"] or tweets.stat().st_size == m["files"]["tweets"]["bytes"]):
        (DUMP / "export_id").write_text(m["export_id"])
        return m["export_id"]
    for name in ("profiles", "tweets"):
        url = m["publication"]["urls"][name]
        print(f"downloading {name}.parquet ({m['files'][name]['bytes'] // 1_000_000} MB)")
        urllib.request.urlretrieve(url, DUMP / f"{name}.parquet")
    (DUMP / "export_id").write_text(m["export_id"])
    return m["export_id"]


def connect():
    import duckdb
    con = duckdb.connect()
    con.execute(f"CREATE VIEW tweets AS SELECT * FROM read_parquet('{DUMP / 'tweets.parquet'}')")
    con.execute(f"CREATE VIEW profiles AS SELECT * FROM read_parquet('{DUMP / 'profiles.parquet'}')")
    return con


if __name__ == "__main__":
    print("export", ensure())
