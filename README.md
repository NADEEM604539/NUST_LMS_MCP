# NUST LMS MCP

MCP server version of the MyAgent LMS agent. Any MCP-capable chatbot (Claude Desktop, Cursor, etc.) can now log in to NUST LMS, open courses, download slides, and manage assignment submissions.

## Tools (19) and resources (2)

| Tool | What it does | Safety |
|---|---|---|
| `login_to_lms(person_name="MY")` | Opens Chrome, signs in using `MY_LMS_USERNAME` / `MY_LMS_PASSWORD` from `.env` (prefix is `<NAME>_LMS_...`, default `MY`) | call first |
| `course_selector(course_name)` | Opens a course (fuzzy name / abbreviation like "DV") | read-only |
| `open_course_quiz(course_name, quiz_name)` | Opens a quiz page (does not start it) | read-only |
| `open_course_submission(course_name, submission_name)` | Opens an assignment page | read-only |
| `get_course_submission_details(...)` | Dates, status, grade, files, feedback | read-only |
| `download_course_material(course_name, material_name, save_dir)` | Saves ONE named slide/file into `save_dir` | writes a local file |
| `download_course_materials(save_dir, courses, period, last_weeks, ...)` | **Bulk, filtered download** of many files at once (see below) | writes local files |
| `list_course_materials(...)` | Same filters, but only previews what would be downloaded | read-only |
| `get_materials_job_status(job_id)` | Progress / final report of a bulk job that is still running | read-only |
| `download_course_submission(course_name, submission_name, save_dir)` | Saves your already-submitted files into `save_dir` | writes a local file |
| `upload_course_submission(course, name, file_path, replace_existing=False, topic="", apply_naming=True)` | Uploads the file at `file_path` under the name `<topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext>` (renamed copy, original untouched); won't overwrite unless `replace_existing=True` | changes LMS |
| `delete_course_submission(course, name, confirm=False)` | Removes your submission; does nothing unless `confirm=True` | irreversible |
| `get_student_profile()` | Returns `STUDENT_NAME`, `CMS`, `CLASS` from `.env` (never credentials) | read-only |
| `get_submission_header(topic, file_type)` | Header text (Name, CMS ID, Class, Topic, Date) to put at the very top of any submission file, in the right comment/markup syntax | read-only |
| `get_submission_filename(topic, file_type)` | The required file name `<topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext>` | read-only |
| `create_submission_file(topic, content, save_dir, file_type, overwrite=False)` | Creates the file with the student header on top and the correct name. Types: docx, txt, md, html, css, py, c, cpp, java, js, ts, cs, go, sql, ... | writes a local file |
| `get_system_info()` / `get_current_time()` | Host info | read-only |
| `execute_calculator(expression)` | Math (now AST-based, not `eval`) | read-only |

| Resource | Contents |
|---|---|
| `student://profile` | Student name, CMS ID, class |
| `student://submission-rules` | The naming convention and header rules, with an example |

## Bulk downloads with filters

`download_course_materials` scans the courses you pick, applies every filter you give it, and downloads all matches in one run. Filters combine with AND; anything left at its default is ignored.

| Filter | Meaning | Example |
|---|---|---|
| `courses` | Course names / codes / abbreviations. Omit or `['all']` = every course on the dashboard | `['DV', 'Operating Systems']` |
| `last_weeks`, `last_days` | Rolling window ending now | `last_weeks=2` |
| `period` | `today`, `yesterday`, `this_week`, `last_week` (previous Mon-Sun), `this_month`, `last_month` | `period='last_week'` |
| `since`, `until` | Explicit dates (`YYYY-MM-DD`) | `since='2026-10-01'` |
| `categories` | `lectures`, `labs`, `assignments` (decided from the item title) | `['labs']` |
| `name_contains`, `name_excludes` | Keywords in the item title | `['lecture 5']` |
| `sections` | Keywords in the course section title | `['Week 7']` |
| `last_sections` | Only the newest N sections (weeks) of each course | `last_sections=2` |
| `file_types` | `pdf`, `slides`, `word`, `excel`, `zip`, `image`, `video`, or an extension | `['pdf', 'slides']` |
| `include_folders` | Also download the files inside LMS folders (default on) | |
| `include_undated` | Also take items whose date could not be found (default off) | |
| `skip_already_downloaded` | Skip items an earlier bulk run saved under the same `save_dir` (default on; tracked in `.lms_downloads.json`) | |
| `limit` | Stop after N items | `limit=10` |

Examples you can say to Claude: "download the lectures of the last week", "all slides from DV and OS from the last two weeks", "every lab from all my courses", "only the PDFs from this month".

**How the date of a file is found** (first one that works wins):

1. A date shown on the course page for that item.
2. A week range in the section title (e.g. `14 October - 20 October`).
3. The file's own upload time, read from the LMS response headers (`probe_dates`, on by default; a small extra request per file).

Items with no determinable date are left out and reported, so nothing is silently dropped. Use `include_undated=True`, or `last_sections` / `sections`, when your courses name weeks like "Week 7" instead of using dates.

**Long jobs:** MCP clients give up on a single tool call after a minute or so, so bulk jobs run in the background. The tool waits ~40 s and, if the job is still going, returns a job id; Claude then polls `get_materials_job_status` until the final summary (saved / skipped / failed) is ready. `list_course_materials` takes the same filters and downloads nothing, which is useful for checking what a filter catches first.

## Submission naming and header

Student identity is read from `.env` (`STUDENT_NAME`, `CMS`, `CLASS`); it is never typed from memory.

- **File name:** `<topic>_<CMS>_<CLASS>_<STUDENT_NAME>.<ext>`, e.g. `Linked_List_Lab_123456_BSCS-12A_Nadeem_Mushtaq.docx`. Spaces become underscores and characters Windows forbids are removed.
- **File header:** the first lines of every submission file show Name, CMS ID, Class, Topic and Date. If the content already starts with the student's details, no second header is added.
- When Claude is asked to create a file that will be submitted, the server instructions tell it to use `create_submission_file` (or `get_submission_header` + `get_submission_filename` when another tool builds the file).
- On upload, a **copy** of the file is renamed to the convention in a temp folder and that copy is uploaded, so your original file keeps its name. Pass `topic` to control the first part of the name; if omitted, the file's current name is used. Set `apply_naming=False` to upload under the original name.
- If `STUDENT_NAME`, `CMS` or `CLASS` is missing from `.env`, these tools stop with an error naming the missing key instead of guessing.

## Files: use the Filesystem extension

Downloads and uploads happen on real folders, and the **Filesystem extension** decides which folders Claude can use. One-time setup:

1. Claude Desktop > Settings > Extensions > search **Filesystem** > Install.
2. Open its settings and add every folder you want to use (e.g. your labs folder, a downloads folder) under **Allowed Directories**.
3. Restart Claude Desktop.

Then Claude does the rest: for a download it asks the Filesystem extension which folders are allowed, passes one as `save_dir`, and checks the saved file afterwards; for an upload it finds your file with the Filesystem tools and passes the exact path as `file_path`. If a path is missing or wrong, the tool's error tells Claude (and you) to set the extension up.

### Folder layout (automatic)

`save_dir` is only the root: pass the allowed directory itself (for example `E:\NUST_LMS`), with no extra `Downloads`-style subfolder. The course folders sit directly inside it, and the server files each download itself, for single files and bulk downloads alike:

```
<save_dir>/<Course name>/                          lectures and other material
<save_dir>/<Course name>/Labs/Lab 5/               lab manuals
<save_dir>/<Course name>/Assignments/Assignment 2/ assignment files
<save_dir>/<Course name>/Submissions/Lab 5/        your own submitted work
<save_dir>/<Course name>/Submissions/Assignment 2/
```

Folder names come from the LMS item name (`LAB 5 SUBMISSION-B` -> `Lab 5`). If the item title has no number (e.g. an item called just `LAB`), the number is read from the real file name (`Lab 7.docx` -> `Lab 7`). A lab/assignment with no number anywhere keeps its own name; other submissions go to `Submissions/<item name>`. Files inside an LMS folder resource are filed the same way, with no extra subfolder.

Nothing is saved to a default location. Paths must be full absolute paths. `save_dir` is created if missing, and a filename clash gets a ` (1)` suffix instead of overwriting.

## Setup

```bat
cd /d "E:\Agentic AI projects\NUST_LMS_MCP"
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

`.env` needs:

```
MY_LMS_USERNAME=...
MY_LMS_PASSWORD=...
CMS=...
CLASS=...
STUDENT_NAME=...
```

`.docx` submission files need `python-docx` (listed in `requirements.txt`; if you installed earlier, run `venv\Scripts\pip install python-docx`). Run `venv\Scripts\python.exe check_setup.py` to verify everything.

## Connect to Claude Desktop

Edit `claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "nust-lms": {
      "command": "E:\\Agentic AI projects\\NUST_LMS_MCP\\venv\\Scripts\\python.exe",
      "args": ["E:\\Agentic AI projects\\NUST_LMS_MCP\\app\\main.py"]
    }
  }
}
```

Restart Claude Desktop. Other stdio clients use the same command/args.

## Notes

- stdio only: the client launches the server itself, so you never start it manually. Claude.ai on the web can't reach it; use a desktop client.
- The login lives as long as the server process; closing the client stops it, so log in again next session.
- Test with MCP Inspector: `npx @modelcontextprotocol/inspector python app/main.py`
