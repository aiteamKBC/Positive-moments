import html
import re


def normalize_group(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value).casefold()).strip()


def match_active_group(subject: str, active_groups: list[str]) -> str | None:
    wanted = normalize_group(subject)
    by_normalized: dict[str, str] = {}
    for group in active_groups:
        by_normalized.setdefault(normalize_group(group), group.strip())
    return by_normalized.get(wanted)

