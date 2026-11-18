"""Conservative keys for normalized Windows paths, without Unicode expansion."""
import os

_ASCII_CASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def path_key(value: str) -> str:
    """Fold ASCII casing only; Unicode casefold can merge distinct NTFS names.

    Callers validate and resolve existing paths to physical casing first. An
    unresolved Unicode casing variant therefore fails closed instead of gaining
    permission through a language-level equivalence (for example ß → ss).
    """
    normalized = value.replace("\\", "/")
    return normalized.translate(_ASCII_CASE) if os.name == "nt" else normalized


def same_path(left, right) -> bool:
    return left is not None and right is not None and path_key(str(left)) == path_key(str(right))


def path_within(path, root) -> bool:
    """Compare already validated/canonical absolute paths without Unicode aliases."""
    candidate, parent = path_key(str(path)), path_key(str(root)).rstrip("/")
    return candidate == parent or candidate.startswith(parent + "/")
