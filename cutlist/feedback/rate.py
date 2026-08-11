from cutlist.db.store import MARKS, RatingError


def parse_segment_marks(text: str) -> list[tuple[int, str]]:
    """Parse `"1:good,3:veto"` into 1-based (position, mark) pairs.

    Positions are 1-based because that is what the review page prints under
    each segment, and typing what you can see beats an off-by-one.
    """
    pairs: list[tuple[int, str]] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise RatingError(f"empty entry in --segments: {text!r}")
        position, _, mark = chunk.partition(":")
        position, mark = position.strip(), mark.strip()
        if not position.isdigit():
            raise RatingError(f"segment position must be a number, got {position!r}")
        if mark not in MARKS:
            raise RatingError(f"mark must be one of {', '.join(MARKS)}, got {mark!r}")
        pairs.append((int(position), mark))
    return pairs
