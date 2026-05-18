#!/usr/bin/env python3
"""
studying.jp 全教材スクレイパー v2

取得対象:
  - 全コースのシラバス PDF / Excel
  - 全サブカテゴリ → 全レッスン
    - トレーニング/練習問題: waterMarkContentZip API → ZIP (設問+解答 PDF)
    - スマート問題集: 同上
    - 基本講座: テキスト教材があれば取得

Usage:
    python scraper.py                   # 全件
    python scraper.py --max-courses 1   # テスト（1コースだけ）
    python scraper.py --max-zips 20     # テスト（ZIP20個まで）
"""

import asyncio
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote

import requests
import fitz  # PyMuPDF
from playwright.async_api import async_playwright

# ──────────────────────────────────────────────────────────────────────────────
BASE_URL = "https://member.studying.jp"
REPO_DIR = Path(__file__).parent
PDF_DIR  = REPO_DIR / "PDF"
DB_PATH  = REPO_DIR / "studyin.db"

CPA_COURSE_IDS = [2098, 2109, 2110, 2099, 2106, 2107, 2252]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# ──────────────────────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    # WAL モード: 並列プロセスからの同時書き込みに対応
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS courses (
            id    INTEGER PRIMARY KEY,
            name  TEXT NOT NULL,
            url   TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS pdfs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id    INTEGER REFERENCES courses(id),
            title        TEXT,
            download_url TEXT UNIQUE,
            local_path   TEXT,
            pdf_type     TEXT,  -- 設問 / 解答 / シラバス / その他
            page_count   INTEGER,
            text_content TEXT,
            sha256       TEXT,
            scraped_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pdfs_fts USING fts5(
            title, text_content,
            content=pdfs, content_rowid=id,
            tokenize="trigram"
        );
    """)
    conn.commit()


def already_downloaded(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM pdfs WHERE download_url=?", (url,)
    ).fetchone() is not None


def save_pdf_record(conn, course_id, title, dl_url, local_path, pdf_type,
                    text, page_count, sha):
    cur = conn.execute(
        "INSERT OR IGNORE INTO pdfs "
        "(course_id, title, download_url, local_path, pdf_type, page_count, text_content, sha256) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (course_id, title, dl_url, str(local_path), pdf_type, page_count, text, sha),
    )
    if cur.lastrowid:
        conn.execute(
            "INSERT OR IGNORE INTO pdfs_fts(rowid, title, text_content) VALUES (?,?,?)",
            (cur.lastrowid, title, text),
        )
    conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────────────────────

def safe_filename(name: str, max_len: int = 80) -> str:
    return re.sub(r'[\\/*?:"<>|\n\r\t]', '_', name).strip()[:max_len]


def extract_pdf_text(path: Path) -> tuple[int, str]:
    try:
        doc = fitz.open(str(path))
        pages = [page.get_text() for page in doc]
        return len(doc), "\n".join(pages)
    except Exception:
        return 0, ""


def pdf_type_from_name(name: str) -> str:
    n = name.lower()
    if "設問" in n or "mondai" in n:
        return "設問"
    if "解答" in n or "kaito" in n:
        return "解答"
    if "シラバス" in n or "syllabus" in n:
        return "シラバス"
    return "その他"


# ──────────────────────────────────────────────────────────────────────────────
# ログイン
# ──────────────────────────────────────────────────────────────────────────────

async def login(email: str, password: str) -> requests.Session:
    """Playwright でログインし、requests.Session にクッキーを移す"""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        print("[login] ログイン中...")
        await page.goto(BASE_URL + "/login", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)
        await page.fill('input[name="mail"]', email)
        await page.fill('input[name="password"]', password)
        await page.click('a.a_submit, button[type="submit"]')
        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)

        if "/login" in page.url:
            raise RuntimeError(f"ログイン失敗: {page.url}")
        print(f"[login] OK → {page.url}")

        # コース名も取得
        await page.click('a:has-text("コース")', timeout=10000)
        await page.wait_for_timeout(2000)
        links = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href*="/course/id/"]'))
                .map(a => ({name: a.innerText.trim(), href: a.href}))
                .filter(x => x.name.length > 1)
        """)

        cookies = await ctx.cookies()
        await browser.close()

    sess = requests.Session()
    sess.headers.update(HEADERS)
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    course_names = {}
    for lnk in links:
        m = re.search(r'/course/id/(\d+)/', lnk["href"])
        if m:
            cid = int(m.group(1))
            if cid not in course_names and lnk["name"]:
                course_names[cid] = lnk["name"]

    print(f"[courses] {len(course_names)} コース確認: {list(course_names.keys())}")
    return sess, course_names


# ──────────────────────────────────────────────────────────────────────────────
# コース教材メタ (シラバス PDF / Excel)
# ──────────────────────────────────────────────────────────────────────────────

def download_course_meta(sess: requests.Session, course_id: int,
                         course_name: str, conn: sqlite3.Connection):
    out_dir = PDF_DIR / safe_filename(course_name)[:30]
    out_dir.mkdir(parents=True, exist_ok=True)

    for kind, ext, url in [
        ("syllabus", "pdf", f"{BASE_URL}/course/index/download-pdf/id/{course_id}/"),
        ("excel",    "xlsx", f"{BASE_URL}/course/index/download-excel/id/{course_id}/"),
    ]:
        if already_downloaded(conn, url):
            continue
        try:
            r = sess.get(url, timeout=60)
            if r.status_code != 200 or len(r.content) < 100:
                continue
            fname = f"{safe_filename(course_name)[:40]}_シラバス.{ext}"
            path = out_dir / fname
            path.write_bytes(r.content)
            if ext == "pdf":
                page_count, text = extract_pdf_text(path)
                sha = hashlib.sha256(r.content).hexdigest()
                save_pdf_record(conn, course_id, f"{course_name} シラバス",
                                url, path, "シラバス", text, page_count, sha)
                print(f"  [meta] シラバス PDF {page_count}p 保存")
            else:
                print(f"  [meta] Excel 保存: {fname}")
        except Exception as e:
            print(f"  [meta] {kind} 取得失敗: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# サブカテゴリ / レッスン 一覧取得
# ──────────────────────────────────────────────────────────────────────────────

def get_subcats(sess: requests.Session, course_id: int) -> list[str]:
    """コース内の全サブカテゴリ ID を取得 (lesson + practice 両型)"""
    r = sess.get(f"{BASE_URL}/course/index/list/id/{course_id}/", timeout=30)
    # lesson 型 (基本講座) と practice 型 (トレーニング) の両方を取得
    ids = re.findall(r"open_detail\('(?:lesson|practice)',\s*(\d+)", r.text)
    return list(dict.fromkeys(ids))  # 順序保持で重複除去


def get_lessons_in_subcat(sess: requests.Session, subcat_id: str) -> list[dict]:
    """サブカテゴリ内の全レッスン情報を取得"""
    r = sess.get(f"{BASE_URL}/course/index/lesson/id/{subcat_id}/", timeout=30)
    html = r.text

    lessons = []
    # <a> タグを1個ずつ処理（属性順序は不定）
    for anchor in re.findall(r'<a\b[^>]*>', html, re.DOTALL):
        href_m  = re.search(r'href=["\']([^"\']+course/(?:lesson|practice|question)[^"\']+)["\']', anchor)
        lid_m   = re.search(r'data-course-lesson-id=["\'](\d+)["\']', anchor)
        if not href_m:
            continue
        href = href_m.group(1)
        lesson_id = lid_m.group(1) if lid_m else None

        # URL から ID 抽出
        url_id_m = re.search(r'/id/(\d+)/', href)
        q_id_m   = re.search(r'/q/(\d+)/', href)
        url_id = (url_id_m or q_id_m)
        if not url_id:
            continue

        if "/practice/" in href:
            ltype = "practice"
        elif "/question/" in href:
            ltype = "question"
        else:
            ltype = "lesson"

        lessons.append({
            "url":       href if href.startswith("http") else BASE_URL + href,
            "url_id":    url_id.group(1),
            "lesson_id": lesson_id or url_id.group(1),
            "type":      ltype,
        })

    # data-course-lesson-id が <a> タグ外にある場合の補足
    # <li data-course-lesson-id="..."><a href="...">
    for li in re.findall(r'<li\b[^>]*data-course-lesson-id=["\'](\d+)["\'][^>]*>.*?</li>',
                         html, re.DOTALL):
        lid = re.search(r'data-course-lesson-id=["\'](\d+)["\']', li)
        href_m = re.search(r'href=["\']([^"\']+course/(?:lesson|practice|question)[^"\']+)["\']', li)
        if not lid or not href_m:
            continue
        href = href_m.group(1)
        url_id_m = re.search(r'/id/(\d+)/', href)
        q_id_m   = re.search(r'/q/(\d+)/', href)
        url_id = (url_id_m or q_id_m)
        if not url_id:
            continue
        # 既存エントリの lesson_id を上書き更新
        for entry in lessons:
            if entry["url_id"] == url_id.group(1):
                entry["lesson_id"] = lid.group(1)
                break

    return lessons


# ──────────────────────────────────────────────────────────────────────────────
# タイトル解決（レッスンページから）
# ──────────────────────────────────────────────────────────────────────────────

def get_lesson_title(sess: requests.Session, lesson_url: str) -> str:
    """レッスンページの <title> または h1 から表示名を取得"""
    try:
        r = sess.get(lesson_url, timeout=20)
        m = re.search(r'<title[^>]*>([^<]+)</title>', r.text)
        if m:
            t = m.group(1).strip()
            # "ページ名 | サイト名" の形式から前半を取る
            return t.split("|")[0].split("｜")[0].strip()
    except Exception:
        pass
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# ZIP ダウンロード (waterMarkContentZip API)
# ──────────────────────────────────────────────────────────────────────────────

def download_lesson_zip(sess: requests.Session, lesson_id: str,
                        out_dir: Path, conn: sqlite3.Connection,
                        course_id: int, title_hint: str = "") -> int:
    """
    waterMarkContentZip API → ZIP DL → 各 PDF を抽出・保存・DB登録。
    返値: 新規保存した PDF 数
    """
    api_url = f"{BASE_URL}/api/practice/waterMarkContentZip/id/{lesson_id}/"

    if already_downloaded(conn, api_url):
        return 0

    try:
        r = sess.get(api_url, timeout=30)
        if r.status_code != 200:
            return 0
        data = r.json()
        if data.get("status") != "200" or not data.get("downLoadUrl"):
            # 空レスポンスもマーク → 次回実行で再クエリしない
            conn.execute(
                "INSERT OR IGNORE INTO pdfs "
                "(course_id, title, download_url, pdf_type, scraped_at) "
                "VALUES (?,?,?,'_zip_marker',datetime('now'))",
                (course_id, f"no_content_{lesson_id}", api_url),
            )
            conn.commit()
            return 0

        zip_url  = data["downLoadUrl"]
        zip_name = data.get("fileName", f"lesson_{lesson_id}.zip")
        title_base = safe_filename(zip_name.replace(".zip", ""))

        # ZIP ダウンロード（pre-signed URL なのでクッキー不要）
        rz = requests.get(zip_url, timeout=120, stream=True, headers=HEADERS)
        if rz.status_code != 200:
            return 0
        zip_bytes = rz.content

    except Exception as e:
        print(f"      ZIP 取得失敗 [{lesson_id}]: {e}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".pdf"):
                    continue

                pdf_bytes = zf.read(name)
                if b"%PDF" not in pdf_bytes[:20]:
                    continue

                # ファイル名を UTF-8 デコード（CP437 / UTF-8 混在対策）
                try:
                    fname = name.encode("cp437").decode("utf-8")
                except Exception:
                    fname = name
                fname = Path(fname).name

                pdf_path = out_dir / safe_filename(fname)
                if not pdf_path.suffix:
                    pdf_path = pdf_path.with_suffix(".pdf")

                # 重複チェックは sha で
                sha = hashlib.sha256(pdf_bytes).hexdigest()
                dup = conn.execute(
                    "SELECT 1 FROM pdfs WHERE sha256=?", (sha,)
                ).fetchone()
                if dup:
                    continue

                pdf_path.write_bytes(pdf_bytes)
                page_count, text = extract_pdf_text(pdf_path)
                pdf_type = pdf_type_from_name(fname)
                title = safe_filename(fname.replace(".pdf", ""))[:120]

                # download_url としてはAPIのURLを使い、ファイル名+ハッシュで uniquify
                dl_url = f"{api_url}#{sha[:8]}"
                save_pdf_record(conn, course_id, title, dl_url,
                                pdf_path, pdf_type, text, page_count, sha)
                print(f"      ✓ [{pdf_type}] {title[:60]}  ({page_count}p)")
                saved += 1

        # ZIP 全体をスキップ済みとしてマーク（再ダウンロード防止）
        if saved > 0 or True:  # always mark attempted
            conn.execute(
                "INSERT OR IGNORE INTO pdfs "
                "(course_id, title, download_url, pdf_type, scraped_at) "
                "VALUES (?,?,?,'_zip_marker',datetime('now'))",
                (course_id, title_base, api_url),
            )
            conn.commit()

    except zipfile.BadZipFile as e:
        print(f"      ZIP 破損 [{lesson_id}]: {e}")

    return saved


# ──────────────────────────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────────────────────────

async def run(email: str, password: str, max_courses: int = 0, max_zips: int = 0,
             course_ids: list[int] | None = None):
    PDF_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    sess, course_names = await login(email, password)

    if course_ids:
        target_ids = course_ids
    elif max_courses > 0:
        target_ids = CPA_COURSE_IDS[:max_courses]
    else:
        target_ids = CPA_COURSE_IDS

    total_zips = 0
    total_pdfs = 0

    for course_id in target_ids:
        name = course_names.get(course_id, f"course_{course_id}")
        print(f"\n{'='*60}")
        print(f"[course {course_id}] {name}")
        print(f"{'='*60}")

        conn.execute(
            "INSERT OR IGNORE INTO courses (id, name, url) VALUES (?,?,?)",
            (course_id, name, f"{BASE_URL}/course/id/{course_id}/"),
        )
        conn.commit()

        # ① シラバス PDF + Excel
        download_course_meta(sess, course_id, name, conn)

        out_dir_base = PDF_DIR / safe_filename(name)[:30]

        # ② サブカテゴリ一覧
        subcats = get_subcats(sess, course_id)
        print(f"  サブカテゴリ: {len(subcats)} 件")

        for sc_idx, subcat_id in enumerate(subcats, 1):
            print(f"\n  [{sc_idx}/{len(subcats)}] subcat={subcat_id}")
            lessons = get_lessons_in_subcat(sess, subcat_id)

            # practice/question: waterMarkContentZip が確実に動く
            # lesson: 基本講座にも付属テキスト PDF がある場合があるので同 API を試みる
            all_downloadable = [l for l in lessons if l["type"] in ("practice", "question", "lesson")]
            practice_count = sum(1 for l in lessons if l["type"] in ("practice", "question"))
            print(f"    レッスン合計: {len(lessons)}  (practice/question: {practice_count})")

            for lsn in all_downloadable:
                ltype = lsn["type"]
                print(f"    → lesson_id={lsn['lesson_id']}  type={ltype}")

                n = download_lesson_zip(
                    sess, lsn["lesson_id"],
                    out_dir_base, conn, course_id,
                )
                total_pdfs += n
                if n > 0:
                    total_zips += 1

                # lesson 型は API 空のことが多いので短めに待つ
                time.sleep(0.2 if ltype == "lesson" else 0.5)

                if max_zips > 0 and total_zips >= max_zips:
                    print(f"\n[info] --max-zips {max_zips} 到達。終了。")
                    break

            if max_zips > 0 and total_zips >= max_zips:
                break

        if max_zips > 0 and total_zips >= max_zips:
            break

    conn.close()

    # 統計
    conn2 = sqlite3.connect(DB_PATH)
    n_pdfs    = conn2.execute("SELECT COUNT(*) FROM pdfs WHERE pdf_type != '_zip_marker'").fetchone()[0]
    n_courses = conn2.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
    by_type   = conn2.execute(
        "SELECT pdf_type, COUNT(*) FROM pdfs WHERE pdf_type != '_zip_marker' GROUP BY pdf_type"
    ).fetchall()
    conn2.close()

    print(f"\n{'='*60}")
    print(f"完了: {n_courses} コース / {n_pdfs} PDF")
    for t, c in by_type:
        print(f"  {t or 'その他': <12} {c} 件")
    print(f"DB:   {DB_PATH}")
    print(f"PDF:  {PDF_DIR}/")
    print(f"\n検索: python search.py '棚卸資産'")


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    parser = argparse.ArgumentParser(description="studying.jp 全教材スクレイパー v2")
    parser.add_argument("--email",       default=os.environ.get("STUDYIN_EMAIL"))
    parser.add_argument("--password",    default=os.environ.get("STUDYIN_PASSWORD"))
    parser.add_argument("--max-courses", type=int, default=0, help="テスト: コース数上限")
    parser.add_argument("--max-zips",    type=int, default=0, help="テスト: ZIP数上限")
    parser.add_argument("--course-ids",  type=int, nargs="+",
                        help="処理するコースIDを指定 (例: --course-ids 2109 2110)")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("[error] studying/.env に認証情報を設定してください。")
        sys.exit(1)

    asyncio.run(run(args.email, args.password,
                    args.max_courses, args.max_zips, args.course_ids))
