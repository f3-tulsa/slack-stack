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
  email: str

def get_user(user_id, client) -> User:
  user_info_dict = client.users_info(user=user_id)
  _LOG.debug("users_info user_id=%s ok=%s", user_id, user_info_dict.get("ok") if isinstance(user_info_dict, dict) else None)
  user_name = safe_get(user_info_dict, 'user', 'profile', 'display_name') or \
              safe_get(user_info_dict, 'user', 'profile', 'real_name') or None
  if user_name:
    return User(
      id = user_id,
      name = user_name,
      email = safe_get(user_info_dict, 'user', 'profile', 'email')
    )
  else:
    return None
