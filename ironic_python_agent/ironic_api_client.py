# Copyright 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from oslo_config import cfg
from oslo_log import log
from oslo_serialization import jsonutils
from oslo_service import loopingcall
import requests

from ironic_python_agent import encoding
from ironic_python_agent import errors
from ironic_python_agent import netutils
from ironic_python_agent import utils
from ironic_python_agent import version


CONF = cfg.CONF
LOG = log.getLogger(__name__)

MIN_IRONIC_VERSION = (1, 22)
AGENT_VERSION_IRONIC_VERSION = (1, 36)


class APIClient(object):
    api_version = 'v1'
    lookup_api = '/%s/lookup' % api_version
    heartbeat_api = '/%s/heartbeat/{uuid}' % api_version
    _ironic_api_version = None

    def __init__(self, api_url):
        self.api_url = api_url.rstrip('/')

        # Only keep alive a maximum of 2 connections to the API. More will be
        # opened if they are needed, but they will be closed immediately after
        # use.
        adapter = requests.adapters.HTTPAdapter(pool_connections=2,
                                                pool_maxsize=2)
        self.session = requests.Session()
        self.session.mount(self.api_url, adapter)

        self.encoder = encoding.RESTJSONEncoder()

    def _request(self, method, path, data=None, headers=None, **kwargs):
        request_url = '{api_url}{path}'.format(api_url=self.api_url, path=path)

        if data is not None:
            data = self.encoder.encode(data)

        headers = headers or {}
        headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

        verify, cert = utils.get_ssl_client_options(CONF)
        return self.session.request(method,
                                    request_url,
                                    headers=headers,
                                    data=data,
                                    verify=verify,
                                    cert=cert,
                                    **kwargs)

    def _get_ironic_api_version_header(self, version=MIN_IRONIC_VERSION):
        version_str = "%d.%d" % version
        return {'X-OpenStack-Ironic-API-Version': version_str}

    def _get_ironic_api_version(self):
        if not self._ironic_api_version:
            try:
                response = self._request('GET', '/')
                data = jsonutils.loads(response.content)
                version = data['default_version']['version'].split('.')
                self._ironic_api_version = (int(version[0]), int(version[1]))
            except Exception:
                LOG.exception("An error occurred while attempting to discover "
                              "the available Ironic API versions, falling "
                              "back to using version %s",
                              ".".join(map(str, MIN_IRONIC_VERSION)))
                return MIN_IRONIC_VERSION
        return self._ironic_api_version

    def heartbeat(self, uuid, advertise_address):
        path = self.heartbeat_api.format(uuid=uuid)

        data = {'callback_url': self._get_agent_url(advertise_address)}

        if self._get_ironic_api_version() >= AGENT_VERSION_IRONIC_VERSION:
            data['agent_version'] = version.version_info.release_string()
            headers = self._get_ironic_api_version_header(
                AGENT_VERSION_IRONIC_VERSION)
        else:
            headers = self._get_ironic_api_version_header()

        try:
            response = self._request('POST', path, data=data, headers=headers)
        except Exception as e:
            raise errors.HeartbeatError(str(e))

        if response.status_code == requests.codes.CONFLICT:
            data = jsonutils.loads(response.content)
            raise errors.HeartbeatConflictError(data.get('faultstring'))
        elif response.status_code != requests.codes.ACCEPTED:
            msg = 'Invalid status code: {}'.format(response.status_code)
            raise errors.HeartbeatError(msg)

    def lookup_node(self, hardware_info, timeout, starting_interval,
                    node_uuid=None):
        timer = loopingcall.BackOffLoopingCall(
            self._do_lookup,
            hardware_info=hardware_info,
            node_uuid=node_uuid)
        try:
            node_content = timer.start(starting_interval=starting_interval,
                                       timeout=timeout).wait()
        except loopingcall.LoopingCallTimeOut:
            raise errors.LookupNodeError('Could not look up node info. Check '
                                         'logs for details.')
        return node_content

    def _do_lookup(self, hardware_info, node_uuid):
        """The actual call to lookup a node.

        Should be called as a `loopingcall.BackOffLoopingCall`.
        """
        params = {
            'addresses': ','.join(iface.mac_address
                                  for iface in hardware_info['interfaces']
                                  if iface.mac_address)
        }
        if node_uuid:
            params['node_uuid'] = node_uuid

        try:
            response = self._request(
                'GET', self.lookup_api,
                headers=self._get_ironic_api_version_header(),
                params=params)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.HTTPError) as err:
            LOG.warning(
                'Error detected while attempting to perform lookup '
                'with %s, retrying. Error: %s', self.api_url, err
            )
            return False
        except Exception as err:
            # NOTE(TheJulia): If you're looking here, and you're wondering
            # why the retry logic is not working or your investigating a weird
            # error or even IPA just exiting,
            # See https://storyboard.openstack.org/#!/story/2007968
            # To be clear, we're going to try to provide as much detail as
            # possible in the exit handling
            msg = ('Unhandled error looking up node with addresses {} at '
                   '{}: {}'.format(params['addresses'], self.api_url, err))
            # No matter what we do at this point, IPA is going to exit.
            # This is because we don't know why the exception occured and
            # we likely should not try to retry as such.
            # We will attempt to provide as much detail to the logs as
            # possible as to what occured, although depending on the logging
            # subsystem, additional errors can occur, thus the additional
            # handling below.
            try:
                LOG.exception(msg)
                return False
            except Exception as exc_err:
                LOG.error(msg)
                exc_msg = ('Unexpected exception occured while trying to '
                           'log additional detail. Error: {}'.format(exc_err))
                LOG.error(exc_msg)
                raise errors.LookupNodeError(msg)

        if response.status_code != requests.codes.OK:
            LOG.warning(
                'Failed looking up node with addresses %r at %s, '
                'status code: %s',
                params['addresses'], self.api_url, response.status_code,
            )
            return False

        try:
            content = jsonutils.loads(response.content)
        except Exception as e:
            LOG.warning('Error decoding response: %s', e)
            return False

        # Check for valid response data
        if 'node' not in content or 'uuid' not in content['node']:
            LOG.warning(
                'Got invalid node data in response to query for node '
                'with addresses %r from %s: %s',
                params['addresses'], self.api_url, content,
            )
            return False

        if 'config' not in content:
            # Old API
            try:
                content['config'] = {'heartbeat_timeout':
                                     content.pop('heartbeat_timeout')}
            except KeyError:
                LOG.warning('Got invalid heartbeat from the API: %s', content)
                return False

        # Got valid content
        raise loopingcall.LoopingCallDone(retvalue=content)

    def _get_agent_url(self, advertise_address):
        return 'http://{}:{}'.format(netutils.wrap_ipv6(advertise_address[0]),
                                     advertise_address[1])
