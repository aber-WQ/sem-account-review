"""Meaningful regression checks; run with python -m unittest discover -s tests -v."""
import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sem_data.py"
spec = importlib.util.spec_from_file_location("sem_data", SCRIPT)
sem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sem)

PERIODS = [{"id": "W1", "start": "2026-10-05", "end": "2026-10-11"},
           {"id": "W2", "start": "2026-10-12", "end": "2026-10-18"}]
HEADERS = ["date", "campaign", "group", "kwid", "keyword", "query", "cost", "impressions", "clicks", "conversions"]
BASE = {"columns": {"date": "date", "campaign": "campaign", "ad_group": "group", "keyword_id": "kwid",
                    "keyword": "keyword", "search_term": "query", "cost": "cost", "impressions": "impressions",
                    "clicks": "clicks", "conversions": "conversions"},
        "constants": {"account_id": "001", "currency": "CNY", "conversion_event": "lead_form"},
        "grain": "search_relation"}


class DataToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def csv_table(self, rows, headers=HEADERS, name="input.csv"):
        path = self.root / name
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(rows)
        return sem.read_table(path)

    def mapping(self):
        return json.loads(json.dumps(BASE))

    def test_weighted_ratios_and_decimal_totals(self):
        table = self.csv_table([
            ["2026-10-05", "P", "G", "00012", "api", "api data", "0.10", 100, 10, 1],
            ["2026-10-06", "P", "G", "00012", "api", "api data", "0.20", 900, 20, 2]])
        result = sem.prepare_table(table, self.mapping(), PERIODS)
        total = result["summaries"]["period"][0]
        self.assertEqual(total["metrics"]["cost"]["exact_decimal"], "0.30")
        self.assertAlmostEqual(total["ratios"]["ctr"]["value"], 0.03)
        self.assertAlmostEqual(total["ratios"]["cpa"]["value"], 0.1)
        self.assertEqual(result["facts"][0]["dimensions"]["keyword_id"], "00012")
        self.assertEqual(len(result["facts"]), 2)
        self.assertEqual(len(result["summaries"]["relation"]), 1)

    def test_blank_is_not_zero_and_zero_cpa_is_null(self):
        table = self.csv_table([
            ["2026-10-05", "P", "G", "12", "api", "api data", 10, 100, 10, ""],
            ["2026-10-12", "P", "G", "12", "api", "api data", 20, 100, 10, 0]])
        result = sem.prepare_table(table, self.mapping(), PERIODS)
        first, second = result["summaries"]["period"]
        self.assertIsNone(first["metrics"]["conversions"]["value"])
        self.assertEqual(first["ratios"]["cpa"]["reason"], "incomplete_inputs")
        self.assertIsNone(second["ratios"]["cpa"]["value"])
        self.assertEqual(second["ratios"]["cpa"]["reason"], "zero_conversions")

    def test_partial_sum_is_explicit(self):
        table = self.csv_table([
            ["2026-10-05", "P", "G", "12", "api", "a", 10, 100, 10, 2],
            ["2026-10-06", "P", "G", "12", "api", "b", "", 100, 10, 1]])
        result = sem.prepare_table(table, self.mapping(), PERIODS)
        metric = result["summaries"]["period"][0]["metrics"]["cost"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["observed_sum"], 10)
        self.assertEqual(metric["missing_rows"], 1)
        self.assertFalse(metric["complete"])

    def test_keyword_move_keeps_object_but_separates_relation(self):
        table = self.csv_table([
            ["2026-10-05", "P", "old", "12", "api", "query", 10, 100, 10, 1],
            ["2026-10-12", "P", "new", "12", "api", "query", 20, 100, 10, 1]])
        first, second = sem.prepare_table(table, self.mapping(), PERIODS)["facts"]
        self.assertEqual(first["object_id"], second["object_id"])
        self.assertNotEqual(first["relation_id"], second["relation_id"])
        self.assertEqual([first["period"], second["period"]], ["W1", "W2"])
        self.assertEqual(first["source_refs"][0]["row"], 2)

    def test_keyword_relation_combines_match_and_trigger_details(self):
        table = self.csv_table([
            ["2026-10-05", "P", "G", "12", "api", "query", 10, 100, 10, 0, "exact", "keyword"],
            ["2026-10-05", "P", "G", "12", "api", "query", 20, 200, 20, 1, "broad", "expansion"]],
            HEADERS + ["match", "trigger"])
        mapping = self.mapping()
        mapping["columns"].update(actual_match="match", trigger_type="trigger")
        result = sem.prepare_table(table, mapping, PERIODS)
        first, second = result["facts"]
        self.assertEqual(first["relation_id"], second["relation_id"])
        self.assertNotEqual(first["dimensions"]["actual_match"], second["dimensions"]["actual_match"])
        self.assertEqual(len(result["summaries"]["relation"]), 1)
        summary = result["summaries"]["relation"][0]
        self.assertEqual(summary["metrics"]["cost"]["value"], 30)
        self.assertEqual(summary["metrics"]["conversions"]["value"], 1)

    def test_original_query_and_keyword_whitespace_is_preserved(self):
        table = self.csv_table([
            ["2026-10-05", "P", "G", "12", " api ", "query", 10, 100, 10, 0],
            ["2026-10-05", "P", "G", "12", "api", " query ", 20, 200, 20, 1]])
        result = sem.prepare_table(table, self.mapping(), PERIODS)
        first, second = result["facts"]
        self.assertEqual(first["dimensions"]["keyword"], " api ")
        self.assertEqual(second["dimensions"]["search_term"], " query ")
        self.assertEqual(first["object_id"], second["object_id"])
        self.assertNotEqual(first["relation_id"], second["relation_id"])
        self.assertEqual(len(result["summaries"]["relation"]), 2)

    def test_fact_identity_separates_periods_and_column_mapping(self):
        table = self.csv_table([["", "P", "G", "12", "api", "query", 10, 100, 10, 0, 20]], HEADERS + ["W2cost"])
        mapping = self.mapping()
        del mapping["columns"]["date"]
        mapping["constants"]["period"] = "W1"
        first = sem.prepare_table(table, mapping, PERIODS)["facts"][0]
        rerun = sem.prepare_table(table, mapping, PERIODS)["facts"][0]
        self.assertEqual(first["fact_id"], rerun["fact_id"])
        reordered = self.mapping()
        reordered["columns"] = dict(reversed(list(mapping["columns"].items())))
        reordered["constants"]["period"] = "W1"
        self.assertEqual(first["fact_id"], sem.prepare_table(table, reordered, PERIODS)["facts"][0]["fact_id"])
        mapping["constants"]["period"] = "W2"
        second = sem.prepare_table(table, mapping, PERIODS)["facts"][0]
        self.assertNotEqual(first["fact_id"], second["fact_id"])
        mapping["columns"]["cost"] = "W2cost"
        different_columns = sem.prepare_table(table, mapping, PERIODS)["facts"][0]
        self.assertNotEqual(second["fact_id"], different_columns["fact_id"])

    def test_non_keyword_traffic_is_separate(self):
        table = self.csv_table([["2026-10-05", "P", "G", "", "", "query", 10, 100, 10, 1]])
        fact = sem.prepare_table(table, self.mapping(), PERIODS)["facts"][0]
        self.assertEqual(fact["object_type"], "non_keyword_traffic")
        self.assertIsNone(fact["dimensions"]["keyword_id"])

    def test_no_date_requires_explicit_period_and_no_invented_second_week(self):
        table = self.csv_table([["", "P", "G", "12", "api", "query", 10, 100, 10, 1]])
        mapping = self.mapping()
        del mapping["columns"]["date"]
        with self.assertRaises(sem.DataError):
            sem.prepare_table(table, mapping, PERIODS)
        mapping["constants"]["period"] = "W2"
        result = sem.prepare_table(table, mapping, PERIODS)
        self.assertEqual(len(result["summaries"]["period"]), 1)
        self.assertEqual(result["summaries"]["period"][0]["period"], "W2")
        self.assertTrue(any(w["code"] == "periods_unobserved" for w in result["warnings"]))

    def test_period_overlap_and_outside_dates_fail(self):
        table = self.csv_table([["2026-10-01", "P", "G", "12", "api", "query", 10, 100, 10, 1]])
        with self.assertRaises(sem.DataError):
            sem.prepare_table(table, self.mapping(), PERIODS)
        overlap = PERIODS + [{"id": "W3", "start": "2026-10-10", "end": "2026-10-11"}]
        with self.assertRaises(sem.DataError):
            sem.validate_periods(overlap)

    def test_events_currencies_accounts_are_not_merged(self):
        headers = HEADERS + ["event", "currency", "account"]
        table = self.csv_table([
            ["2026-10-05", "P", "G", "12", "api", "q", 10, 100, 10, 1, "form", "CNY", "a"],
            ["2026-10-05", "P", "G", "12", "api", "q", 10, 100, 10, 2, "phone", "CNY", "a"],
            ["2026-10-05", "P", "G", "12", "api", "q", 10, 100, 10, 1, "form", "USD", "a"],
            ["2026-10-05", "P", "G", "12", "api", "q", 10, 100, 10, 1, "form", "CNY", "b"]], headers)
        mapping = self.mapping()
        mapping["constants"] = {}
        mapping["columns"].update(conversion_event="event", currency="currency", account_id="account")
        result = sem.prepare_table(table, mapping, PERIODS)
        self.assertEqual(len(result["summaries"]["period"]), 4)
        self.assertEqual([s["metrics"]["cost"]["value"] for s in result["summaries"]["period"]], [10] * 4)
        self.assertNotEqual(result["facts"][0]["object_id"], result["facts"][3]["object_id"])

    def test_aggregate_reports_do_not_create_query_relations(self):
        table = self.csv_table([["2026-10-05", "P", "G", "", "", "", 10, 100, 10, 1]])
        mapping = self.mapping()
        mapping["grain"] = "aggregate"
        for field in ("keyword", "keyword_id", "search_term"):
            del mapping["columns"][field]
        result = sem.prepare_table(table, mapping, PERIODS)
        self.assertFalse(result["capabilities"]["search_term_relationships"])
        self.assertEqual(result["summaries"]["relation"], [])
        self.assertEqual(result["facts"][0]["object_type"], "aggregate")

    def test_duplicates_are_preserved_and_totals_require_explicit_exclusion(self):
        row = ["2026-10-05", "P", "G", "12", "api", "query", 10, 100, 10, 1]
        table = self.csv_table([row, row, row])
        mapping = self.mapping()
        result = sem.prepare_table(table, mapping, PERIODS)
        self.assertEqual(result["summaries"]["period"][0]["metrics"]["cost"]["value"], 30)
        mapping.update(exclude_rows=[4], exclude_reason="Verified exporter total row")
        result = sem.prepare_table(table, mapping, PERIODS)
        self.assertEqual(result["summaries"]["period"][0]["metrics"]["cost"]["value"], 20)
        self.assertEqual(result["source"]["excluded_rows"], [4])

    def make_xlsx(self, cells, shared="", styles="", date1904=False):
        path = self.root / "cells.xlsx"
        namespace = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", f'<workbook {namespace} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><workbookPr date1904="{int(date1904)}"/><sheets><sheet name="原始数据" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet {namespace}><sheetData>{cells}</sheetData></worksheet>')
            if shared:
                archive.writestr("xl/sharedStrings.xml", f'<sst {namespace}>{shared}</sst>')
            if styles:
                archive.writestr("xl/styles.xml", f'<styleSheet {namespace}>{styles}</styleSheet>')
        return path

    def test_xlsx_text_ids_shared_inline_dates_and_cached_formula(self):
        path = self.make_xlsx(
            '<row r="1"><c r="A1" t="inlineStr"><is><t>日期</t></is></c><c r="B1" t="inlineStr"><is><t>ID</t></is></c><c r="C1" t="inlineStr"><is><t>消费</t></is></c></row>'
            '<row r="2"><c r="A2" s="1"><v>46200</v></c><c r="B2" t="s"><v>0</v></c><c r="C2"><f>1+2</f><v>3</v></c></row>',
            '<si><t>00012345678901234567</t></si>', '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>')
        table = sem.read_table(path)
        self.assertEqual(table["rows"][0]["cells"][1]["value"], "00012345678901234567")
        self.assertRegex(table["rows"][0]["cells"][0]["value"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(table["rows"][0]["cells"][2]["value"], "3")
        self.assertTrue(any(w["code"] == "formula_cache_unverified" for w in table["warnings"]))

    def test_xlsx_missing_cache_blocks_mapped_fact(self):
        path = self.make_xlsx('<row r="1"><c r="A1" t="inlineStr"><is><t>cost</t></is></c></row><row r="2"><c r="A2"><f>1+2</f></c></row>')
        table = sem.read_table(path)
        self.assertEqual(len(table["rows"]), 1)
        mapping = {"columns": {"cost": "cost"}, "constants": {"account_id": "a", "currency": "CNY", "period": "W1"}, "grain": "aggregate"}
        with self.assertRaisesRegex(sem.DataError, "formula_cache_missing"):
            sem.prepare_table(table, mapping, PERIODS)

    def test_xlsx_numeric_long_id_is_not_trusted(self):
        path = self.make_xlsx('<row r="1"><c r="A1" t="inlineStr"><is><t>id</t></is></c><c r="B1" t="inlineStr"><is><t>cost</t></is></c></row><row r="2"><c r="A2"><v>1234567890123456</v></c><c r="B2"><v>1</v></c></row>')
        mapping = {"columns": {"keyword_id": "id", "cost": "cost"}, "constants": {"account_id": "a", "currency": "CNY", "period": "W1"}, "grain": "aggregate"}
        with self.assertRaisesRegex(sem.DataError, "15 digits"):
            sem.prepare_table(sem.read_table(path), mapping, PERIODS)

    def test_excel_date_systems_and_fake_leap_day(self):
        self.assertEqual(sem.excel_date("1", False), "1900-01-01")
        self.assertEqual(sem.excel_date("61", False), "1900-03-01")
        self.assertEqual(sem.excel_date("0", True), "1904-01-01")
        with self.assertRaises(sem.DataError):
            sem.excel_date("60", False)

    def test_custom_xlsx_date_needs_explicit_serial_mapping(self):
        path = self.make_xlsx('<row r="1"><c r="A1" t="inlineStr"><is><t>date</t></is></c><c r="B1" t="inlineStr"><is><t>cost</t></is></c></row><row r="2"><c r="A2" s="1"><v>46200</v></c><c r="B2"><v>3</v></c></row>',
                              styles='<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy/mm/dd"/></numFmts><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>')
        table = sem.read_table(path)
        mapping = {"columns": {"date": "date", "cost": "cost"}, "constants": {"account_id": "a", "currency": "CNY"}, "grain": "aggregate"}
        periods = [{"id": "P", "start": "2026-01-01", "end": "2026-12-31"}]
        with self.assertRaisesRegex(sem.DataError, "date_encoding"):
            sem.prepare_table(table, mapping, periods)
        mapping["date_encoding"] = "excel_serial"
        result = sem.prepare_table(table, mapping, periods)
        self.assertEqual(result["facts"][0]["period"], "P")
        self.assertTrue(any(w["code"] == "custom_date_format" for w in result["warnings"]))

    def test_cvr_zero_clicks_and_fractional_conversions(self):
        table = self.csv_table([["2026-10-05", "P", "G", "12", "api", "query", "1.25", 0, 0, "0.5"]])
        summary = sem.prepare_table(table, self.mapping(), PERIODS)["summaries"]["period"][0]
        self.assertEqual(summary["metrics"]["conversions"]["value"], 0.5)
        self.assertEqual(summary["ratios"]["cpa"]["value"], 2.5)
        self.assertEqual(summary["ratios"]["conversion_rate"]["reason"], "zero_clicks")
        self.assertEqual(summary["ratios"]["ctr"]["reason"], "zero_impressions")

    def test_cli_end_to_end_and_no_overwrite(self):
        table = self.csv_table([["2026-10-05", "P", "G", "12", "api", "query", 10, 100, 10, 1]])
        mapping_path, periods_path, output = [self.root / name for name in ("mapping.json", "periods.json", "facts.json")]
        mapping_path.write_text(json.dumps(self.mapping()), encoding="utf-8")
        periods_path.write_text(json.dumps(PERIODS), encoding="utf-8")
        command = [sys.executable, str(SCRIPT), "prepare", table["file"], "--mapping", str(mapping_path), "--periods", str(periods_path), "--output", str(output)]
        first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["source"]["included_rows"], 1)
        second = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(second.returncode, 2)
        self.assertTrue(output.exists())

    def test_unconfirmed_scope_is_explicit_and_supported(self):
        table = self.csv_table([["2026-10-05", "P", "G", "12", "api", "query", 10, 100, 10, 1]])
        mapping = self.mapping()
        mapping["constants"].update(account_id="source_scope:file_set_1", currency="unknown", conversion_event="平台报告转化_定义待核验")
        mapping["field_status"] = {"account_id": "internal_scope", "currency": "unknown", "conversion_event": "unknown"}
        result = sem.prepare_table(table, mapping, PERIODS)
        self.assertEqual(sum(w["code"] == "unconfirmed_definition" for w in result["warnings"]), 3)


if __name__ == "__main__":
    unittest.main()
