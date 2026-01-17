# Fork of Gamdl v2.8.2 by Glomatico

A CLI & Web-based GUI app for downloading Apple Music songs, & music videos.

-------------------------------------------------------------------------------------------------------------------------------
**Disclaimer:** I know very little about Python, this is all thanks to Claude.


# Unique Features in my Fork

## ✅ Current Features
These work well enough that I haven't encountered major issues or bugs that break the experience.

- WebUI Basic (offers simple CLI features)
- WebUI Advanced (offers an improved GUI experience for your Apple Music Library like one-click downloads!)
- Queue downloads & management
- Retry failed download
- Pause after failures
- Delay between downloads
- Search feature (no need to open a seperate apple music tab)
- One-click, download entire artist discography (albums, songs, EPs)
- Set a monitored playlist to download new additions automatically


## ⚠️ WIP Features
These are in "active" development and in varying states of useability. (Check the branches for these features)

- Apple Podcast Support
- Download part of an Artists Discography


## 🗓️ Future Features
These will come in time (maybe), I don't have a timeline tho.

- Fix the "null songs" count in WebUI playlists
- A better UI
- Improved Download Queue information

-------------------------------------------------------------------------------------------------------------------------------

## Installation

1. Have [Python](https://www.python.org/downloads/) installed.

2. Download the code from this repository (from the green Code button)

3. Unzip and save it somewhere like Documents

4. Right click on the gamdl folder, select "Open a Terminal"

5. Enter the following into the Terminal to setup the tool & download the essential dependencies:
```
pip install -e ".[web]"
```

6. Once complete, in the Terminal type:
```
gamdl-web --advanced
```
or
```
gamdl-web
```
7. This will start the tool, and automatically open a web browser with the WebUI (The --advanced option contains the above features).


See [WEB_UI_QUICKSTART.md](WEB_UI_QUICKSTART.md) for detailed instructions.


-------------------------------------------------------------------------------------------------------------------------------

## Original Readme

[Click here to view the original readme](https://github.com/glomatico/gamdl)
