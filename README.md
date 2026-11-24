# Melody

Melody is a desktop music player and library manager for Linux, written in Python with GTK 4 and libadwaita. It keeps your music in one folder, organizes the files for you, and lets you browse and edit your collection by track, album, artist and playlist.

## Features

### Library

- **Back and forward:** the arrows next to the title of the sidebar go back and forth between the pages visited, like a browser's.
- **Library page:** every track in one list, with search and sorting by title, album, artist or duration. A side panel shows the selected track's artwork and details.
- **Albums, Artists and Playlists pages:** a grid of covers, with a page of its own for each album, artist and playlist.
- **Compilations:** long audio files such as mixes and live sets can be kept apart from regular tracks. They get their own page, and the minimum length that makes a file a compilation is configurable.
- **Singles:** a track with no album is treated as a single and filed in its artist's folder.
- **Artwork:** covers are read from the files and from image files in the folders, and looked up online when nothing is found locally.

### Editing

- **Tracks, albums, artists and playlists** can be edited from the right-click menu or from their page.
- **Renaming moves the files:** the music folder always follows the tags, as `Artist/Album/Title`. Renaming something to a name that already exists offers to merge the two.
- **Covers and images** can be changed for tracks, albums, artists and playlists, by choosing an image file or by searching YouTube. Above the search, a row of suggestions from other sites: Deezer, iTunes and the Cover Art Archive for albums (and for the album of a track), Deezer, Wikimedia and Fanart.tv for artists. A track's search looks for its music video ("title artist") and its edit window can also reuse the album's cover; an album's looks for covers on YouTube Music; an artist's shows the pictures of the YouTube channels found for their name; a playlist's is a plain video search. The thumbnails are cropped to a square.
- **Start and end times:** a track can be trimmed to a section of its file.
- **Delete:** the right-click menu on a track can delete it. The file is moved to the trash and the track leaves the library and its playlists.
- **Multi-track select:** in every list of tracks, click selects a track, Ctrl+click adds or removes one, Shift+click selects a range and Ctrl+A selects the whole list. Right-clicking the selection opens a menu to play it, play it after the current track, add it to the queue, set the album or the artist of all the tracks at once, add them to a playlist, mark them as favorites, remove them from the playlist or delete them.

### Importing music

- **Sources:** audio files from your computer, a YouTube link, or a YouTube search.
- **Preview before saving:** a single file opens straight in the edit window. Several files open a preview list where each track can be edited before anything is added.
- **Copied, not moved:** imported files are copied into the music folder and tagged. The originals are left where they are.
- **Import from a page:** the album, artist and playlist pages have their own Import button. An album page fills in the album and artist, an artist page fills in the artist, and a playlist page adds what is imported to the playlist.

### Playback

- Playback through GStreamer, with a player bar offering previous, play and pause, next, a seek bar, volume, shuffle and repeat (off, all, one).
- Pressing play with nothing loaded plays the track selected in the Library.
- The play queue follows the page you start playing from.
- **Currently playing page:** under Search in the sidebar, it shows the track being played large, with its cover, title, artist and album (both open their pages), duration and play count, the previous, play or pause and next controls, and buttons to favorite, add to a playlist or edit it.
- **Playing queue page:** under Search in the sidebar, it lists the track being played and everything after it. Double-click a track to play from there, right-click to play it, move it to right after the current track or remove it, and use Clear to empty everything after the current track.
- Desktop integration through MPRIS: media keys, the shell's media widget and tools such as `playerctl` can show what is playing and control play, pause, next, previous, seek, shuffle, repeat and volume.

## Requirements

- Python 3.14 or newer
- [uv](https://docs.astral.sh/uv/)
- GTK 4, libadwaita and PyGObject
- GStreamer, with the plugins for the audio formats you want to play
- [mutagen](https://mutagen.readthedocs.io/) and [Pillow](https://python-pillow.org/)
- yt-dlp (installed by uv) for the YouTube import sources and the YouTube image search
- ffmpeg (optional) to convert downloaded audio when importing from YouTube

PyGObject, GTK, libadwaita, GStreamer, mutagen and Pillow are expected to come from your system packages. Melody's virtual environment is created with access to the system site packages so that it can use them.

Supported audio formats: MP3, FLAC, Ogg, Opus, M4A, M4B, AAC, WAV, AIFF, APE, WavPack and Musepack.

## Running

```sh
uv venv --system-site-packages
uv sync
uv run melody
```

The application can also be started directly with `python src/main.py`, from an environment that has the requirements above.

By default the library lives in `~/Musique`. Files found there are read when Melody starts, and **Rescan library** in the preferences checks everything again and moves any file that is not where its tags say it belongs.

## Preferences

- **Rescan library:** re-reads every file and reorganizes the folders.
- **Organization:** sorts files into artist and album folders.
- **Compilations:** turns the Compilations page on or off, sets the minimum length of a compilation, and chooses whether compilations also appear in their albums.

Every change is applied and saved immediately.

## Project layout

```
src/
  main.py                 entry point
  melody/
    application.py        the Adw.Application
    window.py             main window, navigation, edit and import flows
    features.py           build-time switches (for example the YouTube sources)
    database/             SQLite storage for tracks and playlists
    import_sources/       where music can be imported from (files, YouTube)
    services/             library scanning, editing, metadata, artwork, playback, MPRIS
    views/                Library, Albums, Artists, Playlists and Preferences pages
    widgets/              player bar, edit and import windows, shared controls
```

### Adding an import source

Each source is a class in its own file in `src/melody/import_sources/`, described in `base.py`, and listed in `__init__.py`. Sources contain no GTK code: the same windows serve all of them.

### Build-time features

`src/melody/features.py` holds constants that are fixed when Melody is built. Setting `YOUTUBE_IMPORT_ENABLED` to `False` removes the YouTube sources, and `youtube.py` and the yt-dlp dependency can then be left out of a package altogether. `YOUTUBE_IMAGE_SEARCH_ENABLED` does the same for the YouTube search in the image picker (which then only offers a file chooser, and a track's album cover): `services/youtube_images.py` can be left out.

## Notice about downloading

Only import audio you have the right to use. YouTube's terms of service do not allow downloading, and most of the music on it is copyrighted. The YouTube import sources are provided for content you are allowed to download. The same goes for the thumbnails and channel pictures offered when choosing an image: they belong to whoever made them.

## TODO

- [x] Multi-track select
- [x] "Currently playing" menu
- [x] "Playing queue" menu
- [x] Equalizer
- [x] Per-track Equalizer
- [x] ReplayGain
- [ ] Keyboard shortcuts
- [ ] Auto-scan
- [ ] Drag and drop to import
- [ ] Duplicate check on import