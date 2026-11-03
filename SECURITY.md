# Security

Fel's Voice Lab is a single-user local application. The HTTP server binds to
`127.0.0.1`; foreign Host/Origin requests are rejected, and so is any request another
website's page makes (its `Sec-Fetch-Site`), except opening the Lab's pages in a tab.
Its pages may not be shown inside another site's frame. A path in a request is checked
against the Lab's folders before Windows opens it. It has no account system,
TLS termination, public-service authentication or multi-user isolation. Do not
expose it through a public bind address, reverse proxy or tunnel.

Recordings, transcripts, consent notes, voice embeddings, generated speech and
logs can contain personal information. They are kept in `data/`, `out/` and
`backups/` (or the state root you choose), and the repository holds none of them.
Back up private stores as described in [the guide](docs/USER_GUIDE.md).
Local filesystem access is trusted; same-user malware is outside this boundary.

Do not put credentials or personal recordings in a public issue. Report security
problems through GitHub's private vulnerability reporting feature after the
maintainer enables it. If that feature is unavailable, open an issue requesting
a private contact without including exploit details or personal data.

`tools/check_repo.py` is a heuristic check of the files Git would publish, not a
guarantee that every possible secret, dependency vulnerability or rights issue is
absent. It finds private keys and provider tokens, personal Windows home paths,
recordings, model files, databases and logs, and Markdown links to pages that are
missing. It looks for personal names or other words only if you list them in a
file and pass it with `--deny-file`.
