#!/usr/bin/env python3
"""
studying.jp DB 全文検索

Usage:
    python search.py '棚卸資産'
    python search.py '減価償却' --limit 5
    python search.py '内部統制' --type 設問
    python search.py --stats
"""

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "studyin.db"


def search(query: str, limit: int = 10, pdf_type: str = None):
    if not DB_PATH.exists():
        print(f"[error] DB が見つかりません: {DB_PATH}")
        print("先に scraper.py を実行してください。")
        return

    conn = sqlite3.connect(DB_PATH)

    # trigram FTS は3文字以上、短いクエリは LIKE フォールバック
    use_fts = len(query) >= 3
    params = []

    if use_fts:
        base_sql = """
            SELECT p.title, p.pdf_type, p.download_url, p.local_path,
                   snippet(pdfs_fts, 1, '>>>>', '<<<<', '...', 20) AS snippet
            FROM pdfs_fts
            JOIN pdfs p ON pdfs_fts.rowid = p.id
            WHERE pdfs_fts MATCH ?
        """
        params = [query]
        if pdf_type:
            base_sql += " AND p.pdf_type = ?"
            params.append(pdf_type)
        base_sql += " ORDER BY rank LIMIT ?"
    else:
        base_sql = """
            SELECT title, pdf_type, download_url, local_path,
                   substr(text_content, instr(text_content, ?)-30, 80) AS snippet
            FROM pdfs
            WHERE text_content LIKE ?
        """
        params = [query, f"%{query}%"]
        if pdf_type:
            base_sql += " AND pdf_type = ?"
            params.append(pdf_type)
        base_sql += " LIMIT ?"

    params.append(limit)
    rows = conn.execute(base_sql, params).fetchall()
    conn.close()

    if not rows:
        print(f"「{query}」に一致する結果はありません。")
        return

    print(f"\n検索: 「{query}」  ({len(rows)} 件)\n")
    for i, (title, ptype, url, pdf_path, snippet) in enumerate(rows, 1):
        print(f"[{i}] {title}")
        print(f"     タイプ: {ptype}")
        if pdf_path:
            print(f"     PDF:  {pdf_path}")
        print(f"     {snippet}")
        print()


def stats():
    if not DB_PATH.exists():
        print("DB なし")
        return
    conn = sqlite3.connect(DB_PATH)
    n_pdfs    = conn.execute("SELECT COUNT(*) FROM pdfs").fetchone()[0]
    n_courses = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
    by_type   = conn.execute(
        "SELECT pdf_type, COUNT(*) FROM pdfs GROUP BY pdf_type"
    ).fetchall()
    by_course = conn.execute(
        "SELECT c.name, COUNT(p.id) FROM courses c LEFT JOIN pdfs p ON c.id=p.course_id GROUP BY c.id"
    ).fetchall()
    conn.close()

    print(f"DB: {DB_PATH}")
    print(f"コース: {n_courses}  PDF: {n_pdfs}")
    print("\n種別:")
    for ptype, cnt in by_type:
        print(f"  {ptype or 'その他': <10} {cnt}")
    print("\nコース別:")
    for cname, cnt in by_course:
        print(f"  {cname[:40]:40s} {cnt}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="studying.jp 全文検索")
    parser.add_argument("query",  nargs="?", default=None, help="検索クエリ")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--type",  default=None, help="設問 / 解答")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.stats or args.query is None:
        stats()
    else:
        search(args.query, args.limit, args.type)
