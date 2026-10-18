# Security

Report vulnerabilities privately through the repository's security advisory feature. Do not post tokens, webhook
URLs, webhook keys or host details in a public issue.

t3-done-notify reads T3 thread snapshots with an existing read-only token. When Claude Code or Codex
watching is enabled, it also reads those tools' local session transcripts. It posts JSON to the webhook you
configure. Keep the env file readable only by its owner (mode 600). Webhook payloads contain thread titles and
message text, so send them only to a receiver you trust, over HTTPS when it is not on the same machine. Redirects are
refused and response bodies are not logged.
