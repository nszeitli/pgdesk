# PGDesk checklist

- [x] Inspect Oracle libraries and keyboard conventions.
- [x] Resolve standalone architecture and safe configuration.
- [x] Create repository under the personal workspace.
- [x] Implement cluster/database tabs and keyboard connection chooser.
- [x] Maintain resilient per-tab PostgreSQL connection pools.
- [x] Display pool health, connection age and payload statistics.
- [x] Implement keyboard-navigable schema/tables/views tree.
- [x] Implement independently toggleable equal-sized work panels.
- [x] Implement syntax-highlighted SQL editor.
- [x] Implement query results, errors and CSV export.
- [x] Implement schema-aware direct OpenAI SQL assistance.
- [x] Implement model, reasoning, Fast mode and custom prompt settings.
- [x] Exercise database recovery and query execution scenarios.
- [x] Launch TUI and verify keyboard-driven workflows.
- [x] Run focused checks and review all deliverables.

## AWS credential configuration follow-up
- [x] Locate miscellaneous OpenAI key secret reference.
- [x] Discover test and production RDS secret shapes.
- [x] Resolve OpenAI credentials from miscellaneous secret.
- [x] Add test cluster using blp-test-local.
- [x] Add production cluster using blp-prod-local.
- [x] Verify live credentials and read-only connections.
- [x] Move cluster and secret references into dotenv.
- [x] Replace obsolete TOML configuration and affected callers.
- [x] Exercise configured TUI and credential handling tests.
- [x] Review and commit the verified AWS integration.

## Configuration portability correction
- [x] Make default dotenv resolution independent of cwd.
- [x] Remove account references from committed configuration example.
- [x] Verify installed CLI configuration portability and commit.
