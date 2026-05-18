"""
AI 問題生成パイプライン (Phase B)

scraped PDF chunks から Claude/Gemini/OpenAI で MCQ を自動生成し、
questions.json または uscpa_questions.json に追記する。

使い方:
  # 日本語 CPA 問題生成 (studying.jp チャンクから)
  python generate_questions_ai.py --provider claude --lang ja --n 3

  # USCPA 英語問題生成 (OpenStax チャンクから)
  python generate_questions_ai.py --provider gemini --lang en --source openstack --n 5

  # dry-run でプロンプト確認
  python generate_questions_ai.py --dry-run --n 2
"""

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path

STUDY_DIR    = Path(__file__).parent
DB_PATH      = STUDY_DIR / "studyin.db"
CHUNKS_FILE  = STUDY_DIR / "chunks.jsonl"
JPQA_OUT     = STUDY_DIR.parent / "questions.json"
USCPA_OUT    = STUDY_DIR.parent / "uscpa_questions.json"
GENERATED_LOG = STUDY_DIR / "generated_questions.log"

SUBJECT_MAP_JA = {
    2109: "Financial",   # 財務会計論
    2110: "Management",  # 管理会計論
    2099: "Audit",       # 監査論
    2106: "Company",     # 企業法
    2107: "Financial",   # 財務（別コース）
    2098: "Management",
    2252: "Financial",
    9001: "Financial",   # 金融庁過去問（財務系）
}

SUBJECT_MAP_EN = {
    "principles-of-accounting-volume-1": "USCPA_FAR",
    "principles-of-accounting-volume-2": "USCPA_FAR",
    "auditing": "USCPA_AUD",
    "business-law": "USCPA_REG",
}

# course_id → subject (EN) — IRS/PCAOB/AICPA
COURSE_ID_MAP_EN: dict[int, str] = {
    9021: "USCPA_REG",  # IRS Publications (tax)
    9022: "USCPA_AUD",  # PCAOB Auditing Standards
    9023: "USCPA_FAR",  # AICPA Blueprints (FAR/AUD/REG/BAR mixed; FAR as default)
}

MCQ_SCHEMA = '{"q": "問題文", "options": ["A", "B", "C", "D"], "correct": 0, "explanation": "解説", "level": 2}'
MCQ_SCHEMA_EN = '{"q": "Question text", "options": ["A", "B", "C", "D"], "correct": 0, "explanation": "Explanation", "level": 2}'

PROMPT_JA = """以下の会計テキストから、公認会計士試験の選択問題を{n}問作成してください。

要件:
- 4択の選択問題 (options は A〜D の4つ)
- correct は正解の選択肢インデックス (0〜3)
- explanation は簡潔な解説 (1〜3文)
- level: 1=基礎, 2=標準, 3=応用
- テキストの内容に直接基づく問題のみ作成すること
- 重複や曖昧な問題は作らないこと

テキスト:
{text}

JSON 配列形式で返してください（他のテキストは不要）:
[{schema}, ...]"""

PROMPT_EN = """Generate {n} CPA exam multiple-choice questions from the following accounting text.

Requirements:
- 4 options (A-D), correct is the 0-indexed answer (0-3)
- explanation should be 1-3 concise sentences
- level: 1=basic, 2=intermediate, 3=advanced
- Only create questions directly based on the text content

Text:
{text}

Return JSON array only:
[{schema}, ...]"""


def load_chunks(course_ids: list[int] | None = None, limit: int = 0) -> list[dict]:
    """chunks.jsonl からチャンクを読み込む。course_ids でフィルター可能。"""
    if not CHUNKS_FILE.exists():
        return []
    chunks = []
    for line in CHUNKS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if course_ids is not None:
            if int(c.get("course_id", 0)) not in course_ids:
                continue
        chunks.append(c)
    if limit:
        chunks = chunks[:limit]
    return chunks


def already_logged(chunk_key: str) -> bool:
    if not GENERATED_LOG.exists():
        return False
    return chunk_key in GENERATED_LOG.read_text()


def log_chunk(chunk_key: str):
    with open(GENERATED_LOG, "a") as f:
        f.write(chunk_key + "\n")


def call_claude(prompt: str, api_key: str, model: str = "claude-sonnet-4-6") -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model, max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def call_gemini(prompt: str, api_key: str, model: str = "gemini-2.0-flash") -> str:
    from google import genai
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(model=model, contents=prompt)
    return resp.text


def call_openai(prompt: str, api_key: str, model: str = "gpt-4o-mini") -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model, max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content


_CALLERS = {"claude": call_claude, "gemini": call_gemini, "openai": call_openai}
_ENV_KEYS = {"claude": "ANTHROPIC_API_KEY", "gemini": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY"}
_DEFAULT_MODELS = {"claude": "claude-sonnet-4-6", "gemini": "gemini-2.0-flash", "openai": "gpt-4o-mini"}


def extract_json_array(text: str) -> list[dict]:
    """LLM レスポンスから JSON 配列を抽出する。"""
    text = text.strip()
    # コードブロックを除去
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    # 先頭の [ を探して配列を抽出
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group())
    except Exception:
        return []


def validate_question(q: dict) -> bool:
    """MCQ の形式・内容を検証する。"""
    if not isinstance(q.get("q"), str) or len(q["q"]) < 10:
        return False
    if not isinstance(q.get("options"), list) or len(q["options"]) != 4:
        return False
    if not isinstance(q.get("correct"), int) or not (0 <= q["correct"] <= 3):
        return False
    if not isinstance(q.get("explanation"), str) or len(q["explanation"]) < 5:
        return False
    return True


def load_existing(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_questions(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def generate(args):
    api_key = args.api_key or os.environ.get(_ENV_KEYS[args.provider], "")
    if not api_key:
        raise SystemExit(f"⚠️ {_ENV_KEYS[args.provider]} が未設定です")

    caller = _CALLERS[args.provider]
    model  = args.model or _DEFAULT_MODELS[args.provider]

    course_ids = None
    if args.course_ids:
        course_ids = [int(x) for x in args.course_ids.split(",")]
    elif args.lang == "en":
        # デフォルトで英語ソース (IRS/PCAOB/AICPA) のみ対象
        course_ids = list(COURSE_ID_MAP_EN.keys())

    chunks = load_chunks(course_ids=course_ids, limit=args.max_chunks)
    if not chunks:
        raise SystemExit("⚠️ chunks.jsonl が空です。先に extract_qa_pairs.py を実行してください。")

    print(f"チャンク数: {len(chunks)} | プロバイダー: {args.provider} ({model}) | {args.n}問/chunk")

    if args.lang == "ja":
        prompt_tmpl = PROMPT_JA
        schema      = MCQ_SCHEMA
        out_path    = JPQA_OUT
    else:
        prompt_tmpl = PROMPT_EN
        schema      = MCQ_SCHEMA_EN
        out_path    = USCPA_OUT

    existing = load_existing(out_path)
    total_added = 0
    errors = 0

    for i, chunk in enumerate(chunks):
        chunk_key = f"{chunk.get('source_title','')}_{chunk.get('chunk_idx','')}"
        if not args.force and already_logged(chunk_key):
            continue

        text = chunk.get("text", "").strip()
        if len(text) < 100:
            continue

        prompt = prompt_tmpl.format(n=args.n, text=text[:1500], schema=schema)

        if args.dry_run:
            print(f"\n--- Chunk {i}: {chunk_key} ---")
            print(prompt[:500] + "...")
            if i >= 2:
                print("(dry-run: 最初の3チャンクのみ表示)")
                break
            continue

        try:
            resp = caller(prompt, api_key, model)
            questions = extract_json_array(resp)
        except Exception as e:
            print(f"  [error] chunk {i}: {e}")
            errors += 1
            time.sleep(2)
            continue

        # 科目割り当て
        course_id  = int(chunk.get("course_id", 0))
        if args.lang == "ja":
            subject = SUBJECT_MAP_JA.get(course_id, "Financial")
        else:
            # course_id ベースの直接マッピング優先
            if course_id in COURSE_ID_MAP_EN:
                subject = COURSE_ID_MAP_EN[course_id]
            else:
                title = chunk.get("source_title", "")
                subject = next((v for k, v in SUBJECT_MAP_EN.items() if k in title.lower()), "USCPA_FAR")

        valid_qs = [q for q in questions if validate_question(q)]
        if not valid_qs:
            print(f"  [skip] no valid questions from chunk {i}")
            continue

        for q in valid_qs:
            q["subject"] = subject
        existing.setdefault(subject, []).extend(valid_qs)
        total_added += len(valid_qs)
        log_chunk(chunk_key)

        print(f"  [{i+1}/{len(chunks)}] +{len(valid_qs)}問 → {subject} (合計 {total_added})")

        # レート制限対策
        time.sleep(args.sleep)

        # 中間保存（10チャンクごと）
        if total_added > 0 and i % 10 == 9:
            save_questions(out_path, existing)
            print(f"  >>> 中間保存: {out_path} ({total_added}問追加済み)")

    if not args.dry_run and total_added > 0:
        save_questions(out_path, existing)
        print(f"\n完了: {total_added}問追加 → {out_path}")
        print(f"エラー: {errors}件")
    elif not args.dry_run:
        print("新規問題なし（すべて処理済みか、バリデーション失敗）")


def main():
    parser = argparse.ArgumentParser(description="AI 問題生成パイプライン")
    parser.add_argument("--provider", default="claude", choices=["claude", "gemini", "openai"])
    parser.add_argument("--model", default=None, help="モデル名（省略時はデフォルト）")
    parser.add_argument("--api-key", default=None, help="API キー（省略時は環境変数）")
    parser.add_argument("--lang", default="ja", choices=["ja", "en"], help="生成言語")
    parser.add_argument("--n", type=int, default=3, help="チャンクあたりの生成問題数")
    parser.add_argument("--max-chunks", type=int, default=50, help="処理するチャンク数の上限")
    parser.add_argument("--sleep", type=float, default=1.0, help="API 呼び出し間隔 (秒)")
    parser.add_argument("--course-ids", default=None, help="対象 course_id をカンマ区切りで指定 (例: 9021,9022)")
    parser.add_argument("--force", action="store_true", help="処理済みチャンクも再処理")
    parser.add_argument("--dry-run", action="store_true", help="プロンプト確認のみ（API 呼び出しなし）")
    args = parser.parse_args()
    generate(args)


if __name__ == "__main__":
    main()
