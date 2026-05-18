"""
Supabase クライアントラッパー。
app.py から SQLite の代わりに呼び出す。

環境変数:
    SUPABASE_URL  — https://xxxx.supabase.co
    SUPABASE_KEY  — anon key (読み取り専用でOK)
"""

import os
from functools import lru_cache
from typing import Any

_client = None


def get_supabase():
    global _client
    if _client is not None:
        return _client
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception:
        return None


def is_available() -> bool:
    return get_supabase() is not None


# ── スマート問題集 ─────────────────────────────────────────────────────────

def fetch_smart_questions(course_ids: list[int] | None = None) -> list[dict]:
    """スマート問題集を Supabase から取得"""
    sb = get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("pdfs").select(
            "id, course_id, title, text_content"
        ).eq("pdf_type", "スマート問題集").order("course_id").order("id")

        if course_ids:
            q = q.in_("course_id", course_ids)

        res = q.execute()
        return res.data or []
    except Exception as e:
        print(f"[supabase] fetch_smart_questions error: {e}")
        return []


def fetch_pdf_count() -> dict[str, int]:
    """pdf_type 別件数を返す"""
    sb = get_supabase()
    if sb is None:
        return {}
    try:
        res = sb.table("pdfs").select("pdf_type").execute()
        from collections import Counter
        return dict(Counter(r["pdf_type"] for r in (res.data or [])))
    except Exception:
        return {}


# ── 全文検索 ────────────────────────────────────────────────────────────────

def fulltext_search(query: str, k: int = 10,
                    pdf_types: list[str] | None = None) -> list[dict]:
    """PostgreSQL FTS で検索"""
    sb = get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("pdfs").select(
            "id, course_id, title, pdf_type, text_content"
        ).text_search("fts", query).limit(k)

        if pdf_types:
            q = q.in_("pdf_type", pdf_types)

        res = q.execute()
        return res.data or []
    except Exception as e:
        print(f"[supabase] fulltext_search error: {e}")
        return []
