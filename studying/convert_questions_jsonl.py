"""
questions.json (5,200問) を fine-tune 用 JSONL に変換する。

出力: studying/qa_from_questions.jsonl
形式: ChatML messages (system / user / assistant)

user   = 問題文 + 選択肢
assistant = 正解 + 解説
"""

import json
import random
from pathlib import Path

QUESTIONS_FILE = Path(__file__).parent.parent / "questions.json"
OUT_FILE = Path(__file__).parent / "qa_from_questions.jsonl"
COMBINED_OUT = Path(__file__).parent / "qa_all.jsonl"
QA_PAIRS_FILE = Path(__file__).parent / "qa_pairs.jsonl"

SYSTEM_PROMPT = (
    "あなたは公認会計士試験（CPA）の専門家です。"
    "選択問題に対して正解を選び、理由とともに丁寧に解説してください。"
)

SUBJECT_JA = {
    "Financial": "財務会計論",
    "Management": "管理会計論",
    "Audit": "監査論",
    "Company": "企業法",
}


def format_question(q: dict, subject: str) -> tuple[str, str]:
    """(user_message, assistant_message) を返す"""
    opts_text = "\n".join(
        f"  {'ABCD'[i]}. {opt}" for i, opt in enumerate(q["options"])
    )
    user = (
        f"【{SUBJECT_JA.get(subject, subject)}・Level {q.get('level', '?')}】\n\n"
        f"{q['q']}\n\n"
        f"選択肢:\n{opts_text}"
    )
    correct_letter = "ABCD"[q["correct"]]
    correct_text = q["options"][q["correct"]]
    assistant = (
        f"正解: {correct_letter}. {correct_text}\n\n"
        f"解説:\n{q['explanation']}"
    )
    return user, assistant


def main():
    data = json.load(open(QUESTIONS_FILE, encoding="utf-8"))

    records = []
    counts = {}
    for subject, questions in data.items():
        counts[subject] = len(questions)
        for q in questions:
            user, assistant = format_question(q, subject)
            records.append({
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": assistant},
                ],
                "meta": {"subject": subject, "level": q.get("level")},
            })

    random.seed(42)
    random.shuffle(records)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"変換完了: {len(records)} 件 → {OUT_FILE}")
    for subj, cnt in counts.items():
        print(f"  {SUBJECT_JA.get(subj, subj)}: {cnt} 問")

    # studying.jp データと結合した全データも作成
    studyin_records = []
    if QA_PAIRS_FILE.exists():
        for line in QA_PAIRS_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                studyin_records.append(json.loads(line))

    all_records = records + studyin_records
    random.shuffle(all_records)
    with open(COMBINED_OUT, "w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n統合データ: {len(all_records)} 件 → {COMBINED_OUT}")
    print(f"  questions.json: {len(records)} 件")
    print(f"  studying.jp:   {len(studyin_records)} 件")


if __name__ == "__main__":
    main()
