# gamdl Web UI

A modern web interface for gamdl with real-time progress tracking.

## Features

- **Real-time Progress Updates**: WebSocket-based live progress updates for all stages:
  - Playlist pagination (fetching all pages)
  - Metadata processing with percentage completion
  - Individual track downloads

- **Clean, Modern Interface**: Simple and intuitive web UI that works in any browser

- **Full Feature Support**: Access to all major gamdl features including:
  - Multiple URLs (songs, albums, playlists)
  - Codec selection (AAC, ALAC, Atmos, etc.)
  - Cover art customization
  - Lyrics management
  - Extra tags from Apple Music preview

- **Session Management**: Each download session is tracked independently

## Installation

Install gamdl with web UI support:

```bash
# Install with optional web dependencies
pip install -e ".[web]"
```

Or install the dependencies manually:

```bash
pip install fastapi uvicorn[standard]
```

## Usage

### Starting the Web Server

```bash
# Start the web UI server (default: http://127.0.0.1:8080)
gamdl-web

# Custom host and port
gamdl-web --host 0.0.0.0 --port 3000
```

### Using the Web Interface

1. Open your browser to `http://127.0.0.1:8080`
2. Enter your Apple Music URLs (one per line)
3. Configure the cookies path (usually `~/.gamdl/cookies.txt`)
4. Optionally configure advanced settings:
   - Song codec
   - Cover art size and format
   - Video resolution
   - Metadata options
5. Click "Start Download"
6. Watch the real-time progress updates in the log window

## Advanced Web UI

The advanced web UI includes all features from the basic UI plus a **Library Browser** that lets you browse and download content directly from your Apple Music library.

### Starting the Advanced UI

```bash
# Start with library browser (default: http://127.0.0.1:8080)
gamdl-web --advanced

# Or use the short form
gamdl-web -adv

# Custom host and port
gamdl-web --advanced --host 0.0.0.0 --port 3000
```

### Additional Features

- **Albums View**: Browse all albums in your library with cover art
- **Playlists View**: View all your playlists
- **Songs View**: Browse individual songs
- **One-Click Downloads**: Download any library item directly from the UI
- **Dual Interface**: Switch between library browser and URL downloads

### Using the Library Browser

1. Start the advanced UI and open your browser
2. Initialize the API by starting any URL download first (required for library access)
3. Switch to the "Library Browser" tab
4. Choose between Albums, Playlists, or Songs
5. Click "Download" on any item to start downloading
6. Click "Load More" to view more items

For detailed documentation, see [ADVANCED_UI.md](ADVANCED_UI.md).

### Configuration

The web UI uses the same configuration as the CLI:

- **Cookies**: You need a valid Netscape format cookies file from Apple Music
  - Default location: `~/.gamdl/cookies.txt`
  - Can be overridden in the web interface

- **Output Path**: Where downloaded files will be saved
  - Default: `./downloads`

- **Temp Path**: Temporary files during download
  - Default: `./temp`

## Progress Tracking

The web UI provides detailed progress information at every stage:

### Large Playlists (e.g., 13,000 songs)

For large playlists, you'll see progress at multiple stages:

1. **Fetching Playlist Tracks**:
   ```
   Fetching playlist tracks (page 1, 300 tracks so far)...
   Fetching playlist tracks (page 2, 600 tracks so far)...
   ...
   Fetched 13000 tracks total. Processing metadata...
   ```

2. **Processing Metadata**:
   ```
   Processing metadata: 1300/13000 tracks (10.0%)
   Processing metadata: 2600/13000 tracks (20.0%)
   ...
   ```

3. **Downloading Tracks**:
   ```
   [Track 1/13000] Downloading: Song Title
   [Track 1/13000] Completed: Song Title
   ...
   ```

This ensures you always know the tool is working and not frozen, even for very large playlists.

## Architecture

### Backend (FastAPI)

- **Framework**: FastAPI with async support
- **WebSockets**: Real-time bidirectional communication
- **Sessions**: UUID-based session management
- **API Endpoints**:
  - `POST /api/download` - Start a download session
  - `WS /ws/{session_id}` - WebSocket for progress updates
  - `GET /health` - Health check

### Frontend (HTML/JavaScript)

- **Vanilla JavaScript**: No build tools required
- **WebSocket Client**: Connects to backend for real-time updates
- **Responsive Design**: Works on desktop and mobile
- **Color-coded Logs**: Different colors for info, warning, error, success

## Development

The web UI is structured as a separate module within gamdl:

```
gamdl/web/
├── __init__.py
├── cli.py          # CLI entry point
├── server.py       # FastAPI server with WebSocket support
└── README.md       # This file
```

### Running in Development

```bash
# From the project root
python -m gamdl.web.cli --host 127.0.0.1 --port 8080
```

Or directly:

```bash
python gamdl/web/cli.py
```

## Troubleshooting

### "Cookies file not found"

Make sure you have a valid cookies file:

1. Export cookies from your browser using a cookies extension
2. Save in Netscape format
3. Place at `~/.gamdl/cookies.txt` or specify the path in the web UI

### "Connection error" or WebSocket issues

- Check if the server is running
- Ensure no firewall is blocking the port
- Try a different browser

### Downloads not starting

- Verify your Apple Music subscription is active
- Check the cookies file is valid and not expired
- Look at the error messages in the log window

## Comparison with CLI

| Feature | CLI | Web UI |
|---------|-----|--------|
| Real-time Progress | ✅ (terminal) | ✅ (browser) |
| Multiple URLs | ✅ | ✅ |
| Configuration File | ✅ | ❌ (per-session) |
| Batch Processing | ✅ | ✅ |
| Remote Access | ❌ | ✅ (with --host 0.0.0.0) |
| Visual Interface | ❌ | ✅ |
| Background Downloads | ❌ | ✅ (browser can close) |

## Security Notes

- **Default**: Server binds to `127.0.0.1` (localhost only)
- **Remote Access**: Use `--host 0.0.0.0` only on trusted networks
- **Cookies**: Keep your cookies file secure - it provides access to your Apple Music account
- **HTTPS**: For production use, run behind a reverse proxy with HTTPS

## Future Enhancements

Potential improvements for future versions:

- Download queue management
- Download history
- User authentication
- Configuration persistence
- Download progress persistence (resume after browser refresh)
- Multiple concurrent downloads
- Download scheduling
