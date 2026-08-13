"""Who may sign in, and which towing companies they may look at.

This is the module that makes one install safe to sell to more than one
company. ``core/companies.py`` already keeps every tenant's ROWS apart -- every
query filters on ``company_id`` and has since the roster existed. What it could
not do was keep tenants' READERS apart, because there was one shared password
and the company switcher was a preference rather than a permission. A customer
handed the URL could type another customer's slug into it and be shown their
client names, volumes and acceptance rates.

TWO MODES, AND THE TABLE DECIDES WHICH
--------------------------------------
    no enabled rows   -> SHARED PASSWORD. DASHBOARD_PASSWORD, one login, every
                         company on the install. Exactly the behaviour before
                         this module existed, so the deployments already
                         running do not change under them and no flag has to be
                         set to keep them working.

    any enabled row   -> ACCOUNTS. Username and password, and a reader sees
                         only the companies their row names.

Creating the first account is what moves an install from the first to the
second, and deleting the last one moves it back. There is deliberately no
``ACCOUNTS_ENABLED`` variable: a permission model that can be switched off by
an environment variable is one that will be switched off by an environment
variable, and a half-configured install that answers to ``1234`` while holding
four companies' data is worse than either mode on its own.

MIXING THEM IS NOT ALLOWED
--------------------------
Once one account exists the shared password stops being accepted entirely. It
is not kept as a fallback for the operator: a "break glass" password that opens
every company is the thing this module was written to remove, and an operator
who is locked out has ``python -m towbook_agent users`` on the server.

PASSWORDS
---------
PBKDF2-HMAC-SHA256 from :mod:`hashlib`, 600,000 iterations, 16 random bytes of
salt per user, stored as ``pbkdf2_sha256$<iterations>$<salt>$<hash>``. Not
bcrypt, argon2 or scrypt: each of those is a compiled dependency in a container
that currently builds from a pure-Python requirements.txt, and PBKDF2 at
600k iterations is what OWASP names for SHA-256 as of this writing. The
iteration count is stored per row, so raising it later re-hashes each user at
their next successful sign-in instead of invalidating every password at once --
see :func:`authenticate`.

WHAT IS NOT HERE
----------------
No self-serve signup, no password-reset email, no OAuth, no per-tab
permissions. An operator creates accounts and hands over a first password. That
is a deliberate stop: every one of those features needs a mail path this system
does not have (config/notifications.yaml ships with every route disabled), and
a reset link that cannot be delivered is a support call rather than a feature.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select

from ..core import companies as _companies
from ..core import db as core_db
from ..core.models import (
    ROLE_MEMBER,
    ROLE_OPERATOR,
    ROLE_VALUES,
    DashboardUser,
    utcnow,
)

__all__ = [
    "ROLE_MEMBER",
    "ROLE_OPERATOR",
    "ROLE_VALUES",
    "AccountError",
    "Principal",
    "accounts_are_configured",
    "authenticate",
    "count_enabled",
    "create_user",
    "delete_user",
    "get_user",
    "hash_password",
    "list_users",
    "normalise_username",
    "password_complaint",
    "principal_for_user",
    "set_password",
    "shared_password_principal",
    "update_user",
    "verify_hash",
    "bootstrap_operator_from_env",
]

logger = logging.getLogger(__name__)


class AccountError(ValueError):
    """An account could not be created or changed, with a reason to show."""


# ==========================================================================
# Passwords
# ==========================================================================

#: OWASP's PBKDF2-HMAC-SHA256 figure. Stored per row so it can be raised
#: without invalidating anybody's password -- see :func:`authenticate`.
PBKDF2_ITERATIONS: int = 600_000

_ALGORITHM = "pbkdf2_sha256"

#: Long enough to be worth the iterations, short enough to type once. There is
#: no upper bound and no character-class rule: a length floor is the only
#: password rule with evidence behind it, and the rest push people to "Tow1ng!".
MIN_PASSWORD_LENGTH: int = 12

_USERNAME_SAFE = re.compile(r"[^a-z0-9._@+-]+")


def normalise_username(value: Any) -> str:
    """Casefold and strip a username down to what is stored.

    Stored in this form rather than compared case-insensitively at read time,
    so the UNIQUE constraint does the work. SQLite and PostgreSQL disagree
    about case-insensitive uniqueness, and "Owner" and "owner" being one
    account on one backend and two on the other is not a difference anybody
    should meet in production.
    """
    text = str(value or "").strip().casefold()
    return _USERNAME_SAFE.sub("", text)


def password_complaint(password: str | None) -> str | None:
    """What is wrong with this password, or ``None`` if nothing is.

    Returns a sentence to show the user rather than a bool, because "that
    password is not acceptable" without saying why is how somebody ends up
    trying the same thing four times.
    """
    text = str(password or "")
    if len(text.strip()) != len(text):
        return "That password starts or ends with a space, which is too easy to lose."
    if len(text) < MIN_PASSWORD_LENGTH:
        return (
            f"That password is {len(text)} characters. It needs at least "
            f"{MIN_PASSWORD_LENGTH}. Length is what makes a password hard to "
            f"guess -- three or four unrelated words beat one clever one."
        )
    return None


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_hash(password: str | None, encoded: str | None) -> tuple[bool, bool]:
    """``(matches, should_be_rehashed)`` for a candidate password.

    The second value is True when the stored hash used fewer iterations than
    :data:`PBKDF2_ITERATIONS`, which is how the cost is raised over time
    without a flag day: the row is re-hashed on the next correct sign-in.

    A malformed or empty stored hash is a non-match, not an exception. It also
    still costs a full PBKDF2 round against a throwaway salt, so that "this
    account has no usable password" and "this password is wrong" take the same
    time to answer.
    """
    parts = str(encoded or "").split("$")
    if len(parts) != 4 or parts[0] != _ALGORITHM:
        hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), b"x" * 16, 1000)
        return (False, False)

    _, raw_iterations, salt_hex, expected_hex = parts
    try:
        iterations = int(raw_iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except ValueError:
        return (False, False)

    candidate = hashlib.pbkdf2_hmac(
        "sha256", str(password or "").encode("utf-8"), salt, iterations
    )
    matches = hmac.compare_digest(candidate, expected)
    return (matches, matches and iterations < PBKDF2_ITERATIONS)


# ==========================================================================
# The principal -- who is making this request
# ==========================================================================


@dataclass(frozen=True)
class Principal:
    """The signed-in reader, and what they may see.

    Assembled once per request from the session cookie and the user row, then
    carried on ``request.state``. It is the only thing the view layer is
    allowed to ask "may this person see company X", and it answers by handing
    :attr:`company_ids` to ``core.companies.use_visible_companies`` rather than
    by being consulted per endpoint -- forty endpoints that each remember to
    check is thirty-nine chances to forget.
    """

    #: Row id, or None in shared-password mode where there is no row.
    user_id: int | None
    username: str
    display_name: str
    role: str
    #: The companies this reader may open, or ``None`` for "every company on
    #: this install". None is the operator and the shared-password session; it
    #: is what :func:`~towbook_agent.core.companies.use_visible_companies`
    #: reads as unscoped.
    company_ids: tuple[str, ...] | None
    must_change_password: bool = False
    #: True for a session authenticated by DASHBOARD_PASSWORD rather than by an
    #: account. The banner on the board says so, because a reader who cannot
    #: see a name in the corner has no way to tell which mode they are in.
    is_shared_password: bool = False

    @property
    def is_operator(self) -> bool:
        return self.role == ROLE_OPERATOR

    @property
    def label(self) -> str:
        return self.display_name.strip() or self.username

    def may_manage_accounts(self) -> bool:
        """Only an operator. A member cannot widen its own scope.

        The shared-password session counts, and it is safe that it does
        BECAUSE OF WHEN IT CAN EXIST: ``resolve_principal`` only issues one
        while the accounts table is empty, so this permission is the
        bootstrap and nothing else. The first account created from such a
        session immediately invalidates it -- the shared password stops being
        accepted and the ``v1`` cookie stops verifying -- so it grants exactly
        one use of this screen, to somebody who could already read every
        company on the install.

        Without it the first account could only be made by setting two
        environment variables and redeploying, which is a poor way to be told
        that the board you have been running for a year now needs logins.
        """
        return self.is_operator

    def as_dict(self) -> dict[str, Any]:
        """Template-safe. Carries no hash and no session material."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "label": self.label,
            "role": self.role,
            "is_operator": self.is_operator,
            "company_ids": list(self.company_ids) if self.company_ids is not None else None,
            "must_change_password": self.must_change_password,
            "is_shared_password": self.is_shared_password,
            "may_manage_accounts": self.may_manage_accounts(),
        }


def principal_for_user(user: DashboardUser) -> Principal:
    """The :class:`Principal` for a user row.

    An operator's scope is ``None`` -- every company -- and their stored
    ``company_scope`` is ignored rather than merged in. The list is kept on the
    row so that demoting them to a member restores a scope they once had
    instead of silently granting them nothing.
    """
    scope: tuple[str, ...] | None
    if user.role == ROLE_OPERATOR:
        scope = None
    else:
        scope = tuple(
            _companies.normalise_company_id(value)
            for value in (user.company_scope or [])
            if str(value or "").strip()
        )
    return Principal(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name or "",
        role=user.role,
        company_ids=scope,
        must_change_password=bool(user.must_change_password),
        is_shared_password=False,
    )


def shared_password_principal() -> Principal:
    """The reader on an install that has no accounts yet.

    Unscoped and an operator, because that is exactly what the shared password
    has always granted -- every company on the install -- and pretending
    otherwise would break the single-company deployments already running.

    It can only be handed out while the accounts table is empty (see
    ``auth.resolve_principal``), which is what makes the operator role on it
    self-limiting: the first account it creates ends it. See
    :meth:`Principal.may_manage_accounts`.
    """
    return Principal(
        user_id=None,
        username="",
        display_name="Shared password",
        role=ROLE_OPERATOR,
        company_ids=None,
        must_change_password=False,
        is_shared_password=True,
    )


# ==========================================================================
# The store
# ==========================================================================


def count_enabled() -> int:
    """How many accounts can sign in. Zero means shared-password mode.

    Read on every request that carries a session, which is one indexed count
    against a table with as many rows as the operator has customers. It is not
    cached, and that is the point: disabling an account has to lock somebody
    out NOW, not at the end of a cache window, and the same read is what makes
    a scope change take effect on the reader's next click.
    """
    try:
        with core_db.get_session(commit=False) as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(DashboardUser)
                    .where(DashboardUser.enabled.is_(True))
                ).scalar_one()
            )
    except Exception as exc:
        # TWO DIFFERENT FAILURES, AND ONLY ONE OF THEM IS SAFE TO ABSORB.
        #
        # No such table: this install has not run migration 0008 yet. It is not
        # an outage, it is the state every deployment was in before this
        # feature existed, and shared-password mode is the correct -- and
        # previous -- behaviour. Absorbed, so upgrading the code without
        # running alembic does not take the board down.
        #
        # Anything else is the database being unreachable, and absorbing THAT
        # would turn an outage into "every company's data is behind 1234
        # again". Re-raised, so the request 500s instead of failing open.
        if not _table_is_missing():
            logger.error("could not count dashboard accounts: %s", exc)
            raise
        global _warned_about_missing_table
        if not _warned_about_missing_table:
            _warned_about_missing_table = True
            logger.warning(
                "the dashboard_users table does not exist, so this install is on "
                "the shared password and every session sees every company. Run "
                "`alembic upgrade head` and create an operator account before "
                "adding a company you do not own."
            )
        return 0


_warned_about_missing_table: bool = False


def _table_is_missing() -> bool:
    """Is ``dashboard_users`` absent, as opposed to unreadable?

    Asked only after a query has already failed, so the cost of the inspection
    is paid on the error path and never on a healthy request.
    """
    try:
        from sqlalchemy import inspect as sa_inspect

        return not sa_inspect(core_db.get_engine()).has_table(DashboardUser.__tablename__)
    except Exception:  # pragma: no cover - the database is unreachable
        return False


def accounts_are_configured() -> bool:
    """True once at least one account can sign in."""
    return count_enabled() > 0


def get_user(user_id: int | None = None, username: str | None = None) -> DashboardUser | None:
    """One user by id or by username. Detached from the session.

    Returned detached (``expunge``) because the caller is the request layer,
    which reads attributes long after the session has closed and must not hold
    a connection from the pool while it renders a page.
    """
    if user_id is None and not username:
        return None
    with core_db.get_session(commit=False) as session:
        statement = select(DashboardUser)
        if user_id is not None:
            statement = statement.where(DashboardUser.id == int(user_id))
        else:
            statement = statement.where(
                DashboardUser.username == normalise_username(username)
            )
        user = session.execute(statement).scalar_one_or_none()
        if user is not None:
            session.expunge(user)
        return user


def list_users() -> list[DashboardUser]:
    """Every account, enabled first-created order. Detached."""
    with core_db.get_session(commit=False) as session:
        users = list(
            session.execute(
                select(DashboardUser).order_by(
                    DashboardUser.role.desc(), DashboardUser.username
                )
            ).scalars()
        )
        for user in users:
            session.expunge(user)
        return users


def _clean_scope(company_ids: Iterable[str] | None, role: str) -> list[str]:
    """The scope to store for this role.

    An operator stores an EMPTY list: their access comes from the role, and a
    wildcard left in the column would survive a demotion to member and hand
    them the whole install under a name that reads as restricted.
    """
    if role == ROLE_OPERATOR:
        return []
    cleaned: list[str] = []
    for value in company_ids or []:
        slug = _companies.normalise_company_id(value)
        if slug and slug not in cleaned:
            cleaned.append(slug)
    return cleaned


def _validate(username: str, role: str, scope: Sequence[str]) -> None:
    if not username:
        raise AccountError(
            "A username is required. Letters, digits and . _ @ + - only."
        )
    if role not in ROLE_VALUES:
        raise AccountError(f"{role!r} is not a role. Use one of: {', '.join(ROLE_VALUES)}.")
    if role == ROLE_MEMBER and not scope:
        # A member with no companies can sign in and see an empty board, which
        # reads as "the system is broken" rather than as "you were given no
        # access". Refused at the point of creation, where it can still be
        # fixed by the person who meant to name a company.
        raise AccountError(
            "A member account must name at least one company. An account with no "
            "companies can sign in and see nothing, which looks like a fault "
            "rather than a permission."
        )


def create_user(
    username: str,
    password: str,
    *,
    role: str = ROLE_MEMBER,
    company_ids: Iterable[str] | None = None,
    display_name: str = "",
    email: str | None = None,
    must_change_password: bool = True,
) -> DashboardUser:
    """Create an account. Raises :class:`AccountError` with a readable reason.

    ``must_change_password`` defaults True: the first password is chosen by the
    operator and travels to the customer through some channel the operator does
    not control, so it is a delivery token rather than a password and the board
    refuses to render anything until it has been replaced.
    """
    name = normalise_username(username)
    scope = _clean_scope(company_ids, role)
    _validate(name, role, scope)

    complaint = password_complaint(password)
    if complaint:
        raise AccountError(complaint)

    with core_db.get_session() as session:
        exists = session.execute(
            select(DashboardUser.id).where(DashboardUser.username == name)
        ).scalar_one_or_none()
        if exists is not None:
            raise AccountError(f"There is already an account called {name!r}.")

        now = utcnow()
        user = DashboardUser(
            username=name,
            password_hash=hash_password(password),
            display_name=(display_name or "").strip(),
            email=(email or "").strip() or None,
            role=role,
            company_scope=scope,
            enabled=True,
            must_change_password=bool(must_change_password),
            created_at=now,
            password_changed_at=now,
        )
        session.add(user)
        session.flush()
        session.expunge(user)
        logger.info(
            "created dashboard account %r (%s) scoped to %s",
            name,
            role,
            scope or "every company",
        )
        return user


def update_user(
    user_id: int,
    *,
    role: str | None = None,
    company_ids: Iterable[str] | None = None,
    display_name: str | None = None,
    email: str | None = None,
    enabled: bool | None = None,
) -> DashboardUser:
    """Change an account. Does NOT touch the password -- see :func:`set_password`."""
    with core_db.get_session() as session:
        user = session.get(DashboardUser, int(user_id))
        if user is None:
            raise AccountError("That account no longer exists.")

        new_role = role if role is not None else user.role
        if company_ids is not None or role is not None:
            source = company_ids if company_ids is not None else user.company_scope
            user.company_scope = _clean_scope(source, new_role)
        _validate(user.username, new_role, user.company_scope)
        user.role = new_role

        if display_name is not None:
            user.display_name = display_name.strip()
        if email is not None:
            user.email = email.strip() or None
        if enabled is not None:
            if not enabled and _would_orphan_the_install(session, user):
                raise AccountError(
                    "That is the only operator account left. Disabling it would "
                    "leave nobody able to create accounts, and there is no "
                    "recovery password. Create another operator first."
                )
            user.enabled = bool(enabled)

        session.flush()
        session.expunge(user)
        return user


def set_password(user_id: int, password: str, *, must_change: bool = False) -> None:
    """Replace an account's password and end every session it has open.

    ``password_changed_at`` is part of the session signature (``web/auth.py``),
    so moving it invalidates every cookie already issued to this user. That is
    the point of a password change and not a side effect of one: a rotation
    that left the previous holder signed in has not rotated anything.
    """
    complaint = password_complaint(password)
    if complaint:
        raise AccountError(complaint)

    with core_db.get_session() as session:
        user = session.get(DashboardUser, int(user_id))
        if user is None:
            raise AccountError("That account no longer exists.")
        user.password_hash = hash_password(password)
        user.password_changed_at = utcnow()
        user.must_change_password = bool(must_change)
        logger.info("password changed for dashboard account %r", user.username)


def delete_user(user_id: int) -> None:
    """Remove an account outright.

    Prefer disabling: a deleted account's name stops resolving, which makes the
    log of what it did unreadable. Deletion is here for the account created by
    a typo, not for a departure.
    """
    with core_db.get_session() as session:
        user = session.get(DashboardUser, int(user_id))
        if user is None:
            return
        if _would_orphan_the_install(session, user):
            raise AccountError(
                "That is the only operator account left. Deleting it would leave "
                "nobody able to create accounts. Create another operator first."
            )
        logger.warning("deleted dashboard account %r", user.username)
        session.delete(user)


def _would_orphan_the_install(session: Any, user: DashboardUser) -> bool:
    """Is this the last enabled operator?

    Removing it would leave an install with member accounts, no way to create
    another operator, and no shared password to fall back to -- because the
    shared password stops being accepted the moment any account exists. The
    only repair would be a shell on the server, which a customer's operator may
    not have at 2am.
    """
    if user.role != ROLE_OPERATOR or not user.enabled:
        return False
    remaining = session.execute(
        select(func.count())
        .select_from(DashboardUser)
        .where(
            DashboardUser.role == ROLE_OPERATOR,
            DashboardUser.enabled.is_(True),
            DashboardUser.id != user.id,
        )
    ).scalar_one()
    return int(remaining) == 0


# ==========================================================================
# Signing in
# ==========================================================================


def authenticate(username: str | None, password: str | None) -> DashboardUser | None:
    """The user these credentials belong to, or ``None``.

    Returns None for an unknown username, a disabled account and a wrong
    password alike, and the caller shows one message for all three: telling an
    attacker which usernames exist is free reconnaissance, and telling them an
    account is disabled tells them it is real.

    An unknown username still costs a PBKDF2 round against a dummy hash, so the
    "no such user" answer takes as long as the "wrong password" one.
    """
    name = normalise_username(username)
    if not name:
        verify_hash(password, None)
        return None

    with core_db.get_session() as session:
        user = session.execute(
            select(DashboardUser).where(DashboardUser.username == name)
        ).scalar_one_or_none()

        if user is None:
            verify_hash(password, None)
            return None

        matches, needs_rehash = verify_hash(password, user.password_hash)
        if not matches:
            logger.warning("failed sign-in for dashboard account %r", name)
            return None
        if not user.enabled:
            logger.warning("sign-in refused for disabled dashboard account %r", name)
            return None

        if needs_rehash:
            # The stored cost is below the current one. Re-hash HERE, where the
            # plaintext is in hand and known correct, rather than forcing a
            # password reset on everybody the day the figure is raised.
            #
            # `password_changed_at` is deliberately NOT moved: the password did
            # not change, and moving it would sign the user out of their other
            # sessions for an upgrade they did not ask for.
            user.password_hash = hash_password(password or "")
            logger.info("re-hashed %r at %d iterations", name, PBKDF2_ITERATIONS)

        user.last_login_at = utcnow()
        session.flush()
        session.expunge(user)
        return user


# ==========================================================================
# Bootstrap
#
# A fresh install has no accounts, which means shared-password mode, which is
# not what anybody selling this to four towing companies wants on day one. So
# the first operator can be declared in the environment -- the same place every
# other credential on this system lives -- and is created at boot.
# ==========================================================================


def bootstrap_operator_from_env() -> str | None:
    """Create or repair the operator account named by the environment.

    ``DASHBOARD_ADMIN_USER`` and ``DASHBOARD_ADMIN_PASS``. Returns a sentence
    for the boot log describing what it did, or None if it did nothing.

    IT DOES NOT RESET THE PASSWORD OF AN ACCOUNT THAT ALREADY EXISTS. The
    variables stay set on the Railway service forever, and a boot that pushed
    them back over the top would silently undo every password change the
    operator made afterwards -- on every redeploy, invisibly. It only ever
    creates the account, or re-enables one that was disabled while its
    variables are still present.
    """
    username = normalise_username(os.environ.get("DASHBOARD_ADMIN_USER"))
    password = os.environ.get("DASHBOARD_ADMIN_PASS") or ""
    if not username or not password:
        return None

    complaint = password_complaint(password)
    if complaint:
        logger.error(
            "DASHBOARD_ADMIN_USER is set but DASHBOARD_ADMIN_PASS is not usable: %s "
            "No account was created, so this install is still on the shared "
            "password.",
            complaint,
        )
        return None

    existing = get_user(username=username)
    if existing is not None:
        if existing.enabled and existing.role == ROLE_OPERATOR:
            return None
        update_user(existing.id, role=ROLE_OPERATOR, enabled=True)
        return f"re-enabled the operator account {username!r}"

    create_user(
        username,
        password,
        role=ROLE_OPERATOR,
        display_name="Operator",
        # The operator typed this password into the Railway variables tab
        # themselves; it did not travel to anyone. Nothing to force a change of.
        must_change_password=False,
    )
    return (
        f"created the operator account {username!r} from DASHBOARD_ADMIN_USER. "
        f"DASHBOARD_PASSWORD is no longer accepted on this install"
    )


# ==========================================================================
# Reporting
# ==========================================================================


def scope_description(principal: Principal) -> str:
    """One sentence naming what this reader can see. Shown on the accounts page."""
    if principal.company_ids is None:
        return "Every company on this install"
    names: list[str] = []
    for company_id in principal.company_ids:
        company = _companies.get_company(company_id)
        names.append(company.label if company is not None else f"{company_id} (not in the roster)")
    return ", ".join(names) or "No company"
