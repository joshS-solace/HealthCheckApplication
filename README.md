# Solace Appliance Health Check

## Setup

```bash
python -m pip install -r requirements.txt
```

## Running

```bash
# Full orchestrated workflow (file picker if no args)
python run_health_check_application.py [gd-folder ...]

# Or use the plugin commands inside Claude Code
/health-check:initialize <gd1> <gd2>
/health-check:analyze <router-name>
```

---

## Environment Variables

### Anthropic API Key (required for LLM features)

```bash
llm keys set anthropic
# Enter your Anthropic API key when prompted
```

---

## Atlassian MCP (Confluence Search)

The project includes a `.mcp.json` that configures the [Atlassian MCP server](https://mcp.atlassian.com) for Claude Code. This enables Claude to search Confluence directly using natural language rather than raw API calls.

### First-time setup

1. Open Claude Code in this project directory
2. Run `/mcp` inside Claude Code
3. Select **atlassian** and choose **Authenticate**
4. Complete the OAuth login in your browser — you'll be redirected back automatically

### Verify it's connected

```bash
claude mcp list
```

Once authenticated, Claude can search Confluence natively during health check analysis (e.g. looking up KBAs for FAIL results).