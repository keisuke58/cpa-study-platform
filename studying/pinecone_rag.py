"""
Pinecone + Gemini text-embedding-004 クラウド対応 RAG。
ChromaDB/sentence-transformers の代替として Streamlit Cloud 上で動作する。

環境変数:
    PINECONE_API_KEY — Pinecone serverless API key (console.pinecone.io)
    GOOGLE_API_KEY   — Gemini API key (ai.google.dev)

インデックス構築 (初回のみ、ローカルで実行):
    python pinecone_rag.py --build
    python pinecone_rag.py --build --batch 50   # slow connection
"""

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

CHUNKS_FILE = Path(__file__).parent / "chunks.jsonl"
INDEX_NAME = "cpa-chunks"
EMBED_MODEL = "models/text-embedding-004"
EMBED_DIM = 768
TOP_K = 5
TEXT_TRUNCATE = 8000  # Pinecone metadata per-field limit (~40KB total)

_SOURCE_COURSE_IDS: dict[str, list[int]] = {
    "studyin": list(range(1, 9000)),
    "fsa":     [9001],
    "openstax":[9003],
    "boki":    [9011, 9012, 9013],
    "irs":     [9021],
    "pcaob":   [9022],
    "aicpa":   [9023],
}


@dataclass
class Chunk:
    text: str
    source_title: str
    course_id: int
    pdf_type: str
    chunk_idx: int
    score: float = 0.0


def is_available() -> bool:
    return bool(os.environ.get("PINECONE_API_KEY")) and bool(os.environ.get("GOOGLE_API_KEY"))


def _gemini_embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    from google import genai
    from google.genai.types import EmbedContentConfig
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    resp = client.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=EmbedContentConfig(task_type=task_type),
    )
    return [e.values for e in resp.embeddings]


def _get_index():
    from pinecone import Pinecone, ServerlessSpec
    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    existing = {i.name for i in pc.list_indexes()}
    if INDEX_NAME not in existing:
        print(f"インデックス '{INDEX_NAME}' を作成中...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        # インデックスが ready になるまで待機
        for _ in range(30):
            if pc.describe_index(INDEX_NAME).status["ready"]:
                break
            time.sleep(2)
    return pc.Index(INDEX_NAME)


def _build_pinecone_filter(sources: list[str] | None) -> dict | None:
    if not sources:
        return None
    studyin_included = "studyin" in sources
    other_ids: list[int] = []
    for s in sources:
        if s != "studyin":
            other_ids.extend(_SOURCE_COURSE_IDS.get(s, []))

    if studyin_included and other_ids:
        return {"$or": [{"course_id": {"$lt": 9000}}, {"course_id": {"$in": other_ids}}]}
    if studyin_included:
        return {"course_id": {"$lt": 9000}}
    if other_ids:
        return {"course_id": {"$in": other_ids}}
    return None


def build_index(force: bool = False, batch_size: int = 96):
    """chunks.jsonl を Pinecone に upsert する（初回 or 強制再構築）"""
    if not is_available():
        raise RuntimeError("PINECONE_API_KEY と GOOGLE_API_KEY を設定してください")

    chunks = [json.loads(l) for l in CHUNKS_FILE.read_text().splitlines() if l.strip()]
    print(f"{len(chunks)} チャンクを読み込みました")

    index = _get_index()

    if force:
        print("既存ベクトルを削除中...")
        index.delete(delete_all=True)
        time.sleep(2)

    # 既存 ID を確認してスキップ
    all_ids = [str(i) for i in range(len(chunks))]
    existing_ids: set[str] = set()
    for start in range(0, len(all_ids), 1000):
        batch_ids = all_ids[start:start + 1000]
        resp = index.fetch(ids=batch_ids)
        existing_ids.update(resp.vectors.keys())

    new_chunks = [(i, c) for i, c in enumerate(chunks) if str(i) not in existing_ids]
    if not new_chunks:
        print("すべてのチャンクがすでにインデックス済みです")
        return

    print(f"{len(new_chunks)} チャンクを Embedding → Pinecone にアップロード中...")
    uploaded = 0

    for start in range(0, len(new_chunks), batch_size):
        batch = new_chunks[start:start + batch_size]
        texts = [c["text"] for _, c in batch]

        try:
            embeddings = _gemini_embed(texts, task_type="RETRIEVAL_DOCUMENT")
        except Exception as e:
            print(f"  Embedding エラー (offset={start}): {e} — リトライ中...")
            time.sleep(5)
            try:
                embeddings = _gemini_embed(texts, task_type="RETRIEVAL_DOCUMENT")
            except Exception as e2:
                print(f"  スキップ: {e2}")
                continue

        vectors = []
        for (orig_idx, c), emb in zip(batch, embeddings):
            vectors.append({
                "id": str(orig_idx),
                "values": emb,
                "metadata": {
                    "text": c["text"][:TEXT_TRUNCATE],
                    "source_title": c.get("source_title", ""),
                    "base_title": c.get("base_title", ""),
                    "course_id": int(c.get("course_id", 0)),
                    "pdf_type": c.get("pdf_type", ""),
                    "chunk_idx": int(c.get("chunk_idx", 0)),
                },
            })

        try:
            index.upsert(vectors=vectors)
            uploaded += len(vectors)
            print(f"  {uploaded}/{len(new_chunks)}")
        except Exception as e:
            print(f"  Upsert エラー: {e}")

        time.sleep(0.5)

    stats = index.describe_index_stats()
    print(f"完了: {stats.total_vector_count} ベクトル")


def retrieve(query: str, k: int = TOP_K,
             sources: list[str] | None = None) -> list[Chunk]:
    """Pinecone dense retrieval"""
    if not is_available():
        return []
    try:
        index = _get_index()
        query_emb = _gemini_embed([query], task_type="RETRIEVAL_QUERY")[0]
        pinecone_filter = _build_pinecone_filter(sources)

        kwargs: dict = {"vector": query_emb, "top_k": k, "include_metadata": True}
        if pinecone_filter:
            kwargs["filter"] = pinecone_filter

        results = index.query(**kwargs)

        chunks = []
        for match in results.matches:
            m = match.metadata or {}
            chunks.append(Chunk(
                text=m.get("text", ""),
                source_title=m.get("source_title", ""),
                course_id=int(m.get("course_id", 0)),
                pdf_type=m.get("pdf_type", ""),
                chunk_idx=int(m.get("chunk_idx", 0)),
                score=float(match.score),
            ))
        return chunks
    except Exception as e:
        print(f"[pinecone] retrieve error: {e}")
        return []


def index_stats() -> dict:
    if not is_available():
        return {}
    try:
        return _get_index().describe_index_stats().to_dict()
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser(description="Pinecone RAG ビルド / クエリ")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--batch", type=int, default=96)
    parser.add_argument("--query", type=str)
    parser.add_argument("--k", type=int, default=TOP_K)
    args = parser.parse_args()

    if args.build:
        build_index(force=args.force, batch_size=args.batch)

    if args.query:
        chunks = retrieve(args.query, k=args.k)
        for i, c in enumerate(chunks):
            print(f"[{i+1}] {c.source_title} (score={c.score:.3f})")
            print(f"    {c.text[:100].replace(chr(10), ' ')}...")


if __name__ == "__main__":
    main()
