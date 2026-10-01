CREATE TABLE sellers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE connections (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL REFERENCES sellers(id),
    adapter_key TEXT NOT NULL,
    name TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    config_json TEXT NOT NULL CHECK(json_valid(config_json)),
    operations_json TEXT NOT NULL CHECK(json_valid(operations_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(seller_id, id),
    UNIQUE(seller_id, adapter_key, external_account_id)
);

CREATE TABLE warehouses (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    attributes_json TEXT NOT NULL CHECK(json_valid(attributes_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(seller_id, id),
    UNIQUE(seller_id, connection_id, external_id),
    FOREIGN KEY(seller_id, connection_id) REFERENCES connections(seller_id, id)
);

CREATE TABLE products (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    sku TEXT,
    category_json TEXT NOT NULL CHECK(json_valid(category_json)),
    identifiers_json TEXT NOT NULL CHECK(json_valid(identifiers_json)),
    attributes_json TEXT NOT NULL CHECK(json_valid(attributes_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(seller_id, id),
    UNIQUE(seller_id, connection_id, external_id),
    FOREIGN KEY(seller_id, connection_id) REFERENCES connections(seller_id, id)
);

CREATE TABLE supplies (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    status TEXT NOT NULL,
    warehouse_external_id TEXT,
    attributes_json TEXT NOT NULL CHECK(json_valid(attributes_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(seller_id, id),
    UNIQUE(seller_id, connection_id, external_id),
    FOREIGN KEY(seller_id, connection_id) REFERENCES connections(seller_id, id),
    FOREIGN KEY(seller_id, connection_id, warehouse_external_id)
        REFERENCES warehouses(seller_id, connection_id, external_id)
);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    status TEXT NOT NULL,
    warehouse_external_id TEXT,
    supply_external_id TEXT,
    attributes_json TEXT NOT NULL CHECK(json_valid(attributes_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(seller_id, id),
    UNIQUE(seller_id, connection_id, external_id),
    FOREIGN KEY(seller_id, connection_id) REFERENCES connections(seller_id, id),
    FOREIGN KEY(seller_id, connection_id, warehouse_external_id)
        REFERENCES warehouses(seller_id, connection_id, external_id),
    FOREIGN KEY(seller_id, connection_id, supply_external_id)
        REFERENCES supplies(seller_id, connection_id, external_id)
);

CREATE TABLE settings (
    seller_id TEXT NOT NULL REFERENCES sellers(id),
    key TEXT NOT NULL CHECK(length(trim(key)) > 0),
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    PRIMARY KEY(seller_id, key)
);

CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(seller_id, id),
    FOREIGN KEY(seller_id, connection_id) REFERENCES connections(seller_id, id)
);

CREATE INDEX operations_queue ON operations(created_at, id) WHERE status = 'queued';
CREATE INDEX operations_seller ON operations(seller_id, created_at);
CREATE UNIQUE INDEX operations_active
    ON operations(seller_id, connection_id, operation_key)
    WHERE status IN ('queued', 'running');
