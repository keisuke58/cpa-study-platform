"""
Neon (PostgreSQL) クライアントラッパー。
DATABASE_URL 環境変数が設定されている場合に使用。

環境変数:
    DATABASE_URL — postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require

neon.tech: 無料枠 512MB, 接続は serverless (autosuspend あり)
"""

import os
from contextlib import contextmanager
from typing import Any

_pool = None


def _get_url() -> str | None:
    return os.environ.get("DATABASE_URL", "")


def is_available() -> bool:
    return bool(_get_url())


@contextmanager
def _conn():
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise RuntimeError("pip install psycopg2-binary")
    c = psycopg2.connect(_get_url())
    try:
        yield c
    finally:
        c.close()


# ── スマート問題集 ──────────────────────────────────────────────────────────

def fetch_smart_questions(course_ids: list[int] | None = None) -> list[dict]:
    if not is_available():
        return []
    try:
        with _conn() as c:
            cur = c.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor)
            if course_ids:
                cur.execute(
                    "SELECT id, course_id, title, text_content FROM pdfs "
                    "WHERE pdf_type=%s AND course_id = ANY(%s) ORDER BY course_id, id",
                    ("スマート問題集", course_ids),
                )
            else:
                cur.execute(
                    "SELECT id, course_id, title, text_content FROM pdfs "
                    "WHERE pdf_type=%s ORDER BY course_id, id",
                    ("スマート問題集",),
                )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[neon] fetch_smart_questions error: {e}")
        return []


def fetch_pdf_count() -> dict[str, int]:
    if not is_available():
        return {}
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT pdf_type, COUNT(*) FROM pdfs GROUP BY pdf_type")
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        print(f"[neon] fetch_pdf_count error: {e}")
        return {}


def fulltext_search(query: str, k: int = 10,
                    pdf_types: list[str] | None = None) -> list[dict]:
    """PostgreSQL FTS で検索"""
    if not is_available():
        return []
    try:
        with _conn() as c:
            cur = c.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor)
            type_clause = ""
            params: list[Any] = [query, k]
            if pdf_types:
                type_clause = "AND pdf_type = ANY(%s)"
                params.insert(1, pdf_types)
            cur.execute(
                f"""SELECT id, course_id, title, pdf_type, text_content
                    FROM pdfs
                    WHERE fts @@ plainto_tsquery('simple', %s)
                    {type_clause}
                    LIMIT %s""",
                params,
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[neon] fulltext_search error: {e}")
        return []
