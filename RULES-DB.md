# Storage contracts

Runtime configuration and SQLite/JSONL state stay in a private data directory.
Directories 700, files 600. Bind the owner explicitly, persist deduplication and send reservations.
Ack specific message IDs only after send confirmation. Ack does not imply history deletion.
No automatic destructive migration, pruning or concurrent consumers.
