from app.schemas import RuleDefinition
from app.templates import TEMPLATES


def test_all_templates_validate() -> None:
    assert len(TEMPLATES) == 11
    for definition in TEMPLATES.values():
        RuleDefinition.model_validate(definition)


def test_googl_templates_are_single_symbol_and_default_to_research_safe_sizing() -> None:
    googl_templates = [definition for key, definition in TEMPLATES.items() if key.startswith("googl_")]
    assert len(googl_templates) == 4
    for definition in googl_templates:
        assert definition["symbols"] == ["GOOGL"]
        assert definition["risk"]["max_positions"] == 1
        assert definition["risk"]["max_symbol_pct"] <= 8
