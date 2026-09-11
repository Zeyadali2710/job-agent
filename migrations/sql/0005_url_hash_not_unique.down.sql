DROP INDEX app.postings_canonical_url;
-- Fails if cross-source duplicates have accumulated, which is the honest
-- outcome: the constraint cannot be restored without discarding those rows.
ALTER TABLE app.postings ADD CONSTRAINT postings_url_hash UNIQUE (url_hash);
