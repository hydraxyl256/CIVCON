"""
Shared database helpers used by the router layer.

The functions here exist to collapse N+1 patterns into a single
batched query. They are intentionally tiny and only know about
SQLAlchemy 2.x async + the dialect-neutral constructs already used
elsewhere in the project.

Nothing here changes endpoint behaviour — the JSON shape and status
codes of every caller are identical to the unrolled version.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Column, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def batched_counts(
    db: AsyncSession,
    *,
    model,
    fk_col: Column,
    ids: Iterable[int],
    distinct: bool = False,
) -> dict[int, int]:
    """Return ``{fk_value: count}`` for the given parent ids.

    Replaces the common ``for row in rows: scalar(count(fk == row.id))``
    pattern with a single ``GROUP BY fk_col`` query. Empty ids return
    an empty dict without hitting the database.

    Args:
        db: Active async session.
        model: The child model whose rows we are counting. Only used
            so the FROM clause is explicit; in practice the count
            expression is built directly from ``fk_col``.
        fk_col: The foreign-key column on the child model that points
            at the parent ids.
        ids: Iterable of parent ids to count rows for.
        distinct: If ``True``, count distinct (model.pk). Otherwise
            count rows.

    Returns:
        Mapping of ``fk_value -> int``. Ids with no rows are absent
        from the dict — callers should default missing keys to 0.
    """
    id_list = [int(i) for i in ids]
    if not id_list:
        return {}

    if distinct:
        count_expr = func.count(func.distinct(model.__table__.primary_key.columns[0]))
    else:
        count_expr = func.count()

    stmt = (
        select(fk_col, count_expr.label("c"))
        .where(fk_col.in_(id_list))
        .group_by(fk_col)
    )
    result = await db.execute(stmt)
    return {row[0]: int(row[1]) for row in result.all()}