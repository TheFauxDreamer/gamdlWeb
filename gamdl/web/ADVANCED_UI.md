# gamdl Advanced Web UI Documentation

The Advanced Web UI provides a library browser that lets you view and download content directly from your Apple Music library.

## Features

### Library Browser
- **Albums View**: Browse all albums in your library with cover art
- **Playlists View**: View all your playlists
- **Songs View**: Browse individual songs
- **Pagination**: Load more content with "Load More" button
- **One-Click Downloads**: Download any library item directly from the UI

### URL Downloads
- All features from the basic Web UI
- Same download functionality and progress tracking
- Real-time WebSocket updates

## Installation

The advanced UI requires the same dependencies as the basic web UI:

```bash
pip install -e ".[web]"
```

Or install dependencies manually:

```bash
pip install fastapi uvicorn[standard]
```

## Usage

### Starting the Advanced UI

```bash
# Start with default settings (localhost:8080)
gamdl-web --advanced

# Or use the short form
gamdl-web -adv

# Custom host and port
gamdl-web --advanced --host 0.0.0.0 --port 3000
```

### Starting the Basic UI

The basic UI still works as before:

```bash
gamdl-web
```

## How It Works

### 1. First Time Setup

When you first open the advanced UI, you need to initialize the API:

1. Switch to the "URL Downloads" tab
2. Enter your cookies path
3. Start any download to initialize the API
4. Once initialized, switch back to "Library Browser" tab

**Important**: The library browser requires an initialized Apple Music API with valid cookies. The API is automatically initialized when you start your first download.

### 2. Browsing Your Library

Once the API is initialized:

1. Open the "Library Browser" tab
2. Choose between Albums, Playlists, or Songs
3. Scroll through your library content
4. Click "Load More" to view more items

### 3. Downloading from Library

To download an album, playlist, or song:

1. Find the item in the library browser
2. Click the "Download" button
3. The UI automatically switches to the "URL Downloads" view
4. Watch real-time progress via WebSocket connection
5. Files are saved to your configured output path

## Authentication

### Requirements

The advanced UI requires the same authentication as the basic UI:

- **Cookies**: Valid `cookies.txt` file in Netscape format
- **Subscription**: Active Apple Music subscription
- **Media User Token**: Extracted from browser cookies

### How to Export Cookies

1. Log in to [music.apple.com](https://music.apple.com) in your browser
2. Use a cookie export extension:
   - Chrome/Edge: "[Get cookies.txt](https://chrome.google.com/webstore/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid)"
   - Firefox: "[cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)"
3. Export cookies for `music.apple.com`
4. Save as `cookies.txt`

### Token Expiration

If you see errors like "No active subscription" or "API not initialized":

1. Your cookies may have expired
2. Re-export cookies from your browser
3. Restart the web UI with the new cookies file

## UI Overview

### Library Browser Tab

```
┌──────────────────────────────────────────────────┐
│  gamdl Advanced Web UI                           │
│  Browse your library or download from URLs       │
├──────────────────────────────────────────────────┤
│  [Library Browser] URL Downloads                 │
├──────────────────────────────────────────────────┤
│  [Albums] Playlists Songs                        │
│                                                   │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐   │
│  │ Cover  │ │ Cover  │ │ Cover  │ │ Cover  │   │
│  │ Name   │ │ Name   │ │ Name   │ │ Name   │   │
│  │ Artist │ │ Artist │ │ Artist │ │ Artist │   │
│  │12 songs│ │15 songs│ │10 songs│ │14 songs│   │
│  │Download│ │Download│ │Download│ │Download│   │
│  └────────┘ └────────┘ └────────┘ └────────┘   │
│                                                   │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐   │
│  │ ...    │ │ ...    │ │ ...    │ │ ...    │   │
│  └────────┘ └────────┘ └────────┘ └────────┘   │
│                                                   │
│                  [Load More]                      │
└──────────────────────────────────────────────────┘
```

### URL Downloads Tab

Same as the basic Web UI with:
- URL input field
- Cookies and output path configuration
- Advanced options (codec, cover size, etc.)
- Real-time progress log
- Cancel button

## Configuration

### Download Options

All download options from the basic UI are available:

- **Song Codec**: AAC, ALAC, Atmos, etc.
- **Music Video Resolution**: Best available, 1080p, 720p, etc.
- **Cover Size**: Custom pixel dimensions
- **Cover Format**: JPG, PNG
- **Metadata Options**: Skip cover, skip lyrics, extra tags

Configure these in the "URL Downloads" tab before downloading from the library.

## API Endpoints

The advanced UI adds these REST API endpoints:

- `GET /api/library/albums?limit=50&offset=0` - List library albums
- `GET /api/library/playlists?limit=50&offset=0` - List library playlists
- `GET /api/library/songs?limit=50&offset=0` - List library songs
- `POST /api/library/download` - Start download from library item

## Troubleshooting

### "API not initialized" Error

**Problem**: Library endpoints return 401 error

**Solution**:
1. Switch to "URL Downloads" tab
2. Configure cookies path
3. Start any download to initialize the API
4. Return to "Library Browser" tab

### "No active subscription" Error

**Problem**: Apple Music subscription not detected

**Solutions**:
1. Verify you have an active Apple Music subscription
2. Re-export cookies while logged in to music.apple.com
3. Ensure cookies.txt contains `media-user-token` cookie

### Empty Library

**Problem**: "No albums/playlists found" message

**Possible Causes**:
1. Your Apple Music library is actually empty
2. Cookies are from a different Apple ID
3. Library access is restricted

**Solutions**:
1. Add music to your library on music.apple.com
2. Verify cookies are from the correct Apple ID
3. Check subscription status

### Library Won't Load

**Problem**: Loading indicator stays forever

**Solutions**:
1. Check browser console for errors (F12)
2. Verify server is running
3. Check network connectivity
4. Restart the web UI

### Downloads Fail

**Problem**: Downloaded files are corrupted or missing

**Solutions**:
1. Check output path is writable
2. Verify disk space is available
3. Check cookies are still valid
4. Review progress log for specific errors

## Comparison with Basic UI

| Feature | Basic UI | Advanced UI |
|---------|----------|-------------|
| URL Downloads | ✅ | ✅ |
| Library Browser | ❌ | ✅ |
| View Albums | ❌ | ✅ |
| View Playlists | ❌ | ✅ |
| View Songs | ❌ | ✅ |
| Real-time Progress | ✅ | ✅ |
| WebSocket Updates | ✅ | ✅ |
| Cancel Downloads | ✅ | ✅ |
| Advanced Options | ✅ | ✅ |

## Performance

### Library Loading

- Albums/Playlists: 50 items per page
- Songs: 50 items per page
- Average load time: 1-3 seconds per page

### Memory Usage

- Basic UI: ~50MB
- Advanced UI: ~60MB (includes library caching)

### Network Usage

- Initial page load: ~500KB
- Library page: ~100-200KB (50 items with thumbnails)
- Thumbnails: ~5-10KB each (300x300px)

## Privacy & Security

### Local Operation

- All processing happens locally
- No data sent to third parties
- Direct connection to Apple Music API

### Cookie Security

- Cookies stored locally only
- Never transmitted except to Apple Music API
- Recommend using file permissions to protect cookies.txt

### Library Data

- Library metadata cached in browser only
- No persistent storage on server
- Cleared on page refresh

## Future Enhancements

Potential features for future versions:

- [ ] Sort options (recently added, alphabetical, by artist)
- [ ] Search/filter within library
- [ ] Bulk download (select multiple items)
- [ ] Recently played section
- [ ] Favorites/starred items
- [ ] Artist view (group by artist)
- [ ] Genre filtering
- [ ] Download queue management
- [ ] Download history
- [ ] Dark mode toggle

## Support

For issues or questions:

1. Check this documentation
2. Review the main README
3. Check GitHub issues: https://github.com/glomatico/gamdl/issues
4. Create a new issue with:
   - Error messages
   - Browser console logs (F12)
   - Steps to reproduce

## Technical Details

### Architecture

```
Browser
  ↓ HTTP GET /api/library/albums
FastAPI Server (server_advanced.py)
  ↓ await api.get_all_library_albums()
AppleMusicApi (apple_music_api.py)
  ↓ GET /v1/me/library/albums
Apple Music API
  ↓ Response
Browser (renders library grid)
```

### Download Flow

```
1. User clicks "Download" on library item
2. Browser → POST /api/library/download
3. Server constructs Apple Music URL
4. Server creates download session
5. Browser connects via WebSocket
6. Server processes download
7. Real-time updates via WebSocket
8. Download completes
```

### Session Management

- Each download gets a unique session ID (UUID)
- Sessions stored in memory (`active_sessions` dict)
- API instance shared across sessions via `app.state.api`
- WebSocket connection per session
- Automatic cleanup after completion

## Example Usage

### Scenario 1: Download Multiple Albums

1. Start advanced UI: `gamdl-web --advanced`
2. Open browser to http://127.0.0.1:8080
3. Initialize API (download any URL first)
4. Go to Library Browser → Albums
5. Click "Download" on first album
6. Wait for completion
7. Return to Library Browser
8. Click "Download" on next album
9. Repeat as needed

### Scenario 2: Browse and Download Playlist

1. Start advanced UI
2. Initialize API
3. Go to Library Browser → Playlists
4. Find desired playlist
5. Click "Download"
6. Watch real-time progress
7. Files saved to output path

### Scenario 3: Mix URL and Library Downloads

1. Start with URL download (initializes API)
2. While download is running, can browse library
3. After completion, download from library
4. Or switch back to URLs tab for more URL downloads
5. Seamlessly alternate between both methods

## License

Same as gamdl: MIT License

## Credits

Built on top of:
- gamdl by glomatico
- FastAPI framework
- Apple Music API
