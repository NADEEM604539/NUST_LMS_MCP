"""Pre-flight check for the NUST LMS MCP server.

Run from the project folder with the venv python:

    venv\Scripts\python.exe check_setup.py

Prints OK / PROBLEM lines. It never prints your credentials (key names only).
It does not log in to the LMS or open a browser window beyond a quick Chrome
launch test.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
problems = 0


def ok(msg):
    print(f"  OK       {msg}")


def bad(msg):
    global problems
    problems += 1
    print(f"  PROBLEM  {msg}")


def warn(msg):
    print(f"  NOTE     {msg}")


print(f"Python {sys.version.split()[0]} ({sys.executable})")

# 1. imports + server object
try:
    from app import main as server_module  # loads .env too

    ok("app.main imported")
except Exception as error:
    bad(f"could not import app.main: {type(error).__name__}: {error}")
    sys.exit(1)

# 2. credentials + student profile present (names only, values never printed)
for key in (
    "MY_LMS_USERNAME", "MY_LMS_PASSWORD",
    "CMS", "CLASS", "STUDENT_NAME",
):
    if os.getenv(key):
        ok(f"{key} is set in .env")
    else:
        bad(f"{key} is missing from {ROOT / '.env'}")

try:
    import docx  # noqa: F401

    ok("python-docx installed (.docx submission files supported)")
except ImportError:
    warn(
        "python-docx is not installed: .docx submission files cannot be created. "
        "Run: venv\\Scripts\\pip install python-docx"
    )

# 3. tools registered
async def list_tools():
    from fastmcp import Client

    async with Client(server_module.mcp) as client:
        return [tool.name for tool in await client.list_tools()]


try:
    names = asyncio.run(list_tools())
    expected = {
        "get_system_info", "get_current_time", "execute_calculator",
        "login_to_lms", "course_selector", "open_course_quiz",
        "open_course_submission", "download_course_material",
        "download_course_submission", "get_course_submission_details",
        "delete_course_submission", "upload_course_submission",
        "get_student_profile", "get_submission_header",
        "get_submission_filename", "create_submission_file",
    }
    missing = expected - set(names)
    if missing:
        bad(f"tools missing: {sorted(missing)}")
    else:
        ok(f"{len(names)} tools registered")
except Exception as error:
    bad(f"could not list tools: {type(error).__name__}: {error}")

# 4. browser available
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, channel="chrome")
            browser.close()
            ok("Google Chrome can be launched")
        except Exception:
            try:
                browser = p.chromium.launch(headless=True)
                browser.close()
                ok("Playwright Chromium can be launched (Chrome not found)")
            except Exception as error:
                bad(
                    "no browser available. Run: venv\\Scripts\\playwright install chromium"
                    f"  ({str(error).splitlines()[0]})"
                )
except Exception as error:
    bad(f"playwright check failed: {type(error).__name__}: {error}")

print()
print("All good - add it to Claude Desktop." if not problems else f"{problems} problem(s) above - fix them first.")
sys.exit(1 if problems else 0)
