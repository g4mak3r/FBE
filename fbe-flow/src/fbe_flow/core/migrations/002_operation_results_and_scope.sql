ALTER TABLE operations ADD COLUMN scope_key TEXT NOT NULL DEFAULT ''
    CHECK(scope_key = trim(scope_key));

DROP INDEX operations_active;
CREATE UNIQUE INDEX operations_active
    ON operations(seller_id, connection_id, operation_key, scope_key)
    WHERE status IN ('queued', 'running');

-- Version 1 stored just the normalized record counts in successful result_json.
-- Keep those counts while moving every persisted success to the uniform result envelope.
UPDATE operations
SET result_json = json_object('counts', json(result_json), 'data', json('{}'))
WHERE status = 'succeeded' AND result_json IS NOT NULL;
