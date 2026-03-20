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

def _normalize_gdh(text: str) -> str:
    """Convert gather-diagnostics-host format to standard cli-diagnostics format.

    GDH sections look like:
        hostname> show version
        <output>
        hostname> show hardware detail
        <output>

    This converts them to the separator-based format expected by extract_command_output().
    """
    m = re.search(r'^(\S+)> (?:show |no |clear |debug )', text, re.MULTILINE)
    if not m:
        return text
    hostname = re.escape(m.group(1))
    sections = re.split(rf'^{hostname}> ', text, flags=re.MULTILINE)
    sep = "=" * 50
    result = []
    for section in sections[1:]:
        newline_idx = section.find('\n')
        if newline_idx == -1:
            command = section.strip()
            output = ""
        else:
            command = section[:newline_idx].strip()
            output = section[newline_idx + 1:].rstrip()
        result.append(f"\n{sep}\n# {command}\n{sep}\n{output}\n")
    return '\n'.join(result)


def load_diagnostics(folder: Path) -> str:
    path = folder / "cli-diagnostics.txt"
    if not path.exists():
        nested = folder / folder.name / "cli-diagnostics.txt"
        if nested.exists():
            path = nested
        else:
            gdh_path = folder / "gdh-diagnostics.txt"
            if gdh_path.exists():
                with open(gdh_path, "r", errors="replace") as f:
                    return _normalize_gdh(f.read())
            raise FileNotFoundError(f"cli-diagnostics.txt not found in '{folder}'.")
    with open(path, "r", errors="replace") as f:
        return f.read()


def extract_command_output(diagnostics: str, command: str) -> str:
    escaped = re.escape(command)
    # any_sep: detect section headers (= # or - are all used as opening separators)
    any_sep = r"[-=#]{5,}"
    # sec_sep: only = or # terminate a section — table content uses --- lines which
    # must NOT be treated as section boundaries.
    sec_sep = r"[=#]{5,}"
    pat_a = (
        rf"(?:{any_sep})\s*\n"
        rf"\s*#?\s*{escaped}[^\n]*\n"
        rf"\s*{any_sep}\s*\n"
        rf"(.*?)"
        rf"(?=\n{sec_sep}|\Z)"
    )
    m = re.search(pat_a, diagnostics, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    pat_b = rf"(?:{any_sep})\s*{escaped}[^\n]*(?:{any_sep})(.*?)(?={sec_sep}|\Z)"
    m = re.search(pat_b, diagnostics, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return diagnostics


def _parse_redundancy_group(output: str) -> list:
    """Parse 'show redundancy group' table into [{name, node_type, status}].

    Expected format:
        Node Router-Name   Node Type       Address           Status
        -----------------  --------------  ----------------  ---------
        routerA            Message-Router  host.example.com  Online
        routerB            Monitor         host2.example.com Online
          *                                                   (current node marker)
    """
    rows = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip header and separator lines
        if re.match(r'^[-=]+', stripped) or re.match(r'^Node Router-Name', stripped, re.IGNORECASE):
            continue
        # Skip continuation/indented lines and the current-node asterisk marker
        if line.startswith(' ') or stripped == '*' or stripped.startswith('* -'):
            continue
        # Parse: name  type  addr  status  (split on 2+ spaces)
        # Strip trailing * from name — Solace appends * to mark the current node
        # either on the name itself (e.g. "routerA*") or on a separate continuation
        # line (the latter is handled by the leading-space skip above).
        parts = re.split(r'\s{2,}', stripped)
        if len(parts) >= 4:
            rows.append({"name": parts[0].rstrip('*'), "node_type": parts[1], "status": parts[-1]})
        elif len(parts) == 3:
            rows.append({"name": parts[0].rstrip('*'), "node_type": parts[1], "status": parts[2]})
    return rows


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

    # Use show router-name first (most reliable); anchored to line start to avoid
    # matching "Mate Router Name" which also contains "Router Name".
    router_name_out = extract_command_output(diagnostics, "show router-name")
    router_name = first_match(r"^Router Name\s*:\s*(\S+)", router_name_out)
    if router_name == "Unknown":
        router_name = first_match(r"^Router Name\s*:\s*(\S+)", redundancy_out)
    if router_name == "Unknown":
        router_name = first_match(r"^Router Name\s*:\s*(\S+)", diagnostics)

    serial           = first_match(r"Chassis serial:\s*(\S+)", hardware_out)
    chassis_product  = first_match(r"Chassis Product #:\s*(\S+)", hardware_out, default="")
    version_out      = extract_command_output(diagnostics, "show version")
    solos_version    = first_match(r"Solace PubSub\+.*?Version\s+(\S+)", version_out, default="")
    _model_m         = re.search(r"Solace PubSub\+\s+(\S+)\s+Version", version_out or diagnostics)
    platform_type    = "appliance" if (_model_m and _model_m.group(1).isdigit()) else "software"

    operating_mode_raw  = first_match(r"Operating Mode[ \t]*:[ \t]*(.+)", redundancy_out, default="")
    operating_mode      = operating_mode_raw.strip()
    is_monitor          = "monitor" in operating_mode.lower()

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

    # Determine replication status from ConfigSync Bridge block within show replication stats
    repl_cs_admin = ""
    repl_cs_state = ""
    cs_admin_m = re.search(r'Admin State\s*:\s*(\S+)', replication_out)
    if cs_admin_m:
        repl_cs_admin = cs_admin_m.group(1)
        after_admin = replication_out[cs_admin_m.end():]
        cs_state_m = re.search(r'^\s*State\s*:\s*(\S+)', after_admin, re.MULTILINE)
        if cs_state_m:
            repl_cs_state = cs_state_m.group(1)

    if repl_cs_admin.lower() == 'disabled':
        replication_status = "Disabled / Down"
        replication_active = False
    elif repl_cs_admin.lower() == 'enabled':
        if repl_cs_state.lower() == 'n/a':
            replication_status = "N/A"
            replication_active = False
        else:
            replication_status = "Enabled / Up" if repl_cs_state.lower() == 'up' else "Enabled / Down"
            replication_active = True
    else:
        # No ConfigSync block found — fall back to presence of interface fields
        replication_active = all([repl_interface, repl_mate, repl_connect_via])
        replication_status = "Active" if replication_active else "N/A"

    replication_site = ""
    if replication_active:
        bridge_out = extract_command_output(diagnostics, "show bridge *")
        lines = bridge_out.splitlines()

        # Find CFGSYNC replication bridge — Admin=Up establisher (L/R) determines local site role
        cfgsync_establisher = ""
        cfgsync_found = False
        for line in lines:
            if re.match(r"#CFGSYNC_REP", line):
                cfgsync_found = True
                m = re.search(r"U ([LR]) [UD-]", line)
                if m:
                    cfgsync_establisher = m.group(1)
                break

        # Collect MSGVPN replication bridge establisher flags (L, R, or -)
        msgvpn_establishers = []
        for line in lines:
            if re.match(r"#MSGVPN_REPL", line):
                m = re.search(r"[UD] ([LR-]) [UD-]", line)
                if m:
                    msgvpn_establishers.append(m.group(1))

        has_L = "L" in msgvpn_establishers
        has_R = "R" in msgvpn_establishers

        if cfgsync_establisher in ("L", "R"):
            if has_L and has_R:
                active_count   = msgvpn_establishers.count("L")
                standby_count  = msgvpn_establishers.count("R")
                total          = active_count + standby_count
                active_pct     = round(active_count  / total * 100)
                standby_pct    = 100 - active_pct
                replication_site = f"Active ({active_pct}%) / Standby ({standby_pct}%)"
            elif cfgsync_establisher == "L":
                replication_site = "Active"
            else:
                replication_site = "Standby"
        elif cfgsync_found or msgvpn_establishers:
            # Bridges found but none established — resolve against mate after all contexts built
            replication_site = "_down"

    # Redundancy group membership (software brokers — show redundancy group)
    redun_group_out  = extract_command_output(diagnostics, "show redundancy group")
    redundancy_group = _parse_redundancy_group(redun_group_out) if redun_group_out else []

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
        "platform_type":   platform_type,
        "operating_mode":  operating_mode,
        "is_monitor":      is_monitor,
        "redundancy_group": redundancy_group,
        "redundancy_mode": redundancy_mode,
        "role":             role,
        "redundancy_role":  redundancy_role,
        "active_standby_role": active_standby_role,
        "activity_status": activity_status,
        "mate_router":     mate_router,
        "standalone":          standalone,
        "replication_active":  replication_active,
        "replication_status":  replication_status,
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
    is_sw      = ctx.get("platform_type") == "software"
    is_monitor = ctx.get("is_monitor", False)

    if ctx.get("standalone"):
        suffix = " - Standalone"
    elif is_monitor:
        suffix = " - Monitor Node"
    elif ctx.get("redundancy_mode") in ("Active/Active", "Active/Standby"):
        suffix = " - Redundant Appliance" if ctx.get("platform_type") == "appliance" else " - Redundant Configuration"
    else:
        suffix = ""
    header = f"Broker Context — {label}{suffix}" if label else f"Broker Context{suffix}"
    print(f"\n{header}")
    print("-" * 50)
    w = 19  # width of longest label ("Active-Standby Role")

    spool_config = ctx.get('spool_config', '')
    spool_oper   = ctx.get('spool_oper', '')
    redun_config = ctx.get('redun_config', '')
    redun_status = ctx.get('redun_status', '')
    csync_config = ctx.get('csync_config', '')
    csync_oper   = ctx.get('csync_oper', '')

    # Left column: identity + status fields
    left_lines = []
    def row(lbl, value):
        left_lines.append(f"  {lbl:<{w}} : {value}")

    row("Router Name",       ctx['router_name'])
    if not is_sw:
        row("Serial Number",     ctx['serial'])
    if ctx.get('chassis_product'):
        row("Chassis Product #", ctx['chassis_product'])
    if ctx.get('solos_version'):
        row("SolOS",             ctx['solos_version'])
    if spool_config:
        row("Message Spool", spool_config + (f" / {spool_oper}" if spool_oper else ""))
    if redun_config not in ('', 'Unknown'):
        row("Redundancy", redun_config + (f" / {redun_status}" if redun_status else ""))
    if csync_config not in ('', 'Unknown'):
        row("Config Sync", csync_config + (f" / {csync_oper}" if csync_oper else ""))

    # Right column: redundancy topology + replication
    right_lines = []
    def rrow(lbl, val):
        right_lines.append(f"  {lbl:<{w}} : {val}")

    redundancy_enabled_up = (
        redun_config.lower() == 'enabled' and redun_status.lower() == 'up'
    )
    if is_monitor:
        rrow("Operating Role", "Monitoring Node")
    elif redundancy_enabled_up:
        rrow("Redundancy Mode",     ctx['redundancy_mode'])
        rrow("Active-Standby Role", ctx['active_standby_role'])
        ad_role = f"AD-{ctx['redundancy_role']}" if ctx.get('redundancy_role') else ""
        rrow("Redundancy Role",     ad_role)
    if ctx['mate_router']:
        rrow("Mate Router",         ctx['mate_router'])
    repl_status = ctx.get("replication_status", "N/A")
    rrow("Replication", repl_status)
    if ctx.get("replication_active"):
        rrow("Replication Mate", ctx['replication_mate'])
        rrow("Replication Site", ctx['replication_site'])

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
    """Return 'Active-Standby Role/AD-Redundancy Role' label for table rows."""
    role_str = ctx.get("redundancy_role") or ""
    act_str  = ctx.get("active_standby_role") or ""
    ad_role  = f"AD-{role_str}" if role_str else ""
    if act_str and ad_role:
        return f"{act_str}/{ad_role}"
    return act_str or ad_role


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


def validate_ha_triplets(contexts: list):
    """Validate HA triplets (Primary + Backup + Monitor) for software brokers.

    Groups brokers by their shared redundancy-group membership, then for each
    group shows a table of all three nodes and cross-validates that every
    provided GD reports the same group composition.
    """
    if not contexts:
        return []

    # Group contexts that share at least one common redundancy-group member name.
    ungrouped = list(range(len(contexts)))
    triplet_groups = []
    while ungrouped:
        idx = ungrouped.pop(0)
        ctx = contexts[idx]
        group_names = {m["name"] for m in ctx.get("redundancy_group", [])}
        group_names.add(ctx["router_name"])

        same_group = [idx]
        remaining = []
        for other_idx in ungrouped:
            other_ctx = contexts[other_idx]
            other_names = {m["name"] for m in other_ctx.get("redundancy_group", [])}
            other_names.add(other_ctx["router_name"])
            if group_names & other_names:
                same_group.append(other_idx)
                group_names |= other_names
            else:
                remaining.append(other_idx)
        ungrouped = remaining
        triplet_groups.append(same_group)

    print("\nHA Triplet Validation")
    print("-" * 50)

    TRIPLET_HEADERS = ["Node Type", "HA Role", "Router", "Status", "Info"]

    def _node_type_rank(node_type: str) -> int:
        t = node_type.lower()
        if "primary" in t:
            return 0
        if "backup" in t:
            return 1
        if "monitor" in t:
            return 2
        return 3

    def _enrich_node_type(member: dict, ctx_by_name: dict) -> str:
        """Return Primary, Backup, or Monitor as the display node type."""
        node_type = member.get("node_type", "")
        if node_type.lower() == "monitor":
            return "Monitor"
        if node_type.lower() == "message-router":
            ctx = ctx_by_name.get(member["name"])
            if ctx:
                role = ctx.get("active_standby_role", "")
                if role in ("Primary", "Backup"):
                    return role
        return node_type

    triplets_json = []

    for n, group_indices in enumerate(triplet_groups, 1):
        group_contexts = [contexts[i] for i in group_indices]
        ctx_by_name = {ctx["router_name"]: ctx for ctx in group_contexts}

        # Build the canonical member list from the first context that has one
        canonical_group = next(
            (ctx.get("redundancy_group") for ctx in group_contexts if ctx.get("redundancy_group")),
            None
        )
        if not canonical_group:
            canonical_group = [
                {"name": ctx["router_name"], "node_type": "Unknown", "status": "Unknown"}
                for ctx in group_contexts
            ]

        # Cross-validate: all provided GDs should agree on group membership
        all_reported = [ctx.get("redundancy_group", []) for ctx in group_contexts if ctx.get("redundancy_group")]
        if len(all_reported) > 1:
            ref_names = {m["name"] for m in all_reported[0]}
            for other in all_reported[1:]:
                if {m["name"] for m in other} != ref_names:
                    print(f"  [WARNING] Triplet {n}: Redundancy group membership differs across provided GDs.")
                    break
            # Check per-node status consistency
            for member in canonical_group:
                statuses = set()
                for grp in all_reported:
                    for m in grp:
                        if m["name"] == member["name"]:
                            statuses.add(m["status"])
                            break
                if len(statuses) > 1:
                    print(f"  [WARNING] Triplet {n}: Node '{member['name']}' has conflicting statuses across GDs: {statuses}")

        # Enrich node types first, then sort by enriched type (Primary → Backup → Monitor)
        entries = []
        for member in canonical_group:
            name      = member["name"]
            node_type = _enrich_node_type(member, ctx_by_name)
            status    = member["status"]
            has_gd    = name in ctx_by_name
            info      = "" if has_gd else "Missing GD"
            if node_type.lower() == "monitor":
                ha_role = "N/A"
            else:
                ctx = ctx_by_name.get(name)
                ha_role = broker_site_label(ctx) if ctx else ""
            entries.append((node_type, ha_role, name, status, info))
        entries.sort(key=lambda e: _node_type_rank(e[0]))

        rows = []
        brokers_json = []
        for (node_type, ha_role, name, status, info) in entries:
            rows.append([node_type, ha_role, name, status, info])
            brokers_json.append({
                "router_name": name,
                "node_type":   node_type,
                "status":      status,
                "missing_gd":  info == "Missing GD",
            })

        mode_label = ""
        for ctx in group_contexts:
            mode = ctx.get("redundancy_mode", "")
            if mode and mode not in ("N/A", "Unknown"):
                mode_label = f" - {mode}"
                break

        print(f"\n  HA Triplet {n}{mode_label}")
        print(_draw_table(TRIPLET_HEADERS, [rows]))
        triplets_json.append({"triplet_number": n, "brokers": brokers_json})

    return triplets_json


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

    platform_types = [c.get("platform_type", "appliance") for c in contexts]
    is_software = platform_types.count("software") > len(platform_types) / 2
    monitor_ctxs = [c for c in contexts if c.get("is_monitor")] if is_software else []

    REPL_HEADERS = ["Repl Site Status", "Router", "HA Role", "Info"]

    def _repl_rows_for_group(group):
        """Build display rows for a site group (appliance path). Missing mate appended inline."""
        rows = []
        for ctx in sorted(group, key=_broker_order):
            rows.append([ctx.get("replication_site", ""), ctx["router_name"], broker_site_label(ctx), ""])
        if len(group) == 1:
            mate = group[0].get("mate_router", "")
            if mate:
                role = group[0].get("redundancy_role", "")
                missing_role = "Backup" if role == "Primary" else "Primary" if role == "Backup" else "Mate"
                rows.append(["-", mate, missing_role, "Missing GD"])
        return rows

    def _find_monitor(ag, bg):
        """Find the monitor node belonging to the same HA triplet as this replication pair."""
        pair_names = set()
        for g in [ag, bg]:
            if g:
                pair_names.update(c["router_name"] for c in g)
        for mon in monitor_ctxs:
            mon_names = {m["name"] for m in mon.get("redundancy_group", [])}
            mon_names.add(mon["router_name"])
            if pair_names & mon_names:
                return mon
        return None

    pairs_json = []
    for n, (ag, bg) in enumerate(matched_pairs, 1):
        if ag is None and bg is None:
            continue

        print(f"\n  Replication Pair {n}")
        pair = {"pair_number": n}

        if is_software:
            # Software brokers: all present nodes first (including monitor), missing GD rows at end
            present_rows = []
            missing_rows = []
            all_present_names = {c["router_name"] for g in [ag, bg] if g for c in g}

            # Consolidated repl site status: prefer any non-Down value; fall back to Down
            all_ha_ctxs = sorted([ctx for g in [ag, bg] if g for ctx in g], key=_broker_order)
            site_statuses = [ctx.get("replication_site", "") for ctx in all_ha_ctxs]
            non_down = [s for s in site_statuses if s and s.lower() != "down"]
            consolidated_site = non_down[0] if non_down else ("Down" if any(site_statuses) else "")

            # Only the AD-Active broker shows the consolidated status
            ad_active = next((ctx["router_name"] for ctx in all_ha_ctxs if ctx.get("redundancy_role") == "Active"), None)

            for ctx in all_ha_ctxs:
                repl_site = consolidated_site if ctx["router_name"] == ad_active else ""
                present_rows.append([repl_site, ctx["router_name"], broker_site_label(ctx), ""])

            # Missing HA mate within a site group
            for group in [ag, bg]:
                if group is None or len(group) != 1:
                    continue
                mate = group[0].get("mate_router", "")
                if mate and mate not in all_present_names:
                    role = group[0].get("redundancy_role", "")
                    missing_role = "Backup" if role == "Primary" else "Primary" if role == "Backup" else "Mate"
                    missing_rows.append(["-", mate, missing_role, "Missing GD"])

            monitor = _find_monitor(ag, bg)
            if monitor:
                present_rows.append(["", monitor["router_name"], "N/A", ""])

            if ag is not None:
                pair["active_site"] = _group_to_json(ag) + _missing_mate_json(ag)
            if bg is not None:
                pair["standby_site"] = _group_to_json(bg) + _missing_mate_json(bg)

            # Infer missing entire opposite site
            if ag is not None and bg is None:
                repl_mate = ag[0].get("replication_mate", "")
                if repl_mate and repl_mate not in all_present_names and not any(r[1] == repl_mate for r in missing_rows):
                    missing_rows.append(["-", repl_mate, "-", "Missing GD"])
                    pair["standby_site"] = [{"router_name": repl_mate, "missing_gd": True}]
            elif bg is not None and ag is None:
                repl_mate = bg[0].get("replication_mate", "")
                if repl_mate and repl_mate not in all_present_names and not any(r[1] == repl_mate for r in missing_rows):
                    missing_rows.append(["-", repl_mate, "-", "Missing GD"])
                    pair["active_site"] = [{"router_name": repl_mate, "missing_gd": True}]

            row_groups = [g for g in [present_rows, missing_rows] if g]

        else:
            # Appliances: original site-grouped structure, reordered columns
            primary_rows = []
            backup_rows = []

            if ag is not None:
                primary_rows = _repl_rows_for_group(ag)
                pair["active_site"] = _group_to_json(ag) + _missing_mate_json(ag)
            if bg is not None:
                backup_rows = _repl_rows_for_group(bg)
                pair["standby_site"] = _group_to_json(bg) + _missing_mate_json(bg)

            # Infer missing opposite site from replication_mate
            if ag is not None and not backup_rows:
                repl_mate = ag[0].get("replication_mate", "")
                if repl_mate:
                    backup_rows = [["-", repl_mate, "-", "Missing GD"]]
                    pair["standby_site"] = [{"router_name": repl_mate, "missing_gd": True}]
            elif bg is not None and not primary_rows:
                repl_mate = bg[0].get("replication_mate", "")
                if repl_mate:
                    primary_rows = [["-", repl_mate, "-", "Missing GD"]]
                    pair["active_site"] = [{"router_name": repl_mate, "missing_gd": True}]

            row_groups = [g for g in [primary_rows, backup_rows] if g]

        if row_groups:
            print(_draw_table(REPL_HEADERS, row_groups))

        pairs_json.append(pair)
    return pairs_json


def validate_ha_pairs(contexts: list):
    if not contexts:
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

    HA_HEADERS = ["Active-Standby Role", "Redundancy Role", "Router", "Info"]

    def _ha_row(ctx, missing_role=None):
        if missing_role is not None:
            return [missing_role, "-", ctx["router_name"], "Missing GD"]
        ad_role = f"AD-{ctx['redundancy_role']}" if ctx.get("redundancy_role") else ""
        return [
            ctx.get("active_standby_role", ""),
            ad_role,
            ctx["router_name"],
            "",
        ]

    pairs_json = []
    for n, (kind, ctx1, ctx2) in enumerate(pairs, 1):
        mode = ctx1.get("redundancy_mode", "") if kind == "solo" else (ctx1.get("redundancy_mode") or (ctx2.get("redundancy_mode") if ctx2 else "") or "")
        mode_label = f" - {mode}" if mode and mode not in ("N/A", "Unknown") else ""
        print(f"\n  HA Pair {n}{mode_label}")
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

    args = sys.argv[1:]
    output_dir = None
    if "--output-dir" in args:
        idx = args.index("--output-dir")
        output_dir = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if not args:
        print("Usage:")
        print("  python establish_context.py <folder> [folder2] ... [--output-dir <dir>]")
        sys.exit(1)

    folders = list(dict.fromkeys(Path(a) for a in args))
    for f in folders:
        if not f.exists() or not f.is_dir():
            print(f"[ERROR] Folder not found: {f}")
            sys.exit(1)

    data_dir = output_dir if output_dir is not None else Path(__file__).parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    tee = _Tee(data_dir / "context_output.txt")
    sys.stdout = tee

    # Build all contexts first so we can cross-reference mates
    # results is a list of (folder, ctx_or_None) — None means cli-diagnostics.txt missing
    results = []
    for folder in folders:
        try:
            ctx = extract_context(folder)
            results.append((folder, ctx))
        except FileNotFoundError:
            results.append((folder, None))

    contexts = [ctx for _, ctx in results if ctx is not None]

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

    try:
        _plugin_json = Path(__file__).parent.parent / ".claude-plugin" / "plugin.json"
        _version = "v" + json.load(open(_plugin_json)).get("version", "?")
    except Exception:
        _version = ""

    print("=" * 50)
    print(f"Solace Broker Context ({_version})" if _version else "Solace Broker Context")
    print("=" * 50)

    multi = len(results) > 1
    for i, (folder, ctx) in enumerate(results, 1):
        label = f"Broker {i}" if multi else ""
        if ctx is None:
            header = f"Broker Context — {label} - No cli-diagnostics.txt" if label else "Broker Context — No cli-diagnostics.txt"
            print(f"\n{header}")
            print("-" * 50)
        else:
            print_context(ctx, label)

    repl_pairs = validate_replication_pairs(contexts)

    # Choose HA validation strategy based on platform type of provided brokers
    platform_types = [ctx.get("platform_type", "appliance") for ctx in contexts]
    is_software = platform_types.count("software") > len(platform_types) / 2

    ha_pairs = []
    ha_triplets = []
    if is_software:
        ha_triplets = validate_ha_triplets(contexts)
    else:
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

    if ha_triplets:
        ha_path = data_dir / "HA_triplet_validation.json"
        with open(ha_path, "w") as f:
            json.dump(ha_triplets, f, indent=2)
        print(f"HA triplets written to {ha_path}")

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
