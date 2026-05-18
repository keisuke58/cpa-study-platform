"""
studyin.db から 設問↔解答 ペアを抽出し、
- qa_pairs.jsonl  : fine-tune 用（設問テキスト → 解答テキスト）
- chunks.jsonl    : RAG 用（800字チャンク、overlap 200字）
を生成する。
"""

import sqlite3
import json
import re
from pathlib import Path

DB_PATH = Path(__file__).parent / "studyin.db"
OUT_DIR = Path(__file__).parent
QA_OUT = OUT_DIR / "qa_pairs.jsonl"
CHUNKS_OUT = OUT_DIR / "chunks.jsonl"

CHUNK_SIZE = 800
CHUNK_OVERLAP = 200

SYSTEM_PROMPT = "あなたは公認会計士試験（CPA）の専門家です。与えられた設問に対して、正確かつ丁寧な解答を提供してください。"


_KANJI_RANGE = r"[一-鿿぀-ヿ＀-￯]"


def _join_vertical_chars(text: str) -> str:
    """PDF縦書きテーブルで1文字ずつ改行される漢字列を結合する。

    例: "借\n方\n科\n目" → "借方科目"
    連続する単一文字行(漢字/かな)を1つの単語に結合する。
    """
    lines = text.split("\n")
    result = []
    buf = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) == 1 and re.match(_KANJI_RANGE, stripped):
            buf.append(stripped)
        else:
            if buf:
                result.append("".join(buf))
                buf = []
            result.append(line)
    if buf:
        result.append("".join(buf))
    return "\n".join(result)


def clean_text(text: str) -> str:
    """PDF抽出テキストの正規化"""
    # 禁止事項ヘッダー除去 (複数行にまたがる場合も対応)
    text = re.sub(r"スタディング[^\n]*?（禁無断転載）", "", text)
    # PDF縦書きテーブルの1文字/行を結合
    text = _join_vertical_chars(text)
    # 漢字・かな間の余分スペース除去 (例: "借 方" → "借方")
    text = re.sub(r"(?<=" + _KANJI_RANGE[1:-1] + r") (?=" + _KANJI_RANGE[1:-1] + r")", "", text)
    # 連続スペースを1つに
    text = re.sub(r" {2,}", " ", text)
    # 3行以上の空行を2行に
    text = re.sub(r"\n{3,}", "\n\n", text)
    # ページ番号行 (例: "- 1 -") を除去
    text = re.sub(r"^\s*-\s*\d+\s*-\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def base_title(title: str) -> str:
    """_設問 / _解答 サフィックスを除いてベースタイトルを返す"""
    for suffix in ("_設問", "_解答"):
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title


def make_chunks(text: str, metadata: dict) -> list[dict]:
    """テキストを CHUNK_SIZE 字のチャンクに分割する（overlap あり）"""
    chunks = []
    start = 0
    idx = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk_text = text[start:end]
        chunks.append({
            "text": chunk_text,
            "chunk_idx": idx,
            **metadata,
        })
        start += CHUNK_SIZE - CHUNK_OVERLAP
        idx += 1
    return chunks


def fetch_pdfs(conn: sqlite3.Connection) -> dict[str, dict]:
    """pdf_type ごとに {base_title: row} のマップを返す"""
    cur = conn.execute(
        "SELECT id, course_id, pdf_type, title, text_content FROM pdfs"
    )
    result: dict[str, dict[str, dict]] = {"設問": {}, "解答": {}, "その他": {}}
    for row in cur.fetchall():
        rid, course_id, pdf_type, title, text = row
        if pdf_type not in result:
            result[pdf_type] = {}
        base = base_title(title)
        result[pdf_type][base] = {
            "id": rid,
            "course_id": course_id,
            "title": title,
            "text": clean_text(text or ""),
        }
    return result


def main():
    conn = sqlite3.connect(DB_PATH)
    pdfs = fetch_pdfs(conn)
    conn.close()

    setsumon_map = pdfs.get("設問", {})
    kaitou_map = pdfs.get("解答", {})
    other_map = pdfs.get("その他", {})

    print(f"設問: {len(setsumon_map)}件, 解答: {len(kaitou_map)}件, その他: {len(other_map)}件")

    qa_pairs = []
    all_chunks = []
    matched = 0
    unmatched_q = []

    for base, q_row in setsumon_map.items():
        if base in kaitou_map:
            a_row = kaitou_map[base]
            matched += 1
            qa_pairs.append({
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": q_row["text"]},
                    {"role": "assistant", "content": a_row["text"]},
                ],
                "meta": {
                    "base_title": base,
                    "course_id": q_row["course_id"],
                    "q_id": q_row["id"],
                    "a_id": a_row["id"],
                },
            })
        else:
            unmatched_q.append(base)

    # RAG チャンク: 設問・解答・その他すべてチャンク化
    for pdf_type, pmap in pdfs.items():
        for base, row in pmap.items():
            meta = {
                "source_title": row["title"],
                "base_title": base,
                "course_id": row["course_id"],
                "pdf_type": pdf_type,
                "pdf_id": row["id"],
            }
            chunks = make_chunks(row["text"], meta)
            all_chunks.extend(chunks)

    # 書き出し
    with open(QA_OUT, "w", encoding="utf-8") as f:
        for pair in qa_pairs:
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")

    with open(CHUNKS_OUT, "w", encoding="utf-8") as f:
        for chunk in all_chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    print(f"マッチ成功: {matched}ペア → {QA_OUT}")
    print(f"チャンク数: {len(all_chunks)} → {CHUNKS_OUT}")
    if unmatched_q:
        print(f"マッチなし設問: {unmatched_q}")


if __name__ == "__main__":
    main()
