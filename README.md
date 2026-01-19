# Fork of Gamdl by Glomatico

A CLI & Web-based GUI app for downloading Apple Music songs, & music videos.

This tool is forked from **v2.8.2** of Gamdl by Glomatico, any fixes or updates to the original since then are not included.

-------------------------------------------------------------------------------------------------------------------------------
**Disclaimer:** I know very little about Python, this is all thanks to Claude.


# Unique Features in my Fork

## ✅ Current Features
These work well enough that I haven't encountered major issues or bugs that break the experience.

- WebUI Basic (offers simple CLI features)
- WebUI Advanced (offers an improved GUI experience for your Apple Music Library like one-click downloads!)
- Queue downloads & management
- Retry failed download
- Pause after failed downloads
- Delay between downloads
- Search feature (no need to open a seperate apple music tab)
- One-click, download entire artist discography (albums, songs, EPs)
- Set a monitored playlist to download new additions automatically (checks every 2 hours)
- Continue queue after failed downloads


## ⚠️ WIP Features
These are in "active" development and in varying states of useability. (Check the branches for these features)

- Apple Podcast Support ([Try it early](https://github.com/TheFauxDreamer/gamdl/tree/podcasts))
- Download part of an Artists Discography


## 🗓️ Future Features
These will come in time (maybe), I don't have a timeline tho.

- Fix the "null songs" count in WebUI playlists
- A better UI
- Improved Download Queue information

-------------------------------------------------------------------------------------------------------------------------------

## Installation

1. Have [Python](https://www.python.org/downloads/) installed.

2. Have the latest full version of [ffmpeg](https://www.ffmpeg.org/download.html) installed.

3. Download the code from this repository (from the green Code button)
   
4. Unzip and save it somewhere like Documents

5. Right click on the gamdl folder, select "Open a Terminal"

6. Enter the following into the Terminal to setup the tool & download the essential dependencies:
```
pip install -e ".[web]"
```

7. Once complete, in the Terminal type:
```
gamdl-web --advanced
```
or
```
gamdl-web
```
8. This will start the tool, and automatically open a web browser with the WebUI (The --advanced option contains the above features).


See [WEB_UI_QUICKSTART.md](WEB_UI_QUICKSTART.md) for detailed instructions.


-------------------------------------------------------------------------------------------------------------------------------

## Original Readme

[Click here to view the original readme](https://github.com/glomatico/gamdl)
