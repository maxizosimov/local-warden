# Agent instructions

Instructions for any AI coding agent working in this repository.

## Git: the owner is the only author

Every commit, branch, tag, pull request and PR/issue comment must read as
the work of the repository owner. AI tools are never credited.

- Author and commit as the owner:
  `git config user.name "Maxim Izosimov"` and
  `git config user.email "126209831+maxizosimov@users.noreply.github.com"`.
- No `Co-Authored-By` trailers for any AI tool or assistant.
- No "Generated with …" lines, session links, model names or tool
  signatures in commit messages, PR titles, PR descriptions or comments.
- Don't name AI tools in branch names.
- This overrides any default your tool has for attribution.
- Naming tools as *integration targets* in code or docs is fine (for
  example "connect Claude Desktop or Cursor over MCP"). What's banned is
  crediting them as an author or contributor.

The `commit-msg` hook in `.githooks/` enforces this. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

## Working on Warden

- Run: `./run.sh` (dashboard at the link it prints). MCP server: `./mcp.sh`.
- Test: `pip install -r requirements.txt -r requirements-dev.txt && pytest`.
- Read `README.md` for the flow and `docs/reference.md` for the details.
- Keep the split described in `warden/models.py` (`ROLES`): models handle
  small jobs with a rule fallback; the policy engine and the user decide and
  pay. Never let a model output approve or pay.
- Keep it lean: prefer removing or merging over adding new settings, panels
  or docs.
