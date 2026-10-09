-- Preserve command journals and reservations while extending command types.
CREATE TEMP TABLE saved_wb_actions AS SELECT * FROM wb_actions;
CREATE TEMP TABLE saved_wb_action_targets AS SELECT * FROM wb_action_targets;
CREATE TEMP TABLE saved_wb_code_assignments AS SELECT * FROM wb_code_assignments;
CREATE TEMP TABLE saved_wb_events AS SELECT * FROM wb_events;
DROP TABLE wb_events;
DROP TABLE wb_code_assignments;
DROP TABLE wb_action_targets;
DROP TABLE wb_actions;
CREATE TABLE wb_actions (
    id TEXT PRIMARY KEY,
    seller_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('sgtin','supply_create','supply_add','supply_deliver','supply_delete','expiration')),
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
INSERT INTO wb_actions SELECT * FROM saved_wb_actions;
DROP TABLE saved_wb_actions;
INSERT INTO wb_action_targets SELECT * FROM saved_wb_action_targets;
DROP TABLE saved_wb_action_targets;
INSERT INTO wb_code_assignments SELECT * FROM saved_wb_code_assignments;
DROP TABLE saved_wb_code_assignments;
INSERT INTO wb_events SELECT * FROM saved_wb_events;
DROP TABLE saved_wb_events;
CREATE TABLE wb_print_jobs (
 id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 kind TEXT NOT NULL, labels_json TEXT NOT NULL CHECK(json_valid(labels_json)),
 confirmed_at TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 FOREIGN KEY(seller_id,connection_id) REFERENCES connections(seller_id,id)
);
CREATE INDEX wb_print_jobs_scope ON wb_print_jobs(seller_id,connection_id,created_at);

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
