"""Services for locator correction records."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.locators.corrections import normalize_target_description
from app.locators.url_pattern import generalize_url
from app.models import LocatorCorrection, LocatorCorrectionEvent, TestCaseRun, User
from app.schemas.corrections import (
    BatchUpdateCorrectionStateRequest,
    CreateCorrectionRequest,
    LocatorCorrectionsOverview,
    LocatorCorrectionEventPoint,
    StoredLocatorCorrection,
    StoredLocatorCorrectionEvent,
    UpdateCorrectionStateRequest,
)
from app.services.cases import EntityNotFoundError


logger = logging.getLogger(__name__)


class CorrectionConflictError(ValueError):
    """Raised when a correction activation conflicts with an existing active record."""


CONFLICT_DETAIL = "Another active correction already exists for the same page URL pattern and target description."


def create_correction(session: Session, payload: CreateCorrectionRequest) -> StoredLocatorCorrection:
    _ensure_user_exists(session, payload.created_by)
    _ensure_execution_exists(session, payload.source_execution_id)
    page_url_pattern = generalize_url(payload.page_url)
    normalized_target = normalize_target_description(payload.target_description)

    existing_records = _lock_lookup_records(
        session,
        page_url_pattern=page_url_pattern,
        normalized_target_description=normalized_target,
    )
    existing_active_records = [record for record in existing_records if record.is_active]

    try:
        if existing_active_records:
            target_record = existing_active_records[0]
            target_record.correction_type = payload.correction_type
            target_record.correction_value = payload.correction_value
            target_record.source_execution_id = payload.source_execution_id
            session.add(target_record)
            session.flush()
            _add_correction_event(
                session, target_record,
                event_type="created",
                execution_id=payload.source_execution_id,
            )
            session.commit()
            session.refresh(target_record)

            logger.info(
                "Updated existing locator correction id=%s page_url_pattern=%s target=%s "
                "correction_type=%s correction_value=%s",
                target_record.id,
                target_record.page_url_pattern,
                target_record.target_description,
                target_record.correction_type,
                target_record.correction_value,
            )
            return _to_stored_locator_correction(target_record)

        correction = LocatorCorrection(
            page_url_pattern=page_url_pattern,
            target_description=payload.target_description,
            normalized_target_description=normalized_target,
            correction_type=payload.correction_type,
            correction_value=payload.correction_value,
            source_execution_id=payload.source_execution_id,
            created_by=payload.created_by,
        )
        session.add(correction)
        session.flush()
        _add_correction_event(session, correction, event_type="created", execution_id=payload.source_execution_id)
        session.commit()
        session.refresh(correction)

        logger.info(
            "Created locator correction id=%s page_url_pattern=%s target=%s",
            correction.id,
            correction.page_url_pattern,
            correction.target_description,
        )
        return _to_stored_locator_correction(correction)
    except IntegrityError as exc:
        session.rollback()
        raise CorrectionConflictError(CONFLICT_DETAIL) from exc


def list_corrections(
    session: Session,
    *,
    target_description: str | None = None,
    page_url: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[StoredLocatorCorrection]:
    statement = select(LocatorCorrection).order_by(LocatorCorrection.updated_at.desc(), LocatorCorrection.id.desc())
    if target_description is not None:
        statement = statement.where(
            LocatorCorrection.normalized_target_description == normalize_target_description(target_description)
        )
    if page_url is not None:
        statement = statement.where(LocatorCorrection.page_url_pattern == generalize_url(page_url))
    if is_active is not None:
        statement = statement.where(LocatorCorrection.is_active.is_(is_active))
    statement = statement.limit(limit).offset(offset)
    records = session.scalars(statement).all()
    logger.debug(
        "Listed locator corrections target_filter=%s page_filter=%s is_active=%s limit=%s offset=%s count=%s",
        target_description,
        page_url,
        is_active,
        limit,
        offset,
        len(records),
    )
    return [_to_stored_locator_correction(record) for record in records]


def update_correction_state(
    session: Session,
    correction_id: int,
    payload: UpdateCorrectionStateRequest,
) -> StoredLocatorCorrection:
    correction = session.get(LocatorCorrection, correction_id)
    if correction is None:
        raise EntityNotFoundError(f"Correction {correction_id} not found.")

    if payload.is_active == correction.is_active:
        return _to_stored_locator_correction(correction)

    if payload.is_active:
        _ensure_activation_conflict_free(session, [correction])

    correction.is_active = payload.is_active
    session.add(correction)
    try:
        session.flush()
        _add_correction_event(session, correction, event_type="activated" if payload.is_active else "deactivated")
        session.commit()
        session.refresh(correction)
    except IntegrityError as exc:
        session.rollback()
        raise CorrectionConflictError(CONFLICT_DETAIL) from exc

    logger.info(
        "Updated locator correction state id=%s is_active=%s",
        correction.id,
        correction.is_active,
    )
    return _to_stored_locator_correction(correction)


def batch_update_correction_state(
    session: Session,
    payload: BatchUpdateCorrectionStateRequest,
) -> list[StoredLocatorCorrection]:
    correction_ids = list(dict.fromkeys(payload.correction_ids))
    records = session.scalars(
        select(LocatorCorrection)
        .where(LocatorCorrection.id.in_(correction_ids))
        .order_by(LocatorCorrection.id.asc())
    ).all()
    record_by_id = {record.id: record for record in records}
    missing_ids = [correction_id for correction_id in correction_ids if correction_id not in record_by_id]
    if missing_ids:
        raise EntityNotFoundError(f"Correction {missing_ids[0]} not found.")

    changed_records = [
        record_by_id[correction_id]
        for correction_id in correction_ids
        if record_by_id[correction_id].is_active != payload.is_active
    ]
    if not changed_records:
        return [record_by_id[correction_id] for correction_id in correction_ids]

    if payload.is_active:
        _ensure_activation_conflict_free(session, changed_records)

    try:
        for record in changed_records:
            record.is_active = payload.is_active
            session.add(record)
        session.flush()
        event_type = "activated" if payload.is_active else "deactivated"
        for record in changed_records:
            _add_correction_event(session, record, event_type=event_type)
        session.commit()
        for record in changed_records:
            session.refresh(record)
    except IntegrityError as exc:
        session.rollback()
        raise CorrectionConflictError(CONFLICT_DETAIL) from exc

    return [_to_stored_locator_correction(record_by_id[correction_id]) for correction_id in correction_ids]


def list_correction_events(
    session: Session,
    correction_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[StoredLocatorCorrectionEvent]:
    _ensure_correction_exists(session, correction_id)
    statement = (
        select(LocatorCorrectionEvent)
        .where(LocatorCorrectionEvent.correction_id == correction_id)
        .order_by(LocatorCorrectionEvent.created_at.desc(), LocatorCorrectionEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    events = session.scalars(statement).all()
    return [_to_stored_locator_correction_event(event) for event in events]


def get_corrections_overview(session: Session, *, window_days: int = 7) -> LocatorCorrectionsOverview:
    today = datetime.now(UTC).date()
    window_start = today - timedelta(days=window_days - 1)
    range_start = datetime.combine(window_start, time.min)
    range_end_exclusive = datetime.combine(today + timedelta(days=1), time.min)

    total_count = session.scalar(select(func.count()).select_from(LocatorCorrection)) or 0
    active_count = (
        session.scalar(
            select(func.count()).select_from(LocatorCorrection).where(LocatorCorrection.is_active.is_(True))
        )
        or 0
    )

    event_rows = session.execute(
        select(LocatorCorrectionEvent.event_type, LocatorCorrectionEvent.created_at).where(
            LocatorCorrectionEvent.created_at >= range_start,
            LocatorCorrectionEvent.created_at < range_end_exclusive,
        )
    ).all()

    hit_count = 0
    miss_count = 0
    auto_deactivated_count = 0
    trend_buckets: dict[date, dict[str, int]] = defaultdict(lambda: {"hit_count": 0, "miss_count": 0})
    for event_type, created_at in event_rows:
        event_date = created_at.date()
        if event_type == "tier0_hit":
            hit_count += 1
            trend_buckets[event_date]["hit_count"] += 1
        elif event_type == "tier0_miss":
            miss_count += 1
            trend_buckets[event_date]["miss_count"] += 1
        elif event_type == "auto_deactivated":
            auto_deactivated_count += 1

    return LocatorCorrectionsOverview(
        total_count=total_count,
        active_count=active_count,
        inactive_count=max(0, total_count - active_count),
        hit_count=hit_count,
        miss_count=miss_count,
        auto_deactivated_count=auto_deactivated_count,
        current_window_start=window_start,
        current_window_end=today,
        trend_points=_build_correction_trend_points(window_start, today, trend_buckets),
    )


def delete_correction(session: Session, correction_id: int) -> None:
    """Delete a locator correction and its cascade events."""
    record = session.get(LocatorCorrection, correction_id)
    if record is None:
        raise EntityNotFoundError(f"Correction {correction_id} not found.")
    session.delete(record)
    session.commit()


def _ensure_user_exists(session: Session, user_id: int) -> None:
    if session.get(User, user_id) is None:
        raise EntityNotFoundError(f"User {user_id} not found.")


def _ensure_execution_exists(session: Session, execution_id: int) -> None:
    if session.get(TestCaseRun, execution_id) is None:
        raise EntityNotFoundError(f"Execution {execution_id} not found.")


def _ensure_correction_exists(session: Session, correction_id: int) -> None:
    if session.get(LocatorCorrection, correction_id) is None:
        raise EntityNotFoundError(f"Correction {correction_id} not found.")


def _lock_lookup_records(
    session: Session,
    *,
    page_url_pattern: str,
    normalized_target_description: str,
) -> list[LocatorCorrection]:
    statement = (
        select(LocatorCorrection)
        .where(
            LocatorCorrection.page_url_pattern == page_url_pattern,
            LocatorCorrection.normalized_target_description == normalized_target_description,
        )
        .order_by(LocatorCorrection.updated_at.desc(), LocatorCorrection.id.desc())
    )
    if _supports_for_update(session):
        statement = statement.with_for_update()
    return session.scalars(statement).all()


def _supports_for_update(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _ensure_activation_conflict_free(session: Session, records: list[LocatorCorrection]) -> None:
    grouped_records: dict[tuple[str, str], list[LocatorCorrection]] = defaultdict(list)
    for record in records:
        grouped_records[(record.page_url_pattern, record.normalized_target_description)].append(record)

    if any(len(grouped) > 1 for grouped in grouped_records.values()):
        raise CorrectionConflictError(CONFLICT_DETAIL)

    activating_ids = {record.id for record in records}
    for (page_url_pattern, normalized_target_description), grouped in grouped_records.items():
        related_records = _lock_lookup_records(
            session,
            page_url_pattern=page_url_pattern,
            normalized_target_description=normalized_target_description,
        )
        conflicting_active = next(
            (
                related
                for related in related_records
                if related.id not in activating_ids and related.is_active
            ),
            None,
        )
        if conflicting_active is not None:
            raise CorrectionConflictError(CONFLICT_DETAIL)


def _add_correction_event(
    session: Session,
    correction: LocatorCorrection,
    *,
    event_type: str,
    execution_id: int | None = None,
) -> None:
    session.add(
        LocatorCorrectionEvent(
            correction_id=correction.id,
            event_type=event_type,
            page_url_pattern=correction.page_url_pattern,
            target_description=correction.target_description,
            execution_id=execution_id,
            verified_count_after=correction.verified_count,
            consecutive_failures_after=correction.consecutive_failures,
            is_active_after=correction.is_active,
        )
    )


def _to_stored_locator_correction(record: LocatorCorrection) -> StoredLocatorCorrection:
    return StoredLocatorCorrection(
        id=record.id,
        page_url_pattern=record.page_url_pattern,
        target_description=record.target_description,
        correction_type=record.correction_type,
        correction_value=record.correction_value,
        verified_count=record.verified_count,
        consecutive_failures=record.consecutive_failures,
        is_active=record.is_active,
        source_execution_id=record.source_execution_id,
        created_by=record.created_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_stored_locator_correction_event(record: LocatorCorrectionEvent) -> StoredLocatorCorrectionEvent:
    return StoredLocatorCorrectionEvent(
        id=record.id,
        correction_id=record.correction_id,
        event_type=record.event_type,
        page_url_pattern=record.page_url_pattern,
        target_description=record.target_description,
        execution_id=record.execution_id,
        verified_count_after=record.verified_count_after,
        consecutive_failures_after=record.consecutive_failures_after,
        is_active_after=record.is_active_after,
        created_at=record.created_at,
    )


def _build_correction_trend_points(
    start_date: date,
    end_date: date,
    buckets: dict[date, dict[str, int]],
) -> list[LocatorCorrectionEventPoint]:
    total_days = (end_date - start_date).days + 1
    return [
        LocatorCorrectionEventPoint(
            date=current_date,
            hit_count=buckets[current_date]["hit_count"],
            miss_count=buckets[current_date]["miss_count"],
        )
        for current_date in (start_date + timedelta(days=offset) for offset in range(total_days))
    ]
