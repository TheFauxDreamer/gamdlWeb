# Fork of Gamdl by Glomatico

A CLI & Web-based GUI app for downloading Apple Music songs, & music videos.

-------------------------------------------------------------------------------------------------------------------------------
**Disclaimer:** I know very little about Python, this is all thanks to Claude.

PODCASTS

# Unique Features in my Fork

## ✅ Current Features
These work to a degree I haven't encountered major issues or bugs that break the experience.

- WebUI Basic (offers simple CLI features)
- WebUI Advanced (offers an improved GUI experience for your Apple Music Library like one-click downloads!)
- Queue downloads & management
- Retry failed download
- Pause after failures
- Delay between downloads
- Search feature (no need to open a seperate apple music tab)
- One-click, download entire artist discography (albums, songs, EPs)


## ⚠️ WIP Features
These are in "active" development and in varying states of useability.
- Set a monitored playlist and download new additions automatically


## 🗓️ Future Features
These will come in time, I don't have a timeline tho.

- Fix the "null songs" count in WebUI playlists
- Apple Podcast Support

-------------------------------------------------------------------------------------------------------------------------------

## 🌐 Web UI

Gamdl now includes a modern web interface with "real-time" progress tracking!

**Quick Start:**

```bash
# Install with web support
pipx install "gamdl[web]"

# Start the basic web server (similar features to the CLI)
gamdl-web

# Start the advanced web server (additional WIP features)
gamdl-web --advanced

```
See [WEB_UI_QUICKSTART.md](WEB_UI_QUICKSTART.md) for detailed instructions.


-------------------------------------------------------------------------------------------------------------------------------

## Original Readme

[Click here to view the original readme](https://github.com/glomatico/gamdl)
