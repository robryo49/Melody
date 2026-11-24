"""Measuring tracks' loudness and writing their ReplayGain tags.

Each file is decoded as fast as it can be through GStreamer's ``rganalysis``
element (part of gst-plugins-good), which reports the track gain and the
track peak at the end of the file. ``analyse_tracks`` does that for a whole
list of tracks, a few files at a time, and tags the files (see
``replaygain.py``).

Nothing here touches the interface. It is meant to be run in a thread of its
own, and reports its progress through a callback.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from melody.services.replaygain import (
    ReplayGainError,
    read_track_gain,
    supports_replaygain,
    write_track_gain,
)

# How long to wait for the pipeline between two looks at whether the person
# cancelled (nanoseconds, as GStreamer wants it).
_POLL = 200 * Gst.MSECOND


class AnalysisError(Exception):
    """A file could not be analysed."""


class Cancelled(Exception):
    """The person stopped the analysis."""


def analysis_available():
    """Whether GStreamer has what the analysis needs (its ``rganalysis``
    element, from gst-plugins-good)."""
    Gst.init(None)
    return Gst.ElementFactory.find("rganalysis") is not None


def _link_audio(decode, pad, convert):
    """``decodebin`` found a stream: use it if it is the audio."""
    sink = convert.get_static_pad("sink")

    if sink.is_linked():
        return

    caps = pad.get_current_caps() or pad.query_caps(None)

    if caps is None or caps.get_size() == 0:
        return

    if caps.get_structure(0).get_name().startswith("audio/"):
        pad.link(sink)


def analyse_file(path, should_stop=lambda: False):
    """``(gain, peak)`` of the audio file at ``path``: the track gain in dB
    and the loudest sample (1.0 = full scale). Raises ``AnalysisError`` if it
    cannot be measured, and ``Cancelled`` when ``should_stop()`` says so."""
    pipeline = Gst.Pipeline.new(None)

    names = (
        "filesrc",
        "decodebin",
        "audioconvert",
        "audioresample",
        "rganalysis",
        "fakesink",
    )
    elements = [Gst.ElementFactory.make(name, None) for name in names]

    if any(element is None for element in elements):
        raise AnalysisError("A GStreamer plugin is missing")

    source, decode, convert, resample, analysis, sink = elements

    source.set_property("location", str(path))
    # Analyse even a file that already has tags: that is what was asked for.
    analysis.set_property("forced", True)
    # One track, so its result is final as soon as the file ends.
    analysis.set_property("num-tracks", 1)
    # As fast as the file can be decoded, not at its own speed.
    sink.set_property("sync", False)

    for element in elements:
        pipeline.add(element)

    source.link(decode)
    convert.link(resample)
    resample.link(analysis)
    analysis.link(sink)
    decode.connect("pad-added", _link_audio, convert)

    bus = pipeline.get_bus()
    gain = None
    peak = None

    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise AnalysisError("Could not open the file")

        while True:
            if should_stop():
                raise Cancelled()

            message = bus.timed_pop_filtered(
                _POLL,
                Gst.MessageType.EOS
                | Gst.MessageType.ERROR
                | Gst.MessageType.TAG,
            )

            if message is None:
                continue

            if message.type == Gst.MessageType.ERROR:
                error, _debug = message.parse_error()
                raise AnalysisError(error.message)

            if message.type == Gst.MessageType.TAG:
                tags = message.parse_tag()

                found, value = tags.get_double("replaygain-track-gain")

                if found:
                    gain = value

                found, value = tags.get_double("replaygain-track-peak")

                if found:
                    peak = value

            elif message.type == Gst.MessageType.EOS:
                break
    finally:
        pipeline.set_state(Gst.State.NULL)

    if gain is None:
        raise AnalysisError(
            "No loudness could be measured (is the file very short?)"
        )

    return gain, peak


def default_workers():
    """How many files to analyse at once: a few, leaving a core free for the
    rest of the computer."""
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def analyse_tracks(
        tracks,
        *,
        skip_tagged=True,
        progress=None,
        should_stop=lambda: False,
        workers=None,
):
    """Analyse ``tracks`` and write each one's ReplayGain tags into its file.

    * ``skip_tagged``: leave alone the tracks that already have a track gain.
    * ``progress(done, total, title)`` is called (from the worker threads)
      each time a track is dealt with.
    * ``should_stop()`` is asked all the time; once it is True the rest is
      dropped.

    Returns a dictionary with ``analysed`` (how many were tagged), ``skipped``
    (already tagged), ``unsupported`` (a file type that cannot hold the tag),
    ``failed`` (a list of messages) and ``cancelled``.
    """
    tracks = list(tracks)
    total = len(tracks)
    summary = {
        "analysed": 0,
        "skipped": 0,
        "unsupported": 0,
        "failed": [],
        "cancelled": False,
    }
    lock = threading.Lock()
    finished = [0]

    def deal_with(track):
        title = str(track.get("title") or track.get("path") or "")
        outcome = None
        failure = None

        if should_stop():
            return

        path = track.get("path")

        try:
            if not path or not supports_replaygain(path):
                outcome = "unsupported"
            elif skip_tagged and read_track_gain(path) is not None:
                outcome = "skipped"
            else:
                gain, peak = analyse_file(path, should_stop)
                write_track_gain(path, gain, peak)
                outcome = "analysed"
        except Cancelled:
            return
        except (AnalysisError, ReplayGainError) as error:
            failure = f"{title}: {error}"
        except Exception as error:
            failure = f"{title}: {error}"

        with lock:
            if failure is not None:
                summary["failed"].append(failure)
            else:
                summary[outcome] += 1

            finished[0] += 1
            done = finished[0]

        if progress is not None:
            progress(done, total, title)

    with ThreadPoolExecutor(max_workers=workers or default_workers()) as pool:
        # ``map`` only to wait for every track; each does its own reporting.
        list(pool.map(deal_with, tracks))

    summary["cancelled"] = bool(should_stop())
    return summary
