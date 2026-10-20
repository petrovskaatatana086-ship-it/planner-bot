"""Short SQLite write transactions. No external I/O is allowed in the callback."""
import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

T = TypeVar("T")


class DatabaseBusy(Exception):
    code = "DATABASE_BUSY"


def is_sqlite_busy(error: OperationalError) -> bool:
    # SQLite extended result codes have the primary code in the low byte.
    code = getattr(error.orig, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF == 5


async def run_sqlite_write(
    sessions: async_sessionmaker[AsyncSession],
    operation: Callable[[AsyncSession], Awaitable[T]],
    *,
    retries: int = 2,
) -> T:
    if retries < 0:
        raise ValueError("retries must be non-negative")
    for attempt in range(retries + 1):
        try:
            async with sessions() as session:
                if session.get_bind().dialect.name != "sqlite":
                    raise ValueError("This transaction runner requires SQLite")
                try:
                    # This must be the first SQL on a fresh session, before reads.
                    await session.execute(text("BEGIN IMMEDIATE"))
                    result = await operation(session)
                    await session.commit()
                    return result
                except BaseException:
                    await session.rollback()
                    raise
        except OperationalError as error:
            if not is_sqlite_busy(error):
                raise
            if attempt == retries:
                raise DatabaseBusy() from error
            await asyncio.sleep(0.02 * (attempt + 1))
    raise AssertionError("unreachable")
