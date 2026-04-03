import logging
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)
def safe_get(data, *keys):
  try:
    result = data
    for k in keys:
      if result.get(k):
        result = result[k]
      else:
        return None
    return result
  except KeyError:
    return None

def list_to_dict(l, fn):
  result = {}
  for i in (l or []):
    key = fn(i)
    if not result.get(key): result[key] = []
    result[key].append(i)
  return result

@dataclass
class User:
  id: str
  name: str
  email: str | None = None
  is_admin: bool = False

def get_user(user_id, client) -> User | None:
  user_info_dict = client.users_info(user=user_id)
  _LOG.debug("users_info user_id=%s ok=%s", user_id, user_info_dict.get("ok") if isinstance(user_info_dict, dict) else None)
  if not safe_get(user_info_dict, "user"):
    return None
  u = user_info_dict.get("user") or {}
  user_name = safe_get(user_info_dict, 'user', 'profile', 'display_name') or \
              safe_get(user_info_dict, 'user', 'profile', 'real_name') or None
  display = user_name if user_name else user_id
  is_admin = bool(
      u.get("is_admin") or u.get("is_owner") or u.get("is_primary_owner")
  )
  return User(
      id=user_id,
      name=display,
      email=safe_get(user_info_dict, 'user', 'profile', 'email'),
      is_admin=is_admin,
  )
