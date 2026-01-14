"""Web UI server for gamdl using FastAPI."""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict
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
from gamdl.downloader.exceptions import GamdlError
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


class SessionResponse(BaseModel):
    session_id: str
    status: str
    message: str


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

                <div class="button-group">
                    <button type="button" onclick="saveAllSettings()">Save Settings</button>
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

                const payload = {
                    urls: urls,
                    cookies_path: formData.get('cookiesPath') || null,
                    output_path: formData.get('outputPath') || null,
                    song_codec: formData.get('songCodec') || null,
                    cover_size: formData.get('coverSize') ? parseInt(formData.get('coverSize')) : null,
                    music_video_resolution: formData.get('musicVideoResolution') || null,
                    cover_format: formData.get('coverFormat') || null,
                    no_cover: document.getElementById('noCover').checked,
                    no_lyrics: document.getElementById('noLyrics').checked,
                    extra_tags: document.getElementById('extraTags').checked,
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
                btn.onclick = () => downloadLibraryItem(item.id, type);

                div.appendChild(img);
                div.appendChild(title);
                div.appendChild(subtitle);
                div.appendChild(btn);

                return div;
            }

            async function downloadLibraryItem(libraryId, mediaType) {
                try {
                    const response = await fetch('/api/library/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            library_id: libraryId,
                            media_type: mediaType,
                            cookies_path: document.getElementById('cookiesPath').value,
                            output_path: document.getElementById('outputPath').value,
                        })
                    });

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to start download');
                    }

                    const data = await response.json();
                    sessionId = data.session_id;

                    // Switch to downloads view
                    const downloadsTab = document.querySelectorAll('.nav-tabs > .nav-tab')[1];
                    switchView('downloads', downloadsTab);

                    // Connect WebSocket
                    connectWebSocket(sessionId);
                    updateStatus('Connected - Processing...', true);
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
            }

            function saveAllSettings() {
                savePreferences();
                alert('Settings saved successfully!');
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

            // Load albums and preferences on page load
            document.addEventListener('DOMContentLoaded', () => {
                loadPreferences();
                loadLibraryAlbums();
            });
        </script>
    </body>
    </html>
    """


@app.post("/api/download", response_model=SessionResponse)
async def start_download(request: DownloadRequest):
    """Start a new download session."""
    session_id = str(uuid.uuid4())

    active_sessions[session_id] = {
        "status": "initializing",
        "request": request,
        "logs": [],
        "websocket": None,
    }
    cancellation_flags[session_id] = False

    logger.info(f"Created download session {session_id}")

    return SessionResponse(
        session_id=session_id,
        status="created",
        message="Download session created. Connect to WebSocket for progress.",
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
    """Start download from library item."""
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

    # Create new download session
    session_id = str(uuid.uuid4())

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

    # Store session
    active_sessions[session_id] = {
        "request": download_request,
        "status": "pending",
        "websocket": None,
    }

    return SessionResponse(
        session_id=session_id,
        status="pending",
        message="Download session created. Connect via WebSocket to start."
    )


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time progress updates."""
    await websocket.accept()

    if session_id not in active_sessions:
        await websocket.send_json({
            "type": "error",
            "message": "Invalid session ID",
        })
        await websocket.close()
        return

    session = active_sessions[session_id]
    session["websocket"] = websocket
    session["status"] = "running"

    try:
        # Send initial connection message
        await websocket.send_json({
            "type": "log",
            "message": "Session started",
            "level": "info",
        })

        # Run the download process
        await run_download_session(session_id, session, websocket)

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
        session["status"] = "completed"
        if session_id in active_sessions:
            del active_sessions[session_id]
        if session_id in cancellation_flags:
            del cancellation_flags[session_id]


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

        # Initialize API
        cookies_path = request.cookies_path or os.path.expanduser("~/.gamdl/cookies.txt")
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

        # Initialize downloaders
        output_path = request.output_path or "./downloads"
        temp_path = request.temp_path or "./temp"

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

                    try:
                        await downloader.download(download_item)
                        await send_log(f"[Track {download_index}/{len(download_queue)}] Completed: {media_title}", "success")
                    except GamdlError as e:
                        # Expected errors (file exists, not streamable, etc.) - show as warnings
                        await send_log(f"[Track {download_index}/{len(download_queue)}] Skipped: {str(e)}", "warning")
                        continue
                    except Exception as e:
                        # Unexpected errors - show as errors
                        await send_log(f"[Track {download_index}/{len(download_queue)}] Error: {str(e)}", "error")
                        continue

            except Exception as e:
                await send_log(f"Error processing URL: {str(e)}", "error")
                logger.exception(f"Full traceback for URL {url}:")
                continue

            # Safety check - ensure download_queue was successfully created
            if not download_queue:
                await send_log("No download queue available, skipping URL", "warning")
                continue

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

    print(f"\n🎵 gamdl Web UI starting...")
    print(f"📡 Server: http://{host}:{port}")
    print(f"⚡ Press Ctrl+C to stop\n")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
