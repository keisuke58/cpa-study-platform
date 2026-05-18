"""
CPA QLoRA Fine-tuning スクリプト

ベースモデル: Qwen/Qwen2.5-7B-Instruct
データ優先順位:
  1. qa_all.jsonl          (questions.json 5200問 + studying.jp PDF ペア 統合)
  2. qa_from_questions.jsonl (questions.json のみ)
  3. qa_pairs.jsonl        (studying.jp PDFのみ)
GPU: 4090 × 1 推奨 (24 GB VRAM)

使い方:
  python finetune_lora.py                          # デフォルト設定で学習
  python finetune_lora.py --epochs 3 --batch 2    # エポック数・バッチ調整
  python finetune_lora.py --base-model Qwen/Qwen2.5-3B-Instruct  # 小さいモデル
  python finetune_lora.py --data qa_from_questions.jsonl          # データ指定
"""

import argparse
import json
from pathlib import Path

_STUDY_DIR = Path(__file__).parent
# データファイル優先順位
_DATA_CANDIDATES = [
    _STUDY_DIR / "qa_all.jsonl",
    _STUDY_DIR / "qa_from_questions.jsonl",
    _STUDY_DIR / "qa_pairs.jsonl",
]
QA_FILE = next((p for p in _DATA_CANDIDATES if p.exists()), _DATA_CANDIDATES[-1])
OUTPUT_DIR = _STUDY_DIR / "models" / "cpa-qwen2.5-7b-lora"

DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_EPOCHS = 3
DEFAULT_BATCH = 4
DEFAULT_GRAD_ACCUM = 4
LORA_RANK = 16
LORA_ALPHA = 32
LORA_TARGET = ["q_proj", "v_proj", "k_proj", "o_proj"]
MAX_SEQ_LEN = 2048


def load_dataset(path: Path):
    """qa_pairs.jsonl を HuggingFace Dataset 形式に変換する"""
    from datasets import Dataset

    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        records.append({"messages": obj["messages"]})

    if not records:
        raise ValueError(f"{path} にデータがありません。先に extract_qa_pairs.py を実行してください。")

    print(f"学習データ: {len(records)} 件")
    return Dataset.from_list(records)


def train(args):
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
    from peft import LoraConfig, get_peft_model, TaskType
    from trl import SFTTrainer, SFTConfig

    print(f"ベースモデル: {args.base_model}")
    print(f"出力先: {OUTPUT_DIR}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 4-bit 量子化設定
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False

    # LoRA 設定
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=LORA_RANK,
        lora_alpha=LORA_ALPHA,
        lora_dropout=0.05,
        bias="none",
        target_modules=LORA_TARGET,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    dataset = load_dataset(getattr(args, "_data_path", QA_FILE))

    def format_messages(example):
        """ChatML 形式に整形"""
        return tokenizer.apply_chat_template(
            example["messages"],
            tokenize=False,
            add_generation_prompt=False,
        )

    training_args = SFTConfig(
        output_dir=str(OUTPUT_DIR),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=DEFAULT_GRAD_ACCUM,
        learning_rate=2e-4,
        warmup_ratio=0.05,
        lr_scheduler_type="cosine",
        fp16=False,
        bf16=True,
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=2,
        report_to="none",
        max_seq_length=MAX_SEQ_LEN,
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        formatting_func=format_messages,
        tokenizer=tokenizer,
    )

    print("学習開始...")
    trainer.train()
    trainer.save_model(str(OUTPUT_DIR))
    tokenizer.save_pretrained(str(OUTPUT_DIR))
    print(f"モデル保存完了: {OUTPUT_DIR}")


def main():
    parser = argparse.ArgumentParser(description="CPA QLoRA Fine-tuning")
    parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL, help="ベースモデル名")
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS, help="エポック数")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="バッチサイズ (per device)")
    parser.add_argument("--data", type=str, default=None, help="学習データ JSONL ファイルパス (省略時は自動選択)")
    parser.add_argument("--dry-run", action="store_true", help="データ確認のみ（学習しない）")
    args = parser.parse_args()

    data_path = Path(args.data) if args.data else QA_FILE
    print(f"使用データ: {data_path} ({data_path.stat().st_size // 1024} KB)")

    if args.dry_run:
        ds = load_dataset(data_path)
        print("最初のサンプル (messages):")
        for msg in ds[0]["messages"]:
            print(f"  [{msg['role']}] {msg['content'][:120]}...")
        return

    args._data_path = data_path
    train(args)


if __name__ == "__main__":
    main()
