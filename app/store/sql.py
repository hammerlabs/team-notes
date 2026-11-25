"""The storage operations, written once in SQLAlchemy for both databases.

Almost everything is the same SQL on SQLite and Postgres. The four things
that are not are hooks a subclass must answer (`sqlite.py`, `postgres.py`):
how to build the engine, how to fold case in search, how to make a
count-based guard atomic, and which errors mean "busy".

Conditional writes, judged by their outcome
-------------------------------------------
Every write with a rule attached is one conditional statement whose WHERE
clause holds the whole rule. The lock is the one write that takes two: a
renewal attempt, then the take (or, for a renew-only call, the idle release),
two conditional UPDATEs in one transaction, each holding its whole rule (two
so the caller learns which happened). "Live" means the lease has not run out
and the holder was active within the idle limit (`models.lock_is_live`):

    renew the lock    UPDATE notes SET lock_expires_at [, lock_active_at] WHERE id = ? AND lock is mine and live
     else take it     UPDATE notes SET lock... WHERE id = ? AND (unlocked OR expired OR idle OR mine)
     or release idle  UPDATE notes SET lock... = NULL WHERE id = ? AND lock is mine, lease running, holder idle
    save              UPDATE notes SET ... WHERE id = ? AND lock is mine and live AND version = ?
    delete            DELETE FROM notes WHERE id = ? AND (not locked by another) AND version = ? [AND author = me]
    add a member      INSERT ... SELECT ... WHERE (SELECT count(*) ...) < cap
    add a note        INSERT ... SELECT ... WHERE (SELECT count(*) ...) < cap
    remove a member   DELETE ... WHERE role <> 'admin' OR (SELECT count(*) of admins) > 1

There is no "check, then write": nothing can change between the two because
they are one step. The database reports how many rows the statement changed,
and that count is the decision. One row: done. Zero rows: refused, and only
then is the note read, to say why (`_write_or_refuse`). UPDATE and DELETE
report it as the cursor's rowcount, on both databases. The two INSERT ...
SELECTs report it with RETURNING instead: DB-API lets rowcount be -1
("unknown") for them, and through SQLAlchemy's psycopg dialect it is, which
would read as "inserted" when the cap refused the row.

Why each kind is atomic differs between the two databases, which is why the
three count-based guards (member cap, note cap, last admin) have a hook:

- Single-row updates and deletes (lock, save, delete) are atomic on both.
  SQLite runs one writer at a time. Postgres, at READ COMMITTED, makes a
  second writer of the same row wait for the first to commit and then
  re-evaluates the WHERE clause against the row as committed, so the second
  sees the first's lock or version and changes nothing.
- A count in a subquery is a read of *other* rows. SQLite is still safe:
  the statement runs while holding the single write lock, so no other write
  can land between its count and its insert. Postgres is not: two
  transactions each count from their own snapshot, neither sees the other's
  uncommitted row, and both pass. `_lock_team` closes that gap on Postgres.

The connection rule
-------------------
This is the one full explanation; other places point here.

Each operation opens its own session and closes it before returning, so a
connection is held only while a worker thread runs code that uses it. A
FastAPI request makes several thread-pool hops (each sync dependency, and the
handler, is a separate trip to a worker thread), and a session that outlived
its hop would hold a connection while queued for the next thread. With a
capped pool that is hold-and-wait over two resources: every thread waiting for
a connection that only a queued request can give back. Given the rule, at most
one connection is checked out per busy thread, so a pool as large as the
thread pool (`config.WORKER_THREADS`, 40) never makes a thread wait. The pool
is capped there (`max_overflow=0`) and never opens a connection beyond it,
however many requests or long polls are in flight. Each engine is also safe to
use from any worker thread for the same reason: a connection is used by one
thread at a time.
"""

from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import ColumnElement, Engine, delete, false, func, insert, literal, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, aliased, defer, joinedload, load_only, sessionmaker
from sqlalchemy.sql.base import ExecutableOption

from app import config
from app.models import (
    AuthSession,
    Base,
    Membership,
    Note,
    Team,
    User,
    UTCDateTime,
    lock_available_to,
    lock_held_by,
    lock_is_idle,
    lock_is_live,
    new_id,
    note_in_team,
)
from app.store.interface import (
    AlreadyMember,
    Busy,
    EmailTaken,
    LastAdmin,
    LockHeldByAnother,
    LockOutcome,
    LockRequired,
    NotAuthor,
    NoteLocked,
    NotePage,
    NotePosition,
    NotFound,
    Refusal,
    Store,
    TeamFull,
    TeamNotesFull,
    UserNotFound,
    VersionConflict,
)

_NEW_NOTE_COLUMNS = ("id", "team_id", "author_id", "title", "body", "version", "created_at", "updated_at")
"""The columns a new note is inserted with, in this order (`create_note_if_room`'s INSERT ... SELECT)."""

_CHANGE_COLUMNS = load_only(Note.version, Note.lock_user_id, Note.lock_expires_at, Note.lock_active_at, raiseload=True)
"""Load option for the long poll's re-read on every wake: only what a Change shows, never the body (up to 100 KB).

`raiseload`: touching any other column is a bug that raises, not a silent extra query.
"""


def with_lock_holder() -> ExecutableOption:
    """Load option: the lock holder's id and name, in the same query (no email or password hash)."""
    return joinedload(Note.lock_holder).load_only(User.id, User.name)


def version_is(version: int | None) -> ColumnElement[bool]:
    """SQL condition: the note is at `version`. None (no usable version was sent) matches no row.

    A missing or malformed precondition still runs the conditional write, which
    then changes nothing, so the refusal that follows reports the lock and
    ownership rules before the version, in one code path.
    """
    return false() if version is None else Note.version == version


class SqlStore(Store):
    """The shared SQLAlchemy implementation. Subclasses answer the four hooks below.

    Each operation's contract is `Store`'s (shown here too); the comments in its source say how this implementation
    keeps it.
    """

    def __init__(self, url: str, pool_size: int) -> None:
        self.engine = self._make_engine(url, pool_size)
        # Objects stay readable after commit: every operation returns its
        # results after its session has closed, so nothing could refresh
        # them later. Reads that must see fresh state say so (`_load_note`).
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    # Hooks: what differs between databases

    @abstractmethod
    def _make_engine(self, url: str, pool_size: int) -> Engine:
        """An engine with at most `pool_size` connections and no overflow."""

    @abstractmethod
    def _contains_folded(self, column: ColumnElement[str], text: str) -> ColumnElement[bool]:
        """SQL condition: `text` occurs in `column`, ignoring case, with `%` and `_` literal."""

    @abstractmethod
    def _lock_team(self, db: Session, team_id: str) -> None:
        """Make the rest of this transaction's count-based guard on `team_id` atomic.

        Called inside the transaction, as its own statement, before the
        statement that counts. See the module docstring for why SQLite needs
        nothing here and Postgres does.
        """

    @abstractmethod
    def _is_busy(self, error: OperationalError) -> bool:
        """True if `error` means "gave up waiting for another writer": nothing written, retry is safe."""

    # Plumbing

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """One short session; a busy database surfaces as `Busy`, other errors as they are."""
        try:
            with self._sessionmaker() as db:
                yield db
        except PoolTimeoutError as error:
            # No free connection within POOL_TIMEOUT_SECONDS. Cannot happen
            # under the connection rule (module docstring); if it ever does,
            # it is overload, and "busy" is honest.
            raise Busy() from error
        except OperationalError as error:
            if self._is_busy(error):
                raise Busy() from error
            raise

    def _load_note(self, db: Session, team_id: str, note_id: str, *options: ExecutableOption) -> Note:
        """A note of `team_id` with its lock holder, or `NotFound`.

        populate_existing: after a refused write this may be the second read
        of the same note in one session, and it must show the row as it is
        now, not as the session first saw it.
        """
        note = db.scalar(
            select(Note)
            .where(note_in_team(team_id, note_id))
            .options(with_lock_holder(), *options)
            .execution_options(populate_existing=True)
        )
        if note is None:
            raise NotFound("Note")
        return note

    def _note_for_refusal(self, db: Session, team_id: str, note_id: str) -> Note:
        """The read that words a refused write's error. Its own method so tests can step in just before it."""
        return self._load_note(db, team_id, note_id)

    # Lifecycle

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    # Accounts and sessions

    def create_user(self, email: str, name: str, password_hash: str, now: datetime) -> User:
        with self._session() as db:
            user = User(email=email, name=name, password_hash=password_hash, created_at=now)
            db.add(user)
            # No "is the email free?" pre-check: two signups could both pass
            # it. The UNIQUE constraint decides atomically, on both databases.
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise EmailTaken() from None
            return user

    def user_by_email(self, email: str) -> User | None:
        with self._session() as db:
            return db.scalar(select(User).where(User.email == email))

    def has_users(self) -> bool:
        with self._session() as db:
            return db.scalar(select(User.id).limit(1)) is not None

    def create_session(self, token_hash: str, user_id: str, expires_at: datetime) -> None:
        with self._session() as db:
            db.add(AuthSession(token_hash=token_hash, user_id=user_id, expires_at=expires_at))
            db.commit()

    def user_for_token(self, token_hash: str, now: datetime) -> User | None:
        with self._session() as db:
            return db.scalar(
                select(User)
                .join(AuthSession, AuthSession.user_id == User.id)
                .where(AuthSession.token_hash == token_hash, AuthSession.expires_at > now)
            )

    # Teams and membership

    def create_team(self, name: str, admin_id: str, now: datetime) -> Team:
        with self._session() as db:
            team = Team(name=name, created_at=now)
            db.add(team)
            db.flush()
            # Same transaction as the team: a team without an admin could never be reached.
            db.add(Membership(team_id=team.id, user_id=admin_id, role="admin", created_at=now))
            db.commit()
            return team

    def teams_of(self, user_id: str) -> list[tuple[Team, str]]:
        with self._session() as db:
            rows = db.execute(
                select(Team, Membership.role)
                .join(Membership, Membership.team_id == Team.id)
                .where(Membership.user_id == user_id)
                .order_by(Team.created_at, Team.id)
            )
            return [(team, role) for team, role in rows]

    def team(self, team_id: str) -> Team | None:
        with self._session() as db:
            return db.get(Team, team_id)

    def membership(self, team_id: str, user_id: str) -> Membership | None:
        with self._session() as db:
            return db.get(Membership, (team_id, user_id))

    def members(self, team_id: str) -> list[tuple[Membership, User]]:
        with self._session() as db:
            rows = db.execute(
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .where(Membership.team_id == team_id)
                .order_by(Membership.created_at, User.email)
            )
            return [(membership, user) for membership, user in rows]

    def add_member_if_room(self, team_id: str, email: str, role: str, now: datetime) -> User:
        with self._session() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                raise UserNotFound()
            # The primary key refuses a duplicate; the WHERE refuses a full team.
            existing = aliased(Membership)
            member_count = select(func.count()).where(existing.team_id == team_id).scalar_subquery()
            source = select(literal(team_id), literal(user.id), literal(role), literal(now, UTCDateTime())).where(
                member_count < config.MAX_TEAM_MEMBERS
            )
            statement = (
                insert(Membership)
                .from_select(["team_id", "user_id", "role", "created_at"], source)
                .returning(Membership.user_id)
            )
            try:
                self._lock_team(db, team_id)
                inserted = db.scalars(statement).first() is not None
                db.commit()
            except IntegrityError:
                db.rollback()
                raise AlreadyMember() from None
            if not inserted:
                # The team was full. Read once only to word the error precisely.
                if db.get(Membership, (team_id, user.id)) is not None:
                    raise AlreadyMember()
                raise TeamFull()
            return user

    def remove_member_keeping_an_admin(self, team_id: str, user_id: str) -> list[str]:
        # The admin count is inside the DELETE's WHERE, so the check and the
        # act are one step. The alias stops SQLAlchemy correlating the
        # subquery with the row being deleted, which would count only that row.
        admins = aliased(Membership)
        admin_count = select(func.count()).where(admins.team_id == team_id, admins.role == "admin").scalar_subquery()
        remove = (
            delete(Membership)
            .where(Membership.team_id == team_id, Membership.user_id == user_id)
            .where((Membership.role != "admin") | (admin_count > 1))
            .execution_options(synchronize_session=False)
        )
        # Their locks go in the same transaction. Otherwise a removed member's
        # lock would block the note for the rest of its lease unless an admin
        # broke it, and once removed they cannot reach the note to release
        # it. Expired leases are cleared too; that changes no tag.
        release_their_locks = (
            update(Note)
            .where(Note.team_id == team_id, Note.lock_user_id == user_id)
            .values(lock_user_id=None, lock_expires_at=None, lock_active_at=None)
            .returning(Note.id)
            .execution_options(synchronize_session=False)
        )
        with self._session() as db:
            self._lock_team(db, team_id)
            deleted = db.execute(remove).rowcount
            unlocked = list(db.scalars(release_their_locks)) if deleted else []
            db.commit()
            if deleted == 0:
                if db.get(Membership, (team_id, user_id)) is None:
                    raise NotFound("Member")
                raise LastAdmin()
            return unlocked

    # Notes

    def create_note_if_room(self, team_id: str, author_id: str, title: str, body: str, now: datetime) -> Note:
        # Never added to the session: it holds the values to insert and is returned as the result.
        note = Note(
            id=new_id(),
            team_id=team_id,
            author_id=author_id,
            title=title,
            body=body,
            version=1,
            created_at=now,
            updated_at=now,
        )
        note_count = select(func.count()).select_from(Note).where(Note.team_id == team_id).scalar_subquery()
        values = [
            literal(getattr(note, column), UTCDateTime()) if column.endswith("_at") else literal(getattr(note, column))
            for column in _NEW_NOTE_COLUMNS
        ]
        statement = (
            insert(Note)
            .from_select(list(_NEW_NOTE_COLUMNS), select(*values).where(note_count < config.MAX_NOTES_PER_TEAM))
            .returning(Note.id)
        )
        with self._session() as db:
            self._lock_team(db, team_id)
            inserted = db.scalars(statement).first() is not None
            db.commit()
        if not inserted:
            raise TeamNotesFull()
        return note

    def notes_page(self, team_id: str, search: str | None, limit: int, after: NotePosition | None) -> NotePage:
        # A summary has no body, so do not fetch up to 100 KB per row; the search still reads it in SQL.
        query = (
            select(Note).options(defer(Note.body, raiseload=True), with_lock_holder()).where(Note.team_id == team_id)
        )
        if search:
            query = query.where(
                or_(self._contains_folded(Note.title, search), self._contains_folded(Note.body, search))
            )
        if after is not None:
            # Keyset paging: start strictly after the last row shown, found
            # through the index rather than by reading and discarding the rows
            # before it, so a note created, edited or deleted elsewhere in the
            # list cannot shift this page. A row value compares as the ORDER
            # BY does (both columns descending), and both SQLite and Postgres
            # turn it into a range on ix_notes_team_recent. The bound values
            # go through the columns' own types, so the timestamp is compared
            # in its stored form, to the microsecond.
            bound = tuple_(literal(after.updated_at, UTCDateTime()), literal(after.id, Note.id.type))
            query = query.where(tuple_(Note.updated_at, Note.id) < bound)
        # One row more than the page: if it comes back there is a next page,
        # without a second query to count. Ties on updated_at fall back to the
        # id, so the order is total and a position names one point in it.
        query = query.order_by(Note.updated_at.desc(), Note.id.desc()).limit(limit + 1)
        with self._session() as db:
            notes = list(db.scalars(query))
        return NotePage(notes=notes[:limit], has_more=len(notes) > limit)

    def note(self, team_id: str, note_id: str) -> Note:
        with self._session() as db:
            return self._load_note(db, team_id, note_id)

    def note_change_for_member(self, team_id: str, note_id: str, user_id: str) -> Note:
        with self._session() as db:
            if db.get(Membership, (team_id, user_id)) is None:
                raise NotFound("Team")
            return self._load_note(db, team_id, note_id, _CHANGE_COLUMNS)

    def save_note(
        self,
        team_id: str,
        note_id: str,
        user_id: str,
        version: int | None,
        title: str | None,
        body: str | None,
        now: datetime,
    ) -> Note:
        # A save is activity, so it also restarts the holder's idle clock.
        changes: dict[str, object] = {"version": Note.version + 1, "updated_at": now, "lock_active_at": now}
        if title is not None:
            changes["title"] = title
        if body is not None:
            changes["body"] = body
        # RETURNING: the result is the row this statement wrote. Reading the
        # note again after the commit could return someone else's later save
        # as if it were this one.
        statement = (
            update(Note)
            .where(note_in_team(team_id, note_id), lock_held_by(user_id, now), version_is(version))
            .values(changes)
            .returning(Note)
            # On a retry the refusal read has already put this note in the
            # session; take the returned values over that older copy.
            .execution_options(synchronize_session=False, populate_existing=True)
        )

        # The lock rule is judged before the version, so a caller without the
        # lock learns that, whatever version they sent.
        def refusal(note: Note) -> Refusal | None:
            holder = note.live_lock_user_id(now)
            if holder is not None and holder != user_id:
                return NoteLocked(note, now)
            if holder is None:
                return LockRequired()
            if version is None or note.version != version:
                return VersionConflict(note, now)
            return None

        with self._session() as db:

            def write() -> Note | None:
                saved = db.scalars(statement).one_or_none()
                db.commit()
                return saved

            return _write_or_refuse(write, lambda: refusal(self._note_for_refusal(db, team_id, note_id)), now)

    def delete_note(
        self, team_id: str, note_id: str, user_id: str, is_admin: bool, version: int | None, now: datetime
    ) -> None:
        statement = (
            delete(Note)
            .where(note_in_team(team_id, note_id))
            # Part of the DELETE, not a prior check, so nobody can take the
            # lock, or save, between "is anyone editing?" and the delete.
            .where(lock_available_to(user_id, now), version_is(version))
            .execution_options(synchronize_session=False)
        )
        if not is_admin:
            statement = statement.where(Note.author_id == user_id)

        # Ownership and the lock are judged before the version, as for a save.
        def refusal(note: Note) -> Refusal | None:
            if not is_admin and note.author_id != user_id:
                return NotAuthor()
            holder = note.live_lock_user_id(now)
            if holder is not None and holder != user_id:
                return NoteLocked(note, now)
            if version is None or note.version != version:
                return VersionConflict(note, now)
            return None

        with self._session() as db:

            def write() -> bool | None:
                deleted = db.execute(statement).rowcount
                db.commit()
                return True if deleted else None

            _write_or_refuse(write, lambda: refusal(self._note_for_refusal(db, team_id, note_id)), now)

    # The edit lock

    def take_lock(
        self,
        team_id: str,
        note_id: str,
        user_id: str,
        now: datetime,
        expires_at: datetime,
        renew_only: bool,
        active: bool,
    ) -> LockOutcome:
        renewed: dict[str, object] = {"lock_expires_at": expires_at}
        if active:
            renewed["lock_active_at"] = now
        # `lock_held_by` includes the idle condition, so a renewal can never
        # extend a lock whose holder is already idle, whatever `active` says:
        # from the idle limit on, others see the note as free.
        renewal = (
            update(Note)
            .where(note_in_team(team_id, note_id), lock_held_by(user_id, now))
            .values(renewed)
            .execution_options(synchronize_session=False)
        )
        # The take itself: one conditional UPDATE, so two people asking at
        # once cannot both succeed. One row changed means the caller has it.
        take = (
            update(Note)
            .where(note_in_team(team_id, note_id), lock_available_to(user_id, now))
            .values(lock_user_id=user_id, lock_expires_at=expires_at, lock_active_at=now)
            .execution_options(synchronize_session=False)
        )
        # The check that the caller's lock is idle and its release are one
        # statement, so no take or save can land between them.
        release_idle = (
            update(Note)
            .where(note_in_team(team_id, note_id), Note.lock_user_id == user_id, lock_is_idle(now))
            .values(lock_user_id=None, lock_expires_at=None, lock_active_at=None)
            .execution_options(synchronize_session=False)
        )

        def refusal(note: Note) -> Refusal | None:
            holder = note.live_lock_user_id(now)
            if holder is not None and holder != user_id:
                return NoteLocked(note, now)
            if holder is None and renew_only:
                return LockRequired()
            return None

        with self._session() as db:

            def write() -> LockOutcome | None:
                # Renewal is tried as its own statement first so the caller
                # knows which happened: a renewal leaves the tag unchanged and
                # must not wake viewers, a take changes it and must. Both run
                # in one transaction. A renew-only call never takes; instead
                # it is the call that discovers an idle lock and ends it.
                if db.execute(renewal).rowcount:
                    outcome: LockOutcome | None = LockOutcome.RENEWED
                elif not renew_only and db.execute(take).rowcount:
                    outcome = LockOutcome.TAKEN
                elif renew_only and db.execute(release_idle).rowcount:
                    outcome = LockOutcome.IDLE_RELEASED
                else:
                    outcome = None
                db.commit()
                return outcome

            return _write_or_refuse(write, lambda: refusal(self._note_for_refusal(db, team_id, note_id)), now)

    def release_lock(self, team_id: str, note_id: str, user_id: str, is_admin: bool, now: datetime) -> bool:
        releasable = lock_is_live(now) if is_admin else lock_held_by(user_id, now)
        statement = (
            update(Note)
            .where(note_in_team(team_id, note_id), releasable)
            .values(lock_user_id=None, lock_expires_at=None, lock_active_at=None)
            .execution_options(synchronize_session=False)
        )
        with self._session() as db:
            # Not `_write_or_refuse`: here one refusal ("no live lock") is a
            # success. Same idea: the read may be later than the write, so if
            # it shows a lock this caller may release, release again, once.
            for _ in range(2):
                released = bool(db.execute(statement).rowcount)
                db.commit()
                if released:
                    return True
                holder = self._note_for_refusal(db, team_id, note_id).live_lock_user_id(now)
                if holder is None:
                    return False
                if holder != user_id and not is_admin:
                    raise LockHeldByAnother()
            raise NoteLocked(None, now)  # the lock changed hands twice meanwhile: "retry"


def _write_or_refuse[T](write: Callable[[], T | None], refusal: Callable[[], Refusal | None], now: datetime) -> T:
    """Run a conditional write; if it changed nothing, raise the reason.

    `write` runs one conditional statement (its WHERE holds every rule) and
    commits; it returns None if no row matched. Only then does `refusal` read
    the note to word the error. That read is a later snapshot than the write,
    so it can disagree with it: the lock that refused a take may have been
    released in between, and answering `note_locked` with no lock in it, or
    `precondition_failed` carrying the very version the client sent, would
    contradict itself. So `refusal` returns None when it finds nothing in the
    way, and the write is tried once more, as if the request had arrived a
    moment later. If the state flips back again before the second read (two
    changes inside a millisecond), the answer is `NoteLocked` with no note,
    which the contract defines as "retry".
    """
    for _ in range(2):
        result = write()
        if result is not None:
            return result
        reason = refusal()
        if reason is not None:
            raise reason
    raise NoteLocked(None, now)
