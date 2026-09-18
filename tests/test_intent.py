from loom_ai.intent import Intent, IntentParseError


def test_intent_markdown_round_trip_preserves_semantics() -> None:
    markdown = """# Add Redis queues

## Goal

Build a Redis-backed queue implementation.

## Requirements

- Use the public queue contract.
- Preserve provenance.

## Constraints

- Do not change the public API.

## Acceptance

- Unit tests pass.
- Integration tests exercise Redis.
"""

    intent = Intent.from_markdown(markdown, intent_id="intent-1")
    restored = Intent.from_markdown(intent.to_markdown(), intent_id=intent.intent_id)

    assert restored == intent
    assert restored.intent_id == "intent-1"


def test_intent_requires_goal() -> None:
    try:
        Intent.from_markdown("# Missing goal\n\n## Requirements\n\n- Something")
    except IntentParseError as exc:
        assert "Goal" in str(exc)
    else:
        raise AssertionError("missing Goal section should fail")
