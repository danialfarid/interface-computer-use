import json

from cua.models import Locator
from cua.surface import BrowserSurface


def test_stable_locator_prefers_label_then_stable_dom_metadata_then_role():
    assert BrowserSurface._stable_locator(
        {"label": "Member ID", "role": "input", "text": "", "aria": "", "name": "member", "tag": "input", "id": "", "href": ""}
    ).strategy == "label"
    assert BrowserSurface._stable_locator(
        {"label": "", "role": "button", "text": "Search", "aria": "", "name": "", "tag": "input", "id": "", "href": ""}
    ).value == "button:Search"
    assert BrowserSurface._stable_locator(
        {"label": "", "role": "input", "text": "", "aria": "", "name": "member", "tag": "input", "id": "", "href": ""}
    ).value == 'input[name="member"]'
    assert BrowserSurface._stable_locator(
        {"label": "", "role": "input", "text": "Member ID", "aria": "Member ID", "name": "", "tag": "input", "id": "", "href": ""}
    ).value == 'input[aria-label="Member ID"]'
    assert BrowserSurface._stable_locator(
        {"label": "", "role": "link", "text": "Return to lookup", "aria": "", "name": "", "tag": "a", "id": "", "href": "/"}
    ).value == 'a[href="/"]'


def test_stable_locator_quotes_punctuation_in_dom_ids():
    locator = BrowserSurface._stable_locator(
        {"label": "", "role": "cell", "text": "$1,240.50", "aria": "", "name": "", "tag": "td", "id": "balance.value", "href": ""}
    )

    assert locator.value == '[id="balance.value"]'


def test_capture_persists_structural_state_without_page_text(tmp_path):
    class FakeLocator:
        def evaluate(self, _script):
            return {
                "tag": "body",
                "id": "",
                "role": "",
                "state": {},
                "error_marker": False,
                "children": [
                    {
                        "tag": "input",
                        "id": "member-number",
                        "role": "textbox",
                        "state": {"invalid": "true"},
                        "error_marker": True,
                        "children": [],
                        "text": "José Núñez",
                        "value": "123456",
                    }
                ],
            }

    class FakePage:
        def locator(self, _selector):
            return FakeLocator()

    surface = object.__new__(BrowserSurface)
    surface.page = FakePage()
    surface._sensitive_values = {"123456"}
    _screenshot, snapshot = surface.capture(tmp_path, "failure")
    structure = json.loads(snapshot.read_text(encoding="utf-8"))
    serialized = json.dumps(structure, ensure_ascii=False)

    assert "José Núñez" not in serialized
    assert "123456" not in serialized
    assert structure["children"][0]["id"] == "member-number"
    assert structure["children"][0]["error_marker"] is True
    assert "text" not in structure["children"][0]
    assert "value" not in structure["children"][0]
