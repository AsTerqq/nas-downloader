#!/usr/bin/env python3
"""
NAS Downloader Server - prehrajto.cz → NAS
Spusti: python server.py
Otvor: http://localhost:5000
"""

import os
import re
import json
import subprocess
import threading
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─── WHISPER MODEL (lazy, thread-safe) ───────────────────────────────────────
_whisper_model = None
_whisper_init_lock = threading.Lock()

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_init_lock:
            if _whisper_model is None:
                try:
                    from faster_whisper import WhisperModel
                    _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
                except Exception:
                    _whisper_model = False  # mark failed so we don't retry
    return _whisper_model if _whisper_model is not False else None


def detect_audio_language(url):
    """Download 30s of audio via yt-dlp+ffmpeg and detect language using Whisper tiny."""
    import tempfile, glob
    model = _get_whisper()
    if model is None:
        print("[whisper] model unavailable", flush=True)
        return ""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "sample.%(ext)s")
        wav_path = os.path.join(tmpdir, "audio.wav")
        try:
            print(f"[whisper] downloading 30s audio from {url[:60]}...", flush=True)
            r1 = subprocess.run([
                YTDLP_PATH, "--no-playlist",
                "-f", "bestaudio/best",
                "--download-sections", "*0-30",
                "--no-part",
                "-o", raw_path,
                url
            ], capture_output=True, timeout=90)
            print(f"[whisper] yt-dlp exit={r1.returncode}", flush=True)

            files = glob.glob(os.path.join(tmpdir, "sample.*"))
            if not files:
                print(f"[whisper] no audio file downloaded. stderr={r1.stderr[-300:].decode(errors='replace')}", flush=True)
                return ""
            print(f"[whisper] got {os.path.getsize(files[0])} bytes: {files[0]}", flush=True)

            # convert to 16kHz mono WAV for Whisper
            r2 = subprocess.run([
                "ffmpeg", "-y", "-i", files[0],
                "-ar", "16000", "-ac", "1", "-f", "wav", wav_path
            ], capture_output=True, timeout=30)

            if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 5000:
                print(f"[whisper] WAV too small or missing. ffmpeg exit={r2.returncode}", flush=True)
                return ""

            print(f"[whisper] transcribing {os.path.getsize(wav_path)} byte WAV...", flush=True)
            _, info = model.transcribe(wav_path, beam_size=1)
            print(f"[whisper] detected language: {info.language}", flush=True)
            return info.language or ""
        except Exception as e:
            print(f"[whisper] exception: {e}", flush=True)
            return ""


VERSION = "4.13"

MOVIES_PATH   = os.environ.get("MOVIES_PATH", "/data/films")
SERIES_PATH   = os.environ.get("SERIES_PATH", "/data/series")
YTDLP_PATH    = "yt-dlp"
TMDB_API_KEY  = os.environ.get("TMDB_API_KEY", "0130bf38e3b81e1adb7d0f6e9107da9a")
GITHUB_RAW    = "https://raw.githubusercontent.com/AsTerqq/nas-downloader/main/server.py"
GITHUB_ZIP    = "https://github.com/AsTerqq/nas-downloader/archive/refs/heads/main.zip"

# Aktívne sťahovania: { job_id: { status, log, progress } }
jobs = {}
job_counter = 0
jobs_lock = threading.Lock()

HISTORY_FILE = "/app/jobs_history.json"

def _save_history():
    try:
        skip = {"_proc"}
        done = {jid: {k: v for k, v in j.items() if k not in skip}
                for jid, j in jobs.items()
                if j.get("status") in ("done", "error", "cancelled")}
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)
    except Exception:
        pass

def _load_history():
    global job_counter
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        jobs.update(data)
        if data:
            job_counter = max(int(k) for k in data.keys())
    except Exception:
        pass

_load_history()

import unicodedata as _ud

def detect_lang(text):
    def _norm(s):
        return ''.join(c for c in _ud.normalize('NFD', s.lower()) if _ud.category(c) != 'Mn')
    t = re.sub(r'[-_+.]', ' ', _norm(text))
    if re.search(r'\bsk\b.{0,15}da[bv]|\bskdab|\bskdub|slovensky\s+dabing', t): return 'sk-dub'
    if re.search(r'\bcz\b.{0,15}da[bv]|\bczdab|\bczdub|cesky\s+dabing', t): return 'cz-dub'
    if re.search(r'\bsk\b.{0,10}tit|\bsktit|slovenske\s+titulky', t): return 'sk-sub'
    if re.search(r'\bcz\b.{0,10}tit|\bcztit|ceske\s+titulky', t): return 'cz-sub'
    if 'dabing' in t or 'dubbing' in t: return 'dub'
    if 'titulky' in t or 'subtitles' in t: return 'sub'
    return 'unknown'

_LANG_TESTS = [
    ("Reacher.S02E04.2160p.CZdab",              "cz-dub"),
    ("Reacher S02E04 CZ dabing 1080p",           "cz-dub"),
    ("TVD.S01E01.CZ.DAB.mkv",                   "cz-dub"),
    ("Show.S01E01.czdub.BluRay",                 "cz-dub"),
    ("česky dabing S01E01",                      "cz-dub"),
    ("Reacher S02E04 CZtitulky",                 "cz-sub"),
    ("Reacher.S02E04.CzTit.1080p",               "cz-sub"),
    ("Show.S01E01.cztit",                        "cz-sub"),
    ("české titulky S01E01",                     "cz-sub"),
    ("Show.S01E01.SK.DAB.mkv",                   "sk-dub"),
    ("Show S01E01 slovensky dabing",              "sk-dub"),
    ("Show.S01E01.SKdab.1080p",                  "sk-dub"),
    ("Show S01E01 SK titulky",                   "sk-sub"),
    ("Show.S01E01.SKtit.720p",                   "sk-sub"),
]

def _run_lang_tests():
    failed = 0
    for text, expected in _LANG_TESTS:
        got = detect_lang(text)
        if got != expected:
            print(f"[lang-test] FAIL: {text!r} → {got!r} (expected {expected!r})", flush=True)
            failed += 1
    if failed == 0:
        print(f"[lang-test] all {len(_LANG_TESTS)} tests passed", flush=True)
    else:
        print(f"[lang-test] {failed}/{len(_LANG_TESTS)} FAILED", flush=True)

_run_lang_tests()

import queue as _queue_mod
_dl_queue = _queue_mod.Queue()

def _dl_worker():
    while True:
        fn = _dl_queue.get()
        try:
            fn()
        finally:
            _dl_queue.task_done()

for _ in range(3):
    threading.Thread(target=_dl_worker, daemon=True).start()


def sanitize(name: str) -> str:
    """Odstráni znaky nepovolené vo Windows názvoch súborov/priečinkov."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.strip()


def quality_label(height):
    if not height:
        return ""
    if height >= 2160:
        return "4K"
    if height >= 1080:
        return "FullHD"
    if height >= 720:
        return "HD"
    return f"{height}p"


def get_height(url):
    try:
        result = subprocess.run(
            [YTDLP_PATH, "-j", "--no-playlist", url],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace"
        )
        info = json.loads(result.stdout)
        h = info.get("height")
        if not h:
            fmts = info.get("requested_formats", [])
            heights = [f.get("height", 0) for f in fmts if f.get("height")]
            h = max(heights) if heights else None
        return h
    except Exception:
        return None


def build_output_path(media_type, title, year=None, season=None, quality=""):
    title = sanitize(title)
    q = f" [{quality}]" if quality else ""
    if media_type == "film":
        folder_name = f"{title} ({year}){q}" if year else f"{title}{q}"
        return os.path.join(MOVIES_PATH, folder_name)
    else:
        season_folder = f"Season {season}{q}" if season else f"Season 1{q}"
        return os.path.join(SERIES_PATH, title, season_folder)


def run_download(job_id, url, output_path, media_type, title, year, season, episode_num=None, ep_name=""):
    """Enqueue download — worker thread picks it up in FIFO order (max 3 parallel)."""
    def _execute():
        nonlocal url
        with jobs_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["log"] = ["🔍 Zisťujem zdroj..."]

        # Auto-resolve tuu.to episode URLs → voe.sx/netu/dood
        tuu_m = re.match(r'https?://tuu\.to/(?:serialy|tv-shows)/([^/?#]+)/([^/?#]+)', url)
        if tuu_m:
            with jobs_lock:
                jobs[job_id]["log"].append("🔗 Rozbalujem tuu.to URL...")
            lang_pref = "sk-dub" if "sk" in (jobs[job_id].get("lang_pref") or "cz") else "cz-dub"
            resolved, src_info = resolve_tuu_url(tuu_m.group(1), tuu_m.group(2), lang_pref)
            if resolved:
                url = resolved
                audio = src_info.get("audio", "")
                subs = src_info.get("subs", "")
                label = audio + (f" + titulky {subs}" if subs else "")
                with jobs_lock:
                    jobs[job_id]["log"].append(f"✅ Zdroj: {label}")
            else:
                with jobs_lock:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["log"].append("❌ Nepodarilo sa rozbalit tuu.to URL — skúste iný player")
                _save_history()
                return

        with jobs_lock:
            jobs[job_id]["log"].append("🔍 Zisťujem kvalitu...")

        op = output_path
        h = get_height(url)
        ql = quality_label(h)
        if ql:
            op = build_output_path(media_type, title, year, season, ql)
            with jobs_lock:
                jobs[job_id]["output_path"] = op
                jobs[job_id]["log"].append(f"📺 Kvalita: {ql}")

        os.makedirs(op, exist_ok=True)
        safe_title = sanitize(title)
        q_suffix = f" [{ql}]" if ql else ""
        if media_type == "serial" and episode_num:
            try:
                ep_str = f" S{int(season or 1):02d}E{int(episode_num):02d}"
            except Exception:
                ep_str = ""
            ep_name_part = f" – {sanitize(ep_name)}" if ep_name else ""
            output_template = os.path.join(op, f"{safe_title}{ep_str}{ep_name_part}{q_suffix}.%(ext)s")
        else:
            output_template = os.path.join(op, f"{safe_title}{q_suffix}.%(ext)s")

        cmd = [
            YTDLP_PATH,
            "--no-playlist",
            "--external-downloader", "aria2c",
            "--external-downloader-args", "aria2c:-x 16 -s 16 -k 1M --min-split-size 1M",
            "--merge-output-format", "mkv",
            "--output", output_template,
            "--progress",
            url
        ]

        import time as _time
        last_rc = -1
        for attempt in range(3):
            if attempt > 0:
                with jobs_lock:
                    if jobs[job_id]["status"] == "cancelled":
                        break
                    jobs[job_id]["log"].append(f"🔄 Retry {attempt}/2...")
                _time.sleep(5 * attempt)
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace"
                )
                with jobs_lock:
                    if jobs[job_id]["status"] == "cancelled":
                        proc.kill()
                        break
                    jobs[job_id]["_proc"] = proc

                for line in proc.stdout:
                    line = line.rstrip()
                    with jobs_lock:
                        if jobs[job_id]["status"] == "cancelled":
                            break
                        jobs[job_id]["log"].append(line)
                        # drž log max ~400 riadkov, inak žerie RAM a spomaľuje UI
                        if len(jobs[job_id]["log"]) > 400:
                            del jobs[job_id]["log"][:200]
                        m = (re.search(r'\[download\]\s+(\d+(?:\.\d+)?)%', line)
                             or re.search(r'\((\d+(?:\.\d+)?)%\)', line))
                        if m:
                            jobs[job_id]["progress"] = float(m.group(1))
                        # rýchlosť + ETA (aria2c: "DL:9.2MiB ETA:1h4m", yt-dlp: "at 5.2MiB/s ETA 01:23")
                        sp = re.search(r'DL:([\d.]+[KMGT]?i?B)', line)
                        if sp:
                            jobs[job_id]["speed"] = sp.group(1) + "/s"
                        else:
                            sp = re.search(r'at\s+([\d.]+[KMGT]?i?B/s)', line)
                            if sp:
                                jobs[job_id]["speed"] = sp.group(1)
                        et = re.search(r'ETA[:\s]+([\dhms:]+)', line)
                        if et:
                            jobs[job_id]["eta"] = et.group(1)

                proc.wait()
                last_rc = proc.returncode
                with jobs_lock:
                    jobs[job_id]["_proc"] = None
                if last_rc == 0:
                    break
            except FileNotFoundError:
                with jobs_lock:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["log"].append("❌ yt-dlp nenájdený!")
                _save_history()
                return
            except Exception as e:
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                with jobs_lock:
                    jobs[job_id]["_proc"] = None
                    jobs[job_id]["log"].append(f"⚠️ Pokus {attempt+1} zlyhal: {str(e)}")
                last_rc = -1

        with jobs_lock:
            if jobs[job_id]["status"] != "cancelled":
                jobs[job_id]["status"] = "done" if last_rc == 0 else "error"
        _save_history()

    _dl_queue.put(_execute)


def find_episodes(title, season, lang_pref="any"):
    import urllib.request as ureq
    import urllib.parse as uparse
    import unicodedata

    season_n = int(str(season)) if str(season).strip().isdigit() else 1

    SKIP_KW = {'rarbg', 'bluray', 'bdrip', '1337x', 'freedisc', 'torrent',
               'yts', 'eztv', 'yify', 'doqnload', 'mkvcage', 'ettv'}

    def norm(s):
        return ''.join(
            c for c in unicodedata.normalize('NFD', s.lower())
            if unicodedata.category(c) != 'Mn'
        )

    # detect_lang is defined at module level

    def detect_quality(text):
        t = norm(text)
        if '4k' in t or '2160' in t or 'uhd' in t or '8k' in t: return 4
        if '1080' in t or 'fullhd' in t or 'full hd' in t: return 3
        if '720' in t or bool(re.search(r'\bhd\b', t)): return 2
        if '480' in t or '360' in t or '240' in t: return 1
        return 0

    def extract_ep(text):
        m = re.search(r'[Ss]0*(\d+)[Ee]0*(\d+)', text)
        if m: return int(m.group(1)), int(m.group(2))
        m = re.search(r'\b(\d{1,2})[xX](\d{1,3})\b', text)
        if m: return int(m.group(1)), int(m.group(2))
        # CZ/SK formáty: "3. díl", "díl 3", "epizoda 5", "5 dil" (bez čísla série)
        t = norm(text)
        m = re.search(r'\b0*(\d{1,3})\s*\.?\s*(?:dil|cast|epizoda)\b', t)
        if m: return None, int(m.group(1))
        m = re.search(r'\b(?:dil|cast|epizoda|episode)\s*\.?\s*0*(\d{1,3})\b', t)
        if m: return None, int(m.group(1))
        return None, None

    def strip_tags(s):
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s)).strip()

    def fetch(url):
        req = ureq.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "cs,sk;q=0.9,en;q=0.8",
        })
        with ureq.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")

    def ddg_search(query):
        """
        DuckDuckGo HTML search. Returns list of (url, display_title).
        Episode info is read from display title (not URL), so ID-based URLs work too.
        """
        try:
            html = fetch(f"https://html.duckduckgo.com/html/?q={uparse.quote('site:prehrajto.cz ' + query)}")
        except Exception as e:
            print(f"[search] DDG zlyhal: {e}", flush=True)
            return []

        pairs = []
        # Each DDG result has href="//duckduckgo.com/l/?uddg=ENCODED_URL..."
        # followed (within ~600 chars) by the display title in result__a anchor.
        for m in re.finditer(r'uddg=([^"&\s]+)', html):
            url = uparse.unquote(m.group(1))
            if 'prehrajto.cz' not in url:
                continue
            # skip torrent aggregators
            if any(kw in norm(url) for kw in SKIP_KW):
                continue
            # grab display title from the next result__a closing within 600 chars
            after = html[m.end():m.end() + 600]
            t_m = re.search(r'>(.*?)</a>', after, re.DOTALL | re.IGNORECASE)
            display = strip_tags(t_m.group(1)) if t_m else ''
            if not display:
                display = url.rstrip('/').split('/')[-1].replace('-', ' ')
            pairs.append((url, display))
        if not pairs and ('captcha' in html.lower() or 'anomaly' in html.lower()):
            print("[search] DDG vrátil CAPTCHA — výsledky dočasne nedostupné", flush=True)
        return pairs

    def direct_search(query):
        """
        prehrajto.cz is SSR — results are in the initial HTML, paginated via ?page=N.
        URL: /hledej/{query}  (path-based)
        Video cards: <a class="...video--link..." href="/{slug}/{hash}" title="filename.mkv">
        The `title` attribute IS the filename and contains episode + language info.
        """
        all_pairs = []
        seen_hrefs = set()
        q_enc = uparse.quote(query)
        for base_url in [
            f"https://prehrajto.cz/hledej/{q_enc}",
            f"https://prehrajto.cz/hledat/{q_enc}",
            f"https://prehrajto.cz/hledej?q={q_enc}",
        ]:
            try:
                for page in range(1, 6):  # try up to 5 pages
                    sep = '&' if '?' in base_url else '?'
                    page_url = base_url if page == 1 else f"{base_url}{sep}page={page}"
                    try:
                        html = fetch(page_url)
                    except Exception:
                        break
                    new_on_page = 0
                    for m in re.finditer(
                        r'<a\b[^>]{0,600}?video--link[^>]{0,600}?>',
                        html, re.IGNORECASE | re.DOTALL
                    ):
                        tag = m.group(0)
                        href_m  = re.search(r'href="(/[^"]+)"', tag)
                        title_m = re.search(r'title="([^"]+)"', tag)
                        if not href_m or not title_m:
                            continue
                        href = 'https://prehrajto.cz' + href_m.group(1)
                        if href in seen_hrefs:
                            continue
                        seen_hrefs.add(href)
                        new_on_page += 1
                        file_name = title_m.group(1)
                        label = re.sub(r'\.(mkv|mp4|avi|mov)$', '', file_name, flags=re.IGNORECASE)
                        label = re.sub(r'[-_+]', ' ', label).strip()
                        after = html[m.end():m.end() + 2000]
                        q_m = re.search(r'class="format__text">([^<]+)<', after)
                        if q_m:
                            label += ' ' + q_m.group(1).strip()
                        all_pairs.append((href, label))
                    if new_on_page == 0:
                        break  # no new results — either last page or ?page= ignored
                if all_pairs:
                    break
            except Exception:
                continue
        return all_pairs

    # CZ translation — len ak názov vyzerá anglicky (diakritika = už je CZ/SK)
    cz_title = None
    if norm(title) == title.lower():
        try:
            with ureq.urlopen(
                f"https://api.mymemory.translated.net/get?q={uparse.quote(title)}&langpair=en|cs",
                timeout=5
            ) as r:
                cz_title = json.loads(r.read()).get("responseData", {}).get("translatedText", "")
            if cz_title and norm(cz_title) == norm(title):
                cz_title = None
        except Exception:
            pass

    lang_order = {
        'cz':  ['cz-dub', 'cz-sub', 'sk-dub', 'sk-sub', 'dub', 'sub', 'unknown'],
        'sk':  ['sk-dub', 'sk-sub', 'cz-dub', 'cz-sub', 'dub', 'sub', 'unknown'],
        'en':  ['unknown', 'cz-dub', 'sk-dub', 'cz-sub', 'sk-sub', 'dub', 'sub'],
        'any': ['cz-dub', 'sk-dub', 'cz-sub', 'sk-sub', 'dub', 'sub', 'unknown'],
    }
    order = lang_order.get(lang_pref, lang_order['any'])

    def score(r):
        try: lr = order.index(r['lang'])
        except ValueError: lr = len(order)
        return (lr, -detect_quality(r['title'] + ' ' + r['url']))

    seen = set()
    results = []

    # TMDB — get episode count + names up front (used to cap targeted search)
    tmdb_ep_count = None
    tmdb_ep_names = {}
    try:
        sr = ureq.urlopen(
            f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={uparse.quote(title)}&language=cs",
            timeout=5
        )
        series_id = (json.loads(sr.read()).get("results") or [{}])[0].get("id")
        if series_id:
            er = ureq.urlopen(
                f"https://api.themoviedb.org/3/tv/{series_id}/season/{season_n}?api_key={TMDB_API_KEY}&language=cs",
                timeout=5
            )
            eps_data = json.loads(er.read()).get("episodes", [])
            tmdb_ep_count = len(eps_data)
            for ep in eps_data:
                ep_n = ep.get("episode_number")
                name = ep.get("name") or ""
                if ep_n and name:
                    tmdb_ep_names[ep_n] = name
    except Exception:
        pass

    def process_pairs(pairs):
        for url, display in pairs:
            if url in seen:
                continue
            slug = url.rstrip('/').split('/')[-1]
            s_num, ep_num = extract_ep(display)
            if s_num is None:
                s_num, ep_num = extract_ep(slug)
            if s_num is not None and s_num != season_n:
                continue
            if ep_num is None:
                continue
            # cap at known season length
            if tmdb_ep_count and ep_num > tmdb_ep_count:
                continue
            lang = detect_lang(display + ' ' + slug)
            seen.add(url)
            results.append({
                "url": url,
                "title": display[:120],
                "episode": ep_num,
                "season": s_num or season_n,
                "lang": lang,
            })

    # 1. Broad search (EN title) — direct_search first, DDG fallback
    broad_query = f'{title} S{season_n:02d}'
    pairs = direct_search(broad_query)
    if not pairs:
        pairs = ddg_search(broad_query)
    print(f"[search] broad EN raw={len(pairs)}", flush=True)
    process_pairs(pairs)

    # 2. Targeted searches for gaps within known season length
    found_eps = set(r['episode'] for r in results)
    search_limit = tmdb_ep_count if tmdb_ep_count else (max(found_eps) + 1 if found_eps else 0)
    for ep_n in range(1, search_limit + 1):
        if ep_n not in found_eps:
            q = f'{title} S{season_n:02d}E{ep_n:02d}'
            targeted = direct_search(q)
            if targeted:
                print(f"[search] targeted E{ep_n:02d} found {len(targeted)}", flush=True)
                process_pairs(targeted)
                found_eps = set(r['episode'] for r in results)

    print(f"[search] final episodes: {sorted(set(r['episode'] for r in results))}", flush=True)

    # Group by episode, pick best version
    ep_groups = {}
    for r in results:
        ep = r.get('episode')
        if ep:
            ep_groups.setdefault(ep, []).append(r)

    best = []
    for ep_num in sorted(ep_groups):
        versions = ep_groups[ep_num]
        picked = min(versions, key=score)
        if len(versions) > 1:
            picked['alt_count'] = len(versions) - 1
        best.append(picked)

    best.sort(key=lambda x: (x.get('episode') or 999))

    # Apply TMDB episode names
    for b in best:
        ep_n = b.get("episode")
        if ep_n and ep_n in tmdb_ep_names:
            b["ep_name"] = tmdb_ep_names[ep_n]

    return best


def resolve_tuu_url(show_slug, ep_slug, lang_pref="sk-dub"):
    """
    Convert tuu.to show+episode slugs → direct voe.sx/netu/dood URL.
    Chain: tuu.to API → base64 protect_link → govoyra GET → govoyra POST → player data-href → base64 → URL
    Returns (video_url, source_info_dict) or (None, None).
    """
    import urllib.request as _ureq, urllib.parse as _uparse, base64 as _b64

    _hdr = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://tuu.to/",
        "Accept": "*/*",
    }

    def _fetch(url, post_data=None, extra=None):
        h = dict(_hdr)
        if extra:
            h.update(extra)
        req = _ureq.Request(url, data=post_data, headers=h)
        return _ureq.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")

    def _pick_source(sources, pref):
        # pref: "sk-dub" | "cz-dub" | "any"
        for s in sources:
            pt = s.get("pretty_type", {})
            if pref == "sk-dub" and pt.get("has_dub") and pt.get("audio", "").upper() in ("SK", "SL"):
                return s
        for s in sources:
            pt = s.get("pretty_type", {})
            if pref == "cz-dub" and pt.get("has_dub") and pt.get("audio", "").upper() in ("CZ", "CS", "ČJ"):
                return s
        # fallback: any dub, then first
        for s in sources:
            if s.get("pretty_type", {}).get("has_dub"):
                return s
        return sources[0] if sources else None

    def _b64d(s):
        return _b64.b64decode(s + "=" * ((4 - len(s) % 4) % 4)).decode("utf-8", errors="replace")

    for player in ["voe", "netu", "dood"]:
        try:
            # 1. tuu.to API → sources with protect_link
            api_url = f"https://api.tuu.to/api/v1/tv-shows/{show_slug}/episode/{ep_slug}?player={player}"
            print(f"[tuu] step1 GET {api_url}", flush=True)
            raw = _fetch(api_url)
            print(f"[tuu] step1 response len={len(raw)} snippet={raw[:120]}", flush=True)
            data = json.loads(raw)
            sources = data.get("sources", [])
            if not sources:
                print(f"[tuu] {player}: no sources. keys={list(data.keys())}", flush=True)
                continue

            src = _pick_source(sources, lang_pref)
            if not src:
                print(f"[tuu] {player}: no matching source", flush=True)
                continue
            protect_link = src.get("protect_link", "")
            if not protect_link:
                print(f"[tuu] {player}: empty protect_link", flush=True)
                continue

            # 2. base64 decode → https://watch.govoyra.com/?data=HASH
            govoyra_get_url = _b64d(protect_link)
            print(f"[tuu] step2 govoyra GET: {govoyra_get_url[:100]}", flush=True)

            # 3. GET govoyra → HTML form (auto-submits to POST)
            html = _fetch(govoyra_get_url)
            print(f"[tuu] step3 govoyra GET response len={len(html)} snippet={html[:200]}", flush=True)
            # form: <input type="hidden" name="data" value="..."> (attr order may vary)
            fm = re.search(r'name=["\']data["\']\s+value=["\']([^"\']+)["\']', html)
            if not fm:
                fm = re.search(r'value=["\']([^"\']{20,})["\'][^>]*name=["\']data["\']', html)
            if not fm:
                print(f"[tuu] {player}: no form data field in govoyra response", flush=True)
                continue

            # 4. POST to govoyra → player HTML with change_player links
            post_body = _uparse.urlencode({"data": fm.group(1)}).encode()
            print(f"[tuu] step4 POST govoyra data len={len(fm.group(1))}", flush=True)
            html2 = _fetch(
                "https://watch.govoyra.com/",
                post_data=post_body,
                extra={"Content-Type": "application/x-www-form-urlencoded"}
            )
            print(f"[tuu] step4 POST response len={len(html2)} snippet={html2[:300]}", flush=True)

            # 5. Parse data-href from change_player link (base64 → actual URL)
            # Try both attribute orders
            pm = re.search(r'data-href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*change_player[^"\']*' + re.escape(player), html2)
            if not pm:
                pm = re.search(r'class=["\'][^"\']*change_player[^"\']*' + re.escape(player) + r'[^"\']*["\'][^>]*data-href=["\']([^"\']+)["\']', html2)
            if pm:
                video_url = _b64d(pm.group(1)).rstrip("?").rstrip("&")
                print(f"[tuu] resolved → {video_url}", flush=True)
                return video_url, src.get("pretty_type", {})

            print(f"[tuu] {player}: change_player link not found. Searching all data-href...", flush=True)
            all_hrefs = re.findall(r'data-href=["\']([^"\']+)["\']', html2)
            print(f"[tuu] all data-href values: {all_hrefs}", flush=True)
        except Exception as e:
            import traceback
            print(f"[tuu] {player} error: {e}\n{traceback.format_exc()}", flush=True)

    return None, None


def tuu_episode_list(show_slug):
    """
    Fetch episode list for a show from tuu.to API.
    Returns list of {season, episode, slug, name, tuu_url} or [].
    """
    import urllib.request as _ureq
    _hdr = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://tuu.to/",
        "Accept": "application/json",
    }

    def _get(url):
        req = _ureq.Request(url, headers=_hdr)
        raw = _ureq.urlopen(req, timeout=12).read()
        parsed = json.loads(raw)
        d = parsed.get("data", parsed)
        keys = list(d.keys()) if isinstance(d, dict) else f"list[{len(d)}]"
        print(f"[tuu] GET {url} → {len(raw)}B, keys={keys}", flush=True)
        return parsed

    def _make_ep(s_num, e_num, name, show_slug):
        slug = f"s{s_num:02d}e{e_num:02d}"
        return {
            "season": s_num, "episode": e_num, "slug": slug,
            "name": name or f"Epizóda {e_num}",
            "tuu_url": f"https://tuu.to/tv-shows/{show_slug}/{slug}",
        }

    episodes = []

    try:
        # Endpoint 1: show detail — 41KB response should have seasons inside
        data = _get(f"https://api.tuu.to/api/v1/tv-shows/{show_slug}")
        show_data = data.get("data", data)
        print(f"[tuu] show_data keys: {list(show_data.keys())}", flush=True)

        for season_obj in show_data.get("seasons", []):
            s_num = int(season_obj.get("season_number") or season_obj.get("number") or 1)
            ep_list = season_obj.get("episodes", [])
            print(f"[tuu] season {s_num}: {len(ep_list)} episodes", flush=True)
            if ep_list:
                print(f"[tuu] first ep keys: {list(ep_list[0].keys())}", flush=True)
            for ep in ep_list:
                e_num = int(ep.get("episode_number") or ep.get("episode") or ep.get("ep_num") or ep.get("number") or ep.get("position") or ep.get("order") or 0)
                if not e_num:
                    continue
                episodes.append(_make_ep(s_num, e_num, ep.get("name") or ep.get("title"), show_slug))

        if not episodes:
            for ep in show_data.get("episodes", []):
                s_num = int(ep.get("season_number") or ep.get("season") or 1)
                e_num = int(ep.get("episode_number") or ep.get("episode") or ep.get("number") or 0)
                if e_num:
                    episodes.append(_make_ep(s_num, e_num, ep.get("name") or ep.get("title"), show_slug))
    except Exception as e:
        print(f"[tuu] show endpoint error: {e}", flush=True)

    # Endpoint 2: /tv-shows/{slug}/season/{n}
    if not episodes:
        for s_num in range(1, 5):
            try:
                data2 = _get(f"https://api.tuu.to/api/v1/tv-shows/{show_slug}/season/{s_num}")
                d2 = data2.get("data", data2)
                ep_list2 = d2.get("episodes", []) if isinstance(d2, dict) else (d2 if isinstance(d2, list) else [])
                print(f"[tuu] season/{s_num}: {len(ep_list2)} episodes", flush=True)
                if not ep_list2:
                    break
                for ep in ep_list2:
                    e_num = int(ep.get("episode_number") or ep.get("number") or 0)
                    if e_num:
                        episodes.append(_make_ep(s_num, e_num, ep.get("name") or ep.get("title"), show_slug))
            except Exception as e2:
                print(f"[tuu] season/{s_num} error: {e2}", flush=True)
                break

    print(f"[tuu] episode_list found {len(episodes)} episodes", flush=True)
    return episodes


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # ticho

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        with open(path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_file(os.path.join(os.path.dirname(__file__), "index.html"))

        elif path == "/status":
            qs = parse_qs(parsed.query)
            job_id = qs.get("id", [None])[0]
            if job_id and job_id in jobs:
                with jobs_lock:
                    safe = {k: v for k, v in jobs[job_id].items() if k != "_proc"}
                self.send_json(safe)
            else:
                self.send_json({"error": "Job nenájdený"}, 404)

        elif path == "/fetch-title":
            qs = parse_qs(parsed.query)
            url = qs.get("url", [None])[0]
            if not url:
                self.send_json({"error": "No URL"}, 400)
                return
            try:
                import urllib.request, urllib.parse as uparse
                # 1. stiahni stránku
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    html = r.read().decode("utf-8", errors="replace")
                # 2. vyber surový title tag
                title_m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
                raw = title_m.group(1).strip() if title_m else ""
                # 3. odrež site suffix: "- online ke zhlédnutí", "- Přehraj.to", "~ ..."
                raw = re.split(r'\s*-\s*online\b', raw, flags=re.IGNORECASE)[0].strip()
                raw = re.split(r'\s*[~|–—]\s*', raw)[0].strip()
                # 4. odrež záverečné "Cz dabing", "SK dabing" a podobne
                raw = re.sub(r'\s+(cz|sk|en)\s+dabing.*$', '', raw, flags=re.IGNORECASE).strip()
                # 5. odstraň zátvork y s hercami/žánrami (obsahujú čiarku alebo kvalitu)
                raw = re.sub(r'\([^)]*,[^)]*\)', '', raw).strip()
                raw = re.sub(r'\([^)]*(?:p\b|bdrip|bluray|hd|dvd)[^)]*\)', '', raw, flags=re.IGNORECASE).strip()
                # 6. vytiahni rok
                year = ""
                year_m = re.search(r'\((\d{4})\)', raw) or re.search(r'[_\-](\d{4})[_\-]', url)
                if year_m:
                    year = year_m.group(1)
                    raw = re.sub(r'\s*\(\d{4}\)', '', raw).strip()
                # ak rok nie je v title, skús z URL slugu
                if not year:
                    y = re.search(r'[-/](\d{4})[-/]', url)
                    if y: year = y.group(1)
                # 5. TMDB + Wikidata lookup pre anglický názov
                title = raw

                def tmdb_find(query, yr):
                    q = uparse.quote(query)
                    y = f"&year={yr}" if yr else ""
                    with urllib.request.urlopen(
                        f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={q}{y}&language=en-US",
                        timeout=8
                    ) as r:
                        results = json.loads(r.read()).get("results", [])
                    if yr and results:
                        results = [x for x in results if x.get("release_date", "").startswith(yr)] or results
                    return results[0] if results else None

                def wikidata_find(query, yr):
                    for lang in ["sk", "cs"]:
                        q = uparse.quote(query)
                        with urllib.request.urlopen(
                            f"https://www.wikidata.org/w/api.php?action=wbsearchentities&search={q}&language={lang}&type=item&format=json&limit=10",
                            timeout=8
                        ) as r:
                            items = json.loads(r.read()).get("search", [])
                        for item in items:
                            desc = item.get("description", "").lower()
                            if not any(w in desc for w in ("film", "movie", "series", "television", "miniseries")):
                                continue
                            if yr and yr not in desc:
                                continue
                            qid = item["id"]
                            with urllib.request.urlopen(
                                f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&props=labels&languages=en&format=json",
                                timeout=8
                            ) as r:
                                en = json.loads(r.read()).get("entities", {}).get(qid, {}).get("labels", {}).get("en", {}).get("value")
                            if en:
                                return en
                        # druhý pokus bez roku
                        if yr:
                            for item in items:
                                desc = item.get("description", "").lower()
                                if not any(w in desc for w in ("film", "movie", "series", "television", "miniseries")):
                                    continue
                                qid = item["id"]
                                with urllib.request.urlopen(
                                    f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&props=labels&languages=en&format=json",
                                    timeout=8
                                ) as r:
                                    en = json.loads(r.read()).get("entities", {}).get(qid, {}).get("labels", {}).get("en", {}).get("value")
                                if en:
                                    return en
                    return None

                def translate_to_en(text):
                    q = uparse.quote(text)
                    with urllib.request.urlopen(
                        f"https://api.mymemory.translated.net/get?q={q}&langpair=sk|en",
                        timeout=6
                    ) as r:
                        data = json.loads(r.read())
                    t = data.get("responseData", {}).get("translatedText", "")
                    return t if t and t.lower() != text.lower() else None

                try:
                    # 1. Wikidata (presný SK/CZ label)
                    en = wikidata_find(raw, year)
                    # ignoruj ak vrátilo to isté (neprelozilo)
                    if en and en.lower().strip() != raw.lower().strip():
                        title = en
                    else:
                        # 2. Preložiť SK→EN a hľadať na TMDB
                        translated = None
                        try:
                            translated = translate_to_en(raw)
                        except Exception:
                            pass

                        search_query = translated or raw
                        if TMDB_API_KEY:
                            hit = tmdb_find(search_query, year)
                            if not hit and translated:
                                hit = tmdb_find(raw, year)
                            if hit:
                                title = hit.get("title") or raw
                                if not year:
                                    year = (hit.get("release_date") or "")[:4]
                        elif translated:
                            title = translated
                except Exception:
                    pass
                self.send_json({"title": title, "year": year})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/tmdb-search":
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0]
            kind  = qs.get("type", ["film"])[0]
            if not query or not TMDB_API_KEY:
                self.send_json({"results": []}); return
            try:
                import urllib.request, urllib.parse as uparse
                endpoint = "movie" if kind == "film" else "tv"
                q = uparse.quote(query)
                url2 = f"https://api.themoviedb.org/3/search/{endpoint}?api_key={TMDB_API_KEY}&query={q}&language=en-US&page=1"
                with urllib.request.urlopen(url2, timeout=8) as r:
                    data = json.loads(r.read())
                results = []
                for item in data.get("results", [])[:8]:
                    title = item.get("title") or item.get("name") or ""
                    date  = item.get("release_date") or item.get("first_air_date") or ""
                    year  = date[:4] if date else ""
                    poster = item.get("poster_path")
                    poster_url = f"https://image.tmdb.org/t/p/w92{poster}" if poster else ""
                    results.append({"title": title, "year": year, "poster": poster_url})
                self.send_json({"results": results})
            except Exception as e:
                self.send_json({"results": [], "error": str(e)})

        elif path == "/tuu-episodes":
            qs = parse_qs(parsed.query)
            show_slug = qs.get("show", [""])[0].strip()
            if not show_slug:
                self.send_json({"error": "No show slug"}, 400)
                return
            # Allow full URL: https://tuu.to/serialy/sen-cal-kapimi
            m_slug = re.search(r'tuu\.to/serialy/([^/?#]+)', show_slug)
            if m_slug:
                show_slug = m_slug.group(1)
            episodes = tuu_episode_list(show_slug)
            self.send_json({"episodes": episodes, "show_slug": show_slug})

        elif path == "/search-episodes":
            qs = parse_qs(parsed.query)
            ep_title  = qs.get("title",  [""])[0]
            ep_season = qs.get("season", ["1"])[0]
            ep_lang   = qs.get("lang",   ["any"])[0]
            if not ep_title:
                self.send_json({"error": "No title"}, 400)
                return
            try:
                episodes = find_episodes(ep_title, ep_season, ep_lang)
                self.send_json({"episodes": episodes})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/check-lang":
            qs = parse_qs(parsed.query)
            url = qs.get("url", [""])[0]
            if not url:
                self.send_json({"error": "No URL"}, 400)
                return
            lang_map = {
                'cs': 'cz-dub', 'cze': 'cz-dub', 'ces': 'cz-dub',
                'sk': 'sk-dub', 'slk': 'sk-dub', 'slo': 'sk-dub',
                'en': 'unknown', 'eng': 'unknown',
            }
            try:
                # 1. Try audio stream metadata first (fast)
                result = subprocess.run(
                    [YTDLP_PATH, "-j", "--no-playlist", url],
                    capture_output=True, text=True, timeout=30,
                    encoding="utf-8", errors="replace"
                )
                info = json.loads(result.stdout.strip().split('\n')[0])
                lang_code = ""
                for fmt in info.get("formats", []):
                    if fmt.get("acodec") and fmt.get("acodec") != "none":
                        lc = fmt.get("language") or ""
                        if lc:
                            lang_code = lc.lower()
                            break
                if not lang_code:
                    lang_code = (info.get("language") or "").lower()

                lang = lang_map.get(lang_code[:3] if lang_code else '', 'unknown')

                # 2. If metadata gave no useful result, detect from audio via Whisper
                method = "metadata"
                if lang == 'unknown':
                    method = "whisper"
                    whisper_code = detect_audio_language(url)
                    if whisper_code:
                        lang = lang_map.get(whisper_code[:3], 'unknown')
                self.send_json({"lang": lang, "raw": lang_code, "method": method})
            except Exception as e:
                print(f"[check-lang] exception: {e}", flush=True)
                self.send_json({"lang": "unknown", "method": "metadata", "error": str(e)})

        elif path == "/restart":
            self.send_json({"ok": True})
            threading.Thread(target=lambda: (__import__('time').sleep(0.3), __import__('os')._exit(0)), daemon=True).start()

        elif path == "/config":
            self.send_json({
                "movies_path": MOVIES_PATH,
                "series_path": SERIES_PATH,
                "version": VERSION
            })

        elif path == "/check-update":
            try:
                import urllib.request as _ureq2
                raw = _ureq2.urlopen(GITHUB_RAW, timeout=5).read().decode("utf-8", errors="replace")
                m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', raw, re.MULTILINE)
                latest = m.group(1) if m else VERSION
                self.send_json({"current": VERSION, "latest": latest, "has_update": latest != VERSION})
            except Exception:
                self.send_json({"current": VERSION, "latest": VERSION, "has_update": False})

        elif path == "/jobs":
            with jobs_lock:
                summary = {
                    jid: {
                        "status": j["status"],
                        "title": j.get("title", ""),
                        "series_key": j.get("series_key", j.get("title", "")),
                        "progress": j.get("progress", 0),
                        "speed": j.get("speed", ""),
                        "eta": j.get("eta", ""),
                        "output_path": j.get("output_path", "")
                    }
                    for jid, j in jobs.items()
                }
            self.send_json(summary)

        elif path == "/clear-history":
            qs = parse_qs(parsed.query)
            key = qs.get("key", [""])[0]
            with jobs_lock:
                to_del = [jid for jid, j in jobs.items()
                          if j.get("status") in ("done", "error", "cancelled")
                          and (not key or j.get("series_key") == key)]
                for jid in to_del:
                    del jobs[jid]
            _save_history()
            self.send_json({"ok": True, "deleted": len(to_del)})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global job_counter
        parsed = urlparse(self.path)

        if parsed.path == "/download":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except Exception:
                self.send_json({"error": "Neplatný JSON"}, 400)
                return

            url        = data.get("url", "").strip()
            media_type = data.get("type", "film")   # "film" | "serial"
            title      = data.get("title", "Neznámy").strip()
            year       = data.get("year", "").strip()
            season     = data.get("season", "1").strip()
            episode_num = data.get("episode_num", None)
            lang_pref  = data.get("lang_pref", "cz-dub").strip()  # for tuu.to resolving

            if not url:
                self.send_json({"error": "URL je prázdna"}, 400)
                return

            # Skip if already downloaded (check history + filesystem)
            if media_type == "serial" and episode_num:
                try:
                    s = int(season or 1)
                    e = int(episode_num)
                    ep_key = f"S{s:02d}E{e:02d}"
                    sk = f"{title} S{s:02d}"
                    already = False
                    with jobs_lock:
                        already = any(
                            j.get("status") == "done" and
                            j.get("series_key") == sk and
                            ep_key in j.get("title", "")
                            for j in jobs.values()
                        )
                    if not already:
                        import glob as _glob
                        pat = os.path.join(SERIES_PATH, sanitize(title), "**", f"*{ep_key}*")
                        already = bool(_glob.glob(pat, recursive=True))
                    if already:
                        self.send_json({"skipped": True})
                        return
                except Exception:
                    pass

            output_path = build_output_path(media_type, title, year, season)

            ep_name = data.get("ep_name", "").strip()
            display_title = title
            if media_type == "serial" and episode_num:
                try:
                    ep_str = f"S{int(season or 1):02d}E{int(episode_num):02d}"
                    display_title = f"{ep_str} – {ep_name}" if ep_name else ep_str
                except Exception:
                    pass

            try:
                series_key = f"{title} S{int(season or 1):02d}" if media_type == "serial" else title
            except Exception:
                series_key = title

            with jobs_lock:
                job_counter += 1
                job_id = str(job_counter)
                jobs[job_id] = {
                    "status": "queued",
                    "title": display_title,
                    "series_key": series_key,
                    "progress": 0,
                    "log": ["⏳ Čakám vo fronte..."],
                    "output_path": output_path,
                    "lang_pref": lang_pref
                }

            run_download(job_id, url, output_path, media_type, title, year, season, episode_num, ep_name)
            self.send_json({"job_id": job_id, "output_path": output_path})

        elif parsed.path == "/cancel":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            job_id = data.get("job_id", "")
            with jobs_lock:
                job = jobs.get(job_id)
            if job:
                proc = job.get("_proc")
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                with jobs_lock:
                    jobs[job_id]["status"] = "cancelled"
                    jobs[job_id]["log"].append("⛔ Stiahnutie zrušené")
                    jobs[job_id]["_proc"] = None
            self.send_json({"ok": True})

        elif parsed.path == "/delete-job":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            job_id = data.get("job_id", "")
            with jobs_lock:
                job = jobs.pop(job_id, None)
            if job:
                proc = job.get("_proc")
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self.send_json({"ok": True})

        elif parsed.path == "/dsm-run":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
            except Exception:
                self.send_json({"error": "bad json"}, 400)
                return
            import urllib.request, urllib.parse
            dsm_host = f"https://{data['host']}:{data['port']}"
            ctx = __import__('ssl').create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = __import__('ssl').CERT_NONE
            try:
                login_url = f"{dsm_host}/webapi/auth.cgi?api=SYNO.API.Auth&version=3&method=login&account={urllib.parse.quote(data['user'])}&passwd={urllib.parse.quote(data['pass'])}&session=TaskScheduler&format=sid"
                with urllib.request.urlopen(login_url, context=ctx, timeout=10) as r:
                    login_data = json.loads(r.read())
                if not login_data.get("success"):
                    self.send_json({"error": "login_failed"}); return
                sid = login_data["data"]["sid"]
                list_url = f"{dsm_host}/webapi/entry.cgi?api=SYNO.Core.TaskScheduler&version=3&method=list&_sid={sid}"
                with urllib.request.urlopen(list_url, context=ctx, timeout=10) as r:
                    list_data = json.loads(r.read())
                task = next((t for t in list_data.get("data", {}).get("tasks", []) if t["name"] == data.get("task", "Rebuild nas-downloader")), None)
                if not task:
                    self.send_json({"error": "task_not_found"}); return
                run_ok = False
                run_debug = {}
                for ver in ["7", "3", "1"]:
                    run_payload = urllib.parse.urlencode({"api": "SYNO.Core.TaskScheduler", "version": ver, "method": "run", "id": task["id"], "_sid": sid}).encode()
                    req = urllib.request.Request(f"{dsm_host}/webapi/entry.cgi", data=run_payload)
                    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                        run_data = json.loads(r.read())
                    run_debug = run_data
                    if run_data.get("success"):
                        run_ok = True
                        break
                urllib.request.urlopen(f"{dsm_host}/webapi/auth.cgi?api=SYNO.API.Auth&version=1&method=logout&_sid={sid}", context=ctx, timeout=5)
                self.send_json({"success": run_ok, "debug": run_debug})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif parsed.path == "/do-update":
            def _run_update():
                import urllib.request as _ureq3, zipfile, io, shutil
                SKIP = {".env", "jobs_history.json"}
                try:
                    print("[update] Downloading ZIP from GitHub...", flush=True)
                    data = _ureq3.urlopen(GITHUB_ZIP, timeout=60).read()
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        for member in z.namelist():
                            parts = member.split("/", 1)
                            if len(parts) < 2 or not parts[1]:
                                continue
                            rel = parts[1]
                            if rel in SKIP:
                                continue
                            dest = os.path.join("/app", rel)
                            if member.endswith("/"):
                                os.makedirs(dest, exist_ok=True)
                            else:
                                os.makedirs(os.path.dirname(dest), exist_ok=True)
                                with z.open(member) as src, open(dest, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                    print("[update] Done — watchdog reštartuje server.", flush=True)
                except Exception as e:
                    print(f"[update] Chyba: {e}", flush=True)
            self.send_json({"ok": True})
            threading.Thread(target=_run_update, daemon=True).start()

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    port = 8080
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"✅ NAS Downloader beží na http://localhost:{port}")
    print(f"   Filmy  → {MOVIES_PATH}")
    print(f"   Seriály→ {SERIES_PATH}")
    print(f"   Stlač CTRL+C pre zastavenie\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⛔ Server zastavený.")
