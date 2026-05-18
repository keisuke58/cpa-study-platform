"""
CPA RAG パイプライン

使い方:
  # DB 構築（初回 or 新規 PDF 追加後）
  python rag_pipeline.py --build

  # クエリ
  python rag_pipeline.py --query "退職給付引当金の計算方法を教えてください"

  # Python API として import して使う場合
  from rag_pipeline import retrieve, generate_answer
"""

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CHROMA_DIR = Path(__file__).parent / "chroma_db"
CHUNKS_FILE = Path(__file__).parent / "chunks.jsonl"
DB_PATH = Path(__file__).parent / "studyin.db"

EMBED_MODEL = "intfloat/multilingual-e5-large"
COLLECTION_NAME = "cpa_chunks"
TOP_K = 5


@dataclass
class Chunk:
    text: str
    source_title: str
    course_id: int
    pdf_type: str
    chunk_idx: int
    score: float = 0.0


def _get_embedder():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL)


def _get_chroma():
    import chromadb
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client


def build_index(force: bool = False):
    """chunks.jsonl を読み込み ChromaDB に Embedding してインデックスを構築する"""
    client = _get_chroma()
    embedder = _get_embedder()

    if force:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass

    collection = client.get_or_create_collection(
        COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    if not CHUNKS_FILE.exists():
        raise FileNotFoundError(f"{CHUNKS_FILE} が見つかりません。先に extract_qa_pairs.py を実行してください。")

    chunks = [json.loads(line) for line in CHUNKS_FILE.read_text().splitlines() if line.strip()]
    print(f"{len(chunks)} チャンクを読み込みました")

    # 既存 ID を確認してスキップ (バッチで取得して SQLite 変数上限を回避)
    existing: set[str] = set()
    _BATCH = 5000
    for _start in range(0, len(chunks), _BATCH):
        _ids_batch = [str(i) for i in range(_start, min(_start + _BATCH, len(chunks)))]
        _res = collection.get(ids=_ids_batch, include=[])
        existing.update(_res["ids"])
    new_chunks = [c for i, c in enumerate(chunks) if str(i) not in existing]
    if not new_chunks:
        print("すべてのチャンクがすでにインデックス済みです")
        return

    texts = [c["text"] for c in new_chunks]
    ids = [str(chunks.index(c)) for c in new_chunks]

    print(f"{len(new_chunks)} チャンクを Embedding 中...")
    # multilingual-e5 は "passage: " プレフィックスが必要
    embeddings = embedder.encode(
        [f"passage: {t}" for t in texts],
        batch_size=32,
        show_progress_bar=True,
        normalize_embeddings=True,
    ).tolist()

    metadatas = [
        {
            "source_title": c.get("source_title", ""),
            "base_title": c.get("base_title", ""),
            "course_id": int(c.get("course_id", 0)),
            "pdf_type": c.get("pdf_type", ""),
            "chunk_idx": int(c.get("chunk_idx", 0)),
        }
        for c in new_chunks
    ]

    # ChromaDB の最大バッチサイズに合わせて分割して追加
    CHROMA_BATCH = 5000
    for start in range(0, len(new_chunks), CHROMA_BATCH):
        end = min(start + CHROMA_BATCH, len(new_chunks))
        collection.add(
            ids=ids[start:end],
            embeddings=embeddings[start:end],
            documents=texts[start:end],
            metadatas=metadatas[start:end],
        )
        print(f"  追加: {end}/{len(new_chunks)}")
    print(f"インデックス構築完了: {CHROMA_DIR}")


# course_id 範囲によるソース分類
_SOURCE_COURSE_IDS: dict[str, list[int]] = {
    "studyin": list(range(1, 9000)),   # studying.jp コース (< 9000)
    "fsa":     [9001],                  # 金融庁 公式過去問
    "openstax":[9003],                  # OpenStax 英語テキスト
    "boki":    [9011, 9012, 9013],      # 日商簿記
    "irs":     [9021],                  # IRS Publications (REG)
    "pcaob":   [9022],                  # PCAOB Auditing Standards (AUD)
    "aicpa":   [9023],                  # AICPA Exam Blueprints
}


def _build_chroma_where(sources: list[str] | None) -> dict | None:
    """ソースリストから ChromaDB where 句を構築する"""
    if not sources:
        return None
    ids: list[int] = []
    for s in sources:
        ids.extend(_SOURCE_COURSE_IDS.get(s, []))
    if not ids:
        return None
    # studying.jp は課題数が多いので $lt で代替
    if any(s == "studyin" for s in sources):
        others = [i for i in ids if i >= 9000]
        if others:
            return {"$or": [
                {"course_id": {"$lt": 9000}},
                {"course_id": {"$in": others}},
            ]}
        return {"course_id": {"$lt": 9000}}
    return {"course_id": {"$in": ids}}


def _build_sql_where(sources: list[str] | None) -> tuple[str, list]:
    """ソースリストから SQL WHERE 句と params を返す"""
    if not sources:
        return "", []
    ids: list[int] = []
    for s in sources:
        ids.extend(_SOURCE_COURSE_IDS.get(s, []))
    if not ids:
        return "", []
    placeholders = ",".join("?" * len(ids))
    return f"AND p.course_id IN ({placeholders})", ids


def _dense_retrieve(query: str, k: int,
                    sources: list[str] | None = None) -> list[Chunk]:
    """Dense retrieval: Pinecone (cloud) → ChromaDB (local) の順に試みる"""
    try:
        from pinecone_rag import is_available as _pc_avail, retrieve as _pc_retrieve
        if _pc_avail():
            return _pc_retrieve(query, k=k, sources=sources)
    except ImportError:
        pass

    # ChromaDB fallback (ローカル)
    client = _get_chroma()
    embedder = _get_embedder()
    collection = client.get_collection(COLLECTION_NAME)

    query_emb = embedder.encode(
        [f"query: {query}"],
        normalize_embeddings=True,
    ).tolist()

    where = _build_chroma_where(sources)
    results = collection.query(
        query_embeddings=query_emb,
        n_results=k,
        where=where,
    )
    chunks = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        chunks.append(Chunk(
            text=doc,
            source_title=meta.get("source_title", ""),
            course_id=meta.get("course_id", 0),
            pdf_type=meta.get("pdf_type", ""),
            chunk_idx=meta.get("chunk_idx", 0),
            score=1.0 - dist,  # cosine distance → similarity
        ))
    return chunks


def _sparse_retrieve(query: str, k: int,
                     sources: list[str] | None = None) -> list[Chunk]:
    """FTS5 (BM25) による sparse retrieval"""
    conn = sqlite3.connect(DB_PATH)
    src_clause, src_params = _build_sql_where(sources)
    rows = conn.execute(
        f"""
        SELECT p.title, p.course_id, p.pdf_type,
               snippet(pdfs_fts, 1, '', '', '...', 20) as snip,
               rank
        FROM pdfs_fts
        JOIN pdfs p ON pdfs_fts.rowid = p.id
        WHERE pdfs_fts MATCH ?
        {src_clause}
        ORDER BY rank
        LIMIT ?
        """,
        (query, *src_params, k),
    ).fetchall()
    conn.close()

    chunks = []
    for title, course_id, pdf_type, snip, rank in rows:
        chunks.append(Chunk(
            text=snip,
            source_title=title,
            course_id=course_id,
            pdf_type=pdf_type,
            chunk_idx=-1,
            score=1.0 / (1.0 + abs(rank)),  # BM25 rank → 正規化スコア
        ))
    return chunks


def _rrf(dense: list[Chunk], sparse: list[Chunk], k_rrf: int = 60) -> list[Chunk]:
    """Reciprocal Rank Fusion で dense + sparse を統合する"""
    scores: dict[str, float] = {}
    chunks_map: dict[str, Chunk] = {}

    for rank, c in enumerate(dense):
        key = f"d_{c.source_title}_{c.chunk_idx}"
        scores[key] = scores.get(key, 0) + 1.0 / (k_rrf + rank + 1)
        chunks_map[key] = c

    for rank, c in enumerate(sparse):
        key = f"s_{c.source_title}_{c.chunk_idx}"
        scores[key] = scores.get(key, 0) + 1.0 / (k_rrf + rank + 1)
        chunks_map[key] = c

    sorted_keys = sorted(scores, key=lambda k: scores[k], reverse=True)
    result = []
    for key in sorted_keys:
        c = chunks_map[key]
        c.score = scores[key]
        result.append(c)
    return result


def retrieve(query: str, k: int = TOP_K,
             sources: list[str] | None = None) -> list[Chunk]:
    """Hybrid (dense + sparse + RRF) で上位 k チャンクを返す

    sources: ["studyin", "fsa", "openstax"] などでフィルター。None で全ソース。
    """
    try:
        dense = _dense_retrieve(query, k, sources)
    except Exception as e:
        print(f"[dense retrieval error] {e}")
        dense = []

    try:
        sparse = _sparse_retrieve(query, k, sources)
    except Exception as e:
        print(f"[sparse retrieval error] {e}")
        sparse = []

    if not dense and not sparse:
        return []

    merged = _rrf(dense, sparse)
    return merged[:k]


_SYSTEM_PROMPT = (
    "あなたは公認会計士試験（CPA）の専門家です。"
    "以下の参考資料を基に、正確かつ丁寧に回答してください。"
    "参考資料に答えがない場合は、その旨を伝えてください。"
)

LOCAL_MODEL_DIR = Path(__file__).parent / "models" / "cpa-qwen2.5-7b-lora"

PROVIDERS = {
    "claude": {
        "models": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "env_key": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "models": ["gemini-2.0-flash", "gemini-1.5-pro"],
        "env_key": "GOOGLE_API_KEY",
    },
    "openai": {
        "models": ["gpt-4o-mini", "gpt-4o"],
        "env_key": "OPENAI_API_KEY",
    },
    "local": {
        "models": [str(LOCAL_MODEL_DIR)],
        "env_key": "",
    },
}


def _build_context(chunks: list[Chunk]) -> str:
    return "\n\n---\n\n".join(
        f"[出典: {c.source_title}]\n{c.text}" for c in chunks
    )


def _generate_claude(query: str, context: str, model: str, api_key: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"【参考資料】\n{context}\n\n【質問】\n{query}"}],
    )
    return message.content[0].text


def _generate_gemini(query: str, context: str, model: str, api_key: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=f"【参考資料】\n{context}\n\n【質問】\n{query}",
        config=types.GenerateContentConfig(system_instruction=_SYSTEM_PROMPT),
    )
    return resp.text


def _generate_openai(query: str, context: str, model: str, api_key: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"【参考資料】\n{context}\n\n【質問】\n{query}"},
        ],
    )
    return resp.choices[0].message.content


def _generate_local(query: str, context: str, model: str, api_key: str) -> str:
    """Fine-tuned LoRA モデルでローカル推論する（transformers + peft）"""
    model_path = model if model != str(LOCAL_MODEL_DIR) else str(LOCAL_MODEL_DIR)
    if not Path(model_path).exists():
        return f"⚠️ ローカルモデルが見つかりません: {model_path}\n`finetune_lora.py` で学習後に使用してください。"

    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import PeftModel

    # モデルキャッシュ（Streamlit の再実行で再ロードしない）
    cache_key = f"_local_model_{model_path}"
    import functools
    if not hasattr(_generate_local, "_cache"):
        _generate_local._cache = {}
    if cache_key not in _generate_local._cache:
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        base_model_name = "Qwen/Qwen2.5-7B-Instruct"
        base = AutoModelForCausalLM.from_pretrained(
            base_model_name,
            torch_dtype=torch.float16,
            device_map="auto",
        )
        model_obj = PeftModel.from_pretrained(base, model_path)
        model_obj.eval()
        _generate_local._cache[cache_key] = (tokenizer, model_obj)
    tokenizer, model_obj = _generate_local._cache[cache_key]

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"【参考資料】\n{context}\n\n【質問】\n{query}"},
    ]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(model_obj.device)
    with torch.no_grad():
        out = model_obj.generate(**inputs, max_new_tokens=512, temperature=0.7, do_sample=True)
    return tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


_GENERATORS = {
    "claude": _generate_claude,
    "gemini": _generate_gemini,
    "openai": _generate_openai,
    "local":  _generate_local,
}


def generate_answer(
    query: str,
    chunks: list[Chunk],
    provider: str = "claude",
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    """取得チャンクを使って指定プロバイダーの LLM で回答を生成する

    provider: "claude" | "gemini" | "openai"
    model:    None で各プロバイダーのデフォルトモデルを使用
    """
    import os

    if provider not in PROVIDERS:
        return f"⚠️ 未対応のプロバイダー: {provider}。{list(PROVIDERS.keys())} から選択してください。"

    pconf = PROVIDERS[provider]
    # ローカルモードは API キー不要
    if provider == "local":
        key = ""
    else:
        key = api_key or os.environ.get(pconf["env_key"], "")
        if not key:
            return f"⚠️ {pconf['env_key']} が設定されていません。"

    m = model or pconf["models"][0]
    context = _build_context(chunks)

    try:
        return _GENERATORS[provider](query, context, m, key)
    except Exception as e:
        return f"⚠️ {provider} API エラー: {e}"


def main():
    parser = argparse.ArgumentParser(description="CPA RAG パイプライン")
    parser.add_argument("--build", action="store_true", help="ChromaDB インデックスを構築する")
    parser.add_argument("--force", action="store_true", help="既存インデックスを削除して再構築")
    parser.add_argument("--query", type=str, help="検索クエリ")
    parser.add_argument("--k", type=int, default=TOP_K, help="取得チャンク数")
    parser.add_argument("--no-llm", action="store_true", help="LLM を使わず検索結果のみ表示")
    parser.add_argument("--provider", type=str, default="claude", choices=list(PROVIDERS.keys()), help="LLM プロバイダー")
    parser.add_argument("--model", type=str, default=None, help="使用モデル名 (省略時はデフォルト)")
    args = parser.parse_args()

    if args.build:
        build_index(force=args.force)

    if args.query:
        print(f"\nクエリ: {args.query}")
        chunks = retrieve(args.query, k=args.k)
        print(f"\n取得チャンク ({len(chunks)}件):")
        for i, c in enumerate(chunks):
            print(f"  [{i+1}] {c.source_title} (score={c.score:.3f})")
            print(f"      {c.text[:120].replace(chr(10), ' ')}...")

        if not args.no_llm:
            print(f"\n--- LLM 回答 ({args.provider} / {args.model or PROVIDERS[args.provider]['models'][0]}) ---")
            answer = generate_answer(args.query, chunks, provider=args.provider, model=args.model)
            print(answer)


if __name__ == "__main__":
    main()
