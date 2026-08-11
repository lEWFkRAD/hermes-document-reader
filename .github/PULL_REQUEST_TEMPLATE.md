## Summary

Describe the problem and the smallest complete change that solves it.

## Validation

- [ ] `npm run validate`
- [ ] `python -m pytest tests -q --basetemp ../document-reader-pytest`
- [ ] Python compilation across plugin, dashboard, engine, installer, MCP, service, tests, and viewer
- [ ] Dependency audits completed or an exact blocker is documented
- [ ] Service dependency changes regenerate and review both hashed Windows locks
- [ ] No live OCR endpoint, scheduled task, private profile, or real document was used

## Security and compatibility

- [ ] Profile isolation and fail-closed behavior are preserved
- [ ] No credentials, private paths, hostnames, documents, OCR output, or logs are included
- [ ] Windows and non-Windows behavior was considered
- [ ] Release metadata remains synchronized and the changelog remains `Unreleased`

## Contribution statement

- [ ] Every commit has a matching `Signed-off-by` trailer
- [ ] Material AI assistance is disclosed below

AI assistance:
