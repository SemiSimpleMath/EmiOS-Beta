from pydantic import BaseModel, Field
from typing import List


class AgentForm(BaseModel):
    """Output schema for pod_classifier (Pass 1).

    Given a burst of conversation and a tag vocabulary, decide which tags
    apply, write a one-line subject, and reorganize the burst into topic
    sections. No entity resolution here — that is a separate pass.
    """

    tags: List[str] = Field(
        default_factory=list,
        description=(
            "Tag names from the provided vocabulary that apply to this burst. "
            "Multiple tags allowed when the burst genuinely covers multiple "
            "topics. Empty list when nothing in the vocabulary fits."
        ),
    )
    one_liner: str = Field(
        default="",
        description=(
            "A TERSE topic label for this burst — typically 3 to 6 words. "
            "Think of it as a subject line for the conversation, not a "
            "summary. Examples: 'diet mindset shift', 'AMC movie night: "
            "Good Bad Ugly', 'NYC weather check', 'rough sleep report'. "
            "Do NOT write narrative sentences. Empty when tags is empty."
        ),
    )
    sections: List[str] = Field(
        default_factory=list,
        description=(
            "The burst reorganized into topic sections. Each entry in the "
            "list is ONE section — a single string containing the "
            "messages that belong to one topic. Rules:\n"
            "\n"
            "1. Group the burst's messages by shared topic. Each group "
            "produces one string. A message belongs to only one section.\n"
            "\n"
            "2. Within a section string, reproduce relevant messages "
            "VERBATIM in their original order, keeping the full line "
            "including the timestamped speaker prefix, separated by "
            "newlines. Drop throwaway lines ('lol', 'ok', 'hello?').\n"
            "\n"
            "3. Do NOT insert entity parentheticals, type labels, or any "
            "annotation. A separate pass handles entity resolution.\n"
            "\n"
            "4. Do NOT write bridging narration, summaries, or invented "
            "speakers. Do NOT add topic headers inside a section.\n"
            "\n"
            "5. Empty list when tags is empty."
        ),
    )
    reasoning: str = Field(
        default="",
        description=(
            "Brief explanation of which tags were chosen and why. Used for "
            "debugging and for tuning the tag vocabulary."
        ),
    )
