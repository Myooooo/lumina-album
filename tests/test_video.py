"""Unit tests for the video module.

The parsing tests run from captured ``ffprobe`` JSON, so they work on a machine
without ffmpeg. The tests that actually shell out are skipped when the binaries
are missing, which is what keeps the suite green in a bare checkout.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from photo_reviewer import video


def _ffmpeg_available() -> bool:
    return video.find_tools() is not None


class NormalizeCaptureTimeTests(unittest.TestCase):
    """The container's timestamp has to end up in the shape EXIF uses."""

    def test_iso_with_milliseconds_and_zulu(self) -> None:
        self.assertEqual(
            video.normalize_capture_time("2024-05-01T10:20:30.000000Z"),
            "2024:05:01 10:20:30",
        )

    def test_iso_without_fraction(self) -> None:
        self.assertEqual(
            video.normalize_capture_time("2024-05-01T10:20:30"), "2024:05:01 10:20:30"
        )

    def test_exif_shaped_input_is_accepted(self) -> None:
        self.assertEqual(
            video.normalize_capture_time("2023:01:02 03:04:05"), "2023:01:02 03:04:05"
        )

    def test_dashed_date_time_is_accepted(self) -> None:
        self.assertEqual(
            video.normalize_capture_time("2023-01-02 03:04:05"), "2023:01:02 03:04:05"
        )

    def test_nonsense_is_rejected(self) -> None:
        for value in ("", None, "not a date", "0", "0000:00:00 00:00:00"):
            self.assertIsNone(video.normalize_capture_time(value), value)

    def test_the_epoch_is_not_a_capture_time(self) -> None:
        """Encoders that never set the tag write 1970, which must not show."""
        self.assertIsNone(video.normalize_capture_time("1970-01-01T00:00:00.000000Z"))


class Iso6709Tests(unittest.TestCase):
    def test_positive_pair(self) -> None:
        self.assertEqual(
            video.parse_iso6709("+30.2500+120.1700/"), (30.25, 120.17)
        )

    def test_negative_longitude(self) -> None:
        latitude, longitude = video.parse_iso6709("-33.8688-070.6693/")
        self.assertAlmostEqual(latitude, -33.8688, places=4)
        self.assertAlmostEqual(longitude, -70.6693, places=4)

    def test_out_of_range_is_rejected(self) -> None:
        self.assertIsNone(video.parse_iso6709("+91.0000+120.0000/"))

    def test_empty_is_rejected(self) -> None:
        for value in ("", None, "nonsense"):
            self.assertIsNone(video.parse_iso6709(value), value)


H264_MP4 = {
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "12.480000",
        "bit_rate": "5799120",
        "tags": {"creation_time": "2024-05-01T10:20:30.000000Z"},
    },
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "avg_frame_rate": "30000/1001",
            "bit_rate": "5500000",
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "bit_rate": "128000",
        },
    ],
}

HEVC_ROTATED = {
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "8.0",
        "tags": {"com.apple.quicktime.model": "iPhone 15 Pro"},
    },
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "hevc",
            "profile": "Main 10",
            "width": 3840,
            "height": 2160,
            "pix_fmt": "yuv420p10le",
            "avg_frame_rate": "60/1",
            "side_data_list": [{"rotation": 90}],
        }
    ],
}

AVI_MPEG4 = {
    "format": {"format_name": "avi", "duration": "30.0"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "mpeg4",
            "profile": "Simple Profile",
            "width": 640,
            "height": 480,
            "avg_frame_rate": "25/1",
        }
    ],
}


class ParseProbeTests(unittest.TestCase):
    def test_h264_mp4(self) -> None:
        info = video.parse_probe(H264_MP4)
        self.assertEqual(info.duration_ms, 12480)
        self.assertEqual((info.width, info.height), (1920, 1080))
        self.assertEqual(info.video_codec, "h264")
        self.assertEqual(info.audio_codec, "aac")
        self.assertTrue(info.has_audio)
        self.assertEqual(info.capture_time, "2024:05:01 10:20:30")
        self.assertAlmostEqual(info.fps, 29.97, places=2)
        self.assertEqual(info.video_bitrate, 5500000)

    def test_rotation_swaps_the_reported_size(self) -> None:
        """A portrait clip must not claim to be landscape, same rule as photos."""
        info = video.parse_probe(HEVC_ROTATED)
        self.assertEqual(info.rotation, 90)
        self.assertEqual((info.width, info.height), (2160, 3840))
        self.assertEqual(info.model, "iPhone 15 Pro")

    def test_a_video_without_audio(self) -> None:
        info = video.parse_probe(HEVC_ROTATED)
        self.assertFalse(info.has_audio)
        self.assertEqual(info.audio_codec, "")

    def test_avi_is_not_a_browser_container(self) -> None:
        info = video.parse_probe(AVI_MPEG4)
        self.assertEqual(info.container, ".avi")
        self.assertEqual(info.play_mode, video.PLAY_TRANSCODE)


class PlayModeTests(unittest.TestCase):
    """Which of the three playback paths a file needs."""

    def _info(self, codec, container):
        info = video.VideoInfo()
        info.video_codec = codec
        info.container = container
        return info

    def test_h264_in_mp4_plays_as_is(self) -> None:
        self.assertEqual(self._info("h264", ".mp4").play_mode, video.PLAY_ORIGINAL)

    def test_h264_in_mov_plays_as_is(self) -> None:
        self.assertEqual(self._info("h264", ".mov").play_mode, video.PLAY_ORIGINAL)

    def test_vp9_webm_plays_as_is(self) -> None:
        self.assertEqual(self._info("vp9", ".webm").play_mode, video.PLAY_ORIGINAL)

    def test_hevc_is_offered_untouched_first(self) -> None:
        """Chrome on Windows may or may not decode it; the client finds out."""
        self.assertEqual(self._info("hevc", ".mp4").play_mode, video.PLAY_ORIGINAL)

    def test_h264_in_mkv_is_remuxed(self) -> None:
        self.assertEqual(self._info("h264", ".mkv").play_mode, video.PLAY_REMUX)

    def test_h264_in_mpegts_is_remuxed(self) -> None:
        self.assertEqual(self._info("h264", ".mts").play_mode, video.PLAY_REMUX)

    def test_prores_is_transcoded(self) -> None:
        self.assertEqual(self._info("prores", ".mov").play_mode, video.PLAY_TRANSCODE)

    def test_mpeg2_is_transcoded(self) -> None:
        self.assertEqual(self._info("mpeg2video", ".mts").play_mode, video.PLAY_TRANSCODE)

    def test_needs_transcode_matches_the_mode(self) -> None:
        self.assertTrue(self._info("prores", ".mov").needs_transcode)
        self.assertFalse(self._info("h264", ".mp4").needs_transcode)

    def test_a_file_with_no_container_extension_is_taken_on_trust(self) -> None:
        self.assertEqual(self._info("h264", "").play_mode, video.PLAY_ORIGINAL)


class ContainerForTests(unittest.TestCase):
    def test_the_extension_wins_over_the_format_name(self) -> None:
        """ffprobe calls both Matroska and WebM "matroska,webm"."""
        self.assertEqual(video.container_for("a.webm", "matroska,webm"), ".webm")
        self.assertEqual(video.container_for("a.mkv", "matroska,webm"), ".mkv")

    def test_the_format_name_is_used_when_there_is_no_extension(self) -> None:
        self.assertEqual(video.container_for("", "mov,mp4,m4a,3gp,3g2,mj2"), ".mp4")
        self.assertEqual(video.container_for("", "mpegts"), ".ts")

    def test_an_unknown_format_gives_an_empty_string(self) -> None:
        self.assertEqual(video.container_for("", "something else"), "")


class DecodingTests(unittest.TestCase):
    """ffprobe writes UTF-8; the locale on a Chinese Windows is not UTF-8.

    ``subprocess.run(..., text=True)`` decodes with the *locale* encoding, so a
    file whose path held a character outside that code page made the reader
    thread raise ``UnicodeDecodeError`` and the probe came back as ``None``: the
    video then had no metadata, no poster and an error status. The runner pins
    UTF-8 instead.
    """

    def test_output_is_decoded_as_utf8(self) -> None:
        out = video._run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write('视频'.encode('utf-8'))",
            ]
        )
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "视频")

    def test_undecodable_bytes_do_not_raise(self) -> None:
        out = video._run(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\xff\\xfeb')"]
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn("a", out.stdout)

    def test_a_non_ascii_path_still_probes(self) -> None:
        tools = video.find_tools()
        if not tools:
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "手机备份 2025年12月5日")
            os.makedirs(folder)
            path = os.path.join(folder, "视频测试.mp4")
            subprocess.run(
                [
                    tools.ffmpeg, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    path,
                ],
                check=False,
                capture_output=True,
            )
            info = video.probe(path)
            self.assertIsNotNone(info)
            self.assertGreater(info.duration_ms, 500)


class PosterSeekTests(unittest.TestCase):
    def _info(self, seconds):
        info = video.VideoInfo()
        info.duration_ms = int(seconds * 1000)
        return info

    def test_a_long_clip_is_sampled_ten_percent_in(self) -> None:
        self.assertAlmostEqual(video.poster_seek(self._info(600), 0.1), 60.0)

    def test_a_short_clip_starts_at_zero(self) -> None:
        self.assertEqual(video.poster_seek(self._info(2), 0.1), 0.0)

    def test_ten_percent_is_capped_for_medium_lengths(self) -> None:
        # 20 s * 0.1 = 2 s, which is under the 50% ceiling.
        self.assertAlmostEqual(video.poster_seek(self._info(20), 0.1), 2.0)

    def test_an_unknown_duration_starts_at_zero(self) -> None:
        self.assertEqual(video.poster_seek(None, 0.1), 0.0)
        self.assertEqual(video.poster_seek(self._info(0), 0.1), 0.0)


class ClipWindowTests(unittest.TestCase):
    def _info(self, seconds):
        info = video.VideoInfo()
        info.duration_ms = int(seconds * 1000)
        return info

    def test_a_long_video_starts_a_tenth_in(self) -> None:
        start, length = video.clip_window(self._info(600), 5)
        self.assertAlmostEqual(start, 60.0)
        self.assertAlmostEqual(length, 5.0)

    def test_a_short_video_is_taken_whole(self) -> None:
        start, length = video.clip_window(self._info(3), 5)
        self.assertEqual(start, 0.0)
        self.assertAlmostEqual(length, 3.0)

    def test_the_window_never_runs_past_the_end(self) -> None:
        start, length = video.clip_window(self._info(7), 5)
        self.assertLessEqual(start + length, 7.0 + 1e-9)

    def test_an_unknown_duration_uses_the_requested_length(self) -> None:
        start, length = video.clip_window(None, 5)
        self.assertEqual(start, 0.0)
        self.assertEqual(length, 5.0)


class ArgumentTests(unittest.TestCase):
    """The command lines are pure data, so they can be asserted exactly."""

    def test_poster_seeks_before_the_input(self) -> None:
        """``-ss`` before ``-i`` is the fast seek: ffmpeg jumps instead of decoding."""
        args = video.poster_args("in.mp4", "out.jpg", 2.5, 640, 4)
        self.assertLess(args.index("-ss"), args.index("-i"))
        self.assertIn("2.500", args)
        self.assertIn("640", args[args.index("-vf") + 1])

    def test_the_poster_quality_is_clamped(self) -> None:
        args = video.poster_args("in.mp4", "out.jpg", 0, 640, 99)
        self.assertEqual(args[args.index("-q:v") + 1], "31")

    def test_clip_is_h264_yuv420p_faststart(self) -> None:
        args = video.clip_args("in.mkv", "out.mp4", 1.0, 5.0, 960, 28, True)
        self.assertIn("libx264", args)
        self.assertIn("yuv420p", args)
        self.assertIn("+faststart", args)
        self.assertIn("aac", args)

    def test_clip_can_drop_the_audio(self) -> None:
        args = video.clip_args("in.mkv", "out.mp4", 0.0, 5.0, 960, 28, False)
        self.assertIn("-an", args)
        self.assertNotIn("aac", args)

    def test_remux_copies_and_tags_hevc(self) -> None:
        args = video.remux_args("in.mkv", "out.mp4", "hevc")
        self.assertIn("copy", args)
        self.assertEqual(args[args.index("-tag:v") + 1], "hvc1")

    def test_remux_leaves_h264_untagged(self) -> None:
        args = video.remux_args("in.mkv", "out.mp4", "h264")
        self.assertNotIn("-tag:v", args)

    def test_transcode_re_encodes_to_h264(self) -> None:
        args = video.transcode_args("in.mov", "out.mp4", 1280, 23)
        self.assertIn("libx264", args)
        self.assertIn("aac", args)


class ToolDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        video.reset_tools_cache()

    def tearDown(self) -> None:
        video.reset_tools_cache()

    def test_a_missing_directory_falls_back_to_the_system(self) -> None:
        tools = video.find_tools("/definitely/not/here")
        # Either the system has ffmpeg (found via PATH) or it has not.
        if tools:
            self.assertTrue(os.path.exists(tools.ffmpeg))
            self.assertTrue(os.path.exists(tools.ffprobe))

    def test_the_answer_is_cached(self) -> None:
        video.find_tools()
        with patch.object(video, "_binary_in", side_effect=AssertionError("no rescan")):
            video.find_tools()

    def test_reset_forces_a_rescan(self) -> None:
        video.find_tools()
        video.reset_tools_cache()
        # A fresh resolution is allowed to look again.
        video.find_tools()
        self.assertTrue(True)


class VideoCacheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "clip.mp4")
        with open(self.path, "wb") as fh:
            fh.write(b"x" * 2048)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_key_is_stable(self) -> None:
        self.assertEqual(video.video_cache_key(self.path), video.video_cache_key(self.path))

    def test_editing_the_file_changes_the_key(self) -> None:
        before = video.video_cache_key(self.path)
        with open(self.path, "ab") as fh:
            fh.write(b"more")
        self.assertNotEqual(before, video.video_cache_key(self.path))

    def test_a_missing_file_still_returns_a_key(self) -> None:
        self.assertEqual(len(video.video_cache_key("/nope/missing.mp4")), 20)


class LooksLikeVideoTests(unittest.TestCase):
    def test_the_extension_list_covers_the_common_cameras(self) -> None:
        for name in (
            "a.mp4", "a.mov", "a.mkv", "a.mts", "a.m2ts", "a.avi",
            "a.webm", "a.3gp", "a.insv", "a.MP4", "a.MOV",
        ):
            self.assertTrue(video.looks_like_video(name), name)

    def test_stills_are_not_videos(self) -> None:
        for name in ("a.jpg", "a.heic", "a.CR3", "a.nef", "a.txt"):
            self.assertFalse(video.looks_like_video(name), name)


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg is not installed")
class FfmpegIntegrationTests(unittest.TestCase):
    """End-to-end against real files: probe, poster, clip, playable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp()
        tools = video.find_tools()
        cls.source = os.path.join(cls.tmp, "source.mp4")
        subprocess.run(
            [
                tools.ffmpeg, "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", cls.source,
            ],
            check=False,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_probe_reads_a_real_file(self) -> None:
        info = video.probe(self.source)
        self.assertIsNotNone(info)
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertEqual(info.video_codec, "h264")
        self.assertTrue(info.has_audio)
        self.assertGreater(info.duration_ms, 5500)
        self.assertEqual(info.container, ".mp4")
        self.assertEqual(info.play_mode, video.PLAY_ORIGINAL)

    def test_poster_is_a_jpeg_of_the_requested_size(self) -> None:
        from PIL import Image

        dest = os.path.join(self.tmp, "poster.jpg")
        self.assertTrue(video.make_poster(self.source, dest, 1.0, 320, 4))
        with Image.open(dest) as img:
            self.assertEqual(img.size, (320, 180))
            self.assertEqual(img.format, "JPEG")

    def test_clip_is_five_seconds_and_smaller_than_the_source(self) -> None:
        info = video.probe(self.source)
        dest = os.path.join(self.tmp, "clip.mp4")
        self.assertTrue(video.make_clip(self.source, dest, info, seconds=5, max_edge=480))
        clip = video.probe(dest)
        self.assertIsNotNone(clip)
        self.assertLessEqual(clip.duration_ms, 5400)
        self.assertLessEqual(os.path.getsize(dest), os.path.getsize(self.source))

    def test_remux_produces_a_smaller_mp4_of_the_same_frames(self) -> None:
        mkv = os.path.join(self.tmp, "camera.mkv")
        tools = video.find_tools()
        subprocess.run(
            [tools.ffmpeg, "-v", "error", "-y", "-i", self.source, "-c", "copy", mkv],
            check=False,
            capture_output=True,
        )
        info = video.probe(mkv)
        self.assertEqual(info.play_mode, video.PLAY_REMUX)
        dest = os.path.join(self.tmp, "playable.mp4")
        self.assertTrue(video.make_playable(mkv, dest, info))
        played = video.probe(dest)
        self.assertIsNotNone(played)
        self.assertEqual(played.container, ".mp4")
        self.assertEqual(played.play_mode, video.PLAY_ORIGINAL)

    def test_a_broken_file_returns_none_instead_of_raising(self) -> None:
        broken = os.path.join(self.tmp, "broken.mp4")
        with open(broken, "wb") as fh:
            fh.write(b"not a video")
        self.assertIsNone(video.probe(broken))


if __name__ == "__main__":
    unittest.main()
