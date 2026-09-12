-- Approving an application from the dashboard now enqueues a `submit` item,
-- so ja_api needs to be able to enqueue. INSERT only: the API must never
-- claim work, which needs UPDATE, and it has no handler to run one with.
--
-- Found by approving an application and getting a 500: "permission denied for
-- table work_items". The grant model is deliberately an explicit allowlist,
-- so a new capability needs a new grant rather than inheriting one.
GRANT INSERT ON app.work_items TO ja_api;
