---
description: Decrypt/extract gather-diagnostics files and establish broker context. Usage - /support-health-check:initialize <gd1> <gd2> ...
---
Process the following gather-diagnostics inputs and establish broker context: $ARGUMENTS

Steps:
1. Use Glob to find `handle_gather_diagnostics.py` in the workspace — this gives its absolute path. Call its directory `<project_root>`. Run `python <project_root>/handle_gather_diagnostics.py $ARGUMENTS` **in the background** (use `run_in_background: true`). Note the output file path returned in the result.
2. Immediately run a Bash polling loop (do NOT use TaskOutput) to tail the output file and watch for the auth prompt. Use this exact pattern:
   ```bash
   output_file="<path from step 1>"
   for i in $(seq 1 60); do
     sleep 3
     content=$(cat "$output_file" 2>/dev/null)
     if echo "$content" | grep -q "Please visit the following URL"; then
       echo "AUTH_PROMPT_FOUND"
       echo "$content" | grep -A2 "Please visit the following URL"
       break
     fi
     if echo "$content" | grep -q "Extracted:"; then
       echo "ALREADY_DONE"
       echo "$content"
       break
     fi
   done
   ```
   As soon as you see `AUTH_PROMPT_FOUND`, extract the URL and code from the grep output and display them to the user in a prominent block:
   ```
   ACTION REQUIRED — Microsoft authentication needed:
     URL:  <URL>
     Code: <CODE>
   ```
   Tell the user: "Please open the URL above, enter the code, and authenticate. I will continue automatically once the process completes."
3. After showing the auth prompt (or if `ALREADY_DONE` was seen), poll the output file in a second Bash loop until `Extracted:` appears or the process clearly failed (look for `[ERROR]` with no further progress):
   ```bash
   output_file="<path from step 1>"
   for i in $(seq 1 60); do
     sleep 5
     content=$(cat "$output_file" 2>/dev/null)
     if echo "$content" | grep -q "Extracted:"; then
       echo "$content"
       break
     fi
   done
   ```
4. Parse the `Extracted:` section from the output to get the folder name(s). Resolve the full path for each — it lives next to its input file (`Path(arg).parent / folder_name`), falling back to the current directory.
5. Run `python <project_root>/establish_context.py <folder-path-1> <folder-path-2> ...` with all resolved paths.
6. Read `<project_root>/data/context_output.txt` using the Read tool and paste its full contents verbatim as plain text in your response (do not summarize or restate it — paste it exactly as-is, inside a code block). Output nothing else.
