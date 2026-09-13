# Technical Design

Add an agent-operation domain beside chat and documents. A structured agent tool produces a draft/patch; a document service validates it, checks the base version, writes a revision atomically, marks the operation applied, and enqueues existing indexing. Chat history remains complete-turn-only.

Flow: create running operation -> persist draft and transition completed (or interrupted/failed) -> inspect/resume explicitly -> apply with version and idempotency checks -> enqueue indexing. Use UUIDs, SQLAlchemy repositories, Alembic, and existing AppError envelopes. Index enqueue failure leaves durable content pending for retry.
