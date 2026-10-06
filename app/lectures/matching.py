import html
import re
from dataclasses import dataclass


def normalize_group(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value).casefold()).strip()


def index_module_names(names: list[str]) -> dict[str, str]:
    """Normalized name -> the first source spelling, built once per batch."""
    by_normalized: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not normalize_group(name):
            continue                    # a NULL or blank title names nothing
        by_normalized.setdefault(normalize_group(name), name.strip())
    return by_normalized


def match_active_group(subject: str, active_groups: list[str]) -> str | None:
    return index_module_names(active_groups).get(normalize_group(subject))


APTEM = "aptem"
LMS = "lms"
BOTH = "both"
NONE = "none"


@dataclass(frozen=True)
class ModuleMatch:
    """Which reference source(s) know this Teams subject. Eligible if either."""
    module: str | None
    matched_in_aptem: bool
    matched_in_lms: bool

    @property
    def eligible(self) -> bool:
        return self.matched_in_aptem or self.matched_in_lms

    @property
    def matched_source(self) -> str:
        if self.matched_in_aptem and self.matched_in_lms:
            return BOTH
        return APTEM if self.matched_in_aptem else LMS if self.matched_in_lms else NONE


def match_module(subject: str, aptem_index: dict[str, str],
                 lms_index: dict[str, str]) -> ModuleMatch:
    """
    The SAME exact-after-normalization rule against each source. When both
    know the subject the Aptem spelling is kept, so a lecture Aptem already
    matched keeps exactly the module it had.
    """
    wanted = normalize_group(subject)
    aptem, lms = aptem_index.get(wanted), lms_index.get(wanted)
    # LMS titles can carry HTML entities ("Strategy &amp; Planning"); the
    # stored module is the readable form.
    module = aptem if aptem is not None else (html.unescape(lms) if lms is not None else None)
    return ModuleMatch(module=module, matched_in_aptem=aptem is not None,
                       matched_in_lms=lms is not None)
