"""NUST LMS browser automation.

Ported from MyAgent/backend/lms_tools.py. The Playwright worker and all page
logic are unchanged; only the LangChain ``@tool`` wrappers were replaced by
plain functions (login, select_course, open_quiz, ...) that ``main.py`` exposes
as MCP tools.

All browser work happens on ONE dedicated thread (Playwright's sync API is not
thread-safe). Every public function below just queues a request to that thread
and waits for the result, so they are safe to call from any thread.
"""

from concurrent.futures import Future
import os
from pathlib import Path
from queue import Queue
import re
import shutil
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app import student_profile

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


def _download_destination(
    filename: str, activity_name: str, download_dir: Path
) -> Path:
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
    if not filename:
        filename = re.sub(r"[^a-zA-Z0-9_-]+", "_", activity_name).strip("_")
    if not Path(filename).suffix:
        filename = f"{filename}.download"

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
                            destination = self._download_course_file(
                                page, item, Path(extra["download_dir"])
                            )
                            result.set_result(
                                f'Downloaded "{item["name"]}" from course '
                                f'"{course["name"]}" to {destination}.'
                            )
                            continue

                        if operation == "download_submission":
                            paths = self._download_submission_files(
                                page, item, Path(extra["download_dir"])
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
                    else:
                        raise ValueError(f"Unknown LMS browser operation: {operation}")
                except Exception as error:
                    result.set_exception(error)
        finally:
            browser.close()
            playwright.stop()

    def _open_course(self, page, course_query: str):
        if page.is_closed():
            return None, "The LMS tab was closed. Log in again before continuing."

        page.goto(
            "https://lms.nust.edu.pk/my/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        if page.locator("#login input#password").count():
            return None, "The LMS session has expired. Log in again before continuing."

        course_links = page.locator('a.coursename[href*="/course/view.php"]')
        try:
            course_links.first.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError:
            return None, "No courses were found on the LMS dashboard."

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
