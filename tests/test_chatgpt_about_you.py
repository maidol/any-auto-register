"""Regression tests for ChatGPT about_you age-field handling."""

from platforms.chatgpt import browser_register as br


def test_about_you_input_hints_include_japanese_age_label():
    hints = br._about_you_input_hints(
        {
            "visibleIndex": 1,
            "name": "age",
            "id": "_r_i_-age",
            "labels": ["年齢"],
        }
    )
    assert "年齢" in hints


def test_japanese_age_label_is_selected_as_age_input():
    entries = [
        {"visibleIndex": 0, "name": "name", "labels": ["氏名"]},
        {"visibleIndex": 1, "name": "age", "id": "_r_i_-age", "labels": ["年齢"]},
    ]
    name_entry = br._pick_best_about_you_input(entries, "name")
    age_entry = br._pick_best_about_you_input(
        entries, "age", exclude_visible_indices={name_entry["visibleIndex"]}
    )
    assert name_entry["visibleIndex"] == 0
    assert age_entry["visibleIndex"] == 1


def test_about_you_mode_detects_japanese_age_page():
    mode = br._detect_about_you_mode(
        {"hasAge": True, "hasBirthday": False},
        has_age_field=True,
        has_birthday_field=False,
        has_birthday_select=False,
    )
    assert mode == "age"


def test_about_you_age_mode_requires_age_result():
    assert not br._about_you_fill_complete(
        "age", {"name": True, "age": False, "birthdate": True}
    )
    assert br._about_you_fill_complete(
        "age", {"name": True, "age": True, "birthdate": False}
    )


def test_about_you_birthday_mode_keeps_birthday_completion():
    assert br._about_you_fill_complete(
        "birthday", {"name": True, "age": False, "birthdate": True}
    )
