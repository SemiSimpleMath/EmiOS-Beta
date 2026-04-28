from __future__ import annotations

from app.assistant.signal_router.contracts import GarbageFilterDecision, SignalEnvelope, WatchRegistration
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _preview(value, *, limit: int = 300) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


class GarbageFilterService:
    """
    Deterministic prefilter for SignalRouter.

    This service intentionally performs only strict, explainable checks
    before invoking the watcher agent.
    """

    def decide_for_watch(self, *, signal: SignalEnvelope, watch: WatchRegistration) -> GarbageFilterDecision:
        content = str(signal.content or "").strip()
        logger.info(
            "SignalRouter prefilter trace registration_id=%s watch_type=%s event_name=%s compare_content=%r",
            watch.registration_id,
            watch.watch_type,
            watch.event_name,
            _preview(content),
        )
        if not content:
            return GarbageFilterDecision(
                action="drop",
                reason_code="empty_content",
                details="signal has no content",
                normalized_content="",
                tags=["prefilter", "empty"],
            )

        if watch.watch_type == "keyword_contains":
            keywords = watch.predicate.get("keywords") if isinstance(watch.predicate, dict) else None
            logger.info(
                "SignalRouter prefilter keyword trace registration_id=%s keywords=%r",
                watch.registration_id,
                keywords,
            )
            if not isinstance(keywords, list) or not keywords:
                return GarbageFilterDecision(
                    action="drop",
                    reason_code="invalid_watch_keywords",
                    details="watch keyword list missing or invalid",
                    normalized_content=content,
                    tags=["prefilter", "invalid_watch"],
                )
            content_lc = content.lower()
            matched = any(str(keyword).strip().lower() in content_lc for keyword in keywords)
            logger.info(
                "SignalRouter prefilter keyword compare registration_id=%s matched=%s",
                watch.registration_id,
                matched,
            )
            if not matched:
                return GarbageFilterDecision(
                    action="drop",
                    reason_code="keyword_no_match",
                    details=f"signal content does not match watch keywords for {watch.watch_key}",
                    normalized_content=content,
                    tags=["prefilter", "keyword_no_match"],
                )
            return GarbageFilterDecision(
                action="keep",
                reason_code="keyword_prefilter_match",
                details=f"signal matched watch keywords for {watch.watch_key}",
                normalized_content=content,
                tags=["prefilter", "keyword_match"],
            )

        if watch.watch_type == "email_semantic_match":
            signal_type = str(signal.signal_type or "").strip().lower()
            if signal_type != "email":
                return GarbageFilterDecision(
                    action="drop",
                    reason_code="email_watch_non_email_signal",
                    details=f"email_semantic_match requires signal_type=email, got '{signal_type}'",
                    normalized_content=content,
                    tags=["prefilter", "email_watch", "non_email_signal"],
                )
            semantic_query = watch.predicate.get("semantic_query") if isinstance(watch.predicate, dict) else None
            sender_equals = watch.predicate.get("sender_equals") if isinstance(watch.predicate, dict) else None
            sender_contains = watch.predicate.get("sender_contains") if isinstance(watch.predicate, dict) else None
            subject_contains = watch.predicate.get("subject_contains") if isinstance(watch.predicate, dict) else None
            account_id_equals = watch.predicate.get("account_id_equals") if isinstance(watch.predicate, dict) else None
            signal_account_id = str(signal.data.get("account_id") or "").strip() if isinstance(signal.data, dict) else ""
            logger.info(
                "SignalRouter prefilter semantic trace registration_id=%s semantic_query=%r sender_equals=%r sender_contains=%r subject_contains=%r account_id_equals=%r signal_account_id=%r",
                watch.registration_id,
                semantic_query,
                sender_equals,
                sender_contains,
                subject_contains,
                account_id_equals,
                signal_account_id,
            )
            if account_id_equals is not None:
                expected_account_id = str(account_id_equals).strip()
                if not signal_account_id:
                    return GarbageFilterDecision(
                        action="drop",
                        reason_code="email_watch_missing_signal_account_id",
                        details=f"email watch requires account_id={expected_account_id}, signal had no account_id",
                        normalized_content=content,
                        tags=["prefilter", "email_watch", "missing_account_id"],
                    )
                if signal_account_id != expected_account_id:
                    return GarbageFilterDecision(
                        action="drop",
                        reason_code="email_watch_account_mismatch",
                        details=f"email watch account mismatch signal={signal_account_id} expected={expected_account_id}",
                        normalized_content=content,
                        tags=["prefilter", "email_watch", "account_mismatch"],
                    )

            # Deterministic sender checks — avoids LLM call for clear sender mismatches.
            # Canonical fields normalised at signal construction time:
            #   sender_display — human name e.g. "Jane Smith"
            #   sender_email   — RFC 5322 address e.g. "user@example.com"
            signal_sender_display = str(signal.data.get("sender_display") or signal.data.get("sender") or "").strip().lower() if isinstance(signal.data, dict) else ""
            signal_sender_email = str(signal.data.get("sender_email") or signal.data.get("email_address") or "").strip().lower() if isinstance(signal.data, dict) else ""

            if sender_equals is not None:
                expected = str(sender_equals).strip().lower()
                # Compare against email address when predicate is an email, display name otherwise.
                actual = signal_sender_email if "@" in expected else signal_sender_display
                if actual != expected:
                    logger.info(
                        "SignalRouter prefilter sender_equals drop registration_id=%s expected=%r actual=%r (display=%r email=%r)",
                        watch.registration_id,
                        expected,
                        actual,
                        signal_sender_display,
                        signal_sender_email,
                    )
                    return GarbageFilterDecision(
                        action="drop",
                        reason_code="email_watch_sender_mismatch",
                        details=f"sender_equals mismatch: expected={expected} actual={actual}",
                        normalized_content=content,
                        tags=["prefilter", "email_watch", "sender_mismatch"],
                    )
            if sender_contains is not None:
                fragment = str(sender_contains).strip().lower()
                haystack = signal_sender_email if "@" in fragment else signal_sender_display
                if fragment and fragment not in haystack:
                    logger.info(
                        "SignalRouter prefilter sender_contains drop registration_id=%s fragment=%r actual=%r",
                        watch.registration_id,
                        fragment,
                        haystack,
                    )
                    return GarbageFilterDecision(
                        action="drop",
                        reason_code="email_watch_sender_not_contains",
                        details=f"sender_contains mismatch: fragment={fragment} actual={haystack}",
                        normalized_content=content,
                        tags=["prefilter", "email_watch", "sender_mismatch"],
                    )
            if subject_contains is not None:
                fragment = str(subject_contains).strip().lower()
                signal_subject = str(signal.data.get("subject") or "").strip().lower() if isinstance(signal.data, dict) else ""
                if fragment and fragment not in signal_subject:
                    logger.info(
                        "SignalRouter prefilter subject_contains drop registration_id=%s fragment=%r actual=%r",
                        watch.registration_id,
                        fragment,
                        signal_subject,
                    )
                    return GarbageFilterDecision(
                        action="drop",
                        reason_code="email_watch_subject_not_contains",
                        details=f"subject_contains mismatch: fragment={fragment} actual={signal_subject}",
                        normalized_content=content,
                        tags=["prefilter", "email_watch", "subject_mismatch"],
                    )

            return GarbageFilterDecision(
                action="keep",
                reason_code="email_semantic_prefilter_pass",
                details=f"email semantic watch forwarded to watcher for {watch.watch_key}",
                normalized_content=content,
                tags=["prefilter", "email_semantic_watch"],
            )

        if watch.watch_type == "semantic_match":
            # Semantic watches are intentionally evaluated by watcher agent.
            # Prefilter should not block them based on keyword-only logic.
            semantic_query = watch.predicate.get("semantic_query") if isinstance(watch.predicate, dict) else None
            sender_equals = watch.predicate.get("sender_equals") if isinstance(watch.predicate, dict) else None
            sender_contains = watch.predicate.get("sender_contains") if isinstance(watch.predicate, dict) else None
            subject_contains = watch.predicate.get("subject_contains") if isinstance(watch.predicate, dict) else None
            logger.info(
                "SignalRouter prefilter semantic trace registration_id=%s semantic_query=%r sender_equals=%r sender_contains=%r subject_contains=%r",
                watch.registration_id,
                semantic_query,
                sender_equals,
                sender_contains,
                subject_contains,
            )
            return GarbageFilterDecision(
                action="keep",
                reason_code="semantic_watch_prefilter_pass",
                details=f"semantic watch forwarded to watcher for {watch.watch_key}",
                normalized_content=content,
                tags=["prefilter", "semantic_watch"],
            )

        return GarbageFilterDecision(
            action="drop",
            reason_code="unsupported_watch_type",
            details=f"unsupported watch_type={watch.watch_type}",
            normalized_content=content,
            tags=["prefilter", "unsupported"],
        )
