"""
SQLite (studyin.db) → Supabase (PostgreSQL) 移行スクリプト

実行前に環境変数を設定:
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_KEY="your-service-role-key"   # service_role key (書き込み権限)

実行:
    python migrate_to_supabase.py
    python migrate_to_supabase.py --batch 100      # バッチサイズ変更
    python migrate_to_supabase.py --type スマート問題集  # 特定タイプのみ
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "studyin.db"


def get_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL / SUPABASE_KEY 環境変数を設定してください")
        sys.exit(1)
    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        print("ERROR: pip install supabase")
        sys.exit(1)


def migrate_courses(client, conn):
    rows = conn.execute("SELECT id, name, url FROM courses").fetchall()
    print(f"courses: {len(rows)} 件 → Supabase へ")
    for r in rows:
        client.table("courses").upsert({
            "id": r[0], "name": r[1], "url": r[2]
        }).execute()
    print("  courses 完了")


def migrate_pdfs(client, conn, batch_size: int, pdf_type_filter: str | None):
    where = f"WHERE pdf_type = '{pdf_type_filter}'" if pdf_type_filter else ""
    total = conn.execute(f"SELECT COUNT(*) FROM pdfs {where}").fetchone()[0]
    print(f"pdfs: {total} 件 → Supabase へ (batch={batch_size})")

    offset = 0
    uploaded = 0
    while offset < total:
        rows = conn.execute(
            f"""SELECT id, course_id, title, download_url, local_path,
                       pdf_type, page_count, text_content, sha256, scraped_at
                FROM pdfs {where} LIMIT ? OFFSET ?""",
            (batch_size, offset),
        ).fetchall()

        batch = [
            {
                "id":           r[0],
                "course_id":    r[1],
                "title":        r[2],
                "download_url": r[3],
                "local_path":   r[4],
                "pdf_type":     r[5],
                "page_count":   r[6],
                "text_content": r[7],
                "sha256":       r[8],
                "scraped_at":   r[9],
            }
            for r in rows
        ]

        try:
            client.table("pdfs").upsert(batch).execute()
            uploaded += len(batch)
            print(f"  {uploaded}/{total} 完了")
        except Exception as e:
            print(f"  ERROR (offset={offset}): {e}")
            # 1件ずつリトライ
            for item in batch:
                try:
                    client.table("pdfs").upsert(item).execute()
                    uploaded += 1
                except Exception as e2:
                    print(f"    SKIP id={item['id']}: {e2}")
            time.sleep(1)

        offset += batch_size
        time.sleep(0.2)  # rate limit 対策

    print(f"pdfs 完了: {uploaded}/{total} 件")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=50, help="バッチサイズ")
    parser.add_argument("--type", type=str, default=None, help="特定 pdf_type のみ移行")
    parser.add_argument("--skip-courses", action="store_true")
    args = parser.parse_args()

    client = get_client()
    conn = sqlite3.connect(str(DB_PATH))

    if not args.skip_courses:
        migrate_courses(client, conn)

    migrate_pdfs(client, conn, args.batch, args.type)
    conn.close()
    print("移行完了!")


if __name__ == "__main__":
    main()
