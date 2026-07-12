import copy
import json
from logging import Logger

from slack_sdk.web import WebClient

from utilities import constants
from utilities.database import DbManager
from utilities.database.orm import Region
from utilities.field_encryption import decrypt_field, encrypt_field
from utilities.helper_functions import (
    safe_get,
    update_local_region_records,
)
from utilities.slack import actions, forms


def build_config_form(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    user_id = safe_get(body, "user_id") or safe_get(body, "user", "id")
    user_info_dict = client.users_info(user=user_id)
    update_view_id = safe_get(body, actions.LOADING_ID)

    if user_info_dict["user"]["is_admin"]:
        config_form = copy.deepcopy(forms.CONFIG_FORM)
    else:
        config_form = copy.deepcopy(forms.CONFIG_NO_PERMISSIONS_FORM)

    config_form.update_modal(
        client=client,
        view_id=update_view_id,
        callback_id=actions.CONFIG_CALLBACK_ID,
        title_text="Slackblast Settings",
        submit_button_text="None",
    )


def build_config_email_form(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    config_form = copy.deepcopy(forms.CONFIG_EMAIL_FORM)

    if region_record.email_password:
        email_password_decrypted = decrypt_field(region_record.email_password) or ""
        if email_password_decrypted.startswith("gAAAAA"):
            email_password_decrypted = ""
    else:
        email_password_decrypted = "SamplePassword123!"

    config_form.set_initial_values(
        {
            actions.CONFIG_EMAIL_ENABLE: "enable" if region_record.email_enabled == 1 else "disable",
            actions.CONFIG_EMAIL_SHOW_OPTION: "yes" if region_record.email_option_show == 1 else "no",
            actions.CONFIG_EMAIL_FROM: region_record.email_user or "example_sender@gmail.com",
            actions.CONFIG_EMAIL_TO: region_record.email_to or "example_destination@gmail.com",
            actions.CONFIG_EMAIL_SERVER: region_record.email_server or "smtp.gmail.com",
            actions.CONFIG_EMAIL_PORT: str(region_record.email_server_port or 587),
            actions.CONFIG_EMAIL_PASSWORD: email_password_decrypted,
            actions.CONFIG_POSTIE_ENABLE: "yes" if region_record.postie_format == 1 else "no",
        }
    )

    config_form.post_modal(
        client=client,
        trigger_id=safe_get(body, "trigger_id"),
        callback_id=actions.CONFIG_EMAIL_CALLBACK_ID,
        title_text="Email Settings",
        new_or_add="add",
    )


def build_config_general_form(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    config_form = copy.deepcopy(forms.CONFIG_GENERAL_FORM)
    if not region_record.paxminer_schema:
        config_form.delete_block(actions.CONFIG_POST_ACHIEVEMENTS_TO_AO)
        config_form.delete_block(actions.CONFIG_POST_ACHIEVEMENTS_TO_AO_CONTEXT)

    config_form.set_initial_values(
        {
            actions.CONFIG_EDITING_LOCKED: "yes" if region_record.editing_locked == 1 else "no",
            actions.CONFIG_DEFAULT_DESTINATION: region_record.default_destination
            or constants.CONFIG_DESTINATION_AO["value"],
            actions.CONFIG_BACKBLAST_MOLESKINE_TEMPLATE: region_record.backblast_moleskin_template
            or constants.DEFAULT_BACKBLAST_MOLESKINE_TEMPLATE,
            actions.CONFIG_PREBLAST_MOLESKINE_TEMPLATE: region_record.preblast_moleskin_template
            or constants.DEFAULT_PREBLAST_MOLESKINE_TEMPLATE,
            actions.CONFIG_ENABLE_STRAVA: "enable" if region_record.strava_enabled == 1 else "disable",
            actions.CONFIG_POST_ACHIEVEMENTS_TO_AO: "yes"
            if getattr(region_record, "post_achievements_to_ao", 0) == 1 and region_record.paxminer_schema
            else "no",
        }
    )

    config_form.post_modal(
        client=client,
        trigger_id=safe_get(body, "trigger_id"),
        callback_id=actions.CONFIG_GENERAL_CALLBACK_ID,
        title_text="General Settings",
        new_or_add="add",
    )


def handle_config_email_post(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    config_data = forms.CONFIG_EMAIL_FORM.get_selected_values(body)

    fields = {
        Region.email_enabled: 1 if safe_get(config_data, actions.CONFIG_EMAIL_ENABLE) == "enable" else 0,
    }
    if safe_get(config_data, actions.CONFIG_EMAIL_ENABLE) == "enable":
        email_password_decrypted = safe_get(config_data, actions.CONFIG_EMAIL_PASSWORD)
        if email_password_decrypted:
            email_password_encrypted = encrypt_field(email_password_decrypted)
        else:
            email_password_encrypted = None
        fields.update(
            {
                Region.email_option_show: 1 if safe_get(config_data, actions.CONFIG_EMAIL_SHOW_OPTION) == "yes" else 0,
                Region.email_server: safe_get(config_data, actions.CONFIG_EMAIL_SERVER),
                Region.email_server_port: safe_get(config_data, actions.CONFIG_EMAIL_PORT),
                Region.email_user: safe_get(config_data, actions.CONFIG_EMAIL_FROM),
                Region.email_to: safe_get(config_data, actions.CONFIG_EMAIL_TO),
                Region.email_password: email_password_encrypted,
                Region.postie_format: 1 if safe_get(config_data, actions.CONFIG_POSTIE_ENABLE) == "yes" else 0,
            }
        )

    DbManager.update_record(
        cls=Region,
        id=context["team_id"],
        fields=fields,
    )
    update_local_region_records(context["team_id"])
    logger.info(json.dumps({"event_type": "successful_config_update", "team_name": region_record.workspace_name}))


def handle_config_general_post(body: dict, client: WebClient, logger: Logger, context: dict, region_record: Region):
    config_data = forms.CONFIG_GENERAL_FORM.get_selected_values(body)

    fields = {
        Region.editing_locked: 1 if safe_get(config_data, actions.CONFIG_EDITING_LOCKED) == "yes" else 0,
        Region.default_destination: safe_get(config_data, actions.CONFIG_DEFAULT_DESTINATION),
        Region.backblast_moleskin_template: safe_get(config_data, actions.CONFIG_BACKBLAST_MOLESKINE_TEMPLATE),
        Region.preblast_moleskin_template: safe_get(config_data, actions.CONFIG_PREBLAST_MOLESKINE_TEMPLATE),
        Region.strava_enabled: 1 if safe_get(config_data, actions.CONFIG_ENABLE_STRAVA) == "enable" else 0,
    }
    if region_record.paxminer_schema:
        fields[Region.post_achievements_to_ao] = (
            1 if safe_get(config_data, actions.CONFIG_POST_ACHIEVEMENTS_TO_AO) == "yes" else 0
        )

    DbManager.update_record(
        cls=Region,
        id=context["team_id"],
        fields=fields,
    )
    update_local_region_records(context["team_id"])
    logger.info(json.dumps({"event_type": "successful_config_update", "team_name": region_record.workspace_name}))
