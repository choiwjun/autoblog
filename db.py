# autoblog/db.py — blog_posts 저장소 (SQLite ↔ Postgres 이중 SQL)
# 별도 Supabase(B) 전용 — autostudio DB와 무관.
# 스키마: slug 유니크 · status(draft/published) · indexed(운세 인덱스 정책)
import os
import sqlite3

try:
    import psycopg2
    CONNECTION_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)
except ImportError:
    CONNECTION_ERRORS = ()

SCHEMAS = {
    "sqlite": """
CREATE TABLE IF NOT EXISTS blog_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    indexed INTEGER NOT NULL DEFAULT 1,
    engine_meta TEXT NOT NULL DEFAULT '{}',
    published_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
""",
    "postgres": """
CREATE TABLE IF NOT EXISTS blog_posts (
    id SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    indexed INTEGER NOT NULL DEFAULT 1,
    engine_meta TEXT NOT NULL DEFAULT '{}',
    published_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
""",
}


class Database:
    def __init__(self, url, connect=True):
        self.url = url
        self.dialect = "postgres" if url.startswith("postgresql") else "sqlite"
        if self.dialect == "sqlite":
            self.path = url.replace("sqlite:///", "", 1)
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.conn = None
        if connect:
            self._connect()

    def _connect(self):
        if self.dialect == "postgres":
            from psycopg2.extras import RealDictCursor
            self.conn = psycopg2.connect(self.url, cursor_factory=RealDictCursor)
        else:
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            # 다중 커넥션 가시성 안정화 — WAL + busy_timeout (동시 읽기/쓰기 경합 방지)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")

    def _q(self, sqlite_sql, pg_sql, params, fetch=False):
        for attempt in (0, 1):
            try:
                return self._q_once(sqlite_sql, pg_sql, params, fetch)
            except CONNECTION_ERRORS:
                if attempt == 1:
                    raise
                self._connect()

    def _q_once(self, sqlite_sql, pg_sql, params, fetch=False):
        if self.dialect == "postgres":
            with self.conn.cursor() as cur:
                cur.execute(pg_sql, params)
                rows = [dict(r) for r in cur.fetchall()] if fetch else None
                self.conn.commit()
            return rows
        cur = self.conn.execute(sqlite_sql, params)
        self.conn.commit()
        return [dict(r) for r in cur.fetchall()] if fetch else None

    def init(self):
        if self.dialect == "postgres":
            self._q(None, SCHEMAS["postgres"], ())
        else:
            self.conn.executescript(SCHEMAS["sqlite"])
            self.conn.commit()

    # ---------- 발행 API (2.7) ----------

    def upsert_post(self, slug, title, body_md, tags="", category="", image_url="",
                    engine_meta="{}", published_at="", updated_at=""):
        """slug 기준 멱등 — 없으면 INSERT, 있으면 UPDATE (PATCH 의미).
        반환: ('created'|'updated', id)"""
        existing = self.get_post_by_slug(slug)
        if existing:
            self._q(
                "UPDATE blog_posts SET title = ?, body_md = ?, tags = ?, "
                "category = ?, image_url = ?, engine_meta = ?, published_at = ?, "
                "updated_at = ? WHERE slug = ?",
                "UPDATE blog_posts SET title = %s, body_md = %s, tags = %s, "
                "category = %s, image_url = %s, engine_meta = %s, published_at = %s, "
                "updated_at = %s WHERE slug = %s",
                (title, body_md, tags, category, image_url, engine_meta,
                 published_at, updated_at, slug),
            )
            return ("updated", existing["id"])
        self._q(
            "INSERT INTO blog_posts (slug, title, body_md, tags, category, image_url, "
            "engine_meta, published_at, updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            "INSERT INTO blog_posts (slug, title, body_md, tags, category, image_url, "
            "engine_meta, published_at, updated_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (slug, title, body_md, tags, category, image_url, engine_meta,
             published_at, updated_at, updated_at),
        )
        return ("created", self.get_post_by_slug(slug)["id"])

    def set_post_status(self, slug, status, published_at="", updated_at=""):
        self._q(
            "UPDATE blog_posts SET status = ?, published_at = ?, updated_at = ? WHERE slug = ?",
            "UPDATE blog_posts SET status = %s, published_at = %s, updated_at = %s WHERE slug = %s",
            (status, published_at, updated_at, slug),
        )

    def delete_post(self, slug):
        self._q("DELETE FROM blog_posts WHERE slug = ?",
                "DELETE FROM blog_posts WHERE slug = %s", (slug,))

    # ---------- 공개 조회 (2.3) ----------

    def get_post_by_slug(self, slug, status=None):
        where = "slug = ?" + (" AND status = ?" if status else "")
        pg_where = "slug = %s" + (" AND status = %s" if status else "")
        params = (slug,) + ((status,) if status else ())
        rows = self._q(
            f"SELECT * FROM blog_posts WHERE {where}",
            f"SELECT * FROM blog_posts WHERE {pg_where}", params, fetch=True)
        return rows[0] if rows else None

    def list_posts(self, status="published", category="", tag="", page=1,
                   page_size=10, indexable_only=False):
        """목록 (페이지네이션) — 카테고리/태그 필터."""
        where, params = ["status = ?"], [status]
        pg_where = ["status = %s"]
        if category:
            where.append("category = ?")
            pg_where.append("category = %s")
            params.append(category)
        if tag:
            where.append("(',' || tags || ',') LIKE ?")
            pg_where.append("(',' || tags || ',') LIKE %s")
            params.append(f"%,{tag},%")
        if indexable_only:
            where.append("indexed = 1")
            pg_where.append("indexed = 1")
        # LIMIT ? OFFSET ? 순서에 맞춰 page_size → offset
        params.append(page_size)
        params.append((page - 1) * page_size)
        rows = self._q(
            f"SELECT * FROM blog_posts WHERE {' AND '.join(where)} "
            "ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
            f"SELECT * FROM blog_posts WHERE {' AND '.join(pg_where)} "
            "ORDER BY published_at DESC, id DESC LIMIT %s OFFSET %s",
            params, fetch=True)
        # where는 필터 조각만 보유 (offset/limit은 params에만) — 그대로 사용
        count_where = f" WHERE {' AND '.join(where)}" if where else ""
        pg_count_where = f" WHERE {' AND '.join(pg_where)}" if pg_where else ""
        count = self._q(
            f"SELECT COUNT(*) AS c FROM blog_posts{count_where}",
            f"SELECT COUNT(*) AS c FROM blog_posts{pg_count_where}",
            params[:-2], fetch=True)[0]["c"]
        return rows, count

    def list_categories(self):
        rows = self._q(
            "SELECT category, COUNT(*) AS c FROM blog_posts WHERE category != '' "
            "AND status = 'published' GROUP BY category ORDER BY c DESC",
            "SELECT category, COUNT(*) AS c FROM blog_posts WHERE category != '' "
            "AND status = 'published' GROUP BY category ORDER BY c DESC",
            (), fetch=True)
        return rows

    def list_tags(self):
        rows = self._q(
            "SELECT tags FROM blog_posts WHERE status = 'published' AND tags != ''",
            "SELECT tags FROM blog_posts WHERE status = 'published' AND tags != ''",
            (), fetch=True)
        seen = {}
        for r in rows:
            for t in (r["tags"] or "").split(","):
                t = t.strip()
                if t:
                    seen[t] = seen.get(t, 0) + 1
        return sorted(seen.items(), key=lambda kv: -kv[1])

    def related_posts(self, post, limit=3):
        """관련 글 — 태그 매칭 → 최신순 (2.5 AEO/GEO 내부 링크)."""
        tags = [t.strip() for t in (post.get("tags") or "").split(",") if t.strip()]
        if not tags:
            return []
        clauses, pg_clauses, params = [], [], [post["slug"], "published"]
        for i, t in enumerate(tags[:3]):
            clauses.append("(',' || tags || ',') LIKE ?")
            pg_clauses.append("(',' || tags || ',') LIKE %s")
            params.append(f"%,{t},%")
        params.append(limit)
        return self._q(
            f"SELECT * FROM blog_posts WHERE slug != ? AND status = ? "
            f"AND ({' OR '.join(clauses)}) ORDER BY published_at DESC LIMIT ?",
            f"SELECT * FROM blog_posts WHERE slug != %s AND status = %s "
            f"AND ({' OR '.join(pg_clauses)}) ORDER BY published_at DESC LIMIT %s",
            params, fetch=True)

    def close(self):
        if self.conn:
            self.conn.close()
