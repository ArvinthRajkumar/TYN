# Security

## Secrets

- Configuration comes only from environment variables or a local `.env`
  file, loaded and validated by `tyn/config.py`. `.env` is git-ignored and
  never committed; `.env.example` holds placeholders only.
- Secrets (`JIRA_API_TOKEN`, `GROQ_API_KEY`, and `JIRA_EMAIL`) are held as
  `SecretStr`, so they do not appear in reprs, logs or error messages.
  Configuration errors name the variable, never its value.
- Use a dedicated, revocable Atlassian API token for this tool and revoke it
  when the assessment is over. The Groq key should also be dedicated.
- A logging filter masks the configured values, the basic-auth header and
  generic credential patterns in every log line and exception.
- Report, trace and metrics files are redacted before they are written and
  verified afterwards. The check takes its values from the environment, so
  nothing secret is stored in code.
- `detect-secrets` runs as a pre-commit hook and in CI over the working tree
  and every blob in git history (`python -m scripts.scan_secrets history`).
  `python -m scripts.scan_secrets values` looks for the exact configured
  values in the tree and history and prints only the setting name.

## Threat notes

- **Prompt injection.** All Jira text is untrusted. Tool results are wrapped
  in a `<jira_data>` envelope that ticket text cannot close, the system
  prompt forbids following instructions found there, and a detector flags
  text that addresses reviewers or dictates an outcome. Flagged text is
  reported as an observation, never obeyed.
- **Scope widening.** The model never writes JQL. Tools take structured,
  allow-listed parameters; values are escaped; the project and scope label
  are always ANDed in by code; every issue key must belong to the project;
  every fetched issue must carry the scope label. Injected text therefore
  cannot read other projects or tickets.
- **Least privilege.** The agent receives only a read-only Jira client. Only
  the importer can write to Jira.
- **Credential exfiltration.** `JIRA_BASE_URL` must be an
  `https://<site>.atlassian.net` root; TLS verification is never disabled;
  redirects are not followed, so credentials are not replayed elsewhere.
- **Resource exhaustion.** Every HTTP call has connect and read timeouts;
  retries are bounded with backoff and jitter; model turns, tool calls,
  per-requirement time and overall time are capped; token use is paced per
  minute and capped per day.
- **File writes.** Output goes only to `reports/` and `state/`, with fixed
  file names chosen by code and paths re-checked after resolution.
- **Supply chain.** Dependencies are pinned with hashes and installed with
  `--require-hashes`; `pip-audit` runs locally and in CI; GitHub Actions are
  pinned to commit SHAs and the workflow token is read-only.

## Reporting a problem

Please open a private security advisory on the GitHub repository (Security
tab, "Report a vulnerability") rather than a public issue. Include the
affected file or command and steps to reproduce. Do not include real
credentials in the report.
