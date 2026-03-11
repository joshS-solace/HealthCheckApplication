# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt   # pyyaml

# Full orchestrated workflow (file picker if no args)
python run_health_check_application.py [gd-folder ...]

# Individual steps
python handle_gather_diagnostics.py <gd-file-or-folder> [...]
python establish_context.py <gd-folder> [gd-folder2 ...]
python health_check.py <gd-folder>
```

## Architecture

The project health-checks Solace message broker appliances from `gather-diagnostics` bundles (`.tgz` or encrypted `.tgz.p7m`).

### Orchestration flow

```
handle_gather_diagnostics  →  establish_context  →  [user selects routers]
       ↓                            ↓
  extracts/decrypts           router_context.json
  GD archives                 HA_pair_validation.json
                              replication_pair_validation.json
                                       ↓
                              health_check (per appliance)
                                       ↓
                              health_check_results.json
                              + troubleshoot report (on FAILs)
```

`run_health_check_application.py` orchestrates all steps. Each script is also independently runnable.

### Rules engine (`health_check.py`)

Health checks are entirely driven by `rules/healthcheck_rules.yaml`. Each rule targets a named section and runs one or more typed checks against `cli-diagnostics.txt` or log files.

**Check types:**
- `regex` / `contains` / `not_contains_regex` — pattern match on CLI output
- `log_grep_absent` — fail if a pattern appears in logs within a rolling time window
- `log_paired_events` — detect unresolved DOWN/FAIL events by pairing with recovery events
- `supported_version_check` / `supported_chassis_check` — lifecycle/EOL date checks
- `print_info_fields` — extract and display metadata fields (always passes)
- `alarm_check`, `hba_status_check`, `adb_status_check`, `redundancy_standalone_check`, `config_sync_status_check`, `dns_log_check`, `message_spool_status_check` — domain-specific checks

**Section grouping:** Sections with a letter component (e.g. `6.A.i`, `6.B.ii`) form an OR group under their numeric prefix — only one variant needs to pass. Purely numeric sections (`1.1`, `4.2`) are always independent. `section_group_key()` implements this logic.

**Output suppression:** Passing sections produce no output unless they generated INFO/WARNING lines during the check. Captured via `contextlib.redirect_stdout`.

`rules/further_troubleshooting_rules.yaml` defines per-section log grep steps that run only on FAILs and populate `troubleshooting_context` in results JSON.

### Context extraction (`establish_context.py`)

Parses `cli-diagnostics.txt` to extract redundancy mode, HA roles, replication topology, and mate relationships. Validates HA pairs and replication pairs across multiple GD folders. Stores `full_path` (absolute) for each broker so downstream scripts can locate files.

### Nested archive layout

GD archives typically extract to `<folder>/<folder>/cli-diagnostics.txt` (double-nested). `resolve_folder()` in `health_check.py` and `load_diagnostics()` in `establish_context.py` both detect and handle this transparently.

### Troubleshooting report (built into `health_check.py`)

When any section FAILs, `health_check.py` automatically prints a troubleshoot report (log grep matches and correlated events from `further_troubleshooting_rules.yaml`). No separate script is needed.

### Plugin commands

`plugin/` — shareable Claude Code plugin (load with `claude --plugin-dir ./plugin`):
- `/health-check:initialize <gd1> <gd2> ...` — runs handle_gather_diagnostics + establish_context
- `/health-check:analyze <router-name> ...` — runs health_check by router name from `router_context.json`
