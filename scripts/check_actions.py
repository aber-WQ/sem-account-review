"""Validate declared action/relationship consistency, not SEM semantic truth.

Python standard library only. No network access or account mutations.
"""
import argparse
import json
from pathlib import Path

SHEETS = {"scope_change": "05", "keyword_change": "06", "negative": "07", "new_keyword": "08"}
STATUSES = {"executable", "verify", "observe", "rejected"}
RECORD_TYPES = {"change", "verification", "observation"}


def validate(bundle):
    errors = []
    def fail(code, where, message):
        errors.append({"code": code, "where": where, "message": message})

    if not isinstance(bundle, dict):
        return {"passed": False, "errors": [{"code": "TYPE", "where": "$", "message": "Top level must be an object"}]}
    if bundle.get("schema_version") != "1.0":
        fail("VERSION", "$", "schema_version must be 1.0")
    relations, actions, priorities = (bundle.get(k) for k in ("relations", "actions", "priority_action_ids"))
    if not all(isinstance(x, list) for x in (relations, actions, priorities)):
        fail("TYPE", "$", "relations, actions and priority_action_ids must be arrays")
        return {"passed": False, "errors": errors}

    def index_records(records, key, label):
        indexed = {}
        for i, row in enumerate(records):
            where = f"{label}[{i}]"
            if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key].strip():
                fail("ID", where, f"Nonempty {key} required")
                continue
            if row[key] in indexed:
                fail("DUPLICATE", where, f"Duplicate {key}: {row[key]}")
            indexed[row[key]] = row
        return indexed

    rels = index_records(relations, "relationship_id", "relations")
    acts = index_records(actions, "action_id", "actions")

    def ids(row, field, where):
        values = row.get(field)
        if not isinstance(values, list) or not all(isinstance(x, str) and x for x in values):
            fail("TYPE", where, f"{field} must be an array of IDs")
            return []
        if len(set(values)) != len(values):
            fail("DUPLICATE", where, f"Duplicate IDs in {field}")
        return values

    def active_change(action):
        return action.get("status") == "executable" and action.get("record_type") == "change"

    sources, covers, deps, links, atomic_targets = {}, {}, {}, {}, {}
    for rid, row in rels.items():
        if row.get("diagnosis") not in {"D1", "D2", "D3", "D4", "D5"}:
            fail("ENUM", rid, "Invalid diagnosis")
        if type(row.get("retain_in_scope")) is not bool:
            fail("TYPE", rid, "retain_in_scope must be a boolean")
        if row.get("current_negative_state") not in {"none", "pending", "executable"}:
            fail("ENUM", rid, "Invalid current_negative_state")
        links[rid] = ids(row, "current_action_ids", rid)
        for aid in links[rid]:
            if aid not in acts:
                fail("REFERENCE", rid, f"Unknown action {aid}")

    for aid, action in acts.items():
        kind = action.get("action_type")
        if kind not in SHEETS:
            fail("ENUM", aid, "Invalid action_type")
        elif action.get("origin_sheet") != SHEETS[kind]:
            fail("SHEET", aid, "Action belongs to a different primary sheet")
        if action.get("status") not in STATUSES or action.get("record_type") not in RECORD_TYPES:
            fail("ENUM", aid, "Invalid status or record_type")
        if action.get("priority") not in {"P1", "P2", "P3"}:
            fail("ENUM", aid, "Invalid priority")
        target = action.get("target")
        if not isinstance(target, dict):
            target = {}
        for field in ("account_id", "level", "scope_id"):
            if not isinstance(target.get(field), str) or not target[field].strip():
                fail("TARGET", aid, f"target.{field} is required")
        sources[aid] = ids(action, "source_relationship_ids", aid)
        covers[aid] = ids(action, "covered_relationship_ids", aid)
        deps[aid] = ids(action, "dependencies", aid)
        for rid in set(sources[aid] + covers[aid]):
            if rid not in rels:
                fail("REFERENCE", aid, f"Unknown relationship {rid}")
        for parent in deps[aid]:
            if parent not in acts:
                fail("REFERENCE", aid, f"Unknown dependency {parent}")
            elif active_change(action) and acts[parent].get("status") != "executable":
                fail("DEPENDENCY", aid, f"Dependency {parent} is not ready; change must remain pending")
        if not active_change(action):
            continue
        evidence = action.get("evidence_refs")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(x, str) and x.strip() for x in evidence):
            fail("EVIDENCE", aid, "Executable change requires evidence references")
        required = {"object_verified", "scope_verified", "conflicts_resolved"}
        if kind == "negative":
            required |= {"platform_semantics_verified", "assets_checked"}
        elif kind == "new_keyword":
            required |= {"capability_supported", "business_allowed", "campaign_scope_allowed", "assets_checked"}
        else:
            required |= {"current_setting_verified", "operation_supported"}
        declared = action.get("checks", {})
        if not isinstance(declared, dict):
            declared = {}
        for field in sorted(required):
            if declared.get(field) is not True:
                fail("PREREQUISITE", aid, f"Necessary check not passed: {field}")
        if kind in {"negative", "new_keyword"}:
            for field in ("text", "match_type"):
                if not isinstance(target.get(field), str) or not target[field].strip():
                    fail("TARGET", aid, f"target.{field} is required")
            if not sources[aid]:
                fail("SOURCE", aid, "A search-derived executable candidate needs source relationships")
        else:
            if not target.get("field") or "value" not in target or target["value"] is None or "current_value" not in action or action["current_value"] is None:
                fail("TARGET", aid, "Setting change needs a field, explicit current_value and target value")
        if kind in SHEETS:
            identity = [kind, target.get("account_id"), target.get("level"), target.get("scope_id")]
            identity += [target.get("text"), target.get("match_type")] if kind in {"negative", "new_keyword"} else [target.get("field")]
            key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
            value = json.dumps(target.get("value"), ensure_ascii=False, sort_keys=True)
            if key in atomic_targets:
                previous_id, previous_value = atomic_targets[key]
                code = "TARGET_CONFLICT" if previous_value != value else "DUPLICATE_ACTION"
                fail(code, aid, f"Same atomic target already addressed by {previous_id}")
            else:
                atomic_targets[key] = (aid, value)
        if kind == "negative":
            if not covers[aid]:
                fail("COVERAGE", aid, "Executable negative needs a known coverage set")
            for rid in covers[aid]:
                row = rels.get(rid)
                if row is None:
                    continue
                if row.get("retain_in_scope") is True:
                    fail("CONFLICT", aid, f"Negative covers retained relationship {rid}")
                if row.get("current_negative_state") != "executable" or aid not in links[rid]:
                    fail("BACKWRITE", rid, f"Missing executable coverage backwrite for {aid}")
        if kind == "new_keyword":
            for rid in sources[aid]:
                if rels.get(rid, {}).get("diagnosis") in {"D1", "D2", "D5"}:
                    fail("NEW_SOURCE", aid, f"Source {rid} is excluded or has decisive unresolved issues")

    for rid, row in rels.items():
        negative_links = [acts[a] for a in links[rid] if a in acts and acts[a].get("action_type") == "negative" and acts[a].get("record_type") == "change"]
        ready = any(active_change(a) and rid in covers.get(a["action_id"], []) for a in negative_links)
        pending = any(a.get("status") == "verify" and rid in set(sources.get(a["action_id"], []) + covers.get(a["action_id"], [])) for a in negative_links)
        expected = "executable" if ready else "pending" if pending else "none"
        if row.get("current_negative_state") != expected:
            fail("STATE", rid, f"Negative state must be {expected} from final ledger")

    visited, visiting = set(), set()
    def visit(aid):
        if aid in visiting:
            fail("CYCLE", aid, "Action dependency cycle")
            return
        if aid in visited:
            return
        visiting.add(aid)
        for parent in deps.get(aid, []):
            if parent in acts:
                visit(parent)
        visiting.remove(aid)
        visited.add(aid)
    for aid in acts:
        visit(aid)

    if not all(isinstance(x, str) for x in priorities):
        fail("TYPE", "priority_action_ids", "IDs must be strings")
    else:
        if len(set(priorities)) != len(priorities) or len(priorities) > 30:
            fail("PRIORITY", "priority_action_ids", "Use unique actions, maximum 30")
        order = {aid: i for i, aid in enumerate(priorities)}
        for aid in priorities:
            if aid not in acts:
                fail("REFERENCE", "priority_action_ids", f"Unknown action {aid}")
                continue
            if acts[aid].get("status") == "rejected":
                fail("PRIORITY", aid, "Rejected candidate cannot appear in priority view")
            for parent in deps.get(aid, []):
                if parent in order and order[parent] >= order[aid]:
                    fail("ORDER", aid, f"Dependency {parent} must appear before this action")
    return {"passed": not errors, "errors": errors,
            "counts": {"relations": len(relations), "actions": len(actions), "priorities": len(priorities)},
            "limits": "Checks declared structure and consistency only; does not establish semantic accuracy, platform matching, source truth or completeness."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.bundle.resolve():
        parser.error("Output must not overwrite the input bundle")
    try:
        result = validate(json.loads(args.bundle.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        result = {"passed": False, "errors": [{"code": "INPUT", "where": str(args.bundle), "message": str(exc)}]}
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
