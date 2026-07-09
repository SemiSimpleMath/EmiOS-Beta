from app.assistant.agent_runtime.services.user_bio_context_service import UserBioContextService
from app.assistant.agent_runtime.services.resource_resolver import ResourceResolver


def _install_fake_semantic(monkeypatch):
    def _fake_embed_text(text: str):
        # "schedul" covers both the query's "schedule" and the bio fact's
        # "scheduling"; the technical axis covers the query's debug/python
        # AND the bio facts' scientist/coding — the section matcher compares
        # query-to-FACT similarity, so both sides must land on one axis.
        t = str(text or "").lower()
        if "schedul" in t or "cleaner" in t or "appointment" in t:
            return [1.0, 0.0, 0.0]
        if "debug" in t or "python" in t or "api " in t or "scientist" in t or "coding" in t:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def _fake_embed_texts(texts):
        return [_fake_embed_text(t) for t in (texts or [])]

    def _fake_route_bucket(query: str):
        vec = _fake_embed_text(query)
        if vec[0] > 0.5:
            return {"admin": 0.9, "technical": 0.2, "research": 0.2, "personal": 0.2, "worldview": 0.1}
        if vec[1] > 0.5:
            return {"admin": 0.2, "technical": 0.9, "research": 0.4, "personal": 0.2, "worldview": 0.1}
        return {"admin": 0.2, "technical": 0.2, "research": 0.2, "personal": 0.4, "worldview": 0.1}

    monkeypatch.setattr(UserBioContextService, "_embed_text", classmethod(lambda cls, text: _fake_embed_text(text)))
    monkeypatch.setattr(UserBioContextService, "_embed_texts", classmethod(lambda cls, texts: _fake_embed_texts(texts)))
    monkeypatch.setattr(
        UserBioContextService,
        "_semantic_route_bucket",
        classmethod(lambda cls, query, payload: _fake_route_bucket(query)),
    )


class _ScopedBlackboard:
    """Bio resolution is scope-gated: the service reads scope_context off the
    agent blackboard and passes it to ResourceResolver."""

    @staticmethod
    def get_state_value(key, default=None):
        if key == "scope_context":
            return {"scope_id": "scope::test", "owner_id": "test_owner"}
        return default


def test_user_bio_context_routing_admin_stays_small(monkeypatch):
    _install_fake_semantic(monkeypatch)

    payload = {
        "style_persona": ["Prefers direct concise responses."],
        "background": ["Data Scientist."],
        "projects": ["Loves coding on the assistant project."],
        "preferences": ["Prefers morning scheduling."],
        "values": [],
        "health_logistics": [],
        "entertainment_hobbies": [],
    }
    monkeypatch.setattr(
        ResourceResolver,
        "get_global_resource",
        staticmethod(lambda resource_id, required=False, scope_context=None: payload),
    )

    class _Agent:
        name = "emi_agent"
        blackboard = _ScopedBlackboard()

    out = UserBioContextService.build_context(
        agent=_Agent(),
        incoming_text="schedule a cleaner for tomorrow morning",
    )
    assert "Relevant user bio context:" in out
    assert "Prefers morning scheduling." in out
    assert "Loves coding on the assistant project." not in out


def test_user_bio_context_routing_technical_includes_background_or_projects(monkeypatch):
    _install_fake_semantic(monkeypatch)

    payload = {
        "style_persona": ["Prefers direct concise responses."],
        "background": ["Data Scientist with PhD in Math."],
        "projects": ["Works on assistant architecture and coding experiments."],
        "preferences": [],
        "values": [],
        "health_logistics": [],
        "entertainment_hobbies": [],
    }
    monkeypatch.setattr(
        ResourceResolver,
        "get_global_resource",
        staticmethod(lambda resource_id, required=False, scope_context=None: payload),
    )

    class _Agent:
        name = "emi_agent"
        blackboard = _ScopedBlackboard()

    out = UserBioContextService.build_context(
        agent=_Agent(),
        incoming_text="help me debug this python API model issue",
    )
    assert "Relevant user bio context:" in out
    assert ("Data Scientist" in out) or ("assistant architecture" in out) or ("PhD in Math" in out)


def test_section_matching_reuses_chunk_cache_no_reembedding(monkeypatch):
    """Section matching reads fact vectors from the payload-hash chunk cache;
    a second build_context over the same payload embeds NO texts at all."""
    _install_fake_semantic(monkeypatch)
    calls = {"texts": 0}
    orig = UserBioContextService._embed_texts.__func__

    def _counting_embed_texts(cls, texts):
        calls["texts"] += 1
        return orig(cls, texts)

    monkeypatch.setattr(UserBioContextService, "_embed_texts", classmethod(_counting_embed_texts))
    # Fresh cache for this payload.
    monkeypatch.setattr(UserBioContextService, "_chunk_cache_key", None)

    payload = {
        "style_persona": ["Prefers direct concise responses."],
        "background": ["Data Scientist."],
        "projects": [],
        "preferences": ["Prefers morning scheduling."],
        "values": [],
        "health_logistics": [],
        "entertainment_hobbies": [],
    }
    monkeypatch.setattr(
        ResourceResolver,
        "get_global_resource",
        staticmethod(lambda resource_id, required=False, scope_context=None: payload),
    )

    class _Agent:
        name = "emi_agent"
        blackboard = _ScopedBlackboard()

    out1 = UserBioContextService.build_context(agent=_Agent(), incoming_text="schedule a cleaner")
    first_pass_calls = calls["texts"]
    out2 = UserBioContextService.build_context(agent=_Agent(), incoming_text="schedule a cleaner")

    assert "Prefers morning scheduling." in out1 and out1 == out2
    assert first_pass_calls == 1  # the chunk-index build — nothing else
    assert calls["texts"] == first_pass_calls  # second render embeds no texts


def test_output_truncates_on_line_boundary(monkeypatch):
    _install_fake_semantic(monkeypatch)
    monkeypatch.setattr(UserBioContextService, "_chunk_cache_key", None)
    monkeypatch.setattr(UserBioContextService, "_MAX_OUTPUT_CHARS", 80)

    long_fact = "Prefers morning scheduling for appointments and cleaner visits every single week without exception."
    payload = {
        "style_persona": [],
        "background": [],
        "projects": [],
        "preferences": [long_fact, "Prefers morning scheduling."],
        "values": [],
        "health_logistics": [],
        "entertainment_hobbies": [],
    }
    monkeypatch.setattr(
        ResourceResolver,
        "get_global_resource",
        staticmethod(lambda resource_id, required=False, scope_context=None: payload),
    )

    class _Agent:
        name = "emi_agent"
        blackboard = _ScopedBlackboard()

    out = UserBioContextService.build_context(agent=_Agent(), incoming_text="schedule a cleaner")
    assert len(out) <= 80
    # No mid-line slice: every line is complete (header, section name, or a whole "- fact").
    for line in out.splitlines():
        assert line == "Relevant user bio context:" or line.endswith(":") or (
            line.startswith("- ") and line[2:] in payload["preferences"]
        ), line


def test_route_bucket_uses_bucket_content_embeddings_not_bucket_name(monkeypatch):
    def _vec(text: str):
        t = str(text or "").lower()
        if "movie" in t or "cinema" in t:
            return [1.0, 0.0]
        return [0.0, 1.0]

    monkeypatch.setattr(UserBioContextService, "_embed_text", classmethod(lambda cls, text: _vec(text)))
    monkeypatch.setattr(UserBioContextService, "_embed_texts", classmethod(lambda cls, texts: [_vec(t) for t in texts]))

    payload = {
        "style_persona": ["Concise responses."],
        "background": ["Data scientist."],
        "projects": ["Build Emi."],
        "preferences": ["Morning tasks."],
        "values": ["Humanist."],
        "health_logistics": ["Chronic back pain."],
        "entertainment_hobbies": ["I love movies and cinema."],
    }

    bucket = UserBioContextService._route_bucket(query="let's talk about movies tonight", payload=payload)
    assert bucket == "personal"
