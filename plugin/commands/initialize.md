---
description: Decrypt/extract gather-diagnostics files and establish broker context. Usage - /support-health-check:initialize <gd1> <gd2> ...
---
Process the following gather-diagnostics inputs and establish broker context: $ARGUMENTS

Steps:
1. Use Glob to find `handle_gather_diagnostics.py` in the workspace — this gives its absolute path. Call its directory `<project_root>`. Run `python <project_root>/handle_gather_diagnostics.py $ARGUMENTS` **in the background** (use `run_in_background: true`).
2. While the command is running, continuously tail the background output. As soon as you see lines matching the pattern:
   ```
   Please visit the following URL to authenticate:
   <URL>
   Enter the code: <CODE>
   ```
   immediately display them to the user in a prominent block like:
   ```
   ACTION REQUIRED — Vault / Microsoft authentication needed:
     URL:  <URL>
     Code: <CODE>
   ```
   Then tell the user: "Please open the URL above, enter the code, and authenticate. I will continue automatically once the process completes."
   Keep tailing until the background process exits.
3. Once the background process finishes, use only the output already returned by `TaskOutput` — do NOT read the temp output file from the tasks/ directory. Parse the "Extracted:" section of that output to get the folder name(s).
4. Resolve the full path for each extracted folder — it lives next to its input file (use `Path(arg).parent / folder_name`), falling back to the current directory.
5. Run `python <project_root>/establish_context.py <folder-path-1> <folder-path-2> ...` with all resolved paths (no redirection needed — the script writes `data/context_output.txt` itself).
6. Read `<project_root>/data/context_output.txt` using the Read tool and paste its full contents verbatim as plain text in your response (do not summarize or restate it — paste it exactly as-is, inside a code block). Output nothing else.
