import json
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class AgentForm(BaseModel):
    relation: Literal['same','different','specialises','supersedes','contradicts','unresolved']
    reason: str
    evidence_ids: list[str]
    survivor_id: str = ''
    current_id: str = ''
    canonical_statement: str = ''
    conditions_json: str | None = None
    scope: Literal['chronic','temporary'] = 'chronic'
    kind: Literal['durable_fact','stable_relationship','stable_preference','routine_pattern','episodic_context','transient_state'] = 'routine_pattern'
    equivalent_observations: list[list[str]] = Field(default_factory=list)

    @model_validator(mode='after')
    def complete_merge(self):
        if self.relation == 'same':
            required = {'canonical_statement','conditions_json','scope','kind','survivor_id'}
            if not required <= self.model_fields_set or not self.canonical_statement.strip():
                raise ValueError('A merge requires an explicit complete canonical belief')
        if self.conditions_json is not None and not isinstance(json.loads(self.conditions_json), dict):
            raise ValueError('conditions_json must encode an object')
        return self
