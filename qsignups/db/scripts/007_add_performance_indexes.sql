-- Change to your target schema before running (e.g. qsignups_prod)
USE qsignups_test;

CREATE INDEX idx_master_team_qpax_date
ON qsignups_master(team_id, q_pax_id, event_date);

CREATE INDEX idx_master_team_date
ON qsignups_master(team_id, event_date);

CREATE INDEX idx_master_team_ao_date
ON qsignups_master(team_id, ao_channel_id, event_date);

CREATE INDEX idx_aos_team
ON qsignups_aos(team_id);
