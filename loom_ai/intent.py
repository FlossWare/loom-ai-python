"""Declarative Intent model and Markdown adapter.

Intent describes what should be accomplished without prescribing how Loom
executes it.  Markdown is an input/output representation, not the runtime
contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


class IntentParseError(ValueError):
    """Raised when Markdown cannot be converted into an Intent."""


@dataclass(frozen=True)
class Intent:
    """Declarative description of a desired outcome."""

    goal: str
    requirements: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    title: str = "Intent"
    intent_id: str = field(default_factory=lambda: str(uuid4()))
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("Intent goal must not be empty")
        for name, values in (
            ("requirements", self.requirements),
            ("constraints", self.constraints),
            ("acceptance", self.acceptance),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"Intent {name} must not contain empty items")

    @classmethod
    def from_markdown(cls, markdown: str, *, intent_id: str | None = None) -> "Intent":
        """Parse the supported human-authored Intent Markdown shape."""
        lines = [line.rstrip() for line in markdown.strip().splitlines()]
        if not lines or not lines[0].startswith("# "):
            raise IntentParseError("Intent Markdown must start with a level-one title")

        title = lines[0][2:].strip()
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in lines[1:]:
            if line.startswith("## "):
                current = line[3:].strip().lower()
                sections.setdefault(current, [])
                continue
            if current is None:
                if line.strip():
                    raise IntentParseError("Content before the first Intent section")
                continue
            if line.startswith("- "):
                sections[current].append(line[2:].strip())
            elif line.strip():
                sections[current].append(line.strip())

        goal_items = sections.get("goal", [])
        if not goal_items:
            raise IntentParseError("Intent Markdown must contain a Goal section")
        return cls(
            title=title or "Intent",
            goal="\n".join(goal_items),
            requirements=tuple(sections.get("requirements", [])),
            constraints=tuple(sections.get("constraints", [])),
            acceptance=tuple(sections.get("acceptance", [])),
            intent_id=intent_id or str(uuid4()),
        )

    def to_markdown(self) -> str:
        """Serialize the semantic Intent representation to Markdown."""
        lines = [f"# {self.title}", "", "## Goal", "", self.goal]
        for heading, values in (
            ("Requirements", self.requirements),
            ("Constraints", self.constraints),
            ("Acceptance", self.acceptance),
        ):
            if values:
                lines.extend(["", f"## {heading}", ""])
                lines.extend(f"- {value}" for value in values)
        return "\n".join(lines) + "\n"
