"""Web UI server for gamdl using FastAPI."""

import asyncio
import json
import logging
import os
import uuid
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
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

    # Retry & delay settings
    enable_retry_delay: bool = True  # Enable/disable retry and delay features
    max_retries: int = 3  # Number of retry attempts
    retry_delay: int = 60  # Seconds to wait between retries

    # Sleep/delay settings
    song_delay: float = 0.0  # Seconds to wait after each song
    queue_item_delay: float = 0.0  # Seconds to wait after each queue item (album/playlist)


class SessionResponse(BaseModel):
    session_id: str
    status: str
    message: str


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
                for item in download_queue:
                    if item.status == QueueItemStatus.QUEUED:
                        next_item = item
                        break

            if not next_item:
                # No more items to process
                await asyncio.sleep(2)

                # Check if queue is truly empty
                with queue_lock:
                    has_queued = any(item.status == QueueItemStatus.QUEUED for item in download_queue)
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

                except Exception as e:
                    # Download failed (after retries exhausted)
                    logger.error(f"Error processing queue item: {e}", exc_info=True)

                    with queue_lock:
                        next_item.status = QueueItemStatus.FAILED
                        next_item.error_message = str(e)
                        next_item.completed_at = datetime.now()

                        # PAUSE THE QUEUE as per user requirement
                        queue_paused = True

                    logger.warning(f"Queue PAUSED due to retry exhaustion for item {next_item.id}")

                    await websocket.send_json({
                        "type": "log",
                        "message": "⚠️ Queue paused due to download failures. Please review errors and resume manually.",
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

                except Exception as e:
                    # Download failed (after retries exhausted)
                    logger.error(f"Error processing queue item: {e}", exc_info=True)

                    with queue_lock:
                        next_item.status = QueueItemStatus.FAILED
                        next_item.error_message = str(e)
                        next_item.completed_at = datetime.now()

                        # PAUSE THE QUEUE as per user requirement
                        queue_paused = True

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
            label {
                display: block;
                margin-bottom: 5px;
                font-weight: 500;
                color: #333;
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
            #settingsView h2 {
                margin-top: 0;
                margin-bottom: 5px;
            }
            #settingsView h3 {
                margin-top: 25px;
                margin-bottom: 15px;
                color: #333;
                font-size: 18px;
                border-bottom: 2px solid #007aff;
                padding-bottom: 5px;
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
            .queue-panel.collapsed {
                transform: translateX(calc(100% - 40px));
            }
            body {
                margin-right: 350px;
                transition: margin-right 0.3s ease;
            }
            body.queue-collapsed {
                margin-right: 40px;
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
            .queue-toggle {
                background: rgba(255, 255, 255, 0.2);
                border: none;
                color: white;
                width: 30px;
                height: 30px;
                border-radius: 50%;
                cursor: pointer;
                font-size: 16px;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: background 0.2s;
            }
            .queue-toggle:hover {
                background: rgba(255, 255, 255, 0.3);
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
                border: 1px solid #ddd;
                border-radius: 4px;
                cursor: pointer;
                transition: all 0.2s;
            }
            .queue-control-btn:hover {
                background: #f0f0f0;
                border-color: #007aff;
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
                border-radius: 6px;
                padding: 12px;
                margin-bottom: 10px;
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
                margin-bottom: 8px;
            }
            .queue-item-title {
                font-weight: 600;
                font-size: 14px;
                color: #333;
                margin-bottom: 4px;
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
                font-size: 16px;
                margin-right: 8px;
            }
            .queue-item-meta {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 8px;
                font-size: 12px;
            }
            .queue-item-info {
                font-size: 11px;
                color: #007aff;
                margin-bottom: 8px;
            }
            .queue-item-error {
                font-size: 11px;
                color: #ff3b30;
                background: #fff5f5;
                padding: 6px 8px;
                border-radius: 4px;
                margin-bottom: 8px;
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
                <button class="nav-tab" onclick="switchView('settings', this)">Settings</button>
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

                <div class="form-group">
                    <label>
                        <input type="checkbox" id="noCover" name="noCover">
                        Skip cover art download
                    </label>
                </div>

                <h3>Metadata Options</h3>
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="noLyrics" name="noLyrics">
                        Skip lyrics download
                    </label>
                </div>

                <div class="form-group">
                    <label>
                        <input type="checkbox" id="extraTags" name="extraTags">
                        Fetch extra tags from Apple Music preview
                    </label>
                </div>

                <h3>⏱️ Retry & Delay Options</h3>
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="enableRetryDelay" name="enableRetryDelay" checked>
                        Enable retry & delay features
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

                <div class="button-group">
                    <button type="button" onclick="saveAllSettings()">Save Settings</button>
                </div>
            </div>

            <!-- Queue Side Panel -->
            <div id="queuePanel" class="queue-panel">
                <div class="queue-header">
                    <h3>Download Queue</h3>
                    <button class="queue-toggle" onclick="toggleQueuePanel()">
                        <span id="queueToggleIcon">◀</span>
                    </button>
                </div>

                <div class="queue-controls">
                    <button id="pauseQueueBtn" onclick="pauseQueue()" class="queue-control-btn">
                        <span id="pauseIcon">⏸</span> Pause
                    </button>
                    <button onclick="clearCompleted()" class="queue-control-btn">
                        🗑 Clear Completed
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
                        <div class="queue-empty-icon">📭</div>
                        <div>No downloads in queue</div>
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

                // Load library data on first view if needed
                if (view === 'library' && !document.getElementById('albumsGrid').hasChildNodes()) {
                    loadLibraryAlbums();
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

                const btn = document.createElement('button');
                btn.textContent = 'Download';
                btn.onclick = () => downloadLibraryItem(item.id, type, item.name, item.artist);

                div.appendChild(img);
                div.appendChild(title);
                div.appendChild(subtitle);
                div.appendChild(btn);

                return div;
            }

            async function downloadLibraryItem(libraryId, mediaType, itemName, itemArtist) {
                try {
                    // Format display title
                    let displayTitle = itemName;
                    if (itemArtist && mediaType !== 'playlist') {
                        displayTitle = `${itemName} - ${itemArtist}`;
                    }

                    const response = await fetch('/api/library/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            library_id: libraryId,
                            media_type: mediaType,
                            display_title: displayTitle,
                            cookies_path: document.getElementById('cookiesPath').value,
                            output_path: document.getElementById('outputPath').value,
                            enable_retry_delay: document.getElementById('enableRetryDelay').checked,
                            max_retries: parseInt(document.getElementById('maxRetries').value) || 3,
                            retry_delay: parseInt(document.getElementById('retryDelay').value) || 60,
                            song_delay: parseFloat(document.getElementById('songDelay').value) || 0,
                            queue_item_delay: parseFloat(document.getElementById('queueItemDelay').value) || 0,
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

                // Retry/delay options
                const enableRetryDelay = localStorage.getItem('gamdl_enable_retry_delay');
                const maxRetries = localStorage.getItem('gamdl_max_retries');
                const retryDelay = localStorage.getItem('gamdl_retry_delay');
                const songDelay = localStorage.getItem('gamdl_song_delay');
                const queueItemDelay = localStorage.getItem('gamdl_queue_item_delay');

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
                if (enableRetryDelay !== null) document.getElementById('enableRetryDelay').checked = enableRetryDelay === 'true';
                if (maxRetries) document.getElementById('maxRetries').value = maxRetries;
                if (retryDelay) document.getElementById('retryDelay').value = retryDelay;
                if (songDelay) document.getElementById('songDelay').value = songDelay;
                if (queueItemDelay) document.getElementById('queueItemDelay').value = queueItemDelay;
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

                // Retry/delay options
                const enableRetryDelay = document.getElementById('enableRetryDelay').checked;
                const maxRetries = document.getElementById('maxRetries').value;
                const retryDelay = document.getElementById('retryDelay').value;
                const songDelay = document.getElementById('songDelay').value;
                const queueItemDelay = document.getElementById('queueItemDelay').value;

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
                localStorage.setItem('gamdl_enable_retry_delay', enableRetryDelay);
                localStorage.setItem('gamdl_max_retries', maxRetries);
                localStorage.setItem('gamdl_retry_delay', retryDelay);
                localStorage.setItem('gamdl_song_delay', songDelay);
                localStorage.setItem('gamdl_queue_item_delay', queueItemDelay);
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

            let queuePanelCollapsed = false;
            let queueUpdateInterval = null;

            function toggleQueuePanel() {
                const panel = document.getElementById('queuePanel');
                const toggleIcon = document.getElementById('queueToggleIcon');
                queuePanelCollapsed = !queuePanelCollapsed;

                if (queuePanelCollapsed) {
                    panel.classList.add('collapsed');
                    document.body.classList.add('queue-collapsed');
                    toggleIcon.textContent = '▶';
                } else {
                    panel.classList.remove('collapsed');
                    document.body.classList.remove('queue-collapsed');
                    toggleIcon.textContent = '◀';
                }
            }

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
                if (queueData.paused) {
                    pauseBtn.textContent = '▶ Resume';
                    pauseBtn.style.background = '#34c759';

                    // Show warning if there are failed items
                    const hasFailed = queueData.items.some(item => item.status === 'failed');
                    if (hasFailed) {
                        pauseBtn.textContent = '⚠️ Resume (Check Errors)';
                        pauseBtn.style.background = '#ff9500';  // Orange warning color
                    }
                } else {
                    pauseBtn.textContent = '⏸ Pause';
                    pauseBtn.style.background = '#007aff';
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
                        'queued': '⏳',
                        'downloading': '⬇️',
                        'completed': '✅',
                        'failed': '❌',
                        'cancelled': '⛔'
                    }[item.status] || '•';

                    const statusText = item.status.charAt(0).toUpperCase() + item.status.slice(1);

                    let actionButton = '';
                    if (item.status === 'queued') {
                        actionButton = `<button class="queue-item-remove" onclick="removeQueueItem('${item.id}')">Remove</button>`;
                    } else if (item.status === 'downloading') {
                        actionButton = `<button class="queue-item-view" onclick="viewDownloadProgress('${item.id}')">View Progress</button>`;
                    } else if (item.status === 'failed') {
                        actionButton = `<button class="queue-item-remove" onclick="removeQueueItem('${item.id}')">Remove</button>`;
                    }

                    const errorMessage = item.error_message ?
                        `<div class="queue-item-error">Error: ${escapeHtml(item.error_message)}</div>` : '';

                    const urlInfo = item.url_count > 1 ?
                        `<div class="queue-item-info">${item.url_count} URLs</div>` : '';

                    return `
                        <div class="queue-item ${statusClass}">
                            <div class="queue-item-header">
                                <span class="queue-item-icon">${statusIcon}</span>
                                <span class="queue-item-title">${escapeHtml(item.display_title)}</span>
                            </div>
                            <div class="queue-item-meta">
                                <span class="queue-item-type">${escapeHtml(item.display_type)}</span>
                                <span class="queue-item-status">${statusText}</span>
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

                // The WebSocket should already be connected for this item
                // If not, we can reconnect using the item ID
                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    sessionId = itemId;
                    connectWebSocket();
                }
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
        </script>
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


@app.post("/api/download", response_model=SessionResponse)
async def start_download(request: DownloadRequest):
    """Add download to queue instead of starting immediately."""
    # Extract display info from URLs
    display_info = None
    if request.urls:
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
            detail="API not initialized. Please start a download session first to initialize API."
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
            detail="API not initialized. Please start a download session first to initialize API."
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
            detail="API not initialized. Please start a download session first to initialize API."
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

    if not library_id or not media_type:
        raise HTTPException(status_code=400, detail="Missing library_id or media_type")

    if not hasattr(app.state, "api") or app.state.api is None:
        raise HTTPException(
            status_code=401,
            detail="API not initialized. Please start a download session first to initialize API."
        )

    api = app.state.api

    # Construct Apple Music URL from library ID
    # Library URLs format: https://music.apple.com/{storefront}/library/{type}/{id}
    storefront = api.storefront
    url = f"https://music.apple.com/{storefront}/library/{media_type}s/{library_id}"

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
    )

    # Add display info for queue
    display_info = {
        "title": request_data.get("display_title", f"Library {media_type}"),
        "type": media_type
    }

    # Add to queue
    item_id = add_to_queue(download_request, display_info)

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

    attempts = 0
    while attempts <= max_retries:
        try:
            # Attempt download
            await downloader.download(download_item)
            return True  # Success

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

        except Exception as e:
            attempts += 1
            error_msg = str(e)

            if attempts <= max_retries:
                # Still have retries left
                retry_num = attempts
                await websocket.send_json({
                    "type": "log",
                    "message": f"Download failed (attempt {retry_num}/{max_retries + 1}): {error_msg}",
                    "level": "warning"
                })
                await websocket.send_json({
                    "type": "log",
                    "message": f"Retrying in {retry_delay} seconds...",
                    "level": "info"
                })

                # Wait for retry delay
                await asyncio.sleep(retry_delay)
            else:
                # All retries exhausted
                await websocket.send_json({
                    "type": "log",
                    "message": f"Download failed after {max_retries + 1} attempts: {error_msg}",
                    "level": "error"
                })
                return False  # Failure

    return False


async def run_download_session(session_id: str, session: dict, websocket: WebSocket):
    """Run the actual download process with progress updates."""
    request: DownloadRequest = session["request"]

    async def send_log(message: str, level: str = "info"):
        """Send a log message via WebSocket."""
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

        await send_log(f"Processing {len(request.urls)} URL(s)...")

        # Process each URL
        for url_index, url in enumerate(request.urls, 1):
            # Check for cancellation
            if cancellation_flags.get(session_id, False):
                await send_log("Download cancelled by user", "warning")
                break

            await send_log(f"[URL {url_index}/{len(request.urls)}] Processing: {url}")

            try:
                # Get URL info
                url_info = downloader.get_url_info(url)
                if not url_info:
                    await send_log(f"Could not parse URL: {url}", "warning")
                    continue

                # Get download queue
                await send_log(f"Fetching metadata for {url}...")
                download_queue = await downloader.get_download_queue(url_info)

                if not download_queue:
                    await send_log("No downloadable media found", "warning")
                    continue

                await send_log(f"Found {len(download_queue)} track(s) to download", "success")

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

                    # Use retry wrapper instead of direct download
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

                    # Apply song delay if configured
                    if song_delay > 0:
                        await send_log(f"Waiting {song_delay} seconds before next song...", "info")
                        await asyncio.sleep(song_delay)

            except Exception as e:
                await send_log(f"Error processing URL: {str(e)}", "error")
                logger.exception(f"Full traceback for URL {url}:")
                continue

            # Safety check - ensure download_queue was successfully created
            if not download_queue:
                await send_log("No download queue available, skipping URL", "warning")
                continue

        # If any download failed after retries, raise error to trigger queue pause
        if any_failed:
            raise Exception(f"One or more downloads failed after {max_retries + 1} attempts")

        await send_log("All downloads completed!", "success")
        await websocket.send_json({"type": "complete"})

    except Exception as e:
        logger.error(f"Download session error: {e}", exc_info=True)
        await send_log(f"Fatal error: {str(e)}", "error")
        await websocket.send_json({"type": "complete"})


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

    print(f"\n🎵 gamdl Web UI starting...")
    print(f"📡 Server: {url}")
    print(f"⚡ Press Ctrl+C to stop\n")

    # Open browser after a short delay to ensure server is ready
    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(url)

    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
