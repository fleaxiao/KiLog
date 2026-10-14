"""Natural reference ordering for the Trace component grid."""
import re


def reference_sort_key(reference: str) -> list[tuple[int, int | str]]:
    """Sort case-insensitively, comparing reference numbers as integers."""
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", reference)
    ]
