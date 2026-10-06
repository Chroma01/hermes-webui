"""#8044 — the "Move to project" pickers: keyboard access, translated labels,
finger-sized rows.

Both pickers (`_showProjectPicker` for one conversation, `_showBatchProjectPicker`
for a selection) built their rows as click-only ``<div>``s, wrote "No project" and
"+ New project" in English whatever the interface language, and kept a 24px row
on a touch screen.

The row helpers are run here in node against a small stand-in DOM, so the key
handling and the focus-return target are exercised, not only read. The real
pickers in a real browser are tests/browser_project_picker_keyboard.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = REPO / "static" / "i18n.js"

LOCALE_CODES = [
    "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko", "fr", "cs", "tr", "pl", "vi",
]


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


BATCH_PICKER = _between(SESSIONS_JS, "function _showBatchProjectPicker(", "function _focusSessionActionMenuRestoreTarget(")
SINGLE_PICKER = _between(SESSIONS_JS, "function _showProjectPicker(", "// ── Project picker rows and keyboard")
HELPERS = _between(SESSIONS_JS, "// ── Project picker rows and keyboard", "// Resize a .project-create-input")
FOCUS_HELPER = _between(
    SESSIONS_JS, "function _focusSessionActionMenuRestoreTarget(", "function closeSessionActionMenu("
)

# A DOM small enough to read: elements with attributes, children, a parent,
# focus, and the two lookups the helpers use.
FAKE_DOM = r"""
let active = null;
const body = {tagName: 'BODY'};
class El {
  constructor(tag){ this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.className = '';
    this.isConnected = true; this.listeners = {}; this.disabled = false; }
  setAttribute(name, value){ this.attrs[name] = String(value); }
  getAttribute(name){ return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  appendChild(child){ this.children.push(child); child.parent = this; return child; }
  addEventListener(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, event){ (this.listeners[type] || []).forEach(fn => fn(event)); }
  focus(){ if (this.isConnected && !this.unfocusable) active = this; }
  hasClass(name){ return this.className.split(/\s+/).includes(name); }
  querySelectorAll(selector){
    if (selector === '.project-picker-item:not([disabled])')
      return this.children.filter(c => c.hasClass('project-picker-item') && !c.disabled);
    throw new Error('unexpected selector ' + selector);
  }
  querySelector(selector){
    if (selector === '.project-picker-item.active')
      return this.children.find(c => c.hasClass('project-picker-item') && c.hasClass('active')) || null;
    if (selector === '.project-picker-item')
      return this.children.find(c => c.hasClass('project-picker-item')) || null;
    if (selector === '.session-actions-trigger') return this.trigger || null;
    throw new Error('unexpected selector ' + selector);
  }
}
const rowsBySid = {};
const document = {
  createElement: tag => new El(tag),
  get activeElement(){ return active || body; },
  body,
};
function _findSessionRenameRow(sid){ return rowsBySid[String(sid || '')] || null; }
function key(name){
  const event = {key: name, prevented: false, stopped: false,
    preventDefault(){ this.prevented = true; }, stopPropagation(){ this.stopped = true; }};
  return event;
}
"""


def _run(body: str):
    script = FAKE_DOM + FOCUS_HELPER + HELPERS + body
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


PICKER_WITH_ROWS = r"""
const picker = new El('div');
const labels = ['No project', 'Research', 'Client work', '+ New project'];
const rows = labels.map((label, index) => {
  const row = _projectPickerItem(index === 3 ? 'project-picker-create' : '', index === 3 ? undefined : index === 1);
  row.label = label;
  return picker.appendChild(row);
});
let escaped = 0;
_wireProjectPickerKeys(picker, () => { escaped += 1; });
const press = name => { const event = key(name); picker.dispatch('keydown', event); return event; };
const focused = () => (active && active.label) || null;
"""


def test_a_row_is_a_button_with_a_menu_role():
    out = _run(PICKER_WITH_ROWS + r"""
console.log(JSON.stringify({
  picker: picker.getAttribute('role'),
  rows: rows.map(row => ({tag: row.tagName, type: row.type, role: row.getAttribute('role'),
                          checked: row.getAttribute('aria-checked'), cls: row.className})),
}));
""")
    assert out["picker"] == "menu"
    assert [row["tag"] for row in out["rows"]] == ["BUTTON"] * 4
    assert [row["type"] for row in out["rows"]] == ["button"] * 4
    # The single picker's choices are radio items; the create row is an action.
    assert [row["role"] for row in out["rows"]] == ["menuitemradio"] * 3 + ["menuitem"]
    assert [row["checked"] for row in out["rows"]] == ["false", "true", "false", None]
    assert out["rows"][1]["cls"] == "project-picker-item active"
    assert out["rows"][3]["cls"] == "project-picker-item project-picker-create"


def test_a_batch_row_has_no_checked_state():
    """Several conversations have no one current project."""
    out = _run(r"""
const row = _projectPickerItem();
console.log(JSON.stringify({role: row.getAttribute('role'), checked: row.getAttribute('aria-checked'),
                            cls: row.className}));
""")
    assert out == {"role": "menuitem", "checked": None, "cls": "project-picker-item"}


def test_focus_opens_on_the_current_project_else_on_the_first_row():
    out = _run(PICKER_WITH_ROWS + r"""
_focusProjectPickerItem(picker);
const withCurrent = focused();
rows[1].className = 'project-picker-item';
active = null;
_focusProjectPickerItem(picker);
console.log(JSON.stringify({withCurrent, without: focused()}));
""")
    assert out == {"withCurrent": "Research", "without": "No project"}


def test_arrows_wrap_and_home_end_jump():
    out = _run(PICKER_WITH_ROWS + r"""
rows[1].focus();
const seen = [];
for (const name of ['ArrowDown', 'ArrowDown', 'ArrowDown', 'ArrowUp', 'ArrowUp', 'Home', 'End', 'Home']) {
  const event = press(name);
  seen.push([name, focused(), event.prevented]);
}
console.log(JSON.stringify(seen));
""")
    assert out == [
        ["ArrowDown", "Client work", True],
        ["ArrowDown", "+ New project", True],
        ["ArrowDown", "No project", True],
        ["ArrowUp", "+ New project", True],
        ["ArrowUp", "Client work", True],
        ["Home", "No project", True],
        ["End", "+ New project", True],
        ["Home", "No project", True],
    ]


def test_other_keys_are_left_to_the_button():
    """Enter and Space activate a button natively; Tab moves on as usual."""
    out = _run(PICKER_WITH_ROWS + r"""
rows[0].focus();
const seen = ['Enter', ' ', 'Tab', 'a'].map(name => { const event = press(name); return [event.prevented, focused()]; });
console.log(JSON.stringify({seen, escaped}));
""")
    assert out == {"seen": [[False, "No project"]] * 4, "escaped": 0}


def test_escape_runs_the_close_handler_and_goes_no_further():
    out = _run(PICKER_WITH_ROWS + r"""
rows[2].focus();
const event = press('Escape');
console.log(JSON.stringify({escaped, prevented: event.prevented, stopped: event.stopped}));
""")
    assert out == {"escaped": 1, "prevented": True, "stopped": True}


def test_focus_returns_to_the_trigger_the_picker_opened_from():
    out = _run(r"""
const trigger = new El('button'); trigger.label = 'original';
console.log(JSON.stringify(_projectPickerFocusReturnTarget({session_id: 'sa'}, trigger).label));
""")
    assert out == "original"


def test_after_a_repaint_focus_returns_to_the_rows_new_trigger():
    """The sidebar is rebuilt on every refresh, which detaches the trigger the
    picker opened from. The conversation's row is looked up again by its id."""
    out = _run(r"""
const original = new El('button'); original.isConnected = false;
const row = new El('div'); row.trigger = new El('button'); row.trigger.label = 'repainted';
rowsBySid['sa'] = row;
const found = _projectPickerFocusReturnTarget({session_id: 'sa'}, original);
const gone = _projectPickerFocusReturnTarget({session_id: 'filtered-away'}, original);
console.log(JSON.stringify({found: found && found.label, gone}));
""")
    assert out == {"found": "repainted", "gone": None}


def test_both_pickers_build_every_row_with_the_helper():
    assert "createElement('div');none" not in BATCH_PICKER
    assert BATCH_PICKER.count("_projectPickerItem(") == 2
    assert SINGLE_PICKER.count("_projectPickerItem(") == 3
    # The class is assigned in one place only: the helper.
    assert SESSIONS_JS.count("className='project-picker-item'") == 1
    assert "className='project-picker-item'" in HELPERS


def test_both_pickers_wire_the_keys_and_focus_a_row_on_open():
    for name, body in (("batch", BATCH_PICKER), ("single", SINGLE_PICKER)):
        assert "_wireProjectPickerKeys(picker," in body, name
        assert "_focusProjectPickerItem(picker)" in body, name
    assert "_focusSessionActionMenuRestoreTarget(openerEl)" in BATCH_PICKER
    assert "_focusSessionActionMenuRestoreTarget(_projectPickerFocusReturnTarget(session,anchorEl))" in SINGLE_PICKER
    # The selection bar hands its Move button over as the place to return to.
    assert "_showBatchProjectPicker(moveBtn)" in SESSIONS_JS


def test_the_two_labels_go_through_t():
    assert "'No project'" not in SESSIONS_JS
    assert "'+ New project'" not in SESSIONS_JS
    assert "t('project_picker_none')" in BATCH_PICKER
    assert "t('project_picker_none')" in SINGLE_PICKER
    assert "t('project_picker_new')" in SINGLE_PICKER


@pytest.fixture(scope="module")
def picker_labels():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const context = {
  localStorage: {getItem(){ return null; }, setItem(){}},
  document: {documentElement: {}, addEventListener(){}, querySelectorAll(){ return []; }},
  navigator: {language: 'en', languages: ['en']},
  console,
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\n;this.__locales = LOCALES;', context);
const out = {};
for (const [code, table] of Object.entries(context.__locales))
  out[code] = [table.project_picker_none, table.project_picker_new];
console.log(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "-e", script, str(I18N_JS)], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def test_every_locale_has_both_labels(picker_labels):
    assert sorted(picker_labels) == sorted(LOCALE_CODES)
    for code, (none, new) in picker_labels.items():
        assert isinstance(none, str) and none.strip(), code
        assert isinstance(new, str) and new.startswith("+ ") and new[2:].strip(), code
    assert picker_labels["en"] == ["No project", "+ New project"]


def test_no_other_locale_repeats_the_english(picker_labels):
    english = picker_labels["en"]
    for code, labels in picker_labels.items():
        if code == "en":
            continue
        assert labels[0] != english[0], code
        assert labels[1] != english[1], code


def test_a_row_is_a_finger_tall_on_a_touch_screen():
    assert "@media (pointer:coarse){.project-picker-item{min-height:44px;" in STYLE_CSS


def test_taller_rows_scroll_inside_the_picker():
    """44px rows make a long project list taller than a phone's screen."""
    rule = _between(STYLE_CSS, ".project-picker{max-height:", "}")
    assert "calc(100dvh - 16px)" in rule
    assert "overflow-y:auto" in rule


def test_a_focused_row_can_be_seen():
    assert ".project-picker-item:focus-visible{" in STYLE_CSS
    rule = _between(STYLE_CSS, ".project-picker-item:focus-visible{", "}")
    assert "outline:2px solid var(--focus-ring)" in rule
    # The current project keeps its colour under focus, as it does under hover.
    assert ".project-picker-item.active:focus-visible{color:var(--blue);}" in STYLE_CSS


def test_the_button_keeps_the_rows_look():
    """A bare <button> brings its own border, background, font and centring. The
    reset has no specificity and sits before the row rules, so the create row's
    top border and every hover background still win over it."""
    reset = ":where(button.project-picker-item){"
    assert reset in STYLE_CSS
    rule = _between(STYLE_CSS, reset, "}")
    for declaration in ("width:100%", "background:none", "border:none", "font:inherit", "text-align:left"):
        assert declaration in rule, declaration
    assert STYLE_CSS.index(reset) < STYLE_CSS.index(".project-picker-item{padding:")
    assert STYLE_CSS.index(reset) < STYLE_CSS.index(".project-picker-create{")
