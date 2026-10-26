#!/usr/bin/env python3
"""Read advertising exports without guessing metric meanings (Python stdlib only)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import posixpath
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import xml.etree.ElementTree as ET


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DATE_FORMAT_IDS = {14, 15, 16, 17, 22}
AMBIGUOUS_TIME_FORMAT_IDS = {18, 19, 20, 21, 45, 46, 47} | set(range(27, 37)) | set(range(50, 59))
DIMENSIONS = ("account_id", "campaign_id", "campaign", "ad_group_id", "ad_group",
              "keyword_id", "keyword", "search_term", "actual_match", "trigger_type",
              "conversion_event", "currency")
METRICS = ("cost", "impressions", "clicks", "conversions")
FIELDS = set(DIMENSIONS) | set(METRICS) | {"date", "period"}


class DataError(ValueError):
    pass


def warning(code, message, **context):
    return {"code": code, "message": message, **context}


def column_index(reference):
    match = re.match(r"([A-Z]+)", reference or "")
    if not match:
        raise DataError("XLSX cell has no usable cell reference.")
    result = 0
    for char in match.group(1):
        result = result * 26 + ord(char) - 64
    return result - 1


def excel_date(raw, date1904):
    serial = Decimal(raw)
    if not serial.is_finite() or serial < 0:
        raise DataError("Negative/non-finite Excel date serial is unsupported.")
    if not date1904 and int(serial) == 60:
        raise DataError("Excel serial 60 represents the fictitious date 1900-02-29.")
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    if not date1904 and serial < 60:
        base = datetime(1899, 12, 31)
    result = base + timedelta(microseconds=int(serial * 86400 * 1000000))
    return result.date().isoformat() if result.time() == datetime.min.time() else result.isoformat()


def read_table(path, sheet=None, encoding="utf-8-sig", delimiter=None, header_row=1):
    path = Path(path)
    if not isinstance(header_row, int) or isinstance(header_row, bool) or header_row < 1:
        raise DataError("header_row must be a positive physical row number.")
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        delim = delimiter if delimiter is not None else ("\t" if suffix == ".tsv" else ",")
        if len(delim) != 1:
            raise DataError("CSV delimiter must be one character.")
        records = []
        with path.open("r", encoding=encoding, newline="") as stream:
            reader = csv.reader(stream, delimiter=delim)
            previous_line = 0
            for values in reader:
                line_start = previous_line + 1
                previous_line = reader.line_num
                if line_start >= header_row:
                    records.append((line_start, [{"value": value, "kind": "text"} for value in values]))
        if not records or records[0][0] != header_row:
            raise DataError("The requested header row was not found (CSV rows use physical line starts).")
        return finish_table(path, None, records, [], [None])
    if suffix != ".xlsx":
        raise DataError("Supported inputs: .csv, .tsv and basic .xlsx. Export .xls/.xlsb as CSV first.")
    return read_xlsx(path, sheet, header_row)


def finish_table(path, sheet, records, warnings, sheets):
    if not records:
        raise DataError("No header/data rows found.")
    headers = [str(cell.get("value") or "").strip() for cell in records[0][1]]
    while headers and not headers[-1]:
        headers.pop()
    if not headers:
        raise DataError("Header row is empty.")
    if len(set(headers)) != len(headers) or any(not value for value in headers):
        warnings.append(warning("ambiguous_headers", "Headers contain duplicates or blanks; prepare requires unique nonblank headers."))
    rows = []
    for physical_row, cells in records[1:]:
        if not any(cell.get("value") not in (None, "") or cell.get("error") for cell in cells):
            continue
        if len(cells) > len(headers) and any(cell.get("value") not in (None, "") or cell.get("error") for cell in cells[len(headers):]):
            warnings.append(warning("unheaded_cells", "Data extends beyond the header; prepare must not silently omit it.", row=physical_row))
        padded = cells + [{"value": None, "kind": "blank"}] * max(0, len(headers) - len(cells))
        rows.append({"row": physical_row, "cells": padded[:len(headers)]})
    return {"file": str(Path(path).resolve()), "sheet": sheet, "sheets": sheets,
            "headers": headers, "rows": rows, "warnings": warnings}


def read_xlsx(path, sheet, header_row):
    warnings = []
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {}
        for relation in relations:
            if relation.get("TargetMode") == "External":
                continue
            target = relation.get("Target", "")
            targets[relation.get("Id")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        sheets = [node.get("name") for node in workbook.findall("s:sheets/s:sheet", NS)]
        if not sheets:
            raise DataError("Workbook has no supported standard-namespace worksheet entries.")
        if sheet is None and len(sheets) != 1:
            raise DataError("Workbook contains multiple sheets; specify --sheet (inspect) or mapping.sheet (prepare): " + ", ".join(sheets))
        sheet = sheet or sheets[0]
        chosen = next((node for node in workbook.findall("s:sheets/s:sheet", NS) if node.get("name") == sheet), None)
        if chosen is None:
            raise DataError("Sheet not found: " + str(sheet))
        sheet_path = targets.get(chosen.get("{" + REL_NS + "}id"))
        if not sheet_path:
            raise DataError("Sheet relationship is missing or external.")
        if chosen.get("state", "visible") != "visible":
            warnings.append(warning("hidden_sheet", "The selected sheet is hidden; it is still read in full."))
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) if not node.findall(".//s:t", NS) else "".join(t.text or "" for t in node.findall(".//s:t", NS)) for node in root]
        formats = {}
        styles = []
        if "xl/styles.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/styles.xml"))
            formats = {int(node.get("numFmtId")): node.get("formatCode", "") for node in root.findall("s:numFmts/s:numFmt", NS)}
            styles = [int(node.get("numFmtId", "0")) for node in root.findall("s:cellXfs/s:xf", NS)]
        props = workbook.find("s:workbookPr", NS)
        date1904 = props is not None and props.get("date1904", "0").lower() in ("1", "true")
        root = ET.fromstring(archive.read(sheet_path))
        if root.find("s:mergeCells", NS) is not None:
            warnings.append(warning("merged_cells", "Merged cells are not forward-filled; use a flat raw export before prepare."))
        if root.find("s:autoFilter", NS) is not None:
            warnings.append(warning("filter_present", "All physical rows are read, including rows hidden by filtering."))
        records = []
        formula_count = 0
        hidden_count = 0
        for row in root.findall("s:sheetData/s:row", NS):
            row_num = int(row.get("r", "0"))
            if row_num < header_row:
                continue
            if row.get("hidden") in ("1", "true"):
                hidden_count += 1
            cells = []
            for node in row.findall("s:c", NS):
                index = column_index(node.get("r"))
                while len(cells) <= index:
                    cells.append({"value": None, "kind": "blank"})
                cell = {"value": None, "kind": "blank", "reference": node.get("r")}
                kind = node.get("t", "n")
                value = node.find("s:v", NS)
                raw = value.text if value is not None else None
                formula = node.find("s:f", NS)
                if formula is not None:
                    formula_count += 1
                    cell["formula"] = formula.text or ""
                    if raw is None:
                        cell["error"] = "formula_cache_missing"
                        warnings.append(warning("formula_cache_missing", "Formula has no cached value; no formula evaluation is performed.", cell=node.get("r")))
                        cells[index] = cell
                        continue
                try:
                    if kind == "s":
                        cell.update(value=shared[int(raw)], kind="text")
                    elif kind == "inlineStr":
                        cell.update(value="".join(t.text or "" for t in node.findall("s:is//s:t", NS)), kind="text")
                    elif kind in ("str", "d"):
                        cell.update(value=raw, kind="date" if kind == "d" else "text")
                    elif kind == "b":
                        cell.update(value="TRUE" if raw == "1" else "FALSE", kind="boolean")
                    elif kind == "e":
                        cell.update(value=raw, kind="error", error="excel_cell_error")
                        warnings.append(warning("excel_cell_error", "Excel error value is not a usable fact.", cell=node.get("r"), value=raw))
                    elif raw is not None:
                        cell.update(value=raw, kind="number")
                        style_index = int(node.get("s", "0"))
                        num_format = styles[style_index] if style_index < len(styles) else 0
                        cell["number_format"] = formats.get(num_format, str(num_format))
                        if num_format in DATE_FORMAT_IDS:
                            cell.update(value=excel_date(raw, date1904), kind="date")
                        elif num_format in AMBIGUOUS_TIME_FORMAT_IDS:
                            warnings.append(warning("ambiguous_time_format", "Time-only or locale-dependent format is not interpreted as a report date; export ISO dates or explicitly confirm date_encoding=excel_serial.", cell=node.get("r"), format=num_format))
                        elif num_format in formats:
                            fmt = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", formats[num_format]).lower()
                            if re.search(r"[ymdhis]", fmt):
                                cell["custom_date_format"] = True
                                warnings.append(warning("custom_date_format", "Custom date/time format is not interpreted; supply mapping.date_encoding=excel_serial if this is a date column.", cell=node.get("r"), format=formats[num_format]))
                except (ValueError, IndexError, InvalidOperation, OverflowError, DataError) as exc:
                    cell["error"] = "unsupported_cell_value"
                    warnings.append(warning("unsupported_cell_value", str(exc), cell=node.get("r")))
                cells[index] = cell
            records.append((row_num, cells))
        if not records or records[0][0] != header_row:
            raise DataError("Requested header row is absent in the XLSX sheet.")
        if formula_count:
            warnings.append(warning("formula_cache_unverified", "Cached formula results may be stale. This tool never recalculates Excel formulas.", count=formula_count))
        if hidden_count:
            warnings.append(warning("hidden_rows_included", "Hidden rows are included, not silently removed.", count=hidden_count))
        table = finish_table(path, sheet, records, warnings, sheets)
        table["date1904"] = date1904
        return table


def inspect_table(table, samples=3):
    return {"format_version": "1.0", "file": table["file"], "sheet": table["sheet"],
            "available_sheets": table["sheets"], "row_count": len(table["rows"]),
            "columns": table["headers"], "warnings": table["warnings"],
            "samples": [{"source_row": row["row"], "values": {header: cell.get("value") for header, cell in zip(table["headers"], row["cells"])}}
                        for row in table["rows"][:samples]],
            "scope": "Physical nonempty data rows, not unique objects. Totals/subtotals are not automatically removed."}


def stable_id(prefix, *parts):
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return prefix + "_" + hashlib.sha256(encoded).hexdigest()[:24]


def decimal_value(value, field, row):
    if value is None or str(value).strip() == "":
        return None
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise DataError(f"Row {row}: {field}={value!r} is not a plain decimal; explicitly clean thousands separators/currency signs first.") from exc
    if not result.is_finite() or result < 0:
        raise DataError(f"Row {row}: {field} must be a finite nonnegative number; credits/adjustments require a separate documented process.")
    if field in ("impressions", "clicks") and result != result.to_integral_value():
        raise DataError(f"Row {row}: {field} must be an integer.")
    return result


def iso_day(value, fmt=None):
    text = str(value).strip()
    try:
        if fmt:
            return datetime.strptime(text, fmt).date()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        # Date-time conversion requires an explicit format so offsets do not silently shift days.
        raise ValueError()
    except ValueError as exc:
        raise DataError(f"Invalid/ambiguous date {value!r}; use ISO YYYY-MM-DD or an explicit date_format.") from exc


def validate_periods(periods):
    if not isinstance(periods, list) or not periods:
        raise DataError("periods must be a nonempty array.")
    result = []
    seen = set()
    for period in periods:
        if not isinstance(period, dict) or not all(key in period for key in ("id", "start", "end")):
            raise DataError("Each period requires id, start, end.")
        key = str(period["id"])
        if not key or key in seen:
            raise DataError("Period IDs must be unique and nonempty.")
        seen.add(key)
        start, end = iso_day(period["start"]), iso_day(period["end"])
        if start > end:
            raise DataError("Period start is after end: " + key)
        for other in result:
            if max(start, other["start"]) <= min(end, other["end"]):
                raise DataError("Periods overlap: " + key + " and " + other["id"])
        result.append({"id": key, "start": start, "end": end})
    return result


def json_number(value):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def sum_metrics(facts):
    result = {}
    for metric in METRICS:
        values = [fact["_metrics"][metric] for fact in facts]
        missing = sum(value is None for value in values)
        observed = sum((value for value in values if value is not None), Decimal(0))
        result[metric] = {"value": json_number(observed) if missing == 0 else None,
                          "observed_sum": json_number(observed) if missing < len(values) else None,
                          "complete": missing == 0, "missing_rows": missing,
                          "exact_decimal": str(observed) if missing == 0 else None}
    ratios = {}
    for name, numerator, denominator in (("ctr", "clicks", "impressions"), ("cpc", "cost", "clicks"),
                                         ("conversion_rate", "conversions", "clicks"), ("cpa", "cost", "conversions")):
        if not result[numerator]["complete"] or not result[denominator]["complete"]:
            value, reason = None, "incomplete_inputs"
        else:
            num = sum((fact["_metrics"][numerator] for fact in facts), Decimal(0))
            den = sum((fact["_metrics"][denominator] for fact in facts), Decimal(0))
            value, reason = (None, "zero_" + denominator) if den == 0 else (json_number(num / den), None)
        ratios[name] = {"value": value, "reason": reason}
    return {"metrics": result, "ratios": ratios}


def prepare_table(table, mapping, periods):
    if not isinstance(mapping, dict):
        raise DataError("mapping must be an object.")
    columns, constants = mapping.get("columns", {}), mapping.get("constants", {})
    if not isinstance(columns, dict) or not isinstance(constants, dict):
        raise DataError("mapping.columns and mapping.constants must be objects.")
    if set(columns) & set(constants):
        raise DataError("A field cannot be supplied by both columns and constants.")
    unknown = (set(columns) | set(constants)) - FIELDS
    if unknown:
        raise DataError("Unknown fields: " + ", ".join(sorted(unknown)))
    headers = table["headers"]
    if any(not header for header in headers) or len(set(headers)) != len(headers):
        raise DataError("prepare requires unique nonblank headers.")
    for field, header in columns.items():
        if header not in headers:
            raise DataError(f"Mapped column {header!r} for {field} does not exist.")
    if len(set(columns.values())) != len(columns):
        raise DataError("One input column cannot supply multiple semantic fields.")
    if any(item["code"] == "unheaded_cells" for item in table["warnings"]):
        raise DataError("Rows extend beyond the header; fix the export first.")
    if not set(METRICS) & (set(columns) | set(constants)):
        raise DataError("Map at least one metric explicitly.")
    if set(METRICS) & set(constants):
        raise DataError("Metrics must come from source columns, not constants that invent facts.")
    grain = mapping.get("grain")
    if grain not in ("search_relation", "aggregate"):
        raise DataError("mapping.grain must be search_relation or aggregate.")
    if "period" in columns:
        raise DataError("This version accepts dates or constants.period only; split aggregated multi-period files explicitly.")
    period_defs = validate_periods(periods)
    has_date = "date" in columns or "date" in constants
    fixed_period = constants.get("period")
    if has_date and fixed_period is not None:
        raise DataError("Specify date or constants.period, not both.")
    if not has_date and fixed_period is None:
        raise DataError("No date column: explicitly set constants.period to the known whole-report period. A filename is not date evidence.")
    if fixed_period is not None and str(fixed_period) not in {period["id"] for period in period_defs}:
        raise DataError("constants.period must match a supplied period ID.")
    warnings = list(table["warnings"])
    field_status = mapping.get("field_status", {})
    if not isinstance(field_status, dict) or any(value not in ("confirmed", "unknown", "internal_scope") for value in field_status.values()):
        raise DataError("field_status values must be confirmed, unknown or internal_scope.")
    for field, status in field_status.items():
        if status != "confirmed":
            warnings.append(warning("unconfirmed_definition", "Explicit source scope/unknown definition is retained, not treated as a confirmed platform business definition.", field=field, status=status))
    facts = []
    mapping_fingerprint = stable_id("mapping", sorted(columns.items()))
    input_columns = {field: headers.index(header) for field, header in columns.items()}
    ignored_rows = mapping.get("exclude_rows", [])
    if not isinstance(ignored_rows, list) or any(not isinstance(row, int) or isinstance(row, bool) for row in ignored_rows):
        raise DataError("exclude_rows must be explicit integer physical row numbers.")
    existing_rows = {row["row"] for row in table["rows"]}
    if set(ignored_rows) - existing_rows:
        raise DataError("exclude_rows contains rows not present in the input.")
    if ignored_rows and not mapping.get("exclude_reason"):
        raise DataError("exclude_rows requires exclude_reason (for example, exporter totals verified manually).")
    for row in table["rows"]:
        if row["row"] in ignored_rows:
            continue
        values = dict(constants)
        for field, index in input_columns.items():
            cell = row["cells"][index]
            if cell.get("error"):
                raise DataError(f"Row {row['row']}, {field}: {cell['error']}; no blank/zero substitution is allowed.")
            value = cell.get("value")
            if field.endswith("_id") and value not in (None, "") and cell.get("kind") == "number":
                try:
                    numeric = Decimal(str(value))
                    if numeric != numeric.to_integral_value() or not numeric.is_finite():
                        raise DataError(f"Row {row['row']}: numeric ID is not an integer.")
                    value = format(numeric, "f").split(".")[0]
                    if len(value.lstrip("-0")) > 15:
                        raise DataError(f"Row {row['row']}: numeric {field} exceeds 15 digits; Excel may have lost precision. Re-export IDs as text.")
                    warnings.append(warning("numeric_id", "ID read as its stored numeric value; leading zeros/display formatting cannot be recovered. Text IDs are preferred.", row=row["row"], field=field))
                except InvalidOperation as exc:
                    raise DataError(f"Row {row['row']}: invalid numeric ID.") from exc
            if field == "date" and cell.get("kind") == "number":
                if mapping.get("date_encoding") != "excel_serial":
                    raise DataError(f"Row {row['row']}: numeric date needs explicit date_encoding=excel_serial.")
                value = excel_date(value, bool(table.get("date1904", False)))
            values[field] = value
        dims = {}
        for field in DIMENSIONS:
            value = str(values[field]) if values.get(field) is not None else None
            # Original keyword/query text is part of the relationship evidence. Do
            # not silently merge variants through whitespace/case normalization.
            dims[field] = (value if field in ("keyword", "search_term") else value.strip()) if value is not None and value.strip() else None
        if not dims["account_id"]:
            raise DataError(f"Row {row['row']}: account_id is required (column or explicit constant).")
        if "cost" in columns and not dims["currency"]:
            raise DataError(f"Row {row['row']}: currency is required when cost is mapped.")
        if "conversions" in columns and not dims["conversion_event"]:
            raise DataError(f"Row {row['row']}: conversion_event is required when conversions is mapped.")
        day = iso_day(values.get("date"), mapping.get("date_format")) if has_date else None
        if day:
            matching = [period["id"] for period in period_defs if period["start"] <= day <= period["end"]]
            if len(matching) != 1:
                raise DataError(f"Row {row['row']}: date {day} is outside the specified periods; no silent exclusion.")
            period_id = matching[0]
        else:
            period_id = str(fixed_period)
        campaign = ("id", dims["campaign_id"]) if dims["campaign_id"] else ("name", dims["campaign"])
        group = ("id", dims["ad_group_id"]) if dims["ad_group_id"] else ("name", dims["ad_group"])
        if grain == "search_relation" and (not campaign[1] or not group[1] or not dims["search_term"]):
            raise DataError(f"Row {row['row']}: search_relation needs campaign, ad_group and search_term; use aggregate only for actual aggregate reports.")
        if dims["keyword_id"]:
            object_type, identification = "keyword", "account_keyword_id"
            object_id = stable_id("kw", dims["account_id"], dims["keyword_id"])
        elif dims["keyword"]:
            if not campaign[1] or not group[1]:
                raise DataError(f"Row {row['row']}: keyword without ID needs campaign and ad_group for a bounded fallback identity.")
            object_type, identification = "keyword", "fallback_names_match_unverified"
            object_id = stable_id("kw_fallback", dims["account_id"], campaign, group, dims["keyword"], dims["actual_match"])
        elif grain == "search_relation":
            object_type, identification = "non_keyword_traffic", "reported_context_only"
            object_id = stable_id("non_kw", dims["account_id"], campaign, group, dims["trigger_type"])
        else:
            object_type, identification = "aggregate", "reported_dimensions_only"
            object_id = stable_id("aggregate", dims["account_id"], campaign, group, dims["trigger_type"])
        relation_id = stable_id("rel", dims["account_id"], campaign, group, object_id,
                                dims["search_term"]) if grain == "search_relation" else None
        metrics = {metric: decimal_value(values.get(metric), metric, row["row"]) for metric in METRICS}
        source = {"file": table["file"], "sheet": table["sheet"], "row": row["row"]}
        facts.append({"fact_id": stable_id("fact", source, period_id, mapping_fingerprint), "period": period_id,
                      "date": day.isoformat() if day else None, "grain": grain,
                      "object_id": object_id, "object_type": object_type, "identification": identification,
                      "relation_id": relation_id, "dimensions": dims,
                      "metrics": {metric: json_number(value) for metric, value in metrics.items()},
                      "metric_decimal": {metric: str(value) if value is not None else None for metric, value in metrics.items()},
                      "raw_values": values, "source_refs": [source], "_metrics": metrics})
    if not facts:
        raise DataError("No fact rows remain.")
    # Every observed row remains a fact. No inferred deduplication, no synthetic zero-period rows.
    def aggregate_by(level):
        buckets = defaultdict(list)
        for fact in facts:
            dims = fact["dimensions"]
            identity = fact["relation_id"] if level == "relation" else fact["object_id"] if level == "object" else None
            if level == "relation" and identity is None:
                continue
            key = (fact["period"], dims["account_id"], dims["currency"], dims["conversion_event"], identity)
            buckets[key].append(fact)
        output = []
        for key, group_facts in buckets.items():
            entry = {"period": key[0], "account_id": key[1], "currency": key[2],
                     "conversion_event": key[3], "row_count": len(group_facts), **sum_metrics(group_facts)}
            if level != "period":
                entry[level + "_id"] = key[4]
                entry["object_type"] = group_facts[0]["object_type"]
            entry["source_refs"] = [ref for fact in group_facts for ref in fact["source_refs"]]
            output.append(entry)
        return output
    summaries = {"period": aggregate_by("period"), "object": aggregate_by("object"), "relation": aggregate_by("relation")}
    partitions = {(fact["dimensions"]["account_id"], fact["dimensions"]["currency"], fact["dimensions"]["conversion_event"]) for fact in facts}
    if len(partitions) > 1:
        warnings.append(warning("multiple_metric_partitions", "Accounts/currencies/conversion events are summarized separately. Never sum their cost/clicks across event rows without source-grain proof.", count=len(partitions)))
    if grain == "aggregate":
        warnings.append(warning("aggregate_only", "No search-term relationship analysis is produced; aggregate rows cannot establish full search-term intent or negatives."))
    absent = [period["id"] for period in period_defs if period["id"] not in {fact["period"] for fact in facts}]
    if absent:
        warnings.append(warning("periods_unobserved", "No input facts for these periods; absence is not zero activity.", periods=absent))
    if any(fact["identification"] == "fallback_names_match_unverified" for fact in facts):
        warnings.append(warning("fallback_keyword_identity", "Keyword IDs were unavailable for some rows; fallback identities cannot reliably follow moves/renames or establish full account inventory."))
    for fact in facts:
        fact.pop("_metrics")
    return {"format_version": "1.0", "source": {"file": table["file"], "sheet": table["sheet"],
            "physical_data_rows": len(table["rows"]), "included_rows": len(facts),
            "excluded_rows": ignored_rows, "exclude_reason": mapping.get("exclude_reason")},
            "mapping": mapping, "mapping_fingerprint": mapping_fingerprint, "periods": periods, "grain": grain,
            "capabilities": {"search_term_relationships": grain == "search_relation", "semantic_classification": False,
                             "formula_recalculation": False, "complete_account_inventory": False},
            "facts": facts, "summaries": summaries, "warnings": warnings,
            "notes": ["Facts preserve source rows; no automatic deduplication or subtotal detection.",
                      "Rates are ratios of complete summed inputs; CTR/CVR are fractions, not percent points.",
                      "A report-visible object is not proof of a complete account asset list.",
                      "Semantic judgments and platform execution settings require separate evidence."]}


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def write_json(result, output):
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output:
        # Avoid silently replacing a prior analysis or user source.
        with Path(output).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    else:
        print(payload, end="")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect", help="Read-only schema, warnings and small samples")
    inspect_parser.add_argument("input")
    inspect_parser.add_argument("--sheet")
    inspect_parser.add_argument("--encoding", default="utf-8-sig")
    inspect_parser.add_argument("--delimiter")
    inspect_parser.add_argument("--header-row", type=int, default=1)
    inspect_parser.add_argument("--samples", type=int, default=3)
    inspect_parser.add_argument("--output")
    prepare_parser = sub.add_parser("prepare", help="Normalize explicitly mapped source facts and metric partitions")
    prepare_parser.add_argument("input")
    prepare_parser.add_argument("--mapping", required=True)
    prepare_parser.add_argument("--periods", required=True)
    prepare_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            if args.samples < 0:
                raise DataError("--samples must be nonnegative.")
            table = read_table(args.input, args.sheet, args.encoding, args.delimiter, args.header_row)
            result = inspect_table(table, args.samples)
        else:
            mapping = read_json(args.mapping)
            if not isinstance(mapping, dict):
                raise DataError("mapping must be an object.")
            table = read_table(args.input, mapping.get("sheet"), mapping.get("encoding", "utf-8-sig"),
                               mapping.get("delimiter"), mapping.get("header_row", 1))
            result = prepare_table(table, mapping, read_json(args.periods))
        write_json(result, args.output)
    except (ValueError, TypeError, OSError, UnicodeError, csv.Error, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "status": "not_prepared"}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
