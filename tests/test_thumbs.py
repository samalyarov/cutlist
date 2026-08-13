import io

from PIL import Image

from cutlist.media.thumbs import thumbnail_bytes


def test_thumbnail_bytes_writes_a_jpeg(fixture_video):
    image = thumbnail_bytes(fixture_video, 7.5)
    assert len(image) > 0
    assert image[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_thumbnail_bytes_honours_width(fixture_video):
    image = Image.open(io.BytesIO(thumbnail_bytes(fixture_video, 2.0, width=80)))
    assert image.width == 80


def test_thumbnails_from_different_shots_differ(fixture_video):
    # fixture_video cuts every 5s between flat colours, so 2.5s and 7.5s are
    # different colours and must not produce identical bytes.
    first = thumbnail_bytes(fixture_video, 2.5)
    second = thumbnail_bytes(fixture_video, 7.5)
    assert first != second
