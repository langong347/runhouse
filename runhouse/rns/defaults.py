import json
import logging
import requests
import yaml
from typing import Dict, Optional, Any
from pathlib import Path
import copy

from runhouse.rns.api_utils.utils import read_response_data, to_bool

from runhouse.logger import LOGGING_CONFIG

# Configure the logger once
logging.config.dictConfig(LOGGING_CONFIG)

logger = logging.getLogger(__name__)


class Defaults:
    """Class to handle defaults for Runhouse. Defaults are stored in a json file in the user's home directory."""

    USER_ENDPOINT = 'user/'
    GROUP_ENDPOINT = 'group/'
    CONFIG_PATH = Path('~/.rh/config.yaml').expanduser()
    # TODO [DG] default sub-dicts for various resources (e.g. defaults.get('cluster').get('resource_type'))
    BASE_DEFAULTS = {'default_folder': '/default',
                     'default_provider': 'cheapest',
                     'default_autostop': -1,
                     'use_spot': False,
                     'use_local_configs': True,
                     'use_rns': False,
                     'api_server_url': "https://api.run.house"
                     }

    def __init__(self):
        self._defaults_cache = {}

    @property
    def defaults_cache(self):
        if not self._defaults_cache:
            self._defaults_cache = self.load_defaults_from_file()
        return self._defaults_cache

    @defaults_cache.setter
    def defaults_cache(self, value: Dict):
        self._defaults_cache = value

    def load_defaults_from_file(self, config_path: Optional[str] = None) -> Dict:
        config_path = config_path or self.CONFIG_PATH
        config = {}
        if Path(config_path).exists():
            with open(config_path, 'r') as stream:
                config = yaml.safe_load(stream)
            logging.info(f'Loaded Runhouse config from {config_path}')

        return config or {}

    @property
    def request_headers(self):
        return {"Authorization": f"Bearer {self.get('token')}"}

    def upload_defaults(self,
                        defaults: Optional[Dict] = None,
                        headers: Optional[Dict] = None,
                        entity: Optional[str] = 'user'):
        """Upload defaults into rns. If defaults is None, upload the defaults from the local config file,
        `~/.rh/config.yaml."""
        if not defaults:
            defaults = self.defaults_cache
            self.save_defaults()
        to_upload = copy.deepcopy(defaults)
        # We don't need to save these
        to_upload.pop('token', None)
        to_upload.pop('username', None)

        endpoint = self.USER_ENDPOINT if entity == 'user' else self.GROUP_ENDPOINT + f'/{entity}'
        resp = requests.put(f'{self.get("api_server_url")}/{endpoint}/config',
                            data=json.dumps(to_upload),
                            headers=headers or self.request_headers)
        if resp.status_code != 200:
            raise Exception(f'Failed to update defaults for {entity}, received status code {resp.status_code}')
        logger.info(f'Uploaded defaults for {entity} to rns.')

    def download_defaults(self,
                          headers: Optional[Dict] = None,
                          entity: Optional[str] = 'user') -> Dict:
        """ Get defaults for user or group."""
        endpoint = self.USER_ENDPOINT if entity == 'user' else self.GROUP_ENDPOINT + f'/{entity}'
        headers = headers or self.request_headers
        resp = requests.get(f'{self.get("api_server_url")}/{endpoint}',
                            headers=headers)
        if resp.status_code != 200:
            raise Exception(f'Failed to download defaults for {entity}, received status code {resp.status_code}')
        resp_data: dict = read_response_data(resp)
        raw_defaults = resp_data.get('config', {})
        raw_defaults['username'] = resp_data.get('username')
        raw_defaults['token'] = headers.get('Authorization')[7:]
        raw_defaults['default_folder'] = '/' + raw_defaults['username'] if \
            raw_defaults.get('default_folder') in ['/default', None] else raw_defaults.get('default_folder')
        formatted = {k: to_bool(v) for k, v in raw_defaults.items()}

        return formatted

    def save_defaults(self, defaults: Optional[Dict] = None, config_path: Optional[str] = None):
        config_path = Path(config_path or self.CONFIG_PATH)
        defaults = defaults or self.defaults_cache

        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)

        with config_path.open('w') as stream:
            yaml.dump(defaults, stream)

    def download_and_save_defaults(self,
                                   headers: Optional[Dict] = None,
                                   entity: Optional[str] = 'user',
                                   config_path: Optional[str] = None):
        """Download defaults from rns and save them to the local config file."""
        defaults = self.download_defaults(headers=headers, entity=entity)
        # Note: downloaded defaults take priority over local defaults
        self.set_many(defaults, config_path=config_path)

    def set(self, key: str, value: Any, config_path: Optional[str] = None):
        self.defaults_cache[key] = value
        self.save_defaults(config_path=config_path)

    def set_many(self, key_value_pairs: Dict, config_path: Optional[str] = None):
        self.defaults_cache.update(key_value_pairs)
        self.save_defaults(config_path=config_path)

    # TODO [DG] allow hierarchical defaults from folders and groups
    def get(self, key: str, alt: Any = None) -> Any:
        res = self.defaults_cache.get(key, alt)
        if not res and key in self.BASE_DEFAULTS:
            res = self.BASE_DEFAULTS[key]
        return res
