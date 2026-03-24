# support-health-check

A Claude Code plugin for running Solace broker health checks from gather-diagnostics/gather-diagnostics-host bundles. Supports both appliance and software broker deployments.

## Installation

Install from the support marketplace inside Claude Code:

```
/plugin marketplace add SolaceDev/support-marketplace
/plugin install support-health-check@support-marketplace
```

## Usage

Run from the directory containing your gather-diagnostics files (e.g. a ticket folder):

```
/support-health-check:initialize [gd1] [gd2] ...
```

With no arguments, the plugin auto-discovers gather-diagnostics files (`*.tgz.p7m`, `*.tgz`, extracted folders) in the current directory. With arguments, those paths are used directly.

```
/support-health-check:analyze <router-name> [router-name2] ...
/support-health-check:analyze all
```

Runs health checks on the named routers and searches Confluence for KBAs matching any failures.

## Permissions setup

The plugin runs Python scripts on your behalf. To avoid approval prompts on every run, add the following to your project's `.claude/settings.json` (create it if it doesn't exist):

```json
{
  "permissions": {
    "allow": [
      "Bash(python *)",
      "Bash(python3 *)",
      "Bash(head *)",
      "Bash(tail *)",
      "mcp__atlassian__searchAtlassian",
      "mcp__atlassian__getConfluencePage",
      "mcp__atlassian__getJiraIssue"
    ]
  }
}
```

If `.claude/settings.json` already exists, merge the `allow` entries in.

## Atlassian MCP (Confluence Search)

The plugin includes a `.mcp.json` that configures the Atlassian MCP server for Claude Code. This enables Confluence KBA search during analysis.

### First-time setup

1. Open Claude Code in your working directory
2. Run `/mcp` inside Claude Code
3. Select **atlassian** and choose **Authenticate**
4. Complete the OAuth login in your browser

Once authenticated, Claude will automatically search Confluence for relevant KBAs when a health check section fails.
