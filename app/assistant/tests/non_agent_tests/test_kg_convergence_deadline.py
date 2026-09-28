"""kg_convergence._bfs_from_seed honours a wall-time deadline: past it, the walk returns what it has.
Fake session: every query returns the same small edge set, which is enough to show the loop expands
without a deadline and stops with one."""
import time
from types import SimpleNamespace

from app.assistant.kg_core.kg_utils import kg_convergence as kc


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _FakeSession:
    """A star of Entity nodes around the seed; filters are ignored, so every node sees all edges."""

    def __init__(self, n=6):
        self.nodes = {f"n{i}": SimpleNamespace(id=f"n{i}", label=f"node {i}", node_type="Entity",
                                                description=None, importance=0.5, aliases=None,
                                                attributes={}, start_date=None, end_date=None)
                      for i in range(n)}
        self.edges = [SimpleNamespace(source_id="n0", target_id=f"n{i}", importance=0.8, updated_at=None)
                      for i in range(1, n)]

    def query(self, model, *cols):
        if model is kc.Edge or getattr(model, "class_", None) is kc.Edge or "Edge" in repr(model):
            return _Query(self.edges)
        return _Query(list(self.nodes.values()))

    def get(self, model, node_id):
        return self.nodes.get(node_id)


def test_walk_expands_without_a_deadline():
    visited = kc._bfs_from_seed(_FakeSession(), "n0", depth=2, max_nodes=300)
    assert len(visited) == 6
    assert visited["n0"]["hop"] == 0 and all(v["hop"] >= 1 for k, v in visited.items() if k != "n0")


def test_walk_stops_at_a_passed_deadline():
    visited = kc._bfs_from_seed(_FakeSession(), "n0", depth=2, max_nodes=300, deadline=time.monotonic() - 1)
    assert list(visited) == ["n0"]


def test_future_deadline_does_not_interfere():
    visited = kc._bfs_from_seed(_FakeSession(), "n0", depth=2, max_nodes=300, deadline=time.monotonic() + 60)
    assert len(visited) == 6
