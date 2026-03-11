#!/usr/bin/env python3
"""
establish_context.py
Reads one or two gather-diagnostics folders and prints a summary of each
broker's identity and redundancy configuration.

With two folders, also validates whether they are configured as HA mates.

Usage:
    python establish_context.py <folder>
    python establish_context.py <folder1> <folder2>
"""

import json
import re
import sys
from pathlib import Path


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
# Helpers (subset of main.py — no shared import to keep this standalone)
# ---------------------------------------------------------------------------

def load_diagnostics(folder: Path) -> str:
    path = folder / "cli-diagnostics.txt"
    if not path.exists():
        nested = folder / folder.name / "cli-diagnostics.txt"
        if nested.exists():
            path = nested
        else:
            raise FileNotFoundError(f"cli-diagnostics.txt not found in '{folder}'.")
    with open(path, "r", errors="replace") as f:
        return f.read()


def extract_command_output(diagnostics: str, command: str) -> str:
    escaped = re.escape(command)
    sep = r"[-=#]{5,}"
    pat_a = (
        rf"(?:{sep})\s*\n"
        rf"\s*#?\s*{escaped}\s*\n"
        rf"\s*{sep}\s*\n"
        rf"(.*?)"
        rf"(?=\n{sep}|\Z)"
    )
    m = re.search(pat_a, diagnostics, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    pat_b = rf"(?:{sep})\s*{escaped}\s*(?:{sep})(.*?)(?={sep}|\Z)"
    m = re.search(pat_b, diagnostics, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return diagnostics


def first_match(pattern: str, text: str, default: str = "Unknown") -> str:
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else default


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_context(folder: Path) -> dict:
    diagnostics = load_diagnostics(folder)

    redundancy_out  = extract_command_output(diagnostics, "show redundancy")
    hardware_out    = extract_command_output(diagnostics, "show hardware detail")

    router_name = first_match(r"Router Name\s*:\s*(\S+)", redundancy_out)
    if router_name == "Unknown":
        router_name = first_match(r"Router Name\s*:\s*(\S+)", diagnostics)

    serial           = first_match(r"Chassis serial:\s*(\S+)", hardware_out)
    chassis_product  = first_match(r"Chassis Product #:\s*(\S+)", hardware_out, default="")
    version_out      = extract_command_output(diagnostics, "show version")
    solos_version    = first_match(r"Solace PubSub\+.*?Version\s+(\S+)", version_out, default="")

    redundancy_config   = first_match(r"Configuration Status[ \t]*:[ \t]*(\S+)", redundancy_out)
    redundancy_mode     = first_match(r"Redundancy Mode[ \t]*:[ \t]*(\S+)", redundancy_out)
    role                = first_match(r"Active-Standby Role[ \t]*:[ \t]*(\S+)", redundancy_out)
    mate_router         = first_match(r"Mate Router Name[ \t]*:[ \t]*(\S+)", redundancy_out)
    if mate_router == "Unknown":
        mate_router = ""

    # Activity Status is a two-column table row — take only the first column
    # Suppress if redundancy is N/A (standalone)
    if redundancy_mode in ("N/A", "Unknown"):
        activity_status = "N/A"
    else:
        activity_raw    = first_match(r"Activity Status[ \t]*:[ \t]*(.+)", redundancy_out)
        activity_status = re.split(r"\s{2,}", activity_raw)[0].strip()

    standalone = (redundancy_mode == "N/A" and redundancy_config == "Shutdown")

    redundancy_role     = ""
    active_standby_role = ""

    if redundancy_mode == "Active/Active":
        # In Active/Active the CLI Active-Standby Role is always None.
        # Redundancy Role is Active when both Virtual Routers show a consistent
        # activity state (both Local Active, or both Mate Active); Active (Down)
        # when the VRs are in a mixed or unexpected state.
        active_standby_role = "None"
        activity_cols = re.split(r"\s{2,}", activity_raw)
        if len(activity_cols) >= 2:
            both_local = all("Local Active" in c for c in activity_cols[:2])
            both_mate  = all("Mate Active"  in c for c in activity_cols[:2])
            redundancy_role = "Active" if (both_local or both_mate) else "Active (Down)"
        else:
            redundancy_role = "Active"
    else:
        # Active/Standby: derive from Internal Redundancy State.
        # Format: <Pri|Bkup>-<Active|Standby|NotReady>
        #   prefix → Active-Standby Role (Primary/Backup)
        #   suffix → Redundancy Role (Active/Standby/Not Ready)
        for irs_m in re.finditer(r"Internal Redundancy State\s{2,}(.*)", redundancy_out):
            vals = irs_m.group(1).split()
            ha_vals = [v for v in vals if re.match(r"^(Pri|Bkup)-", v)]
            if not ha_vals:
                continue
            chosen = ha_vals[0]
            m_parts = re.match(r"^(Pri|Bkup)-(.+)$", chosen)
            if m_parts:
                prefix, suffix = m_parts.group(1), m_parts.group(2)
                active_standby_role = "Primary" if prefix == "Pri" else "Backup"
                suffix_map = {"Active": "Active", "Standby": "Standby", "NotReady": "Not Ready"}
                redundancy_role = suffix_map.get(suffix, suffix)
            break

    replication_out  = extract_command_output(diagnostics, "show replication stats")
    repl_interface   = first_match(r"Replication Interface[ \t]*:[ \t]*(\S+)", replication_out, default="")
    repl_mate        = first_match(r"Replication Mate[ \t]*:[ \t]*(\S+)", replication_out, default="")
    repl_connect_via = first_match(r"Connect-Via[ \t]*:[ \t]*(\S+)", replication_out, default="")
    replication_active = all([repl_interface, repl_mate, repl_connect_via])

    replication_site = ""
    if replication_active:
        bridge_out = extract_command_output(diagnostics, "show bridge *")
        # Each replication bridge row starts with "#MSGVPN_REPL" in the 12-char Name column.
        # The flag columns A E I O Q R appear on that same first line; E is the 2nd flag (L=Local, R=Remote).
        repl_lines = [l for l in bridge_out.splitlines() if re.match(r"#MSGVPN_REPL", l)]
        establishes = []
        for line in repl_lines:
            # Only consider bridges that are Admin Up (A=U) with a valid establisher (E=L or R)
            m = re.search(r"U ([LR]) [UD-]", line)
            if m:
                establishes.append(m.group(1))
        if establishes:
            if all(e == "L" for e in establishes):
                replication_site = "Active"
            elif all(e == "R" for e in establishes):
                replication_site = "Standby"
        elif repl_lines:
            # Bridges exist but all are Admin Down — resolve against mate after all contexts built
            replication_site = "_down"

    # Additional context: message spool, redundancy status, config-sync
    spool_out    = extract_command_output(diagnostics, "show message-spool detail")
    spool_config = first_match(r"Config Status\s*:\s*(.+)", spool_out, default="").strip()
    spool_oper   = ""
    if "enabled" in spool_config.lower():
        spool_oper = first_match(r"Operational Status\s*:\s*(\S+)", spool_out, default="").strip()

    redun_status = ""
    if redundancy_config not in ("Unknown", "") and "enabled" in redundancy_config.lower():
        redun_status = first_match(r"Redundancy Status\s*:\s*(\S+)", redundancy_out, default="").strip()

    csync_out    = extract_command_output(diagnostics, "show config-sync")
    csync_config = first_match(r"Admin Status\s*:\s*(\S+)", csync_out, default="").strip()
    csync_oper   = ""
    if "enabled" in csync_config.lower():
        csync_oper = first_match(r"Oper Status\s*:\s*(\S+)", csync_out, default="").strip()

    return {
        "full_path":       str(folder.resolve()),
        "folder":          folder.name,
        "router_name":     router_name,
        "serial":          serial,
        "chassis_product": chassis_product,
        "solos_version":   solos_version,
        "redundancy_mode": redundancy_mode,
        "role":             role,
        "redundancy_role":  redundancy_role,
        "active_standby_role": active_standby_role,
        "activity_status": activity_status,
        "mate_router":     mate_router,
        "standalone":          standalone,
        "replication_active":  replication_active,
        "replication_mate":    re.sub(r'^v:', '', repl_mate) if replication_active else "",
        "replication_site":    replication_site,
        "spool_config":    spool_config,
        "spool_oper":      spool_oper,
        "redun_config":    redundancy_config,
        "redun_status":    redun_status,
        "csync_config":    csync_config,
        "csync_oper":      csync_oper,
    }


def print_context(ctx: dict, label: str = ""):
    if ctx.get("standalone"):
        suffix = " - Standalone Appliance"
    elif ctx.get("redundancy_mode") in ("Active/Active", "Active/Standby"):
        suffix = " - Redundant Configuration" if ctx.get("serial") == "Unknown" else " - Redundant Appliance"
    else:
        suffix = ""
    header = f"Broker Context — {label}{suffix}" if label else f"Broker Context{suffix}"
    print(f"\n{header}")
    print("-" * 50)
    w = 19  # width of longest label ("Active-Standby Role")

    left_lines = []
    def row(lbl, value):
        left_lines.append(f"  {lbl:<{w}} : {value}")

    row("Router Name",        ctx['router_name'])
    row("Serial Number",      ctx['serial'])
    if ctx.get('chassis_product'):
        row("Chassis Product #",  ctx['chassis_product'])
    if ctx.get('solos_version'):
        row("SolOS Version",      ctx['solos_version'])
    row("Redundancy Mode",    ctx['redundancy_mode'])
    na = ctx['redundancy_mode'] in ("N/A", "Unknown")
    if not na:
        row("Redundancy Role",    ctx['redundancy_role'])
        row("Active-Standby Role", ctx['active_standby_role'])
    if ctx['mate_router']:
        row("Mate Router",    ctx['mate_router'])
    if ctx['replication_active']:
        row("Replication",      "Active")
        row("Replication Mate", ctx['replication_mate'])
        row("Replication Site", ctx['replication_site'])
    else:
        row("Replication",      "N/A")

    # Build Additional Context right column
    right_lines = []
    rw = 13  # width of longest right label ("Message Spool")
    spool_config = ctx.get('spool_config', '')
    spool_oper   = ctx.get('spool_oper', '')
    redun_config = ctx.get('redun_config', '')
    redun_status = ctx.get('redun_status', '')
    csync_config = ctx.get('csync_config', '')
    csync_oper   = ctx.get('csync_oper', '')

    def ac_row(lbl, val):
        right_lines.append(f"  {lbl:<{rw}} : {val}")

    if any(v not in ('', 'Unknown') for v in [spool_config, redun_config, csync_config]):
        right_lines.append("Additional Context")
        if spool_config:
            val = spool_config + (f" / {spool_oper}" if spool_oper else "")
            ac_row("Message Spool", val)
        if redun_config not in ('', 'Unknown'):
            val = redun_config + (f" / {redun_status}" if redun_status else "")
            ac_row("Redundancy", val)
        if csync_config not in ('', 'Unknown'):
            val = csync_config + (f" / {csync_oper}" if csync_oper else "")
            ac_row("Config Sync", val)

    # Print left and right columns side by side
    if right_lines:
        sep = max(len(l) for l in left_lines) + 4
        for i in range(max(len(left_lines), len(right_lines))):
            left  = left_lines[i]  if i < len(left_lines)  else ""
            right = right_lines[i] if i < len(right_lines) else ""
            print(f"{left:<{sep}}{right}" if right else left)
    else:
        for line in left_lines:
            print(line)


def broker_site_label(ctx: dict) -> str:
    """Return 'Role/Activity' label for a broker line in replication/HA output."""
    role_str = ctx.get("redundancy_role") or ""
    act_str  = ctx.get("active_standby_role") or ""
    if act_str and act_str != "None":
        return f"{role_str}/{act_str}"
    return role_str


def _broker_order(c):
    role_rank = 0 if c.get("redundancy_role") == "Primary" else 1
    act_rank  = 0 if c.get("active_standby_role") == "Active" else 1
    return (role_rank, act_rank)


def _draw_table(headers: list, row_groups: list) -> str:
    """Render a box-drawing table. row_groups is a list of row-lists; groups are separated by a mid-divider."""
    all_rows = [r for g in row_groups for r in g]
    col_widths = [len(h) for h in headers]
    for row in all_rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    def fmt_row(cells, L='│', S='│', R='│'):
        parts = [f" {str(c):<{col_widths[i]}} " for i, c in enumerate(cells)]
        return L + S.join(parts) + R

    def fmt_sep(L='├', M='┼', R='┤', F='─'):
        return L + M.join(F * (w + 2) for w in col_widths) + R

    lines = [
        fmt_sep('┌', '┬', '┐'),
        fmt_row(headers),
        fmt_sep('├', '┼', '┤'),
    ]
    for gi, group in enumerate(row_groups):
        for row in group:
            lines.append(fmt_row(row))
        if gi < len(row_groups) - 1:
            lines.append(fmt_sep('├', '┼', '┤'))
    lines.append(fmt_sep('└', '┴', '┘'))
    return '\n'.join('  ' + line for line in lines)


def _group_to_json(group):
    return [
        {"router_name": ctx["router_name"], "role": broker_site_label(ctx), "missing_gd": False}
        for ctx in sorted(group, key=_broker_order)
    ]


def _missing_mate_json(group):
    if len(group) == 1:
        mate = group[0].get("mate_router", "")
        if mate:
            role = group[0].get("redundancy_role", "")
            missing_role = "Backup" if role == "Primary" else "Primary" if role == "Backup" else "Mate"
            return [{"router_name": mate, "role": missing_role, "missing_gd": True}]
    return []


def validate_replication_pairs(contexts: list):
    repl_ctxs = [c for c in contexts if c.get("replication_active")]
    if not repl_ctxs:
        return []

    print("\nReplication Pair Validation")
    print("-" * 50)

    # Group into HA pairs (mate_router cross-reference) or solo entries
    used = set()
    groups = []
    for i, ctx in enumerate(repl_ctxs):
        if i in used:
            continue
        mate_name = ctx.get("mate_router", "")
        mate_idx = next(
            (j for j, other in enumerate(repl_ctxs)
             if j != i and j not in used and other["router_name"] == mate_name),
            None
        )
        if mate_idx is not None:
            used.update([i, mate_idx])
            groups.append([ctx, repl_ctxs[mate_idx]])
        else:
            used.add(i)
            groups.append([ctx])

    # Separate by site — treat Down variants alongside their base category
    def _is_active_site(g):
        s = g[0].get("replication_site", "")
        return s == "Active" or s.startswith("Active")

    def _is_backup_site(g):
        s = g[0].get("replication_site", "")
        return s in ("Standby", "Standby (Down)") or s.startswith("Standby")

    active_groups = [g for g in groups if _is_active_site(g)]
    backup_groups = [g for g in groups if _is_backup_site(g)]
    other_groups  = [g for g in groups if not _is_active_site(g) and not _is_backup_site(g)]

    def router_names(group):
        return {c["router_name"] for c in group}

    def repl_mates(group):
        return {re.sub(r'^v:', '', c.get("replication_mate", ""))
                for c in group if c.get("replication_mate")}

    # Match each Active group to its Backup group via replication_mate
    paired_backup = set()
    matched_pairs = []
    for ag in active_groups:
        mates = repl_mates(ag)
        bg = None
        for k, bg_candidate in enumerate(backup_groups):
            if k not in paired_backup and mates & router_names(bg_candidate):
                bg = bg_candidate
                paired_backup.add(k)
                break
        matched_pairs.append((ag, bg))

    # Unmatched backup groups (no corresponding active found)
    for k, bg in enumerate(backup_groups):
        if k not in paired_backup:
            matched_pairs.append((None, bg))

    # Unknown / unresolved site groups
    for og in other_groups:
        matched_pairs.append((og, None))

    REPL_HEADERS = ["Site", "HA Role", "Router", "Repl Site Status", "Info"]

    def _repl_rows_for_group(group, site_label):
        rows = []
        for ctx in sorted(group, key=_broker_order):
            rows.append([site_label, broker_site_label(ctx), ctx["router_name"], ctx.get("replication_site", ""), ""])
        if len(group) == 1:
            mate = group[0].get("mate_router", "")
            if mate:
                role = group[0].get("redundancy_role", "")
                missing_role = "Backup" if role == "Primary" else "Primary" if role == "Backup" else "Mate"
                rows.append([site_label, missing_role, mate, "-", "Missing GD"])
        return rows

    pairs_json = []
    for n, (ag, bg) in enumerate(matched_pairs, 1):
        if ag is None and bg is None:
            continue

        print(f"\n  Replication Pair {n}")
        pair = {"pair_number": n}
        primary_rows = []
        backup_rows = []

        if ag is not None:
            primary_rows = _repl_rows_for_group(ag, "Active")
            pair["active_site"] = _group_to_json(ag) + _missing_mate_json(ag)

        if bg is not None:
            backup_rows = _repl_rows_for_group(bg, "Standby")
            pair["standby_site"] = _group_to_json(bg) + _missing_mate_json(bg)

        # Infer missing opposite site from replication_mate
        if ag is not None and not backup_rows:
            repl_mate = ag[0].get("replication_mate", "")
            if repl_mate:
                backup_rows = [["Standby", "-", repl_mate, "-", "Missing GD"]]
                pair["standby_site"] = [{"router_name": repl_mate, "missing_gd": True}]
        elif bg is not None and not primary_rows:
            repl_mate = bg[0].get("replication_mate", "")
            if repl_mate:
                primary_rows = [["Active", "-", repl_mate, "-", "Missing GD"]]
                pair["active_site"] = [{"router_name": repl_mate, "missing_gd": True}]

        row_groups = [g for g in [primary_rows, backup_rows] if g]
        if row_groups:
            print(_draw_table(REPL_HEADERS, row_groups))

        pairs_json.append(pair)
    return pairs_json


def validate_ha_pairs(contexts: list):
    if len(contexts) < 2:
        return []

    router_names = {c["router_name"] for c in contexts}

    # Find matched pairs (both mates present)
    checked = set()
    matched = set()
    pairs = []
    for i, ctx1 in enumerate(contexts):
        for j, ctx2 in enumerate(contexts):
            if i >= j or (i, j) in checked:
                continue
            if (ctx1["mate_router"] == ctx2["router_name"] or
                    ctx2["mate_router"] == ctx1["router_name"]):
                checked.add((i, j))
                matched.update([i, j])
                pairs.append(("full", ctx1, ctx2))

    # Find solo brokers whose mate wasn't provided
    for i, ctx in enumerate(contexts):
        if i not in matched and ctx.get("mate_router") and ctx["mate_router"] not in router_names:
            pairs.append(("solo", ctx, None))

    if not pairs:
        return []

    print("\nHA Pair Validation")
    print("-" * 50)

    HA_HEADERS = ["HA Role", "Router", "Redundancy", "Replication", "Info"]

    def _ha_row(ctx, missing_role=None):
        if missing_role is not None:
            return [missing_role, ctx["router_name"], "-", "-", "Missing GD"]
        repl = "N/A"
        if ctx.get("replication_active"):
            site = ctx.get("replication_site", "")
            repl = site if site else "Active"
        return [
            broker_site_label(ctx),
            ctx["router_name"],
            ctx.get("redundancy_mode", ""),
            repl,
            "",
        ]

    pairs_json = []
    for n, (kind, ctx1, ctx2) in enumerate(pairs, 1):
        print(f"\n  HA Pair {n}")
        rows = []
        brokers = []
        if kind == "full":
            for ctx in sorted([ctx1, ctx2], key=_broker_order):
                rows.append(_ha_row(ctx))
                brokers.append({"router_name": ctx["router_name"], "role": broker_site_label(ctx), "missing_gd": False})
        else:
            cur_role = ctx1.get("redundancy_role", "")
            rows.append(_ha_row(ctx1))
            brokers.append({"router_name": ctx1["router_name"], "role": broker_site_label(ctx1), "missing_gd": False})
            missing_role = "Backup" if cur_role == "Primary" else "Primary" if cur_role == "Backup" else "Mate"
            rows.append(_ha_row({"router_name": ctx1["mate_router"]}, missing_role=missing_role))
            brokers.append({"router_name": ctx1["mate_router"], "role": missing_role, "missing_gd": True})
        print(_draw_table(HA_HEADERS, [rows]))
        pairs_json.append({"pair_number": n, "brokers": brokers})
    return pairs_json


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    sys.stdout.reconfigure(encoding='utf-8')

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python establish_context.py <folder> [folder2] [folder3] ...")
        sys.exit(1)

    folders = list(dict.fromkeys(Path(a) for a in sys.argv[1:]))
    for f in folders:
        if not f.exists() or not f.is_dir():
            print(f"[ERROR] Folder not found: {f}")
            sys.exit(1)

    data_dir = Path(__file__).parent / "data"
    tee = _Tee(data_dir / "context_output.txt")
    sys.stdout = tee

    # Build all contexts first so we can cross-reference mates
    contexts = []
    for folder in folders:
        try:
            ctx = extract_context(folder)
        except FileNotFoundError as e:
            print(f"[ERROR] {e}")
            continue
        contexts.append(ctx)

    # Resolve "_down" replication sites against the mate's known site
    for ctx in contexts:
        if ctx.get("replication_site") == "_down":
            mate_name = ctx.get("replication_mate", "")
            mate_ctx  = next((c for c in contexts if c["router_name"] == mate_name), None)
            mate_site = mate_ctx.get("replication_site", "") if mate_ctx else ""
            if mate_site.startswith("Active"):
                ctx["replication_site"] = "Standby (Down)"
            elif mate_site in ("Standby", "Standby (Down)") or mate_site.startswith("Standby"):
                ctx["replication_site"] = "Active (Down)"
            else:
                ctx["replication_site"] = "Down"

    print("=" * 50)
    print("Solace Broker Context")
    print("=" * 50)

    for i, ctx in enumerate(contexts, 1):
        label = f"Broker {i}" if len(contexts) > 1 else ""
        print_context(ctx, label)

    repl_pairs = validate_replication_pairs(contexts)
    ha_pairs = validate_ha_pairs(contexts)

    output_path = data_dir / "router_context.json"
    with open(output_path, "w") as f:
        json.dump(contexts, f, indent=2)
    print(f"\nContext written to {output_path}")

    if repl_pairs:
        repl_path = data_dir / "replication_pair_validation.json"
        with open(repl_path, "w") as f:
            json.dump(repl_pairs, f, indent=2)
        print(f"Replication pairs written to {repl_path}")

    if ha_pairs:
        ha_path = data_dir / "HA_pair_validation.json"
        with open(ha_path, "w") as f:
            json.dump(ha_pairs, f, indent=2)
        print(f"HA pairs written to {ha_path}")

    print()
    sys.stdout = tee._stdout
    tee.close()


if __name__ == "__main__":
    main()
