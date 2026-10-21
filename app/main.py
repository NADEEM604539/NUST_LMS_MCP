"""NUST LMS MCP server.

Exposes the tools from the MyAgent project (NUST LMS automation + a few
utilities) to any MCP-capable chatbot / client.

Transport: stdio only. The MCP client (Claude Desktop, Cursor, ...) launches this
script itself and talks to it over stdin/stdout, so nothing here should ever
print to stdout. Do not start it by hand except to test with MCP Inspector.

    python app/main.py

NOTE: the LMS tools drive a *visible* Chrome window, so this server must run
on a machine with a desktop (your laptop). It cannot be deployed to a headless
cloud host like the RWA server.
"""

import ast
import asyncio
import datetime
import math
import operator
import platform
import sys
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

# Make `import app...` work whether launched as `python app/main.py` or `-m app.main`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app import lms_tools, student_profile

mcp = FastMCP(
    name="NUST LMS",
    instructions=(
        "Tools for the user's NUST LMS (lms.nust.edu.pk), driven through a visible "
        "Chrome window on the user's computer.\n"
        "- Always call login_to_lms first (default account 'MY', which reads "
        "MY_LMS_USERNAME / MY_LMS_PASSWORD from .env); every other LMS "
        "tool fails until a login exists. The login lasts as long as the server runs.\n"
        "- Course and item names are fuzzy: full titles, partial titles, or "
        "abbreviations like 'DV' all work. If a name is ambiguous the tool lists the "
        "candidates; ask the user which one they meant.\n"
        "- FILES GO THROUGH THE FILESYSTEM EXTENSION. Downloads and uploads use real "
        "folders on the user's computer, and the user's Filesystem extension "
        "(Claude Desktop > Settings > Extensions > 'Filesystem') decides which "
        "folders Claude may use. Workflow:\n"
        "  1. DOWNLOAD (download_course_material / download_course_materials / "
        "download_course_submission): call the Filesystem tool "
        "list_allowed_directories and pass the allowed directory ITSELF as save_dir, "
        "exactly as it is returned (e.g. 'E:\\NUST_LMS'). That directory is the ROOT: "
        "the course folders are created directly inside it. NEVER add a subfolder of "
        "your own such as Downloads, Courses, Materials or the course name, and never "
        "use the NUST_LMS_MCP project folder (it holds the server's source code). If "
        "several directories are allowed, use the one the user has designated for "
        "LMS files, otherwise ask the user once which one. Afterwards confirm the "
        "file with the Filesystem tools (list_directory / get_file_info) or read it.\n"
        "  2. UPLOAD (upload_course_submission): locate the file with the Filesystem "
        "tools (search_files / list_directory) and pass its exact full path as "
        "file_path.\n"
        "  3. If no Filesystem tool is available, or the tool returns a path/permission "
        "error, tell the user to install the Filesystem extension and add the folder "
        "under 'Allowed Directories', then retry. Never guess a path and never use a "
        "default location: there is none.\n"
        "- Downloads are auto-organised under save_dir, so pass only the ROOT folder "
        "and never create course/lab subfolders yourself: <Course>/ holds lectures and "
        "other material; <Course>/Labs/Lab N/ and <Course>/Assignments/Assignment N/ "
        "hold lab manuals and assignment files; <Course>/Submissions/<Lab N or "
        "Assignment N>/ holds the user's own submitted work. This applies to bulk "
        "downloads exactly as to single files. The tool result gives the "
        "exact saved path.\n"
        "- BULK / FILTERED DOWNLOADS: for anything that is not exactly ONE named "
        "file (e.g. 'download the lectures of the last week', 'all slides from DV "
        "and OS', 'last two weeks of every course', 'all labs', 'only the PDFs'), "
        "use download_course_materials, NOT download_course_material. It scans the "
        "chosen courses, applies every filter and downloads all matches in one go. "
        "Map the user's words to arguments: courses=[...] (omit for every IN-PROGRESS course; "
        "only in-progress courses are ever used, never past/future ones); last_weeks / last_days / period ('this_week', 'last_week', 'today', "
        "'yesterday', 'this_month', 'last_month') / since+until (YYYY-MM-DD; call "
        "get_current_time first to turn relative dates into these); "
        "categories=['lectures'|'labs'|'assignments']; name_contains / name_excludes; "
        "sections=['Week 5']; last_sections=N (newest N weeks of each course); "
        "file_types=['pdf','slides','word','excel','zip']. If unsure what a filter "
        "will catch, run list_course_materials first (same filters, downloads "
        "nothing). Large jobs run in the background: if a result says the job is "
        "STILL RUNNING, call get_materials_job_status(job_id) about every 30 "
        "seconds until it returns the final report, then summarise it for the user. "
        "If the report says items were left out because their date could not be "
        "determined, tell the user and offer include_undated=True or last_sections.\n"
        "- delete_course_submission is irreversible and upload_course_submission can "
        "replace work: only use confirm=True / replace_existing=True after the user "
        "has explicitly said so for that exact assignment.\n"
        "- SUBMISSION FILES (naming + header). The student's identity lives in .env "
        "as STUDENT_NAME, CMS and CLASS; never guess or type these from memory. "
        "Whenever the user asks you to create a file that is, or may later become, "
        "an LMS submission (assignment, lab, report, code file, notes for a "
        "submission):\n"
        "  1. The file NAME must be <topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext>, where "
        "<topic> is the subject of the file.\n"
        "  2. The very FIRST lines inside the file must show the student's Name, CMS "
        "ID, Class, Topic and Date.\n"
        "  Do both by creating the file with create_submission_file (it writes the "
        "header and the name for you). If the file is produced by another tool "
        "(for example a Word/PowerPoint/PDF generator), call get_submission_header "
        "first and put its text at the very top, and name the file with "
        "get_submission_filename. Read the student://submission-rules resource if "
        "unsure.\n"
        "  upload_course_submission also renames a COPY of the uploaded file to this "
        "convention automatically; pass topic= with the file's topic."
    ),
)


async def _run(func, *args, **kwargs) -> str:
    """Run a blocking LMS call off the event loop and map failures to ToolError."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except ToolError:
        raise
    except FutureTimeoutError as error:
        raise ToolError(
            "The LMS operation timed out after 5 minutes. The Chrome window may "
            "still be busy; check it and try again."
        ) from error
    except PermissionError as error:
        raise ToolError(
            f"Permission denied: {error}. " + lms_tools.FILESYSTEM_HINT
        ) from error
    except Exception as error:  # ValueError, RuntimeError, Playwright errors...
        message = str(error).strip() or type(error).__name__
        raise ToolError(message) from error


# ----------------------------------------------------------------------
# Utility tools
# ----------------------------------------------------------------------
@mcp.tool(
    title="System info",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def get_system_info() -> str:
    """Returns the host machine operating system, release, and architecture details."""
    return f"OS: {platform.system()} {platform.release()} ({platform.machine()})"


@mcp.tool(
    title="Current time",
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def get_current_time() -> str:
    """Returns the host machine's current local date and time as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("Exponent too large.")
        return _BIN_OPS[type(node.op)](left, right)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
    ):
        return _FUNCS[node.func.id](*[_eval_node(arg) for arg in node.args])
    raise ValueError("Unsupported expression.")


@mcp.tool(
    title="Calculator",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def execute_calculator(expression: str) -> str:
    """Evaluates a mathematical expression safely.

    Supports + - * / // % **, parentheses, the constants pi and e, and the
    functions sqrt, abs, round, sin, cos, tan, log, log10, log2, floor, ceil.

    Args:
        expression: A math expression string like '128 * 45 / 3'.
    """
    try:
        if len(expression) > 500:
            raise ValueError("Expression too long.")
        tree = ast.parse(expression.strip(), mode="eval")
        return f"Calculation Result: {_eval_node(tree)}"
    except Exception as error:
        return f"Math Evaluation Error: {error}"


# ----------------------------------------------------------------------
# NUST LMS tools
# ----------------------------------------------------------------------
@mcp.tool(
    title="Log in to NUST LMS",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
async def login_to_lms(person_name: str = "MY") -> str:
    """Opens a visible Chrome tab and signs in to NUST LMS. Call this before any other LMS tool.

    Credentials are read from the server's .env using the account prefix, i.e.
    <NAME>_LMS_USERNAME and <NAME>_LMS_PASSWORD. The default prefix is MY, so
    MY_LMS_USERNAME and MY_LMS_PASSWORD are used. Calling it again while already
    logged in is harmless.

    Args:
        person_name: Account name prefix. Leave as 'MY' (the default) unless the
            user says to use a different prefix present in .env.
    """
    return await _run(lms_tools.login, person_name)


@mcp.tool(
    title="Open a course",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def course_selector(course_name: str) -> str:
    """Finds a matching course on the logged-in LMS dashboard and opens it in the Chrome tab.

    Full or partial course names and abbreviations such as 'DV' are accepted.

    Args:
        course_name: Course title, course code, or abbreviation to find.
    """
    return await _run(lms_tools.select_course, course_name)


@mcp.tool(
    title="Open a quiz",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def open_course_quiz(course_name: str, quiz_name: str) -> str:
    """Finds and opens a named quiz within a specific logged-in LMS course (does not start or answer it).

    Args:
        course_name: Course name, course code, or abbreviation.
        quiz_name: Quiz title or a distinctive part of its title.
    """
    return await _run(lms_tools.open_quiz, course_name, quiz_name)


@mcp.tool(
    title="Open an assignment page",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def open_course_submission(course_name: str, submission_name: str) -> str:
    """Finds and opens a named assignment/submission page within an LMS course.

    Args:
        course_name: Course name, course code, or abbreviation.
        submission_name: Assignment or submission title to open.
    """
    return await _run(lms_tools.open_submission, course_name, submission_name)


@mcp.tool(
    title="Download course material",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def download_course_material(
    course_name: str, material_name: str, save_dir: str = ""
) -> str:
    """Downloads a named file/resource (slides, documents) from an LMS course into the given folder.

    Args:
        course_name: Course name, course code, or abbreviation.
        material_name: Slide, document, or resource title to download.
        save_dir: REQUIRED. Full path of the ROOT folder, e.g. 'E:/Labs/Downloads'.
            The tool files the download itself: <Course>/ for lectures and other
            material, <Course>/Labs/Lab N/ for lab manuals, <Course>/Assignments/
            Assignment N/ for assignment files. Choose a folder inside the Filesystem extension's
            allowed directories (call its list_allowed_directories first) so the
            file can be opened afterwards. Created if missing; a name clash gets a
            ' (1)' suffix, never overwrites. If this is empty or invalid the tool
            explains how to install/configure the Filesystem extension.
    """
    return await _run(lms_tools.download_material, course_name, material_name, save_dir)


@mcp.tool(
    title="List course materials (filtered preview)",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def list_course_materials(
    courses: list[str] | None = None,
    period: str = "",
    last_days: int = 0,
    last_weeks: int = 0,
    since: str = "",
    until: str = "",
    name_contains: list[str] | None = None,
    name_excludes: list[str] | None = None,
    categories: list[str] | None = None,
    sections: list[str] | None = None,
    last_sections: int = 0,
    file_types: list[str] | None = None,
    include_undated: bool = False,
    include_folders: bool = True,
    probe_dates: bool = True,
    limit: int = 0,
) -> str:
    """Previews which course files match the filters WITHOUT downloading anything.

    Takes exactly the same filters as download_course_materials (see that tool for
    what each means). Use it to check what a filter will catch, to see section
    names and dates, or when the user just asks 'what was uploaded last week?'.
    Scans every selected course, so it can take a while: if the result says the
    job is still running, call get_materials_job_status(job_id) until it finishes.
    """
    return await _run(
        lms_tools.start_materials_job,
        True,
        "",
        45,
        courses=courses,
        period=period,
        last_days=last_days,
        last_weeks=last_weeks,
        since=since,
        until=until,
        name_contains=name_contains,
        name_excludes=name_excludes,
        categories=categories,
        sections=sections,
        last_sections=last_sections,
        file_types=file_types,
        include_undated=include_undated,
        include_folders=include_folders,
        probe_dates=probe_dates,
        limit=limit,
    )


@mcp.tool(
    title="Download course materials in bulk (filters)",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
async def download_course_materials(
    save_dir: str,
    courses: list[str] | None = None,
    period: str = "",
    last_days: int = 0,
    last_weeks: int = 0,
    since: str = "",
    until: str = "",
    name_contains: list[str] | None = None,
    name_excludes: list[str] | None = None,
    categories: list[str] | None = None,
    sections: list[str] | None = None,
    last_sections: int = 0,
    file_types: list[str] | None = None,
    include_undated: bool = False,
    include_folders: bool = True,
    skip_already_downloaded: bool = True,
    probe_dates: bool = True,
    limit: int = 0,
) -> str:
    """Downloads MANY course files at once: every file that matches all the filters given.

    Use this (not download_course_material) whenever the request is not exactly one
    named file: 'last week's lectures', 'all slides from DV and OS', 'every lab',
    'last two weeks of all my courses'. All filters combine with AND; leave a
    filter at its default to ignore it. Files are organised automatically under
    save_dir (<Course>/, <Course>/Labs/Lab N/, <Course>/Assignments/Assignment N/):
    never create those subfolders yourself, and files inside LMS folder resources
    are filed the same way (no extra subfolder).

    Big jobs run in the background. If the result says the job is STILL RUNNING,
    call get_materials_job_status(job_id) about every 30 seconds until it reports
    the final summary, then tell the user what was saved / skipped / failed.

    Args:
        save_dir: REQUIRED. Full path of the ROOT folder: the allowed directory itself
            (call the Filesystem extension's list_allowed_directories first), with no
            extra subfolder like 'Downloads'. The course folders are created directly
            inside it. Never the NUST_LMS_MCP project folder.
        courses: Course names/codes/abbreviations, e.g. ['DV', 'Operating Systems'].
            Omit, leave empty, or use ['all'] for EVERY IN-PROGRESS course on the
            dashboard (past, future and hidden courses are never included).
        period: Calendar preset: 'today', 'yesterday', 'this_week' (since Monday),
            'last_week' (previous Monday-Sunday), 'this_month', 'last_month'.
        last_days: Rolling window: uploaded in the last N days (incl. today).
        last_weeks: Rolling window: uploaded in the last N weeks (N*7 days).
            'the last week' / 'last two weeks' -> 1 / 2.
        since: Only items uploaded on/after this date, YYYY-MM-DD.
        until: Only items uploaded on/before this date, YYYY-MM-DD.
        name_contains: Keep items whose title contains ANY of these words,
            e.g. ['lecture 5', 'week 3'].
        name_excludes: Drop items whose title contains any of these words.
        categories: Any of 'lectures' (slides/notes/other material), 'labs',
            'assignments'. Decided from the item title.
        sections: Keep items whose course SECTION title contains any of these,
            e.g. ['Week 7'].
        last_sections: Only the newest N sections (weeks) of each course that
            contain matching items. Good fallback when upload dates are unavailable.
        file_types: 'pdf', 'slides' (ppt/pptx), 'word', 'excel', 'zip', 'image',
            'video', or a raw extension like 'py'.
        include_undated: Also take items whose date could not be determined
            (default False: they are skipped and reported).
        include_folders: Also download the files inside LMS folder resources.
        skip_already_downloaded: Do not fetch an item again if an earlier bulk run
            already saved it under save_dir (default True).
        probe_dates: When the course page shows no date, read each file's upload
            time from the LMS (a little slower but accurate). Default True.
        limit: Stop after this many items (0 = no limit).
    """
    return await _run(
        lms_tools.start_materials_job,
        False,
        save_dir,
        40,
        courses=courses,
        period=period,
        last_days=last_days,
        last_weeks=last_weeks,
        since=since,
        until=until,
        name_contains=name_contains,
        name_excludes=name_excludes,
        categories=categories,
        sections=sections,
        last_sections=last_sections,
        file_types=file_types,
        include_undated=include_undated,
        include_folders=include_folders,
        skip_already_downloaded=skip_already_downloaded,
        probe_dates=probe_dates,
        limit=limit,
    )


@mcp.tool(
    title="Bulk job status",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def get_materials_job_status(job_id: str = "", wait_seconds: int = 25) -> str:
    """Progress or final report of a list_course_materials / download_course_materials job.

    Call it again every ~30 seconds while it says the job is still running.

    Args:
        job_id: The job id from the earlier result. Empty = the most recent job.
        wait_seconds: How long to wait for it to finish before answering (max 50).
    """
    return await _run(lms_tools.materials_job_status, job_id, wait_seconds)


@mcp.tool(
    title="Download my submission",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def download_course_submission(
    course_name: str, submission_name: str, save_dir: str = ""
) -> str:
    """Downloads the file(s) the user has ALREADY submitted to an LMS assignment into the given folder.

    Args:
        course_name: Course name, course code, or abbreviation.
        submission_name: Assignment/submission title, e.g. 'LAB 5 SUBMISSION-B'.
        save_dir: REQUIRED. Full path of the ROOT folder, e.g. 'E:/Labs/Downloads'.
            Files go to <Course>/Submissions/<Lab N or Assignment N>/ inside it,
            created automatically. Choose a folder inside the Filesystem extension's
            allowed directories (call its list_allowed_directories first) so the
            files can be opened afterwards. Created if missing; a name clash gets a
            ' (1)' suffix, never overwrites. If this is empty or invalid the tool
            explains how to install/configure the Filesystem extension.
    """
    return await _run(
        lms_tools.download_submission, course_name, submission_name, save_dir
    )


@mcp.tool(
    title="Get submission details",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def get_course_submission_details(course_name: str, submission_name: str) -> str:
    """Reports the full details of the user's submission on an LMS assignment page (read-only).

    Covers opened/due dates, submission and grading status, time remaining, last
    modified, submitted file names with upload times, comment count, any
    grade/feedback, and which actions (edit/remove/submit) are available.
    Nothing is downloaded or changed.

    Args:
        course_name: Course name, course code, or abbreviation.
        submission_name: Assignment/submission title, e.g. 'LAB 7 SUBMISSION-B'.
    """
    return await _run(lms_tools.submission_details, course_name, submission_name)


@mcp.tool(
    title="Delete my submission",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                 "openWorldHint": True},
)
async def delete_course_submission(
    course_name: str, submission_name: str, confirm: bool = False
) -> str:
    """Deletes (removes) the user's existing submission on an LMS assignment. This cannot be undone.

    Only set confirm=True after the user has explicitly asked to delete that
    specific submission. With confirm=False nothing is changed.

    Args:
        course_name: Course name, course code, or abbreviation.
        submission_name: Assignment/submission title, e.g. 'LAB 5 SUBMISSION-B'.
        confirm: Must be True to actually delete; otherwise nothing is changed.
    """
    return await _run(lms_tools.delete_submission, course_name, submission_name, confirm)


@mcp.tool(
    title="Upload a submission",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                 "openWorldHint": True},
)
async def upload_course_submission(
    course_name: str,
    submission_name: str,
    file_path: str = "",
    replace_existing: bool = False,
    topic: str = "",
    apply_naming: bool = True,
) -> str:
    """Uploads a local file as the user's submission on an LMS assignment and saves it.

    The uploaded file is automatically named <topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext>
    using STUDENT_NAME, CMS and CLASS from .env. This is done on a temporary copy;
    the user's original file is not renamed.

    If a submission already exists, nothing is uploaded unless
    replace_existing=True (which first deletes the old submission, so ask the
    user before using it). The result says if the assignment still needs its
    final "Submit assignment" button pressed.

    Args:
        course_name: Course name, course code, or abbreviation.
        submission_name: Assignment/submission title, e.g. 'LAB 5 SUBMISSION-B'.
        file_path: REQUIRED. Full path of the file to upload. Find it with the
            Filesystem extension's search_files / list_directory (inside an allowed
            directory), e.g. 'E:/Labs/lab5.zip'. If missing or not found the tool
            explains how to install/configure the Filesystem extension.
        replace_existing: Replace an existing submission with this file.
        topic: The topic/subject of the file, used as the start of the uploaded
            file name (e.g. 'Linked List Lab'). If empty, the file's current name
            (without extension) is used as the topic.
        apply_naming: Keep True so the file name follows the convention. Only set
            False if the user explicitly asks to upload under the original name.
    """
    return await _run(
        lms_tools.upload_submission,
        course_name,
        submission_name,
        file_path,
        replace_existing,
        topic,
        apply_naming,
    )


# ----------------------------------------------------------------------
# Student profile + submission file tools (identity comes from .env)
# ----------------------------------------------------------------------
@mcp.tool(
    title="Get student profile",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def get_student_profile() -> str:
    """Returns the student's name, CMS ID and class, read from the server's .env.

    Use this whenever those details are needed (document headers, cover pages,
    file names). Never guess or invent them. Never returns LMS credentials.
    """
    try:
        return student_profile.profile_summary()
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.tool(
    title="Get submission header",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def get_submission_header(topic: str, file_type: str = "txt") -> str:
    """Returns the student-details header that must be the FIRST lines of any submission file.

    Use when the file is created by something other than create_submission_file
    (for example a Word/PowerPoint generator): put this text at the very top of
    the document. The format matches file_type (comment syntax for code files).

    Args:
        topic: Topic/subject of the file (shown in the header and used in the file name).
        file_type: Extension without the dot, e.g. 'docx', 'txt', 'md', 'py', 'cpp', 'java'.
    """
    try:
        return student_profile.render_header(topic, file_type)
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.tool(
    title="Get submission file name",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def get_submission_filename(topic: str, file_type: str) -> str:
    """Returns the required file name <topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext> for a submission.

    Args:
        topic: Topic/subject of the file, e.g. 'Linked List Lab'.
        file_type: Extension without the dot, e.g. 'docx', 'pdf', 'py', 'zip'.
    """
    try:
        extension = (file_type or "").strip().lower().lstrip(".")
        if not extension:
            raise ValueError("file_type is required, e.g. 'docx'.")
        return f"{student_profile.submission_stem(topic)}.{extension}"
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.tool(
    title="Create submission file",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
async def create_submission_file(
    topic: str,
    content: str,
    save_dir: str,
    file_type: str,
    overwrite: bool = False,
) -> str:
    """Creates a file meant for LMS submission with the student's details at the top and the right name.

    The file is named <topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext> and its first
    lines are Name, CMS ID, Class, Topic and Date, all read from .env. Use this
    whenever the user asks you to create a file that will be submitted.

    Args:
        topic: Topic/subject of the file, e.g. 'Linked List Lab'. Becomes the start of the file name.
        content: The body of the file WITHOUT any student header (it is added for you).
            For docx, '# ' / '## ' lines become headings, '- ' lines bullets, '1. '
            lines numbered items and **text** bold. For code files, pass plain code.
        save_dir: REQUIRED. Full path of the folder to create the file in, inside the
            Filesystem extension's allowed directories (call its list_allowed_directories
            first).
        file_type: Extension without the dot: docx, txt, md, html, css, or a code type
            such as py, c, cpp, java, js, ts, cs, go, sql.
        overwrite: Replace the file if it already exists. Only after the user agrees.
    """
    return await _run(
        student_profile.create_submission_file,
        topic,
        content,
        save_dir,
        file_type,
        overwrite,
    )


@mcp.resource(
    "student://profile",
    name="student_profile",
    description="The student's name, CMS ID and class (from .env). No credentials.",
    mime_type="text/plain",
)
def student_profile_resource() -> str:
    try:
        return student_profile.profile_summary()
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.resource(
    "student://submission-rules",
    name="submission_rules",
    description="File-name convention and required header for every LMS submission file.",
    mime_type="text/plain",
)
def submission_rules_resource() -> str:
    try:
        return student_profile.submission_rules()
    except ValueError as error:
        raise ToolError(str(error)) from error


def main() -> None:
    # stdio only: the client spawns this process and owns stdin/stdout.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
