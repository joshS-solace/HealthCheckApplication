---
description: Run health checks on one or more appliances by router name. Usage - /support-health-check:analyze <router-name> <router-name2> ...
---
Run health checks for the following router names: $ARGUMENTS

If $ARGUMENTS is empty, respond with:

```
Please provide one or more router names to analyze.

**Usage:**
/support-health-check:analyze <router-name> [router-name2] ...

**Examples:**
/support-health-check:analyze ROUTER01
/support-health-check:analyze ROUTER01 ROUTER02
/support-health-check:analyze all
```

Then stop — do not proceed with the steps below.

IMPORTANT: Do NOT use Glob or Search to find scripts. All scripts are at a known, fixed path. Use this exact path for all script references below:
`${CLAUDE_SKILL_DIR}/scripts`

Steps:
1. Use the Read tool (NOT Bash or Python) to read `${CLAUDE_SKILL_DIR}/program_data/router_context.json`. Parse it directly as JSON — it is a plain **array**, not a dict. Do NOT access `data['routers']` or any dict key. Each element has `router_name` and `full_path` fields. If $ARGUMENTS is `all`, use every router in the array. Otherwise find the `full_path` for each named router; warn and skip any name not found.
2. For each matched router, run the full pipeline. When running multiple routers: start ALL health check scripts as background tasks in one message, then immediately issue TaskOutput for **every task simultaneously in a single message** as parallel tool calls — never call TaskOutput for one router, wait for its result, and then call TaskOutput for another. If you wait for one before calling the others, the remaining background tasks will complete and their output will be consumed by inline notifications, making those task IDs invalid.
   a. Run `python ${CLAUDE_SKILL_DIR}/scripts/health_check.py [full_path] --router-name [router_name] --output-dir "${CLAUDE_SKILL_DIR}/program_data"`. Once output is collected, read `${CLAUDE_SKILL_DIR}/program_data/health_check_output_[router_name].txt`. Display the result using this format:
      - Print the router name as a bold header (e.g. `**FFGCGEMEASOLAPL01P**`)
      - Under it, list every `[INFO]`, `[WARNING]`, `[FAIL]`, and `[GREP MATCH]` line from the file — indented, flat (no section headers).
      - Deduplicate: if an identical line appears in both the health check section and the troubleshoot report section, only show it once
      - Skip section header lines (`[Section X]`), report header lines (`=== Health Check Troubleshoot Report ===`), `Results written to`, `[INFO] Fails written to`, and `Health check completed...` lines
      - If there are no `[FAIL]` or `[WARNING]` lines, output: `**[router_name]**`, then `  Appliance is Healthy`, then `  The KBA was not searched as the router passed the health check.`
   b. Use the Read tool to read `${CLAUDE_SKILL_DIR}/program_data/health_check_results_[router_name].json`. The structure is `{"reference_date": "...", "overall": "...", "results": [...]}` where `results` is an array of `{"section": "...", "description": "...", "status": "...", "skip_kba": bool}` objects. Note which entries have `"status": "FAIL"` and `"skip_kba": false` — these are the FAILs that require a Confluence search.
3. If no router names matched, list the available router names from `router_context.json` and ask the user to try again.
4. After all health check outputs, for each router: if it has no FAILs, output `The KBA was not searched as the router passed the health check.` and skip to the next router. For FAILs where `skip_kba` is `true`, output the FAIL message as-is but do not search Confluence — the failure is self-diagnosing. Otherwise, **search Confluence for all `skip_kba: false` FAILs in parallel** — issue all `mcp__atlassian__searchAtlassian` calls simultaneously in a single message, one per FAIL. Each FAIL is independent and searches different KBAs. Use a natural language query describing the failure — this gives better results than CQL. Once all search results are back, **fetch all page content simultaneously** — issue every `mcp__atlassian__getConfluencePage` call in a single message. Include the page title, URL, and the relevant troubleshooting steps in your response.

   When presenting the content of any fetched Confluence page, scan the full page body for **Customer Exception** blocks or similar special-handling notices (e.g. paragraphs beginning with "Customer Exception:", "If [Customer] report a...", "please send [person] an out of band heads up", "This is a drastic action", or any escalation/notification requirement directed at Support staff). If any such text is found, **always include it verbatim at the top of that page's section**, formatted as:
   > **Note:** [exact text of the exception/notice]

   Do not add any commentary, applicability notes, or parenthetical remarks below the Note block — include only the verbatim text from the page.

   **If the Confluence search fails or returns an auth error**, tell the user:
   > The Atlassian MCP is not authenticated. To enable Confluence search:
   > 1. Run `/mcp` inside Claude Code
   > 2. Select **atlassian** and choose **Authenticate**
   > 3. Complete the OAuth login in your browser
   > 4. Re-run `/support-health-check:analyze` once authenticated

5. After reading all KBA pages in step 4, scan every page for **diagnostic conditions** — checks the page says to perform against logs or CLI output to determine which resolution path applies (e.g. "grep debug.log for X", "look for Y in system.log", "check if Z is present in the SEL"). Collect every diagnostic condition from every page first, then **run all grep calls simultaneously in a single message** — do not grep one condition at a time. For each condition:
   a. Derive the log file path directly from the router's `full_path` and `platform_type` — do NOT use Glob:
      - `appliance`: `{full_path}/usr/sw/jail/logs/{filename}`
      - `software`: `{full_path}/container_solace/usr/sw/jail/logs/{filename}`
   b. Once all grep results are back, add a **Diagnostic check results** subsection under each KBA's troubleshooting steps. For every condition checked, state the outcome explicitly:
      - If matches found: show the relevant lines and state what this implies (e.g. "Zippy Housing signature **found** — proceed with RND Jira rather than straight PSU replacement").
      - If no matches: explicitly state it was not found and what this rules out (e.g. "Zippy Housing SEL signature **not found** in debug.log — Zippy Housing issue does not apply; proceed directly to PSU replacement").
   Always report both positive and negative results — never silently skip a check.
