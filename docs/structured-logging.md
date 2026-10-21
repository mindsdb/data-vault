# Diagnostics

Application console/file diagnostics default to WARNING and JSON. `LOG_LEVEL`
sets their shared threshold; existing `MINDSDB_CONSOLE_LOG_LEVEL` and
`MINDSDB_FILE_LOG_LEVEL` overrides retain precedence. Temporarily set a logger
threshold to INFO/DEBUG, reproduce, then restore WARNING. These controls do not
change the CLI/framework debug setting.

The HTTP boundary supplies UUID/32-hex `x-request-id` correlation, retained across
the WSGI thread bridge. It emits final 4xx/5xx and streamed exceptions. Successful
GET/HEAD `/api/util/ping` and Docker `/api/status` probes are quiet, including
with access logging enabled. Failed probes remain visible. `ACCESS_LOG_MODE=errors` is the default;
`all` retains ordinary successes independently of the diagnostic threshold.
Native Uvicorn access is disabled because it contains raw targets.

SQL/provider message values and arbitrary extras are excluded. OTLP receives the
same safe JSON body and cannot separately serialize raw exception values. Verify
that the live collector parses that JSON body before rollout. Database audit,
query results and other protocol outputs remain their own streams. Non-HTTP
authentication failures retain their warning diagnostics.

Diagnostics use `logged_at`, `logger`, `log_level`, and `message`. Explicit events,
reasons, source locations and correlation fields identify failures. Exception
values, source lines, locals, arbitrary extras, HTTP queries and provider bodies
are excluded. Use the structured fields when querying logs; the previous text
prefixes are no longer a parsing contract.

Enable extra detail only for an owned troubleshooting session, repeat the failing
operation, and restore the default afterward. Logging controls do not enable
framework debug responses. Roll back a release by restoring its previous image
or package version and its previous logging settings together. Before promotion,
verify collector parsing, saved searches, alerts and retained operational streams
against the emitted records. A passing unit suite does not establish deployment
or collector compatibility.
