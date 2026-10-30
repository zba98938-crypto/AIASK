# launchd templates (not installed)

These are operator templates only. They were not copied into `~/Library/LaunchAgents`,
loaded, bootstrapped, or enabled as part of the 2026-10-29 work.

1. Replace `REPLACE_WITH_AIASK_ROOT` and `REPLACE_WITH_RUNTIME_HOME` in both
   plist files, then run `plutil -lint` on the rendered copies.
2. Review the command line and environment against the approved `.env` changes
   and G2 runtime-start confirmation. The factory supervisor and sidecars can
   write the authoritative database and call the LLM gateway.
3. Only after explicit operator authorization, copy the rendered files to
   `~/Library/LaunchAgents/`, then use `launchctl bootstrap gui/$(id -u) ...`.
4. To stop, use `launchctl bootout gui/$(id -u) ...`; verify child PIDs and
   heartbeat/effective status rather than trusting launchd's last state.

Do not install or load these templates as part of code/test work.
