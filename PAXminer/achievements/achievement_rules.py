"""Achievement rule definitions and seed data for achievements_list."""

from __future__ import annotations

import json
from pathlib import Path

# Rule columns: metric, activity, period, threshold
# metric: posts | qs | distinct_aos | posts_at_single_ao
# activity: beatdown | qsource | any
# period: week | month | year

_DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "achievement_defaults.json"


def _load_achievement_seeds() -> list[dict]:
    with _DEFAULTS_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("achievement_defaults.json must be a list of seed objects")
    return data


ACHIEVEMENT_SEEDS: list[dict] = _load_achievement_seeds()


ACHIEVEMENTS_LIST_DDL = """
CREATE TABLE IF NOT EXISTS `{schema}`.`achievements_list` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `description` varchar(255) NOT NULL,
  `verb` varchar(255) NOT NULL,
  `code` varchar(255) NOT NULL,
  `metric` varchar(32) NOT NULL DEFAULT 'posts',
  `activity` varchar(32) NOT NULL DEFAULT 'beatdown',
  `period` varchar(16) NOT NULL DEFAULT 'year',
  `threshold` int NOT NULL DEFAULT 1,
  `enabled` tinyint NOT NULL DEFAULT 1,
  `reeval_queued_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`),
  KEY `enabled` (`enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

ACHIEVEMENTS_AWARDED_DDL = """
CREATE TABLE IF NOT EXISTS `{schema}`.`achievements_awarded` (
  `id` int NOT NULL AUTO_INCREMENT,
  `achievement_id` int NOT NULL,
  `pax_id` varchar(255) NOT NULL,
  `date_awarded` date NOT NULL,
  `achievement_version_id` int DEFAULT NULL,
  `period` varchar(16) DEFAULT NULL,
  `period_key` varchar(16) DEFAULT NULL,
  `period_start` date DEFAULT NULL,
  `period_end` date DEFAULT NULL,
  `qualifying_count` int DEFAULT NULL,
  `created` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `achievement_id` (`achievement_id`),
  KEY `pax_id` (`pax_id`),
  UNIQUE KEY `uniq_award_period` (`achievement_id`, `pax_id`, `period_key`),
  CONSTRAINT `achievements_awarded_ibfk_1` FOREIGN KEY (`achievement_id`) REFERENCES `{schema}`.`achievements_list` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

ACHIEVEMENT_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS `{schema}`.`achievement_versions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `achievement_id` int NOT NULL,
  `version` int NOT NULL,
  `version_key` varchar(300) NOT NULL,
  `metric` varchar(32) NOT NULL DEFAULT 'posts',
  `activity` json DEFAULT NULL,
  `period` varchar(16) NOT NULL DEFAULT 'year',
  `threshold` int NOT NULL DEFAULT 1,
  `effective_from` date DEFAULT NULL,
  `effective_to` date DEFAULT NULL,
  `range_mode` varchar(24) DEFAULT NULL,
  `superseded_at` datetime DEFAULT NULL,
  `created_by` varchar(255) DEFAULT NULL,
  `created` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `version_key` (`version_key`),
  UNIQUE KEY `achievement_version` (`achievement_id`, `version`),
  KEY `achievement_current` (`achievement_id`, `superseded_at`),
  CONSTRAINT `achievement_versions_ibfk_1` FOREIGN KEY (`achievement_id`)
    REFERENCES `{schema}`.`achievements_list` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

ACHIEVEMENTS_VIEW_DDL = """
CREATE OR REPLACE VIEW `{schema}`.`achievements_view` AS
SELECT u.user_name AS pax, u.user_id AS pax_id, al.name, al.description, aa.date_awarded,
       av.version_key
FROM `{schema}`.`users` u
JOIN `{schema}`.`achievements_awarded` aa ON u.user_id = aa.pax_id
JOIN `{schema}`.`achievements_list` al ON aa.achievement_id = al.id
LEFT JOIN `{schema}`.`achievement_versions` av ON aa.achievement_version_id = av.id
"""

RULE_COLUMNS = ("metric", "activity", "period", "threshold")

AWARDED_PERIOD_COLUMNS = (
    "achievement_version_id",
    "period",
    "period_key",
    "period_start",
    "period_end",
    "qualifying_count",
)
