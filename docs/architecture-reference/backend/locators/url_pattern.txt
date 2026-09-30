"""Helpers for matching correction records against dynamic URLs."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlparse


UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def generalize_url(url: str) -> str:
    parsed = urlparse(url)
    path_segments = parsed.path.split("/")
    generalized_segments = ["*" if _is_dynamic_segment(segment) else segment for segment in path_segments]
    generalized_path = "/".join(generalized_segments)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if query_pairs:
        query_pairs = sorted(query_pairs, key=lambda item: (item[0], item[1]))
        generalized_query = "&".join(
            f"{quote(key, safe='')}={quote('*' if _is_dynamic_segment(value) else value, safe='*')}"
            for key, value in query_pairs
        )
        return f"{parsed.scheme}://{parsed.netloc}{generalized_path}?{generalized_query}"
    return f"{parsed.scheme}://{parsed.netloc}{generalized_path}"


def _is_dynamic_segment(segment: str) -> bool:
    if not segment:
        return False
    if segment.isdigit():
        return True
    if UUID_PATTERN.match(segment):
        return True
    if len(segment) >= 16 and segment.isalnum() and any(char.isdigit() for char in segment) and any(
        char.isalpha() for char in segment
    ):
        return True
    return False
