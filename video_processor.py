#!/usr/bin/env python3
"""
Multi-Platform Video Subtitle Extractor
Extracts subtitles/transcripts from multiple video platforms for AI summarization.

Supported platforms:
  - Bilibili (public API with WBI signing)
  - YouTube (youtube-transcript-api or yt-dlp)
  - Douyin / TikTok (yt-dlp)
  - Xiaohongshu (yt-dlp)
  - Any yt-dlp supported site (1800+ sites)

Fallback chain:
  1. Platform-specific subtitle API (free, no auth)
  2. yt-dlp subtitle extraction
  3. (Douyin recovery) active browser-player media + local Whisper ASR
  4. yt-dlp audio download + Whisper ASR (local or API)
"""

import ssl
try:
    import certifi

    def _verified_https_context(*args, **kwargs):
        kwargs.setdefault("cafile", certifi.where())
        return ssl.create_default_context(*args, **kwargs)

    ssl._create_default_https_context = _verified_https_context
except ImportError:
    # Keep Python's normal certificate verification when certifi is unavailable.
    pass

import atexit
import glob
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import reduce

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")
SCREENSHOTS_DIR = os.path.join(SCRIPT_DIR, "screenshots")
COOKIES_PATHS = [
    os.path.join(SCRIPT_DIR, "cookies.txt"),
    os.path.join(SCRIPT_DIR, "www.douyin.com_cookies.txt"),
    os.path.join(SCRIPT_DIR, "www.xiaohongshu.com_cookies.txt"),
    os.path.join(SCRIPT_DIR, "www.tiktok.com_cookies.txt"),
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Result cache (avoids re-downloading audio / re-running Whisper)
# ---------------------------------------------------------------------------

def _cache_key(url):
    """Normalize URL and produce a stable hash for caching."""
    normalized = re.sub(r'[?&](vd_source|spm_id_from|from|seid)=[^&]*', '', url)
    normalized = normalized.rstrip('/?')
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _read_cache(url):
    key = _cache_key(url)
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(path):
        ttl_days = load_config().get("cache_ttl_days", 7)
        if ttl_days > 0:
            age_days = (time.time() - os.path.getmtime(path)) / 86400
            if age_days > ttl_days:
                log(f"Cache expired ({age_days:.1f}d > {ttl_days}d): {key}")
                return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("subtitle_text"):
                log(f"Cache hit: {key}")
                data["_cached"] = True
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return None


def _write_cache(url, result):
    """Cache a successful extraction result."""
    if not result or not result.get("subtitle_text"):
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _cache_key(url)
    path = os.path.join(CACHE_DIR, f"{key}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log(f"Cached result: {key}")
    except IOError as e:
        log(f"Failed to write cache: {e}", "WARN")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

PLATFORM_PATTERNS = [
    ("bilibili", [
        r"bilibili\.com/video/",
        r"b23\.tv/",
        r"^BV[a-zA-Z0-9]+$",
    ]),
    ("youtube", [
        r"youtube\.com/watch",
        r"youtube\.com/shorts/",
        r"youtu\.be/",
        r"youtube\.com/live/",
    ]),
    ("douyin", [
        r"douyin\.com/",
        r"v\.douyin\.com/",
        r"iesdouyin\.com/",
    ]),
    ("xiaohongshu", [
        r"xiaohongshu\.com/",
        r"xhslink\.com/",
    ]),
    ("tiktok", [
        r"tiktok\.com/",
    ]),
]


def detect_platform(url):
    for platform, patterns in PLATFORM_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, url):
                return platform
    return "generic"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def http_get(url, headers=None, raw_bytes=False):
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        if raw_bytes:
            return data
        return data.decode("utf-8")


def api_request(url, params=None, headers=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        text = http_get(url, headers)
        return json.loads(text)
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code}: {url}", "ERROR")
    except urllib.error.URLError as e:
        log(f"URL error: {e.reason}", "ERROR")
    except Exception as e:
        log(f"Request failed: {e}", "ERROR")
    return None


def log(msg, level="INFO"):
    print(f"[{level}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Bilibili extractor (public API, no cookies)
# ---------------------------------------------------------------------------

BILI_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}

API_VIDEO_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYER_V2 = "https://api.bilibili.com/x/player/wbi/v2"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_CONCLUSION = "https://api.bilibili.com/x/web-interface/view/conclusion/get"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _get_mixin_key(orig):
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _get_wbi_keys():
    try:
        data = api_request(API_NAV, headers=BILI_HEADERS)
        if data and data.get("code") == 0:
            wbi_img = data["data"]["wbi_img"]
            img_key = wbi_img["img_url"].rsplit("/", 1)[1].split(".")[0]
            sub_key = wbi_img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            return img_key, sub_key
    except Exception as e:
        log(f"Failed to get WBI keys: {e}", "WARN")
    return None, None


def _sign_wbi(params, img_key, sub_key):
    mixin_key = _get_mixin_key(img_key + sub_key)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    params = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def _extract_bvid(url_or_bvid):
    if re.match(r"^BV[a-zA-Z0-9]+$", url_or_bvid):
        return url_or_bvid
    for pattern in [
        r"bilibili\.com/video/(BV[a-zA-Z0-9]+)",
        r"b23\.tv/(BV[a-zA-Z0-9]+)",
        r"(BV[a-zA-Z0-9]{10})",
    ]:
        match = re.search(pattern, url_or_bvid)
        if match:
            return match.group(1)
    return None


def _bili_parse_subtitle_list(subtitles):
    urls = []
    for sub in subtitles:
        sub_url = sub.get("subtitle_url", "")
        if sub_url:
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            urls.append({
                "url": sub_url,
                "lang": sub.get("lan", "unknown"),
                "lang_doc": sub.get("lan_doc", "unknown"),
            })
    return urls


def _bili_download_subtitle(url):
    try:
        text = http_get(url, headers=BILI_HEADERS)
        data = json.loads(text)
        body = data.get("body", [])
        if not body:
            return None
        lines = [item.get("content", "").strip() for item in body]
        return "\n".join(line for line in lines if line)
    except Exception as e:
        log(f"Failed to download subtitle: {e}", "ERROR")
        return None


def _bili_pick_and_download(subtitle_urls):
    preferred_langs = ["zh-CN", "zh-Hans", "ai-zh", "zh"]
    for lang in preferred_langs:
        for sub in subtitle_urls:
            if lang in sub["lang"]:
                text = _bili_download_subtitle(sub["url"])
                if text:
                    return text, sub["lang_doc"]
    for sub in subtitle_urls:
        text = _bili_download_subtitle(sub["url"])
        if text:
            return text, sub["lang_doc"]
    return None, None


def extract_bilibili(url):
    bvid = _extract_bvid(url)
    if not bvid:
        return None

    data = api_request(API_VIDEO_VIEW, params={"bvid": bvid}, headers=BILI_HEADERS)
    if not data or data.get("code") != 0:
        log(f"Failed to get video info: {data}", "ERROR")
        return None

    video = data["data"]
    info = {
        "title": video.get("title", ""),
        "author": video.get("owner", {}).get("name", ""),
        "duration": video.get("duration", 0),
        "description": video.get("desc", ""),
    }
    cid = video.get("cid")
    aid = video.get("aid")
    mid = video.get("owner", {}).get("mid")
    bvid = video.get("bvid", bvid)

    log(f"Bilibili video: {info['title']} (aid={aid}, cid={cid})")

    if not cid:
        log("Could not find cid", "ERROR")
        return _make_result(info, "bilibili", url, error="Could not find cid for this video")

    # Method 1: subtitle list from view API
    subtitle_urls = _bili_parse_subtitle_list(
        video.get("subtitle", {}).get("list", [])
    )

    # Method 2: player v2 API with WBI signing
    if not subtitle_urls:
        log("Trying player v2 API with WBI signing...")
        params = {"bvid": bvid, "cid": cid}
        img_key, sub_key = _get_wbi_keys()
        if img_key and sub_key:
            params = _sign_wbi(params, img_key, sub_key)
        resp = api_request(API_PLAYER_V2, params=params, headers=BILI_HEADERS)
        if resp and resp.get("code") == 0:
            subtitle_urls = _bili_parse_subtitle_list(
                resp.get("data", {}).get("subtitle", {}).get("subtitles", [])
            )

    # Method 3: page HTML scraping
    if not subtitle_urls:
        log("Trying page HTML scraping...")
        try:
            html = http_get(f"https://www.bilibili.com/video/{bvid}/", headers=BILI_HEADERS)
            for match in re.findall(r'"subtitle_url"\s*:\s*"(//[^"]+)"', html):
                sub_url = "https:" + match
                if not any(u["url"] == sub_url for u in subtitle_urls):
                    subtitle_urls.append({"url": sub_url, "lang": "zh-CN", "lang_doc": "中文（自动生成）"})
            for block in re.findall(r'"subtitles"\s*:\s*\[(\{.*?\})\]', html, re.DOTALL):
                try:
                    for sub in json.loads(f"[{block}]"):
                        sub_url = sub.get("subtitle_url", "")
                        if sub_url:
                            if sub_url.startswith("//"):
                                sub_url = "https:" + sub_url
                            if not any(u["url"] == sub_url for u in subtitle_urls):
                                subtitle_urls.append({
                                    "url": sub_url,
                                    "lang": sub.get("lan", "unknown"),
                                    "lang_doc": sub.get("lan_doc", "unknown"),
                                })
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            log(f"Page scraping failed: {e}", "WARN")

    # Method 4: multi-page video
    if not subtitle_urls:
        pages = video.get("pages", [])
        if len(pages) > 1:
            for page in pages:
                page_cid = page.get("cid")
                if page_cid and page_cid != cid:
                    params = {"bvid": bvid, "cid": page_cid}
                    img_key, sub_key = _get_wbi_keys()
                    if img_key and sub_key:
                        params = _sign_wbi(params, img_key, sub_key)
                    resp = api_request(API_PLAYER_V2, params=params, headers=BILI_HEADERS)
                    if resp and resp.get("code") == 0:
                        subtitle_urls = _bili_parse_subtitle_list(
                            resp.get("data", {}).get("subtitle", {}).get("subtitles", [])
                        )
                        if subtitle_urls:
                            break

    # Download best subtitle
    if subtitle_urls:
        text, lang = _bili_pick_and_download(subtitle_urls)
        if text:
            return _make_result(info, "bilibili", url, text, "subtitle")

    # Method 5: B站 AI conclusion API
    if aid and cid and mid:
        log("Trying B站 AI conclusion API...")
        params = {"aid": aid, "cid": cid, "up_mid": mid}
        img_key, sub_key = _get_wbi_keys()
        if img_key and sub_key:
            params = _sign_wbi(params, img_key, sub_key)
        resp = api_request(API_CONCLUSION, params=params, headers=BILI_HEADERS)
        if resp and resp.get("code") == 0:
            model_result = resp.get("data", {}).get("model_result", {})
            if model_result:
                parts = []
                summary = model_result.get("summary", "")
                if summary:
                    parts.append(summary)
                for section in model_result.get("outline", []):
                    title = section.get("title", "")
                    if title:
                        parts.append(f"\n## {title}")
                    for kp in section.get("key_point", []):
                        content = kp.get("content", "")
                        if content:
                            parts.append(f"- {content}")
                if parts:
                    return _make_result(info, "bilibili", url, "\n".join(parts), "ai_conclusion")

    return _make_result(info, "bilibili", url)


# ---------------------------------------------------------------------------
# YouTube extractor
# ---------------------------------------------------------------------------

def _extract_youtube_id(url):
    patterns = [
        r"youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/live/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def extract_youtube(url):
    video_id = _extract_youtube_id(url)
    if not video_id:
        return None

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    info = {"title": "", "author": "", "duration": 0, "description": ""}

    # Try youtube-transcript-api first (lightweight, no deps beyond pip)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        log("Using youtube-transcript-api...")
        try:
            ytt = YouTubeTranscriptApi()
            transcript_list = ytt.list(video_id)
            transcript = None
            for lang in ["zh-Hans", "zh-CN", "zh", "zh-Hant", "en"]:
                try:
                    transcript = transcript_list.find_transcript([lang])
                    break
                except Exception:
                    continue
            if not transcript:
                try:
                    transcript = transcript_list.find_generated_transcript(["zh-Hans", "zh-CN", "zh", "en"])
                except Exception:
                    transcripts = list(transcript_list)
                    if transcripts:
                        transcript = transcripts[0]

            if transcript:
                fetched = transcript.fetch()
                lines = []
                for entry in fetched:
                    if isinstance(entry, dict):
                        lines.append(entry.get("text", "").strip())
                    elif hasattr(entry, "text"):
                        lines.append(entry.text.strip())
                    else:
                        lines.append(str(entry).strip())
                text = "\n".join(line for line in lines if line)
                if text:
                    _fill_youtube_info(info, video_id)
                    return _make_result(info, "youtube", canonical_url, text, "transcript_api")
        except Exception as e:
            log(f"youtube-transcript-api failed: {e}", "WARN")
    except ImportError:
        log("youtube-transcript-api not installed, will try yt-dlp", "WARN")

    _fill_youtube_info(info, video_id)
    return _make_result(info, "youtube", canonical_url)


def _fill_youtube_info(info, video_id):
    """Best-effort fill of YouTube video metadata via oembed (no API key needed)."""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        data = json.loads(http_get(oembed_url))
        info["title"] = data.get("title", "")
        info["author"] = data.get("author_name", "")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Douyin extractor (fetches page HTML to extract video description)
# ---------------------------------------------------------------------------

DOUYIN_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.douyin.com",
    "Cookie": "s_v_web_id=verify_placeholder",
}

DOUYIN_MOBILE_UA = (
    "com.ss.android.ugc.aweme/110101 "
    "(Linux; U; Android 12; en_US; Pixel 6; Build/SD1A.210817.036; "
    "Cronet/TTNetVersion:b4d74d15 2023-04-08)"
)

_auto_cookies_path = None


def _cleanup_auto_cookies():
    """Remove the temporary cookie jar created for a single process run."""
    if _auto_cookies_path and os.path.isfile(_auto_cookies_path):
        try:
            os.unlink(_auto_cookies_path)
        except OSError:
            pass


atexit.register(_cleanup_auto_cookies)


def _fetch_fresh_douyin_cookies():
    """Generate fresh Douyin cookies (s_v_web_id, ttwid, etc.) required by yt-dlp.
    s_v_web_id is a client-side cookie generated by JS — we synthesize it here.
    ttwid is obtained by hitting douyin.com and collecting the Set-Cookie header.
    Writes Netscape cookie-jar format for yt-dlp. Returns file path or None."""
    import http.cookiejar
    import secrets
    import time as _time

    global _auto_cookies_path
    if _auto_cookies_path and os.path.isfile(_auto_cookies_path):
        return _auto_cookies_path

    cookie_path = None
    try:
        cj = http.cookiejar.MozillaCookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
        )
        req = urllib.request.Request("https://www.douyin.com/", headers={
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        try:
            opener.open(req, timeout=15).read()
        except Exception:
            pass

        existing_names = {c.name for c in cj}

        if "s_v_web_id" not in existing_names:
            verify_id = f"verify_{secrets.token_hex(16)}"
            cj.set_cookie(http.cookiejar.Cookie(
                version=0, name="s_v_web_id", value=verify_id,
                port=None, port_specified=False,
                domain=".douyin.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True,
                secure=True, expires=int(_time.time()) + 86400 * 30,
                discard=False, comment=None, comment_url=None,
                rest={"HttpOnly": None},
            ))

        if "ttwid" not in existing_names:
            try:
                ttwid_req = urllib.request.Request(
                    "https://ttwid.bytedance.com/ttwid/union/register/",
                    data=json.dumps({
                        "region": "cn",
                        "aid": 1128,
                        "needFid": False,
                        "service": "www.ixigua.com",
                        "migrate_info": {"ticket": "", "source": "node"},
                        "cbUrlProtocol": "https",
                        "union": True,
                    }).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": DEFAULT_HEADERS["User-Agent"],
                    },
                    method="POST",
                )
                resp = urllib.request.urlopen(ttwid_req, timeout=10)
                for header_val in resp.headers.get_all("Set-Cookie") or []:
                    if "ttwid=" in header_val:
                        ttwid_val = header_val.split("ttwid=")[1].split(";")[0]
                        cj.set_cookie(http.cookiejar.Cookie(
                            version=0, name="ttwid", value=ttwid_val,
                            port=None, port_specified=False,
                            domain=".douyin.com", domain_specified=True, domain_initial_dot=True,
                            path="/", path_specified=True,
                            secure=True, expires=int(_time.time()) + 86400 * 30,
                            discard=False, comment=None, comment_url=None,
                            rest={"HttpOnly": None},
                        ))
            except Exception as e:
                log(f"ttwid fetch failed (non-fatal): {e}", "WARN")

        fd, cookie_path = tempfile.mkstemp(prefix="douyin-cookies-", suffix=".txt")
        os.close(fd)
        os.chmod(cookie_path, 0o600)
        cj.save(cookie_path, ignore_discard=True, ignore_expires=True)
        names = [c.name for c in cj]
        log(f"Auto-generated {len(names)} Douyin cookies: {', '.join(names)}")
        _auto_cookies_path = cookie_path
        return _auto_cookies_path
    except Exception as e:
        if cookie_path and os.path.exists(cookie_path):
            try:
                os.unlink(cookie_path)
            except OSError:
                pass
        log(f"Auto-fetch Douyin cookies failed: {e}", "WARN")
        return None


def _resolve_douyin_url(url):
    """Resolve v.douyin.com short link to full URL, extract video ID."""
    try:
        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resolved = resp.url
        match = re.search(r'douyin\.com/video/(\d+)', resolved)
        if match:
            return match.group(1), resolved
    except Exception as e:
        log(f"Failed to resolve Douyin URL: {e}", "WARN")
    match = re.search(r'douyin\.com/video/(\d+)', url)
    if match:
        return match.group(1), url
    return None, url


def _douyin_share_api(video_id):
    """Fetch video metadata from iesdouyin.com share page (mobile UA, no login).
    Returns (info_dict, play_url, subtitle_text, subtitle_coverage)."""
    share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    try:
        req = urllib.request.Request(share_url, headers={
            "User-Agent": DOUYIN_MOBILE_UA,
            "Accept": "*/*",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        m = re.search(r"_ROUTER_DATA\s*=\s*", html)
        if not m:
            log("Douyin share page has no embedded video router data.", "WARN")
            return None, None, None, None
        json_start = m.end()
        script_end = html.find("</script>", json_start)
        if script_end == -1:
            log("Douyin share page contains incomplete router data.", "WARN")
            return None, None, None, None
        raw_json = html[json_start:script_end].strip()

        router_data = json.loads(raw_json)
        item = _find_douyin_item(router_data, video_id)
        if not item:
            log("Douyin share page did not expose item metadata for this video.", "WARN")
            return None, None, None, None

        info = {
            "id": str(video_id),
            "title": item.get("desc", ""),
            "author": (item.get("author") or {}).get("nickname", ""),
            "duration": (item.get("video") or {}).get("duration", 0),
            "description": item.get("desc", ""),
        }
        dur = info["duration"]
        if isinstance(dur, (int, float)) and dur > 10000:
            info["duration"] = dur / 1000.0

        play_url = None
        video_obj = item.get("video", {})
        for addr_key in ("play_addr", "play_addr_h264", "download_addr"):
            addr = video_obj.get(addr_key, {})
            if isinstance(addr, dict):
                urls = addr.get("url_list", [])
                if urls:
                    play_url = urls[0]
                    break

        subtitle_text, subtitle_coverage = _extract_douyin_caption_text(item)
        return info, play_url, subtitle_text, subtitle_coverage
    except Exception as e:
        log(f"Douyin share API failed: {e}", "WARN")
    return None, None, None, None


def _find_douyin_item(data, video_id):
    """Find an aweme item across old and new share-page payload layouts."""
    stack = [data]
    items = []
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            item_list = node.get("item_list")
            if isinstance(item_list, list):
                items.extend(item for item in item_list if isinstance(item, dict))
            stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
        elif isinstance(node, list):
            stack.extend(value for value in node if isinstance(value, (dict, list)))

    for item in items:
        item_id = str(item.get("aweme_id") or item.get("id") or "")
        if item_id == str(video_id):
            return item
    return items[0] if items else None


def _extract_douyin_caption_text(item):
    """Download creator/auto captions exposed in Douyin's share-page payload."""
    video = item.get("video") or {}
    candidates = []

    for caption in video.get("subtitle_infos") or []:
        if isinstance(caption, dict):
            candidates.append((caption.get("LanguageCodeName") or caption.get("LanguageCode") or "",
                               caption.get("Url") or caption.get("url")))

    cla_info = video.get("cla_info") or {}
    for caption in cla_info.get("caption_infos") or []:
        if isinstance(caption, dict):
            candidates.append((caption.get("lang") or caption.get("language") or "",
                               caption.get("url") or caption.get("Url")))

    for sticker in item.get("interaction_stickers") or []:
        if not isinstance(sticker, dict):
            continue
        caption_info = sticker.get("auto_video_caption_info") or {}
        for caption in caption_info.get("auto_captions") or []:
            if not isinstance(caption, dict):
                continue
            url_value = caption.get("url") or caption.get("Url")
            if isinstance(url_value, dict):
                url_value = (url_value.get("url_list") or [None])[0]
            candidates.append((caption.get("language") or caption.get("lang") or "", url_value))

    unique = []
    seen = set()
    for language, url in candidates:
        if isinstance(url, str) and url.startswith("//"):
            url = "https:" + url
        if not isinstance(url, str) or not url.startswith("https://") or url in seen:
            continue
        seen.add(url)
        unique.append((language.lower(), url))
    unique.sort(key=lambda pair: 0 if any(
        tag in pair[0] for tag in ("zh", "chinese", "中文")) else 1)

    for _, caption_url in unique:
        try:
            payload = http_get(caption_url, raw_bytes=True)
            text, coverage = _parse_douyin_caption_payload(payload)
            if text:
                log("Extracted captions from Douyin share metadata.")
                return text, coverage
        except Exception as e:
            log(f"Douyin caption download failed: {e}", "WARN")
    return None, None


def _parse_douyin_caption_payload(payload):
    """Parse Douyin JSON utterances or a text subtitle file."""
    decoded = payload.decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError:
        return _parse_vtt_srt(decoded), _subtitle_text_end_time(decoded)

    utterances = data.get("utterances") or data.get("captions") or data.get("segments") or []
    lines = []
    coverage = 0.0
    for segment in utterances:
        if not isinstance(segment, dict):
            continue
        line = str(segment.get("text") or segment.get("utf8") or "").strip()
        if not line:
            continue
        lines.append(line)
        end = segment.get("end_time", segment.get("endTimeMs", segment.get("end_ms")))
        if isinstance(end, (int, float)):
            coverage = max(coverage, float(end) / 1000.0 if end > 1000 else float(end))
    return ("\n".join(lines), coverage or None) if lines else (None, None)


def _subtitle_text_end_time(content):
    """Return the last end timestamp in an SRT/VTT subtitle document."""
    times = re.findall(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})",
        content,
    )
    if not times:
        return None
    hours, minutes, seconds, millis = map(int, times[-1][4:])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def _download_douyin_audio(play_url, tmp_dir):
    """Download Douyin video (as audio source) directly from play_url.
    Returns the saved file path or None."""
    if not play_url:
        return None
    try:
        audio_path = os.path.join(tmp_dir, "douyin_audio.mp4")
        req = urllib.request.Request(play_url, headers={
            "User-Agent": DOUYIN_MOBILE_UA,
            "Referer": "https://www.douyin.com/",
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(audio_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            log(f"Downloaded Douyin video: {os.path.getsize(audio_path) // 1024}KB")
            return audio_path
    except Exception as e:
        log(f"Douyin direct download failed: {e}", "WARN")
    return None



# ---------------------------------------------------------------------------
# Xiaohongshu extractor (direct page parsing, no cookies needed)
# ---------------------------------------------------------------------------

XHS_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)


def _resolve_xhs_url(url):
    """Resolve xhslink.com short link and extract note ID."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": XHS_MOBILE_UA,
            "Accept": "text/html,*/*",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            resolved = resp.url
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"Failed to resolve XHS URL: {e}", "WARN")
        return None, None, url

    for pat in [
        r"/discovery/item/([a-f0-9]+)",
        r"/explore/([a-f0-9]+)",
        r"noteId[\"=:]\"?([a-f0-9]{24})",
    ]:
        m = re.search(pat, resolved)
        if m:
            return m.group(1), html, resolved

    return None, html, resolved


def _parse_xhs_page(html):
    """Parse XHS page HTML for note data and video URLs from __SETUP_SERVER_STATE__."""
    m = re.search(r"window\.__SETUP_SERVER_STATE__\s*=\s*", html)
    if not m:
        return None, None

    json_start = m.end()
    script_end = html.find("</script>", json_start)
    if script_end == -1:
        return None, None

    raw = html[json_start:script_end].strip()
    if raw.endswith(";"):
        raw = raw[:-1]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None

    note_data = data.get("LAUNCHER_SSR_STORE_PAGE_DATA", {}).get("noteData", {})
    if not note_data:
        return None, None

    user = note_data.get("user", {})
    video_obj = note_data.get("video", {})
    media = video_obj.get("media", {})
    capa = video_obj.get("capa", {})
    vid = media.get("video", {})

    duration = capa.get("duration") or vid.get("duration", 0)
    if isinstance(duration, (int, float)) and duration > 10000:
        duration = duration / 1000.0

    info = {
        "title": note_data.get("title", ""),
        "author": user.get("nickName", ""),
        "duration": duration,
        "description": note_data.get("desc", ""),
    }

    play_url = None
    stream = media.get("stream", {})
    for codec in ("h264", "h265", "av1", "h266"):
        streams = stream.get(codec, [])
        if streams and isinstance(streams, list):
            best = streams[0]
            play_url = best.get("masterUrl")
            if play_url:
                break
            backup = best.get("backupUrls", [])
            if backup:
                play_url = backup[0]
                break

    return info, play_url


def extract_xiaohongshu(url):
    """Extract Xiaohongshu video info via mobile page HTML (no cookies needed)."""
    note_id, html, resolved_url = _resolve_xhs_url(url)

    if html:
        info, play_url = _parse_xhs_page(html)
        if info:
            log(f"XHS note: {info['title']} (duration={info['duration']}s)")
            result = _make_result(info, "xiaohongshu", resolved_url)
            result["_play_url"] = play_url
            return result

    return _make_result(
        {"title": "", "author": "", "duration": 0, "description": ""},
        "xiaohongshu", resolved_url,
    )


def _download_xhs_video(play_url, tmp_dir):
    """Download XHS video directly from CDN URL. Returns file path or None."""
    if not play_url:
        return None
    try:
        video_path = os.path.join(tmp_dir, "xhs_video.mp4")
        req = urllib.request.Request(play_url, headers={
            "User-Agent": XHS_MOBILE_UA,
            "Referer": "https://www.xiaohongshu.com/",
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(video_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        if os.path.exists(video_path) and os.path.getsize(video_path) > 1000:
            log(f"Downloaded XHS video: {os.path.getsize(video_path) // 1024}KB")
            return video_path
    except Exception as e:
        log(f"XHS direct download failed: {e}", "WARN")
    return None


def extract_douyin(url):
    """Extract Douyin video info via the iesdouyin share page (no cookies needed).
    Returns result dict with info (and optionally subtitle_text if found)."""
    video_id, resolved_url = _resolve_douyin_url(url)
    if not video_id:
        return None

    info, play_url, subtitle_text, subtitle_coverage = _douyin_share_api(video_id)
    if not info:
        info = {"id": str(video_id), "title": "", "author": "", "duration": 0, "description": ""}

    result = _make_result(info, "douyin", resolved_url, subtitle_text,
                          "douyin_share_subtitles" if subtitle_text else None,
                          coverage_seconds=subtitle_coverage)
    result["_play_url"] = play_url
    return result


# ---------------------------------------------------------------------------
# yt-dlp based extractor (generic, works for all yt-dlp supported sites)
# ---------------------------------------------------------------------------

PLATFORMS_NEEDING_COOKIES = {"douyin", "xiaohongshu", "tiktok"}

# The newest official stable release available when this copy was patched.
MIN_YTDLP_VERSION = (2026, 8, 19)
_cached_ytdlp_cmd = None
_cached_ytdlp_version = None

def _get_ytdlp_cmd():
    """Return the newest usable yt-dlp command available to this process."""
    global _cached_ytdlp_cmd, _cached_ytdlp_version
    if _cached_ytdlp_cmd is not None:
        return list(_cached_ytdlp_cmd)

    candidates = []
    binary = shutil.which("yt-dlp")
    if binary:
        candidates.append([binary])
    candidates.append([sys.executable, "-m", "yt_dlp"])

    usable = []
    seen = set()
    for command in candidates:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = subprocess.run(
                command + ["--version"], capture_output=True, text=True, timeout=10,
            )
        except Exception:
            continue
        version_lines = (result.stdout or "").strip().splitlines()
        if result.returncode != 0 or not version_lines:
            continue
        version_text = version_lines[0].strip()
        numeric = tuple(int(part) for part in re.findall(r"\d+", version_text)[:4])
        if numeric:
            usable.append((numeric, command, version_text))

    if not usable:
        return None

    usable.sort(key=lambda item: item[0], reverse=True)
    numeric, command, version_text = usable[0]
    _cached_ytdlp_cmd = list(command)
    _cached_ytdlp_version = version_text
    log(f"Using yt-dlp {version_text} ({' '.join(command)}).")
    if numeric[:3] < MIN_YTDLP_VERSION:
        log(
            f"yt-dlp {version_text} is older than the recommended 2026.08.19. "
            "Update the same Python environment used to run this Skill.",
            "WARN",
        )
    return list(_cached_ytdlp_cmd)


def _check_ytdlp():
    return _get_ytdlp_cmd() is not None


def _run_ytdlp(args, timeout=60, platform="generic"):
    """Run yt-dlp and return (returncode, stdout_str), retrying cookie sources.

    Cookie candidates are applied to the requested video itself. This avoids
    treating a successful homepage probe as proof that the video is accessible.
    """
    cmd = _get_ytdlp_cmd()
    if cmd is None:
        return 1, "yt-dlp not found"
    base_args = list(args[1:])
    target_url = base_args[-1] if base_args else ""
    target_key = f"{platform}:{_cache_key(target_url)}"
    if platform == "douyin" and target_key in _ytdlp_blocked_targets:
        log("Skipping repeated yt-dlp calls after Douyin rejected this video's web-detail request.", "WARN")
        return 1, "Douyin rejected the video endpoint with 403/Fresh cookies"
    candidates = _get_cookie_arg_candidates(platform)
    runtime_args = _get_ytdlp_runtime_args(cmd)
    last_result = None
    saw_douyin_block = False

    for index, (label, cookie_args) in enumerate(candidates):
        if cookie_args is None:
            auto_path = _fetch_fresh_douyin_cookies()
            if not auto_path:
                log("Skipping temporary guest cookies because they could not be created.", "WARN")
                continue
            cookie_args = ["--cookies", auto_path]
        actual_args = cmd + runtime_args + cookie_args + base_args
        log(f"Trying yt-dlp with {label}.")
        try:
            result = subprocess.run(
                actual_args, capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            _log_ytdlp_error(e.stderr, f"yt-dlp timed out with {label}")
            continue

        stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        last_result = (result.returncode, stdout)
        if result.returncode == 0:
            return last_result

        if platform == "douyin" and (
                "fresh cookies" in stderr.lower()
                or "failed to download web detail json" in stderr.lower()):
            saw_douyin_block = True
        _log_ytdlp_error(stderr, f"yt-dlp failed with {label} (exit {result.returncode})")
        if index < len(candidates) - 1:
            log("Retrying with the next available cookie source.", "WARN")

    if saw_douyin_block:
        _ytdlp_blocked_targets.add(target_key)
    return last_result or (1, "yt-dlp failed before returning a result")


def _log_ytdlp_error(stderr, heading):
    """Log useful yt-dlp diagnostics without exposing signed URLs or secrets."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not stderr:
        log(heading, "WARN")
        return

    safe = re.sub(r"https?://[^\s\]\[<>\"']+", _redact_url_query, stderr)
    safe = re.sub(
        r"(?i)(cookie|authorization|token|signature|msToken|odin_tt|ttwid)"
        r"(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[redacted]",
        safe,
    )
    lines = [line.strip() for line in safe.splitlines() if line.strip()]
    log(heading, "WARN")
    for line in lines[-8:]:
        log(line[:600], "WARN")


def _redact_url_query(match):
    url = match.group(0)
    if "?" in url:
        return url.split("?", 1)[0] + "?[query redacted]"
    return url


def _detect_browser():
    """Detect an available browser for --cookies-from-browser.
    Checks common browser data directories on Windows/Mac/Linux."""
    home = os.path.expanduser("~")
    browser_paths = {
        "chrome": [
            os.path.join(home, "AppData", "Local", "Google", "Chrome", "User Data"),
            os.path.join(home, ".config", "google-chrome"),
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
        ],
        "edge": [
            os.path.join(home, "AppData", "Local", "Microsoft", "Edge", "User Data"),
        ],
        "firefox": [
            os.path.join(home, "AppData", "Roaming", "Mozilla", "Firefox", "Profiles"),
            os.path.join(home, ".mozilla", "firefox"),
            os.path.join(home, "Library", "Application Support", "Firefox", "Profiles"),
        ],
        "brave": [
            os.path.join(home, "AppData", "Local", "BraveSoftware", "Brave-Browser", "User Data"),
        ],
    }
    for browser, paths in browser_paths.items():
        for p in paths:
            if os.path.isdir(p):
                return browser
    return None


_cached_browser = None
_ytdlp_runtime_args = None
_ytdlp_blocked_targets = set()


def _get_cookie_arg_candidates(platform):
    """Return cookie sources in order, with a no-cookie attempt as final fallback."""
    global _cached_browser
    if platform not in PLATFORMS_NEEDING_COOKIES:
        return [("no cookies", [])]

    candidates = []
    seen_paths = set()
    for cp in COOKIES_PATHS:
        if os.path.isfile(cp) and cp not in seen_paths:
            candidates.append((f"cookie file {os.path.basename(cp)}", ["--cookies", cp]))
            seen_paths.add(cp)
    for f in glob.glob(os.path.join(SCRIPT_DIR, "*cookies*.txt")):
        if (os.path.isfile(f) and f not in seen_paths
                and "_auto_" not in os.path.basename(f)):
            candidates.append((f"cookie file {os.path.basename(f)}", ["--cookies", f]))
            seen_paths.add(f)

    # Read browser cookies for the actual target URL, after user-supplied jars.
    _cached_browser = _cached_browser or _detect_browser() or ""
    if _cached_browser:
        candidates.append((f"browser cookies ({_cached_browser})",
                           ["--cookies-from-browser", _cached_browser]))

    # Create synthetic guest cookies only if real browser cookies fail.
    if platform == "douyin":
        candidates.append(("temporary guest cookies", None))

    candidates.append(("no cookies", []))
    return candidates


def _get_ytdlp_runtime_args(cmd):
    """Enable Node challenge solving when this yt-dlp build supports it."""
    global _ytdlp_runtime_args
    if _ytdlp_runtime_args is not None:
        return _ytdlp_runtime_args
    _ytdlp_runtime_args = []
    if not shutil.which("node"):
        return _ytdlp_runtime_args
    try:
        result = subprocess.run(cmd + ["--help"], capture_output=True, timeout=15)
        help_text = (result.stdout or b"").decode("utf-8", errors="replace")
        if "--js-runtimes" in help_text:
            _ytdlp_runtime_args = ["--js-runtimes", "node"]
    except Exception:
        pass
    return _ytdlp_runtime_args


def _ytdlp_extract_info(url, platform="generic"):
    """Use yt-dlp --dump-json to get video metadata without downloading."""
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-download", "--no-playlist"]
        cmd.append(url)
        rc, stdout = _run_ytdlp(cmd, platform=platform)
        if rc == 0 and stdout.strip():
            return json.loads(stdout)
    except Exception as e:
        log(f"yt-dlp info extraction failed: {e}", "WARN")
    return None


def _ytdlp_extract_subs(url, tmp_dir, platform="generic"):
    """Use yt-dlp to write subtitle files without downloading the video."""
    try:
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "zh-Hans,zh-CN,zh,en,zh-Hant",
            "--sub-format", "json3/srv3/vtt/srt/best",
            "--no-playlist",
            "-o", os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        ]
        cmd.append(url)
        _run_ytdlp(cmd, platform=platform)
    except Exception as e:
        log(f"yt-dlp subtitle download failed: {e}", "WARN")
        return None

    sub_files = (
        glob.glob(os.path.join(tmp_dir, "*.vtt"))
        + glob.glob(os.path.join(tmp_dir, "*.srt"))
        + glob.glob(os.path.join(tmp_dir, "*.json3"))
        + glob.glob(os.path.join(tmp_dir, "*.srv3"))
    )
    if not sub_files:
        return None, None

    for sf in sub_files:
        text = _parse_subtitle_file(sf)
        if text:
            return text, _subtitle_file_coverage(sf)
    return None, None


def _subtitle_file_coverage(filepath):
    """Read the final subtitle timestamp when the downloaded format has timing."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if filepath.lower().endswith(".json3"):
            events = (json.loads(content).get("events") or [])
            ends = [
                (event.get("tStartMs", 0) + event.get("dDurationMs", 0)) / 1000.0
                for event in events if isinstance(event, dict)
            ]
            return max(ends) if ends else None
        return _subtitle_text_end_time(content)
    except Exception:
        return None


def _parse_subtitle_file(filepath):
    """Parse VTT/SRT/JSON3 subtitle file into plain text."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None

    if not content.strip():
        return None

    if ext == ".json3":
        return _parse_json3_subtitle(content)
    elif ext in (".vtt", ".srt"):
        return _parse_vtt_srt(content)
    elif ext == ".srv3":
        return _parse_srv3_subtitle(content)
    return None


def _parse_json3_subtitle(content):
    try:
        data = json.loads(content)
        events = data.get("events", [])
        lines = []
        for event in events:
            segs = event.get("segs", [])
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if text and text != "\n":
                lines.append(text)
        return "\n".join(lines) if lines else None
    except Exception:
        return None


def _parse_srv3_subtitle(content):
    lines = []
    for match in re.findall(r'<p[^>]*>(.*?)</p>', content, re.DOTALL):
        text = re.sub(r'<[^>]+>', '', match).strip()
        if text:
            lines.append(text)
    return "\n".join(lines) if lines else None


def _parse_vtt_srt(content):
    lines = []
    seen = set()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r'^\d+$', line):
            continue
        if re.match(r'^WEBVTT', line):
            continue
        if re.match(r'^NOTE\s', line):
            continue
        if re.match(r'^\d{2}:\d{2}', line):
            continue
        if '-->' in line:
            continue
        text = re.sub(r'<[^>]+>', '', line).strip()
        if text and text not in seen:
            seen.add(text)
            lines.append(text)
    return "\n".join(lines) if lines else None


def _ytdlp_download_audio(url, tmp_dir, platform="generic"):
    """Download audio only via yt-dlp for Whisper transcription."""
    audio_path = os.path.join(tmp_dir, "audio.m4a")
    try:
        cmd = [
            "yt-dlp",
            "-f", "m4a/bestaudio[ext=m4a]/bestaudio",
            "--no-playlist",
            "-o", audio_path,
        ]
        cmd.append(url)
        rc, _ = _run_ytdlp(cmd, timeout=300, platform=platform)
        if rc == 0 and os.path.exists(audio_path):
            return audio_path
    except Exception as e:
        log(f"yt-dlp audio download failed: {e}", "WARN")

    found = glob.glob(os.path.join(tmp_dir, "audio.*"))
    return found[0] if found else None


def _ytdlp_download_video(url, tmp_dir, platform="generic"):
    """Download lowest-quality video via yt-dlp for frame extraction."""
    video_path = os.path.join(tmp_dir, "video.mp4")
    try:
        cmd = [
            "yt-dlp",
            "-f", "worstvideo[ext=mp4]+worstaudio/worst[ext=mp4]/worstvideo+worstaudio/worst",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", video_path,
        ]
        cmd.append(url)
        rc, out = _run_ytdlp(cmd, timeout=300, platform=platform)
        if rc == 0 and os.path.exists(video_path):
            return video_path
        log(f"yt-dlp video download returned rc={rc}", "WARN")
    except Exception as e:
        log(f"yt-dlp video download failed: {e}", "WARN")

    found = glob.glob(os.path.join(tmp_dir, "video.*"))
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Keyframe extraction
# ---------------------------------------------------------------------------

_ffmpeg_path_cache = None


def _get_ffmpeg():
    """Find ffmpeg executable. Caches after first lookup."""
    global _ffmpeg_path_cache
    if _ffmpeg_path_cache is not None:
        return _ffmpeg_path_cache or None

    path = shutil.which("ffmpeg")
    if path:
        _ffmpeg_path_cache = path
        return path

    if sys.platform == "win32":
        winget_links = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"
        )
        candidate = os.path.join(winget_links, "ffmpeg.exe")
        if os.path.isfile(candidate):
            _ffmpeg_path_cache = candidate
            return candidate

    _ffmpeg_path_cache = ""
    return None


def _check_ffmpeg():
    return _get_ffmpeg() is not None


def _get_ffprobe():
    """Find ffprobe next to ffmpeg."""
    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        return None
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if sys.platform == "win32" else ""))
    if os.path.isfile(ffprobe):
        return ffprobe
    return shutil.which("ffprobe")


def _get_video_duration_ffprobe(video_path):
    """Get video duration in seconds using ffprobe."""
    ffprobe = _get_ffprobe()
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _extract_frames_ffmpeg(video_path, output_dir, num_frames, duration=None):
    """Extract evenly-spaced frames from a video using ffmpeg.
    Returns list of frame dicts with 'path' and 'timestamp'."""
    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        log("ffmpeg not installed, skipping frame extraction", "WARN")
        return []

    if duration is None:
        duration = _get_video_duration_ffprobe(video_path)
    if not duration or duration <= 0:
        return []

    if duration < 10:
        num_frames = min(num_frames, 3)

    os.makedirs(output_dir, exist_ok=True)

    frames = []
    for i in range(num_frames):
        ts = duration * (i + 0.5) / num_frames
        out_path = os.path.join(output_dir, f"frame_{i + 1:03d}.jpg")
        try:
            subprocess.run(
                [ffmpeg, "-ss", f"{ts:.2f}", "-i", video_path,
                 "-frames:v", "1", "-q:v", "3",
                 "-vf", "scale='min(640,iw)':-1",
                 "-y", out_path],
                capture_output=True, timeout=15,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
                frames.append({"path": out_path, "timestamp": ts})
        except Exception:
            pass

    log(f"Extracted {len(frames)} keyframes from video")
    return frames


def extract_keyframes(url, platform, config, play_url=None, duration=None):
    """Download video and extract keyframes. Returns list of frame dicts or [].
    Frame dicts have 'path' (absolute) and 'timestamp' (seconds)."""
    if not config.get("extract_frames", True):
        return []
    if not _check_ffmpeg():
        log("ffmpeg not installed, skipping frame extraction", "WARN")
        return []

    num_frames = config.get("frames_per_video", 6)
    cache_key = _cache_key(url)
    frame_dir = os.path.join(SCREENSHOTS_DIR, cache_key)

    existing = sorted(glob.glob(os.path.join(frame_dir, "frame_*.jpg")))
    if existing:
        ttl_days = config.get("cache_ttl_days", 7)
        if ttl_days > 0:
            age_days = (time.time() - os.path.getmtime(existing[0])) / 86400
            if age_days > ttl_days:
                log(f"Cached frames expired ({age_days:.1f}d > {ttl_days}d)")
                shutil.rmtree(frame_dir, ignore_errors=True)
                existing = []
    if existing:
        log(f"Using {len(existing)} cached frames")
        frames = []
        for p in existing:
            idx = int(os.path.basename(p).split("_")[1].split(".")[0]) - 1
            ts = (duration or 0) * (idx + 0.5) / max(len(existing), 1)
            frames.append({"path": p, "timestamp": ts})
        return frames

    tmp_dir = tempfile.mkdtemp(prefix="frames_")
    try:
        video_path = None

        if play_url and platform == "douyin":
            video_path = _download_douyin_audio(play_url, tmp_dir)
        elif play_url and platform == "xiaohongshu":
            video_path = _download_xhs_video(play_url, tmp_dir)

        if not video_path and _check_ytdlp():
            log("Downloading video for frame extraction...")
            video_path = _ytdlp_download_video(url, tmp_dir, platform)

        if not video_path:
            return []

        if not duration:
            duration = _get_video_duration_ffprobe(video_path)

        return _extract_frames_ffmpeg(video_path, frame_dir, num_frames, duration)
    except Exception as e:
        log(f"Frame extraction failed: {e}", "WARN")
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def extract_with_ytdlp(url, platform="generic"):
    """Generic extraction using yt-dlp: metadata + subtitles."""
    if not _check_ytdlp():
        return None

    yt_info = _ytdlp_extract_info(url, platform)
    info = {
        "title": "",
        "author": "",
        "duration": 0,
        "description": "",
    }
    if yt_info:
        info["title"] = yt_info.get("title", "") or yt_info.get("fulltitle", "")
        info["author"] = yt_info.get("uploader", "") or yt_info.get("channel", "")
        info["duration"] = yt_info.get("duration", 0) or 0
        info["description"] = yt_info.get("description", "")

    tmp_dir = tempfile.mkdtemp(prefix="video_sub_")
    try:
        log(f"Trying yt-dlp subtitle extraction for {platform}...")
        text, coverage = _ytdlp_extract_subs(url, tmp_dir, platform)
        if text:
            return _make_result(info, platform, url, text, "yt_dlp_subs",
                                coverage_seconds=coverage)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return _make_result(info, platform, url)


# ---------------------------------------------------------------------------
# Whisper ASR fallback
# ---------------------------------------------------------------------------

def _download_browser_media_audio(media_url, tmp_dir):
    """Download media exposed by the active browser player, then extract audio.

    The signed media URL is accepted only in memory and is never logged or
    cached. This path is for cases where the video page plays successfully but
    the platform's metadata API rejects yt-dlp.
    """
    from urllib.parse import urlparse

    if not isinstance(media_url, str):
        return None, None
    parsed = urlparse(media_url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = ("douyinvod.com", "bytevod.com", "bytecdn.cn", "douyin.com")
    if parsed.scheme != "https" or not any(
        host == domain or host.endswith("." + domain) for domain in allowed_hosts
    ):
        log("Browser player media URL is not from an allowed Douyin media host.", "WARN")
        return None, None

    source_path = os.path.join(tmp_dir, "browser_media.bin")
    audio_path = os.path.join(tmp_dir, "browser_media.wav")
    request = urllib.request.Request(media_url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/140.0.0.0 Safari/537.36",
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            if response.status not in (200, 206):
                log(f"Browser media request returned HTTP {response.status}.", "WARN")
                return None, None
            with open(source_path, "wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
    except urllib.error.HTTPError as e:
        log(f"Browser media request returned HTTP {e.code}.", "WARN")
        return None, None
    except Exception as e:
        # URL errors can contain signed query parameters; keep diagnostics safe.
        log(f"Browser media download failed ({type(e).__name__}).", "WARN")
        return None, None

    duration = _get_video_duration_ffprobe(source_path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("ffmpeg not installed; cannot extract audio from browser media.", "ERROR")
        return None, duration
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source_path,
             "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", audio_path],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        log(f"ffmpeg audio extraction failed ({type(e).__name__}).", "ERROR")
        return None, duration
    if result.returncode != 0 or not os.path.exists(audio_path):
        log("ffmpeg could not extract audio from browser media.", "ERROR")
        return None, duration
    return audio_path, duration


def transcribe_with_whisper(url, platform, info, config, play_url=None,
                            browser_media_url=None):
    """Download audio then transcribe with Whisper.
    For Douyin, uses play_url for direct download if yt-dlp fails."""
    whisper_mode = config.get("whisper_mode", "disabled")
    if whisper_mode == "disabled":
        return None

    tmp_dir = tempfile.mkdtemp(prefix="whisper_")
    try:
        log("Downloading audio for Whisper transcription...")
        audio_path = None
        browser_duration = None

        if browser_media_url:
            log("Using media exposed by the active browser player; signed URL is kept in memory.")
            audio_path, browser_duration = _download_browser_media_audio(
                browser_media_url, tmp_dir,
            )

        if not audio_path and _check_ytdlp() and not browser_media_url:
            audio_path = _ytdlp_download_audio(url, tmp_dir, platform)

        if not audio_path and play_url:
            log("Trying direct download via play_url...")
            if platform == "xiaohongshu":
                audio_path = _download_xhs_video(play_url, tmp_dir)
            else:
                audio_path = _download_douyin_audio(play_url, tmp_dir)

        if not audio_path:
            if not _check_ytdlp():
                log("yt-dlp not installed and no direct download available", "ERROR")
            else:
                log("Failed to download audio", "ERROR")
            return None

        if whisper_mode == "api":
            text = _whisper_api(audio_path, config)
        elif whisper_mode == "local":
            text = _whisper_local(audio_path, config)
        else:
            log(f"Unknown whisper_mode: {whisper_mode}", "ERROR")
            return None

        if text:
            source = ("browser_player_whisper_" + whisper_mode
                      if browser_media_url and audio_path else f"whisper_{whisper_mode}")
            if not info.get("duration") and browser_duration:
                info["duration"] = browser_duration
            # ASR covered the complete downloaded audio file, even when the
            # source page itself exposed no timed caption track.
            return _make_result(info, platform, url, text, source,
                                coverage_seconds=info.get("duration") or browser_duration or None)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return None


def _wait_for_browser_media_url(timeout=180):
    """Accept one signed media URL from this computer's active browser only.

    The URL is sent over loopback and held in memory; it is not written to
    disk, logged, or included in transcript cache entries.
    """
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse

    token = secrets.token_urlsafe(24)
    allowed_hosts = ("douyinvod.com", "bytevod.com", "bytecdn.cn", "douyin.com")

    class Receiver(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self):
            if self.path != "/" + token:
                self.send_response(404)
                self._cors()
                self.end_headers()
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if size <= 0 or size > 16384:
                self.send_response(400)
                self._cors()
                self.end_headers()
                return
            candidate = self.rfile.read(size).decode("utf-8", errors="replace").strip()
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(
                host == domain or host.endswith("." + domain) for domain in allowed_hosts
            ):
                self.send_response(400)
                self._cors()
                self.end_headers()
                return
            self.server.media_url = candidate
            self.send_response(200)
            self._cors()
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, fmt, *args):
            # The request body is a short-lived signed URL; suppress all logs.
            return

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    server.timeout = 1
    endpoint = f"http://127.0.0.1:{server.server_port}/{token}"
    print(f"Waiting up to {timeout}s for the active Douyin player. POST its media URL to {endpoint}",
          file=sys.stderr, flush=True)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not getattr(server, "media_url", None):
            server.handle_request()
    finally:
        server.server_close()
    return getattr(server, "media_url", None)


def _whisper_api(audio_path, config):
    """Transcribe using OpenAI Whisper API."""
    api_key = config.get("openai_api_key", "")
    if not api_key:
        log("openai_api_key not configured in config.json", "ERROR")
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        language = config.get("language", "zh")

        file_size = os.path.getsize(audio_path)
        max_size = 25 * 1024 * 1024  # 25MB API limit

        if file_size > max_size:
            log(f"Audio file too large ({file_size // 1024 // 1024}MB), splitting...", "WARN")
            return _whisper_api_chunked(audio_path, client, language)

        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language=language,
                response_format="text",
            )
        return resp if isinstance(resp, str) else str(resp)
    except ImportError:
        log("openai package not installed. Run: pip install openai", "ERROR")
    except Exception as e:
        log(f"Whisper API failed: {e}", "ERROR")
    return None


def _whisper_api_chunked(audio_path, client, language):
    """Split large audio and transcribe in chunks."""
    try:
        from pydub import AudioSegment
    except ImportError:
        log("pydub not installed, cannot split audio. Run: pip install pydub", "ERROR")
        return None

    try:
        audio = AudioSegment.from_file(audio_path)
        chunk_ms = 10 * 60 * 1000  # 10 minutes per chunk
        chunks = [audio[i:i + chunk_ms] for i in range(0, len(audio), chunk_ms)]

        parts = []
        for i, chunk in enumerate(chunks):
            log(f"Transcribing chunk {i + 1}/{len(chunks)}...")
            chunk_path = audio_path + f".chunk{i}.m4a"
            chunk.export(chunk_path, format="ipod")
            with open(chunk_path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language=language,
                    response_format="text",
                )
            text = resp if isinstance(resp, str) else str(resp)
            if text:
                parts.append(text.strip())
            os.unlink(chunk_path)

        return "\n".join(parts) if parts else None
    except Exception as e:
        log(f"Chunked transcription failed: {e}", "ERROR")
        return None


def _whisper_local(audio_path, config):
    """Transcribe using faster-whisper (local model)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper not installed. Run: pip install faster-whisper", "ERROR")
        return None

    model_size = config.get("whisper_model", "small")
    language = config.get("language", "zh")
    log(f"Loading Whisper model '{model_size}'...")

    try:
        model = WhisperModel(model_size, device="auto", compute_type="auto")
        segments, _ = model.transcribe(
            audio_path,
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
        )
        lines = [seg.text.strip() for seg in segments if seg.text.strip()]
        return "\n".join(lines) if lines else None
    except Exception as e:
        log(f"Local Whisper transcription failed: {e}", "ERROR")
        return None


# ---------------------------------------------------------------------------
# Result builder
# ---------------------------------------------------------------------------

def format_duration(seconds):
    if not seconds or not isinstance(seconds, (int, float)):
        return "00:00"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _make_result(info, platform, url, subtitle_text=None, source=None, error=None,
                 coverage_seconds=None):
    return {
        "info": info,
        "platform": platform,
        "url": url,
        "subtitle_text": subtitle_text,
        "source": source,
        "error": error,
        "coverage_seconds": coverage_seconds,
    }


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def extract(url, config, browser_media_url=None):
    """
    Main entry point. Tries cache, then platform-specific extraction,
    then yt-dlp subs, then Whisper ASR. Returns a result dict.
    """
    # An explicit browser media handoff requests a fresh recovery attempt;
    # a cached partial transcript must not block processing the full audio.
    if browser_media_url:
        log("Bypassing transcript cache for the explicit browser media fallback.")
    else:
        cached = _read_cache(url)
        if cached:
            return cached

    platform = detect_platform(url)
    log(f"Detected platform: {platform}")

    # Phase 1: platform-specific subtitle extraction (fast, no heavy deps)
    result = None
    play_url = None

    if platform == "douyin" and browser_media_url:
        video_id = re.search(r"(?:video/|modal_id=)(\d+)", url)
        result = _make_result({
            "id": video_id.group(1) if video_id else "",
            "title": "",
            "author": "",
            "duration": 0,
            "description": "",
        }, platform, url)
    elif platform == "bilibili":
        result = extract_bilibili(url)
    elif platform == "youtube":
        result = extract_youtube(url)
    elif platform == "douyin":
        result = extract_douyin(url)
    elif platform == "xiaohongshu":
        result = extract_xiaohongshu(url)

    if result:
        play_url = result.get("_play_url")
    # Use the resolved canonical video URL for subsequent yt-dlp/Whisper
    # requests. Short-link hosts can reject guest-cookie retries or fail TLS.
    target_url = (result.get("url") if result and result.get("url") else url)

    final_result = None

    if result and result.get("subtitle_text"):
        final_result = result
    else:
        # Phase 2: yt-dlp subtitle extraction (works for all platforms)
        info = result["info"] if result else {"title": "", "author": "", "duration": 0, "description": ""}

        if _check_ytdlp() and not browser_media_url:
            ytdlp_result = extract_with_ytdlp(target_url, platform)
            if ytdlp_result:
                if ytdlp_result.get("subtitle_text"):
                    if not info.get("title") and ytdlp_result["info"].get("title"):
                        info = ytdlp_result["info"]
                    final_result = _make_result(info, platform, url,
                                                ytdlp_result["subtitle_text"],
                                                ytdlp_result["source"],
                                                coverage_seconds=ytdlp_result.get("coverage_seconds"))
                if not info.get("title") and ytdlp_result["info"].get("title"):
                    info = ytdlp_result["info"]
        elif not browser_media_url:
            log("yt-dlp not installed. Install with: pip install yt-dlp", "WARN")
        else:
            log("Skipping repeated yt-dlp metadata calls; using the verified browser-player media.")

        # Phase 3: Whisper ASR fallback
        if not final_result:
            whisper_result = transcribe_with_whisper(
                target_url, platform, info, config, play_url=play_url,
                browser_media_url=browser_media_url,
            )
            if whisper_result and whisper_result.get("subtitle_text"):
                final_result = whisper_result

        # All methods failed
        if not final_result:
            error_parts = ["No subtitles or transcript could be extracted."]
            if not _check_ytdlp():
                error_parts.append("Install yt-dlp for broader platform support: pip install yt-dlp")
            if platform == "youtube":
                try:
                    import youtube_transcript_api  # noqa: F401
                except ImportError:
                    error_parts.append("Install youtube-transcript-api for YouTube: pip install youtube-transcript-api")
            if platform in PLATFORMS_NEEDING_COOKIES:
                if platform == "douyin":
                    if browser_media_url:
                        error_parts.append(
                            "The browser-player media fallback was received but could not be "
                            "downloaded or transcribed. Check the sanitized ffmpeg/Whisper "
                            "diagnostic above; do not retry authentication or print the signed URL."
                        )
                    else:
                        error_parts.append(
                            "Douyin returned 403/Fresh cookies for the video's web-detail endpoint "
                            "after browser, guest, and anonymous attempts. This can persist with a "
                            "valid login when Douyin changes its API. Update yt-dlp if it is outdated; "
                            "if the error remains, stop retrying logins and use the browser-player "
                            "fallback or another available transcript source."
                        )
                else:
                    error_parts.append(
                        "The platform rejected all available access methods. Check whether the "
                        "supported local browser has an active session."
                    )
            whisper_mode = config.get("whisper_mode", "disabled")
            if whisper_mode == "disabled":
                error_parts.append("Enable Whisper in config.json for audio transcription fallback.")
            return _make_result(info, platform, url, error="\n".join(error_parts))

    # Phase 4: Extract keyframes (runs for all successful extractions)
    duration = (final_result.get("info") or {}).get("duration", 0)
    frames = ([] if browser_media_url else
              extract_keyframes(target_url, platform, config, play_url=play_url, duration=duration))
    if frames:
        final_result["frames"] = frames

    # A direct media URL can carry a short-lived signature. Keep it only in
    # memory for downloading; never put it in the persistent transcript cache.
    final_result.pop("_play_url", None)
    _write_cache(url, final_result)
    return final_result


def _clear_cache():
    """Remove all cached results and screenshots."""
    removed = 0
    for d in [CACHE_DIR, SCREENSHOTS_DIR]:
        if os.path.isdir(d):
            removed += sum(len(files) for _, _, files in os.walk(d))
            shutil.rmtree(d, ignore_errors=True)
    log(f"Cleared cache: {removed} files removed")


import argparse

def main():
    parser = argparse.ArgumentParser(description='Extract video subtitles and create summaries or scripts.')
    parser.add_argument('url', type=str, nargs='?', default=None, help='The URL of the video.')
    parser.add_argument('--mode', type=str, default='summary', choices=['summary', 'script'], help='The output mode: summary or script.')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Directory for script Markdown output (defaults to the current directory).')
    parser.add_argument('--browser-media-url-file', type=str, default=None,
                        help='Read a temporary active-player media URL from a local file; never log or cache it.')
    parser.add_argument('--wait-for-browser-media-url', action='store_true',
                        help='Wait for the verified active browser player to send its media URL over loopback.')
    parser.add_argument('--title', type=str, default=None,
                        help='Optional video title when the platform detail API is unavailable.')
    parser.add_argument('--author', type=str, default=None,
                        help='Optional video author when the platform detail API is unavailable.')
    parser.add_argument('--clear-cache', action='store_true', help='Clear all cached results and screenshots.')
    args = parser.parse_args()

    if args.clear_cache:
        _clear_cache()
        return

    if not args.url:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    browser_media_url = None
    if args.wait_for_browser_media_url:
        browser_media_url = _wait_for_browser_media_url()
        if not browser_media_url:
            log("No browser-player media URL arrived before timeout.", "ERROR")
            sys.exit(1)
    if args.browser_media_url_file:
        try:
            with open(args.browser_media_url_file, "r", encoding="utf-8") as f:
                browser_media_url = f.read().strip()
            if not browser_media_url:
                log("Browser media URL file is empty.", "ERROR")
                sys.exit(1)
        except OSError as e:
            log(f"Cannot read browser media URL file: {e}", "ERROR")
            sys.exit(1)

    log(f"Extracting subtitles for: {args.url}")
    result = extract(args.url, config, browser_media_url=browser_media_url)

    if not result:
        log("Extraction returned no result", "ERROR")
        sys.exit(1)

    info = result.get("info", {})
    if args.title and not info.get("title"):
        info["title"] = args.title
    if args.author and not info.get("author"):
        info["author"] = args.author

    if args.mode == 'script':
        if result.get("subtitle_text"):
            video_title = info.get("title", "video_script")
            safe_title = "".join(c for c in video_title if c.isalnum() or c in (' ', '_', '-')).rstrip()
            safe_title = safe_title or "video_script"
            video_id = str(info.get("id") or "")
            if not video_id:
                id_match = re.search(r"(?:video/|modal_id=)(\d+)", args.url)
                video_id = id_match.group(1) if id_match else ""
            filename = f"{safe_title}_{video_id}.md" if video_id else f"{safe_title}.md"
            output_dir = os.path.abspath(args.output_dir or os.getcwd())
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, filename)
            # Protect transcripts for different videos with identical titles,
            # and avoid replacing a previous run for the same URL.
            if os.path.exists(output_path):
                stem, ext = os.path.splitext(filename)
                index = 2
                while os.path.exists(os.path.join(output_dir, f"{stem}_{index}{ext}")):
                    index += 1
                output_path = os.path.join(output_dir, f"{stem}_{index}{ext}")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(f"# {info.get('title', '')}\n\n")
                f.write(f"**Author:** {info.get('author', '')}\n")
                f.write(f"**Duration:** {format_duration(info.get('duration', 0))}\n")
                f.write(f"**URL:** {result.get('url', args.url)}\n\n")
                f.write(f"**Source:** {result.get('source') or 'unknown'}\n")
                coverage = result.get("coverage_seconds")
                coverage_label = ("Audio duration transcribed" if "whisper" in (result.get("source") or "")
                                  else "Subtitle time coverage")
                if coverage is None:
                    f.write(f"**{coverage_label}:** unknown; compare against the full video before calling this complete.\n\n")
                else:
                    f.write(f"**{coverage_label}:** {format_duration(coverage)} / {format_duration(info.get('duration', 0))}\n\n")
                f.write("---\n\n")
                f.write(result["subtitle_text"])
            print(f"Script saved to {output_path}")
        else:
            print("Failed to extract script.", file=sys.stderr)
            if result.get("error"):
                print(result["error"], file=sys.stderr)
            sys.exit(1)
    else:  # summary mode
        output = {
            "title": info.get("title", ""),
            "author": info.get("author", ""),
            "duration": format_duration(info.get("duration", 0)),
            "description": info.get("description", ""),
            "platform": result.get("platform", "unknown"),
            "url": result.get("url", args.url),
        }

        if result.get("error"):
            output["error"] = result["error"]
            print(json.dumps(output, ensure_ascii=False, indent=2))
            sys.exit(1)

        output["source"] = result.get("source", "unknown")
        output["subtitle_text"] = result.get("subtitle_text")
        output["coverage_seconds"] = result.get("coverage_seconds")

        if result.get("frames"):
            output["frames"] = [
                {"path": f["path"], "timestamp": format_duration(int(f["timestamp"]))}
                for f in result["frames"]
            ]

        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
