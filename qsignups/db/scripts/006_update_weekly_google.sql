-- Change to your target schema before running (e.g. qsignups_prod)
USE qsignups_test;

ALTER TABLE `qsignups_weekly`
ADD COLUMN `google_calendar_id` VARCHAR(100) NULL AFTER `team_id`;

CREATE TABLE `qsignups_features` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `region_id` INT NOT NULL,
  `feature` VARCHAR(45) NOT NULL,
  `enabled` TINYINT NOT NULL,
  `created` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  CONSTRAINT `region_id_fk`
    FOREIGN KEY (`region_id`)
    REFERENCES `qsignups_regions` (`id`)
    ON DELETE NO ACTION
    ON UPDATE NO ACTION);
