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

- **Sync & charts:** Daily user/channel sync; monthly PAX/Q charts and leaderboards (when enabled).
- **Achievements:** Data-driven rules grant and revoke awards. Unlocks post to the configured achievement channel and DM the PAX; optional AO channel posts come from Slackblast when enabled.
- **Leaderboard / almost-there:** Monthly YTD top-10 and “almost there” nudges in the achievement channel (when enabled).
- **Kotter reports:** Monthly posting/Q reminders to the Kotter channel.
- **`/config-paxminer`** (workspace admins): Channels, feature toggles, Kotter thresholds, monthly chart options, and **achievement catalog CRUD** (add / edit / delete rules).
- **`/kotter-report`:** Admins can queue a manual Kotter send.

There is no `/tag-achievement` command; awards are computed from attendance data, not manual tags.

## Getting help

For deployment or database issues, region tech contacts should see **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** and **[DEPLOY.md](DEPLOY.md)**.
