from cua.models import Locator
from cua.surface import BrowserSurface


def test_stable_locator_prefers_label_then_role_then_named_css():
    assert BrowserSurface._stable_locator(
        {"label": "Member ID", "role": "input", "text": "", "aria": "", "name": "member", "tag": "input", "id": "", "href": ""}
    ).strategy == "label"
    assert BrowserSurface._stable_locator(
        {"label": "", "role": "button", "text": "Search", "aria": "", "name": "", "tag": "input", "id": "", "href": ""}
    ).value == "button:Search"
    assert BrowserSurface._stable_locator(
        {"label": "", "role": "input", "text": "", "aria": "", "name": "member", "tag": "input", "id": "", "href": ""}
    ).value == 'input[name="member"]'
