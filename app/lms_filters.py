"""Filters for bulk LMS material downloads.

Pure Python (no Playwright, no browser), so it can be unit-tested on its own.
``lms_tools`` collects the course items and calls into this module to decide
which of them match what the user asked for ("last two weeks", "only labs",
"DV and OS", "pdf slides", ...).

An *item* is a dict with these keys (built by ``lms_tools._collect_materials``):
    name, url, cmid, kind ("file" | "folder"), section_name, section_number,
    type_hint, date (datetime | None), date_source (str), section_range
    ((datetime, datetime) | None)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

# ----------------------------------------------------------------------
# Date parsing
# ----------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DASH_RE = r"(?:-|\u2013|\u2014|to|until)"


def _month_number(text: str) -> int:
    return _MONTHS[text[:3].lower()]


def _make_datetime(year: int, month: int, day: int, hour: int = 0, minute: int = 0):
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _read_time(text: str, start: int) -> tuple[int, int]:
    """Reads an optional '10:15', '10:15 PM' or ', 22:05' that follows a date."""
    match = re.match(
        r"[\s,@-]*(?:at\s+)?(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]m)?",
        text[start:],
        re.I,
    )
    if not match:
        return 0, 0
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return 0, 0
    return hour, minute


def _yearless(day: int, month: int, hour: int, minute: int, now: datetime):
    """A date with no year: assume this year, or last year if that is in the future."""
    candidate = _make_datetime(now.year, month, day, hour, minute)
    if candidate is None:
        return None
    if candidate > now + timedelta(days=7):
        candidate = _make_datetime(now.year - 1, month, day, hour, minute)
    return candidate


def parse_datetime_text(text: str, now: datetime):
    """Finds the first date in free text such as 'Uploaded 21/10/26, 10:15'.

    Understands: 2026-10-21, 21/10/26 (day first, Moodle's default), 21 October
    2026, Wednesday, 21 October 2026, 10:15 AM, Oct 21, 2026, today, yesterday.
    Returns a naive local datetime or None.
    """
    if not text:
        return None

    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        hour, minute = _read_time(text, iso.end())
        return _make_datetime(
            int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), hour, minute
        )

    numeric = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b", text)
    if numeric:
        year = int(numeric.group(3))
        if year < 100:
            year += 2000
        hour, minute = _read_time(text, numeric.end())
        return _make_datetime(
            year, int(numeric.group(2)), int(numeric.group(1)), hour, minute
        )

    day_first = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}\b[,.]?(?:\s+(\d{{4}}))?",
        text,
        re.I,
    )
    if day_first:
        hour, minute = _read_time(text, day_first.end())
        day, month = int(day_first.group(1)), _month_number(day_first.group(2))
        if day_first.group(3):
            return _make_datetime(int(day_first.group(3)), month, day, hour, minute)
        return _yearless(day, month, hour, minute, now)

    month_first = re.search(
        rf"\b{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?",
        text,
        re.I,
    )
    if month_first:
        hour, minute = _read_time(text, month_first.end())
        month, day = _month_number(month_first.group(1)), int(month_first.group(2))
        if month_first.group(3):
            return _make_datetime(int(month_first.group(3)), month, day, hour, minute)
        return _yearless(day, month, hour, minute, now)

    lowered = text.lower()
    for word, offset in (("yesterday", 1), ("today", 0)):
        if word in lowered:
            day = (now - timedelta(days=offset)).date()
            index = lowered.index(word) + len(word)
            hour, minute = _read_time(text, index)
            return datetime.combine(day, time(hour, minute))
    return None


def parse_section_range(name: str, now: datetime):
    """Reads a week range out of a section title.

    '21 October - 27 October', '21 Oct - 27 Oct 2026', '21 - 27 October',
    'Week 5 (14 Oct - 20 Oct)'. Returns (start, end) datetimes or None.
    """
    if not name:
        return None

    full = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}\b\.?(?:\s+(\d{{4}}))?"
        rf"\s*{_DASH_RE}\s*"
        rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}\b\.?(?:\s+(\d{{4}}))?",
        name,
        re.I,
    )
    short = re.search(
        rf"\b(\d{{1,2}})\s*{_DASH_RE}\s*(\d{{1,2}})\s+{_MONTH_RE}\b\.?(?:\s+(\d{{4}}))?",
        name,
        re.I,
    )
    if full:
        d1, m1, y1, d2, m2, y2 = full.groups()
    elif short:
        d1, d2, m1, y1 = short.groups()
        m2, y2 = m1, y1
    else:
        return None

    end_year = int(y2) if y2 else (int(y1) if y1 else now.year)
    start_year = int(y1) if y1 else end_year
    start = _make_datetime(start_year, _month_number(m1), int(d1))
    end = _make_datetime(end_year, _month_number(m2), int(d2), 23, 59)
    if start is None or end is None:
        return None
    if start > end and not y1:
        start = _make_datetime(start.year - 1, start.month, start.day)
        if start is None:
            return None
    if not (y1 or y2) and end > now + timedelta(days=120):
        start = _make_datetime(start.year - 1, start.month, start.day)
        end = _make_datetime(end.year - 1, end.month, end.day, 23, 59)
        if start is None or end is None:
            return None
    return start, end


def resolve_window(
    now: datetime,
    last_days: int = 0,
    last_weeks: int = 0,
    since: str = "",
    until: str = "",
    period: str = "",
):
    """Turns the date arguments into (start, end); either may be None (open)."""
    starts, ends = [], []
    today = datetime.combine(now.date(), time.min)

    if last_days or last_weeks:
        days = int(last_days or 0) + 7 * int(last_weeks or 0)
        starts.append(today - timedelta(days=days))

    period = (period or "").strip().lower().replace("-", "_").replace(" ", "_")
    if period:
        monday = today - timedelta(days=today.weekday())
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - timedelta(seconds=1)
        presets = {
            "today": (today, None),
            "yesterday": (today - timedelta(days=1), today - timedelta(seconds=1)),
            "this_week": (monday, None),
            "last_week": (monday - timedelta(days=7), monday - timedelta(seconds=1)),
            "this_month": (first_of_month, None),
            "last_month": (
                datetime.combine(last_month_end.date().replace(day=1), time.min),
                last_month_end,
            ),
        }
        if period not in presets:
            raise ValueError(
                f'Unknown period "{period}". Use one of: '
                + ", ".join(sorted(presets))
                + "."
            )
        preset_start, preset_end = presets[period]
        starts.append(preset_start)
        if preset_end is not None:
            ends.append(preset_end)

    if since and since.strip():
        parsed = parse_datetime_text(since.strip(), now)
        if parsed is None:
            raise ValueError(f'Could not read since="{since}". Use YYYY-MM-DD.')
        starts.append(datetime.combine(parsed.date(), time.min))
    if until and until.strip():
        parsed = parse_datetime_text(until.strip(), now)
        if parsed is None:
            raise ValueError(f'Could not read until="{until}". Use YYYY-MM-DD.')
        ends.append(datetime.combine(parsed.date(), time(23, 59, 59)))

    start = max(starts) if starts else None
    end = min(ends) if ends else None
    if start and end and start > end:
        raise ValueError("The date filter is empty: the start is after the end.")
    return start, end


# ----------------------------------------------------------------------
# Filter specification
# ----------------------------------------------------------------------
_CATEGORY_ALIASES = {
    "lecture": "lecture", "lectures": "lecture", "slides": "lecture",
    "slide": "lecture", "material": "lecture", "materials": "lecture",
    "notes": "lecture", "other": "lecture",
    "lab": "lab", "labs": "lab",
    "assignment": "assignment", "assignments": "assignment", "assign": "assignment",
}
_ALL_WORDS = {"all", "any", "everything", "*", "all courses", "every course"}

_PPT = {"ppt", "pptx", "pps", "ppsx", "odp", "key"}
_DOC = {"doc", "docx", "odt", "rtf"}
_XLS = {"xls", "xlsx", "xlsm", "csv", "ods"}
_ZIP = {"zip", "rar", "7z", "tar", "gz"}
_IMG = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
_VID = {"mp4", "mkv", "webm", "mov", "avi"}
_TXT = {"txt", "md"}

_TYPE_ALIASES = {
    "pdf": {"pdf"}, "pdfs": {"pdf"},
    "slides": _PPT, "slide": _PPT, "powerpoint": _PPT, "presentation": _PPT,
    "presentations": _PPT, "ppt": _PPT, "pptx": _PPT,
    "word": _DOC, "doc": _DOC, "docx": _DOC, "document": _DOC, "documents": _DOC,
    "excel": _XLS, "spreadsheet": _XLS, "spreadsheets": _XLS, "xls": _XLS,
    "xlsx": _XLS, "csv": _XLS,
    "zip": _ZIP, "archive": _ZIP, "archives": _ZIP, "rar": _ZIP,
    "image": _IMG, "images": _IMG, "picture": _IMG, "pictures": _IMG,
    "video": _VID, "videos": _VID,
    "text": _TXT, "txt": _TXT,
}

# words that show up in the LMS file-type label / icon name -> extensions
_HINT_WORDS = [
    (("pdf",), {"pdf"}),
    (("powerpoint", "presentation", "pptx"), _PPT),
    (("word", "docx", "document"), _DOC),
    (("excel", "spreadsheet", "xlsx"), _XLS),
    (("archive", "zip"), _ZIP),
    (("image", "jpeg", "png"), _IMG),
    (("video", "mpeg", "mp4"), _VID),
]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    parts: list[str] = []
    for item in items:
        parts.extend(re.split(r"[,;\n]", str(item)))
    return [part.strip() for part in parts if part.strip()]


@dataclass
class FilterSpec:
    courses: list[str] = field(default_factory=list)
    all_courses: bool = True
    start: datetime | None = None
    end: datetime | None = None
    name_contains: list[str] = field(default_factory=list)
    name_excludes: list[str] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)
    sections: list[str] = field(default_factory=list)
    last_sections: int = 0
    file_exts: set[str] = field(default_factory=set)
    include_undated: bool = False
    include_folders: bool = True
    skip_downloaded: bool = True
    probe_dates: bool = True
    limit: int = 0

    @property
    def has_window(self) -> bool:
        return self.start is not None or self.end is not None

    def describe(self) -> str:
        parts = [
            "courses: all"
            if self.all_courses
            else "courses: " + ", ".join(self.courses)
        ]
        if self.has_window:
            fmt = "%Y-%m-%d %H:%M"
            parts.append(
                "dates: "
                + (self.start.strftime(fmt) if self.start else "any")
                + " -> "
                + (self.end.strftime(fmt) if self.end else "now")
            )
        if self.categories:
            parts.append("categories: " + ", ".join(sorted(self.categories)))
        if self.name_contains:
            parts.append("name contains: " + " | ".join(self.name_contains))
        if self.name_excludes:
            parts.append("name excludes: " + " | ".join(self.name_excludes))
        if self.sections:
            parts.append("sections: " + " | ".join(self.sections))
        if self.last_sections:
            parts.append(f"newest {self.last_sections} section(s) per course")
        if self.file_exts:
            parts.append("file types: " + ", ".join(sorted(self.file_exts)))
        if self.limit:
            parts.append(f"max {self.limit} file(s)")
        return "; ".join(parts)


def build_spec(
    now: datetime,
    courses=None,
    period: str = "",
    last_days: int = 0,
    last_weeks: int = 0,
    since: str = "",
    until: str = "",
    name_contains=None,
    name_excludes=None,
    categories=None,
    sections=None,
    last_sections: int = 0,
    file_types=None,
    include_undated: bool = False,
    include_folders: bool = True,
    skip_already_downloaded: bool = True,
    probe_dates: bool = True,
    limit: int = 0,
) -> FilterSpec:
    for label, number in (
        ("last_days", last_days),
        ("last_weeks", last_weeks),
        ("last_sections", last_sections),
        ("limit", limit),
    ):
        if int(number or 0) < 0:
            raise ValueError(f"{label} cannot be negative.")

    course_list = _as_list(courses)
    all_courses = not course_list or any(
        course.lower() in _ALL_WORDS for course in course_list
    )
    if all_courses:
        course_list = []
    for course in course_list:
        if len(course) > 120 or not re.search(r"[a-z0-9]", course, re.I):
            raise ValueError(f'"{course}" is not a valid course name.')

    category_set: set[str] = set()
    for word in _as_list(categories):
        key = word.lower()
        if key in _ALL_WORDS:
            category_set.clear()
            break
        if key not in _CATEGORY_ALIASES:
            raise ValueError(
                f'Unknown category "{word}". Use lectures, labs or assignments.'
            )
        category_set.add(_CATEGORY_ALIASES[key])

    ext_set: set[str] = set()
    for word in _as_list(file_types):
        key = word.lower().lstrip(".")
        if key in _ALL_WORDS:
            ext_set.clear()
            break
        if key in _TYPE_ALIASES:
            ext_set |= _TYPE_ALIASES[key]
        elif re.fullmatch(r"[a-z0-9]{1,6}", key):
            ext_set.add(key)
        else:
            raise ValueError(f'Unknown file type "{word}".')

    start, end = resolve_window(
        now,
        last_days=int(last_days or 0),
        last_weeks=int(last_weeks or 0),
        since=since or "",
        until=until or "",
        period=period or "",
    )
    return FilterSpec(
        courses=course_list,
        all_courses=all_courses,
        start=start,
        end=end,
        name_contains=[word.lower() for word in _as_list(name_contains)],
        name_excludes=[word.lower() for word in _as_list(name_excludes)],
        categories=category_set,
        sections=[word.lower() for word in _as_list(sections)],
        last_sections=int(last_sections or 0),
        file_exts=ext_set,
        include_undated=bool(include_undated),
        include_folders=bool(include_folders),
        skip_downloaded=bool(skip_already_downloaded),
        probe_dates=bool(probe_dates),
        limit=int(limit or 0),
    )


# ----------------------------------------------------------------------
# Applying the filters
# ----------------------------------------------------------------------
def guess_extensions(item: dict):
    """Best guess of the file's extension(s) before downloading, or None."""
    name = item["name"].lower()
    match = re.search(r"\.([a-z0-9]{2,5})$", name)
    if match:
        return {match.group(1)}
    hint = (item.get("type_hint") or "").lower()
    found: set[str] = set()
    for words, extensions in _HINT_WORDS:
        if any(word in hint for word in words):
            found |= extensions
    return found or None


def type_matches(item: dict, spec: FilterSpec):
    """True / False when the type is known, None when it cannot be told yet."""
    if not spec.file_exts:
        return True
    guess = guess_extensions(item)
    if guess is None:
        return None
    return bool(guess & spec.file_exts)


def static_filter(items: list[dict], spec: FilterSpec, classify) -> list[dict]:
    """Everything except dates: folders, keywords, category, section, file type.

    ``classify`` is lms_tools._classify_activity: name -> (category, label).
    """
    kept = []
    for item in items:
        name = item["name"].lower()
        if item["kind"] == "folder" and not spec.include_folders:
            continue
        if spec.name_contains and not any(word in name for word in spec.name_contains):
            continue
        if any(word in name for word in spec.name_excludes):
            continue
        if spec.sections:
            section = (item.get("section_name") or "").lower()
            if not any(word in section for word in spec.sections):
                continue
        if spec.categories:
            category = classify(item["name"])[0] or "lecture"
            if category not in spec.categories:
                continue
        if item["kind"] == "file" and type_matches(item, spec) is False:
            continue
        kept.append(item)
    return kept


def latest_sections(items: list[dict], count: int) -> list[dict]:
    """Keeps only items in the newest ``count`` sections that have any item."""
    if not count:
        return items
    numbers = sorted(
        {
            item["section_number"]
            for item in items
            if item.get("section_number") is not None
        },
        reverse=True,
    )
    keep = set(numbers[:count])
    return [item for item in items if item.get("section_number") in keep]


def date_status(item: dict, spec: FilterSpec):
    """True / False if the item's date is known, None if it cannot be told.

    An exact date (from the page or the file) wins; otherwise the week range in
    the section title is used (any overlap with the window counts).
    """
    start, end = spec.start, spec.end
    when = item.get("date")
    if when is not None:
        return (start is None or when >= start) and (end is None or when <= end)
    section_range = item.get("section_range")
    if section_range:
        range_start, range_end = section_range
        return (end is None or range_start <= end) and (
            start is None or range_end >= start
        )
    return None


def describe_item(item: dict) -> str:
    """One line for the preview listing."""
    when = item.get("date")
    if when is not None:
        date_text = f'{when:%Y-%m-%d %H:%M} ({item.get("date_source") or "page"})'
    elif item.get("section_range"):
        range_start, range_end = item["section_range"]
        date_text = f"week {range_start:%d %b} - {range_end:%d %b} (section)"
    else:
        date_text = "date unknown"
    kind = "folder" if item["kind"] == "folder" else "file"
    section = item.get("section_name") or "-"
    return f'{item["name"]}  [{kind}; section: {section}; {date_text}]'
