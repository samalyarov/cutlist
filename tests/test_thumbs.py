from cutlist.media.thumbs import thumbnail


def test_thumbnail_writes_a_jpeg(fixture_film, tmp_path):
    dest = thumbnail(fixture_film, 7.5, tmp_path / "a.jpg")
    assert dest.exists()
    assert dest.stat().st_size > 0
    assert dest.read_bytes()[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_thumbnail_creates_missing_parent_directories(fixture_film, tmp_path):
    dest = thumbnail(fixture_film, 2.0, tmp_path / "nested" / "deep" / "a.jpg")
    assert dest.exists()


def test_thumbnail_is_cached_and_not_regenerated(fixture_film, tmp_path):
    dest = thumbnail(fixture_film, 2.0, tmp_path / "a.jpg")
    marker = b"not-a-real-jpeg"
    dest.write_bytes(marker)
    again = thumbnail(fixture_film, 2.0, tmp_path / "a.jpg")
    assert again.read_bytes() == marker


def test_thumbnails_from_different_shots_differ(fixture_film, tmp_path):
    # fixture_film cuts every 5s between flat colours, so 2.5s and 7.5s are
    # different colours and must not produce identical bytes.
    first = thumbnail(fixture_film, 2.5, tmp_path / "a.jpg").read_bytes()
    second = thumbnail(fixture_film, 7.5, tmp_path / "b.jpg").read_bytes()
    assert first != second
