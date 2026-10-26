import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_actions import validate


def fixture():
    return {
        "schema_version": "1.0",
        "relations": [
            {"relationship_id": "r1", "diagnosis": "D1", "retain_in_scope": False,
             "current_negative_state": "executable", "current_action_ids": ["n1"]},
            {"relationship_id": "r2", "diagnosis": "D4", "retain_in_scope": False,
             "current_negative_state": "executable", "current_action_ids": ["n1"]},
        ],
        "actions": [{"action_id": "n1", "action_type": "negative", "record_type": "change",
                     "status": "executable", "priority": "P2", "origin_sheet": "07",
                     "target": {"account_id": "a", "level": "ad_group", "scope_id": "g", "text": "term", "match_type": "verified_platform_exact"},
                     "source_relationship_ids": ["r1"], "covered_relationship_ids": ["r1", "r2"],
                     "dependencies": [], "evidence_refs": ["synthetic_scope_review"],
                     "checks": {"object_verified": True, "scope_verified": True, "conflicts_resolved": True,
                                "platform_semantics_verified": True, "assets_checked": True}}],
        "priority_action_ids": ["n1"],
    }


class ActionTests(unittest.TestCase):
    def codes(self, bundle):
        return {row["code"] for row in validate(bundle)["errors"]}

    def test_all_coverage_backwritten(self):
        self.assertTrue(validate(fixture())["passed"])

    def test_non_source_retained_relationship_detected(self):
        bundle = fixture()
        bundle["relations"][1]["retain_in_scope"] = True
        self.assertIn("CONFLICT", self.codes(bundle))

    def test_non_source_missing_backwrite(self):
        bundle = fixture()
        bundle["relations"][1]["current_action_ids"] = []
        self.assertIn("BACKWRITE", self.codes(bundle))

    def test_rejected_action_cannot_leave_executable_state(self):
        bundle = fixture()
        bundle["actions"][0]["status"] = "rejected"
        self.assertIn("STATE", self.codes(bundle))
        self.assertIn("PRIORITY", self.codes(bundle))

    def test_pending_negative_is_not_executable(self):
        bundle = fixture()
        bundle["actions"][0]["status"] = "verify"
        for row in bundle["relations"]:
            row["current_negative_state"] = "pending"
        self.assertTrue(validate(bundle)["passed"])

    def test_verification_task_does_not_apply_negative(self):
        bundle = fixture()
        bundle["actions"][0]["record_type"] = "verification"
        for row in bundle["relations"]:
            row["current_negative_state"] = "none"
        self.assertTrue(validate(bundle)["passed"])

    def test_truthy_string_does_not_pass_check(self):
        bundle = fixture()
        bundle["actions"][0]["checks"]["assets_checked"] = "true"
        self.assertIn("PREREQUISITE", self.codes(bundle))

    def test_dependencies_and_order(self):
        bundle = fixture()
        second = copy.deepcopy(bundle["actions"][0])
        second.update(action_id="check", record_type="verification", dependencies=[])
        bundle["actions"].append(second)
        bundle["actions"][0]["dependencies"] = ["check"]
        bundle["priority_action_ids"] = ["n1", "check"]
        self.assertIn("ORDER", self.codes(bundle))
        bundle["priority_action_ids"] = ["check", "n1"]
        self.assertTrue(validate(bundle)["passed"])
        second["status"] = "verify"
        self.assertIn("DEPENDENCY", self.codes(bundle))

    def test_cycle(self):
        bundle = fixture()
        bundle["actions"][0]["dependencies"] = ["n1"]
        self.assertIn("CYCLE", self.codes(bundle))

    def test_new_keyword_from_excluded_source(self):
        bundle = fixture()
        action = bundle["actions"][0]
        action.update(action_type="new_keyword", origin_sheet="08", covered_relationship_ids=[])
        action["checks"]["capability_supported"] = True
        for row in bundle["relations"]:
            row["current_negative_state"] = "none"
        self.assertIn("NEW_SOURCE", self.codes(bundle))

    def test_group_setting_cannot_live_in_keyword_sheet(self):
        bundle = fixture()
        bundle["actions"][0].update(action_type="scope_change", origin_sheet="06", record_type="verification")
        for row in bundle["relations"]:
            row["current_negative_state"] = "none"
        self.assertIn("SHEET", self.codes(bundle))

    def test_new_keyword_obeys_business_and_campaign_scope(self):
        bundle = fixture()
        action = bundle["actions"][0]
        action.update(action_type="new_keyword", origin_sheet="08", covered_relationship_ids=[])
        action["checks"].update(capability_supported=True, business_allowed=True, campaign_scope_allowed=True)
        for row in bundle["relations"]:
            row.update(current_negative_state="none", diagnosis="D4")
        self.assertTrue(validate(bundle)["passed"])
        action["checks"]["campaign_scope_allowed"] = False
        self.assertIn("PREREQUISITE", self.codes(bundle))

    def test_empty_report_and_malformed_input(self):
        self.assertTrue(validate({"schema_version": "1.0", "relations": [], "actions": [], "priority_action_ids": []})["passed"])
        self.assertFalse(validate({})["passed"])

    def test_duplicate_atomic_actions(self):
        bundle = fixture()
        second = copy.deepcopy(bundle["actions"][0])
        second["action_id"] = "n2"
        bundle["actions"].append(second)
        for row in bundle["relations"]:
            row["current_action_ids"].append("n2")
        self.assertIn("DUPLICATE_ACTION", self.codes(bundle))

    def test_conflicting_values_for_same_setting(self):
        bundle = fixture()
        action = bundle["actions"][0]
        action.update(action_type="keyword_change", origin_sheet="06", covered_relationship_ids=[], current_value="active")
        action["target"] = {"account_id": "a", "level": "keyword", "scope_id": "k", "field": "state", "value": "paused"}
        action["checks"].update(current_setting_verified=True, operation_supported=True)
        second = copy.deepcopy(action)
        second["action_id"] = "restore"
        second["target"]["value"] = "active"
        bundle["actions"].append(second)
        for row in bundle["relations"]:
            row["current_negative_state"] = "none"
        self.assertIn("TARGET_CONFLICT", self.codes(bundle))


if __name__ == "__main__":
    unittest.main()
