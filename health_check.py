#!/usr/bin/env python3
"""
Solace Appliance Health Check Tool

Usage:
    python health_check.py <gather-diagnostics-folder>

Example:
    python health_check.py gather-diagnostics-20240101
"""

import sys
import os
import re
import io
import contextlib
import datetime
import urllib.request
from pathlib import Path

import json
import yaml


class _Tee:
    """Write to both the original stdout and a file simultaneously."""
    def __init__(self, filepath: Path):
        self._file = open(filepath, "w", encoding="utf-8")
        self._stdout = sys.stdout
    def write(self, data):
        self._file.write(data)
        self._stdout.write(data)
    def flush(self):
        self._file.flush()
        self._stdout.flush()
    def close(self):
        self._file.close()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_rules(rules_path: Path) -> dict:
    """Load healthcheck_rules.yaml."""
    with open(rules_path, "r") as f:
        return yaml.safe_load(f)


def load_troubleshooting_rules(path: Path) -> dict:
    """Load further_troubleshooting_rules.yaml. Returns empty dict if file is missing."""
    if not path.exists():
        return {}
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    return data.get("troubleshooting", {}) if data else {}


def resolve_folder(folder: Path) -> Path:
    """Resolve the actual diagnostics folder, handling the common case where
    the archive extracts into a same-named subfolder."""
    if not (folder / "cli-diagnostics.txt").exists():
        nested = folder / folder.name / "cli-diagnostics.txt"
        if nested.exists():
            return folder / folder.name
    return folder


def load_diagnostics(folder: Path) -> str:
    """Load cli-diagnostics.txt. Exits with an error if the file is missing."""
    diag_path = folder / "cli-diagnostics.txt"
    if not diag_path.exists():
        print(f"ERROR: cli-diagnostics.txt not found in '{folder}'.")
        print("  This file is required to perform the health check.")
        sys.exit(1)
    with open(diag_path, "r", errors="replace") as f:
        return f.read()


def load_logs(folder: Path) -> dict:
    """
    Load log files from <folder>/usr/sw/jail/logs/.
    Warns if command.log, debug.log, or event.log are missing.
    Numbered logs (e.g. event.log.1) are loaded silently if present.
    """
    logs_dir = folder / "usr" / "sw" / "jail" / "logs"
    logs = {}

    if not logs_dir.exists():
        print(f"[WARNING] Log directory not found: {logs_dir}")
        return logs

    primary_logs = ["command.log", "debug.log", "event.log", "system.log"]
    for log_name in primary_logs:
        log_path = logs_dir / log_name
        if log_path.exists():
            with open(log_path, "r", errors="replace") as f:
                logs[log_name] = f.read()
        else:
            print(f"[WARNING] {log_name} is missing from {logs_dir}")

    # Numbered rotated logs (event.log.1, event.log.2, …) — no warning if absent
    for entry in logs_dir.iterdir():
        if re.match(r".+\.log\.\d+$", entry.name):
            with open(entry, "r", errors="replace") as f:
                logs[entry.name] = f.read()

    return logs


# ---------------------------------------------------------------------------
# Command-output extraction
# ---------------------------------------------------------------------------

def extract_command_output(diagnostics: str, command: str) -> str | None:
    """
    Extract the CLI output for a specific command from cli-diagnostics.txt.

    Solace diagnostic files typically delimit sections with lines of
    repeated characters (=, -, #) surrounding the command name, e.g.:

        ########################################
        # show version
        ########################################
        ...output...

        ==========================================
        show hardware detail
        ==========================================
        ...output...

    Returns the matched section text, or None if the command section is not
    found.  Callers must handle None — the old full-file fallback is removed
    so that a missing section produces an explicit failure rather than silently
    running checks against unrelated content.
    """
    escaped = re.escape(command)
    sep = r"[-=#]{5,}"

    # Pattern A: Solace GD format — "# CLI command: COMMAND" with optional
    # additional "# Header: value" lines, all between hash-only separator lines.
    #   #################################################################
    #   # CLI command: show version
    #   # Host:        router1
    #   #################################################################
    #   <output>
    # Uses #{5,} (not the generic sep) so table border lines (------) inside
    # the content do not prematurely end the capture.
    hash_sep = r"#{5,}"
    pat_a = (
        rf"(?:{hash_sep})\s*\n"
        rf"\s*#\s*CLI command:\s*{escaped}\s*\n"
        rf"(?:\s*#[^\n]*\n)*"          # zero or more extra "# Key: val" header lines
        rf"\s*{hash_sep}\s*\n"
        rf"(.*?)"
        rf"(?=\n\s*{hash_sep}|\Z)"
    )
    m = re.search(pat_a, diagnostics, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Pattern B: separator / optional-"# " / bare command / separator / output
    #   ########################################
    #   # show version
    #   ########################################
    pat_b = (
        rf"(?:{sep})\s*\n"
        rf"\s*#?\s*{escaped}\s*\n"
        rf"\s*{sep}\s*\n"
        rf"(.*?)"
        rf"(?=\n\s*{sep}|\Z)"
    )
    m = re.search(pat_b, diagnostics, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Pattern C: command embedded inside separator line "### show version ###"
    pat_c = rf"(?:{sep})\s*{escaped}\s*(?:{sep})(.*?)(?={sep}|\Z)"
    m = re.search(pat_c, diagnostics, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return None


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def log_line_date(line: str):
    """Return the date parsed from the leading timestamp of a log line, or None."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", line)
    if not m:
        return None
    try:
        return datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def extract_log_timestamp(line: str) -> str:
    """Extract full timestamp including milliseconds and timezone from a log line."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)", line)
    return m.group(1) if m else ""


def extract_log_message(line: str) -> str:
    """Extract the meaningful message portion from a log line."""
    m = re.search(r"(SYSTEM_\S+.*)", line)
    if m:
        return m.group(1).strip()
    m = re.search(r"<[^>]+>\s*\S+\s+(.+)$", line)
    if m:
        return m.group(1).strip()
    return line.strip()


def log_line_datetime(line: str):
    """Return a timezone-aware datetime from the leading timestamp of a log line, or None."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})([+-]\d{4})?", line)
    if not m:
        return None
    ts = m.group(1) + (m.group(2) or "")
    sep = "T" if "T" in ts else " "
    fmt = f"%Y-%m-%d{sep}%H:%M:%S" + ("%z" if m.group(2) else "")
    try:
        dt = datetime.datetime.strptime(ts, fmt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except ValueError:
        return None


def find_latest_log_date(logs: dict) -> tuple:
    """
    Find the most recent date across all loaded log files by reading the last
    timestamped line in each file (logs are oldest-to-newest).
    Returns (reference_date, is_fallback).
    is_fallback is True when no timestamps were found and today's date is used.
    """
    latest = None
    for content in logs.values():
        for line in reversed(content.splitlines()):
            d = log_line_date(line.strip())
            if d:
                if latest is None or d > latest:
                    latest = d
                break  # only need the last timestamp per file
    if latest:
        return latest, False
    return datetime.date.today(), True


# ---------------------------------------------------------------------------
# Troubleshoot report (formerly troubleshoot_failures.py)
# ---------------------------------------------------------------------------

def _clean_message(msg: str) -> str:
    """Strip log source suffix like '(source: event.log.1)'."""
    return re.sub(r"\s*\(source:[^)]+\)", "", msg).strip()


def _print_troubleshoot_report(data: dict):
    fails = [r for r in data["results"] if r["status"] == "FAIL"]
    if not fails:
        return

    print("=== Health Check Troubleshoot Report ===\n")

    for result in fails:
        section = result["section"]
        description = result["description"]
        print(f"[{section}] FAIL - {description}")

        if section in ("1.1", "1.2"):
            for failure in result["failures"]:
                print(f"  -> Upgrade recommended: {_clean_message(failure['message'])}")
            print()
            continue

        for ctx in result.get("troubleshooting_context", []):
            matches = ctx.get("matches", [])
            correlated = ctx.get("correlated", [])
            if not matches and not correlated:
                continue
            print(f"  [{ctx['description']}]")
            for m in matches:
                print(f"    [GREP MATCH] Source: {m['source']} | Time: {m['timestamp']} {m.get('message', m['line'])}")
            if correlated:
                print("    [CORRELATED EVENTS]")
                for m in correlated:
                    print(f"    [GREP MATCH] Source: {m['source']} | Time: {m['timestamp']} {m.get('message', m['line'])}")

        seen = set()
        for failure in result["failures"]:
            msg = _clean_message(failure["message"])
            if msg in seen:
                continue
            seen.add(msg)
            print(f"  [FAIL] {msg}")
            for m in failure.get("matches", []):
                print(f"    [GREP MATCH] Source: {m['source']} | Time: {m['timestamp']} {m.get('message', m['line'])}")

        print()



# ---------------------------------------------------------------------------
# Troubleshooting steps
# ---------------------------------------------------------------------------

def run_troubleshooting_steps(section: str, logs: dict, reference_date: datetime.date, steps: list) -> list:
    """
    Run further troubleshooting grep steps for a failed section.
    Returns a list of context dicts:
      {"description": str, "matches": [...], "correlated": [...] (optional)}
    Each match/correlated entry: {"source": str, "timestamp": str, "line": str}
    """
    context = []

    for step in steps:
        patterns = step.get("patterns") or ([step["pattern"]] if "pattern" in step else [])
        max_age_days = step.get("max_age_days")
        correlate = step.get("correlate")

        cutoff = None
        if max_age_days and reference_date:
            cutoff = reference_date - datetime.timedelta(days=max_age_days)

        source_list = step.get("sources") or ([step["source"]] if "source" in step else [])

        def expand_sources(sources):
            expanded = []
            for src in sources:
                expanded.append(src)
                rotated = sorted(
                    (k for k in logs if re.match(rf"{re.escape(src)}\.\d+$", k)),
                    key=lambda k: int(k.rsplit(".", 1)[-1])
                )
                expanded.extend(rotated)
            return expanded

        matched = []
        for source in expand_sources(source_list):
            if source not in logs:
                continue
            for line in logs[source].splitlines():
                line = line.strip()
                if not line:
                    continue
                if cutoff:
                    line_date = log_line_date(line)
                    if line_date and line_date < cutoff:
                        continue
                if not patterns or any(re.search(p, line, re.IGNORECASE) for p in patterns):
                    matched.append({
                        "source": source,
                        "timestamp": extract_log_timestamp(line),
                        "line": line,
                        "message": extract_log_message(line),
                    })

        # If min_matches_per_pattern is set, skip unless every pattern meets the threshold
        min_per = step.get("min_matches_per_pattern")
        if min_per and patterns:
            if not all(
                sum(1 for m in matched if re.search(p, m["line"], re.IGNORECASE)) >= min_per
                for p in patterns
            ):
                continue

        entry = {"description": step.get("description", ""), "matches": matched}

        if correlate and matched:
            corr_source = correlate["source"]
            window = datetime.timedelta(minutes=correlate.get("window_minutes", 10))

            matched_dts = [dt for m in matched if (dt := log_line_datetime(m["line"]))]

            if matched_dts:
                correlated = []
                seen = set()
                for source in expand_sources([corr_source]):
                    if source not in logs:
                        continue
                    for line in logs[source].splitlines():
                        line = line.strip()
                        if not line or line in seen:
                            continue
                        dt = log_line_datetime(line)
                        if not dt:
                            continue
                        if any(abs((dt - mdt).total_seconds()) <= window.total_seconds() for mdt in matched_dts):
                            seen.add(line)
                            correlated.append({
                                "source": source,
                                "timestamp": extract_log_timestamp(line),
                                "line": line,
                                "message": extract_log_message(line),
                            })
                if correlated:
                    entry["correlated"] = correlated

        context.append(entry)

    return context


# ---------------------------------------------------------------------------
# Individual check execution
# ---------------------------------------------------------------------------

def run_check(check: dict, content: str, section: str, source_label: str, reference_date: datetime.date = None) -> list:
    """
    Execute a single check dict against content.
    Returns a list of failure dicts: {"message": str, "matches": list[dict]}.
    matches entries: {"source": str, "timestamp": str, "line": str}
    Empty list = passed.
    """
    failures = []
    check_type = check["type"]

    def fail(message, matches=None):
        failures.append({"message": message, "matches": matches or []})

    if check_type == "supported_version_check":
        lifecycle = check.get("lifecycle", [])
        today = datetime.date.today()

        ver_match = (
            re.search(r"SolOS[^:\n]*:\s*(\d+\.\d+\.\d+(?:\.\d+)?)", content, re.IGNORECASE)
            or re.search(r"[Ff]irmware[^:\n]*:\s*(\d+\.\d+\.\d+(?:\.\d+)?)", content)
            or re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", content)
        )
        if not ver_match:
            fail("Could not determine installed SolOS version from diagnostics.")
        else:
            installed = ver_match.group(1)
            entry = next((e for e in lifecycle if installed.startswith(e["version"])), None)
            if not entry:
                fail(f"SolOS {installed} is not in the known supported versions list.")
            else:
                eof_s = datetime.date.fromisoformat(entry["end_of_full_support"])
                eot_s = datetime.date.fromisoformat(entry["end_of_technical_support"])
                label = f" ({entry['release_type']})" if entry.get("release_type") else ""
                if today > eot_s:
                    fail(
                        f"SolOS {installed}{label} is out of support — "
                        f"technical support ended {eot_s.strftime('%B %d, %Y')}."
                    )
                elif today > eof_s:
                    print(
                        f"    [INFO] SolOS {installed}{label} — full support ended "
                        f"{eof_s.strftime('%B %d, %Y')}; technical support until "
                        f"{eot_s.strftime('%B %d, %Y')}."
                    )
                else:
                    print(
                        f"    [INFO] SolOS {installed}{label} is in full support "
                        f"until {eof_s.strftime('%B %d, %Y')}."
                    )

    elif check_type == "supported_chassis_check":
        lifecycle = check.get("lifecycle", [])
        today = datetime.date.today()

        prod_match = re.search(r"Chassis Product #:\s*([A-Z0-9][A-Z0-9\-]+)", content, re.IGNORECASE)
        if not prod_match:
            fail("Could not determine chassis product number from diagnostics.")
        else:
            product = prod_match.group(1).strip()
            entry = next((e for e in lifecycle if e["product_number"] == product), None)
            if not entry:
                fail(f"Chassis {product} is not in the supported products list.")
            else:
                eos = datetime.date.fromisoformat(entry["end_of_support"])
                if today > eos:
                    fail(
                        f"Chassis {product} is out of support — "
                        f"end of support was {eos.strftime('%B %d, %Y')}."
                    )
                else:
                    print(
                        f"    [INFO] Chassis {product} is supported "
                        f"until {eos.strftime('%B %d, %Y')}."
                    )

    elif check_type == "hba_status_check":
        if not re.search(r"Link State:", content, re.IGNORECASE):
            print("  [WARNING] No Host Bus Adapter (HBA) detected — HBA check skipped.")
        else:
            if not re.search(r"Operational State:\s+Online", content, re.IGNORECASE):
                fail("HBA is present but not online.")
            if not re.search(r"Link State:\s+Link Up", content, re.IGNORECASE):
                fail("HBA fibre channel link is not up.")

    elif check_type == "adb_status_check":
        if "Assured Delivery Blade" not in content:
            print("  [WARNING] No Assured Delivery Blade (ADB) detected — ADB check skipped.")
        elif not re.search(r"Operational State:\s+Up", content, re.IGNORECASE):
            fail("ADB blade is present but not operational.")

    elif check_type == "redundancy_standalone_check":
        is_shutdown = bool(re.search(r"Configuration Status\s*:\s*Shutdown", content, re.IGNORECASE))
        is_down = bool(re.search(r"Redundancy Status\s*:\s*Down", content, re.IGNORECASE))
        mate_blank = bool(re.search(r"Mate Router Name\s*:\s*$", content, re.IGNORECASE | re.MULTILINE))

        if is_shutdown and is_down and mate_blank:
            pass  # info_message on the rule handles the output
        else:
            fail("Appliance does not appear to be a standalone (redundancy not in expected Shutdown/Down/no-mate state).")

    elif check_type == "config_sync_status_check":
        admin_match = re.search(r"Admin Status\s*:\s*(\S+)", content, re.IGNORECASE)
        oper_match = re.search(r"Oper Status\s*:\s*(.+)$", content, re.IGNORECASE | re.MULTILINE)

        if not admin_match or not oper_match:
            fail("Could not determine config-sync status from diagnostics.")
        else:
            admin_status = admin_match.group(1).strip()
            oper_status = oper_match.group(1).strip()

            if admin_status == "Enabled" and re.match(r"Up$", oper_status, re.IGNORECASE):
                pass  # healthy
            elif admin_status == "Shutdown" and re.search(r"config-sync shutdown", oper_status, re.IGNORECASE):
                print("  [WARNING] Config-sync Admin Status is Shutdown — config-sync is intentionally disabled.")
            else:
                fail(f"Config-sync is not healthy — Admin Status: {admin_status}, Oper Status: {oper_status}.")

    elif check_type == "dns_log_check":
        down_pat = re.compile(r"Name server (\S+) has gone DOWN", re.IGNORECASE)
        up_pat   = re.compile(r"Name server (\S+) is now UP",    re.IGNORECASE)

        # Walk lines top-to-bottom; last event per server is its current state
        last_state = {}  # ip -> ("DOWN"|"UP", full_line)
        for line in content.splitlines():
            m = down_pat.search(line)
            if m:
                last_state[m.group(1)] = ("DOWN", line)
                continue
            m = up_pat.search(line)
            if m:
                last_state[m.group(1)] = ("UP", line)

        for ip, (state, line) in last_state.items():
            if state == "DOWN":
                ts = line.split()[0] if line else ""
                fail(
                    f"Name server {ip} is DOWN.",
                    matches=[{"source": source_label, "timestamp": ts, "line": line.strip()}]
                )

    elif check_type == "message_spool_status_check":
        config_match = re.search(r"Config Status:\s+(\S+)", content, re.IGNORECASE)
        oper_match = re.search(r"Operational Status:\s+(\S+)", content, re.IGNORECASE)

        if not config_match or not oper_match:
            fail("Could not determine message spool status from diagnostics.")
        else:
            config_status = config_match.group(1).strip()
            oper_status = oper_match.group(1).strip()

            if config_status == "Enabled":
                if not re.match(r"AD-(Active|Standby)$", oper_status, re.IGNORECASE):
                    fail(
                        f"Message spool is Enabled but operational status is '{oper_status}' "
                        f"(expected AD-Active or AD-Standby)."
                    )
            elif config_status == "Disabled":
                if oper_status != "AD-Disabled":
                    fail(
                        f"Message spool is Disabled but operational status is '{oper_status}' "
                        f"(expected AD-Disabled)."
                    )
                else:
                    print("  [WARNING] Message spool is Disabled and Operational Status is AD-Disabled — guaranteed messaging is not available.")
            else:
                fail(f"Unexpected message spool Config Status: '{config_status}'.")

    elif check_type == "alarm_check":
        if "show alarm" not in content.lower() and "alarm display" not in content.lower():
            print("  [WARNING] 'show alarm' output not found in diagnostics — alarm check skipped.")
        elif "No current alarms in the system." in content:
            pass  # clean
        else:
            # Extract non-boilerplate lines as the alarm content
            skip = re.compile(r"^\s*$|alarm display is enabled|no current alarms", re.IGNORECASE)
            alarms = [l.strip() for l in content.splitlines() if not skip.match(l.strip())]
            for alarm in alarms:
                fail(f"Active alarm: {alarm}")

    elif check_type == "print_info_fields":
        for field in check.get("fields", []):
            m = re.search(field["pattern"], content, re.IGNORECASE)
            if m:
                print(f"  [INFO] {field['label']}: {m.group(1).strip()}")

    elif check_type == "contains":
        if check["expected"] not in content:
            fail(check["failure_message"])

    elif check_type == "regex":
        if not re.search(check["pattern"], content, re.IGNORECASE):
            fail(check["failure_message"])

    elif check_type == "not_contains_regex":
        if re.search(check["pattern"], content, re.IGNORECASE):
            fail(check["failure_message"])

    elif check_type == "log_grep_absent":
        patterns = check.get("patterns", [])
        exclude_patterns = check.get("exclude_patterns", [])
        max_age_days = check.get("max_age_days",7)
        cutoff = (reference_date or datetime.date.today()) - datetime.timedelta(days=max_age_days)
        found_lines = []

        for line in content.splitlines():
            line_date = log_line_date(line)
            if line_date and line_date < cutoff:
                continue
            if any(re.search(ex, line, re.IGNORECASE) for ex in exclude_patterns):
                continue
            for pat in patterns:
                if re.search(pat, line, re.IGNORECASE):
                    found_lines.append(line.strip())
                    break

        if found_lines:
            matches = []
            for line in found_lines:
                timestamp = extract_log_timestamp(line) or "unknown time"
                message = extract_log_message(line)
                matches.append({"source": source_label, "timestamp": timestamp, "line": line, "message": message})
                print(f"    [GREP MATCH] Source: {source_label} | Time: {timestamp} {message}")
            fail(f"{check['failure_message']} (source: {source_label})", matches)

    elif check_type == "log_paired_events":
        patterns = check.get("patterns", [])
        exclude_patterns = check.get("exclude_patterns", [])
        max_age_days = check.get("max_age_days", 7)
        cutoff = (reference_date or datetime.date.today()) - datetime.timedelta(days=max_age_days)

        PROBLEM_SUFFIXES  = ("_DOWN", "_FAIL", "_FAILURE", "_FAILED", "_OFFLINE", "_MISSING", "_ERROR")
        RECOVERY_SUFFIXES = ("_UP", "_ONLINE", "_RECOVERED", "_RESTORED")

        def classify(line):
            """Return 'problem', 'recovery', or 'other' based on the event name in the line."""
            m = re.search(r"SYSTEM(?:_\w+)+", line, re.IGNORECASE)
            if not m:
                return "other", "", ""
            event = m.group(0).upper()
            for suf in RECOVERY_SUFFIXES:
                if event.endswith(suf):
                    return "recovery", event[: -len(suf)], suf
            for suf in PROBLEM_SUFFIXES:
                if event.endswith(suf):
                    return "problem", event[: -len(suf)], suf
            return "other", "", ""

        def extract_entity(line, direction_suf):
            """
            Pull the entity string out of the message body.
            Looks for text after '- -' or the last ':', then strips the
            trailing direction word(s) (e.g. ' up', ' down', ' failed').
            """
            m = re.search(r"-\s*-\s*(.+)$", line)
            entity_raw = m.group(1).strip() if m else line.strip()
            direction_words = r"\s*(up|down|fail|failed|failure|offline|online|missing|error|recovered|restored)\s*$"
            entity = re.sub(direction_words, "", entity_raw, flags=re.IGNORECASE).strip()
            return entity or entity_raw

        matched = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            line_date = log_line_date(line)
            if line_date and line_date < cutoff:
                continue
            if any(re.search(ex, line, re.IGNORECASE) for ex in exclude_patterns):
                continue
            for pat in patterns:
                if re.search(pat, line, re.IGNORECASE):
                    matched.append(line)
                    break

        # Walk in reverse (most recent first); first state seen per event/entity is current state.
        # If first seen = problem (DOWN/FAIL/etc) → still down → FAIL.
        # If first seen = recovery (UP/etc) → resolved → skip.
        seen = set()
        failing = []

        for line in reversed(matched):
            kind, base, suf = classify(line)
            if kind == "other":
                continue
            entity = extract_entity(line, suf)
            key = (base, entity)
            if key in seen:
                continue
            seen.add(key)
            if kind == "problem":
                failing.append(line)

        if failing:
            matches = []
            for line in failing:
                timestamp = extract_log_timestamp(line) or "unknown time"
                message = extract_log_message(line)
                matches.append({"source": source_label, "timestamp": timestamp, "line": line, "message": message})
                print(f"    [GREP MATCH] Source: {source_label} | Time: {timestamp} {message}")
            fail(f"{check['failure_message']} (source: {source_label})", matches)

    return failures


# ---------------------------------------------------------------------------
# Section runner
# ---------------------------------------------------------------------------

def run_section(rule: dict, diagnostics: str, logs: dict, reference_date: datetime.date = None, date_is_fallback: bool = False) -> tuple:
    """
    Run all checks for a single rule/section.
    Returns (passed: bool, failures: list[str]).

    A rule may specify a single 'source' or a list 'sources'.  When multiple
    sources are given, checks are run against each source independently and
    failures from all sources are combined.
    """
    section = rule["section"]
    command = rule.get("command", "")

    # Build the list of sources to check against
    if "sources" in rule:
        source_list = rule["sources"]
    else:
        source_list = [rule.get("source", "cli-diagnostics.txt")]

    # For any log source, also include loaded rotated variants (.1, .2, ...)
    # e.g. "event.log" -> ["event.log", "event.log.1", "event.log.2", ...]
    expanded_sources = []
    for source in source_list:
        expanded_sources.append(source)
        if source != "cli-diagnostics.txt":
            rotated = sorted(
                (k for k in logs if re.match(rf"{re.escape(source)}\.\d+$", k)),
                key=lambda k: int(k.rsplit(".", 1)[-1])
            )
            expanded_sources.extend(rotated)

    # Print log analysis window if any source is a log file
    log_sources = [s for s in expanded_sources if s != "cli-diagnostics.txt"]
    if log_sources and reference_date:
        max_age_days = next(
            (c.get("max_age_days", 30) for c in rule.get("checks", []) if "max_age_days" in c),
            7
        )
        cutoff = reference_date - datetime.timedelta(days=max_age_days)
        if date_is_fallback:
            print(f"  [WARNING] No log timestamps found — using today's date as reference. Window: {cutoff} to {reference_date}")
        else:
            print(f"  [INFO] Analyzing log data from {cutoff} to {reference_date} ({max_age_days}-day window)")

    all_failures = []

    for source in expanded_sources:
        # Resolve content for this source
        if source == "cli-diagnostics.txt":
            if command:
                content = extract_command_output(diagnostics, command)
                if content is None:
                    all_failures.append({"message": f"Section '{command}' not found in cli-diagnostics.txt.", "matches": []})
                    continue
            else:
                content = diagnostics
        elif source in logs:
            content = logs[source]
        else:
            print(f"  [WARNING] Source '{source}' not available — skipping checks against {source}.")
            continue

        for check in rule.get("checks", []):
            failures = run_check(check, content, section, source, reference_date)
            all_failures.extend(failures)

    return len(all_failures) == 0, all_failures


# ---------------------------------------------------------------------------
# Alternative-group helper
# ---------------------------------------------------------------------------

def section_group_key(section_id: str) -> str:
    """
    Derive the alternative-group key for a section ID.

    Sections whose identifier contains a letter component (e.g. '6.A.i',
    '6.B.ii') are grouped together under their leading all-numeric prefix
    ('6') and evaluated with OR logic — only one variant needs to pass.

    Purely-numeric sections ('1.1', '4.2', '6.2') are their own group and
    are always processed independently.

    Examples:
      '6.A.i'  -> '6'   (alternative group)
      '6.B.ii' -> '6'   (same alternative group)
      '1.1'    -> '1.1' (standalone)
      '6.2'    -> '6.2' (standalone)
    """
    parts = section_id.split(".")
    for i, part in enumerate(parts):
        if re.search(r"[A-Za-z]", part):
            prefix = ".".join(parts[:i])
            return prefix if prefix else section_id
    return section_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(folder: Path, router_name: str = None) -> bool:
    """Run health checks against a gather-diagnostics folder. Returns True if any checks failed."""
    folder = resolve_folder(folder)

    rules_path = Path(__file__).parent / "rules" / "healthcheck_rules.yaml"
    if not rules_path.exists():
        print(f"ERROR: healthcheck_rules.yaml not found at {rules_path}")
        sys.exit(1)

    print(f"Diagnostics folder : {folder.resolve()}")
    print(f"Rules file         : {rules_path.resolve()}")
    print()

    diagnostics = load_diagnostics(folder)
    logs = load_logs(folder)
    rules = load_rules(rules_path).get("rules", [])

    troubleshooting_path = Path(__file__).parent / "rules" / "further_troubleshooting_rules.yaml"
    troubleshooting_rules = load_troubleshooting_rules(troubleshooting_path)

    reference_date, date_is_fallback = find_latest_log_date(logs)
    print(f"Loaded {len(rules)} health check rules.")
    print()

    # Build an ordered task list.
    # Each task is a list of rules.  Length-1 tasks are standalone sections;
    # longer tasks are mutually-exclusive alternatives evaluated with OR logic.
    tasks = []
    group_index = {}  # group_key -> index in tasks

    for rule in rules:
        key = section_group_key(rule["section"])
        if key == rule["section"]:
            tasks.append([rule])          # standalone — always its own task
        elif key in group_index:
            tasks[group_index[key]].append(rule)   # add to existing group
        else:
            group_index[key] = len(tasks)
            tasks.append([rule])          # start a new alternative group

    any_failed = False
    json_results = []

    for task_rules in tasks:
        if len(task_rules) == 1:
            # ── Standalone section ─────────────────────────────────────────
            rule = task_rules[0]
            section = rule["section"]

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                passed, failures = run_section(rule, diagnostics, logs, reference_date, date_is_fallback)
            captured = buf.getvalue()

            if passed:
                info_msg = rule.get("info_message")
                if captured.strip() or info_msg:
                    print(f"[Section {section}] {rule.get('description', '')}")
                    if captured.strip():
                        print(captured, end="")
                    if info_msg:
                        print(f"  [INFO] {info_msg}")
                    print()
            else:
                any_failed = True
                print(f"[Section {section}] {rule.get('description', '')}")
                if captured.strip():
                    print(captured, end="")
                for failure in failures:
                    print(f"  [FAIL] {failure['message']}")
                print()

            result = {
                "section": section,
                "description": rule.get("description", ""),
                "status": "PASS" if passed else "FAIL",
                "failures": failures,
            }
            if not passed and section in troubleshooting_rules:
                result["troubleshooting_context"] = run_troubleshooting_steps(
                    section, logs, reference_date, troubleshooting_rules[section]
                )
            json_results.append(result)

        else:
            # ── Alternative group (OR logic) ───────────────────────────────
            # Evaluate every variant; report the first passing one.
            # If none pass, report failures from all variants.
            group_key = section_group_key(task_rules[0]["section"])

            # If variants have selectors, only run the one whose selector matches
            # the diagnostics content. Fall back to all variants if none match.
            if any(r.get("selector") for r in task_rules):
                active_rules = [
                    r for r in task_rules
                    if r.get("selector") and r["selector"] in (
                        extract_command_output(diagnostics, r["command"]) or "" if r.get("command") else diagnostics
                    )
                ] or task_rules
            else:
                active_rules = task_rules

            variant_results = []  # (rule, passed, failures, captured)

            for rule in active_rules:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    passed, failures = run_section(rule, diagnostics, logs, reference_date, date_is_fallback)
                variant_results.append((rule, passed, failures, buf.getvalue()))

            passing = [(r, f, cap) for r, p, f, cap in variant_results if p]

            if passing:
                rule, _, captured = passing[0]
                info_msg = rule.get("info_message")
                if captured.strip() or info_msg:
                    print(f"[Section {rule['section']}] {rule.get('description', '')}")
                    if captured.strip():
                        print(captured, end="")
                    if info_msg:
                        print(f"  [INFO] {info_msg}")
                    print()
                json_results.append({
                    "section": rule["section"],
                    "description": rule.get("description", ""),
                    "status": "PASS",
                    "failures": [],
                })
            else:
                any_failed = True
                group_desc = active_rules[0].get("description", "No applicable variant passed.")
                print(f"[Section {group_key}] {group_desc}")
                print("  No applicable variant passed.")
                all_failures = []
                for rule, _, failures, captured in variant_results:
                    print(f"  Variant {rule['section']} {rule.get('description', '')}:")
                    if captured.strip():
                        print(captured, end="")
                    for failure in failures:
                        print(f"    [FAIL] {failure['message']}")
                    all_failures.extend([
                        {"message": f"[{rule['section']}] {failure['message']}", "matches": failure["matches"]}
                        for failure in failures
                    ])
                group_result = {
                    "section": group_key,
                    "description": group_desc,
                    "status": "FAIL",
                    "failures": all_failures,
                }
                if group_key in troubleshooting_rules:
                    group_result["troubleshooting_context"] = run_troubleshooting_steps(
                        group_key, logs, reference_date, troubleshooting_rules[group_key]
                    )
                json_results.append(group_result)
            print()

    print("=" * 50)
    if any_failed:
        print("Health check completed with failures. Review the FAIL entries above.")
    else:
        print("Health check PASSED. Appliance appears healthy.")

    output = {
        "reference_date": reference_date.isoformat() if reference_date else None,
        "overall": "FAIL" if any_failed else "PASS",
        "results": json_results,
    }
    suffix = f"_{router_name}" if router_name else ""
    output_path = Path(__file__).parent / "data" / f"health_check_results{suffix}.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Results written to {output_path}")

    if any_failed:
        print()
        _print_troubleshoot_report(output)

    return any_failed


def main():
    args = sys.argv[1:]
    router_name = None
    if "--router-name" in args:
        idx = args.index("--router-name")
        router_name = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    if not args:
        print("Usage:   python health_check.py <gather-diagnostics-folder> [--router-name <name>]")
        print("Example: python health_check.py gather-diagnostics-20240101")
        sys.exit(1)

    folder = Path(args[0])
    if not folder.exists() or not folder.is_dir():
        print(f"ERROR: Folder not found: {folder}")
        sys.exit(1)

    data_dir = Path(__file__).parent / "data"
    suffix = f"_{router_name}" if router_name else ""
    tee = _Tee(data_dir / f"health_check_output{suffix}.txt")
    sys.stdout = tee

    print("Solace Appliance Health Check")
    print("=" * 50)

    run(folder, router_name=router_name)

    sys.stdout = tee._stdout
    tee.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
