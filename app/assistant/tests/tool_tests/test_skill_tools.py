"""Tests for discover_skills + read_skill — the skill-catalog tools.

Both tools delegate to ``DI.skill_registry``. Tests mock the registry with
fixed fake skills so behavior stays stable as the on-disk skill set evolves.

Coverage:
  discover_skills
    - search mode returns ranked matches
    - browse mode (no query) lists everything alphabetically
    - query with no matches returns ok result with empty list
    - limit caps results (default + max enforcement)
    - non-int limits coerce sensibly
    - data shape: query/mode/count/total_available/skills
    - auto_inject_keywords surfaced in payload

  read_skill
    - single name found returns body
    - list of names returns all bodies
    - mixed found/missing reports missing[]
    - all-missing returns found=[] with missing populated
    - duplicate names dedup with order preserved
    - empty names list → invalid_arguments
    - names=None / wrong type → invalid_arguments
    - single string accepted (not just list)
    - more than cap → too_many_names error
"""
from __future__ import annotations

import unittest
from typing import List, Optional

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.lib.tools.discover_skills.discover_skills import DiscoverSkillsTool
from app.assistant.lib.tools.read_skill.read_skill import ReadSkillTool
from app.assistant.utils.pydantic_classes import ToolMessage
from app.skill_registry.models import AutoInjectTrigger, Skill, SkillHeader


def _tm(tool_name: str, args: dict) -> ToolMessage:
    return ToolMessage(
        request_id=f"req-{tool_name}",
        tool_name=tool_name,
        tool_data={"arguments": args},
    )


def _mk_header(name: str, description: str, keywords: List[str] | None = None) -> SkillHeader:
    trigger = AutoInjectTrigger(task_keywords=keywords) if keywords else None
    return SkillHeader(name=name, description=description, auto_inject_when=trigger)


def _mk_skill(name: str, description: str, body: str, keywords: List[str] | None = None) -> Skill:
    trigger = AutoInjectTrigger(task_keywords=keywords) if keywords else None
    return Skill(name=name, description=description, body=body, auto_inject_when=trigger)


class _FakeRegistry:
    """Minimal SkillRegistry stand-in: in-memory dict + the three methods
    the tools actually call (headers / discover / get)."""

    def __init__(self, skills: List[Skill]) -> None:
        self._skills = {s.name: s for s in skills}

    def headers(self) -> List[SkillHeader]:
        return [
            SkillHeader(
                name=s.name,
                description=s.description,
                auto_inject_when=s.auto_inject_when,
            )
            for s in self._skills.values()
        ]

    def discover(self, query: str, limit: int = 5) -> List[SkillHeader]:
        """Trivial substring scoring — not the real implementation, but
        deterministic enough to test the tool's wrapping logic."""
        q = (query or "").lower()
        scored = []
        for s in self._skills.values():
            score = 0
            if q in s.name.lower():
                score += 3
            if q in s.description.lower():
                score += 1
            if score:
                scored.append((-score, s.name, SkillHeader(
                    name=s.name, description=s.description, auto_inject_when=s.auto_inject_when,
                )))
        scored.sort()
        return [h for _, _, h in scored[:limit]]

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)


# Fixture: three fake skills with overlapping vocabulary for ranking tests.
_FAKE_SKILLS = [
    _mk_skill(
        "image-resize",
        "Resize, thumbnail, crop, or convert image format.",
        "# image-resize body\nLong body for image-resize.",
        keywords=["resize image", "thumbnail", "crop image"],
    ),
    _mk_skill(
        "github",
        "Query and interact with GitHub via api.github.com.",
        "# github body\nLong body for github.",
        keywords=["github", "PR", "pull request"],
    ),
    _mk_skill(
        "pod-courier",
        "How pods land in execute_code's workspace.",
        "# pod-courier body\nLong body for pod-courier.",
        keywords=None,  # static-bind only, no triggers
    ),
]


class _RegistryPatch(unittest.TestCase):
    """Base test class that swaps DI.skill_registry with a fake for the
    duration of each test method."""

    fake_skills: List[Skill] = _FAKE_SKILLS

    def setUp(self) -> None:
        self._orig_registry = ServiceLocator.get("skill_registry")
        self._fake_registry = _FakeRegistry(self.fake_skills)
        ServiceLocator.register("skill_registry", self._fake_registry)

    def tearDown(self) -> None:
        ServiceLocator.register("skill_registry", self._orig_registry)


class TestDiscoverSkillsSearch(_RegistryPatch):
    def test_query_returns_matches(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"query": "image"}))

        self.assertEqual(res.data["mode"], "search")
        self.assertEqual(res.data["query"], "image")
        self.assertGreater(res.data["count"], 0)
        # image-resize should rank first (name + description hit).
        self.assertEqual(res.data["skills"][0]["name"], "image-resize")

    def test_query_with_no_matches_returns_empty(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"query": "xyzzy-no-such"}))

        self.assertEqual(res.data["mode"], "search")
        self.assertEqual(res.data["count"], 0)
        self.assertEqual(res.data["skills"], [])
        self.assertIn("No skills matched", res.content)

    def test_search_surfaces_auto_inject_keywords(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"query": "image"}))

        top = res.data["skills"][0]
        self.assertEqual(top["name"], "image-resize")
        self.assertIn("resize image", top["auto_inject_keywords"])

    def test_no_triggers_returns_empty_keyword_list(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"query": "pod-courier"}))
        # pod-courier has no auto_inject_when block.
        target = next(s for s in res.data["skills"] if s["name"] == "pod-courier")
        self.assertEqual(target["auto_inject_keywords"], [])


class TestDiscoverSkillsBrowse(_RegistryPatch):
    def test_no_query_returns_all_alphabetical(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {}))

        self.assertEqual(res.data["mode"], "browse")
        self.assertEqual(res.data["query"], "")
        self.assertEqual(res.data["count"], 3)
        self.assertEqual(res.data["total_available"], 3)
        names = [s["name"] for s in res.data["skills"]]
        self.assertEqual(names, sorted(names))

    def test_browse_respects_limit(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"limit": 2}))

        self.assertEqual(res.data["count"], 2)
        self.assertEqual(res.data["total_available"], 3)
        # First two alphabetically: github, image-resize
        self.assertEqual([s["name"] for s in res.data["skills"]], ["github", "image-resize"])

    def test_browse_when_empty_registry(self):
        # Re-patch with empty registry.
        ServiceLocator.register("skill_registry", _FakeRegistry([]))
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {}))

        self.assertEqual(res.data["count"], 0)
        self.assertEqual(res.data["total_available"], 0)
        self.assertIn("No skills are registered", res.content)


class TestDiscoverSkillsLimit(_RegistryPatch):
    def test_default_limit_is_5(self):
        # Build a registry with 12 skills so default-5 should clamp.
        many = [_mk_skill(f"skill-{i:02d}", "...", "...") for i in range(12)]
        ServiceLocator.register("skill_registry", _FakeRegistry(many))

        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {}))
        self.assertEqual(res.data["count"], 5)
        self.assertEqual(res.data["total_available"], 12)

    def test_explicit_limit_above_max_clamps_to_50(self):
        many = [_mk_skill(f"skill-{i:02d}", "...", "...") for i in range(80)]
        ServiceLocator.register("skill_registry", _FakeRegistry(many))

        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"limit": 999}))
        self.assertEqual(res.data["count"], 50)

    def test_zero_or_negative_limit_falls_back_to_default(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"limit": 0}))
        # Only 3 skills exist, but the point is no crash and a sane count.
        self.assertEqual(res.data["count"], 3)

    def test_non_int_limit_falls_back_to_default(self):
        tool = DiscoverSkillsTool()
        res = tool.execute(_tm("discover_skills", {"limit": "not-a-number"}))
        self.assertEqual(res.data["count"], 3)


class TestReadSkillSingle(_RegistryPatch):
    def test_single_name_returns_body(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {"names": ["image-resize"]}))

        self.assertEqual(res.data["found_count"], 1)
        self.assertEqual(res.data["missing"], [])
        self.assertEqual(res.data["found"][0]["name"], "image-resize")
        self.assertIn("image-resize body", res.data["found"][0]["body"])
        self.assertEqual(res.data["found"][0]["body_chars"], len(res.data["found"][0]["body"]))

    def test_single_name_as_bare_string(self):
        # The pydantic form says list, but the runtime accepts a bare string
        # for robustness against arg-shape drift.
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {"names": "image-resize"}))
        self.assertEqual(res.data["found_count"], 1)
        self.assertEqual(res.data["found"][0]["name"], "image-resize")


class TestReadSkillMulti(_RegistryPatch):
    def test_multiple_names_all_found(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {"names": ["image-resize", "github"]}))
        self.assertEqual(res.data["found_count"], 2)
        self.assertEqual(res.data["missing"], [])
        names = [s["name"] for s in res.data["found"]]
        self.assertEqual(names, ["image-resize", "github"])  # order preserved

    def test_mixed_found_and_missing(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {
            "names": ["nope-1", "image-resize", "nope-2"],
        }))
        self.assertEqual(res.data["found_count"], 1)
        self.assertEqual(res.data["missing"], ["nope-1", "nope-2"])
        self.assertEqual(res.data["found"][0]["name"], "image-resize")

    def test_all_missing(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {"names": ["nope-1", "nope-2"]}))
        self.assertEqual(res.data["found_count"], 0)
        self.assertEqual(res.data["missing"], ["nope-1", "nope-2"])
        self.assertEqual(res.data["found"], [])

    def test_duplicates_deduped_with_order(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {
            "names": ["image-resize", "github", "image-resize"],
        }))
        # Dedup keeps first occurrence; both are real, so found_count=2.
        self.assertEqual(res.data["found_count"], 2)
        names = [s["name"] for s in res.data["found"]]
        self.assertEqual(names, ["image-resize", "github"])


class TestReadSkillErrors(_RegistryPatch):
    def test_empty_names_errors_clearly(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {"names": []}))
        self.assertEqual(res.result_type, "error")
        self.assertEqual(res.data["error_code"], "invalid_arguments")

    def test_missing_names_field_errors_clearly(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {}))
        self.assertEqual(res.result_type, "error")
        self.assertEqual(res.data["error_code"], "invalid_arguments")

    def test_wrong_type_errors_clearly(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {"names": 42}))
        self.assertEqual(res.result_type, "error")
        self.assertEqual(res.data["error_code"], "invalid_arguments")

    def test_whitespace_only_names_treated_as_empty(self):
        tool = ReadSkillTool()
        res = tool.execute(_tm("read_skill", {"names": ["   ", "\t"]}))
        self.assertEqual(res.result_type, "error")
        self.assertEqual(res.data["error_code"], "invalid_arguments")

    def test_over_cap_errors(self):
        tool = ReadSkillTool()
        # Use 6 unique names so dedup doesn't paper over the cap.
        res = tool.execute(_tm("read_skill", {
            "names": ["a", "b", "c", "d", "e", "f"],
        }))
        self.assertEqual(res.result_type, "error")
        self.assertEqual(res.data["error_code"], "too_many_names")


if __name__ == "__main__":
    unittest.main()
