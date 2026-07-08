# location_manager.py
"""
Location Manager - Predicts and tracks user's location over time.

This manager builds a location timeline based on:
1. Calendar events with locations
2. Smart inferences about gaps between events
3. User patterns from resource_location_guidelines.json

Other agents can query: "Where will user be at X time?"
"""
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_to_local, get_local_timezone, local_to_utc
from app.assistant.event_repository.event_repository import EventRepositoryManager
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def _parse_datetime(dt_str: str) -> datetime:
    """Parse datetime string and ensure it's timezone-aware (UTC)."""
    if not dt_str:
        return datetime.now(timezone.utc)
    
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    
    # If naive, assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    return dt


# Dynamic location file — current location + timeline only.
# Home address lives permanently in resource_user_data.json.
RESOURCE_FILE = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..",
    "resources",
    "resource_current_location.json"
)


def _load_home_from_user_data() -> Dict[str, Any]:
    """Read permanent home address from resource_user_data.json."""
    from app.assistant.utils.path_utils import get_resources_dir
    path = get_resources_dir() / "user" / "resource_user_data.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        city = (data.get("home_city") or "").strip()
        if not city:
            return {}
        return {
            "label": "Home",
            "address": {
                "street": "",
                "city": city,
                "state": (data.get("home_state") or "").strip(),
                "zip": "",
                "country": (data.get("home_country") or "").strip() or "US",
            },
        }
    except Exception as e:
        logger.error("Failed reading home address from resource_user_data.json: %s", e)
        return {}


class LocationManager:
    """
    Manages user location tracking and prediction.

    Maintains a timeline of predicted locations based on calendar events
    and smart inferences, queryable by other agents.

    Home address is read from resource_user_data.json (permanent).
    Dynamic state (current_location, location_timeline) is stored in
    resource_current_location.json.
    """

    def __init__(self):
        self.repo = EventRepositoryManager()
        self._load_location_data()

    def _load_location_data(self) -> Dict[str, Any]:
        """Load dynamic location data from resource file."""
        try:
            if os.path.exists(RESOURCE_FILE):
                with open(RESOURCE_FILE, 'r', encoding='utf-8') as f:
                    self.location_data = json.load(f)
                if not isinstance(self.location_data, dict):
                    self.location_data = self._create_empty_location_data()
            else:
                self.location_data = self._create_empty_location_data()

            if "location_timeline" not in self.location_data:
                self.location_data["location_timeline"] = []

            return self.location_data
        except Exception as e:
            logger.error("Error loading location data: %s", e)
            self.location_data = self._create_empty_location_data()
            return self.location_data

    def _create_empty_location_data(self) -> Dict[str, Any]:
        """Create empty dynamic location structure (no home — that's in user_data)."""
        return {
            "current_location": {
                "latitude": None,
                "longitude": None,
                "address": {"street": "", "city": "", "state": "", "zip": "", "country": ""},
                "label": "Unknown"
            },
            "location_timeline": [],
            "last_updated": datetime.now(timezone.utc).isoformat()
        }

    def _save_location(self):
        """Save dynamic location data to resource_current_location.json."""
        try:
            timeline = self.location_data.get("location_timeline", []) or []
            normalized = []
            for entry in timeline:
                try:
                    start_dt = _parse_datetime(entry.get("start", ""))
                    end_dt = _parse_datetime(entry.get("end", ""))
                    normalized.append({**entry, "start": start_dt.isoformat(), "end": end_dt.isoformat()})
                except Exception:
                    normalized.append(entry)
            self.location_data["location_timeline"] = normalized
            self.location_data["last_updated"] = datetime.now(timezone.utc).isoformat()

            os.makedirs(os.path.dirname(RESOURCE_FILE), exist_ok=True)
            with open(RESOURCE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.location_data, f, indent=2, ensure_ascii=False)
            logger.debug("Location data saved to resource_current_location.json")
        except Exception as e:
            logger.error("Error saving location data: %s", e)

    def get_home_location(self) -> Dict[str, Any]:
        """Get the permanent home location from resource_user_data.json."""
        return _load_home_from_user_data()
    
    def get_current_location(self) -> Dict[str, Any]:
        """Get the current location (where user is right now)."""
        return self.get_location_at(datetime.now(timezone.utc))
    
    def get_location_label(self, location: Dict[str, Any]) -> str:
        """Get a human-readable label for a location."""
        if not location:
            return "Unknown"
        label = location.get("label", "")
        if label:
            return label
        addr = location.get("address", {})
        city = addr.get("city", "Unknown")
        state = addr.get("state", "")
        return f"{city}, {state}" if state else city
    
    def get_location_at(self, target_time: datetime) -> Dict[str, Any]:
        """
        Get predicted location at a specific time.
        
        This is the main query method for other agents.
        
        Args:
            target_time: The datetime to query (should be timezone-aware UTC)
            
        Returns:
            Location dict with label, address, source, confidence
        """
        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=timezone.utc)
        
        timeline = self.location_data.get("location_timeline", [])
        
        # Search timeline for matching entry
        for entry in timeline:
            try:
                start = _parse_datetime(entry.get("start", ""))
                end = _parse_datetime(entry.get("end", ""))
                
                if start <= target_time < end:
                    return {
                        "label": entry.get("label", "Unknown"),
                        "address": entry.get("address", {}),
                        "source": entry.get("source", "calendar"),
                        "event_summary": entry.get("event_summary", ""),
                        "confidence": entry.get("confidence", 0.8),
                        "reasoning": entry.get("reasoning", "")
                    }
            except Exception as e:
                logger.warning(f"Error parsing timeline entry: {e}")
                continue
        
        # No entry found - return unknown (timeline should be complete after build)
        return {
            "label": "Unknown",
            "address": {},
            "source": "no_data",
            "event_summary": "",
            "confidence": 0.1,
            "reasoning": "No timeline data for this time"
        }
    
    def get_location_in(self, hours: float = 0, minutes: float = 0) -> Dict[str, Any]:
        """
        Get predicted location in X hours/minutes from now.
        
        Args:
            hours: Hours from now
            minutes: Minutes from now
            
        Returns:
            Location dict
        """
        target_time = datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)
        return self.get_location_at(target_time)
    
    def get_location_summary(self, hours_ahead: int = 24) -> str:
        """
        Get a human-readable summary of location predictions.
        
        Args:
            hours_ahead: How many hours to look ahead
            
        Returns:
            String summary of predicted locations
        """
        now = datetime.now(timezone.utc)
        end_time = now + timedelta(hours=hours_ahead)
        
        current = self.get_location_at(now)
        lines = [f"📍 Currently: {current.get('label', 'Unknown')}"]
        if current.get("event_summary"):
            lines[0] += f" ({current['event_summary']})"
        
        timeline = self.location_data.get("location_timeline", [])
        
        # Filter to relevant time window, skip current entry
        upcoming = []
        current_label = current.get("label")
        
        for entry in timeline:
            try:
                start = _parse_datetime(entry.get("start", ""))
                
                # Only include future entries
                if start > now and start < end_time:
                    # Skip if same label as previous (consolidate)
                    if not upcoming or entry.get("label") != upcoming[-1].get("label"):
                        upcoming.append(entry)
            except Exception:
                continue
        
        # Sort by start time
        upcoming.sort(key=lambda x: x.get("start", ""))
        
        for entry in upcoming[:10]:
            try:
                start = _parse_datetime(entry.get("start", ""))
                start_local = utc_to_local(start)
                label = entry.get("label", "Unknown")
                event = entry.get("event_summary", "")
                source = entry.get("source", "")
                
                time_str = start_local.strftime("%I:%M %p")
                line = f"  • {time_str}: {label}"
                if event:
                    line += f" ({event})"
                if source == "inferred":
                    line += " [inferred]"
                lines.append(line)
            except Exception:
                continue
        
        if len(lines) == 1:
            lines.append("  No location changes predicted")
        
        return "\n".join(lines)
    
    def _get_calendar_events(self, days_ahead: int = 7) -> List[Dict]:
        """Get calendar events for the next N days."""
        try:
            raw = self.repo.search_events(data_type="calendar")
            events = json.loads(raw)
            
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(days=days_ahead)
            
            relevant = []
            for event in events:
                data = event.get("data", {})
                start_iso = data.get("start", "")
                if not start_iso:
                    continue
                try:
                    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    if start_dt >= now - timedelta(hours=2) and start_dt <= cutoff:
                        relevant.append(data)
                except Exception:
                    continue
            
            # Sort by start time
            relevant.sort(key=lambda x: x.get("start", ""))
            return relevant
        except Exception as e:
            logger.error(f"Error fetching calendar events: {e}")
            return []
    
    def _parse_location_from_event(self, event: Dict) -> Optional[Dict[str, Any]]:
        """Extract location info from a calendar event."""
        location_str = event.get("location", "")
        
        if not location_str:
            return None
        
        parts = [p.strip() for p in location_str.split(",")]
        
        if len(parts) >= 3:
            return {
                "label": location_str[:50],
                "address": {
                    "street": parts[0],
                    "city": parts[1] if len(parts) > 1 else "",
                    "state": parts[2] if len(parts) > 2 else "",
                    "zip": parts[3] if len(parts) > 3 else "",
                    "country": "USA"
                }
            }
        elif len(parts) == 2:
            return {
                "label": location_str,
                "address": {"street": "", "city": parts[0], "state": parts[1], "zip": "", "country": "USA"}
            }
        else:
            return {
                "label": location_str,
                "address": {"street": "", "city": "", "state": "", "zip": "", "country": ""}
            }
    
    def _build_calendar_entries(self, days_ahead: int = 7) -> List[Dict]:
        """Build timeline entries from calendar events with locations."""
        events = self._get_calendar_events(days_ahead)
        logger.debug("_build_calendar_entries: Found %d total calendar events", len(events))
        
        entries = []
        events_without_location = 0
        
        for event in events:
            location = self._parse_location_from_event(event)
            if not location:
                events_without_location += 1
                summary = event.get("summary", "No title")
                logger.debug("No location found for event: %s", summary)
                continue
            
            start_iso = event.get("start", "")
            end_iso = event.get("end", "")
            
            if not start_iso or not end_iso:
                continue
            
            try:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                
                entries.append({
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "label": location.get("label", "Unknown"),
                    "address": location.get("address", {}),
                    "source": "calendar",
                    "event_id": event.get("id", ""),
                    "event_summary": event.get("summary", ""),
                    "confidence": 0.95
                })
            except Exception as e:
                logger.warning(f"Error processing event: {e}")
        
        logger.debug("_build_calendar_entries: %d with locations, %d without", len(entries), events_without_location)
        return entries
    
    def _build_context_for_inference(self, all_calendar_events: List[Dict], days_ahead: int) -> Dict:
        """Build context for the inference agent with ALL calendar events."""
        now = datetime.now(timezone.utc)
        now_local = utc_to_local(now)
        
        # Format ALL calendar events for agent (not just ones with locations)
        events_formatted = []
        for event in all_calendar_events:
            try:
                start_iso = event.get("start", "")
                end_iso = event.get("end", "")
                if not start_iso:
                    continue
                    
                start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else start
                start_local = utc_to_local(start)
                end_local = utc_to_local(end)
                
                events_formatted.append({
                    "date": start_local.strftime("%A, %B %d"),
                    "start_time": start_local.strftime("%I:%M %p"),
                    "end_time": end_local.strftime("%I:%M %p"),
                    "event_name": event.get("summary", "Untitled"),
                    "location_if_specified": event.get("location", ""),  # May be empty
                    "description": event.get("description", "")[:200] if event.get("description") else ""
                })
            except Exception as e:
                logger.warning(f"Error formatting event: {e}")
                continue
        
        return {
            "date_time": now_local.strftime("%I:%M %p on %A, %B %d, %Y"),
            "days_to_predict": days_ahead,
            "calendar_events": events_formatted  # ALL events, agent infers locations
            # Note: home_location comes from resource_user_data.json (permanent)
        }
    
    def _infer_gaps_with_agent(self, all_calendar_events: List[Dict], days_ahead: int) -> List[Dict]:
        """
        Use agent to infer locations based on ALL calendar events.
        
        The agent:
        - Analyzes event names to infer locations (e.g., "Sam's drama camp" → Lake Forest)
        - Uses guidelines (work from home, drop-off patterns, travel times)
        - Builds a complete location timeline for the day
        """
        try:
            logger.debug("_infer_gaps_with_agent() called")
            context = self._build_context_for_inference(all_calendar_events, days_ahead)
            
            if not context["calendar_events"]:
                logger.info("No calendar events at all - using home for full timeline")
                return self._create_home_timeline(days_ahead)
            
            logger.debug("Found %d calendar events to analyze", len(context['calendar_events']))
            
            logger.debug("Creating location_inference agent...")
            
            # Use agent to infer locations for ALL events
            location_agent = DI.agent_factory.create_agent('location_inference')
            logger.debug("Calling agent.action_handler()...")
            from app.assistant.utils.identity_names import PRINCIPAL_USER
            from app.assistant.utils.pydantic_classes import ScopeContext, ScopeResourcePolicy
            scope = ScopeContext(
                scope_id="location_inference",
                owner_id=PRINCIPAL_USER,
                actor_id="location_manager",
                room_id="system",
                surface="system",
                resources=ScopeResourcePolicy(
                    allowed_global_resources=[
                        "resource_user_data",
                        "resource_current_location",
                        "resource_location_inference_guidelines",
                    ],
                ),
            )
            result = location_agent.action_handler(Message(agent_input=context, scope_context=scope))
            logger.debug("Agent returned: %s", type(result))
            
            if result and hasattr(result, 'data') and result.data:
                inferred_entries = result.data.get("timeline_entries", [])
                
                # Parse agent response into timeline entries
                parsed_entries = []
                for entry in inferred_entries:
                    try:
                        # Contract: LLM outputs local naive timestamps (no tz, no Z).
                        # Convert local -> UTC deterministically in Python before storing.
                        start_local = (entry.get("start_local") or "").strip()
                        end_local = (entry.get("end_local") or "").strip()

                        # Backward compatibility: older agent versions may have returned start/end directly.
                        if not start_local:
                            start_local = (entry.get("start") or "").strip()
                        if not end_local:
                            end_local = (entry.get("end") or "").strip()

                        # Fix LLM hallucinating hour 24 (should be 00:00 next day).
                        def _fix_hour_24(dt_str):
                            if dt_str and "T24:" in dt_str:
                                fixed = dt_str.replace("T24:", "T00:")
                                try:
                                    dt = datetime.fromisoformat(fixed) + timedelta(days=1)
                                    return dt.isoformat()
                                except Exception:
                                    pass
                            return dt_str
                        start_local = _fix_hour_24(start_local)
                        end_local = _fix_hour_24(end_local)

                        start_utc = local_to_utc(start_local)
                        end_utc = local_to_utc(end_local)

                        # Build structured address from explicit fields.
                        address = {
                            "city": str(entry.get("city") or "").strip(),
                            "state": str(entry.get("state") or "").strip(),
                            "country": str(entry.get("country") or "").strip(),
                        }
                        # Backward compat: older runs may still have address_pairs.
                        if not address["city"]:
                            address_pairs = entry.get("address_pairs")
                            if isinstance(address_pairs, list):
                                for pair in address_pairs:
                                    if not isinstance(pair, dict):
                                        continue
                                    key = str(pair.get("key") or "").strip().lower()
                                    val = str(pair.get("value") or "").strip()
                                    if key in ("city", "state", "country") and val:
                                        address[key] = val

                        parsed_entries.append({
                            "start": start_utc.isoformat(),
                            "end": end_utc.isoformat(),
                            "label": entry.get("label", "Unknown"),
                            "address": address,
                            "source": "inferred",
                            "event_summary": "",
                            "confidence": entry.get("confidence", 0.7),
                            "reasoning": entry.get("reasoning", "")
                        })
                    except Exception as e:
                        logger.warning(f"Error parsing inferred entry: {e}")
                
                return parsed_entries
            
            logger.warning("📍 Agent returned no data - falling back to simple inference")
            return self._simple_gap_inference(calendar_entries, days_ahead)
            
        except Exception as e:
            logger.error(f"Error in agent inference: {e}")
            return self._simple_gap_inference(calendar_entries, days_ahead)
    
    def _simple_gap_inference(self, calendar_entries: List[Dict], days_ahead: int) -> List[Dict]:
        """
        Simple rule-based gap inference (fallback when agent unavailable).
        
        Rules:
        - Before first event of day: at home (preparing/traveling)
        - Between events: traveling or at previous location
        - After last event: traveling home
        - Night time: at home
        """
        if not calendar_entries:
            return self._create_home_timeline(days_ahead)
        
        inferred = []
        home = self.get_home_location()
        home_label = home.get("label", "Home")
        home_addr = home.get("address", {})
        
        now = datetime.now(timezone.utc)
        timeline_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        timeline_end = timeline_start + timedelta(days=days_ahead)
        
        # Sort entries by start time
        sorted_entries = sorted(calendar_entries, key=lambda x: x.get("start", ""))
        
        prev_end = timeline_start
        prev_location = home_label
        
        for entry in sorted_entries:
            try:
                start = _parse_datetime(entry.get("start", ""))
                end = _parse_datetime(entry.get("end", ""))
                
                # Fill gap before this event
                if start > prev_end:
                    gap_hours = (start - prev_end).total_seconds() / 3600
                    
                    if gap_hours > 6:
                        # Long gap - probably home
                        inferred.append({
                            "start": prev_end.isoformat(),
                            "end": start.isoformat(),
                            "label": home_label,
                            "address": home_addr,
                            "source": "inferred",
                            "confidence": 0.6,
                            "reasoning": f"Long gap ({gap_hours:.1f}h) - likely at home"
                        })
                    elif gap_hours > 1:
                        # Medium gap - traveling or staying at previous location
                        inferred.append({
                            "start": prev_end.isoformat(),
                            "end": start.isoformat(),
                            "label": f"Traveling to {entry.get('label', 'next event')}",
                            "address": {},
                            "source": "inferred",
                            "confidence": 0.5,
                            "reasoning": f"Gap of {gap_hours:.1f}h before next event"
                        })
                    # Short gaps (<1h) - still at previous location, no need to add entry
                
                prev_end = end
                prev_location = entry.get("label", "Unknown")
                
            except Exception as e:
                logger.warning(f"Error in simple inference: {e}")
        
        # Fill gap after last event until end of timeline
        if prev_end < timeline_end:
            hours_remaining = (timeline_end - prev_end).total_seconds() / 3600
            if hours_remaining > 2:
                inferred.append({
                    "start": prev_end.isoformat(),
                    "end": timeline_end.isoformat(),
                    "label": home_label,
                    "address": home_addr,
                    "source": "inferred",
                    "confidence": 0.6,
                    "reasoning": "After last event - returning home"
                })
        
        return inferred
    
    def _create_home_timeline(self, days_ahead: int) -> List[Dict]:
        """Create a simple home timeline when no events exist."""
        home = self.get_home_location()
        now = datetime.now(timezone.utc)
        
        return [{
            "start": now.isoformat(),
            "end": (now + timedelta(days=days_ahead)).isoformat(),
            "label": home.get("label", "Home"),
            "address": home.get("address", {}),
            "source": "inferred",
            "confidence": 0.5,
            "reasoning": "No calendar events with locations"
        }]
    
    def _calendar_signature(self, events: List[Dict]) -> str:
        """Stable hash of the calendar inputs that determine the inferred timeline.

        The agent's output is a pure function of these fields, so an unchanged
        signature means a re-inference would reproduce the same timeline.
        """
        parts = [
            f"{e.get('start','')}|{e.get('end','')}|{e.get('summary','')}|{e.get('location','')}"
            for e in events
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def build_location_timeline(self, days_ahead: int = 7, force: bool = False) -> List[Dict]:
        """
        Build complete location timeline.

        1. Get ALL calendar events
        2. Send to agent to infer locations based on event names + guidelines
        3. Agent returns complete timeline

        Cost gate: the LLM inference (~12K output tokens/call on gpt-5.4-mini)
        only runs when the calendar actually changed, or once/day as a floor.
        Previously this fired on every location_refresh poll (~every 15 min),
        re-deriving an unchanged 7-day timeline and burning ~$3/day. Current
        location stays fresh regardless — update_current_location() derives it
        from the cached timeline + clock with no LLM call.
        """
        logger.info(f"📍 Building location timeline for next {days_ahead} days...")

        # Step 1: Get ALL calendar events (not just ones with locations)
        all_calendar_events = self._get_calendar_events(days_ahead)
        logger.info("  Found %d total calendar events", len(all_calendar_events))

        # Cost gate — skip the LLM when calendar is unchanged AND last inference < 24h.
        signature = self._calendar_signature(all_calendar_events)
        existing = self.location_data.get("location_timeline") or []
        last_sig = self.location_data.get("_inference_signature")
        last_at = self.location_data.get("_inference_at_utc")
        stale = True
        if last_at:
            try:
                age_h = (datetime.now(timezone.utc) - _parse_datetime(last_at)).total_seconds() / 3600.0
                stale = age_h >= 24.0
            except Exception:
                stale = True
        if not force and existing and signature == last_sig and not stale:
            logger.info(
                "📍 Calendar unchanged and last inference <24h ago — skipping LLM rebuild (cost gate)."
            )
            return existing

        # Step 2: Send ALL events to agent - it will infer locations based on:
        #   - Event names (e.g., "Sam's drama camp" → Lake Forest)
        #   - Guidelines (work from home, drop-off/pick-up patterns)
        #   - Time of day patterns
        logger.debug("Calling agent to build location timeline...")
        all_entries = self._infer_gaps_with_agent(all_calendar_events, days_ahead)
        logger.info(f"  Agent returned {len(all_entries)} timeline entries")

        # Sort by start time
        all_entries.sort(key=lambda x: x.get("start", ""))

        # Update storage
        self.location_data["location_timeline"] = all_entries
        self.location_data["_inference_signature"] = signature
        self.location_data["_inference_at_utc"] = datetime.now(timezone.utc).isoformat()
        self._save_location()

        logger.info(f"📍 Built timeline with {len(all_entries)} total entries")
        return all_entries
    
    def update_current_location(self):
        """Update the current_location field based on timeline."""
        current = self.get_location_at(datetime.now(timezone.utc))
        label = current.get("label", "Unknown")

        # If at home, use the canonical structured address from user_data
        # instead of whatever the LLM inferred (avoids "Irvine, CA" vs
        # structured city/state/country mismatch).
        address = current.get("address", {})
        if label.lower() in ("home", "home (night)"):
            home = self.get_home_location()
            if home.get("address"):
                address = home["address"]

        self.location_data["current_location"] = {
            "latitude": None,
            "longitude": None,
            "address": address,
            "label": label,
        }

        self._save_location()
        logger.info(f"📍 Current location: {label} (source: {current.get('source')})")

        return current
    
    def refresh(self) -> Dict[str, Any]:
        """
        Full refresh - rebuild timeline and update current location.
        
        Returns:
            Current location after refresh
        """
        logger.info("Refreshing location data...")
        self.build_location_timeline()
        current = self.update_current_location()
        logger.info("Refresh complete. Current location: %s", current.get('label', 'Unknown'))
        return current


# Singleton instance
_location_manager: Optional[LocationManager] = None


def get_location_manager() -> LocationManager:
    """Get or create the singleton LocationManager instance."""
    global _location_manager
    if _location_manager is None:
        _location_manager = LocationManager()
    return _location_manager


# Convenience functions for other agents
def get_user_location_at(target_time: datetime) -> Dict[str, Any]:
    """Get predicted user location at a specific time."""
    return get_location_manager().get_location_at(target_time)


def get_user_location_in(hours: float = 0, minutes: float = 0) -> Dict[str, Any]:
    """Get predicted user location in X hours/minutes from now."""
    return get_location_manager().get_location_in(hours=hours, minutes=minutes)


def get_user_location_summary(hours_ahead: int = 24) -> str:
    """Get a human-readable summary of predicted locations."""
    return get_location_manager().get_location_summary(hours_ahead=hours_ahead)


if __name__ == "__main__":
    manager = LocationManager()
    
    print(f"Home: {manager.get_location_label(manager.get_home_location())}")
    print("\nBuilding timeline...")
    manager.refresh()
    
    print("\nLocation summary (next 48 hours):")
    print(manager.get_location_summary(hours_ahead=48))
    
    print("\nLocation in 2 hours:")
    loc = manager.get_location_in(hours=2)
    print(f"  {loc.get('label')} (confidence: {loc.get('confidence')}, source: {loc.get('source')})")
    if loc.get("reasoning"):
        print(f"  Reasoning: {loc.get('reasoning')}")
