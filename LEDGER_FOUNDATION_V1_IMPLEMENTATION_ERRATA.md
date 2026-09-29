# Ledger Foundation V1 — Implementation Errata

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0`

Status: **NON-SEMANTIC IMPLEMENTATION CLARIFICATION**

This erratum does not supersede or modify
`LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0`.

## Forward migration interpretation

- `schema_metadata.schema_version = 1` identifies the frozen logical Ledger
  schema family/version established by the bootstrap migration. It is not a
  migration sequence number.
- `schema_migration.schema_version` is the monotonically increasing migration
  sequence: `1`, `2`, `3`, and so on. Its existing uniqueness constraint is
  retained.
- `schema_metadata` remains immutable.
- `schema_metadata.applied_migration_id` identifies the bootstrap migration
  that originally created the singleton. It is not updated by later migrations.
- The current applied migration level is derived from the immutable,
  gap-free `schema_migration` history.
- `0001_ledger_core.sql` remains byte-for-byte immutable.
- Future migrations must not recreate or reinsert `schema_metadata` unless a
  future frozen specification explicitly requires it.
