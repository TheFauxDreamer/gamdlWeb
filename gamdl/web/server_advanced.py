"""Web UI server for gamdl using FastAPI."""

import asyncio
import json
import logging
import os
import sys
import uuid
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

import httpx

# Platform-specific file locking
if sys.platform == 'win32':
    # On Windows, skip file locking for JSON config (single-user app, low risk)
    def lock_file(file_obj, exclusive=False):
        """No-op on Windows - file locking not critical for single-user config"""
        pass

    def unlock_file(file_obj):
        """No-op on Windows"""
        pass
else:
    import fcntl

    def lock_file(file_obj, exclusive=False):
        """Lock file on Unix/Linux"""
        lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(file_obj.fileno(), lock_type)

    def unlock_file(file_obj):
        """Unlock file on Unix/Linux"""
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gamdl.api.apple_music_api import AppleMusicApi
from gamdl.api.itunes_api import ItunesApi
from gamdl.downloader.downloader import AppleMusicDownloader
from gamdl.downloader.downloader_base import AppleMusicBaseDownloader
from gamdl.downloader.downloader_song import AppleMusicSongDownloader
from gamdl.downloader.downloader_music_video import AppleMusicMusicVideoDownloader
from gamdl.downloader.downloader_uploaded_video import AppleMusicUploadedVideoDownloader
from gamdl.downloader.enums import DownloadMode, RemuxMode
from gamdl.downloader.exceptions import (
    DependencyMissing,
    ExecutableNotFound,
    GamdlError,
    MediaFileExists,
    NotStreamable,
    FormatNotAvailable,
)
from gamdl.downloader.types import DownloadItem
from gamdl.interface.enums import SongCodec, MusicVideoResolution, CoverFormat
from gamdl.interface.interface import AppleMusicInterface
from gamdl.interface.interface_song import AppleMusicSongInterface
from gamdl.interface.interface_music_video import AppleMusicMusicVideoInterface
from gamdl.interface.interface_uploaded_video import AppleMusicUploadedVideoInterface

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="gamdl Web UI")

# Store active download sessions
active_sessions: Dict[str, dict] = {}

# Store cancellation flags for sessions
cancellation_flags: Dict[str, bool] = {}

# Queue data structures
class QueueItemStatus(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class QueueItem:
    """Represents a single item in the download queue"""
    id: str  # UUID for this queue item
    session_id: Optional[str]  # Session UUID (will be created when item starts downloading)
    download_request: "DownloadRequest"
    status: QueueItemStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    display_title: str = "Unknown"
    display_type: str = "url"  # "url", "album", "playlist", "song"
    url_count: int = 1
    progress_current: int = 0  # Current track/item being processed
    progress_total: int = 0  # Total tracks/items to process

# Global queue state
download_queue: list[QueueItem] = []
queue_lock = threading.Lock()
queue_paused: bool = False
queue_processor_running: bool = False
current_downloading_item: Optional[QueueItem] = None
websocket_clients: list[WebSocket] = []  # For broadcasting queue updates


class DownloadRequest(BaseModel):
    urls: list[str]
    cookies_path: Optional[str] = None
    output_path: Optional[str] = None
    temp_path: Optional[str] = None

    # Common options
    final_path_template: Optional[str] = None
    cover_format: Optional[str] = None
    cover_size: Optional[int] = None
    song_codec: Optional[str] = None
    music_video_codec: Optional[str] = None
    music_video_resolution: Optional[str] = None

    # Metadata options
    no_cover: bool = False
    no_lyrics: bool = False
    extra_tags: bool = False

    # Podcast-specific
    podcast_name: Optional[str] = None
    episode_title: Optional[str] = None
    episode_date: Optional[str] = None

    # Retry & delay settings
    enable_retry_delay: bool = True  # Enable/disable retry and delay features
    max_retries: int = 3  # Number of retry attempts
    retry_delay: int = 60  # Seconds to wait between retries

    # Sleep/delay settings
    song_delay: float = 0.0  # Seconds to wait after each song
    queue_item_delay: float = 0.0  # Seconds to wait after each queue item (album/playlist)

    # Queue behavior
    continue_on_error: bool = False  # Continue queue processing even if items fail

    # Display customization (optional)
    display_title: Optional[str] = None  # Custom display name for queue
    display_type: Optional[str] = None  # Custom display type (e.g., "Discography", "Artist")


class SessionResponse(BaseModel):
    session_id: str
    status: str
    message: str


# WebUI Configuration Management

# Config file location
CONFIG_DIR = Path.home() / ".gamdl"
CONFIG_FILE = CONFIG_DIR / "webui_config.json"


def load_webui_config() -> dict:
    """Load webUI configuration from disk."""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load webUI config: {e}")
    return {}


def save_webui_config(config: dict):
    """Save webUI configuration to disk."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        logger.info(f"Saved webUI config to {CONFIG_FILE}")
    except Exception as e:
        logger.error(f"Failed to save webUI config: {e}")


def get_preferred_cookies_path() -> str:
    """Get user's preferred cookies path from config, or default."""
    config = load_webui_config()
    cookies_path = config.get('cookies_path')

    if cookies_path and cookies_path.strip():
        logger.info(f"Using cookies path from config: {cookies_path}")
        return cookies_path

    default_path = str(Path.home() / ".gamdl" / "cookies.txt")
    logger.info(f"No config found, using default cookies path: {default_path}")
    return default_path


async def initialize_api_from_cookies(cookies_path: str = None) -> bool:
    """
    Initialize Apple Music API from cookies file.
    Returns True if successful, False otherwise.
    """
    # Check if already initialized
    if hasattr(app.state, "api") and app.state.api is not None:
        logger.info("API already initialized")
        return True

    # Get cookies path
    if not cookies_path:
        cookies_path = get_preferred_cookies_path()

    # Expand ~ if present
    cookies_path = cookies_path.strip()
    if cookies_path.startswith("~"):
        cookies_path = str(Path(cookies_path).expanduser())

    # Check if cookies file exists
    cookies_file = Path(cookies_path)
    if not cookies_file.exists():
        logger.warning(f"Cookies file not found at {cookies_path}")
        return False

    try:
        # Initialize API
        api = await AppleMusicApi.create_from_netscape_cookies(
            cookies_path=cookies_path,
        )

        # Store globally
        app.state.api = api
        logger.info(f"Apple Music API initialized successfully from {cookies_path}")
        return True

    except Exception as e:
        logger.error(f"Failed to initialize API: {e}")
        return False


# =============================================================================
# Playlist Monitoring Functions
# =============================================================================

# Monitor config file location
MONITOR_CONFIG_PATH = Path.home() / ".gamdl" / "monitor_config.json"

# Global scheduler instance
monitor_scheduler: Optional[AsyncIOScheduler] = None


def load_monitor_config() -> dict:
    """Load monitor config with file lock."""
    if not MONITOR_CONFIG_PATH.exists():
        return {
            "enabled": False,
            "check_interval_minutes": 60,
            "monitored_playlist": None,
            "tracked_track_ids": [],
            "activity_log": []
        }

    try:
        with open(MONITOR_CONFIG_PATH, 'r') as f:
            lock_file(f, exclusive=False)
            config = json.load(f)
            unlock_file(f)
        return config
    except Exception as e:
        logger.error(f"Failed to load monitor config: {e}")
        return {
            "enabled": False,
            "check_interval_minutes": 60,
            "monitored_playlist": None,
            "tracked_track_ids": [],
            "activity_log": []
        }


def save_monitor_config(config: dict):
    """Save monitor config with atomic write."""
    try:
        MONITOR_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file first
        temp_path = MONITOR_CONFIG_PATH.with_suffix('.tmp')
        with open(temp_path, 'w') as f:
            lock_file(f, exclusive=True)
            json.dump(config, f, indent=2)
            unlock_file(f)

        # Atomic rename
        temp_path.replace(MONITOR_CONFIG_PATH)
        logger.info(f"Saved monitor config to {MONITOR_CONFIG_PATH}")
    except Exception as e:
        logger.error(f"Failed to save monitor config: {e}")


def log_monitor_event(event_type: str, message: str, track_ids: list = None):
    """Add event to activity log (keep last 100)."""
    try:
        config = load_monitor_config()

        event = {
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "event_type": event_type,
            "message": message
        }
        if track_ids:
            event["track_ids"] = track_ids

        config.setdefault('activity_log', []).insert(0, event)
        config['activity_log'] = config['activity_log'][:100]  # Keep last 100

        save_monitor_config(config)
    except Exception as e:
        logger.error(f"Failed to log monitor event: {e}")


async def fetch_playlist_track_ids(playlist_info: dict) -> list[str]:
    """Fetch current track IDs from Apple Music API."""
    try:
        # Initialize API if needed
        if not hasattr(app.state, "api") or app.state.api is None:
            success = await initialize_api_from_cookies()
            if not success:
                raise Exception("Failed to initialize Apple Music API")

        api = app.state.api

        # Fetch playlist data
        if playlist_info['playlist_type'] == 'library':
            # Library playlist
            response = await api.get_library_playlist(playlist_info['playlist_id'])
        else:
            # Catalog playlist
            response = await api.get_playlist(playlist_info['playlist_id'])

        # Extract track IDs from playlist data
        track_ids = []
        tracks_data = response.get('data', [{}])[0].get('relationships', {}).get('tracks', {}).get('data', [])

        for track in tracks_data:
            track_ids.append(track['id'])

        logger.info(f"Fetched {len(track_ids)} tracks from playlist {playlist_info['playlist_name']}")
        return track_ids

    except Exception as e:
        logger.error(f"Failed to fetch playlist tracks: {e}")
        raise


async def handle_new_tracks(playlist_info: dict, new_track_ids: set):
    """Queue new tracks for download with proper song metadata."""
    logger.info(f"Found {len(new_track_ids)} new tracks in monitored playlist '{playlist_info['playlist_name']}'")

    # Check if API is initialized
    if not hasattr(app.state, "api") or app.state.api is None:
        logger.warning("API not initialized, queueing tracks without metadata")
        # Fallback: queue all tracks with track IDs as titles
        for track_id in new_track_ids:
            await _queue_track_without_metadata(track_id, playlist_info)
        log_monitor_event('new_tracks', f'Found {len(new_track_ids)} new tracks (no metadata)', list(new_track_ids))
        return

    api = app.state.api

    # Fetch metadata and queue each track
    for track_id in new_track_ids:
        try:
            # Fetch song metadata from Apple Music API using appropriate endpoint
            # Use library or catalog API based on playlist type
            if playlist_info['playlist_type'] == 'library':
                song_data = await api.get_library_song(track_id, extend="", include="")
            else:
                song_data = await api.get_song(track_id, extend="", include="")

            if song_data and song_data.get("data"):
                song_attrs = song_data["data"][0]["attributes"]
                song_title = song_attrs.get("name", "Unknown Song")
                artist_name = song_attrs.get("artistName", "Unknown Artist")
                display_title = f"{song_title} - {artist_name}"
                logger.info(f"Queueing monitored track: {display_title}")
            else:
                # Metadata fetch returned empty
                display_title = f"Monitored: {track_id}"
                logger.warning(f"Empty metadata for track {track_id}, using ID as title")

        except Exception as e:
            # Metadata fetch failed, use track ID as fallback
            display_title = f"Monitored: {track_id}"
            logger.warning(f"Failed to fetch metadata for track {track_id}: {e}. Using ID as title.")

        # Build track URL based on playlist type
        if playlist_info['playlist_type'] == 'library':
            # Library songs need /library/songs/ prefix
            # Also need to get storefront from API
            api = app.state.api
            storefront = api.storefront
            track_url = f"https://music.apple.com/{storefront}/library/songs/{track_id}"
        else:
            # Catalog songs use /song/ prefix
            track_url = f"https://music.apple.com/us/song/{track_id}"

        # Get webUI config for default settings
        config = load_webui_config()

        # Helper to convert empty strings to None
        def clean_config_value(key, default=None):
            value = config.get(key, default)
            if isinstance(value, str) and value.strip() == "":
                return None
            return value

        # Create download request with default settings
        download_request = DownloadRequest(
            urls=[track_url],
            cookies_path=clean_config_value('cookies_path'),
            output_path=clean_config_value('output_path'),
            temp_path=clean_config_value('temp_path'),
            final_path_template=clean_config_value('final_path_template'),
            cover_format=clean_config_value('cover_format'),
            cover_size=clean_config_value('cover_size'),
            song_codec=clean_config_value('song_codec'),
            music_video_codec=clean_config_value('music_video_codec'),
            music_video_resolution=clean_config_value('music_video_resolution'),
            no_cover=config.get('no_cover', False),
            no_lyrics=config.get('no_lyrics', False),
            extra_tags=config.get('extra_tags', False),
            enable_retry_delay=config.get('enable_retry_delay', True),
            max_retries=config.get('max_retries', 3),
            retry_delay=config.get('retry_delay', 60),
            song_delay=config.get('song_delay', 0.0),
            queue_item_delay=config.get('queue_item_delay', 0.0),
            continue_on_error=config.get('continue_on_error', False),
        )

        # Add to queue with proper display info
        try:
            display_info = {"title": display_title, "type": "Song"}
            add_to_queue(download_request, display_info)
            logger.info(f"Added track {track_id} to download queue")
        except Exception as e:
            logger.error(f"Failed to add track {track_id} to queue: {e}")

    # Log event
    log_monitor_event('new_tracks', f'Found {len(new_track_ids)} new tracks', list(new_track_ids))


async def _queue_track_without_metadata(track_id: str, playlist_info: dict):
    """Fallback function to queue track without metadata (for error cases)."""
    # Build track URL based on playlist type
    if playlist_info['playlist_type'] == 'library':
        # Library songs need /library/songs/ prefix
        # Need to initialize API to get storefront
        if not hasattr(app.state, "api") or app.state.api is None:
            await initialize_api_from_cookies()
        api = app.state.api
        storefront = api.storefront
        track_url = f"https://music.apple.com/{storefront}/library/songs/{track_id}"
    else:
        # Catalog songs use /song/ prefix
        track_url = f"https://music.apple.com/us/song/{track_id}"

    config = load_webui_config()

    # Helper to convert empty strings to None
    def clean_config_value(key, default=None):
        value = config.get(key, default)
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    download_request = DownloadRequest(
        urls=[track_url],
        cookies_path=clean_config_value('cookies_path'),
        output_path=clean_config_value('output_path'),
        temp_path=clean_config_value('temp_path'),
        final_path_template=clean_config_value('final_path_template'),
        cover_format=clean_config_value('cover_format'),
        cover_size=clean_config_value('cover_size'),
        song_codec=clean_config_value('song_codec'),
        music_video_codec=clean_config_value('music_video_codec'),
        music_video_resolution=clean_config_value('music_video_resolution'),
        no_cover=config.get('no_cover', False),
        no_lyrics=config.get('no_lyrics', False),
        extra_tags=config.get('extra_tags', False),
        enable_retry_delay=config.get('enable_retry_delay', True),
        max_retries=config.get('max_retries', 3),
        retry_delay=config.get('retry_delay', 60),
        song_delay=config.get('song_delay', 0.0),
        queue_item_delay=config.get('queue_item_delay', 0.0),
        continue_on_error=config.get('continue_on_error', False),
    )

    try:
        display_info = {"title": f"Monitored: {track_id}", "type": "Song"}
        add_to_queue(download_request, display_info)
        logger.info(f"Added track {track_id} to download queue (without metadata)")
    except Exception as e:
        logger.error(f"Failed to add track {track_id} to queue: {e}")


def log_removed_tracks(playlist_info: dict, removed_track_ids: set):
    """Log removed tracks (no action taken)."""
    logger.info(f"Detected {len(removed_track_ids)} tracks removed from monitored playlist '{playlist_info['playlist_name']}'")
    log_monitor_event('removed_tracks', f'Detected {len(removed_track_ids)} removed tracks', list(removed_track_ids))


async def check_monitored_playlist():
    """Hourly job: Check monitored playlist for changes."""
    try:
        config = load_monitor_config()

        # Skip if monitoring is disabled or no playlist configured
        if not config.get('enabled') or not config.get('monitored_playlist'):
            logger.debug("Monitoring disabled or no playlist configured, skipping check")
            return

        playlist = config['monitored_playlist']
        logger.info(f"Checking monitored playlist '{playlist['playlist_name']}'")

        # Fetch current track IDs
        current_track_ids = await fetch_playlist_track_ids(playlist)
        known_track_ids = set(config.get('tracked_track_ids', []))

        # Detect new tracks
        new_track_ids = set(current_track_ids) - known_track_ids
        if new_track_ids:
            await handle_new_tracks(playlist, new_track_ids)
            # Update tracked IDs
            config['tracked_track_ids'] = current_track_ids

        # Detect removed tracks
        removed_track_ids = known_track_ids - set(current_track_ids)
        if removed_track_ids:
            log_removed_tracks(playlist, removed_track_ids)
            # Update tracked IDs
            config['tracked_track_ids'] = current_track_ids

        # Update last checked timestamp
        config['monitored_playlist']['last_checked_at'] = datetime.utcnow().isoformat() + 'Z'
        save_monitor_config(config)

        logger.info(f"Completed check for playlist '{playlist['playlist_name']}' - {len(new_track_ids)} new, {len(removed_track_ids)} removed")

    except Exception as e:
        logger.error(f"Monitor check failed: {e}", exc_info=True)
        # Log error event
        try:
            log_monitor_event('error', f'Monitor check failed: {str(e)}')
        except:
            pass


def start_monitor_scheduler():
    """Initialize and start the monitoring scheduler."""
    global monitor_scheduler

    try:
        if monitor_scheduler is not None:
            logger.info("Monitor scheduler already running")
            return

        monitor_scheduler = AsyncIOScheduler()
        monitor_scheduler.add_job(
            check_monitored_playlist,
            trigger=IntervalTrigger(minutes=60),
            id='playlist_monitor',
            replace_existing=True
        )
        monitor_scheduler.start()
        logger.info("Monitor scheduler started successfully (checking every 60 minutes)")
    except Exception as e:
        logger.error(f"Failed to start monitor scheduler: {e}", exc_info=True)


def stop_monitor_scheduler():
    """Stop the monitoring scheduler."""
    global monitor_scheduler

    try:
        if monitor_scheduler is not None:
            monitor_scheduler.shutdown(wait=False)
            monitor_scheduler = None
            logger.info("Monitor scheduler stopped")
    except Exception as e:
        logger.error(f"Failed to stop monitor scheduler: {e}")


# Queue Management Functions

def extract_display_info_from_url(url: str) -> dict:
    """Extract display information from an Apple Music URL."""
    # Parse the URL to extract type and ID
    # Example URLs:
    # https://music.apple.com/us/album/album-name/123456
    # https://music.apple.com/us/playlist/playlist-name/pl.u-123456
    # https://music.apple.com/us/song/song-name/123456

    import re

    # Default values
    display_type = "URL"
    display_title = url

    # Try to extract type from URL
    if '/album/' in url:
        display_type = "Album"
        # Try to extract album name from URL
        match = re.search(r'/album/([^/]+)/', url)
        if match:
            # Decode URL encoding and replace hyphens with spaces
            name = match.group(1).replace('-', ' ')
            # Decode URL encoding
            from urllib.parse import unquote
            display_title = unquote(name).title()
    elif '/playlist/' in url:
        display_type = "Playlist"
        match = re.search(r'/playlist/([^/]+)/', url)
        if match:
            name = match.group(1).replace('-', ' ')
            from urllib.parse import unquote
            display_title = unquote(name).title()
    elif '/song/' in url:
        display_type = "Song"
        match = re.search(r'/song/([^/]+)/', url)
        if match:
            name = match.group(1).replace('-', ' ')
            from urllib.parse import unquote
            display_title = unquote(name).title()
    elif '/music-video/' in url:
        display_type = "Music Video"
        match = re.search(r'/music-video/([^/]+)/', url)
        if match:
            name = match.group(1).replace('-', ' ')
            from urllib.parse import unquote
            display_title = unquote(name).title()

    return {"title": display_title, "type": display_type}


def add_to_queue(download_request: DownloadRequest, display_info: Optional[dict] = None) -> str:
    """Add a download request to the queue. Returns queue item ID."""
    global queue_processor_running

    with queue_lock:
        item_id = str(uuid.uuid4())

        # Extract display information
        display_title = "Unknown"
        display_type = "url"
        url_count = len(download_request.urls)

        if display_info:
            display_title = display_info.get("title", "Unknown")
            display_type = display_info.get("type", "url")

        queue_item = QueueItem(
            id=item_id,
            session_id=None,  # Will be set when downloading starts
            download_request=download_request,
            status=QueueItemStatus.QUEUED,
            created_at=datetime.now(),
            display_title=display_title,
            display_type=display_type,
            url_count=url_count
        )

        download_queue.append(queue_item)
        logger.info(f"Added item {item_id} to queue: {display_title}")

        # Start queue processor if not running
        if not queue_processor_running:
            asyncio.create_task(process_queue())

        return item_id


def remove_from_queue(item_id: str) -> bool:
    """Remove an item from the queue. Returns True if successful."""
    with queue_lock:
        for i, item in enumerate(download_queue):
            if item.id == item_id:
                # If item is currently downloading, cancel it
                if item.status == QueueItemStatus.DOWNLOADING:
                    if item.session_id:
                        cancellation_flags[item.session_id] = True
                    item.status = QueueItemStatus.CANCELLED
                else:
                    # Remove from queue
                    download_queue.pop(i)
                    logger.info(f"Removed item {item_id} from queue")
                return True
        return False


def get_queue_status() -> dict:
    """Get current queue status."""
    with queue_lock:
        return {
            "items": [
                {
                    "id": item.id,
                    "status": item.status.value,
                    "display_title": item.display_title,
                    "display_type": item.display_type,
                    "url_count": item.url_count,
                    "created_at": item.created_at.isoformat(),
                    "started_at": item.started_at.isoformat() if item.started_at else None,
                    "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                    "error_message": item.error_message,
                    "progress_current": item.progress_current,
                    "progress_total": item.progress_total,
                }
                for item in download_queue
            ],
            "paused": queue_paused,
            "current_item_id": current_downloading_item.id if current_downloading_item else None,
        }


def pause_queue():
    """Pause the queue processor."""
    global queue_paused
    with queue_lock:
        queue_paused = True
        logger.info("Queue paused")


def resume_queue():
    """Resume the queue processor."""
    global queue_paused, queue_processor_running
    with queue_lock:
        queue_paused = False
        logger.info("Queue resumed")

        # Restart processor if it stopped
        if not queue_processor_running:
            asyncio.create_task(process_queue())


async def wait_for_websocket(session_id: str, timeout: float) -> Optional[WebSocket]:
    """Wait for a WebSocket to connect to this session."""
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        if session_id in active_sessions and active_sessions[session_id].get("websocket"):
            return active_sessions[session_id]["websocket"]
        await asyncio.sleep(0.1)
    return None


async def broadcast_queue_update():
    """Broadcast queue status to all connected WebSocket clients."""
    queue_status = get_queue_status()
    message = {
        "type": "queue_update",
        "data": queue_status
    }

    # Send to all connected clients
    dead_clients = []
    for ws in websocket_clients:
        try:
            await ws.send_json(message)
        except:
            dead_clients.append(ws)

    # Remove dead clients
    for ws in dead_clients:
        websocket_clients.remove(ws)


async def process_queue():
    """Background task that processes the queue sequentially."""
    global queue_processor_running, current_downloading_item, queue_paused

    queue_processor_running = True
    logger.info("Queue processor started")

    try:
        while True:
            # Wait if paused
            if queue_paused:
                await asyncio.sleep(1)
                continue

            # Get next queued item
            next_item = None
            with queue_lock:
                logger.info(f"Queue processor checking queue: {len(download_queue)} total items")
                for item in download_queue:
                    logger.info(f"  Item {item.id}: status={item.status.value}, title={item.display_title}")
                    if item.status == QueueItemStatus.QUEUED:
                        next_item = item
                        break

            if not next_item:
                # No more items to process
                logger.info("No QUEUED items found, sleeping 2 seconds")
                await asyncio.sleep(2)

                # Check if queue is truly empty
                with queue_lock:
                    has_queued = any(item.status == QueueItemStatus.QUEUED for item in download_queue)
                    logger.info(f"After sleep: has_queued={has_queued}")
                    if not has_queued:
                        queue_processor_running = False
                        logger.info("Queue processor stopping (no items)")
                        break
                continue

            # Process the item
            with queue_lock:
                next_item.status = QueueItemStatus.DOWNLOADING
                next_item.started_at = datetime.now()
                next_item.session_id = str(uuid.uuid4())
                current_downloading_item = next_item

            logger.info(f"Processing queue item: {next_item.display_title}")

            # Broadcast queue update to all connected WebSocket clients
            await broadcast_queue_update()

            # Create session and start download
            session_id = next_item.session_id
            active_sessions[session_id] = {
                "status": "running",
                "request": next_item.download_request,
                "logs": [],
                "websocket": None,  # Will be set if client connects
                "queue_item_id": next_item.id,
            }
            cancellation_flags[session_id] = False

            # Wait for WebSocket connection (with timeout)
            websocket = await wait_for_websocket(session_id, timeout=5.0)

            if websocket:
                # Run download with WebSocket updates
                try:
                    await run_download_session(session_id, active_sessions[session_id], websocket)

                    # Success - apply queue item delay if configured
                    request = next_item.download_request
                    enable_retry_delay = getattr(request, 'enable_retry_delay', True)
                    queue_item_delay = getattr(request, 'queue_item_delay', 0.0) if enable_retry_delay else 0.0

                    with queue_lock:
                        next_item.status = QueueItemStatus.COMPLETED
                        next_item.completed_at = datetime.now()

                    if queue_item_delay > 0:
                        logger.info(f"Waiting {queue_item_delay} seconds before next queue item")
                        await asyncio.sleep(queue_item_delay)

                except DependencyMissing as e:
                    # Dependency missing - mark as failed but DON'T pause queue
                    logger.warning(f"Dependency missing for queue item {next_item.id}: {e}")

                    with queue_lock:
                        next_item.status = QueueItemStatus.FAILED
                        next_item.error_message = str(e)
                        next_item.completed_at = datetime.now()
                        # Don't set queue_paused - let queue continue

                    await websocket.send_json({
                        "type": "log",
                        "message": f"Item failed due to missing dependency: {str(e)}. Queue will continue.",
                        "level": "warning"
                    })

                except Exception as e:
                    # Download failed (after retries exhausted)
                    logger.error(f"Error processing queue item: {e}", exc_info=True)

                    # Check if we should continue on error
                    request = next_item.download_request
                    continue_on_error = getattr(request, 'continue_on_error', False)
                    enable_retry_delay = getattr(request, 'enable_retry_delay', True)
                    logger.info(f"[DEBUG] Settings - continue_on_error={continue_on_error}, enable_retry_delay={enable_retry_delay}")

                    with queue_lock:
                        next_item.status = QueueItemStatus.FAILED
                        next_item.error_message = str(e)
                        next_item.completed_at = datetime.now()

                        # Pause queue only if continue_on_error is disabled
                        if not continue_on_error:
                            queue_paused = True

                    if continue_on_error:
                        logger.warning(f"Queue item {next_item.id} failed, but continuing due to continue_on_error setting")
                        await websocket.send_json({
                            "type": "log",
                            "message": "Item failed, but queue will continue processing (continue_on_error enabled).",
                            "level": "warning"
                        })
                    else:
                        logger.warning(f"Queue PAUSED due to retry exhaustion for item {next_item.id}")
                        await websocket.send_json({
                            "type": "log",
                            "message": "Queue paused due to download failures. Please review errors and resume manually.",
                            "level": "error"
                        })
            else:
                # No WebSocket connection, run without it
                logger.warning(f"No WebSocket connection for session {session_id}, running in background")
                try:
                    # Create a dummy WebSocket-like object that does nothing
                    class DummyWebSocket:
                        async def send_json(self, data): pass

                    await run_download_session(session_id, active_sessions[session_id], DummyWebSocket())

                    # Success - apply queue item delay if configured
                    request = next_item.download_request
                    enable_retry_delay = getattr(request, 'enable_retry_delay', True)
                    queue_item_delay = getattr(request, 'queue_item_delay', 0.0) if enable_retry_delay else 0.0

                    with queue_lock:
                        next_item.status = QueueItemStatus.COMPLETED
                        next_item.completed_at = datetime.now()

                    if queue_item_delay > 0:
                        logger.info(f"Waiting {queue_item_delay} seconds before next queue item")
                        await asyncio.sleep(queue_item_delay)

                except DependencyMissing as e:
                    # Dependency missing - mark as failed but DON'T pause queue
                    logger.warning(f"Dependency missing for queue item {next_item.id}: {e}")

                    with queue_lock:
                        next_item.status = QueueItemStatus.FAILED
                        next_item.error_message = str(e)
                        next_item.completed_at = datetime.now()
                        # Don't set queue_paused - let queue continue

                except Exception as e:
                    # Download failed (after retries exhausted)
                    logger.error(f"Error processing queue item: {e}", exc_info=True)

                    # Check if we should continue on error
                    request = next_item.download_request
                    continue_on_error = getattr(request, 'continue_on_error', False)
                    enable_retry_delay = getattr(request, 'enable_retry_delay', True)
                    logger.info(f"[DEBUG] Settings - continue_on_error={continue_on_error}, enable_retry_delay={enable_retry_delay}")

                    with queue_lock:
                        next_item.status = QueueItemStatus.FAILED
                        next_item.error_message = str(e)
                        next_item.completed_at = datetime.now()

                        # Pause queue only if continue_on_error is disabled
                        if not continue_on_error:
                            queue_paused = True

                    if continue_on_error:
                        logger.warning(f"Queue item {next_item.id} failed, but continuing due to continue_on_error setting")
                    else:
                        logger.warning(f"Queue PAUSED due to retry exhaustion for item {next_item.id}")

            # Cleanup
            with queue_lock:
                current_downloading_item = None

            if session_id in active_sessions:
                del active_sessions[session_id]
            if session_id in cancellation_flags:
                del cancellation_flags[session_id]

            # Broadcast queue update
            await broadcast_queue_update()

            # If queue was paused due to failure, break out of loop
            if queue_paused:
                logger.info("Queue processor stopping due to pause")
                break

    finally:
        queue_processor_running = False
        logger.info("Queue processor stopped")


# FastAPI Event Handlers

@app.on_event("startup")
async def startup_event():
    """Initialize API on startup if cookies exist."""
    logger.info("Server startup: attempting to initialize Apple Music API")

    success = await initialize_api_from_cookies()

    if success:
        logger.info("API initialized successfully - library browser ready")
    else:
        logger.info("API not initialized - library browser will require cookies configuration")

    # Start the playlist monitoring scheduler
    start_monitor_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on server shutdown."""
    logger.info("Server shutdown: stopping monitoring scheduler")
    stop_monitor_scheduler()


# API Routes

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main HTML page."""
    html_path = Path(__file__).parent / "static" / "index_advanced.html"
    if html_path.exists():
        return FileResponse(html_path)

    # Fallback inline HTML if static file doesn't exist
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>gamdl Advanced Web UI</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
                background: #f5f5f5;
            }
            .container {
                background: white;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            h1 {
                color: #333;
                margin-bottom: 10px;
            }
            .subtitle {
                color: #666;
                margin-bottom: 30px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            .form-group label {
                display: block;
                margin-bottom: 5px;
                font-weight: 500;
                color: #333;
            }
            .form-group.checkbox-group {
                margin-bottom: 12px;
            }
            .form-group.checkbox-group label {
                display: flex;
                align-items: center;
                gap: 8px;
                cursor: pointer;
                font-weight: 400;
            }
            .form-group.checkbox-group input[type="checkbox"] {
                width: auto;
                margin: 0;
                cursor: pointer;
            }
            input, textarea, select {
                width: 100%;
                padding: 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 14px;
                box-sizing: border-box;
            }
            textarea {
                min-height: 100px;
                font-family: monospace;
            }
            button {
                background: #007aff;
                color: white;
                border: none;
                padding: 12px 24px;
                border-radius: 4px;
                font-size: 16px;
                cursor: pointer;
                font-weight: 500;
                margin-right: 10px;
            }
            button:hover {
                background: #0051d5;
            }
            button:disabled {
                background: #ccc;
                cursor: not-allowed;
            }
            button.cancel {
                background: #ff3b30;
            }
            button.cancel:hover {
                background: #d32f2f;
            }
            button.cancel:disabled {
                background: #ccc;
            }
            .button-group {
                display: flex;
                align-items: center;
                margin-top: 20px;
            }
            .progress-container {
                display: none;
                margin-top: 30px;
                padding: 20px;
                background: #f9f9f9;
                border-radius: 4px;
            }
            .progress-container.active {
                display: block;
            }
            .progress-log {
                background: #1e1e1e;
                color: #d4d4d4;
                padding: 15px;
                border-radius: 4px;
                max-height: 400px;
                overflow-y: auto;
                font-family: 'Courier New', monospace;
                font-size: 13px;
                line-height: 1.5;
            }
            .progress-log .info {
                color: #4ec9b0;
            }
            .progress-log .warning {
                color: #dcdcaa;
            }
            .progress-log .error {
                color: #f48771;
            }
            .progress-log .success {
                color: #b5cea8;
            }
            .status-bar {
                display: flex;
                align-items: center;
                margin-bottom: 15px;
                padding: 10px;
                background: white;
                border-radius: 4px;
            }
            .status-indicator {
                width: 12px;
                height: 12px;
                border-radius: 50%;
                margin-right: 10px;
                background: #ccc;
            }
            .status-indicator.active {
                background: #34c759;
                animation: pulse 2s infinite;
            }
            .status-indicator.cancelled {
                background: #ff9500;
            }
            @keyframes pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.5; }
            }
            .progress-stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 10px;
                margin-bottom: 15px;
            }
            .stat-item {
                background: white;
                padding: 10px;
                border-radius: 4px;
                text-align: center;
            }
            .stat-value {
                font-size: 24px;
                font-weight: bold;
                color: #007aff;
            }
            .stat-label {
                font-size: 12px;
                color: #666;
                margin-top: 5px;
            }
            .collapsible {
                margin-top: 20px;
            }
            .collapsible-header {
                background: #f0f0f0;
                padding: 10px;
                border-radius: 4px;
                cursor: pointer;
                user-select: none;
            }
            .collapsible-header:hover {
                background: #e8e8e8;
            }
            .collapsible-content {
                display: none;
                padding: 15px 0;
            }
            .collapsible-content.active {
                display: block;
            }
            .row {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 15px;
            }

            /* Library Browser Styles */
            .nav-tabs {
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
                border-bottom: 2px solid #e0e0e0;
                padding-bottom: 0;
            }
            .nav-tab {
                padding: 10px 20px;
                background: none;
                border: none;
                cursor: pointer;
                font-size: 16px;
                font-weight: 500;
                color: #666;
                border-bottom: 2px solid transparent;
                margin-bottom: -2px;
                transition: all 0.2s;
            }
            .nav-tab:hover {
                color: #007aff;
                background: #f0f0f0;
            }
            .nav-tab.active {
                color: #007aff;
                border-bottom-color: #007aff;
            }
            .tab-content {
                display: none;
            }
            .tab-content.active {
                display: block;
            }
            .library-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
                gap: 20px;
                margin: 20px 0;
            }
            .library-item {
                background: #f9f9f9;
                border-radius: 8px;
                padding: 10px;
                text-align: center;
                transition: transform 0.2s, box-shadow 0.2s;
                cursor: pointer;
            }
            .library-item:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 8px rgba(0,0,0,0.15);
            }
            .library-item img {
                width: 100%;
                height: auto;
                border-radius: 4px;
                margin-bottom: 10px;
                background: #e0e0e0;
            }
            .library-item-title {
                font-weight: 600;
                font-size: 14px;
                margin-bottom: 4px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .library-item-subtitle {
                font-size: 12px;
                color: #666;
                margin-bottom: 8px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .library-item button {
                width: 100%;
                padding: 8px;
                font-size: 13px;
                margin: 0;
            }
            .library-item .btn-group {
                display: flex;
                gap: 4px;
                width: 100%;
            }
            .library-item .btn-group button {
                flex: 1;
                padding: 8px 4px;
                font-size: 12px;
            }
            .library-item .btn-primary {
                background: #007aff;
                color: white;
                border: 1px solid #007aff;
            }
            .library-item .btn-primary:hover {
                background: #0056b3;
                border-color: #0056b3;
            }
            .library-item .btn-secondary {
                background: white;
                color: #007aff;
                border: 1px solid #007aff;
            }
            .library-item .btn-secondary:hover {
                background: #f0f0f0;
            }
            .load-more {
                text-align: center;
                margin: 20px 0;
            }
            .load-more button {
                padding: 10px 30px;
            }
            .library-empty {
                text-align: center;
                padding: 40px;
                color: #999;
            }
            .library-error {
                background: #fff3cd;
                border: 1px solid #ffc107;
                padding: 15px;
                border-radius: 4px;
                color: #856404;
                margin: 20px 0;
            }
            .view-section {
                display: none;
            }
            .view-section.active {
                display: block;
            }
            .loading {
                text-align: center;
                padding: 40px;
                color: #666;
            }

            .spinner {
                display: inline-block;
                width: 40px;
                height: 40px;
                border: 4px solid #f3f3f3;
                border-top: 4px solid #007aff;
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin-bottom: 15px;
            }

            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            #settingsView h2 {
                margin-top: 0;
                margin-bottom: 5px;
            }
            #settingsView h3 {
                margin-top: 30px;
                margin-bottom: 15px;
                color: #333;
                font-size: 16px;
                border-bottom: 2px solid #e0e0e0;
                padding-bottom: 8px;
            }
            #settingsView h3:first-of-type {
                margin-top: 0;
            }
            #settingsView small {
                display: block;
                margin-top: 5px;
                color: #666;
                font-size: 12px;
            }

            /* Queue Panel Styles */
            .queue-panel {
                position: fixed;
                right: 0;
                top: 0;
                bottom: 0;
                width: 350px;
                background: white;
                box-shadow: -2px 0 10px rgba(0,0,0,0.1);
                transform: translateX(0);
                transition: transform 0.3s ease;
                z-index: 1000;
                display: flex;
                flex-direction: column;
                border-left: 2px solid #e0e0e0;
            }
            body {
                margin-right: 350px;
            }
            .queue-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 15px;
                background: #007aff;
                color: white;
                border-bottom: 1px solid #0051d5;
            }
            .queue-header h3 {
                margin: 0;
                font-size: 18px;
                font-weight: 600;
            }
            .queue-controls {
                display: flex;
                gap: 10px;
                padding: 10px;
                background: #f9f9f9;
                border-bottom: 1px solid #e0e0e0;
            }
            .queue-control-btn {
                flex: 1;
                padding: 8px 12px;
                font-size: 13px;
                background: white;
                color: #333;
                border: 1px solid #ddd;
                border-radius: 4px;
                cursor: pointer;
                transition: all 0.2s;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
            }
            .queue-control-btn svg {
                flex-shrink: 0;
            }
            .queue-control-btn:hover {
                background: #f0f0f0;
                border-color: #007aff;
            }
            .queue-control-btn.clear-btn {
                color: #ff3b30;
            }
            .queue-control-btn.clear-btn:hover {
                background: #fff5f5;
                border-color: #ff3b30;
            }
            .queue-control-btn.paused {
                background: #ff9500;
                color: white;
                border-color: #ff9500;
            }
            .queue-status {
                display: flex;
                justify-content: space-around;
                padding: 10px;
                background: #f9f9f9;
                border-bottom: 1px solid #e0e0e0;
            }
            .queue-stat {
                display: flex;
                flex-direction: column;
                align-items: center;
                font-size: 12px;
            }
            .queue-stat-label {
                color: #666;
                margin-bottom: 4px;
            }
            .queue-stat-value {
                font-size: 20px;
                font-weight: bold;
                color: #007aff;
            }
            .queue-list {
                flex: 1;
                overflow-y: auto;
                padding: 10px;
            }
            .queue-item {
                background: white;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                padding: 8px 10px;
                margin-bottom: 6px;
                transition: all 0.2s;
            }
            .queue-item:hover {
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            }
            .queue-item.queued {
                border-left: 4px solid #007aff;
            }
            .queue-item.downloading {
                border-left: 4px solid #34c759;
                background: #f0fff4;
            }
            .queue-item.completed {
                border-left: 4px solid #8e8e93;
                opacity: 0.7;
            }
            .queue-item.failed {
                border-left: 4px solid #ff3b30;
                background: #fff5f5;
            }
            .queue-item.cancelled {
                border-left: 4px solid #ff9500;
                opacity: 0.6;
            }
            .queue-item-header {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 4px;
            }
            .queue-item-title {
                font-weight: 600;
                font-size: 13px;
                color: #333;
                margin-bottom: 0;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                flex: 1;
            }
            .queue-item-type {
                font-size: 11px;
                color: #666;
                text-transform: uppercase;
                background: #f0f0f0;
                padding: 2px 6px;
                border-radius: 3px;
                margin-left: 8px;
            }
            .queue-item-status {
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 12px;
                color: #666;
                margin-bottom: 8px;
            }
            .queue-item-status-icon {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                display: inline-block;
            }
            .queue-item-status-icon.queued {
                background: #007aff;
            }
            .queue-item-status-icon.downloading {
                background: #34c759;
                animation: pulse 2s infinite;
            }
            .queue-item-status-icon.completed {
                background: #8e8e93;
            }
            .queue-item-status-icon.failed {
                background: #ff3b30;
            }
            .queue-item-actions {
                display: flex;
                gap: 8px;
            }
            .queue-item-btn {
                padding: 4px 8px;
                font-size: 12px;
                border: 1px solid #ddd;
                background: white;
                border-radius: 4px;
                cursor: pointer;
                transition: all 0.2s;
            }
            .queue-item-btn:hover {
                background: #f0f0f0;
            }
            .queue-item-btn.remove {
                color: #ff3b30;
                border-color: #ff3b30;
            }
            .queue-item-btn.remove:hover {
                background: #ff3b30;
                color: white;
            }
            .queue-empty {
                text-align: center;
                padding: 40px 20px;
                color: #999;
            }
            .queue-empty-icon {
                font-size: 48px;
                margin-bottom: 10px;
            }
            .queue-item-icon {
                font-size: 14px;
                margin-right: 6px;
            }
            .queue-item-meta {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 4px;
                font-size: 11px;
            }
            .queue-item-info {
                font-size: 10px;
                color: #007aff;
                margin-bottom: 4px;
            }
            .queue-item-progress {
                font-size: 11px;
                color: #34c759;
                font-weight: 500;
                margin-bottom: 4px;
            }
            .queue-item-error {
                font-size: 10px;
                color: #ff3b30;
                background: #fff5f5;
                padding: 4px 6px;
                border-radius: 3px;
                margin-bottom: 4px;
                word-break: break-word;
            }
            .queue-item-remove,
            .queue-item-view {
                padding: 4px 12px;
                font-size: 12px;
                border: 1px solid #ddd;
                border-radius: 4px;
                background: white;
                cursor: pointer;
                transition: all 0.2s;
            }
            .queue-item-remove {
                color: #ff3b30;
                border-color: #ff3b30;
            }
            .queue-item-remove:hover {
                background: #ff3b30;
                color: white;
            }
            .queue-item-view {
                color: #007aff;
                border-color: #007aff;
            }
            .queue-item-view:hover {
                background: #007aff;
                color: white;
            }

            /* Search Container */
            .search-container {
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
                max-width: 600px;
            }

            .search-container input {
                flex: 1;
                padding: 12px 16px;
                font-size: 16px;
                border: 2px solid #e0e0e0;
                border-radius: 8px;
                outline: none;
                transition: border-color 0.2s;
            }

            .search-container input:focus {
                border-color: #007aff;
            }

            .search-container button {
                padding: 12px 24px;
                font-size: 16px;
                font-weight: 500;
                white-space: nowrap;
            }

            /* Error Message */
            .error-message {
                padding: 12px 16px;
                background: #fff3cd;
                border: 1px solid #ffc107;
                border-radius: 8px;
                color: #856404;
                margin-bottom: 20px;
            }

            /* Modal Styles */
            .modal {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.5);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 10000;
            }

            .modal-content {
                background: white;
                border-radius: 12px;
                width: 90%;
                max-width: 1200px;
                max-height: 90vh;
                display: flex;
                flex-direction: column;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            }

            .modal-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 20px;
                border-bottom: 1px solid #e0e0e0;
            }

            .modal-header h2 {
                margin: 0;
                font-size: 24px;
            }

            .modal-close {
                background: none;
                border: none;
                font-size: 32px;
                cursor: pointer;
                color: #666;
                padding: 0;
                width: 40px;
                height: 40px;
                line-height: 1;
            }

            .modal-close:hover {
                color: #333;
            }

            .modal-body {
                flex: 1;
                overflow-y: auto;
                padding: 20px;
            }

            .modal-footer {
                display: flex;
                justify-content: flex-end;
                gap: 10px;
                padding: 20px;
                border-top: 1px solid #e0e0e0;
            }

            .artist-section {
                margin-bottom: 30px;
            }

            .artist-section h3 {
                font-size: 20px;
                font-weight: 600;
                margin-bottom: 15px;
            }

            /* Monitor View Styles */
            .monitor-card {
                background: white;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 20px;
            }

            .playlist-selector-section {
                margin-bottom: 20px;
                padding: 20px;
                background: #f9f9f9;
                border-radius: 8px;
            }

            .selector-label {
                display: block;
                margin-bottom: 10px;
                font-weight: 600;
                color: #333;
            }

            .selector-controls {
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .playlist-dropdown {
                flex: 1;
                padding: 10px 15px;
                font-size: 14px;
                border: 1px solid #ddd;
                border-radius: 6px;
                background: white;
                cursor: pointer;
                min-width: 300px;
            }

            .playlist-dropdown:focus {
                outline: none;
                border-color: #007AFF;
                box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.1);
            }

            .monitor-empty {
                text-align: center;
                padding: 40px 20px;
                color: #666;
            }

            .monitor-active .monitor-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
                flex-wrap: wrap;
                gap: 15px;
            }

            .monitor-active h3 {
                margin: 0;
                font-size: 20px;
                font-weight: 600;
            }

            .monitor-controls {
                display: flex;
                gap: 10px;
                align-items: center;
            }

            .toggle-switch {
                position: relative;
                display: inline-block;
                width: 50px;
                height: 24px;
            }

            .toggle-switch input {
                opacity: 0;
                width: 0;
                height: 0;
            }

            .toggle-switch .slider {
                position: absolute;
                cursor: pointer;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background-color: #ccc;
                transition: 0.3s;
                border-radius: 24px;
            }

            .toggle-switch .slider:before {
                position: absolute;
                content: "";
                height: 18px;
                width: 18px;
                left: 3px;
                bottom: 3px;
                background-color: white;
                transition: 0.3s;
                border-radius: 50%;
            }

            .toggle-switch input:checked + .slider {
                background-color: #4CAF50;
            }

            .toggle-switch input:checked + .slider:before {
                transform: translateX(26px);
            }

            .monitor-stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin-top: 15px;
            }

            .monitor-stats .stat {
                padding: 10px;
                background: #f5f5f5;
                border-radius: 4px;
            }

            .monitor-stats .stat-label {
                font-weight: 600;
                color: #666;
                font-size: 13px;
            }

            .monitor-stats .stat-value {
                display: block;
                margin-top: 4px;
                font-size: 16px;
                color: #333;
            }

            .activity-log-section {
                background: white;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                padding: 20px;
            }

            .activity-log-section h3 {
                margin: 0 0 15px 0;
                font-size: 18px;
                font-weight: 600;
            }

            .activity-log {
                max-height: 400px;
                overflow-y: auto;
            }

            .activity-empty {
                text-align: center;
                color: #999;
                padding: 20px;
            }

            .activity-item {
                padding: 12px;
                border-bottom: 1px solid #f0f0f0;
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
            }

            .activity-item:last-child {
                border-bottom: none;
            }

            .activity-item .activity-icon {
                width: 32px;
                height: 32px;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-right: 12px;
                flex-shrink: 0;
            }

            .activity-item.new-tracks .activity-icon {
                background: #e8f5e9;
                color: #4CAF50;
            }

            .activity-item.removed-tracks .activity-icon {
                background: #fff3e0;
                color: #ff9800;
            }

            .activity-item.error .activity-icon {
                background: #ffebee;
                color: #f44336;
            }

            .activity-item .activity-content {
                flex: 1;
            }

            .activity-item .activity-message {
                font-weight: 500;
                margin-bottom: 4px;
            }

            .activity-item .activity-time {
                font-size: 12px;
                color: #999;
            }

            .btn-danger {
                background-color: #f44336;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 14px;
            }

            .btn-danger:hover {
                background-color: #d32f2f;
            }

            .btn-secondary {
                background-color: #757575;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 14px;
            }

            .btn-secondary:hover {
                background-color: #616161;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>gamdl Advanced Web UI</h1>
            <p class="subtitle">Browse your library or download from Apple Music URLs</p>

            <!-- View Navigation -->
            <div class="nav-tabs">
                <button class="nav-tab active" onclick="switchView('library', this)">Library Browser</button>
                <button class="nav-tab" onclick="switchView('downloads', this)">URL Downloads</button>
                <button class="nav-tab" onclick="switchView('search', this)">Search</button>
                <button class="nav-tab" onclick="switchView('monitor', this)">Monitor</button>
                <button class="nav-tab" onclick="switchView('settings', this)" style="margin-left: auto;">Settings</button>
            </div>

            <!-- Library Browser View -->
            <div id="libraryView" class="view-section active">
                <div id="libraryError" class="library-error" style="display:none;"></div>

                <!-- Library Type Tabs -->
                <div class="nav-tabs">
                    <button class="nav-tab active" onclick="switchLibraryTab('albums', this)">Albums</button>
                    <button class="nav-tab" onclick="switchLibraryTab('playlists', this)">Playlists</button>
                    <button class="nav-tab" onclick="switchLibraryTab('songs', this)">Songs</button>
                </div>

                <!-- Albums Tab -->
                <div id="albumsTab" class="tab-content active">
                    <div id="albumsLoading" class="loading">Loading albums...</div>
                    <div id="albumsGrid" class="library-grid"></div>
                    <div id="albumsEmpty" class="library-empty" style="display:none;">No albums found in your library</div>
                    <div id="albumsLoadMore" class="load-more" style="display:none;">
                        <button onclick="loadMoreAlbums()">Load More</button>
                    </div>
                </div>

                <!-- Playlists Tab -->
                <div id="playlistsTab" class="tab-content">
                    <div id="playlistsLoading" class="loading">Loading playlists...</div>
                    <div id="playlistsGrid" class="library-grid"></div>
                    <div id="playlistsEmpty" class="library-empty" style="display:none;">No playlists found in your library</div>
                    <div id="playlistsLoadMore" class="load-more" style="display:none;">
                        <button onclick="loadMorePlaylists()">Load More</button>
                    </div>
                </div>

                <!-- Songs Tab -->
                <div id="songsTab" class="tab-content">
                    <div id="songsLoading" class="loading">Loading songs...</div>
                    <div id="songsGrid" class="library-grid"></div>
                    <div id="songsEmpty" class="library-empty" style="display:none;">No songs found in your library</div>
                    <div id="songsLoadMore" class="load-more" style="display:none;">
                        <button onclick="loadMoreSongs()">Load More</button>
                    </div>
                </div>
            </div>

            <!-- URL Downloads View -->
            <div id="downloadsView" class="view-section">
            <form id="downloadForm">
                <div class="form-group">
                    <label for="urls">Apple Music URLs (one per line)</label>
                    <textarea id="urls" name="urls" placeholder="https://music.apple.com/us/album/...&#10;https://music.apple.com/us/playlist/..." required></textarea>
                </div>

                <div class="button-group">
                    <button type="submit" id="downloadBtn">Start Download</button>
                    <button type="button" id="cancelBtn" class="cancel" disabled>Cancel</button>
                    <button type="button" onclick="window.open('https://music.apple.com/au/home', '_blank')" style="background: #FA243C; margin-left: auto;">Open Apple Music</button>
                </div>
            </form>

            <div id="progressContainer" class="progress-container">
                <div class="status-bar">
                    <div id="statusIndicator" class="status-indicator"></div>
                    <div id="statusText">Idle</div>
                </div>
                <div class="progress-stats">
                    <div class="stat-item">
                        <div class="stat-value" id="totalTracks">0</div>
                        <div class="stat-label">Total Tracks</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="completedTracks">0</div>
                        <div class="stat-label">Completed</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="skippedTracks">0</div>
                        <div class="stat-label">Skipped</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="failedTracks">0</div>
                        <div class="stat-label">Failed</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="progressPercent">0%</div>
                        <div class="stat-label">Progress</div>
                    </div>
                </div>
                <div id="progressLog" class="progress-log"></div>
            </div>
            </div> <!-- End of downloadsView -->

            <!-- Settings View -->
            <div id="settingsView" class="view-section">
                <h2>Settings</h2>
                <p class="subtitle">Configure paths and download options</p>

                <h3>Paths</h3>
                <div class="form-group">
                    <label for="cookiesPath">Cookies Path (Netscape format)</label>
                    <input type="text" id="cookiesPath" name="cookiesPath" placeholder="/path/to/cookies.txt">
                    <small>Path to your exported Apple Music cookies file</small>
                </div>

                <div class="form-group">
                    <label for="outputPath">Output Path</label>
                    <input type="text" id="outputPath" name="outputPath" placeholder="./downloads">
                    <small>Directory where downloaded files will be saved</small>
                </div>

                <h3>Audio Options</h3>
                <div class="row">
                    <div class="form-group">
                        <label for="songCodec">Song Codec</label>
                        <select id="songCodec" name="songCodec">
                            <option value="">Default (AAC)</option>
                            <option value="aac-legacy">AAC Legacy</option>
                            <option value="aac-he-legacy">AAC HE Legacy</option>
                            <option value="aac">AAC</option>
                            <option value="aac-he">AAC HE</option>
                            <option value="aac-binaural">AAC Binaural</option>
                            <option value="aac-downmix">AAC Downmix</option>
                            <option value="alac">ALAC</option>
                            <option value="atmos">Atmos</option>
                            <option value="ac3">AC3</option>
                        </select>
                    </div>

                    <div class="form-group">
                        <label for="musicVideoResolution">Music Video Resolution</label>
                        <select id="musicVideoResolution" name="musicVideoResolution">
                            <option value="">Best Available</option>
                            <option value="2160p">2160p (4K)</option>
                            <option value="1080p">1080p</option>
                            <option value="720p">720p</option>
                            <option value="480p">480p</option>
                        </select>
                    </div>
                </div>

                <h3>Cover Art Options</h3>
                <div class="row">
                    <div class="form-group">
                        <label for="coverSize">Cover Size (px)</label>
                        <input type="number" id="coverSize" name="coverSize" placeholder="1200">
                    </div>

                    <div class="form-group">
                        <label for="coverFormat">Cover Format</label>
                        <select id="coverFormat" name="coverFormat">
                            <option value="">Default (JPG)</option>
                            <option value="jpg">JPG</option>
                            <option value="png">PNG</option>
                            <option value="raw">Raw</option>
                        </select>
                    </div>
                </div>

                <div class="form-group checkbox-group">
                    <label>
                        <input type="checkbox" id="noCover" name="noCover">
                        <span>Skip cover art download</span>
                        <small>Doesn't download a seperate JPG of the cover art. Songs still contain the artwork</small>
                    </label>
                </div>

                <h3>Metadata Options</h3>
                <div class="form-group checkbox-group">
                    <label>
                        <input type="checkbox" id="noLyrics" name="noLyrics">
                        <span>Skip lyrics download</span>
                        <small>When enabled, when downloading songs, the accompanying .lrc lyric file is saved alongside the song</small>
                    </label>
                </div>

                <div class="form-group checkbox-group">
                    <label>
                        <input type="checkbox" id="extraTags" name="extraTags">
                        <span>Fetch extra tags from Apple Music preview</span>
                    </label>
                </div>

                <div class="form-group checkbox-group">
                    <label>
                        <input type="checkbox" id="includeVideosInDiscography">
                        <span>Include music videos in artist discography downloads</span>
                    </label>
                    <small>When downloading an artist's discography, also include their music videos</small>
                </div>

                <h3>Retry & Delay Options</h3>
                <div class="form-group checkbox-group">
                    <label>
                        <input type="checkbox" id="enableRetryDelay" name="enableRetryDelay" checked>
                        <span>Enable retry & delay features</span>
                    </label>
                    <small>When disabled, downloads will not retry on failure and will not pause between songs/items</small>
                </div>

                <div id="retryDelaySettings">
                    <div class="form-group">
                        <label for="maxRetries">Max Retries:</label>
                        <input type="number" id="maxRetries" name="maxRetries" min="0" max="10" value="3">
                        <small>Number of times to retry a failed download (0 = no retries)</small>
                    </div>

                    <div class="form-group">
                        <label for="retryDelay">Retry Delay (seconds):</label>
                        <input type="number" id="retryDelay" name="retryDelay" min="0" max="300" value="60">
                        <small>Seconds to wait before retrying a failed download</small>
                    </div>

                    <div class="form-group">
                        <label for="songDelay">Song Delay (seconds):</label>
                        <input type="number" id="songDelay" name="songDelay" min="0" max="60" step="0.5" value="0">
                        <small>Seconds to wait after each individual song download</small>
                    </div>

                    <div class="form-group">
                        <label for="queueItemDelay">Queue Item Delay (seconds):</label>
                        <input type="number" id="queueItemDelay" name="queueItemDelay" min="0" max="300" step="1" value="0">
                        <small>Seconds to wait after completing each album/playlist</small>
                    </div>
                </div>

                <h3>Queue Behavior</h3>
                <div class="form-group checkbox-group">
                    <label>
                        <input type="checkbox" id="continueOnError" name="continueOnError">
                        <span>Continue queue on errors</span>
                    </label>
                    <small>When enabled, the queue will continue processing even if items fail (after retries are exhausted). When disabled, the queue will pause on failures.</small>
                </div>

                <div class="button-group">
                    <button type="button" onclick="saveAllSettings()">Save Settings</button>
                </div>
            </div>

            <!-- Monitor View -->
            <div id="monitorView" class="view-section">
                <h2>Playlist Monitor</h2>
                <p class="subtitle">Automatically download new additions to a monitored playlist</p>

                <!-- Playlist Selector -->
                <div class="playlist-selector-section">
                    <label for="playlistSelector" class="selector-label">Select Playlist to Monitor:</label>
                    <div class="selector-controls">
                        <select id="playlistSelector" class="playlist-dropdown" onchange="handlePlaylistSelection()">
                            <option value="">-- Select a playlist --</option>
                        </select>
                        <button onclick="loadPlaylistsForSelector()" class="btn-secondary">Refresh Playlists</button>
                    </div>
                    <div id="playlistSelectorLoading" style="display: none; margin-top: 10px; color: #666;">
                        Loading playlists...
                    </div>
                    <div id="playlistSelectorError" class="error-message" style="display: none; margin-top: 10px;"></div>
                </div>

                <!-- Monitor Status Card -->
                <div id="monitorStatusCard" class="monitor-card">
                    <div class="monitor-empty" style="display: none;">
                        <p>No playlist is being monitored</p>
                        <p class="subtitle">Select a playlist from the dropdown above to start monitoring</p>
                    </div>

                    <div class="monitor-active" style="display: none;">
                        <div class="monitor-header">
                            <h3 id="monitoredPlaylistName">Playlist Name</h3>
                            <div class="monitor-controls">
                                <label class="toggle-switch">
                                    <input type="checkbox" id="monitorToggle" onchange="toggleMonitoring()">
                                    <span class="slider"></span>
                                </label>
                                <button onclick="manualCheckPlaylist()" class="btn-secondary">Check Now</button>
                                <button onclick="stopMonitoring()" class="btn-danger">Stop Monitoring</button>
                            </div>
                        </div>

                        <div class="monitor-stats">
                            <div class="stat">
                                <span class="stat-label">Status:</span>
                                <span id="monitorStatus" class="stat-value">Enabled</span>
                            </div>
                            <div class="stat">
                                <span class="stat-label">Tracked Tracks:</span>
                                <span id="monitorTrackCount" class="stat-value">0</span>
                            </div>
                            <div class="stat">
                                <span class="stat-label">Last Checked:</span>
                                <span id="monitorLastChecked" class="stat-value">Never</span>
                            </div>
                            <div class="stat">
                                <span class="stat-label">Monitoring Since:</span>
                                <span id="monitoringSince" class="stat-value">-</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Activity Log -->
                <div class="activity-log-section">
                    <h3>Activity Log</h3>
                    <div id="activityLog" class="activity-log">
                        <p class="activity-empty">No activity yet</p>
                    </div>
                </div>
            </div>

            <!-- Search View -->
            <div id="searchView" class="view-section">
                <h2>Search Apple Music</h2>

                <!-- Search Input -->
                <div class="search-container">
                    <input type="text" id="searchInput" placeholder="Search for artists, albums, or songs..."
                           onkeypress="if(event.key === 'Enter') performSearch()">
                    <button onclick="performSearch()" class="btn-primary">Search</button>
                </div>

                <!-- Search Result Tabs -->
                <div class="nav-tabs" style="margin-top: 20px;">
                    <button class="nav-tab active" onclick="switchSearchTab('all', this)">All</button>
                    <button class="nav-tab" onclick="switchSearchTab('songs', this)">Songs</button>
                    <button class="nav-tab" onclick="switchSearchTab('albums', this)">Albums</button>
                    <button class="nav-tab" onclick="switchSearchTab('artists', this)">Artists</button>
                    <button class="nav-tab" onclick="switchSearchTab('playlists', this)">Playlists</button>
                    <button class="nav-tab" onclick="switchSearchTab('music-videos', this)">Music Videos</button>
                    <button class="nav-tab" onclick="switchSearchTab('podcasts', this)">Podcasts</button>
                </div>

                <!-- Error Display -->
                <div id="searchError" class="error-message" style="display:none;"></div>

                <!-- Search Results Container -->
                <div id="searchResults">
                    <!-- All Results Tab -->
                    <div id="allTab" class="tab-content active">
                        <div id="allLoading" class="loading">Searching...</div>
                        <div id="allEmpty" class="library-empty" style="display:none;">
                            <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
                                <circle cx="11" cy="11" r="8"></circle>
                                <path d="m21 21-4.35-4.35"></path>
                            </svg>
                            <p>No results found</p>
                            <small>Try different search terms</small>
                        </div>
                        <div id="allGrid" class="library-grid"></div>
                    </div>

                    <!-- Songs Tab -->
                    <div id="songsSearchTab" class="tab-content">
                        <div id="songsSearchLoading" class="loading">Loading songs...</div>
                        <div id="songsSearchEmpty" class="library-empty" style="display:none;">
                            <p>No songs found</p>
                        </div>
                        <div id="songsSearchGrid" class="library-grid"></div>
                        <div id="songsSearchLoadMore" class="load-more" style="display:none;">
                            <button onclick="loadMoreSearchResults('songs')">Load More</button>
                        </div>
                    </div>

                    <!-- Albums Tab -->
                    <div id="albumsSearchTab" class="tab-content">
                        <div id="albumsSearchLoading" class="loading">Loading albums...</div>
                        <div id="albumsSearchEmpty" class="library-empty" style="display:none;">
                            <p>No albums found</p>
                        </div>
                        <div id="albumsSearchGrid" class="library-grid"></div>
                        <div id="albumsSearchLoadMore" class="load-more" style="display:none;">
                            <button onclick="loadMoreSearchResults('albums')">Load More</button>
                        </div>
                    </div>

                    <!-- Artists Tab -->
                    <div id="artistsSearchTab" class="tab-content">
                        <div id="artistsSearchLoading" class="loading">Loading artists...</div>
                        <div id="artistsSearchEmpty" class="library-empty" style="display:none;">
                            <p>No artists found</p>
                        </div>
                        <div id="artistsSearchGrid" class="library-grid"></div>
                        <div id="artistsSearchLoadMore" class="load-more" style="display:none;">
                            <button onclick="loadMoreSearchResults('artists')">Load More</button>
                        </div>
                    </div>

                    <!-- Playlists Tab -->
                    <div id="playlistsSearchTab" class="tab-content">
                        <div id="playlistsSearchLoading" class="loading">Loading playlists...</div>
                        <div id="playlistsSearchEmpty" class="library-empty" style="display:none;">
                            <p>No playlists found</p>
                        </div>
                        <div id="playlistsSearchGrid" class="library-grid"></div>
                        <div id="playlistsSearchLoadMore" class="load-more" style="display:none;">
                            <button onclick="loadMoreSearchResults('playlists')">Load More</button>
                        </div>
                    </div>

                    <!-- Music Videos Tab -->
                    <div id="musicVideosSearchTab" class="tab-content">
                        <div style="background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; padding: 12px; margin-bottom: 15px;">
                            <strong style="color: #856404;">⚠️ Important:</strong> Music videos require <code>mp4decrypt</code> to be installed.
                            <br>
                            <small style="color: #856404;">
                                Download from <a href="https://www.bento4.com/downloads/" target="_blank" style="color: #856404; text-decoration: underline;">bento4.com/downloads</a>,
                                add to your system PATH, and restart the server. Downloads will fail without it.
                            </small>
                        </div>
                        <div id="musicVideosSearchLoading" class="loading">Loading music videos...</div>
                        <div id="musicVideosSearchEmpty" class="library-empty" style="display:none;">
                            <p>No music videos found</p>
                        </div>
                        <div id="musicVideosSearchGrid" class="library-grid"></div>
                        <div id="musicVideosSearchLoadMore" class="load-more" style="display:none;">
                            <button onclick="loadMoreSearchResults('music-videos')">Load More</button>
                        </div>
                    </div>

                    <!-- Podcasts Tab -->
                    <div id="podcastsSearchTab" class="tab-content">
                        <div id="podcastsSearchLoading" class="loading">Loading podcasts...</div>
                        <div id="podcastsSearchEmpty" class="library-empty" style="display:none;">
                            <p>No podcasts found</p>
                        </div>
                        <div id="podcastsSearchGrid" class="library-grid"></div>
                        <div id="podcastsSearchLoadMore" class="load-more" style="display:none;">
                            <button onclick="loadMoreSearchResults('podcasts')">Load More</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Queue Side Panel -->
            <div id="queuePanel" class="queue-panel">
                <div class="queue-header">
                    <h3>Download Queue</h3>
                </div>

                <div class="queue-controls">
                    <button id="pauseQueueBtn" onclick="pauseQueue()" class="queue-control-btn">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="6" y="4" width="4" height="16"></rect>
                            <rect x="14" y="4" width="4" height="16"></rect>
                        </svg>
                        Pause
                    </button>
                    <button onclick="clearCompleted()" class="queue-control-btn clear-btn">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                        Clear Completed
                    </button>
                </div>

                <div class="queue-status">
                    <div class="queue-stat">
                        <span class="queue-stat-label">Queued:</span>
                        <span id="queuedCount" class="queue-stat-value">0</span>
                    </div>
                    <div class="queue-stat">
                        <span class="queue-stat-label">Downloading:</span>
                        <span id="downloadingCount" class="queue-stat-value">0</span>
                    </div>
                    <div class="queue-stat">
                        <span class="queue-stat-label">Completed:</span>
                        <span id="completedCount" class="queue-stat-value">0</span>
                    </div>
                </div>

                <div id="queueList" class="queue-list">
                    <div class="queue-empty">
                        <div class="queue-empty-icon">
                            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#999" stroke-width="1.5">
                                <path d="M3 7v13a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7m-18 0h18M3 7l3-4h12l3 4M10 11v6m4-6v6"></path>
                            </svg>
                        </div>
                        <div>No downloads in queue</div>
                    </div>
                </div>
            </div>

            <!-- Episode Modal -->
            <div id="episodeModal" class="modal" style="display:none;">
                <div class="modal-content" style="max-width: 900px;">
                    <div class="modal-header">
                        <h3 id="episodeModalTitle">Podcast Episodes</h3>
                        <button onclick="closeEpisodeModal()" style="background: none; border: none; font-size: 28px; cursor: pointer; color: #666;">&times;</button>
                    </div>
                    <div class="modal-body">
                        <div id="episodeLoading" class="loading" style="display:none;">Loading episodes...</div>
                        <div id="episodeList" style="max-height: 600px; overflow-y: auto;"></div>
                        <div id="episodeEmpty" class="library-empty" style="display:none;">
                            <p>No episodes found</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            let ws = null;
            let sessionId = null;
            let totalTracks = 0;
            let completedTracks = 0;
            let skippedTracks = 0;
            let failedTracks = 0;

            function toggleCollapsible(header) {
                const content = header.nextElementSibling;
                content.classList.toggle('active');
            }

            function addLog(message, level = 'info') {
                const log = document.getElementById('progressLog');
                const line = document.createElement('div');
                line.className = level;
                line.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
                log.appendChild(line);
                log.scrollTop = log.scrollHeight;

                // Update stats based on log messages
                updateStatsFromLog(message, level);
            }

            function updateStatsFromLog(message, level) {
                // Extract total tracks from "Found X track(s) to download"
                const totalMatch = message.match(/Found (\\d+) track\\(s\\) to download/);
                if (totalMatch) {
                    totalTracks = parseInt(totalMatch[1]);
                    document.getElementById('totalTracks').textContent = totalTracks;
                }

                // Count completed tracks
                if (message.includes('Completed:') && level === 'success') {
                    completedTracks++;
                    document.getElementById('completedTracks').textContent = completedTracks;
                }

                // Count skipped tracks (file exists, not streamable, etc.)
                if (message.includes('Skipped:') && level === 'warning') {
                    skippedTracks++;
                    document.getElementById('skippedTracks').textContent = skippedTracks;
                }

                // Count failed tracks (unexpected errors)
                if (message.includes('Error:') && level === 'error') {
                    failedTracks++;
                    document.getElementById('failedTracks').textContent = failedTracks;
                }

                // Update progress percentage
                if (totalTracks > 0) {
                    const percent = Math.round(((completedTracks + skippedTracks + failedTracks) / totalTracks) * 100);
                    document.getElementById('progressPercent').textContent = percent + '%';
                }
            }

            function resetStats() {
                totalTracks = 0;
                completedTracks = 0;
                skippedTracks = 0;
                failedTracks = 0;
                document.getElementById('totalTracks').textContent = '0';
                document.getElementById('completedTracks').textContent = '0';
                document.getElementById('skippedTracks').textContent = '0';
                document.getElementById('failedTracks').textContent = '0';
                document.getElementById('progressPercent').textContent = '0%';
            }

            function updateStatus(text, active = false, cancelled = false) {
                document.getElementById('statusText').textContent = text;
                const indicator = document.getElementById('statusIndicator');
                indicator.classList.remove('active', 'cancelled');
                if (active) {
                    indicator.classList.add('active');
                } else if (cancelled) {
                    indicator.classList.add('cancelled');
                }
            }

            async function cancelDownload() {
                if (!sessionId) return;

                try {
                    const response = await fetch(`/api/cancel/${sessionId}`, {
                        method: 'POST',
                    });

                    if (response.ok) {
                        addLog('Cancellation requested...', 'warning');
                        updateStatus('Cancelling...', false, true);
                        document.getElementById('cancelBtn').disabled = true;
                    }
                } catch (error) {
                    addLog(`Failed to cancel: ${error.message}`, 'error');
                }
            }

            function connectWebSocket(sessionId) {
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                ws = new WebSocket(`${protocol}//${window.location.host}/ws/${sessionId}`);

                ws.onopen = () => {
                    addLog('Connected to server', 'success');
                    updateStatus('Connected - Processing...', true);
                    document.getElementById('cancelBtn').disabled = false;
                };

                ws.onmessage = (event) => {
                    const data = JSON.parse(event.data);

                    if (data.type === 'log') {
                        addLog(data.message, data.level || 'info');

                        // Check for cancellation message
                        if (data.message.includes('cancelled')) {
                            updateStatus('Cancelled', false, true);
                            document.getElementById('downloadBtn').disabled = false;
                            document.getElementById('cancelBtn').disabled = true;
                        }
                    } else if (data.type === 'progress') {
                        addLog(data.message, 'info');
                    } else if (data.type === 'error') {
                        addLog(`ERROR: ${data.message}`, 'error');
                        updateStatus('Error occurred', false);
                    } else if (data.type === 'complete') {
                        addLog('Download completed!', 'success');
                        updateStatus('Completed', false);
                        document.getElementById('downloadBtn').disabled = false;
                        document.getElementById('cancelBtn').disabled = true;
                    }
                };

                ws.onerror = (error) => {
                    addLog('WebSocket error occurred', 'error');
                    updateStatus('Connection error', false);
                    document.getElementById('downloadBtn').disabled = false;
                    document.getElementById('cancelBtn').disabled = true;
                };

                ws.onclose = () => {
                    addLog('Connection closed', 'warning');
                    if (document.getElementById('statusIndicator').classList.contains('active')) {
                        updateStatus('Disconnected', false);
                    }
                };
            }

            document.getElementById('downloadForm').addEventListener('submit', async (e) => {
                e.preventDefault();

                const formData = new FormData(e.target);
                const urls = formData.get('urls').split('\\n').filter(u => u.trim());

                // Read settings from Settings tab (not from form)
                const payload = {
                    urls: urls,
                    cookies_path: document.getElementById('cookiesPath').value || null,
                    output_path: document.getElementById('outputPath').value || null,
                    song_codec: document.getElementById('songCodec').value || null,
                    cover_size: document.getElementById('coverSize').value ? parseInt(document.getElementById('coverSize').value) : null,
                    music_video_resolution: document.getElementById('musicVideoResolution').value || null,
                    cover_format: document.getElementById('coverFormat').value || null,
                    no_cover: document.getElementById('noCover').checked,
                    no_lyrics: document.getElementById('noLyrics').checked,
                    extra_tags: document.getElementById('extraTags').checked,
                    enable_retry_delay: document.getElementById('enableRetryDelay').checked,
                    max_retries: parseInt(document.getElementById('maxRetries').value) || 3,
                    retry_delay: parseInt(document.getElementById('retryDelay').value) || 60,
                    song_delay: parseFloat(document.getElementById('songDelay').value) || 0,
                    queue_item_delay: parseFloat(document.getElementById('queueItemDelay').value) || 0,
                    continue_on_error: document.getElementById('continueOnError').checked,
                };

                document.getElementById('downloadBtn').disabled = true;
                document.getElementById('cancelBtn').disabled = true;
                document.getElementById('progressContainer').classList.add('active');
                document.getElementById('progressLog').innerHTML = '';
                resetStats();

                addLog('Starting download session...', 'info');
                updateStatus('Initializing...', true);

                try {
                    const response = await fetch('/api/download', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify(payload),
                    });

                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }

                    const data = await response.json();
                    sessionId = data.session_id;
                    addLog(`Session created: ${sessionId}`, 'success');

                    connectWebSocket(sessionId);
                } catch (error) {
                    addLog(`Failed to start download: ${error.message}`, 'error');
                    updateStatus('Failed to start', false);
                    document.getElementById('downloadBtn').disabled = false;
                    document.getElementById('cancelBtn').disabled = true;
                }
            });

            // Cancel button event listener
            document.getElementById('cancelBtn').addEventListener('click', cancelDownload);

            // Library Browser functionality
            let albumsOffset = 0;
            let playlistsOffset = 0;
            let songsOffset = 0;
            let currentLibraryTab = 'albums';

            function switchView(view, clickedElement) {
                // Update nav tabs
                document.querySelectorAll('.nav-tabs > .nav-tab').forEach(tab => {
                    tab.classList.remove('active');
                });
                if (clickedElement) {
                    clickedElement.classList.add('active');
                }

                // Show/hide views
                document.getElementById('libraryView').classList.toggle('active', view === 'library');
                document.getElementById('downloadsView').classList.toggle('active', view === 'downloads');
                document.getElementById('settingsView').classList.toggle('active', view === 'settings');
                document.getElementById('searchView').classList.toggle('active', view === 'search');
                document.getElementById('monitorView').classList.toggle('active', view === 'monitor');

                // Load library data on first view if needed
                if (view === 'library' && !document.getElementById('albumsGrid').hasChildNodes()) {
                    loadLibraryAlbums();
                }

                // Load monitor status when switching to monitor view
                if (view === 'monitor') {
                    loadPlaylistsForSelector();
                    refreshMonitorStatus();
                }

                // Focus search input when switching to search tab
                if (view === 'search') {
                    setTimeout(() => {
                        document.getElementById('searchInput').focus();
                    }, 100);
                }
            }

            function switchLibraryTab(tab, clickedElement) {
                // Update tabs
                document.querySelectorAll('#libraryView .nav-tabs > .nav-tab').forEach(t => {
                    t.classList.remove('active');
                });
                if (clickedElement) {
                    clickedElement.classList.add('active');
                }

                // Show/hide tab content
                document.querySelectorAll('.tab-content').forEach(content => {
                    content.classList.remove('active');
                });
                document.getElementById(tab + 'Tab').classList.add('active');

                currentLibraryTab = tab;

                // Load data if not already loaded
                if (tab === 'albums' && !document.getElementById('albumsGrid').hasChildNodes()) {
                    loadLibraryAlbums();
                } else if (tab === 'playlists' && !document.getElementById('playlistsGrid').hasChildNodes()) {
                    loadLibraryPlaylists();
                } else if (tab === 'songs' && !document.getElementById('songsGrid').hasChildNodes()) {
                    loadLibrarySongs();
                }
            }

            async function loadLibraryAlbums(offset = 0) {
                const loading = document.getElementById('albumsLoading');
                const grid = document.getElementById('albumsGrid');
                const empty = document.getElementById('albumsEmpty');
                const loadMore = document.getElementById('albumsLoadMore');
                const errorDiv = document.getElementById('libraryError');

                if (offset === 0) {
                    loading.style.display = 'block';
                    grid.innerHTML = '';
                    empty.style.display = 'none';
                    loadMore.style.display = 'none';
                    errorDiv.style.display = 'none';
                }

                try {
                    const response = await fetch(`/api/library/albums?limit=50&offset=${offset}`);
                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to load albums');
                    }

                    const data = await response.json();
                    loading.style.display = 'none';

                    if (data.data.length === 0 && offset === 0) {
                        empty.style.display = 'block';
                        return;
                    }

                    data.data.forEach(album => {
                        const item = createLibraryItem(album, 'album');
                        grid.appendChild(item);
                    });

                    if (data.has_more) {
                        albumsOffset = data.next_offset;
                        loadMore.style.display = 'block';
                    } else {
                        loadMore.style.display = 'none';
                    }
                } catch (error) {
                    loading.style.display = 'none';
                    errorDiv.textContent = error.message;
                    errorDiv.style.display = 'block';
                }
            }

            async function loadLibraryPlaylists(offset = 0) {
                const loading = document.getElementById('playlistsLoading');
                const grid = document.getElementById('playlistsGrid');
                const empty = document.getElementById('playlistsEmpty');
                const loadMore = document.getElementById('playlistsLoadMore');
                const errorDiv = document.getElementById('libraryError');

                if (offset === 0) {
                    loading.style.display = 'block';
                    grid.innerHTML = '';
                    empty.style.display = 'none';
                    loadMore.style.display = 'none';
                    errorDiv.style.display = 'none';
                }

                try {
                    const response = await fetch(`/api/library/playlists?limit=50&offset=${offset}`);
                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to load playlists');
                    }

                    const data = await response.json();
                    loading.style.display = 'none';

                    if (data.data.length === 0 && offset === 0) {
                        empty.style.display = 'block';
                        return;
                    }

                    data.data.forEach(playlist => {
                        const item = createLibraryItem(playlist, 'playlist');
                        grid.appendChild(item);
                    });

                    if (data.has_more) {
                        playlistsOffset = data.next_offset;
                        loadMore.style.display = 'block';
                    } else {
                        loadMore.style.display = 'none';
                    }
                } catch (error) {
                    loading.style.display = 'none';
                    errorDiv.textContent = error.message;
                    errorDiv.style.display = 'block';
                }
            }

            async function loadLibrarySongs(offset = 0) {
                const loading = document.getElementById('songsLoading');
                const grid = document.getElementById('songsGrid');
                const empty = document.getElementById('songsEmpty');
                const loadMore = document.getElementById('songsLoadMore');
                const errorDiv = document.getElementById('libraryError');

                if (offset === 0) {
                    loading.style.display = 'block';
                    grid.innerHTML = '';
                    empty.style.display = 'none';
                    loadMore.style.display = 'none';
                    errorDiv.style.display = 'none';
                }

                try {
                    const response = await fetch(`/api/library/songs?limit=50&offset=${offset}`);
                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to load songs');
                    }

                    const data = await response.json();
                    loading.style.display = 'none';

                    if (data.data.length === 0 && offset === 0) {
                        empty.style.display = 'block';
                        return;
                    }

                    data.data.forEach(song => {
                        const item = createLibraryItem(song, 'song');
                        grid.appendChild(item);
                    });

                    if (data.has_more) {
                        songsOffset = data.next_offset;
                        loadMore.style.display = 'block';
                    } else {
                        loadMore.style.display = 'none';
                    }
                } catch (error) {
                    loading.style.display = 'none';
                    errorDiv.textContent = error.message;
                    errorDiv.style.display = 'block';
                }
            }

            function createLibraryItem(item, type) {
                const div = document.createElement('div');
                div.className = 'library-item';

                const img = document.createElement('img');
                img.src = item.artwork || 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="300" height="300"%3E%3Crect fill="%23ddd" width="300" height="300"/%3E%3C/svg%3E';
                img.alt = item.name;

                const title = document.createElement('div');
                title.className = 'library-item-title';
                title.textContent = item.name;

                const subtitle = document.createElement('div');
                subtitle.className = 'library-item-subtitle';
                if (type === 'song') {
                    subtitle.textContent = `${item.artist} • ${item.album}`;
                } else if (type === 'album') {
                    subtitle.textContent = `${item.artist} • ${item.trackCount} songs`;
                } else if (type === 'playlist') {
                    subtitle.textContent = `${item.trackCount} songs`;
                }

                // Create download button for albums/playlists
                if (type === 'album' || type === 'playlist') {
                    const btnGroup = document.createElement('div');
                    btnGroup.className = 'btn-group';

                    if (type === 'album') {
                        // Albums: show both "Library Tracks" and "Full Album" buttons
                        const mainBtn = document.createElement('button');
                        mainBtn.textContent = 'Library Tracks';
                        mainBtn.className = 'btn-primary';
                        mainBtn.onclick = (e) => {
                            e.stopPropagation();
                            downloadLibraryItem(item.id, type, item.name, item.artist, false);
                        };

                        const fullBtn = document.createElement('button');
                        fullBtn.textContent = 'Full Album';
                        fullBtn.className = 'btn-secondary';
                        fullBtn.onclick = (e) => {
                            e.stopPropagation();
                            downloadLibraryItem(item.id, type, item.name, item.artist, true);
                        };

                        btnGroup.appendChild(mainBtn);
                        btnGroup.appendChild(fullBtn);
                    } else {
                        // Playlists: show only single "Download" button (library tracks = full playlist)
                        const btn = document.createElement('button');
                        btn.textContent = 'Download';
                        btn.className = 'btn-primary';
                        btn.onclick = (e) => {
                            e.stopPropagation();
                            downloadLibraryItem(item.id, type, item.name, item.artist, false);
                        };

                        btnGroup.appendChild(btn);
                    }

                    div.appendChild(img);
                    div.appendChild(title);
                    div.appendChild(subtitle);
                    div.appendChild(btnGroup);
                } else {
                    // For songs, just a single download button
                    const btn = document.createElement('button');
                    btn.textContent = 'Download';
                    btn.onclick = () => downloadLibraryItem(item.id, type, item.name, item.artist, false);

                    div.appendChild(img);
                    div.appendChild(title);
                    div.appendChild(subtitle);
                    div.appendChild(btn);
                }

                return div;
            }

            async function downloadLibraryItem(libraryId, mediaType, itemName, itemArtist, downloadFull = false) {
                try {
                    // Format display title
                    let displayTitle = itemName;
                    if (itemArtist && mediaType !== 'playlist') {
                        displayTitle = `${itemName} - ${itemArtist}`;
                    }

                    // Add indicator to display title if downloading full album/playlist
                    if (downloadFull) {
                        displayTitle = `${displayTitle} (Full)`;
                    }

                    const response = await fetch('/api/library/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            library_id: libraryId,
                            media_type: mediaType,
                            display_title: displayTitle,
                            download_full: downloadFull,
                            cookies_path: document.getElementById('cookiesPath').value,
                            output_path: document.getElementById('outputPath').value,
                            song_codec: document.getElementById('songCodec').value || null,
                            cover_size: document.getElementById('coverSize').value ? parseInt(document.getElementById('coverSize').value) : null,
                            music_video_resolution: document.getElementById('musicVideoResolution').value || null,
                            cover_format: document.getElementById('coverFormat').value || null,
                            no_cover: document.getElementById('noCover').checked,
                            no_lyrics: document.getElementById('noLyrics').checked,
                            extra_tags: document.getElementById('extraTags').checked,
                            enable_retry_delay: document.getElementById('enableRetryDelay').checked,
                            max_retries: parseInt(document.getElementById('maxRetries').value) || 3,
                            retry_delay: parseInt(document.getElementById('retryDelay').value) || 60,
                            song_delay: parseFloat(document.getElementById('songDelay').value) || 0,
                            queue_item_delay: parseFloat(document.getElementById('queueItemDelay').value) || 0,
                            continue_on_error: document.getElementById('continueOnError').checked,
                        })
                    });

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to start download');
                    }

                    const data = await response.json();

                    // Refresh queue to show the new item
                    await refreshQueueStatus();
                } catch (error) {
                    alert(`Failed to start download: ${error.message}`);
                }
            }

            function loadMoreAlbums() {
                loadLibraryAlbums(albumsOffset);
            }

            function loadMorePlaylists() {
                loadLibraryPlaylists(playlistsOffset);
            }

            function loadMoreSongs() {
                loadLibrarySongs(songsOffset);
            }

            // Search functionality
            let currentSearchQuery = '';
            let currentSearchTab = 'all';
            let searchOffsets = {
                songs: 0,
                albums: 0,
                artists: 0,
                playlists: 0,
                'music-videos': 0
            };

            async function performSearch() {
                const query = document.getElementById('searchInput').value.trim();

                if (!query) {
                    return;
                }

                currentSearchQuery = query;
                searchOffsets = { songs: 0, albums: 0, artists: 0, playlists: 0 };

                document.getElementById('allLoading').style.display = 'block';
                document.getElementById('allGrid').innerHTML = '';
                document.getElementById('allEmpty').style.display = 'none';
                document.getElementById('searchError').style.display = 'none';

                try {
                    const response = await fetch(`/api/search?term=${encodeURIComponent(query)}&limit=25`);

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Search failed');
                    }

                    const data = await response.json();
                    displayAllResults(data);

                } catch (error) {
                    document.getElementById('allLoading').style.display = 'none';
                    document.getElementById('searchError').textContent = error.message;
                    document.getElementById('searchError').style.display = 'block';
                }
            }

            function displayAllResults(data) {
                const loading = document.getElementById('allLoading');
                const grid = document.getElementById('allGrid');
                const empty = document.getElementById('allEmpty');

                loading.style.display = 'none';
                grid.innerHTML = '';

                let hasResults = false;

                if (data.albums && data.albums.length > 0) {
                    hasResults = true;
                    const section = createResultSection('Albums', data.albums, 'album');
                    grid.appendChild(section);
                }

                if (data.songs && data.songs.length > 0) {
                    hasResults = true;
                    const section = createResultSection('Songs', data.songs, 'song');
                    grid.appendChild(section);
                }

                if (data.artists && data.artists.length > 0) {
                    hasResults = true;
                    const section = createResultSection('Artists', data.artists, 'artist');
                    grid.appendChild(section);
                }

                if (data.playlists && data.playlists.length > 0) {
                    hasResults = true;
                    const section = createResultSection('Playlists', data.playlists, 'playlist');
                    grid.appendChild(section);
                }

                if (data['music-videos'] && data['music-videos'].length > 0) {
                    hasResults = true;
                    const section = createResultSection('Music Videos', data['music-videos'], 'music-video');
                    grid.appendChild(section);
                }

                if (!hasResults) {
                    empty.style.display = 'block';
                }
            }

            function createResultSection(title, items, type) {
                const section = document.createElement('div');
                section.className = 'result-section';
                section.style.marginBottom = '30px';

                const heading = document.createElement('h3');
                heading.textContent = title;
                heading.style.marginBottom = '15px';
                heading.style.fontSize = '20px';
                heading.style.fontWeight = '600';
                section.appendChild(heading);

                const grid = document.createElement('div');
                grid.className = 'library-grid';

                const displayItems = items.slice(0, 6);
                displayItems.forEach(item => {
                    const itemElement = createSearchResultItem(item, type);
                    grid.appendChild(itemElement);
                });

                section.appendChild(grid);

                if (items.length > 6) {
                    const viewAll = document.createElement('button');
                    viewAll.textContent = `View All ${items.length} ${title}`;
                    viewAll.className = 'btn-secondary';
                    viewAll.style.marginTop = '10px';
                    viewAll.onclick = () => {
                        switchSearchTab(type === 'song' ? 'songs' : type + 's', null);
                    };
                    section.appendChild(viewAll);
                }

                return section;
            }

            function createSearchResultItem(item, type) {
                const div = document.createElement('div');
                div.className = 'library-item';

                const img = document.createElement('img');
                img.src = item.artwork || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180"><rect fill="%23ddd" width="180" height="180"/></svg>';
                img.alt = item.name;
                div.appendChild(img);

                const title = document.createElement('div');
                title.className = 'library-item-title';
                title.textContent = item.name;
                div.appendChild(title);

                const subtitle = document.createElement('div');
                subtitle.className = 'library-item-subtitle';
                if (type === 'song') {
                    subtitle.textContent = `${item.artist}${item.album ? ' • ' + item.album : ''}`;
                } else if (type === 'album' || type === 'playlist') {
                    subtitle.textContent = item.artist || item.curator || '';
                } else if (type === 'artist') {
                    subtitle.textContent = 'Artist';
                } else if (type === 'music-video') {
                    const duration = item.duration ? ` • ${Math.floor(item.duration / 60)}:${(item.duration % 60).toString().padStart(2, '0')}` : '';
                    subtitle.textContent = `${item.artist}${duration}`;
                } else if (type === 'podcast') {
                    subtitle.textContent = `${item.author || 'Unknown'}${item.episodeCount ? ' • ' + item.episodeCount + ' episodes' : ''}`;
                }
                div.appendChild(subtitle);

                if (type === 'podcast') {
                    // Podcast-specific button
                    const btnContainer = document.createElement('div');
                    btnContainer.style.marginTop = '10px';

                    const viewEpisodesBtn = document.createElement('button');
                    viewEpisodesBtn.textContent = 'View Episodes';
                    viewEpisodesBtn.className = 'btn-primary';
                    viewEpisodesBtn.style.width = '100%';
                    viewEpisodesBtn.onclick = () => viewPodcastEpisodes(item.id, item.name);
                    btnContainer.appendChild(viewEpisodesBtn);

                    div.appendChild(btnContainer);
                } else if (type !== 'artist') {
                    const btnContainer = document.createElement('div');
                    btnContainer.style.marginTop = '10px';

                    const downloadBtn = document.createElement('button');
                    downloadBtn.textContent = 'Download';
                    downloadBtn.className = 'btn-primary';
                    downloadBtn.style.width = '100%';
                    downloadBtn.onclick = () => downloadSearchResult(item, type);
                    btnContainer.appendChild(downloadBtn);

                    div.appendChild(btnContainer);
                } else {
                    // Artist-specific buttons
                    const btnContainer = document.createElement('div');
                    btnContainer.style.marginTop = '10px';
                    btnContainer.style.display = 'flex';
                    btnContainer.style.flexDirection = 'column';
                    btnContainer.style.gap = '6px';

                    // Download Discography button
                    const discographyBtn = document.createElement('button');
                    discographyBtn.textContent = 'Download Discography';
                    discographyBtn.className = 'btn-primary';
                    discographyBtn.style.width = '100%';
                    discographyBtn.onclick = () => downloadArtistDiscography(item);
                    btnContainer.appendChild(discographyBtn);

                    // View Artist button
                    const viewBtn = document.createElement('button');
                    viewBtn.textContent = 'View All Content';
                    viewBtn.className = 'btn-secondary';
                    viewBtn.style.width = '100%';
                    viewBtn.onclick = () => viewArtistContent(item);
                    btnContainer.appendChild(viewBtn);

                    div.appendChild(btnContainer);
                }

                return div;
            }

            async function downloadSearchResult(item, type) {
                try {
                    const url = item.url;

                    if (!url) {
                        alert('Cannot download this item - no URL available');
                        return;
                    }

                    // Helper function to get value or null (not empty string)
                    const getValueOrNull = (id) => {
                        const element = document.getElementById(id);
                        if (!element) return null;
                        const value = element.value;
                        return value ? value : null;
                    };

                    // Helper function to get integer value or null
                    const getIntOrNull = (id) => {
                        const element = document.getElementById(id);
                        if (!element) return null;
                        const value = element.value;
                        return value ? parseInt(value) : null;
                    };

                    const response = await fetch('/api/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            urls: [url],
                            cookies_path: getValueOrNull('cookiesPath'),
                            output_path: getValueOrNull('outputPath'),
                            song_codec: getValueOrNull('songCodec'),
                            music_video_codec: getValueOrNull('musicVideoCodec'),
                            music_video_resolution: getValueOrNull('musicVideoResolution'),
                            cover_size: getIntOrNull('coverSize'),
                            cover_format: getValueOrNull('coverFormat'),
                            no_cover: document.getElementById('noCover').checked,
                            no_lyrics: document.getElementById('noLyrics').checked,
                            extra_tags: document.getElementById('extraTags').checked,
                            enable_retry_delay: document.getElementById('enableRetryDelay').checked,
                            max_retries: parseInt(document.getElementById('maxRetries').value) || 3,
                            retry_delay: parseInt(document.getElementById('retryDelay').value) || 60,
                            song_delay: parseFloat(document.getElementById('songDelay').value) || 0,
                            queue_item_delay: parseFloat(document.getElementById('queueItemDelay').value) || 0,
                            continue_on_error: document.getElementById('continueOnError').checked,
                        })
                    });

                    if (!response.ok) {
                        const error = await response.json();
                        alert(`Download failed: ${error.detail || 'Unknown error'}`);
                    }

                } catch (error) {
                    alert(`Error: ${error.message}`);
                }
            }

            async function downloadArtistDiscography(artist) {
                try {
                    // Get user preference for including music videos
                    const includeVideos = document.getElementById('includeVideosInDiscography').checked;

                    // Show loading indicator
                    const originalBtn = event.target;
                    const originalText = originalBtn.textContent;
                    originalBtn.textContent = 'Loading...';
                    originalBtn.disabled = true;

                    // Fetch artist's catalog
                    const response = await fetch(
                        `/api/artist/${artist.id}/catalog?include_music_videos=${includeVideos}`
                    );

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to fetch artist catalog');
                    }

                    const catalog = await response.json();

                    // Restore button
                    originalBtn.textContent = originalText;
                    originalBtn.disabled = false;

                    if (catalog.urls.length === 0) {
                        alert(`No content found for ${artist.name}`);
                        return;
                    }

                    // Confirm download
                    const videoText = includeVideos && catalog.music_videos.length > 0
                        ? ` and ${catalog.music_videos.length} music videos`
                        : '';
                    const confirmed = confirm(
                        `Download ${catalog.albums.length} albums${videoText} by ${artist.name}?\n\n` +
                        `This will add ${catalog.total_items} items to the download queue.`
                    );

                    if (!confirmed) {
                        return;
                    }

                    // Submit download request
                    const downloadResponse = await fetch('/api/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            urls: catalog.urls,
                            cookies_path: document.getElementById('cookiesPath').value,
                            output_path: document.getElementById('outputPath').value,
                            song_codec: document.getElementById('songCodec').value,
                            music_video_resolution: document.getElementById('musicVideoResolution').value,
                            cover_size: parseInt(document.getElementById('coverSize').value) || null,
                            cover_format: document.getElementById('coverFormat').value,
                            no_cover: document.getElementById('noCover').checked,
                            no_lyrics: document.getElementById('noLyrics').checked,
                            extra_tags: document.getElementById('extraTags').checked,
                            enable_retry_delay: document.getElementById('enableRetryDelay').checked,
                            max_retries: parseInt(document.getElementById('maxRetries').value) || 3,
                            retry_delay: parseInt(document.getElementById('retryDelay').value) || 60,
                            song_delay: parseFloat(document.getElementById('songDelay').value) || 0,
                            queue_item_delay: parseFloat(document.getElementById('queueItemDelay').value) || 0,
                            continue_on_error: document.getElementById('continueOnError').checked,
                            display_title: `${artist.name} - Discography`,
                            display_type: 'Discography',
                        })
                    });

                    if (!downloadResponse.ok) {
                        const error = await downloadResponse.json();
                        alert(`Download failed: ${error.detail || 'Unknown error'}`);
                    }

                } catch (error) {
                    alert(`Error: ${error.message}`);
                }
            }

            let currentArtist = null;
            let currentArtistCatalog = null;
            let selectedArtistItems = new Set();

            async function viewArtistContent(artist) {
                // Store current artist for use in download
                currentArtist = artist;

                // Show modal
                document.getElementById('artistModal').style.display = 'flex';
                document.getElementById('artistModalTitle').textContent = `${artist.name} - All Content`;
                document.getElementById('artistModalLoading').style.display = 'block';
                document.getElementById('artistModalContent').style.display = 'none';

                // Animated loading messages
                const loadingMessages = [
                    'Fetching artist catalog...',
                    'Loading album details...',
                    'Retrieving artwork and metadata...',
                    'Almost there...'
                ];
                let messageIndex = 0;
                const loadingTextElement = document.getElementById('artistModalLoadingText');
                const loadingProgressElement = document.getElementById('artistModalLoadingProgress');

                // Update loading message every 1.5 seconds
                const loadingInterval = setInterval(() => {
                    messageIndex = (messageIndex + 1) % loadingMessages.length;
                    loadingTextElement.textContent = loadingMessages[messageIndex];
                }, 1500);

                try {
                    // Show initial progress
                    loadingProgressElement.textContent = 'This may take a few seconds for artists with many albums...';

                    // Fetch artist catalog (always include music videos for viewing)
                    const response = await fetch(`/api/artist/${artist.id}/catalog?include_music_videos=true`);

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to fetch artist content');
                    }

                    const catalog = await response.json();
                    currentArtistCatalog = catalog;
                    selectedArtistItems.clear();

                    // Clear loading interval
                    clearInterval(loadingInterval);

                    // Hide loading, show content
                    document.getElementById('artistModalLoading').style.display = 'none';
                    document.getElementById('artistModalContent').style.display = 'block';

                    // Display albums
                    document.getElementById('artistAlbumsCount').textContent = catalog.albums.length;
                    const albumsGrid = document.getElementById('artistAlbumsGrid');
                    albumsGrid.innerHTML = '';

                    catalog.albums.forEach(album => {
                        const item = document.createElement('div');
                        item.className = 'library-item';
                        item.style.position = 'relative';

                        // Checkbox overlay
                        const checkbox = document.createElement('input');
                        checkbox.type = 'checkbox';
                        checkbox.setAttribute('data-url', album.url);
                        checkbox.onchange = function() { toggleArtistItemSelection(this); };
                        checkbox.style.position = 'absolute';
                        checkbox.style.top = '10px';
                        checkbox.style.left = '10px';
                        checkbox.style.zIndex = '10';
                        checkbox.style.width = '20px';
                        checkbox.style.height = '20px';
                        checkbox.style.cursor = 'pointer';
                        item.appendChild(checkbox);

                        // Artwork
                        const img = document.createElement('img');
                        img.src = album.artwork || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180"><rect fill="%23ddd" width="180" height="180"/></svg>';
                        img.alt = album.name;
                        item.appendChild(img);

                        // Title
                        const title = document.createElement('div');
                        title.className = 'library-item-title';
                        title.textContent = album.name;
                        item.appendChild(title);

                        // Subtitle
                        const subtitle = document.createElement('div');
                        subtitle.className = 'library-item-subtitle';
                        subtitle.textContent = `${album.trackCount} tracks`;
                        item.appendChild(subtitle);

                        albumsGrid.appendChild(item);
                    });

                    // Display music videos if any
                    if (catalog.music_videos.length > 0) {
                        document.getElementById('artistVideosSection').style.display = 'block';
                        document.getElementById('artistVideosCount').textContent = catalog.music_videos.length;
                        const videosGrid = document.getElementById('artistVideosGrid');
                        videosGrid.innerHTML = '';

                        catalog.music_videos.forEach(video => {
                            const item = document.createElement('div');
                            item.className = 'library-item';
                            item.style.position = 'relative';

                            // Checkbox overlay
                            const checkbox = document.createElement('input');
                            checkbox.type = 'checkbox';
                            checkbox.setAttribute('data-url', video.url);
                            checkbox.onchange = function() { toggleArtistItemSelection(this); };
                            checkbox.style.position = 'absolute';
                            checkbox.style.top = '10px';
                            checkbox.style.left = '10px';
                            checkbox.style.zIndex = '10';
                            checkbox.style.width = '20px';
                            checkbox.style.height = '20px';
                            checkbox.style.cursor = 'pointer';
                            item.appendChild(checkbox);

                            // Artwork
                            const img = document.createElement('img');
                            img.src = video.artwork || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180"><rect fill="%23ddd" width="180" height="180"/></svg>';
                            img.alt = video.name;
                            item.appendChild(img);

                            // Title
                            const title = document.createElement('div');
                            title.className = 'library-item-title';
                            title.textContent = video.name;
                            item.appendChild(title);

                            // Subtitle with duration
                            const subtitle = document.createElement('div');
                            subtitle.className = 'library-item-subtitle';
                            const duration = video.duration ? `${Math.floor(video.duration / 60)}:${(video.duration % 60).toString().padStart(2, '0')}` : '';
                            subtitle.textContent = duration;
                            item.appendChild(subtitle);

                            videosGrid.appendChild(item);
                        });
                    } else {
                        document.getElementById('artistVideosSection').style.display = 'none';
                    }

                } catch (error) {
                    // Clear loading interval on error
                    clearInterval(loadingInterval);
                    alert(`Error: ${error.message}`);
                    closeArtistModal();
                }
            }

            function toggleArtistItemSelection(checkbox) {
                const url = checkbox.getAttribute('data-url');
                if (checkbox.checked) {
                    selectedArtistItems.add(url);
                } else {
                    selectedArtistItems.delete(url);
                }
            }

            async function downloadSelectedArtistContent() {
                if (selectedArtistItems.size === 0) {
                    alert('Please select at least one item to download');
                    return;
                }

                try {
                    const urls = Array.from(selectedArtistItems);

                    // Determine display title based on selection
                    let displayTitle = currentArtist ? `${currentArtist.name} - Selected Content` : 'Selected Content';
                    if (urls.length === 1) {
                        displayTitle = currentArtist ? `${currentArtist.name} - 1 item` : '1 item';
                    } else {
                        displayTitle = currentArtist ? `${currentArtist.name} - ${urls.length} items` : `${urls.length} items`;
                    }

                    const response = await fetch('/api/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            urls: urls,
                            cookies_path: document.getElementById('cookiesPath').value,
                            output_path: document.getElementById('outputPath').value,
                            song_codec: document.getElementById('songCodec').value,
                            music_video_resolution: document.getElementById('musicVideoResolution').value,
                            cover_size: parseInt(document.getElementById('coverSize').value) || null,
                            cover_format: document.getElementById('coverFormat').value,
                            no_cover: document.getElementById('noCover').checked,
                            no_lyrics: document.getElementById('noLyrics').checked,
                            extra_tags: document.getElementById('extraTags').checked,
                            enable_retry_delay: document.getElementById('enableRetryDelay').checked,
                            max_retries: parseInt(document.getElementById('maxRetries').value) || 3,
                            retry_delay: parseInt(document.getElementById('retryDelay').value) || 60,
                            song_delay: parseFloat(document.getElementById('songDelay').value) || 0,
                            queue_item_delay: parseFloat(document.getElementById('queueItemDelay').value) || 0,
                            continue_on_error: document.getElementById('continueOnError').checked,
                            display_title: displayTitle,
                            display_type: 'Artist',
                        })
                    });

                    if (response.ok) {
                        closeArtistModal();
                    } else {
                        const error = await response.json();
                        alert(`Download failed: ${error.detail || 'Unknown error'}`);
                    }

                } catch (error) {
                    alert(`Error: ${error.message}`);
                }
            }

            function closeArtistModal() {
                document.getElementById('artistModal').style.display = 'none';
                currentArtistCatalog = null;
                selectedArtistItems.clear();
            }

            function switchSearchTab(tab, clickedElement) {
                const tabs = document.querySelectorAll('#searchView .nav-tabs > .nav-tab');
                tabs.forEach(t => t.classList.remove('active'));
                if (clickedElement) {
                    clickedElement.classList.add('active');
                } else {
                    tabs.forEach(t => {
                        const tabText = t.textContent.toLowerCase();
                        if ((tab === 'songs' && tabText === 'songs') ||
                            (tab === 'albums' && tabText === 'albums') ||
                            (tab === 'artists' && tabText === 'artists') ||
                            (tab === 'playlists' && tabText === 'playlists') ||
                            (tab === 'podcasts' && tabText === 'podcasts') ||
                            (tab === 'all' && tabText === 'all')) {
                            t.classList.add('active');
                        }
                    });
                }

                document.getElementById('allTab').classList.toggle('active', tab === 'all');
                document.getElementById('songsSearchTab').classList.toggle('active', tab === 'songs');
                document.getElementById('albumsSearchTab').classList.toggle('active', tab === 'albums');
                document.getElementById('artistsSearchTab').classList.toggle('active', tab === 'artists');
                document.getElementById('playlistsSearchTab').classList.toggle('active', tab === 'playlists');
                document.getElementById('musicVideosSearchTab').classList.toggle('active', tab === 'music-videos');
                document.getElementById('podcastsSearchTab').classList.toggle('active', tab === 'podcasts');

                currentSearchTab = tab;

                if (tab !== 'all' && currentSearchQuery) {
                    loadSearchTabResults(tab);
                }
            }

            async function loadSearchTabResults(type, loadMore = false) {
                const offset = loadMore ? searchOffsets[type] : 0;

                if (!loadMore) {
                    searchOffsets[type] = 0;
                }

                const loading = document.getElementById(`${type}SearchLoading`);
                const grid = document.getElementById(`${type}SearchGrid`);
                const empty = document.getElementById(`${type}SearchEmpty`);
                const loadMoreBtn = document.getElementById(`${type}SearchLoadMore`);
                const errorDiv = document.getElementById('searchError');

                if (!loadMore) {
                    loading.style.display = 'block';
                    grid.innerHTML = '';
                    empty.style.display = 'none';
                    loadMoreBtn.style.display = 'none';
                    errorDiv.style.display = 'none';
                }

                try {
                    let response, data, results;

                    // Podcasts use iTunes Search API, not Apple Music API
                    if (type === 'podcasts') {
                        response = await fetch(
                            `/api/podcasts/search?term=${encodeURIComponent(currentSearchQuery)}&limit=50&offset=${offset}`
                        );

                        if (!response.ok) {
                            const error = await response.json();
                            throw new Error(error.detail || 'Podcast search failed');
                        }

                        data = await response.json();
                        results = data.podcasts || [];
                    } else {
                        // Apple Music search
                        response = await fetch(
                            `/api/search?term=${encodeURIComponent(currentSearchQuery)}&types=${type}&limit=50&offset=${offset}`
                        );

                        if (!response.ok) {
                            const error = await response.json();
                            throw new Error(error.detail || 'Search failed');
                        }

                        data = await response.json();
                        results = data[type] || [];
                    }

                    loading.style.display = 'none';

                    if (results.length === 0 && offset === 0) {
                        empty.style.display = 'block';
                        return;
                    }

                    const singularType = type === 'podcasts' ? 'podcast' : type.slice(0, -1);
                    results.forEach(item => {
                        const itemElement = createSearchResultItem(item, singularType);
                        grid.appendChild(itemElement);
                    });

                    if (data.has_more) {
                        searchOffsets[type] = data.next_offset;
                        loadMoreBtn.style.display = 'block';
                    } else {
                        loadMoreBtn.style.display = 'none';
                    }

                } catch (error) {
                    loading.style.display = 'none';
                    errorDiv.textContent = error.message;
                    errorDiv.style.display = 'block';
                }
            }

            function loadMoreSearchResults(type) {
                loadSearchTabResults(type, true);
            }

            // Podcast episode functions
            async function viewPodcastEpisodes(podcastId, podcastName) {
                document.getElementById('episodeModalTitle').textContent = podcastName;
                document.getElementById('episodeModal').style.display = 'flex';
                document.getElementById('episodeLoading').style.display = 'block';
                document.getElementById('episodeList').innerHTML = '';
                document.getElementById('episodeEmpty').style.display = 'none';

                // Store podcast name for downloads
                window.currentPodcastName = podcastName;

                try {
                    const response = await fetch(`/api/podcasts/${podcastId}/episodes?limit=200`);

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to load episodes');
                    }

                    const data = await response.json();
                    document.getElementById('episodeLoading').style.display = 'none';

                    if (!data.episodes || data.episodes.length === 0) {
                        document.getElementById('episodeEmpty').style.display = 'block';
                        return;
                    }

                    displayPodcastEpisodes(data.episodes);
                } catch (error) {
                    document.getElementById('episodeLoading').style.display = 'none';
                    alert(`Error loading episodes: ${error.message}`);
                }
            }

            function displayPodcastEpisodes(episodes) {
                const list = document.getElementById('episodeList');
                list.innerHTML = '';

                episodes.forEach(episode => {
                    const episodeDiv = document.createElement('div');
                    episodeDiv.style.cssText = 'display: flex; justify-content: space-between; align-items: center; padding: 15px; border-bottom: 1px solid #eee;';

                    const infoDiv = document.createElement('div');
                    infoDiv.style.flex = '1';

                    const title = document.createElement('div');
                    title.style.cssText = 'font-weight: 500; margin-bottom: 5px;';
                    title.textContent = episode.title;
                    infoDiv.appendChild(title);

                    const meta = document.createElement('div');
                    meta.style.cssText = 'font-size: 13px; color: #666;';
                    const date = episode.date ? new Date(episode.date).toLocaleDateString() : '';
                    const duration = episode.duration ? ` • ${Math.floor(episode.duration / 60)} min` : '';
                    meta.textContent = `${date}${duration}`;
                    infoDiv.appendChild(meta);

                    episodeDiv.appendChild(infoDiv);

                    const downloadBtn = document.createElement('button');
                    downloadBtn.textContent = 'Download';
                    downloadBtn.className = 'btn-primary';
                    downloadBtn.style.marginLeft = '15px';
                    downloadBtn.onclick = () => downloadPodcastEpisode(episode.url, episode.title, episode.date);
                    episodeDiv.appendChild(downloadBtn);

                    list.appendChild(episodeDiv);
                });
            }

            async function downloadPodcastEpisode(episodeUrl, episodeTitle, episodeDate) {
                if (!episodeUrl) {
                    console.error('Episode URL not available');
                    return;
                }

                try {
                    const response = await fetch('/api/podcasts/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            episode_url: episodeUrl,
                            episode_title: episodeTitle,
                            episode_date: episodeDate,
                            podcast_name: window.currentPodcastName || 'Unknown Podcast'
                        })
                    });

                    if (!response.ok) {
                        const error = await response.json();
                        console.error('Download failed:', error.detail || 'Unknown error');
                    }
                } catch (error) {
                    console.error('Error queueing download:', error.message);
                }
            }

            function closeEpisodeModal() {
                document.getElementById('episodeModal').style.display = 'none';
            }

            // Load and save user preferences
            function loadPreferences() {
                // Paths
                const cookiesPath = localStorage.getItem('gamdl_cookies_path');
                const outputPath = localStorage.getItem('gamdl_output_path');

                // Audio options
                const songCodec = localStorage.getItem('gamdl_song_codec');
                const musicVideoResolution = localStorage.getItem('gamdl_music_video_resolution');

                // Cover art options
                const coverSize = localStorage.getItem('gamdl_cover_size');
                const coverFormat = localStorage.getItem('gamdl_cover_format');
                const noCover = localStorage.getItem('gamdl_no_cover');

                // Metadata options
                const noLyrics = localStorage.getItem('gamdl_no_lyrics');
                const extraTags = localStorage.getItem('gamdl_extra_tags');
                const includeVideosInDiscography = localStorage.getItem('gamdl_include_videos_in_discography');

                // Retry/delay options
                const enableRetryDelay = localStorage.getItem('gamdl_enable_retry_delay');
                const maxRetries = localStorage.getItem('gamdl_max_retries');
                const retryDelay = localStorage.getItem('gamdl_retry_delay');
                const songDelay = localStorage.getItem('gamdl_song_delay');
                const queueItemDelay = localStorage.getItem('gamdl_queue_item_delay');

                // Queue behavior options
                const continueOnError = localStorage.getItem('gamdl_continue_on_error');

                // Apply saved values
                if (cookiesPath) document.getElementById('cookiesPath').value = cookiesPath;
                if (outputPath) document.getElementById('outputPath').value = outputPath;
                if (songCodec) document.getElementById('songCodec').value = songCodec;
                if (musicVideoResolution) document.getElementById('musicVideoResolution').value = musicVideoResolution;
                if (coverSize) document.getElementById('coverSize').value = coverSize;
                if (coverFormat) document.getElementById('coverFormat').value = coverFormat;
                if (noCover) document.getElementById('noCover').checked = noCover === 'true';
                if (noLyrics) document.getElementById('noLyrics').checked = noLyrics === 'true';
                if (extraTags) document.getElementById('extraTags').checked = extraTags === 'true';
                if (includeVideosInDiscography === 'true') document.getElementById('includeVideosInDiscography').checked = true;
                if (enableRetryDelay !== null) document.getElementById('enableRetryDelay').checked = enableRetryDelay === 'true';
                if (maxRetries) document.getElementById('maxRetries').value = maxRetries;
                if (retryDelay) document.getElementById('retryDelay').value = retryDelay;
                if (songDelay) document.getElementById('songDelay').value = songDelay;
                if (queueItemDelay) document.getElementById('queueItemDelay').value = queueItemDelay;
                if (continueOnError === 'true') document.getElementById('continueOnError').checked = true;
            }

            function savePreferences() {
                // Paths
                const cookiesPath = document.getElementById('cookiesPath').value;
                const outputPath = document.getElementById('outputPath').value;

                // Audio options
                const songCodec = document.getElementById('songCodec').value;
                const musicVideoResolution = document.getElementById('musicVideoResolution').value;

                // Cover art options
                const coverSize = document.getElementById('coverSize').value;
                const coverFormat = document.getElementById('coverFormat').value;
                const noCover = document.getElementById('noCover').checked;

                // Metadata options
                const noLyrics = document.getElementById('noLyrics').checked;
                const extraTags = document.getElementById('extraTags').checked;
                const includeVideosInDiscography = document.getElementById('includeVideosInDiscography').checked;

                // Retry/delay options
                const enableRetryDelay = document.getElementById('enableRetryDelay').checked;
                const maxRetries = document.getElementById('maxRetries').value;
                const retryDelay = document.getElementById('retryDelay').value;
                const songDelay = document.getElementById('songDelay').value;
                const queueItemDelay = document.getElementById('queueItemDelay').value;

                // Queue behavior options
                const continueOnError = document.getElementById('continueOnError').checked;

                // Save to localStorage
                localStorage.setItem('gamdl_cookies_path', cookiesPath);
                localStorage.setItem('gamdl_output_path', outputPath);
                localStorage.setItem('gamdl_song_codec', songCodec);
                localStorage.setItem('gamdl_music_video_resolution', musicVideoResolution);
                localStorage.setItem('gamdl_cover_size', coverSize);
                localStorage.setItem('gamdl_cover_format', coverFormat);
                localStorage.setItem('gamdl_no_cover', noCover);
                localStorage.setItem('gamdl_no_lyrics', noLyrics);
                localStorage.setItem('gamdl_extra_tags', extraTags);
                localStorage.setItem('gamdl_include_videos_in_discography', includeVideosInDiscography);
                localStorage.setItem('gamdl_enable_retry_delay', enableRetryDelay);
                localStorage.setItem('gamdl_max_retries', maxRetries);
                localStorage.setItem('gamdl_retry_delay', retryDelay);
                localStorage.setItem('gamdl_song_delay', songDelay);
                localStorage.setItem('gamdl_queue_item_delay', queueItemDelay);
                localStorage.setItem('gamdl_continue_on_error', continueOnError);

                // Also save ALL settings to server-side config for background downloads
                fetch('/api/config/all-settings', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        // Paths
                        cookies_path: cookiesPath,
                        output_path: outputPath,

                        // Audio options
                        song_codec: songCodec,
                        music_video_resolution: musicVideoResolution,

                        // Cover art options
                        cover_size: coverSize,
                        cover_format: coverFormat,
                        no_cover: noCover,

                        // Metadata options
                        no_lyrics: noLyrics,
                        extra_tags: extraTags,

                        // Retry/delay options
                        enable_retry_delay: enableRetryDelay,
                        max_retries: parseInt(maxRetries),
                        retry_delay: parseInt(retryDelay),
                        song_delay: parseFloat(songDelay),
                        queue_item_delay: parseFloat(queueItemDelay),

                        // Queue behavior options
                        continue_on_error: continueOnError,
                    })
                }).catch(err => {
                    console.error('Failed to save settings to server config:', err);
                });
            }

            function saveAllSettings() {
                savePreferences();
                alert('Settings saved successfully!');
            }

            function toggleRetryDelaySettings() {
                const checkbox = document.getElementById('enableRetryDelay');
                const settingsContainer = document.getElementById('retryDelaySettings');

                if (checkbox.checked) {
                    settingsContainer.style.display = 'block';
                } else {
                    settingsContainer.style.display = 'none';
                }
            }

            // ========================================
            // Queue Management Functions
            // ========================================

            let queueUpdateInterval = null;

            async function pauseQueue() {
                const btn = document.getElementById('pauseQueueBtn');
                const isPaused = btn.textContent.includes('Resume');

                try {
                    const endpoint = isPaused ? '/api/queue/resume' : '/api/queue/pause';
                    const response = await fetch(endpoint, { method: 'POST' });

                    if (!response.ok) {
                        throw new Error('Failed to toggle queue pause state');
                    }

                    const data = await response.json();
                    btn.textContent = isPaused ? '⏸ Pause' : '▶ Resume';
                    btn.style.background = isPaused ? '#007aff' : '#34c759';

                    // Immediately refresh queue status
                    await refreshQueueStatus();
                } catch (error) {
                    console.error('Error toggling queue pause:', error);
                    alert('Failed to toggle queue pause state');
                }
            }

            async function clearCompleted() {
                try {
                    const response = await fetch('/api/queue/clear', { method: 'POST' });

                    if (!response.ok) {
                        throw new Error('Failed to clear completed items');
                    }

                    await refreshQueueStatus();
                } catch (error) {
                    console.error('Error clearing completed items:', error);
                    alert('Failed to clear completed items');
                }
            }

            async function removeQueueItem(itemId) {
                if (!confirm('Are you sure you want to remove this item from the queue?')) {
                    return;
                }

                try {
                    const response = await fetch(`/api/queue/remove/${itemId}`, { method: 'DELETE' });

                    if (!response.ok) {
                        throw new Error('Failed to remove item from queue');
                    }

                    await refreshQueueStatus();
                } catch (error) {
                    console.error('Error removing queue item:', error);
                    alert('Failed to remove item from queue');
                }
            }

            async function refreshQueueStatus() {
                try {
                    const response = await fetch('/api/queue/status');

                    if (!response.ok) {
                        throw new Error('Failed to fetch queue status');
                    }

                    const data = await response.json();
                    updateQueueUI(data);
                } catch (error) {
                    console.error('Error fetching queue status:', error);
                }
            }

            function updateQueueUI(queueData) {
                // Update counts
                const queued = queueData.items.filter(item => item.status === 'queued').length;
                const downloading = queueData.items.filter(item => item.status === 'downloading').length;
                const completed = queueData.items.filter(item => item.status === 'completed').length;

                document.getElementById('queuedCount').textContent = queued;
                document.getElementById('downloadingCount').textContent = downloading;
                document.getElementById('completedCount').textContent = completed;

                // Update pause button state
                const pauseBtn = document.getElementById('pauseQueueBtn');
                const pauseSVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>';
                const playSVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>';

                if (queueData.paused) {
                    pauseBtn.innerHTML = playSVG + ' Resume';
                    pauseBtn.style.background = '#34c759';
                    pauseBtn.style.color = 'white';
                    pauseBtn.style.borderColor = '#34c759';

                    // Show warning if there are failed items
                    const hasFailed = queueData.items.some(item => item.status === 'failed');
                    if (hasFailed) {
                        pauseBtn.innerHTML = playSVG + ' Resume (Check Errors)';
                        pauseBtn.style.background = '#ff9500';
                        pauseBtn.style.borderColor = '#ff9500';
                    }
                } else {
                    pauseBtn.innerHTML = pauseSVG + ' Pause';
                    pauseBtn.style.background = '#007aff';
                    pauseBtn.style.color = 'white';
                    pauseBtn.style.borderColor = '#007aff';
                }

                // Render queue list
                renderQueueList(queueData.items);
            }

            function renderQueueList(items) {
                const queueList = document.getElementById('queueList');

                if (items.length === 0) {
                    queueList.innerHTML = '<div class="queue-empty">No items in queue</div>';
                    return;
                }

                queueList.innerHTML = items.map(item => {
                    const statusClass = item.status.toLowerCase();
                    const statusIcon = {
                        'queued': '[Q]',
                        'downloading': '[D]',
                        'completed': '[C]',
                        'failed': '[F]',
                        'cancelled': '[X]'
                    }[item.status] || '[ ]';

                    const statusText = item.status.charAt(0).toUpperCase() + item.status.slice(1);

                    let actionButton = '';
                    if (item.status === 'queued') {
                        actionButton = `<button class="queue-item-remove" onclick="removeQueueItem('${item.id}')">Remove</button>`;
                    } else if (item.status === 'failed') {
                        actionButton = `<button class="queue-item-remove" onclick="removeQueueItem('${item.id}')">Remove</button>`;
                    }
                    // Note: No "View Progress" button for downloading items since they run in background without WebSocket

                    const errorMessage = item.error_message ?
                        `<div class="queue-item-error">Error: ${escapeHtml(item.error_message)}</div>` : '';

                    const urlInfo = item.url_count > 1 ?
                        `<div class="queue-item-info">${item.url_count} URLs</div>` : '';

                    // Calculate and display progress percentage inline with status
                    let progressInfo = '';
                    if (item.progress_total > 0 && item.status === 'downloading') {
                        const percentage = Math.round((item.progress_current / item.progress_total) * 100);
                        progressInfo = ` - ${item.progress_current}/${item.progress_total} (${percentage}%)`;
                    }

                    return `
                        <div class="queue-item ${statusClass}">
                            <div class="queue-item-header">
                                <span class="queue-item-icon">${statusIcon}</span>
                                <span class="queue-item-title">${escapeHtml(item.display_title)}</span>
                            </div>
                            <div class="queue-item-meta">
                                <span class="queue-item-type">${escapeHtml(item.display_type)}</span>
                                <span class="queue-item-status">${statusText}${progressInfo}</span>
                            </div>
                            ${urlInfo}
                            ${errorMessage}
                            <div class="queue-item-actions">
                                ${actionButton}
                            </div>
                        </div>
                    `;
                }).join('');
            }

            function viewDownloadProgress(itemId) {
                // Switch to Downloads view
                switchView('downloads', document.querySelector('[onclick*="downloads"]'));

                // Show the progress container
                document.getElementById('progressContainer').classList.add('active');

                // Check if this item has an active WebSocket session
                // Note: Items downloaded from the queue may not have a WebSocket
                if (sessionId === itemId && ws && ws.readyState === WebSocket.OPEN) {
                    // WebSocket already connected for this item
                    return;
                }

                // Can't view progress for completed/failed items without active WebSocket
                addLog(`Cannot view live progress for this item. Check queue status for details.`, 'warning');
            }

            function escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML;
            }

            // Start periodic queue status refresh
            function startQueueRefresh() {
                if (queueUpdateInterval) {
                    clearInterval(queueUpdateInterval);
                }

                // Refresh every 3 seconds
                queueUpdateInterval = setInterval(refreshQueueStatus, 3000);

                // Initial refresh
                refreshQueueStatus();
            }

            function stopQueueRefresh() {
                if (queueUpdateInterval) {
                    clearInterval(queueUpdateInterval);
                    queueUpdateInterval = null;
                }
            }

            // Add event listeners to save preferences when fields change
            document.getElementById('cookiesPath').addEventListener('change', savePreferences);
            document.getElementById('outputPath').addEventListener('change', savePreferences);
            document.getElementById('songCodec').addEventListener('change', savePreferences);
            document.getElementById('musicVideoResolution').addEventListener('change', savePreferences);
            document.getElementById('coverSize').addEventListener('change', savePreferences);
            document.getElementById('coverFormat').addEventListener('change', savePreferences);
            document.getElementById('noCover').addEventListener('change', savePreferences);
            document.getElementById('noLyrics').addEventListener('change', savePreferences);
            document.getElementById('extraTags').addEventListener('change', savePreferences);
            document.getElementById('includeVideosInDiscography').addEventListener('change', savePreferences);
            document.getElementById('enableRetryDelay').addEventListener('change', function() {
                savePreferences();
                toggleRetryDelaySettings();
            });
            document.getElementById('maxRetries').addEventListener('change', savePreferences);
            document.getElementById('retryDelay').addEventListener('change', savePreferences);
            document.getElementById('songDelay').addEventListener('change', savePreferences);
            document.getElementById('queueItemDelay').addEventListener('change', savePreferences);

            // Load albums and preferences on page load
            document.addEventListener('DOMContentLoaded', () => {
                loadPreferences();
                toggleRetryDelaySettings();  // Set initial visibility based on checkbox state
                loadLibraryAlbums();
                startQueueRefresh();
            });

            // =============================================================================
            // Playlist Monitoring Functions
            // =============================================================================

            async function setMonitoredPlaylist(playlistId, playlistUrl, playlistName, playlistType) {
                try {
                    const response = await fetch('/api/monitor/set', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            playlist_id: playlistId,
                            playlist_url: playlistUrl,
                            playlist_name: playlistName,
                            playlist_type: playlistType
                        })
                    });

                    const result = await response.json();

                    if (response.ok) {
                        alert(`Now monitoring playlist: ${playlistName}\n${result.track_count} tracks currently in playlist`);
                        refreshMonitorStatus();
                    } else {
                        alert(`Failed to set monitored playlist: ${result.detail || 'Unknown error'}`);
                    }
                } catch (error) {
                    console.error('Error setting monitored playlist:', error);
                    alert('Failed to set monitored playlist. Check console for details.');
                }
            }

            async function loadPlaylistsForSelector() {
                const dropdown = document.getElementById('playlistSelector');
                const loading = document.getElementById('playlistSelectorLoading');
                const errorDiv = document.getElementById('playlistSelectorError');

                loading.style.display = 'block';
                errorDiv.style.display = 'none';

                try {
                    // Store current selection to preserve it
                    const currentValue = dropdown.value;

                    // Clear existing options except the default
                    dropdown.innerHTML = '<option value="">-- Select a playlist --</option>';

                    let offset = 0;
                    let hasMore = true;
                    let allPlaylists = [];

                    // Fetch all playlists (handle pagination)
                    while (hasMore) {
                        const response = await fetch(`/api/library/playlists?limit=50&offset=${offset}`);
                        if (!response.ok) {
                            const error = await response.json();
                            throw new Error(error.detail || 'Failed to load playlists');
                        }

                        const data = await response.json();
                        allPlaylists = allPlaylists.concat(data.data);

                        if (data.has_more) {
                            offset = data.next_offset;
                        } else {
                            hasMore = false;
                        }
                    }

                    // Sort playlists alphabetically
                    allPlaylists.sort((a, b) => a.name.localeCompare(b.name));

                    // Populate dropdown
                    allPlaylists.forEach(playlist => {
                        const option = document.createElement('option');
                        option.value = playlist.id;
                        option.textContent = `${playlist.name} (${playlist.trackCount} tracks)`;
                        option.dataset.playlistName = playlist.name;
                        dropdown.appendChild(option);
                    });

                    // Get current monitored playlist and pre-select it
                    const statusResponse = await fetch('/api/monitor/status');
                    const status = await statusResponse.json();

                    if (status.monitored_playlist && status.monitored_playlist.playlist_id) {
                        dropdown.value = status.monitored_playlist.playlist_id;
                    } else if (currentValue) {
                        dropdown.value = currentValue;
                    }

                    loading.style.display = 'none';

                } catch (error) {
                    console.error('Error loading playlists for selector:', error);
                    loading.style.display = 'none';
                    errorDiv.textContent = error.message;
                    errorDiv.style.display = 'block';
                }
            }

            async function handlePlaylistSelection() {
                const dropdown = document.getElementById('playlistSelector');
                const selectedId = dropdown.value;

                if (!selectedId) {
                    return; // User selected the placeholder option
                }

                const selectedOption = dropdown.options[dropdown.selectedIndex];
                const playlistName = selectedOption.dataset.playlistName;

                // Confirm with user before starting monitoring
                const confirmMsg = `Start monitoring playlist "${playlistName}"?\n\n` +
                                  `All tracks in this playlist will be queued for download. ` +
                                  `New tracks added later will be automatically downloaded.`;

                if (!confirm(confirmMsg)) {
                    // Reset dropdown to current monitored playlist or empty
                    const statusResponse = await fetch('/api/monitor/status');
                    const status = await statusResponse.json();
                    if (status.monitored_playlist) {
                        dropdown.value = status.monitored_playlist.playlist_id;
                    } else {
                        dropdown.value = '';
                    }
                    return;
                }

                // Build playlist URL (library playlist)
                const playlistUrl = `https://music.apple.com/us/library/playlist/${selectedId}`;

                // Call existing setMonitoredPlaylist function
                await setMonitoredPlaylist(selectedId, playlistUrl, playlistName, 'library');
            }

            async function stopMonitoring() {
                if (!confirm('Stop monitoring this playlist? Activity log will be preserved.')) {
                    return;
                }

                try {
                    const response = await fetch('/api/monitor/stop', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'}
                    });

                    const result = await response.json();

                    if (response.ok) {
                        alert('Monitoring stopped');
                        refreshMonitorStatus();
                    } else {
                        alert(`Failed to stop monitoring: ${result.detail || 'Unknown error'}`);
                    }
                } catch (error) {
                    console.error('Error stopping monitoring:', error);
                    alert('Failed to stop monitoring. Check console for details.');
                }
            }

            async function refreshMonitorStatus() {
                try {
                    const response = await fetch('/api/monitor/status');
                    const status = await response.json();

                    const emptyDiv = document.querySelector('.monitor-empty');
                    const activeDiv = document.querySelector('.monitor-active');

                    if (status.monitored_playlist) {
                        // Show active monitoring UI
                        emptyDiv.style.display = 'none';
                        activeDiv.style.display = 'block';

                        // Update playlist name
                        document.getElementById('monitoredPlaylistName').textContent = status.monitored_playlist.playlist_name;

                        // Update toggle
                        document.getElementById('monitorToggle').checked = status.enabled;

                        // Update stats
                        document.getElementById('monitorStatus').textContent = status.enabled ? 'Enabled' : 'Disabled';
                        document.getElementById('monitorStatus').style.color = status.enabled ? '#4CAF50' : '#999';
                        document.getElementById('monitorTrackCount').textContent = status.track_count;

                        // Format last checked time
                        if (status.monitored_playlist.last_checked_at) {
                            const lastChecked = new Date(status.monitored_playlist.last_checked_at);
                            document.getElementById('monitorLastChecked').textContent = formatRelativeTime(lastChecked);
                        } else {
                            document.getElementById('monitorLastChecked').textContent = 'Never';
                        }

                        // Format monitoring since time
                        if (status.monitored_playlist.monitored_since) {
                            const since = new Date(status.monitored_playlist.monitored_since);
                            document.getElementById('monitoringSince').textContent = formatRelativeTime(since);
                        } else {
                            document.getElementById('monitoringSince').textContent = '-';
                        }

                        // Render activity log
                        renderActivityLog(status.activity_log);
                    } else {
                        // Show empty state
                        emptyDiv.style.display = 'block';
                        activeDiv.style.display = 'none';

                        // Render activity log (might have old events)
                        renderActivityLog(status.activity_log);
                    }
                } catch (error) {
                    console.error('Error refreshing monitor status:', error);
                }
            }

            function renderActivityLog(events) {
                const logContainer = document.getElementById('activityLog');

                if (!events || events.length === 0) {
                    logContainer.innerHTML = '<p class="activity-empty">No activity yet</p>';
                    return;
                }

                let html = '';
                events.forEach(event => {
                    const eventClass = event.event_type.replace('_', '-');
                    const icon = getEventIcon(event.event_type);
                    const time = new Date(event.timestamp);

                    html += `
                        <div class="activity-item ${eventClass}">
                            <div class="activity-icon">${icon}</div>
                            <div class="activity-content">
                                <div class="activity-message">${event.message}</div>
                                <div class="activity-time">${formatRelativeTime(time)}</div>
                            </div>
                        </div>
                    `;
                });

                logContainer.innerHTML = html;
            }

            function getEventIcon(eventType) {
                const icons = {
                    'new_tracks': '+',
                    'removed_tracks': '−',
                    'started': '▶',
                    'stopped': '■',
                    'toggle': '⚙',
                    'error': '!',
                    'check': '↻'
                };
                return icons[eventType] || '•';
            }

            function formatRelativeTime(date) {
                const now = new Date();
                const diff = Math.floor((now - date) / 1000); // seconds

                if (diff < 60) return 'Just now';
                if (diff < 3600) return `${Math.floor(diff / 60)} minutes ago`;
                if (diff < 86400) return `${Math.floor(diff / 3600)} hours ago`;
                if (diff < 604800) return `${Math.floor(diff / 86400)} days ago`;
                return date.toLocaleDateString();
            }

            async function toggleMonitoring() {
                const enabled = document.getElementById('monitorToggle').checked;

                try {
                    const response = await fetch('/api/monitor/toggle', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({enabled: enabled})
                    });

                    const result = await response.json();

                    if (response.ok) {
                        refreshMonitorStatus();
                    } else {
                        alert(`Failed to toggle monitoring: ${result.detail || 'Unknown error'}`);
                        // Revert toggle
                        document.getElementById('monitorToggle').checked = !enabled;
                    }
                } catch (error) {
                    console.error('Error toggling monitoring:', error);
                    alert('Failed to toggle monitoring. Check console for details.');
                    // Revert toggle
                    document.getElementById('monitorToggle').checked = !enabled;
                }
            }

            async function manualCheckPlaylist() {
                const button = event.target;
                button.disabled = true;
                button.textContent = 'Checking...';

                try {
                    const response = await fetch('/api/monitor/check', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'}
                    });

                    const result = await response.json();

                    if (response.ok) {
                        alert('Check completed! Refresh the monitor status to see updates.');
                        refreshMonitorStatus();
                    } else {
                        alert(`Check failed: ${result.detail || 'Unknown error'}`);
                    }
                } catch (error) {
                    console.error('Error checking playlist:', error);
                    alert('Failed to check playlist. Check console for details.');
                } finally {
                    button.disabled = false;
                    button.textContent = 'Check Now';
                }
            }
        </script>

        <!-- Artist Content Modal -->
        <div id="artistModal" class="modal" style="display:none;">
            <div class="modal-content">
                <div class="modal-header">
                    <h2 id="artistModalTitle">Artist Content</h2>
                    <button class="modal-close" onclick="closeArtistModal()">&times;</button>
                </div>
                <div class="modal-body">
                    <div id="artistModalLoading" class="loading">
                        <div style="text-align: center;">
                            <div class="spinner"></div>
                            <p id="artistModalLoadingText">Fetching artist catalog...</p>
                            <small id="artistModalLoadingProgress" style="color: #666;"></small>
                        </div>
                    </div>
                    <div id="artistModalContent" style="display:none;">
                        <!-- Albums Section -->
                        <div class="artist-section">
                            <h3>Albums (<span id="artistAlbumsCount">0</span>)</h3>
                            <div id="artistAlbumsGrid" class="library-grid"></div>
                        </div>

                        <!-- Music Videos Section -->
                        <div class="artist-section" id="artistVideosSection" style="display:none;">
                            <h3>Music Videos (<span id="artistVideosCount">0</span>)</h3>
                            <div id="artistVideosGrid" class="library-grid"></div>
                        </div>
                    </div>
                </div>
                <div class="modal-footer">
                    <button onclick="downloadSelectedArtistContent()" class="btn-primary">Download Selected</button>
                    <button onclick="closeArtistModal()" class="btn-secondary">Close</button>
                </div>
            </div>
        </div>
    </body>
    </html>
    """


# Queue API Endpoints

@app.get("/api/queue/status")
async def get_queue():
    """Get current queue status."""
    return get_queue_status()


@app.post("/api/queue/pause")
async def pause_queue_endpoint():
    """Pause the download queue."""
    pause_queue()
    await broadcast_queue_update()
    return {"status": "paused", "message": "Queue paused"}


@app.post("/api/queue/resume")
async def resume_queue_endpoint():
    """Resume the download queue."""
    resume_queue()
    await broadcast_queue_update()
    return {"status": "resumed", "message": "Queue resumed"}


@app.delete("/api/queue/remove/{item_id}")
async def remove_queue_item(item_id: str):
    """Remove an item from the queue."""
    success = remove_from_queue(item_id)
    if success:
        await broadcast_queue_update()
        return {"status": "removed", "message": "Item removed from queue"}
    else:
        raise HTTPException(status_code=404, detail="Queue item not found")


@app.post("/api/queue/clear")
async def clear_queue_endpoint():
    """Clear all completed/failed items from queue."""
    with queue_lock:
        global download_queue
        download_queue = [
            item for item in download_queue
            if item.status in [QueueItemStatus.QUEUED, QueueItemStatus.DOWNLOADING]
        ]
    await broadcast_queue_update()
    return {"status": "cleared", "message": "Completed items cleared"}


@app.post("/api/config/cookies-path")
async def save_cookies_path_config(request_data: dict):
    """Save user's preferred cookies path to server-side config."""
    cookies_path = request_data.get("cookies_path", "")

    # Load existing config
    config = load_webui_config()

    # Update cookies path
    config["cookies_path"] = cookies_path

    # Save to disk
    save_webui_config(config)

    return {"success": True, "message": "Cookies path saved to configuration"}


@app.post("/api/config/all-settings")
async def save_all_settings_config(request_data: dict):
    """Save all user settings to server-side config for background downloads."""
    # Load existing config
    config = load_webui_config()

    # Helper to convert empty strings to None
    def clean_value(value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    # Update all settings from request
    # Paths
    if "cookies_path" in request_data:
        config["cookies_path"] = clean_value(request_data["cookies_path"])
    if "output_path" in request_data:
        config["output_path"] = clean_value(request_data["output_path"])
    if "temp_path" in request_data:
        config["temp_path"] = clean_value(request_data["temp_path"])
    if "final_path_template" in request_data:
        config["final_path_template"] = clean_value(request_data["final_path_template"])

    # Audio options
    if "song_codec" in request_data:
        config["song_codec"] = clean_value(request_data["song_codec"])
    if "music_video_codec" in request_data:
        config["music_video_codec"] = clean_value(request_data["music_video_codec"])
    if "music_video_resolution" in request_data:
        config["music_video_resolution"] = clean_value(request_data["music_video_resolution"])

    # Cover art options
    if "cover_size" in request_data:
        config["cover_size"] = clean_value(request_data["cover_size"])
    if "cover_format" in request_data:
        config["cover_format"] = clean_value(request_data["cover_format"])
    if "no_cover" in request_data:
        config["no_cover"] = request_data["no_cover"]

    # Metadata options
    if "no_lyrics" in request_data:
        config["no_lyrics"] = request_data["no_lyrics"]
    if "extra_tags" in request_data:
        config["extra_tags"] = request_data["extra_tags"]

    # Retry/delay options
    if "enable_retry_delay" in request_data:
        config["enable_retry_delay"] = request_data["enable_retry_delay"]
    if "max_retries" in request_data:
        config["max_retries"] = request_data["max_retries"]
    if "retry_delay" in request_data:
        config["retry_delay"] = request_data["retry_delay"]
    if "song_delay" in request_data:
        config["song_delay"] = request_data["song_delay"]
    if "queue_item_delay" in request_data:
        config["queue_item_delay"] = request_data["queue_item_delay"]

    # Queue behavior options
    if "continue_on_error" in request_data:
        config["continue_on_error"] = request_data["continue_on_error"]

    # Save to disk
    save_webui_config(config)

    return {"success": True, "message": "All settings saved to configuration"}


# =============================================================================
# Playlist Monitoring API Endpoints
# =============================================================================

@app.post("/api/monitor/set")
async def set_monitored_playlist_endpoint(request_data: dict):
    """
    Set a playlist as THE monitored playlist.

    Expected request body:
    {
        "playlist_id": "p.abc123",
        "playlist_url": "https://...",
        "playlist_name": "My Playlist",
        "playlist_type": "library"
    }
    """
    try:
        playlist_id = request_data.get("playlist_id")
        playlist_url = request_data.get("playlist_url")
        playlist_name = request_data.get("playlist_name")
        playlist_type = request_data.get("playlist_type")

        if not all([playlist_id, playlist_url, playlist_name, playlist_type]):
            raise HTTPException(status_code=400, detail="Missing required fields")

        # Build playlist info dict
        playlist_info = {
            "playlist_id": playlist_id,
            "playlist_url": playlist_url,
            "playlist_name": playlist_name,
            "playlist_type": playlist_type
        }

        # Fetch current track IDs and queue them all for download
        logger.info(f"Setting monitored playlist: {playlist_name}")
        try:
            current_track_ids = await fetch_playlist_track_ids(playlist_info)
        except Exception as e:
            logger.error(f"Failed to fetch playlist tracks: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch playlist: {str(e)}")

        # Create new config
        config = {
            "enabled": True,
            "check_interval_minutes": 60,
            "monitored_playlist": {
                "playlist_id": playlist_id,
                "playlist_url": playlist_url,
                "playlist_name": playlist_name,
                "playlist_type": playlist_type,
                "last_checked_at": datetime.utcnow().isoformat() + 'Z',
                "monitored_since": datetime.utcnow().isoformat() + 'Z'
            },
            "tracked_track_ids": current_track_ids,
            "activity_log": []
        }

        # Save config
        save_monitor_config(config)

        # Queue all current tracks for download
        if current_track_ids:
            logger.info(f"Queueing {len(current_track_ids)} existing tracks for download")
            await handle_new_tracks(playlist_info, set(current_track_ids))

        # Log event
        log_monitor_event('started', f"Started monitoring playlist '{playlist_name}' - queued {len(current_track_ids)} tracks for download")

        logger.info(f"Successfully set monitored playlist: {playlist_name} ({len(current_track_ids)} tracks queued)")

        return {
            "success": True,
            "message": f"Now monitoring playlist '{playlist_name}'",
            "track_count": len(current_track_ids)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set monitored playlist: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/monitor/stop")
async def stop_monitoring_endpoint():
    """Stop monitoring (clear all data)."""
    try:
        config = load_monitor_config()

        playlist_name = "Unknown"
        if config.get("monitored_playlist"):
            playlist_name = config["monitored_playlist"].get("playlist_name", "Unknown")

        # Reset config
        config = {
            "enabled": False,
            "check_interval_minutes": 60,
            "monitored_playlist": None,
            "tracked_track_ids": [],
            "activity_log": config.get("activity_log", [])  # Keep log
        }

        save_monitor_config(config)

        # Log event
        log_monitor_event('stopped', f"Stopped monitoring playlist '{playlist_name}'")

        logger.info(f"Stopped monitoring playlist: {playlist_name}")

        return {"success": True, "message": "Monitoring stopped"}

    except Exception as e:
        logger.error(f"Failed to stop monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/monitor/status")
async def get_monitor_status_endpoint():
    """Get current monitor status and activity log."""
    try:
        config = load_monitor_config()

        return {
            "enabled": config.get("enabled", False),
            "monitored_playlist": config.get("monitored_playlist"),
            "track_count": len(config.get("tracked_track_ids", [])),
            "activity_log": config.get("activity_log", [])[:20]  # Return last 20 events
        }

    except Exception as e:
        logger.error(f"Failed to get monitor status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/monitor/check")
async def manual_check_endpoint():
    """Manually trigger check for monitored playlist."""
    try:
        config = load_monitor_config()

        if not config.get("enabled") or not config.get("monitored_playlist"):
            raise HTTPException(status_code=400, detail="No playlist is being monitored")

        # Run check
        await check_monitored_playlist()

        return {"success": True, "message": "Check completed"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/monitor/toggle")
async def toggle_monitoring_endpoint(request_data: dict):
    """Enable/disable monitoring without clearing data."""
    try:
        enabled = request_data.get("enabled")

        if enabled is None:
            raise HTTPException(status_code=400, detail="Missing 'enabled' field")

        config = load_monitor_config()

        if not config.get("monitored_playlist"):
            raise HTTPException(status_code=400, detail="No playlist configured to monitor")

        config["enabled"] = bool(enabled)
        save_monitor_config(config)

        status_text = "enabled" if enabled else "disabled"
        log_monitor_event('toggle', f"Monitoring {status_text}")

        logger.info(f"Monitoring {status_text}")

        return {"success": True, "message": f"Monitoring {status_text}", "enabled": enabled}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to toggle monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search")
async def search_apple_music(
    term: str,
    types: str = "songs,albums,artists,playlists,music-videos",
    limit: int = 25,
    offset: int = 0,
):
    """Search Apple Music catalog."""
    if not hasattr(app.state, "api") or app.state.api is None:
        raise HTTPException(
            status_code=401,
            detail="API not initialized. Please set your cookies path in Settings, then restart the server."
        )

    api = app.state.api

    try:
        # Perform search using Apple Music API
        search_results = await api.get_search_results(
            term=term,
            types=types,
            limit=limit,
            offset=offset,
        )

        # Format results for frontend
        formatted_results = {}
        results = search_results.get("results", {})

        # Format albums
        if "albums" in results:
            albums_data = results["albums"].get("data", [])
            formatted_results["albums"] = [
                {
                    "id": album["id"],
                    "name": album["attributes"]["name"],
                    "artist": album["attributes"]["artistName"],
                    "artwork": album["attributes"]["artwork"]["url"].replace("{w}", "300").replace("{h}", "300") if "artwork" in album["attributes"] else None,
                    "trackCount": album["attributes"].get("trackCount", 0),
                    "url": album["attributes"]["url"],
                }
                for album in albums_data
            ]
            formatted_results["has_more"] = len(albums_data) >= limit
            formatted_results["next_offset"] = offset + len(albums_data)

        # Format songs
        if "songs" in results:
            songs_data = results["songs"].get("data", [])
            formatted_results["songs"] = [
                {
                    "id": song["id"],
                    "name": song["attributes"]["name"],
                    "artist": song["attributes"]["artistName"],
                    "album": song["attributes"].get("albumName", ""),
                    "artwork": song["attributes"]["artwork"]["url"].replace("{w}", "300").replace("{h}", "300") if "artwork" in song["attributes"] else None,
                    "url": song["attributes"]["url"],
                }
                for song in songs_data
            ]
            if "has_more" not in formatted_results:
                formatted_results["has_more"] = len(songs_data) >= limit
                formatted_results["next_offset"] = offset + len(songs_data)

        # Format artists
        if "artists" in results:
            artists_data = results["artists"].get("data", [])
            formatted_results["artists"] = [
                {
                    "id": artist["id"],
                    "name": artist["attributes"]["name"],
                    "artwork": artist["attributes"].get("artwork", {}).get("url", "").replace("{w}", "300").replace("{h}", "300") if "artwork" in artist["attributes"] else None,
                    "url": artist["attributes"]["url"],
                }
                for artist in artists_data
            ]
            if "has_more" not in formatted_results:
                formatted_results["has_more"] = len(artists_data) >= limit
                formatted_results["next_offset"] = offset + len(artists_data)

        # Format playlists
        if "playlists" in results:
            playlists_data = results["playlists"].get("data", [])
            formatted_results["playlists"] = [
                {
                    "id": playlist["id"],
                    "name": playlist["attributes"]["name"],
                    "curator": playlist["attributes"].get("curatorName", "Apple Music"),
                    "artwork": playlist["attributes"]["artwork"]["url"].replace("{w}", "300").replace("{h}", "300") if "artwork" in playlist["attributes"] else None,
                    "trackCount": playlist["attributes"].get("trackCount", 0),
                    "url": playlist["attributes"]["url"],
                }
                for playlist in playlists_data
            ]
            if "has_more" not in formatted_results:
                formatted_results["has_more"] = len(playlists_data) >= limit
                formatted_results["next_offset"] = offset + len(playlists_data)

        # Format music-videos
        if "music-videos" in results:
            music_videos_data = results["music-videos"].get("data", [])
            formatted_results["music-videos"] = [
                {
                    "id": video["id"],
                    "name": video["attributes"]["name"],
                    "artist": video["attributes"]["artistName"],
                    "artwork": video["attributes"]["artwork"]["url"].replace("{w}", "300").replace("{h}", "300") if "artwork" in video["attributes"] else None,
                    "duration": video["attributes"].get("durationInMillis", 0) // 1000,  # Convert to seconds
                    "url": video["attributes"]["url"],
                }
                for video in music_videos_data
            ]
            if "has_more" not in formatted_results:
                formatted_results["has_more"] = len(music_videos_data) >= limit
                formatted_results["next_offset"] = offset + len(music_videos_data)

        return formatted_results

    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Search failed: {str(e)}"
        )


@app.get("/api/podcasts/search")
async def search_podcasts_endpoint(term: str, limit: int = 50, offset: int = 0):
    """Search for podcasts using iTunes Search API."""
    from gamdl.api.itunes_api import ItunesApi

    try:
        # Initialize iTunes API (doesn't require authentication)
        if not hasattr(app.state, "itunes_api"):
            app.state.itunes_api = ItunesApi(storefront="us", language="en-US")

        itunes = app.state.itunes_api
        results = await itunes.search_podcasts(term=term, limit=limit, offset=offset)

        # Format response
        podcasts = []
        for item in results.get("results", []):
            if item.get("wrapperType") == "track" and item.get("kind") == "podcast":
                podcasts.append({
                    "id": item["collectionId"],
                    "name": item["collectionName"],
                    "author": item.get("artistName", "Unknown"),
                    "artwork": item.get("artworkUrl600", "").replace("{w}", "300").replace("{h}", "300"),
                    "episodeCount": item.get("trackCount", 0),
                    "feedUrl": item.get("feedUrl"),
                    "genres": item.get("genres", [])
                })

        return {
            "podcasts": podcasts,
            "has_more": len(podcasts) >= limit,
            "next_offset": offset + len(podcasts)
        }

    except Exception as e:
        logger.error(f"Podcast search failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Podcast search failed: {str(e)}"
        )


@app.get("/api/podcasts/{podcast_id}/episodes")
async def get_podcast_episodes_endpoint(podcast_id: int, limit: int = 200):
    """Get episodes for a specific podcast."""
    from gamdl.api.itunes_api import ItunesApi

    try:
        if not hasattr(app.state, "itunes_api"):
            app.state.itunes_api = ItunesApi(storefront="us", language="en-US")

        itunes = app.state.itunes_api
        results = await itunes.get_podcast_episodes(podcast_id=podcast_id, limit=limit)

        # Format response
        episodes = []
        for item in results.get("results", []):
            if item.get("kind") == "podcast-episode":
                episodes.append({
                    "id": item["trackId"],
                    "title": item["trackName"],
                    "url": item.get("episodeUrl"),
                    "description": item.get("description", ""),
                    "date": item.get("releaseDate"),
                    "duration": item.get("trackTimeMillis", 0) / 1000 if item.get("trackTimeMillis") else 0,
                    "episodeNumber": item.get("trackNumber")
                })

        return {"episodes": episodes}

    except Exception as e:
        logger.error(f"Failed to get podcast episodes: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get podcast episodes: {str(e)}"
        )


@app.post("/api/podcasts/download")
async def download_podcast_episode_endpoint(request: Request):
    """Queue a podcast episode for download."""
    try:
        request_data = await request.json()
        episode_url = request_data.get("episode_url")
        episode_title = request_data.get("episode_title", "Podcast Episode")
        episode_date = request_data.get("episode_date")
        podcast_name = request_data.get("podcast_name", "Unknown Podcast")

        if not episode_url:
            raise HTTPException(status_code=400, detail="Missing episode_url")

        # Load config for output path and settings
        config = load_webui_config()

        # Parse cover_size as integer
        cover_size = config.get('cover_size')
        if cover_size:
            try:
                cover_size = int(cover_size)
            except (ValueError, TypeError):
                cover_size = 1200
        else:
            cover_size = 1200

        # Create download request for the podcast episode
        download_request = DownloadRequest(
            urls=[episode_url],
            cookies_path=None,  # Podcasts don't need authentication
            output_path=config.get('output_path'),
            temp_path=config.get('temp_path'),
            final_path_template=config.get('final_path_template'),
            song_codec=config.get('song_codec', 'aac'),
            cover_size=cover_size,
            cover_format=config.get('cover_format', 'jpg'),
            no_cover=config.get('no_cover', False),
            no_lyrics=config.get('no_lyrics', False),
            extra_tags=config.get('extra_tags', False),
            podcast_name=podcast_name,
            episode_title=episode_title,
            episode_date=episode_date,
        )

        # Add to queue with podcast-specific display info
        display_info = {
            "title": episode_title,
            "type": "Podcast Episode"
        }
        add_to_queue(download_request, display_info)

        return {"success": True, "message": "Episode added to queue"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to queue podcast download: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to queue podcast download: {str(e)}"
        )


@app.get("/api/artist/{artist_id}/catalog")
async def get_artist_catalog(
    artist_id: str,
    include_music_videos: bool = False,
):
    """Get an artist's complete catalog (albums, singles, music videos)."""
    if not hasattr(app.state, "api") or app.state.api is None:
        raise HTTPException(
            status_code=401,
            detail="API not initialized. Please set your cookies path in Settings, then restart the server."
        )

    api = app.state.api

    try:
        # Determine what to include
        include_types = "albums"
        if include_music_videos:
            include_types += ",music-videos"

        # Fetch artist data with relationships
        artist_data = await api.get_artist(
            artist_id=artist_id,
            include=include_types,
            limit=100  # Get up to 100 of each type
        )

        if not artist_data:
            raise HTTPException(
                status_code=404,
                detail="Artist not found"
            )

        # Extract artist info
        artist_info = artist_data.get("data", [{}])[0]
        artist_name = artist_info.get("attributes", {}).get("name", "Unknown Artist")

        # Extract album and music video IDs from relationships
        relationships = artist_info.get("relationships", {})
        album_ids = []
        video_ids = []

        if "albums" in relationships:
            albums_data = relationships["albums"].get("data", [])
            album_ids = [album["id"] for album in albums_data]

        if include_music_videos and "music-videos" in relationships:
            videos_data = relationships["music-videos"].get("data", [])
            video_ids = [video["id"] for video in videos_data]

        # Fetch full details for each album using get_album()
        albums = []
        for album_id in album_ids:
            try:
                album_data = await api.get_album(album_id=album_id)
                if album_data and "data" in album_data:
                    album_info = album_data["data"][0]
                    attributes = album_info.get("attributes", {})

                    albums.append({
                        "id": album_id,
                        "type": "albums",
                        "name": attributes.get("name", f"Album {album_id}"),
                        "artist": attributes.get("artistName", artist_name),
                        "artwork": attributes.get("artwork", {}).get("url", "").replace("{w}", "300").replace("{h}", "300") if "artwork" in attributes else None,
                        "trackCount": attributes.get("trackCount", 0),
                        "releaseDate": attributes.get("releaseDate", ""),
                        "url": f"https://music.apple.com/{api.storefront}/album/{album_id}"
                    })
            except Exception as e:
                logger.warning(f"Failed to fetch album {album_id}: {e}")
                # Still add it with minimal info so download URL works
                albums.append({
                    "id": album_id,
                    "type": "albums",
                    "name": f"Album {album_id}",
                    "artist": artist_name,
                    "artwork": None,
                    "trackCount": 0,
                    "releaseDate": "",
                    "url": f"https://music.apple.com/{api.storefront}/album/{album_id}"
                })

        # Fetch full details for each music video using get_music_video()
        music_videos = []
        for video_id in video_ids:
            try:
                video_data = await api.get_music_video(music_video_id=video_id)
                if video_data and "data" in video_data:
                    video_info = video_data["data"][0]
                    attributes = video_info.get("attributes", {})

                    music_videos.append({
                        "id": video_id,
                        "type": "music-videos",
                        "name": attributes.get("name", f"Video {video_id}"),
                        "artist": attributes.get("artistName", artist_name),
                        "artwork": attributes.get("artwork", {}).get("url", "").replace("{w}", "300").replace("{h}", "300") if "artwork" in attributes else None,
                        "duration": attributes.get("durationInMillis", 0) // 1000,
                        "releaseDate": attributes.get("releaseDate", ""),
                        "url": f"https://music.apple.com/{api.storefront}/music-video/{video_id}"
                    })
            except Exception as e:
                logger.warning(f"Failed to fetch music video {video_id}: {e}")
                # Still add it with minimal info
                music_videos.append({
                    "id": video_id,
                    "type": "music-videos",
                    "name": f"Video {video_id}",
                    "artist": artist_name,
                    "artwork": None,
                    "duration": 0,
                    "releaseDate": "",
                    "url": f"https://music.apple.com/{api.storefront}/music-video/{video_id}"
                })

        # Compile URLs for download
        all_urls = [item["url"] for item in albums]
        if include_music_videos:
            all_urls.extend([item["url"] for item in music_videos])

        return {
            "artist_id": artist_id,
            "artist_name": artist_name,
            "albums": albums,
            "music_videos": music_videos,
            "urls": all_urls,
            "total_items": len(all_urls)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get artist catalog: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get artist catalog: {str(e)}"
        )


@app.post("/api/download", response_model=SessionResponse)
async def start_download(request: DownloadRequest):
    """Add download to queue instead of starting immediately."""
    # Extract display info from URLs or use custom display info
    display_info = None

    # Use custom display info if provided
    if request.display_title or request.display_type:
        display_info = {
            "title": request.display_title or "Unknown",
            "type": request.display_type or "url"
        }
    elif request.urls:
        # Fall back to extracting from URL
        first_url = request.urls[0]
        url_info = extract_display_info_from_url(first_url)

        # If multiple URLs, update the title to indicate count
        if len(request.urls) > 1:
            url_info["title"] = f"{url_info['title']} (+{len(request.urls) - 1} more)"

        display_info = url_info

    item_id = add_to_queue(request, display_info)

    return SessionResponse(
        session_id=item_id,  # Return item_id as session_id for compatibility
        status="queued",
        message="Download added to queue",
    )


@app.post("/api/cancel/{session_id}")
async def cancel_download(session_id: str):
    """Cancel an active download session."""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    cancellation_flags[session_id] = True
    logger.info(f"Cancellation requested for session {session_id}")

    return {"status": "cancelled", "message": "Cancellation requested"}


@app.get("/api/library/albums")
async def get_library_albums(
    limit: int = 50,
    offset: int = 0,
):
    """Get albums from user's library."""
    # Get the API instance from a session (we'll need to track this globally)
    if not hasattr(app.state, "api") or app.state.api is None:
        raise HTTPException(
            status_code=401,
            detail="API not initialized. Please set your cookies path in Settings, then restart the server."
        )

    api = app.state.api

    try:
        response = await api.get_all_library_albums(
            limit=limit,
            offset=offset,
        )

        if not response:
            raise HTTPException(
                status_code=403,
                detail="No active subscription or library access denied"
            )

        albums = response.get("data", [])

        # Format for frontend
        formatted_albums = []
        for album in albums:
            attrs = album.get("attributes", {})
            artwork_url = attrs.get("artwork", {}).get("url", "")
            # Replace placeholders in artwork URL
            if artwork_url:
                artwork_url = artwork_url.replace("{w}", "300").replace("{h}", "300")

            formatted_albums.append({
                "id": album.get("id"),
                "name": attrs.get("name"),
                "artist": attrs.get("artistName"),
                "artwork": artwork_url,
                "trackCount": attrs.get("trackCount"),
                "dateAdded": attrs.get("dateAdded"),
            })

        # Check if there are more results
        has_more = len(albums) == limit
        next_offset = offset + limit if has_more else None

        return {
            "data": formatted_albums,
            "next_offset": next_offset,
            "has_more": has_more,
        }
    except Exception as e:
        logger.error(f"Error fetching library albums: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/library/playlists")
async def get_library_playlists(
    limit: int = 50,
    offset: int = 0,
):
    """Get playlists from user's library."""
    if not hasattr(app.state, "api") or app.state.api is None:
        raise HTTPException(
            status_code=401,
            detail="API not initialized. Please set your cookies path in Settings, then restart the server."
        )

    api = app.state.api

    try:
        response = await api.get_all_library_playlists(
            limit=limit,
            offset=offset,
        )

        if not response:
            raise HTTPException(
                status_code=403,
                detail="No active subscription or library access denied"
            )

        playlists = response.get("data", [])

        # Format for frontend
        formatted_playlists = []
        for playlist in playlists:
            attrs = playlist.get("attributes", {})
            artwork_url = attrs.get("artwork", {}).get("url", "")
            # Replace placeholders in artwork URL
            if artwork_url:
                artwork_url = artwork_url.replace("{w}", "300").replace("{h}", "300")

            formatted_playlists.append({
                "id": playlist.get("id"),
                "name": attrs.get("name"),
                "description": attrs.get("description", {}).get("standard", ""),
                "artwork": artwork_url,
                "trackCount": attrs.get("trackCount"),
                "dateAdded": attrs.get("dateAdded"),
            })

        # Check if there are more results
        has_more = len(playlists) == limit
        next_offset = offset + limit if has_more else None

        return {
            "data": formatted_playlists,
            "next_offset": next_offset,
            "has_more": has_more,
        }
    except Exception as e:
        logger.error(f"Error fetching library playlists: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/library/songs")
async def get_library_songs(
    limit: int = 50,
    offset: int = 0,
):
    """Get songs from user's library."""
    if not hasattr(app.state, "api") or app.state.api is None:
        raise HTTPException(
            status_code=401,
            detail="API not initialized. Please set your cookies path in Settings, then restart the server."
        )

    api = app.state.api

    try:
        response = await api.get_all_library_songs(
            limit=limit,
            offset=offset,
        )

        if not response:
            raise HTTPException(
                status_code=403,
                detail="No active subscription or library access denied"
            )

        songs = response.get("data", [])

        # Format for frontend
        formatted_songs = []
        for song in songs:
            attrs = song.get("attributes", {})
            artwork_url = attrs.get("artwork", {}).get("url", "")
            # Replace placeholders in artwork URL
            if artwork_url:
                artwork_url = artwork_url.replace("{w}", "300").replace("{h}", "300")

            formatted_songs.append({
                "id": song.get("id"),
                "name": attrs.get("name"),
                "artist": attrs.get("artistName"),
                "album": attrs.get("albumName"),
                "artwork": artwork_url,
                "duration": attrs.get("durationInMillis"),
                "dateAdded": attrs.get("dateAdded"),
            })

        # Check if there are more results
        has_more = len(songs) == limit
        next_offset = offset + limit if has_more else None

        return {
            "data": formatted_songs,
            "next_offset": next_offset,
            "has_more": has_more,
        }
    except Exception as e:
        logger.error(f"Error fetching library songs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/library/download")
async def download_from_library(request_data: dict):
    """Add library download to queue."""
    library_id = request_data.get("library_id")
    media_type = request_data.get("media_type")  # "album", "playlist", or "song"
    download_full = request_data.get("download_full", False)  # Download full album/playlist vs library tracks only

    logger.info(f"Library download request: id={library_id}, type={media_type}, download_full={download_full}")

    if not library_id or not media_type:
        raise HTTPException(status_code=400, detail="Missing library_id or media_type")

    if not hasattr(app.state, "api") or app.state.api is None:
        raise HTTPException(
            status_code=401,
            detail="API not initialized. Please set your cookies path in Settings, then restart the server."
        )

    api = app.state.api

    # Construct Apple Music URL
    storefront = api.storefront

    if download_full:
        # For full download, we need the catalog ID
        # First, get the library item to extract the catalog reference
        try:
            if media_type == "album":
                library_response = await api.get_library_album(library_id)
            elif media_type == "playlist":
                library_response = await api.get_library_playlist(library_id)
            else:
                # Songs don't have a "full" version, just download the library track
                download_full = False

            if download_full and library_response:
                # Try to extract catalog ID from relationships
                logger.info(f"Library response for full download: {library_response}")

                # The response structure is: {"data": [item]}
                data = library_response.get("data", [])
                if data:
                    item_data = data[0]
                    relationships = item_data.get("relationships", {})
                    logger.info(f"Relationships: {relationships}")

                    catalog_data = relationships.get("catalog", {}).get("data")
                    catalog_id = None

                    # Method 1: Check for direct catalog relationship
                    if catalog_data and len(catalog_data) > 0:
                        catalog_id = catalog_data[0].get("id")
                        logger.info(f"Method 1: Found catalog ID from direct relationship: {catalog_id}")

                    # Method 2: Extract catalog ID from track's playParams
                    if not catalog_id:
                        logger.info("Method 1 failed, trying Method 2: extracting from track playParams")
                        tracks_relationship = relationships.get("tracks", {})
                        tracks_data = tracks_relationship.get("data", [])

                        if tracks_data and len(tracks_data) > 0:
                            # Get the first track's catalog ID
                            first_track = tracks_data[0]
                            track_attributes = first_track.get("attributes", {})
                            play_params = track_attributes.get("playParams", {})
                            catalog_song_id = play_params.get("catalogId")

                            logger.info(f"Found catalog song ID from track playParams: {catalog_song_id}")

                            if catalog_song_id:
                                try:
                                    # Fetch the catalog song to get its album relationship
                                    logger.info(f"Fetching catalog song {catalog_song_id} to extract album ID")
                                    catalog_song_response = await api.get_song(catalog_song_id, extend="", include="albums")

                                    song_data = catalog_song_response.get("data", [])
                                    if song_data and len(song_data) > 0:
                                        song_relationships = song_data[0].get("relationships", {})
                                        albums_data = song_relationships.get("albums", {}).get("data", [])

                                        if albums_data and len(albums_data) > 0:
                                            catalog_id = albums_data[0].get("id")
                                            logger.info(f"Method 2: Extracted catalog album ID: {catalog_id}")
                                        else:
                                            logger.warning("No albums found in catalog song relationships")
                                    else:
                                        logger.warning("No data in catalog song response")
                                except Exception as e:
                                    logger.warning(f"Failed to fetch catalog song: {e}")
                            else:
                                logger.warning("No catalogId found in track playParams")
                        else:
                            logger.warning("No tracks found in relationships")

                    # Use the catalog ID if we found one
                    if catalog_id:
                        # Use catalog URL for full download
                        url = f"https://music.apple.com/{storefront}/{media_type}/{catalog_id}"
                        logger.info(f"Using catalog URL for full download: {url}")
                    else:
                        # Fallback to library URL if no catalog reference found
                        logger.warning("Could not find catalog ID via any method, using library URL")
                        url = f"https://music.apple.com/{storefront}/library/{media_type}s/{library_id}"
                else:
                    logger.warning("No data in library response, using library URL")
                    url = f"https://music.apple.com/{storefront}/library/{media_type}s/{library_id}"
            else:
                # Fallback to library URL
                logger.info("No library response or download_full is False, using library URL")
                url = f"https://music.apple.com/{storefront}/library/{media_type}s/{library_id}"
        except Exception as e:
            logger.warning(f"Could not fetch catalog reference, using library URL: {e}", exc_info=True)
            url = f"https://music.apple.com/{storefront}/library/{media_type}s/{library_id}"
    else:
        # Library URLs format: https://music.apple.com/{storefront}/library/{type}/{id}
        url = f"https://music.apple.com/{storefront}/library/{media_type}s/{library_id}"

    logger.info(f"Generated URL for download: {url}")
    logger.info(f"DownloadRequest will have urls=['{url}']")

    # Create a download request
    download_request = DownloadRequest(
        urls=[url],
        cookies_path=request_data.get("cookies_path"),
        output_path=request_data.get("output_path"),
        temp_path=request_data.get("temp_path"),
        final_path_template=request_data.get("final_path_template"),
        cover_format=request_data.get("cover_format"),
        cover_size=request_data.get("cover_size"),
        song_codec=request_data.get("song_codec"),
        music_video_codec=request_data.get("music_video_codec"),
        music_video_resolution=request_data.get("music_video_resolution"),
        no_cover=request_data.get("no_cover", False),
        no_lyrics=request_data.get("no_lyrics", False),
        extra_tags=request_data.get("extra_tags", False),
        enable_retry_delay=request_data.get("enable_retry_delay", True),
        max_retries=request_data.get("max_retries", 3),
        retry_delay=request_data.get("retry_delay", 60),
        song_delay=request_data.get("song_delay", 0.0),
        queue_item_delay=request_data.get("queue_item_delay", 0.0),
        continue_on_error=request_data.get("continue_on_error", False),
    )

    # Add display info for queue
    display_info = {
        "title": request_data.get("display_title", f"Library {media_type}"),
        "type": media_type
    }

    # Add to queue
    logger.info(f"About to call add_to_queue with display_info={display_info}")
    item_id = add_to_queue(download_request, display_info)
    logger.info(f"add_to_queue returned item_id={item_id}")

    # Debug: Check queue state immediately after adding
    with queue_lock:
        queue_count = len(download_queue)
        queued_count = sum(1 for item in download_queue if item.status == QueueItemStatus.QUEUED)
        logger.info(f"Queue state after add: total={queue_count}, queued={queued_count}")

    return SessionResponse(
        session_id=item_id,
        status="queued",
        message="Download added to queue"
    )


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time progress updates."""
    await websocket.accept()

    # Add to broadcast list for queue updates
    websocket_clients.append(websocket)

    try:
        # Check if this is a queue item ID
        queue_item = None
        with queue_lock:
            for item in download_queue:
                if item.id == session_id:
                    queue_item = item
                    break

        if queue_item:
            # This is a queue item - wait for it to start downloading
            await websocket.send_json({
                "type": "log",
                "message": f"Item added to queue. Position: {[i.id for i in download_queue if i.status == QueueItemStatus.QUEUED].index(session_id) + 1 if session_id in [i.id for i in download_queue if i.status == QueueItemStatus.QUEUED] else 'Processing'}",
                "level": "info",
            })

            # Wait for the item to start downloading
            while queue_item.status == QueueItemStatus.QUEUED:
                await asyncio.sleep(0.5)

            # Check if item was cancelled
            if queue_item.status == QueueItemStatus.CANCELLED:
                await websocket.send_json({
                    "type": "error",
                    "message": "Download was cancelled",
                })
                return

            # Item is now downloading - get the actual session
            if queue_item.session_id and queue_item.session_id in active_sessions:
                session = active_sessions[queue_item.session_id]
                session["websocket"] = websocket

                await websocket.send_json({
                    "type": "log",
                    "message": "Download starting...",
                    "level": "info",
                })

                # Keep connection alive until download completes
                while session.get("status") == "running":
                    await asyncio.sleep(1)
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": "Session not found",
                })
                return

        elif session_id in active_sessions:
            # This is a direct session ID (old behavior)
            session = active_sessions[session_id]
            session["websocket"] = websocket
            session["status"] = "running"

            # Send initial connection message
            await websocket.send_json({
                "type": "log",
                "message": "Session started",
                "level": "info",
            })

            # Run the download process
            await run_download_session(session_id, session, websocket)

            session["status"] = "completed"
            if session_id in active_sessions:
                del active_sessions[session_id]
            if session_id in cancellation_flags:
                del cancellation_flags[session_id]

        else:
            await websocket.send_json({
                "type": "error",
                "message": "Invalid session ID or queue item ID",
            })

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"Error in WebSocket for session {session_id}: {e}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e),
            })
        except:
            pass
    finally:
        # Remove from broadcast list
        if websocket in websocket_clients:
            websocket_clients.remove(websocket)


def parse_apple_music_error(exception: Exception) -> str:
    """Extract user-friendly error message from Apple Music API exception.

    Apple Music API errors come in this format:
    Exception: ('Error getting webplayback:', '{"customerMessage":"..."}')

    This function extracts the customerMessage for display to users.
    """
    import json

    error_str = str(exception)

    # Check if this looks like an Apple Music API error
    if "Error getting" in error_str and "{" in error_str:
        try:
            # Extract JSON from string representation
            start_idx = error_str.find('{')
            end_idx = error_str.rfind('}') + 1

            if start_idx > 0 and end_idx > start_idx:
                json_str = error_str[start_idx:end_idx]
                error_data = json.loads(json_str)

                # Try to extract user-friendly message
                # Priority: customerMessage > dialog.message > dialog.explanation
                if "customerMessage" in error_data:
                    return error_data["customerMessage"]
                elif "dialog" in error_data:
                    if "message" in error_data["dialog"]:
                        msg = error_data["dialog"]["message"]
                        if msg and msg.strip():
                            return msg
                    elif "explanation" in error_data["dialog"]:
                        expl = error_data["dialog"]["explanation"]
                        if expl and expl.strip():
                            return expl
        except (json.JSONDecodeError, KeyError, ValueError, AttributeError):
            pass  # Fall through to return original error

    # Return original error string if parsing fails
    return error_str


async def safe_send_log(websocket: WebSocket, message: str, level: str = "info"):
    """Safely send log message via WebSocket and also log to logger."""
    # Always log to logger for background mode
    if level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    else:
        logger.info(message)

    # Also try to send via WebSocket if available
    if websocket:
        try:
            await websocket.send_json({
                "type": "log",
                "message": message,
                "level": level,
            })
        except:
            pass


async def download_with_retry(
    downloader,
    download_item,
    max_retries: int,
    retry_delay: int,
    websocket: WebSocket,
    session_id: str
) -> bool:
    """Download item with retry logic. Returns True if successful, False if all retries exhausted."""
    import asyncio

    # Extract track name for retry messages
    try:
        track_name = download_item.media_metadata.get("attributes", {}).get("name", "Unknown track")
    except (AttributeError, KeyError):
        track_name = "Unknown track"

    attempts = 0
    while attempts <= max_retries:
        try:
            # Log retry attempt (not first attempt)
            if attempts > 0 and max_retries > 0:
                await websocket.send_json({
                    "type": "log",
                    "message": f"Retry attempt {attempts}/{max_retries} for: {track_name}",
                    "level": "info"
                })

            # Attempt download
            logger.info(f"Calling downloader.download() for track: {track_name}")
            await downloader.download(download_item)
            logger.info(f"downloader.download() completed successfully for track: {track_name}")

            # Success! Log if this was after retries
            if attempts > 0 and max_retries > 0:
                await websocket.send_json({
                    "type": "log",
                    "message": f"Download succeeded after {attempts} retry attempt(s)",
                    "level": "success"
                })
            return True

        except MediaFileExists as e:
            # File already exists - treat as success (skip)
            await websocket.send_json({
                "type": "log",
                "message": f"Skipping: {str(e)}",
                "level": "info"
            })
            return True  # Skip, don't retry

        except (NotStreamable, FormatNotAvailable) as e:
            # Permanent content issues - don't retry, log as warning and skip
            await websocket.send_json({
                "type": "log",
                "message": f"Content unavailable: {str(e)}",
                "level": "warning"
            })
            return True  # Skip, don't retry (retrying won't fix these issues)

        except ExecutableNotFound as e:
            # Missing required executable (e.g., mp4decrypt for music videos)
            # Don't retry - installation required, so re-raise immediately
            error_msg = str(e)

            # Check if it's specifically mp4decrypt
            if "mp4decrypt" in error_msg.lower():
                await websocket.send_json({
                    "type": "log",
                    "message": "Music videos require mp4decrypt to be installed.",
                    "level": "error"
                })
                await websocket.send_json({
                    "type": "log",
                    "message": "Download mp4decrypt from: https://www.bento4.com/downloads/",
                    "level": "error"
                })
                await websocket.send_json({
                    "type": "log",
                    "message": "Add mp4decrypt to your system PATH and restart the server.",
                    "level": "error"
                })
            else:
                await websocket.send_json({
                    "type": "log",
                    "message": f"Required executable not found: {error_msg}",
                    "level": "error"
                })

            # Re-raise with clear message so it's not retried
            raise ExecutableNotFound(error_msg)

        except Exception as e:
            attempts += 1

            # Parse error message for user-friendly display
            error_msg = parse_apple_music_error(e)

            # Log the full exception for debugging
            logger.exception(f"Download attempt {attempts} failed:")

            # Send message to UI
            await safe_send_log(websocket, f"Download failed (attempt {attempts}/{max_retries + 1}): {error_msg}", "warning")

            # Check if we should retry
            if attempts <= max_retries:
                # Only sleep and retry if retries are enabled
                if max_retries > 0:
                    await safe_send_log(websocket, f"Retrying in {retry_delay} seconds...", "info")
                    await asyncio.sleep(retry_delay)
                else:
                    # No retries configured, fail immediately
                    return False
            else:
                # All retries exhausted
                await safe_send_log(websocket, f"Download failed after {max_retries + 1} attempts: {error_msg}", "error")
                return False

    return False


def is_direct_audio_url(url: str) -> bool:
    """Check if URL is a direct audio file (not Apple Music).

    Podcast URLs are direct HTTP links to MP3/M4A files.
    Apple Music URLs always start with https://music.apple.com
    """
    return not url.startswith("https://music.apple.com")


async def download_podcast_episodes(session_id: str, session: dict, websocket: WebSocket):
    """Download podcast episodes directly via HTTP (no authentication needed)."""
    request: DownloadRequest = session["request"]

    logger.info(f"download_podcast_episodes called with session_id={session_id}")
    logger.info(f"Podcast URLs: {request.urls}")

    async def send_log(message: str, level: str = "info"):
        """Send a log message via WebSocket and to logger."""
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

        try:
            await websocket.send_json({
                "type": "log",
                "message": message,
                "level": level,
            })
        except:
            pass

    try:
        # Setup output directory
        output_path = request.output_path
        if not output_path or output_path.strip() == "":
            output_path = "./downloads"
        else:
            output_path = output_path.strip()
            if output_path.startswith("~"):
                output_path = os.path.expanduser(output_path)

        base_output_dir = Path(output_path)

        # Create podcast-specific subdirectory if podcast name is available
        if request.podcast_name:
            import re
            # Sanitize podcast name for filesystem
            safe_podcast_name = re.sub(r'[\\/:*?"<>|]', '_', request.podcast_name)
            output_dir = base_output_dir / safe_podcast_name
        else:
            output_dir = base_output_dir

        output_dir.mkdir(parents=True, exist_ok=True)

        await send_log(f"Downloading {len(request.urls)} podcast episode(s)...")

        # Download each episode
        for url_index, url in enumerate(request.urls, 1):
            # Check for cancellation
            if cancellation_flags.get(session_id, False):
                await send_log("Download cancelled by user", "warning")
                break

            await send_log(f"[Episode {url_index}/{len(request.urls)}] Downloading: {url}")

            try:
                # Simple HTTP download
                async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
                    response = await client.get(url)
                    response.raise_for_status()

                    # Extract file extension from URL or Content-Disposition header
                    file_extension = None
                    if "Content-Disposition" in response.headers:
                        import re
                        disposition = response.headers["Content-Disposition"]
                        match = re.findall(r'filename="?([^"]+)"?', disposition)
                        if match:
                            original_filename = match[0]
                            # Extract extension from original filename
                            if '.' in original_filename:
                                file_extension = '.' + original_filename.rsplit('.', 1)[1]

                    if not file_extension:
                        # Extract extension from URL
                        from urllib.parse import urlparse, unquote
                        parsed = urlparse(url)
                        url_filename = unquote(Path(parsed.path).name)
                        # Remove query parameters
                        if '?' in url_filename:
                            url_filename = url_filename.split('?')[0]
                        # Extract extension
                        if '.' in url_filename:
                            file_extension = '.' + url_filename.rsplit('.', 1)[1]

                    # Default to .mp3 if no extension found
                    if not file_extension or not file_extension.lower().endswith(('.mp3', '.m4a', '.mp4', '.aac')):
                        file_extension = '.mp3'

                    # Build filename with date prefix and episode title
                    if request.episode_date and request.episode_title:
                        # Parse date and format as YYYY-MM-DD
                        from datetime import datetime
                        try:
                            date_obj = datetime.fromisoformat(request.episode_date.replace('Z', '+00:00'))
                            date_prefix = date_obj.strftime('%Y-%m-%d')
                        except (ValueError, AttributeError):
                            date_prefix = None

                        if date_prefix:
                            # Sanitize episode title for filename
                            import re
                            safe_title = re.sub(r'[\\/:*?"<>|]', '_', request.episode_title)
                            filename = f"{date_prefix} - {safe_title}{file_extension}"
                        else:
                            # No valid date, use title only
                            import re
                            safe_title = re.sub(r'[\\/:*?"<>|]', '_', request.episode_title)
                            filename = f"{safe_title}{file_extension}"
                    elif request.episode_title:
                        # No date, use title only
                        import re
                        safe_title = re.sub(r'[\\/:*?"<>|]', '_', request.episode_title)
                        filename = f"{safe_title}{file_extension}"
                    else:
                        # Fallback to URL-based filename
                        from urllib.parse import urlparse, unquote
                        parsed = urlparse(url)
                        filename = unquote(Path(parsed.path).name)
                        if '?' in filename:
                            filename = filename.split('?')[0]
                        if not filename.endswith(('.mp3', '.m4a', '.mp4', '.aac')):
                            filename += file_extension

                    # Save to podcast-specific directory
                    filepath = output_dir / filename

                    with open(filepath, 'wb') as f:
                        f.write(response.content)

                    await send_log(f"Downloaded: {filename}", "success")
                    logger.info(f"Podcast episode saved to: {filepath}")

            except Exception as e:
                await send_log(f"Failed to download episode: {str(e)}", "error")
                logger.error(f"Podcast download error: {e}")
                continue

        await send_log("All podcast downloads completed", "success")
        await websocket.send_json({"type": "complete"})

    except Exception as e:
        logger.error(f"Podcast download session failed: {e}")
        await send_log(f"Download failed: {str(e)}", "error")
        await websocket.send_json({"type": "complete"})


async def run_download_session(session_id: str, session: dict, websocket: WebSocket):
    """Run the actual download process with progress updates."""
    request: DownloadRequest = session["request"]

    logger.info(f"run_download_session called with session_id={session_id}")
    logger.info(f"request.urls = {request.urls}")
    logger.info(f"len(request.urls) = {len(request.urls) if request.urls else 'None'}")

    # NEW: Detect if URLs are podcasts (direct audio) or Apple Music URLs
    if request.urls and all(is_direct_audio_url(url) for url in request.urls):
        logger.info("Detected podcast URLs - routing to simple HTTP downloader")
        return await download_podcast_episodes(session_id, session, websocket)

    # Continue with Apple Music download flow for music.apple.com URLs
    logger.info("Detected Apple Music URLs - routing to standard downloader")

    async def send_log(message: str, level: str = "info"):
        """Send a log message via WebSocket and to logger."""
        # Always log to logger for background mode
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

        # Also try to send via WebSocket if available
        try:
            await websocket.send_json({
                "type": "log",
                "message": message,
                "level": level,
            })
        except:
            pass

    try:
        await send_log("Initializing Apple Music API...")

        # Extract retry/delay settings
        enable_retry_delay = getattr(request, 'enable_retry_delay', True)
        max_retries = getattr(request, 'max_retries', 3) if enable_retry_delay else 0
        retry_delay = getattr(request, 'retry_delay', 60) if enable_retry_delay else 0
        song_delay = getattr(request, 'song_delay', 0.0) if enable_retry_delay else 0.0
        queue_item_delay = getattr(request, 'queue_item_delay', 0.0) if enable_retry_delay else 0.0

        # Track if any download failed after retries
        any_failed = False

        # Initialize API - handle empty strings and expand ~ paths
        cookies_path = request.cookies_path
        logger.info(f"Received cookies_path: {repr(cookies_path)}")

        if not cookies_path or cookies_path.strip() == "":
            cookies_path = os.path.expanduser("~/.gamdl/cookies.txt")
            logger.info(f"Using default cookies path: {cookies_path}")
        else:
            cookies_path = cookies_path.strip()
            logger.info(f"Using provided cookies path (before expansion): {cookies_path}")
            # Expand ~ in user-provided paths
            if cookies_path.startswith("~"):
                cookies_path = os.path.expanduser(cookies_path)
                logger.info(f"Expanded ~ to: {cookies_path}")

        if not Path(cookies_path).exists():
            await send_log(f"Cookies file not found at {cookies_path}", "error")
            await send_log("Please provide a valid cookies.txt file", "error")
            await websocket.send_json({"type": "complete"})
            return

        api = await AppleMusicApi.create_from_netscape_cookies(
            cookies_path=cookies_path,
        )
        await send_log("Apple Music API initialized successfully", "success")

        # Store API instance globally for library endpoints
        app.state.api = api

        # Save cookies path to config for future startups
        config = load_webui_config()
        config["cookies_path"] = cookies_path
        save_webui_config(config)

        # Initialize iTunes API
        itunes_api = ItunesApi(
            api.storefront,
            api.language,
        )
        await send_log("iTunes API initialized successfully", "success")

        # Initialize interface
        interface = AppleMusicInterface(api, itunes_api)
        song_interface = AppleMusicSongInterface(interface)
        music_video_interface = AppleMusicMusicVideoInterface(interface)
        uploaded_video_interface = AppleMusicUploadedVideoInterface(interface)

        # Initialize downloaders - handle empty strings and expand ~ paths
        output_path = request.output_path
        if not output_path or output_path.strip() == "":
            output_path = "./downloads"
        else:
            output_path = output_path.strip()
            if output_path.startswith("~"):
                output_path = os.path.expanduser(output_path)

        temp_path = request.temp_path
        if not temp_path or temp_path.strip() == "":
            temp_path = "./temp"
        else:
            temp_path = temp_path.strip()
            if temp_path.startswith("~"):
                temp_path = os.path.expanduser(temp_path)

        # Parse enum values
        cover_format = CoverFormat.JPG
        if request.cover_format:
            try:
                cover_format = CoverFormat[request.cover_format.upper()]
            except KeyError:
                pass

        song_codec = SongCodec.AAC_LEGACY
        if request.song_codec:
            try:
                song_codec = SongCodec[request.song_codec.upper().replace('-', '_')]
            except KeyError:
                pass

        music_video_resolution = MusicVideoResolution.R1080P
        if request.music_video_resolution:
            try:
                music_video_resolution = MusicVideoResolution[f"R{request.music_video_resolution.upper()}"]
            except KeyError:
                pass

        base_downloader = AppleMusicBaseDownloader(
            output_path=output_path,
            temp_path=temp_path,
            wvd_path=None,
            save_cover=not request.no_cover,
            cover_size=request.cover_size or 1200,
            cover_format=cover_format,
        )

        song_downloader = AppleMusicSongDownloader(
            base_downloader=base_downloader,
            interface=song_interface,
            codec=song_codec,
            fetch_extra_tags=request.extra_tags,
            no_synced_lyrics=request.no_lyrics,
        )

        music_video_downloader = AppleMusicMusicVideoDownloader(
            base_downloader=base_downloader,
            interface=music_video_interface,
            resolution=music_video_resolution,
        )

        uploaded_video_downloader = AppleMusicUploadedVideoDownloader(
            base_downloader=base_downloader,
            interface=uploaded_video_interface,
        )

        downloader = AppleMusicDownloader(
            interface=interface,
            base_downloader=base_downloader,
            song_downloader=song_downloader,
            music_video_downloader=music_video_downloader,
            uploaded_video_downloader=uploaded_video_downloader,
        )

        logger.info("Downloader initialized successfully")
        logger.info(f"About to process {len(request.urls)} URL(s)")
        logger.info(f"request.urls type: {type(request.urls)}")
        logger.info(f"request.urls value: {request.urls}")
        await send_log(f"Processing {len(request.urls)} URL(s)...")

        # Set initial progress tracking based on whether we're downloading multiple URLs
        # For multi-URL downloads (like discographies), track at URL level
        # For single URL downloads, track at track level
        use_url_level_progress = len(request.urls) > 1

        if use_url_level_progress and current_downloading_item:
            # Initialize progress for multi-URL download
            with queue_lock:
                current_downloading_item.progress_total = len(request.urls)
                current_downloading_item.progress_current = 0
            await broadcast_queue_update()

        # Process each URL
        logger.info(f"Entering URL processing loop with {len(request.urls)} URLs")
        for url_index, url in enumerate(request.urls, 1):
            logger.info(f"Loop iteration {url_index}: processing URL: {url}")
            # Check for cancellation
            if cancellation_flags.get(session_id, False):
                await send_log("Download cancelled by user", "warning")
                break

            await send_log(f"[URL {url_index}/{len(request.urls)}] Processing: {url}")
            logger.info(f"After send_log, about to get URL info for: {url}")

            # Update progress for multi-URL downloads at URL level
            if use_url_level_progress and current_downloading_item:
                with queue_lock:
                    current_downloading_item.progress_current = url_index - 1
                await broadcast_queue_update()

            try:
                # Get URL info
                logger.info("Calling downloader.get_url_info()")
                url_info = downloader.get_url_info(url)
                logger.info(f"get_url_info returned: {url_info}")
                if not url_info:
                    await send_log(f"Could not parse URL: {url}", "warning")
                    logger.warning(f"URL info is None/empty, continuing to next URL")
                    continue

                # Get download queue
                await send_log(f"Fetching metadata for {url}...")
                logger.info("Calling downloader.get_download_queue()")
                download_queue = await downloader.get_download_queue(url_info)
                logger.info(f"get_download_queue returned {len(download_queue) if download_queue else 0} items")

                if not download_queue:
                    await send_log("No downloadable media found", "warning")
                    logger.warning("download_queue is empty, continuing to next URL")
                    continue

                await send_log(f"Found {len(download_queue)} track(s) to download", "success")
                logger.info(f"About to download {len(download_queue)} tracks")

                # Update queue item total progress for single URL downloads only
                # For multi-URL downloads, we already set this at the URL level
                if not use_url_level_progress and current_downloading_item:
                    with queue_lock:
                        current_downloading_item.progress_total = len(download_queue)
                        current_downloading_item.progress_current = 0
                    await broadcast_queue_update()

                # Download each item
                for download_index, download_item in enumerate(download_queue, 1):
                    # Check for cancellation before each download
                    if cancellation_flags.get(session_id, False):
                        await send_log("Download cancelled by user", "warning")
                        break

                    # Safely extract media title
                    if isinstance(download_item, DownloadItem) and download_item.media_metadata:
                        media_title = download_item.media_metadata.get("attributes", {}).get("name", "Unknown Title")
                    else:
                        media_title = "Unknown Title"
                        await send_log(f"[Track {download_index}/{len(download_queue)}] Warning: Invalid download item", "warning")

                    await send_log(f"[Track {download_index}/{len(download_queue)}] Downloading: {media_title}")

                    # Update queue item progress only for single URL downloads
                    # For multi-URL downloads, progress is tracked at URL level
                    if not use_url_level_progress and current_downloading_item:
                        with queue_lock:
                            current_downloading_item.progress_current = download_index
                        await broadcast_queue_update()

                    # Use retry wrapper instead of direct download
                    try:
                        success = await download_with_retry(
                            downloader=downloader,
                            download_item=download_item,
                            max_retries=max_retries,
                            retry_delay=retry_delay,
                            websocket=websocket,
                            session_id=session_id
                        )

                        if success:
                            await send_log(f"[Track {download_index}/{len(download_queue)}] Completed: {media_title}", "success")
                        else:
                            any_failed = True
                            await send_log(f"[Track {download_index}/{len(download_queue)}] Failed after retries: {media_title}", "error")

                    except ExecutableNotFound as e:
                        # Missing executable - raise DependencyMissing to mark as failed but continue queue
                        error_msg = str(e)
                        if "mp4decrypt" in error_msg.lower():
                            await send_log(f"[Track {download_index}/{len(download_queue)}] Failed: mp4decrypt dependency missing", "error")
                            raise DependencyMissing("mp4decrypt")
                        else:
                            await send_log(f"[Track {download_index}/{len(download_queue)}] Failed: {error_msg}", "error")
                            raise DependencyMissing(error_msg)

                    # Apply song delay if configured
                    if song_delay > 0:
                        await send_log(f"Waiting {song_delay} seconds before next song...", "info")
                        await asyncio.sleep(song_delay)

            except DependencyMissing:
                # Re-raise DependencyMissing to propagate to queue processor
                raise
            except Exception as e:
                await send_log(f"Error processing URL: {str(e)}", "error")
                logger.exception(f"Full traceback for URL {url}:")
                continue

            # Safety check - ensure download_queue was successfully created
            if not download_queue:
                await send_log("No download queue available, skipping URL", "warning")
                continue

            # Update progress after successfully completing this URL (for multi-URL downloads)
            if use_url_level_progress and current_downloading_item:
                with queue_lock:
                    current_downloading_item.progress_current = url_index
                await broadcast_queue_update()

        # If any download failed after retries, raise error to trigger queue pause
        if any_failed:
            raise Exception(f"One or more downloads failed after {max_retries + 1} attempts")

        await send_log("All downloads completed!", "success")
        await websocket.send_json({"type": "complete"})

    except Exception as e:
        logger.error(f"Download session error: {e}", exc_info=True)
        await send_log(f"Fatal error: {str(e)}", "error")
        await websocket.send_json({"type": "complete"})
        raise  # Re-raise to propagate to process_queue()


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


def main(host: str = "127.0.0.1", port: int = 8080):
    """Start the web server."""
    import uvicorn
    import webbrowser
    import threading

    url = f"http://{host}:{port}"

    print(f"\ngamdl Web UI starting...")
    print(f"Server: {url}")
    print(f"Press Ctrl+C to stop\n")

    # Open browser after a short delay to ensure server is ready
    def open_browser():
        import time
        time.sleep(3.0)
        webbrowser.open(url)

    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    # Filter out noisy queue status requests from access logs
    class QueueStatusFilter(logging.Filter):
        def filter(self, record):
            # Don't log /api/queue/status requests
            return "/api/queue/status" not in record.getMessage()

    # Add filter to uvicorn access logger before starting server
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.addFilter(QueueStatusFilter())

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
