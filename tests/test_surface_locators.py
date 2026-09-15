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
