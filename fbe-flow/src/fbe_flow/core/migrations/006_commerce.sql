-- WB, Ozon and the independent store share physical-code reservations.
CREATE TABLE code_reservations (
    code TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    code_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('wb','commerce')),
    action_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(seller_id,code_id) REFERENCES marking_codes(seller_id,id),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id),
    FOREIGN KEY(seller_id,order_id) REFERENCES orders(seller_id,id)
);
INSERT INTO code_reservations(code,seller_id,code_id,connection_id,order_id,origin,action_id)
SELECT code,seller_id,code_id,connection_id,order_id,'wb',action_id FROM wb_code_assignments;
CREATE TRIGGER wb_reserve_code AFTER INSERT ON wb_code_assignments BEGIN
    INSERT INTO code_reservations(code,seller_id,code_id,connection_id,order_id,origin,action_id)
    VALUES(NEW.code,NEW.seller_id,NEW.code_id,NEW.connection_id,NEW.order_id,'wb',NEW.action_id);
END;
CREATE TRIGGER wb_release_code AFTER DELETE ON wb_code_assignments BEGIN
    DELETE FROM code_reservations WHERE code=OLD.code AND seller_id=OLD.seller_id
        AND origin='wb' AND action_id=OLD.action_id;
END;
CREATE TRIGGER wb_assignment_immutable BEFORE UPDATE ON wb_code_assignments BEGIN
    SELECT RAISE(ABORT,'Code assignment is immutable');
END;
CREATE TABLE commerce_snapshots (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(seller_id,connection_id,key),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE TABLE commerce_objects (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(seller_id,connection_id,kind,external_id),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE TABLE commerce_links (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    chz_connection_id TEXT NOT NULL,
    chz_product_id TEXT NOT NULL,
    gtin TEXT NOT NULL,
    product_group TEXT NOT NULL,
    UNIQUE(seller_id,id),
    UNIQUE(seller_id,connection_id,variant),
    FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id),
    FOREIGN KEY(seller_id,product_id) REFERENCES products(seller_id,id),
    FOREIGN KEY(seller_id,chz_connection_id) REFERENCES connections(seller_id,id),
    FOREIGN KEY(seller_id,chz_product_id) REFERENCES products(seller_id,id)
);
CREATE TABLE commerce_actions (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN
        ('draft','queued','submitting','accepted','pending','confirmed','rejected',
         'unknown','conflict','partial','cancelled','awaiting_manual')),
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
CREATE TABLE commerce_targets (
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    target_key TEXT NOT NULL,
    action_id TEXT NOT NULL,
    PRIMARY KEY(seller_id,connection_id,target_key),
    FOREIGN KEY(seller_id,connection_id,action_id) REFERENCES commerce_actions(seller_id,connection_id,id)
);
CREATE TABLE commerce_events (
    id INTEGER PRIMARY KEY,
    seller_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL CHECK(json_valid(data_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(seller_id,action_id) REFERENCES commerce_actions(seller_id,id)
);
CREATE INDEX commerce_due ON commerce_actions(next_check_at) WHERE state IN ('accepted','pending');
