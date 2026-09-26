---
description: Run the sandcoder specialist panel on this project (default - security audit + tests)
argument-hint: [skills or what to check]
---

Use the sandcoder MCP tools to run a specialist panel on the current project.

Request from the user: $ARGUMENTS

If the request names skills (security-auditor, test-writer, bug-reproducer, feature-researcher),
use those; otherwise use security-auditor and test-writer. Detect the test command and dependency
setup from the project files. Start the run with panel_run, keep helping while it runs, then collect
results with panel_results and report them following the sandbox-panel skill's safety rules.
