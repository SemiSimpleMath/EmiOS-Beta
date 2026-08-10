"""
work_objects.tools — the WorkGraph tool surface a WorkAgent acts through.

This is HOW a planner manipulates the graph: a small tool family, bound to the
node it is working on, wrapping store.apply (so the writer stays the single
mutation authority). The agent runs its normal loop on the blackboard and calls
these like any other tool.

  READ (progressive disclosure — see little by default, drill down on demand):
    graph_summary / graph_peek / graph_neighbors / graph_search
  WRITE (the mutation vocabulary):
    add_subtask / add_dependency / record_finding / produce_artifact /
    ask_question / defer / mark_satisfied / mark_failed / abandon

A real WorkAgent gets these as registered LLM tools in its allowed_tools; here
they are plain methods so a scripted agent can drive them without an LLM. The
write tools are bound to `node_id` (the node the agent owns): subtasks/findings/
artifacts/questions are created UNDER it (parent_id) and wired to it.
"""
from __future__ import annotations

from typing import Optional

from work_objects.model import new_id
from work_objects.store import WorkStore


class WorkGraphTools:
    def __init__(self, store: WorkStore, work_id: str, node_id: str, actor: str):
        self.store = store
        self.work_id = work_id
        self.node_id = node_id          # the node this agent owns / is working on
        self.actor = actor

    # ----------------------- READ (progressive disclosure) ----------------------- #
    def graph_summary(self, subtree_only: bool = False) -> list[dict]:
        """One line per node (id/type/status/title). The default context is small;
        this is how the agent surveys the rest without pulling full bodies."""
        wo = self.store.load(self.work_id)
        nodes = list(wo.nodes.values())
        if subtree_only:
            keep = self._subtree_ids(wo, self.node_id)
            nodes = [n for n in nodes if n.id in keep]
        return [self._summ(n) for n in nodes]

    def graph_peek(self, node_id: str) -> dict:
        """Full body of one node — content, payload, pod, contract. Drill-down."""
        wo = self.store.load(self.work_id)
        n = wo.nodes.get(node_id)
        if n is None:
            raise KeyError(f"graph_peek: node {node_id!r} not found")
        return {
            "id": n.id, "type": n.type, "status": n.status, "title": n.title,
            "content": n.content, "payload": n.payload, "pod_ref": n.pod_ref,
            "satisfied_when_kind": n.satisfied_when_kind, "owner_agent": n.owner_agent,
        }

    def graph_neighbors(self, node_id: str, relation: Optional[str] = None) -> list[dict]:
        """Walk the DAG from a node (deps in, produces out, …)."""
        wo = self.store.load(self.work_id)
        out = []
        for e in wo.edges:
            if relation and e.relation != relation:
                continue
            if e.src == node_id:
                out.append({"dir": "out", "relation": e.relation, "node": self._summ(wo.nodes.get(e.dst))})
            elif e.dst == node_id:
                out.append({"dir": "in", "relation": e.relation, "node": self._summ(wo.nodes.get(e.src))})
        return out

    def graph_search(self, query: str) -> list[dict]:
        """Find nodes by substring of title/content (semantic recall later)."""
        wo = self.store.load(self.work_id)
        q = query.lower()
        return [self._summ(n) for n in wo.nodes.values()
                if q in (n.title or "").lower() or q in (n.content or "").lower()]

    # ----------------------- WRITE (the mutation vocabulary) ----------------------- #
    def add_subtask(self, title: str, content: str = "", satisfied_when_kind: Optional[str] = None,
                    owner_agent: Optional[str] = None, depends_on: Optional[list[str]] = None) -> str:
        nid = new_id("node")
        self.store.apply("add_node", {
            "work_id": self.work_id, "id": nid, "type": "subtask", "title": title, "content": content,
            "parent_id": self.node_id, "satisfied_when_kind": satisfied_when_kind, "owner_agent": owner_agent,
        }, actor=self.actor)
        for dep in (depends_on or []):
            self.store.apply("add_edge", {"work_id": self.work_id, "src": dep, "dst": nid, "relation": "depends_on"}, actor=self.actor)
        return nid

    def add_dependency(self, on_node_id: str) -> None:
        """Declare that MY node depends on another (I won't be ready until it's satisfied)."""
        self.store.apply("add_edge", {"work_id": self.work_id, "src": on_node_id, "dst": self.node_id, "relation": "depends_on"}, actor=self.actor)

    def record_finding(self, claim: str, confidence: Optional[float] = None, pod_ref: Optional[str] = None) -> str:
        """Mint an Evidence node NOW (written sooner so the curator can see it)."""
        nid = new_id("node")
        self.store.apply("add_node", {
            "work_id": self.work_id, "id": nid, "type": "evidence", "title": claim, "status": "assumed",
            "parent_id": self.node_id, "pod_ref": pod_ref,
            "payload": {"confidence": confidence} if confidence is not None else {},
        }, actor=self.actor)
        self.store.apply("add_edge", {"work_id": self.work_id, "src": self.node_id, "dst": nid, "relation": "produces"}, actor=self.actor)
        return nid

    def produce_artifact(self, title: str, pod_ref: str) -> str:
        """The deliverable — an Artifact node pointing at the minted pod."""
        nid = new_id("node")
        self.store.apply("add_node", {
            "work_id": self.work_id, "id": nid, "type": "artifact", "title": title, "status": "done",
            "parent_id": self.node_id, "pod_ref": pod_ref,
        }, actor=self.actor)
        self.store.apply("add_edge", {"work_id": self.work_id, "src": self.node_id, "dst": nid, "relation": "produces"}, actor=self.actor)
        return nid

    def ask_question(self, text: str, blocks: bool = False) -> str:
        """Open a Question. blocks=True makes MY node depend on it being answered."""
        nid = new_id("node")
        self.store.apply("add_node", {
            "work_id": self.work_id, "id": nid, "type": "question", "title": text, "status": "open",
            "parent_id": self.node_id,
        }, actor=self.actor)
        if blocks:
            self.store.apply("add_edge", {"work_id": self.work_id, "src": nid, "dst": self.node_id, "relation": "depends_on"}, actor=self.actor)
        return nid

    def defer(self, wake_kind: str, wake_at: Optional[str] = None, wake_ref: Optional[str] = None, reason: str = "") -> None:
        """Park MY node ('too hard now / wait for X') with a wake condition."""
        self.store.apply("defer_node", {
            "work_id": self.work_id, "node_id": self.node_id,
            "wake_kind": wake_kind, "wake_at": wake_at, "wake_ref": wake_ref, "reason": reason,
        }, actor=self.actor)

    def mark_satisfied(self) -> None:
        self.store.apply("set_status", {"work_id": self.work_id, "node_id": self.node_id, "status": "done"}, actor=self.actor)

    def mark_failed(self, reason: str = "") -> None:
        self.store.apply("set_status", {"work_id": self.work_id, "node_id": self.node_id, "status": "failed"}, actor=self.actor)

    def abandon(self, reason: str = "") -> None:
        self.store.apply("set_status", {"work_id": self.work_id, "node_id": self.node_id, "status": "abandoned",
                                        "reason": f"worker abandoned: {reason or 'no stated reason'}"}, actor=self.actor)

    # ----------------------------- helpers ----------------------------- #
    @staticmethod
    def _summ(n) -> Optional[dict]:
        return None if n is None else {"id": n.id, "type": n.type, "status": n.status, "title": n.title}

    @staticmethod
    def _subtree_ids(wo, root_id: str) -> set:
        keep, frontier = {root_id}, [root_id]
        while frontier:
            cur = frontier.pop()
            for n in wo.nodes.values():
                if n.parent_id == cur and n.id not in keep:
                    keep.add(n.id)
                    frontier.append(n.id)
        return keep
