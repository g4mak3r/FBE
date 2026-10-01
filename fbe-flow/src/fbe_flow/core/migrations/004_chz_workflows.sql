-- Outbound business request and worker job have independent lifecycles.
CREATE TABLE marking_documents (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body_json TEXT NOT NULL CHECK(json_valid(body_json)),
    digest TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'prepared'
        CHECK(state IN ('prepared','submitting','accepted','processing','succeeded',
                        'partial','rejected','unknown','cancelled')),
    external_id TEXT,
    external_status TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    wire_json TEXT CHECK(wire_json IS NULL OR json_valid(wire_json)),
    next_check_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(seller_id,id),
    UNIQUE(seller_id,connection_id,id),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE UNIQUE INDEX marking_document_duplicate
    ON marking_documents(seller_id,connection_id,kind,digest) WHERE state<>'cancelled';
CREATE UNIQUE INDEX marking_document_external
    ON marking_documents(seller_id,connection_id,kind,external_id) WHERE external_id IS NOT NULL;
CREATE INDEX marking_documents_due ON marking_documents(next_check_at)
    WHERE state IN ('accepted','processing');
CREATE TABLE marking_targets (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    target_key TEXT NOT NULL,
    document_id TEXT NOT NULL,
    PRIMARY KEY(seller_id,connection_id,target_key),
    FOREIGN KEY(seller_id,connection_id,document_id)
        REFERENCES marking_documents(seller_id,connection_id,id)
);
CREATE TABLE marking_events (
    id INTEGER PRIMARY KEY,
    seller_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL CHECK(json_valid(data_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(seller_id,document_id) REFERENCES marking_documents(seller_id,id)
);
CREATE TABLE marking_code_blocks (
    id TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    gtin TEXT NOT NULL,
    document_id TEXT NOT NULL,
    codes_json TEXT NOT NULL CHECK(json_valid(codes_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(seller_id,connection_id,id),
    FOREIGN KEY(seller_id,connection_id,document_id)
        REFERENCES marking_documents(seller_id,connection_id,id)
);
CREATE TABLE marking_codes (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    code TEXT NOT NULL,
    full_code TEXT,
    block_id TEXT,
    order_id TEXT,
    product_group TEXT,
    gtin TEXT,
    external_status TEXT,
    attributes_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(attributes_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(seller_id,id),
    UNIQUE(seller_id,connection_id,code),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id),
    FOREIGN KEY(seller_id,connection_id,block_id)
        REFERENCES marking_code_blocks(seller_id,connection_id,id)
);
