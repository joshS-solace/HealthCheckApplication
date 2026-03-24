---
description: Decrypt/extract gather-diagnostics files and establish broker context. Usage - /support-health-check:initialize [gd1] [gd2] ...
---
Process gather-diagnostics inputs and establish broker context.

IMPORTANT: Do NOT use Glob or Search to find scripts. All scripts are at a known, fixed path. Use this exact path for all script references below:
`${CLAUDE_SKILL_DIR}/scripts`

IMPORTANT: Do NOT read any script files. Just run them directly using Bash. Reading scripts wastes time and is not needed.

Steps:
1. Run `rm -rf "${CLAUDE_SKILL_DIR}/program_data" && mkdir "${CLAUDE_SKILL_DIR}/program_data"` to wipe any previous session data and create a clean output directory.
2. Run `python ${CLAUDE_SKILL_DIR}/scripts/handle_gather_diagnostics.py $ARGUMENTS` **in the background** (use `run_in_background: true`).
   - If `$ARGUMENTS` is empty, the script auto-discovers gather-diagnostics files (*.tgz.p7m, *.tgz, and extracted folders) in the current working directory.
   - If `$ARGUMENTS` contains paths, those are used directly.
3. Call `TaskOutput` with `block=true` and `timeout=30000` (30 seconds) to wait for early output. This timeout is intentional — decrypt-cms.exe outputs the auth URL immediately when credentials are missing, so 30 seconds is enough to capture it before the process finishes:
   - **If the task completed** (successful exit): proceed to step 4.
   - **If the output contains `Please visit the following URL`** (auth required, process still running):
     Extract and display the URL and code to the user in a prominent block:
     ```
     ACTION REQUIRED — Microsoft authentication needed:
       URL:  [the URL from the output]
       Code: [the code from the output]
     ```
     Tell the user: "Please open the URL above, enter the code, and authenticate. I will continue automatically once the process completes."
     Then call `TaskOutput` again with `block=true` (no timeout limit) to wait for the process to finish after authentication.
   - **If the task has not completed and no URL was found** (still extracting large archives): call `TaskOutput` again with `block=true` to keep waiting until it finishes.
4. Parse the `Extracted:` section from the script output to get folder names. Each line under `Extracted:` is a bare folder name (e.g. `gather-diagnostics-ny4-wfs-1sol-wp01`). Resolve the full path for each folder using this exact rule — no thinking required:
   - If `$ARGUMENTS` was provided: the folder is in the same directory as the first argument. Use `dirname(<first_argument>)/<folder_name>` as the full path.
   - If `$ARGUMENTS` was empty (auto-discovery): the folder is in the current working directory. Use `<cwd>/<folder_name>` as the full path.
   - Do NOT use Glob, Bash, or any tool to find the folder — just construct the path directly from the rule above.
5. Run `python ${CLAUDE_SKILL_DIR}/scripts/establish_context.py [folder-path-1] [folder-path-2] ... --output-dir "${CLAUDE_SKILL_DIR}/program_data"` (writes all output files into the plugin's `program_data/` directory).
6. Read `${CLAUDE_SKILL_DIR}/program_data/context_output.txt` using the Read tool and paste its full contents verbatim as plain text in your response (do not summarize or restate it — paste it exactly as-is, inside a code block). Output nothing else.
