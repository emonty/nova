#    Copyright 2011 OpenStack LLC
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os


from nova import log as logging
from nova.api.openstack import common as nova_common
from nova.compute import power_state
from nova.exception import InstanceNotFound
from nova.notifier import api as notifier

from reddwarf.api import common
from reddwarf.api.views import flavors


LOG = logging.getLogger('reddwarf.api.views.instance')
LOG.setLevel(logging.DEBUG)


def _project_id(req):
    return getattr(req.environ['nova.context'], 'project_id', '')


def _base_url(req):
    return req.application_url


class ViewBuilder(object):
    """Views for an instance"""

    def x_build_basic(self, server, req, guest_states=None):
        """Build the very basic information for an instance"""
        instance = {}
        instance['id'] = server['uuid']
        instance['name'] = server['name']
        instance['status'] = self.get_instance_status(server, guest_states)
        instance['links'] = self._build_links(req, instance)
        return instance

    def _build_basic(self, server, req, guest_states=None):
        """Build the very basic information for an instance"""
        instance = {}
        instance['id'] = server.uuid
        instance['name'] = server.name
        instance['status'] = self.get_instance_status(server, guest_states)
        instance['links'] = self._build_links(req, instance)
        return instance

    def x_build_detail(self, server, req, instance):
        """Build out a more detailed view of the instance"""
        flavor_view = flavors.ViewBuilder(_base_url(req), _project_id(req))
        instance['flavor'] = server['flavor']
        instance['flavor']['links'] = flavor_view._build_links(instance['flavor'])
        instance['created'] = server['created']
        instance['updated'] = server['updated']
        # Add the hostname
        if 'hostname' in server:
            instance['hostname'] = server['hostname']

        # Add volume information
        dbvolume = self.build_volume(server)
        if dbvolume:
            instance['volume'] = dbvolume
        return instance

    def _build_detail(self, server, req, instance):
        """Build out a more detailed view of the instance"""
        flavor_view = flavors.ViewBuilder(_base_url(req), _project_id(req))
        instance['flavor'] = server.flavor['id']
        
        LOG.error("Flavor object: %s" % instance['flavor'])
        #instance['flavor']['links'] = flavor_view._build_links(instance['flavor'])
        instance['created'] = server.created
        instance['updated'] = server.updated
        # Add the hostname
        if server.hostId:
            instance['hostname'] = server.hostId

        # Add volume information
#        dbvolume = self.build_volume(server)
#        if dbvolume:
#            instance['volume'] = dbvolume
        return instance

    @staticmethod
    def _build_links(req, instance):
        """Build the links for the instance"""
        base_url = _base_url(req)
        href = os.path.join(base_url, _project_id(req),
                            "instances", str(instance['id']))
        bookmark = os.path.join(nova_common.remove_version_from_href(base_url),
                                "instances", str(instance['id']))
        links = [
            {
                'rel': 'self',
                'href': href
            },
            {
                'rel': 'bookmark',
                'href': bookmark
            }
        ]
        return links

    def build_index(self, server, req, guest_states):
        """Build the response for an instance index call"""
        return self._build_basic(server, req, guest_states)

    def build_detail(self, server, req, guest_states):
        """Build the response for an instance detail call"""
        instance = self._build_basic(server, req, guest_states)
        instance = self._build_detail(server, req, instance)
        return instance

    def build_single(self, server, req, guest_states, databases=None,
                     root_enabled=False, create=False):
        """
        Given a server (obtained from the servers API) returns an instance.
        """
        instance = self._build_basic(server, req, guest_states)
        instance = self._build_detail(server, req, instance)
        if not create:
            # Add Database and root_enabled
            instance['databases'] = databases
            instance['rootEnabled'] = root_enabled

        return instance

    @staticmethod
    def build_volume(server):
        """Given a server dict returns the instance volume dict."""
        try:
            volumes = server['volumes']
            volume_dict = volumes[0]
        except (KeyError, IndexError):
            return None
        if len(volumes) > 1:
            error_msg = {'instanceId': server['id'],
                         'msg': "> 1 volumes in the underlying instance!"}
            LOG.error(error_msg)
            notifier.notify(notifier.publisher_id("reddwarf-api"),
                            'reddwarf.instance.list', notifier.ERROR,
                            error_msg)
        return {'size': volume_dict['size']}

    @staticmethod
    def xget_instance_status(server, guest_states):
        """Figures out what the instance status should be.

        First looks at the server status, then to a dictionary mapping guest
        IDs to their states.

        """
        if server['status'] == 'ERROR':
            return 'ERROR'
        else:
            try:
                state = guest_states[server['id']]
            except (KeyError, InstanceNotFound):
                # we set the state to shutdown if not found
                state = power_state.SHUTDOWN
            return common.dbaas_mapping.get(state, None)

    @staticmethod
    def get_instance_status(server, guest_states):
        """Figures out what the instance status should be.

        First looks at the server status, then to a dictionary mapping guest
        IDs to their states.

        """
        if server.status == 'ERROR':
            return 'ERROR'
        else:
            try:
                #state = guest_states[server.id]
                state = server.status
            except (KeyError, InstanceNotFound):
                # we set the state to shutdown if not found
                state = power_state.SHUTDOWN
            return common.dbaas_mapping.get(state, None)


class MgmtViewBuilder(ViewBuilder):
    """Management views for an instance"""

    def __init__(self):
        super(MgmtViewBuilder, self).__init__()

    def build_mgmt_single(self, server, instance_ref, req, guest_states):
        """Build out the management view for a single instance"""
        instance = self._build_basic(server, req, guest_states)
        instance = self._build_detail(server, req, instance)
        instance = self._build_server_details(server, instance)
        instance = self._build_compute_api_details(instance_ref, instance)
        return instance

    def build_guest_info(self, instance, status=None, dbs=None, users=None,
                         root_enabled=None):
        """Build out all possible information for a guest"""
        instance['guest_status'] = self._build_guest_status(status)
        instance['databases'] = dbs
        instance['users'] = users
        root_history = self.build_root_history(instance['id'],
                                                       root_enabled)
        instance['root_enabled_at'] = root_history['root_enabled_at']
        instance['root_enabled_by'] = root_history['root_enabled_by']
        return instance

    def build_root_history(self, id, root_enabled):
        if root_enabled is not None:
            return {
                    'id': id,
                    'root_enabled_at': root_enabled.created_at,
                    'root_enabled_by': root_enabled.user_id}
        else:
            return {
                    'id': id,
                    'root_enabled_at': 'Never',
                    'root_enabled_by': 'Nobody'
                   }

    @staticmethod
    def _build_server_details(server, instance):
        """Build more information from the servers api"""
        instance['addresses'] = server['addresses']
        del instance['links']
        return instance

    @staticmethod
    def _build_compute_api_details(instance_ref, instance):
        """Build out additional information from the compute api"""
        instance['server_state_description'] = instance_ref['vm_state']
        instance['host'] = instance_ref['host']
        instance['account_id'] = instance_ref['user_id']
        return instance

    @staticmethod
    def _build_guest_status(status):
        """Build out the guest status information"""
        guest_status = {}
        if status is not None:
            guest_status['created_at'] = status.created_at
            guest_status['deleted'] = status.deleted
            guest_status['deleted_at'] = status.deleted_at
            guest_status['instance_id'] = status.instance_id
            guest_status['state'] = status.state
            guest_status['state_description'] = status.state_description
            guest_status['updated_at'] = status.updated_at
        return guest_status

    @staticmethod
    def build_volume(server):
        """Build out a more detailed volumes view"""
        if 'volumes' in server:
            volumes = server['volumes']
            volume_dict = volumes[0]
        else:
            volume_dict = None
        return volume_dict
