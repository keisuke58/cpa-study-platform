#!/usr/bin/env python3
"""
studying.jp 動画ダウンローダー (1本ずつ)

HLS (.m3u8) → セグメント .ts を結合して保存。
ffmpeg があれば MP4 に変換、なければ .ts のまま保存。

Usage:
    python video_downloader.py --lesson-id 158556
    python video_downloader.py --lesson-id 158556 --out /tmp/video.mp4
    python video_downloader.py --hls-url "https://...Playlist.m3u8" --out /tmp/vid.ts
    python video_downloader.py --links-json video_links.json --lesson-id 158556
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE_URL  = "https://member.studying.jp"
AIRCOURSE = "https://video.aircourse.com/api/player/id/1/accounts/1/playlists/{pid}?{sig}"
CDN_BASE  = "https://d2sp4gi8kq98cy.cloudfront.net"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


# ──────────────────────────────────────────────────────────────────────────────
# ログイン (Playwright)
# ──────────────────────────────────────────────────────────────────────────────

def login_sync(email: str, password: str) -> requests.Session:
    import asyncio
    from playwright.async_api import async_playwright

    async def _login():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx     = await browser.new_context()
            page    = await ctx.new_page()

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
            return cookies

    cookies = asyncio.run(_login())
    sess = requests.Session()
    sess.headers.update(HEADERS)
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
    return sess


# ──────────────────────────────────────────────────────────────────────────────
# レッスンページ → プレイリスト設定取得
# ──────────────────────────────────────────────────────────────────────────────

def get_playlist_config(sess: requests.Session, lesson_id: str) -> dict:
    url  = f"{BASE_URL}/course/lesson/index/id/{lesson_id}/f/0/"
    r    = sess.get(url, timeout=30)
    html = r.text

    pid_m = re.search(r'data-id=["\'](\d+)["\']', html)
    sig_m = re.search(r'data-signature=["\']([^"\']+)["\']', html)
    if not pid_m or not sig_m:
        raise RuntimeError(f"lesson {lesson_id}: data-id / data-signature が見つかりません")

    return {"playlist_id": pid_m.group(1), "signature": sig_m.group(1)}


def resolve_hls_url(sess: requests.Session, playlist_id: str, signature: str,
                    quality: str = "1280W") -> tuple[str, str]:
    """aircourse API → (hls_url, title)"""
    api_url = AIRCOURSE.format(pid=playlist_id, sig=signature)
    r = sess.get(api_url, timeout=20, headers={**HEADERS, "Referer": BASE_URL + "/"})
    r.raise_for_status()
    data = r.json()

    playlists = data.get("playlists") or data.get("playlist") or []
    if isinstance(playlists, dict):
        playlists = [playlists]
    if not playlists:
        if data.get("video_key") or data.get("videoKey"):
            playlists = [data]
        else:
            raise RuntimeError(f"aircourse API: playlists 空 ({data})")

    item  = playlists[0]
    vkey  = item.get("video_key") or item.get("videoKey") or item.get("key")
    title = item.get("title", "video")
    if not vkey:
        raise RuntimeError(f"aircourse API: video_key 取得失敗 ({item})")

    hls = f"{CDN_BASE}/1/{vkey}/HLS/{quality}.m3u8"
    return hls, title


# ──────────────────────────────────────────────────────────────────────────────
# HLS ダウンロード
# ──────────────────────────────────────────────────────────────────────────────

def parse_m3u8(text: str, base_url: str) -> list[str]:
    """m3u8 からセグメント URL リストを取得 (マスター → メディア再帰)"""
    lines = [l.strip() for l in text.splitlines()]

    # マスタープレイリスト (EXT-X-STREAM-INF) の場合は最高画質を選択
    stream_urls = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                stream_urls.append(urljoin(base_url, lines[i + 1]))

    if stream_urls:
        return None, stream_urls[-1]  # (None=まだセグメントなし, メディアURL)

    # メディアプレイリスト
    segments = []
    for line in lines:
        if line and not line.startswith("#"):
            segments.append(urljoin(base_url, line))
    return segments, None


def download_hls(sess: requests.Session, hls_url: str, out_path: Path,
                 use_ffmpeg: bool = True, delay: float = 0.05) -> Path:
    print(f"[hls] {hls_url}")

    # m3u8 取得 (マスター or メディア)
    r = sess.get(hls_url, timeout=30)
    r.raise_for_status()
    segments, media_url = parse_m3u8(r.text, hls_url)

    if media_url:
        print(f"[hls] メディアプレイリスト → {media_url}")
        r = sess.get(media_url, timeout=30)
        r.raise_for_status()
        segments, _ = parse_m3u8(r.text, media_url)

    if not segments:
        raise RuntimeError("セグメント URL が取得できませんでした")

    print(f"[hls] {len(segments)} セグメント")

    # ffmpeg が使えるなら直接変換
    if use_ffmpeg and shutil.which("ffmpeg"):
        out_mp4 = out_path.with_suffix(".mp4")
        print(f"[ffmpeg] {out_mp4} に変換中...")
        cmd = [
            "ffmpeg", "-y", "-allowed_extensions", "ALL",
            "-i", hls_url,
            "-c", "copy", str(out_mp4),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"[done] {out_mp4}")
        return out_mp4

    # ffmpeg なし: セグメントを逐次取得して結合
    with tempfile.TemporaryDirectory() as tmpdir:
        ts_files = []
        for i, seg_url in enumerate(segments):
            ts_path = Path(tmpdir) / f"{i:05d}.ts"
            for attempt in range(3):
                try:
                    chunk = sess.get(seg_url, timeout=30)
                    chunk.raise_for_status()
                    ts_path.write_bytes(chunk.content)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise RuntimeError(f"セグメント取得失敗: {seg_url}") from e
                    time.sleep(1)
            ts_files.append(ts_path)
            if i % 20 == 0:
                print(f"  {i+1}/{len(segments)} セグメント取得済み", end="\r", flush=True)
            time.sleep(delay)

        print()
        # 結合
        out_ts = out_path.with_suffix(".ts")
        with open(out_ts, "wb") as dst:
            for ts in ts_files:
                dst.write(ts.read_bytes())

    print(f"[done] {out_ts} ({out_ts.stat().st_size // 1024 // 1024} MB)")
    return out_ts


# ──────────────────────────────────────────────────────────────────────────────
# video_links.json から取得
# ──────────────────────────────────────────────────────────────────────────────

def find_in_links_json(json_path: Path, lesson_id: str) -> dict | None:
    import json
    data = json.loads(json_path.read_text())
    for item in data:
        if str(item.get("lesson_id")) == str(lesson_id):
            return item
    return None


# ──────────────────────────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="studying.jp 動画ダウンローダー (1本)")
    parser.add_argument("--lesson-id",  help="レッスン ID (例: 158556)")
    parser.add_argument("--hls-url",    help="HLS URL を直接指定")
    parser.add_argument("--links-json", help="video_links.json のパス")
    parser.add_argument("--out",        help="出力ファイルパス (拡張子不要)")
    parser.add_argument("--quality",    default="1280W", help="画質 (1280W/640W/320W)")
    parser.add_argument("--no-ffmpeg",  action="store_true")
    parser.add_argument("--email",      default=os.getenv("STUDYIN_EMAIL"))
    parser.add_argument("--password",   default=os.getenv("STUDYIN_PASSWORD"))
    args = parser.parse_args()

    if not args.lesson_id and not args.hls_url:
        parser.error("--lesson-id または --hls-url が必要です")

    hls_url = args.hls_url
    title   = "video"

    # video_links.json から情報取得
    if not hls_url and args.links_json:
        lj = Path(args.links_json)
        if lj.exists():
            entry = find_in_links_json(lj, args.lesson_id)
            if entry and entry.get("videos"):
                v = entry["videos"][0]
                hls_url = v["hls_url"]
                title   = entry.get("lesson_title", title)
                print(f"[json] {title}: {hls_url}")

    # ログインしてレッスンページから取得
    if not hls_url:
        if not args.email or not args.password:
            raise SystemExit("STUDYIN_EMAIL / STUDYIN_PASSWORD が未設定")
        sess = login_sync(args.email, args.password)
        cfg  = get_playlist_config(sess, args.lesson_id)
        hls_url, title = resolve_hls_url(sess, cfg["playlist_id"], cfg["signature"],
                                         quality=args.quality)
        print(f"[resolved] {title}: {hls_url}")
    else:
        sess = requests.Session()
        sess.headers.update(HEADERS)

    out_path = Path(args.out) if args.out else Path(f"{title[:60]}.ts")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    download_hls(sess, hls_url, out_path, use_ffmpeg=not args.no_ffmpeg)


if __name__ == "__main__":
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    main()
