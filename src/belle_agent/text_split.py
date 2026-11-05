"""Split a long message into the parts a platform accepts, at natural boundaries."""

from collections.abc import Callable


def split_index(content: str, limit: int) -> int:
    """Where to cut ``content`` so that its first part fits ``limit``.

    The cut is the last paragraph, line or word break in the second half of the limit, or the limit itself.
    """
    lower_bound = max(1, limit // 2)
    for separator in ("\n\n", "\n", " "):
        split_at = content.rfind(separator, lower_bound, limit + 1)
        if split_at >= 0:
            return split_at
    return limit


def split_text(
    content: str, limit: int, first_limit: int = 0, *, cut: Callable[[str, int], int] = split_index,
    preserve_whitespace: bool = False,
) -> list[str]:
    """Split text into parts of at most ``limit`` characters, the first of at most ``first_limit`` where it is set.

    ``cut`` chooses each cut. By default, whitespace at the cut is removed.
    """
    remaining = content if preserve_whitespace else content.strip()
    chunks: list[str] = []
    while True:
        part_limit = first_limit if first_limit and not chunks else limit
        if len(remaining) <= part_limit:
            break
        split_at = cut(remaining, part_limit)
        if preserve_whitespace and not remaining[split_at:split_at + limit].strip():
            split_at = part_limit
        chunks.append(remaining[:split_at] if preserve_whitespace else remaining[:split_at].rstrip())
        remaining = remaining[split_at:] if preserve_whitespace else remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
