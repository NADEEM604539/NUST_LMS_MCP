"""NUST LMS browser automation.

Ported from MyAgent/backend/lms_tools.py. The Playwright worker and all page
logic are unchanged; only the LangChain ``@tool`` wrappers were replaced by
plain functions (login, select_course, open_quiz, ...) that ``main.py`` exposes
as MCP tools.

All browser work happens on ONE dedicated thread (Playwright's sync API is not
thread-safe). Every public function below just queues a request to that thread
and waits for the result, so they are safe to call from any thread.
"""

from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
from queue import Queue
import re
import shutil
from threading import Event, Lock, Thread
import time
from urllib.parse import parse_qs, urljoin, urlparse
import uuid

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app import lms_filters, student_profile

_lms_browser_worker = None
_lms_browser_worker_lock = Lock()

FILESYSTEM_HINT = (
    "HOW TO FIX: downloads and uploads go through folders on the user's computer. "
    "Install the Filesystem extension in Claude Desktop (Settings > Extensions > "
    "search 'Filesystem' > Install) and add the folder you want to use under "
    "'Allowed Directories'. Then use that extension's list_allowed_directories / "
    "search_files tools to get an exact full path, and retry with a path inside it."
)


def _resolve_save_dir(save_dir: str) -> Path:
    """Validates the caller-supplied download folder and creates it if missing."""
    cleaned = (save_dir or "").strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError(
            "save_dir is required: pass the full path of the folder to save into, "
            "e.g. 'E:/Labs/Downloads'. " + FILESYSTEM_HINT
        )
    path = Path(cleaned).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f'save_dir must be a full (absolute) path, got "{cleaned}". '
            + FILESYSTEM_HINT
        )
    if path.exists() and not path.is_dir():
        raise ValueError(
            f"save_dir points to a file, not a folder: {path}. " + FILESYSTEM_HINT
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(
            f"Could not use save_dir {path}: {error}. " + FILESYSTEM_HINT
        ) from error
    return path.resolve()


_CATEGORY_RE = re.compile(r"\b(labs?|assignments?|assign|assgn|asg)(?![a-z])", re.I)
# Same keywords, but for FILE names such as 'CS333_Lab_4_Process.docx' where the
# keyword is glued to the rest with underscores (no \b boundary there).
_FILENAME_CATEGORY_RE = re.compile(
    r"(?<![a-z])(labs?|assignments?|assign|assgn|asg)(?![a-z])", re.I
)


def _safe_folder_name(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_len].rstrip(" .") or "Untitled"


def _classify_activity(item_name: str, filename: str = ""):
    """Returns (category, folder_label): category is 'lab', 'assignment' or None.

    'LAB 5 SUBMISSION-B' -> ('lab', 'Lab 5'); 'Assignment 02' -> ('assignment',
    'Assignment 2'); 'Lab Manual' (no number) -> ('lab', 'Lab Manual').

    ``filename`` (optional, the real file name once the download starts) is only a
    fallback for items whose TITLE has no number: an item titled just 'LAB' that
    holds 'Lab 7.docx' is filed under 'Lab 7' instead of a folder called 'LAB'.
    """
    match = _CATEGORY_RE.search(item_name)
    if not match:
        return None, None
    category = "lab" if match.group(1).lower().startswith("lab") else "assignment"
    label = "Lab" if category == "lab" else "Assignment"
    number = re.match(r"[^0-9]{0,20}?(\d+)", item_name[match.end():])
    if number:
        return category, f"{label} {int(number.group(1))}"
    if filename:
        file_match = _FILENAME_CATEGORY_RE.search(filename)
        if file_match and (
            ("lab" if file_match.group(1).lower().startswith("lab") else "assignment")
            == category
        ):
            file_number = re.match(r"[^0-9]{0,20}?(\d+)", filename[file_match.end():])
            if file_number:
                return category, f"{label} {int(file_number.group(1))}"
    return category, _safe_folder_name(item_name)


def _organized_dir(
    root: Path,
    course_name: str,
    item_name: str,
    is_submission: bool,
    filename: str = "",
) -> Path:
    """Where a download goes under the caller's root folder.

    <root>/<Course>/                          lectures and other material
    <root>/<Course>/Labs/Lab N/               lab manuals
    <root>/<Course>/Assignments/Assignment N/ assignment files
    <root>/<Course>/Submissions/<Lab N|Assignment N|item name>/  your submissions

    ``filename`` is the real file name (known once the download starts); it only
    helps number a lab/assignment whose item title has no number.
    """
    course_dir = root / _safe_folder_name(course_name)
    category, folder = _classify_activity(item_name, filename)
    if is_submission:
        return course_dir / "Submissions" / (folder or _safe_folder_name(item_name))
    if category == "lab":
        return course_dir / "Labs" / folder
    if category == "assignment":
        return course_dir / "Assignments" / folder
    return course_dir


def _download_destination(
    filename: str,
    activity_name: str,
    download_dir: "Path | Callable[[str], Path]",
) -> Path:
    """Picks a non-clashing path for a download.

    ``download_dir`` is either a folder or a function(filename) -> folder, so the
    final folder can depend on the real file name (see _organized_dir).
    """
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
    if not filename:
        filename = re.sub(r"[^a-zA-Z0-9_-]+", "_", activity_name).strip("_")
    if not Path(filename).suffix:
        filename = f"{filename}.download"

    if callable(download_dir):
        download_dir = download_dir(filename)
    download_dir.mkdir(parents=True, exist_ok=True)
    destination = download_dir / filename
    suffix = destination.suffix
    stem = destination.stem
    counter = 1
    while destination.exists():
        destination = download_dir / f"{stem} ({counter}){suffix}"
        counter += 1
    return destination


def _course_match_score(query: str, course_name: str) -> int:
    query_words = re.findall(r"[a-z0-9]+", query.lower())
    course_words = re.findall(r"[a-z0-9]+", course_name.lower())
    normalized_query = "".join(query_words)
    normalized_course = "".join(course_words)
    if normalized_query == normalized_course:
        return 100
    if normalized_query in normalized_course:
        return 90
    if normalized_query in course_words:
        return 80
    if len(normalized_query) >= 2:
        for start in range(len(course_words) - len(normalized_query) + 1):
            initials = "".join(
                word[0] for word in course_words[start:start + len(normalized_query)]
            )
            if initials == normalized_query:
                return 70
    return 0


def _select_best_match(query: str, entries: list[dict], item_type: str):
    scored_entries = [
        (score, entry)
        for entry in entries
        if (score := _course_match_score(query, entry["name"]))
    ]
    if not scored_entries:
        available = ", ".join(entry["name"] for entry in entries)
        return None, (
            f'{item_type.title()} "{query}" was not found.'
            + (f" Available {item_type}s: {available}" if available else "")
        )

    highest_score = max(score for score, _ in scored_entries)
    matches = [
        entry for score, entry in scored_entries if score == highest_score
    ]
    if len(matches) > 1:
        choices = ", ".join(entry["name"] for entry in matches)
        return None, (
            f'"{query}" matches multiple {item_type}s: {choices}. '
            "Please specify which one."
        )
    return matches[0], None


class _LmsBrowserWorker:
    def __init__(self):
        self._requests = Queue()
        self._ready = Event()
        self._startup_error = None
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("Timed out while opening the LMS browser.")
        if self._startup_error:
            raise RuntimeError(
                f"Could not start the LMS browser: {self._startup_error}"
            ) from self._startup_error

    def login(self, person_name: str, username: str, password: str) -> str:
        return self._request(("login", person_name, username, password))

    def select_course(self, course_query: str) -> str:
        return self._request(("select_course", course_query))

    def open_activity(
        self, operation: str, course_query: str, item_query: str, extra=None
    ) -> str:
        return self._request((operation, course_query, item_query, extra))

    def _request(self, request) -> str:
        result = Future()
        self._requests.put((request, result))
        return result.result(timeout=300)

    def submit_materials_job(self, job, spec, save_root) -> None:
        """Queues a bulk list/download job and returns immediately.

        The job object carries progress and the final report; callers wait on
        ``job.finished`` instead of on a Future, so a long download never hits
        the MCP client's per-call timeout.
        """
        self._requests.put((("materials_job", job, spec, save_root), Future()))

    def _run(self):
        playwright = None
        browser = None
        try:
            playwright = sync_playwright().start()
            try:
                browser = playwright.chromium.launch(headless=False, channel="chrome")
            except PlaywrightError as chrome_error:
                try:
                    browser = playwright.chromium.launch(headless=False)
                except PlaywrightError as chromium_error:
                    raise RuntimeError(
                        f"Could not open Chrome or Playwright Chromium: {chromium_error}"
                    ) from chrome_error
        except Exception as error:
            if browser is not None:
                browser.close()
            if playwright is not None:
                playwright.stop()
            self._startup_error = error
            self._ready.set()
            return

        pages = {}
        active_person = None
        self._ready.set()
        try:
            while True:
                request, result = self._requests.get()
                if request is None:
                    break
                if not result.set_running_or_notify_cancel():
                    continue
                try:
                    operation = request[0]
                    if not browser.is_connected():
                        browser = playwright.chromium.launch(headless=False)
                        pages.clear()
                        active_person = None

                    if operation == "login":
                        _, person_name, username, password = request
                        page = pages.get(person_name)
                        if page is None or page.is_closed():
                            # Use an explicit context so extra tabs (e.g. for
                            # downloads) can share this login session.
                            page = browser.new_context().new_page()
                            pages[person_name] = page

                        if (
                            page.url.startswith("https://lms.nust.edu.pk/")
                            and not page.url.endswith("/login/index.php")
                            and page.locator("#login input#password").count() == 0
                        ):
                            active_person = person_name
                            result.set_result(
                                f"Already logged into NUST LMS (account {person_name}). "
                                "The Chrome tab has been left open."
                            )
                            continue

                        page.goto(
                            "https://lms.nust.edu.pk/login/index.php",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        username_field = page.locator("#login input#username")
                        password_field = page.locator("#login input#password")
                        username_field.wait_for(state="visible", timeout=15000)
                        password_field.wait_for(state="visible", timeout=15000)
                        username_field.fill(username, timeout=10000)
                        password_field.fill(password, timeout=10000)
                        page.locator("#login button[type='submit']").click(timeout=10000)
                        try:
                            page.wait_for_function(
                                """() => window.location.pathname !== '/login/index.php'
                                    || Boolean(document.querySelector(
                                        '#loginerrormessage, .loginerrors, .alert-danger'
                                    ))""",
                                timeout=20000,
                            )
                        except PlaywrightTimeoutError as error:
                            raise RuntimeError(
                                "LMS did not confirm login within 20 seconds; "
                                "the Chrome tab was left open."
                            ) from error

                        if page.locator("#login input#password").count():
                            error_message = page.locator(
                                "#loginerrormessage, .loginerrors, .alert-danger"
                            ).first
                            if error_message.count() and error_message.is_visible():
                                raise RuntimeError(
                                    f"LMS login failed: {error_message.inner_text().strip()}"
                                )
                            raise RuntimeError(
                                "LMS login did not complete; the login form remains open."
                            )

                        active_person = person_name
                        result.set_result(
                            f"Logged into NUST LMS (account {person_name}). "
                            "The Chrome tab has been left open."
                        )
                    elif operation == "select_course":
                        _, course_query = request
                        if active_person is None or active_person not in pages:
                            result.set_result(
                                "No LMS account is logged in yet. Log in first, then "
                                "ask me to open the course."
                            )
                            continue

                        page = pages[active_person]
                        course, message = self._open_course(page, course_query)
                        if message:
                            result.set_result(message)
                            continue
                        result.set_result(
                            f'Opened course "{course["name"]}" in the existing '
                            f"{active_person} LMS tab."
                        )
                    elif operation in {
                        "open_quiz",
                        "open_submission",
                        "download_material",
                        "download_submission",
                        "delete_submission",
                        "upload_submission",
                        "submission_details",
                    }:
                        _, course_query, item_query, extra = request
                        if active_person is None or active_person not in pages:
                            result.set_result(
                                "No LMS account is logged in yet. Log in first, then "
                                "try again."
                            )
                            continue

                        page = pages[active_person]
                        if page.is_closed():
                            pages.pop(active_person, None)
                            active_person = None
                            result.set_result(
                                "The LMS tab was closed. Log in again before continuing."
                            )
                            continue

                        course, message = self._open_course(page, course_query)
                        if message:
                            result.set_result(message)
                            continue

                        if operation == "open_quiz":
                            module_path = "/mod/quiz/"
                            item_type = "quiz"
                        elif operation in {
                            "open_submission",
                            "download_submission",
                            "delete_submission",
                            "upload_submission",
                            "submission_details",
                        }:
                            module_path = "/mod/assign/"
                            item_type = "submission"
                        else:
                            module_path = "/mod/resource/"
                            item_type = "course file"

                        activities = self._get_course_activities(page, module_path)
                        item, message = _select_best_match(
                            item_query, activities, item_type
                        )
                        if message:
                            result.set_result(
                                f'In course "{course["name"]}": {message}'
                            )
                            continue

                        if operation == "download_material":
                            download_root = Path(extra["download_dir"])

                            def target_dir(filename, _item=item, _course=course):
                                return _organized_dir(
                                    download_root,
                                    _course["name"],
                                    _item["name"],
                                    False,
                                    filename,
                                )

                            destination = self._download_course_file(
                                page, item, target_dir
                            )
                            result.set_result(
                                f'Downloaded "{item["name"]}" from course '
                                f'"{course["name"]}" to {destination}.'
                            )
                            continue

                        if operation == "download_submission":
                            target_dir = _organized_dir(
                                Path(extra["download_dir"]),
                                course["name"],
                                item["name"],
                                True,
                            )
                            paths = self._download_submission_files(
                                page, item, target_dir
                            )
                            if not paths:
                                result.set_result(
                                    f'In course "{course["name"]}": nothing has been '
                                    f'submitted to "{item["name"]}" yet, so there is '
                                    "no file to download."
                                )
                                continue
                            result.set_result(
                                f'Downloaded {len(paths)} submitted file(s) for '
                                f'"{item["name"]}" in course "{course["name"]}" to: '
                                + "; ".join(str(path) for path in paths)
                            )
                            continue

                        if operation == "delete_submission":
                            message = self._delete_submission(page, item)
                            result.set_result(
                                f'In course "{course["name"]}": {message}'
                            )
                            continue

                        if operation == "upload_submission":
                            message = self._upload_submission(page, item, extra or {})
                            result.set_result(
                                f'In course "{course["name"]}": {message}'
                            )
                            continue

                        if operation == "submission_details":
                            message = self._get_submission_details(page, item)
                            result.set_result(
                                f'In course "{course["name"]}":\n{message}'
                            )
                            continue

                        page.goto(
                            item["url"],
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        result.set_result(
                            f'Opened {item_type} "{item["name"]}" in course '
                            f'"{course["name"]}" in the existing LMS tab.'
                        )
                    elif operation == "materials_job":
                        _, job, spec, save_root = request
                        if active_person is None or active_person not in pages:
                            job.fail(
                                "No LMS account is logged in yet. Log in first, "
                                "then try again."
                            )
                            result.set_result(job.status)
                            continue
                        page = pages[active_person]
                        if page.is_closed():
                            pages.pop(active_person, None)
                            active_person = None
                            job.fail(
                                "The LMS tab was closed. Log in again before "
                                "continuing."
                            )
                            result.set_result(job.status)
                            continue
                        try:
                            self._materials_job(page, job, spec, save_root)
                        except Exception as job_error:
                            job.fail(f"{type(job_error).__name__}: {job_error}")
                        result.set_result(job.status)
                    else:
                        raise ValueError(f"Unknown LMS browser operation: {operation}")
                except Exception as error:
                    result.set_exception(error)
        finally:
            browser.close()
            playwright.stop()

    _COURSES_VIEW = '[data-region="courses-view"]'

    def _ensure_inprogress_view(self, page):
        """Forces the dashboard 'Course overview' block to the 'In progress' filter.

        STRICT: only in-progress courses may ever be used. The dashboard remembers
        the last grouping the user picked (All / Past / Future / Starred ...), so
        it is switched explicitly and then verified. Returns None when the view is
        confirmed to be 'In progress', otherwise an error message (never falls
        back to showing all courses).
        """
        view = page.locator(self._COURSES_VIEW).first
        try:
            view.wait_for(state="attached", timeout=15000)
        except PlaywrightTimeoutError:
            return "The 'Course overview' block was not found on the LMS dashboard."

        if view.get_attribute("data-grouping") != "inprogress":
            try:
                page.locator("#groupingdropdown").click(timeout=10000)
                page.locator(
                    'a.dropdown-item[data-filter="grouping"][data-value="inprogress"]'
                ).click(timeout=10000)
                page.wait_for_function(
                    """() => document.querySelector('[data-region="courses-view"]')
                        ?.getAttribute('data-grouping') === 'inprogress'""",
                    timeout=15000,
                )
            except PlaywrightError as error:
                return (
                    "Could not switch the LMS dashboard to the 'In progress' course "
                    f"filter: {error}"
                )

        # The cards re-render asynchronously after a filter change: wait until the
        # set of course ids stops changing so stale 'All' cards are never read.
        read_ids = (
            "() => Array.from(document.querySelectorAll("
            "'[data-region=\"courses-view\"] [data-course-id].course-card'))"
            ".map(card => card.dataset.courseId).sort().join(',')"
        )
        page.wait_for_timeout(800)
        previous = None
        for _ in range(20):
            current = page.evaluate(read_ids)
            if current == previous:
                break
            previous = current
            page.wait_for_timeout(500)

        grouping = page.locator(self._COURSES_VIEW).first.get_attribute(
            "data-grouping"
        )
        active = page.locator("#groupingdropdown [data-active-item-text]").first
        active_text = (active.inner_text().strip().lower() if active.count() else "")
        if grouping != "inprogress" or (active_text and "in progress" not in active_text):
            return (
                "Refusing to continue: the LMS dashboard is not on the 'In progress' "
                f"filter (grouping={grouping!r}, shown={active_text!r}). Only "
                "in-progress courses are allowed."
            )
        return None

    def _get_courses(self, page):
        """Reads the IN-PROGRESS courses on the LMS dashboard (and nothing else).

        Returns (courses, error). Past / future / hidden courses are never
        returned.
        """
        if page.is_closed():
            return None, "The LMS tab was closed. Log in again before continuing."

        page.goto(
            "https://lms.nust.edu.pk/my/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        if page.locator("#login input#password").count():
            return None, "The LMS session has expired. Log in again before continuing."

        problem = self._ensure_inprogress_view(page)
        if problem:
            return None, problem

        course_links = page.locator(
            self._COURSES_VIEW + ' a.coursename[href*="/course/view.php"]'
        )
        try:
            course_links.first.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError:
            return None, "No in-progress courses were found on the LMS dashboard."

        courses = course_links.evaluate_all(
            """links => {
                const seen = new Set();
                return links.flatMap(link => {
                    if (seen.has(link.href)) return [];
                    seen.add(link.href);
                    return [{
                        name: link.querySelector('.multiline')?.innerText?.trim()
                            || link.innerText.replace('Course name', '').trim(),
                        url: link.href
                    }];
                });
            }"""
        )
        return courses, None

    def _open_course(self, page, course_query: str):
        courses, message = self._get_courses(page)
        if message:
            return None, message
        course, message = _select_best_match(course_query, courses, "course")
        if message:
            return None, message

        page.goto(course["url"], wait_until="domcontentloaded", timeout=30000)
        return course, None

    def _get_course_activities(self, page, module_path: str) -> list[dict]:
        cards = page.locator('.activity-item[data-region="activity-card"]')
        try:
            cards.first.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError:
            return []
        return cards.evaluate_all(
            """(cards, modulePath) => cards.flatMap(card => {
                const link = Array.from(card.querySelectorAll('a[href]'))
                    .find(anchor => new URL(anchor.href).pathname.includes(modulePath));
                const name = card.dataset.activityname?.trim();
                return link && name ? [{name, url: link.href}] : [];
            })""",
            module_path,
        )

    def _download_course_file(
        self, page, activity: dict, download_dir: Path
    ) -> Path:
        browser_download = None

        def capture_download(download):
            nonlocal browser_download
            browser_download = download

        page.on("download", capture_download)
        navigation_error = None
        try:
            try:
                page.goto(
                    activity["url"], wait_until="domcontentloaded", timeout=30000
                )
            except PlaywrightError as error:
                if "Download is starting" not in str(error):
                    raise
                navigation_error = error
                # goto() raises before the "download" event is delivered, so keep
                # the listener attached and wait (up to 30s) for the event.
                for _ in range(300):
                    if browser_download is not None:
                        break
                    page.wait_for_timeout(100)
        finally:
            page.remove_listener("download", capture_download)

        if browser_download is not None:
            destination = _download_destination(
                browser_download.suggested_filename, activity["name"], download_dir
            )
            browser_download.save_as(str(destination))
            if not destination.exists():
                raise RuntimeError(
                    f"Download finished but the file was not found at {destination}."
                )
            return destination
        if navigation_error is not None:
            raise RuntimeError(
                "The LMS started a download but the browser never reported it."
            ) from navigation_error

        file_links = page.locator(
            'a[href*="/pluginfile.php/"], a[href*="/draftfile.php/"], '
            'a[href*="forcedownload=1"], iframe[src*="/pluginfile.php/"], '
            'object[data*="/pluginfile.php/"], embed[src*="/pluginfile.php/"]'
        )
        direct_url = None
        if file_links.count():
            link = file_links.first
            direct_url = (
                link.get_attribute("href")
                or link.get_attribute("src")
                or link.get_attribute("data")
            )
        elif "/pluginfile.php/" in page.url or "/draftfile.php/" in page.url:
            direct_url = page.url

        if not direct_url:
            raise RuntimeError(
                f'Could not find a downloadable file for "{activity["name"]}" '
                "on the LMS resource page."
            )

        return self._download_url(page, direct_url, activity["name"], download_dir)

    def _download_url(
        self, page, url: str, fallback_name: str, download_dir: Path
    ) -> Path:
        """Downloads a same-host LMS file into download_dir using Chrome itself.

        Playwright's APIRequestContext runs on Node's OpenSSL, which rejects the
        LMS server's legacy TLS renegotiation (EPROTO ... unsafe legacy
        renegotiation disabled). Chrome connects fine, so the file is fetched as
        a normal browser download in a temporary tab that shares the login.
        """
        url = urljoin(page.url, url)
        parsed_url = urlparse(url)
        if parsed_url.hostname != "lms.nust.edu.pk":
            raise RuntimeError("Refusing to download a file from an unexpected host.")
        if (
            "/pluginfile.php/" in parsed_url.path
            or "/draftfile.php/" in parsed_url.path
        ) and "forcedownload" not in parsed_url.query:
            url += ("&" if parsed_url.query else "?") + "forcedownload=1"

        download_page = page.context.new_page()
        captured = []
        download_page.on("download", lambda download: captured.append(download))
        try:
            try:
                download_page.goto(url, wait_until="commit", timeout=60000)
            except PlaywrightError as error:
                # Expected: navigating to a file attachment starts a download.
                if "Download is starting" not in str(error):
                    raise
            # The download event can arrive just after goto() returns/raises.
            for _ in range(600):
                if captured:
                    break
                download_page.wait_for_timeout(100)
            if not captured:
                raise RuntimeError(
                    f'The LMS did not start a download for "{fallback_name}".'
                )

            download = captured[0]
            if urlparse(download.url).hostname != "lms.nust.edu.pk":
                raise RuntimeError("The LMS file came from an unexpected host.")
            destination = _download_destination(
                download.suggested_filename, fallback_name, download_dir
            )
            download.save_as(str(destination))
        finally:
            download_page.close()

        if not destination.exists():
            raise RuntimeError(
                f"Download finished but the file was not found at {destination}."
            )
        return destination

    # ------------------------------------------------------------------
    # Bulk / filtered material downloads
    # ------------------------------------------------------------------
    def _collect_materials(self, page, course: dict, now: datetime) -> list[dict]:
        """Opens a course page and lists every file / folder on it with metadata."""
        page.goto(course["url"], wait_until="domcontentloaded", timeout=30000)
        if page.locator("#login input#password").count():
            raise RuntimeError(
                "The LMS session has expired. Log in again before continuing."
            )
        cards = page.locator('.activity-item[data-region="activity-card"]')
        try:
            cards.first.wait_for(state="attached", timeout=15000)
        except PlaywrightTimeoutError:
            return []
        raw_items = cards.evaluate_all(
            r"""(cards, modulePaths) => cards.flatMap(card => {
                const clean = text => (text || '').replace(/\s+/g, ' ').trim();
                const link = Array.from(card.querySelectorAll('a[href]')).find(anchor => {
                    const path = new URL(anchor.href).pathname;
                    return modulePaths.some(module => path.includes(module));
                });
                const name = card.dataset.activityname?.trim();
                if (!link || !name) return [];
                const section = card.closest(
                    'li.section, [data-region="section"], .course-section'
                );
                const sectionName = clean(
                    section?.dataset.sectionname
                    || section?.querySelector(
                        '.sectionname, [data-for="section_title"], h3'
                    )?.innerText
                );
                const numberText = section?.dataset.number
                    ?? (section?.id || '').replace(/^section-/, '');
                const details = Array.from(card.querySelectorAll(
                    '.resourcelinkdetails, .activity-information, '
                    + '[data-region="activity-information"], .activity-dates'
                )).map(element => clean(element.innerText)).filter(Boolean).join(' | ');
                const icon = card.querySelector('img.activityicon, img.icon');
                return [{
                    name, url: link.href, sectionName, numberText, details,
                    icon: icon ? icon.src : ''
                }];
            })""",
            ["/mod/resource/", "/mod/folder/"],
        )

        items = []
        seen_urls = set()
        section_order: dict[str, int] = {}
        for raw in raw_items:
            if raw["url"] in seen_urls:
                continue
            seen_urls.add(raw["url"])
            parsed_url = urlparse(raw["url"])
            cmid = parse_qs(parsed_url.query).get("id", [""])[0]
            section_name = raw.get("sectionName") or ""
            number_text = str(raw.get("numberText") or "")
            if number_text.isdigit():
                section_number = int(number_text)
            else:
                section_number = section_order.setdefault(
                    section_name, len(section_order)
                )
            icon_name = (raw.get("icon") or "").rsplit("/", 1)[-1]
            details = raw.get("details") or ""
            page_date = lms_filters.parse_datetime_text(details, now)
            items.append(
                {
                    "name": raw["name"],
                    "url": raw["url"],
                    "cmid": cmid,
                    "kind": "folder" if "/mod/folder/" in parsed_url.path else "file",
                    "section_name": section_name,
                    "section_number": section_number,
                    "type_hint": f"{details} {icon_name}",
                    "date": page_date,
                    "date_source": "page" if page_date else "",
                    "section_range": lms_filters.parse_section_range(
                        section_name, now
                    ),
                }
            )
        return items

    def _probe_last_modified(self, page, item: dict):
        """Reads a file's upload time from the LMS response headers.

        Used only when neither the course page nor the section title shows a
        date. A temporary tab opens the file, notes the Last-Modified header of
        the /pluginfile.php/ response and is closed again (the download is
        abandoned). Returns a naive local datetime or None.
        """
        if item["kind"] != "file":
            return None
        stamps: list[str] = []

        def on_response(response):
            try:
                if "/pluginfile.php/" in response.url or "/draftfile.php/" in response.url:
                    value = response.headers.get("last-modified")
                    if value:
                        stamps.append(value)
            except Exception:
                pass

        probe = page.context.new_page()
        probe.on("response", on_response)
        try:
            try:
                probe.goto(item["url"], wait_until="commit", timeout=30000)
            except PlaywrightError as error:
                if "Download is starting" not in str(error):
                    return None
            for _ in range(100):
                if stamps:
                    break
                probe.wait_for_timeout(100)
            if not stamps:
                return None
            try:
                return parsedate_to_datetime(stamps[0]).astimezone().replace(tzinfo=None)
            except (TypeError, ValueError):
                return None
        except Exception:
            return None
        finally:
            try:
                probe.close()
            except Exception:
                pass

    def _apply_date_filter(self, page, items: list[dict], spec, job):
        """Keeps items inside the date window. Returns (kept, undated_count)."""
        kept = []
        undated = 0
        to_probe = [
            item
            for item in items
            if spec.probe_dates and lms_filters.date_status(item, spec) is None
        ]
        if to_probe:
            job.log(
                f"  No date on the page for {len(to_probe)} item(s); "
                "reading their upload time from the LMS..."
            )
        for item in items:
            status = lms_filters.date_status(item, spec)
            if status is None and spec.probe_dates:
                probed = self._probe_last_modified(page, item)
                if probed is not None:
                    item["date"] = probed
                    item["date_source"] = "file"
                    status = lms_filters.date_status(item, spec)
            if status is None:
                if spec.include_undated:
                    kept.append(item)
                else:
                    undated += 1
            elif status:
                kept.append(item)
        return kept, undated

    def _download_folder_files(
        self, page, item: dict, target_dir: Path, spec
    ) -> list[Path]:
        """Downloads every file inside an LMS folder resource."""
        page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
        links = page.locator(
            '.foldertree a[href*="/pluginfile.php/"], '
            '.filemanager a[href*="/pluginfile.php/"]'
        ).evaluate_all(
            "links => links.map(a => ({name: (a.innerText || '').trim(), url: a.href}))"
        )
        if not links:
            raise RuntimeError(f'No files were found inside folder "{item["name"]}".')
        paths = []
        seen = set()
        for link in links:
            if link["url"] in seen:
                continue
            seen.add(link["url"])
            suffix = Path(urlparse(link["url"]).path).suffix.lower().lstrip(".")
            if spec.file_exts and suffix and suffix not in spec.file_exts:
                continue
            paths.append(
                self._download_url(
                    page, link["url"], link["name"] or item["name"], target_dir
                )
            )
        return paths

    def _materials_job(self, page, job, spec, save_root) -> None:
        """Scans the chosen courses, applies the filters, lists or downloads."""
        job.status = "running"
        now = datetime.now()
        courses, message = self._get_courses(page)
        if message:
            job.fail(message)
            return

        if spec.all_courses:
            selected = list(courses)
        else:
            selected = []
            for query in spec.courses:
                course, message = _select_best_match(query, courses, "course")
                if message:
                    job.notes.append(message)
                elif course not in selected:
                    selected.append(course)
        for note in job.notes:
            job.log(note)
        if not selected:
            job.fail("No matching course to scan. " + " ".join(job.notes))
            return

        plan: list[tuple[dict, dict]] = []
        undated_total = 0
        scanned_total = 0
        for course in selected:
            job.log(f'Scanning "{course["name"]}"')
            try:
                items = self._collect_materials(page, course, now)
            except Exception as error:
                job.log(f"  Could not read this course: {error}")
                job.notes.append(f'Could not read "{course["name"]}": {error}')
                continue
            scanned_total += len(items)
            items = lms_filters.static_filter(items, spec, _classify_activity)
            items = lms_filters.latest_sections(items, spec.last_sections)
            if spec.has_window and items:
                items, undated = self._apply_date_filter(page, items, spec, job)
                undated_total += undated
            job.log(f"  {len(items)} matching item(s)")
            plan.extend((course, item) for item in items)
            if spec.limit and len(plan) >= spec.limit:
                break
        if spec.limit:
            plan = plan[: spec.limit]
        job.total = len(plan)

        header = f"Filters -> {spec.describe()}"
        footer = []
        if undated_total:
            footer.append(
                f"{undated_total} item(s) were left out because no upload date could "
                "be determined for them (the course page shows none and reading "
                "the file's date failed). Re-run with include_undated=True to "
                "include them, or use last_sections / sections instead of a date."
            )
        footer.extend(job.notes)

        if job.dry_run or not plan:
            lines = [header, f"Scanned {scanned_total} file/folder item(s) in "
                     f"{len(selected)} course(s); {len(plan)} match."]
            current = None
            for course, item in plan:
                if course["name"] != current:
                    current = course["name"]
                    lines.append(f"\n{current}")
                lines.append("  - " + lms_filters.describe_item(item))
            if job.dry_run and plan:
                lines.append(
                    "\nNothing was downloaded. Call download_course_materials "
                    "with the same filters to download these."
                )
            lines.extend(footer)
            job.finish("\n".join(lines))
            return

        self._download_plan(page, job, plan, spec, Path(save_root), header, footer)

    def _download_plan(
        self, page, job, plan, spec, root: Path, header: str, footer: list[str]
    ) -> None:
        manifest = _load_manifest(root)
        saved: list[str] = []
        skipped_known: list[str] = []
        skipped_type: list[str] = []
        failed: list[str] = []
        for index, (course, item) in enumerate(plan, 1):
            job.log(f'[{index}/{job.total}] {course["name"]} / {item["name"]}')
            key = item["cmid"] or item["url"]
            known = manifest.get(key)
            if (
                spec.skip_downloaded
                and known
                and all(Path(path).is_file() for path in known)
            ):
                skipped_known.append(item["name"])
                job.done_count += 1
                continue
            try:
                # Every file lands in the architecture under the root:
                #   <root>/<Course>/  (+ Labs/Lab N, Assignments/Assignment N)
                # No extra subfolders (not even for LMS folder resources): their
                # files are filed exactly like single files.
                def target_dir(filename, _course=course, _item=item):
                    return _organized_dir(
                        root, _course["name"], _item["name"], False, filename
                    )

                if item["kind"] == "folder":
                    paths = self._download_folder_files(
                        page, item, target_dir, spec
                    )
                else:
                    paths = [self._download_course_file(page, item, target_dir)]
                kept = []
                for path in paths:
                    extension = path.suffix.lower().lstrip(".")
                    if spec.file_exts and extension not in spec.file_exts:
                        path.unlink(missing_ok=True)
                        skipped_type.append(f"{path.name} (.{extension or '?'})")
                    else:
                        kept.append(path)
                if kept:
                    saved.extend(str(path) for path in kept)
                    manifest[key] = [str(path) for path in kept]
                    _save_manifest(root, manifest)
                job.done_count += 1
            except Exception as error:
                message = str(error).strip() or type(error).__name__
                failed.append(f'{course["name"]} / {item["name"]}: {message}')
                job.failed_count += 1
                job.log(f"  FAILED: {message}")

        lines = [header, f"Downloaded {len(saved)} file(s) into {root}"]
        lines.extend(f"  - {path}" for path in saved[:80])
        if len(saved) > 80:
            lines.append(f"  ... and {len(saved) - 80} more")
        if skipped_known:
            lines.append(
                f"Skipped {len(skipped_known)} item(s) already downloaded earlier "
                "(set skip_already_downloaded=False to fetch them again)."
            )
        if skipped_type:
            lines.append(
                f"Discarded {len(skipped_type)} file(s) that did not match the "
                "requested file type: " + "; ".join(skipped_type[:20])
            )
        if failed:
            lines.append(f"FAILED ({len(failed)}):")
            lines.extend(f"  - {entry}" for entry in failed[:30])
        lines.extend(footer)
        job.finish("\n".join(lines))

    # ------------------------------------------------------------------
    # Assignment submissions (download / delete / upload)
    # ------------------------------------------------------------------
    @staticmethod
    def _assignment_url(activity: dict, action: str | None = None) -> str:
        cmid = parse_qs(urlparse(activity["url"]).query).get("id", [""])[0]
        if not cmid.isdigit():
            raise RuntimeError(
                f'Could not read the assignment id from {activity["url"]}.'
            )
        url = f"https://lms.nust.edu.pk/mod/assign/view.php?id={cmid}"
        if action:
            url += f"&action={action}"
        return url

    def _open_assignment(self, page, activity: dict):
        page.goto(
            self._assignment_url(activity),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        if page.locator("#login input#password").count():
            raise RuntimeError(
                "The LMS session has expired. Log in again before continuing."
            )
        try:
            page.locator(".submissionstatustable").first.wait_for(
                state="visible", timeout=15000
            )
        except PlaywrightTimeoutError as error:
            raise RuntimeError(
                f'Could not find the submission status on the page for '
                f'"{activity["name"]}".'
            ) from error

    @staticmethod
    def _submitted_file_links(page) -> list[dict]:
        return page.locator(
            '.fileuploadsubmission a[href*="/assignsubmission_file/"]'
        ).evaluate_all(
            "links => links.map(a => ({name: a.innerText.trim(), url: a.href}))"
        )

    def _download_submission_files(
        self, page, activity: dict, download_dir: Path
    ) -> list[Path]:
        self._open_assignment(page, activity)
        paths = []
        seen_urls = set()
        for link in self._submitted_file_links(page):
            if link["url"] in seen_urls:
                continue
            seen_urls.add(link["url"])
            paths.append(
                self._download_url(
                    page,
                    link["url"],
                    link["name"] or activity["name"],
                    download_dir,
                )
            )
        return paths

    def _get_submission_details(self, page, activity: dict) -> str:
        """Read-only: summarises everything the assignment page shows."""
        self._open_assignment(page, activity)
        data = page.evaluate(
            r"""() => {
                const clean = text => (text || '').replace(/\s+/g, ' ').trim();
                const tableRows = table => Array.from(table.querySelectorAll('tr'))
                    .flatMap(row => {
                        const label = row.querySelector('th');
                        const value = row.querySelector('td');
                        return label && value
                            ? [[clean(label.innerText), clean(value.innerText)]]
                            : [];
                    });
                const summary = document.querySelector('.submissionsummarytable table');
                const feedbackTables = Array.from(new Set(Array.from(
                    document.querySelectorAll('.feedbacktable table, .feedback table')
                )));
                return {
                    title: clean(document.querySelector('.activity-name')?.innerText),
                    description: clean(
                        document.querySelector('.activity-description')?.innerText
                    ),
                    dates: Array.from(document.querySelectorAll(
                        '[data-region="activity-dates"] > div'
                    )).map(element => clean(element.innerText)),
                    rows: summary ? tableRows(summary) : [],
                    files: Array.from(document.querySelectorAll(
                        '.fileuploadsubmission a[href]'
                    )).map(link => ({
                        name: clean(link.innerText),
                        url: link.href,
                        time: clean(
                            link.closest('.fileuploadsubmission')?.parentElement
                                ?.querySelector('.fileuploadsubmissiontime')?.innerText
                        ),
                    })),
                    commentCount: clean(
                        document.querySelector('.comment-link-count')?.innerText
                    ),
                    feedback: feedbackTables.flatMap(tableRows),
                    actions: Array.from(document.querySelectorAll(
                        '.singlebutton button'
                    )).map(button => clean(button.innerText)).filter(Boolean),
                };
            }"""
        )

        lines = [f'Assignment: {data["title"] or activity["name"]}']
        lines.extend(data["dates"])
        if data["description"]:
            lines.append(f'Description: {data["description"]}')
        lines.append("Submission status:")
        for label, value in data["rows"]:
            if label in {"File submissions", "Submission comments"}:
                continue
            lines.append(f"  {label}: {value}")
        files = data["files"]
        if files:
            lines.append(f"Submitted files ({len(files)}):")
            for file in files:
                when = f' (submitted {file["time"]})' if file["time"] else ""
                lines.append(f'  - {file["name"]}{when} -> {file["url"]}')
        else:
            lines.append("Submitted files: none")
        lines.append(f'Submission comments: {data["commentCount"] or "0"}')
        if data["feedback"]:
            lines.append("Feedback / grading:")
            for label, value in data["feedback"]:
                lines.append(f"  {label}: {value}")
        if data["actions"]:
            lines.append("Available actions: " + ", ".join(data["actions"]))
        lines.append(f"Page: {self._assignment_url(activity)}")
        return "\n".join(lines)

    def _delete_submission(self, page, activity: dict) -> str:
        self._open_assignment(page, activity)
        files = self._submitted_file_links(page)
        has_remove_button = page.locator(
            'form input[name="action"][value="removesubmissionconfirm"]'
        ).count()
        if not has_remove_button:
            if not files:
                return f'There is no submission on "{activity["name"]}" to delete.'
            return (
                f'The "Remove submission" option is not available for '
                f'"{activity["name"]}" (it may be locked or already graded).'
            )

        page.goto(
            self._assignment_url(activity, "removesubmissionconfirm"),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        continue_button = page.get_by_role(
            "button", name=re.compile(r"^\s*continue\s*$", re.I)
        ).first
        try:
            continue_button.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError as error:
            raise RuntimeError(
                "The LMS did not show the remove-submission confirmation page."
            ) from error
        with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            continue_button.click(timeout=10000)

        self._open_assignment(page, activity)
        if self._submitted_file_links(page):
            raise RuntimeError(
                "Confirmed the removal but the submitted file is still listed."
            )
        names = ", ".join(link["name"] for link in files) or "the submission"
        return f'Deleted {names} from "{activity["name"]}".'

    def _upload_submission(self, page, activity: dict, options: dict) -> str:
        file_path = Path(options["file_path"])
        replace_existing = bool(options.get("replace_existing"))

        self._open_assignment(page, activity)
        existing = self._submitted_file_links(page)
        removed_note = ""
        if existing:
            names = ", ".join(link["name"] for link in existing)
            if not replace_existing:
                return (
                    f'"{activity["name"]}" already has a submission ({names}). '
                    "Nothing was uploaded. Ask the user whether to replace it, "
                    "then retry with replace_existing=True."
                )
            self._delete_submission(page, activity)
            removed_note = f"Removed the previous submission ({names}). "

        page.goto(
            self._assignment_url(activity, "editsubmission"),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        if page.locator("#login input#password").count():
            raise RuntimeError(
                "The LMS session has expired. Log in again before continuing."
            )
        add_button = page.locator(".filemanager .fp-btn-add a").first
        try:
            add_button.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError as error:
            alert = page.locator(".alert").first
            reason = (
                f" LMS says: {alert.inner_text().strip()}"
                if alert.count() and alert.is_visible()
                else ""
            )
            raise RuntimeError(
                f'Could not open the upload form for "{activity["name"]}" '
                f"(submissions may be closed or locked).{reason}"
            ) from error

        files_before = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '.filemanager .fp-content .fp-file'
            )).filter(element => element.offsetParent !== null).length"""
        )
        add_button.click(timeout=10000)
        upload_repo = (
            page.locator(".fp-repo-area .fp-repo")
            .filter(has_text=re.compile(r"upload a file", re.I))
            .first
        )
        upload_repo.wait_for(state="visible", timeout=15000)
        upload_repo.click(timeout=10000)

        file_input = page.locator('.fp-upload-form input[type="file"]').first
        file_input.wait_for(state="attached", timeout=15000)
        file_input.set_input_files(str(file_path))
        page.locator(".fp-upload-form .fp-upload-btn").first.click(timeout=10000)

        outcome = page.wait_for_function(
            """(before) => {
                const visible = element => element.offsetParent !== null;
                const error = Array.from(document.querySelectorAll(
                    '.fp-msg-error, .moodle-dialogue-exception .moodle-exception-message'
                )).find(visible);
                if (error) {
                    const text = error.querySelector('.fp-msg-text') || error;
                    return 'error:' + text.innerText.trim();
                }
                const files = Array.from(document.querySelectorAll(
                    '.filemanager .fp-content .fp-file'
                )).filter(visible);
                return files.length > before ? 'ok' : false;
            }""",
            arg=files_before,
            timeout=240000,
        ).json_value()
        if str(outcome).startswith("error:"):
            raise RuntimeError(f"LMS rejected the upload: {outcome[6:]}")

        # The LMS requires the academic-honesty statement to be ticked before
        # the submission can be saved.
        statement = page.locator("#id_submissionstatement")
        if statement.count():
            if not statement.is_checked():
                statement.check(timeout=10000)
            if not statement.is_checked():
                raise RuntimeError(
                    "Could not tick the 'This submission is my own work' statement."
                )

        with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
            page.locator("#id_submitbutton").click(timeout=10000)

        self._open_assignment(page, activity)
        saved = self._submitted_file_links(page)
        if not saved:
            raise RuntimeError(
                "The form was saved but no submitted file is listed on the "
                "assignment page. Please check it manually."
            )
        message = (
            f'{removed_note}Uploaded "{file_path.name}" to "{activity["name"]}". '
            f'Files now submitted: {", ".join(link["name"] for link in saved)}.'
        )
        if page.locator('form input[name="action"][value="submit"]').count():
            message += (
                " It is saved as a draft: this assignment still has a "
                '"Submit assignment" button that must be pressed to finalise it.'
            )
        return message


def _get_lms_browser_worker() -> _LmsBrowserWorker:
    global _lms_browser_worker
    with _lms_browser_worker_lock:
        if (
            _lms_browser_worker is None
            or not _lms_browser_worker._thread.is_alive()
        ):
            _lms_browser_worker = _LmsBrowserWorker()
        return _lms_browser_worker


# ----------------------------------------------------------------------
# Public functions (exposed as MCP tools by main.py). Plain, blocking,
# thread-safe; they raise ValueError for bad input.
# ----------------------------------------------------------------------
def _validate_lms_item_query(value: str, label: str) -> str:
    query = value.strip()
    if not query:
        raise ValueError(f"{label} name cannot be empty.")
    if len(query) > 120:
        raise ValueError(f"{label} name must be 120 characters or fewer.")
    if not re.search(r"[a-z0-9]", query, re.IGNORECASE):
        raise ValueError(f"Enter a valid {label.lower()} name or abbreviation.")
    return query


DEFAULT_ACCOUNT = "MY"  # reads MY_LMS_USERNAME / MY_LMS_PASSWORD from .env


def login(person_name: str = DEFAULT_ACCOUNT) -> str:
    normalized_name = (person_name or "").strip().upper() or DEFAULT_ACCOUNT
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized_name):
        raise ValueError(
            "Person name must start with a letter and contain only letters, "
            "numbers, or underscores."
        )

    username_key = f"{normalized_name}_LMS_USERNAME"
    password_key = f"{normalized_name}_LMS_PASSWORD"
    username = os.getenv(username_key)
    password = os.getenv(password_key)
    missing_keys = [
        key
        for key, value in ((username_key, username), (password_key, password))
        if not value
    ]
    if missing_keys:
        raise ValueError(
            f"Missing required .env variable(s): {', '.join(missing_keys)}. "
            f"Add them to the .env file in the NUST_LMS_MCP folder "
            f"(the default account uses {DEFAULT_ACCOUNT}_LMS_USERNAME and "
            f"{DEFAULT_ACCOUNT}_LMS_PASSWORD) and restart the MCP server."
        )

    return _get_lms_browser_worker().login(normalized_name, username, password)


def select_course(course_name: str) -> str:
    course_query = course_name.strip()
    if not course_query:
        raise ValueError("Course name or abbreviation cannot be empty.")
    if len(course_query) > 120:
        raise ValueError("Course name must be 120 characters or fewer.")
    if not re.search(r"[a-z0-9]", course_query, re.IGNORECASE):
        raise ValueError("Enter a course name, course code, or abbreviation.")

    return _get_lms_browser_worker().select_course(course_query)


def open_quiz(course_name: str, quiz_name: str) -> str:
    course_query = _validate_lms_item_query(course_name, "Course")
    quiz_query = _validate_lms_item_query(quiz_name, "Quiz")
    return _get_lms_browser_worker().open_activity(
        "open_quiz", course_query, quiz_query
    )


def open_submission(course_name: str, submission_name: str) -> str:
    course_query = _validate_lms_item_query(course_name, "Course")
    submission_query = _validate_lms_item_query(submission_name, "Submission")
    return _get_lms_browser_worker().open_activity(
        "open_submission", course_query, submission_query
    )


def download_material(course_name: str, material_name: str, save_dir: str) -> str:
    course_query = _validate_lms_item_query(course_name, "Course")
    material_query = _validate_lms_item_query(material_name, "Material")
    download_dir = _resolve_save_dir(save_dir)
    return _get_lms_browser_worker().open_activity(
        "download_material",
        course_query,
        material_query,
        {"download_dir": str(download_dir)},
    )


def download_submission(
    course_name: str, submission_name: str, save_dir: str
) -> str:
    course_query = _validate_lms_item_query(course_name, "Course")
    submission_query = _validate_lms_item_query(submission_name, "Submission")
    download_dir = _resolve_save_dir(save_dir)
    return _get_lms_browser_worker().open_activity(
        "download_submission",
        course_query,
        submission_query,
        {"download_dir": str(download_dir)},
    )


def submission_details(course_name: str, submission_name: str) -> str:
    course_query = _validate_lms_item_query(course_name, "Course")
    submission_query = _validate_lms_item_query(submission_name, "Submission")
    return _get_lms_browser_worker().open_activity(
        "submission_details", course_query, submission_query
    )


def delete_submission(
    course_name: str, submission_name: str, confirm: bool = False
) -> str:
    course_query = _validate_lms_item_query(course_name, "Course")
    submission_query = _validate_lms_item_query(submission_name, "Submission")
    if not confirm:
        return (
            f'Nothing was deleted. Deleting the submission on "{submission_query}" '
            "cannot be undone; ask the user to confirm, then call this tool again "
            "with confirm=True."
        )
    return _get_lms_browser_worker().open_activity(
        "delete_submission", course_query, submission_query
    )


def upload_submission(
    course_name: str,
    submission_name: str,
    file_path: str,
    replace_existing: bool = False,
    topic: str = "",
    apply_naming: bool = True,
) -> str:
    course_query = _validate_lms_item_query(course_name, "Course")
    submission_query = _validate_lms_item_query(submission_name, "Submission")
    cleaned_path = file_path.strip().strip('"').strip("'")
    if not cleaned_path:
        raise ValueError(
            "file_path is required: pass the full path of the file to upload. "
            + FILESYSTEM_HINT
        )
    path = Path(cleaned_path).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f'file_path must be a full (absolute) path, got "{cleaned_path}". '
            + FILESYSTEM_HINT
        )
    if not path.is_file():
        raise ValueError(f"File not found: {path}. " + FILESYSTEM_HINT)
    if path.stat().st_size == 0:
        raise ValueError(f"File is empty: {path}")

    upload_path = path.resolve()
    temp_dir = None
    rename_note = ""
    if apply_naming:
        # Validates STUDENT_NAME / CMS / CLASS in .env before anything is
        # uploaded. Uploads a renamed COPY; the user's original is untouched.
        upload_path, temp_dir = student_profile.prepare_upload_copy(path, topic)
        rename_note = (
            f'File name set to the submission convention: "{path.name}" -> '
            f'"{upload_path.name}". '
        )
    try:
        result = _get_lms_browser_worker().open_activity(
            "upload_submission",
            course_query,
            submission_query,
            {"file_path": str(upload_path), "replace_existing": replace_existing},
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return rename_note + result


# ----------------------------------------------------------------------
# Bulk / filtered material downloads (list + download as background jobs)
# ----------------------------------------------------------------------
_MANIFEST_NAME = ".lms_downloads.json"


def _load_manifest(root: Path) -> dict:
    """Remembers which LMS items were already saved under ``root``."""
    try:
        data = json.loads((root / _MANIFEST_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(root: Path, manifest: dict) -> None:
    try:
        (root / _MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=1), encoding="utf-8"
        )
    except OSError:
        pass  # losing the manifest only means a file may be fetched twice


class _MaterialsJob:
    """Progress + result of one list/download run, shared with the worker thread."""

    def __init__(self, dry_run: bool):
        self.id = uuid.uuid4().hex[:8]
        self.dry_run = dry_run
        self.status = "queued"
        self.started = time.time()
        self.total = 0
        self.done_count = 0
        self.failed_count = 0
        self.notes: list[str] = []
        self.report = ""
        self.finished = Event()
        self._log: list[str] = []
        self._lock = Lock()

    def log(self, message: str) -> None:
        with self._lock:
            self._log.append(message)
            del self._log[:-200]

    def finish(self, report: str) -> None:
        self.report = report
        self.status = "done"
        self.finished.set()

    def fail(self, message: str) -> None:
        self.report = f"FAILED: {message}"
        self.status = "failed"
        self.finished.set()

    def snapshot(self) -> str:
        elapsed = int(time.time() - self.started)
        if self.finished.is_set():
            return f"{self.report}\n\n(job {self.id}, finished in {elapsed}s)"
        with self._lock:
            tail = "\n".join(self._log[-6:]) or "starting..."
        progress = (
            f"{self.done_count}/{self.total} files processed"
            + (f", {self.failed_count} failed" if self.failed_count else "")
            if self.total
            else "scanning courses"
        )
        return (
            f"Job {self.id} is STILL RUNNING after {elapsed}s ({progress}). "
            f"Latest progress:\n{tail}\n\n"
            f"Call get_materials_job_status(job_id='{self.id}') again in about "
            "30 seconds to get the result."
        )


_jobs: dict[str, _MaterialsJob] = {}
_jobs_lock = Lock()


def _register_job(job: _MaterialsJob) -> None:
    with _jobs_lock:
        _jobs[job.id] = job
        while len(_jobs) > 20:
            _jobs.pop(next(iter(_jobs)))


def _wait_for_job(job: _MaterialsJob, wait_seconds: int) -> str:
    wait = max(0, min(int(wait_seconds or 0), 50))
    job.finished.wait(timeout=wait)
    return job.snapshot()


def start_materials_job(
    dry_run: bool,
    save_dir: str = "",
    wait_seconds: int = 40,
    **filters,
) -> str:
    """Starts a filtered list (dry_run) or download of course materials.

    ``filters`` are passed to lms_filters.build_spec (courses, period,
    last_days, last_weeks, since, until, name_contains, name_excludes,
    categories, sections, last_sections, file_types, include_undated,
    include_folders, skip_already_downloaded, probe_dates, limit).
    Waits up to ``wait_seconds`` for the result; longer jobs keep running in
    the background and are followed up with materials_job_status().
    """
    spec = lms_filters.build_spec(datetime.now(), **filters)
    save_root = None if dry_run else str(_resolve_save_dir(save_dir))
    job = _MaterialsJob(dry_run)
    _register_job(job)
    _get_lms_browser_worker().submit_materials_job(job, spec, save_root)
    return _wait_for_job(job, wait_seconds)


def materials_job_status(job_id: str = "", wait_seconds: int = 20) -> str:
    """Progress / final report of a bulk job (the latest one if job_id is empty)."""
    with _jobs_lock:
        if job_id.strip():
            job = _jobs.get(job_id.strip())
        else:
            job = next(reversed(_jobs.values()), None) if _jobs else None
    if job is None:
        known = ", ".join(_jobs) or "none"
        raise ValueError(
            f'No bulk job found{" with id " + job_id if job_id.strip() else ""}. '
            f"Known job ids: {known}."
        )
    return _wait_for_job(job, wait_seconds)
