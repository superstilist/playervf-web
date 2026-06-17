import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Default directory layout (all relative to this file; callers can override)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("PLAYER_VF_OUTPUT_DIR") or (BASE_DIR / "output")).expanduser()
DOWNLOAD_DIR = OUTPUT_DIR / "downloads"
JSON_DIR = OUTPUT_DIR / "metadata"
COVER_DIR = OUTPUT_DIR / "covers"
STREAM_VIDEO_CACHE_DIR = Path(
    os.environ.get("PLAYER_VF_STREAM_VIDEO_CACHE_DIR") or (OUTPUT_DIR / "stream_video_cache")
).expanduser()
MAX_STREAM_VIDEO_HEIGHT = 1080


def _stream_cache_enabled(default: bool = True) -> bool:
    raw = os.environ.get("PLAYER_VF_STREAM_CACHE_ENABLED")
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _bundled_tool_path(name: str) -> Optional[str]:
    env_key = f"PLAYER_VF_{name.upper()}"
    env_value = os.environ.get(env_key)
    if env_value and Path(env_value).exists():
        return env_value

    if os.name == "nt" and not name.lower().endswith(".exe"):
        executable_names = [f"{name}.exe"]
    else:
        executable_names = [name]

    search_roots = [
        BASE_DIR,
        BASE_DIR.parent,
        BASE_DIR.parent.parent,
        Path.cwd(),
        Path.cwd() / "tools",
        Path.cwd() / "linux" / "packaged",
    ]
    for root in search_roots:
        for executable_name in executable_names:
            for candidate in (
                root / name / "bin" / executable_name,
                root / "tools" / name / "bin" / executable_name,
                root / "packaged" / name / "bin" / executable_name,
                root / executable_name,
            ):
                if candidate.exists():
                    return str(candidate)
    return None


def _ffmpeg_location() -> Optional[str]:
    ffmpeg = _bundled_tool_path("ffmpeg") or shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    return str(Path(ffmpeg).resolve())

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def safe_filename(name: str, max_len: int = 180) -> str:
    """Strip filesystem-unsafe characters and trim length."""
    name = str(name or "")
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] if len(name) > max_len else name


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


def has_cjk_text(value: str) -> bool:
    return bool(_CJK_RE.search(str(value or "")))


def romanize_cjk_text(value: str) -> str:
    text = str(value or "").strip()
    if not has_cjk_text(text):
        return ""

    romanized = ""
    if re.search(r"[\u3040-\u30ff]", text):
        try:
            import pykakasi

            kakasi = pykakasi.kakasi()
            parts = kakasi.convert(text)
            romanized = " ".join(
                str(part.get("hepburn") or part.get("kunrei") or part.get("orig") or "").strip()
                for part in parts
            )
        except Exception:
            romanized = ""

    if not romanized.strip():
        try:
            from pypinyin import lazy_pinyin

            romanized = " ".join(lazy_pinyin(text, errors="default"))
        except Exception:
            romanized = ""

    romanized = re.sub(r"\s+", " ", romanized).strip()
    if not romanized or romanized == text:
        return ""
    return romanized


def display_with_romanization(value: str, romanized: str = "") -> str:
    text = str(value or "").strip()
    romaji = str(romanized or romanize_cjk_text(text)).strip()
    if not text or not romaji or romaji.lower() == text.lower():
        return text
    return f"{text} ({romaji})"


def filename_text(value: str) -> str:
    text = str(value or "").strip()
    romanized = romanize_cjk_text(text)
    return romanized or text


def unique_path(path: Path) -> Path:
    """Return *path* unchanged if it does not exist, otherwise append *(n)*."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def unique_media_stem(stem: Path) -> Path:
    suffixes = (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".aac", ".mp4", ".mkv", ".mov", ".m4v")
    temp_suffixes = tuple(f".temp{suffix}" for suffix in suffixes) + (".part",)

    def is_free(candidate: Path) -> bool:
        return not any(candidate.with_suffix(suffix).exists() for suffix in suffixes + temp_suffixes)

    if is_free(stem):
        return stem

    index = 1
    while True:
        candidate = stem.with_name(f"{stem.name} ({index})")
        if is_free(candidate):
            return candidate
        index += 1


def download_bytes(url: str, timeout: int = 30) -> bytes:
    """Download *url* and return raw bytes."""
    import requests

    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def download_to_file(url: str, path: Path, timeout: int = 30) -> Path:
    """Download *url* and save to *path*, returning *path*."""
    path.write_bytes(download_bytes(url, timeout=timeout))
    return path


def detect_mime(path: Path) -> str:
    """Guess image MIME type from file extension."""
    ext = path.suffix.lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")


def _thumbnail_dimensions(thumb: Dict[str, Any], url: str) -> Tuple[int, int]:
    width = int(thumb.get("width") or 0)
    height = int(thumb.get("height") or 0)
    if width > 0 and height > 0:
        return width, height

    clean = str(url or "").split("?")[0]
    match = re.search(r"=w(\d+)-h(\d+)", clean)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"=s(\d+)", clean)
    if match:
        size = int(match.group(1))
        return size, size

    lower = clean.lower()
    if lower.endswith("maxresdefault.jpg"):
        return 1280, 720
    if lower.endswith("sddefault.jpg"):
        return 640, 480
    if lower.endswith("hq720.jpg"):
        return 1280, 720
    if lower.endswith("hqdefault.jpg"):
        return 480, 360
    if lower.endswith("mqdefault.jpg"):
        return 320, 180
    return 0, 0


def _is_music_art_url(url: str) -> bool:
    lower = str(url or "").lower()
    return (
        "googleusercontent.com" in lower
        or "yt3.ggpht.com" in lower
        or "=w" in lower
        or "=s" in lower
    )


def _thumbnail_sort_key(thumb: Dict[str, Any], prefer_square: bool) -> Tuple[float, ...]:
    url = str(thumb.get("url") or "")
    width, height = _thumbnail_dimensions(thumb, url)
    area = width * height
    if not prefer_square:
        return (float(area),)
    if width <= 0 or height <= 0:
        aspect_delta = 1.0
    else:
        aspect_delta = abs((width / height) - 1.0)
    square_score = max(0.0, 1.0 - min(aspect_delta, 1.0))
    music_art_score = 1.0 if _is_music_art_url(url) else 0.0
    return (music_art_score, square_score, float(area))


def thumbnail_candidates(*entities: Optional[Dict[str, Any]], prefer_square: bool = False) -> List[str]:
    """Return thumbnail URLs from all entities, highest-resolution first."""
    thumbs: List[Dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            if value.get("url"):
                thumbs.append(value)
            for key in ("thumbnails", "thumbnail"):
                if key in value:
                    collect(value[key])

    for entity in entities:
        if isinstance(entity, dict):
            collect(entity)

    thumbs.sort(key=lambda t: _thumbnail_sort_key(t, prefer_square), reverse=True)
    urls: List[str] = []
    seen = set()
    for thumb in thumbs:
        url = thumb.get("url")
        if isinstance(url, str) and url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def high_quality_thumbnail_urls(url: str, prefer_square: bool = False) -> List[str]:
    """Expand a YouTube thumbnail URL into likely higher-quality variants."""
    urls: List[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in urls:
            urls.append(candidate)

    clean = str(url or "").split("?")[0]
    google_sized = re.search(r"=w\d+-h\d+[^?]*$", clean)
    google_square = re.search(r"=s\d+[^?]*$", clean)
    if google_sized:
        for suffix in ("=w1200-h1200-l90-rj", "=w960-h960-l90-rj", "=w544-h544-l90-rj"):
            add(re.sub(r"=w\d+-h\d+[^?]*$", suffix, clean))
    elif google_square:
        for suffix in ("=s1200", "=s960", "=s544"):
            add(re.sub(r"=s\d+[^?]*$", suffix, clean))

    match = re.search(r"(https?://[^/]+/(?:vi|vi_webp)/([^/]+)/)", clean)
    if match:
        prefix = match.group(1).replace("/vi_webp/", "/vi/")
        names = (
            ("hqdefault.jpg", "mqdefault.jpg", "sddefault.jpg", "maxresdefault.jpg", "hq720.jpg")
            if prefer_square
            else ("maxresdefault.jpg", "sddefault.jpg", "hq720.jpg", "hqdefault.jpg", "mqdefault.jpg")
        )
        for name in names:
            add(prefix + name)
    add(url)
    add(clean)
    return urls


def detect_image_mime(data: bytes, fallback_url: str = "") -> Optional[str]:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    ext = Path(fallback_url.split("?")[0]).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    return None


def _is_video_thumbnail_url(url: str) -> bool:
    return bool(re.search(r"/(?:vi|vi_webp)/[^/]+/", str(url or "")))


def _normalized_cover_image(data: bytes, mime: str) -> Tuple[bytes, str]:
    """
    Return square cover-art bytes.

    YouTube video thumbnails are often 16:9 frames with grey/colored side
    padding. The app needs album-style artwork, so crop the downloaded image to
    a centered square before embedding it into PC/Android music files.
    """
    try:
        from PIL import Image
    except Exception:
        return data, mime

    try:
        with Image.open(io.BytesIO(data)) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width <= 0 or height <= 0:
                return data, mime

            side = min(width, height)
            left = max(0, (width - side) // 2)
            top = max(0, (height - side) // 2)
            cropped = rgb.crop((left, top, left + side, top + side))

            max_side = 1200
            if side > max_side:
                cropped = cropped.resize((max_side, max_side), Image.Resampling.LANCZOS)

            out = io.BytesIO()
            cropped.save(out, format="JPEG", quality=95, optimize=True, progressive=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return data, mime


def download_best_cover(*entities: Optional[Dict[str, Any]]) -> Optional[Tuple[bytes, str]]:
    """Download the best available JPEG/PNG cover bytes from candidate metadata."""
    all_urls: List[str] = []
    seen = set()
    for entity in entities:
        for url in thumbnail_candidates(entity, prefer_square=True):
            if url not in seen:
                all_urls.append(url)
                seen.add(url)

    ordered_urls = [url for url in all_urls if not _is_video_thumbnail_url(url)]
    ordered_urls.extend(url for url in all_urls if _is_video_thumbnail_url(url))

    for url in ordered_urls:
        for candidate in high_quality_thumbnail_urls(url, prefer_square=True):
            try:
                data = download_bytes(candidate, timeout=20)
                mime = detect_image_mime(data, candidate)
                if mime:
                    return _normalized_cover_image(data, mime)
            except Exception:
                continue
    return None


def preview_thumbnail_url(*entities: Optional[Dict[str, Any]]) -> str:
    """Return the best verified image URL for streaming preview artwork."""
    import requests

    for entity in entities:
        for url in thumbnail_candidates(entity, prefer_square=True):
            for candidate in high_quality_thumbnail_urls(url, prefer_square=True):
                try:
                    with requests.get(candidate, stream=True, timeout=8) as response:
                        response.raise_for_status()
                        content_type = (response.headers.get("content-type") or "").lower()
                        chunk = next(response.iter_content(16), b"")
                        if "image/" in content_type or detect_image_mime(chunk, candidate):
                            return candidate
                except Exception:
                    continue
    return ""


def best_thumbnail(entity: Dict[str, Any]) -> Optional[str]:
    """Return the highest-resolution thumbnail URL found in *entity*, or None."""
    candidates = thumbnail_candidates(entity)
    return candidates[0] if candidates else None


def format_track_no(index: int, total: int) -> str:
    """Zero-padded track number string, e.g. '03' for index=3, total=12."""
    return str(index).zfill(max(2, len(str(total))))


def duration_seconds_from_value(value: Any) -> Optional[int]:
    """Normalize common YTMusic/yt-dlp duration values to whole seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if seconds > 0 else None

    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    if text.isdigit():
        number = int(text)
        return number // 1000 if number > 10000 else number
    if ":" in text:
        parts = text.split(":")
        if 2 <= len(parts) <= 3 and all(part.strip().isdigit() for part in parts):
            total = 0
            for part in parts:
                total = (total * 60) + int(part.strip())
            return total if total > 0 else None
    return None


def extract_duration_seconds(*entities: Optional[Dict[str, Any]]) -> Optional[int]:
    """Find the best duration value in YTMusic or yt-dlp metadata dictionaries."""
    keys = (
        "duration",
        "durationSeconds",
        "lengthSeconds",
        "approxDurationMs",
        "duration_ms",
    )
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        for key in keys:
            seconds = duration_seconds_from_value(entity.get(key))
            if seconds:
                return seconds
        video_details = entity.get("videoDetails")
        if isinstance(video_details, dict):
            for key in keys:
                seconds = duration_seconds_from_value(video_details.get(key))
                if seconds:
                    return seconds
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SearchItem:
    """Lightweight representation of a single YTMusic search result."""
    result_type: str
    title: str
    artist: str = ""
    title_romaji: str = ""
    artist_romaji: str = ""
    duration: str = ""
    video_id: str = ""
    browse_id: str = ""
    thumbnails: Optional[List[Dict[str, Any]]] = None
    raw: Optional[Dict[str, Any]] = field(default=None, repr=False)

    @property
    def thumbnail_url(self) -> Optional[str]:
        if self.thumbnails:
            best = max(
                self.thumbnails,
                key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0),
            )
            return best.get("url")
        return best_thumbnail(self.raw or {})


# ---------------------------------------------------------------------------
# Core downloader
# ---------------------------------------------------------------------------

class MediaDownloader:
    """
    High-level interface for searching YouTube Music and downloading tracks,
    albums, and playlists as tagged MP3 files.

    Parameters
    ----------
    ytmusic : YTMusic
        An authenticated (or anonymous) YTMusic instance.
    download_dir : Path, optional
        Root folder for downloaded MP3s. Defaults to module-level DOWNLOAD_DIR.
    json_dir : Path, optional
        Folder for saved metadata JSON files. Defaults to module-level JSON_DIR.
    cover_dir : Path, optional
        Folder for saved cover-art images. Defaults to module-level COVER_DIR.
    """

    def __init__(
            self,
            ytmusic: Optional[Any] = None,
            download_dir: Path = DOWNLOAD_DIR,
            json_dir: Path = JSON_DIR,
            cover_dir: Path = COVER_DIR,
            progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        from ytmusicapi import YTMusic

        self.ytmusic = ytmusic or YTMusic(language="en")
        self.download_dir = download_dir
        self.json_dir = json_dir
        self.cover_dir = cover_dir
        self.progress_cb = progress_cb

        self.download_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
            self,
            query: str,
            filter_name: str = "songs",
            limit: int = 25,
    ) -> List[SearchItem]:
        """
        Search YouTube Music.

        Parameters
        ----------
        query : str
            Search terms.
        filter_name : str
            One of ``"songs"``, ``"albums"``, ``"playlists"``, ``"videos"``.
        limit : int
            Maximum number of results to return.

        Returns
        -------
        List[SearchItem]
        """
        results = self.ytmusic.search(query, filter=filter_name, limit=limit)
        items: List[SearchItem] = []
        for r in results:
            result_type = r.get(
                "resultType",
                filter_name[:-1] if filter_name.endswith("s") else filter_name,
            )
            artists = r.get("artists") or []
            artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            title = r.get("title", "")
            items.append(
                SearchItem(
                    result_type=result_type,
                    title=title,
                    artist=artist,
                    title_romaji=romanize_cjk_text(title),
                    artist_romaji=romanize_cjk_text(artist),
                    duration=r.get("duration", "") or (
                        str(extract_duration_seconds(r) or "")
                        if extract_duration_seconds(r)
                        else ""
                    ),
                    video_id=r.get("videoId", ""),
                    browse_id=r.get("browseId", ""),
                    thumbnails=r.get("thumbnails"),
                    raw=r,
                )
            )
        return items

    # ------------------------------------------------------------------
    # Entity detail fetchers
    # ------------------------------------------------------------------

    def get_entity_details(self, item: SearchItem) -> Dict[str, Any]:
        """Fetch full metadata for a SearchItem from the YTMusic API."""
        if item.result_type in ("song", "video") and item.video_id:
            return self.ytmusic.get_song(item.video_id)
        if item.result_type == "album" and item.browse_id:
            return self.ytmusic.get_album(item.browse_id)
        if item.result_type in ("playlist", "featured_playlist", "community_playlist") and item.browse_id:
            pid = item.browse_id[2:] if item.browse_id.startswith("VL") else item.browse_id
            return self.ytmusic.get_playlist(pid, limit=None, related=False)
        return item.raw or {}

    @staticmethod
    def get_album_tracks(album: Dict[str, Any]) -> List[Dict[str, Any]]:
        return album.get("tracks", []) or []

    @staticmethod
    def get_playlist_tracks(playlist: Dict[str, Any]) -> List[Dict[str, Any]]:
        return playlist.get("tracks", []) or []

    # ------------------------------------------------------------------
    # Audio download
    # ------------------------------------------------------------------

    def download_audio_file(
            self,
            video_id: str,
            out_stem: Path,
            verbose: bool = False,
    ) -> Tuple[Path, Dict[str, Any]]:
        """
        Download a YouTube video as a playable audio file via yt-dlp.

        Parameters
        ----------
        video_id : str
            YouTube video ID.
        out_stem : Path
            Output path *without* extension. yt-dlp appends the final extension.
        verbose : bool
            Pass ``True`` to enable yt-dlp console output.

        Returns
        -------
        Tuple[Path, Dict[str, Any]]
            ``(audio_path, yt_dlp_info_dict)``
        """
        ffmpeg_location = _ffmpeg_location()
        has_ffmpeg = ffmpeg_location is not None
        self._delete_stale_outputs(out_stem)
        outtmpl = str(out_stem) + ".%(ext)s"
        ydl_opts: Dict[str, Any] = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": not verbose,
            "noprogress": True,
            "verbose": verbose,
            "writethumbnail": False,
            "continuedl": True,
        }
        if ffmpeg_location:
            ydl_opts["ffmpeg_location"] = ffmpeg_location

        if self.progress_cb:
            def _hook(status: Dict[str, Any]) -> None:
                total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                downloaded = status.get("downloaded_bytes") or 0
                percent = float(downloaded) / float(total) if total else None
                self.progress_cb({
                    "event": "download",
                    "status": status.get("status", ""),
                    "percent": percent,
                    "downloadedBytes": downloaded,
                    "totalBytes": total,
                    "filename": status.get("filename", ""),
                })

            ydl_opts["progress_hooks"] = [_hook]
        if has_ffmpeg:
            ydl_opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                {"key": "FFmpegMetadata"},
            ]

        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        preferred_path = Path(str(out_stem) + ".mp3") if has_ffmpeg else None
        if preferred_path and preferred_path.exists():
            self._delete_sidecar_images(preferred_path)
            return preferred_path, info

        candidates = sorted(
            out_stem.parent.glob(out_stem.name + ".*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        media_candidates = [
            path for path in candidates
            if path.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".aac")
        ]
        if media_candidates:
            audio_path = media_candidates[0]
        else:
            candidates = sorted(out_stem.parent.glob(out_stem.name + "*.mp3"))
            if candidates:
                audio_path = candidates[0]
            else:
                raise FileNotFoundError(f"Audio file was not created for video_id={video_id}")

        self._delete_sidecar_images(audio_path)
        return audio_path, info

    def download_audio_mp3(
            self,
            video_id: str,
            out_stem: Path,
            verbose: bool = False,
    ) -> Tuple[Path, Dict[str, Any]]:
        return self.download_audio_file(video_id, out_stem, verbose=verbose)

    @staticmethod
    def _delete_stale_outputs(stem: Path) -> None:
        for suffix in (".temp.mp3", ".temp.m4a", ".temp.webm", ".temp.opus", ".temp.ogg", ".temp.aac", ".part"):
            try:
                candidate = stem.with_suffix(suffix)
                if candidate.exists():
                    candidate.unlink()
            except Exception:
                pass

    @staticmethod
    def _delete_sidecar_images(media_path: Path) -> None:
        for suffix in (".webp", ".jpg", ".jpeg", ".png"):
            for candidate in (
                media_path.with_suffix(suffix),
                media_path.with_name(f"{media_path.stem}.cover{suffix}"),
            ):
                try:
                    if candidate.exists():
                        candidate.unlink()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # ID3 tag writing
    # ------------------------------------------------------------------

    def write_mp3_tags(
            self,
            mp3_path: Path,
            title: str,
            artist: str,
            album: str = "",
            date: str = "",
            genre: str = "",
            track_no: Optional[int] = None,
            total_tracks: Optional[int] = None,
            cover_path: Optional[Path] = None,
            comment: str = "",
    ) -> None:
        """Write ID3v2.3 tags (and optional cover art) to an MP3 file."""
        from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TIT2, TPE1, TDRC, TLEN, TRCK
        from mutagen.mp3 import MP3

        audio = MP3(str(mp3_path), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        for frame in ("TIT2", "TPE1", "TALB", "TDRC", "TCON", "TRCK", "TLEN", "COMM", "APIC"):
            try:
                tags.delall(frame)
            except Exception:
                pass

        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if date:
            tags.add(TDRC(encoding=3, text=date))
        if genre:
            tags.add(TCON(encoding=3, text=genre))
        if track_no is not None:
            track_str = f"{track_no}/{total_tracks}" if total_tracks is not None else str(track_no)
            tags.add(TRCK(encoding=3, text=track_str))
        audio_duration = getattr(audio.info, "length", 0) or 0
        if audio_duration > 0:
            tags.add(TLEN(encoding=3, text=str(int(audio_duration * 1000))))
        if comment:
            tags.add(COMM(encoding=3, lang="eng", desc="Comment", text=comment))
        if cover_path and cover_path.exists():
            tags.add(
                APIC(
                    encoding=3,
                    mime=detect_mime(cover_path),
                    type=3,
                    desc="Cover",
                    data=cover_path.read_bytes(),
                )
            )

        audio.save(v2_version=3)

    # ------------------------------------------------------------------
    # Helpers: JSON persistence & cover art
    # ------------------------------------------------------------------

    def save_json(
            self,
            data: Dict[str, Any],
            name: str,
            subdir: Optional[Path] = None,
    ) -> Path:
        """Serialise *data* to a uniquely-named JSON file and return its path."""
        folder = subdir if subdir else self.json_dir
        folder.mkdir(parents=True, exist_ok=True)
        path = unique_path(folder / f"{safe_filename(name)}.json")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def save_cover(self, entity: Dict[str, Any], filename_base: str) -> Optional[Path]:
        """
        Download the best thumbnail from *entity* and save it to the covers
        directory. Returns the saved path, or ``None`` on failure.
        """
        thumb_urls = thumbnail_candidates(entity, prefer_square=True)
        if not thumb_urls:
            return None
        thumb_url = thumb_urls[0]
        suffix = Path(thumb_url.split("?")[0]).suffix or ".jpg"
        cover_path = unique_path(self.cover_dir / f"{safe_filename(filename_base)}{suffix}")
        try:
            cover = download_best_cover(entity)
            if cover:
                data, mime = cover
                suffix = ".png" if mime == "image/png" else ".jpg"
                cover_path = cover_path.with_suffix(suffix)
                cover_path.write_bytes(data)
                return cover_path
            download_to_file(thumb_url, cover_path)
            return cover_path
        except Exception:
            return None

    def save_temp_cover(self, entity: Dict[str, Any], fallback_entity: Optional[Dict[str, Any]] = None) -> Optional[Path]:
        """Download the best cover art to a temp file for ID3 embedding."""
        cover = download_best_cover(entity, fallback_entity)
        if not cover:
            return None

        data, mime = cover
        suffix = ".png" if mime == "image/png" else ".jpg"
        try:
            with tempfile.NamedTemporaryFile(prefix="playervf_cover_", suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                return Path(tmp.name)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Track field extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_track_fields(track: Dict[str, Any]) -> Tuple[str, str, str, str]:
        """
        Pull ``(title, artist, album, date)`` strings out of a raw track dict.
        Falls back to sensible defaults when fields are missing.
        """
        video_details = track.get("videoDetails") if isinstance(track.get("videoDetails"), dict) else {}
        title = track.get("title") or track.get("name") or video_details.get("title") or "Unknown Title"

        artists = track.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
        if not artist:
            author = track.get("author")
            artist = author if isinstance(author, str) else video_details.get("author") or "Unknown Artist"

        album = ""
        if isinstance(track.get("album"), dict):
            album = track["album"].get("name", "") or ""
        elif isinstance(track.get("album"), str):
            album = track["album"]

        raw_date = str(track.get("year") or track.get("releaseDate") or "")
        date = "" if raw_date == "None" else raw_date

        return title, artist, album, date

    # ------------------------------------------------------------------
    # Single-track download + tag pipeline
    # ------------------------------------------------------------------

    def download_track(
            self,
            track: Dict[str, Any],
            base_dir: Path,
            index: Optional[int] = None,
            total: Optional[int] = None,
            cover_path: Optional[Path] = None,
            verbose: bool = False,
    ) -> Optional[Path]:
        """
        Download a single track dict, write ID3 tags, and save metadata JSON.

        Parameters
        ----------
        track : dict
            Raw track dict from YTMusic (must contain ``"videoId"``).
        base_dir : Path
            Directory in which to save the MP3.
        index : int, optional
            1-based track position within an album/playlist.
        total : int, optional
            Total number of tracks in the collection.
        cover_path : Path, optional
            Pre-downloaded cover image; fetched automatically when ``None``.
        verbose : bool
            Enable yt-dlp verbose output.

        Returns
        -------
        Path or None
            Path to the saved MP3, or ``None`` if the track has no video ID.
        """
        video_details = track.get("videoDetails") if isinstance(track.get("videoDetails"), dict) else {}
        video_id = track.get("videoId") or video_details.get("videoId")
        if not video_id:
            return None

        title, artist, album, date = self.extract_track_fields(track)
        title_romaji = romanize_cjk_text(title)
        artist_romaji = romanize_cjk_text(artist)
        base_dir.mkdir(parents=True, exist_ok=True)

        if index is not None and total is not None:
            stem = base_dir / f"{format_track_no(index, total)} - {safe_filename(filename_text(title))}"
        else:
            stem = base_dir / f"{safe_filename(filename_text(artist))} - {safe_filename(filename_text(title))}"
        stem = unique_media_stem(stem)

        audio_path, info = self.download_audio_file(video_id, stem, verbose=verbose)

        cover_sidecar = None
        if audio_path.suffix.lower() == ".mp3":
            temp_cover = None
            tag_cover = cover_path
            if tag_cover is None:
                temp_cover = self.save_temp_cover(track, info if isinstance(info, dict) else None)
                tag_cover = temp_cover
            if tag_cover and tag_cover.exists():
                suffix = tag_cover.suffix or ".jpg"
                cover_sidecar = unique_path(audio_path.with_suffix(f".cover{suffix}"))
                try:
                    shutil.copyfile(tag_cover, cover_sidecar)
                except Exception:
                    cover_sidecar = None
            try:
                self.write_mp3_tags(
                    mp3_path=audio_path,
                    title=title,
                    artist=artist,
                    album=album,
                    date=date,
                    genre=str(track.get("genre") or ""),
                    track_no=index,
                    total_tracks=total,
                    cover_path=tag_cover,
                    comment=f"Downloaded via yt-dlp from YouTube Music. videoId={video_id}",
                )
            finally:
                if temp_cover:
                    try:
                        temp_cover.unlink(missing_ok=True)
                    except Exception:
                        pass
        else:
            cover_source = cover_path or self.save_temp_cover(track, info if isinstance(info, dict) else None)
            if cover_source and cover_source.exists():
                suffix = cover_source.suffix or ".jpg"
                cover_sidecar = unique_path(audio_path.with_suffix(f".cover{suffix}"))
                try:
                    shutil.copyfile(cover_source, cover_sidecar)
                except Exception:
                    cover_sidecar = None
                if cover_path is None:
                    try:
                        cover_source.unlink(missing_ok=True)
                    except Exception:
                        pass
        duration_seconds = extract_duration_seconds(track, info if isinstance(info, dict) else None)
        metadata = {
            "type": "playervf.youtubeAudio",
            "version": 1,
            "title": title,
            "artist": artist,
            "titleRomaji": title_romaji,
            "artistRomaji": artist_romaji,
            "displayTitle": display_with_romanization(title, title_romaji),
            "displayArtist": display_with_romanization(artist, artist_romaji),
            "album": album,
            "date": date,
            "durationSeconds": duration_seconds or 0,
            "videoId": video_id,
            "file": str(audio_path),
            "coverPath": str(cover_sidecar) if cover_sidecar else "",
            "downloadedAt": datetime.now().isoformat(timespec="seconds"),
        }
        self.save_json(metadata, f"{artist} - {title}")
        audio_path.with_suffix(".playervf.audio.json").write_text(
            json.dumps(metadata, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return audio_path

    # ------------------------------------------------------------------
    # High-level entity downloaders
    # ------------------------------------------------------------------

    def download_song(
            self,
            item: SearchItem,
            progress_cb: Optional[Callable[[str], None]] = None,
            verbose: bool = False,
    ) -> List[Path]:
        """Download a single song or video SearchItem. Returns list with one path."""
        entity = self.get_entity_details(item)
        track = entity if isinstance(entity, dict) else item.raw or {}
        video_details = track.get("videoDetails") if isinstance(track.get("videoDetails"), dict) else {}
        if item.video_id and not track.get("videoId") and not video_details.get("videoId"):
            track["videoId"] = item.video_id
        if item.title and not track.get("title") and not video_details.get("title"):
            track["title"] = item.title
        if item.artist and not track.get("author") and not track.get("artists"):
            track["author"] = item.artist
        if item.thumbnail_url and not best_thumbnail(track):
            track["thumbnail"] = [{"url": item.thumbnail_url, "width": 0, "height": 0}]
        if progress_cb:
            progress_cb(f"Downloading: {item.title}")

        mp3 = self.download_track(track, self.download_dir, cover_path=None, verbose=verbose)
        return [mp3] if mp3 else []

    def download_album(
            self,
            item: SearchItem,
            progress_cb: Optional[Callable[[str], None]] = None,
            verbose: bool = False,
    ) -> List[Path]:
        """
        Download every track in an album SearchItem into a dedicated sub-folder.

        Parameters
        ----------
        item : SearchItem
            Must have ``result_type == "album"``.
        progress_cb : callable, optional
            Called with a status string before each track download.
        verbose : bool
            Enable yt-dlp verbose output per track.

        Returns
        -------
        List[Path]
            Paths of all successfully downloaded MP3 files.
        """
        album = self.get_entity_details(item)
        tracks = self.get_album_tracks(album)

        album_title = album.get("title") or item.title or "Album"
        artist = (
                ", ".join(a.get("name", "") for a in album.get("artists", []) if a.get("name"))
                or item.artist
                or "Unknown Artist"
        )
        year = str(album.get("year") or "")

        album_folder = self.download_dir / safe_filename(
            f"{filename_text(artist)} - {filename_text(album_title)}"
        )
        album_folder.mkdir(parents=True, exist_ok=True)
        cover_path = self.save_temp_cover(album)

        usable = [t for t in tracks if t.get("videoId")]
        total = len(usable)
        downloaded: List[Path] = []
        idx = 0

        try:
            for track in tracks:
                if not track.get("videoId"):
                    continue
                idx += 1
                if progress_cb:
                    progress_cb(f"Album {idx}/{total}: {track.get('title') or 'Unknown'}")

                # Inject album metadata into the track dict when missing
                if not track.get("album"):
                    track["album"] = {"name": album_title}
                elif isinstance(track.get("album"), dict):
                    track["album"]["name"] = track["album"].get("name") or album_title
                track["year"] = track.get("year") or year

                mp3 = self.download_track(
                    track, album_folder,
                    index=idx, total=total,
                    cover_path=cover_path,
                    verbose=verbose,
                )
                if mp3:
                    downloaded.append(mp3)
        finally:
            if cover_path:
                try:
                    cover_path.unlink(missing_ok=True)
                except Exception:
                    pass

        return downloaded

    def download_playlist(
            self,
            item: SearchItem,
            progress_cb: Optional[Callable[[str], None]] = None,
            verbose: bool = False,
    ) -> List[Path]:
        """
        Download every track in a playlist SearchItem into a dedicated sub-folder.

        Parameters
        ----------
        item : SearchItem
            Must have ``result_type`` in
            ``("playlist", "featured_playlist", "community_playlist")``.
        progress_cb : callable, optional
            Called with a status string before each track download.
        verbose : bool
            Enable yt-dlp verbose output per track.

        Returns
        -------
        List[Path]
            Paths of all successfully downloaded MP3 files.
        """
        playlist = self.get_entity_details(item)
        tracks = self.get_playlist_tracks(playlist)

        playlist_title = playlist.get("title") or item.title or "Playlist"
        author_raw = playlist.get("author")
        author = (
                     author_raw.get("name") if isinstance(author_raw, dict) else author_raw
                 ) or item.artist or "Unknown Artist"

        playlist_folder = self.download_dir / safe_filename(
            f"{filename_text(author)} - {filename_text(playlist_title)}"
        )
        playlist_folder.mkdir(parents=True, exist_ok=True)
        cover_path = self.save_temp_cover(playlist)

        usable = [t for t in tracks if t.get("videoId")]
        total = len(usable)
        downloaded: List[Path] = []
        idx = 0

        try:
            for track in tracks:
                if not track.get("videoId"):
                    continue
                idx += 1
                if progress_cb:
                    progress_cb(f"Playlist {idx}/{total}: {track.get('title') or 'Unknown'}")

                if not track.get("album"):
                    track["album"] = {"name": playlist_title}
                elif isinstance(track.get("album"), dict):
                    track["album"]["name"] = track["album"].get("name") or playlist_title

                mp3 = self.download_track(
                    track, playlist_folder,
                    index=idx, total=total,
                    cover_path=cover_path,
                    verbose=verbose,
                )
                if mp3:
                    downloaded.append(mp3)
        finally:
            if cover_path:
                try:
                    cover_path.unlink(missing_ok=True)
                except Exception:
                    pass

        return downloaded

    def download(
            self,
            item: SearchItem,
            progress_cb: Optional[Callable[[str], None]] = None,
            verbose: bool = False,
    ) -> List[Path]:
        """
        Dispatch to the correct downloader based on ``item.result_type``.

        Supports ``"song"``, ``"video"``, ``"album"``, ``"playlist"``,
        ``"featured_playlist"``, and ``"community_playlist"``.

        Parameters
        ----------
        item : SearchItem
            Any item returned by :meth:`search`.
        progress_cb : callable, optional
            Called with a human-readable progress string before each track.
        verbose : bool
            Enable yt-dlp verbose output.

        Returns
        -------
        List[Path]
            Paths of all downloaded MP3 files.

        Raises
        ------
        ValueError
            If ``item.result_type`` is not recognised.
        """
        if item.result_type in ("song", "video"):
            return self.download_song(item, progress_cb=progress_cb, verbose=verbose)
        if item.result_type == "album":
            return self.download_album(item, progress_cb=progress_cb, verbose=verbose)
        if item.result_type in ("playlist", "featured_playlist", "community_playlist"):
            return self.download_playlist(item, progress_cb=progress_cb, verbose=verbose)
        raise ValueError(f"Unsupported result_type: {item.result_type!r}")


# ---------------------------------------------------------------------------
# Flutter / native bridge helpers
# ---------------------------------------------------------------------------

def add(a: int, b: int) -> int:
    """Tiny bridge smoke-test used by Flutter MethodChannel."""
    return int(a) + int(b)


def _artist_text(entity: Dict[str, Any]) -> str:
    artists = entity.get("artists") or []
    if isinstance(artists, list):
        names = [artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        if names:
            return ", ".join(names)

    author = entity.get("author")
    if isinstance(author, dict):
        return author.get("name", "") or ""
    return author if isinstance(author, str) else ""


def _search_item_to_dict(item: SearchItem) -> Dict[str, Any]:
    title_romaji = item.title_romaji or romanize_cjk_text(item.title)
    artist_romaji = item.artist_romaji or romanize_cjk_text(item.artist)
    return {
        "resultType": item.result_type,
        "title": item.title,
        "artist": item.artist,
        "titleRomaji": title_romaji,
        "artistRomaji": artist_romaji,
        "displayTitle": display_with_romanization(item.title, title_romaji),
        "displayArtist": display_with_romanization(item.artist, artist_romaji),
        "duration": item.duration or (
            str(extract_duration_seconds(item.raw or {}) or "") if extract_duration_seconds(item.raw or {}) else ""
        ),
        "videoId": item.video_id,
        "browseId": item.browse_id,
        "thumbnailUrl": item.thumbnail_url or "",
        "raw": item.raw or {},
    }


def _search_item_from_dict(data: Dict[str, Any]) -> SearchItem:
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else data
    result_type = str(data.get("resultType") or data.get("result_type") or raw.get("resultType") or "")
    video_id = str(data.get("videoId") or data.get("video_id") or raw.get("videoId") or "")
    if not result_type and video_id:
        result_type = "video" if data.get("video") is True else "song"
    return SearchItem(
        result_type=result_type,
        title=str(raw.get("title") or data.get("title") or "Unknown title"),
        artist=str(_artist_text(raw) or data.get("artist") or ""),
        title_romaji=str(data.get("titleRomaji") or data.get("title_romaji") or ""),
        artist_romaji=str(data.get("artistRomaji") or data.get("artist_romaji") or ""),
        duration=str(
            data.get("duration")
            or raw.get("duration")
            or (extract_duration_seconds(raw) or "")
        ),
        video_id=video_id,
        browse_id=str(data.get("browseId") or data.get("browse_id") or raw.get("browseId") or ""),
        thumbnails=raw.get("thumbnails") if isinstance(raw.get("thumbnails"), list) else None,
        raw=raw,
    )


def search_youtube_music(query: str, filter_name: str = "songs", limit: int = 20) -> str:
    """Return YouTube Music search results as a JSON string for platform bridges."""
    query = str(query or "").strip()
    if not query:
        return "[]"

    allowed_filters = {"songs", "videos", "albums", "playlists"}
    filter_name = filter_name if filter_name in allowed_filters else "songs"
    downloader = MediaDownloader()
    items = downloader.search(query=query, filter_name=filter_name, limit=int(limit or 20))
    return json.dumps([_search_item_to_dict(item) for item in items], ensure_ascii=True)


def _download_youtube_video_file(
    item: SearchItem,
    download_dir: Path,
    quality_height: Optional[int] = None,
    quality_heights: Optional[List[int]] = None,
    write_subtitles: bool = False,
    subtitle_lang: Optional[str] = None,
    subtitle_langs: Optional[List[str]] = None,
    auto_subtitles: bool = False,
) -> List[Path]:
    video_id = item.video_id or item.raw.get("videoId")
    if not video_id:
        raise ValueError("This video result cannot be downloaded because it has no videoId.")

    import yt_dlp

    ffmpeg_location = _ffmpeg_location()
    if not ffmpeg_location:
        raise RuntimeError(
            "FFmpeg was not found, so this video cannot be merged with audio. "
            "Run ./tool/run_linux.sh again so PlayerVF bundles FFmpeg, or install ffmpeg."
        )
    download_dir.mkdir(parents=True, exist_ok=True)
    stem = download_dir / safe_filename(
        f"{filename_text(item.artist)} - {filename_text(item.title)}"
        if item.artist else filename_text(item.title)
    )
    heights = [int(h) for h in (quality_heights or []) if int(h or 0) > 0]
    if not heights and quality_height:
        heights = [int(quality_height)]
    if not heights:
        heights = [0]
    heights = sorted(set(heights), reverse=True)

    subtitle_languages = [
        str(lang).strip()
        for lang in (subtitle_langs or ([] if not subtitle_lang else [subtitle_lang]))
        if str(lang).strip()
    ]
    if not subtitle_languages:
        subtitle_languages = ["en", "cs", "sk"]

    def _hook(status: Dict[str, Any]) -> None:
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        downloaded = status.get("downloaded_bytes") or 0
        percent = float(downloaded) / float(total) if total else None
        _emit_progress({
            "event": "download",
            "status": status.get("status", ""),
            "percent": percent,
            "downloadedBytes": downloaded,
            "totalBytes": total,
            "filename": status.get("filename", ""),
        })

    for candidate in stem.parent.glob(stem.name + ".*"):
        if candidate.suffix.lower() in (".part", ".ytdl", ".tmp"):
            try:
                candidate.unlink()
            except Exception:
                pass

    all_files: List[Path] = []
    manifest_qualities: List[Dict[str, Any]] = []
    manifest_subtitles: Dict[str, Dict[str, Any]] = {}
    subtitle_failures: List[str] = []
    url = f"https://www.youtube.com/watch?v={video_id}"
    manifest_path = stem.with_suffix(".playervf.json")
    existing_manifest = _read_youtube_video_manifest(manifest_path)
    if not existing_manifest:
        existing_path = _find_youtube_video_manifest(download_dir, str(video_id))
        if existing_path:
            manifest_path = existing_path
            stem = existing_path.with_suffix("")
            existing_manifest = _read_youtube_video_manifest(existing_path)
    manifest_qualities = list(existing_manifest.get("qualities") or [])
    manifest_subtitles = _subtitle_manifest_map(existing_manifest.get("subtitles") or [])
    subtitle_failures = list(existing_manifest.get("subtitleFailures") or [])

    for cap in heights:
        label = _video_quality_label(cap)
        existing_quality = _find_manifest_quality(manifest_qualities, cap)
        if existing_quality is not None:
            existing_path = Path(str(existing_quality.get("path") or ""))
            if existing_path.exists():
                _emit_progress({
                    "event": "download",
                    "status": "skipped",
                    "percent": 1,
                    "filename": f"{label} already saved",
                })
                all_files.append(existing_path)
                continue

        quality_stem = unique_media_stem(download_dir / safe_filename(
            f"{filename_text(item.artist)} - {filename_text(item.title)}.{label}"
            if item.artist else f"{filename_text(item.title)}.{label}"
        ))
        outtmpl = str(quality_stem) + ".%(ext)s"
        format_selector = _video_download_format_selector(cap)
        for old_file in quality_stem.parent.glob(quality_stem.name + ".*"):
            if old_file.suffix.lower() in {".part", ".ytdl", ".tmp"}:
                try:
                    old_file.unlink()
                except Exception:
                    pass

        ydl_opts: Dict[str, Any] = {
            "format": format_selector,
            "format_sort": ["res", "fps", "br"],
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "writethumbnail": False,
            "ignoreerrors": True,
            "overwrites": False,
            "continuedl": True,
            "progress_hooks": [_hook],
        }
        ydl_opts["ffmpeg_location"] = ffmpeg_location
        ydl_opts["merge_output_format"] = "mp4"

        _emit_progress({
            "event": "download",
            "status": "starting",
            "percent": None,
            "filename": f"{label} video",
        })
        downloaded_info: Dict[str, Any] = {}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                extracted = ydl.extract_info(url, download=True)
                if isinstance(extracted, dict):
                    downloaded_info = extracted
        except Exception as error:
            message = str(error)
            raise RuntimeError(f"Video quality {label} download failed: {message}") from error

        media_suffixes = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
        candidates = sorted(
            [path for path in quality_stem.parent.glob(quality_stem.name + ".*") if path.exists()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        media_files = [path for path in candidates if path.suffix.lower() in media_suffixes]
        if not media_files:
            raise FileNotFoundError(f"Video file was not created for video_id={video_id}")
        media_path = media_files[0]
        resolved_height = cap
        for requested in downloaded_info.get("requested_downloads") or []:
            if not isinstance(requested, dict):
                continue
            height = int(requested.get("height") or 0)
            if height > resolved_height:
                resolved_height = height
        if resolved_height <= 0:
            selected = _select_video_format(downloaded_info, None)
            resolved_height = int(selected.get("height") or 0) if isinstance(selected, dict) else 0
        resolved_label = _video_quality_label(resolved_height or cap)
        all_files.append(media_path)
        manifest_qualities = [
            quality for quality in manifest_qualities
            if int(quality.get("height") or -1) not in {cap, resolved_height}
        ]
        manifest_qualities.append({
            "label": resolved_label,
            "requestedLabel": label,
            "requestedHeight": cap,
            "height": resolved_height,
            "path": str(media_path),
            "ext": media_path.suffix.lstrip("."),
            "formatSelector": format_selector,
        })

    if write_subtitles:
        subtitle_entries, subtitle_failures = _download_video_subtitle_sidecars(
            video_url=url,
            stem=stem,
            languages=subtitle_languages,
            automatic=auto_subtitles,
        )
        for subtitle_entry in subtitle_entries:
            subtitle_path = Path(str(subtitle_entry.get("path") or ""))
            key = f"{subtitle_entry.get('language')}:{subtitle_entry.get('automatic')}"
            manifest_subtitles[key] = subtitle_entry
            all_files.append(subtitle_path)

    manifest = {
        "type": "playervf.youtubeVideoSet",
        "version": 1,
        "videoId": video_id,
        "title": item.title,
        "artist": item.artist,
        "titleRomaji": item.title_romaji or romanize_cjk_text(item.title),
        "artistRomaji": item.artist_romaji or romanize_cjk_text(item.artist),
        "displayTitle": display_with_romanization(
            item.title,
            item.title_romaji or romanize_cjk_text(item.title),
        ),
        "displayArtist": display_with_romanization(
            item.artist,
            item.artist_romaji or romanize_cjk_text(item.artist),
        ),
        "qualities": manifest_qualities,
        "subtitles": list(manifest_subtitles.values()),
        "subtitleFailures": subtitle_failures,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
    all_files.append(manifest_path)
    return all_files


def _download_youtube_video_subtitles_only(
    item: SearchItem,
    download_dir: Path,
    subtitle_lang: Optional[str] = None,
    subtitle_langs: Optional[List[str]] = None,
    auto_subtitles: bool = False,
) -> List[Path]:
    video_id = item.video_id or item.raw.get("videoId")
    if not video_id:
        raise ValueError("This video result cannot download subtitles because it has no videoId.")

    download_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = _find_youtube_video_manifest(download_dir, str(video_id))
    if manifest_path is None:
        stem = download_dir / safe_filename(
            f"{filename_text(item.artist)} - {filename_text(item.title)}"
            if item.artist else filename_text(item.title)
        )
        manifest_path = stem.with_suffix(".playervf.json")
        manifest = {}
    else:
        stem = manifest_path.with_suffix("")
        manifest = _read_youtube_video_manifest(manifest_path)

    subtitle_languages = [
        str(lang).strip()
        for lang in (subtitle_langs or ([] if not subtitle_lang else [subtitle_lang]))
        if str(lang).strip()
    ]
    if not subtitle_languages:
        subtitle_languages = ["en", "cs", "sk"]

    url = f"https://www.youtube.com/watch?v={video_id}"
    subtitle_entries, subtitle_failures = _download_video_subtitle_sidecars(
        video_url=url,
        stem=stem,
        languages=subtitle_languages,
        automatic=auto_subtitles,
    )

    subtitles = _subtitle_manifest_map(manifest.get("subtitles") or [])
    for subtitle_entry in subtitle_entries:
        key = f"{subtitle_entry.get('language')}:{subtitle_entry.get('automatic')}"
        subtitles[key] = subtitle_entry

    merged_manifest = {
        "type": "playervf.youtubeVideoSet",
        "version": 1,
        "videoId": video_id,
        "title": item.title,
        "artist": item.artist,
        "qualities": list(manifest.get("qualities") or []),
        "subtitles": list(subtitles.values()),
        "subtitleFailures": list(manifest.get("subtitleFailures") or []) + subtitle_failures,
        "createdAt": manifest.get("createdAt") or datetime.now().isoformat(timespec="seconds"),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    manifest_path.write_text(json.dumps(merged_manifest, ensure_ascii=True, indent=2), encoding="utf-8")
    return [Path(str(item.get("path"))) for item in subtitle_entries] + [manifest_path]


def _read_youtube_video_manifest(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("type") == "playervf.youtubeVideoSet":
            return data
    except Exception:
        pass
    return {}


def _find_youtube_video_manifest(download_dir: Path, video_id: str) -> Optional[Path]:
    if not video_id:
        return None
    try:
        candidates = sorted(
            download_dir.rglob("*.playervf.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return None
    for candidate in candidates:
        data = _read_youtube_video_manifest(candidate)
        if str(data.get("videoId") or "") == str(video_id):
            return candidate
    return None


def _subtitle_manifest_map(items: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        language = str(item.get("language") or "")
        automatic = bool(item.get("automatic"))
        path = str(item.get("path") or item.get("url") or "")
        key = f"{language}:{automatic}" if language else path
        if key:
            result[key] = dict(item)
    return result


def _find_manifest_quality(qualities: List[Dict[str, Any]], height: int) -> Optional[Dict[str, Any]]:
    for quality in qualities:
        if not isinstance(quality, dict):
            continue
        try:
            if int(quality.get("height") or -1) == int(height):
                return quality
        except Exception:
            continue
    return None


def _video_download_format_selector(max_height: int, has_ffmpeg: bool = True) -> str:
    if not has_ffmpeg:
        if max_height > 0:
            return "/".join([
                f"best[height={max_height}][vcodec!=none][acodec!=none]",
                f"best[height<={max_height}][vcodec!=none][acodec!=none]",
                "best[vcodec!=none][acodec!=none]",
                "best",
            ])
        return "/".join([
            "best[vcodec!=none][acodec!=none]",
            "best",
        ])

    if max_height > 0:
        return "/".join([
            f"bv*[height={max_height}][ext=mp4]+ba[ext=m4a]",
            f"bv*[height={max_height}]+ba",
            f"bestvideo[height={max_height}]+bestaudio",
            f"best[height={max_height}][vcodec!=none][acodec!=none]",
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]",
            f"bv*[height<={max_height}]+ba",
            f"bestvideo[height<={max_height}]+bestaudio",
            f"best[height<={max_height}][vcodec!=none][acodec!=none]",
            "bv*[ext=mp4]+ba[ext=m4a]",
            "bv*+ba",
            "best[vcodec!=none][acodec!=none]",
            "best",
        ])
    return "/".join([
        "bv*[ext=mp4]+ba[ext=m4a]",
        "bv*+ba",
        "bestvideo+bestaudio",
        "best[vcodec!=none][acodec!=none]",
        "best",
    ])


def _download_video_subtitle_sidecars(
    video_url: str,
    stem: Path,
    languages: List[str],
    automatic: bool,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    import yt_dlp

    failures: List[str] = []
    downloaded: List[Dict[str, Any]] = []
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as error:
        return [], [f"Subtitle metadata failed: {error}"]

    manual_tracks = info.get("subtitles") or {}
    auto_tracks = info.get("automatic_captions") or {}
    preferred_sources: List[Tuple[Dict[str, Any], bool]] = (
        [(auto_tracks, True), (manual_tracks, False)]
        if automatic
        else [(manual_tracks, False), (auto_tracks, True)]
    )
    for language in languages:
        track = None
        resolved_language = language
        resolved_automatic = automatic
        for source, source_automatic in preferred_sources:
            resolved_language, track = _choose_subtitle_track(source, language)
            if track is not None:
                resolved_automatic = source_automatic
                break
        if track is None:
            failures.append(f"No subtitle track found for {language}")
            continue

        ext = str(track.get("ext") or "vtt").lower()
        if ext not in {"vtt", "srt", "ass", "ssa"}:
            ext = "vtt"
        label = safe_filename(
            f"{stem.name}.{resolved_language}{'.auto' if resolved_automatic else ''}",
            max_len=200,
        )
        target = unique_path(stem.with_name(f"{label}.{ext}"))
        try:
            _emit_progress({
                "event": "download",
                "status": "subtitle",
                "percent": None,
                "filename": target.name,
            })
            download_to_file(track["url"], target, timeout=45)
            downloaded.append({
                "label": target.stem,
                "language": resolved_language,
                "path": str(target),
                "automatic": resolved_automatic,
                "ext": ext,
            })
        except Exception as error:
            failures.append(f"{resolved_language}: {error}")
    return downloaded, failures


def _choose_subtitle_track(source: Dict[str, Any], language: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not source:
        return language, None
    language_lower = language.lower()
    matching_key = None
    for key in source.keys():
        key_lower = str(key).lower()
        if key_lower == language_lower or key_lower.startswith(language_lower):
            matching_key = key
            break
    if matching_key is None:
        return language, None

    tracks = source.get(matching_key) or []
    if not isinstance(tracks, list):
        return str(matching_key), None
    best = None
    for candidate in tracks:
        if not isinstance(candidate, dict) or not candidate.get("url"):
            continue
        ext = str(candidate.get("ext") or "").lower()
        if best is None or ext in ("vtt", "srt"):
            best = candidate
            if ext == "vtt":
                break
    return str(matching_key), best


def _subtitle_language_from_filename(name: str) -> str:
    parts = name.split(".")
    if len(parts) >= 4 and parts[-2].lower() == "auto":
        return parts[-3]
    if len(parts) >= 3:
        return parts[-2]
    return ""


def download_youtube_music(
    item_json: Any,
    output_dir: Optional[str] = None,
    video: bool = False,
    quality_height: Optional[int] = None,
    quality_heights: Optional[List[int]] = None,
    write_subtitles: bool = False,
    subtitle_lang: Optional[str] = None,
    subtitle_langs: Optional[List[str]] = None,
    auto_subtitles: bool = False,
    subtitles_only: bool = False,
) -> str:
    """Download a search result and return a JSON payload for Flutter."""
    if isinstance(item_json, str):
        item_data = json.loads(item_json)
    else:
        item_data = dict(item_json or {})

    download_dir = Path(output_dir).expanduser().resolve() if output_dir else DOWNLOAD_DIR
    downloader = MediaDownloader(download_dir=download_dir, progress_cb=_emit_progress)
    item = _search_item_from_dict(item_data)
    has_video_id = bool(item.video_id or item.raw.get("videoId"))
    if subtitles_only and has_video_id:
        files = _download_youtube_video_subtitles_only(
            item,
            download_dir,
            subtitle_lang=subtitle_lang,
            subtitle_langs=subtitle_langs,
            auto_subtitles=auto_subtitles,
        )
    elif video and has_video_id:
        files = _download_youtube_video_file(
            item,
            download_dir,
            quality_height=quality_height,
            quality_heights=quality_heights,
            write_subtitles=write_subtitles,
            subtitle_lang=subtitle_lang,
            subtitle_langs=subtitle_langs,
            auto_subtitles=auto_subtitles,
        )
    else:
        files = downloader.download(item)
    payload = {
        "files": [str(path) for path in files],
        "libraryFiles": _library_files_for_download(files) if video or subtitles_only else [str(path) for path in files],
        "downloadDir": str(download_dir),
        "message": _download_message(files, video or subtitles_only),
        "downloadedAt": datetime.now().isoformat(timespec="seconds"),
    }
    return json.dumps(payload, ensure_ascii=True)


def _library_files_for_download(files: List[Path]) -> List[str]:
    media_suffixes = {
        ".mp4", ".mkv", ".webm", ".mov", ".m4v",
        ".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg", ".aac",
    }
    return [
        str(path)
        for path in files
        if path.suffix.lower() in media_suffixes
    ][:1]


def _download_message(files: List[Path], video: bool) -> str:
    if not video:
        return "Downloaded from YouTube Music"
    quality_count = len([path for path in files if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}])
    subtitle_count = len([path for path in files if path.suffix.lower() in {".vtt", ".srt", ".ass", ".ssa"}])
    if quality_count == 0 and subtitle_count > 0:
        return f"Downloaded {subtitle_count} subtitle file{'s' if subtitle_count != 1 else ''}."
    quality_word = "quality" if quality_count == 1 else "qualities"
    return f"Downloaded {quality_count} video {quality_word}" + (
        f" and {subtitle_count} subtitle file{'s' if subtitle_count != 1 else ''}." if subtitle_count else "."
    )


def _video_quality_label(height: Any) -> str:
    try:
        value = int(height or 0)
    except Exception:
        value = 0
    return f"{value}p" if value > 0 else "Auto"


def _select_video_format(info: Dict[str, Any], max_height: Optional[int]) -> Dict[str, Any]:
    formats = info.get("formats") or []
    usable: List[Dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if not fmt.get("url"):
            continue
        if fmt.get("vcodec") in (None, "none") or fmt.get("acodec") in (None, "none"):
            continue
        height = int(fmt.get("height") or 0)
        if height <= 0:
            continue
        usable.append(fmt)

    if not usable:
        return info

    usable.sort(
        key=lambda fmt: (
            int(fmt.get("height") or 0),
            int(fmt.get("tbr") or 0),
        ),
        reverse=True,
    )
    if max_height and max_height > 0:
        capped = [fmt for fmt in usable if int(fmt.get("height") or 0) <= max_height]
        if capped:
            return capped[0]
    return usable[0]


def _select_direct_video_format(info: Dict[str, Any], max_height: Optional[int]) -> Dict[str, Any]:
    effective_max_height = max_height if max_height and max_height > 0 else MAX_STREAM_VIDEO_HEIGHT
    formats = info.get("formats") or []
    usable: List[Dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if not fmt.get("url"):
            continue
        if fmt.get("vcodec") in (None, "none") or fmt.get("acodec") in (None, "none"):
            continue
        protocol = str(fmt.get("protocol") or "").lower()
        if protocol and "m3u8" in protocol:
            continue
        ext = str(fmt.get("ext") or "").lower()
        height = int(fmt.get("height") or 0)
        if height <= 0:
            continue
        if effective_max_height and height > effective_max_height:
            continue
        directness = 3 if ext == "mp4" else 2 if ext in ("webm", "m4v", "mov") else 1
        fmt = dict(fmt)
        fmt["_playervf_directness"] = directness
        usable.append(fmt)

    if not usable:
        return {}

    usable.sort(
        key=lambda fmt: (
            int(fmt.get("_playervf_directness") or 0),
            int(fmt.get("height") or 0),
            int(fmt.get("tbr") or 0),
        ),
        reverse=True,
    )
    return usable[0]


def _cached_stream_video_file(video_id: str, max_height: Optional[int]) -> Optional[Path]:
    label = _video_quality_label(max_height)
    stem = safe_filename(f"{video_id}.{label}", max_len=120)
    media_suffixes = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    for path in sorted(
        STREAM_VIDEO_CACHE_DIR.glob(stem + ".*"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    ):
        if path.suffix.lower() in media_suffixes and path.exists() and path.stat().st_size > 0:
            return path
    return None


def _stream_cache_stem(video_id: str, max_height: Optional[int]) -> Path:
    label = _video_quality_label(max_height)
    return STREAM_VIDEO_CACHE_DIR / safe_filename(f"{video_id}.{label}", max_len=120)


def _stream_cache_state_path(video_id: str, max_height: Optional[int]) -> Path:
    return _stream_cache_stem(video_id, max_height).with_suffix(".stream-cache.json")


def _stream_cache_raw_path(video_id: str, max_height: Optional[int]) -> Path:
    return _stream_cache_stem(video_id, max_height).with_suffix(".raw")


def _write_stream_cache_state(
    video_id: str,
    title: str,
    max_height: Optional[int],
    status: str,
    **extra: Any,
) -> None:
    STREAM_VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = _stream_cache_state_path(video_id, max_height)
    raw_path = _stream_cache_raw_path(video_id, max_height)
    payload: Dict[str, Any] = {
        "type": "playervf.youtubeStreamCache",
        "videoId": video_id,
        "title": title,
        "requestedHeight": int(max_height or 0),
        "qualityLabel": _video_quality_label(max_height),
        "status": status,
        "rawPath": str(raw_path),
        "statePath": str(state_path),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    temp_path.replace(state_path)


def _snapshot_partial_stream_file(path_text: str, raw_path: Path) -> int:
    if not path_text:
        return 0
    source = Path(path_text)
    candidates = [
        source,
        Path(str(source) + ".part"),
        Path(str(source) + ".ytdl"),
    ]
    existing = [path for path in candidates if path.exists() and path.is_file()]
    if not existing:
        return 0
    source = max(existing, key=lambda path: path.stat().st_size)
    if source.stat().st_size <= 0:
        return 0
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_path.exists() and raw_path.stat().st_size == source.stat().st_size:
        return raw_path.stat().st_size
    shutil.copyfile(source, raw_path)
    return raw_path.stat().st_size


def _start_background_stream_cache(
    video_id: str,
    title: str,
    max_height: Optional[int],
) -> None:
    max_height = int(max_height or MAX_STREAM_VIDEO_HEIGHT)
    if _cached_stream_video_file(video_id, max_height) is not None:
        return

    STREAM_VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = _stream_cache_state_path(video_id, max_height)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("status") in {"queued", "running"}:
                return
        except Exception:
            pass

    _write_stream_cache_state(
        video_id,
        title,
        max_height,
        "queued",
        message="Queued background stream cache.",
    )
    args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "cache-stream-video",
        "--video-id",
        video_id,
        "--title",
        title,
    ]
    args.extend(["--quality-height", str(int(max_height))])

    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def _prepare_stream_video_file(
    video_id: str,
    title: str,
    max_height: Optional[int],
) -> Tuple[Path, int]:
    import yt_dlp

    ffmpeg_location = _ffmpeg_location()
    if not ffmpeg_location:
        raise RuntimeError(
            "FFmpeg was not found, so this YouTube video cannot be merged for playback. "
            "Install ffmpeg or put ffmpeg.exe in PlayerVF tools/ffmpeg/bin."
        )

    cap = int(max_height or 0)
    cached = _cached_stream_video_file(video_id, cap)
    if cached is not None:
        _write_stream_cache_state(
            video_id,
            title,
            cap,
            "complete",
            finalPath=str(cached),
            message="Stream video cache already complete.",
        )
        return cached, cap

    STREAM_VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stem = _stream_cache_stem(video_id, cap)
    raw_path = _stream_cache_raw_path(video_id, cap)
    outtmpl = str(stem) + ".%(ext)s"
    for old_file in stem.parent.glob(stem.name + ".*"):
        if old_file.suffix.lower() in {".tmp"}:
            try:
                old_file.unlink()
            except Exception:
                pass

    _write_stream_cache_state(
        video_id,
        title,
        cap,
        "running",
        rawPath=str(raw_path),
        message="Caching YouTube stream video.",
    )

    def _cache_progress(status: Dict[str, Any]) -> None:
        filename = str(status.get("filename") or status.get("tmpfilename") or "")
        raw_bytes = _snapshot_partial_stream_file(filename, raw_path)
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        downloaded = status.get("downloaded_bytes") or raw_bytes
        percent = float(downloaded) / float(total) if total else None
        _write_stream_cache_state(
            video_id,
            title,
            cap,
            "running",
            rawPath=str(raw_path),
            tempPath=filename,
            downloadedBytes=int(downloaded or 0),
            totalBytes=int(total or 0),
            percent=percent,
            message="Caching YouTube stream video.",
        )

    ydl_opts: Dict[str, Any] = {
        "format": _video_download_format_selector(cap),
        "format_sort": ["res", "fps", "br"],
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "writethumbnail": False,
        "ignoreerrors": False,
        "overwrites": True,
        "continuedl": True,
        "ffmpeg_location": ffmpeg_location,
        "merge_output_format": "mp4",
        "progress_hooks": [_cache_progress],
    }
    downloaded_info: Dict[str, Any] = {}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            extracted = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            if isinstance(extracted, dict):
                downloaded_info = extracted
    except Exception as error:
        for partial in stem.parent.glob(stem.name + ".*"):
            if partial.suffix.lower() in {".part", ".ytdl", ".tmp"}:
                _snapshot_partial_stream_file(str(partial), raw_path)
        _write_stream_cache_state(
            video_id,
            title,
            cap,
            "incomplete",
            rawPath=str(raw_path),
            error=str(error),
            message="Background cache interrupted; .raw can be used to resume.",
        )
        raise

    media_suffixes = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    candidates = sorted(
        [path for path in stem.parent.glob(stem.name + ".*") if path.exists()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    media_files = [
        path for path in candidates
        if path.suffix.lower() in media_suffixes and path.stat().st_size > 0
    ]
    if not media_files:
        display_title = title or video_id
        _write_stream_cache_state(
            video_id,
            title,
            cap,
            "incomplete",
            rawPath=str(raw_path),
            message=f"FFmpeg video cache was not created for {display_title}.",
        )
        raise FileNotFoundError(f"FFmpeg video cache was not created for {display_title}.")
    resolved_height = cap
    for requested in downloaded_info.get("requested_downloads") or []:
        if not isinstance(requested, dict):
            continue
        height = int(requested.get("height") or 0)
        if height > resolved_height:
            resolved_height = height
    if resolved_height <= 0:
        selected = _select_video_format(downloaded_info, None)
        resolved_height = int(selected.get("height") or 0) if isinstance(selected, dict) else 0
    if raw_path.exists():
        try:
            raw_path.unlink()
        except Exception:
            pass
    _write_stream_cache_state(
        video_id,
        title,
        cap,
        "complete",
        rawPath=str(raw_path),
        finalPath=str(media_files[0]),
        resolvedHeight=resolved_height,
        message="Stream video cache complete.",
    )
    return media_files[0], resolved_height


def _select_audio_format(info: Dict[str, Any]) -> Dict[str, Any]:
    formats = info.get("formats") or []
    usable: List[Dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if not fmt.get("url"):
            continue
        if fmt.get("acodec") in (None, "none"):
            continue
        if fmt.get("vcodec") not in (None, "none"):
            continue
        usable.append(fmt)

    if not usable:
        return info

    def score(fmt: Dict[str, Any]) -> Tuple[int, int]:
        ext = str(fmt.get("ext") or "").lower()
        preferred = 3 if ext == "m4a" else 2 if ext in ("webm", "opus", "ogg") else 1
        bitrate = int(fmt.get("abr") or fmt.get("tbr") or 0)
        return preferred, bitrate

    usable.sort(key=score, reverse=True)
    return usable[0]


def _stream_http_headers(info: Dict[str, Any], selected_format: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for source in (info.get("http_headers"), selected_format.get("http_headers")):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if key_text and value_text:
                headers[key_text] = value_text
    return headers


def _collect_video_qualities(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_height: Dict[int, Dict[str, Any]] = {}
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        if not fmt.get("url"):
            continue
        if fmt.get("vcodec") in (None, "none"):
            continue
        height = int(fmt.get("height") or 0)
        if height <= 0:
            continue
        current = by_height.get(height)
        has_audio = fmt.get("acodec") not in (None, "none")
        current_has_audio = current and current.get("acodec") not in (None, "none")
        if (
            current is None
            or (has_audio and not current_has_audio)
            or (
                has_audio == current_has_audio
                and int(fmt.get("tbr") or 0) > int(current.get("tbr") or 0)
            )
        ):
            by_height[height] = fmt

    qualities: List[Dict[str, Any]] = []
    for height, fmt in sorted(by_height.items(), reverse=True):
        qualities.append(
            {
                "label": _video_quality_label(height),
                "height": height,
                "url": fmt.get("url", ""),
                "formatId": str(fmt.get("format_id") or ""),
                "ext": str(fmt.get("ext") or ""),
                "hasAudio": fmt.get("acodec") not in (None, "none"),
                "streamable": height <= MAX_STREAM_VIDEO_HEIGHT,
            }
        )
    return qualities


def _collect_subtitles(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    subtitles: List[Dict[str, Any]] = []

    def add_tracks(source: Dict[str, Any], automatic: bool) -> None:
        for language, tracks in (source or {}).items():
            if not isinstance(tracks, list):
                continue
            best = None
            for track in tracks:
                if not isinstance(track, dict) or not track.get("url"):
                    continue
                ext = str(track.get("ext") or "").lower()
                if best is None or ext in ("vtt", "srt"):
                    best = track
                    if ext == "vtt":
                        break
            if best is None:
                continue
            subtitles.append(
                {
                    "language": str(language),
                    "label": f"{language}{' auto' if automatic else ''}",
                    "url": best.get("url", ""),
                    "automatic": automatic,
                }
            )

    add_tracks(info.get("subtitles") or {}, False)
    add_tracks(info.get("automatic_captions") or {}, True)

    seen = set()
    deduped = []
    preferred_prefixes = ("en", "cs", "sk")
    for item in sorted(
        subtitles,
        key=lambda sub: (
            0 if str(sub.get("language", "")).lower().startswith(preferred_prefixes) else 1,
            1 if sub.get("automatic") else 0,
            str(sub.get("language", "")),
        ),
    ):
        key = (item.get("language"), item.get("automatic"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:20]


def stream_youtube_music(
    item_json: Any,
    quality_height: Optional[int] = None,
    audio_only: bool = False,
    stream_cache_enabled: Optional[bool] = None,
) -> str:
    """Resolve a playable audio stream URL for a search result."""
    if isinstance(item_json, str):
        item_data = json.loads(item_json)
    else:
        item_data = dict(item_json or {})

    item = _search_item_from_dict(item_data)
    result_type = str(item.result_type or item_data.get("resultType") or "").lower()
    video_id = item.video_id or item.raw.get("videoId")

    if not video_id and result_type in ("album", "playlist", "featured_playlist", "community_playlist"):
        downloader = MediaDownloader()
        entity = downloader.get_entity_details(item)
        tracks = (
            downloader.get_album_tracks(entity)
            if result_type == "album"
            else downloader.get_playlist_tracks(entity)
        )
        first_track = next((track for track in tracks if track.get("videoId")), None)
        if first_track:
            video_id = first_track.get("videoId")
            item.raw = first_track
            title, artist, album, _ = downloader.extract_track_fields(first_track)
            item.title = title or item.title
            item.artist = artist or item.artist

    if not video_id:
        raise ValueError("This result cannot be streamed because it has no videoId.")

    is_video = bool(video_id) and not audio_only
    should_cache_stream = _stream_cache_enabled() if stream_cache_enabled is None else stream_cache_enabled

    import yt_dlp

    ydl_opts = {
        "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best" if is_video else "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)

    info_thumbnails = {"thumbnails": info.get("thumbnails") or [{"url": info.get("thumbnail", "")}]}
    preview_thumb = (
        item.thumbnail_url
        or best_thumbnail(item.raw or {})
        or best_thumbnail(info_thumbnails)
        or info.get("thumbnail", "")
    )
    title = item.title or info.get("title", "YouTube Music")
    artist = item.artist or info.get("uploader", "YouTube Music")
    title_romaji = item.title_romaji or romanize_cjk_text(title)
    artist_romaji = item.artist_romaji or romanize_cjk_text(artist)

    if is_video:
        if quality_height and quality_height > MAX_STREAM_VIDEO_HEIGHT:
            raise ValueError(
                f"{_video_quality_label(quality_height)} is download-only. "
                f"Streaming supports up to {_video_quality_label(MAX_STREAM_VIDEO_HEIGHT)}."
            )
        stream_cache_height = int(quality_height or MAX_STREAM_VIDEO_HEIGHT)
        selected_format: Dict[str, Any] = {}
        cache_path = (
            _cached_stream_video_file(str(video_id), stream_cache_height)
            if should_cache_stream
            else None
        )
        if cache_path is not None:
            selected_format = {
                "url": str(cache_path),
                "height": stream_cache_height,
                "ext": cache_path.suffix.lstrip("."),
                "http_headers": {},
                "_playervf_cache_status": "complete",
            }
        else:
            selected_format = _select_direct_video_format(info, quality_height)
            if selected_format:
                selected_format["_playervf_cache_status"] = (
                    "background" if should_cache_stream else "disabled"
                )
                if should_cache_stream:
                    try:
                        _start_background_stream_cache(str(video_id), str(title), quality_height)
                    except Exception:
                        pass

        if not selected_format:
            if not should_cache_stream:
                selected_format = _select_direct_video_format(info, quality_height)
                if not selected_format:
                    raise ValueError("This YouTube video has no direct streamable format. Enable stream caching or download it first.")
                selected_format["_playervf_cache_status"] = "disabled"
            else:
                try:
                    video_file, resolved_height = _prepare_stream_video_file(
                        str(video_id),
                        str(title),
                        stream_cache_height,
                    )
                    selected_format = {
                        "url": str(video_file),
                        "height": resolved_height,
                        "ext": video_file.suffix.lstrip("."),
                        "http_headers": {},
                        "_playervf_cache_status": "complete",
                    }
                except Exception:
                    selected_format = _select_direct_video_format(info, quality_height)
                    if not selected_format:
                        raise
                    selected_format["_playervf_cache_status"] = "incomplete"
    else:
        selected_format = _select_audio_format(info)

    cache_state_path = _stream_cache_state_path(str(video_id), stream_cache_height) if is_video else None
    cache_raw_path = _stream_cache_raw_path(str(video_id), stream_cache_height) if is_video else None
    payload = {
        "url": selected_format.get("url", "") or info.get("url", ""),
        "title": title,
        "artist": artist,
        "titleRomaji": title_romaji,
        "artistRomaji": artist_romaji,
        "displayTitle": display_with_romanization(title, title_romaji),
        "displayArtist": display_with_romanization(artist, artist_romaji),
        "album": item.raw.get("album", {}).get("name", "") if isinstance(item.raw.get("album"), dict) else "",
        "thumbnailUrl": preview_thumb,
        "httpHeaders": _stream_http_headers(info, selected_format),
        "durationSeconds": extract_duration_seconds(item.raw or {}, info) or 0,
        "videoId": video_id,
        "isVideo": is_video,
        "qualityLabel": _video_quality_label(selected_format.get("height")) if is_video else "Audio",
        "qualities": _collect_video_qualities(info) if is_video else [],
        "subtitles": _collect_subtitles(info) if is_video else [],
        "cacheStatus": selected_format.get("_playervf_cache_status", ""),
        "cacheStatePath": str(cache_state_path) if cache_state_path else "",
        "cacheRawPath": str(cache_raw_path) if cache_raw_path else "",
    }
    return json.dumps(payload, ensure_ascii=True)


def _emit_progress(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True), file=sys.stderr, flush=True)


def _parse_int_list(value: Optional[str]) -> List[int]:
    if not value:
        return []
    result: List[int] = []
    for part in str(value).split(","):
        try:
            parsed = int(part.strip())
        except Exception:
            continue
        if parsed > 0:
            result.append(parsed)
    return result


def _parse_text_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _main() -> int:
    parser = argparse.ArgumentParser(description="PlayerVf YouTube Music bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("a", type=int)
    add_parser.add_argument("b", type=int)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--filter", default="songs")
    search_parser.add_argument("--limit", type=int, default=20)

    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("--item-json", required=True)
    download_parser.add_argument("--output-dir", required=True)
    download_parser.add_argument("--video", action="store_true")
    download_parser.add_argument("--quality-height", type=int, default=None)
    download_parser.add_argument("--quality-heights", default=None)
    download_parser.add_argument("--write-subtitles", action="store_true")
    download_parser.add_argument("--subtitle-lang", default=None)
    download_parser.add_argument("--subtitle-langs", default=None)
    download_parser.add_argument("--auto-subtitles", action="store_true")
    download_parser.add_argument("--subtitles-only", action="store_true")

    stream_parser = subparsers.add_parser("stream")
    stream_parser.add_argument("--item-json", required=True)
    stream_parser.add_argument("--quality-height", type=int, default=None)
    stream_parser.add_argument("--audio-only", action="store_true")
    stream_parser.add_argument("--no-stream-cache", action="store_true")

    cache_stream_parser = subparsers.add_parser("cache-stream-video")
    cache_stream_parser.add_argument("--video-id", required=True)
    cache_stream_parser.add_argument("--title", default="")
    cache_stream_parser.add_argument("--quality-height", type=int, default=None)

    args = parser.parse_args()
    if args.command == "add":
        print(add(args.a, args.b))
    elif args.command == "search":
        print(search_youtube_music(args.query, args.filter, args.limit))
    elif args.command == "download":
        print(download_youtube_music(
            args.item_json,
            args.output_dir,
            video=args.video,
            quality_height=args.quality_height,
            quality_heights=_parse_int_list(args.quality_heights),
            write_subtitles=args.write_subtitles,
            subtitle_lang=args.subtitle_lang,
            subtitle_langs=_parse_text_list(args.subtitle_langs),
            auto_subtitles=args.auto_subtitles,
            subtitles_only=args.subtitles_only,
        ))
    elif args.command == "stream":
        print(stream_youtube_music(
            args.item_json,
            args.quality_height,
            audio_only=args.audio_only,
            stream_cache_enabled=not args.no_stream_cache,
        ))
    elif args.command == "cache-stream-video":
        path, height = _prepare_stream_video_file(
            args.video_id,
            args.title,
            args.quality_height,
        )
        print(json.dumps({
            "path": str(path),
            "height": height,
            "statePath": str(_stream_cache_state_path(args.video_id, args.quality_height)),
        }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
