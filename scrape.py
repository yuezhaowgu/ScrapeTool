from playwright.sync_api import sync_playwright
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import json
from collections import Counter

# ============================================================
# CONFIGURATION
# ============================================================

COURSE_OUTLINE_URL = "https://studio.learn.authoring.goacademy.wgu.edu/course/course-v1:Academy+C963A+101"
OUTPUT_FILE = "course_summary.json"
AUTH_FILE = "auth.json"

WORDS_PER_MINUTE = 250
MINUTES_PER_INTERACTIVE = 3

NUM_WORKERS = 5
HEADLESS = True

# Base URL for building per-vertical page links from locators
STUDIO_BASE = "https://studio.learn.authoring.goacademy.wgu.edu"

# Thread-safe printing so parallel workers don't interleave output
_print_lock = threading.Lock()
def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs, flush=True)

# ============================================================
# TIME CALCULATOR (unchanged)
# ============================================================

def estimate_minutes(html_words, video_seconds, interactive_count):
    return (html_words / WORDS_PER_MINUTE
            + video_seconds / 60
            + interactive_count * MINUTES_PER_INTERACTIVE)

def recalculate_estimates(data):
    for p in data["pages"]:
        p["estimated_minutes"] = round(estimate_minutes(
            p["html_word_count"], p["video_duration_seconds"], p["interactive_count"]
        ))
    t = data["course_totals"]
    t["video_duration_seconds"] = sum(p["video_duration_seconds"] for p in data["pages"])
    t["video_duration_minutes"] = round(t["video_duration_seconds"] / 60, 2)
    t["video_duration_hours"] = round(t["video_duration_seconds"] / 3600, 2)
    course_min = estimate_minutes(t["html_word_count"], t["video_duration_seconds"], t["interactive_count"])
    t["estimated_minutes"] = round(course_min)
    t["estimated_hours"] = round(course_min / 60, 2)

def apply_resolved_videos(data, resolved):
    extra_by_page = {}
    for r in resolved:
        extra_by_page[r["page"]] = extra_by_page.get(r["page"], 0) + r["duration_seconds"]
    for page_entry in data["pages"]:
        extra = extra_by_page.get(page_entry["page"], 0)
        if extra:
            page_entry["video_duration_seconds"] += extra
            page_entry["video_duration_minutes"] = round(page_entry["video_duration_seconds"] / 60, 2)
    data.setdefault("resolved_video_durations", []).extend(resolved)

# ============================================================
# JS PAYLOADS
# ============================================================

# Same content analysis as before
ANALYZE_BLOCKS_JS = r"""
() => {
    function countWords(text) {
        return (text.match(/\b\w+\b/g) || []).length;
    }
    function cleanClone(block) {
        const clone = block.cloneNode(true);
        clone.querySelectorAll(
            '.add-xblock-component, .xblock-header, .wrapper-xblock-actions, .ui-loading'
        ).forEach(el => el.remove());
        return clone;
    }
    function extractVideoFromUrl(url) {
        if (!url) return null;
        let m = url.match(/(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
        if (m) return { source: 'youtube', video_id: m[1], embed_url: url };
        m = url.match(/(?:player\.)?vimeo\.com\/(?:video\/)?(\d+)/);
        if (m) return { source: 'vimeo', video_id: m[1], embed_url: url };
        return null;
    }
    function isVideoUrl(url) { return extractVideoFromUrl(url) !== null; }
    function getVideos(block) {
        const videos = [];
        const seen = new Set();
        function addVideo(v) {
            const key = `${v.source}:${v.video_id || v.embed_url}`;
            if (seen.has(key)) return;
            seen.add(key);
            videos.push(v);
        }
        const videoEl = block.querySelector('video');
        if (videoEl && !isNaN(videoEl.duration) && videoEl.duration > 0) {
            addVideo({ source: 'html5', duration_seconds: Math.round(videoEl.duration) });
        }
        const dataEl = block.querySelector('[data-metadata]');
        if (dataEl) {
            try {
                const meta = JSON.parse(dataEl.getAttribute('data-metadata'));
                if (meta && meta.duration) addVideo({ source: 'metadata', duration_seconds: meta.duration });
            } catch(e) {}
        }
        block.querySelectorAll('iframe').forEach(iframe => {
            const v = extractVideoFromUrl(iframe.src);
            if (v) addVideo(v);
        });
        block.querySelectorAll('a[href]').forEach(a => {
            const v = extractVideoFromUrl(a.href);
            if (v) addVideo(v);
        });
        return videos;
    }
    function getNonVideoIframes(block) {
        const iframes = [];
        block.querySelectorAll('iframe').forEach(iframe => {
            const src = iframe.src || '';
            if (src && !isVideoUrl(src)) iframes.push(src);
        });
        return iframes;
    }
    const blocks = document.querySelectorAll('.studio-xblock-wrapper');
    const results = [];
    blocks.forEach(block => {
        const locator = block.getAttribute('data-locator') || '';
        const match = locator.match(/type@([^+]+)\+/);
        const rawType = match ? match[1] : 'unknown';
        if (rawType === 'vertical') return;
        const videos = getVideos(block);
        const nonVideoIframes = getNonVideoIframes(block);
        if (rawType === 'video' || videos.length > 0) {
            if (videos.length === 0) {
                results.push({ category: 'video', raw_type: rawType, duration_found: false });
            } else {
                videos.forEach(v => {
                    results.push({
                        category: 'video', raw_type: rawType, ...v,
                        duration_found: !!v.duration_seconds,
                    });
                });
            }
        }
        if (rawType === 'html' && nonVideoIframes.length > 0) {
            nonVideoIframes.forEach(src => {
                results.push({ category: 'interactive', raw_type: 'embedded_iframe', embed_url: src });
            });
        }
        if (rawType === 'html') {
            // Check for activity/case study resource banners — count as interactive
            const imgs = block.querySelectorAll('img');
            let isInteractiveBanner = false;
            for (const img of imgs) {
                const src = img.getAttribute('src') || '';
                if (src.includes('resource_banner-generic-activity') ||
                    src.includes('resource_banner-generic-case_study')) {
                    isInteractiveBanner = true;
                    break;
                }
            }
            if (isInteractiveBanner) {
                results.push({ category: 'interactive', raw_type: 'activity_banner' });
            } else {
                const text = cleanClone(block).innerText || '';
                const words = countWords(text);
                if (words > 0) results.push({ category: 'html', raw_type: rawType, words: words });
            }
        }
        else if (rawType !== 'video') {
            results.push({ category: 'interactive', raw_type: rawType });
        }
    });
    return results;
}
"""

# Expand every collapsed section/subsection in the outline so all verticals appear in DOM
EXPAND_OUTLINE_JS = r"""
async () => {
    // Click any collapsed toggles; loop until none remain
    for (let i = 0; i < 5; i++) {
        const collapsed = document.querySelectorAll(
            '.outline-section.is-collapsed > .section-header .ui-toggle-expansion, ' +
            '.outline-subsection.is-collapsed > .subsection-header .ui-toggle-expansion, ' +
            'li.outline-item.is-collapsed > div > .ui-toggle-expansion'
        );
        if (collapsed.length === 0) break;
        collapsed.forEach(el => el.click());
        await new Promise(r => setTimeout(r, 400));
    }
    return true;
}
"""

# Pull every vertical locator + its title (h3 or unit-header text)
EXTRACT_VERTICALS_JS = r"""
() => {
    const verts = document.querySelectorAll('li.outline-item.outline-unit');
    return Array.from(verts).map(li => {
        const locator = li.getAttribute('data-locator') || '';
        const titleEl = li.querySelector('.unit-header-details, h3, .unit-title');
        const title = titleEl ? titleEl.textContent.trim() : '';
        return { locator, title };
    }).filter(v => v.locator);
}
"""

# Vimeo postMessage handshake (single video per call — safe for parallel contexts)
VIMEO_EMBED_AND_QUERY_JS = """
async (videoInfo) => {
    return new Promise((resolve) => {
        let playerUrl;
        if (videoInfo.embed_url && videoInfo.embed_url.includes('player.vimeo.com')) {
            playerUrl = videoInfo.embed_url;
        } else if (videoInfo.embed_url) {
            const m = videoInfo.embed_url.match(/vimeo\\.com\\/(\\d+)(?:\\/([a-zA-Z0-9]+))?/);
            if (m) {
                playerUrl = `https://player.vimeo.com/video/${m[1]}`;
                if (m[2]) playerUrl += `?h=${m[2]}`;
            } else {
                playerUrl = `https://player.vimeo.com/video/${videoInfo.video_id}`;
            }
        } else {
            playerUrl = `https://player.vimeo.com/video/${videoInfo.video_id}`;
        }
        const iframe = document.createElement('iframe');
        iframe.src = playerUrl;
        iframe.allow = 'autoplay; fullscreen';
        iframe.width = '640'; iframe.height = '360';
        document.body.appendChild(iframe);
        const handler = (event) => {
            try {
                const d = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
                if (d && d.event === 'ready') {
                    iframe.contentWindow.postMessage({ method: 'getDuration' }, '*');
                }
                if (d && d.method === 'getDuration' && d.value) {
                    window.removeEventListener('message', handler);
                    iframe.remove();
                    resolve({ duration: d.value });
                }
            } catch(e) {}
        };
        window.addEventListener('message', handler);
        iframe.addEventListener('load', () => {
            setTimeout(() => {
                iframe.contentWindow.postMessage({ method: 'addEventListener', value: 'ready' }, '*');
                iframe.contentWindow.postMessage({ method: 'getDuration' }, '*');
            }, 1500);
        });
        setTimeout(() => {
            window.removeEventListener('message', handler);
            iframe.remove();
            resolve({ error: 'timeout' });
        }, 15000);
    });
}
"""

# ============================================================
# OUTLINE EXTRACTION — get all vertical URLs upfront
# ============================================================

def extract_vertical_urls():
    """Visit the course outline, expand all sections, return ordered list of vertical URLs."""
    print("\n" + "="*60)
    print("PREP: Extracting vertical URLs from course outline")
    print("="*60 + "\n")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(storage_state=AUTH_FILE)
        page = context.new_page()
        page.goto(COURSE_OUTLINE_URL)
        try: page.wait_for_load_state("domcontentloaded", timeout=15000)
        except: pass
        page.wait_for_timeout(2000)
        
        # Expand collapsed sections so all verticals are in DOM
        page.evaluate(EXPAND_OUTLINE_JS)
        page.wait_for_timeout(1500)
        
        verticals = page.evaluate(EXTRACT_VERTICALS_JS)
        browser.close()
    
    # Build URLs and dedupe in order
    seen = set()
    ordered = []
    for i, v in enumerate(verticals, 1):
        url = f"{STUDIO_BASE}/container/{v['locator']}"
        if url in seen: continue
        seen.add(url)
        ordered.append({
            "page": len(ordered) + 1,
            "title_hint": v["title"] or "(no title)",
            "url": url,
        })
    
    print(f"   Found {len(ordered)} verticals\n")
    return ordered

# ============================================================
# STAGE 1: PARALLEL PAGE SCRAPING
# ============================================================

# Each thread gets its own browser context to avoid sharing state.
# Playwright's sync API isn't thread-safe across contexts created in one Playwright instance,
# so we spin up sync_playwright PER THREAD. Heavier but bulletproof.

def scrape_one_page(page_info):
    """Open one vertical URL in a fresh browser, return its analyzed data."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(storage_state=AUTH_FILE)
            page = context.new_page()
            page.goto(page_info["url"], timeout=30000)
            try: page.wait_for_load_state("domcontentloaded", timeout=10000)
            except: pass
            try:
                page.wait_for_selector(".studio-xblock-wrapper", timeout=15000)
            except:
                browser.close()
                return {**page_info, "error": "no_content", "blocks": []}
            page.wait_for_timeout(1500)
            
            title = (page.locator("h1").first.text_content().strip()
                     if page.locator("h1").count() else page_info["title_hint"])
            blocks = page.evaluate(ANALYZE_BLOCKS_JS)
            browser.close()
            return {**page_info, "title": title, "blocks": blocks}
    except Exception as e:
        return {**page_info, "error": str(e)[:120], "blocks": []}

def scrape_course(vertical_list):
    """Stage 1: scrape all verticals in parallel, return structured data."""
    print("="*60)
    print(f"STAGE 1/3: Scraping {len(vertical_list)} pages with {NUM_WORKERS} workers")
    print("="*60 + "\n")
    
    results_by_page = {}
    completed = 0
    total = len(vertical_list)
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as ex:
        future_to_info = {ex.submit(scrape_one_page, v): v for v in vertical_list}
        for fut in as_completed(future_to_info):
            info = future_to_info[fut]
            result = fut.result()
            results_by_page[info["page"]] = result
            completed += 1
            err = f" ❌ {result.get('error')}" if result.get("error") else ""
            safe_print(f"[{completed:>3}/{total}] done: {result.get('title', info['title_hint'])[:55]}{err}")
    
    # Build final pages list in original order
    pages = []
    interactive_types = Counter()
    video_sources = Counter()
    videos_needing_lookup = []
    
    for i in range(1, total + 1):
        r = results_by_page.get(i)
        if not r: continue
        blocks = r.get("blocks", [])
        page_html_words = sum(b.get("words", 0) for b in blocks if b["category"] == "html")
        page_video_count = sum(1 for b in blocks if b["category"] == "video")
        page_video_seconds = sum(b.get("duration_seconds") or 0 for b in blocks if b["category"] == "video")
        page_interactive_count = sum(1 for b in blocks if b["category"] == "interactive")
        
        for b in blocks:
            if b["category"] == "video":
                if not b.get("duration_seconds"):
                    videos_needing_lookup.append({
                        "page": i, "title": r.get("title", ""),
                        "source": b.get("source"), "video_id": b.get("video_id"),
                        "embed_url": b.get("embed_url"),
                    })
                video_sources[b.get("source", "unknown")] += 1
            elif b["category"] == "interactive":
                interactive_types[b["raw_type"]] += 1
        
        est = estimate_minutes(page_html_words, page_video_seconds, page_interactive_count)
        pages.append({
            "page": i, "title": r.get("title", ""), "url": r["url"],
            "html_word_count": page_html_words,
            "video_count": page_video_count,
            "video_duration_seconds": page_video_seconds,
            "video_duration_minutes": round(page_video_seconds / 60, 2),
            "interactive_count": page_interactive_count,
            "estimated_minutes": round(est),
            **({"error": r["error"]} if r.get("error") else {}),
        })
    
    total_words = sum(p["html_word_count"] for p in pages)
    total_seconds = sum(p["video_duration_seconds"] for p in pages)
    total_interactive = sum(p["interactive_count"] for p in pages)
    course_min = estimate_minutes(total_words, total_seconds, total_interactive)
    
    return {
        "estimation_rates": {
            "words_per_minute": WORDS_PER_MINUTE,
            "minutes_per_interactive": MINUTES_PER_INTERACTIVE,
        },
        "course_totals": {
            "pages_scraped": len(pages),
            "html_word_count": total_words,
            "video_count": sum(p["video_count"] for p in pages),
            "video_duration_seconds": total_seconds,
            "video_duration_minutes": round(total_seconds / 60, 2),
            "video_duration_hours": round(total_seconds / 3600, 2),
            "interactive_count": total_interactive,
            "estimated_minutes": round(course_min),
            "estimated_hours": round(course_min / 60, 2),
        },
        "video_sources": dict(video_sources),
        "interactive_types": dict(interactive_types),
        "videos_needing_manual_lookup": videos_needing_lookup,
        "pages": pages,
    }

# ============================================================
# STAGE 2: PARALLEL YOUTUBE LOOKUPS (threads + yt-dlp)
# ============================================================

def fetch_youtube_durations(data):
    youtube_videos = [v for v in data.get("videos_needing_manual_lookup", [])
                      if v.get("source") == "youtube"]
    if not youtube_videos:
        print("\n[Stage 2] No YouTube videos — skipping")
        return
    
    print("\n" + "="*60)
    print(f"STAGE 2/3: Fetching {len(youtube_videos)} YouTube durations ({NUM_WORKERS} workers)")
    print("="*60 + "\n")
    
    try:
        import yt_dlp
    except ImportError:
        print("⚠️  yt-dlp not installed — skipping")
        return
    
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "nocheckcertificate": True}
    
    def lookup_one(vid):
        url = f"https://www.youtube.com/watch?v={vid['video_id']}"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                duration = info.get("duration")
                if duration:
                    return {**vid, "duration_seconds": int(duration),
                            "video_title": info.get("title", "")}, None
                return vid, "no_duration"
        except Exception as e:
            return vid, str(e)[:100]
    
    resolved = []
    still_failed = []
    completed = 0
    total = len(youtube_videos)
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as ex:
        futures = [ex.submit(lookup_one, v) for v in youtube_videos]
        for fut in as_completed(futures):
            result, err = fut.result()
            completed += 1
            if err:
                still_failed.append({**result, "error": err})
                safe_print(f"[{completed:>3}/{total}] ❌ youtube/{result.get('video_id')} — {err[:40]}")
            else:
                resolved.append(result)
                safe_print(f"[{completed:>3}/{total}] ✓ youtube/{result['video_id']} → {result['duration_seconds']}s")
    
    apply_resolved_videos(data, resolved)
    data["videos_needing_manual_lookup"] = [
        v for v in data["videos_needing_manual_lookup"] if v.get("source") != "youtube"
    ] + still_failed
    recalculate_estimates(data)
    print(f"\n   ✓ Resolved {len(resolved)}/{total} YouTube videos")

# ============================================================
# STAGE 3: PARALLEL VIMEO LOOKUPS (one browser context per worker)
# ============================================================

def fetch_one_vimeo(vid):
    """Open a fresh browser context on the WGU host, query Vimeo, return duration."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(
                storage_state=AUTH_FILE,
                extra_http_headers={"Referer": f"{STUDIO_BASE}/"},
            )
            page = context.new_page()
            try:
                page.goto(COURSE_OUTLINE_URL, timeout=20000)
                page.wait_for_timeout(2000)
            except Exception:
                page.goto("about:blank")
            result = page.evaluate(VIMEO_EMBED_AND_QUERY_JS, vid)
            browser.close()
            if "duration" in result and result["duration"]:
                return {**vid, "duration_seconds": int(result["duration"])}, None
            return vid, result.get("error", "unknown")
    except Exception as e:
        return vid, str(e)[:100]

def fetch_vimeo_durations(data):
    vimeo_videos = [v for v in data.get("videos_needing_manual_lookup", [])
                    if v.get("source") == "vimeo"]
    if not vimeo_videos:
        print("\n[Stage 3] No Vimeo videos — skipping")
        return
    
    print("\n" + "="*60)
    print(f"STAGE 3/3: Fetching {len(vimeo_videos)} Vimeo durations ({NUM_WORKERS} workers)")
    print("="*60 + "\n")
    
    resolved = []
    still_failed = []
    completed = 0
    total = len(vimeo_videos)
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as ex:
        futures = [ex.submit(fetch_one_vimeo, v) for v in vimeo_videos]
        for fut in as_completed(futures):
            result, err = fut.result()
            completed += 1
            if err:
                still_failed.append({**result, "error": err})
                safe_print(f"[{completed:>3}/{total}] ❌ vimeo/{result.get('video_id')} — {err[:40]}")
            else:
                resolved.append(result)
                safe_print(f"[{completed:>3}/{total}] ✓ vimeo/{result['video_id']} → {result['duration_seconds']}s")
    
    apply_resolved_videos(data, resolved)
    data["videos_needing_manual_lookup"] = [
        v for v in data["videos_needing_manual_lookup"] if v.get("source") != "vimeo"
    ] + still_failed
    recalculate_estimates(data)
    print(f"\n   ✓ Resolved {len(resolved)}/{total} Vimeo videos")

# ============================================================
# MAIN
# ============================================================

def print_summary(data):
    t = data["course_totals"]
    print("\n" + "="*65)
    print(f"✅ FINAL SUMMARY ({t['pages_scraped']} pages)")
    print("="*65)
    print(f"📝 HTML word count:       {t['html_word_count']:,}  (~{round(t['html_word_count']/WORDS_PER_MINUTE)} min reading)")
    print(f"🎬 Videos:                {t['video_count']} videos, {t['video_duration_minutes']} min")
    print(f"🎮 Interactive blocks:    {t['interactive_count']}  (~{t['interactive_count'] * MINUTES_PER_INTERACTIVE} min)")
    print(f"\n⏱️  ESTIMATED TOTAL:       {t['estimated_minutes']} min  ({t['estimated_hours']} hours)")
    failed = data.get("videos_needing_manual_lookup", [])
    if failed:
        print(f"\n⚠️  {len(failed)} video(s) still need manual lookup")
        for v in failed[:5]:
            print(f"   - p.{v['page']}: {v.get('title','')[:45]} ({v.get('source')}/{v.get('video_id')})")
        if len(failed) > 5:
            print(f"   ... and {len(failed)-5} more (see {OUTPUT_FILE})")

def save(data):
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def main():
    verticals = extract_vertical_urls()
    if not verticals:
        print("❌ No verticals found — check the outline URL and auth.")
        return
    
    data = scrape_course(verticals)
    save(data)
    
    fetch_youtube_durations(data)
    save(data)
    
    fetch_vimeo_durations(data)
    save(data)
    
    print_summary(data)
    print(f"\nFull results in {OUTPUT_FILE}")

if __name__ == "__main__":
    main()