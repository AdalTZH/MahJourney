CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id UUID PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('USER', 'ASSISTANT')),
    content TEXT NOT NULL,
    trust_label TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversation_messages_conversation_created
    ON conversation_messages(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_conversation_messages_expires
    ON conversation_messages(expires_at);
CREATE INDEX IF NOT EXISTS ix_conversation_messages_search
    ON conversation_messages USING GIN (to_tsvector('english', content));

CREATE INDEX IF NOT EXISTS ix_memory_items_embedding
    ON memory_items USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'CURATED' AND embedding IS NOT NULL;
