"""Achievement rule definitions and seed data for achievements_list."""

from __future__ import annotations

# Rule columns: metric, activity, period, threshold
# metric: posts | qs | distinct_aos | posts_at_single_ao
# activity: beatdown | qsource | any
# period: week | month | year

ACHIEVEMENT_SEEDS: list[dict] = [
    {
        "name": "The Priest",
        "description": "Post for 25 Qsource lessons",
        "verb": "posting for 25 Qsource lessons",
        "code": "the_priest",
        "metric": "posts",
        "activity": "qsource",
        "period": "year",
        "threshold": 25,
    },
    {
        "name": "The Monk",
        "description": "Post at 4 QSources in a month",
        "verb": "posting at 4 Qsources in a month",
        "code": "the_monk",
        "metric": "posts",
        "activity": "qsource",
        "period": "month",
        "threshold": 4,
    },
    {
        "name": "Leader of Men",
        "description": "Q at 4 beatdowns in a month",
        "verb": "Qing at 4 beatdowns in a month",
        "code": "leader_of_men",
        "metric": "qs",
        "activity": "beatdown",
        "period": "month",
        "threshold": 4,
    },
    {
        "name": "The Boss",
        "description": "Q at 6 beatdowns in a month",
        "verb": "Qing at 6 beatdowns in a month",
        "code": "the_boss",
        "metric": "qs",
        "activity": "beatdown",
        "period": "month",
        "threshold": 6,
    },
    {
        "name": "Be the Hammer, Not the Nail",
        "description": "Q at 6 beatdowns in a week",
        "verb": "Qing at 6 beatdowns in a week",
        "code": "be_the_hammer_not_the_nail",
        "metric": "qs",
        "activity": "beatdown",
        "period": "week",
        "threshold": 6,
    },
    {
        "name": "Cadre",
        "description": "Q at 7 different AOs in a month",
        "verb": "Qing at 7 different AOs in a month",
        "code": "cadre",
        "metric": "distinct_aos",
        "activity": "beatdown",
        "period": "month",
        "threshold": 7,
    },
    {
        "name": "El Presidente",
        "description": "Q at 20 beatdowns in a year",
        "verb": "Qing at 20 beatdowns in a year",
        "code": "el_presidente",
        "metric": "qs",
        "activity": "beatdown",
        "period": "year",
        "threshold": 20,
    },
    {
        "name": "El Quatro",
        "description": "Post at 25 beatdowns in a year",
        "verb": "posting at 25 beatdowns in a year",
        "code": "el_quatro",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 25,
    },
    {
        "name": "Golden Boy",
        "description": "Post at 50 beatdowns in a year",
        "verb": "posting at 50 beatdowns in a year",
        "code": "golden_boy",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 50,
    },
    {
        "name": "Centurion",
        "description": "Post at 100 beatdowns in a year",
        "verb": "posting at 100 beatdowns in a year",
        "code": "centurion",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 100,
    },
    {
        "name": "Karate Kid",
        "description": "Post at 150 beatdowns in a year",
        "verb": "posting at 150 beatdowns in a year",
        "code": "karate_kid",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 150,
    },
    {
        "name": "Crazy Person",
        "description": "Post at 200 beatdowns in a year",
        "verb": "posting at 200 beatdowns in a year",
        "code": "crazy_person",
        "metric": "posts",
        "activity": "beatdown",
        "period": "year",
        "threshold": 200,
    },
    {
        "name": "6 pack",
        "description": "Post at 6 beatdowns in a week",
        "verb": "posting at 6 beatdowns in a week",
        "code": "6_pack",
        "metric": "posts",
        "activity": "beatdown",
        "period": "week",
        "threshold": 6,
    },
    {
        "name": "Holding Down the Fort",
        "description": "Post 50 times at an AO",
        "verb": "posting 50 times at an AO",
        "code": "holding_down_the_fort",
        "metric": "posts_at_single_ao",
        "activity": "beatdown",
        "period": "year",
        "threshold": 50,
    },
]

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
  KEY `awarded_period_lookup` (`achievement_id`, `pax_id`, `period_key`),
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
