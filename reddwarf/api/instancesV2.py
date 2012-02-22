# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 OpenStack LLC.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import copy
import json
from webob import exc

from nova import compute
from nova import db
from nova import exception as nova_exception
from nova import flags
from nova import log as logging
from nova import volume
from nova import utils
from nova.api.openstack import common as nova_common
from nova.api.openstack import faults
from nova.api.openstack import servers
from nova.api.openstack import wsgi
from nova.compute import power_state
from nova.compute import vm_states
from nova.notifier import api as notifier

from novaclient.v1_1 import servers as novaclientservers
import novaclient

from reddwarf import exception
from reddwarf.api import common
from reddwarf.api import deserializer
from reddwarf.api.views import instances
from reddwarf.db import api as dbapi
from reddwarf.guest import api as guest_api
from reddwarf.client import osclient


LOG = logging.getLogger('reddwarf.api.instances')
LOG.setLevel(logging.DEBUG)


FLAGS = flags.FLAGS
#flags.DEFINE_string('reddwarf_mysql_data_dir', '/var/lib/mysql',
# 'Mount point within the instance for MySQL data.')
#flags.DEFINE_string('reddwarf_volume_description',
# 'Volume ID: %s assigned to Instance: %s',
# 'Default description populated for volumes')
#flags.DEFINE_integer('reddwarf_max_accepted_volume_size', 128,
# 'Maximum accepted volume size (in gigabytes) when creating'
# ' an instance.')


def publisher_id(host=None):
    return notifier.publisher_id("reddwarf-api", host)


class ControllerV2(object):
    """ The Instance API controller for the Platform API """

    def __init__(self):
        self.compute_api = compute.API()
        self.guest_api = guest_api.API()
        self.server_controller = servers.ControllerV11()
        self.volume_api = volume.API()
        self.view = instances.ViewBuilder()
        self.client = osclient.OSClient(FLAGS.novaclient_account_id,
            FLAGS.novaclient_access_key,
            FLAGS.novaclient_project_id,
            'https://region-a.geo-1.identity.hpcloudsvc.com:35357/v2.0/tokens',
            FLAGS.novaclient_region_name)
        super(ControllerV2, self).__init__()

    def index(self, req):
        """ Returns a list of instance names and ids for a given user """
        LOG.info("Call to Instances index")
        LOG.debug("%s - %s", req.environ, req.body)


        # Insert Novaclient
        from novaclient.v1_1 import client
        try:
            nt = client.Client(self, "CHANGEME", "CHANGEME", "CHANGEME", "https://az-2.region-a.geo-1.compute.hpcloudsvc.com/v1.1/")
            server_list = nt.flavors.list()
        except:
            print "AAAAAAAA"

        servers_respose = self.server_controller.index(req)
        server_list = servers_response['servers']
        context = req.environ['nova.context']

        # Instances need the status for each instance in all circumstances,
        # unlike servers.
        server_states = db.instance_state_get_all_filtered(context)
        # for server in server_list:
        # state = server_states[server['id']]
        # server['status'] = nova_common.status_from_state(state)

        id_list = [server['id'] for server in server_list.list()]
        guest_state_mapping = self.get_guest_state_mapping(id_list)
        instances = [self.view.build_index(server, req, guest_state_mapping)
                     for server in server_list]
        return {'instances': instances}

    def detail(self, req):
        """ Returns a list of instance details for a given user """
        LOG.debug("%s - %s", req.environ, req.body)
        server_list = self.server_controller.detail(req)['servers']
        id_list = [server['id'] for server in server_list]
        guest_state_mapping = self.get_guest_state_mapping(id_list)
        instances = [self.view.build_detail(server, req, guest_state_mapping)
                     for server in server_list]
        return {'instances': instances}

    def xshow(self, req, id):
        """ Returns instance details by instance id """
        LOG.info("Get Instance by ID - %s", id)
        LOG.debug("%s - %s", req.environ, req.body)
        instance_id = dbapi.localid_from_uuid(id)
        LOG.debug("Local ID: " + str(instance_id))
        server_response = self.server_controller.show(req, instance_id)
        if isinstance(server_response, Exception):
            return server_response # Just return the exception to throw it
        context = req.environ['nova.context']
        server = server_response['server']

        guest_state = self.get_guest_state_mapping([server['id']])
        databases = None
        root_enabled = None
        if guest_state:
            databases, root_enabled = self._get_guest_info(context, server['id'],
                guest_state[server['id']])
        instance = self.view.build_single(server,
            req,
            guest_state,
            databases=databases,
            root_enabled=root_enabled)
        LOG.debug("instance - %s" % instance)
        return {'instance': instance}

    def show(self, req, id):
        """ Returns instance details by instance id """
        LOG.info("Get Instance by ID - %s", id)
        LOG.debug("%s - %s", req.environ, req.body)
        instance_id = dbapi.localid_from_uuid(id)
        LOG.debug("Local ID: " + str(instance_id))
        #server_response = self.server_controller.show(req, instance_id)
        server_response = self.client.show(id)
        if isinstance(server_response, Exception):
            return server_response # Just return the exception to throw it
        context = req.environ['nova.context']
        #server = server_response['server']

        guest_state = self.get_guest_state_mapping([server_response.id])
        databases = None
        root_enabled = None
        if guest_state:
            databases, root_enabled = self._get_guest_info(context, server_response.id,
                guest_state[server_response.id])
        instance = self.view.build_single(server_response,
            req,
            guest_state,
            databases=databases,
            root_enabled=root_enabled)
        LOG.debug("instance - %s" % instance)
        return {'instance': instance}

    def delete(self, req, id):
        """ Destroys an instance """
        LOG.info("Delete Instance by ID - %s", id)
        LOG.debug("%s - %s", req.environ, req.body)
        context = req.environ['nova.context']
        instance_id = dbapi.localid_from_uuid(id)

        # Checking the server state to see if it is building or not
        try:
            instance = self.compute_api.get(context, instance_id)
            #TODO(tim.simpson): Try to get this fixed for real in Nova.
            if instance['vm_state'] in [vm_states.SUSPENDED, vm_states.ERROR]:
                # SUSPENDED and ERROR are not valid 'shut_down' states to the
                # Compute API. But we want our customers to be able to delete
                # things in the event of failure, in which case we set the state to
                # SUSPENDED. Additionally as of 2011-10-12 ERROR is used in two
                # places: 1, the compute manager when a resize fails, or 2. by our
                # very own UnforgivingMemoryScheduler. So as of today ERROR is
                # also a viable state for deletion.
                db.instance_update(context, id, {'vm_state': vm_states.ACTIVE,})

            compute_response = self.compute_api.get(context, instance_id)
        except nova_exception.NotFound:
            raise exception.NotFound()
        LOG.debug("server_response - %s", compute_response)
        build_states = [
            nova_common.vm_states.REBUILDING,
            nova_common.vm_states.BUILDING,
            ]
        if compute_response['vm_state'] in build_states:
            # what if guest_state is failed and vm_state is still building
            # need to be able to delete instance still
            deletable_states = [
                power_state.FAILED,
                ]
            status = dbapi.guest_status_get(instance_id).state
            if not status in deletable_states:
                LOG.debug("guest status(%s) will not allow delete" % status)
                # If the state is building then we throw an exception back
                raise exception.UnprocessableEntity("Instance %s is not ready."
                % id)

        self.server_controller.delete(req, instance_id)
        #TODO(rnirmal): Use a deferred here to update status
        dbapi.guest_status_delete(instance_id)
        return exc.HTTPAccepted()

    def xcreate(self, req, body):
        """ Creates a new Instance for a given user """
        self._validate(body)

        LOG.info("Create Instance")
        LOG.debug("%s - %s", req.environ, body)

        context = req.environ['nova.context']

        # Create the Volume before hand
        volume_ref = self.create_volume(context, body)
        # Setup Security groups
        self._setup_security_groups(context,
            FLAGS.default_firewall_rule_name,
            FLAGS.default_guest_mysql_port)

        server = self._create_server_dict(body['instance'],
            volume_ref['id'],
            FLAGS.reddwarf_mysql_data_dir)

        # Add any extra data that's required by the servers api
        server_req_body = {'server':server}
        server_resp = self._try_create_server(req, server_req_body)
        instance_id = str(server_resp['server']['uuid'])
        local_id = str(server_resp['server']['id'])
        dbapi.guest_status_create(local_id)

        guest_state = self.get_guest_state_mapping([local_id])
        instance = self.view.build_single(server_resp['server'], req,
            guest_state, create=True)

        # add the volume information to response
        LOG.debug("adding the volume information to the response...")
        instance['volume'] = {'size': volume_ref['size']}
        return { 'instance': instance }

    def create(self, req, body):
        """ Creates a new Instance for a given user """
        self._validate(body)

        LOG.info("Create Instance")
        LOG.debug("%s - %s", req.environ, body)

        context = req.environ['nova.context']
        LOG.debug(" BODY Instance Name: " + body['instance']['name'])
        instance_name = body['instance']['name']

        # This should be fetched from Flags, image should contain mysqld and agent
        image_id = '397'
        flavor_ref = '102'

        # Create the Volume before hand
        # volume_ref = self.create_volume(context, body)
        # # Setup Security groups
        # self._setup_security_groups(context,
        # FLAGS.default_firewall_rule_name,
        # FLAGS.default_guest_mysql_port)
        #
        # server = self._create_server_dict(body['instance'],
        # volume_ref['id'],
        # FLAGS.reddwarf_mysql_data_dir)

        # Add any extra data that's required by the servers api
        #server_req_body = {'server':server}
        
        # Generate a UUID and set as the hostname, to guarantee unique
        # routing key for Agent
        host_name = str(utils.gen_uuid());
        
        server_resp = self._try_create_server(req, host_name, image_id, flavor_ref)
        
        #LOG.debug("Server_Response type: " + server_resp.getid(server_resp))
        from inspect import getmembers
        for name,thing in getmembers(server_resp):
            LOG.debug(" ----- " + str(name) + " : " + str(thing) )

        instance_id = server_resp.uuid
        local_id = str(server_resp.id)

        LOG.info("Created server with uuid: " + instance_id + " and local id: " + local_id)
        
        # Need to assign public IP, but also need to wait for Networking
        self.client.assign_public_ip(local_id)

        dbapi.instance_create(instance_name, server_resp)

        dbapi.guest_status_create(local_id)

        guest_state = self.get_guest_state_mapping([local_id])
        instance = self.view.build_single(server_resp, req,
            guest_state, create=True)

        # add the volume information to response
        LOG.debug("adding the volume information to the response...")
        #instance['volume'] = {'size': volume_ref['size']}
        return { 'instance': instance }

    @staticmethod
    def get_guest_state_mapping(id_list):
        """Returns a dictionary of guest statuses keyed by guest ids."""
        results = dbapi.guest_status_get_list(id_list)
        return dict([(r.instance_id, r.state) for r in results])

    def create_volume(self, context, body):
        """Creates the volume for the instance and returns its ID."""
        volume_size = body['instance']['volume']['size']
        name = body['instance'].get('name', None)
        description = FLAGS.reddwarf_volume_description % (None, None)

        return self.volume_api.create(context, size=volume_size,
            snapshot_id=None,
            name=name,
            description=description)

    def X_try_create_server(self, req, body):
        """Handle the call to create a server through the openstack servers api.

Separating this so we could do retries in the future and other
processing of the result etc.
"""
        try:
            server = self.server_controller.create(req, body)
            if not server or isinstance(server, faults.Fault)\
            or isinstance(server, exc.HTTPClientError):
                if isinstance(server, faults.Fault):
                    LOG.error("%s: %s", server.wrapped_exc,
                        server.wrapped_exc.detail)
                if isinstance(server, exc.HTTPClientError):
                    LOG.error("a 400 error occurred %s" % server)
                raise exception.InstanceFault("Could not complete the request."
                                              " Please try again later or contact Customer Support")
            return server
        except (TypeError, AttributeError, KeyError) as e:
            LOG.error(e)
            raise exception.UnprocessableEntity()

    def _try_create_server(self, req, instance_name, image_id, flavor_ref):
        """Handle the call to create a server through novaclient.

Separating this so we could do retries in the future and other
processing of the result etc.
"""
        try:

            server = self.client.create(instance_name, image_id, flavor_ref,'hpdefault',['default'])

            #server = self.server_controller.create(req, body)
            if not server or isinstance(server, faults.Fault)\
            or isinstance(server, exc.HTTPClientError):
                if isinstance(server, faults.Fault):
                    LOG.error("%s: %s", server.wrapped_exc,
                        server.wrapped_exc.detail)
                if isinstance(server, exc.HTTPClientError):
                    LOG.error("a 400 error occurred %s" % server)
                raise exception.InstanceFault("Could not complete the request."
                                              " Please try again later or contact Customer Support")
            return server
        except (TypeError, AttributeError, KeyError) as e:
            LOG.error(e)
            raise exception.UnprocessableEntity()

    @staticmethod
    def _create_server_dict(instance, volume_id, mount_point):
        """Creates a server dict from the request instance dict."""
        server = copy.copy(instance)
        # Append additional stuff to create.
        # Add image_ref
        try:
            server['imageRef'] = dbapi.config_get("reddwarf_imageref").value
        except nova_exception.ConfigNotFound:
            msg = "Cannot find the reddwarf_imageref config value, "\
                  "using default of 1"
            LOG.warn(msg)
            notifier.notify(publisher_id(), "reddwarf.image", notifier.WARN,
                msg)
            server['imageRef'] = 1
            # Add security groups
        security_groups = [{'name': FLAGS.default_firewall_rule_name}]
        server['security_groups'] = security_groups
        # Add volume id
        if not 'metadata' in instance:
            server['metadata'] = {}
        server['metadata']['volume_id'] = str(volume_id)
        # Add mount point
        server['metadata']['mount_point'] = str(mount_point)
        # Add databases
        # We create these once and throw away the result to take advantage
        # of the validators.
        db_list = common.populate_databases(instance.get('databases', []))
        server['metadata']['database_list'] = json.dumps(db_list)
        return server

    def _setup_security_groups(self, context, group_name, port):
        """ Setup a default firewall rule for reddwarf.

We are using the existing infrastructure of security groups in nova
used by the ec2 api and piggy back on it. Reddwarf by default will have
one rule which will allow access to the specified tcp port, the default
being 3306 from anywhere. For this the group_id and parent_id are the
same, we are not doing any hierarchical rules yet.
Here's how it would look in iptables.

-A nova-compute-inst-<id> -p tcp -m tcp --dport 3306 -j ACCEPT
"""
        self.compute_api.ensure_default_security_group(context)

        if not db.security_group_exists(context, context.project_id,
            group_name):
            LOG.debug('Creating a new firewall rule %s for project %s'
            % (group_name, context.project_id))
            values = {'name': group_name,
                      'description': group_name,
                      'user_id': context.user_id,
                      'project_id': context.project_id}
            security_group = db.security_group_create(context, values)
            rules = {'group_id': security_group['id'],
                     'parent_group_id': security_group['id'],
                     'cidr': '0.0.0.0/0',
                     'protocol': 'tcp',
                     'from_port': port,
                     'to_port': port}
            db.security_group_rule_create(context, rules)
            self.compute_api.trigger_security_group_rules_refresh(context,
                security_group['id'])

    def _get_guest_info(self, context, id, state):
        """Get the list of databases on a instance"""
        running = common.dbaas_mapping[power_state.RUNNING]
        if common.dbaas_mapping.get(state, None) == running:
            try:
                result = self.guest_api.list_databases(context, id)
                LOG.debug("LIST DATABASES RESULT - %s", str(result))
                databases = [{'name': db['_name'],
                              'collate': db['_collate'],
                              'character_set': db['_character_set']}
                for db in result]
                root_enabled = self.guest_api.is_root_enabled(context, id)
                return databases, root_enabled
            except Exception as err:
                LOG.error(err)
                LOG.error("guest not responding on instance %s" % id)
                #TODO(cp16net) we have hidden the actual exception by returning [],None
        return [], None

    @staticmethod
    def _validate(body):
        """Validate that the request has all the required parameters"""
        if not body:
            raise exception.BadRequest("The request contains an empty body")

        try:
            body['instance']
            body['instance']['flavorRef']
            try:
                volume_size = float(body['instance']['volume']['size'])
            except (ValueError, TypeError) as e:
                LOG.error("Create Instance Required field(s) - "
                          "['instance']['volume']['size']")
                raise exception.BadRequest("Required element/key - instance "
                                           "volume 'size' was not specified as a number")
            if int(volume_size) != volume_size or int(volume_size) < 1:
                raise exception.BadRequest("Volume 'size' needs to be a "
                                           "positive integer value, %s cannot be accepted."
                % volume_size)
            max_size = FLAGS.reddwarf_max_accepted_volume_size
            if int(volume_size) > max_size:
                raise exception.BadRequest("Volume 'size' cannot exceed maximum "
                                           "of %d Gb, %s cannot be accepted."
                % (max_size, volume_size))
        except KeyError as e:
            LOG.error("Create Instance Required field(s) - %s" % e)
            raise exception.BadRequest("Required element/key - %s was not "
                                       "specified" % e)


def create_resource(version='1.0'):
    controller = {
        '1.0': ControllerV2,
        }[version]()

    metadata = {
        'attributes': {
            'instance': ['created', 'hostname', 'id', 'name', 'rootEnabled',
                         'status', 'updated'],
            'dbtype': ['name', 'version'],
            'flavor': ['id'],
            'link': ['rel', 'href'],
            'volume': ['size'],
            'database': ['name', 'collate', 'character_set'],
            },
        }

    xmlns = {
        '1.0': common.XML_NS_V10,
        }[version]

    serializers = {
        'application/xml': wsgi.XMLDictSerializer(metadata=metadata,
            xmlns=xmlns),
        }

    deserializers = {
        'application/xml': deserializer.InstanceXMLDeserializer(),
        }

    response_serializer = wsgi.ResponseSerializer(body_serializers=serializers)
    request_deserializer = wsgi.RequestDeserializer(deserializers)
    return wsgi.Resource(controller, deserializer=request_deserializer,
        serializer=response_serializer)
