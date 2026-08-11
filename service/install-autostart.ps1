Write-Error @'
This legacy autostart installer is retired and performs no changes.

It used one fixed SYSTEM task/port and could stop a foreign listener. Use the
profile-aware, receipt-guarded lifecycle instead:

  hermes document-reader install
  hermes document-reader status

For a named profile, put `-p <profile>` immediately after `hermes`.
Hermes Agent 0.20.0 or newer is required.
'@
exit 2
