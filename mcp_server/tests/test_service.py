from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from contoso_mcp.service import BankService, DEMO_CODE, MAX_SEARCH_RESULTS, OTP_TTL
from contoso_mcp.storage import SqliteStore


ROOT = Path(__file__).resolve().parents[2]
CUSTOMERS = ROOT / "mcp_server" / "data" / "customers.json"
DIRECTORY = ROOT / "data" / "officers_20k.json"


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.db"
        self.now = 1_000_000.0
        self.next_code = "00123456"
        self.service = self.make_service()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_service(self) -> BankService:
        return BankService(
            SqliteStore(self.path), DIRECTORY, CUSTOMERS,
            clock=lambda: self.now, code_factory=lambda: self.next_code,
        )

    def start(self) -> str:
        return self.service.start_call()["session_id"]

    def authenticated(self) -> str:
        session = self.start()
        self.assertTrue(self.service.verify_caller_otp(session, DEMO_CODE)["authenticated"])
        return session

    def test_full_routing_flow_and_public_fields(self) -> None:
        session = self.authenticated()
        selected = self.service.search_loan_officer(session, "Michael Brennan")["matches"][0]
        self.assertEqual(selected["full_name"], "Michael Brennan")
        self.assertEqual(selected["status"], "available")
        self.assertNotIn("extension", selected)
        self.assertNotIn("nmls_id", selected)
        officer_id = selected["officer_id"]
        self.assertEqual(
            self.service.transfer_to_officer(session, officer_id)["error"],
            "officer_not_confirmed",
        )
        self.assertTrue(self.service.confirm_loan_officer(session, officer_id)["confirmed"])
        transfer = self.service.transfer_to_officer(session, officer_id)
        self.assertTrue(transfer["simulated"])
        self.assertTrue(transfer["transferred"])
        self.assertEqual(self.service.search_loan_officer(session, "anything")["error"],
                         "call_already_ended")
        self.assertEqual(self.service.end_call(session, "other")["disposition"], "transferred")

    def test_directory_has_twenty_thousand_unique_officers(self) -> None:
        self.assertEqual(len(self.service.officers), 20_000)
        self.assertEqual(len(self.service.available), 13_962)

    def test_requested_names_resolve_across_the_full_directory_without_random_selection(self) -> None:
        session = self.authenticated()
        for officer_id in ("lo-00001", "lo-00005", "lo-10000", "lo-19997", "lo-20000"):
            expected = self.service.officers[officer_id]
            with self.subTest(officer=expected["full_name"]):
                result = self.service.search_loan_officer(session, expected["full_name"])
                self.assertEqual(result["status"], "one_match")
                self.assertEqual(result["match_type"], "exact")
                self.assertEqual(result["match_count"], 1)
                self.assertEqual(result["matches"][0]["officer_id"], officer_id)
                repeated = self.service.search_loan_officer(session, expected["full_name"])
                self.assertEqual(repeated["matches"], result["matches"])

    def test_case_punctuation_aliases_and_reordered_names(self) -> None:
        session = self.authenticated()
        for query, officer_id, match_type in (
            ("  MICHAEL   BRENNAN. ", "lo-00001", "exact"),
            ("Mich Brennan", "lo-00002", "exact"),
            ("Brennan Michael", "lo-00001", "partial"),
        ):
            with self.subTest(query=query):
                result = self.service.search_loan_officer(session, query)
                self.assertEqual(result["match_type"], match_type)
                self.assertEqual([row["officer_id"] for row in result["matches"]], [officer_id])

    def test_close_spelling_returns_the_requested_person_not_an_arbitrary_officer(self) -> None:
        session = self.authenticated()
        result = self.service.search_loan_officer(session, "Micheal Brennan")
        self.assertEqual(result["match_type"], "fuzzy")
        self.assertEqual(result["status"], "one_match")
        self.assertEqual(result["matches"][0]["officer_id"], "lo-00001")
        surname = self.service.search_loan_officer(session, "Brenan")
        self.assertEqual(surname["match_type"], "fuzzy")
        self.assertEqual(surname["status"], "multiple_matches")
        self.assertTrue(all(
            row["full_name"].split()[-1] in {"Brennan", "Brendan"}
            for row in surname["matches"]
        ))

    def test_partial_matching_does_not_combine_tokens_from_different_aliases(self) -> None:
        result = self.service.search_loan_officer(self.authenticated(), "Michelle Mich Brennan")
        self.assertEqual(result["match_type"], "fuzzy")
        self.assertEqual(result["matches"][0]["officer_id"], "lo-00002")

    def test_partial_names_require_clarification_and_results_are_bounded(self) -> None:
        session = self.authenticated()
        result = self.service.search_loan_officer(session, "Brennan")
        expected = [row for row in self.service.officers.values()
                    if "Brennan" in row["full_name"].split()]
        self.assertEqual(result["status"], "multiple_matches")
        self.assertEqual(result["match_type"], "partial")
        self.assertEqual(result["match_count"], len(expected))
        self.assertEqual(result["returned_count"], MAX_SEARCH_RESULTS)
        self.assertEqual(len(result["matches"]), MAX_SEARCH_RESULTS)
        self.assertTrue(result["truncated"])
        self.assertTrue(all("Brennan" in row["full_name"] for row in result["matches"]))
        clarified = self.service.search_loan_officer(session, "Michelle Brennan")
        self.assertEqual(clarified["status"], "one_match")
        self.assertEqual(clarified["matches"][0]["officer_id"], "lo-00002")

    def test_unknown_and_empty_names_never_fall_back_to_an_available_officer(self) -> None:
        session = self.authenticated()
        for query in ("Qzxv Plmok", "a nonexistent name", "", " . -- "):
            with self.subTest(query=query):
                result = self.service.search_loan_officer(session, query)
                self.assertEqual(result["status"], "no_match")
                self.assertEqual(result["match_type"], "none")
                self.assertEqual(result["match_count"], 0)
                self.assertEqual(result["matches"], [])
                self.assertFalse(result["truncated"])

    def test_transcription_alternatives_are_searched_and_deduplicated(self) -> None:
        session = self.authenticated()
        result = self.service.search_loan_officer(
            session, "Qzxv Plmok", ["Michael Brennan", "Michael Brennan"]
        )
        self.assertEqual(result["status"], "one_match")
        self.assertEqual(result["matches"][0]["officer_id"], "lo-00001")
        ambiguous = self.service.search_loan_officer(
            session, "Michael Brennan", ["Priya Raghavan"]
        )
        self.assertEqual(ambiguous["status"], "multiple_matches")
        self.assertEqual({row["officer_id"] for row in ambiguous["matches"]},
                         {"lo-00001", "lo-00005"})

    def test_unavailable_exact_match_is_not_replaced_by_an_available_officer(self) -> None:
        session = self.authenticated()
        result = self.service.search_loan_officer(session, "Michael Brendan")
        self.assertEqual(result["status"], "one_match")
        self.assertEqual(result["match_type"], "exact")
        self.assertEqual(result["matches"][0]["officer_id"], "lo-00003")
        self.assertEqual(result["matches"][0]["status"], "out_of_office")
        self.assertEqual(
            self.service.confirm_loan_officer(session, "lo-00003")["error"],
            "officer_unavailable",
        )
        self.assertEqual(
            self.service.confirm_loan_officer(session, "lo-00001")["error"],
            "officer_not_in_last_search",
        )

    def test_pre_authentication_gates(self) -> None:
        session = self.start()
        for result in (
            self.service.search_loan_officer(session, "Michael Brennan"),
            self.service.confirm_loan_officer(session, "invented"),
            self.service.transfer_to_officer(session, "invented"),
        ):
            self.assertEqual(result["error"], "authentication_required")

    def test_partial_code_persists_across_service_recreation(self) -> None:
        session = self.start()
        result = self.service.verify_caller_otp(session, DEMO_CODE[:4])
        self.assertEqual(result["digits_missing"], 4)
        self.assertEqual(result["attempts_remaining"], 3)
        self.assertNotIn(DEMO_CODE[:4], json.dumps(result))
        recreated = self.make_service()
        result = recreated.verify_caller_otp(session, f"{DEMO_CODE[4:6]}-{DEMO_CODE[6:]}")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["customer"]["full_name"], "Avery Example")

    def test_three_complete_attempts_lock_out_durably(self) -> None:
        session = self.start()
        for remaining in (2, 1, 0):
            result = self.service.verify_caller_otp(session, "00000000")
            self.assertEqual(result["attempts_remaining"], remaining)
        self.assertTrue(result["locked_out"])
        self.assertEqual(self.make_service().verify_caller_otp(session, DEMO_CODE)["error"],
                         "locked_out")
        self.assertTrue(self.service.end_call(session, "authentication_failed")["ended"])

    def test_complete_reread_replaces_partial_buffer(self) -> None:
        session = self.start()
        self.service.verify_caller_otp(session, "111")
        self.assertTrue(self.service.verify_caller_otp(session, DEMO_CODE)["authenticated"])

    def test_invalid_format_does_not_disclose_or_consume_attempts(self) -> None:
        session = self.start()
        result = self.service.verify_caller_otp(session, "not digits")
        self.assertEqual(result["error"], "access_code_invalid_format")
        self.assertEqual(result["attempts_remaining"], 3)
        self.assertEqual(self.service.verify_caller_otp(session, "")["digits_missing"], 8)

    def test_issued_otp_is_candidate_bound_single_use_and_survives_restart(self) -> None:
        issued = self.service.issue_otp("cand-oakley-example")
        self.assertEqual(issued["otp"], "00123456")
        self.assertEqual(issued["expires_in_seconds"], OTP_TTL)
        recreated = self.make_service()
        first = recreated.verify_caller_otp(self.start(), "00123456")
        self.assertTrue(first["authenticated"])
        self.assertEqual(first["customer"]["full_name"], "Oakley Example")
        second = self.make_service().verify_caller_otp(self.start(), "00123456")
        self.assertFalse(second["authenticated"])
        self.assertEqual(second["status"], "consumed")
        self.assertNotIn(b"00123456", self.path.read_bytes())

    def test_expiry_boundary_and_candidate_code_replacement(self) -> None:
        self.service.issue_otp("cand-bailey-example")
        self.now += OTP_TTL
        self.assertEqual(self.service.verify_caller_otp(self.start(), self.next_code)["status"],
                         "expired")
        self.now += 1
        self.next_code = "12341234"
        self.service.issue_otp("cand-bailey-example")
        self.next_code = "23452345"
        self.service.issue_otp("cand-bailey-example")
        self.assertFalse(self.service.verify_caller_otp(self.start(), "12341234")["authenticated"])
        self.assertTrue(self.service.verify_caller_otp(self.start(), "23452345")["authenticated"])

    def test_only_one_concurrent_caller_can_consume_generated_code(self) -> None:
        self.service.issue_otp("cand-bailey-example")
        sessions = [self.start(), self.start()]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda session: self.service.verify_caller_otp(session, self.next_code),
                sessions,
            ))
        self.assertEqual(sum(result["authenticated"] for result in results), 1)

    def test_generated_otp_is_verified_by_a_fresh_python_process(self) -> None:
        self.service.issue_otp("cand-bailey-example")
        session = self.start()
        program = (
            "import json,sys; from pathlib import Path; "
            "from contoso_mcp.service import BankService; "
            "from contoso_mcp.storage import SqliteStore; "
            "v=json.load(sys.stdin); "
            "s=BankService(SqliteStore(Path(v['state'])),Path(v['directory']),Path(v['customers']),clock=lambda:v['now']); "
            "print(json.dumps(s.verify_caller_otp(v['session'],v['code'])))"
        )
        child = subprocess.run(
            [sys.executable, "-c", program],
            input=json.dumps({
                "state": str(self.path), "directory": str(DIRECTORY),
                "customers": str(CUSTOMERS), "session": session, "code": self.next_code,
                "now": self.now,
            }),
            capture_output=True, text=True, timeout=20, check=True,
        )
        self.assertTrue(json.loads(child.stdout)["authenticated"])
        self.assertEqual(self.service.verify_caller_otp(self.start(), self.next_code)["status"],
                         "consumed")

    def test_reserved_demo_identity_and_public_fixture_contract(self) -> None:
        directory = self.service.candidates()
        self.assertEqual(directory["permanent_demo"]["full_name"], "Avery Example")
        self.assertEqual(directory["permanent_demo"]["otp"], DEMO_CODE)
        self.assertEqual(len(directory["candidates"]), 20)
        self.assertTrue(all(
            row["fixture"] == "fictional" and row["full_name"].endswith(" Example")
            for row in directory["candidates"]
        ))
        self.assertEqual(
            self.service.verify_caller_otp(self.start(), DEMO_CODE)["customer"]["candidate_id"],
            "cand-avery-example",
        )
        self.next_code = DEMO_CODE
        with self.assertRaisesRegex(ValueError, "unique non-reserved"):
            self.service.issue_otp("cand-bailey-example")

    def test_confirmation_only_accepts_latest_search_and_search_invalidates_confirmation(self) -> None:
        session = self.authenticated()
        result = self.service.search_loan_officer(session, "Michael Brennan")
        officer = result["matches"][0]["officer_id"]
        self.assertEqual(self.service.confirm_loan_officer(session, "invented")["error"],
                         "officer_not_in_last_search")
        self.service.confirm_loan_officer(session, officer)
        self.service.search_loan_officer(session, "Priya Raghavan")
        self.assertEqual(self.service.transfer_to_officer(session, officer)["error"],
                         "officer_not_confirmed")
        self.assertEqual(self.service.confirm_loan_officer(session, officer)["error"],
                         "officer_not_in_last_search")
        selected = self.service.search_loan_officer(session, "Michael Brennan")["matches"][0]
        self.service.confirm_loan_officer(session, selected["officer_id"])
        self.service.search_loan_officer(session, "Qzxv Plmok")
        self.assertEqual(self.service.transfer_to_officer(session, officer)["error"],
                         "officer_not_confirmed")
        self.assertEqual(self.service.confirm_loan_officer(session, officer)["error"],
                         "officer_not_in_last_search")

    def test_closure_and_input_errors_are_explicit(self) -> None:
        self.assertEqual(self.service.issue_otp("not-a-customer")["error"], "unknown_candidate")
        self.assertEqual(self.service.verify_caller_otp("../bad", DEMO_CODE)["error"],
                         "unknown_session")
        session = self.authenticated()
        self.assertEqual(self.service.search_loan_officer(session, "")["status"], "no_match")
        self.assertEqual(self.service.search_loan_officer(session, "x", ["a", "b", "c"])["error"],
                         "too_many_alternatives")
        self.assertEqual(self.service.search_loan_officer(session, "x" * 257)["error"],
                         "name_query_too_long")
        self.assertEqual(self.service.end_call(session, "invalid")["error"], "unknown_reason")
        self.assertTrue(self.service.end_call(session, "caller_declined")["ended"])
        self.assertTrue(self.service.end_call(session, "caller_hung_up")["idempotent"])
        self.assertEqual(self.service.verify_caller_otp(session, DEMO_CODE)["error"],
                         "call_already_ended")


if __name__ == "__main__":
    unittest.main()
