-- Source-card identities and operational journals remain untouched.
CREATE TABLE catalog_products (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL REFERENCES sellers(id),
 sku TEXT NOT NULL, title TEXT NOT NULL, data_json TEXT NOT NULL CHECK(json_valid(data_json)),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 UNIQUE(seller_id,id), UNIQUE(seller_id,sku)
);
CREATE INDEX catalog_products_title ON catalog_products(seller_id,title,id);
CREATE TABLE catalog_identifiers (
 seller_id TEXT NOT NULL, product_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(seller_id,product_id,kind,value),
 FOREIGN KEY(seller_id,product_id) REFERENCES catalog_products(seller_id,id)
);
CREATE INDEX catalog_identifier_lookup ON catalog_identifiers(seller_id,kind,value);
CREATE UNIQUE INDEX catalog_gtin_unique ON catalog_identifiers(seller_id,value) WHERE kind='gtin';
CREATE TABLE catalog_links (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, product_id TEXT NOT NULL,
 source_product_id TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '',
 overrides_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(overrides_json)),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 UNIQUE(seller_id,id), UNIQUE(seller_id,source_product_id,variant),
 FOREIGN KEY(seller_id,product_id) REFERENCES catalog_products(seller_id,id),
 FOREIGN KEY(seller_id,source_product_id) REFERENCES products(seller_id,id)
);
CREATE INDEX catalog_links_product ON catalog_links(seller_id,product_id);
CREATE TABLE catalog_documents (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL REFERENCES sellers(id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 UNIQUE(seller_id,id)
);
CREATE TABLE catalog_document_products (
 seller_id TEXT NOT NULL, document_id TEXT NOT NULL, product_id TEXT NOT NULL,
 PRIMARY KEY(seller_id,document_id,product_id),
 FOREIGN KEY(seller_id,document_id) REFERENCES catalog_documents(seller_id,id),
 FOREIGN KEY(seller_id,product_id) REFERENCES catalog_products(seller_id,id)
);
CREATE TABLE catalog_files (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, document_id TEXT NOT NULL,
 filename TEXT NOT NULL, mime TEXT NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 UNIQUE(seller_id,id),
 FOREIGN KEY(seller_id,document_id) REFERENCES catalog_documents(seller_id,id)
);
CREATE TABLE catalog_batches (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, product_id TEXT NOT NULL,
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 UNIQUE(seller_id,id),
 FOREIGN KEY(seller_id,product_id) REFERENCES catalog_products(seller_id,id)
);
CREATE TABLE catalog_units (
 seller_id TEXT NOT NULL, batch_id TEXT NOT NULL, code_id TEXT NOT NULL,
 PRIMARY KEY(seller_id,code_id),
 FOREIGN KEY(seller_id,batch_id) REFERENCES catalog_batches(seller_id,id),
 FOREIGN KEY(seller_id,code_id) REFERENCES marking_codes(seller_id,id)
);
CREATE TABLE catalog_events (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, product_id TEXT NOT NULL, batch_id TEXT,
 code_id TEXT, kind TEXT NOT NULL, data_json TEXT NOT NULL CHECK(json_valid(data_json)),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 FOREIGN KEY(seller_id,product_id) REFERENCES catalog_products(seller_id,id),
 FOREIGN KEY(seller_id,batch_id) REFERENCES catalog_batches(seller_id,id),
 FOREIGN KEY(seller_id,code_id) REFERENCES marking_codes(seller_id,id)
);
CREATE INDEX catalog_events_product ON catalog_events(seller_id,product_id,created_at);
CREATE TABLE catalog_rules (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL REFERENCES sellers(id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(seller_id,id)
);
CREATE TABLE catalog_schemas (
 seller_id TEXT NOT NULL, connection_id TEXT NOT NULL, category_key TEXT NOT NULL,
 value_json TEXT NOT NULL CHECK(json_valid(value_json)),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(seller_id,connection_id,category_key),
 FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE TABLE catalog_checks (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, product_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 product_revision INTEGER NOT NULL, rules_digest TEXT NOT NULL,
 value_json TEXT NOT NULL CHECK(json_valid(value_json)),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(seller_id,id),
 FOREIGN KEY(seller_id,product_id) REFERENCES catalog_products(seller_id,id),
 FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE INDEX catalog_checks_product ON catalog_checks(seller_id,product_id,created_at);
CREATE TABLE catalog_imports (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL REFERENCES sellers(id), digest TEXT NOT NULL,
 plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
 state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','applied','cancelled')),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), applied_at TEXT,
 UNIQUE(seller_id,id)
);

CREATE TABLE catalog_document_events (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, document_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), data_json TEXT NOT NULL CHECK(json_valid(data_json)),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 UNIQUE(seller_id,document_id,revision),
 FOREIGN KEY(seller_id,document_id) REFERENCES catalog_documents(seller_id,id)
);

CREATE INDEX catalog_events_batch_code
ON catalog_events(seller_id,batch_id,code_id,created_at);
