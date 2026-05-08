from __future__ import annotations

from typing import List, Literal, Annotated

from pydantic import BaseModel, Field, ConfigDict


Priority = Literal["low", "medium", "high"]
ScheduleType = Literal["fixed_event", "task_block", "travel", "buffer", "break", "free_time"]
TriageLabel = Literal["urgent", "time_sensitive", "fyi", "ignore"]
LoadRecommendation = Literal["rest", "normal", "push"]

ShortStr = Annotated[str, Field(max_length=160)]
MedStr = Annotated[str, Field(max_length=200)]
LongStr = Annotated[str, Field(max_length=300)]


# ---------- leaf models ----------

class SuggestedTimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str  # ISO format datetime string
    end: str  # ISO format datetime string



class TaskSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = None
    title: Annotated[str, Field(max_length=160)]
    urgency_score: Annotated[int, Field(ge=0, le=10)]
    effort_minutes: Annotated[int, Field(ge=5)]
    due: str = None  # ISO format datetime string
    suggested_time_window: SuggestedTimeWindow = None
    rationale: MedStr


class EmailItem(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    email_id: str
    thread_id: str = None
    subject: Annotated[str, Field(max_length=160)]
    from_: Annotated[str, Field(alias="from", max_length=120)]
    received_at: str  # ISO format datetime string
    triage_label: TriageLabel
    action_recommended: Annotated[str, Field(max_length=120)] = None
    reason: ShortStr


class Metrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workload_score: Annotated[int, Field(ge=0, le=10)]
    total_free_minutes: Annotated[int, Field(ge=0)]
    total_task_minutes: Annotated[int, Field(ge=0)]
    buffers_added_minutes: Annotated[int, Field(ge=0)]


class ConflictItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: MedStr
    severity: Priority
    resolution: MedStr


# ---------- schedule and windows ----------

class ScheduleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str  # ISO format datetime string
    end: str  # ISO format datetime string
    type: ScheduleType
    title: Annotated[str, Field(max_length=120)]
    source_event_id: str = None
    task_id: str = None
    location: str = None
    travel_required: bool = False
    travel_minutes: Annotated[int, Field(ge=0)] = None
    priority: Priority = "medium"
    notes: LongStr = None

class FreeTimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str  # ISO format datetime string
    end: str  # ISO format datetime string
    length_minutes: Annotated[int, Field(ge=0)]
    suggestions: List[ShortStr] = Field(default_factory=list)


# ---------- upcoming events ----------

class UpcomingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    title: Annotated[str, Field(max_length=160)]
    date: str  # ISO format datetime string
    days_until: Annotated[int, Field(ge=0)]
    importance: Priority
    lead_actions: List[ShortStr] = Field(default_factory=list)
    watch_flag: bool = False
    notes: Annotated[str, Field(max_length=200)] = None


# ---------- grouped sections ----------

class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    load_recommendation: LoadRecommendation
    load_rationale: MedStr = None
    tasks: List[TaskSuggestion] = Field(default_factory=list)


class EmailTriage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urgent: List[EmailItem] = Field(default_factory=list)
    time_sensitive: List[EmailItem] = Field(default_factory=list)
    fyi: List[EmailItem] = Field(default_factory=list)
    ignore: List[EmailItem] = Field(default_factory=list)


class NewsSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_name: str = Field(description="Name of the news source, e.g. CNN, BBC")
    screenshot_path: str = Field(default="", description="File path to the captured screenshot")
    headlines: List[str] = Field(default_factory=list, description="Top headlines extracted from the source")


class NewsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: List[NewsSource] = Field(default_factory=list)


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day_date: str  # ISO format datetime string
    narrative: Annotated[str, Field(max_length=2000)]
    schedule: List[ScheduleItem] = Field(default_factory=list)
    free_time_windows: List[FreeTimeWindow] = Field(default_factory=list)
    upcoming_events: List[UpcomingEvent] = Field(default_factory=list)
    task_plan: TaskPlan
    email_triage: EmailTriage
    news: NewsSection = Field(default_factory=NewsSection)
    metrics: Metrics
    assumptions: List[ShortStr] = Field(default_factory=list)
    conflicts: List[ConflictItem] = Field(default_factory=list)
    summary: Annotated[str, Field(max_length=400)]
