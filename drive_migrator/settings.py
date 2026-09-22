"""Explicit configuration; private runtime state stays outside the checkout."""
import json
import os
from pathlib import Path
import re

DEFAULT = {
    'source_email': 'source@example.test',
    'destination_email': 'destination@example.test',
    'source_root_id': 'SOURCE_ROOT_ID',
    'destination_root_id': 'DESTINATION_ROOT_ID',
    'controller_id': 'CONTROLLER_ID',
    'state_dir': '~/.local/state/drive-account-migrator',
    'share_source': False,
    'blocked_source_ids': [],
}


def load():
    path = os.environ.get('DRIVE_MIGRATOR_CONFIG')
    if not path:
        return dict(DEFAULT)  # Importable for offline unit tests; CLI requires a config.
    value = json.loads(Path(path).expanduser().read_text())
    missing = set(DEFAULT) - set(value)
    if missing:
        raise ValueError('Missing configuration keys: ' + ', '.join(sorted(missing)))
    for field in ('source_email', 'destination_email'):
        value[field] = value[field].strip().lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value[field]):
            raise ValueError('Invalid email configuration')
    if value['source_email'] == value['destination_email']:
        raise ValueError('Source and destination must be different accounts')
    for field in ('source_root_id', 'destination_root_id', 'controller_id'):
        if not isinstance(value[field], str) or not re.fullmatch(r'[\w-]+', value[field]):
            raise ValueError('Invalid Drive ID: ' + field)
    if value['source_root_id'] == value['destination_root_id']:
        raise ValueError('Source and destination roots must differ')
    if type(value['share_source']) is not bool:
        raise ValueError('share_source must be true or false')
    if not isinstance(value['blocked_source_ids'], list) or any(
        not isinstance(v, str) or not re.fullmatch(r'[\w-]+', v)
        for v in value['blocked_source_ids']
    ):
        raise ValueError('Invalid blocked_source_ids')
    state = Path(value['state_dir']).expanduser().resolve()
    checkout = Path(__file__).resolve().parent.parent
    if state == checkout or checkout in state.parents:
        raise ValueError('state_dir must be outside the repository')
    value['state_dir'] = str(state)
    return value


CONFIG = load()
