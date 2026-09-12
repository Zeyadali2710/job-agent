REVOKE DELETE ON app.score_features FROM ja_worker;
DELETE FROM app.settings WHERE key = 'notify.high_match_min_score';
