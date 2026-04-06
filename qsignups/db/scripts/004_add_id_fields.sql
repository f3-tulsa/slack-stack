-- Change to your target schema before running (e.g. qsignups_prod)
USE qsignups_test;

ALTER TABLE `qsignups_regions`
ADD COLUMN `id` INT NOT NULL AUTO_INCREMENT FIRST,
DROP PRIMARY KEY,
ADD PRIMARY KEY (`id`);

CREATE UNIQUE INDEX region_idx
ON `qsignups_regions`(`team_id`);

ALTER TABLE `qsignups_aos`
ADD COLUMN `id` INT NOT NULL AUTO_INCREMENT FIRST,
DROP PRIMARY KEY,
ADD PRIMARY KEY (`id`);

CREATE UNIQUE INDEX aos_idx
ON `qsignups_aos`(`ao_channel_id`, `team_id`);

ALTER TABLE `qsignups_weekly`
ADD COLUMN `id` INT NOT NULL AUTO_INCREMENT FIRST,
DROP PRIMARY KEY,
ADD PRIMARY KEY (`id`);

CREATE UNIQUE INDEX weekly_idx
ON `qsignups_weekly`(`ao_channel_id`, `event_day_of_week`, `event_time`, `team_id`);

ALTER TABLE `qsignups_master`
ADD COLUMN `id` INT NOT NULL AUTO_INCREMENT FIRST,
DROP PRIMARY KEY,
ADD PRIMARY KEY (`id`);

CREATE UNIQUE INDEX master_idx
ON `qsignups_master`(`ao_channel_id`, `event_date`, `event_time`, `team_id`);
