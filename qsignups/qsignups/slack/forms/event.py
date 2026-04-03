import json
from datetime import date, datetime, timedelta
from typing import List, Optional

import constants
from database import DbManager
from database.orm.views import vwWeeklyEvents, vwAOsSort, vwMasterEvents
from slack import actions, forms, inputs

from utilities import list_to_dict


def _aos_for_team(
    team_id: str, allowed_ao_channel_ids: Optional[List[str]] = None
) -> list[vwAOsSort]:
    aos: list[vwAOsSort] = DbManager.find_records(vwAOsSort, [vwAOsSort.team_id == team_id])
    aos.sort(key=lambda x: x.ao_display_name.replace("The ", ""))
    if allowed_ao_channel_ids is not None:
        allow = set(allowed_ao_channel_ids)
        aos = [a for a in aos if a.ao_channel_id in allow]
    return aos


def add_single_form(
    team_id,
    user_id,
    client,
    logger,
    allowed_ao_channel_ids: Optional[List[str]] = None,
):
    aos = _aos_for_team(team_id, allowed_ao_channel_ids)
    ao_list = [ao.ao_display_name for ao in aos]

    ao_selector = inputs.ActionSelector(
        label = "Select an AO",
        action = "ao_display_name_select_action",
        options = inputs.as_selector_options(ao_list))

    blocks = [
        inputs.EVENT_TYPE_SELECTOR.as_form_field(),
        inputs.ActionInput(
            label = "Custom Event Name",
            action = "event_type_custom",
            placeholder = "If custom is selected, specify a name",
            optional = True).as_form_field(),
        ao_selector.as_form_field(),
    ]

    # TODO: have "other" / freeform option
    # TODO: add this to form
    special_list = [
        'None',
        'The Forge',
        'VQ',
        'F3versary',
        'Birthday Q',
        'AO Launch',
        'IronPAX',
        'Convergence',
        "Flag Handoff",
        "Ghost Q",
        "Roulette Q",
        "Q School",
    ]
    special_selector = inputs.ActionSelector(
        label = "Special Event Tag",
        action = "event_special_type_selector",
        options = inputs.as_selector_options(special_list))
    blocks.append(special_selector.as_form_field())
    blocks.append(inputs.EVENT_DATE_SELECTOR.as_form_field(initial_value = date.today().strftime('%Y-%m-%d')))

    blocks += [
        inputs.START_TIME_SELECTOR.as_form_field(initial_value = constants.DEFAULT_EVENT_START),
        inputs.END_TIME_SELECTOR.as_form_field(initial_value = constants.DEFAULT_EVENT_END),

        forms.make_action_button_row([
            inputs.make_submit_button(actions.ADD_SINGLE_EVENT_ACTION),
            inputs.BACK_TO_MANAGE_BUTTON
        ]),
        forms.make_header_row("Please wait after hitting Submit, and do not hit it more than once")
    ]

    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks
            }
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)

def add_recurring_form(
    team_id,
    user_id,
    client,
    logger,
    allowed_ao_channel_ids: Optional[List[str]] = None,
):
    aos = _aos_for_team(team_id, allowed_ao_channel_ids)
    ao_list = [ao.ao_display_name for ao in aos]

    ao_selector = inputs.ActionSelector(
        label = "Select an AO",
        action = "ao_display_name_select_action",
        options = inputs.as_selector_options(ao_list))

    blocks = [
        inputs.EVENT_TYPE_SELECTOR.as_form_field(),
        inputs.ActionInput(
            label = "Custom Event Name",
            action = "event_type_custom",
            placeholder = "If custom is selected, specify a name",
            optional = True).as_form_field(),
        ao_selector.as_form_field(),
    ]

    blocks.append(inputs.WEEKDAY_SELECTOR.as_form_field())
    blocks.append(inputs.START_DATE_SELECTOR.as_form_field(initial_value = date.today().strftime('%Y-%m-%d')))

    blocks += [
        inputs.START_TIME_SELECTOR.as_form_field(initial_value = constants.DEFAULT_EVENT_START),
        inputs.END_TIME_SELECTOR.as_form_field(initial_value = constants.DEFAULT_EVENT_END),

        forms.make_action_button_row([
            inputs.make_submit_button(actions.ADD_RECURRING_EVENT_ACTION),
            inputs.BACK_TO_MANAGE_BUTTON
        ]),
        forms.make_header_row("Please wait after hitting Submit, and do not hit it more than once")
    ]

    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks
            }
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)

def publish_single_event_edit_slots(
    team_id, user_id, client, logger, ao_channel_id: str, ao_display_name: str
):
    """Q slot picker for editing a single (non-recurring) master event."""
    events = DbManager.find_records(
        vwMasterEvents,
        [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.ao_channel_id == ao_channel_id,
            vwMasterEvents.event_date > datetime.now(tz=constants.app_timezone()),
            vwMasterEvents.event_date
            <= date.today() + timedelta(weeks=constants.EVENT_PICKER_WEEKS),
        ],
    )
    events.sort(key=lambda e: (e.event_date, e.event_time or ""))

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Please select a Q slot to edit for:"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{ao_display_name}*"}},
        {"type": "divider"},
    ]

    for event in events[:90]:
        event_date_time = datetime.strptime(
            event.event_date.strftime("%Y-%m-%d") + " " + event.event_time, "%Y-%m-%d %H%M"
        )
        date_fmt = event_date_time.strftime("%a, %m-%d @ %H%M")
        date_fmt_value = event_date_time.strftime("%Y-%m-%d %H:%M:%S")

        if event.q_pax_id is None:
            date_status = "OPEN!"
        else:
            date_status = event.q_pax_name

        action_id = "edit_single_event_button"
        value = date_fmt_value + "|" + event.ao_display_name

        new_button = {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"{event.event_type} {date_fmt}: {date_status}",
                        "emoji": True,
                    },
                    "action_id": action_id,
                    "value": value,
                }
            ],
        }
        blocks.append(new_button)

    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    try:
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)


def edit_single_form(
    team_id,
    user_id,
    client,
    logger,
    allowed_ao_channel_ids: Optional[List[str]] = None,
):
    aos = _aos_for_team(team_id, allowed_ao_channel_ids)
    if not aos:
        logger.warning("edit_single_form: no AOs")
        return
    if len(aos) == 1:
        publish_single_event_edit_slots(
            team_id, user_id, client, logger, aos[0].ao_channel_id, aos[0].ao_display_name
        )
        return

    ao_list = [ao.ao_display_name for ao in aos]
    ao_id_list = [ao.ao_channel_id for ao in aos]

    blocks = [
        inputs.SectionBlock(
            label="Please select an AO to edit:",
            action=actions.EDIT_SINGLE_EVENT_AO_SELECT,
            element=inputs.SelectorElement(
                placeholder="Select an AO",
                options=inputs.as_selector_options(ao_list, ao_id_list),
            ),
        ).as_form_field()
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

def publish_single_event_delete_slots(
    team_id, user_id, client, logger, ao_channel_id: str, ao_display_name: str
):
    events = DbManager.find_records(
        vwMasterEvents,
        [
            vwMasterEvents.team_id == team_id,
            vwMasterEvents.ao_channel_id == ao_channel_id,
            vwMasterEvents.event_date > datetime.now(tz=constants.app_timezone()) - timedelta(weeks=1),
            vwMasterEvents.event_date
            <= date.today() + timedelta(weeks=constants.EVENT_PICKER_WEEKS),
        ],
    )
    events.sort(key=lambda e: (e.event_date, e.event_time or ""))

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Please select a Q slot to delete for:"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{ao_display_name}*"}},
        {"type": "divider"},
    ]

    for event in events[:90]:
        event_date_time = datetime.strptime(
            event.event_date.strftime("%Y-%m-%d") + " " + event.event_time, "%Y-%m-%d %H%M"
        )
        date_fmt = event_date_time.strftime("%a, %m-%d @ %H%M")
        date_fmt_value = event_date_time.strftime("%Y-%m-%d %H:%M:%S")

        if event.q_pax_id is None:
            date_status = "OPEN!"
        else:
            date_status = event.q_pax_name

        action_id = "delete_single_event_button"
        value = date_fmt_value + "|" + event.ao_channel_id
        new_button = inputs.ActionButton(
            label=f"{date_fmt}: {date_status}", value=value, action=action_id
        )
        blocks.append(forms.make_action_button_row([new_button]))

    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    try:
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)


def delete_single_form(
    team_id,
    user_id,
    client,
    logger,
    allowed_ao_channel_ids: Optional[List[str]] = None,
):
    aos = _aos_for_team(team_id, allowed_ao_channel_ids)
    if not aos:
        logger.warning("delete_single_form: no AOs")
        return
    if len(aos) == 1:
        publish_single_event_delete_slots(
            team_id, user_id, client, logger, aos[0].ao_channel_id, aos[0].ao_display_name
        )
        return

    ao_options = []
    for ao in aos:
        new_option = {
            "text": {
                "type": "plain_text",
                "text": ao.ao_display_name,
                "emoji": True
            },
            "value": ao.ao_channel_id
        }
        ao_options.append(new_option)

    # Build blocks
    blocks = [
        {
            "type": "section",
            "block_id": "delete_single_event_ao_select",
            "text": {
                "type": "mrkdwn",
                "text": "Please select an AO to delete an event from:"
            },
            "accessory": {
                "action_id": "delete_single_event_ao_select",
                "type": "static_select",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select an AO"
            },
            "options": ao_options
            }
        }
    ]

    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    # Publish view
    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks
            }
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)

def select_recurring_form_for_edit(
    team_id,
    user_id,
    client,
    logger,
    input_data=None,
    ao_channel_id: Optional[str] = None,
):
    if ao_channel_id is None:
        ao_channel_id = inputs.SECTION_SELECTOR.get_selected_value(input_data)

    weekly_events: list[vwWeeklyEvents] = DbManager.find_records(vwWeeklyEvents, [
        vwWeeklyEvents.team_id == team_id, 
        vwWeeklyEvents.ao_channel_id == ao_channel_id
    ])

    # Construct view
    # Top of view
    blocks = [
        forms.make_header_row("Please select a recurring event to edit:"),
        forms.make_divider(),
    ]

    current_ao = ''
    for event in weekly_events:

        if event.ao_display_name != current_ao:
            if current_ao != '':
                blocks.append(forms.make_divider())

            blocks.append(forms.make_section_header_row(event.ao_display_name))
            current_ao = event.ao_display_name

        button = inputs.ActionButton(
            "Edit Event",
            actions.SELECT_SLOT_EDIT_RECURRING_EVENT_FORM,
            value = str(event.id))
        blocks.append(
          forms.make_header_row(
            f"{event.event_type} {event.event_day_of_week}s @ {event.event_time}",
            accessory = button)
        )


    # Cancel block
    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    # Publish view
    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks
            }
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)

def select_recurring_form_for_delete(
    team_id,
    user_id,
    client,
    logger,
    input_data=None,
    ao_channel_id: Optional[str] = None,
):
    if ao_channel_id is None:
        ao_channel_id = inputs.SECTION_SELECTOR.get_selected_value(input_data)
    events = DbManager.find_records(vwWeeklyEvents, [ vwWeeklyEvents.team_id == team_id, vwWeeklyEvents.ao_channel_id == ao_channel_id])

    # Sort results_df
    day_of_week_map = {'Sunday':0, 'Monday':1, 'Tuesday':2, 'Wednesday':3, 'Thursday':4, 'Friday':5, 'Saturday':6}

    # Construct view
    # Top of view
    blocks = [
        forms.make_header_row("Please select a recurring event to delete:"),
        forms.make_divider()
    ]

    events_by_ao = list_to_dict(events, lambda x: x.ao_display_name)

    sorted_event_names = sorted(events_by_ao.keys(), key = lambda a: a.replace('The ', ''))

    # Show next x number of events
    for ao_display_name in sorted_event_names:
        # Header block
        blocks.append(forms.make_section_header_row(ao_display_name))

        sorted_events = sorted(events_by_ao[ao_display_name], key = lambda x: day_of_week_map[x.event_day_of_week])

        # Create button blocks for each event for each AO
        for event in sorted_events:
            button = inputs.ActionButton(
                label="Delete Event Series",
                action=actions.DELETE_RECURRING_SELECT_ACTION,
                value=str(event.id),
                style="danger",
            )
            blocks.append(forms.make_header_row(f"{event.event_type} {event.event_day_of_week}s @ {event.event_time}", accessory = button))
            blocks.append(forms.make_divider())

    # Cancel block
    blocks.append(forms.make_action_button_row([inputs.BACK_TO_MANAGE_BUTTON]))

    logger.debug("add_recurring_form blocks count=%s", len(blocks))
    # Publish view
    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks
            }
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)

def edit_recurring_form(
    team_id,
    user_id,
    client,
    logger,
    input_data,
    allowed_ao_channel_ids: Optional[List[str]] = None,
):
    event_id = int(input_data)
    event: vwWeeklyEvents = DbManager.find_records(vwWeeklyEvents, [vwWeeklyEvents.id == event_id])[0]

    aos = _aos_for_team(team_id, allowed_ao_channel_ids)
    ao_list = [ao.ao_display_name for ao in aos]

    event_start_time = event.event_time[:2] + ':' + event.event_time[2:]
    if event.event_end_time:
        event_end_time = event.event_end_time[:2] + ':' + event.event_end_time[2:]
    else:
        event_end_time = None
        
    if event.event_type in ['Bootcamp', 'QSource', 'Custom']:
        initial_type_select = event.event_type
        initial_type_manual = ''
    else:
        initial_type_select = 'Custom'
        initial_type_manual = event.event_type

    selector_input: inputs.ActionSelector = inputs.AO_SELECTOR.with_options(inputs.as_selector_options(ao_list))
    blocks = [
        inputs.EVENT_TYPE_SELECTOR.as_form_field(initial_value = initial_type_select),
        inputs.CUSTOM_EVENT_INPUT.as_form_field(initial_value = initial_type_manual),
        selector_input.as_form_field(initial_value = event.ao_display_name),
        inputs.WEEKDAY_SELECTOR.as_form_field(initial_value = event.event_day_of_week),
        inputs.START_DATE_SELECTOR.as_form_field(),
        inputs.START_TIME_SELECTOR.as_form_field(initial_value = event_start_time),
        inputs.END_TIME_SELECTOR.as_form_field(initial_value = event_end_time),
        forms.make_action_button_row([
            inputs.make_submit_button(actions.EDIT_RECURRING_EVENT_ACTION),
            inputs.BACK_TO_MANAGE_BUTTON
        ]),
        forms.make_header_row("Please wait after hitting Submit, and do not hit it more than once"),
    ]

    try:
        client.views_publish(
            user_id=user_id,
            view={
                "type": "home",
                "blocks": blocks,
                "private_metadata": json.dumps({"event_id": event_id}),
            }
        )
    except Exception as e:
        logger.error("Error publishing home tab: %s", e, exc_info=True)

def make_ao_section_selector(
    team_id,
    user_id,
    client,
    logger,
    label,
    action,
    allowed_ao_channel_ids: Optional[List[str]] = None,
    on_single_ao_callback=None,
):
    """
    If exactly one AO is allowed, call on_single_ao_callback(team_id, user_id, client, logger, channel_id)
    instead of showing a selector (e.g. jump straight to recurring list).
    """
    aos = _aos_for_team(team_id, allowed_ao_channel_ids)
    if not aos:
        logger.warning("make_ao_section_selector: no AOs")
        return
    if len(aos) == 1 and on_single_ao_callback:
        on_single_ao_callback(team_id, user_id, client, logger, aos[0].ao_channel_id)
        return

    ao_list = [ao.ao_display_name for ao in aos]
    ao_id_list = [ao.ao_channel_id for ao in aos]

    blocks = [
        inputs.SectionBlock(
            label=label,
            action=action,
            element=inputs.SelectorElement(
                placeholder="Select an AO",
                options=inputs.as_selector_options(ao_list, ao_id_list),
            ),
        ).as_form_field()
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