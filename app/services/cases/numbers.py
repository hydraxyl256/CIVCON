"""Race-safe case-number generation.

Case numbers follow the format `CIV-YYYY-NNNNNN` (e.g. `CIV-2026-000001`).
The numeric suffix is produced by a Postgres SEQUENCE
(`case_number_seq`) installed by migration c2b3c4d5e6f7. Two concurrent
inserts cannot collide because `nextval()` is atomic at the engine
level.

The year prefix is captured at the moment of generation. The sequence
itself does NOT reset between years (a global monotonic counter is the
correct invariant for a sequential case number — restarting at zero
each year would risk confusion with archived cases).

If the database is ever migrated from Postgres to a backend without
sequences, this module is the single point of change.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def next_case_number(db: AsyncSession) -> str:
    """Generate the next case number as `CIV-YYYY-NNNNNN`.

    Uses `nextval('case_number_seq')` so concurrent inserts cannot
    collide. The caller is responsible for committing the transaction;
    if the surrounding transaction rolls back, the sequence value is
    NOT returned (sequences are not transactional in Postgres), so a
    small gap may appear in the issued numbers. This is acceptable
    per spec STEP 3 — numbers must be unique, not gap-free.
    """
    seq_value = await db.scalar(text("SELECT nextval('case_number_seq')"))
    if seq_value is None:
        # nextval() should never return NULL on a normal SEQUENCE, but
        # guard against a misconfigured DB.
        raise RuntimeError(
            "case_number_seq returned NULL — is the sequence installed? "
            "Run alembic upgrade head."
        )
    year = datetime.now(tz=UTC).year
    return f"CIV-{year}-{int(seq_value):06d}"
