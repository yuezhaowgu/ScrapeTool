from playwright.sync_api import sync_playwright
import json
from collections import Counter

# ============================================================
# CONFIGURATION
# ============================================================

# Replace these URLs with the course you want to scrape
START_URL = ""
COURSE_OUTLINE_URL = ""
OUTPUT_FILE = "course_summary.json"
AUTH_FILE = "auth.json"

WORDS_PER_MINUTE = 250
MINUTES_PER_INTERACTIVE = 5

# ============================================================
# TIME CALCULATOR
# ============================================================

def estimate_minutes(html_words, video_seconds, interactive_count):
    return (html_words / WORDS_PER_MINUTE 
            + video_seconds / 60 
            + interactive_count * MINUTES_PER_INTERACTIVE)

def recalculate_estimates(data):
    """Update estimated_minutes on every page and the course totals."""
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
    """Given a list of videos with newly-resolved durations, update per-page totals."""
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
# STAGE 1: SCRAPE THE COURSE
# ============================================================

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
            const text = cleanClone(block).innerText || '';
            const words = countWords(text);
            if (words > 0) results.push({ category: 'html', raw_type: rawType, words: words });
        }
        else if (rawType !== 'video') {
            results.push({ category: 'interactive', raw_type: rawType });
        }
    });
    return results;
}
"""

NEXT_BUTTON_CLICK_JS = """
() => {
    const buttons = document.querySelectorAll('button');
    for (const btn of buttons) {
        if (btn.classList.contains('button-next') && btn.classList.contains('sequence-nav-button')) {
            if (btn.disabled) return { found: true, disabled: true };
            btn.click();
            return { found: true, disabled: false };
        }
    }
    return { found: false };
}
"""

def scrape_course():
    """Stage 1: Walk every page in the course, return structured data."""
    print("\n" + "="*60)
    print("STAGE 1/3: Scraping course pages")
    print("="*60 + "\n")
    
    pages = []
    interactive_types = Counter()
    video_sources = Counter()
    videos_needing_lookup = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=AUTH_FILE)
        page = context.new_page()
        page.goto(START_URL)
        try: page.wait_for_load_state("domcontentloaded", timeout=10000)
        except: pass
        page.wait_for_timeout(2000)
        
        page_num = 1
        seen_urls = set()
        
        while True:
            current_url = page.url
            if current_url in seen_urls:
                print(f"   Already seen this URL — stopping")
                break
            seen_urls.add(current_url)
            
            try:
                page.wait_for_selector(".studio-xblock-wrapper", timeout=15000)
            except:
                print(f"   No content wrappers — stopping")
                break
            page.wait_for_timeout(2000)
            
            title = page.locator("h1").first.text_content().strip() if page.locator("h1").count() else "(no title)"
            blocks = page.evaluate(ANALYZE_BLOCKS_JS)
            
            page_html_words = sum(b.get("words", 0) for b in blocks if b["category"] == "html")
            page_video_count = sum(1 for b in blocks if b["category"] == "video")
            page_video_seconds = sum(b.get("duration_seconds") or 0 for b in blocks if b["category"] == "video")
            page_interactive_count = sum(1 for b in blocks if b["category"] == "interactive")
            
            for b in blocks:
                if b["category"] == "video":
                    if not b.get("duration_seconds"):
                        videos_needing_lookup.append({
                            "page": page_num, "title": title,
                            "source": b.get("source"), "video_id": b.get("video_id"),
                            "embed_url": b.get("embed_url"),
                        })
                    video_sources[b.get("source", "unknown")] += 1
                elif b["category"] == "interactive":
                    interactive_types[b["raw_type"]] += 1
            
            est = estimate_minutes(page_html_words, page_video_seconds, page_interactive_count)
            pages.append({
                "page": page_num, "title": title, "url": current_url,
                "html_word_count": page_html_words,
                "video_count": page_video_count,
                "video_duration_seconds": page_video_seconds,
                "video_duration_minutes": round(page_video_seconds / 60, 2),
                "interactive_count": page_interactive_count,
                "estimated_minutes": round(est),
            })
            
            parts = []
            if page_html_words: parts.append(f"{page_html_words}w")
            if page_video_count: parts.append(f"{page_video_count}v ({round(page_video_seconds/60, 1)}m)")
            if page_interactive_count: parts.append(f"{page_interactive_count}i")
            print(f"[{page_num:>3}] {title[:45]:<47} {' '.join(parts) or '—':<25} ≈{round(est)}m")
            
            url_before = page.url
            click_result = page.evaluate(NEXT_BUTTON_CLICK_JS)
            if not click_result.get("found"):
                print("\n   No Next button — finished!"); break
            if click_result.get("disabled"):
                print("\n   Next button disabled — finished!"); break
            
            navigated = False
            for _ in range(30):
                page.wait_for_timeout(500)
                if page.url != url_before:
                    navigated = True
                    break
            if not navigated:
                print(f"\n   ⚠️ URL didn't change. Stopping."); break
            
            try: page.wait_for_load_state("domcontentloaded", timeout=10000)
            except: pass
            page_num += 1
            page.wait_for_timeout(1500)
        
        browser.close()
    
    # Build output
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
# STAGE 2: FETCH YOUTUBE DURATIONS (via yt-dlp)
# ============================================================

def fetch_youtube_durations(data):
    """Stage 2: Use yt-dlp to look up YouTube video durations."""
    youtube_videos = [v for v in data.get("videos_needing_manual_lookup", []) 
                      if v.get("source") == "youtube"]
    
    if not youtube_videos:
        print("\n[Stage 2] No YouTube videos to look up — skipping")
        return
    
    print("\n" + "="*60)
    print(f"STAGE 2/3: Fetching {len(youtube_videos)} YouTube durations")
    print("="*60 + "\n")
    
    try:
        import yt_dlp
    except ImportError:
        print("⚠️  yt-dlp not installed — skipping YouTube lookups")
        print("   Install with: pip3 install yt-dlp")
        return
    
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "nocheckcertificate": True}
    resolved = []
    still_failed = []
    
    for i, vid in enumerate(youtube_videos, 1):
        url = f"https://www.youtube.com/watch?v={vid['video_id']}"
        print(f"[{i}/{len(youtube_videos)}] {vid['title'][:45]:<47} youtube/{vid['video_id']}", end=" ")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                duration = info.get("duration")
                if duration:
                    resolved.append({**vid, "duration_seconds": int(duration),
                                     "video_title": info.get("title", "")})
                    print(f"→ {int(duration)}s ({round(duration/60, 1)}m)")
                else:
                    still_failed.append({**vid, "error": "no_duration"})
                    print("→ no duration")
        except Exception as e:
            still_failed.append({**vid, "error": str(e)[:100]})
            print(f"❌ {str(e)[:40]}")
    
    apply_resolved_videos(data, resolved)
    # Remove resolved from the lookup list, keep failed + non-youtube
    data["videos_needing_manual_lookup"] = [
        v for v in data["videos_needing_manual_lookup"]
        if v.get("source") != "youtube"
    ] + still_failed
    recalculate_estimates(data)
    
    print(f"\n   ✓ Resolved {len(resolved)} of {len(youtube_videos)} YouTube videos")

# ============================================================
# STAGE 3: FETCH VIMEO DURATIONS (via Playwright on WGU)
# ============================================================

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

def fetch_vimeo_durations(data):
    """Stage 3: Use Playwright on WGU domain to query Vimeo iframes."""
    vimeo_videos = [v for v in data.get("videos_needing_manual_lookup", [])
                    if v.get("source") == "vimeo"]
    
    if not vimeo_videos:
        print("\n[Stage 3] No Vimeo videos to look up — skipping")
        return
    
    print("\n" + "="*60)
    print(f"STAGE 3/3: Fetching {len(vimeo_videos)} Vimeo durations via WGU")
    print("="*60 + "\n")
    
    resolved = []
    still_failed = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            storage_state=AUTH_FILE,
            extra_http_headers={"Referer": "https://studio.learn.authoring.goacademy.wgu.edu/"},
        )
        page = context.new_page()
        
        try:
            page.goto(COURSE_OUTLINE_URL, timeout=20000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"⚠️ Couldn't load host page: {e}")
            page.goto("about:blank")
        
        for i, v in enumerate(vimeo_videos, 1):
            print(f"[{i}/{len(vimeo_videos)}] {v['title'][:45]:<47} vimeo/{v['video_id']}", end=" ")
            try:
                result = page.evaluate(VIMEO_EMBED_AND_QUERY_JS, v)
                if "duration" in result and result["duration"]:
                    duration_sec = int(result["duration"])
                    resolved.append({**v, "duration_seconds": duration_sec})
                    print(f"→ {duration_sec}s ({round(duration_sec/60, 1)}m)")
                else:
                    still_failed.append({**v, "error": result.get("error", "unknown")})
                    print(f"❌ {result.get('error', 'unknown')}")
            except Exception as e:
                still_failed.append({**v, "error": str(e)[:100]})
                print(f"❌ {str(e)[:40]}")
            page.wait_for_timeout(1000)
        
        browser.close()
    
    apply_resolved_videos(data, resolved)
    data["videos_needing_manual_lookup"] = [
        v for v in data["videos_needing_manual_lookup"]
        if v.get("source") != "vimeo"
    ] + still_failed
    recalculate_estimates(data)
    
    print(f"\n   ✓ Resolved {len(resolved)} of {len(vimeo_videos)} Vimeo videos")

# ============================================================
# MAIN — orchestrates the three stages
# ============================================================

def print_summary(data):
    t = data["course_totals"]
    print("\n" + "="*65)
    print(f"✅ FINAL SUMMARY ({t['pages_scraped']} pages)")
    print("="*65)
    print(f"📝 HTML word count:       {t['html_word_count']:,}  (~{round(t['html_word_count']/WORDS_PER_MINUTE)} min reading)")
    print(f"🎬 Videos:                {t['video_count']} videos, {t['video_duration_minutes']} min")
    print(f"🎮 Interactive blocks:    {t['interactive_count']}  (~{t['interactive_count'] * MINUTES_PER_INTERACTIVE} min @ {MINUTES_PER_INTERACTIVE} min each)")
    print(f"\n⏱️  ESTIMATED TOTAL:       {t['estimated_minutes']} min  ({t['estimated_hours']} hours)")
    
    failed = data.get("videos_needing_manual_lookup", [])
    if failed:
        print(f"\n⚠️  {len(failed)} video(s) still need manual duration lookup")
        for v in failed[:5]:
            print(f"   - p.{v['page']}: {v['title'][:45]} ({v.get('source')}/{v.get('video_id')})")
        if len(failed) > 5:
            print(f"   ... and {len(failed)-5} more (see {OUTPUT_FILE})")

def main():
    # Stage 1: Scrape
    data = scrape_course()
    
    # Save after stage 1 so we don't lose work if a later stage crashes
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    # Stage 2: YouTube
    fetch_youtube_durations(data)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    # Stage 3: Vimeo
    fetch_vimeo_durations(data)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    # Final summary
    print_summary(data)
    print(f"\nFull results in {OUTPUT_FILE}")

if __name__ == "__main__":
    main()