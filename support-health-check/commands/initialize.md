---
description: Decrypt/extract gather-diagnostics files and establish broker context. Usage - /support-health-check:initialize [gd1] [gd2] ...
---
Process gather-diagnostics inputs and establish broker context.

IMPORTANT: Do NOT use Glob or Search to find scripts. All scripts are at a known, fixed path. Use this exact path for all script references below:
`./support-health-check/scripts`

Steps:
1. Run `python ./support-health-check/scripts/handle_gather_diagnostics.py $ARGUMENTS` **in the background** (use `run_in_background: true`).
   - If `$ARGUMENTS` is empty, the script auto-discovers gather-diagnostics files (*.tgz.p7m, *.tgz, and extracted folders) in the current working directory.
   - If `$ARGUMENTS` contains paths, those are used directly.
2. Use `TaskOutput` (blocking) to wait for the background process to complete. Once the output is returned:
   - If it contains `Please visit the following URL`, extract and display the URL and code to the user in a prominent block:
     ```
     ACTION REQUIRED — Microsoft authentication needed:
       URL:  [the URL from the output]
       Code: [the code from the output]
     ```
     Tell the user: "Please open the URL above, enter the code, and authenticate. I will continue automatically once the process completes."
     Then call `TaskOutput` again (blocking) to wait for the process to finish after authentication.
3. Parse the `Extracted:` section from the final output to get the folder name(s). Resolve the full path for each extracted folder:
   - If `$ARGUMENTS` was provided: use `Path(arg).parent / folder_name`, falling back to the current directory.
   - If `$ARGUMENTS` was empty (auto-discovery): the folder is in the current working directory, so use `Path('.') / folder_name` resolved to absolute.
4. Run `python ./support-health-check/scripts/establish_context.py [folder-path-1] [folder-path-2] ... --output-dir .` (the `--output-dir .` writes output files to the current working directory).
5. Read `./context_output.txt` using the Read tool and paste its full contents verbatim as plain text in your response (do not summarize or restate it — paste it exactly as-is, inside a code block). Output nothing else.
