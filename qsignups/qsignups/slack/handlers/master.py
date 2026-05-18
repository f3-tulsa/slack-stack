import json
from datetime import datetime

from database import DbManager
from database.orm import Master, AO, Region, helper
# from google import calendar
from . import UpdateResponse
from utilities import safe_get, User
# from google import authenticate, calendar

def _event_when_text(event_date, event_time) -> str:
    try:
        if event_date and event_time:
            dt = datetime.strptime(f"{event_date.strftime('%Y-%m-%d')} {event_time}", "%Y-%m-%d %H%M")
            day = dt.strftime("%d").lstrip("0") or "0"
            return f"{dt.strftime('%A, %B')} {day} @ {dt.strftime('%H%M')}"
    except Exception:
        pass
    return "the selected workout"


def _q_label(q_id: str | None, q_name: str | None) -> str:
    if q_id:
        return f"<@{q_id}>"
    if q_name:
        return q_name
    return "*OPEN*"


def _notify_signup_state_change(
    client,
    logger,
    ao_channel_id: str | None,
    ao_display_name: str | None,
    actor: User | None,
    event_date,
    event_time,
    previous_q_id: str | None,
    previous_q_name: str | None,
    new_q_id: str | None,
    new_q_name: str | None,
) -> None:
    if previous_q_id == new_q_id and previous_q_name == new_q_name:
        return
    if not ao_channel_id:
        return

    actor_label = f"<@{actor.id}>" if actor and actor.id else (actor.name if actor and actor.name else "Someone")
    when_text = _event_when_text(event_date, event_time)
    ao_name = ao_display_name or "this AO"
    message = (
        f":mega: Q signup update for *{ao_name}* on *{when_text}*.\n"
        f"Updated by: {actor_label}\n"
        f"Previous: {_q_label(previous_q_id, previous_q_name)}\n"
        f"Now: {_q_label(new_q_id, new_q_name)}"
    )
    try:
        client.chat_postMessage(channel=ao_channel_id, text=message)
    except Exception:
        logger.warning(
            "Failed to send Q signup state change notification channel=%s ao=%s",
            ao_channel_id,
            ao_name,
            exc_info=True,
        )


def delete(client, user_id, team_id, logger, input_data) -> UpdateResponse:

    # gather and format selected date and time
    selected_list = str.split(input_data,'|')
    selected_date = selected_list[0]
    selected_ao_id = selected_list[1]
    selected_date_dt = datetime.strptime(selected_date, '%Y-%m-%d %H:%M:%S')
    selected_date_db = datetime.date(selected_date_dt).strftime('%Y-%m-%d')
    selected_time_db = datetime.time(selected_date_dt).strftime('%H%M')

    # in the future we can use the primary key (id)
    master_filter = [
        Master.team_id == team_id,
        Master.ao_channel_id == selected_ao_id,
        Master.event_date == selected_date_dt.date(),
        Master.event_time == selected_time_db
    ]

    # Perform deletions
    try:
        DbManager.delete_records(Master, master_filter)
        return UpdateResponse(success = True, message=f"Success! Deleted event on {selected_date_db} at {selected_time_db}")
    except Exception as e:
        logger.error(f"Error deleting: {e}")
        return UpdateResponse(success = False, message = f"Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker. Errors:\n{e}")

def insert(client, user_id, team_id, logger, input_data) -> UpdateResponse:

    ao_display_name = input_data['ao_display_name_select_action']['ao_display_name_select_action']['selected_option']['value']
    event_date = input_data['add_event_datepicker']['add_event_datepicker']['selected_date']
    event_time = input_data['event_start_time_select']['event_start_time_select']['selected_time'].replace(':','')
    event_end_time = input_data['event_end_time_select']['event_end_time_select']['selected_time'].replace(':','')

    # Logic for custom events
    if input_data['event_type_select_action']['event_type_select_action']['selected_option']['value'] == 'Custom':
        event_type = input_data['event_type_custom']['event_type_custom']['value']
    else:
        event_type = input_data['event_type_select_action']['event_type_select_action']['selected_option']['value']

    # Grab channel id
    ao_channel_id = DbManager.find_records(AO, [
        AO.team_id == team_id,
        AO.ao_display_name == ao_display_name
    ])[0].ao_channel_id

    event_day_of_week = datetime.strptime(event_date, '%Y-%m-%d').date().strftime('%A')
    event_recurring = False

    # Attempt insert
    try:
        DbManager.create_record(Master(
            ao_channel_id = ao_channel_id,
            event_date = event_date,
            event_time = event_time,
            event_end_time = event_end_time,
            event_day_of_week = event_day_of_week,
            event_type = event_type,
            event_recurring = event_recurring,
            team_id = team_id
        ))
        return UpdateResponse(success = True, message="Got it - I've made your updates!")
    except Exception as e:
        logger.error(f"Error inserting: {e}")
        return UpdateResponse(success = False, message = f"Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker. Errors:\n{e}")

def update_events_from_state(
    client,
    user: User,
    team_id,
    logger,
    ao_channel_id: str,
    results: dict,
    original_date: str,
    original_time: str,
) -> UpdateResponse:
    """Apply single-event edit from home-tab state values (also used after confirmation modal)."""
    selected_date = results['edit_event_datepicker']['edit_event_datepicker']['selected_date']
    selected_time = results['edit_event_timepicker']['edit_event_timepicker']['selected_time'].replace(':','')
    selected_end_time = results['edit_event_end_timepicker']['edit_event_end_timepicker']['selected_time'].replace(':','')
    selected_q_id_list = results['edit_event_q_select']['edit_event_q_select']['selected_users']
    if len(selected_q_id_list) == 0:
        selected_q_id_fmt = None
        selected_q_name_fmt = None
    else:
        selected_q_id = selected_q_id_list[0]
        user_info_dict = client.users_info(user=selected_q_id)
        selected_q_name = safe_get(user_info_dict, 'user', 'profile', 'display_name') or safe_get(
            user_info_dict, 'user', 'profile', 'real_name') or None

        selected_q_id_fmt = selected_q_id
        selected_q_name_fmt = selected_q_name
    selected_special = results['edit_event_special_select']['edit_event_special_select']['selected_option']['text']['text']
    if selected_special == 'None':
        selected_special_fmt = None
    else:
        selected_special_fmt = selected_special

    try:
        DbManager.get_record(Region, team_id)
        ao: AO = helper.find_ao(team_id, ao_channel_id = ao_channel_id)

        records: list[Master] = DbManager.find_records(Master, filters = [
            Master.team_id == team_id,
            Master.ao_channel_id == ao.ao_channel_id,
            Master.event_date == datetime.strptime(original_date, '%Y-%m-%d'),
            Master.event_time == original_time,
        ])
        record_ids = [x.id for x in records]
        DbManager.update_records(cls=Master, filters=[
            Master.id.in_(record_ids)
        ], fields={
            Master.q_pax_id: selected_q_id_fmt,
            Master.q_pax_name: selected_q_name_fmt,
            Master.event_date: datetime.strptime(selected_date, '%Y-%m-%d'),
            Master.event_time: selected_time,
            Master.event_end_time: selected_end_time,
            Master.event_special: selected_special_fmt
        })
        google_records = [ x for x in records if x.google_event_id ]
        DbManager.find_records(Master, filters = [
            Master.id.in_([x.id for x in google_records])
        ])
        previous_q_id = records[0].q_pax_id if records else None
        previous_q_name = records[0].q_pax_name if records else None
        _notify_signup_state_change(
            client=client,
            logger=logger,
            ao_channel_id=ao.ao_channel_id if ao else None,
            ao_display_name=ao.ao_display_name if ao else None,
            actor=user,
            event_date=datetime.strptime(selected_date, "%Y-%m-%d").date(),
            event_time=selected_time,
            previous_q_id=previous_q_id,
            previous_q_name=previous_q_name,
            new_q_id=selected_q_id_fmt,
            new_q_name=selected_q_name_fmt,
        )
        # for event in records_to_reschedules:
        #     calendar.schedule_event(team_id, user, region, event, ao)
        return UpdateResponse(success = True, message="Got it - I've made your updates!")
    except Exception as e:
        logger.error(f"Error inserting: {e}")
        return UpdateResponse(success = False, message = f"Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker. Errors:\n{e}")


def update_events(client, user: User, team_id, logger, input_data) -> UpdateResponse:
    ao_channel_id = input_data["actions"][0]["value"]
    results = input_data["view"]["state"]["values"]
    pm = json.loads(input_data["view"].get("private_metadata") or "{}")
    return update_events_from_state(
        client,
        user,
        team_id,
        logger,
        ao_channel_id,
        results,
        pm["original_date"],
        pm["original_time"],
    )


def clear_event_q(client, user: User, team_id, logger, ao_display_name, selected_dt) -> UpdateResponse:

    # gather and format selected date and time
    result: helper.MasterEventAndAO = helper.find_master_event(team_id, selected_dt, ao_display_name = ao_display_name)
    if not result:
        return UpdateResponse(success = False, message = "Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker")

    try:
        previous_q_id = result.event.q_pax_id
        previous_q_name = result.event.q_pax_name
        DbManager.update_record(Master, result.event.id, {
            Master.q_pax_id: None,
            Master.q_pax_name: None
        })
        _notify_signup_state_change(
            client=client,
            logger=logger,
            ao_channel_id=result.ao.ao_channel_id,
            ao_display_name=result.ao.ao_display_name,
            actor=user,
            event_date=result.event.event_date,
            event_time=result.event.event_time,
            previous_q_id=previous_q_id,
            previous_q_name=previous_q_name,
            new_q_id=None,
            new_q_name=None,
        )
        if result.event.google_event_id:
            DbManager.get_record(Region, team_id)
            # calendar.schedule_event(team_id, None, region, result.event, result.ao)

        return UpdateResponse(success = True, message=f"Got it, {user.name}! I have cleared the Q slot at *{ao_display_name}* on *{selected_dt.strftime('%A, %B %-d @ %H%M')}*")
    except Exception as e:
        logger.error("Error updating: %s", e, exc_info=True)
        return UpdateResponse(success = False, message = f"Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker {e}")

def assign_event_q(client, user: User, team_id, logger, selected_dt, ao_display_name = None, ao_channel_id = None) -> UpdateResponse:

    result: helper.MasterEventAndAO = helper.find_master_event(team_id, selected_dt, ao_display_name = ao_display_name, ao_channel_id = ao_channel_id)

    if not result:
        return UpdateResponse(success = False, message = "Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker.")
    previous_q_id = result.event.q_pax_id
    previous_q_name = result.event.q_pax_name
    DbManager.update_record(Master, result.event.id, {
        Master.q_pax_id: user.id,
        Master.q_pax_name: user.name
    })
    _notify_signup_state_change(
        client=client,
        logger=logger,
        ao_channel_id=result.ao.ao_channel_id,
        ao_display_name=result.ao.ao_display_name,
        actor=user,
        event_date=result.event.event_date,
        event_time=result.event.event_time,
        previous_q_id=previous_q_id,
        previous_q_name=previous_q_name,
        new_q_id=user.id,
        new_q_name=user.name,
    )
    # if authenticate.is_connected(team_id):
    #     new_master = DbManager.get_record(Master, result.event.id)
    #     region: Region = DbManager.get_record(Region, team_id)
    #     event = calendar.schedule_event(team_id, user, region, new_master, result.ao)
    #     if event and event.get('id'):
    #         DbManager.update_record(Master, result.event.id, {
    #             Master.google_event_id: event['id'],
    #         })

    return UpdateResponse(success = True, message="Got it - I've made your updates!")
