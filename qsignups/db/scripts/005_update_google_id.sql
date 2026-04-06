-- Change to your target schema before running (e.g. qsignups_prod)
USE qsignups_test;

ALTER TABLE `qsignups_regions`
CHANGE COLUMN `google_calendar_id` `google_calendar_id` VARCHAR(100) NULL DEFAULT NULL;

ALTER TABLE `qsignups_regions`
ADD COLUMN `google_auth_data` JSON NULL AFTER `google_calendar_id`;

ALTER TABLE `qsignups_regions`
ADD COLUMN `timezone` VARCHAR(45) NULL DEFAULT 'America/New_York' AFTER `google_auth_data`;
