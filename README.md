# ScrapeTool

Scrapes course pages from WGU's authoring studio and estimates total study time by analyzing HTML text, videos, and interactive elements.

## Setup

**Requires Python 3.10+**

1. **Install dependencies**

```bash
pip install playwright yt-dlp
playwright install chromium
```

2. **Authenticate**

Run the login script to save your browser session:

```bash
python login.py
```

A browser window will open. Log in to WGU Studio manually, then press Enter in the terminal. This saves your session to `auth.json`.

## Running

```bash
python scrape.py
```

The script runs in 3 stages:

| Stage | What it does |
|-------|-------------|
| 1 | Walks every page via the Next button, extracts HTML word counts, video info, and interactive blocks |
| 2 | Looks up YouTube video durations using `yt-dlp` |
| 3 | Looks up Vimeo video durations by embedding iframes on the authenticated WGU domain |

Results are saved to `course_summary.json` after each stage (so nothing is lost if a later stage fails).

## Time Estimation

Total estimated time is calculated per page and for the whole course:

```
estimated_minutes = (html_words / 250) + (video_seconds / 60) + (interactive_count × 5)
```

| Element | Rate |
|---------|------|
| HTML text | 250 words per minute reading speed |
| Video | Actual duration (fetched from YouTube/Vimeo) |
| Interactive blocks | 5 minutes each (flat estimate) |

These rates are configurable at the top of `scrape.py` (`WORDS_PER_MINUTE`, `MINUTES_PER_INTERACTIVE`).
