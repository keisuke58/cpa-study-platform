-- =====================================================
-- CPA Study Platform — Supabase (PostgreSQL) Schema
-- =====================================================

-- courses テーブル
CREATE TABLE IF NOT EXISTS courses (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    url   TEXT UNIQUE
);

-- pdfs テーブル (studying.jp スクレイピングデータ)
CREATE TABLE IF NOT EXISTS pdfs (
    id           SERIAL PRIMARY KEY,
    course_id    INTEGER REFERENCES courses(id),
    title        TEXT,
    download_url TEXT UNIQUE,
    local_path   TEXT,
    pdf_type     TEXT,       -- 設問 / 解答 / スマート問題集 / その他
    page_count   INTEGER,
    text_content TEXT,
    sha256       TEXT,
    scraped_at   TIMESTAMPTZ DEFAULT now()
);

-- 全文検索用インデックス (PostgreSQL FTS)
ALTER TABLE pdfs ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(text_content, ''))
    ) STORED;

CREATE INDEX IF NOT EXISTS pdfs_fts_idx ON pdfs USING GIN(fts);

-- pdf_type インデックス (フィルタ高速化)
CREATE INDEX IF NOT EXISTS pdfs_pdf_type_idx ON pdfs(pdf_type);
CREATE INDEX IF NOT EXISTS pdfs_course_id_idx ON pdfs(course_id);

-- Row Level Security (読み取り公開、書き込み禁止)
ALTER TABLE courses ENABLE ROW LEVEL SECURITY;
ALTER TABLE pdfs    ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public read courses" ON courses FOR SELECT USING (true);
CREATE POLICY "public read pdfs"    ON pdfs    FOR SELECT USING (true);
