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

Steps:
1. Read `data/router_context.json`. If $ARGUMENTS is `all`, use every router in the file. Otherwise find the `full_path` for each named router; warn and skip any name not found. Use Glob to find `health_check.py` in the workspace — this gives its absolute path. Call its directory `<project_root>`.
2. For each matched router, run the full pipeline. You may run multiple routers in parallel since each writes its own results file:
   a. Run `python <project_root>/health_check.py <full_path> --router-name <router_name>`, then read `<project_root>/data/health_check_output_<router_name>.txt`. Display the result using this format:
      - Print the router name as a bold header (e.g. `**FFGCGEMEASOLAPL01P**`)
      - Under it, list every `[INFO]`, `[WARNING]`, `[FAIL]`, and `[GREP MATCH]` line from the file — indented, flat (no section headers)
      - Deduplicate: if an identical line appears in both the health check section and the troubleshoot report section, only show it once
      - Skip all lines from `[Section 1.1]` and `[Section 1.2]` blocks, and from `[1.1]`/`[1.2]` blocks in the troubleshoot report
      - Skip section header lines (`[Section X]`), report header lines (`=== Health Check Troubleshoot Report ===`), `Results written to`, `[INFO] Fails written to`, and `Health check completed...` lines
      - If there are no non-1.1/1.2 `[FAIL]` or `[WARNING]` lines, output only: `**<router_name>**` then `  [INFO] Appliance is Healthy`
   b. Read `data/health_check_results_<router_name>.json` and note whether any entry has `"status": "FAIL"` outside of sections 1.1 and 1.2 (for deciding which FAILs to search Confluence for).
3. If no router names matched, list the available router names from `router_context.json` and ask the user to try again.
4. After all health check outputs, search Confluence via the Atlassian MCP for KBAs matching each non-1.1/1.2 FAIL. Use the `mcp__atlassian__search` tool (Rovo Search) with a natural language query describing the failure — this gives better results than CQL. For each result, fetch the page content with `mcp__atlassian__getConfluencePage` and include the page title, URL, and the relevant troubleshooting steps in your response. **Skip sections 1.1 and 1.2 entirely — do not output anything about them.**

   When presenting the content of any fetched Confluence page, scan the full page body for **Customer Exception** blocks or similar special-handling notices (e.g. paragraphs beginning with "Customer Exception:", "If [Customer] report a...", "please send [person] an out of band heads up", "This is a drastic action", or any escalation/notification requirement directed at Support staff). If any such text is found, **always include it verbatim at the top of that page's section**, formatted as:
   > **Note:** <exact text of the exception/notice>

   Do not add any commentary, applicability notes, or parenthetical remarks below the Note block — include only the verbatim text from the page.

   **If the Confluence search fails or returns an auth error**, tell the user:
   > The Atlassian MCP is not authenticated. To enable Confluence search:
   > 1. Run `/mcp` inside Claude Code
   > 2. Select **atlassian** and choose **Authenticate**
   > 3. Complete the OAuth login in your browser
   > 4. Re-run `/support-health-check:analyze` once authenticated

5. For each KBA fetched in step 4, scan the page content for **diagnostic conditions** — these are checks the page says to perform against logs or CLI output to determine which resolution path applies (e.g. "grep debug.log for X", "look for Y in system.log", "check if Z is present in the SEL"). For each such condition:
   a. Locate the relevant log file(s) under the router's `full_path` using Glob (log files may be in a nested subfolder — use `**/<filename>` to find them).
   b. Run the grep using the Grep tool.
   c. After the KBA troubleshooting steps, add a **Diagnostic check results** subsection. For every condition checked, state the outcome explicitly:
      - If matches found: show the relevant lines and state what this implies (e.g. "Zippy Housing signature **found** — proceed with RND Jira rather than straight PSU replacement").
      - If no matches: explicitly state it was not found and what this rules out (e.g. "Zippy Housing SEL signature **not found** in debug.log — Zippy Housing issue does not apply; proceed directly to PSU replacement").
   Always report both positive and negative results — never silently skip a check.
