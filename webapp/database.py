import os

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


WEBAPP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEBAPP_DIR.parent

PRODUCTION_DATABASE_PATH = WEBAPP_DIR / "app.db"


def _resolve_database_path():
    """
    Work out which SQLite file to open.

    Three sources, in order of precedence:

      1. MLS_DATABASE_PATH -- lets tests point at a throwaway file.
         Anything that DROPS tables must also call
         assert_not_production_db(); reverting this override once
         left a test pointed at the real app.db.

      2. DATABASE_URL -- the deployment setting, as documented in
         .env. Only sqlite:// URLs are understood; a relative path
         is taken relative to the project root rather than the
         current working directory, so it means the same thing no
         matter where the process was started from.

      3. The historical default, webapp/app.db.
    """

    override = os.environ.get("MLS_DATABASE_PATH")

    if override:
        return Path(override)

    url = (os.environ.get("DATABASE_URL") or "").strip()

    if url:

        if not url.startswith("sqlite:"):
            raise RuntimeError(
                "Only sqlite:// DATABASE_URLs are supported; got "
                f"{url!r}. Leave it unset to use the default at "
                f"{PRODUCTION_DATABASE_PATH}."
            )

        # sqlite:///relative/path or sqlite:////absolute/path
        raw = url.split("://", 1)[1].lstrip("/")

        candidate = Path(raw)

        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate

        return candidate

    return PRODUCTION_DATABASE_PATH


DATABASE_PATH = _resolve_database_path()

DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"


engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,

        # Wait for a competing writer instead of failing outright
        # with "database is locked". Grading runs in a background
        # thread, so writes genuinely do overlap.
        "timeout": 30,
    },
    pool_pre_ping=True,
)


# Journal mode for SQLite.
#
# WAL is the right choice on a normal disk: the default rollback
# journal makes readers and writers block each other, so one student
# submitting at a deadline stalls everyone loading a page. WAL lets
# reads continue during a write.
#
# It cannot be used on a network filesystem. WAL coordinates readers
# and writers through a shared-memory (-shm) file, which needs mmap
# semantics that NFS and similar do not provide -- SQLite reports
# "disk I/O error" on the first query rather than at PRAGMA time.
# Shared hosts commonly put home directories on exactly that kind of
# storage, so those deployments must set:
#
#     MLS_SQLITE_JOURNAL_MODE=DELETE
#
# The cost is that writes briefly block reads. With one teacher and a
# few hundred students that is unnoticeable outside a deadline rush.
SQLITE_JOURNAL_MODE = (
    os.environ.get("MLS_SQLITE_JOURNAL_MODE") or "WAL"
).strip().upper()


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record):
    """
    Apply the connection pragmas to every new SQLite connection.
    """

    cursor = dbapi_connection.cursor()

    try:
        cursor.execute(
            f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}"
        )
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class Base(DeclarativeBase):
    pass


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def assert_not_production_db():
    """
    Refuse to continue when the engine is pointed at the real database.

    Destructive test setup must call this first. It is cheap insurance
    against an import-order or environment mistake wiping real work.
    """

    # The only safe case is an explicit throwaway override. Checking
    # solely against webapp/app.db was not enough once DATABASE_URL
    # could move the real database somewhere else -- a test would
    # then have sailed past this guard straight into live data.
    if not os.environ.get("MLS_DATABASE_PATH"):
        raise RuntimeError(
            "Refusing to run: this process is connected to the "
            f"configured database at {DATABASE_PATH}. Set "
            "MLS_DATABASE_PATH to a throwaway file before importing "
            "webapp.database."
        )

    if DATABASE_PATH.resolve() == PRODUCTION_DATABASE_PATH.resolve():
        raise RuntimeError(
            "Refusing to run: MLS_DATABASE_PATH points at the real "
            f"database at {DATABASE_PATH}."
        )
