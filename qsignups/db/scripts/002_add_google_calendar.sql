-- Change to your target schema before running (e.g. qsignups_prod)
USE qsignups_test;

ALTER TABLE `qsignups_regions`
ADD COLUMN `google_calendar_id` VARCHAR(45) NULL AFTER `weekly_ao_reminders`;
ALTER TABLE `qsignups_master`
ADD COLUMN `google_event_id` VARCHAR(45) NULL AFTER `team_id`;
