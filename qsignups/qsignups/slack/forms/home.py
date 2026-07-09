import logging
from datetime import timedelta, date, datetime

import constants
from database import DbManager
from database.orm import AO, Region
from database.orm.views import vwMasterEvents
from permissions import PermissionLevel, resolve_user_permission, resolved_paxminer_regional_schema
from slack import actions, forms, inputs
# from google import authenticate
from field_encryption import decrypt_field, encrypt_field
from utilities import User

def refresh(client, user: User, logger, top_message, team_id, context):
    sMsg = ""
    current_week_weinke_url = None
    next_week_weinke_url = None
    ao_list = None

    upcoming_qs = []

    try:
        # list of AOs for dropdown
        ao_list = DbManager.find_records(AO, [
            AO.team_id == team_id
        ])
        ao_list.sort( key = lambda x: x.ao_display_name.replace('The ', ''))

        # Event pulls
        tz = constants.app_timezone()
        upcoming_qs = DbManager.find_records(vwMasterEvents, [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.q_pax_id == user.id,
            vwMasterEvents.event_date > datetime.now(tz=tz)
        ])
        upcoming_qs.sort(key=lambda e: (e.event_date, e.event_time or ""))
        upcoming_events = DbManager.find_records(vwMasterEvents, [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.event_date > datetime.now(tz=tz),
            vwMasterEvents.event_date <= date.today()+timedelta(days=constants.UPCOMING_DAYS),
        ])
        upcoming_events.sort(
            key=lambda e: (
                e.event_date,
                e.ao_display_name.replace("The ", "", 1) if e.ao_display_name else "",
                e.event_time or "",
            )
        )

        region_record = DbManager.get_record(Region, team_id)

        if region_record is None:
            # team_id not on region table, so we insert it
            region_record = DbManager.create_record(Region(
                team_id = team_id,
                bot_token = encrypt_field(context['bot_token'])
            ))
        else:
            current_week_weinke_url = region_record.current_week_weinke
            next_week_weinke_url = region_record.next_week_weinke

        stored = decrypt_field(region_record.bot_token) if region_record.bot_token else None
        if stored != context['bot_token']:
            DbManager.update_record(Region, team_id, {
                Region.bot_token: encrypt_field(context['bot_token'])
            })

        # Create upcoming schedule message
        sMsg = ':calendar: Upcoming Q Slots—who\'s bringing the pain?'
        iterate_date = ''
        for event in upcoming_events:
            if event.event_date != iterate_date:
                sMsg += f"\n\n:calendar: *{event.event_date.strftime('%A %m/%d/%y')}*"
                iterate_date = event.event_date

            if event.q_pax_name is None:
                q_name = '*OPEN—Who wants it?*'
            else:
                q_name = event.q_pax_name
            sMsg += f"\n{event.ao_display_name} - {event.event_type} @ {event.event_time} - {q_name}"

        logging.getLogger(__name__).debug("home upcoming schedule preview: %s", sMsg[:2000])

    except Exception as e:
        logger.error("Error pulling user db info: %s", e, exc_info=True)

    # Extend top message with upcoming qs list
    if len(upcoming_qs) > 0:
        top_message += '\n\n:fire: You\'re on the Q sheet for these upcoming beatdowns:'
        for q in upcoming_qs:
            dt_fmt = q.event_date.strftime("%a %m-%d")
            top_message += f"\n- {q.event_type} on {dt_fmt} @ {q.event_time} at {q.ao_display_name}"

    user_info = client.users_info(user=user.id)
    permission = resolve_user_permission(
        user_info, user.id, resolved_paxminer_regional_schema()
    )
    buttons = [inputs.ActionButton("Refresh", action=actions.REFRESH_ACTION)]
    if permission.level in (PermissionLevel.ADMIN, PermissionLevel.AOQ):
        buttons.append(
            inputs.ActionButton("Manage Region Calendar", action=actions.MANAGE_SCHEDULE_ACTION)
        )
    if permission.level == PermissionLevel.ADMIN:
        buttons.append(inputs.GENERAL_SETTINGS)
        buttons.append(inputs.SEND_REMINDERS_NOW)
    home_actions = forms.make_action_button_row(buttons)
    refresh_context = {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": "*Last updated: "
                + datetime.now(tz=constants.app_timezone()).strftime("%m/%d/%Y %I:%M %p")
                + "*"
            }
        ]
    }
    
    blocks = [
        forms.make_header_row(top_message),
        home_actions,
        refresh_context,
        forms.make_divider(),
    ]
    if not ao_list:
        if permission.level in (PermissionLevel.ADMIN, PermissionLevel.AOQ):
            blocks.append(
                forms.make_header_row(
                    "Time to get this region dialed in—use the Manage Region Calendar button to add AOs and Events!"
                )
            )
        else:
            blocks.append(
                forms.make_header_row(
                    "No AOs are live yet. EH your region admin to get the schedule going!"
                )
            )
    else:
        options = []
        for ao_row in ao_list:
            new_option = {
                "text": {
                    "type": "plain_text",
                    "text": ao_row.ao_display_name
                },
                "value": ao_row.ao_channel_id
            }
            options.append(new_option)

        new_block = {
            "type": "section",
            "block_id": "ao_select_block",
            "text": {
                "type": "mrkdwn",
                "text": "Select an AO to claim a Q slot and lead the beatdown:"
            },
            "accessory": {
                "action_id": "ao-select",
                "type": "static_select",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select an AO"
                },
                "options": options
            }
        }
        blocks.append(new_block)

    if (current_week_weinke_url != None) and (next_week_weinke_url != None):
        weinke_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "This week's Weinke:"
                }
            },
            {
                "type": "image",
                "image_url": current_week_weinke_url,
                "alt_text": "This week's Weinke",
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Next week's Weinke:"
                }
            },
            {
                "type": "image",
                "image_url": next_week_weinke_url,
                "alt_text": "Next week's Weinke",
            },
            {
                "type": "divider",
            }
        ]

        for block in weinke_blocks:
            blocks.append(block)

    # add upcoming schedule text block
    if sMsg:
        upcoming_schedule_block = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": sMsg[:3000]
            }
        }
        blocks.append(upcoming_schedule_block)

    # if google.is_available(team_id):
    #     if authenticate.is_connected(team_id):
    #         blocks.append(forms.make_action_button_row([inputs.GOOGLE_DISCONNECT]))
    #     else:
    #         blocks.append(forms.make_action_button_row([inputs.GOOGLE_CONNECT]))

    # Attempt to publish view
    try:
        logger.debug(blocks)
        client.views_publish(
            user_id=user.id,
            view={
                "type": "home",
                "blocks":blocks
            }
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)
