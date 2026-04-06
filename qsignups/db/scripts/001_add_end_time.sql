-- Change to your target schema before running (e.g. qsignups_prod)
USE qsignups_test;

ALTER TABLE `qsignups_master`
ADD COLUMN `event_end_time` VARCHAR(255) NULL AFTER `event_time`;
ALTER TABLE `qsignups_weekly`
ADD COLUMN `event_end_time` VARCHAR(45) NULL AFTER `event_time`;
