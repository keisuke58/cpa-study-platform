#!/usr/bin/env bash
# studying.jp + 公開データ 完全収集スクリプト
# - 7コースを並列処理 (クラッシュ時は最大3回リトライ)
# - 全コース完了後に public_downloader.py を実行
# - ログは logs/ 以下
#
# Usage:
#   ./run_all.sh          # 全部
#   ./run_all.sh scraper  # studying.jp のみ
#   ./run_all.sh public   # 公開データのみ

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source .env 2>/dev/null || true
mkdir -p logs

PYTHON="${PYTHON:-python}"
MAX_RETRIES=3
COURSE_IDS=(2098 2109 2110 2099 2106 2107 2252)

MODE="${1:-all}"

# ──────────────────────────────────────────────────────────────
# 1コースを最大MAX_RETRIESまでリトライ
# ──────────────────────────────────────────────────────────────
run_course() {
    local CID="$1"
    local LOG="logs/scraper_${CID}.log"
    local attempt=0

    while [ $attempt -lt $MAX_RETRIES ]; do
        attempt=$((attempt + 1))
        echo "[$(date '+%H:%M:%S')] Course $CID: 開始 (attempt $attempt/$MAX_RETRIES)"
        if $PYTHON -u scraper.py --course-ids "$CID" >> "$LOG" 2>&1; then
            echo "[$(date '+%H:%M:%S')] Course $CID: 完了"
            return 0
        else
            local EC=$?
            echo "[$(date '+%H:%M:%S')] Course $CID: 失敗 (exit=$EC, attempt $attempt/$MAX_RETRIES)"
            if [ $attempt -lt $MAX_RETRIES ]; then
                echo "  → 30秒待機後リトライ..."
                sleep 30
            fi
        fi
    done
    echo "[$(date '+%H:%M:%S')] Course $CID: $MAX_RETRIES 回失敗。スキップ。"
    return 1
}

# ──────────────────────────────────────────────────────────────
# scraper.py: 7コース並列
# ──────────────────────────────────────────────────────────────
run_scrapers() {
    echo "========================================"
    echo "studying.jp 全コース並列収集 開始"
    echo "========================================"

    local pids=()
    for CID in "${COURSE_IDS[@]}"; do
        # ログをクリアせず追記 (リトライ履歴を残す)
        echo "--- run_all.sh 起動: $(date) ---" >> "logs/scraper_${CID}.log"
        run_course "$CID" &
        pids+=($!)
    done

    # 全プロセス完了を待つ
    local failed=0
    for pid in "${pids[@]}"; do
        if ! wait "$pid"; then
            failed=$((failed + 1))
        fi
    done

    if [ $failed -gt 0 ]; then
        echo "[警告] $failed コースで最終的に失敗しました。ログを確認してください。"
    else
        echo "全コース正常完了"
    fi
}

# ──────────────────────────────────────────────────────────────
# public_downloader.py
# ──────────────────────────────────────────────────────────────
run_public() {
    echo "========================================"
    echo "公開データ (金融庁/日商簿記/OpenStax) 収集 開始"
    echo "========================================"

    local attempt=0
    while [ $attempt -lt $MAX_RETRIES ]; do
        attempt=$((attempt + 1))
        echo "[$(date '+%H:%M:%S')] public_downloader: attempt $attempt/$MAX_RETRIES"
        if $PYTHON -u public_downloader.py >> logs/public_dl.log 2>&1; then
            echo "[$(date '+%H:%M:%S')] public_downloader: 完了"
            return 0
        fi
        sleep 30
    done
    echo "[$(date '+%H:%M:%S')] public_downloader: 失敗"
    return 1
}

# ──────────────────────────────────────────────────────────────
# 実行
# ──────────────────────────────────────────────────────────────
case "$MODE" in
    scraper)
        run_scrapers
        ;;
    public)
        run_public
        ;;
    all|*)
        run_scrapers
        run_public
        ;;
esac

echo ""
echo "========================================"
echo "全処理完了: $(date)"
echo "DB統計:"
python -c "
import sqlite3
conn = sqlite3.connect('studyin.db')
rows = conn.execute(\"SELECT pdf_type, COUNT(*) FROM pdfs WHERE pdf_type != '_zip_marker' GROUP BY pdf_type ORDER BY 2 DESC\").fetchall()
total = sum(c for _,c in rows)
for t,c in rows: print(f'  {t or \"その他\":<12} {c}')
print(f'  {\"合計\":<12} {total}')
conn.close()
"
echo "========================================"
