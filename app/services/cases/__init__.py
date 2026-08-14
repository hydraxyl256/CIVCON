"""Case-domain service layer.

Modules in this package implement the case-management business logic
that the future case routers will call. They are deliberately small,
focused, and unit-testable in isolation.

- numbers.py    : race-safe CIV-YYYY-NNNNNN case-number generation
- workflow.py   : 9-step status state machine + apply_transition()
- anonymity.py  : build_reporter_view() — the ONLY function allowed
                  to produce the MP-visible reporter block
- audit.py      : log_audit_event() — write to case_audit_log with
                  the current X-Request-Id automatically attached
- routing.py    : intelligent MP routing engine — given a Category +
                  Location, return ranked MPProfile suggestions
                  (the citizen NEVER picks an MP directly)
"""
