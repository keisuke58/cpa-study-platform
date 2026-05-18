"""
SQLite (studyin.db) → Neon (PostgreSQL) 移行スクリプト

事前準備:
    pip install psycopg2-binary

環境変数:
    DATABASE_URL="postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require"

実行:
    python migrate_to_neon.py
    python migrate_to_neon.py --type スマート問題集   # 特定タイプのみ
    python migrate_to_neon.py --batch 20              # バッチサイズ変更
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "studyin.db"
SCHEMA_FILE = Path(__file__).parent / "supabase_schema.sql"


def get_conn():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("ERROR: DATABASE_URL 環境変数を設定してください")
        print("  例: export DATABASE_URL='postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require'")
        sys.exit(1)
    try:
        import psycopg2
        return psycopg2.connect(url)
    except ImportError:
        print("ERROR: pip install psycopg2-binary")
        sys.exit(1)


def apply_schema(pg):
    """supabase_schema.sql を Neon に適用（RLS 行は除外）"""
    sql = SCHEMA_FILE.read_text()
    # RLS / POLICY 文はスキップ（Neon 標準 PostgreSQL では不要）
    lines = [l for l in sql.splitlines()
             if not l.strip().upper().startswith(("ALTER TABLE", "CREATE POLICY"))]
    cleaned = "\n".join(lines)
    cur = pg.cursor()
    # 生成列 (GENERATED ALWAYS AS STORED) はNeonでサポート
    try:
        cur.execute(cleaned)
        pg.commit()
        print("スキーマ適用完了")
    except Exception as e:
        pg.rollback()
        print(f"スキーマ適用エラー: {e}")
        print("→ Neon SQL Editor で supabase_schema.sql を手動実行してください")


def migrate_courses(pg, sqlite_conn):
    rows = sqlite_conn.execute("SELECT id, name, url FROM courses").fetchall()
    cur = pg.cursor()
    for r in rows:
        cur.execute(
            "INSERT INTO courses (id, name, url) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, url=EXCLUDED.url",
            r,
        )
    pg.commit()
    print(f"courses: {len(rows)} 件 完了")


def migrate_pdfs(pg, sqlite_conn, batch_size: int, pdf_type_filter: str | None):
    where = f"WHERE pdf_type = '{pdf_type_filter}'" if pdf_type_filter else ""
    total = sqlite_conn.execute(f"SELECT COUNT(*) FROM pdfs {where}").fetchone()[0]
    print(f"pdfs: {total} 件 → Neon (batch={batch_size})")

    cur = pg.cursor()
    offset = 0
    uploaded = 0

    while offset < total:
        rows = sqlite_conn.execute(
            f"""SELECT id, course_id, title, download_url, local_path,
                       pdf_type, page_count, text_content, sha256, scraped_at
                FROM pdfs {where} LIMIT ? OFFSET ?""",
            (batch_size, offset),
        ).fetchall()

        try:
            cur.executemany(
                """INSERT INTO pdfs
                   (id, course_id, title, download_url, local_path,
                    pdf_type, page_count, text_content, sha256, scraped_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET
                     text_content=EXCLUDED.text_content,
                     scraped_at=EXCLUDED.scraped_at""",
                rows,
            )
            pg.commit()
            uploaded += len(rows)
            print(f"  {uploaded}/{total}")
        except Exception as e:
            pg.rollback()
            print(f"  ERROR (offset={offset}): {e}")
            # 1件ずつリトライ
            for row in rows:
                try:
                    cur.execute(
                        """INSERT INTO pdfs
                           (id, course_id, title, download_url, local_path,
                            pdf_type, page_count, text_content, sha256, scraped_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (id) DO NOTHING""",
                        row,
                    )
                    pg.commit()
                    uploaded += 1
                except Exception as e2:
                    pg.rollback()
                    print(f"    SKIP id={row[0]}: {e2}")
            time.sleep(1)

        offset += batch_size
        time.sleep(0.1)

    print(f"pdfs 完了: {uploaded}/{total} 件")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=30)
    parser.add_argument("--type", type=str, default=None)
    parser.add_argument("--skip-schema", action="store_true")
    parser.add_argument("--skip-courses", action="store_true")
    args = parser.parse_args()

    pg = get_conn()
    sqlite_conn = sqlite3.connect(str(DB_PATH))

    if not args.skip_schema:
        apply_schema(pg)
    if not args.skip_courses:
        migrate_courses(pg, sqlite_conn)

    migrate_pdfs(pg, sqlite_conn, args.batch, args.type)
    sqlite_conn.close()
    pg.close()
    print("移行完了!")


if __name__ == "__main__":
    main()
