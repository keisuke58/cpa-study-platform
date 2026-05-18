#!/usr/bin/env python3
"""
公開データ一括ダウンローダー

取得対象:
  1. 金融庁 公認会計士試験 過去問 (2006〜最新年)
     - 短答式 第I・II回 (科目別PDF)
     - 論文式 (科目別PDF)
  2. 日商簿記 1〜3級 公式サンプル問題 (kentei.ne.jp)
  3. OpenStax 会計テキスト (CC-BY, 英語)

Usage:
    python public_downloader.py           # 全部
    python public_downloader.py --source fsa
    python public_downloader.py --source boki
    python public_downloader.py --source openstax
"""

import argparse
import hashlib
import re
import sqlite3
import time
from pathlib import Path

import requests
import fitz  # PyMuPDF

REPO_DIR = Path(__file__).parent
PDF_DIR  = REPO_DIR / "PDF" / "public"
DB_PATH  = REPO_DIR / "studyin.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# ──────────────────────────────────────────────────────────────────────────────
# DB (scraper.py と共通スキーマ)
# ──────────────────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS courses (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            url  TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS pdfs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id    INTEGER REFERENCES courses(id),
            title        TEXT,
            download_url TEXT UNIQUE,
            local_path   TEXT,
            pdf_type     TEXT,
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


def ensure_course(conn, cid, name, url):
    conn.execute("INSERT OR IGNORE INTO courses (id,name,url) VALUES (?,?,?)",
                 (cid, name, url))
    conn.commit()


def already_downloaded(conn, url):
    return conn.execute(
        "SELECT 1 FROM pdfs WHERE download_url=?", (url,)
    ).fetchone() is not None


def save_pdf(conn, course_id, title, dl_url, local_path,
             pdf_type, text, pages, sha):
    cur = conn.execute(
        "INSERT OR IGNORE INTO pdfs "
        "(course_id,title,download_url,local_path,pdf_type,page_count,text_content,sha256) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (course_id, title, dl_url, str(local_path), pdf_type, pages, text, sha),
    )
    if cur.lastrowid:
        conn.execute(
            "INSERT OR IGNORE INTO pdfs_fts(rowid,title,text_content) VALUES (?,?,?)",
            (cur.lastrowid, title, text),
        )
    conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────────────────────

def safe_fn(name: str, max_len: int = 100) -> str:
    return re.sub(r'[\\/*?:"<>|\n\r\t]', '_', name).strip()[:max_len]


def extract_text(path: Path) -> tuple[int, str]:
    try:
        doc = fitz.open(str(path))
        return len(doc), "\n".join(p.get_text() for p in doc)
    except Exception:
        return 0, ""


def fetch_html(sess, url, encoding="utf-8") -> str | None:
    try:
        r = sess.get(url, timeout=30, headers=HEADERS)
        if r.status_code != 200:
            return None
        r.encoding = r.apparent_encoding or encoding
        return r.text
    except Exception as e:
        print(f"    fetch error {url}: {e}")
        return None


def dl_pdf(sess, url, path: Path) -> bool:
    try:
        r = sess.get(url, timeout=90, stream=True, headers=HEADERS)
        if r.status_code != 200:
            return False
        data = r.content
        if b"%PDF" not in data[:20]:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True
    except Exception as e:
        print(f"    DL error: {e}")
        return False


def process_pdf(conn, sess, course_id, title, pdf_url,
                out_path: Path, pdf_type: str) -> bool:
    """ダウンロード → テキスト抽出 → DB保存。新規保存なら True。"""
    if already_downloaded(conn, pdf_url):
        print(f"    skip (済): {title[:50]}")
        return False

    ok = dl_pdf(sess, pdf_url, out_path)
    if not ok:
        print(f"    DL失敗: {title[:50]}")
        return False

    pages, text = extract_text(out_path)
    sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
    save_pdf(conn, course_id, title, pdf_url, out_path,
             pdf_type, text, pages, sha)
    print(f"    ✓ [{pdf_type}] {title[:60]}  ({pages}p)")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# 1. 金融庁 公認会計士試験 過去問
# ──────────────────────────────────────────────────────────────────────────────

FSA_BASE = "https://www.fsa.go.jp"
FSA_CID  = 9001
FSA_SHIKEN_PATH = "/cpaaob/kouninkaikeishi-shiken/"

FSA_YEAR_PAGES = [
    (2025, "2025shiken.html"),
    (2024, "2024shiken.html"),
    (2023, "2023shiken.html"),
    (2022, "2022shiken.html"),
    (2021, "2021shiken.html"),
    (2020, "2020shiken.html"),
    (2019, "2019shiken.html"),
    (2018, "2018shiken.html"),
    (2017, "2017shiken.html"),
    (2016, "2016shiken.html"),
    (2015, "2015shiken.html"),
    (2014, "2014shiken.html"),
    (2013, "2013shiken.html"),
    (2012, "2012shiken.html"),
    (2011, "2011shiken.html"),
    (2010, "2010shiken.html"),
    (2009, "2009shiken.html"),
    (2008, "2008shiken.html"),
    (2007, "2007shiken.html"),
    (2006, "18shiken.html"),
]

# 西暦 → 和暦プレフィックス (rX=令和, hX=平成)
YEAR_TO_WAREKI = {
    2025: "r7", 2024: "r6", 2023: "r5", 2022: "r4", 2021: "r3",
    2020: "r2", 2019: "r1",
    2018: "h30", 2017: "h29", 2016: "h28", 2015: "h27",
    2014: "h26", 2013: "h25", 2012: "h24", 2011: "h23",
    2010: "h22", 2009: "h21", 2008: "h20", 2007: "h19", 2006: "h18",
}

SAIYOU_FILTER = "/cpaaob/shinsakai/recruit/"  # 採用情報PDFを除外


def _resolve_href(href: str, page_url: str) -> str:
    """href を絶対 URL に変換"""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return FSA_BASE + href
    base_dir = page_url.rsplit("/", 1)[0]
    return base_dir + "/" + href


def _exam_type_from_url(url: str) -> str:
    if "ronbun" in url:
        return "論文式"
    if "tantou" in url:
        return "短答式"
    return "その他"


def _pdf_subject_from_link_text(text: str) -> str:
    t = text.strip()
    if not t:
        return "その他"
    for subj in ["財務会計論", "管理会計論", "監査論", "企業法", "租税法",
                 "経営学", "会計学", "経済学", "民法", "統計学", "答案用紙"]:
        if subj in t:
            return subj
    return t[:30]


def _extract_subpage_pdfs(conn, sess, year, sp_url, out_base):
    """サブページから PDF リンクを全て取得してダウンロード。保存件数を返す。"""
    exam_type = _exam_type_from_url(sp_url)
    sp_html = fetch_html(sess, sp_url)
    if not sp_html:
        return 0

    saved = 0
    pdf_hrefs = re.findall(r'href=["\']([^"\']+\.pdf)["\']', sp_html, re.IGNORECASE)
    seen = set()
    for href in pdf_hrefs:
        abs_pdf = _resolve_href(href, sp_url)
        if SAIYOU_FILTER in abs_pdf or abs_pdf in seen:
            continue
        seen.add(abs_pdf)

        # リンクテキストを周辺 HTML から取得
        fn_pat = re.escape(abs_pdf.split("/")[-1].split("?")[0])
        m = re.search(
            rf'href=["\'][^"\']*{fn_pat}["\'][^>]*>(.*?)</a>',
            sp_html, re.DOTALL | re.IGNORECASE,
        )
        link_text = re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ""
        subject = _pdf_subject_from_link_text(link_text or abs_pdf.split("/")[-1])
        pdf_type_str = "答案用紙" if ("答案" in link_text or "touan" in abs_pdf.lower()) else "設問"

        stem = abs_pdf.split("/")[-1].split("?")[0].replace(".pdf", "")
        title = f"[{year}年 {exam_type}] {subject}"
        fname = safe_fn(f"{year}_{exam_type}_{subject}_{stem}") + ".pdf"
        out_path = out_base / str(year) / fname

        ok = process_pdf(conn, sess, FSA_CID, title, abs_pdf, out_path, pdf_type_str)
        if ok:
            saved += 1
        time.sleep(0.3)
    return saved


def scrape_fsa(conn: sqlite3.Connection, sess: requests.Session):
    print("\n" + "="*60)
    print("[金融庁] 公認会計士試験 過去問 (短答式・論文式)")
    print("="*60)
    ensure_course(conn, FSA_CID, "公認会計士試験 過去問 (金融庁)",
                  FSA_BASE + FSA_SHIKEN_PATH + "kakoshiken.html")

    total = 0
    out_base = PDF_DIR / "金融庁_CPA過去問"

    for year, year_file in FSA_YEAR_PAGES:
        year_url = FSA_BASE + FSA_SHIKEN_PATH + year_file
        wareki   = YEAR_TO_WAREKI.get(year, "")
        print(f"\n  [{year}] {year_url}")

        html = fetch_html(sess, year_url)

        subpage_urls: set[str] = set()
        direct_pdfs: set[str] = set()

        if html:
            # href を全抽出（ネスト済みタグのリンクも正しく取得）
            for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
                if SAIYOU_FILTER in href:
                    continue
                abs_h = _resolve_href(href, year_url)
                if FSA_SHIKEN_PATH not in abs_h:
                    continue
                if abs_h.lower().endswith(".pdf"):
                    direct_pdfs.add(abs_h)
                elif abs_h.endswith(".html"):
                    # 問題ページ: tantou_mondai_r06a.html (親ディレクトリ) も
                    #             ronbun_mondai_r06.html (サブディレクトリ) も "mondai" を含む
                    page_name = abs_h.split("/")[-1].lower()
                    if "mondai" in page_name:
                        subpage_urls.add(abs_h)

        # フォールバック: 既知パターンで直接 URL を構築して HEAD 確認
        # tantou_mondai は親ディレクトリ、ronbun_mondai はサブディレクトリ
        if not subpage_urls and not direct_pdfs and wareki:
            num = wareki.lstrip("rh")
            num_padded = num.zfill(2)
            base_par = FSA_BASE + FSA_SHIKEN_PATH
            base_sub = f"{base_par}{wareki}shiken/"
            candidates = [
                f"{base_sub}ronbun_mondai_{wareki}{num_padded}.html",
                f"{base_par}tantou_mondai_{wareki}{num_padded}a.html",
                f"{base_par}tantou_mondai_{wareki}{num_padded}b.html",
                f"{base_par}tantou_mondai_{wareki}{num_padded}.html",
                f"{base_sub}mondai.html",
            ]
            for cand in candidates:
                try:
                    r = sess.head(cand, timeout=10, headers=HEADERS, allow_redirects=True)
                    if r.status_code == 200:
                        subpage_urls.add(cand)
                except Exception:
                    pass

        print(f"    {len(subpage_urls)} サブページ, {len(direct_pdfs)} 直接PDF")

        # 直接PDFリンク（旧年度）
        for abs_pdf in direct_pdfs:
            exam_type = _exam_type_from_url(abs_pdf)
            stem = abs_pdf.split("/")[-1].replace(".pdf", "")
            title = f"[{year}年 {exam_type}] {stem}"
            fname = safe_fn(f"{year}_{exam_type}_{stem}") + ".pdf"
            out_path = out_base / str(year) / fname
            ok = process_pdf(conn, sess, FSA_CID, title, abs_pdf, out_path, "設問")
            if ok:
                total += 1
            time.sleep(0.3)

        # サブページ → PDF
        for sp_url in sorted(subpage_urls):
            total += _extract_subpage_pdfs(conn, sess, year, sp_url, out_base)

    print(f"\n[金融庁] 合計 {total} PDF 新規保存")
    return total


# ──────────────────────────────────────────────────────────────────────────────
# 2. 日商簿記 公式サンプル問題
# ──────────────────────────────────────────────────────────────────────────────

BOKI_BASE = "https://www.kentei.ne.jp"
BOKI_CIDS = {1: 9011, 2: 9012, 3: 9013}
BOKI_NAMES = {1: "日商簿記1級", 2: "日商簿記2級", 3: "日商簿記3級"}

# 公式サンプル問題ページ
BOKI_SAMPLE_PAGES = [
    (1, "/bookkeeping/candidate/example-1"),
    (2, "/bookkeeping/candidate/example-2"),
    (3, "/bookkeeping/candidate/example-3"),
]

# 既知の公式PDF URL（ページから見つからない場合のフォールバック）
BOKI_KNOWN_PDFS = [
    # 2023年改訂版サンプル問題
    (3, "簿記3級 サンプル問題（2023年度）",
     "https://www.kentei.ne.jp/wp-content/uploads/2023/10/3kyu_sample_2023.pdf"),
    (2, "簿記2級 サンプル問題 商業簿記（2023年度）",
     "https://www.kentei.ne.jp/wp-content/uploads/2023/10/2kyu_sho_sample_2023.pdf"),
    (2, "簿記2級 サンプル問題 工業簿記（2023年度）",
     "https://www.kentei.ne.jp/wp-content/uploads/2023/10/2kyu_ko_sample_2023.pdf"),
    (1, "簿記1級 サンプル問題 商業簿記・会計学（2023年度）",
     "https://www.kentei.ne.jp/wp-content/uploads/2023/10/1kyu_sho_sample_2023.pdf"),
    (1, "簿記1級 サンプル問題 工業簿記・原価計算（2023年度）",
     "https://www.kentei.ne.jp/wp-content/uploads/2023/10/1kyu_ko_sample_2023.pdf"),
    # 2019年以前の旧サンプル問題 (公開されている場合)
    (3, "簿記3級 サンプル問題（旧版）",
     "https://www.kentei.ne.jp/wp-content/uploads/2019/07/3kyu_sample.pdf"),
    (2, "簿記2級 サンプル問題 商業簿記（旧版）",
     "https://www.kentei.ne.jp/wp-content/uploads/2019/07/2kyu_sho_sample.pdf"),
    (2, "簿記2級 サンプル問題 工業簿記（旧版）",
     "https://www.kentei.ne.jp/wp-content/uploads/2019/07/2kyu_ko_sample.pdf"),
    (1, "簿記1級 サンプル問題 商業簿記・会計学（旧版）",
     "https://www.kentei.ne.jp/wp-content/uploads/2019/07/1kyu_sho_sample.pdf"),
    (1, "簿記1級 サンプル問題 工業簿記・原価計算（旧版）",
     "https://www.kentei.ne.jp/wp-content/uploads/2019/07/1kyu_ko_sample.pdf"),
]


def scrape_boki(conn: sqlite3.Connection, sess: requests.Session):
    print("\n" + "="*60)
    print("[日商簿記] 1〜3級 公式サンプル問題")
    print("="*60)

    for grade in [1, 2, 3]:
        ensure_course(conn, BOKI_CIDS[grade], BOKI_NAMES[grade],
                      BOKI_BASE + "/bookkeeping")

    out_dir = PDF_DIR / "日商簿記"
    total = 0

    # ① 既知 URL から試行
    for grade, title, url in BOKI_KNOWN_PDFS:
        cid = BOKI_CIDS[grade]
        fname = safe_fn(title) + ".pdf"
        out_path = out_dir / f"{grade}級" / fname
        ok = process_pdf(conn, sess, cid, title, url, out_path, "サンプル問題")
        if ok:
            total += 1
        time.sleep(0.4)

    # ② 公式サンプルページをスクレイプして追加 PDF を収集
    for grade, path in BOKI_SAMPLE_PAGES:
        cid = BOKI_CIDS[grade]
        page_url = BOKI_BASE + path
        print(f"\n  [{BOKI_NAMES[grade]}] {page_url}")

        html = fetch_html(sess, page_url)
        if not html:
            continue

        pdf_links = re.findall(r'href="([^"]+\.pdf)"', html, re.IGNORECASE)
        pdf_links = list(dict.fromkeys(pdf_links))
        print(f"    {len(pdf_links)} PDFリンク")

        for rel in pdf_links:
            abs_url = rel if rel.startswith("http") else BOKI_BASE + rel
            fname = safe_fn(Path(abs_url.split("?")[0]).name)
            out_path = out_dir / f"{grade}級" / fname
            title = f"[{BOKI_NAMES[grade]}] {fname.replace('.pdf','')}"
            ok = process_pdf(conn, sess, cid, title, abs_url, out_path, "サンプル問題")
            if ok:
                total += 1
            time.sleep(0.4)

    print(f"\n[日商簿記] 合計 {total} PDF 新規保存")
    return total


# ──────────────────────────────────────────────────────────────────────────────
# 3. OpenStax 会計テキスト (CC-BY)
# ──────────────────────────────────────────────────────────────────────────────

OPENSTAX_CID = 9003

OPENSTAX_BOOKS = [
    {
        "title":    "Principles of Accounting Vol.1 - Financial Accounting (OpenStax)",
        "slug":     "principles-of-accounting-volume-1-financial-accounting",
        "fallback": "PrinciplesFinancialAccounting-OP_LR.pdf",
    },
    {
        "title":    "Principles of Accounting Vol.2 - Managerial Accounting (OpenStax)",
        "slug":     "principles-of-accounting-volume-2-managerial-accounting",
        "fallback": "PrinciplesManagerialAccounting-OP_LR.pdf",
    },
    {
        "title":    "Financial Accounting (OpenStax)",
        "slug":     "financial-accounting",
        "fallback": "FinancialAccounting-OP_LR.pdf",
    },
]

OPENSTAX_CDN = "https://assets.openstax.org/oscms-prodcms/media/documents/"


def _find_openstax_pdf_url(sess: requests.Session, slug: str) -> str | None:
    """OpenStax 書籍ページから PDF ダウンロード URL を取得"""
    book_url = f"https://openstax.org/books/{slug}/pages/1-introduction"
    html = fetch_html(sess, book_url)
    if not html:
        return None

    # assets.openstax.org の .pdf URL を探す
    m = re.search(r'https://assets\.openstax\.org/[^\s"\']+\.pdf', html)
    if m:
        return m.group(0)

    # JSON-LD / script タグ内の pdf_url
    m = re.search(r'"pdf_url"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1)

    # API エンドポイント経由
    api_url = f"https://openstax.org/api/v2/pages/?slug={slug}&format=json"
    api_html = fetch_html(sess, api_url)
    if api_html:
        m = re.search(r'"pdf_url"\s*:\s*"([^"]+)"', api_html)
        if m:
            return m.group(1)

    return None


def scrape_openstax(conn: sqlite3.Connection, sess: requests.Session):
    print("\n" + "="*60)
    print("[OpenStax] 会計テキスト (CC-BY)")
    print("="*60)
    ensure_course(conn, OPENSTAX_CID,
                  "OpenStax Accounting Textbooks (CC-BY)",
                  "https://openstax.org/subjects/business")

    out_dir = PDF_DIR / "OpenStax"
    total = 0

    for book in OPENSTAX_BOOKS:
        title    = book["title"]
        slug     = book["slug"]
        fallback = book["fallback"]
        out_path = out_dir / fallback

        print(f"\n  {title}")

        # 書籍ページから PDF URL を動的取得
        pdf_url = _find_openstax_pdf_url(sess, slug)

        # フォールバック: 既知 CDN パス
        if not pdf_url:
            pdf_url = OPENSTAX_CDN + fallback
            print(f"    動的取得失敗 → CDN フォールバック")

        print(f"    URL: {pdf_url}")
        ok = process_pdf(conn, sess, OPENSTAX_CID, title, pdf_url,
                         out_path, "テキスト")
        if ok:
            total += 1
        time.sleep(1)

    print(f"\n[OpenStax] 合計 {total} PDF 新規保存")
    return total


# ──────────────────────────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="公開データ一括ダウンローダー")
    parser.add_argument("--source", default="all",
                        choices=["all", "fsa", "boki", "openstax"])
    args = parser.parse_args()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    sess = requests.Session()
    sess.headers.update(HEADERS)
    grand = 0

    if args.source in ("all", "fsa"):
        grand += scrape_fsa(conn, sess)
    if args.source in ("all", "boki"):
        grand += scrape_boki(conn, sess)
    if args.source in ("all", "openstax"):
        grand += scrape_openstax(conn, sess)

    conn.close()
    print(f"\n{'='*60}")
    print(f"全ソース合計: {grand} PDF 新規保存")
    print(f"DB: {DB_PATH}")
    print(f"検索: python search.py '棚卸資産'")


if __name__ == "__main__":
    main()
