"""Cases router — timeline + status + responses + duplicate-detection endpoints.

Eight endpoints, all behind the new case-domain (not the chat-era
surface, which is deprecated and being sunset 2026-09-04):

  - GET   /cases/{case_id}/timeline
  - POST  /cases/{case_id}/status
  - PATCH /cases/{case_id}/status
  - GET   /cases/{case_id}/responses
  - POST  /cases/{case_id}/responses
  - POST  /cases/duplicates/check         (read-only, no auth above login)
  - POST  /cases/                         (citizen + admin; new filing)
  - POST  /cases/{case_id}/support        (citizen + admin; co-reporting)

The GET endpoints are read-only and permitted for admin, the reporter,
and any MP. POST/PATCH on `/status` apply a workflow transition and are
restricted to MP and admin (citizens cannot mutate case status). POST
on `/responses` is the citizen-facing message thread: any party to the
case (admin / reporter / MP) may post; `is_internal=True` is reserved
to MP and admin (citizens cannot create internal MP notes). The three
duplicate-detection endpoints implement the spec at
`Intelligent Duplicate Detection.txt`: the citizen is shown similar
cases before they file, can choose to support an existing case OR
create a new one, and the support association is stored in the
`case_support` table (separate from the Case itself).

Anonymity invariant (spec STEP 6):

  The timeline events use `actor_role` only — never the actor's name,
  email, phone, photo, or any other PII. The GET response is the same
  shape regardless of who is viewing, exactly because the actor block
  on each row is empty-by-design. Responses echo `author_role` only —
  not the actor's name or email. Duplicate candidates echo ONLY the
  Case row columns (case_number, title, description, district,
  similarity, support_count) — never the reporter's user_id or any
  User-side identifier. The wire-format schema
  (`CaseDuplicateCandidateOut`) is the enforcement point; adding a
  PII-shaped field there is a CODE REVIEW PIN.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.cases import (
    can_view_case,
    get_case_for_viewer,
    require_case_actor,
)
from app.enums import CaseAuditAction, CasePriority, CaseStatus, CaseTimelineEventType
from app.models import (
    Case,
    CaseAssignment,
    CaseAttachment,
    CaseResponse,
    CaseSupport,
    CaseTimeline,
    User,
)
from app.schemas import Role
from app.schemas_case import (
    CaseAssignmentCreate,
    CaseAssignmentResponse,
    CaseAttachmentResponse,
    CaseAttachmentUploadResponse,
    CaseCreate,
    CaseCreateResponse,
    CaseDetailOut,
    CaseDuplicateCandidateOut,
    CaseDuplicateCheckRequest,
    CaseDuplicateCheckResponse,
    CaseListItemOut,
    CaseReporterAnonymousSummary,
    CaseResponseCreate,
    CaseResponseResponse,
    CaseSupportCreate,
    CaseSupportResponse,
    CaseTimelineResponse,
    CaseTransitionRequest,
    CloseCaseCreate,
    InformationRequestCreate,
    MPQueueItemOut,
    MPQueueListResponse,
)
from app.services.cases.assignments import get_viewer_mp_profile
from app.services.cases.audit import log_audit_event
from app.services.cases.duplicates import find_duplicate_candidates
from app.services.cases.numbers import next_case_number
from app.services.cases.timeline import (
    record_attachment_added,
    record_information_requested,
    record_response_added,
)
from app.services.cases.workflow import InvalidTransition, apply_transition

router = APIRouter(prefix="/cases", tags=["cases"])


def _user_role_str(user: User) -> str:
    """Normalise the User.role (Enum or str) to a string."""
    role = user.role
    return role.value if hasattr(role, "value") else str(role)


def _is_admin(user: User) -> bool:
    role = _user_role_str(user)
    return role == Role.ADMIN.value


# ============================================================================
# GET /cases/{case_id}/timeline
# ============================================================================


@router.get(
    "/{case_id}/timeline",
    response_model=list[CaseTimelineResponse],
    summary="List timeline events for a case",
)
async def get_case_timeline(
    case_id: int = Path(..., ge=1),
    limit: int = Query(
        50,
        ge=1,
        le=200,
        description="Maximum number of timeline rows to return.",
    ),
    before_id: int | None = Query(
        None,
        ge=1,
        description=(
            "Cursor: return rows with id strictly less than this value. "
            "Use the smallest id from the previous page to fetch the "
            "next page."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[CaseTimelineResponse]:
    """Return timeline rows for a case, ascending by created_at.

    Pagination strategy: keyset (cursor) on `id`, ordered ascending.
    The caller requests a page with `limit` and (for subsequent pages)
    `before_id` set to the smallest id they already hold. Default
    `limit=50`, max `200`. This avoids OFFSET scanning which degrades
    on long-running cases.

    Authorisation:
    - admin: always allowed
    - reporter themselves: allowed
    - any active MP: allowed
    - everyone else: 403
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    if not can_view_case(
        case,
        viewer_user_id=user.id,
        is_admin=_is_admin(user),
        viewer_role=role,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this case.",
        )

    stmt = (
        select(CaseTimeline)
        .where(CaseTimeline.case_id == case_id)
        .order_by(CaseTimeline.id.asc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(CaseTimeline.id < before_id)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    # Audit the read — case reads are security-relevant (the Future
    # DPIA will require this in the audit log).
    await log_audit_event(
        db,
        case_id=case_id,
        action=CaseAuditAction.CASE_VIEWED,
        actor_user_id=user.id,
        actor_role=role,
        payload={"endpoint": "GET /cases/{id}/timeline", "row_count": len(rows)},
    )
    await db.commit()

    return [CaseTimelineResponse.model_validate(r) for r in rows]


# ============================================================================
# POST /cases/{case_id}/status  — first creation-style submit
# ============================================================================
# Per the design discussion: POST is the create-or-initial-submit verb;
# the body is the same shape as PATCH. Both go through apply_transition
# under the hood and emit the same audit + timeline rows.


@router.post(
    "/{case_id}/status",
    response_model=CaseTimelineResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a status transition (POST verb)",
)
async def post_case_status(
    payload: CaseTransitionRequest,
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseTimelineResponse:
    """Apply a status transition to an existing case via POST.

    Restrictions: only MPs and admins may POST.
    """
    return await _apply_status(
        db=db,
        case_id=case_id,
        user=user,
        payload=payload,
    )


# ============================================================================
# PATCH /cases/{case_id}/status  — idempotent status edit
# ============================================================================
# Same implementation as POST; PATCH is reserved per the spec for
# idempotent edits. The HTTP verb difference is enough to satisfy both
# the "Submit" workflow verb and the REST design discussion.


@router.patch(
    "/{case_id}/status",
    response_model=CaseTimelineResponse,
    summary="Apply a status transition (PATCH verb, idempotent)",
)
async def patch_case_status(
    payload: CaseTransitionRequest,
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseTimelineResponse:
    """Apply a status transition to an existing case via PATCH."""
    return await _apply_status(
        db=db,
        case_id=case_id,
        user=user,
        payload=payload,
    )


# ============================================================================
# Shared implementation
# ============================================================================


async def _apply_status(
    *,
    db: AsyncSession,
    case_id: int,
    user: User,
    payload: CaseTransitionRequest,
) -> CaseTimelineResponse:
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    require_case_actor(
        case=case,
        viewer=user,
        is_admin=_is_admin(user),
        viewer_role=role,
    )

    try:
        await apply_transition(
            db,
            case=case,
            to_status=payload.to_status,
            actor_user_id=user.id,
            actor_role=role,
            description=payload.description,
        )
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot move from {exc.from_status.value!r} to "
                f"{exc.to_status.value!r}."
            ),
        ) from exc

    await db.commit()
    # Refresh the case to pick up any state changes apply_transition
    # made (status, resolved_at) so the freshly-inserted timeline row
    # in the result is then read in a fresh state.
    await db.refresh(case)

    # Pull the just-written CaseTimeline row for the response. We
    # know there's exactly one timeline row from this call because
    # apply_transition writes one row per transition, and we just
    # committed.
    stmt = (
        select(CaseTimeline)
        .where(CaseTimeline.case_id == case_id)
        .order_by(CaseTimeline.created_at.desc(), CaseTimeline.id.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one()
    return CaseTimelineResponse.model_validate(row)


# ============================================================================
# GET /cases/{case_id}/responses — message thread
# ============================================================================
# The thread is part of the case's public record for the parties to
# the case (admin / reporter / MP). Internal MP notes (`is_internal=True`)
# are filtered out for non-MP, non-admin viewers — citizens NEVER see
# the MP's internal scratchpad.
#
# Pagination is intentionally absent here for now (see task #161); the
# case-detail page is the only consumer in this PR and the thread is
# expected to be short (handful of messages per case).
# ============================================================================


@router.get(
    "/{case_id}/responses",
    response_model=list[CaseResponseResponse],
    summary="List responses in the case message thread",
)
async def get_case_responses(
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[CaseResponseResponse]:
    """Return all CaseResponse rows for a case, ascending by created_at.

    Authorisation:
    - admin: always allowed
    - reporter themselves: allowed (sees only non-internal)
    - any MP: allowed (sees ALL including internal notes)
    - everyone else: 403

    Visibility of internal notes is filtered here at the query level:
    MPs and admins see both `is_internal=False` and `is_internal=True`
    rows. The reporter (and any other future citizen-side viewer) sees
    only `is_internal=False`. This is the read-side enforcement of
    the spec rule "MPs have private internal notes that the citizen
    must not see".
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    if not can_view_case(
        case,
        viewer_user_id=user.id,
        is_admin=_is_admin(user),
        viewer_role=role,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this case.",
        )

    stmt = select(CaseResponse).where(CaseResponse.case_id == case_id)
    # Citizens can NEVER see internal MP notes. MPs and admins see all.
    if not (_is_admin(user) or role == "mp"):
        stmt = stmt.where(CaseResponse.is_internal.is_(False))
    stmt = stmt.order_by(
        CaseResponse.created_at.asc(),
        CaseResponse.id.asc(),
    )
    rows = (await db.execute(stmt)).scalars().all()

    # Audit the read — same posture as GET /timeline.
    await log_audit_event(
        db,
        case_id=case_id,
        action=CaseAuditAction.CASE_VIEWED,
        actor_user_id=user.id,
        actor_role=role,
        payload={
            "endpoint": "GET /cases/{id}/responses",
            "row_count": len(rows),
            "saw_internal": _is_admin(user) or role == "mp",
        },
    )
    await db.commit()

    return [CaseResponseResponse.model_validate(r) for r in rows]


# ============================================================================
# POST /cases/{case_id}/responses — write a message in the thread
# ============================================================================
# The reporter is allowed to post on their OWN case (their own
# thread), in response to INFORMATION_REQUESTED or simply to add
# new information. MPs and admins may also post. Internal notes
# (`is_internal=True`) are reserved to MPs and admins.
#
# This endpoint is the citizen-facing write path that the previous
# PR's `record_response_added()` helper was set up to support.
# ============================================================================


@router.post(
    "/{case_id}/responses",
    response_model=CaseResponseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Post a response in the case message thread",
)
async def post_case_response(
    payload: CaseResponseCreate,
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseResponseResponse:
    """Add a CaseResponse to a case's message thread.

    Authorisation:
    - admin: always allowed, may post internal notes
    - reporter themselves: allowed (public reply only)
    - any MP: allowed (public reply AND internal note)
    - everyone else: 403

    Side effects:
    - One `CaseResponse` row written
    - One `CaseTimeline` row (event_type = response_added)
    - One `CaseAuditLog` row (action = response_added)
    All in one transaction so a failure rolls everything back.
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    is_admin = _is_admin(user)

    # The reporter themselves may post on their own case. Anyone else
    # is denied. This is a smaller circle than `can_view_case` —
    # viewing is broader (e.g. MPs in other regions may browse), but
    # writing must be restricted to actual participants.
    is_reporter = (
        user.id is not None
        and case.reporter_user_id is not None
        and int(user.id) == int(case.reporter_user_id)
    )
    is_participant = is_admin or (role == "mp") or is_reporter
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to post on this case.",
        )

    # Citizens cannot create internal notes — only MPs and admins may.
    if payload.is_internal and not (is_admin or role == "mp"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal notes can only be created by MPs or admins.",
        )

    response = CaseResponse(
        case_id=case_id,
        author_user_id=user.id,
        author_role=role,
        body=payload.body.strip(),
        is_internal=payload.is_internal,
    )
    db.add(response)
    # Flush so response.id is populated for the timeline helper.
    await db.flush()

    await record_response_added(
        db,
        case_id=case_id,
        response_id=response.id,
        actor_user_id=user.id,
        actor_role=role,
        is_internal=payload.is_internal,
    )

    await db.commit()
    await db.refresh(response)
    return CaseResponseResponse.model_validate(response)


# ============================================================================
# POST /cases/duplicates/check — read-only duplicate detection
# ============================================================================
# Spec (`Intelligent Duplicate Detection.txt`):
#   Before a case is created compare Topic, Description, Category,
#   Location, Recent submissions using PostgreSQL Full Text Search.
#
# This endpoint NEVER mutates state. It returns up to 5 ranked existing
# cases that look similar to a would-be new case. The citizen UI then
# shows the dialog: "Support Existing Case or Create New Case".
#
# Anonymity: the response carries ONLY the Case row columns plus a
# similarity score. No reporter_user_id, no User-side fields. The
# service layer (`services/cases/duplicates.py`) does not import
# `User` either — defence in depth.
# ============================================================================


@router.post(
    "/duplicates/check",
    response_model=CaseDuplicateCheckResponse,
    summary="Find recently-filed cases similar to a would-be new case",
)
async def post_duplicate_check(
    payload: CaseDuplicateCheckRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseDuplicateCheckResponse:
    """Return up to 5 ranked existing cases similar to the would-be new case.

    Auth: any logged-in user (citizen / MP / admin). Read-only.

    The ranking combines:
      - tsvector @@ tsquery match (existing GIN index `ix_cases_search_vector`)
      - pg_trgm similarity (extension + index `ix_cases_title_desc_trgm`)
      - category_id exact-match bonus
      - district_id exact-match bonus

    Excludes terminal statuses (withdrawn / rejected / closed). Restricts
    to the last 30 days. Hard cap at 5 candidates.
    """
    candidates = await find_duplicate_candidates(
        db,
        title=payload.title,
        description=payload.description,
        category_id=payload.category_id,
        district_id=payload.district_id,
        language=payload.language,
    )

    # The check itself is a read-only query and the spec doesn't require
    # an audit row here (the response is the same shape whether or not
    # anyone logs it; the endpoint has no specific case_id to attach to
    # since the would-be case doesn't exist yet). The MP-facing
    # PII-stripping is enforced by the service layer
    # (`find_duplicate_candidates` only reads `cases.*`).

    return CaseDuplicateCheckResponse(
        candidates=[CaseDuplicateCandidateOut.model_validate(c) for c in candidates],
        checked_at=datetime.now(tz=UTC),
    )


# ============================================================================
# POST /cases/ — file a new case (citizen filing flow)
# ============================================================================
# This is the wire entry to the citizen-side "Create a New Case" flow.
# The frontend modal (`src/pages/CreateCase.tsx`) calls this after the
# user has either:
#   (a) clicked "Support this case" on a candidate (in which case the
#       frontend has called `POST /cases/{id}/support` instead — they
#       do NOT reach this endpoint), or
#   (b) explicitly chosen "Continue creating new case" in the duplicate
#       dialog and then clicked "File".
#
# Auth: citizen OR admin only. MPs may NOT file their own cases — they
# are the responders, not the reporters. Anonymous filing (no reporter)
# is also refused here; it is a USSD / admin tooling flow that lives
# outside the auth-driven web UI.
# ============================================================================


@router.post(
    "/",
    response_model=CaseCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="File a new case",
)
async def post_case_create(
    payload: CaseCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseCreateResponse:
    """File a new case.

    Side effects (all in one transaction so a failure rolls back):
      1. One `Case` row (case_number via `next_case_number()`).
      2. One `CaseTimeline` row (event_type = case_created).
      3. One `CaseAuditLog` row (action = case_created).
    """
    role = _user_role_str(user)
    if role == "mp":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MPs may not file cases.",
        )
    if payload.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Anonymous filing is supported via USSD / admin tools only."
            ),
        )

    case_number = await next_case_number(db)
    case = Case(
        case_number=case_number,
        reporter_user_id=user.id,
        display_handle=payload.display_handle or "Anonymous Citizen",
        category_id=payload.category_id,
        title=payload.title,
        description=payload.description,
        district_id=payload.district_id,
        is_anonymous=False,
        language=payload.language,
        # status defaults to "submitted" via the column server_default.
        # Explicit assignment here is defensive — keeps the wire value
        # identical regardless of DB defaults.
        status=CaseStatus.SUBMITTED.value,
    )
    db.add(case)
    await db.flush()  # populate case.id for the timeline + audit rows

    timeline = CaseTimeline(
        case_id=case.id,
        event_type=CaseTimelineEventType.CASE_CREATED.value,
        from_status=None,
        to_status=CaseStatus.SUBMITTED.value,
        actor_role=role,
        actor_user_id=user.id,
        description="Case filed",
    )
    db.add(timeline)

    await log_audit_event(
        db,
        case_id=case.id,
        action=CaseAuditAction.CASE_CREATED,
        actor_user_id=user.id,
        actor_role=role,
        payload={
            "case_number": case_number,
            "category_id": payload.category_id,
            "district_id": payload.district_id,
            "language": payload.language,
        },
    )

    await db.commit()
    await db.refresh(case)
    return CaseCreateResponse(
        id=case.id,
        case_number=case.case_number,
        status=CaseStatus(case.status),
        submitted_at=case.submitted_at,
    )


# ============================================================================
# POST /cases/{case_id}/support — declare an existing case as yours too
# ============================================================================
# Spec ("Store supporters separately"): when the citizen picks
# "Support Existing Case" in the duplicate dialog, this endpoint writes
# ONE CaseSupport row linking the authenticated user to the existing
# case. The existing case's `reporter_user_id` is NOT touched — the
# original reporter remains primary, the new supporter is recorded
# separately so the original's identity is preserved (anonymity
# invariant).
#
# Idempotency:
#   - The pre-check returns 409 if the user already has a CaseSupport
#     row on this case.
#   - The partial unique index `uq_case_support_pair` is the
#     race-safe backstop; the `IntegrityError` catch converts the
#     race into a 409 (otherwise it would surface as a 500).
#
# Auth: citizen OR admin only. MPs may NOT co-report.
# ============================================================================


@router.post(
    "/{case_id}/support",
    response_model=CaseSupportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Declare an existing case as your case too (duplicate support)",
)
async def post_case_support(
    case_id: int = Path(..., ge=1),
    payload: CaseSupportCreate | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseSupportResponse:
    """Add a CaseSupport row linking the authenticated user to the existing case.

    Auth: citizen OR admin. MPs may not co-report.
    """
    role = _user_role_str(user)
    if role == "mp":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MPs may not co-report cases.",
        )

    case = await get_case_for_viewer(db, case_id=case_id)
    if case.status in {"withdrawn", "rejected", "closed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case is terminal; cannot support.",
        )

    # Pre-check for a clean 409. The partial unique index is the
    # race-safe backstop (handled in the except clause below).
    existing = await db.execute(
        select(CaseSupport).where(
            CaseSupport.original_case_id == case_id,
            CaseSupport.supporter_user_id == user.id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already support this case.",
        )

    row = CaseSupport(
        original_case_id=case_id,
        duplicate_case_id=None,  # v1: only one-hop support; no ghost case stub
        supporter_user_id=user.id,
    )
    db.add(row)

    # Reuse CASE_VIEWED with a payload marker. A dedicated
    # `CASE_SUPPORTED` enum value is a candidate for a future PR.
    await log_audit_event(
        db,
        case_id=case_id,
        action=CaseAuditAction.CASE_VIEWED,
        actor_user_id=user.id,
        actor_role=role,
        payload={
            "event": "case_supported",
            "original_case_id": case_id,
            "note": (payload.note if payload else None),
        },
    )

    try:
        await db.commit()
    except IntegrityError:
        # The partial unique index caught a race between two concurrent
        # POSTs from the same user. Roll back the in-flight transaction
        # and surface a clean 409 — callers should treat 409 as
        # "already supported".
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already support this case.",
        ) from None

    await db.refresh(row)
    return CaseSupportResponse.model_validate(row)


# ============================================================================
# GET /cases/ — list cases visible to the viewer
# ============================================================================
# Citizen UI surface. The list shape is intentionally lightweight
# (no description body, no timeline, no attachments) — the full detail
# lives at GET /cases/{id}.
#
# Auth:
# - citizen: their own filed cases + cases they co-reported
# - admin: all cases (kept broad for now)
# - mp: cases routed to them or unassigned in their region
#
# Anonymity: the `reporter` block is the same anonymous summary shape
# for every viewer — even the reporter themselves sees
# `display_handle`, NEVER `first_name` / `email` / `phone`. This is
# enforced by `CaseReporterAnonymousSummary` (FORBIDDEN fields comment
# in schemas_case.py).
# ============================================================================


@router.get(
    "/",
    response_model=list[CaseListItemOut],
    summary="List cases visible to the viewer",
)
async def list_cases_for_viewer(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[CaseListItemOut]:
    """List cases the viewer can see on /cases.

    MP inbox (assigned + unassigned in region) and admin full list are
    basic shapes — the richer MP inbox is a follow-up PR.
    """
    role = _user_role_str(user)
    is_admin = _is_admin(user)

    if is_admin:
        stmt = select(Case).order_by(Case.submitted_at.desc()).limit(500)
    elif role == "mp":
        stmt = (
            select(Case)
            .where(Case.status.notin_(["withdrawn", "rejected", "closed"]))
            .order_by(Case.submitted_at.desc())
            .limit(500)
        )
    else:
        # Citizen: their own filed cases. Co-reported cases (where the
        # viewer has a CaseSupport row) are surfaced in a follow-up PR
        # via a UNION; for v1 the citizen sees only their own.
        stmt = (
            select(Case)
            .where(Case.reporter_user_id == user.id)
            .order_by(Case.submitted_at.desc())
            .limit(500)
        )

    rows = (await db.execute(stmt)).scalars().all()

    # Coalesce the support_count for all rows in one query
    # (avoids N+1 over the list).
    case_ids = [r.id for r in rows]
    support_counts: dict[int, int] = {}
    if case_ids:
        from sqlalchemy import func as sa_func
        sc = (
            select(CaseSupport.original_case_id, sa_func.count(CaseSupport.id))
            .where(CaseSupport.original_case_id.in_(case_ids))
            .group_by(CaseSupport.original_case_id)
        )
        for cid, count in (await db.execute(sc)).all():
            support_counts[int(cid)] = int(count)

    return [
        CaseListItemOut(
            id=r.id,
            case_number=r.case_number,
            status=CaseStatus(r.status),
            title=r.title,
            submitted_at=r.submitted_at,
            reporter=CaseReporterAnonymousSummary(
                display_handle=r.display_handle or "Anonymous Citizen",
                district_label=r.district_id,
            ),
            support_count=support_counts.get(r.id, 0),
        )
        for r in rows
    ]


# ============================================================================
# GET /cases/{case_id} — case detail for the /cases/:id page
# ============================================================================
# Returns the full case row + a `viewer_can_respond` flag the detail
# page uses to mount the composer + status form. The reporter block
# stays anonymous — even for the reporter themselves we project only
# `display_handle` + `district_label` (NOT first_name/email/etc).
# ============================================================================


@router.get(
    "/{case_id}",
    response_model=CaseDetailOut,
    summary="Read a single case (anonymized reporter block)",
)
async def get_case(
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseDetailOut:
    """Read a single case for the detail page.

    Auth: same as `can_view_case` — admin, reporter themselves, any MP.
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    if not can_view_case(
        case,
        viewer_user_id=user.id,
        is_admin=_is_admin(user),
        viewer_role=role,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this case.",
        )

    # support_count for this case
    from sqlalchemy import func as sa_func
    sc = (
        select(sa_func.count(CaseSupport.id))
        .where(CaseSupport.original_case_id == case_id)
    )
    support_count = int((await db.execute(sc)).scalar_one() or 0)

    # viewer_can_respond: admin / reporter themselves / any MP
    is_admin = _is_admin(user)
    is_reporter = (
        user.id is not None
        and case.reporter_user_id is not None
        and int(user.id) == int(case.reporter_user_id)
    )
    viewer_can_respond = bool(is_admin or (role == "mp") or is_reporter)

    # Audit the read.
    await log_audit_event(
        db,
        case_id=case_id,
        action=CaseAuditAction.CASE_VIEWED,
        actor_user_id=user.id,
        actor_role=role,
        payload={"endpoint": "GET /cases/{id}", "row_count": 1},
    )
    await db.commit()

    return CaseDetailOut(
        id=case.id,
        case_number=case.case_number,
        status=CaseStatus(case.status),
        title=case.title,
        description=case.description,
        district_id=case.district_id,
        language=case.language,
        submitted_at=case.submitted_at,
        resolved_at=case.resolved_at,
        reporter=CaseReporterAnonymousSummary(
            display_handle=case.display_handle or "Anonymous Citizen",
            district_label=case.district_id,
        ),
        support_count=support_count,
        viewer_can_respond=viewer_can_respond,
    )


# ============================================================================
# Case attachments — list / upload / delete
# ============================================================================
# The upload endpoint accepts a single multipart file (multipart/form-data;
# field name "file"). The binary is stored as a base64 data URL in
# `media_url`. This avoids needing a StaticFiles mount in `app/main.py`
# — the existing article-image upload uses Cloudinary, the case
# attachments use the inline pattern because the typical file size is
# < 25 MB and we want zero external-service dependency for evidence.
#
# Allowed MIME types: image/*, application/pdf, video/mp4, audio/*. The
# same rules are re-enforced on the client side (CaseAttachments.tsx)
# so we reject fast before upload. Server side the limits are 25 MB.
#
# Auth:
# - list: admin / reporter / MP / co-reporter (everyone in can_view_case)
# - upload: admin / reporter / MP (the parties to the case)
# - delete: admin OR the uploader themselves
# ============================================================================

ALLOWED_MIME_PREFIXES = ("image/", "video/", "audio/")
ALLOWED_MIME_EXACTES = ("application/pdf",)
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB


def _is_allowed_attachment(mime: str) -> bool:
    if not mime:
        return False
    if mime.startswith(ALLOWED_MIME_PREFIXES):
        return True
    return mime in ALLOWED_MIME_EXACTES


@router.get(
    "/{case_id}/attachments",
    response_model=list[CaseAttachmentResponse],
    summary="List attachments for a case",
)
async def list_case_attachments(
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[CaseAttachmentResponse]:
    """List attachments for a case.

    Auth: same as `can_view_case` (admin / reporter / MP).
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    if not can_view_case(
        case,
        viewer_user_id=user.id,
        is_admin=_is_admin(user),
        viewer_role=role,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this case.",
        )

    stmt = (
        select(CaseAttachment)
        .where(CaseAttachment.case_id == case_id)
        .order_by(CaseAttachment.created_at.desc(), CaseAttachment.id.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()

    await log_audit_event(
        db,
        case_id=case_id,
        action=CaseAuditAction.CASE_VIEWED,
        actor_user_id=user.id,
        actor_role=role,
        payload={"endpoint": "GET /cases/{id}/attachments", "row_count": len(rows)},
    )
    await db.commit()

    return [CaseAttachmentResponse.model_validate(r) for r in rows]


@router.post(
    "/{case_id}/attachments",
    response_model=CaseAttachmentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an attachment (multipart/form-data, field=file)",
)
async def upload_case_attachment(
    case_id: int = Path(..., ge=1),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseAttachmentUploadResponse:
    """Upload one attachment to a case.

    Stores the binary as a base64 data URL in `media_url`. Validates
    MIME type and byte size; both are re-checked server-side because
    the client side check is best-effort.
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    is_admin = _is_admin(user)
    is_reporter = (
        user.id is not None
        and case.reporter_user_id is not None
        and int(user.id) == int(case.reporter_user_id)
    )
    if not (is_admin or (role == "mp") or is_reporter):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only parties to this case may upload attachments.",
        )

    mime_type = (file.content_type or "").lower()
    if not _is_allowed_attachment(mime_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unsupported file type. Allowed: image/*, application/pdf, "
                "video/mp4, audio/*."
            ),
        )

    raw = await file.read()
    byte_size = len(raw)
    if byte_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )
    if byte_size > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Attachment exceeds the 25 MB limit.",
        )

    sha256 = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"

    # Map MIME -> media_type bucket (image / document / video / audio).
    if mime_type.startswith("image"):
        media_type = "image"
    elif mime_type.startswith("video"):
        media_type = "video"
    elif mime_type.startswith("audio"):
        media_type = "audio"
    else:
        media_type = "document"

    attachment = CaseAttachment(
        case_id=case_id,
        file_name=(file.filename or "upload")[:255],
        media_url=data_url,
        media_type=media_type,
        mime_type=mime_type,
        byte_size=byte_size,
        sha256=sha256,
        uploaded_by_id=user.id,
    )
    db.add(attachment)
    await db.flush()  # populate attachment.id for the timeline helper

    await record_attachment_added(
        db,
        case_id=case_id,
        attachment_id=attachment.id,
        actor_user_id=user.id,
        actor_role=role,
        media_type=media_type,
    )

    await db.commit()
    await db.refresh(attachment)

    return CaseAttachmentUploadResponse.model_validate(attachment)


@router.delete(
    "/{case_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an attachment (uploader or admin only)",
)
async def delete_case_attachment(
    case_id: int = Path(..., ge=1),
    attachment_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Delete an attachment. Only the uploader or an admin may delete."""
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    is_admin = _is_admin(user)

    # Parties to the case may attempt the delete — the per-row check
    # below restricts it to uploader-or-admin.
    is_participant = is_admin or (role == "mp") or (
        user.id is not None
        and case.reporter_user_id is not None
        and int(user.id) == int(case.reporter_user_id)
    )
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this case.",
        )

    stmt = select(CaseAttachment).where(
        CaseAttachment.id == attachment_id,
        CaseAttachment.case_id == case_id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found.",
        )

    # Per-row check: only the uploader or an admin may delete.
    if not is_admin and (
        row.uploaded_by_id is None
        or int(user.id) != int(row.uploaded_by_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the uploader or an admin may delete this attachment.",
        )

    await db.delete(row)
    await db.commit()
    # 204 No Content — no body.


# ============================================================================
# MP queue + dashboard endpoints
# ============================================================================
#
# Spec: "MP Inbox -> Case Management Dashboard".
#
# Auth:
# - All four endpoints below are gated to `role == "mp"`. The MP
#   queue filter requires an active MPProfile linked to the calling
#   user.
# - Admins have separate tooling; for the v1 PR they get 403 here
#   (future PR: an admin queue page).
#
# Anonymity:
# - The wire shape is `MPQueueItemOut` / `CaseReporterAnonymousSummary`
#   - no first_name, no email, no phone, no photo.
# - The MP's own user_id never enters the wire.
# - The MPProfile row IS referenced indirectly via the
#   `CaseAssignment.mp_profile_id`. That is an internal PK, not PII.
#
# Endpoints added:
# - GET    /cases/mp/queue
# - POST   /cases/{case_id}/assign-self
# - POST   /cases/{case_id}/unassign
# - POST   /cases/{case_id}/request-information
# - POST   /cases/{case_id}/close
# ============================================================================


async def _require_mp_viewer(
    db: AsyncSession, user: User,
):
    """Raise 403 unless the user is an MP with an MPProfile.

    Returns the resolved MPProfile so callers can use it for scoping.
    """
    role = _user_role_str(user)
    if role != Role.MP.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only MPs may perform this action.",
        )
    mp_profile = await get_viewer_mp_profile(db, user=user)
    if mp_profile is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No MPProfile linked to this user.",
        )
    return mp_profile


@router.get(
    "/mp/queue",
    response_model=MPQueueListResponse,
    summary="MP queue - cases assigned to the viewer's MPProfile",
)
async def list_mp_queue(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    status_filter: list[CaseStatus] | None = Query(
        None,
        alias="status",
        description=(
            "Multi-select status filter. Repeat `status=` per value, "
            "e.g. ?status=submitted&status=assigned."
        ),
    ),
    priority: list[CasePriority] | None = Query(
        None, description="Multi-select priority filter.",
    ),
    category_id: int | None = Query(None, ge=1),
    district_id: str | None = Query(None, max_length=80),
    region_id: int | None = Query(None, ge=1),
    date_from: datetime | None = Query(
        None, description="Inclusive lower bound on `submitted_at`.",
    ),
    date_to: datetime | None = Query(
        None, description="Inclusive upper bound on `submitted_at`.",
    ),
    search: str | None = Query(
        None,
        max_length=255,
        description="Case-insensitive substring on `case_number` or `title`.",
    ),
    sort: str = Query(
        "newest",
        pattern="^(newest|oldest|priority)$",
        description="`newest` (default), `oldest`, or `priority`.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> MPQueueListResponse:
    """List cases the viewer's MPProfile has an active assignment on.

    Filter params are all optional; the default is "every active
    assignment for me, newest-first". The result is paginated.

    ANONYMITY INVARIANT: the returned `reporter` block is
    `CaseReporterAnonymousSummary` - `display_handle` and
    `district_label` only. There is no path in this handler that
    touches the User row.
    """
    mp_profile = await _require_mp_viewer(db, user)

    # Join CaseAssignment back to Case - the queue is "cases I have
    # an active assignment on", not "cases routed to my region".
    stmt = (
        select(Case, CaseAssignment.assigned_at)
        .join(CaseAssignment, CaseAssignment.case_id == Case.id)
        .where(
            CaseAssignment.mp_profile_id == mp_profile.id,
            CaseAssignment.unassigned_at.is_(None),
        )
    )
    if status_filter:
        stmt = stmt.where(Case.status.in_([s.value for s in status_filter]))
    if priority:
        stmt = stmt.where(Case.priority.in_([p.value for p in priority]))
    if category_id is not None:
        stmt = stmt.where(Case.category_id == category_id)
    if district_id is not None:
        stmt = stmt.where(Case.district_id == district_id)
    if region_id is not None:
        stmt = stmt.where(Case.region_id == region_id)
    if date_from is not None:
        stmt = stmt.where(Case.submitted_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Case.submitted_at <= date_to)
    if search:
        # case_number or title substring (case-insensitive).
        needle = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Case.case_number).like(needle),
                func.lower(Case.title).like(needle),
            )
        )

    # Sort. Priority uses the underlying enum order: low < normal <
    # high < critical - desc puts critical first.
    if sort == "priority":
        stmt = stmt.order_by(Case.priority.desc(), Case.submitted_at.desc())
    elif sort == "oldest":
        stmt = stmt.order_by(Case.submitted_at.asc())
    else:  # "newest"
        stmt = stmt.order_by(Case.submitted_at.desc())

    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).all()

    # Coalesce support_count in one query (no N+1).
    case_ids = [int(r[0].id) for r in rows]
    support_counts: dict[int, int] = {}
    if case_ids:
        sc = (
            select(CaseSupport.original_case_id, func.count(CaseSupport.id))
            .where(CaseSupport.original_case_id.in_(case_ids))
            .group_by(CaseSupport.original_case_id)
        )
        for cid, count in (await db.execute(sc)).all():
            support_counts[int(cid)] = int(count)

    # Build the MPQueueItemOut list. The reporter block uses the
    # anonymous summary - NO user_id, NO PII.
    now = datetime.now(UTC)
    items = []
    for case, assigned_at in rows:
        age_in_hours = int(
            (now - case.submitted_at).total_seconds() / 3600
        )
        items.append(
            MPQueueItemOut(
                id=int(case.id),
                case_number=case.case_number,
                status=CaseStatus(case.status),
                priority=CasePriority(case.priority),
                title=case.title,
                category_id=int(case.category_id),
                district_id=case.district_id,
                region_id=getattr(mp_profile, "region_id", None),
                submitted_at=case.submitted_at,
                assigned_at=assigned_at,
                age_in_hours=age_in_hours,
                reporter=CaseReporterAnonymousSummary(
                    display_handle=case.display_handle or "Anonymous Citizen",
                    district_label=case.district_id,
                ),
                support_count=support_counts.get(int(case.id), 0),
            )
        )

    return MPQueueListResponse(
        items=items,
        total=len(items),
        limit=limit,
        offset=offset,
        checked_at=now,
    )


@router.post(
    "/{case_id}/assign-self",
    response_model=CaseAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Claim a case for the viewer's MPProfile",
)
async def assign_self(
    payload: CaseAssignmentCreate | None = Body(default=None),
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseAssignmentResponse:
    """Claim a case. Creates an active `CaseAssignment` row.

    Idempotent: if the case is already actively assigned to this
    MPProfile, returns the existing row without writing. If assigned
    to a different MP, returns 409. Unassigned cases can be claimed by
    any MP.

    The optional `reason` body is recorded into the audit log only.
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    mp_profile = await _require_mp_viewer(db, user)

    # Check for an active assignment on this case (across all MPs).
    existing = (
        await db.execute(
            select(CaseAssignment).where(
                CaseAssignment.case_id == case_id,
                CaseAssignment.unassigned_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if int(existing.mp_profile_id) == int(mp_profile.id):
            return CaseAssignmentResponse.model_validate(existing)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case is already assigned to another MP.",
        )

    assignment = CaseAssignment(
        case_id=case_id,
        mp_profile_id=int(mp_profile.id),
        assigned_by_user_id=user.id,
        assigned_at=datetime.now(UTC),
    )
    db.add(assignment)
    await db.flush()  # populate assignment.id

    # Mark the top-level case column too so other endpoints can read
    # the assignment in one hop.
    case.assigned_mp_profile_id = int(mp_profile.id)
    db.add(case)

    # Audit only - assignment is a state change, not a citizen-visible
    # timeline event.
    reason = payload.reason if payload else None
    await log_audit_event(
        db,
        case_id=case_id,
        action=CaseAuditAction.CASE_ASSIGNED,
        actor_user_id=user.id,
        actor_role="mp",
        payload={"mp_profile_id": int(mp_profile.id), "reason": reason},
    )

    await db.commit()
    await db.refresh(assignment)
    return CaseAssignmentResponse.model_validate(assignment)


@router.post(
    "/{case_id}/unassign",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Release a case (the MP withdraws from the queue)",
)
async def unassign_case(
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Release an MP's active assignment on a case.

    404 if there is no active assignment for this MP on this case.
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    mp_profile = await _require_mp_viewer(db, user)

    active = (
        await db.execute(
            select(CaseAssignment).where(
                CaseAssignment.case_id == case_id,
                CaseAssignment.mp_profile_id == int(mp_profile.id),
                CaseAssignment.unassigned_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active assignment for this case.",
        )

    active.unassigned_at = datetime.now(UTC)
    db.add(active)

    # If the case is also assigned to this MPProfile at the top level,
    # clear it (so the next listing doesn't accidentally show it as
    # still assigned).
    if int(case.assigned_mp_profile_id or 0) == int(mp_profile.id):
        case.assigned_mp_profile_id = None
        db.add(case)

    # Audit only - unassignment is a passive action and is not
    # surfaced to the citizen timeline.
    await log_audit_event(
        db,
        case_id=case_id,
        action=CaseAuditAction.CASE_UNASSIGNED,
        actor_user_id=user.id,
        actor_role="mp",
        payload={"mp_profile_id": int(mp_profile.id)},
    )
    await db.commit()
    # 204 No Content - no body.


@router.post(
    "/{case_id}/request-information",
    response_model=CaseTimelineResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request additional information from the citizen",
)
async def request_information(
    payload: InformationRequestCreate,
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseTimelineResponse:
    """MP asks the citizen for more information.

    Emits a `CaseTimeline` row + transitions status to
    `information_requested`. The status transition is the source of
    truth for the citizen's view; the helper just adds the
    customer-visible "Information requested" event with the note.
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    await _require_mp_viewer(db, user)  # hard MP gate

    # Apply the workflow transition. The adjacency list must allow
    # `from -> information_requested` for the case's current state -
    # if it doesn't, 409. We do NOT bypass that check here.
    try:
        updated = await apply_transition(
            db,
            case=case,
            to_status=CaseStatus.INFORMATION_REQUESTED,
            actor_user_id=user.id,
            actor_role=role,
            description=payload.note,
        )
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot move from {exc.from_status.value!r} to "
                f"{CaseStatus.INFORMATION_REQUESTED.value!r}."
            ),
        ) from exc

    # Emit the dedicated information-requested timeline + audit row
    # IN ADDITION to the status-changed row that apply_transition
    # already wrote.
    await record_information_requested(
        db,
        case_id=case_id,
        actor_user_id=user.id,
        actor_role=role,
        note=payload.note,
    )

    await db.commit()
    await db.refresh(updated)
    return CaseTimelineResponse(
        id=int(updated.id),
        case_id=case_id,
        event_type=CaseTimelineEventType.INFORMATION_REQUESTED.value,
        from_status=CaseStatus(case.status).value,
        to_status=CaseStatus.INFORMATION_REQUESTED.value,
        actor_role=role,
        actor_user_id=user.id,
        description=payload.note,
        created_at=datetime.now(UTC),
    )


@router.post(
    "/{case_id}/close",
    response_model=CaseTimelineResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Close a case (MP convenience wrapper)",
)
async def close_case(
    payload: CloseCaseCreate | None = Body(default=None),
    case_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseTimelineResponse:
    """MP closes a case.

    Thin wrapper around `apply_transition(case, CLOSED)` that the MP
    UI can call without knowing the underlying status flow shape.
    The body is optional.
    """
    case = await get_case_for_viewer(db, case_id=case_id)
    role = _user_role_str(user)
    await _require_mp_viewer(db, user)  # hard MP gate

    description = payload.description if payload else None
    try:
        updated = await apply_transition(
            db,
            case=case,
            to_status=CaseStatus.CLOSED,
            actor_user_id=user.id,
            actor_role=role,
            description=description,
        )
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot move from {exc.from_status.value!r} to "
                f"{CaseStatus.CLOSED.value!r}."
            ),
        ) from exc

    await db.commit()
    await db.refresh(updated)
    return CaseTimelineResponse.model_validate(updated)


__all__ = ["router"]
