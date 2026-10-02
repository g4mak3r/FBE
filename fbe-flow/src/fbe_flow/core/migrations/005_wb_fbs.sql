CREATE TABLE wb_snapshots (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(seller_id,connection_id,key),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE TABLE wb_links (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    chrt_id TEXT NOT NULL,
    chz_connection_id TEXT NOT NULL,
    chz_product_id TEXT NOT NULL,
    gtin TEXT NOT NULL,
    product_group TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(seller_id,id),
    UNIQUE(seller_id,connection_id,product_id,chrt_id),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id),
    FOREIGN KEY(seller_id,product_id) REFERENCES products(seller_id,id),
    FOREIGN KEY(seller_id,chz_connection_id) REFERENCES connections(seller_id,id),
    FOREIGN KEY(seller_id,chz_product_id) REFERENCES products(seller_id,id)
);
CREATE TABLE wb_actions (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('sgtin','supply_create','supply_add','supply_deliver','supply_delete')),
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','queued','submitting','accepted','pending','confirmed','rejected','unknown','conflict','partial','cancelled')),
    body_json TEXT NOT NULL CHECK(json_valid(body_json)),
    body_digest TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(result_json)),
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
    error_code TEXT,
    next_check_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(seller_id,id),
    UNIQUE(seller_id,connection_id,id),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE INDEX wb_actions_due ON wb_actions(next_check_at) WHERE state IN ('accepted','pending');
CREATE TABLE wb_action_targets (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    target_key TEXT NOT NULL,
    action_id TEXT NOT NULL,
    PRIMARY KEY(seller_id,connection_id,target_key),
    FOREIGN KEY(seller_id,connection_id,action_id) REFERENCES wb_actions(seller_id,connection_id,id)
);
CREATE TABLE wb_code_assignments (
    seller_id TEXT NOT NULL,
    code TEXT NOT NULL,
    code_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    PRIMARY KEY(seller_id,code),
    UNIQUE(code),
    FOREIGN KEY(seller_id,code_id) REFERENCES marking_codes(seller_id,id),
    FOREIGN KEY(seller_id,order_id) REFERENCES orders(seller_id,id),
    FOREIGN KEY(seller_id,connection_id,action_id) REFERENCES wb_actions(seller_id,connection_id,id)
);
CREATE TABLE wb_events (
    id INTEGER PRIMARY KEY,
    seller_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL CHECK(json_valid(data_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(seller_id,action_id) REFERENCES wb_actions(seller_id,id)
);
