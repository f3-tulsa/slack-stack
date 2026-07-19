# User guide

Short orientation for PAX and region admins. Exact Slack wording may vary by workspace.

## QSignups

- **Home tab:** Weekly schedule; **Refresh** updates the view.
- **Sign up:** Tap an open slot and confirm; your name appears on that date.
- **Your Q slot:** If you already have a slot, you can edit or clear it from the same flow.
- **Manage Region Calendar** (admins and **Site Q / AOQ** when configured): add or change AOs and events (AOQ only for their AO(s); admins have full control).
- **General Settings** (Slack workspace admins only): app-wide options.
- **Recurring vs single events:** Recurring patterns repeat; single events are one-off. Edits and deletes may show confirmation modals.

If your region does not set the PAXminer regional link for QSignups, only Slack workspace admins get calendar management; everyone else uses Refresh and personal signups.

## Slackblast

- **`/slackblast`**, **`/backblast`**, **`/preblast`:** Post formatted beatdown content to channels.
- **Backblast fields:** Title, date, AO, PAX list, FNGs, moleskin, etc., as configured for your region.
- **Strava:** Link activities when enabled.
- **Email / Postie:** Optional outbound email flows where configured.
- **`/config-slackblast`**, **`/config-welcome-message`:** Admin-style configuration for region and welcome content.
- **Achievements coupling:** When PAXMiner is linked, saving a backblast can trigger achievement evaluation for affected PAX. In **General Settings**, admins can enable **Also post achievement unlocks to the AO channel** (only shown when PAXMiner is linked).

## PAXMiner

PAXMiner runs in the background and in Slack admin flows:

- **Sync:** Daily user/channel sync.
- **Achievements:** Data-driven rules grant and revoke awards. Unlocks post to the configured achievement channel and DM the PAX; optional AO channel posts come from Slackblast when enabled.
- **Scheduled reports:** Charts, leaderboards, Kotter, and custom reports run on the unified schedule (default monthly). Items with no destination channel configured are skipped until an admin sets one.
- **`/config-paxminer`** (workspace admins): Timezone and daily achievement toggles/channel on Save; hub buttons for achievement rules, report definitions, Kotter thresholds, and **Schedule** (including **Run Now** for a single report item).
- Manual Kotter (and other reports): use **Schedule → select item → Run Now** (one region / one schedule item). There is no `/kotter-report` slash command. Run Now DMs you the result in the PAXMiner **Messages** tab; it does not post to `#paxminer_logs`.
- **`#paxminer_logs`:** Operational summaries for automatic runs — achievement grants/revokes, Kotter posts, and scheduled report success/skip/failure (with error detail on failure). Open that channel to audit background activity.

There is no `/tag-achievement` command; awards are computed from attendance data, not manual tags.

## Getting help

For deployment or database issues, region tech contacts should see **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** and **[DEPLOY.md](DEPLOY.md)**.
