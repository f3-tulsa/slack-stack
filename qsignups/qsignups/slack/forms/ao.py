import json
from typing import List, Optional

from database import DbManager
from database.orm import AO
from database.orm.views import vwAOsSort

from slack import actions, forms, inputs
from slack.handlers import ao as ao_handler


def publish_edit_ao_home(client, user_id, team_id, logger, selected_channel: str) -> None:
    """Publish home tab with edit-AO fields for one channel (shared by picker flow and single-AOQ shortcut)."""
    aos: list[vwAOsSort] = DbManager.find_records(vwAOsSort, [vwAOsSort.team_id == team_id])
    if selected_channel not in [a.ao_channel_id for a in aos]:
        logger.warning("publish_edit_ao_home: channel %s not in team AOs", selected_channel)
        return

    ao_index = [a.ao_channel_id for a in aos].index(selected_channel)
    ao_display_name = aos[ao_index].ao_display_name or ""
    ao_location_subtitle = aos[ao_index].ao_location_subtitle or ""
    site_q_user_id = ao_handler.get_site_q(selected_channel)
    selected_channel_name = ao_display_name

    blocks = [
        {
            "type": "section",
            "block_id": "page_label",
            "text": {"type": "mrkdwn", "text": f"*Edit AO:*\n*{selected_channel_name}*"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Channel: <#{selected_channel}>"}],
        },
        {
            "type": "input",
            "block_id": "ao_display_name",
            "element": {
                "type": "plain_text_input",
                "action_id": "ao_display_name",
                "placeholder": {"type": "plain_text", "text": "Weasel's Ridge"},
                "initial_value": ao_display_name,
            },
            "label": {"type": "plain_text", "text": "AO Title"},
        },
        {
            "type": "input",
            "block_id": "ao_location_subtitle",
            "element": {
                "type": "plain_text_input",
                "multiline": True,
                "action_id": "ao_location_subtitle",
                "placeholder": {"type": "plain_text", "text": "Oompa Loompa Kingdom"},
                "initial_value": ao_location_subtitle,
            },
            "label": {"type": "plain_text", "text": "Location (township, park, etc.)"},
        },
        {
            "type": "input",
            "block_id": "site_q_user_id",
            "optional": True,
            "element": {
                "type": "users_select",
                "action_id": "site_q_user_id",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select AOQ",
                    "emoji": True,
                },
                **({"initial_user": site_q_user_id} if site_q_user_id else {}),
            },
            "label": {"type": "plain_text", "text": "AOQ", "emoji": True},
        },
    ]
    blocks.append(
        forms.make_action_button_row(
            [inputs.make_submit_button(actions.EDIT_AO_ACTION), inputs.BACK_TO_MANAGE_BUTTON]
        )
    )

    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks,
                "private_metadata": json.dumps({"ao_channel_id": selected_channel}),
            },
        )
    except Exception as e:
        logger.error("Error publishing edit AO home tab: %s", e, exc_info=True)


def add_form(team_id, user_id, client, logger):
    logger.info("gather input data")
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Select an AO channel:*",
            },
        },
        {
            "type": "input",
            "block_id": "add_ao_channel_select",
            "element": {
                "type": "channels_select",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select a channel",
                    "emoji": True,
                },
                "action_id": "add_ao_channel_select",
            },
            "label": {
                "type": "plain_text",
                "text": "Channel associated with AO",
                "emoji": True,
            },
        },
        {
            "type": "input",
            "block_id": "ao_display_name",
            "element": {
                "type": "plain_text_input",
                "action_id": "ao_display_name",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Weasel's Ridge",
                },
            },
            "label": {
                "type": "plain_text",
                "text": "AO Title",
            },
        },
        {
            "type": "input",
            "block_id": "ao_location_subtitle",
            "element": {
                "type": "plain_text_input",
                "multiline": True,
                "action_id": "ao_location_subtitle",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Oompa Loompa Kingdom",
                },
            },
            "label": {
                "type": "plain_text",
                "text": "Location (township, park, etc.)",
            },
        },
        {
            "type": "input",
            "block_id": "site_q_user_id",
            "optional": True,
            "element": {
                "type": "users_select",
                "action_id": "site_q_user_id",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select AOQ",
                    "emoji": True,
                },
            },
            "label": {
                "type": "plain_text",
                "text": "AOQ",
                "emoji": True,
            },
        },
    ]

    blocks.append(
        forms.make_action_button_row(
            [
                inputs.make_submit_button(actions.ADD_AO_ACTION),
                inputs.BACK_TO_MANAGE_BUTTON,
            ]
        )
    )

    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks,
            },
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)


def edit_form(
    team_id,
    user_id,
    client,
    logger,
    allowed_ao_channel_ids: Optional[List[str]] = None,
):
    aos: list[vwAOsSort] = DbManager.find_records(vwAOsSort, [vwAOsSort.team_id == team_id])
    aos.sort(key=lambda x: x.ao_display_name.replace("The ", ""))
    if allowed_ao_channel_ids is not None:
        allow = set(allowed_ao_channel_ids)
        aos = [a for a in aos if a.ao_channel_id in allow]
    if not aos:
        logger.warning("edit_form: no AOs available for this user")
        return
    if len(aos) == 1:
        publish_edit_ao_home(client, user_id, team_id, logger, aos[0].ao_channel_id)
        return

    ao_options = []
    for ao in aos:
        new_option = {
            "text": {
                "type": "plain_text",
                "text": ao.ao_display_name,
                "emoji": True,
            },
            "value": ao.ao_channel_id,
        }
        ao_options.append(new_option)

    blocks = [
        {
            "type": "section",
            "block_id": "edit_ao_select",
            "text": {
                "type": "mrkdwn",
                "text": "Please select an AO to edit:",
            },
            "accessory": {
                "action_id": "edit_ao_select",
                "type": "static_select",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select an AO",
                },
                "options": ao_options,
            },
        }
    ]

    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks,
            },
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)


def delete_form(
    team_id,
    user_id,
    client,
    logger,
    allowed_ao_channel_ids: Optional[List[str]] = None,
):
    aos: list[vwAOsSort] = DbManager.find_records(vwAOsSort, [vwAOsSort.team_id == team_id])
    aos.sort(key=lambda x: x.ao_display_name.replace("The ", ""))
    if allowed_ao_channel_ids is not None:
        allow = set(allowed_ao_channel_ids)
        aos = [a for a in aos if a.ao_channel_id in allow]

    blocks = [forms.make_section_header_row("Delete an AO")]

    for ao in aos:
        button = inputs.ActionButton(
            label="Delete AO",
            action=actions.DELETE_AO_ACTION,
            value=ao.ao_channel_id,
            style="danger",
        )
        blocks.append(forms.make_header_row(ao.ao_display_name, accessory=button))
        blocks.append(forms.make_divider())

    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks,
            },
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)


def pull_aos(team_id):
    aos: list[AO] = DbManager.find_records(AO, [AO.team_id == team_id])
    aos_list, aos_sort = {}, {}
    for index, ao in enumerate(aos):
        aos_list[index] = ao.ao_display_name
        aos_sort[index] = ao.ao_display_name.replace("The ", "")

    aos_sort = dict(sorted(aos_sort.items(), key=lambda x: x[1]))
    return [aos_list[i] for i in aos_sort.keys()]
