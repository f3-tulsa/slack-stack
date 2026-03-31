from database import DbManager, get_session, close_session
from database.orm import Weekly, Master, AO
from database.orm.views import vwWeeklyEvents
from . import UpdateResponse
from datetime import date, datetime, timedelta
from typing import List

from sqlalchemy import and_, func
from slack import inputs
from utilities import safe_get
from constants import SCHEDULE_CREATE_LENGTH_DAYS

# Chunk size for bulk inserts (avoid oversized single transactions)
_EXTEND_INSERT_CHUNK = 2000


def _schedule_horizon() -> date:
    return date.today() + timedelta(days=SCHEDULE_CREATE_LENGTH_DAYS)


def _build_recurring_master_rows(
    ao_channel_id: str,
    event_day_of_week: str,
    event_time: str,
    event_end_time,
    event_type: str,
    team_id: str,
    event_recurring: bool,
    start_date_inclusive: date,
    horizon_exclusive: date,
) -> List[Master]:
    """One row per matching weekday from start_date_inclusive up to (not including) horizon_exclusive."""
    record_list = []
    iterate_date = start_date_inclusive
    while iterate_date < horizon_exclusive:
        if iterate_date.strftime("%A") == event_day_of_week:
            record_list.append(
                Master(
                    ao_channel_id=ao_channel_id,
                    event_date=iterate_date,
                    event_time=event_time,
                    event_end_time=event_end_time,
                    event_day_of_week=event_day_of_week,
                    event_type=event_type,
                    event_recurring=event_recurring,
                    team_id=team_id,
                )
            )
        iterate_date += timedelta(days=1)
    return record_list


def extend_all_schedules(logger) -> None:
    """
    Remove orphan future recurring Master rows (no matching Weekly series), then for each
    qsignups_weekly row ensure qsignups_master has recurring rows through today +
    SCHEDULE_CREATE_LENGTH_DAYS (same horizon as weekly.insert).
    """
    today = date.today()
    horizon = _schedule_horizon()
    record_list = []
    session = get_session()
    try:
        weeklies = session.query(Weekly).all()
        weekly_set = {(w.ao_channel_id, w.event_day_of_week, w.event_time, w.team_id) for w in weeklies}

        # Orphan cleanup: future recurring Master rows whose series tuple no longer exists in Weekly
        distinct_rows = (
            session.query(
                Master.ao_channel_id,
                Master.event_day_of_week,
                Master.event_time,
                Master.team_id,
            )
            .filter(
                Master.event_date > today,
                Master.event_recurring == True,
            )
            .distinct()
            .all()
        )
        for row in distinct_rows:
            t = (row.ao_channel_id, row.event_day_of_week, row.event_time, row.team_id)
            if t not in weekly_set:
                DbManager.delete_records(
                    Master,
                    [
                        Master.team_id == row.team_id,
                        Master.ao_channel_id == row.ao_channel_id,
                        Master.event_day_of_week == row.event_day_of_week,
                        Master.event_time == row.event_time,
                        Master.event_date > today,
                        Master.event_recurring == True,
                    ],
                )
                logger.info("extend_all_schedules: removed orphan recurring series %s", t)

        for w in weeklies:
            max_date = (
                session.query(func.max(Master.event_date))
                .filter(
                    and_(
                        Master.ao_channel_id == w.ao_channel_id,
                        Master.event_day_of_week == w.event_day_of_week,
                        Master.event_time == w.event_time,
                        Master.team_id == w.team_id,
                        Master.event_recurring == True,
                    )
                )
                .scalar()
            )
            start = (max_date + timedelta(days=1)) if max_date else date.today()
            record_list.extend(
                _build_recurring_master_rows(
                    w.ao_channel_id,
                    w.event_day_of_week,
                    w.event_time,
                    w.event_end_time,
                    w.event_type,
                    w.team_id,
                    True,
                    start,
                    horizon,
                )
            )
    finally:
        session.rollback()
        close_session(session)

    if not record_list:
        logger.info("extend_all_schedules: no new master rows needed")
        return

    for i in range(0, len(record_list), _EXTEND_INSERT_CHUNK):
        DbManager.create_records(record_list[i : i + _EXTEND_INSERT_CHUNK])
    logger.info("extend_all_schedules: inserted %s master rows", len(record_list))


def delete(client, user_id, team_id, logger, input_data) -> UpdateResponse:

    weekly_event = DbManager.get_record(vwWeeklyEvents, input_data)

    # in the future we can use the FK from Weekly
    master_filter = [
        Master.team_id == team_id,
        Master.ao_channel_id == weekly_event.ao_channel_id,
        Master.event_day_of_week == weekly_event.event_day_of_week,
        Master.event_time == weekly_event.event_time,
        Master.event_date >= date.today()
    ]

    # Perform deletions
    try:
        DbManager.delete_records(Master, master_filter)
        DbManager.delete_record(Weekly, weekly_event.id)
        return UpdateResponse(success = True, message=f"I've deleted all future {weekly_event.ao_display_name}s from the schedule for {weekly_event.event_day_of_week}s at {weekly_event.event_time} at {weekly_event.ao_display_name}.")
    except Exception as e:
        logger.error(f"Error deleting: {e}")
        return UpdateResponse(success = False, message = f"Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker. Errors:\n{e}")

def edit(client, user_id, team_id, logger, body) -> UpdateResponse:

    input_data = body['view']['state']['values']
    event_id = int(body['view']['blocks'][-1]['elements'][0]['text'])

    # Gather inputs from form
    ao_display_name = inputs.AO_SELECTOR.get_selected_value(input_data)
    event_day_of_week = inputs.WEEKDAY_SELECTOR.get_selected_value(input_data)
    event_time = inputs.START_TIME_SELECTOR.get_selected_value(input_data).replace(":", "")
    event_end_time = inputs.END_TIME_SELECTOR.get_selected_value(input_data)
    if event_end_time:
        event_end_time = event_end_time.replace(":", "")
    event_type = inputs.EVENT_TYPE_SELECTOR.get_selected_value(input_data)

    event_recurring = True
    
    if event_type == 'Custom':
        event_type = inputs.CUSTOM_EVENT_INPUT.get_selected_value(input_data) or 'Custom'

    try:
        # Grab channel id
        ao: AO = DbManager.find_records(AO, [AO.team_id == team_id, AO.ao_display_name == ao_display_name])[0]
        ao_channel_id = ao.ao_channel_id

        original_record: Weekly = DbManager.get_record(Weekly, event_id)

        # Update Weekly table
        DbManager.update_record(Weekly, event_id, {
            Weekly.ao_channel_id: ao_channel_id,
            Weekly.event_day_of_week: event_day_of_week,
            Weekly.event_time: event_time,
            Weekly.event_end_time: event_end_time,
            Weekly.event_type: event_type,
            Weekly.team_id: team_id
        })

        # Delete future recurring rows for the old series, then rebuild through the rolling horizon
        DbManager.delete_records(Master, [
            Master.team_id == team_id,
            Master.ao_channel_id == original_record.ao_channel_id,
            Master.event_day_of_week == original_record.event_day_of_week,
            Master.event_time == original_record.event_time,
            Master.event_recurring == True,
            Master.event_date > date.today(),
        ])
        tomorrow = date.today() + timedelta(days=1)
        horizon = _schedule_horizon()
        new_rows = _build_recurring_master_rows(
            ao_channel_id,
            event_day_of_week,
            event_time,
            event_end_time,
            event_type,
            team_id,
            event_recurring,
            tomorrow,
            horizon,
        )
        if new_rows:
            DbManager.create_records(new_rows)

        return UpdateResponse(success = True, message=f"Got it - I've made your updates!")
    except Exception as e:
        logger.error(f"Error updating: {e}")
        return UpdateResponse(success = False, message = f"Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker. Errors:\n{e}")

def insert(client, user_id, team_id, logger, input_data) -> UpdateResponse:

    ao_display_name = safe_get(input_data, 'ao_display_name_select_action','ao_display_name_select_action','selected_option','value')
    event_day_of_week = safe_get(input_data, 'event_day_of_week_select_action','event_day_of_week_select_action','selected_option','value')
    starting_date = safe_get(input_data, 'add_event_datepicker','add_event_datepicker','selected_date')
    event_time = safe_get(input_data, 'event_start_time_select','event_start_time_select','selected_time').replace(':','')
    event_end_time = safe_get(input_data, 'event_end_time_select','event_end_time_select','selected_time').replace(':','')
    event_type_select = safe_get(input_data, 'event_type_select_action','event_type_select_action','selected_option','value')
    event_type_custom = safe_get(input_data, 'event_type_custom','event_type_custom','value')
    event_recurring = True

    # Logic for custom events
    if event_type_select == 'Custom':
        event_type = event_type_custom
    else:
        event_type = event_type_select

    ao_channel_id = DbManager.find_records(AO, [
        AO.team_id == team_id,
        AO.ao_display_name == ao_display_name
    ])[0].ao_channel_id

    try:
        DbManager.create_record(Weekly(
            ao_channel_id = ao_channel_id,
            event_day_of_week = event_day_of_week,
            event_time = event_time,
            event_end_time = event_end_time,
            event_type = event_type,
            team_id = team_id
        ))

        start = datetime.strptime(starting_date, '%Y-%m-%d').date()
        record_list = _build_recurring_master_rows(
            ao_channel_id,
            event_day_of_week,
            event_time,
            event_end_time,
            event_type,
            team_id,
            event_recurring,
            start,
            _schedule_horizon(),
        )

        DbManager.create_records(record_list)
        return UpdateResponse(success = True, message=f"Got it - I've made your updates!")
    except Exception as e:
        logger.error(f"Error updating: {e}")
        return UpdateResponse(success = False, message = f"Sorry, there was an error of some sort; please try again or contact your local administrator / Weasel Shaker. Errors:\n{e}")

