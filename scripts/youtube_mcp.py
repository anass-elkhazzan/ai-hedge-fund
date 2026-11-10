"""MCP server: watch a YouTube video so Claude can learn from it: captions, metadata, and frame contact sheets.

Captions say what is said; youtube_frames saves a contact sheet of the picture (view it with Read) for charts or
on-screen settings. Frames are written to runs/youtube/<id>/ (gitignored).
Run: python scripts/youtube_mcp.py   (stdio transport; registered in .mcp.json as "youtube")
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from mcp.server import MCPServer
from youtube_transcript_api import YouTubeTranscriptApi

OUT = Path(__file__).resolve().parent.parent / "runs" / "youtube"

mcp = MCPServer(
    name="youtube",
    instructions="Watch a YouTube video by URL or id: transcript (captions), info (title, views, chapters...), "
                 "and youtube_frames for a contact sheet of the picture, viewed with Read.",
)


def _video_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    raise ValueError(f"Not a YouTube URL or video id: {url_or_id}")


def _stamp(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


@mcp.tool()
def get_transcript(url: str, languages: str = "en", timestamps: bool = True) -> dict:
    """Transcript of a YouTube video. `languages`: comma-separated preference list, e.g. "en,fr"."""
    try:
        vid = _video_id(url)
        api = YouTubeTranscriptApi()
        t = api.fetch(vid, languages=[x.strip() for x in languages.split(",") if x.strip()])
        lines = [f"[{_stamp(s.start)}] {s.text}" if timestamps else s.text for s in t.snippets]
        return {"video_id": vid, "language": t.language_code, "auto_generated": t.is_generated,
                "transcript": "\n".join(lines)}
    except Exception as e:  # no captions, private video, blocked, ...
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def list_transcripts(url: str) -> dict:
    """Which caption languages a video has (manual and auto-generated)."""
    try:
        vid = _video_id(url)
        return {"video_id": vid, "available": [
            {"language": t.language, "code": t.language_code, "auto_generated": t.is_generated}
            for t in YouTubeTranscriptApi().list(vid)]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def youtube_info(url: str) -> dict:
    """Title, channel, views, likes, duration, upload date, tags, description and chapters of a YouTube video."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "js_runtimes": {"node": {}}}) as y:
            i = y.extract_info(f"https://www.youtube.com/watch?v={_video_id(url)}", download=False)
        keys = ("id", "title", "channel", "channel_follower_count", "view_count", "like_count", "comment_count",
                "duration", "upload_date", "tags", "description")
        info = {k: i.get(k) for k in keys}
        info["chapters"] = [{"start": _stamp(c["start_time"]), "title": c["title"]} for c in (i.get("chapters") or [])]
        return info
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def youtube_frames(url: str, count: int = 8) -> dict:
    """Save a contact sheet of `count` (2-16) evenly spaced frames from a low-res copy; view the returned path with Read."""
    try:
        vid = _video_id(url)
        d = OUT / vid
        d.mkdir(parents=True, exist_ok=True)
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        opts = {"outtmpl": str(d / "frames_src.%(ext)s"), "format": "best[height<=480]/bv*[height<=480]/best",
                "quiet": True, "js_runtimes": {"node": {}}, "ffmpeg_location": ff}
        with yt_dlp.YoutubeDL(opts) as y:
            i = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
            src = y.prepare_filename(i)
        count = max(2, min(int(count), 16))
        sheet = d / "frames.jpg"
        step = max(1.0, (i.get("duration") or 60) / count)
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-vf",
                        f"fps=1/{step:.2f},scale=240:-2,tile={count}x1", "-frames:v", "1", str(sheet)], check=True)
        os.remove(src)
        return {"video_id": vid, "frames": str(sheet)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    mcp.run("stdio")
