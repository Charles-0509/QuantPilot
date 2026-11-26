from app.schemas import RuleDefinition
from app.templates import TEMPLATES


def test_all_templates_validate() -> None:
    assert len(TEMPLATES) == 13
    for definition in TEMPLATES.values():
        RuleDefinition.model_validate(definition)
    kinds = {key: (definition["direction"], definition["instrument"]) for key, definition in TEMPLATES.items()}
    assert kinds["trend_breakdown_short"] == ("short", "stock")
    assert kinds["breakout_call_option"] == ("long", "option")
    assert sum(kind == ("long", "stock") for kind in kinds.values()) == 11


def test_googl_templates_are_single_symbol_and_default_to_research_safe_sizing() -> None:
    googl_templates = [definition for key, definition in TEMPLATES.items() if key.startswith("googl_")]
    assert len(googl_templates) == 4
    for definition in googl_templates:
        assert definition["symbols"] == ["GOOGL"]
        assert definition["risk"]["max_positions"] == 1
        assert definition["risk"]["max_symbol_pct"] <= 8
