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

## Recent-record browsing and direct SQL prompting
- [x] Define safe recent-record browsing and interaction contract.
- [x] Add light last hundred records hotkey.
- [x] Add heavy last hundred records hotkey.
- [x] Infer light and heavy table queries on first view.
- [x] Automatically refresh light last five records on selection.
- [x] Add independent duplicate workspace tab hotkey.
- [x] Replace AI output pane with smaller prompt input.
- [x] Insert SQL-only AI responses directly into query editor.
- [x] Exercise table browsing and duplicate tab workflows.
- [x] Update affected help and commit verified features.
- [x] Add settings theme selector with every Oracle TUI theme.
- [x] Recommend detailed inspection for large structured values.
