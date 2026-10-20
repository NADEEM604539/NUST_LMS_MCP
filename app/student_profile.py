"""Student identity + submission conventions.

Everything about *who the student is* comes from the server's .env:

    STUDENT_NAME=...
    CMS=...
    CLASS=...

Two conventions are enforced from here:

1. FILE NAME  ->  <topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext>
2. FILE HEADER -> the first lines of every submission document carry the
   student's name, CMS ID, class, topic and date.

Nothing in this module ever reads or returns the LMS username/password.
"""

import datetime
import os
import re
import shutil
import tempfile
from pathlib import Path

PROFILE_KEYS = ("STUDENT_NAME", "CMS", "CLASS")

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# File types that can carry a header, and how the header is written.
_LINE_COMMENT = {
    "py": "#", "sh": "#", "rb": "#", "yaml": "#", "yml": "#", "r": "#",
    "ps1": "#", "pl": "#",
    "js": "//", "ts": "//", "jsx": "//", "tsx": "//", "java": "//", "c": "//",
    "cpp": "//", "h": "//", "hpp": "//", "cs": "//", "go": "//", "rs": "//",
    "swift": "//", "kt": "//", "dart": "//", "scala": "//",
    "sql": "--", "lua": "--", "hs": "--",
    "m": "%", "tex": "%",
}
_PLAIN_TYPES = {"txt"}
_MARKDOWN_TYPES = {"md"}
_HTML_COMMENT_TYPES = {"html", "htm", "xml"}
_CSS_COMMENT_TYPES = {"css"}
_DOCX_TYPES = {"docx"}

SUPPORTED_TYPES = sorted(
    set(_LINE_COMMENT)
    | _PLAIN_TYPES
    | _MARKDOWN_TYPES
    | _HTML_COMMENT_TYPES
    | _CSS_COMMENT_TYPES
    | _DOCX_TYPES
)


# ----------------------------------------------------------------------
# Profile
# ----------------------------------------------------------------------
def get_profile() -> dict[str, str]:
    """Returns {'STUDENT_NAME', 'CMS', 'CLASS'} from .env or raises ValueError."""
    values = {
        key: (os.getenv(key) or "").strip().strip('"').strip("'").strip()
        for key in PROFILE_KEYS
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError(
            f"Missing required .env variable(s): {', '.join(missing)}. Add "
            "STUDENT_NAME, CMS and CLASS to the .env file in the NUST_LMS_MCP "
            "folder, then restart the MCP server (restart Claude Desktop)."
        )
    return values


def profile_summary() -> str:
    profile = get_profile()
    return (
        f"Student name: {profile['STUDENT_NAME']}\n"
        f"CMS ID: {profile['CMS']}\n"
        f"Class: {profile['CLASS']}"
    )


# ----------------------------------------------------------------------
# File naming
# ----------------------------------------------------------------------
def _clean_part(text: str, max_len: int | None = None) -> str:
    text = _INVALID_FILENAME_CHARS.sub("", text or "")
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip("._")
    if max_len is not None:
        text = text[:max_len].rstrip("._")
    return text


def identity_suffix(profile: dict[str, str] | None = None) -> str:
    """'<CMS>_<CLASS>_<STUDENT_NAME>' made filename-safe."""
    profile = profile or get_profile()
    return "_".join(_clean_part(profile[key]) for key in ("CMS", "CLASS", "STUDENT_NAME"))


def submission_stem(topic: str) -> str:
    """'<topic>_<CMS>_<CLASS>_<STUDENT_NAME>' (no extension).

    Idempotent: a topic that already ends with the identity suffix is not
    suffixed twice.
    """
    suffix = identity_suffix()
    clean_topic = _clean_part(topic, 80)
    tail = f"_{suffix}".lower()
    if clean_topic.lower().endswith(tail):
        clean_topic = clean_topic[: -len(tail)].rstrip("._")
    if not clean_topic:
        raise ValueError("topic cannot be empty: it becomes the start of the file name.")
    return f"{clean_topic}_{suffix}"


def submission_filename(topic: str, file_type: str) -> str:
    extension = _normalise_type(file_type)
    return f"{submission_stem(topic)}.{extension}"


def _normalise_type(file_type: str) -> str:
    extension = (file_type or "").strip().lower().lstrip(".")
    if extension not in SUPPORTED_TYPES:
        raise ValueError(
            f'File type "{file_type}" is not supported for headed submission '
            f"files. Supported: {', '.join(SUPPORTED_TYPES)}. (Binary or "
            "data formats such as pdf, csv, json and zip cannot carry a text "
            "header; create them elsewhere and pass them to "
            "upload_course_submission, which still applies the file naming.)"
        )
    return extension


# ----------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------
def header_fields(topic: str) -> list[tuple[str, str]]:
    profile = get_profile()
    return [
        ("Name", profile["STUDENT_NAME"]),
        ("CMS ID", profile["CMS"]),
        ("Class", profile["CLASS"]),
        ("Topic", topic.strip()),
        ("Date", datetime.date.today().isoformat()),
    ]


def render_header(topic: str, file_type: str) -> str:
    """Ready-to-paste header text for the given file type (ends with a blank line)."""
    extension = _normalise_type(file_type)
    fields = header_fields(topic)

    if extension in _DOCX_TYPES:
        return "\n".join(f"{label}: {value}" for label, value in fields) + "\n\n"
    if extension in _PLAIN_TYPES:
        body = "\n".join(f"{label}: {value}" for label, value in fields)
        return f"{body}\n{'-' * 40}\n\n"
    if extension in _MARKDOWN_TYPES:
        body = "\n".join(f"**{label}:** {value}  " for label, value in fields)
        return f"{body}\n\n---\n\n"
    if extension in _HTML_COMMENT_TYPES:
        body = "\n".join(f"  {label}: {value}" for label, value in fields)
        return f"<!--\n{body}\n-->\n"
    if extension in _CSS_COMMENT_TYPES:
        body = "\n".join(f" * {label}: {value}" for label, value in fields)
        return f"/*\n{body}\n */\n"
    prefix = _LINE_COMMENT[extension]
    body = "\n".join(f"{prefix} {label}: {value}" for label, value in fields)
    return f"{body}\n\n"


def _already_has_header(content: str) -> bool:
    profile = get_profile()
    head = content[:600].lower()
    return profile["CMS"].lower() in head and profile["STUDENT_NAME"].lower() in head


# ----------------------------------------------------------------------
# Creating a headed submission file
# ----------------------------------------------------------------------
def _write_docx(destination: Path, topic: str, content: str) -> None:
    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError as error:
        raise ValueError(
            "python-docx is not installed, so .docx files cannot be created. "
            "Run:  venv\\Scripts\\pip install python-docx  (then restart "
            "Claude Desktop), or use another file_type such as txt or md."
        ) from error

    def add_inline(paragraph, text: str) -> None:
        for index, piece in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
            if piece:
                paragraph.add_run(piece).bold = index % 2 == 1

    document = Document()
    if not _already_has_header(content):
        for label, value in header_fields(topic):
            paragraph = document.add_paragraph()
            paragraph.add_run(f"{label}: ").bold = True
            paragraph.add_run(value)
            paragraph.paragraph_format.space_after = Pt(0)
        document.add_paragraph()

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if heading:
            document.add_heading(heading.group(2).strip(), level=len(heading.group(1)))
        elif bullet:
            add_inline(document.add_paragraph(style="List Bullet"), bullet.group(1))
        elif numbered:
            add_inline(document.add_paragraph(style="List Number"), numbered.group(1))
        else:
            add_inline(document.add_paragraph(), line)
    document.save(str(destination))


def create_submission_file(
    topic: str,
    content: str,
    save_dir: str,
    file_type: str,
    overwrite: bool = False,
) -> str:
    # Imported here: lms_tools imports this module at load time.
    from app.lms_tools import _resolve_save_dir

    extension = _normalise_type(file_type)
    if not (content or "").strip():
        raise ValueError("content is empty: there is nothing to write into the file.")

    filename = submission_filename(topic, extension)  # also validates .env + topic
    directory = _resolve_save_dir(save_dir)
    destination = directory / filename
    if destination.exists() and not overwrite:
        raise ValueError(
            f"{destination} already exists. Nothing was written. Ask the user "
            "whether to overwrite it (then retry with overwrite=True) or use a "
            "different topic."
        )

    header_note = "student header added at the top"
    if extension in _DOCX_TYPES:
        if _already_has_header(content):
            header_note = "content already started with the student details, so no second header was added"
        _write_docx(destination, topic, content)
    else:
        if _already_has_header(content):
            text = content
            header_note = "content already started with the student details, so no second header was added"
        else:
            header = render_header(topic, extension)
            if extension in _LINE_COMMENT and content.startswith("#!"):
                first_line, _, rest = content.partition("\n")
                text = f"{first_line}\n{header}{rest}"
            else:
                text = header + content
        destination.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")

    return (
        f"Created {destination} ({header_note}). File name follows "
        "<topic>_<CMS>_<CLASS>_<STUDENT_NAME>. Pass this exact path as "
        "file_path to upload_course_submission when the user wants it submitted."
    )


# ----------------------------------------------------------------------
# Uploading: rename a copy, never the user's original
# ----------------------------------------------------------------------
def prepare_upload_copy(file_path: Path, topic: str = "") -> tuple[Path, Path]:
    """Copies file_path to a temp folder under the convention name.

    Returns (renamed_copy, temp_dir). The caller must delete temp_dir.
    """
    new_name = submission_stem(topic.strip() or file_path.stem) + file_path.suffix
    temp_dir = Path(tempfile.mkdtemp(prefix="lms_upload_"))
    renamed = temp_dir / new_name
    try:
        shutil.copy2(file_path, renamed)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return renamed, temp_dir


# ----------------------------------------------------------------------
# Rules text (served as an MCP resource)
# ----------------------------------------------------------------------
def submission_rules() -> str:
    profile = get_profile()
    example = submission_filename("Linked List Lab", "docx")
    return (
        "SUBMISSION RULES\n"
        "================\n"
        f"Student: {profile['STUDENT_NAME']} | CMS ID: {profile['CMS']} | "
        f"Class: {profile['CLASS']}\n\n"
        "1. FILE NAME: <topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext>\n"
        f"   Example: {example}\n"
        "   The topic is the subject of the file (e.g. the lab or assignment "
        "topic), not the student's name.\n\n"
        "2. FILE HEADER: the very first lines of every file that will become a "
        "submission must show Name, CMS ID, Class, Topic and Date, taken from "
        "this profile. Never type these details from memory or guess them.\n\n"
        "3. Creating the file: use create_submission_file (does 1 and 2 "
        "automatically). If another tool creates the file, call "
        "get_submission_header first and put its text at the top.\n\n"
        "4. Uploading: upload_course_submission renames a COPY of the file to "
        "the convention automatically; the original file is left untouched."
    )
