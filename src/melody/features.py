"""Features that are switched on or off when Melody is built.

These are constants, not settings: nothing in the app changes them. A build
meant for other people (a Debian package, say) sets them before packaging.
"""

# The YouTube import sources ("From YouTube link" and "Search YouTube") in the
# import window. When False they are not offered and
# ``melody.import_sources.youtube`` is never imported, so that file (and the
# yt-dlp dependency) can be left out of the package altogether.
YOUTUBE_IMPORT_ENABLED = True

# The YouTube search in the image picker (the thumbnails of music videos for a
# track, covers for an album, channel pictures for an artist). When False the
# picker only offers "Select image…" (and, for a track, "Use album cover"), and
# ``melody.services.youtube_images`` is never imported, so that file can be
# left out of the package. Like the import sources, it needs yt-dlp.
YOUTUBE_IMAGE_SEARCH_ENABLED = True
