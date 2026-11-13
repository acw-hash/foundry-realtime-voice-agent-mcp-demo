from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher, get_close_matches
from pathlib import Path
from typing import Any, Callable

from .storage import Document, Store, Transaction


DEMO_CODE = "12345678"
DEMO_CANDIDATE = "cand-avery-example"
OTP_TTL = 48 * 60 * 60
MAX_ATTEMPTS = 3
MAX_SEARCH_RESULTS = 5
END_REASONS = frozenset(
    {"authentication_failed", "officer_not_found", "caller_declined", "caller_hung_up", "other"}
)


def fail(code: str, **fields: Any) -> Document:
    return {"ok": False, "error": code, **fields}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def normalize_name(name: str) -> str:
    return " ".join(
        "".join(character if character.isalnum() else " " for character in name.casefold()).split()
    )


class BankService:
    def __init__(
        self,
        store: Store,
        directory_file: Path,
        customers_file: Path,
        *,
        clock: Callable[[], float] = time.time,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.code_factory = code_factory or (
            lambda: f"{secrets.randbelow(100_000_000):08d}"
        )
        document = json.loads(directory_file.read_text(encoding="utf-8"))
        officers = document["officers"]
        if not officers or not all(
            isinstance(row, dict)
            and all(isinstance(row.get(key), str) and row[key] for key in (
                "officer_id", "full_name", "title", "status"
            ))
            for row in officers
        ):
            raise ValueError("Officer fixture requires named, uniquely identified officers")
        self.officers = {row["officer_id"]: row for row in officers}
        if len(self.officers) != len(officers):
            raise ValueError("Officer IDs must be unique")
        self.available = [row for row in officers if row["status"] == "available"]
        self.name_index: dict[str, set[str]] = {}
        self.token_index: dict[str, set[str]] = {}
        for officer in officers:
            aliases = officer.get("aliases", [])
            if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                raise ValueError("Officer aliases must be a list of names")
            for name in [officer["full_name"], *aliases]:
                normalized = normalize_name(name)
                if normalized:
                    self.name_index.setdefault(normalized, set()).add(officer["officer_id"])
                    for token in normalized.split():
                        self.token_index.setdefault(token, set()).add(officer["officer_id"])
        customers = json.loads(customers_file.read_text(encoding="utf-8"))
        if customers.get("fixture") != "fictional":
            raise ValueError("Customer profiles must be explicitly fictional")
        self.customers = {row["candidate_id"]: row for row in customers["candidates"]}
        if len(self.customers) != len(customers["candidates"]):
            raise ValueError("Candidate IDs must be unique")
        if DEMO_CANDIDATE not in self.customers:
            raise ValueError("The fictional Avery Example demo profile is required")

    def candidates(self) -> Document:
        return {
            "ok": True,
            "candidates": [
                {**row, "fixture": "fictional"} for row in self.customers.values()
            ],
            "permanent_demo": {
                "candidate_id": DEMO_CANDIDATE,
                "full_name": "Avery Example",
                "otp": DEMO_CODE,
                "fixture": "fictional",
            },
        }

    def issue_otp(self, candidate_id: str) -> Document:
        if candidate_id not in self.customers:
            return fail("unknown_candidate")

        def issue(tx: Transaction) -> Document:
            for _ in range(12):
                code = self.code_factory()
                if not re.fullmatch(r"[0-9]{8}", code):
                    raise ValueError("OTP generator must return eight ASCII digits")
                if secrets.compare_digest(code, DEMO_CODE):
                    continue
                key = "otp_" + digest(code)
                existing = tx.get(key)
                now = self.clock()
                if existing and existing["expires_at"] > now:
                    continue
                active_key = "active_" + digest(candidate_id)
                previous = tx.get(active_key)
                if previous and previous["key"] != key:
                    tx.delete(previous["key"])
                tx.set(
                    key,
                    {
                        "candidate_id": candidate_id,
                        "expires_at": now + OTP_TTL,
                        "consumed": False,
                    },
                )
                tx.set(active_key, {"key": key})
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "otp": code,
                    "created_at": iso(now),
                    "expires_at": iso(now + OTP_TTL),
                    "expires_in_seconds": OTP_TTL,
                }
            raise ValueError("Unable to allocate a unique non-reserved OTP")

        return self.store.run(issue)

    def start_call(self) -> Document:
        session_id = "finance-" + uuid.uuid4().hex

        def start(tx: Transaction) -> Document:
            tx.set(
                session_id,
                {
                    "session_id": session_id,
                    "authenticated": False,
                    "customer": None,
                    "otp_attempts": 0,
                    "otp_digits": "",
                    "locked_out": False,
                    "last_match_ids": [],
                    "confirmed_officer_id": None,
                    "search_attempts": 0,
                    "ended": False,
                    "disposition": None,
                    "state_version": 0,
                    "created_at": iso(self.clock()),
                },
            )
            return {
                "ok": True,
                "session_id": session_id,
                "authenticated": False,
                "max_otp_attempts": MAX_ATTEMPTS,
                "state_version": 0,
            }

        return self.store.run(start)

    def _call(
        self,
        session_id: str,
        action: Callable[[Transaction, Document], Document],
        *,
        allow_ended: bool = False,
    ) -> Document:
        if not re.fullmatch(r"finance-[0-9a-f]{32}", session_id):
            return fail("unknown_session", session_id=session_id)

        def execute(tx: Transaction) -> Document:
            state = tx.get(session_id)
            if state is None:
                return fail("unknown_session", session_id=session_id)
            if state["ended"] and not allow_ended:
                return fail("call_already_ended", session_id=session_id)
            result = action(tx, state)
            state["state_version"] += 1
            tx.set(session_id, state)
            return {**result, "session_id": session_id, "state_version": state["state_version"]}

        return self.store.run(execute)

    def verify_caller_otp(self, session_id: str, otp: str) -> Document:
        def verify(tx: Transaction, state: Document) -> Document:
            attempts = state["otp_attempts"]
            if state["authenticated"]:
                return fail("already_authenticated")
            if state["locked_out"]:
                return fail("locked_out", attempts_remaining=0, locked_out=True)
            if not re.fullmatch(r"[0-9 \t-]*", otp):
                return fail("access_code_invalid_format", attempts_remaining=MAX_ATTEMPTS - attempts)
            digits = re.sub(r"[^0-9]", "", otp)
            collected = digits if len(digits) >= 8 else state["otp_digits"] + digits
            if len(collected) < 8:
                state["otp_digits"] = collected
                return fail(
                    "access_code_incomplete",
                    digits_collected=len(collected),
                    digits_missing=8 - len(collected),
                    attempts_remaining=MAX_ATTEMPTS - attempts,
                )
            state["otp_digits"] = ""
            state["otp_attempts"] = attempts + 1
            candidate_id = None
            status = "invalid"
            if secrets.compare_digest(collected, DEMO_CODE):
                candidate_id, status = DEMO_CANDIDATE, "test_verified"
            elif len(collected) == 8:
                key = "otp_" + digest(collected)
                record = tx.get(key)
                if record:
                    if self.clock() >= record["expires_at"]:
                        status = "expired"
                    elif record["consumed"]:
                        status = "consumed"
                    else:
                        candidate_id, status = record["candidate_id"], "verified"
                        record["consumed"] = True
                        tx.set(key, record)
            authenticated = candidate_id is not None
            state["authenticated"] = authenticated
            state["locked_out"] = not authenticated and state["otp_attempts"] >= MAX_ATTEMPTS
            fields = {
                "authenticated": authenticated,
                "status": status,
                "otp_attempts": state["otp_attempts"],
                "attempts_remaining": MAX_ATTEMPTS - state["otp_attempts"],
                "locked_out": state["locked_out"],
            }
            if authenticated:
                state["customer"] = {**self.customers[candidate_id], "fixture": "fictional"}
                return {"ok": True, **fields, "customer": state["customer"]}
            return fail("access_code_invalid", **fields)

        return self._call(session_id, verify)

    @staticmethod
    def public_officer(officer: Document) -> Document:
        return {key: officer.get(key, "") for key in (
            "officer_id", "full_name", "title", "branch", "status"
        )}

    def find_officers(self, queries: list[str]) -> tuple[list[Document], str]:
        normalized = {normalize_name(query) for query in queries} - {""}
        candidates: set[str] = set()
        match_type = "none"
        for query in normalized:
            candidates.update(self.name_index.get(query, set()))
        if candidates:
            match_type = "exact"
        else:
            for query in normalized:
                token_matches = [self.token_index.get(token, set()) for token in query.split()]
                tokens = set(query.split())
                for officer_id in set.intersection(*token_matches):
                    officer = self.officers[officer_id]
                    names = [officer["full_name"], *officer.get("aliases", [])]
                    if any(tokens.issubset(normalize_name(name).split()) for name in names):
                        candidates.add(officer_id)
            if candidates:
                match_type = "partial"
            else:
                scores: dict[str, float] = {}
                for query in normalized:
                    if len(query) < 4:
                        continue
                    index = self.token_index if len(query.split()) == 1 else self.name_index
                    for name in get_close_matches(query, index, n=5, cutoff=0.85):
                        score = SequenceMatcher(None, name, query).ratio()
                        for officer_id in index[name]:
                            scores[officer_id] = max(score, scores.get(officer_id, 0.0))
                if scores:
                    best = max(scores.values())
                    candidates = {
                        officer_id for officer_id, score in scores.items() if score >= best - 0.03
                    }
                    match_type = "fuzzy"
        matches = sorted(
            (self.officers[officer_id] for officer_id in candidates),
            key=lambda row: (normalize_name(row["full_name"]), row["branch"], row["officer_id"]),
        )
        return matches, match_type

    def search_loan_officer(
        self, session_id: str, spoken_name: str, also_heard: list[str] | None = None
    ) -> Document:
        def search(tx: Transaction, state: Document) -> Document:
            if not state["authenticated"]:
                return fail("authentication_required")
            if len(also_heard or []) > 2:
                return fail("too_many_alternatives", maximum=2)
            if len(spoken_name) > 256 or any(len(item) > 256 for item in also_heard or []):
                return fail("name_query_too_long")
            queries = [value.strip() for value in [spoken_name, *(also_heard or [])] if value.strip()]
            state["confirmed_officer_id"] = None
            state["search_attempts"] += 1
            found, match_type = self.find_officers(queries)
            matches = [self.public_officer(officer) for officer in found[:MAX_SEARCH_RESULTS]]
            state["last_match_ids"] = [row["officer_id"] for row in matches]
            return {
                "ok": True,
                "status": (
                    "multiple_matches" if len(found) > 1 else "one_match" if found else "no_match"
                ),
                "query": spoken_name,
                "queries": queries,
                "matches": matches,
                "match_count": len(found),
                "returned_count": len(matches),
                "truncated": len(found) > MAX_SEARCH_RESULTS,
                "match_type": match_type,
                "search_attempts": state["search_attempts"],
                "implementation": "directory_name_search",
            }

        return self._call(session_id, search)

    def confirm_loan_officer(self, session_id: str, officer_id: str) -> Document:
        def confirm(tx: Transaction, state: Document) -> Document:
            if not state["authenticated"]:
                return fail("authentication_required")
            if officer_id not in state["last_match_ids"]:
                return fail("officer_not_in_last_search")
            officer = self.officers[officer_id]
            if officer["status"] != "available":
                return fail("officer_unavailable")
            state["confirmed_officer_id"] = officer_id
            return {"ok": True, "confirmed": True, "officer": self.public_officer(officer)}

        return self._call(session_id, confirm)

    def transfer_to_officer(self, session_id: str, officer_id: str) -> Document:
        def transfer(tx: Transaction, state: Document) -> Document:
            if not state["authenticated"]:
                return fail("authentication_required")
            if state["confirmed_officer_id"] != officer_id or not officer_id:
                return fail("officer_not_confirmed")
            officer = self.officers[officer_id]
            if officer["status"] != "available":
                return fail("officer_unavailable")
            state["ended"], state["disposition"] = True, "transferred"
            return {
                "ok": True,
                "transferred": True,
                "simulated": True,
                "disposition": "transferred",
                "officer": self.public_officer(officer),
            }

        return self._call(session_id, transfer)

    def end_call(self, session_id: str, reason: str) -> Document:
        if reason not in END_REASONS:
            return fail("unknown_reason", allowed=sorted(END_REASONS))

        def end(tx: Transaction, state: Document) -> Document:
            if state["ended"]:
                return {
                    "ok": True, "ended": True, "idempotent": True,
                    "disposition": state["disposition"],
                }
            state["otp_digits"] = ""
            state["ended"], state["disposition"] = True, reason
            return {"ok": True, "ended": True, "disposition": reason}

        return self._call(session_id, end, allow_ended=True)
