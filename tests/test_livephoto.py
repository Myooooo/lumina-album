"""Unit tests for motion photo / Live Photo detection.

Every fixture is built by hand in a temporary directory: a JPEG with an XMP
packet and an MP4 appended, exactly the way the specification describes. No
ffmpeg is involved, so these run everywhere.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest

from PIL import Image

from photo_reviewer import livephoto


def _mp4_bytes(payload_len: int = 2048) -> bytes:
    """A minimal but structurally valid MP4: ftyp, free, mdat chained to the end."""
    ftyp = struct.pack(">I", 24) + b"ftypisom" + b"\x00\x00\x02\x00isomiso2"
    free = struct.pack(">I", 8) + b"free"
    mdat_body = b"\xab" * payload_len
    mdat = struct.pack(">I", 8 + len(mdat_body)) + b"mdat" + mdat_body
    return ftyp + free + mdat


def _xmp(attrs: str) -> bytes:
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f"<rdf:Description {attrs}</rdf:Description>"
        "</rdf:RDF></x:xmpmeta>"
    ).encode()


CAMERA_NS = 'xmlns:Camera="http://ns.google.com/photos/1.0/camera/"'
CONTAINER_NS = (
    'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
    'xmlns:Item="http://ns.google.com/photos/1.0/container/item/"'
)


def _container_xmp(video_length: int, primary_padding: int = 0) -> bytes:
    return _xmp(
        f"{CAMERA_NS} {CONTAINER_NS} "
        'Camera:MotionPhoto="1" Camera:MotionPhotoVersion="1" '
        'Camera:MotionPhotoPresentationTimestampUs="-1">'
        "<Container:Directory><rdf:Seq>"
        '<rdf:li rdf:parseType="Resource"><Container:Item '
        'Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Length="0"'
        f' Item:Padding="{primary_padding}"/></rdf:li>'
        '<rdf:li rdf:parseType="Resource"><Container:Item '
        f'Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="{video_length}"/>'
        "</rdf:li></rdf:Seq></Container:Directory>"
        "</rdf:Description>"
    )


def _micro_xmp(offset: int) -> bytes:
    return _xmp(
        f'{CAMERA_NS} Camera:MicroVideo="1" Camera:MicroVideoVersion="1" '
        f'Camera:MicroVideoOffset="{offset}" Camera:MicroVideoPresentationTimestampUs="-1"'
    )


def _plain_motion_xmp() -> bytes:
    """Samsung style: says a video exists, does not say how long it is."""
    return _xmp(f'{CAMERA_NS} Camera:MotionPhoto="1" Camera:MotionPhotoVersion="1"')


def _app1(packet: bytes) -> bytes:
    body = b"http://ns.adobe.com/xap/1.0/\x00" + packet
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def build_still(path: str, xmp: bytes | None, video: bytes | None, name_ext: str = ".jpg") -> str:
    image = os.path.join(os.path.dirname(path), "tmp_still" + name_ext)
    Image.new("RGB", (160, 120), (140, 110, 80)).save(image, "JPEG")
    with open(image, "rb") as fh:
        jpeg = fh.read()
    if xmp is not None:
        jpeg = jpeg[:2] + _app1(xmp) + jpeg[2:]
    with open(path, "wb") as fh:
        fh.write(jpeg)
        if video is not None:
            fh.write(video)
    if image != path:
        os.remove(image)
    return path


class MotionPhotoDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.video = _mp4_bytes()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def test_container_directory_with_a_length(self) -> None:
        """Motion Photo 1.0: the length is taken from the end of the file."""
        path = build_still(
            self._path("IMG_0001MP.jpg"),
            _container_xmp(len(self.video)),
            self.video,
        )
        source = livephoto.detect_motion_photo(path)
        self.assertIsNotNone(source)
        self.assertEqual(source.kind, "motion-photo")
        self.assertTrue(source.embedded)
        self.assertEqual(source.length, len(self.video))
        self.assertEqual(source.start, os.path.getsize(path) - len(self.video))

    def test_legacy_micro_video_offset(self) -> None:
        """Micro Video v1: the offset is measured back from the end."""
        path = build_still(
            self._path("IMG_0002.jpg"),
            _micro_xmp(len(self.video)),
            self.video,
        )
        source = livephoto.detect_motion_photo(path)
        self.assertIsNotNone(source)
        self.assertEqual(source.kind, "micro-video")
        self.assertEqual(source.length, len(self.video))

    def test_a_trailer_with_no_recorded_length(self) -> None:
        """Samsung writes the flag but not the size; the bytes have to say."""
        path = build_still(
            self._path("IMG_0003MP.jpg"),
            _plain_motion_xmp(),
            self.video,
        )
        source = livephoto.detect_motion_photo(path)
        self.assertIsNotNone(source)
        self.assertEqual(source.length, len(self.video))
        self.assertEqual(source.start, os.path.getsize(path) - len(self.video))

    def test_the_mp_filename_convention_alone_is_enough(self) -> None:
        """Some builds append the video without any XMP at all."""
        path = build_still(self._path("VID_0004MP.jpg"), None, self.video)
        source = livephoto.detect_motion_photo(path)
        self.assertIsNotNone(source)
        self.assertEqual(source.kind, "trailer")

    def test_a_plain_photo_is_not_a_motion_photo(self) -> None:
        path = self._path("plain.jpg")
        Image.new("RGB", (160, 120), (10, 20, 30)).save(path, "JPEG")
        self.assertIsNone(livephoto.detect_motion_photo(path))

    def test_leftover_xmp_without_a_video_is_ignored(self) -> None:
        """Editors keep the metadata but strip the movie; the spec says to check."""
        path = build_still(
            self._path("stripped.jpg"),
            _container_xmp(4096),
            None,
        )
        self.assertIsNone(livephoto.detect_motion_photo(path))

    def test_a_stray_ftyp_string_inside_the_jpeg_is_not_a_video(self) -> None:
        """A thumbnail can contain the letters "ftyp"; the box chain must not hold."""
        junk = struct.pack(">I", 9_000_000) + b"ftyp" + b"\x00" * 32
        path = build_still(self._path("bogusMP.jpg"), None, junk)
        self.assertIsNone(livephoto.detect_motion_photo(path))

    def test_a_non_still_extension_is_skipped(self) -> None:
        path = self._path("clip.mp4")
        with open(path, "wb") as fh:
            fh.write(self.video)
        self.assertIsNone(livephoto.detect_motion_photo(path))

    def test_a_truncated_file_is_skipped(self) -> None:
        path = self._path("tiny.jpg")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8\xff")
        self.assertIsNone(livephoto.detect_motion_photo(path))


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.video = _mp4_bytes(4096)
        self.path = build_still(
            os.path.join(self.tmp.name, "IMG_9MP.jpg"),
            _container_xmp(len(self.video)),
            self.video,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_sliced_bytes_are_the_movie(self) -> None:
        source = livephoto.detect_motion_photo(self.path)
        dest = os.path.join(self.tmp.name, "motion.mp4")
        self.assertTrue(livephoto.extract_embedded(self.path, source, dest))
        with open(dest, "rb") as fh:
            extracted = fh.read()
        self.assertEqual(extracted, self.video)
        self.assertEqual(extracted[4:8], b"ftyp")

    def test_an_unwritable_destination_reports_failure(self) -> None:
        source = livephoto.detect_motion_photo(self.path)
        dest = os.path.join(self.tmp.name, "no_such_dir", "x", "motion.mp4")
        self.assertFalse(livephoto.extract_embedded(self.path, source, dest))

    def test_a_paired_source_is_not_embedded(self) -> None:
        source = livephoto.MotionSource("paired", video_path="x.mov")
        self.assertFalse(source.embedded)
        self.assertFalse(
            livephoto.extract_embedded(self.path, source, os.path.join(self.tmp.name, "o.mp4"))
        )


class LivePhotoPairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _still(self, name: str) -> str:
        path = os.path.join(self.dir, name)
        Image.new("RGB", (80, 60), (100, 120, 140)).save(path, "JPEG")
        return path

    def test_a_same_stem_mov_is_found(self) -> None:
        still = self._still("IMG_1234.JPG")
        movie = os.path.join(self.dir, "IMG_1234.MOV")
        with open(movie, "wb") as fh:
            fh.write(b"\x00" * 64)
        self.assertEqual(livephoto.paired_video_path(still), movie)

    def test_the_match_is_case_insensitive(self) -> None:
        still = self._still("IMG_2000.JPG")
        movie = os.path.join(self.dir, "img_2000.mov")
        with open(movie, "wb") as fh:
            fh.write(b"\x00" * 64)
        self.assertEqual(livephoto.paired_video_path(still), movie)

    def test_a_different_stem_is_not_a_pair(self) -> None:
        still = self._still("IMG_3000.JPG")
        with open(os.path.join(self.dir, "IMG_3001.MOV"), "wb") as fh:
            fh.write(b"\x00" * 64)
        self.assertIsNone(livephoto.paired_video_path(still))

    def test_a_non_video_sidecar_is_ignored(self) -> None:
        still = self._still("IMG_4000.JPG")
        with open(os.path.join(self.dir, "IMG_4000.AAE"), "wb") as fh:
            fh.write(b"<plist/>")
        self.assertIsNone(livephoto.paired_video_path(still))

    def test_detect_falls_back_to_the_pair(self) -> None:
        still = self._still("IMG_5000.JPG")
        movie = os.path.join(self.dir, "IMG_5000.MOV")
        with open(movie, "wb") as fh:
            fh.write(b"\x00" * 64)
        source = livephoto.detect(still)
        self.assertIsNotNone(source)
        self.assertEqual(source.kind, "paired")
        self.assertEqual(source.video_path, movie)

    def test_an_embedded_video_wins_over_a_pair(self) -> None:
        video = _mp4_bytes()
        still = build_still(
            os.path.join(self.dir, "IMG_6000MP.jpg"),
            _container_xmp(len(video)),
            video,
        )
        with open(os.path.join(self.dir, "IMG_6000.MOV"), "wb") as fh:
            fh.write(b"\x00" * 64)
        source = livephoto.detect(still)
        self.assertEqual(source.kind, "motion-photo")


class XmpParsingTests(unittest.TestCase):
    def test_container_items_are_read_in_order(self) -> None:
        xmp = _container_xmp(5000).decode("utf-8")
        items = livephoto.parse_container_items(xmp)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["Item:Semantic"], "Primary")
        self.assertEqual(items[1]["Item:Mime"], "video/mp4")
        self.assertEqual(items[1]["Item:Length"], "5000")

    def test_a_padding_attribute_is_kept(self) -> None:
        xmp = _container_xmp(5000, primary_padding=8).decode("utf-8")
        items = livephoto.parse_container_items(xmp)
        self.assertEqual(items[0]["Item:Padding"], "8")

    def test_reading_an_xmp_packet_from_a_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = build_still(
                os.path.join(tmp, "IMG_1MP.jpg"), _container_xmp(100), _mp4_bytes()
            )
            xmp = livephoto.read_xmp(path)
            self.assertIn("Container:Directory", xmp)
            self.assertIn("Camera:MotionPhoto", xmp)

    def test_a_photo_without_xmp_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.jpg")
            Image.new("RGB", (40, 30), (1, 2, 3)).save(path, "JPEG")
            self.assertEqual(livephoto.read_xmp(path), "")


class BoxChainTests(unittest.TestCase):
    def test_a_well_formed_chain_runs_to_the_end(self) -> None:
        data = _mp4_bytes()
        self.assertEqual(livephoto._box_chain_length(data, 0), len(data))

    def test_a_chain_that_overruns_is_rejected(self) -> None:
        data = struct.pack(">I", 999) + b"ftyp" + b"\x00" * 16
        self.assertIsNone(livephoto._box_chain_length(data, 0))

    def test_a_non_printable_type_is_rejected(self) -> None:
        data = struct.pack(">I", 16) + b"\x00\x01\x02\x03" + b"\x00" * 8
        self.assertIsNone(livephoto._box_chain_length(data, 0))

    def test_a_size_below_eight_is_rejected(self) -> None:
        data = struct.pack(">I", 4) + b"ftyp" + b"\x00" * 16
        self.assertIsNone(livephoto._box_chain_length(data, 0))

    def test_a_size_of_zero_means_to_end_of_file(self) -> None:
        data = struct.pack(">I", 0) + b"mdat" + b"\x00" * 32
        self.assertEqual(livephoto._box_chain_length(data, 0), len(data))


if __name__ == "__main__":
    unittest.main()
