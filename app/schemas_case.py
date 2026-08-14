"""Pydantic schemas for the case-management domain.

New file — deliberately separate from the 785-LOC `app/schemas.py`
to avoid making that file any larger. The future case router (next
PR) imports from here.

Anonymity invariant (spec STEP 6):

  The `*Public*` schemas are the MP-visible shape. They have NO fields
  that could leak PII (`first_name`, `last_name`, `email`, `phone`,
  `photo`, `user_id`). The Pydantic schema is the THIRD layer of the
  anonymity defense (the first is the service-layer
  `build_reporter_view()`, the second is the CI grep gate in the
  next PR). A reviewer's checklist for adding a field here:

  1. Could the field identify a specific human?
  2. Could it be combined with public data to identify a human?
  3. Could a citizen choose to have it suppressed and the answer is
     YES — then the field belongs on `SelfReporter`, not on the public
     shape.

  If the answer to (1) or (2) is YES, do NOT add it. File a
  spec-change request instead.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.enums import (
    CaseAuditAction,
    CasePriority,
    CaseStatus,
    CaseTimelineEventType,
)

# ---------------------------------------------------------------------------
# Base config — keep snake_case wire format consistent with the existing
# `app/schemas.py`.
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "from_attributes": True,
    "populate_by_name": True,
    "str_strip_whitespace": True,
}


# ---------------------------------------------------------------------------
# CaseCategory
# ---------------------------------------------------------------------------


class CaseCategoryBase(BaseModel):
    model_config = _BASE_CONFIG
    name: str = Field(..., max_length=120)
    description: str | None = None
    is_active: bool = True


class CaseCategoryCreate(CaseCategoryBase):
    pass


class CaseCategoryResponse(CaseCategoryBase):
    id: int
    created_at: datetime
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# MPRegion
# ---------------------------------------------------------------------------


class MPRegionBase(BaseModel):
    model_config = _BASE_CONFIG
    name: str = Field(..., max_length=120)
    code: str = Field(..., max_length=32)
    district_id: str | None = Field(default=None, max_length=80)


class MPRegionCreate(MPRegionBase):
    pass


class MPRegionResponse(MPRegionBase):
    id: int
    created_at: datetime


# ---------------------------------------------------------------------------
# MPProfile
# ---------------------------------------------------------------------------


class MPProfileBase(BaseModel):
    model_config = _BASE_CONFIG
    region_id: int | None = None
    office: str | None = Field(default=None, max_length=255)
    photo_url: str | None = Field(default=None, max_length=2048)
    bio: str | None = None
    is_active: bool = True
    is_accepting_cases: bool = True


class MPProfileCreate(MPProfileBase):
    user_id: int


class MPProfileResponse(MPProfileBase):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime | None = None


class MPProfilePublic(BaseModel):
    """MP-side view of an MPProfile — what the citizen sees.

    Intentionally excludes `user_id` so the citizen cannot correlate
    an MPProfile with a specific User account. The user_id is
    only present in the admin-only `MPProfileResponse` (different
    schema), accessible only via the future admin tooling.
    """

    model_config = _BASE_CONFIG
    id: int
    office: str | None = None
    photo_url: str | None = None
    bio: str | None = None
    is_accepting_cases: bool
    region: MPRegionResponse | None = None


# ---------------------------------------------------------------------------
# CaseAttachment
# ---------------------------------------------------------------------------


class CaseAttachmentCreate(BaseModel):
    model_config = _BASE_CONFIG
    media_url: str = Field(..., max_length=2048)
    media_type: str = Field(default="image", max_length=64)
    sha256: str | None = Field(default=None, max_length=64)


class CaseAttachmentResponse(BaseModel):
    model_config = _BASE_CONFIG
    id: int
    case_id: int
    file_name: str
    media_url: str
    media_type: str
    mime_type: str
    byte_size: int
    sha256: str | None = None
    uploaded_by_id: int | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# CaseResponse (a single message in a case thread)
# ---------------------------------------------------------------------------


class CaseResponseCreate(BaseModel):
    model_config = _BASE_CONFIG
    body: str = Field(..., min_length=1, max_length=10_000)
    is_internal: bool = False


class CaseResponseResponse(BaseModel):
    model_config = _BASE_CONFIG
    id: int
    case_id: int
    author_user_id: int | None = None
    author_role: str
    body: str
    is_internal: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# CaseTimeline
# ---------------------------------------------------------------------------


class CaseTimelineResponse(BaseModel):
    model_config = _BASE_CONFIG
    id: int
    case_id: int
    event_type: str  # value from CaseTimelineEventType
    from_status: str | None = None
    to_status: str | None = None
    actor_role: str
    actor_user_id: int | None = None
    description: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Reporter blocks (anonymity contract — see anonymity.py for the service
# layer that constructs these)
# ---------------------------------------------------------------------------


class CaseReporterAnonymousOut(BaseModel):
    """The reporter block as the MP (or any non-self viewer) sees it.

    FORBIDDEN FIELDS (enforced by code review + CI grep gate):
      id, user_id, first_name, last_name, email, phone, photo,
      photo_url, profile_image, avatar_url.
    """

    model_config = _BASE_CONFIG
    display_handle: str
    district_label: str | None = None
    region_label: str | None = None
    submitted_at: str


class CaseReporterSelfOut(BaseModel):
    """The reporter block the citizen sees when viewing their own case."""

    model_config = _BASE_CONFIG
    is_self: bool = True
    user_id: int
    display_handle: str
    district_label: str | None = None
    region_label: str | None = None


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------


class CaseBase(BaseModel):
    model_config = _BASE_CONFIG
    title: str = Field(..., max_length=255)
    description: str = Field(..., min_length=1)
    category_id: int
    district_id: str | None = Field(default=None, max_length=80)
    is_anonymous: bool = False
    language: str = Field(default="EN", max_length=8)


class CaseCreate(CaseBase):
    """Input shape for filing a new case."""

    # Optional — if absent, the reporter_user_id is set from the
    # authenticated user's id. If both is_anonymous=True and
    # display_handle is provided, the handle overrides the default
    # "Anonymous Citizen".
    display_handle: str | None = Field(default=None, max_length=120)


class CaseUpdate(BaseModel):
    """Partial update — every field is optional."""

    model_config = _BASE_CONFIG
    title: str | None = Field(default=None, max_length=255)
    description: str | None = None
    category_id: int | None = None
    priority: CasePriority | None = None
    district_id: str | None = Field(default=None, max_length=80)
    is_anonymous: bool | None = None
    display_handle: str | None = Field(default=None, max_length=120)


class CasePublicDetail(BaseModel):
    """MP-visible case detail (the wire format the MP inbox / detail
    endpoints will return).

    The reporter block is the discriminated union below; the schema
    does NOT include any field that could leak PII.
    """

    model_config = _BASE_CONFIG
    id: int
    case_number: str
    category_id: int
    title: str
    description: str
    priority: CasePriority
    status: CaseStatus
    district_id: str | None = None
    is_anonymous: bool
    language: str
    submitted_at: datetime
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    assigned_mp_profile_id: int | None = None

    # The MP cannot see this — it's metadata only, no User reference.
    reporter: CaseReporterAnonymousOut | None = None

    # Lazy lists. The router may omit them for list endpoints and
    # populate them for the detail endpoint.
    attachments: list[CaseAttachmentResponse] = Field(default_factory=list)
    timeline: list[CaseTimelineResponse] = Field(default_factory=list)


class CaseSelfDetail(BaseModel):
    """Citizen-visible case detail (when the viewer is the reporter)."""

    model_config = _BASE_CONFIG
    id: int
    case_number: str
    category_id: int
    title: str
    description: str
    priority: CasePriority
    status: CaseStatus
    district_id: str | None = None
    is_anonymous: bool
    language: str
    submitted_at: datetime
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    assigned_mp_profile_id: int | None = None

    # SelfReporter — has user_id, but only because the viewer IS the user.
    reporter: CaseReporterSelfOut | None = None

    attachments: list[CaseAttachmentResponse] = Field(default_factory=list)
    timeline: list[CaseTimelineResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Workflow transitions
# ---------------------------------------------------------------------------


class CaseTransitionRequest(BaseModel):
    """Input shape for `POST /cases/{id}/transitions`.

    The router checks the move against ALLOWED_TRANSITIONS and applies
    it via `apply_transition()`. Raises 409 on invalid transition.
    """

    model_config = _BASE_CONFIG
    to_status: CaseStatus
    description: str | None = Field(default=None, max_length=10_000)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class CaseAssignmentCreate(BaseModel):
    """Input shape for `POST /cases/{id}/assign-self`.

    The viewer claims a case for their MPProfile. The body is optional;
    a reason may be recorded into the CaseAuditLog payload only.
    """

    model_config = _BASE_CONFIG
    reason: str | None = Field(default=None, max_length=500)


class CloseCaseCreate(BaseModel):
    """Input shape for `POST /cases/{id}/close` (MP convenience wrapper).

    Reuses the standard transition machinery but lives at a stable URL
    so the MP UI does not need to know about the underlying status flow.
    """

    model_config = _BASE_CONFIG
    description: str | None = Field(default=None, max_length=10_000)


class InformationRequestCreate(BaseModel):
    """Input shape for `POST /cases/{id}/request-information`.

    The `note` text is what the citizen sees on the case timeline
    prompting them to provide more information. It becomes the
    `description` on the status-transition timeline row.
    """

    model_config = _BASE_CONFIG
    note: str = Field(..., min_length=1, max_length=10_000)


class MPQueueItemOut(BaseModel):
    """A single row in the MP queue.

    ANONYMITY INVARIANT: This schema is the MP-visible wire shape for the
    queue. Fields MUST NOT include `reporter_user_id`, `first_name`,
    `last_name`, `username`, `email`, `phone`, `photo`, `photo_url`,
    `avatar_url`, or any other User-side identifier. The reporter block
    is the `CaseReporterAnonymousSummary` — `display_handle` +
    `district_label` only.

    Adding a field here is a CODE REVIEW PIN.
    """

    model_config = _BASE_CONFIG
    id: int
    case_number: str
    status: CaseStatus
    priority: CasePriority
    title: str
    category_id: int
    district_id: str | None = None
    region_id: int | None = None
    submitted_at: datetime
    assigned_at: datetime
    # Age of the underlying Case in hours since submission. Cheaply
    # computed server-side; the client doesn't have to know timezone.
    age_in_hours: int
    reporter: CaseReporterAnonymousSummary
    support_count: int


class MPQueueListResponse(BaseModel):
    """Wire shape of `GET /cases/mp/queue`."""

    model_config = _BASE_CONFIG
    items: list[MPQueueItemOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    checked_at: datetime


class CaseAssignmentResponse(BaseModel):
    model_config = _BASE_CONFIG
    id: int
    case_id: int
    mp_profile_id: int
    assigned_at: datetime
    assigned_by_user_id: int | None = None
    unassigned_at: datetime | None = None


class CaseSupportResponse(BaseModel):
    model_config = _BASE_CONFIG
    id: int
    original_case_id: int
    duplicate_case_id: int | None = None
    supporter_user_id: int | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Duplicate detection + case creation (POST /cases/duplicates/check,
# POST /cases/, POST /cases/{id}/support)
# ---------------------------------------------------------------------------


class CaseDuplicateCheckRequest(BaseModel):
    """Input shape for `POST /cases/duplicates/check`.

    Read-only — the endpoint NEVER mutates state. Mirrors `CaseCreate`
    but without reporter fields (the reporter is the authenticated
    user; the check is for content-only similarity).
    """

    model_config = _BASE_CONFIG
    title: str = Field(..., max_length=255)
    description: str = Field(..., min_length=1)
    category_id: int
    district_id: str | None = Field(default=None, max_length=80)
    language: str = Field(default="EN", max_length=8)


class CaseDuplicateCandidateOut(BaseModel):
    """Wire format for a single duplicate candidate.

    ANONYMITY INVARIANT: This schema is what an MP viewer receives when
    the filing-flow surfaces "similar cases". Fields MUST NOT include
    `reporter_user_id`, `first_name`, `last_name`, `username`, `email`,
    `phone`, `photo`, `photo_url`, `avatar_url`, or any other User-side
    identifier. The service layer (`services/cases/duplicates.py`)
    reads ONLY from the Case row, never from the User row — so there
    is no path for PII to leak through.

    Adding a field here is a CODE REVIEW PIN: confirm it does not
    violate the anonymity invariant.
    """

    model_config = _BASE_CONFIG
    case_id: int
    case_number: str
    category_id: int
    title: str
    # Truncated to 200 chars server-side (see SNIPPET_MAX_LEN in
    # services/cases/duplicates.py). The full description is fetched
    # lazily by the future detail endpoint.
    description_snippet: str
    district_id: str | None = None
    constituency: str | None = None
    # 0.0..~1.0; pre-rounded to 4dp. Composite of FTS rank, trigram
    # similarity, and category/district bonuses.
    similarity_score: float
    support_count: int
    submitted_at: datetime


class CaseDuplicateCheckResponse(BaseModel):
    """Wire format for `POST /cases/duplicates/check`."""

    model_config = _BASE_CONFIG
    candidates: list[CaseDuplicateCandidateOut] = Field(default_factory=list)
    # Server-side timestamp at the moment the check ran. Useful for
    # client-side caching ("results older than 5 min → re-check").
    checked_at: datetime


class CaseCreateResponse(BaseModel):
    """Wire format for `POST /cases/` — the just-created case."""

    model_config = _BASE_CONFIG
    id: int
    case_number: str
    status: CaseStatus
    submitted_at: datetime


class CaseSupportCreate(BaseModel):
    """Input shape for `POST /cases/{id}/support`.

    Empty body is fine — the authenticated user IS the supporter.
    `note` is reserved for a future "why do you support this case?"
    question (out of scope for v1).
    """

    model_config = _BASE_CONFIG
    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# My Cases list + detail (citizen-facing UI)
# ---------------------------------------------------------------------------
# The wire shape is intentionally distinct from the MP-visible
# `CasePublicDetail` / `CaseSelfDetail` already in this file because
# the citizen UI surface (`/cases`, `/cases/:id`) projects a
# narrower subset (no timeline, no attachments) and adds a `support_count`
# + a `viewer_can_respond` flag the detail page consumes directly.


class CaseReporterAnonymousSummary(BaseModel):
    """Compact reporter block for the citizen list — what a co-reporter
    sees when looking at someone else's case on `/cases`.

    ANONYMITY INVARIANT (matches `CaseReporterAnonymousOut`):
      NEVER includes `id`, `user_id`, `first_name`, `last_name`,
      `username`, `email`, `phone`, `photo`, `photo_url`. Adding a
      User-side field is a CODE REVIEW PIN.
    """

    model_config = _BASE_CONFIG
    display_handle: str
    district_label: str | None = None


class CaseListItemOut(BaseModel):
    """Wire shape of `GET /cases/` — one row per case.

    No description body — the list endpoint stays lightweight. The
    full body lives on `CaseDetailOut`.
    """

    model_config = _BASE_CONFIG
    id: int
    case_number: str
    status: CaseStatus
    title: str
    submitted_at: datetime
    reporter: CaseReporterAnonymousSummary
    support_count: int


class CaseDetailOut(BaseModel):
    """Wire shape of `GET /cases/{case_id}` — full detail for the
    `/cases/:id` page.

    The `reporter` block is the SAME anonymous summary as the list
    row — even on the detail page, the citizen viewer never sees
    `first_name` / `email` etc. `viewer_can_respond` is computed by
    the router based on `case.reporter_user_id == user.id` or
    user.role in {mp, admin}.
    """

    model_config = _BASE_CONFIG
    id: int
    case_number: str
    status: CaseStatus
    title: str
    description: str
    district_id: str | None = None
    language: str
    submitted_at: datetime
    resolved_at: datetime | None = None
    reporter: CaseReporterAnonymousSummary
    support_count: int
    viewer_can_respond: bool


class CaseAttachmentUploadResponse(BaseModel):
    """Wire shape of `POST /cases/{id}/attachments` — the just-uploaded
    row. Mirrors the existing `CaseAttachmentResponse` but with an
    extra `byte_size` field (since the upload endpoint receives a real
    file and the byte count is part of the contract).
    """

    model_config = _BASE_CONFIG
    id: int
    case_id: int
    file_name: str
    media_url: str
    media_type: str
    mime_type: str
    byte_size: int
    sha256: str | None = None
    uploaded_by_id: int | None = None
    created_at: datetime


# Re-export the enum types so router code can do:
#   from app.schemas_case import CaseStatus
# without a separate import from app.enums.
__all__ = [  # noqa: RUF022 — items grouped by feature, not alphabetical
    "CaseCategoryBase", "CaseCategoryCreate", "CaseCategoryResponse",
    "MPRegionBase", "MPRegionCreate", "MPRegionResponse",
    "MPProfileBase", "MPProfileCreate", "MPProfileResponse", "MPProfilePublic",
    "CaseAttachmentCreate", "CaseAttachmentResponse",
    "CaseResponseCreate", "CaseResponseResponse",
    "CaseTimelineResponse",
    "CaseReporterAnonymousOut", "CaseReporterSelfOut",
    "CaseBase", "CaseCreate", "CaseUpdate", "CasePublicDetail", "CaseSelfDetail",
    "CaseTransitionRequest",
    "CaseAssignmentResponse", "CaseSupportResponse",
    # Duplicate detection + case creation:
    "CaseDuplicateCheckRequest", "CaseDuplicateCheckResponse",
    "CaseDuplicateCandidateOut", "CaseCreateResponse", "CaseSupportCreate",
    # Citizen UI list + detail:
    "CaseReporterAnonymousSummary",
    "CaseListItemOut", "CaseDetailOut",
    "CaseAttachmentUploadResponse",
    # MP queue / dashboard:
    "MPQueueItemOut", "MPQueueListResponse",
    "CaseAssignmentCreate", "InformationRequestCreate", "CloseCaseCreate",
    "CaseStatus", "CasePriority", "CaseTimelineEventType", "CaseAuditAction",
]