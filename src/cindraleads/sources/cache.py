"""Content-addressed fetch cache.

This is what makes the Phase 2 gate's "second identical run costs 0 credits" true, and
more importantly what stops the pipeline re-asking a prospect's server the same question
every hour.

**Split storage.** The index is a SQLite row; the body is a gzip file under
``var/cache/``. Keeping bodies out of the database is the point: at 200+ documents per
harvest, hourly, inline bodies would grow the DB to gigabytes, bloat the WAL, and make
the nightly ``.backup`` slow enough to be its own thermal event (PLAN.md 2.7). Evidence
snippets are capped at 500 chars and stay in the DB, so provenance survives cache
eviction.

**Two hashes, deliberately.** ``cache_key`` is ``sha256(engine|query|params)`` and
answers "have I asked this?". ``content_sha256`` is the hash of the body and answers
"what did I see?" — it is the provenance value that ends up on an Evidence row. They are
not interchangeable: two different queries can return byte-identical bodies, and the
same query re-run tomorrow returns a different body under the same key.

**gzip, not zstd.** PLAN.md 2.7 said zstd. Python 3.13 has no stdlib zstd (it lands in
3.14), so that would mean adding the ``zstandard`` package for perhaps 15% better ratio
on a 256 GB card where space is not the constraint. Boring and dependency-free wins;
revisit if the cache ever becomes large enough for the ratio to matter.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from cindraleads.config import Settings, settings
from cindraleads.logging import get_logger
from cindraleads.models import LegalityClass, from_iso, to_iso, utcnow
from cindraleads.store import Store

__all__ = ["CachedDocument", "DocumentCache", "cache_key_for"]

log = get_logger("cindraleads.cache")


def cache_key_for(engine: str, query: str, params: dict[str, Any] | None = None) -> str:
    """``sha256(engine|query|params)``.

    Params are serialized with sorted keys so that logically identical requests made
    with differently-ordered dicts collapse onto one key — otherwise the cache silently
    misses and the credit is spent anyway.
    """
    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{engine}|{query}|{canonical}".encode()).hexdigest()


@dataclass(frozen=True)
class CachedDocument:
    cache_key: str
    content_sha256: str
    url: str
    source_id: str
    legality_class: LegalityClass
    body: str
    content_type: str | None
    status_code: int | None
    byte_size: int
    fetched_at: str
    expires_at: str
    hit_count: int = 0


class DocumentCache:
    def __init__(
        self,
        store: Store,
        *,
        cache_dir: Path | None = None,
        config: Settings | None = None,
    ) -> None:
        cfg = config or settings()
        self.store = store
        self.cache_dir = cache_dir or cfg.resolve(cfg.cache_dir)

    # ------------------------------------------------------------ body files

    def _body_path(self, content_sha256: str) -> Path:
        # Two-character fan-out: a flat directory with 100k files is slow to list and
        # unpleasant on any filesystem, SD cards especially.
        return self.cache_dir / content_sha256[:2] / f"{content_sha256}.gz"

    def _write_body(self, content_sha256: str, body: str) -> int:
        path = self._body_path(content_sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # Content-addressed: identical bytes are already on disk under this name.
            return path.stat().st_size
        # Write to a temp name and rename, so a crash mid-write cannot leave a
        # truncated file that looks valid to a later reader.
        temp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        temp.write_bytes(gzip.compress(body.encode("utf-8"), compresslevel=6))
        temp.replace(path)
        return path.stat().st_size

    def _read_body(self, content_sha256: str) -> str | None:
        path = self._body_path(content_sha256)
        if not path.is_file():
            return None
        try:
            return gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
        except (OSError, gzip.BadGzipFile, EOFError) as exc:
            # A corrupt body is a cache miss, never an exception at the caller. The
            # worst case is one wasted re-fetch.
            log.warning("cache_body_unreadable", sha=content_sha256, error=str(exc))
            return None

    # ----------------------------------------------------------------- reads

    def get(self, cache_key: str, *, allow_stale: bool = False) -> CachedDocument | None:
        """Return a fresh cached document, or ``None``.

        ``allow_stale`` is for the degraded path: if a source's circuit is open, a
        stale answer beats no answer, and the caller decides whether that is
        acceptable for its stage.
        """
        row = self.store.conn.execute(
            "SELECT * FROM fetch_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            return None

        if not allow_stale and from_iso(row["expires_at"]) <= utcnow():
            return None

        body = self._read_body(str(row["content_sha256"]))
        if body is None:
            # Index says yes, disk says no. Drop the row so the next call re-fetches
            # rather than looping on a phantom hit.
            with self.store.tx() as conn:
                conn.execute("DELETE FROM fetch_cache WHERE cache_key = ?", (cache_key,))
            return None

        with self.store.tx() as conn:
            conn.execute(
                "UPDATE fetch_cache SET hit_count = hit_count + 1, last_hit_at = ? "
                "WHERE cache_key = ?",
                (to_iso(utcnow()), cache_key),
            )

        return CachedDocument(
            cache_key=cache_key,
            content_sha256=str(row["content_sha256"]),
            url=str(row["url"]),
            source_id=str(row["source_id"]),
            legality_class=row["legality_class"],
            body=body,
            content_type=row["content_type"],
            status_code=row["status_code"],
            byte_size=int(row["byte_size"]),
            fetched_at=str(row["fetched_at"]),
            expires_at=str(row["expires_at"]),
            hit_count=int(row["hit_count"]) + 1,
        )

    def has_fresh(self, cache_key: str) -> bool:
        row = self.store.conn.execute(
            "SELECT expires_at FROM fetch_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return row is not None and from_iso(row["expires_at"]) > utcnow()

    # ---------------------------------------------------------------- writes

    def put(
        self,
        cache_key: str,
        body: str,
        *,
        url: str,
        source_id: str,
        legality_class: LegalityClass,
        ttl_hours: float,
        content_type: str | None = None,
        status_code: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Store a body and its index row. Returns the ``content_sha256``."""
        now = utcnow()
        content_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        stored_bytes = self._write_body(content_sha256, body)

        def _index(active: sqlite3.Connection) -> None:
            active.execute(
                "INSERT INTO fetch_cache (cache_key, content_sha256, url, source_id, "
                "legality_class, content_type, status_code, byte_size, stored_bytes, "
                "fetched_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "  content_sha256=excluded.content_sha256, url=excluded.url,"
                "  content_type=excluded.content_type, status_code=excluded.status_code,"
                "  byte_size=excluded.byte_size, stored_bytes=excluded.stored_bytes,"
                "  fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                (
                    cache_key,
                    content_sha256,
                    url,
                    source_id,
                    legality_class,
                    content_type,
                    status_code,
                    len(body.encode("utf-8")),
                    stored_bytes,
                    to_iso(now),
                    to_iso(now + timedelta(hours=ttl_hours)),
                ),
            )
            # Provenance index, keyed by what we saw rather than what we asked.
            active.execute(
                "INSERT INTO raw_documents (content_sha256, url, source_id, "
                "legality_class, content_type, byte_size, fetched_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(content_sha256) DO NOTHING",
                (
                    content_sha256,
                    url,
                    source_id,
                    legality_class,
                    content_type,
                    len(body.encode("utf-8")),
                    to_iso(now),
                    to_iso(now + timedelta(hours=ttl_hours)),
                ),
            )

        if conn is not None:
            _index(conn)
        else:
            with self.store.tx() as owned:
                _index(owned)
        return content_sha256

    # ----------------------------------------------------------- maintenance

    def purge_expired(self, *, older_than_days: float = 30.0) -> tuple[int, int]:
        """Drop expired index rows and orphaned bodies. Returns ``(rows, files)``.

        Bodies outlive their index entries on purpose — the same bytes may be
        referenced by another key — so files are only removed once nothing points at
        them and they are older than the retention floor.
        """
        cutoff = to_iso(utcnow() - timedelta(days=older_than_days))
        with self.store.tx() as conn:
            rows = int(
                conn.execute(
                    "DELETE FROM fetch_cache WHERE expires_at < ?", (to_iso(utcnow()),)
                ).rowcount
            )
            referenced = {
                str(r["content_sha256"])
                for r in conn.execute("SELECT content_sha256 FROM fetch_cache").fetchall()
            }

        files = 0
        if self.cache_dir.is_dir():
            for path in self.cache_dir.glob("*/*.gz"):
                sha = path.stem
                if sha in referenced:
                    continue
                stamp = self.store.conn.execute(
                    "SELECT fetched_at FROM raw_documents WHERE content_sha256 = ?", (sha,)
                ).fetchone()
                if stamp is not None and str(stamp["fetched_at"]) >= cutoff:
                    continue
                path.unlink(missing_ok=True)
                files += 1
        return rows, files

    def stats(self) -> dict[str, int]:
        row = self.store.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count),0) AS hits, "
            "COALESCE(SUM(stored_bytes),0) AS bytes, COALESCE(SUM(byte_size),0) AS raw "
            "FROM fetch_cache"
        ).fetchone()
        fresh = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM fetch_cache WHERE expires_at > ?", (to_iso(utcnow()),)
        ).fetchone()
        return {
            "entries": int(row["n"]),
            "fresh": int(fresh["n"]),
            "hits": int(row["hits"]),
            "stored_bytes": int(row["bytes"]),
            "raw_bytes": int(row["raw"]),
        }
