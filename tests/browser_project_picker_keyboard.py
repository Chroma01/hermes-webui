#!/usr/bin/env python3
"""
Headless browser gate: the "Move to project" pickers work from the keyboard,
return focus when they close, are translated, and have finger-sized rows (#8044).

WHY THIS EXISTS
  The single-conversation picker and the batch picker built their rows as
  click-only <div>s: no role, no tabindex, no key handling. A keyboard or
  screen-reader user could open the conversation's ⋮ menu, choose "Move to
  project", and then reach nothing. "No project" and "+ New project" were
  hard-coded English, and a row was about 24px tall on a phone.

WHAT IT CHECKS
  single-conversation picker, opened from the ⋮ menu with the keyboard
  - the picker is a menu of buttons, and focus lands on the conversation's
    current project;
  - ArrowDown / ArrowUp wrap, Home / End jump;
  - Escape closes it and focus returns to the conversation's ⋮ trigger, also
    when the sidebar was repainted while the picker was open (the original
    trigger is gone by then);
  - Enter on a project and Space on "No project" send the move, as a mouse
    click on a row does;
  - "No project" and "+ New project" follow the interface language.
  batch picker, opened from the selection bar
  - rows are buttons, focus lands on the first, "No project" is translated;
  - Escape closes it and focus returns to the bar's "Move to project" button;
  - it still sits inside the selection bar.
  rows under a coarse pointer (a touch context, 390x844)
  - every row of both pickers is at least 44px tall; with a mouse they keep
    their compact height.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state. Conversations are imported and projects created
  through the public API. Where the picker sits on screen is not checked here.

USAGE
  python tests/browser_project_picker_keyboard.py
  python tests/browser_project_picker_keyboard.py --screenshots DIR
      also writes the open single-conversation picker at 390x844, 844x390 and
      1440x900 (many projects, long names, German) into DIR.
  (Requires: playwright + chromium.)

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

PORT = int(os.getenv("PROJECT_PICKER_PORT", "8798"))
BASE = f"http://127.0.0.1:{PORT}"
WAIT_MS = 5000
# After a move, a selection change or entering selection mode the sidebar is
# repainted once more about 300 ms later, which replaces every row, trigger and
# selection-bar button. A person is slower than that; the driver waits it out.
SETTLE_MS = 600
MIN_TOUCH_ROW_PX = 44
PROJECTS = ["Research", "Client work", "Reading list"]
LONG_PROJECTS = [
    "Quartalsbericht und Budgetplanung für das kommende Geschäftsjahr",
    "Kundengespräche",
    "Übersetzungen",
    "Архив переписки",
    "読書メモ",
    "Maintenance",
    "Onboarding",
    "Hiring",
    "Infrastructure",
    "Design reviews",
    "Release notes",
    "Experiments",
]


def _wait_for_health(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _wait_until(page, expression, timeout_ms=WAIT_MS):
    """Poll ``expression`` with page.evaluate. The app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(50)
    return False


SEED_JS = """async ({projects}) => {
  const post = async (path, body) => {
    const response = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(path + ' failed: ' + JSON.stringify(data));
    return data;
  };
  const messages = [
    {role: 'user', content: 'hello'},
    {role: 'assistant', content: 'hello back'},
  ];
  const sids = [];
  for (const title of ['Alpha conversation', 'Beta conversation']) {
    const data = await post('/api/session/import', {title, messages});
    sids.push(data.session.session_id);
  }
  const ids = [];
  for (const name of projects) {
    const data = await post('/api/projects/create', {name, color: '#7cb9ff'});
    ids.push(data.project.project_id);
  }
  await post('/api/session/move', {session_id: sids[0], project_id: ids[0]});
  await renderSessionList();
  return {alpha: sids[0], beta: sids[1], projects: ids};
}"""

# What the open picker looks like, as data.
PICKER_JS = """(selector) => {
  const picker = document.querySelector(selector);
  if (!picker) return null;
  const rows = Array.from(picker.querySelectorAll('.project-picker-item'));
  return {
    role: picker.getAttribute('role'),
    insideBatchBar: !!picker.closest('#batchActionBar'),
    rows: rows.map(row => ({
      tag: row.tagName,
      type: row.getAttribute('type'),
      role: row.getAttribute('role'),
      checked: row.getAttribute('aria-checked'),
      text: row.textContent.trim(),
      focused: row === document.activeElement,
      height: Math.round(row.getBoundingClientRect().height),
    })),
  };
}"""

FOCUSED_TRIGGER_JS = """(sid) => {
  const active = document.activeElement;
  if (!active || !active.classList.contains('session-actions-trigger')) return false;
  const row = active.closest('.session-item,.session-child-session');
  return !!row && row.dataset.sid === sid && active.isConnected;
}"""

SINGLE = ".project-picker:not(.batch-project-picker)"
BATCH = ".batch-project-picker"


def _new_page(browser, **context_args):
    ctx = browser.new_context(base_url=BASE, **context_args)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    ready = "typeof S !== 'undefined' && typeof renderSessionList === 'function'"
    if not _wait_until(page, ready, 15000):
        ctx.close()
        return None, None, ["the app did not initialize"]
    page.wait_for_timeout(1000)
    return ctx, page, errors


def _open_single_picker(page, sid):
    """Open the picker the way a keyboard user does: ⋮ trigger, then the menu's
    "Move to project" item. Returns a failure line, or None.

    A sidebar repaint replaces the row and its trigger, so the trigger focused
    here can be gone before Enter is pressed. That race is this driver's: let the
    sidebar settle first, and focus again and retry if it still happens."""
    problem = None
    page.wait_for_timeout(SETTLE_MS)
    for _attempt in range(3):
        focused = page.evaluate(
            """(sid) => {
              const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
              const trigger = row && row.querySelector('.session-actions-trigger');
              if (!trigger) return false;
              trigger.focus();
              return document.activeElement === trigger;
            }""",
            sid,
        )
        if not focused:
            problem = "the conversation's ⋮ trigger could not be focused"
            page.wait_for_timeout(200)
            continue
        page.keyboard.press("Enter")
        if not _wait_until(page, "!!document.querySelector('.session-action-menu')", 1500):
            problem = "the ⋮ menu did not open from the keyboard"
            continue
        moved = page.evaluate(
            """() => {
              const label = t('session_move_project');
              const item = Array.from(document.querySelectorAll('.session-action-menu .session-action-opt'))
                .find(opt => opt.textContent.trim() === label);
              if (!item) return false;
              item.focus();
              return document.activeElement === item;
            }"""
        )
        if not moved:
            return "the ⋮ menu has no focusable 'Move to project' item"
        page.keyboard.press("Enter")
        if _wait_until(page, f"!!document.querySelector('{SINGLE}')", 1500):
            return None
        problem = "the project picker did not open"
    return problem


def _focused_text(page):
    return page.evaluate("document.activeElement ? document.activeElement.textContent.trim() : ''")


def _check_single(page, seed):
    failures = []
    alpha = seed["alpha"]

    def fail(message):
        failures.append(f"  [single] {message}")

    problem = _open_single_picker(page, alpha)
    if problem:
        return [f"  [single] {problem}"]
    picker = page.evaluate(PICKER_JS, SINGLE)
    texts = [row["text"] for row in picker["rows"]]
    expected = ["No project", *PROJECTS, "+ New project"]
    if texts != expected:
        fail(f"rows are {texts}, expected {expected}")
    if picker["role"] != "menu":
        fail(f"the picker's role is {picker['role']!r}, expected 'menu'")
    not_buttons = [row["text"] for row in picker["rows"] if row["tag"] != "BUTTON" or row["type"] != "button"]
    if not_buttons:
        fail(f"rows that are not <button type=button>: {not_buttons}")
    roles = [row["role"] for row in picker["rows"]]
    if roles != ["menuitemradio"] * (len(PROJECTS) + 1) + ["menuitem"]:
        fail(f"row roles are {roles}")
    checked = [row["text"] for row in picker["rows"] if row["checked"] == "true"]
    if checked != [PROJECTS[0]]:
        fail(f"checked rows are {checked}, expected only the current project")
    focused = [row["text"] for row in picker["rows"] if row["focused"]]
    if focused != [PROJECTS[0]]:
        fail(f"focus opened on {focused}, expected the current project")
    if failures:
        return failures

    # Arrows wrap, Home and End jump.
    steps = [
        ("ArrowDown", PROJECTS[1]),
        ("ArrowUp", PROJECTS[0]),
        ("Home", "No project"),
        ("ArrowUp", "+ New project"),
        ("ArrowDown", "No project"),
        ("End", "+ New project"),
    ]
    for key, want in steps:
        page.keyboard.press(key)
        got = _focused_text(page)
        if got != want:
            fail(f"{key} moved focus to {got!r}, expected {want!r}")

    # Escape closes and returns focus to the conversation's trigger.
    page.keyboard.press("Escape")
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("Escape did not close the picker")
    if not page.evaluate(FOCUSED_TRIGGER_JS, alpha):
        fail("after Escape focus is not on the conversation's ⋮ trigger")

    # The same after a sidebar repaint replaced the trigger the picker opened from.
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.evaluate("renderSessionListFromCache()")
    if not _wait_until(page, f"!!document.querySelector('{SINGLE}')", 500):
        fail("a sidebar repaint closed the picker")
    else:
        page.evaluate(f"document.querySelector('{SINGLE} .project-picker-item').focus()")
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
        if not page.evaluate(FOCUSED_TRIGGER_JS, alpha):
            fail("after a sidebar repaint, Escape did not return focus to the conversation's new ⋮ trigger")

    # Enter on a project moves the conversation.
    moves = []
    page.on(
        "request",
        lambda r: moves.append(r.post_data_json)
        if r.method == "POST" and urlsplit(r.url).path == "/api/session/move" else None,
    )
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    target = {"session_id": alpha, "project_id": seed["projects"][1]}
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("Enter on a project did not close the picker")
    page.wait_for_timeout(300)
    if moves != [target]:
        fail(f"Enter on a project sent {moves}, expected one move {target}")

    # Space on "No project" unassigns.
    moves.clear()
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.keyboard.press("Home")
    page.keyboard.press("Space")
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("Space on 'No project' did not close the picker")
    page.wait_for_timeout(300)
    if moves != [{"session_id": alpha, "project_id": None}]:
        fail(f"Space on 'No project' sent {moves}, expected one unassign")

    # A mouse click moves too.
    moves.clear()
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.click(f"{SINGLE} .project-picker-item:nth-child(2)")
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("a click on a project did not close the picker")
    page.wait_for_timeout(300)
    if moves != [{"session_id": alpha, "project_id": seed["projects"][0]}]:
        fail(f"a click on a project sent {moves}")

    # The two labels follow the interface language.
    page.evaluate("setLocale('de')")
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen in German: {problem}"]
    texts = [row["text"] for row in page.evaluate(PICKER_JS, SINGLE)["rows"]]
    if texts[0] != "Kein Projekt" or texts[-1] != "+ Neues Projekt":
        fail(f"in German the picker reads {texts[0]!r} … {texts[-1]!r}")
    page.keyboard.press("Escape")
    page.evaluate("setLocale('en')")
    return failures


def _open_batch_picker(page, seed):
    # Entering selection mode repaints the sidebar and, a moment later, rebuilds
    # the selection bar and replaces its buttons. That race is this driver's, not
    # the picker's: select, let the bar settle, then focus and press Enter, and
    # retry if the button was replaced all the same.
    page.evaluate(
        """({alpha, beta}) => {
          if (!_sessionSelectMode) toggleSessionSelectMode();
          setSessionSelected(alpha, true);
          setSessionSelected(beta, true);
        }""",
        seed,
    )
    page.wait_for_timeout(SETTLE_MS)
    for _attempt in range(3):
        focused = page.evaluate(
            """({alpha, beta}) => {
              if (!_sessionSelectMode) toggleSessionSelectMode();
              setSessionSelected(alpha, true);
              setSessionSelected(beta, true);
              const label = t('session_batch_move');
              const button = Array.from(document.querySelectorAll('#batchActionBar .batch-action-btn'))
                .find(btn => btn.textContent.trim() === label);
              if (!button) return false;
              button.focus();
              return document.activeElement === button;
            }""",
            seed,
        )
        if not focused:
            return "the selection bar has no focusable 'Move to project' button"
        page.keyboard.press("Enter")
        if _wait_until(page, f"!!document.querySelector('{BATCH}')", 1500):
            return None
    return "the batch picker did not open"


def _check_batch(page, seed):
    failures = []

    def fail(message):
        failures.append(f"  [batch] {message}")

    page.evaluate("setLocale('de')")
    problem = _open_batch_picker(page, seed)
    if problem:
        return [f"  [batch] {problem}"]
    picker = page.evaluate(PICKER_JS, BATCH)
    if not picker["insideBatchBar"]:
        fail("the batch picker is not inside the selection bar")
    if picker["role"] != "menu":
        fail(f"the picker's role is {picker['role']!r}, expected 'menu'")
    not_buttons = [row["text"] for row in picker["rows"] if row["tag"] != "BUTTON" or row["type"] != "button"]
    if not_buttons:
        fail(f"rows that are not <button type=button>: {not_buttons}")
    if [row["role"] for row in picker["rows"]] != ["menuitem"] * len(picker["rows"]):
        fail(f"row roles are {[row['role'] for row in picker['rows']]}")
    texts = [row["text"] for row in picker["rows"]]
    if texts != ["Kein Projekt", *PROJECTS]:
        fail(f"rows are {texts}")
    if [row["text"] for row in picker["rows"] if row["focused"]] != ["Kein Projekt"]:
        fail("focus did not open on the first row")
    page.keyboard.press("End")
    if _focused_text(page) != PROJECTS[-1]:
        fail(f"End moved focus to {_focused_text(page)!r}")
    page.keyboard.press("Escape")
    if not _wait_until(page, f"!document.querySelector('{BATCH}')"):
        fail("Escape did not close the batch picker")
    on_move_button = page.evaluate(
        "() => { const a = document.activeElement; return !!a && a.classList.contains('batch-action-btn')"
        " && a.textContent.trim() === t('session_batch_move'); }"
    )
    if not on_move_button:
        fail("after Escape focus is not on the bar's 'Move to project' button")
    page.evaluate("() => { exitSessionSelectMode(); setLocale('en'); }")
    return failures


# Opens both pickers and measures their rows in one step: a background sidebar
# refresh rebuilds the selection bar, and with it the batch picker, at any time.
ROW_HEIGHTS_JS = """({alpha, beta}) => {
  const heights = picker => Array.from(picker.querySelectorAll('.project-picker-item'))
    .map(row => Math.round(row.getBoundingClientRect().height));
  const row = document.querySelector('.session-item[data-sid="' + alpha + '"]');
  const session = _allSessions.find(s => s && s.session_id === alpha);
  if (!row || !session) return {problem: 'the conversation has no sidebar row'};
  if (!_sessionSelectMode) toggleSessionSelectMode();
  setSessionSelected(alpha, true);
  setSessionSelected(beta, true);
  const button = Array.from(document.querySelectorAll('#batchActionBar .batch-action-btn'))
    .find(btn => btn.textContent.trim() === t('session_batch_move'));
  if (!button) return {problem: 'the selection bar has no Move button'};
  _showBatchProjectPicker(button);
  const batch = document.querySelector('.batch-project-picker');
  if (!batch) return {problem: 'the batch picker did not open'};
  // Measured before the other picker opens: opening one removes every other.
  const result = {batch: heights(batch)};
  _showProjectPicker(session, document.querySelector('.session-item[data-sid="' + alpha + '"]') || row);
  const single = document.querySelector('.project-picker:not(.batch-project-picker)');
  if (!single) return {problem: 'the conversation picker did not open'};
  result.single = heights(single);
  document.querySelectorAll('.project-picker').forEach(p => p.remove());
  exitSessionSelectMode();
  return result;
}"""


def _row_heights(page, seed):
    """Row heights of both pickers on this page: (single, batch). The pickers are
    opened directly: on a phone the sidebar is a drawer, and how a picker is
    reached is the keyboard checks' business."""
    result = page.evaluate(ROW_HEIGHTS_JS, seed)
    if result.get("problem"):
        return None, None, result["problem"]
    return result["single"], result["batch"], None


def _check_heights(page, seed, *, coarse):
    label = "touch" if coarse else "mouse"
    if page.evaluate("matchMedia('(pointer:coarse)').matches") != coarse:
        return [f"  [{label}] the context's pointer is not {'coarse' if coarse else 'fine'}"]
    single, batch, problem = _row_heights(page, seed)
    if problem:
        return [f"  [{label}] {problem}"]
    failures = []
    for name, heights in (("single", single), ("batch", batch)):
        if coarse and min(heights) < MIN_TOUCH_ROW_PX:
            failures.append(f"  [touch] {name} picker rows are {heights}px tall, under {MIN_TOUCH_ROW_PX}px")
        if not coarse and max(heights) >= MIN_TOUCH_ROW_PX:
            failures.append(f"  [mouse] {name} picker rows grew to {heights}px with a fine pointer")
    return failures


def _screenshots(browser, directory, seed):
    """The open picker with many projects and long names, in German."""
    os.makedirs(directory, exist_ok=True)
    for width, height in ((390, 844), (844, 390), (1440, 900)):
        mobile = width < 1000
        ctx, page, errors = _new_page(
            browser, viewport={"width": width, "height": height}, has_touch=mobile, is_mobile=mobile
        )
        if page is None:
            print(f"screenshot {width}x{height}: {errors}", file=sys.stderr)
            continue
        page.evaluate("() => { setLocale('de'); if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM(); }")
        if mobile:
            # The sidebar is a drawer on a phone.
            page.evaluate(
                "() => { if (typeof toggleMobileSidebar === 'function'"
                " && !document.querySelector('.sidebar.mobile-open')) toggleMobileSidebar(); }"
            )
            page.wait_for_timeout(400)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(600)
        # On a phone the ⋮ menu opens from a long press on the row, so the row
        # is the anchor there; with a mouse it is the row's ⋮ trigger.
        opened = page.evaluate(
            """({sid, mobile}) => {
              const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
              const anchor = row && (mobile ? row : row.querySelector('.session-actions-trigger'));
              const session = _allSessions.find(s => s && s.session_id === sid);
              if (!anchor || !session) return false;
              _showProjectPicker(session, anchor);
              return !!document.querySelector('.project-picker');
            }""",
            {"sid": seed["alpha"], "mobile": mobile},
        )
        page.wait_for_timeout(300)
        path = os.path.join(directory, f"picker-{width}x{height}.png")
        page.screenshot(path=path)
        rows = page.evaluate(PICKER_JS, SINGLE) if opened else None
        heights = sorted({row["height"] for row in rows["rows"]}) if rows else None
        print(f"screenshot {path}: picker open={opened}, row heights={heights}")
        ctx.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    shots = None
    if "--screenshots" in sys.argv:
        shots = sys.argv[sys.argv.index("--screenshots") + 1]

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-project-picker-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
            if page is None:
                print(f"SETUP FAIL: {errors}", file=sys.stderr)
                return 2
            seed = page.evaluate(SEED_JS, {"projects": PROJECTS})
            for name, check in (("single", _check_single), ("batch", _check_batch)):
                found = check(page, seed)
                failures.extend(found)
                if not found:
                    print(f"OK  {name} picker — keyboard, focus return, translated labels")
            found = _check_heights(page, seed, coarse=False)
            failures.extend(found)
            if not found:
                print("OK  mouse — rows keep their compact height")
            failures.extend(f"  [desktop] pageerror: {err}" for err in errors)
            ctx.close()

            ctx, page, errors = _new_page(
                browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
            )
            if page is None:
                failures.append(f"  [touch] {errors}")
            else:
                page.evaluate("renderSessionList()")
                page.wait_for_timeout(500)
                found = _check_heights(page, seed, coarse=True)
                failures.extend(found)
                if not found:
                    print(f"OK  touch — every row is at least {MIN_TOUCH_ROW_PX}px tall")
                failures.extend(f"  [touch] pageerror: {err}" for err in errors)
                ctx.close()

            if shots:
                ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
                if page is not None:
                    page.evaluate(
                        """async (names) => {
                          for (const name of names) {
                            await fetch('/api/projects/create', {
                              method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify({name, color: '#f5c542'}),
                            });
                          }
                        }""",
                        LONG_PROJECTS,
                    )
                    ctx.close()
                _screenshots(browser, shots, seed)
            browser.close()

        if failures:
            print("\nPROJECT PICKER KEYBOARD FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nPROJECT PICKER KEYBOARD PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
