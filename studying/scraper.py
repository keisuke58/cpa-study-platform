#!/usr/bin/env python3
"""
studying.jp 全教材スクレイパー
- Playwright でログイン → セッションクッキーを requests に渡す
- 全コースの練習問題 (practice) ページを走査
- waterMark/disposition/download リンクから 設問/解答 PDF を直接ダウンロード
- SQLite/FTS5 でテキスト検索 DB を構築

Usage:
    python scraper.py                   # .env から認証情報を読む（全件）
    python scraper.py --max-courses 1   # テスト（1コースだけ）
    python scraper.py --max-pdfs 10     # テスト（10 PDF だけ）
"""

import asyncio
import hashlib
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import requests
import fitz  # PyMuPDF
from playwright.async_api import async_playwright, Page

# ──────────────────────────────────────────────────────────────────────────────
BASE_URL = "https://member.studying.jp"
REPO_DIR = Path(__file__).parent
PDF_DIR  = REPO_DIR / "PDF"
DB_PATH  = REPO_DIR / "studyin.db"

# 公認会計士コース ID（コース一覧ページで確認済み）
CPA_COURSE_IDS = [2098, 2109, 2110, 2099, 2106, 2107, 2252]

# ──────────────────────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
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
            pdf_type     TEXT,  -- 設問 / 解答 / その他
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


def already_downloaded(conn, url: str) -> bool:
    return conn.execute("SELECT 1 FROM pdfs WHERE download_url=?", (url,)).fetchone() is not None


def save_pdf_record(conn, course_id, title, dl_url, local_path, pdf_type, text, page_count, sha):
    conn.execute(
        "INSERT OR IGNORE INTO pdfs "
        "(course_id, title, download_url, local_path, pdf_type, page_count, text_content, sha256) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (course_id, title, dl_url, str(local_path), pdf_type, page_count, text, sha),
    )
    conn.execute(
        "INSERT INTO pdfs_fts(rowid, title, text_content) "
        "SELECT id, title, text_content FROM pdfs WHERE download_url=? AND id NOT IN "
        "(SELECT rowid FROM pdfs_fts)",
        (dl_url,),
    )
    conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────────────────────

def safe_filename(name: str, max_len: int = 100) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name)[:max_len]


def extract_pdf_text(path: Path) -> tuple[int, str]:
    try:
        doc = fitz.open(str(path))
        pages = [p.get_text() for p in doc]
        return len(doc), "\n".join(pages)
    except Exception:
        return 0, ""


def pdf_type_from_title(title: str) -> str:
    if "設問" in title:
        return "設問"
    if "解答" in title:
        return "解答"
    return "その他"


# ──────────────────────────────────────────────────────────────────────────────
# ログイン & セッション取得
# ──────────────────────────────────────────────────────────────────────────────

async def login_and_get_session(email: str, password: str) -> tuple[requests.Session, list]:
    """Playwright でログインし、requests.Session にクッキーを移す"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
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

        cookies = await ctx.cookies()
        await browser.close()

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                      " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": BASE_URL + "/",
    })
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    return sess


# ──────────────────────────────────────────────────────────────────────────────
# コース情報取得
# ──────────────────────────────────────────────────────────────────────────────

async def get_course_names(email: str, password: str) -> dict[int, str]:
    """コース一覧から ID → 名前 マッピングを取得"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(BASE_URL + "/login", wait_until="domcontentloaded")
        await page.fill('input[name="mail"]', email)
        await page.fill('input[name="password"]', password)
        await page.click('a.a_submit')
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)
        await page.click('a:has-text("コース")', timeout=5000)
        await page.wait_for_timeout(2000)

        links = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href*="/course/id/"]'))
                .map(a => ({name: a.innerText.trim(), href: a.href}))
                .filter(x => x.name.length > 1)
        """)
        await browser.close()

    result = {}
    for l in links:
        m = re.search(r'/course/id/(\d+)/', l["href"])
        if m:
            cid = int(m.group(1))
            if cid not in result and l["name"]:
                result[cid] = l["name"]
    return result


# ──────────────────────────────────────────────────────────────────────────────
# practice ページ URL 取得（Playwright で SPA レンダリング後に取得）
# ──────────────────────────────────────────────────────────────────────────────

async def get_practice_urls(page: Page, course_id: int) -> list[str]:
    """コース詳細ページから全 practice URL を取得（Playwright 使用）"""
    await page.goto(f"{BASE_URL}/course/id/{course_id}/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    links = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('a[href]'))
            .map(a => a.href)
            .filter(h => h.includes('/course/practice/') || h.includes('/course/text/'))
    """)

    seen = set()
    result = []
    for url in links:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# practice ページから PDF ダウンロードリンクを取得
# ──────────────────────────────────────────────────────────────────────────────

def get_pdf_links_from_practice(sess: requests.Session, practice_url: str) -> list[dict]:
    """practice ページを requests で取得して waterMark ダウンロードリンクを抽出"""
    from html.parser import HTMLParser

    class PdfLinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
            self._current_text = ""
            self._in_link = False

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                for k, v in attrs:
                    if k == "href" and v and "waterMark/disposition/download" in v:
                        self._in_link = True
                        url = v if v.startswith("http") else BASE_URL + "/" + v.lstrip("/")
                        self.links.append({"url": url, "text": ""})
            elif tag in ("span", "div") and self._in_link:
                pass

        def handle_data(self, data):
            if self._in_link and self.links:
                self.links[-1]["text"] += data.strip()

        def handle_endtag(self, tag):
            if tag == "a":
                self._in_link = False

    r = sess.get(practice_url, timeout=30)
    if r.status_code != 200:
        return []

    parser = PdfLinkParser()
    parser.feed(r.text)

    # テキストが空のリンクにはファイル名から推測
    result = []
    seen = set()
    for link in parser.links:
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        result.append({"url": link["url"], "title": link["text"].strip()})
    return result


# ──────────────────────────────────────────────────────────────────────────────
# PDF ダウンロード & 保存
# ──────────────────────────────────────────────────────────────────────────────

def download_pdf(sess: requests.Session, url: str, out_dir: Path, title_hint: str = "") -> tuple[Path | None, str]:
    """PDF をダウンロードして保存。(path, title) を返す"""
    try:
        r = sess.get(url, allow_redirects=True, timeout=60, stream=True)
        if r.status_code != 200:
            return None, ""

        # ファイル名を Content-Disposition から取得
        cd = r.headers.get("content-disposition", "")
        fname_match = re.search(r"filename\*?=(?:UTF-8'')?([^\s;]+)", cd)
        if fname_match:
            fname = unquote(fname_match.group(1).strip('"'))
        elif title_hint:
            fname = safe_filename(title_hint) + ".pdf"
        else:
            fname = "unknown.pdf"

        fname = safe_filename(fname.replace(".pdf", "")) + ".pdf"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / fname

        if path.exists():
            return path, path.stem

        content = r.content
        if b"%PDF" not in content[:20]:
            return None, ""

        path.write_bytes(content)
        return path, path.stem

    except Exception as e:
        print(f"    DL error {url}: {e}")
        return None, ""


# ──────────────────────────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────────────────────────

async def run(email: str, password: str, max_courses: int = 0, max_pdfs: int = 0):
    PDF_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        nav_page = await ctx.new_page()

        # ① ログイン（Playwright）
        print("[login] ログイン中...")
        await nav_page.goto(BASE_URL + "/login", wait_until="domcontentloaded", timeout=60000)
        await nav_page.wait_for_timeout(1000)
        await nav_page.fill('input[name="mail"]', email)
        await nav_page.fill('input[name="password"]', password)
        await nav_page.click('a.a_submit, button[type="submit"]')
        await nav_page.wait_for_load_state("domcontentloaded", timeout=60000)
        await nav_page.wait_for_timeout(2000)
        if "/login" in nav_page.url:
            raise RuntimeError(f"ログイン失敗: {nav_page.url}")
        print(f"[login] OK → {nav_page.url}")

        # ② セッションクッキーを requests に転送
        cookies = await ctx.cookies()
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"})
        for c in cookies:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        # ③ コース名取得
        print("[courses] コース名を取得中...")
        await nav_page.click('a:has-text("コース")', timeout=5000)
        await nav_page.wait_for_timeout(2000)
        links = await nav_page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href*="/course/id/"]'))
                .map(a => ({name: a.innerText.trim(), href: a.href}))
                .filter(x => x.name.length > 1)
        """)
        course_names = {}
        for l in links:
            m = re.search(r'/course/id/(\d+)/', l["href"])
            if m:
                cid = int(m.group(1))
                if cid not in course_names and l["name"]:
                    course_names[cid] = l["name"]
        print(f"  {len(course_names)} コース確認")

        target_ids = CPA_COURSE_IDS
        if max_courses > 0:
            target_ids = target_ids[:max_courses]

        total_pdfs = 0

        # ④ コースごとに処理
        for course_id in target_ids:
            name = course_names.get(course_id, f"course_{course_id}")
            print(f"\n[course {course_id}] {name}")

            conn.execute(
                "INSERT OR IGNORE INTO courses (id, name, url) VALUES (?,?,?)",
                (course_id, name, f"{BASE_URL}/course/id/{course_id}/"),
            )
            conn.commit()

            # ⑤ practice URL 収集（Playwright）
            practice_urls = await get_practice_urls(nav_page, course_id)
            print(f"  practice ページ: {len(practice_urls)}")

            out_dir = PDF_DIR / safe_filename(name)[:30]

            # ⑥ 各 practice ページの PDF をダウンロード
            for p_idx, p_url in enumerate(practice_urls, 1):
                print(f"  [{p_idx}/{len(practice_urls)}] {p_url}")

                pdf_links = get_pdf_links_from_practice(sess, p_url)
                if not pdf_links:
                    print(f"    PDF リンクなし（requests で取得できず）")
                    continue

                for link in pdf_links:
                    if already_downloaded(conn, link["url"]):
                        print(f"    skip (済): {link['title'][:40]}")
                        continue

                    pdf_path, title = download_pdf(sess, link["url"], out_dir, link["title"])
                    if not pdf_path:
                        continue

                    page_count, text = extract_pdf_text(pdf_path)
                    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
                    pdf_t = pdf_type_from_title(title)

                    save_pdf_record(conn, course_id, title, link["url"],
                                    pdf_path, pdf_t, text, page_count, sha)
                    print(f"    ✓ [{pdf_t}] {title[:50]}  ({page_count}p)")
                    total_pdfs += 1

                    if max_pdfs > 0 and total_pdfs >= max_pdfs:
                        print(f"\n[info] --max-pdfs {max_pdfs} 到達。終了。")
                        break
                    time.sleep(0.3)

                if max_pdfs > 0 and total_pdfs >= max_pdfs:
                    break

            if max_pdfs > 0 and total_pdfs >= max_pdfs:
                break

        await browser.close()

    conn.close()

    # 統計
    conn2 = sqlite3.connect(DB_PATH)
    n_pdfs    = conn2.execute("SELECT COUNT(*) FROM pdfs").fetchone()[0]
    n_courses = conn2.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
    conn2.close()

    print(f"\n{'='*50}")
    print(f"完了: {n_courses} コース / {n_pdfs} PDF")
    print(f"DB:   {DB_PATH}")
    print(f"PDF:  {PDF_DIR}/")
    print(f"\n検索: python search.py '貸倒引当金'")


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

    parser = argparse.ArgumentParser(description="studying.jp 全教材スクレイパー")
    parser.add_argument("--email",       default=os.environ.get("STUDYIN_EMAIL"))
    parser.add_argument("--password",    default=os.environ.get("STUDYIN_PASSWORD"))
    parser.add_argument("--max-courses", type=int, default=0, help="テスト: コース数上限")
    parser.add_argument("--max-pdfs",    type=int, default=0, help="テスト: PDF数上限")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("[error] studying/.env に認証情報を設定してください。")
        sys.exit(1)

    asyncio.run(run(args.email, args.password, args.max_courses, args.max_pdfs))
