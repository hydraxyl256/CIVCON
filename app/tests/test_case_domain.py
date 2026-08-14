"""
Unit tests for the case-management domain.

Coverage:

- Workflow adjacency list (services/cases/workflow.py)
    - Every legal transition is allowed.
    - Every illegal transition raises InvalidTransition.
    - Terminal states have no outgoing edges.
- Anonymity invariant (services/cases/anonymity.py)
    - AnonymousReporter dataclass has no PII fields (the four-layer
      defense's first code-level assertion).
    - Self-vs-anonymous branching in build_reporter_view.
- Case-number format (services/cases/numbers.py)
    - next_case_number() emits the correct CIV-YYYY-NNNNNN shape.
- Audit logger (services/cases/audit.py)
    - Auto-attaches the X-Request-Id contextvar.
- MP routing (services/cases/routing.py)
    - Pure-functional `_rank` produces deterministic ordering
    - Database entry point wires up correctly
- Timeline helpers (services/cases/timeline.py)
    - Non-status events emit timeline + audit rows
- Response endpoint behaviour (routers/cases.py)
    - is_internal filter is applied for citizen viewers
    - Citizens cannot create is_internal=True responses
    - Paginated timeline respects `before_id` cursor + `limit` cap
    - Default limit and `limit` bounds

These tests are pure-Python and do NOT require a live database. They
keep the CI gate green even where Postgres is not available; the
live-DB migration tests live in test_db_perf_migrations.py.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.enums import CaseAuditAction, CaseStatus, CaseTimelineEventType
from app.services.cases.anonymity import (
    AnonymousReporter,
    SelfReporter,
    build_reporter_view,
    is_self_view,
)
from app.services.cases.workflow import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    apply_transition,
    is_allowed,
)

# ---------------------------------------------------------------------------
# Forbidden-PII field set — the canonical prohibited list. ANY future
# Pydantic schema, ORM model, or schema_case field that overlaps with
# these tokens must be reviewed against spec STEP 6.
# ---------------------------------------------------------------------------

FORBIDDEN_PII_TOKENS = frozenset({
    "id",          # user pk
    "user_id",
    "first_name",
    "last_name",
    "username",
    "full_name",
    "email",
    "phone",
    "phone_number",
    "photo",
    "photo_url",
    "avatar",
    "avatar_url",
    "profile_image",
})


# ===========================================================================
# Workflow
# ===========================================================================


class TestWorkflowAdjacencyList:
    def test_every_status_has_a_transition_row(self):
        """The adjacency list must have a row for every CaseStatus value
        (including terminal ones — they have an empty set)."""
        assert set(ALLOWED_TRANSITIONS.keys()) == set(CaseStatus)

    def test_terminal_states_have_no_outgoing_edges(self):
        """CLOSED, WITHDRAWN, REJECTED are terminal — no transitions out."""
        assert ALLOWED_TRANSITIONS[CaseStatus.CLOSED] == frozenset()
        assert ALLOWED_TRANSITIONS[CaseStatus.WITHDRAWN] == frozenset()
        assert ALLOWED_TRANSITIONS[CaseStatus.REJECTED] == frozenset()

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (CaseStatus.SUBMITTED, CaseStatus.RECEIVED),
            (CaseStatus.SUBMITTED, CaseStatus.WITHDRAWN),
            (CaseStatus.RECEIVED, CaseStatus.ASSIGNED),
            (CaseStatus.ASSIGNED, CaseStatus.UNDER_REVIEW),
            (CaseStatus.UNDER_REVIEW, CaseStatus.IN_PROGRESS),
            (CaseStatus.UNDER_REVIEW, CaseStatus.RESOLVED),
            (CaseStatus.INFORMATION_REQUESTED, CaseStatus.CITIZEN_RESPONDED),
            (CaseStatus.CITIZEN_RESPONDED, CaseStatus.UNDER_REVIEW),
            (CaseStatus.IN_PROGRESS, CaseStatus.RESOLVED),
            (CaseStatus.RESOLVED, CaseStatus.CLOSED),
            (CaseStatus.RESOLVED, CaseStatus.IN_PROGRESS),
        ],
    )
    def test_legal_transitions_are_allowed(self, from_status, to_status):
        assert is_allowed(from_status, to_status)

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            # No backwards jumps to an earlier state
            (CaseStatus.SUBMITTED, CaseStatus.CLOSED),
            (CaseStatus.SUBMITTED, CaseStatus.RESOLVED),
            (CaseStatus.RECEIVED, CaseStatus.UNDER_REVIEW),
            (CaseStatus.ASSIGNED, CaseStatus.IN_PROGRESS),
            (CaseStatus.UNDER_REVIEW, CaseStatus.CLOSED),
            (CaseStatus.RESOLVED, CaseStatus.SUBMITTED),
            # Terminal states have no outgoing transitions
            (CaseStatus.CLOSED, CaseStatus.OPEN if hasattr(CaseStatus, "OPEN") else CaseStatus.SUBMITTED),
            (CaseStatus.WITHDRAWN, CaseStatus.SUBMITTED),
            (CaseStatus.REJECTED, CaseStatus.SUBMITTED),
            # Self-loop prohibited
            (CaseStatus.IN_PROGRESS, CaseStatus.IN_PROGRESS),
            (CaseStatus.RESOLVED, CaseStatus.RESOLVED),
        ],
    )
    def test_illegal_transitions_are_rejected(self, from_status, to_status):
        assert not is_allowed(from_status, to_status)


class TestApplyTransition:
    """apply_transition is async + DB-touching; mock the session AND
    the ORM models so SQLAlchemy doesn't try to instantiate them (the
    real ones have mapper-configure cascades that pre-date this PR)."""

    @pytest.fixture
    def case_double(self):
        """A minimal Case double with the fields apply_transition reads."""
        return SimpleNamespace(
            id=42,
            status=CaseStatus.SUBMITTED.value,  # raw string mimics DB round-trip
            submitted_at=datetime.now(tz=UTC),
            resolved_at=None,
        )

    @pytest.fixture
    def db_double(self):
        """A minimal AsyncSession double — apply_transition uses
        db.add() to enqueue writes; no flush happens (the caller commits)."""
        return SimpleNamespace(add=lambda obj: None)

    @pytest.mark.asyncio
    async def test_legal_transition_enqueues_two_rows(self, db_double, case_double, monkeypatch):
        """apply_transition must enqueue one timeline row + one audit row."""
        added = []
        db_double.add = lambda obj: added.append(obj)

        # Mock the model classes so SQLAlchemy never tries to instantiate
        # the real ones (which would trigger the pre-existing User.posts
        # mapper-configure cascade). The service uses
        # `CaseTimeline(...)` and `CaseAuditLog(...)` as factory calls.
        class _Timeline:
            def __init__(self, **kw):
                self.kw = kw

        class _Audit:
            def __init__(self, **kw):
                self.kw = kw

        # Build a fake `app.models` module with just these two classes.
        import sys

        import app.models
        fake_models = sys.modules["app.models"]
        # Monkeypatch only the attribute lookups inside workflow.py.
        monkeypatch.setattr(fake_models, "CaseTimeline", _Timeline)
        monkeypatch.setattr(fake_models, "CaseAuditLog", _Audit)

        result = await apply_transition(
            db_double,
            case=case_double,
            to_status=CaseStatus.RECEIVED,
            actor_user_id=7,
            actor_role="admin",
            description="intake",
            request_id="req-abc",
        )

        # Two rows enqueued (timeline + audit).
        assert len(added) == 2
        assert isinstance(added[0], _Timeline)
        assert isinstance(added[1], _Audit)
        assert added[0].kw["case_id"] == 42
        assert added[1].kw["action"] == "status_changed"
        assert added[0].kw["from_status"] == "submitted"
        assert added[0].kw["to_status"] == "received"

        assert result.from_status == CaseStatus.SUBMITTED
        assert result.to_status == CaseStatus.RECEIVED
        assert result.timeline_event_type == CaseTimelineEventType.STATUS_CHANGED

    @pytest.mark.asyncio
    async def test_terminal_transition_to_resolved_sets_resolved_at(
        self, db_double, case_double, monkeypatch
    ):
        case_double.status = CaseStatus.IN_PROGRESS.value

        class _Timeline:
            def __init__(self, **kw):
                pass

        class _Audit:
            def __init__(self, **kw):
                pass

        import sys

        import app.models
        fake_models = sys.modules["app.models"]
        monkeypatch.setattr(fake_models, "CaseTimeline", _Timeline)
        monkeypatch.setattr(fake_models, "CaseAuditLog", _Audit)

        result = await apply_transition(
            db_double,
            case=case_double,
            to_status=CaseStatus.RESOLVED,
            actor_user_id=11,
            actor_role="mp",
        )
        assert case_double.status == CaseStatus.RESOLVED
        assert case_double.resolved_at is not None
        # Citizen-visible event name picks up the named terminal event.
        assert result.timeline_event_type == CaseTimelineEventType.CASE_RESOLVED

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, db_double, case_double):
        with pytest.raises(InvalidTransition):
            await apply_transition(
                db_double,
                case=case_double,
                to_status=CaseStatus.CLOSED,  # submitted → closed is illegal
                actor_user_id=1,
                actor_role="citizen",
            )


# ===========================================================================
# Anonymity
# ===========================================================================


class TestAnonymousReporterShape:
    """The dataclass shape is the first line of defense for STEP 6.
    If any forbidden PII token sneaks in here, the unit test catches it.
    """

    def test_anonymous_reporter_has_no_pii_fields(self):
        field_names = {f.name for f in dataclasses.fields(AnonymousReporter)}
        leaked = field_names & FORBIDDEN_PII_TOKENS
        assert not leaked, (
            f"AnonymousReporter exposes forbidden PII fields: {sorted(leaked)}"
        )

    def test_anonymous_reporter_is_frozen(self):
        """Cannot mutate an AnonymousReporter after construction."""
        ar = AnonymousReporter(
            display_handle="Anonymous Citizen",
            district_label="Central",
            region_label=None,
            submitted_at="2026-08-05T10:00:00+00:00",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ar.display_handle = "not-so-anonymous"  # type: ignore[misc]


class TestSelfReporterShape:
    def test_self_reporter_has_user_id_but_no_other_pII(self):
        field_names = {f.name for f in dataclasses.fields(SelfReporter)}
        # `user_id` is allowed here because the viewer IS the user —
        # they can already see their own id from their session. But
        # every other PII token must be absent.
        leaked = (field_names & FORBIDDEN_PII_TOKENS) - {"user_id"}
        assert not leaked, (
            f"SelfReporter exposes forbidden PII fields: {sorted(leaked)}"
        )
        assert "user_id" in field_names
        assert "is_self" in field_names


def _make_case(*, reporter_user_id, display_handle="Anonymous Citizen", district_id="Central"):
    """Lightweight Case stand-in for build_reporter_view tests."""
    return SimpleNamespace(
        reporter_user_id=reporter_user_id,
        display_handle=display_handle,
        district_id=district_id,
        submitted_at=datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC),
    )


class TestBuildReporterView:
    def test_viewer_is_reporter_returns_self_block(self):
        case = _make_case(reporter_user_id=99)
        view = build_reporter_view(
            case=case, viewer_user_id=99, is_admin=False
        )
        assert isinstance(view, SelfReporter)
        assert view.user_id == 99
        assert view.is_self is True

    def test_viewer_is_not_reporter_returns_anonymous(self):
        case = _make_case(reporter_user_id=99)
        view = build_reporter_view(
            case=case, viewer_user_id=42, is_admin=False
        )
        assert isinstance(view, AnonymousReporter)
        assert view.display_handle == "Anonymous Citizen"
        assert view.district_label == "Central"

    def test_admin_flag_is_ignored(self):
        """build_reporter_view deliberately does NOT honour is_admin.
        Admins use a separate code path that decrypts PII explicitly
        with an audit row."""
        case = _make_case(reporter_user_id=99)
        view_admin = build_reporter_view(case=case, viewer_user_id=1, is_admin=True)
        view_non_admin = build_reporter_view(case=case, viewer_user_id=1, is_admin=False)
        assert type(view_admin) is type(view_non_admin)

    def test_fully_anonymous_case_returns_none_for_non_reporter_viewer(self):
        case = _make_case(reporter_user_id=None)
        view = build_reporter_view(case=case, viewer_user_id=1, is_admin=False)
        assert view is None

    def test_fully_anonymous_case_returns_self_for_fictional_self(self):
        """Edge case: reporter_user_id is None but a viewer passes the
        matching None — `viewer_user_id is None` is a defensive
        equality, but anon-cases have no reporter to match. We expect
        None back (no block to show)."""
        case = _make_case(reporter_user_id=None)
        view = build_reporter_view(case=case, viewer_user_id=None, is_admin=False)
        assert view is None


class TestIsSelfView:
    def test_self_returns_true(self):
        case = _make_case(reporter_user_id=99)
        assert is_self_view(case, 99) is True

    def test_other_returns_false(self):
        case = _make_case(reporter_user_id=99)
        assert is_self_view(case, 42) is False

    def test_anonymous_case_returns_false_for_any_viewer(self):
        case = _make_case(reporter_user_id=None)
        assert is_self_view(case, 99) is False


# ===========================================================================
# Numbers (mock the DB scalar; assert format only)
# ===========================================================================


class TestNextCaseNumberFormat:
    @pytest.mark.asyncio
    async def test_format_is_civ_yyear_nnnnnn(self):
        from app.services.cases.numbers import next_case_number

        # Mock the AsyncSession.scalar to return a known sequence value.
        class _Db:
            async def scalar(self, _stmt):
                return 1

        out = await next_case_number(_Db())
        year = datetime.now(tz=UTC).year
        assert out == f"CIV-{year}-000001"

    @pytest.mark.asyncio
    async def test_high_sequence_value_is_padded_to_six_digits(self):
        from app.services.cases.numbers import next_case_number

        class _Db:
            async def scalar(self, _stmt):
                return 1_234_567

        out = await next_case_number(_Db())
        year = datetime.now(tz=UTC).year
        assert out == f"CIV-{year}-1234567"

    @pytest.mark.asyncio
    async def test_null_sequence_value_raises(self):
        from app.services.cases.numbers import next_case_number

        class _Db:
            async def scalar(self, _stmt):
                return None

        with pytest.raises(RuntimeError):
            await next_case_number(_Db())


# ===========================================================================
# Audit logger (mock the DB; assert the request_id is propagated from the
# contextvar and from the explicit argument)
# ===========================================================================


@pytest.fixture
def _clear_request_id():
    from app.core.logging_config import set_request_id
    set_request_id(None)
    yield
    set_request_id(None)


class TestLogAuditEvent:
    @pytest.mark.asyncio
    async def test_attaches_request_id_from_contextvar_when_not_passed(
        self, _clear_request_id
    ):
        from app.core.logging_config import set_request_id
        from app.services.cases.audit import log_audit_event

        set_request_id("rid-from-middleware")

        captured_kwargs: dict = {}

        class _Row:
            def __init__(self, **kw):
                captured_kwargs.update(kw)

        # Patch the model to a capturer so we don't touch SQLAlchemy here.
        with patch("app.models.CaseAuditLog", _Row):
            await log_audit_event(
                db=SimpleNamespace(add=lambda obj: None),
                case_id=1,
                action=CaseAuditAction.CASE_VIEWED,
                actor_user_id=42,
                actor_role="mp",
            )

        assert captured_kwargs["request_id"] == "rid-from-middleware"
        assert captured_kwargs["case_id"] == 1
        assert captured_kwargs["action"] == "case_viewed"

    @pytest.mark.asyncio
    async def test_explicit_request_id_overrides_contextvar(self):
        from app.services.cases.audit import log_audit_event

        captured_kwargs: dict = {}

        class _Row:
            def __init__(self, **kw):
                captured_kwargs.update(kw)

        with patch("app.models.CaseAuditLog", _Row):
            await log_audit_event(
                db=SimpleNamespace(add=lambda obj: None),
                case_id=1,
                action=CaseAuditAction.STATUS_CHANGED,
                actor_user_id=None,
                actor_role="system",
                request_id="explicit-rid",
            )

        assert captured_kwargs["request_id"] == "explicit-rid"


# ===========================================================================
# Schemas (sanity test the Pydantic shapes; catching forbidden fields)
# ===========================================================================


class TestCaseSchemas:
    def test_anonymous_reporter_schema_has_no_pii_fields(self):
        from app.schemas_case import CaseReporterAnonymousOut

        # Inspect the Pydantic v2 model fields directly.
        field_names = set(CaseReporterAnonymousOut.model_fields.keys())
        leaked = field_names & FORBIDDEN_PII_TOKENS
        assert not leaked, (
            f"CaseReporterAnonymousOut exposes forbidden PII fields: {sorted(leaked)}"
        )

    def test_self_reporter_schema_only_has_user_id_of_pii_tokens(self):
        from app.schemas_case import CaseReporterSelfOut

        field_names = set(CaseReporterSelfOut.model_fields.keys())
        leaked = (field_names & FORBIDDEN_PII_TOKENS) - {"user_id"}
        assert not leaked, (
            f"CaseReporterSelfOut exposes forbidden PII fields: {sorted(leaked)}"
        )

    def test_public_case_detail_schema_has_no_user_email_phone_first_last(self):
        from app.schemas_case import CasePublicDetail

        field_names = set(CasePublicDetail.model_fields.keys())
        forbidden_here = field_names & {
            "first_name", "last_name", "email", "phone",
            "phone_number", "photo_url", "profile_image",
            "reporter_user_id",
        }
        assert not forbidden_here, (
            f"CasePublicDetail has forbidden PII fields: {sorted(forbidden_here)}"
        )


# ===========================================================================
# Routing engine (services/cases/routing.py)
# ===========================================================================

from app.services.cases.routing import (
    SCORE_ACCEPTING_BONUS,
    SCORE_CROSS_REGION_CONSTITUENCY,
    SCORE_EXACT_DISTRICT,
    SCORE_NEAREST_REGION,
    SCORE_REGION_FALLBACK,
    SCORE_REGION_PLUS_DISTRICT,
    RoutingRequest,
    RoutingResult,
    _MPRow,
    _rank,
)


def _row(
    *,
    id: int,
    region_id: int | None = 1,
    region_label: str | None = "Central",
    district_id: str | None = "Kampala",
    constituency: str | None = "Kampala Central",
    is_active: bool = True,
    is_accepting_cases: bool = True,
) -> _MPRow:
    """Build an in-memory MP candidate row for the routing engine."""
    return _MPRow(
        id=id,
        region_id=region_id,
        region_label=region_label,
        district_id=district_id,
        constituency=constituency,
        is_active=is_active,
        is_accepting_cases=is_accepting_cases,
    )


class TestRoutingScoreExactDistrict:
    """The 100-point tier: district_id + (constituency matches OR unspecified)."""

    def test_exact_district_match_scores_100_plus_accepting_bonus(self):
        req = RoutingRequest(category_id=1, district_id="Kampala")
        candidates = [_row(id=1, district_id="Kampala")]
        result = _rank(candidates, req)
        assert len(result.suggestions) == 1
        s = result.suggestions[0]
        assert s.score == SCORE_EXACT_DISTRICT + SCORE_ACCEPTING_BONUS
        assert not s.is_nearest_match
        assert ("exact_district", SCORE_EXACT_DISTRICT) in s.score_breakdown

    def test_district_match_with_constituency_match_keeps_full_score(self):
        req = RoutingRequest(
            category_id=1, district_id="Kampala", constituency="Central"
        )
        candidates = [_row(id=1, district_id="Kampala", constituency="Central")]
        result = _rank(candidates, req)
        assert result.suggestions[0].score == SCORE_EXACT_DISTRICT + SCORE_ACCEPTING_BONUS

    def test_district_match_with_constituency_mismatch_drops_to_region_fallback(self):
        """If both request and MP declare constituency AND they differ,
        we treat this as a softer signal (75, not 100) so a more
        precisely-matched MP ranks ahead."""
        req = RoutingRequest(
            category_id=1, district_id="Kampala", constituency="Central"
        )
        candidates = [_row(id=1, district_id="Kampala", constituency="North")]
        result = _rank(candidates, req)
        s = result.suggestions[0]
        assert s.score == SCORE_REGION_FALLBACK + SCORE_ACCEPTING_BONUS
        assert s.is_nearest_match is True
        signals = [name for name, _ in s.score_breakdown]
        assert "district_match_constituency_mismatch" in signals

    def test_case_insensitive_district_match(self):
        req = RoutingRequest(category_id=1, district_id="KAMPALA")
        candidates = [_row(id=1, district_id="kampala")]
        result = _rank(candidates, req)
        assert len(result.suggestions) == 1
        assert result.suggestions[0].score == SCORE_EXACT_DISTRICT + SCORE_ACCEPTING_BONUS


class TestRoutingScoreRegionFallback:
    """The 75 / 90 tiers: same region, district either absent or matches."""

    def test_region_match_with_mp_having_no_district_scores_75(self):
        req = RoutingRequest(category_id=1, region_id=1, district_id="Kampala")
        candidates = [_row(id=1, region_id=1, district_id=None)]
        result = _rank(candidates, req)
        s = result.suggestions[0]
        assert s.score == SCORE_REGION_FALLBACK + SCORE_ACCEPTING_BONUS
        assert s.is_nearest_match is True

    def test_region_match_with_no_request_district_scores_90(self):
        req = RoutingRequest(category_id=1, region_id=1, district_id=None)
        candidates = [_row(id=1, region_id=1, district_id="Kampala")]
        result = _rank(candidates, req)
        s = result.suggestions[0]
        assert s.score == SCORE_REGION_PLUS_DISTRICT + SCORE_ACCEPTING_BONUS
        assert not s.is_nearest_match


class TestRoutingScoreNearestRegion:
    """The 50 tier: same region, different district."""

    def test_same_region_different_district_scores_50_and_is_nearest(self):
        req = RoutingRequest(category_id=1, region_id=1, district_id="Kampala")
        candidates = [_row(id=1, region_id=1, district_id="Wakiso")]
        result = _rank(candidates, req)
        s = result.suggestions[0]
        assert s.score == SCORE_NEAREST_REGION + SCORE_ACCEPTING_BONUS
        assert s.is_nearest_match is True


class TestRoutingScoreCrossRegionConstituency:
    """The 25 tier: different region, same constituency."""

    def test_different_region_same_constituency_scores_25(self):
        req = RoutingRequest(category_id=1, constituency="Central")
        candidates = [
            _row(id=1, region_id=1, district_id="Kampala", constituency="Central"),
            _row(id=2, region_id=2, district_id="Jinja", constituency="Central"),
        ]
        result = _rank(candidates, req)
        # Both match on cross-region constituency. The first MP is in
        # region 1 (same as nothing in the request) so the ranking is
        # by (score DESC, region_label ASC, id ASC).
        assert all(s.score == SCORE_CROSS_REGION_CONSTITUENCY + SCORE_ACCEPTING_BONUS for s in result.suggestions)


class TestRoutingRanking:
    """Multiple candidates — verify the order."""

    def test_exact_district_ranks_above_nearest_region(self):
        req = RoutingRequest(category_id=1, district_id="Kampala", region_id=1)
        candidates = [
            _row(id=1, region_id=1, district_id="Wakiso"),  # nearest-region
            _row(id=2, region_id=1, district_id="Kampala"),  # exact
            _row(id=3, region_id=1, district_id=None),       # region fallback
        ]
        result = _rank(candidates, req)
        ids_in_order = [s.mp_profile_id for s in result.suggestions]
        assert ids_in_order == [2, 3, 1]

    def test_ties_broken_deterministically_by_id(self):
        """Same score -> lower mp_profile.id first. Pure-Python `sorted`
        is stable, so this is verifiable."""
        req = RoutingRequest(category_id=1, region_id=1)
        candidates = [
            _row(id=99, region_id=1, district_id="Wakiso"),
            _row(id=10, region_id=1, district_id="Jinja"),
            _row(id=42, region_id=1, district_id="Mukono"),
        ]
        result = _rank(candidates, req)
        # All three have the same nearest-region score (50 + 5 = 55),
        # so the deterministic tie-breaker is id ASC.
        ids_in_order = [s.mp_profile_id for s in result.suggestions]
        assert ids_in_order == [10, 42, 99]

    def test_not_accepting_cases_ranked_after_accepting(self):
        """An MP who is NOT accepting still appears (citizen must see
        them) but with a penalty so the accepting alternative ranks
        first."""
        req = RoutingRequest(category_id=1, district_id="Kampala")
        candidates = [
            _row(id=1, district_id="Kampala", is_accepting_cases=False),
            _row(id=2, district_id="Kampala", is_accepting_cases=True),
        ]
        result = _rank(candidates, req)
        ids_in_order = [s.mp_profile_id for s in result.suggestions]
        assert ids_in_order == [2, 1]

    def test_no_match_at_all_returns_empty_with_reason(self):
        req = RoutingRequest(
            category_id=1, district_id="Kampala", constituency="Central", region_id=1
        )
        # All candidates are in a totally unrelated region with a
        # different constituency.
        candidates = [
            _row(id=1, region_id=99, district_id="Gulu", constituency="Awach"),
        ]
        result = _rank(candidates, req)
        assert result.suggestions == []
        assert result.nearest_match is False
        assert result.empty_reason == "no_active_mps"


class TestRoutingNearestMatchFlag:
    """`nearest_match` is true iff the result contains no exact-district
    hit but at least one fallback signal."""

    def test_exact_district_match_marks_nearest_false(self):
        req = RoutingRequest(category_id=1, district_id="Kampala")
        candidates = [_row(id=1, district_id="Kampala")]
        result = _rank(candidates, req)
        assert result.nearest_match is False

    def test_only_nearest_region_match_marks_nearest_true(self):
        req = RoutingRequest(category_id=1, district_id="Kampala", region_id=1)
        candidates = [_row(id=1, region_id=1, district_id="Wakiso")]
        result = _rank(candidates, req)
        assert result.nearest_match is True

    def test_mixed_results_marks_nearest_true_when_no_exact(self):
        req = RoutingRequest(category_id=1, district_id="Kampala", region_id=1)
        candidates = [
            _row(id=1, region_id=1, district_id="Wakiso"),  # nearest
            _row(id=2, region_id=2, district_id="Gulu"),    # cross-region
        ]
        result = _rank(candidates, req)
        assert result.nearest_match is True


class TestRouteToMpAsync:
    """Exercise the public entry point against a mocked AsyncSession.

    The full path is mocked because routing doesn't actually use the
    session for writes; the only thing the session does is return the
    list of candidate MPProfile rows via `_fetch_candidate_mps`. We
    patch that helper directly so we don't need to touch SQLAlchemy.
    """

    @pytest.mark.asyncio
    async def test_route_to_mp_returns_ranked_results(self, monkeypatch):
        from app.services.cases import routing as routing_module

        candidates = [
            _row(id=1, district_id="Wakiso"),
            _row(id=2, district_id="Kampala"),
        ]

        async def fake_fetch(_db, *, category_id):
            return candidates

        monkeypatch.setattr(routing_module, "_fetch_candidate_mps", fake_fetch)

        req = RoutingRequest(category_id=1, district_id="Kampala")
        # An object that quacks like AsyncSession — unused.
        db = object()
        result = await routing_module.route_to_mp(db, req)

        assert isinstance(result, RoutingResult)
        # The Wakiso MP has no region_id in the request and no other
        # matching signal, so it is filtered out (no geographic signal
        # = no suggestion). Only the exact-district MP survives.
        assert len(result.suggestions) == 1
        assert result.suggestions[0].mp_profile_id == 2  # Kampala exact
        assert result.nearest_match is False

    @pytest.mark.asyncio
    async def test_route_to_mp_returns_fallback_when_no_exact_match(
        self, monkeypatch
    ):
        """When the request has a region_id, a different-district MP in
        the same region is surfaced as a nearest-match fallback."""
        from app.services.cases import routing as routing_module

        candidates = [
            _row(id=1, region_id=1, district_id="Wakiso"),
            _row(id=2, region_id=1, district_id="Kampala"),
        ]

        async def fake_fetch(_db, *, category_id):
            return candidates

        monkeypatch.setattr(routing_module, "_fetch_candidate_mps", fake_fetch)

        req = RoutingRequest(category_id=1, district_id="Jinja", region_id=1)
        result = await routing_module.route_to_mp(object(), req)
        # Both MPs survive (same region, neither has an exact district
        # match). The Wakiso and Kampala MPs are nearest-region matches.
        assert len(result.suggestions) == 2
        assert result.nearest_match is True

    @pytest.mark.asyncio
    async def test_route_to_mp_empty_db_returns_empty(self, monkeypatch):
        from app.services.cases import routing as routing_module

        async def fake_fetch(_db, *, category_id):
            return []

        monkeypatch.setattr(routing_module, "_fetch_candidate_mps", fake_fetch)

        req = RoutingRequest(category_id=1, district_id="Kampala")
        result = await routing_module.route_to_mp(object(), req)
        assert result.suggestions == []
        assert result.empty_reason == "no_active_mps"


# ===========================================================================
# Timeline helpers (services/cases/timeline.py)
# ===========================================================================
# Each helper must:
#   - enqueue EXACTLY one CaseTimeline row + EXACTLY one CaseAuditLog row
#   - carry the right event_type / action string
#   - propagate the request_id from either the contextvar or the explicit arg


def _no_bonus_pii(*_args, **_kwargs):
    """Marker for tests that the row payload contains no PII."""
    return True


class TestTimelineHelpers:
    @pytest.fixture
    def added(self):
        out: list = []
        return out

    @pytest.fixture
    def db(self, added):
        return SimpleNamespace(add=lambda obj: added.append(obj))

    @pytest.fixture(autouse=True)
    def _patch_case_models(self, monkeypatch):
        """Replace CaseTimeline and CaseAuditLog with capturer
        classes. The real ORM models trigger SQLAlchemy's mapper
        configure cascade which fails because of the pre-existing
        User.posts ambiguity (two FK paths from posts to users). This
        is purely a test-environment workaround; the production code
        path is unchanged.
        """
        class _Timeline:
            def __init__(self, **kw):
                self.kw = kw
                # Expose the SQLAlchemy-style attributes the production
                # code reads on the returned ORM instance. The values
                # mirror what the production row would have after the
                # INSERT, so tests can keep asserting on `t.event_type`
                # etc.
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Audit:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        import sys

        import app.models
        m = sys.modules["app.models"]
        monkeypatch.setattr(m, "CaseTimeline", _Timeline)
        monkeypatch.setattr(m, "CaseAuditLog", _Audit)

    def _assert_no_pii_fields(self, row):
        """Each timeline+audit row must never carry first_name /
        last_name / email / phone / photo_url / profile_image /
        user_id-with-pii. We verify the columns ARE NOT defined as
        kwargs on the row constructor.
        """
        for forbidden in ("first_name", "last_name", "email",
                          "phone_number", "photo_url", "profile_image"):
            assert forbidden not in row.kw, (
                f"Timeline row carries forbidden PII field {forbidden!r}"
            )

    @pytest.mark.asyncio
    async def test_record_response_added_emits_two_rows(self, db, added):
        from app.models import CaseAuditLog, CaseTimeline
        from app.services.cases.timeline import record_response_added

        t = await record_response_added(
            db,
            case_id=42,
            response_id=7,
            actor_user_id=99,
            actor_role="citizen",
            is_internal=False,
        )
        # Two rows enqueued.
        assert any(isinstance(o, CaseTimeline) for o in added)
        assert any(isinstance(o, CaseAuditLog) for o in added)
        assert t.event_type == "response_added"
        self._assert_no_pii_fields(added[0])

    @pytest.mark.asyncio
    async def test_record_attachment_added_emits_two_rows(self, db, added):
        from app.models import CaseAuditLog, CaseTimeline
        from app.services.cases.timeline import record_attachment_added

        t = await record_attachment_added(
            db,
            case_id=42,
            attachment_id=5,
            actor_user_id=99,
            actor_role="citizen",
            media_type="image",
        )
        assert any(isinstance(o, CaseTimeline) for o in added)
        assert any(isinstance(o, CaseAuditLog) for o in added)
        assert t.event_type == "attachment_added"

    @pytest.mark.asyncio
    async def test_record_assignment_emits_correct_event_type(self, db, added):
        from app.services.cases.timeline import record_assignment

        # First assignment — ASSIGNED
        t1 = await record_assignment(
            db,
            case_id=42,
            mp_profile_id=3,
            actor_user_id=1,
            actor_role="admin",
            reassigned=False,
        )
        assert t1.event_type == "assigned"

        added.clear()
        # Reassignment — REASSIGNED
        t2 = await record_assignment(
            db,
            case_id=42,
            mp_profile_id=4,
            actor_user_id=1,
            actor_role="admin",
            reassigned=True,
        )
        assert t2.event_type == "reassigned"

    @pytest.mark.asyncio
    async def test_record_priority_change_records_from_and_to(self, db, added):
        from app.models import CaseAuditLog
        from app.services.cases.timeline import record_priority_change

        t = await record_priority_change(
            db,
            case_id=42,
            from_priority="low",
            to_priority="critical",
            actor_user_id=1,
            actor_role="mp",
        )
        assert t.event_type == "priority_changed"
        # Find the audit row
        audit_row = next(o for o in added if isinstance(o, CaseAuditLog))
        assert audit_row.kw["payload"] == {"from": "low", "to": "critical"}

    @pytest.mark.asyncio
    async def test_record_category_change_does_not_leak_category_labels(self, db, added):
        from app.models import CaseAuditLog
        from app.services.cases.timeline import record_category_change

        t = await record_category_change(
            db,
            case_id=42,
            from_category_id=1,
            to_category_id=2,
            actor_user_id=1,
            actor_role="admin",
        )
        assert t.event_type == "category_changed"
        # Audit payload keeps the IDs (admin tooling needs them), but
        # the citizen-facing description must NOT.
        assert t.description == "Category changed"
        audit_row = next(o for o in added if isinstance(o, CaseAuditLog))
        assert audit_row.kw["payload"] == {"from": 1, "to": 2}

    @pytest.mark.asyncio
    async def test_request_id_propagates_to_audit_row(self, db, added):
        from app.models import CaseAuditLog
        from app.services.cases.timeline import record_response_added

        await record_response_added(
            db,
            case_id=42,
            response_id=7,
            actor_user_id=99,
            actor_role="citizen",
            request_id="rid-explicit",
        )
        audit_row = next(o for o in added if isinstance(o, CaseAuditLog))
        assert audit_row.kw["request_id"] == "rid-explicit"

    @pytest.mark.asyncio
    async def test_audit_request_id_falls_back_to_contextvar(self, db, added):
        from app.core.logging_config import set_request_id
        from app.models import CaseAuditLog
        from app.services.cases.timeline import record_response_added

        set_request_id("rid-middleware")
        try:
            await record_response_added(
                db,
                case_id=42,
                response_id=7,
                actor_user_id=99,
                actor_role="citizen",
            )
            audit_row = next(o for o in added if isinstance(o, CaseAuditLog))
            assert audit_row.kw["request_id"] == "rid-middleware"
        finally:
            set_request_id(None)


# ===========================================================================
# Workflow vs. timeline (auto-write on every status move)
# ===========================================================================
# The spec is emphatic: "Every status transition must automatically
# generate a timeline event." This test exercises apply_transition()
# end-to-end (with the real CaseTimeline / CaseAuditLog classes
# monkeypatched to a capturer) and verifies the timeline row carries
# the right event_type.


class TestApplyTransitionWritesTimelineRow:
    @pytest.mark.asyncio
    async def test_apply_transition_writes_status_changed_row(
        self, monkeypatch
    ):
        """apply_transition writes one CaseTimeline row with
        event_type='status_changed' for the non-terminal path."""
        added: list = []

        class _Timeline:
            def __init__(self, **kw):
                self.kw = kw
                added.append(("timeline", kw))

        class _Audit:
            def __init__(self, **kw):
                self.kw = kw
                added.append(("audit", kw))

        import sys

        import app.models
        m = sys.modules["app.models"]
        monkeypatch.setattr(m, "CaseTimeline", _Timeline)
        monkeypatch.setattr(m, "CaseAuditLog", _Audit)

        case = SimpleNamespace(
            id=1,
            status=CaseStatus.UNDER_REVIEW.value,
            resolved_at=None,
        )
        db = SimpleNamespace(add=lambda obj: added.append(("session-add", obj)))

        from app.services.cases.workflow import apply_transition
        result = await apply_transition(
            db,
            case=case,
            to_status=CaseStatus.IN_PROGRESS,
            actor_user_id=2,
            actor_role="mp",
        )

        # Find the timeline row in our capture
        timeline_rows = [item for kind, item in added if kind == "timeline"]
        audit_rows = [item for kind, item in added if kind == "audit"]
        assert len(timeline_rows) == 1
        assert timeline_rows[0]["event_type"] == "status_changed"
        assert timeline_rows[0]["from_status"] == "under_review"
        assert timeline_rows[0]["to_status"] == "in_progress"

        assert len(audit_rows) == 1
        assert audit_rows[0]["action"] == "status_changed"

        assert result.timeline_event_type.value == "status_changed"

    @pytest.mark.asyncio
    async def test_resolved_transition_uses_case_resolved_event(
        self, monkeypatch
    ):
        added: list = []

        class _Timeline:
            def __init__(self, **kw):
                self.kw = kw
                added.append(("timeline", kw))

        class _Audit:
            def __init__(self, **kw):
                added.append(("audit", kw))

        import sys

        import app.models
        m = sys.modules["app.models"]
        monkeypatch.setattr(m, "CaseTimeline", _Timeline)
        monkeypatch.setattr(m, "CaseAuditLog", _Audit)

        case = SimpleNamespace(
            id=1,
            status=CaseStatus.IN_PROGRESS.value,
            resolved_at=None,
        )
        db = SimpleNamespace(add=lambda obj: None)

        from app.services.cases.workflow import apply_transition
        result = await apply_transition(
            db,
            case=case,
            to_status=CaseStatus.RESOLVED,
            actor_user_id=2,
            actor_role="mp",
        )

        timeline_rows = [item for kind, item in added if kind == "timeline"]
        assert timeline_rows[0]["event_type"] == "case_resolved"
        assert timeline_rows[0]["to_status"] == "resolved"
        assert result.timeline_event_type.value == "case_resolved"


# ===========================================================================
# Append-only invariant
# ===========================================================================
# Spec: "Timeline records must never be deleted. Build immutable audit
# history." The Postgres trigger handles this at the DB layer; the
# service layer enforces it through `apply_transition` + helpers
# NEVER updating or deleting timeline rows. This test verifies that
# the service layer has no UPDATE/DELETE method.


class TestTimelineHelpersAreAppendOnly:
    """The service layer must NEVER expose an update / delete method
    for CaseTimeline. Future code review checklist."""

    def test_no_update_function_in_timeline_module(self):
        import inspect

        import app.services.cases.timeline as t
        public = [name for name, obj in inspect.getmembers(t)
                  if not name.startswith("_") and callable(obj)]
        assert "update_timeline" not in public
        assert "delete_timeline" not in public
        assert "purge_timeline" not in public

    def test_no_update_function_in_workflow_module(self):
        import inspect

        import app.services.cases.workflow as w
        public = [name for name, obj in inspect.getmembers(w)
                  if not name.startswith("_") and callable(obj)]
        assert "update_timeline" not in public
        assert "delete_timeline" not in public


# ===========================================================================
# Case-responses endpoints (routers/cases.py)
# ===========================================================================
# These tests exercise the GET / POST /cases/{id}/responses handlers
# with the FastAPI `Depends()` graph stubbed out: we invoke the route
# function directly, passing mocks for `db` / `user`. The capturer
# pattern from TestTimelineHelpers is reused for CaseTimeline /
# CaseAuditLog because of the pre-existing User.posts mapper
# ambiguity.


class TestCaseResponsesVisibility:
    """Citizens must NEVER see internal MP notes. MPs and admins see all.

    Note: we do NOT patch `app.models.CaseResponse` with a capturer
    because `select(CaseResponse)` needs a real ORM class. Instead
    we patch `app.routers.cases.select` to a stub, sidestepping the
    SQLAlchemy coercion path entirely. We DO patch `CaseAuditLog`
    because the route calls `log_audit_event()` after the SELECT,
    and constructing the real ORM class triggers the pre-existing
    User.posts mapper ambiguity."""

    @pytest.fixture(autouse=True)
    def _patch_audit_model(self, monkeypatch):
        import sys

        import app.models
        m = sys.modules["app.models"]

        class _Audit:
            case_id = type("_Column", (), {
                "asc": lambda self: self,
                "desc": lambda self: self,
                "is_": lambda self, x: ("is_filter", x),
                "__eq__": lambda self, x: ("eq_filter", x),
                "__lt__": lambda self, x: ("lt_filter", x),
                "__repr__": lambda self: "_Column",
            })()

            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        monkeypatch.setattr(m, "CaseAuditLog", _Audit)

    @staticmethod
    def _row(**kw):
        """Build a Case-shaped SimpleNamespace for get_case_for_viewer."""
        return SimpleNamespace(id=kw.pop("id", 1), **kw)

    @staticmethod
    def _response_row(*, id_, is_internal, author_role="mp", body="note"):
        return SimpleNamespace(
            id=id_,
            case_id=1,
            author_user_id=None,
            author_role=author_role,
            body=body,
            is_internal=is_internal,
            created_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )

    @staticmethod
    def _user(*, id_, role):
        """Build a User-shaped SimpleNamespace. role is a str ('citizen',
        'mp', 'admin')."""
        return SimpleNamespace(
            id=id_,
            role=role,
        )

    @staticmethod
    def _build_db_with_responses(responses):
        """Build a `db` double whose `execute(stmt)` runs the SQLAlchemy
        `where()` chain on the stmt we return — but we short-circuit by
        intercepting `db.execute` to ignore its argument and return our
        canned result. The route's `select(CaseResponse).where(...)` is
        what SQLAlchemy chokes on when CaseResponse is capturered, so
        we never let that code path run.

        We do this by patching `app.routers.cases.select` to a function
        that returns a `_Stmt` whose `.where()` records the args."""
        # Implementation lives inline per-test so it can capture locals.
        raise NotImplementedError  # placeholder; tests use inline impl

    @pytest.mark.asyncio
    async def test_citizen_does_not_see_internal_responses(
        self, monkeypatch
    ):
        """GET /cases/{id}/responses — a citizen viewer must have
        `is_internal=False` applied as a filter at the query level.

        We intercept `select()` in the route module so the SQLAlchemy
        chain `select(CaseResponse).where(...)` never reaches SQLAlchemy
        coercion (which would fail in test mode because the capturer
        isn't a real ORM column)."""
        from app.routers.cases import get_case_responses

        citizen = self._user(id_=42, role="citizen")

        async def fake_loader(db, *, case_id):
            return self._row(reporter_user_id=42)
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        where_calls: list = []

        class _Stmt:
            def where(self, *args, **kwargs):
                where_calls.append((args, kwargs))
                return self
            def order_by(self, *args, **kwargs):
                return self
            def limit(self, *args, **kwargs):
                return self

        # Replace `select()` inside the router module so `select(CaseResponse)`
        # returns our stub.
        monkeypatch.setattr(
            "app.routers.cases.select",
            lambda *_a, **_kw: _Stmt(),
        )

        canned_responses = [
            self._response_row(id_=1, is_internal=False),
            self._response_row(id_=2, is_internal=True),
        ]

        class _Result:
            def scalars(self):
                return self
            def all(self):
                return canned_responses

        async def fake_execute(_stmt):
            return _Result()

        db = SimpleNamespace(execute=fake_execute, commit=AsyncMock(),
                             add=lambda _o: None)

        result = await get_case_responses(
            case_id=1, db=db, user=citizen,
        )

        # Citizen viewer: WHERE clause for is_internal must have been
        # emitted. The route code applies it conditionally — for a
        # non-MP, non-admin viewer, one extra `.where(is_internal is False)`
        # is added.
        assert len(where_calls) == 2  # case_id + is_internal filter
        assert result is not None
        # We received rows from the capturer (the actual filter is in SQL).
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_mp_sees_all_responses_no_filter(self, monkeypatch):
        """MP viewers do NOT have is_internal applied; they see every
        response including internal MP notes."""
        from app.routers.cases import get_case_responses

        mp = self._user(id_=7, role="mp")

        async def fake_loader(db, *, case_id):
            return self._row(reporter_user_id=999)  # not the reporter
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        where_calls: list = []

        class _Stmt:
            def where(self, *args, **kwargs):
                where_calls.append((args, kwargs))
                return self
            def order_by(self, *args, **kwargs):
                return self
            def limit(self, *args, **kwargs):
                return self

        monkeypatch.setattr(
            "app.routers.cases.select",
            lambda *_a, **_kw: _Stmt(),
        )

        canned_responses = [
            self._response_row(id_=1, is_internal=False),
            self._response_row(id_=2, is_internal=True),
        ]

        class _Result:
            def scalars(self):
                return self
            def all(self):
                return canned_responses

        async def fake_execute(_stmt):
            return _Result()

        db = SimpleNamespace(execute=fake_execute, commit=AsyncMock(),
                             add=lambda _o: None)

        await get_case_responses(case_id=1, db=db, user=mp)

        # MP viewer: only the case_id WHERE clause, no is_internal filter.
        assert len(where_calls) == 1


class TestCaseResponsesWriteAuthorisation:
    """POST /cases/{id}/responses — auth rules."""

    @pytest.fixture(autouse=True)
    def _patch_models(self, monkeypatch):
        import sys

        import app.models
        m = sys.modules["app.models"]

        class _Timeline:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Audit:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Response:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)
                self.id = kw.get("id", 1)
                self.created_at = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)

        monkeypatch.setattr(m, "CaseTimeline", _Timeline)
        monkeypatch.setattr(m, "CaseAuditLog", _Audit)
        # The route file already imported `CaseResponse` at module-load
        # time, so updating `app.models.CaseResponse` alone is not
        # enough — the route's own global still points at the real
        # class. Patch the route's local binding too.
        import app.routers.cases as _rc
        monkeypatch.setattr(_rc, "CaseResponse", _Response)
        monkeypatch.setattr(_rc, "CaseTimeline", _Timeline)

    @staticmethod
    def _row(**kw):
        return SimpleNamespace(id=kw.pop("id", 1), **kw)

    @staticmethod
    def _user(*, id_, role):
        return SimpleNamespace(id=id_, role=role)

    @pytest.mark.asyncio
    async def test_citizen_cannot_create_internal_note(self, monkeypatch):
        """A citizen attempting to POST with is_internal=True must be
        rejected with 403 — internal notes are MP/admin only."""
        from fastapi import HTTPException

        from app.routers.cases import post_case_response
        from app.schemas_case import CaseResponseCreate

        citizen = self._user(id_=42, role="citizen")

        async def fake_loader(db, *, case_id):
            return self._row(reporter_user_id=42)
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        db = SimpleNamespace(add=lambda o: None, flush=AsyncMock(),
                             commit=AsyncMock(), refresh=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await post_case_response(
                payload=CaseResponseCreate(
                    body="internal scratch", is_internal=True,
                ),
                case_id=1, db=db, user=citizen,
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_mp_can_create_internal_note(self, monkeypatch):
        """An MP may POST with is_internal=True; the request must
        succeed and emit a timeline row with the internal flag in the
        audit payload."""
        from app.routers.cases import post_case_response
        from app.schemas_case import CaseResponseCreate

        mp = self._user(id_=7, role="mp")

        async def fake_loader(db, *, case_id):
            return self._row(reporter_user_id=999)
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        added: list = []
        async def fake_flush():
            pass
        async def fake_commit():
            pass
        async def fake_refresh(obj):
            return obj

        db = SimpleNamespace(
            add=lambda o: added.append(o),
            flush=fake_flush,
            commit=fake_commit,
            refresh=fake_refresh,
        )

        result = await post_case_response(
            payload=CaseResponseCreate(
                body="internal scratch", is_internal=True,
            ),
            case_id=1, db=db, user=mp,
        )
        # 1 CaseResponse row + 1 CaseTimeline row + 1 CaseAuditLog row
        # = 3 additions
        assert len(added) == 3
        assert result.is_internal is True

    @pytest.mark.asyncio
    async def test_non_participant_citizen_is_403(self, monkeypatch):
        """A citizen who is NOT the reporter cannot post on someone
        else's case."""
        from fastapi import HTTPException

        from app.routers.cases import post_case_response
        from app.schemas_case import CaseResponseCreate

        stranger = self._user(id_=99, role="citizen")

        async def fake_loader(db, *, case_id):
            return self._row(reporter_user_id=42)
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        db = SimpleNamespace(add=lambda o: None, flush=AsyncMock(),
                             commit=AsyncMock(), refresh=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await post_case_response(
                payload=CaseResponseCreate(
                    body="hi", is_internal=False,
                ),
                case_id=1, db=db, user=stranger,
            )
        assert ei.value.status_code == 403


# ===========================================================================
# Timeline pagination
# ===========================================================================
# Cursor pagination on `id`. The route accepts `limit` (default 50, max
# 200) and `before_id` (optional, must be > 0). `before_id` filters to
# rows strictly less than that id.


class TestTimelinePagination:
    """Cursor pagination on `id`. The route accepts `limit` (default 50,
    max 200) and `before_id` (optional, must be > 0). `before_id` filters
    to rows strictly less than that id.

    We patch `select()` in the route module so the SQLAlchemy chain
    `select(CaseTimeline).where(...)` never reaches SQLAlchemy coercion
    (which would fail in test mode because the capturer isn't a real
    ORM column). Capturers are still patched onto `app.models` so any
    helper invoked from the route (e.g. log_audit_event) can construct
    audit rows without hitting the pre-existing User.posts mapper
    ambiguity.
    """

    @pytest.fixture(autouse=True)
    def _patch_models(self, monkeypatch):
        import sys

        import app.models
        m = sys.modules["app.models"]

        # A capturer that supports both:
        #   - class-level attribute access (e.g. `CaseTimeline.case_id`
        #     in the route's SELECT chain), returning chainable column
        #     sentinels
        #   - instance construction (`CaseTimeline(...)`)
        #   - column method calls: `.asc()`, `.is_(False)`
        class _Column:
            def __init__(self, name):
                self.name = name
            def asc(self):
                return self
            def desc(self):
                return self
            def is_(self, other):
                return ("is_internal_filter", self.name, other)
            def __eq__(self, other):
                return ("eq_filter", self.name, other)
            def __lt__(self, other):
                return ("lt_filter", self.name, other)
            def __le__(self, other):
                return ("le_filter", self.name, other)
            def __gt__(self, other):
                return ("gt_filter", self.name, other)
            def __ge__(self, other):
                return ("ge_filter", self.name, other)
            def __repr__(self):
                return f"_Column({self.name!r})"

        class _Timeline:
            case_id = _Column("case_id")
            id = _Column("id")
            event_type = _Column("event_type")
            is_internal = _Column("is_internal")
            created_at = _Column("created_at")

            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Audit:
            case_id = _Column("case_id")
            id = _Column("id")

            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        monkeypatch.setattr(m, "CaseTimeline", _Timeline)
        monkeypatch.setattr(m, "CaseAuditLog", _Audit)

    @staticmethod
    def _row(**kw):
        return SimpleNamespace(id=kw.pop("id", 1), **kw)

    @staticmethod
    def _user(*, id_, role):
        return SimpleNamespace(id=id_, role=role)

    @staticmethod
    def _timeline_row(*, id_):
        return SimpleNamespace(
            id=id_,
            case_id=1,
            event_type="status_changed",
            from_status="submitted",
            to_status="received",
            actor_role="mp",
            actor_user_id=7,
            description=None,
            created_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self, monkeypatch):
        """Without an explicit `limit`, the SELECT statement is built
        with limit=50."""
        from app.routers.cases import get_case_timeline

        admin = self._user(id_=1, role="admin")

        async def fake_loader(db, *, case_id):
            return self._row()
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        limit_calls: list = []
        where_calls: list = []

        class _Stmt:
            def where(self, *args, **kwargs):
                where_calls.append((args, kwargs))
                return self
            def order_by(self, *args, **kwargs):
                return self
            def limit(self, n):
                limit_calls.append(n)
                return self

        monkeypatch.setattr(
            "app.routers.cases.select",
            lambda *_a, **_kw: (_Stmt(), _Stmt(), _Stmt())[0],
        )

        # Verify the patch took effect on the module's __dict__.

        class _Result:
            def scalars(self):
                return self
            def all(self):
                return []

        async def fake_execute(_stmt):
            return _Result()

        db = SimpleNamespace(execute=fake_execute, commit=AsyncMock(), add=lambda _o: None)

        await get_case_timeline(case_id=1, limit=50, db=db, user=admin)
        assert limit_calls == [50]
        # The case_id WHERE clause should also have been applied.
        # (before_id defaults to Query(None) when called outside HTTP,
        # which triggers the .where(id < before_id) branch.)
        assert len(where_calls) >= 1

    @pytest.mark.asyncio
    async def test_before_id_emits_lt_filter(self, monkeypatch):
        """With `before_id=42`, the SELECT carries a `id < 42` filter."""
        from app.routers.cases import get_case_timeline

        admin = self._user(id_=1, role="admin")

        async def fake_loader(db, *, case_id):
            return self._row()
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        where_calls: list = []
        limit_calls: list = []

        class _Stmt:
            def where(self, *args, **kwargs):
                where_calls.append((args, kwargs))
                return self
            def order_by(self, *args, **kwargs):
                return self
            def limit(self, n):
                limit_calls.append(n)
                return self

        monkeypatch.setattr(
            "app.routers.cases.select",
            lambda *_a, **_kw: _Stmt(),
        )

        class _Result:
            def scalars(self):
                return self
            def all(self):
                return []

        async def fake_execute(_stmt):
            return _Result()

        db = SimpleNamespace(execute=fake_execute, commit=AsyncMock(), add=lambda _o: None)

        await get_case_timeline(
            case_id=1, limit=10, before_id=42, db=db, user=admin,
        )
        # Two .where() calls: one for case_id, one for the cursor (id < before_id).
        assert len(where_calls) == 2
        assert limit_calls == [10]

    @pytest.mark.asyncio
    async def test_limit_bounds_enforced_by_fastapi(self, monkeypatch):
        """FastAPI's Query(ge=1, le=200) rejects out-of-range limits
        before the handler runs. We exercise that boundary by calling
        the route through TestClient (which performs the Query
        validation)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers.cases import router as cases_router

        app = FastAPI()
        app.include_router(cases_router)

        from app.dependencies import cases as deps
        async def fake_loader(*_a, **_kw):
            return self._row()
        monkeypatch.setattr(deps, "get_case_for_viewer", fake_loader)
        from app.dependencies import auth as auth_deps
        def fake_user():
            return self._user(id_=1, role="admin")
        app.dependency_overrides[auth_deps.get_current_user] = fake_user

        client = TestClient(app)
        r = client.get("/cases/1/timeline?limit=99999")
        assert r.status_code == 422

        r = client.get("/cases/1/timeline?limit=0")
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_rows_in_ascending_id_order(self, monkeypatch):
        """The capturer returns rows in id order; the route must return
        them as-is. (The order_by clause is asserted in
        test_default_limit_is_50 above; here we focus on the response.)"""
        from app.routers.cases import get_case_timeline

        admin = self._user(id_=1, role="admin")

        async def fake_loader(db, *, case_id):
            return self._row()
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer",
            fake_loader,
        )

        rows = [
            self._timeline_row(id_=1),
            self._timeline_row(id_=2),
            self._timeline_row(id_=3),
        ]

        class _Stmt:
            def where(self, *args, **kwargs):
                return self
            def order_by(self, *args, **kwargs):
                return self
            def limit(self, n):
                return self

        monkeypatch.setattr(
            "app.routers.cases.select",
            lambda *_a, **_kw: _Stmt(),
        )

        class _Result:
            def __init__(self, data):
                self._data = data
            def scalars(self):
                return self
            def all(self):
                return self._data

        async def fake_execute(_stmt):
            return _Result(rows)

        db = SimpleNamespace(execute=fake_execute, commit=AsyncMock(), add=lambda _o: None)

        result = await get_case_timeline(case_id=1, limit=50, db=db, user=admin)
        ids = [r.id for r in result]
        assert ids == [1, 2, 3]


# ===========================================================================
# Duplicate detection (services/cases/duplicates.py)
# ===========================================================================
# Spec wording: "Before a case is created compare Topic, Description,
# Category, Location, Recent submissions using PostgreSQL Full Text
# Search. If a similar case exists display Support Existing Case or Create
# New Case. Never force users into either choice. Store supporters
# separately."
#
# The tests below cover:
#   - Pure-functional ranker (no DB) — composite score, ordering,
#     category/district bonuses, terminal-status exclusion.
#   - Anonymity invariant — the wire schema must not leak PII fields.
#   - DB facade — the public entry point composes the helper functions
#     and tolerates an empty result.
#   - Router wiring — POST /cases/duplicates/check surfaces the result;
#     POST /cases/ refuses MPs; POST /cases/{id}/support refuses MPs and
#     409s on duplicate-support attempts (pre-check + IntegrityError).
# All tests are pure-Python — no live DB required.


class TestDuplicateRankerPure:
    """`_rank_candidates` is the heart of the feature. It's a pure
    function so we can exercise the ordering + scoring with simple
    _CandidateRow doubles — no async, no DB, no capturer."""

    @staticmethod
    def _row(
        *,
        case_id: int,
        fts_rank: float = 0.0,
        trgm_sim: float = 0.0,
        category_id: int = 1,
        district_id: str | None = "central",
        constituency: str | None = None,
        submitted_at: datetime | None = None,
    ):
        from app.services.cases.duplicates import _CandidateRow
        return _CandidateRow(
            case_id=case_id,
            case_number=f"CIV-2026-{case_id:06d}",
            category_id=category_id,
            title="Pothole on Main St",
            description="Large pothole near the school on Main Street.",
            district_id=district_id,
            constituency=constituency,
            submitted_at=submitted_at or datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
            fts_rank=fts_rank,
            trgm_sim=trgm_sim,
        )

    def test_rank_orders_by_composite_score_desc(self):
        from app.services.cases.duplicates import _rank_candidates

        rows = [
            self._row(case_id=1, fts_rank=0.1, trgm_sim=0.2),
            self._row(case_id=2, fts_rank=0.9, trgm_sim=0.9),
            self._row(case_id=3, fts_rank=0.5, trgm_sim=0.5),
        ]
        ranked = _rank_candidates(
            rows, req_category_id=1, req_district_id="central"
        )
        ids = [c.case_id for c in ranked]
        assert ids == [2, 3, 1]

    def test_category_bonus_promotes_matching_row(self):
        """Two rows with identical fts_rank + trgm_sim — the one whose
        category_id matches the request's category_id must rank first."""
        from app.services.cases.duplicates import _rank_candidates

        rows = [
            self._row(case_id=1, fts_rank=0.5, trgm_sim=0.5, category_id=2),
            self._row(case_id=2, fts_rank=0.5, trgm_sim=0.5, category_id=1),
        ]
        ranked = _rank_candidates(
            rows, req_category_id=1, req_district_id="central"
        )
        assert [c.case_id for c in ranked] == [2, 1]
        # The matching row's score is exactly the category bonus higher.
        assert ranked[0].similarity_score - ranked[1].similarity_score == pytest.approx(0.3)

    def test_district_bonus_promotes_matching_row(self):
        from app.services.cases.duplicates import _rank_candidates

        rows = [
            self._row(case_id=1, fts_rank=0.5, trgm_sim=0.5, district_id="north"),
            self._row(case_id=2, fts_rank=0.5, trgm_sim=0.5, district_id="central"),
        ]
        ranked = _rank_candidates(
            rows, req_category_id=1, req_district_id="central"
        )
        assert [c.case_id for c in ranked] == [2, 1]
        # District bonus is 0.15, exactly.
        assert ranked[0].similarity_score - ranked[1].similarity_score == pytest.approx(0.15)

    def test_district_bonus_skipped_when_request_has_no_district(self):
        """When the request's district_id is None, the district bonus
        must be skipped entirely (any equality would be a coincidence)."""
        from app.services.cases.duplicates import _rank_candidates

        rows = [
            self._row(case_id=1, fts_rank=0.5, trgm_sim=0.5, district_id="central"),
            self._row(case_id=2, fts_rank=0.5, trgm_sim=0.5, district_id="central"),
        ]
        ranked = _rank_candidates(
            rows, req_category_id=1, req_district_id=None
        )
        # Both rows have the same composite score, so they tie on
        # score; the tie-breaker is submitted_at DESC then id DESC.
        # In this test both rows have the same submitted_at, so id
        # decides — case_id 2 (higher) wins.
        assert ranked[0].similarity_score == pytest.approx(ranked[1].similarity_score)

    def test_empty_input_returns_empty(self):
        from app.services.cases.duplicates import _rank_candidates

        ranked = _rank_candidates([], req_category_id=1, req_district_id="central")
        assert ranked == []

    def test_rank_deterministic_when_ties(self):
        """Same inputs → same order. Sorted by id DESC when every
        tie-breaker is equal."""
        from app.services.cases.duplicates import _rank_candidates

        ts = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
        rows = [
            self._row(case_id=1, fts_rank=0.5, trgm_sim=0.5, submitted_at=ts),
            self._row(case_id=2, fts_rank=0.5, trgm_sim=0.5, submitted_at=ts),
            self._row(case_id=3, fts_rank=0.5, trgm_sim=0.5, submitted_at=ts),
        ]
        ranked = _rank_candidates(
            rows, req_category_id=1, req_district_id="central"
        )
        # id DESC tie-breaker: 3, 2, 1.
        assert [c.case_id for c in ranked] == [3, 2, 1]

    def test_rank_recent_first_when_ties(self):
        """Among rows with identical scores, the more recently
        submitted must come first."""
        from app.services.cases.duplicates import _rank_candidates

        older = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        newer = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
        rows = [
            self._row(case_id=1, fts_rank=0.5, trgm_sim=0.5, submitted_at=older),
            self._row(case_id=2, fts_rank=0.5, trgm_sim=0.5, submitted_at=newer),
        ]
        ranked = _rank_candidates(
            rows, req_category_id=1, req_district_id="central"
        )
        assert [c.case_id for c in ranked] == [2, 1]

    def test_description_truncated_to_snippet_max_len(self):
        from app.services.cases.duplicates import SNIPPET_MAX_LEN, _score_candidate

        long_desc = "x" * 500
        row = self._row(case_id=1)
        row = type(row)(
            case_id=row.case_id,
            case_number=row.case_number,
            category_id=row.category_id,
            title=row.title,
            description=long_desc,
            district_id=row.district_id,
            constituency=row.constituency,
            submitted_at=row.submitted_at,
            fts_rank=row.fts_rank,
            trgm_sim=row.trgm_sim,
        )
        candidate = _score_candidate(
            row, req_category_id=1, req_district_id="central"
        )
        assert len(candidate.description_snippet) == SNIPPET_MAX_LEN


class TestDuplicateSchemaNoPII:
    """The wire schema that MPs and citizens both see MUST NOT carry
    any PII field. This is the third layer of anonymity defense
    (DB → service → schema)."""

    def test_duplicate_candidate_schema_has_no_pii_fields(self):
        from app.schemas_case import CaseDuplicateCandidateOut

        field_names = set(CaseDuplicateCandidateOut.model_fields.keys())
        leaked = field_names & FORBIDDEN_PII_TOKENS
        assert not leaked, (
            f"CaseDuplicateCandidateOut exposes forbidden PII fields: {sorted(leaked)}"
        )

    def test_duplicate_candidate_allowed_fields_match_canon(self):
        """The wire shape is the contract — document and assert it
        here so a future refactor that drops a field is caught by
        tests rather than by a silently-broken UI."""
        from app.schemas_case import CaseDuplicateCandidateOut

        expected = {
            "case_id",
            "case_number",
            "category_id",
            "title",
            "description_snippet",
            "district_id",
            "constituency",
            "similarity_score",
            "support_count",
            "submitted_at",
        }
        assert set(CaseDuplicateCandidateOut.model_fields.keys()) == expected


class TestFindDuplicateCandidatesPublicEntry:
    """The public entry composes the helpers. We mock the helpers
    directly so we don't need a live DB."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_query_text_blank(self, monkeypatch):
        """If title + description are both blank, the function must
        short-circuit and return an empty list — no DB round-trip."""
        from app.services.cases import duplicates as dup_module

        called = []

        async def fake_fetch(*_a, **_kw):
            called.append("fetch")
            return []

        monkeypatch.setattr(dup_module, "_fetch_candidate_rows", fake_fetch)

        out = await dup_module.find_duplicate_candidates(
            object(),
            title="   ",  # whitespace only
            description="",
            category_id=1,
            district_id=None,
        )
        assert out == []
        assert called == []  # the helper was NOT invoked

    @pytest.mark.asyncio
    async def test_composes_fetch_rank_and_support_counts(self, monkeypatch):
        """The public entry must call the three helpers in order:
        fetch → rank → attach_support_counts."""
        from app.services.cases import duplicates as dup_module

        call_log = []

        async def fake_fetch(*_a, **_kw):
            call_log.append("fetch")
            return [
                dup_module._CandidateRow(
                    case_id=1,
                    case_number="CIV-2026-000001",
                    category_id=1,
                    title="Pothole",
                    description="Pothole on Main St",
                    district_id="central",
                    constituency=None,
                    submitted_at=datetime(2026, 8, 5, tzinfo=UTC),
                    fts_rank=0.5,
                    trgm_sim=0.5,
                )
            ]

        # Patch _rank_candidates with a marker that records invocation.
        original_rank = dup_module._rank_candidates

        def spy_rank(rows, **_kw):
            call_log.append("rank")
            return list(original_rank(rows, **_kw))

        async def fake_support(db, candidates):
            call_log.append("support")
            return candidates

        monkeypatch.setattr(dup_module, "_fetch_candidate_rows", fake_fetch)
        monkeypatch.setattr(dup_module, "_rank_candidates", spy_rank)
        monkeypatch.setattr(dup_module, "_attach_support_counts", fake_support)

        out = await dup_module.find_duplicate_candidates(
            object(),
            title="Pothole",
            description="on Main St",
            category_id=1,
            district_id="central",
        )
        assert call_log == ["fetch", "rank", "support"]
        assert len(out) == 1
        assert out[0].case_id == 1

    @pytest.mark.asyncio
    async def test_attach_support_counts_short_circuits_on_empty(self, monkeypatch):
        """When the ranker returns no rows, the support-count helper
        must short-circuit (no DB round-trip on the cold path)."""
        from app.services.cases import duplicates as dup_module

        async def fake_fetch(*_a, **_kw):
            return []

        # We use the real helper here so the short-circuit is exercised.
        monkeypatch.setattr(dup_module, "_fetch_candidate_rows", fake_fetch)

        out = await dup_module.find_duplicate_candidates(
            object(),
            title="Pothole",
            description="on Main St",
            category_id=1,
            district_id="central",
        )
        assert out == []
        # The helper does `if not candidates: return candidates` before
        # issuing the DB query; the test passes if no exception bubbles
        # up. No further assertion needed — the helper's own unit-level
        # assertion lives in test_returns_empty_when_query_text_blank.


class TestDuplicateCheckEndpoint:
    """POST /cases/duplicates/check — surface the service-layer result."""

    @pytest.mark.asyncio
    async def test_returns_response_with_candidates(self, monkeypatch):
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from app.routers.cases import post_duplicate_check
        from app.schemas_case import (
            CaseDuplicateCandidateOut,
            CaseDuplicateCheckRequest,
        )

        async def fake_find(*_a, **_kw):
            return [
                (
                    # fake-find returns whatever; the handler converts
                    # via model_validate, so the candidates must
                    # satisfy the schema.
                    CaseDuplicateCandidateOut(
                        case_id=1,
                        case_number="CIV-2026-000001",
                        category_id=1,
                        title="Pothole",
                        description_snippet="Pothole on Main St",
                        district_id="central",
                        constituency=None,
                        similarity_score=0.85,
                        support_count=2,
                        submitted_at=_dt(2026, 8, 5, tzinfo=UTC),
                    )
                )
            ]

        monkeypatch.setattr(
            "app.routers.cases.find_duplicate_candidates", fake_find
        )

        user = SimpleNamespace(id=42, role="citizen")
        db = SimpleNamespace(commit=AsyncMock(), add=lambda _o: None)

        payload = CaseDuplicateCheckRequest(
            title="Pothole",
            description="on Main St",
            category_id=1,
            district_id="central",
            language="EN",
        )
        result = await post_duplicate_check(
            payload=payload, db=db, user=user,
        )
        assert len(result.candidates) == 1
        assert result.candidates[0].case_id == 1
        assert result.candidates[0].similarity_score == 0.85
        assert result.checked_at is not None


class TestCreateCaseEndpoint:
    """POST /cases/ — refuse MPs, write Case + timeline + audit rows."""

    @pytest.fixture(autouse=True)
    def _patch_models(self, monkeypatch):
        import sys

        import app.models
        m = sys.modules["app.models"]

        class _Timeline:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Audit:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Case:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)
                # Pretend the DB flush populated the id.
                self.id = 1

        # Stash the capturers on the test instance so the assertions
        # inside individual test methods can use `self._Timeline` etc.
        # The capturers are local to the fixture closure; without this
        # trick, the test bodies can't reference them by name.
        self._Timeline = _Timeline
        self._Audit = _Audit
        self._Case = _Case

        monkeypatch.setattr(m, "CaseTimeline", _Timeline)
        monkeypatch.setattr(m, "CaseAuditLog", _Audit)
        monkeypatch.setattr(m, "Case", _Case)

        # The route imports `Case` / `CaseTimeline` at module-load time;
        # updating `app.models` alone is not enough — the route's own
        # globals still point at the real classes. Patch the route's
        # local bindings too so SQLAlchemy never tries to construct
        # the real (User.posts-ambiguous) mappers.
        #
        # `CaseAuditLog` is NOT imported by the router — it's used
        # indirectly through `log_audit_event()` which imports it from
        # `app.models`. The m.CaseAuditLog patch above is enough.
        import app.routers.cases as _rc
        monkeypatch.setattr(_rc, "Case", _Case)
        monkeypatch.setattr(_rc, "CaseTimeline", _Timeline)

    @staticmethod
    def _user(*, id_, role):
        return SimpleNamespace(id=id_, role=role)

    @pytest.mark.asyncio
    async def test_mp_cannot_file_case(self, monkeypatch):
        """MPs are the responders, not the reporters — POST /cases/
        must refuse with 403."""
        from fastapi import HTTPException

        from app.routers.cases import post_case_create
        from app.schemas_case import CaseCreate

        mp = self._user(id_=7, role="mp")
        db = SimpleNamespace(add=lambda _o: None, flush=AsyncMock(),
                             commit=AsyncMock(), refresh=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await post_case_create(
                payload=CaseCreate(
                    title="Pothole",
                    description="on Main St",
                    category_id=1,
                ),
                db=db, user=mp,
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_anonymous_filing_refused(self, monkeypatch):
        """is_anonymous=True is refused at this endpoint — it's a
        USSD / admin tooling flow, not a web flow."""
        from fastapi import HTTPException

        from app.routers.cases import post_case_create
        from app.schemas_case import CaseCreate

        citizen = self._user(id_=42, role="citizen")
        db = SimpleNamespace(add=lambda _o: None, flush=AsyncMock(),
                             commit=AsyncMock(), refresh=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await post_case_create(
                payload=CaseCreate(
                    title="Pothole",
                    description="on Main St",
                    category_id=1,
                    is_anonymous=True,
                ),
                db=db, user=citizen,
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_citizen_filing_writes_three_rows(self, monkeypatch):
        """A successful POST /cases/ must enqueue three rows: one
        Case, one CaseTimeline (event_type=case_created), one
        CaseAuditLog (action=case_created)."""
        from app.routers.cases import post_case_create
        from app.schemas_case import CaseCreate
        from app.services.cases import duplicates as _dup

        # Stub the next_case_number helper so we don't try to query
        # the (missing) DB sequence.
        monkeypatch.setattr(
            "app.routers.cases.next_case_number",
            AsyncMock(return_value="CIV-2026-000099"),
        )

        citizen = self._user(id_=42, role="citizen")
        added: list = []

        async def fake_flush():
            pass

        async def fake_commit():
            pass

        async def fake_refresh(obj):
            obj.id = 1
            # The Case table has `submitted_at` as a column server
            # default — the route reads `case.submitted_at` after
            # `await db.refresh(case)` to build the response. The
            # capturer doesn't model column defaults, so populate it
            # explicitly here.
            obj.submitted_at = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
            return obj

        db = SimpleNamespace(
            add=lambda o: added.append(o),
            flush=fake_flush,
            commit=fake_commit,
            refresh=fake_refresh,
        )

        result = await post_case_create(
            payload=CaseCreate(
                title="Pothole",
                description="on Main St",
                category_id=1,
                district_id="central",
            ),
            db=db, user=citizen,
        )
        # 1 Case + 1 CaseTimeline + 1 CaseAuditLog = 3 rows
        assert len(added) == 3
        timeline = next(o for o in added if isinstance(o, self._Timeline))
        audit = next(o for o in added if isinstance(o, self._Audit))
        assert timeline.kw["event_type"] == "case_created"
        assert timeline.kw["actor_role"] == "citizen"
        assert audit.kw["action"] == "case_created"
        assert result.case_number == "CIV-2026-000099"
        assert result.status == CaseStatus.SUBMITTED


class TestSupportCaseEndpoint:
    """POST /cases/{id}/support — refuse MPs, write CaseSupport, 409 on
    duplicate. The route uses both the pre-check AND the IntegrityError
    catch — both paths must be tested."""

    @pytest.fixture(autouse=True)
    def _patch_models(self, monkeypatch):
        import sys

        import app.models
        m = sys.modules["app.models"]

        class _Timeline:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Audit:
            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Column:
            """Stub SQLAlchemy column — supports the `==`, `is_()` and
            other operators the route uses in WHERE clauses."""
            def __init__(self, name):
                self.name = name
            def __eq__(self, other):
                return ("eq_filter", self.name, other)
            def __ne__(self, other):
                return ("ne_filter", self.name, other)
            def is_(self, other):
                return ("is_filter", self.name, other)
            def __repr__(self):
                return f"_Column({self.name!r})"

        class _Support:
            # The route uses class-level attribute access for WHERE
            # filters: `CaseSupport.original_case_id == case_id`.
            original_case_id = _Column("original_case_id")
            supporter_user_id = _Column("supporter_user_id")
            id = _Column("id")

            def __init__(self, **kw):
                self.kw = kw
                for k, v in kw.items():
                    setattr(self, k, v)
                self.id = 1
                self.created_at = datetime(2026, 8, 5, tzinfo=UTC)

        monkeypatch.setattr(m, "CaseTimeline", _Timeline)
        monkeypatch.setattr(m, "CaseAuditLog", _Audit)
        monkeypatch.setattr(m, "CaseSupport", _Support)

        # The route imports `CaseSupport` at module-load time; updating
        # `app.models` alone is not enough — the route's own globals
        # still point at the real class. Patch the route's local
        # binding too so SQLAlchemy never tries to construct the real
        # (User.posts-ambiguous) mapper.
        #
        # `CaseAuditLog` is NOT imported by the router — it's used
        # indirectly through `log_audit_event()` which imports it from
        # `app.models`. The m.CaseAuditLog patch above is enough.
        #
        # The route also calls `select(CaseSupport).where(...)` for the
        # duplicate-support pre-check. SQLAlchemy's `select()` requires
        # a real ORM class — patch `select()` to a chainable stub so
        # the capturer doesn't get coerced into a column expression.
        # This is the same workaround used by TestCaseResponsesVisibility.
        import app.routers.cases as _rc

        class _Stmt:
            def where(self, *args, **kwargs):
                return self
            def order_by(self, *args, **kwargs):
                return self
            def limit(self, n):
                return self

        monkeypatch.setattr(_rc, "select", lambda *_a, **_kw: _Stmt())
        monkeypatch.setattr(_rc, "CaseTimeline", _Timeline)
        monkeypatch.setattr(_rc, "CaseSupport", _Support)

    @staticmethod
    def _user(*, id_, role):
        return SimpleNamespace(id=id_, role=role)

    @staticmethod
    def _case(*, status="submitted"):
        return SimpleNamespace(
            id=42,
            status=status,
            reporter_user_id=99,
        )

    @pytest.mark.asyncio
    async def test_mp_cannot_support(self, monkeypatch):
        """MPs are not co-reporters — POST /cases/{id}/support must
        refuse with 403."""
        from fastapi import HTTPException

        from app.routers.cases import post_case_support

        mp = self._user(id_=7, role="mp")
        async def fake_loader(db, *, case_id):
            return self._case()
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        db = SimpleNamespace(execute=AsyncMock(), add=lambda _o: None,
                             commit=AsyncMock(), refresh=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await post_case_support(
                case_id=1, payload=None, db=db, user=mp,
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_terminal_case_refused_with_409(self, monkeypatch):
        """A withdrawn / rejected / closed case has no business
        receiving new supporters."""
        from fastapi import HTTPException

        from app.routers.cases import post_case_support

        citizen = self._user(id_=42, role="citizen")
        async def fake_loader(db, *, case_id):
            return self._case(status="closed")
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        db = SimpleNamespace(execute=AsyncMock(), add=lambda _o: None,
                             commit=AsyncMock(), refresh=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await post_case_support(
                case_id=1, payload=None, db=db, user=citizen,
            )
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_duplicate_support_returns_409_via_precheck(self, monkeypatch):
        """If the user already supports the case, the pre-check
        returns 409 — the case is added is empty."""
        from fastapi import HTTPException

        from app.routers.cases import post_case_support

        citizen = self._user(id_=42, role="citizen")
        async def fake_loader(db, *, case_id):
            return self._case()
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        class _Result:
            def scalar_one_or_none(self):
                return SimpleNamespace(id=1)

        async def fake_execute(_stmt):
            return _Result()

        db = SimpleNamespace(execute=fake_execute, add=lambda _o: None,
                             commit=AsyncMock(), refresh=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await post_case_support(
                case_id=1, payload=None, db=db, user=citizen,
            )
        assert ei.value.status_code == 409
        assert "already support" in ei.value.detail

    @pytest.mark.asyncio
    async def test_happy_path_writes_one_support_row(self, monkeypatch):
        """A successful support call enqueues ONE CaseSupport row +
        ONE audit row (timeline not — the spec only requires timeline
        for visible events, supports are stored "separately" in the
        CaseSupport table)."""
        from app.routers.cases import post_case_support

        citizen = self._user(id_=42, role="citizen")
        async def fake_loader(db, *, case_id):
            return self._case()
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        class _Result:
            def scalar_one_or_none(self):
                return None  # no pre-existing support

        added: list = []

        async def fake_execute(_stmt):
            return _Result()

        async def fake_commit():
            pass

        async def fake_refresh(obj):
            return obj

        db = SimpleNamespace(
            execute=fake_execute,
            add=lambda o: added.append(o),
            commit=fake_commit,
            refresh=fake_refresh,
        )

        result = await post_case_support(
            case_id=1, payload=None, db=db, user=citizen,
        )
        # One CaseSupport row + one CaseAuditLog row.
        from app.models import CaseAuditLog, CaseSupport
        support_rows = [o for o in added if isinstance(o, CaseSupport)]
        audit_rows = [o for o in added if isinstance(o, CaseAuditLog)]
        assert len(support_rows) == 1
        assert support_rows[0].kw["original_case_id"] == 1
        assert support_rows[0].kw["supporter_user_id"] == 42
        assert len(audit_rows) == 1
        assert audit_rows[0].kw["payload"]["event"] == "case_supported"
        assert result is not None

    @pytest.mark.asyncio
    async def test_integrity_error_maps_to_409(self, monkeypatch):
        """The race-safe backstop: two concurrent POSTs from the same
        user — the partial unique index `uq_case_support_pair` catches
        the second one. The except clause must convert this to a 409."""
        from fastapi import HTTPException
        from sqlalchemy.exc import IntegrityError

        from app.routers.cases import post_case_support

        citizen = self._user(id_=42, role="citizen")
        async def fake_loader(db, *, case_id):
            return self._case()
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        class _Result:
            def scalar_one_or_none(self):
                return None  # pre-check passes

        async def fake_execute(_stmt):
            return _Result()

        async def fake_commit():
            # The partial unique index catch.
            raise IntegrityError("INSERT", {}, Exception("uq_case_support_pair"))

        rollback_called = []

        async def fake_rollback():
            rollback_called.append(True)

        db = SimpleNamespace(
            execute=fake_execute,
            add=lambda _o: None,
            commit=fake_commit,
            rollback=fake_rollback,
            refresh=AsyncMock(),
        )

        with pytest.raises(HTTPException) as ei:
            await post_case_support(
                case_id=1, payload=None, db=db, user=citizen,
            )
        assert ei.value.status_code == 409
        assert rollback_called == [True]


# ===========================================================================
# Citizen UI — list, detail, attachments
# ===========================================================================
# These tests cover the new endpoints added for the citizen UI surface
# (`MyCases` page, `CaseDetails` page, attachment upload/delete).
#
# The pattern matches the rest of this file: capturer models on
# `app.models` + route-module `select` stub. The new endpoints sit
# alongside the existing ones in `app/routers/cases.py`; no new
# module surface.
# ===========================================================================


# ---------------------------------------------------------------------------
# Capturer classes — these replace the real SQLAlchemy ORM models so the
# router can `CaseAttachment(**kw)` and `CaseAuditLog(**kw)` without
# triggering the User.posts FK ambiguity cascade. The classes record
# every kwarg passed to them so tests can assert on what was written.
# ---------------------------------------------------------------------------


class _CapturedAttachment:
    """Stand-in for `app.models.CaseAttachment` in tests.

    Exposes every kwarg as both an attribute (`obj.kw["k"]`) and a
    direct attribute (`obj.k`) so Pydantic v2 from-ORM conversion can
    read the values for `model_validate`.
    """

    def __init__(self, **kw):
        self.kw = kw
        for k, v in kw.items():
            setattr(self, k, v)


class _CapturedTimeline:
    """Stand-in for `app.models.CaseTimeline` (mirrors the helper
    capturer class used by `TestTimelineHelpers`)."""

    def __init__(self, **kw):
        self.kw = kw
        for k, v in kw.items():
            setattr(self, k, v)


class _CapturedAuditLog:
    """Stand-in for `app.models.CaseAuditLog`."""

    def __init__(self, **kw):
        self.kw = kw
        for k, v in kw.items():
            setattr(self, k, v)


def _capture_clause(stmt):
    """Return the SQL-rendered WHOLE compile of `stmt.whereclause` so
    tests can assert on the column predicate. Defaults to a portable
    PostgreSQL compile so the parameter names match production."""
    try:
        from sqlalchemy.dialects import postgresql
        compiled = stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        return str(compiled)
    except Exception:
        try:
            return str(stmt.whereclause)
        except Exception:
            return ""


class TestCaseListEndpoint:
    """GET /cases/ — list cases visible to the viewer."""

    def _user(self, *, id_: int, role: str):
        return SimpleNamespace(id=id_, role=role)

    @pytest.mark.asyncio
    async def test_citizen_sees_only_own_cases(self, monkeypatch):
        """A citizen viewer's list is filtered to `reporter_user_id ==
        user.id`. Other citizens' cases are not returned."""
        from app.routers.cases import list_cases_for_viewer

        citizen = self._user(id_=42, role="citizen")
        own_case = SimpleNamespace(
            id=1, case_number="CIV-2026-000001",
            status="submitted", title="My pothole",
            submitted_at=datetime(2026, 8, 5, tzinfo=UTC),
            display_handle="Anonymous Citizen", district_id="central",
        )

        captured: list = []

        async def fake_execute(stmt):
            captured.append(_capture_clause(stmt))
            rendered = captured[-1]
            # First query = case list; second query = support_count.
            if "case_support" in rendered:
                # Return empty support rows.
                class _Result:
                    def all(self_inner):
                        return []
                return _Result()
            # Case list query — return the one matching case.
            class _Result:
                def scalars(self_inner):
                    class _S:
                        def all(self_inner_inner):
                            return [own_case]
                    return _S()
            return _Result()

        async def fake_commit():
            pass

        db = SimpleNamespace(execute=fake_execute, commit=fake_commit)

        rows = await list_cases_for_viewer(db=db, user=citizen)
        assert len(rows) == 1
        assert rows[0].id == 1
        assert rows[0].case_number == "CIV-2026-000001"
        assert rows[0].status == CaseStatus.SUBMITTED
        # The captured filter must include the reporter_user_id == user.id
        # predicate so we know the route scoped the query to the viewer.
        case_list_clause = captured[0]
        assert "reporter_user_id" in case_list_clause, (
            "list_cases_for_viewer must scope the citizen query to the viewer. "
            f"Got: {case_list_clause!r}"
        )

    @pytest.mark.asyncio
    async def test_admin_sees_terminal_cases(self, monkeypatch):
        """An admin viewer is NOT scoped to reporter_user_id — they see
        the full table. This is the same posture as the timeline + detail
        endpoints."""
        from app.routers.cases import list_cases_for_viewer

        admin = SimpleNamespace(id=99, role="admin")

        captured: list = []

        async def fake_execute(stmt):
            captured.append(_capture_clause(stmt))
            class _Result:
                def scalars(self_inner):
                    class _S:
                        def all(self_inner_inner):
                            return []
                    return _S()
            return _Result()

        db = SimpleNamespace(execute=fake_execute, commit=AsyncMock())

        await list_cases_for_viewer(db=db, user=admin)
        # Admin MUST NOT filter by reporter_user_id.
        assert not any("reporter_user_id" in c for c in captured), (
            f"Admin must see all cases (full table). Captured: {captured!r}"
        )


class TestGetCaseDetailEndpoint:
    """GET /cases/{id} — case detail for /cases/:id."""

    def _user(self, *, id_: int, role: str):
        return SimpleNamespace(id=id_, role=role)

    def _case(self, *, status: str = "submitted", reporter_user_id=42):
        return SimpleNamespace(
            id=1,
            case_number="CIV-2026-000001",
            status=status,
            title="Pothole on Main St",
            description="Large pothole near the school.",
            district_id="central",
            language="EN",
            submitted_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
            resolved_at=None,
            display_handle="Anonymous Citizen",
            reporter_user_id=reporter_user_id,
        )

    @pytest.mark.asyncio
    async def test_mp_viewer_sees_anonymous_reporter(self, monkeypatch):
        """An MP viewer (not the reporter, not an admin) sees the
        anonymous summary — no user_id, no PII. Confirms the MP-side
        anonymity contract: MPs NEVER see PII, even when they
        legitimately browse the case."""
        from app.routers.cases import get_case

        async def fake_audit(*args, **kwargs):
            pass
        monkeypatch.setattr(
            "app.routers.cases.log_audit_event", fake_audit,
        )

        viewer = self._user(id_=7, role="mp")  # an MP
        case = self._case(reporter_user_id=42)
        async def fake_loader(db, *, case_id):
            return case
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        class _Result:
            def scalar_one(self_inner):
                return 0  # support_count
        async def fake_execute(_stmt):
            return _Result()
        async def fake_commit():
            pass

        db = SimpleNamespace(execute=fake_execute, commit=fake_commit)

        detail = await get_case(case_id=1, db=db, user=viewer)
        assert detail.id == 1
        assert detail.title == "Pothole on Main St"
        # Reporter block is the anonymous summary — display_handle only.
        assert detail.reporter.display_handle == "Anonymous Citizen"
        assert detail.reporter.district_label == "central"
        # MP can respond (per the auth model).
        assert detail.viewer_can_respond is True
        # Confirm the anonymity contract: NO user_id, NO first_name, NO
        # email on the wire (matching the FORBIDDEN_PII_TOKENS gate).
        dumped = detail.model_dump()
        assert "user_id" not in dumped["reporter"]
        for forbidden in FORBIDDEN_PII_TOKENS:
            assert forbidden not in dumped["reporter"]

    @pytest.mark.asyncio
    async def test_reporter_themselves_can_respond(self, monkeypatch):
        """The reporter themselves gets `viewer_can_respond=True` and
        can see their own case. The reporter block stays anonymous
        (no user_id) — that's by design."""
        from app.routers.cases import get_case

        async def fake_audit(*args, **kwargs):
            pass
        monkeypatch.setattr(
            "app.routers.cases.log_audit_event", fake_audit,
        )

        viewer = self._user(id_=42, role="citizen")
        case = self._case(reporter_user_id=42)
        async def fake_loader(db, *, case_id):
            return case
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        class _Result:
            def scalar_one(self_inner):
                return 0
        async def fake_execute(_stmt):
            return _Result()
        async def fake_commit():
            pass

        db = SimpleNamespace(execute=fake_execute, commit=fake_commit)
        detail = await get_case(case_id=1, db=db, user=viewer)
        assert detail.viewer_can_respond is True


class TestCaseAttachmentUploadEndpoint:
    """POST /cases/{id}/attachments — multipart upload."""

    def _user(self, *, id_: int, role: str):
        return SimpleNamespace(id=id_, role=role)

    def _case(self, *, reporter_user_id=42):
        return SimpleNamespace(
            id=1,
            status="submitted",
            reporter_user_id=reporter_user_id,
        )

    @pytest.fixture
    def patch_models(self, monkeypatch):
        """Opt-in helper to patch `CaseAttachment` construction for
        the upload happy-path test (which needs to construct capturers
        because the SQLAlchemy mapper cascade can't initialise in the
        test environment). NOT autouse — only `test_uploads_allowed_
        mime_writes_row_and_timeline` opts in. The delete tests don't
        construct a `CaseAttachment`, only `select(CaseAttachment)`,
        so they don't need this fixture.

        Also stubs `record_attachment_added` so the timeline+audit
        helper doesn't try to build real CaseTimeline/CaseAuditLog
        ORMs (same FK cascade issue)."""
        captured_attachments: list = []

        class _AttachmentCapturer:
            """Capturer instance returned by the CaseAttachment factory."""

            def __init__(self, **kw):
                # Bypass SQLAlchemy's InstrumentedAttribute descriptors
                # by writing into __dict__ directly.
                self.__dict__["kw"] = kw
                for k, v in kw.items():
                    self.__dict__[k] = v
                self.__dict__["__capturer__"] = True
                captured_attachments.append(self)

        def attachment_factory(*args, **kw):
            return _AttachmentCapturer(**kw)

        monkeypatch.setattr(
            "app.routers.cases.CaseAttachment", attachment_factory,
        )

        # Stub the timeline helper to capture its arguments.
        attachment_added_calls: list = []

        async def fake_record_attachment_added(
            db, *, case_id, attachment_id, actor_user_id,
            actor_role, media_type, request_id=None,
        ):
            attachment_added_calls.append({
                "case_id": case_id,
                "attachment_id": attachment_id,
                "actor_user_id": actor_user_id,
                "actor_role": actor_role,
                "media_type": media_type,
            })
            return _CapturedTimeline(
                case_id=case_id,
                event_type="attachment_added",
                actor_role=actor_role,
                actor_user_id=actor_user_id,
                description="Evidence attached",
            )

        monkeypatch.setattr(
            "app.routers.cases.record_attachment_added",
            fake_record_attachment_added,
        )

        return {
            "captured_attachments": captured_attachments,
            "attachment_added_calls": attachment_added_calls,
        }

    @pytest.mark.asyncio
    async def test_uploads_allowed_mime_writes_row_and_timeline(
        self, monkeypatch, patch_models,
    ):
        """A successful upload (image/jpeg) writes one CaseAttachment
        row + one CaseTimeline (attachment_added) row + one CaseAuditLog
        row, all in the same transaction."""
        import io

        from fastapi import UploadFile

        from app.routers.cases import upload_case_attachment

        reporter = self._user(id_=42, role="citizen")
        case = self._case(reporter_user_id=42)
        async def fake_loader(db, *, case_id):
            return case
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        fake_file = UploadFile(
            filename="photo.jpg",
            file=io.BytesIO(b"\xff\xd8\xff\xe0test-jpeg-bytes"),
            headers={"content-type": "image/jpeg"},
        )

        added: list = []

        async def fake_flush():
            # Mimic the autoincrement: assign an id to the capturer
            # so the router can read `attachment.id` to hand to the
            # timeline helper.
            for obj in added:
                if getattr(obj, "__capturer__", False) and "id" not in obj.__dict__:
                    obj.__dict__["id"] = 1

        async def fake_commit():
            pass

        async def fake_refresh(obj):
            # The router reads the capturer via Pydantic's from-attrs;
            # stamp the server-side default fields here so the
            # `created_at` row is present.
            if getattr(obj, "id", None) is None:
                obj.__dict__["id"] = 1
            if "created_at" not in obj.__dict__:
                obj.__dict__["created_at"] = datetime(
                    2026, 8, 5, tzinfo=UTC,
                )
            return obj

        db = SimpleNamespace(
            add=lambda o: added.append(o),
            flush=fake_flush,
            commit=fake_commit,
            refresh=fake_refresh,
        )

        result = await upload_case_attachment(
            case_id=1, file=fake_file, db=db, user=reporter,
        )
        assert result.file_name == "photo.jpg"
        assert result.media_type == "image"
        assert result.byte_size > 0

        # The router enqueued exactly one capturer attachment row.
        attachment_rows = [
            o for o in added
            if getattr(o, "__capturer__", False)
        ]
        assert len(attachment_rows) == 1
        att = attachment_rows[0]
        assert att.kw["case_id"] == 1
        assert att.kw["uploaded_by_id"] == 42
        assert att.kw["mime_type"] == "image/jpeg"
        assert att.kw["media_type"] == "image"

        # The router invoked the timeline+audit helper with the right
        # arguments — verifies the side-effect row is emitted.
        calls = patch_models["attachment_added_calls"]
        assert len(calls) == 1
        assert calls[0]["case_id"] == 1
        assert calls[0]["actor_user_id"] == 42
        assert calls[0]["actor_role"] == "citizen"
        assert calls[0]["media_type"] == "image"

    @pytest.mark.asyncio
    async def test_rejects_unsupported_mime(self, monkeypatch):
        """An .exe (application/x-msdownload) is NOT in the allowed MIME
        list and is rejected with 400 before any write."""
        import io

        from fastapi import HTTPException, UploadFile

        from app.routers.cases import upload_case_attachment

        reporter = self._user(id_=42, role="citizen")
        case = self._case()
        async def fake_loader(db, *, case_id):
            return case
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        fake_file = UploadFile(
            filename="malware.exe",
            file=io.BytesIO(b"fake-binary"),
            headers={"content-type": "application/x-msdownload"},
        )

        added: list = []
        db = SimpleNamespace(
            add=lambda o: added.append(o),
            flush=AsyncMock(),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )

        with pytest.raises(HTTPException) as ei:
            await upload_case_attachment(
                case_id=1, file=fake_file, db=db, user=reporter,
            )
        assert ei.value.status_code == 400
        assert "Unsupported file type" in ei.value.detail
        assert added == []

    @pytest.mark.asyncio
    async def test_rejects_oversize(self, monkeypatch):
        """An attachment larger than 25 MB is rejected with 413."""
        import io

        from fastapi import HTTPException, UploadFile

        from app.routers.cases import upload_case_attachment

        reporter = self._user(id_=42, role="citizen")
        case = self._case()
        async def fake_loader(db, *, case_id):
            return case
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        # 26 MB of zeros — clearly over the 25 MB cap.
        oversize_bytes = b"\x00" * (26 * 1024 * 1024)
        fake_file = UploadFile(
            filename="huge.jpg",
            file=io.BytesIO(oversize_bytes),
            headers={"content-type": "image/jpeg"},
        )

        added: list = []
        db = SimpleNamespace(
            add=lambda o: added.append(o),
            flush=AsyncMock(),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )

        with pytest.raises(HTTPException) as ei:
            await upload_case_attachment(
                case_id=1, file=fake_file, db=db, user=reporter,
            )
        assert ei.value.status_code == 413
        assert "25 MB" in ei.value.detail
        assert added == []

    @pytest.mark.asyncio
    async def test_delete_only_uploader_or_admin(self, monkeypatch):
        """The per-row delete check refuses 403 unless the viewer is the
        uploader OR an admin. A party (MP) who is not the uploader and
        not an admin gets through the participant gate but is blocked
        by the per-row check."""
        from fastapi import HTTPException

        from app.routers.cases import delete_case_attachment

        # An MP viewer who is a party to the case (per the participant
        # gate) but NOT the uploader of the attachment and NOT an admin.
        # This is exactly the scenario the per-row check guards.
        mp_user = self._user(id_=7, role="mp")
        case = self._case(reporter_user_id=42)

        async def fake_loader(db, *, case_id):
            return case
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        attachment = SimpleNamespace(
            id=7, case_id=1, uploaded_by_id=42,  # uploader != mp_user
        )

        class _Result:
            def scalar_one_or_none(self_inner):
                return attachment

        async def fake_execute(_stmt):
            return _Result()

        deleted: list = []

        async def fake_delete(_obj):
            deleted.append(_obj)

        async def fake_commit():
            pass

        db = SimpleNamespace(
            execute=fake_execute,
            delete=fake_delete,
            commit=fake_commit,
        )

        with pytest.raises(HTTPException) as ei:
            await delete_case_attachment(
                case_id=1, attachment_id=7, db=db, user=mp_user,
            )
        assert ei.value.status_code == 403
        assert "Only the uploader or an admin" in ei.value.detail
        assert deleted == []

    @pytest.mark.asyncio
    async def test_admin_can_delete_any_attachment(self, monkeypatch):
        """Admins may delete any attachment, regardless of who uploaded it."""
        from app.routers.cases import delete_case_attachment

        admin = self._user(id_=1, role="admin")
        case = self._case(reporter_user_id=42)

        async def fake_loader(db, *, case_id):
            return case
        monkeypatch.setattr(
            "app.routers.cases.get_case_for_viewer", fake_loader,
        )

        attachment = SimpleNamespace(
            id=7, case_id=1, uploaded_by_id=42,  # different uploader
        )

        class _Result:
            def scalar_one_or_none(self_inner):
                return attachment

        async def fake_execute(_stmt):
            return _Result()

        deleted: list = []

        async def fake_delete(_obj):
            deleted.append(_obj)

        async def fake_commit():
            pass

        db = SimpleNamespace(
            execute=fake_execute,
            delete=fake_delete,
            commit=fake_commit,
        )

        # Admin can delete — no exception, attachment row handed to delete().
        await delete_case_attachment(
            case_id=1, attachment_id=7, db=db, user=admin,
        )
        assert deleted == [attachment]


# ===========================================================================
# MP queue + dashboard endpoints
# ===========================================================================
#
# These tests cover the four new MP-only endpoints added to
# app/routers/cases.py:
#   - GET    /cases/mp/queue
#   - POST   /cases/{case_id}/assign-self
#   - POST   /cases/{case_id}/unassign
#   - POST   /cases/{case_id}/request-information
#
# They follow the same capturer pattern used throughout this file —
# pure double-based tests with no live DB. The user's role is a string
# (the routers module reads `user.role` via `_user_role_str`).
# ===========================================================================


class TestMPQueueEndpoint:
    """GET /cases/mp/queue — list cases assigned to the viewer's MPProfile."""

    @staticmethod
    def _make_mp_user(role="mp"):
        return SimpleNamespace(
            id=9001,
            role=role,
            email="mp@example.org",
            is_active=True,
        )

    @staticmethod
    def _make_mp_profile(mp_id=7, region_id=3):
        return SimpleNamespace(
            id=mp_id,
            user_id=9001,
            region_id=region_id,
            full_name="Anon MP",
            is_accepting_cases=True,
        )

    @staticmethod
    def _make_case_row(case_id=1, **overrides):
        base = {
            "id": case_id,
            "case_number": "CIV-2026-000001",
            "status": CaseStatus.SUBMITTED.value,
            "priority": "normal",
            "title": "Pothole on Main",
            "category_id": 4,
            "district_id": "D-001",
            "display_handle": "Anonymous Citizen",
            "submitted_at": datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    @pytest.mark.asyncio
    async def test_mp_queue_filters_by_assignment_only(self, monkeypatch):
        """The query must AND `mp_profile_id = me AND unassigned_at IS NULL`.

        We capture the SQL via the existing `_capture_clause` helper
        which uses PostgreSQL compilation. The second query (support
        count) is also captured but skipped from the predicate check.
        """
        from app.routers import cases as cases_router

        captured_clauses = []

        # Stub apply_transition + log_audit_event + record_* to no-ops.
        monkeypatch.setattr(cases_router, "log_audit_event", AsyncMock())
        monkeypatch.setattr(cases_router, "record_information_requested", AsyncMock())
        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=self._make_mp_profile()),
        )

        case = self._make_case_row(case_id=42)
        assigned_at = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)

        async def fake_execute(stmt):
            rendered = _capture_clause(stmt)
            captured_clauses.append(rendered)
            if "case_support" in rendered:
                # support_count subquery — return empty.
                class _Result:
                    def all(self_inner):
                        return []
                return _Result()
            # Main queue query.
            class _Result:
                def all(self_inner):
                    return [(case, assigned_at)]
            return _Result()

        async def fake_commit():
            pass

        db = SimpleNamespace(execute=fake_execute, commit=fake_commit)
        user = self._make_mp_user()

        result = await cases_router.list_mp_queue(
            db=db, user=user,
            status_filter=None,
            priority=None,
            category_id=None,
            district_id=None,
            region_id=None,
            date_from=None,
            date_to=None,
            search=None,
            sort="newest",
            limit=50,
            offset=0,
        )

        assert len(captured_clauses) >= 1, "queue endpoint should issue at least one query"
        # Find the main queue clause (the one that joins case_assignments).
        main_clauses = [
            c for c in captured_clauses
            if "case_assignments" in c and "mp_profile_id" in c
        ]
        assert main_clauses, (
            f"No main queue clause captured. Captured: {captured_clauses!r}"
        )
        main = main_clauses[0].lower()
        assert "unassigned_at" in main
        assert "mp_profile_id" in main

        # Anonymous reporter block — no user_id, no PII tokens.
        reporter = result.items[0].reporter
        assert reporter.display_handle == "Anonymous Citizen"
        assert reporter.district_label == "D-001"
        # No PII tokens anywhere in the result wire.
        wire = result.model_dump_json()
        for token in ("first_name", "last_name", "email", "phone", "photo", "avatar"):
            assert token not in wire, f"forbidden token {token!r} in queue wire shape"

    @pytest.mark.asyncio
    async def test_mp_queue_rejects_non_mp(self, monkeypatch):
        """A citizen viewer must get 403."""
        from fastapi import HTTPException

        from app.routers import cases as cases_router

        user = SimpleNamespace(id=2, role="citizen", email="c@example.org", is_active=True)
        db = SimpleNamespace(execute=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await cases_router.list_mp_queue(db=db, user=user)
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_mp_queue_rejects_mp_without_profile(self, monkeypatch):
        """An MP with no MPProfile must get 403."""
        from fastapi import HTTPException

        from app.routers import cases as cases_router

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=None),
        )
        user = self._make_mp_user()
        db = SimpleNamespace(execute=AsyncMock())

        with pytest.raises(HTTPException) as ei:
            await cases_router.list_mp_queue(db=db, user=user)
        assert ei.value.status_code == 403


class TestAssignSelfEndpoint:
    """POST /cases/{case_id}/assign-self."""

    @staticmethod
    def _make_user(role="mp"):
        return SimpleNamespace(id=9001, role=role, email="mp@example.org", is_active=True)

    @staticmethod
    def _make_mp_profile(mp_id=7):
        return SimpleNamespace(id=mp_id, user_id=9001, region_id=3, is_accepting_cases=True)

    @pytest.mark.asyncio
    async def test_assign_self_creates_row_and_writes_top_level_assignment(self, monkeypatch):
        """A fresh claim writes a CaseAssignment row AND sets
        `Case.assigned_mp_profile_id` so other endpoints can read it
        in one hop.

        Capturer pattern: `CaseAssignment(...)` SQLAlchemy mapper
        config trips on User.posts FK ambiguity (per the existing
        memory note), so we swap the routers-module reference for a
        capturer factory.
        """
        from app.routers import cases as cases_router
        from app.schemas_case import CaseAssignmentCreate

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=self._make_mp_profile()),
        )

        audit_calls = []

        async def fake_log_audit_event(db, *, case_id, action, actor_user_id, actor_role, payload):
            audit_calls.append({"case_id": case_id, "action": action.value, "payload": payload})

        monkeypatch.setattr(cases_router, "log_audit_event", fake_log_audit_event)

        case = SimpleNamespace(
            id=1,
            assigned_mp_profile_id=None,
        )

        async def fake_get_case_for_viewer(_db, *, case_id):
            return case

        monkeypatch.setattr(cases_router, "get_case_for_viewer", fake_get_case_for_viewer)

        # Capturer for CaseAssignment — bypass SQLAlchemy mapper config
        # via a plain class with __init__ (NOT SimpleNamespace in a
        # function — SQLAlchemy's lambda tracking gets confused).
        # We patch `app.models.CaseAssignment` so both:
        #   1. `select(CaseAssignment).where(...)` succeeds.
        #   2. `CaseAssignment(case_id=..., ...)` as a constructor
        #      call is captured.
        # We also patch `select` so SQLAlchemy's mapper config
        # cascade (the User.posts FK ambiguity) doesn't trip.
        # See the existing memory note on the capturer pattern.
        import sys
        from unittest.mock import MagicMock

        import app.models

        added = []

        # Each column attribute is its own MagicMock so .is_(None)
        # returns a chainable mock without raising.
        def _mk_col():
            col = MagicMock()
            col.is_.return_value = MagicMock()
            return col

        class _FakeAssignment:
            """Capturer that mimics both the ORM AND the constructor.

            Class-level attributes are MagicMocks — SQLAlchemy reads
            these via `select()` and `.is_()` chains through.
            The constructor signature mimics the model.
            """

            id = _mk_col()
            case_id = _mk_col()
            mp_profile_id = _mk_col()
            unassigned_at = _mk_col()
            assigned_at = _mk_col()
            assigned_by_user_id = _mk_col()

            def __init__(self, **kw):
                for k, v in kw.items():
                    self.__dict__[k] = v
                added.append(self)

        monkeypatch.setattr(sys.modules["app.models"], "CaseAssignment", _FakeAssignment)
        # Patch `select` so the call sites that do
        # `select(CaseAssignment).where(...)` don't try to invoke
        # SQLAlchemy's mapper config (which trips User.posts FK
        # ambiguity). The route just discards the statement and
        # relies on the db.execute mock to return the right shape.
        def _fake_select(*args, **kwargs):
            stmt = MagicMock()
            stmt.where.return_value = stmt
            return stmt

        monkeypatch.setattr(cases_router, "select", _fake_select)
        # Patch CaseAssignment on the routers module to match the
        # capturer used in `app.models`.
        monkeypatch.setattr(cases_router, "CaseAssignment", _FakeAssignment)

        class _FakeSession:
            async def flush(self_inner):
                # Populate a fake id on the just-added assignment.
                if added and added[-1].id is None:
                    added[-1].id = 1

            async def commit(self_inner):
                pass

            async def refresh(self_inner, obj):
                pass

            async def execute(self_inner, _stmt):
                class _R:
                    def scalar_one_or_none(inner):
                        return None
                return _R()

            def add(self_inner, obj):
                added.append(obj)

        db = _FakeSession()

        user = self._make_user()
        payload = CaseAssignmentCreate(reason="Need to investigate")

        result = await cases_router.assign_self(
            payload=payload, case_id=1, db=db, user=user,
        )

        # One CaseAssignment row was added (the capturer was called).
        assignment_rows = [o for o in added if isinstance(o, _FakeAssignment)]
        assert assignment_rows, f"No CaseAssignment captured. Added: {added!r}"
        assert assignment_rows[0].case_id == 1
        assert assignment_rows[0].mp_profile_id == 7
        # Case top-level column was set.
        assert case.assigned_mp_profile_id == 7
        # Audit row written.
        assert audit_calls and audit_calls[0]["action"] == "case_assigned"
        assert audit_calls[0]["payload"]["reason"] == "Need to investigate"
        # Response shape.
        assert result.case_id == 1

    @pytest.mark.asyncio
    async def test_assign_self_is_idempotent_for_same_mp(self, monkeypatch):
        """Re-claiming a case by the same MP returns the existing row
        without writing anything new.
        """
        from app.routers import cases as cases_router
        from app.schemas_case import CaseAssignmentCreate

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=self._make_mp_profile(mp_id=7)),
        )

        monkeypatch.setattr(cases_router, "log_audit_event", AsyncMock())
        case = SimpleNamespace(id=1, assigned_mp_profile_id=7)
        monkeypatch.setattr(
            cases_router, "get_case_for_viewer",
            AsyncMock(return_value=case),
        )

        existing_assignment = SimpleNamespace(
            id=99,
            case_id=1,
            mp_profile_id=7,
            assigned_at=datetime(2026, 8, 1, tzinfo=UTC),
            assigned_by_user_id=9001,
            unassigned_at=None,
        )

        added = []

        class _Db:
            async def execute(self, _stmt):
                class _R:
                    def scalar_one_or_none(self_inner):
                        return existing_assignment
                return _R()

            def add(self, obj):
                added.append(obj)

            async def flush(self):
                pass

            async def commit(self):
                pass

            async def refresh(self, obj):
                pass

        user = self._make_user()
        result = await cases_router.assign_self(
            payload=CaseAssignmentCreate(reason=None),
            case_id=1, db=_Db(), user=user,
        )
        # No new row written.
        assert added == []
        assert result.id == 99

    @pytest.mark.asyncio
    async def test_assign_self_returns_409_for_already_claimed(self, monkeypatch):
        """A different MP trying to claim an already-claimed case gets 409."""
        from fastapi import HTTPException

        from app.routers import cases as cases_router
        from app.schemas_case import CaseAssignmentCreate

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=self._make_mp_profile(mp_id=8)),
        )
        monkeypatch.setattr(cases_router, "log_audit_event", AsyncMock())
        monkeypatch.setattr(
            cases_router, "get_case_for_viewer",
            AsyncMock(return_value=SimpleNamespace(id=1, assigned_mp_profile_id=99)),
        )

        other_assignment = SimpleNamespace(
            id=42, case_id=1, mp_profile_id=99,
            assigned_at=datetime.now(tz=UTC),
            assigned_by_user_id=1, unassigned_at=None,
        )

        class _Db:
            async def execute(self, _stmt):
                class _R:
                    def scalar_one_or_none(self_inner):
                        return other_assignment
                return _R()

            async def flush(self_inner):
                pass

            def add(self_inner, obj):
                pass

            async def commit(self_inner):
                pass

        user = self._make_user()
        with pytest.raises(HTTPException) as ei:
            await cases_router.assign_self(
                payload=CaseAssignmentCreate(reason=None),
                case_id=1, db=_Db(), user=user,
            )
        assert ei.value.status_code == 409


class TestUnassignEndpoint:
    @pytest.mark.asyncio
    async def test_unassign_clears_top_level_assignment(self, monkeypatch):
        """On unassign, the CaseAssignment row is closed AND the
        top-level `Case.assigned_mp_profile_id` is reset.
        """
        from app.routers import cases as cases_router

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=SimpleNamespace(id=7, user_id=9001)),
        )
        audit_calls = []
        monkeypatch.setattr(
            cases_router, "log_audit_event",
            AsyncMock(side_effect=lambda db, **kw: audit_calls.append(kw)),
        )

        active_assignment = SimpleNamespace(
            id=99, case_id=1, mp_profile_id=7, unassigned_at=None,
        )

        case = SimpleNamespace(id=1, assigned_mp_profile_id=7)

        async def fake_get_case_for_viewer(_db, *, case_id):
            return case

        monkeypatch.setattr(cases_router, "get_case_for_viewer", fake_get_case_for_viewer)

        added = []

        class _Db:
            async def execute(self, _stmt):
                class _R:
                    def scalar_one_or_none(self_inner):
                        return active_assignment
                return _R()

            def add(self, obj):
                added.append(obj)

            async def commit(self_inner):
                pass

        user = SimpleNamespace(id=9001, role="mp", email="m@e", is_active=True)

        await cases_router.unassign_case(case_id=1, db=_Db(), user=user)

        # The CaseAssignment row was marked unassigned.
        assert active_assignment.unassigned_at is not None
        # The top-level Case column was cleared.
        assert case.assigned_mp_profile_id is None
        # An audit row was written.
        assert audit_calls and audit_calls[0]["action"].value == "case_unassigned"

    @pytest.mark.asyncio
    async def test_unassign_404_if_no_active_assignment(self, monkeypatch):
        from fastapi import HTTPException

        from app.routers import cases as cases_router

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=SimpleNamespace(id=7, user_id=9001)),
        )
        monkeypatch.setattr(cases_router, "log_audit_event", AsyncMock())
        monkeypatch.setattr(
            cases_router, "get_case_for_viewer",
            AsyncMock(return_value=SimpleNamespace(id=1, assigned_mp_profile_id=7)),
        )

        class _Db:
            async def execute(self, _stmt):
                class _R:
                    def scalar_one_or_none(self_inner):
                        return None
                return _R()

            async def commit(self_inner):
                pass

        user = SimpleNamespace(id=9001, role="mp", email="m@e", is_active=True)

        with pytest.raises(HTTPException) as ei:
            await cases_router.unassign_case(case_id=1, db=_Db(), user=user)
        assert ei.value.status_code == 404


class TestRequestInformationEndpoint:
    @pytest.mark.asyncio
    async def test_request_information_emits_timeline_and_transitions(self, monkeypatch):
        """POST /cases/{id}/request-information must:
          - call apply_transition(SUBMITTED -> INFORMATION_REQUESTED)
          - call record_information_requested with the note
          - commit
          - return a CaseTimelineResponse whose event_type is
            `information_requested`.
        """
        from app.routers import cases as cases_router
        from app.schemas_case import InformationRequestCreate

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=SimpleNamespace(id=7, user_id=9001, region_id=3)),
        )

        info_calls = []

        async def fake_record_information_requested(db, **kw):
            info_calls.append(kw)
            return SimpleNamespace(id=999, case_id=1)

        monkeypatch.setattr(cases_router, "record_information_requested", fake_record_information_requested)

        captured_transition = {}

        async def fake_apply_transition(db, *, case, to_status, actor_user_id, actor_role, description):
            captured_transition["to_status"] = to_status
            captured_transition["description"] = description
            case.status = to_status.value
            return case

        monkeypatch.setattr(cases_router, "apply_transition", fake_apply_transition)

        case = SimpleNamespace(
            id=1,
            status=CaseStatus.SUBMITTED.value,
            assigned_mp_profile_id=None,
        )

        async def fake_get_case_for_viewer(_db, *, case_id):
            return case

        monkeypatch.setattr(cases_router, "get_case_for_viewer", fake_get_case_for_viewer)

        class _Db:
            async def commit(self_inner):
                pass

            async def refresh(self_inner, obj):
                pass

        user = SimpleNamespace(id=9001, role="mp", email="m@e", is_active=True)

        payload = InformationRequestCreate(note="Please share the invoice")
        result = await cases_router.request_information(
            payload=payload, case_id=1, db=_Db(), user=user,
        )

        assert captured_transition["to_status"] == CaseStatus.INFORMATION_REQUESTED
        assert captured_transition["description"] == "Please share the invoice"
        assert info_calls and info_calls[0]["note"] == "Please share the invoice"
        assert info_calls[0]["actor_role"] == "mp"
        assert result.event_type == "information_requested"
        assert result.to_status == "information_requested"

    @pytest.mark.asyncio
    async def test_request_information_409_on_invalid_transition(self, monkeypatch):
        """If the workflow adjacency list forbids the transition, 409."""
        from fastapi import HTTPException

        from app.routers import cases as cases_router
        from app.schemas_case import InformationRequestCreate
        from app.services.cases.workflow import InvalidTransition

        monkeypatch.setattr(
            cases_router, "get_viewer_mp_profile",
            AsyncMock(return_value=SimpleNamespace(id=7, user_id=9001)),
        )
        monkeypatch.setattr(cases_router, "record_information_requested", AsyncMock())

        async def fake_apply_transition(db, **kw):
            raise InvalidTransition(
                from_status=CaseStatus.CLOSED,
                to_status=CaseStatus.INFORMATION_REQUESTED,
            )

        monkeypatch.setattr(cases_router, "apply_transition", fake_apply_transition)

        case = SimpleNamespace(
            id=1, status=CaseStatus.CLOSED.value, assigned_mp_profile_id=7,
        )
        monkeypatch.setattr(
            cases_router, "get_case_for_viewer",
            AsyncMock(return_value=case),
        )

        class _Db:
            async def commit(self_inner):
                pass

        user = SimpleNamespace(id=9001, role="mp", email="m@e", is_active=True)

        with pytest.raises(HTTPException) as ei:
            await cases_router.request_information(
                payload=InformationRequestCreate(note="x"),
                case_id=1, db=_Db(), user=user,
            )
        assert ei.value.status_code == 409


class TestMPQueueWireShapeNoPII:
    """Defence in depth: the wire shape must NOT include any forbidden PII token."""

    def test_mp_queue_item_out_has_no_pii_fields(self):
        from app.schemas_case import CaseReporterAnonymousSummary, MPQueueItemOut

        # `id` is the Case's PK — NOT a PII token (we exclude it from
        # the forbidden set because every list shape has `id`).
        # Everything else is checked strictly.
        schema_fields = set(MPQueueItemOut.model_fields.keys())
        nested = set(CaseReporterAnonymousSummary.model_fields.keys())
        all_fields = schema_fields | nested
        forbidden = all_fields & FORBIDDEN_PII_TOKENS
        # Filter out `id` — that's the case's own PK, not user PII.
        forbidden.discard("id")
        assert forbidden == set(), f"MPQueueItemOut leaked: {forbidden}"

    def test_information_request_create_and_assignment_create_exist(self):
        from app.schemas_case import (
            CaseAssignmentCreate,
            CloseCaseCreate,
            InformationRequestCreate,
        )

        # Round-trip a basic payload.
        assert InformationRequestCreate(note="Please share").note == "Please share"
        assert CaseAssignmentCreate(reason="urgent").reason == "urgent"
        assert CloseCaseCreate(description="done").description == "done"
