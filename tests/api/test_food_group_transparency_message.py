from api.app import _build_food_group_selection_explanations


def test_transparency_message_added_when_group_gap_is_closed():
    messages = _build_food_group_selection_explanations(
        missing_groups_before=["vegetables", "whole_grains"],
        missing_groups_after=["whole_grains"],
    )
    assert len(messages) == 1
    assert "vegetables" in messages[0]


def test_transparency_message_handles_multiple_closed_groups():
    messages = _build_food_group_selection_explanations(
        missing_groups_before=["vegetables", "whole_grains", "fruits"],
        missing_groups_after=["fruits"],
    )
    assert len(messages) == 1
    assert "vegetables" in messages[0]
    assert "whole grains" in messages[0]


def test_transparency_message_omitted_when_no_groups_closed():
    messages = _build_food_group_selection_explanations(
        missing_groups_before=["vegetables"],
        missing_groups_after=["vegetables"],
    )
    assert messages == []
