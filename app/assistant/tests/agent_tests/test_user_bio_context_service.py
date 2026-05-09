from app.assistant.agent_runtime.services.user_bio_context_service import UserBioContextService
from app.assistant.agent_runtime.services.resource_resolver import ResourceResolver


def _install_fake_semantic(monkeypatch):
    def _fake_embed_text(text: str):
        t = str(text or "").lower()
        if "schedule" in t or "cleaner" in t or "appointment" in t:
            return [1.0, 0.0, 0.0]
        if "debug" in t or "python" in t or "api" in t:
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


def test_user_bio_context_routing_admin_stays_small(monkeypatch):
    _install_fake_semantic(monkeypatch)

    payload = {
        "style_persona": ["Prefers direct concise responses."],
        "background": ["Data Scientist."],
        "projects": ["Loves coding on Emi project."],
        "preferences": ["Prefers morning scheduling."],
        "values": [],
        "health_logistics": [],
        "entertainment_hobbies": [],
    }
    monkeypatch.setattr(ResourceResolver, "get_global_resource", staticmethod(lambda resource_id, required=False: payload))

    class _Agent:
        name = "emi_agent"
        blackboard = object()

    out = UserBioContextService.build_context(
        agent=_Agent(),
        incoming_text="schedule a cleaner for tomorrow morning",
    )
    assert "Relevant user bio context:" in out
    assert "Prefers morning scheduling." in out
    assert "Loves coding on Emi project." not in out


def test_user_bio_context_routing_technical_includes_background_or_projects(monkeypatch):
    _install_fake_semantic(monkeypatch)

    payload = {
        "style_persona": ["Prefers direct concise responses."],
        "background": ["Data Scientist with PhD in Math."],
        "projects": ["Works on Emi architecture and coding experiments."],
        "preferences": [],
        "values": [],
        "health_logistics": [],
        "entertainment_hobbies": [],
    }
    monkeypatch.setattr(ResourceResolver, "get_global_resource", staticmethod(lambda resource_id, required=False: payload))

    class _Agent:
        name = "emi_agent"
        blackboard = object()

    out = UserBioContextService.build_context(
        agent=_Agent(),
        incoming_text="help me debug this python API model issue",
    )
    assert "Relevant user bio context:" in out
    assert ("Data Scientist" in out) or ("Emi architecture" in out) or ("PhD in Math" in out)


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
