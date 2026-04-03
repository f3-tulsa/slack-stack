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

## PAXminer

- Runs in the background: mines backblasts, tracks attendance, builds monthly charts and stats.
- Data flows from Slack channels into the regional database; you usually interact via posted charts or region-specific commands (see [PAXminer README](../PAXminer/README.md)).

## Weaselbot

- **Achievements:** Badges and milestones based on attendance and activity rules.
- **Kotter reports:** Summaries for Kotter channels or admins.
- **Configuration:** Achievement channels and tiers are set per region (see [weaselbot README](../weaselbot/README.md)).

## Getting help

For deployment or database issues, region tech contacts should see **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** and **[DEPLOY.md](DEPLOY.md)**.
