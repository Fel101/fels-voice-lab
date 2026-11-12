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

**Check for updates** and `update.bat` contact GitHub only when you start them, or once a day if you tick **Check when the
Lab opens**. The check asks `api.github.com` for the newest release; the request names the Lab's version and nothing about
you or your library. `update.bat` takes files only from github.com over HTTPS, refuses a release whose `app_version.py` does
not match its tag, and keeps a copy of everything it replaces. Releases are not signed, so an update trusts this GitHub
repository just as the first download did.

`tools/check_repo.py` is a heuristic check of the files Git would publish, not a
guarantee that every possible secret, dependency vulnerability or rights issue is
absent. It finds private keys and provider tokens, personal Windows home paths,
recordings, model files, databases and logs, and Markdown links to pages that are
missing. It looks for personal names or other words only if you list them in a
file and pass it with `--deny-file`.

## Verify a download

From 0.3.0 each release carries `FelsVoiceLab-Setup-<version>.exe` and `SHA256SUMS`, which the release workflow builds from
the release's tag, with a [GitHub attestation](https://docs.github.com/actions/security-for-github-actions/using-artifact-attestations)
that records which workflow run built both. In PowerShell, in the folder you downloaded them to:

```powershell
Get-FileHash .\FelsVoiceLab-Setup-0.3.0.exe -Algorithm SHA256
Get-Content .\SHA256SUMS
```

The two hashes must be the same. With the [GitHub CLI](https://cli.github.com/),
`gh attestation verify FelsVoiceLab-Setup-0.3.0.exe --repo Fel101/fels-voice-lab` checks that this repository's release
workflow built the file. Setup.exe is not signed yet, so Windows SmartScreen warns before it runs; until it is, the hash and
the attestation are how to check it.

## Reporting a security problem

Report it privately, not in a public issue: use
[GitHub's form for reporting a vulnerability](https://github.com/Fel101/fels-voice-lab/security/advisories/new)
(or **Report a vulnerability** on the repository's **Security** tab). You need to be
signed in to GitHub. Only the maintainer sees the report, and you can discuss it there
until a fix is out. If you cannot use the form, open an issue asking for a private
contact, with no exploit details or personal data in it. Never put credentials or
personal recordings in a public issue.
