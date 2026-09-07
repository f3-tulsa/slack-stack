import copy
import json
import random
from logging import Logger
from typing import Callable

from slack_sdk.web import WebClient
from sqlalchemy.exc import IntegrityError

from utilities import constants
from utilities.database import DbManager
from utilities.database.orm import (
    Region,
    WelcomeDelivery,
)
from utilities.helper_functions import (
    safe_get,
    update_local_region_records,
)
from utilities.slack import actions, forms

WELCOME_DM_DESTINATION = "dm"
WELCOME_CHANNEL_DESTINATION = "channel"


def _is_duplicate_key_error(error: IntegrityError) -> bool:
    return bool(error.orig and getattr(error.orig, "args", ()) and error.orig.args[0] == 1062)


def claim_welcome_delivery(event_id: str, destination: str, team_id: str, user_id: str) -> bool:
    try:
        with DbManager.transaction() as session:
            session.add(
                WelcomeDelivery(
                    event_id=event_id,
                    destination=destination,
                    team_id=team_id,
                    user_id=user_id,
                )
            )
            session.flush()
        return True
    except IntegrityError as error:
        if _is_duplicate_key_error(error):
            return False
        raise


def release_welcome_delivery(event_id: str, destination: str) -> None:
    with DbManager.transaction() as session:
        session.query(WelcomeDelivery).filter(
            WelcomeDelivery.event_id == event_id,
            WelcomeDelivery.destination == destination,
        ).delete(synchronize_session=False)


def _post_welcome_once(
    *,
    event_id: str,
    destination: str,
    team_id: str,
    user_id: str,
    post: Callable[[], None],
    logger: Logger,
) -> None:
    if not claim_welcome_delivery(event_id, destination, team_id, user_id):
        logger.info(
            "Skipping duplicate welcome event_id=%s team_id=%s user_id=%s destination=%s",
            event_id,
            team_id,
            user_id,
            destination,
        )
        return

    try:
        post()
    except Exception:
        release_welcome_delivery(event_id, destination)
        raise


def build_welcome_config_form(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    welcome_message_config_form = copy.deepcopy(forms.WELCOME_MESSAGE_CONFIG_FORM)

    welcome_message_config_form.set_initial_values(
        {
            actions.WELCOME_DM_TEMPLATE: region_record.welcome_dm_template,
            actions.WELCOME_DM_ENABLE: "enable" if region_record.welcome_dm_enable else "disable",
            actions.WELCOME_CHANNEL: region_record.welcome_channel or "",
            actions.WELCOME_CHANNEL_ENABLE: "enable" if region_record.welcome_channel_enable else "disable",
        }
    )

    welcome_message_config_form.post_modal(
        client=client,
        trigger_id=safe_get(body, "trigger_id"),
        callback_id=actions.WELCOME_MESSAGE_CONFIG_CALLBACK_ID,
        title_text="Welcomebot Settings",
        new_or_add="add",
    )


# eventually will not need this when we take out the /config-welcome-message command
def build_welcome_message_form(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    update_view_id = safe_get(body, actions.LOADING_ID)
    welcome_message_config_form = copy.deepcopy(forms.WELCOME_MESSAGE_CONFIG_FORM)

    welcome_message_config_form.set_initial_values(
        {
            actions.WELCOME_DM_TEMPLATE: region_record.welcome_dm_template,
            actions.WELCOME_DM_ENABLE: "enable" if region_record.welcome_dm_enable else "disable",
            actions.WELCOME_CHANNEL: region_record.welcome_channel or "",
            actions.WELCOME_CHANNEL_ENABLE: "enable" if region_record.welcome_channel_enable else "disable",
        }
    )

    welcome_message_config_form.update_modal(
        client=client,
        view_id=update_view_id,
        callback_id=actions.WELCOME_MESSAGE_CONFIG_CALLBACK_ID,
        title_text="Welcomebot Settings",
        parent_metadata=None,
    )


def handle_welcome_message_config_post(
    body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region
):
    welcome_config_data = forms.WELCOME_MESSAGE_CONFIG_FORM.get_selected_values(body)

    fields = {
        Region.welcome_dm_enable: 1 if safe_get(welcome_config_data, actions.WELCOME_DM_ENABLE) == "enable" else 0,
        Region.welcome_dm_template: safe_get(welcome_config_data, actions.WELCOME_DM_TEMPLATE) or "",
        Region.welcome_channel_enable: (
            1 if safe_get(welcome_config_data, actions.WELCOME_CHANNEL_ENABLE) == "enable" else 0
        ),
        Region.welcome_channel: safe_get(welcome_config_data, actions.WELCOME_CHANNEL) or "",
    }

    DbManager.update_record(
        cls=Region,
        id=context["team_id"],
        fields=fields,
    )
    update_local_region_records(context["team_id"])
    logger.info(json.dumps({"event_type": "successful_config_update", "team_name": region_record.workspace_name}))


def handle_team_join(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    welcome_channel = region_record.welcome_channel
    workspace_name = region_record.workspace_name
    user_id = safe_get(body, "event", "user", "id")
    event_id = safe_get(body, "event_id")
    team_id = safe_get(body, "team_id") or safe_get(body, "team", "id") or context.get("team_id")

    if (region_record.welcome_dm_enable or region_record.welcome_channel_enable) and not event_id:
        raise ValueError("team_join event is missing event_id; refusing non-idempotent welcome delivery")

    if region_record.welcome_dm_enable:
        _post_welcome_once(
            event_id=event_id,
            destination=WELCOME_DM_DESTINATION,
            team_id=team_id,
            user_id=user_id,
            post=lambda: client.chat_postMessage(
                channel=user_id,
                blocks=[region_record.welcome_dm_template],
                text="Welcome!",
            ),
            logger=logger,
        )
    if region_record.welcome_channel_enable:
        _post_welcome_once(
            event_id=event_id,
            destination=WELCOME_CHANNEL_DESTINATION,
            team_id=team_id,
            user_id=user_id,
            post=lambda: client.chat_postMessage(
                channel=welcome_channel,
                text=random.choice(constants.WELCOME_MESSAGE_TEMPLATES).format(
                    user=f"<@{user_id}>",
                    region=workspace_name,
                ),
            ),
            logger=logger,
        )
