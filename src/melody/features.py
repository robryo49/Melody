"""Features that are switched on or off when Melody is built.

These are constants, not settings: nothing in the app changes them. A build
meant for other people (a Debian package, say) sets them before packaging.
"""

# The YouTube import sources ("From YouTube link" and "Search YouTube") in the
# import window. When False they are not offered and
# ``melody.import_sources.youtube`` is never imported, so that file (and the
# yt-dlp dependency) can be left out of the package altogether.
YOUTUBE_IMPORT_ENABLED = True
