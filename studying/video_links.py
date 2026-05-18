#!/usr/bin/env python3
"""
studying.jp 全コース 動画リンク収集

各レッスンページから aircourse.com プレイリスト ID と署名を取得し、
video_links.json に保存する。実際の動画ダウンロードは行わない。

Usage:
    python video_links.py
    python video_links.py --course-ids 2098 2109
    python video_links.py --out my_links.json
"""

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import requests
from playwright.async_api import async_playwright

BASE_URL    = "https://member.studying.jp"
AIRCOURSE   = "https://video.aircourse.com/api/player/id/1/accounts/1/playlists/{pid}?{sig}"
CDN_HLS     = "https://d2sp4gi8kq98cy.cloudfront.net/1/{key}/HLS/Playlist.m3u8"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

CPA_COURSE_IDS = [2098, 2109, 2110, 2099, 2106, 2107, 2252]


# ──────────────────────────────────────────────────────────────────────────────

async def login(email: str, password: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

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

        # コース名取得
        await page.click('a:has-text("コース")', timeout=10000)
        await page.wait_for_timeout(2000)
        links = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href*="/course/id/"]'))
                .map(a => ({name: a.innerText.trim(), href: a.href}))
                .filter(x => x.name.length > 1)
        """)

        cookies = await ctx.cookies()
        await browser.close()

    sess = requests.Session()
    sess.headers.update(HEADERS)
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    course_names = {}
    for lnk in links:
        m = re.search(r'/course/id/(\d+)/', lnk["href"])
        if m:
            cid = int(m.group(1))
            if cid not in course_names and lnk["name"]:
                course_names[cid] = lnk["name"]

    return sess, course_names


def get_subcats(sess: requests.Session, course_id: int) -> list[str]:
    r = sess.get(f"{BASE_URL}/course/index/list/id/{course_id}/", timeout=30)
    ids = re.findall(r"open_detail\('lesson',\s*(\d+)", r.text)
    return list(dict.fromkeys(ids))


def get_lessons_in_subcat(sess: requests.Session, subcat_id: str) -> list[dict]:
    r = sess.get(f"{BASE_URL}/course/index/lesson/id/{subcat_id}/", timeout=30)
    html = r.text

    lessons = []
    for anchor in re.findall(r'<a\b[^>]*>', html, re.DOTALL):
        href_m = re.search(r'href=["\']([^"\']+course/lesson[^"\']+)["\']', anchor)
        if not href_m:
            continue
        href = href_m.group(1)
        url_id_m = re.search(r'/id/(\d+)/', href)
        if not url_id_m:
            continue
        lesson_id = url_id_m.group(1)
        title_m = re.search(r'title=["\']([^"\']+)["\']', anchor)
        title = title_m.group(1) if title_m else lesson_id
        lessons.append({"id": lesson_id, "title": title, "href": href})

    return lessons


def extract_video_config(sess: requests.Session, lesson_id: str) -> dict | None:
    """レッスンページから aircourse data-id / data-signature を取得"""
    url = f"{BASE_URL}/course/lesson/index/id/{lesson_id}/f/0/"
    try:
        r = sess.get(url, timeout=30)
    except Exception as e:
        print(f"    [video] GET失敗 lesson={lesson_id}: {e}")
        return None

    html = r.text
    pid_m = re.search(r'data-id=["\'](\d+)["\']', html)
    sig_m = re.search(r'data-signature=["\']([^"\']+)["\']', html)
    if not pid_m or not sig_m:
        return None

    playlist_id = pid_m.group(1)
    signature   = sig_m.group(1)
    return {"playlist_id": playlist_id, "signature": signature}


def resolve_video_key(sess: requests.Session, playlist_id: str, signature: str) -> dict | None:
    """aircourse API を叩いてビデオキー・タイトル・尺を取得"""
    api_url = AIRCOURSE.format(pid=playlist_id, sig=signature)
    try:
        r = sess.get(api_url, timeout=20, headers={**HEADERS, "Referer": BASE_URL + "/"})
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    # レスポンス構造: {"playlists": [{"video_key": ..., "title": ..., "duration": ...}]}
    playlists = data.get("playlists") or data.get("playlist") or []
    if isinstance(playlists, dict):
        playlists = [playlists]
    if not playlists:
        # フラットな構造の場合
        vkey = data.get("video_key") or data.get("videoKey")
        if vkey:
            playlists = [data]
        else:
            return None

    videos = []
    for item in playlists:
        vkey = item.get("video_key") or item.get("videoKey") or item.get("key")
        if not vkey:
            continue
        videos.append({
            "video_key": vkey,
            "title":     item.get("title", ""),
            "duration":  item.get("duration") or item.get("play_time"),
            "hls_url":   CDN_HLS.format(key=vkey),
        })

    return {"raw": data, "videos": videos} if videos else None


def collect_course(sess, course_id: int, course_name: str, delay: float = 0.5) -> list[dict]:
    print(f"\n[course {course_id}] {course_name}")
    subcats = get_subcats(sess, course_id)
    print(f"  サブカテゴリ {len(subcats)} 件")

    results = []
    for sci, sc_id in enumerate(subcats):
        lessons = get_lessons_in_subcat(sess, sc_id)
        video_lessons = [l for l in lessons if "lesson" in l.get("href", "")]
        if not video_lessons:
            continue
        print(f"  [subcat {sc_id}] レッスン {len(lessons)} 件 (動画候補 {len(video_lessons)} 件)")

        for les in video_lessons:
            lid = les["id"]
            cfg = extract_video_config(sess, lid)
            if not cfg:
                continue

            info = {
                "course_id":    course_id,
                "course_name":  course_name,
                "subcat_id":    sc_id,
                "lesson_id":    lid,
                "lesson_title": les["title"],
                "playlist_id":  cfg["playlist_id"],
                "signature":    cfg["signature"],
                "lesson_url":   f"{BASE_URL}/course/lesson/index/id/{lid}/f/0/",
            }

            # aircourse API でビデオキー解決（オプション: 失敗しても記録は残す）
            vdata = resolve_video_key(sess, cfg["playlist_id"], cfg["signature"])
            if vdata:
                info["videos"] = vdata["videos"]
                for v in vdata["videos"]:
                    print(f"    ✓ {les['title'][:40]} → {v['video_key']} ({v['duration']}s)")
            else:
                info["videos"] = []
                print(f"    ? {les['title'][:40]} → API解決失敗 (playlist={cfg['playlist_id']})")

            results.append(info)
            time.sleep(delay)

    return results


async def main():
    parser = argparse.ArgumentParser(description="studying.jp 動画リンク全収集")
    parser.add_argument("--email",      default=os.getenv("STUDYIN_EMAIL"))
    parser.add_argument("--password",   default=os.getenv("STUDYIN_PASSWORD"))
    parser.add_argument("--course-ids", type=int, nargs="+", default=None)
    parser.add_argument("--out",        default="video_links.json")
    parser.add_argument("--delay",      type=float, default=0.5, help="リクエスト間隔 (秒)")
    parser.add_argument("--no-resolve", action="store_true",
                        help="aircourse API を叩かず playlist_id + signature のみ保存")
    args = parser.parse_args()

    if not args.email or not args.password:
        raise SystemExit("STUDYIN_EMAIL / STUDYIN_PASSWORD が未設定")

    sess, course_names = await login(args.email, args.password)

    target_ids = args.course_ids or CPA_COURSE_IDS
    all_links  = []

    for cid in target_ids:
        cname = course_names.get(cid, f"course_{cid}")
        links = collect_course(sess, cid, cname, delay=args.delay)
        all_links.extend(links)
        # 途中保存
        Path(args.out).write_text(json.dumps(all_links, ensure_ascii=False, indent=2))
        print(f"  → {args.out} に {len(all_links)} 件保存済み")

    print(f"\n完了: 合計 {len(all_links)} レッスン → {args.out}")


if __name__ == "__main__":
    # .env 読み込み
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    asyncio.run(main())
