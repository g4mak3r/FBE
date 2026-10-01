CREATE TABLE marking_snapshots (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY(seller_id, connection_id, key),
    FOREIGN KEY(seller_id, connection_id) REFERENCES connections(seller_id, id)
);

-- Keep source presence separate from the last readable card. Missing cards are not deleted.
CREATE TABLE nk_cards (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    etag TEXT,
    last_seen_run TEXT NOT NULL,
    present INTEGER NOT NULL CHECK(present IN (0,1)),
    detail_available INTEGER NOT NULL CHECK(detail_available IN (0,1)),
    PRIMARY KEY(seller_id, connection_id, external_id),
    FOREIGN KEY(seller_id, connection_id) REFERENCES connections(seller_id, id)
);
