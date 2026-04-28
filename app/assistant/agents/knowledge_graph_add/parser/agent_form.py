from typing import List
from pydantic import BaseModel, Field

class ParsedSentence(BaseModel):
    """A self-contained sentence that can be used to construct a KG node"""
    reasoning: str
    sentence: str
    context: str

class AgentForm(BaseModel):
    """Form for parser results"""
    reasoning: str
    parsed_sentences: List[ParsedSentence]
