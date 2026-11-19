"""Private, account-wide admission from observed Linear quota headers.

Reservations precede current-effect fences. Unknown dispatches remain charged;
ordinary responses can lower a budget but cannot open a new quota window.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_task_manager.config.files import private_path

from .errors import LinearError

IDENTITY = """query VerifyBinding($team: String!, $project: String!) {
 viewer { id } organization { id }
 team(id: $team) { id key states(first: 50) {
 nodes { id name } pageInfo { hasNextPage endCursor } } }
 project(id: $project) { id teams(first: 50) {
 nodes { id } pageInfo { hasNextPage endCursor } } }
}"""
# Linear's documented maximum complexity of one accepted query.
MAX_COMPLEXITY = 10_000


def retry_seconds(headers):
    try:
        value = float(headers["retry-after"])
        return math.ceil(value) if math.isfinite(value) and value >= 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class QuotaStore:
    _failed_paths = set()

    def __init__(self, path=None, *, clock=time.time):
        self.path = (
            Path(path)
            if path is not None
            else (self._user_home() / ".config" / "ai-task-manager" / "linear-quota.sqlite3")
        )
        self.clock = clock

    @staticmethod
    def _user_home():
        # Native children can replace HOME; Unix identity remains stable.
        try:
            import pwd
        except ImportError:
            return Path.home()
        return Path(pwd.getpwuid(os.getuid()).pw_dir)

    @staticmethod
    def _query_hash(query):
        return hashlib.sha256(query.encode()).hexdigest()

    @classmethod
    def _applies(cls, state, name, query):
        if not name.startswith("endpoint-"):
            return True
        if query is None:
            return False
        endpoint = name.split(":", 1)[-1]
        if state.get("queries", {}).get(cls._query_hash(query)) == endpoint:
            return True
        # Endpoint names denote GraphQL fields. Match the same field in named,
        # aliased and differently parameterized operations, not variable values.
        field = endpoint.rsplit(".", 1)[-1]
        if not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", field):
            return False
        scrubbed = re.sub(r'"(?:\\.|[^"\\])*"|#[^\n]*', "", query)
        return re.search(r"\b" + re.escape(field) + r"\s*(?:\(|\{)", scrubbed) is not None

    @contextmanager
    def _db(self):
        path = private_path(self.path, existing=False)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_mode & 0o077:
            raise LinearError("quota_state_unavailable")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        private_path(path)
        db = sqlite3.connect(path, timeout=0.1)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS accounts (user TEXT PRIMARY KEY, state TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS aliases (credential TEXT PRIMARY KEY, user TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reservations "
                "(id TEXT PRIMARY KEY, user TEXT, credential TEXT, charges TEXT, bootstrap INT, "
                "query_hash TEXT)"
            )
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(db, user):
        row = db.execute("SELECT state FROM accounts WHERE user=?", (user,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, user, state):
        db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?)", (user, json.dumps(state)))

    @staticmethod
    def _charge(name):
        return MAX_COMPLEXITY if "complexity" in name else 1

    def _blocked(self, state, query=None):
        now = self.clock()
        if state is None:
            return "quota_identity_required", None
        if state.get("unavailable"):
            return "quota_state_unavailable", None
        cooldown = state.get("cooldown", 0)
        if now < cooldown:
            return "rate_limited", math.ceil(cooldown - now)
        if state.get("limited"):
            return "rate_limited", None
        if not {"requests", "complexity"} <= state["buckets"].keys():
            return "quota_identity_required", None
        if any(
            bucket["available"] < (self._charge(name) if query is not None else 1)
            for name, bucket in state["buckets"].items()
            if self._applies(state, name, query)
        ):
            return "rate_limited", None
        return None

    def reserve(self, query, variables, credentials, *, principal=None):
        if str(self.path.absolute()) in self._failed_paths:
            raise LinearError("quota_state_unavailable")
        credential = hashlib.sha256(credentials.authorization().encode()).hexdigest()
        with self._db() as db:
            alias = db.execute(
                "SELECT user FROM aliases WHERE credential=?", (credential,)
            ).fetchone()
            if alias and principal is not None and principal != alias[0]:
                raise LinearError("credential_binding_changed")
            user = principal or (alias[0] if alias else None)
            state = self._state(db, user) if user else None
            blocked = self._blocked(state, query)
            bootstrap = blocked is not None or alias is None
            if bootstrap:
                category, retry = blocked or ("quota_identity_required", None)
                unknown = self._state(db, "__unverified_identity__")
                if unknown:
                    horizon = max((b["reset"] for b in unknown["buckets"].values()), default=0)
                    bound = max(unknown["cooldown"], horizon)
                    if bound <= 0 or self.clock() < bound:
                        raise LinearError("rate_limited")
                if query != IDENTITY:
                    raise LinearError(category, retry_after=retry)
                if state is not None and blocked:
                    horizon = max(
                        (
                            b["reset"]
                            for name, b in state["buckets"].items()
                            if self._applies(state, name, query)
                        ),
                        default=0,
                    )
                    bound = max(state.get("cooldown", 0), horizon)
                    if self.clock() < bound or (state.get("limited") and bound <= 0):
                        raise LinearError(category, retry_after=retry)
                    if db.execute("SELECT 1 FROM reservations WHERE user=?", (user,)).fetchone():
                        raise LinearError("quota_busy")
                if db.execute("SELECT 1 FROM reservations WHERE bootstrap>0").fetchone():
                    raise LinearError("quota_busy")
            charges = (
                {}
                if blocked
                else {
                    name: self._charge(name)
                    for name in state["buckets"]
                    if self._applies(state, name, query)
                }
            )
            if state is not None:
                for name, charge in charges.items():
                    state["buckets"][name]["available"] -= charge
                self._save(db, user, state)
            reference = str(uuid4())
            db.execute(
                "INSERT INTO reservations VALUES (?,?,?,?,?,?)",
                (
                    reference,
                    user,
                    credential,
                    json.dumps(charges),
                    2 if blocked and state is not None else int(bootstrap),
                    self._query_hash(query),
                ),
            )
            return reference

    def accounting_failed(self, reference):
        # Preserve the response, while refusing later spending from uncertain
        # local quota observations. No reservation is refunded or replayed.
        self._failed_paths.add(str(self.path.absolute()))
        with self._db() as db:
            row = db.execute("SELECT user FROM reservations WHERE id=?", (reference,)).fetchone()
            if row and row[0]:
                state = self._state(db, row[0])
                if state:
                    state["unavailable"] = True
                    self._save(db, row[0], state)

    def cancel(self, reference):
        # Only the owner that has not crossed the actual transport boundary calls this.
        with self._db() as db:
            row = db.execute(
                "SELECT user,charges FROM reservations WHERE id=?", (reference,)
            ).fetchone()
            if row:
                user, raw = row
                state = self._state(db, user)
                if state:
                    for name, charge in json.loads(raw).items():
                        state["buckets"][name]["available"] += charge
                    self._save(db, user, state)
                db.execute("DELETE FROM reservations WHERE id=?", (reference,))
                if state:
                    self._clamp(db, user, state)
                    self._save(db, user, state)

    def _clamp(self, db, user, state):
        outstanding = [
            json.loads(r[0])
            for r in db.execute("SELECT charges FROM reservations WHERE user=?", (user,))
        ]
        for name, bucket in state["buckets"].items():
            pending = sum(c.get(name, 0) for c in outstanding)
            bucket["available"] = max(0, min(bucket["available"], bucket["floor"] - pending))

    def complete(self, reference, response=None):
        with self._db() as db:
            row = db.execute(
                "SELECT user,credential,charges,bootstrap,query_hash FROM reservations WHERE id=?",
                (reference,),
            ).fetchone()
            if row is None:
                raise LinearError("quota_reservation_missing")
            user, credential, raw, bootstrap, query_hash = row
            charges = json.loads(raw)
            db.execute("DELETE FROM reservations WHERE id=?", (reference,))
            if response is None:
                return  # Never refund a send whose dispatch/receipt is unknown.
            headers = {k.lower(): v for k, v in response.headers.items()}
            try:
                body = json.loads(response.body)
            except (ValueError, UnicodeError):
                body = {}
            errors = body.get("errors", []) if isinstance(body, dict) else []
            limited = response.status == 429 or (
                isinstance(errors, list)
                and any(
                    isinstance(e, dict)
                    and isinstance(e.get("extensions"), dict)
                    and e["extensions"].get("code") == "RATELIMITED"
                    for e in errors
                )
            )
            if bootstrap:
                data = body.get("data") if isinstance(body, dict) else None
                viewer = data.get("viewer") if isinstance(data, dict) else None
                actual = viewer.get("id") if isinstance(viewer, dict) else None
                if not isinstance(actual, str) or (user is not None and actual != user):
                    # Rate refusal need not contain identity data. Retain its
                    # cooldown under the expected user, or the global unknown gate.
                    if not limited:
                        return
                    user = user or "__unverified_identity__"
                    bootstrap = 0
                else:
                    user = actual
                    db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (credential, user))
            if not bootstrap and isinstance(body, dict):
                data = body.get("data")
                viewer = data.get("viewer") if isinstance(data, dict) else None
                if isinstance(viewer, dict) and viewer.get("id") == user:
                    db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (credential, user))
            state = self._state(db, user) or {"buckets": {}, "cooldown": 0, "limited": False}
            if bootstrap == 2:
                # Only an explicitly serialized identity after the retained horizon
                # can establish a fresh window. Moving ordinary reset headers cannot.
                state["buckets"] = {
                    name: bucket
                    for name, bucket in state["buckets"].items()
                    if name.startswith("endpoint-")
                }
                state["limited"] = False
            elif bootstrap and state["buckets"] and not charges:
                # Learning a rotated token's identity cannot reset its user's budget.
                for name, bucket in state["buckets"].items():
                    if not name.startswith("endpoint-"):
                        bucket["available"] -= self._charge(name)
            try:
                cost = int(headers["x-complexity"])
                if not 0 <= cost <= MAX_COMPLEXITY:
                    cost = MAX_COMPLEXITY
            except (KeyError, ValueError, TypeError):
                cost = MAX_COMPLEXITY
            for name, reserved in charges.items():
                actual_cost = cost if "complexity" in name else 1
                state["buckets"][name]["available"] += reserved - actual_cost
            outstanding = [
                json.loads(r[0])
                for r in db.execute("SELECT charges FROM reservations WHERE user=?", (user,))
            ]
            for key, value in headers.items():
                if not key.startswith("x-ratelimit-") or not key.endswith("-remaining"):
                    continue
                name = key[len("x-ratelimit-") : -len("-remaining")]
                header_name = name
                if name.startswith("endpoint-"):
                    endpoint = headers.get("x-ratelimit-endpoint-name")
                    if not endpoint:
                        continue
                    name += ":" + endpoint
                    state.setdefault("queries", {})[query_hash] = endpoint
                try:
                    remaining = int(value)
                    limit = int(headers["x-ratelimit-" + header_name + "-limit"])
                    reset = int(headers["x-ratelimit-" + header_name + "-reset"]) / 1000
                    if not 0 <= remaining <= limit or reset <= 0:
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                pending = sum(c.get(name, 0) for c in outstanding)
                old = state["buckets"].get(name)
                floor = min(old["floor"], remaining) if old else remaining
                available = min(old["available"], floor - pending) if old else floor - pending
                state["buckets"][name] = {
                    "available": max(0, available),
                    "floor": floor,
                    "limit": min(old["limit"], limit) if old else limit,
                    "reset": max(old["reset"], reset) if old else reset,
                }
            if limited:
                state["limited"] = True
                retry = retry_seconds(headers)
                if retry is not None:
                    state["cooldown"] = max(state["cooldown"], self.clock() + retry)
            self._clamp(db, user, state)
            self._save(db, user, state)

    def require_model(self, principal):
        if str(self.path.absolute()) in self._failed_paths:
            raise LinearError("quota_state_unavailable")
        with self._db() as db:
            blocked = self._blocked(self._state(db, principal))
            if blocked:
                raise LinearError(blocked[0], retry_after=blocked[1])


def require_model(scope):
    if scope.provider == "linear":
        QuotaStore().require_model(scope.principal)
