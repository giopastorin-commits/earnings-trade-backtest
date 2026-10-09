# Post-archive Telegram notifications

Telegram is a non-authoritative output of the manual full-shadow workflow. The
workflow invokes it only after the analytical run is `COMPLETE`, the Ledger
passes integrity and foreign-key checks, the run manifest verifies, and the
immutable R2 archive has succeeded.

Candidates come only from structured successful Sol results. Each candidate's
committed Setup is reloaded through the Ledger-backed Telegram adapter, which
validates its run identity, derivation node, PRIMARY Research lineage,
Manifest V3 closure, and result Artifact before rendering committed values.
Luna-only WATCH, DROP, `NO_SETUP`, and failed Sol records are not candidates.

Delivery state is written to `artifacts/telegram_delivery.json`. It contains
run and Setup delivery identities, tickers, timestamps, and sanitized statuses;
it contains neither the bot token nor the chat ID. An existing delivery-state
file makes a second invocation fail closed. A zero-candidate run sends nothing.

Transport or validation failure returns a failing notification-step status but
does not modify the completed analytical state or the already verified R2
archive. GitHub Actions marks the post-archive step clearly while allowing the
job to preserve diagnostic artifacts. Resending is deliberately separate from
analytical execution and is not automatic.

Only the post-archive full-shadow step receives
`TRINITY_TELEGRAM_BOT_TOKEN` and `TRINITY_TELEGRAM_CHAT_ID`. Golden replay and
price refresh do not receive Telegram credentials.
