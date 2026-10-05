from deskhand.snapshot import Snapshot, chunks, role_words, visible_text

TREE = (
    '- [0] AXWindow "Work"\n  - [1] AXOutline (sidebar)\n    - [2] AXRow\n      - [3] AXCell\n'
    '        - AXStaticText = "Recents"\n    - [4] AXRow\n      - AXStaticText = "Favorites"\n'
)
ELEMENTS = [
    {
        "element_index": 0,
        "element_token": "a",
        "role": "AXWindow",
        "label": "Work",
        "screenshot_frame": {"x": 0, "y": 0, "w": 900, "h": 700},
    },
    {
        "element_index": 3,
        "element_token": "b",
        "role": "AXCell",
        "screenshot_frame": {"x": 0, "y": 80, "w": 300, "h": 40},
    },
    {
        "element_index": 5,
        "element_token": "c",
        "role": "AXPopUpButton",
        "label": "Sort",
        "screenshot_frame": {"x": 400, "y": 10, "w": 60, "h": 20},
    },
]


def test_elements_by_token_and_by_position():
    snap = Snapshot.from_structured(
        {"app_name": "Finder", "window_title": "Work", "elements": ELEMENTS, "tree_markdown": TREE}
    )
    assert snap is not None and (snap.app, snap.window) == ("Finder", "Work")
    cell, sort = snap.by_token("b"), snap.at(410, 15)
    assert cell is not None and sort is not None
    assert snap.describe(cell) == 'cell "Recents"'  # words from the child line
    assert snap.describe(sort) == 'pop up button "Sort"'  # the smallest frame wins
    assert snap.at(5000, 5000) is None
    assert Snapshot.from_structured({"no": "elements"}) is None
    assert role_words("AXSecureTextField") == "secure text field"


def test_visible_text_and_chunks():
    assert visible_text(TREE) == "Work\nRecents\nFavorites"
    assert visible_text("plain words only") == "plain words only"
    pieces = chunks("a" * 2500 + "\nshort\nlines", size=1000)
    assert [len(p) for p in pieces] == [1000, 1000, 512]  # short lines join the remainder
    assert pieces[-1].endswith("a\nshort\nlines")


def test_tree_markup_never_reaches_laya():
    tree = (
        '- [0] AXWindow "Open" [id=open-panel actions=[raise]]\n    - [66] AXButton "New Document"'
    )
    assert visible_text(tree) == "Open\nNew Document"
    assert visible_text("- [0] AXWindow [actions=[raise]]") == ""
